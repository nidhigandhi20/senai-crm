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
    - GDPR/Compliance → always flag_for_legal() + create_internal_ticket()
      + draft GDPR acknowledgement citing 30-day statutory window
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
    create_internal_ticket,
)

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "llama3.2")
MAX_STEPS       = 6

# Whitelist of valid tool names — agent cannot hallucinate tools
VALID_TOOLS = {
    "get_thread_history",
    "search_knowledge_base",
    "get_contact_profile",
    "draft_reply",
    "escalate_to_human",
    "flag_for_legal",
    "create_internal_ticket",
    "DONE",
}


# ─────────────────────────────────────────
# GDPR acknowledgement template
# Generated once for hard-rule GDPR path — never auto-sent,
# proposed_reply is preserved for human review before dispatch.
# ─────────────────────────────────────────

GDPR_ACKNOWLEDGEMENT_TEMPLATE = """Dear {name},

Thank you for your request regarding your personal data rights under the General Data Protection Regulation (GDPR).

We have received your request and wish to confirm the following:

• **Request type:** {request_type}
• **Received:** {received_date}
• **Statutory deadline:** {deadline_date} (30 calendar days from receipt, per GDPR Article 12(3))

We are obligated under GDPR to respond to your request within 30 days. If your request is complex or we receive a high volume of requests, we may extend this period by a further two months — we will notify you if this applies.

**What happens next:**
1. Our Data Protection team will review your request within 5 business days.
2. We may need to verify your identity before processing the request.
3. You will receive a full response by the statutory deadline above.

If you have any questions in the meantime, please contact our Data Protection Officer at dpo@company.com.

Best regards,
Data Protection Team

---
*This is an automated acknowledgement. A member of our compliance team will follow up with a substantive response before the deadline stated above.*
"""


# ─────────────────────────────────────────
# Agent system prompt
# ─────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are an autonomous customer support agent operating in a ReAct loop.
You receive a classified email and decide what actions to take.

## Available Tools (ONLY these 7 — never invent tool names)

1. get_thread_history
   Input: {"sender_email": "<email>"}
   → Fetch all prior emails from this sender. Always call this first.

2. search_knowledge_base
   Input: {"query": "<search terms>"}
   → RAG search over internal policy docs (pricing, SLA, refunds, compliance, escalation).
   ALWAYS call this before drafting a reply. Use exact policy text in your draft.

3. get_contact_profile
   Input: {"email": "<email>"}
   → Fetch CRM data: name, company, ARR, churn risk score, VIP status.

4. draft_reply
   Input: {"context": "<detailed situation>", "tone": "<tone>", "policy_context": "<exact policy text>", "contact_name": "<name or null>"}
   → Generate a reply draft. ONLY call if category is NOT Legal/Compliance/Spam and urgency is NOT Critical.
   The "context" field MUST be plain English — NOT a JSON string or dict.
   It MUST include: plan name, seat counts, dollar amounts, discount rates, days remaining,
   and the fully worked calculation. Do NOT use placeholder text.

5. escalate_to_human
   Input: {"email_id": <id>, "reason": "<reason>", "escalation_target": "<team@company.com>"}
   → Mark email as Escalated. Use for VIP churn risk, unanswered complaints, high-value accounts.
   Escalation targets by category:
     - Billing / pricing questions      → support@company.com
     - Chatbot discrepancy reports      → product@company.com
     - Churn / retention / complaints   → customer_success@company.com
     - Critical urgency                 → ops@company.com
     - Legal / compliance matters       → legal@company.com

6. flag_for_legal
   Input: {"email_id": <id>, "issue_type": "<type>", "notes": "<notes>"}
   → Flag for legal/compliance. ONLY use for: GDPR, HIPAA, legal threats, cease-and-desist, ransomware.
   NEVER call flag_for_legal for category=Complaint, even if the customer is angry or threatening a review.

7. create_internal_ticket
   Input: {"title": "<title>", "body": "<details>", "assignee": "<team or email>", "priority": "<low|medium|high|critical>"}
   → Create an internal support/engineering/compliance ticket.
   Use for: GDPR requests (assignee=dpo@company.com), bug reports (assignee=engineering@company.com),
   feature requests (assignee=product@company.com),
   billing questions needing ops review (assignee=support@company.com).

## Decision Rules (NEVER violate these)

- category = Legal OR Compliance     → call flag_for_legal(), then DONE
- urgency = Critical                 → call escalate_to_human(), then DONE. NO reply.
- category = Spam                    → DONE immediately. No tools needed. No reply.
- GDPR / Article 20 / right to erasure / data portability → flag_for_legal(issue_type="GDPR")
- VIP customer (account_value > $10k) with churn risk > 0.7 → escalate_to_human()
- category = Complaint with requires_human = true → escalate_to_human(), optionally also draft_reply
- NEVER call flag_for_legal for category=Complaint. Complaints are NOT legal matters.

## Special Scenario Rules

### Chatbot Misinformation (customer says chatbot gave wrong info)
- NEVER say "our chatbot made an error" or "our chatbot was wrong" (liability risk)
- DO acknowledge confusion and cite the ACTUAL policy from search_knowledge_base
- DO offer a goodwill gesture if appropriate (credit, not a refund if outside 14 days)
- MUST escalate_to_human with escalation_target="product@company.com" and note about chatbot discrepancy
- Final decision: BOTH draft_reply AND escalate_to_human

### Complaint with sentiment deterioration (3+ negative emails)
- MUST escalate_to_human to customer_success@company.com
- Draft reply MUST include a specific retention offer (e.g. "We'd like to offer you one month free")
- NEVER flag_for_legal for complaints about unanswered emails or review threats
- Tone must be empathetic and apologetic — NEVER defensive or blame the customer

### Pro-rata billing inquiry (mid-cycle seat additions)
- MUST call get_thread_history first to find plan, seat count, and discount from prior emails
- MUST call search_knowledge_base("pro-rata billing non-profit discount") for the formula
- escalate_to_human with escalation_target="support@company.com" for billing questions
- The draft_reply context MUST be plain English — NOT a JSON string or dict.
- The context MUST include: current plan, current seats, per-seat price, discount %, days remaining,
  the number of NEW seats being added, and the fully worked calculation.
- CRITICAL: Only charge for NEW seats, not existing seats.
- CRITICAL: Formula is new_seats × per_seat_price × (days_remaining / total_days) × (1 - discount_rate)
  Do NOT use a "price difference" — that formula is for plan upgrades only, NOT seat additions.
- Show the worked calculation in the context, e.g.:
  "5 new seats × $12/seat × (15/30 days) × 70% (after 30% non-profit discount) = $21.00"
- Do NOT just say "pro-rata billing applies" — include the exact numbers and result.

## Output Format

Each step respond with ONLY a JSON object — no markdown fences, no preamble:

{
  "thought": "<your reasoning>",
  "action": "<tool_name>",
  "action_input": { "<param>": "<value>" }
}

When finished:

{
  "thought": "<final reasoning>",
  "action": "DONE",
  "action_input": {},
  "final_decision": "<Auto-Reply|Escalate|Legal-Flag|Ignored>",
  "decision_reason": "<one sentence summary>"
}

IMPORTANT: If you called both draft_reply and escalate_to_human, set final_decision = "Escalate"
(human review needed even though a draft exists). The proposed_reply will be preserved for the human.
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

        # ── MALFORMED PAYLOAD GUARD ───────────────────────────────────
        # The agent relies on category, urgency, sender, and timestamp
        # throughout the ReAct loop.  Catch missing/corrupt values before
        # they cause confusing mid-loop failures.
        _payload_error = _validate_agent_email(email)
        if _payload_error:
            logger.error(
                f"[Agent][VALIDATION] Email {email.message_id} has a malformed payload: "
                f"{_payload_error} — escalating without entering the loop."
            )
            if not dry_run:
                email.status         = "Escalated"
                email.requires_human = True
                self.db.commit()
                action = Action(
                    email_id=email_id,
                    agent_reasoning_log=[{
                        "step":        0,
                        "thought":     f"Malformed email payload: {_payload_error}",
                        "action":      "DONE",
                        "observation": {"error": _payload_error},
                    }],
                    action_type="Escalate",
                    proposed_content=None,
                    is_approved=False,
                )
                self.db.add(action)
                self.db.commit()
            return {
                "email_id":        email_id,
                "message_id":      getattr(email, "message_id", str(email_id)),
                "final_decision":  "Escalate",
                "decision_reason": f"Malformed payload — {_payload_error}",
                "dry_run":         dry_run,
                "steps":           0,
                "reasoning_trace": [{
                    "step":        0,
                    "thought":     f"Payload validation failed: {_payload_error}",
                    "action":      "DONE",
                    "observation": {"error": _payload_error},
                }],
                "proposed_reply":  None,
                "is_safe":         True,
                "validation_error": _payload_error,
            }

        # ── IDEMPOTENCY CHECK ──────────────────────────────────────────
        # Prevent the agent from running twice on the same email (e.g. from
        # retry storms or duplicate webhook deliveries).  An email that
        # already has an Action row with action_type in the terminal set has
        # been fully processed; running again would create duplicate tickets,
        # duplicate escalations, and duplicate legal flags.
        _TERMINAL_ACTION_TYPES = {"Auto-Reply", "Escalate", "Legal-Flag", "Ignored"}
        existing_action = (
            self.db.query(Action)
            .filter(
                Action.email_id   == email_id,
                Action.action_type.in_(_TERMINAL_ACTION_TYPES),
            )
            .first()
        )
        if existing_action and not dry_run:
            logger.info(
                "[Agent][IDEMPOTENCY] "
                f"Email {email.message_id} already has a "
                f"{existing_action.action_type} action "
                f"(id={existing_action.id}) — skipping duplicate agent run."
            )
            return {
                "email_id":        email_id,
                "message_id":      email.message_id,
                "final_decision":  existing_action.action_type,
                "decision_reason": "Skipped — email already processed (idempotency guard).",
                "dry_run":         dry_run,
                "steps":           0,
                "reasoning_trace": [],
                "proposed_reply":  existing_action.proposed_content,
                "is_safe":         True,
                "idempotent_skip": True,
            }

        logger.info(
            f"[Agent] Starting run for email {email.message_id} "
            f"(dry_run={dry_run})"
        )

        # ── Safety gate: check hard rules before entering the loop ────
        hard_decision = await self._check_hard_safety_rules(email, dry_run)
        if hard_decision:
            logger.info(
                f"[Agent] Hard safety rule fired: {hard_decision['final_decision']}"
            )
            if not dry_run:
                await self._commit_hard_decision(email, hard_decision)
            return {**hard_decision, "email_id": email_id, "dry_run": dry_run, "steps": 0}

        # ── Build initial context for the LLM ─────────────────────────
        context = await self._build_initial_context(email)
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

            thought      = parsed.get("thought", "")
            action_name  = parsed.get("action", "DONE")
            action_input = parsed.get("action_input", {})

            # ── Validate tool name (prevent hallucination) ────────────
            if action_name not in VALID_TOOLS:
                logger.warning(
                    f"[Agent] Invalid tool '{action_name}' at step {step}. "
                    f"Valid tools: {', '.join(sorted(VALID_TOOLS))}"
                )
                # Force DONE → Escalate on invalid tool
                action_name  = "DONE"
                final_decision  = "Escalate"
                decision_reason = (
                    f"Agent invoked invalid tool '{parsed.get('action', 'unknown')}' at step {step}. "
                    "Escalating for human review."
                )
                reasoning_trace.append({
                    "step":        step,
                    "thought":     thought + " [SAFETY: Invalid tool detected]",
                    "action":      "DONE",
                    "observation": {
                        "error": f"Invalid tool: {parsed.get('action', 'unknown')}",
                        "valid_tools": list(sorted(VALID_TOOLS)),
                    },
                })
                break

            # ── Post-parse safety override: block flag_for_legal on non-legal categories ──
            # The LLM occasionally confuses billing questions, complaints, or other
            # categories with legal/compliance matters and calls flag_for_legal
            # inappropriately.  Only Legal and Compliance emails may be flagged.
            # Hard-block for everything else regardless of LLM output.
            _LEGAL_FLAGGABLE_CATEGORIES = {"Legal", "Compliance"}
            if action_name == "flag_for_legal" and (email.category or "") not in _LEGAL_FLAGGABLE_CATEGORIES:
                logger.warning(
                    f"[Agent] Blocked flag_for_legal call on {email.category} email {email_id} — "
                    "only Legal/Compliance categories may be flagged; overriding to escalate_to_human."
                )
                action_name  = "escalate_to_human"
                # Choose the most appropriate escalation target by category
                _category_escalation_map = {
                    "Billing":   "support@company.com",
                    "Inquiry":   "support@company.com",
                    "Complaint": "customer_success@company.com",
                }
                _override_target = _category_escalation_map.get(
                    email.category or "", "ops@company.com"
                )
                action_input = {
                    "email_id":          email_id,
                    "reason":            (
                        f"{email.category} email incorrectly routed to legal — "
                        "overrode erroneous flag_for_legal call"
                    ),
                    "escalation_target": _override_target,
                }
                # Patch the thought so the trace is honest
                thought = (
                    thought
                    + f" [SAFETY OVERRIDE: flag_for_legal blocked for {email.category} category; "
                    f"redirected to escalate_to_human → {_override_target}]"
                )

            # ── Post-parse safety override: correct wrong escalation target for billing ──
            # Prevent the LLM from routing billing questions to product@company.com.
            if (
                action_name == "escalate_to_human"
                and (email.category or "") in ("Billing", "Inquiry")
                and action_input.get("escalation_target") == "product@company.com"
            ):
                logger.warning(
                    f"[Agent] Corrected escalation_target from product@company.com to "
                    f"support@company.com for {email.category} email {email_id}."
                )
                action_input = {
                    **action_input,
                    "escalation_target": "support@company.com",
                }
                thought = (
                    thought
                    + " [SAFETY OVERRIDE: product@company.com is for chatbot reports only; "
                    "billing escalation redirected to support@company.com]"
                )

            # ── DONE ──────────────────────────────────────────────────
            if action_name == "DONE":
                final_decision  = parsed.get("final_decision", "Escalate")
                decision_reason = parsed.get("decision_reason", "Agent concluded.")

                # Post-loop safety: if a draft was produced but decision is Auto-Reply
                # and email requires_human, downgrade to Escalate so a human reviews.
                if final_decision == "Auto-Reply" and email.requires_human:
                    final_decision  = "Escalate"
                    decision_reason = (
                        decision_reason
                        + " [Override: requires_human=true, downgraded Auto-Reply → Escalate]"
                    )

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
                "step":         step,
                "thought":      thought,
                "action":       action_name,
                "action_input": action_input,
                "observation":  {
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
            "message_id":      email.message_id,
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

    async def _check_hard_safety_rules(
        self, email: Email, dry_run: bool = False
    ) -> Optional[dict]:
        """
        Checks hard-coded rules that bypass the ReAct loop entirely.
        Returns a decision dict if a rule fires, None otherwise.

        Rules checked:
          - Spam       → Ignored
          - Legal      → Legal-Flag + security escalation
          - Compliance → Legal-Flag + create_internal_ticket + GDPR acknowledgement draft
          - Critical urgency → Escalate, no reply

        NOTE: Complaint is intentionally NOT handled here — complaints
        go through the full ReAct loop so the agent can assess churn risk,
        draft a retention reply, and escalate with full context.

        IMPORTANT: The GDPR acknowledgement in proposed_reply is NEVER
        auto-sent. It is preserved for human review so the DPO can verify
        the response before dispatch.
        """
        category = email.category or ""
        urgency  = email.urgency  or ""

        # ── Spam ──────────────────────────────────────────────────────
        if category == "Spam":
            return {
                "final_decision":  "Ignored",
                "decision_reason": "Spam email — no action taken.",
                "reasoning_trace": [{
                    "step":    0,
                    "thought": "Hard rule: Spam → Ignored immediately. No reply, no ticket.",
                    "action":  "DONE",
                    "observation": {"final_decision": "Ignored"},
                }],
                "proposed_reply": None,
                "is_safe":        True,
                "steps":          0,
            }

        # ── Compliance (GDPR/HIPAA/CCPA) ──────────────────────────────
        # Special handling: flag_for_legal + create_internal_ticket +
        # generate a GDPR acknowledgement draft (not auto-sent).
        if category == "Compliance":
            gdpr_draft = self._build_gdpr_acknowledgement(email)

            # Create compliance ticket (dry-run: skip DB write)
            ticket_result = None
            ticket_summary = "[DRY RUN] Would create compliance ticket for DPO team."
            if not dry_run:
                ticket_result = await create_internal_ticket(
                    title=f"GDPR Request — {email.sender} — {email.subject or 'Data Rights Request'}",
                    body=(
                        f"Sender: {email.sender}\n"
                        f"Message ID: {email.message_id}\n"
                        f"Received: {email.timestamp.isoformat() if email.timestamp else 'unknown'}\n"
                        f"Statutory deadline: 30 days from receipt\n\n"
                        f"Email body:\n{(email.body or '')[:1000]}\n\n"
                        "Action required: Verify identity, process data subject request, "
                        "respond within statutory window."
                    ),
                    assignee="dpo@company.com",
                    priority="high",
                    db=self.db,
                )
                ticket_summary = ticket_result.summary if ticket_result else "Ticket creation failed."

            return {
                "final_decision":  "Legal-Flag",
                "decision_reason": (
                    "Compliance (GDPR/data rights) request — auto-flagged for legal/compliance team. "
                    "Internal ticket created for DPO. Acknowledgement draft prepared (not sent — "
                    "awaiting DPO identity verification before dispatch)."
                ),
                "reasoning_trace": [
                    {
                        "step":    0,
                        "thought": (
                            "Hard rule: Compliance → flag_for_legal immediately. "
                            "GDPR Article 12(3) requires acknowledgement within 30 days. "
                            "Must NOT auto-reply — identity verification required first."
                        ),
                        "action":       "flag_for_legal",
                        "action_input": {"issue_type": "GDPR"},
                        "observation":  {"summary": "Hard safety rule — no LLM loop needed."},
                    },
                    {
                        "step":    0,
                        "thought": (
                            "Creating internal compliance ticket so DPO team is notified "
                            "and can begin processing within the statutory 30-day window."
                        ),
                        "action":       "create_internal_ticket",
                        "action_input": {"assignee": "dpo@company.com", "priority": "high"},
                        "observation":  {"summary": ticket_summary},
                    },
                    {
                        "step":    0,
                        "thought": (
                            "Generating GDPR acknowledgement draft. This is NOT sent automatically. "
                            "DPO must verify sender identity before dispatching any response."
                        ),
                        "action":      "draft_gdpr_acknowledgement",
                        "action_input": {},
                        "observation":  {
                            "summary": "GDPR acknowledgement draft prepared for DPO review.",
                            "note":    "NOT auto-sent. Human review required before dispatch.",
                        },
                    },
                ],
                "proposed_reply": gdpr_draft,   # preserved for DPO, never auto-sent
                "is_safe":        True,
                "steps":          0,
            }

        # ── Legal (threats, ransomware, cease-and-desist) ─────────────
        if category == "Legal":
            return {
                "final_decision":  "Legal-Flag",
                "decision_reason": "Legal category — auto-flagged for security & legal team. No reply sent.",
                "reasoning_trace": [{
                    "step":    0,
                    "thought": (
                        "Hard rule: Legal → flag_for_legal immediately. "
                        "NO auto-reply. NO LLM loop. Route to security@company.com and legal@company.com."
                    ),
                    "action":       "flag_for_legal",
                    "action_input": {"issue_type": "legal-threat"},
                    "observation":  {
                        "summary": "Hard safety rule — no LLM loop needed.",
                        "escalation_targets": ["security@company.com", "legal@company.com"],
                    },
                }],
                "proposed_reply": None,   # NEVER reply to legal threats
                "is_safe":        True,
                "steps":          0,
            }

        # ── Critical urgency ──────────────────────────────────────────
        if urgency == "Critical":
            return {
                "final_decision":  "Escalate",
                "decision_reason": "Critical urgency — escalated to ops team. No auto-reply.",
                "reasoning_trace": [{
                    "step":    0,
                    "thought": (
                        "Hard rule: Critical urgency → escalate immediately, no reply. "
                        "Human must handle this."
                    ),
                    "action":       "escalate_to_human",
                    "action_input": {"reason": "Critical urgency hard rule"},
                    "observation":  {"summary": "Hard safety rule — no LLM loop needed."},
                }],
                "proposed_reply": None,
                "is_safe":        True,
                "steps":          0,
            }

        return None

    def _build_gdpr_acknowledgement(self, email: Email) -> str:
        """
        Builds a GDPR acknowledgement draft from the template.
        Infers request type from email subject/body keywords.
        Calculates the 30-day statutory deadline from email timestamp.
        """
        from datetime import timedelta

        # Infer request type from email content
        body_lower  = (email.body or "").lower()
        subject_low = (email.subject or "").lower()
        combined    = body_lower + " " + subject_low

        if any(k in combined for k in ["article 20", "portability", "data portability", "export my data"]):
            request_type = "Data Portability Request (GDPR Article 20)"
        elif any(k in combined for k in ["article 17", "erasure", "right to be forgotten", "delete my data"]):
            request_type = "Right to Erasure Request (GDPR Article 17)"
        elif any(k in combined for k in ["article 15", "access", "subject access", "what data"]):
            request_type = "Data Subject Access Request (GDPR Article 15)"
        elif any(k in combined for k in ["article 16", "rectification", "correct my data", "update my data"]):
            request_type = "Right to Rectification Request (GDPR Article 16)"
        else:
            request_type = "Data Subject Rights Request (GDPR)"

        # Dates
        received_dt  = email.timestamp if email.timestamp else datetime.utcnow()
        deadline_dt  = received_dt + timedelta(days=30)
        received_str = received_dt.strftime("%d %B %Y")
        deadline_str = deadline_dt.strftime("%d %B %Y")

        # Try to extract a name from sender email
        name = email.sender.split("@")[0].replace(".", " ").replace("_", " ").title()

        return GDPR_ACKNOWLEDGEMENT_TEMPLATE.format(
            name=name,
            request_type=request_type,
            received_date=received_str,
            deadline_date=deadline_str,
        )


    async def _commit_hard_decision(self, email: Email, decision: dict) -> None:
        """Persists a hard-rule decision to DB without going through the ReAct loop."""
        final = decision["final_decision"]

        if final == "Legal-Flag":
            issue = "GDPR" if email.category == "Compliance" else "legal-threat"
            await flag_for_legal(
                email_id=email.id,
                issue_type=issue,
                db=self.db,
                notes=(
                    "Auto-flagged by hard safety rule. "
                    + ("GDPR acknowledgement draft prepared for DPO review."
                       if email.category == "Compliance" else "")
                ),
            )

        elif final in ("Escalate", "Ignored"):
            email.status         = "Escalated" if final == "Escalate" else "Ignored"
            email.requires_human = (final == "Escalate")
            self.db.commit()

            action = Action(
                email_id=email.id,
                agent_reasoning_log=decision["reasoning_trace"],
                action_type=final,
                proposed_content=decision.get("proposed_reply"),
                is_approved=False,
            )
            self.db.add(action)
            self.db.commit()


    # ─────────────────────────────────────────
    # Initial context builder
    # ─────────────────────────────────────────

    async def _build_initial_context(self, email: Email) -> str:
        """
        Builds the first user message for the ReAct loop.

        For billing/inquiry emails we pre-load thread history and inject
        it here so the LLM has the full context (plan, seats, discounts)
        before step 1 — this prevents generic draft_reply calls that
        lack the numbers needed for pro-rata calculations.
        """
        base = f"""You are processing the following classified email.

## Email Details
- Email ID:    {email.id}
- Message ID:  {email.message_id}
- Sender:      {email.sender}
- Subject:     {email.subject or '(no subject)'}
- Timestamp:   {email.timestamp.isoformat() if email.timestamp else 'unknown'}
- Body:        {(email.body or '')[:800]}

## Classification Results (from classifier engine)
- Category:       {email.category}
- Urgency:        {email.urgency}
- Sentiment:      {email.sentiment_score}
- Requires Human: {email.requires_human}
- Confidence:     {email.confidence}
"""

        # ── Pre-load thread history for Billing / Inquiry / Complaint ─
        # These categories often need prior context (plan, seats, discounts,
        # or prior unanswered emails) to produce accurate replies.
        if email.category in ("Billing", "Inquiry", "Complaint"):
            prior = (
                self.db.query(Email)
                .filter(
                    Email.sender == email.sender,
                    Email.id != email.id,
                )
                .order_by(Email.timestamp.asc())
                .limit(10)
                .all()
            )
            if prior:
                thread_lines = []
                for e in prior:
                    thread_lines.append(
                        f"  [{e.timestamp.strftime('%Y-%m-%d') if e.timestamp else '?'}] "
                        f"{e.sender}: {(e.body or e.subject or '')[:300]}"
                    )
                base += "\n## Prior Thread History (most relevant for context)\n"
                base += "\n".join(thread_lines)

                if email.category in ("Billing", "Inquiry"):
                    base += (
                        "\n\nIMPORTANT: Use the thread history above to extract the plan name, "
                        "current seat count, pricing, and any discounts. Include exact numbers in "
                        "your draft_reply context — no placeholder text, no policy links.\n"
                        "The draft_reply context MUST be plain English, NOT a JSON string.\n"
                        "\n"
                        "PRO-RATA SEAT ADDITION RULES:\n"
                        "  1. Charge NEW seats only.\n"
                        "  2. Two steps required:\n"
                        "       Step 1 undiscounted: new_seats x per_seat_price x (days_remaining / total_days)\n"
                        "       Step 2 discounted:   Step1_result x (1 - discount_rate)\n"
                        "  3. NEVER present the undiscounted subtotal as the final charge.\n"
                        "  4. Do NOT use a price-difference formula — that is for plan upgrades only.\n"
                        "\n"
                        "Correct example context string:\n"
                        "  Customer Alice is on the Standard plan (10 existing seats, $12/seat, "
                        "30% non-profit discount). She wants to add 5 new seats mid-cycle with "
                        "15 of 30 days remaining. "
                        "Step 1 undiscounted: 5 x $12 x (15/30) = $30.00. "
                        "Step 2 after 30% discount: $30.00 x 0.70 = $21.00. Total due: $21.00\n"
                    )
                elif email.category == "Complaint":
                    # Count unanswered emails for the complaint context
                    unanswered = sum(
                        1 for e in prior
                        if e.status in ("Received", "Escalated")
                    )
                    if unanswered >= 2:
                        base += (
                            f"\n\n⚠ WARNING: This customer has sent {len(prior)} prior email(s) "
                            f"with {unanswered} appearing unanswered. "
                            f"Sentiment deterioration detected. "
                            "The draft reply MUST be empathetic, apologetic, and include a "
                            "specific retention offer (e.g. one month free). "
                            "Do NOT be defensive. Do NOT blame the customer. "
                            "MUST escalate_to_human to customer_success@company.com.\n"
                        )

        # ── Chatbot misinformation hint ───────────────────────────────
        body_lower = (email.body or "").lower()
        if "chatbot" in body_lower or "bot told me" in body_lower or "ai said" in body_lower:
            base += """
## Special Handling Required: Chatbot Misinformation
The customer claims our chatbot gave them incorrect information.
Required steps:
1. search_knowledge_base to find the ACTUAL policy
2. draft_reply that:
   - Acknowledges confusion WITHOUT saying "chatbot was wrong" (liability)
   - Cites the actual policy clearly
   - Offers a goodwill gesture if the customer acted on the wrong info
3. escalate_to_human(escalation_target="product@company.com") with a note about the chatbot
Final decision must be "Escalate" (human review required for chatbot discrepancy reports).
"""

        base += "\n## Your Task\nDecide what action to take. Start your ReAct loop now.\n"
        return base


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
        In dry_run mode, write operations (escalate, flag, ticket) are skipped
        and a mock result is returned instead.

        Unknown tool names return a clear error so the LLM can recover
        gracefully rather than wasting steps on a silent no-op.
        """
        db = self.db

        # ── Read-only tools — always execute ─────────────────────────
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
            # FIX 1: Pass all optional kwargs from tool_input so sentiment_deteriorating
            # is never left undefined — it has a safe default of False in draft_reply().
            return await draft_reply(
                context=tool_input.get("context", ""),
                tone=tool_input.get("tone", "professional"),
                policy_context=tool_input.get("policy_context", ""),
                contact_name=tool_input.get("contact_name"),
                sender_email=tool_input.get("sender_email"),
                company_name=tool_input.get("company_name"),
                sentiment_deteriorating=tool_input.get("sentiment_deteriorating", False),
                db=db,
            )

        # ── Write tools — skip in dry_run ─────────────────────────────
        if tool_name == "escalate_to_human":
            # Validate required fields — the LLM occasionally passes wrong keys
            # (e.g. "category", "customer_id") instead of the required ones.
            # Coerce to safe defaults and log so the trace is honest.
            _esc_email_id = tool_input.get("email_id", email_id)
            _esc_reason   = tool_input.get("reason", "")
            _esc_target   = tool_input.get("escalation_target", "")

            if not _esc_reason:
                logger.warning(
                    f"[Agent] escalate_to_human called without 'reason' — using default. "
                    f"Raw input: {tool_input}"
                )
                _esc_reason = f"Agent escalation for {email.category or 'unknown'} email (reason not provided)"

            if not _esc_target:
                logger.warning(
                    f"[Agent] escalate_to_human called without 'escalation_target' — "
                    f"defaulting by category '{email.category}'. Raw input: {tool_input}"
                )
                _category_escalation_defaults = {
                    "Billing":   "support@company.com",
                    "Inquiry":   "support@company.com",
                    "Complaint": "customer_success@company.com",
                    "Legal":     "legal@company.com",
                    "Compliance":"legal@company.com",
                }
                _esc_target = _category_escalation_defaults.get(
                    email.category or "", "ops@company.com"
                )

            if dry_run:
                return ToolResult(
                    ok=True,
                    data={"dry_run": True},
                    summary=f"[DRY RUN] Would escalate to {_esc_target}.",
                )
            return await escalate_to_human(
                email_id=_esc_email_id,
                reason=_esc_reason,
                escalation_target=_esc_target,
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

        if tool_name == "create_internal_ticket":
            if dry_run:
                return ToolResult(
                    ok=True,
                    data={"dry_run": True, "ticket_id": "DRY-RUN-TICKET"},
                    summary=(
                        f"[DRY RUN] Would create ticket: '{tool_input.get('title', 'untitled')}' "
                        f"→ {tool_input.get('assignee', 'unassigned')} "
                        f"(priority={tool_input.get('priority', 'medium')})."
                    ),
                )
            return await create_internal_ticket(
                title=tool_input.get("title", "Internal Ticket"),
                body=tool_input.get("body", ""),
                assignee=tool_input.get("assignee", "support@company.com"),
                priority=tool_input.get("priority", "medium"),
                db=db,
            )

        # ── Unknown tool ──────────────────────────────────────────────
        return ToolResult(
            ok=False,
            error=f"Unknown tool: '{tool_name}'",
            summary=(
                f"Tool '{tool_name}' does not exist. "
                f"Valid tools: {', '.join(sorted(VALID_TOOLS - {'DONE'}))}, or DONE to finish. "
                "If you want to end the loop, use action='DONE' with final_decision and decision_reason."
            ),
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

        status_map = {
            "Auto-Reply": "Resolved",
            "Escalate":   "Escalated",
            "Legal-Flag": "Escalated",
            "Ignored":    "Ignored",
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

# ─────────────────────────────────────────
# Agent-level payload validator
# ─────────────────────────────────────────

def _validate_agent_email(email) -> "str | None":
    """
    Validates that an Email row has the fields the agent needs to run safely.

    Returns None if everything looks good, or an error string describing
    the first problem found.  This is intentionally stricter than the
    engine-level validator because the agent relies on category, urgency,
    sender, and timestamp throughout the ReAct loop and in the hard-rule gate.

    Called at the very top of AgentReasoner.run() before any tool is invoked.
    """
    import re as _re

    # sender — required for get_thread_history and get_contact_profile
    if not email.sender or not email.sender.strip():
        return "sender is missing or empty"
    if not _re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.sender.strip()):
        return f"sender '{email.sender}' is not a valid email address"

    # message_id — required for idempotency key in logs / action records
    if not email.message_id or not email.message_id.strip():
        return "message_id is missing — idempotency key unavailable"

    # category — the hard-safety gate branches on this; None causes a silent bypass
    if not email.category or email.category.strip() == "":
        return "category is None — email was not classified before agent ran"

    # urgency — the hard-safety gate and tool-routing both depend on this
    if not email.urgency or email.urgency.strip() == "":
        return "urgency is None — cannot safely route without urgency"

    # timestamp — used for thread ordering and GDPR deadline calculation
    if email.timestamp is None:
        return "timestamp is None — required for thread ordering and GDPR deadlines"

    return None  # all checks passed