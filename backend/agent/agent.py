"""
Autonomous Agent — ReAct Loop
==============================
Implements the Thought → Action → Observation loop (max 6 steps) that
decides what to do with a classified email.

Architecture:
    classify_email()  →  AgentReasoner.run()  →  Action record in DB

The agent uses the classification output (category, urgency, etc.)
as its starting context, then calls tools in a loop until it reaches
a final decision.

Safety contract (hard-coded, never overrideable by LLM):
    - Never auto-reply if urgency = Critical
    - Never auto-reply if category in (Legal, Compliance, Spam)
    - GDPR/Compliance → always flag_for_legal()
    - Reasoning trace written to agent_reasoning_log for every decision

Usage:
    from agent.agent import AgentReasoner
    from db.database import SessionLocal

    db = SessionLocal()
    agent = AgentReasoner(db)
    result = await agent.run(email_id=42)
    # or in dry-run mode (plans but does not write to DB):
    result = await agent.run(email_id=42, dry_run=True)
"""

import os
import json
import re
import logging
from datetime import datetime
from typing import Optional, Any
import httpx
from sqlalchemy.orm import Session

from db.models import Email, Action, Contact
from db.database import SessionLocal
from classifier.schemas import ClassificationResult
from agent.tools import (
    ToolResult,
    get_thread_history,
    search_knowledge_base,
    get_contact_profile,
    draft_reply,
    escalate_to_human,
    flag_for_legal,
)

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "llama3.2")
MAX_STEPS       = 6


# ─────────────────────────────────────────
# Agent system prompt
# ─────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are an autonomous customer support agent operating in a ReAct loop.
You receive a classified email and decide what actions to take.

## Available Tools

1. get_thread_history(sender_email)
   → Fetch all prior emails from this sender. Use to understand context and detect unanswered threads.

2. search_knowledge_base(query)
   → RAG search over internal policy docs (pricing, SLA, refunds, compliance, escalation).
   Use whenever you need to cite or verify a policy before drafting a reply.

3. get_contact_profile(email)
   → Fetch CRM data: name, company, ARR, churn risk score, VIP status.
   Use to calibrate response priority and personalise the reply.

4. draft_reply(context, tone, policy_context, contact_name)
   → Generate a reply draft using the policy context you have gathered.
   Only call this if it is safe to reply (not Legal, not Critical).

5. escalate_to_human(email_id, reason, escalation_target)
   → Mark email as Escalated and create an escalation record.
   Use for VIP churn risk, unanswered complaint threads, high-value accounts.

6. flag_for_legal(email_id, issue_type, notes)
   → Flag for legal/compliance review. ALWAYS use for GDPR, HIPAA, legal threats.

## Decision Rules (NEVER violate these)

- category = Legal OR Compliance     → call flag_for_legal(), then DONE
- urgency = Critical                 → call escalate_to_human(), then DONE. NO reply.
- category = Spam                    → DONE immediately. No tools needed. No reply.
- GDPR / Article 20 / right to erasure / data portability → flag_for_legal(issue_type="GDPR")
- VIP customer (account_value > $10k) with churn risk > 0.7 → escalate_to_human()

## Output Format

Each step you must respond with ONLY a JSON object:

{
  "thought": "<your reasoning about the current situation>",
  "action": "<tool_name>",
  "action_input": {
    "<param>": "<value>",
    ...
  }
}

OR, when you are done:

{
  "thought": "<final reasoning>",
  "action": "DONE",
  "action_input": {},
  "final_decision": "<Auto-Reply|Escalate|Legal-Flag|Ignored>",
  "decision_reason": "<one sentence summary>"
}

Respond with ONLY the JSON. No preamble, no explanation, no markdown fences.
"""


# ─────────────────────────────────────────
# AgentReasoner
# ─────────────────────────────────────────

class AgentReasoner:
    """
    Runs the ReAct (Reason + Act) loop for a single email.

    The loop:
      1. Build context from the classified email
      2. Call LLM to decide next action (Thought + Action)
      3. Execute the tool (Observation)
      4. Append to trace and repeat (max MAX_STEPS times)
      5. When LLM returns action=DONE, commit the final decision

    dry_run=True runs the full loop but skips all DB writes —
    useful for the /agent/dry-run API endpoint.
    """

    def __init__(self, db: Session):
        self.db = db

    async def run(
        self,
        email_id: int,
        dry_run: bool = False,
    ) -> dict:
        """
        Main entry point.

        Args:
            email_id: DB primary key of the email to process
            dry_run:  If True, plan actions but don't write to DB

        Returns:
            {
                "email_id":       int,
                "final_decision": str,
                "decision_reason": str,
                "dry_run":        bool,
                "steps":          int,
                "reasoning_trace": [...],
                "proposed_reply": str | None,
                "is_safe":        bool,
            }
        """
        email = self.db.query(Email).filter(Email.id == email_id).first()
        if not email:
            raise ValueError(f"Email {email_id} not found")

        logger.info(
            f"[Agent] Starting run for email {email.message_id} "
            f"(dry_run={dry_run})"
        )

        # ── Safety gate: check hard rules before entering the loop ────
        hard_decision = self._check_hard_safety_rules(email)
        if hard_decision:
            logger.info(
                f"[Agent] Hard safety rule fired: {hard_decision['final_decision']}"
            )
            if not dry_run:
                await self._commit_hard_decision(email, hard_decision)
            return {**hard_decision, "email_id": email_id, "dry_run": dry_run, "steps": 0}

        # ── Build initial context for the LLM ─────────────────────────
        context = self._build_initial_context(email)
        messages = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user",   "content": context},
        ]

        reasoning_trace = []
        proposed_reply: Optional[str] = None
        final_decision = "Escalate"       # safe default
        decision_reason = "Max steps reached without conclusion"

        # ── ReAct loop ────────────────────────────────────────────────
        for step in range(1, MAX_STEPS + 1):
            logger.info(f"[Agent] Step {step}/{MAX_STEPS}")

            # LLM decides next action
            raw = await self._call_llm(messages)
            parsed = _parse_agent_response(raw)

            thought        = parsed.get("thought", "")
            action_name    = parsed.get("action", "DONE")
            action_input   = parsed.get("action_input", {})

            # ── DONE ──────────────────────────────────────────────────
            if action_name == "DONE":
                final_decision  = parsed.get("final_decision", "Escalate")
                decision_reason = parsed.get("decision_reason", "Agent concluded.")
                reasoning_trace.append({
                    "step":        step,
                    "thought":     thought,
                    "action":      "DONE",
                    "observation": {"final_decision": final_decision, "reason": decision_reason},
                })
                break

            # ── Execute tool ──────────────────────────────────────────
            tool_result = await self._execute_tool(
                action_name, action_input, email_id, dry_run
            )

            # Capture draft if tool produced one
            if action_name == "draft_reply" and tool_result.ok:
                proposed_reply = tool_result.data.get("draft")

            # Append step to trace
            step_record = {
                "step":        step,
                "thought":     thought,
                "action":      action_name,
                "action_input": action_input,
                "observation": {
                    "ok":      tool_result.ok,
                    "summary": tool_result.summary,
                    "data":    tool_result.data,
                    **({"error": tool_result.error} if tool_result.error else {}),
                },
            }
            reasoning_trace.append(step_record)
            logger.info(f"[Agent] Step {step} — {action_name}: {tool_result.summary}")

            # Feed observation back into the conversation
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    f"Tool result for {action_name}:\n"
                    f"OK: {tool_result.ok}\n"
                    f"Summary: {tool_result.summary}\n"
                    f"Data: {json.dumps(tool_result.data, default=str)}\n\n"
                    "Continue the ReAct loop. What is your next action?"
                ),
            })

        # ── Persist reasoning trace ───────────────────────────────────
        if not dry_run:
            await self._persist_result(
                email=email,
                final_decision=final_decision,
                decision_reason=decision_reason,
                reasoning_trace=reasoning_trace,
                proposed_reply=proposed_reply,
            )

        result = {
            "email_id":        email_id,
            "final_decision":  final_decision,
            "decision_reason": decision_reason,
            "dry_run":         dry_run,
            "steps":           len(reasoning_trace),
            "reasoning_trace": reasoning_trace,
            "proposed_reply":  proposed_reply,
            "is_safe":         True,
        }

        logger.info(
            f"[Agent] Completed email {email.message_id}: "
            f"decision={final_decision}, steps={len(reasoning_trace)}"
        )
        return result


    # ─────────────────────────────────────────
    # Hard safety rules (pre-loop gate)
    # ─────────────────────────────────────────

    def _check_hard_safety_rules(self, email: Email) -> Optional[dict]:
        """
        Checks hard-coded rules that bypass the ReAct loop entirely.
        Returns a decision dict if a rule fires, None otherwise.

        Rules checked:
          - Spam → Ignored
          - Legal → Legal-Flag
          - Compliance (GDPR etc.) → Legal-Flag
          - Critical urgency → Escalate, no reply
        """
        category = email.category or ""
        urgency  = email.urgency  or ""

        if category == "Spam":
            return {
                "final_decision":  "Ignored",
                "decision_reason": "Spam email — no action taken.",
                "reasoning_trace": [{"step": 0, "thought": "Hard rule: Spam → Ignored", "action": "DONE"}],
                "proposed_reply":  None,
                "is_safe":         True,
                "steps":           0,
            }

        if category in ("Legal", "Compliance"):
            issue = "GDPR" if category == "Compliance" else "legal-threat"
            return {
                "final_decision":  "Legal-Flag",
                "decision_reason": f"{category} category — auto-flagged for legal/compliance team.",
                "reasoning_trace": [{
                    "step":    0,
                    "thought": f"Hard rule: {category} → flag_for_legal immediately",
                    "action":  "flag_for_legal",
                    "action_input": {"issue_type": issue},
                    "observation": {"summary": "Hard safety rule — no LLM loop needed."},
                }],
                "proposed_reply": None,
                "is_safe":        True,
                "steps":          0,
            }

        if urgency == "Critical":
            return {
                "final_decision":  "Escalate",
                "decision_reason": "Critical urgency — escalated to ops team. No auto-reply.",
                "reasoning_trace": [{
                    "step":    0,
                    "thought": "Hard rule: Critical urgency → escalate immediately, no reply",
                    "action":  "escalate_to_human",
                    "action_input": {"reason": "Critical urgency hard rule"},
                    "observation": {"summary": "Hard safety rule — no LLM loop needed."},
                }],
                "proposed_reply": None,
                "is_safe":        True,
                "steps":          0,
            }

        return None


    async def _commit_hard_decision(self, email: Email, decision: dict) -> None:
        """Persists a hard-rule decision to DB without going through the ReAct loop."""
        final = decision["final_decision"]

        if final == "Legal-Flag":
            issue = "GDPR" if email.category == "Compliance" else "legal-threat"
            await flag_for_legal(
                email_id=email.id,
                issue_type=issue,
                db=self.db,
                notes="Auto-flagged by hard safety rule.",
            )

        elif final in ("Escalate", "Ignored"):
            email.status = "Escalated" if final == "Escalate" else "Ignored"
            email.requires_human = (final == "Escalate")
            self.db.commit()

            action = Action(
                email_id=email.id,
                agent_reasoning_log=decision["reasoning_trace"],
                action_type=final,
                proposed_content=None,
                is_approved=False,
            )
            self.db.add(action)
            self.db.commit()


    # ─────────────────────────────────────────
    # Initial context builder
    # ─────────────────────────────────────────

    def _build_initial_context(self, email: Email) -> str:
        """
        Builds the first user message for the ReAct loop —
        everything the agent needs to start reasoning.
        """
        return f"""You are processing the following classified email.

## Email Details
- Email ID:    {email.id}
- Message ID:  {email.message_id}
- Sender:      {email.sender}
- Subject:     {email.subject or '(no subject)'}
- Timestamp:   {email.timestamp.isoformat() if email.timestamp else 'unknown'}
- Body Preview: {(email.body or '')[:500]}

## Classification Results (from classifier engine)
- Category:       {email.category}
- Urgency:        {email.urgency}
- Sentiment:      {email.sentiment_score}
- Requires Human: {email.requires_human}
- Confidence:     {email.confidence}

## Your Task
Decide what action to take for this email.
Use the available tools to gather context before making a decision.
Remember: check the thread history and contact profile before drafting a reply.

Start your ReAct loop now.
"""


    # ─────────────────────────────────────────
    # Tool dispatcher
    # ─────────────────────────────────────────

    async def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        email_id: int,
        dry_run: bool,
    ) -> ToolResult:
        """
        Dispatches a tool call by name.
        In dry_run mode, write operations (escalate, flag) are skipped
        and a mock result is returned instead.
        """
        db = self.db

        # Read-only tools — always execute
        if tool_name == "get_thread_history":
            return await get_thread_history(
                sender_email=tool_input.get("sender_email", ""),
                db=db,
            )

        if tool_name == "search_knowledge_base":
            return await search_knowledge_base(
                query=tool_input.get("query", ""),
                db=db,
            )

        if tool_name == "get_contact_profile":
            return await get_contact_profile(
                email_address=tool_input.get("email", tool_input.get("email_address", "")),
                db=db,
            )

        if tool_name == "draft_reply":
            return await draft_reply(
                context=tool_input.get("context", ""),
                tone=tool_input.get("tone", "professional"),
                policy_context=tool_input.get("policy_context", ""),
                contact_name=tool_input.get("contact_name"),
            )

        # Write tools — skip in dry_run
        if tool_name == "escalate_to_human":
            if dry_run:
                return ToolResult(
                    ok=True,
                    data={"dry_run": True},
                    summary=f"[DRY RUN] Would escalate to {tool_input.get('escalation_target', 'ops team')}.",
                )
            return await escalate_to_human(
                email_id=email_id,
                reason=tool_input.get("reason", "Agent decision"),
                escalation_target=tool_input.get("escalation_target", "ops@company.com"),
                db=db,
                proposed_reply=tool_input.get("proposed_reply"),
            )

        if tool_name == "flag_for_legal":
            if dry_run:
                return ToolResult(
                    ok=True,
                    data={"dry_run": True},
                    summary=f"[DRY RUN] Would flag for legal: {tool_input.get('issue_type')}.",
                )
            return await flag_for_legal(
                email_id=email_id,
                issue_type=tool_input.get("issue_type", "unknown"),
                db=db,
                notes=tool_input.get("notes", ""),
            )

        # Unknown tool
        return ToolResult(
            ok=False,
            error=f"Unknown tool: {tool_name}",
            summary=f"Tool '{tool_name}' not found.",
        )


    # ─────────────────────────────────────────
    # LLM call
    # ─────────────────────────────────────────

    async def _call_llm(self, messages: list[dict]) -> str:
        """Calls Ollama with the current conversation history."""
        url = f"{OLLAMA_BASE_URL}/api/chat"
        payload = {
            "model":  OLLAMA_MODEL,
            "stream": False,
            "messages": messages,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 1000},
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return data["message"]["content"].strip()


    # ─────────────────────────────────────────
    # Persist final result
    # ─────────────────────────────────────────

    async def _persist_result(
        self,
        email: Email,
        final_decision: str,
        decision_reason: str,
        reasoning_trace: list,
        proposed_reply: Optional[str],
    ) -> None:
        """Writes the final Action record and updates the email status."""

        # Map decision → email status
        status_map = {
            "Auto-Reply":  "Resolved",
            "Escalate":    "Escalated",
            "Legal-Flag":  "Escalated",
            "Ignored":     "Ignored",
        }
        email.status         = status_map.get(final_decision, "Escalated")
        email.requires_human = final_decision != "Auto-Reply"
        self.db.commit()

        action = Action(
            email_id=email.id,
            agent_reasoning_log=reasoning_trace,
            action_type=final_decision,
            proposed_content=proposed_reply,
            is_approved=False,
            executed_at=datetime.utcnow() if final_decision == "Auto-Reply" else None,
        )
        self.db.add(action)
        self.db.commit()

        logger.info(
            f"[Agent] Persisted: email {email.id} → status={email.status}, "
            f"action={final_decision}"
        )


# ─────────────────────────────────────────
# JSON parsing helper
# ─────────────────────────────────────────

def _parse_agent_response(raw: str) -> dict:
    """
    Parses the LLM's JSON response for one ReAct step.
    Strips markdown fences if present.
    Falls back to a safe DONE+Escalate on any parse error.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"[Agent] Failed to parse LLM response: {e}\nRaw: {raw[:300]}")
        return {
            "thought":         f"Parse error — defaulting to escalation. Raw: {raw[:100]}",
            "action":          "DONE",
            "action_input":    {},
            "final_decision":  "Escalate",
            "decision_reason": f"Agent parse error: {e}",
        }


# ─────────────────────────────────────────
# Convenience runner
# ─────────────────────────────────────────

async def run_agent(
    email_id: int,
    dry_run: bool = False,
    db: Optional[Session] = None,
) -> dict:
    """
    Module-level convenience function.
    Creates its own DB session if one isn't provided.

    Example:
        result = await run_agent(email_id=42)
        result = await run_agent(email_id=42, dry_run=True)
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        agent = AgentReasoner(db)
        return await agent.run(email_id=email_id, dry_run=dry_run)
    finally:
        if close_db:
            db.close()