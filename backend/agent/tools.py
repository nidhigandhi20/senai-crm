"""
Agent Tools
===========
All tools available to the AgentReasoner's ReAct loop.

Each tool follows the same contract:
    async def tool_name(args..., db: Session) -> ToolResult

ToolResult always carries:
    - ok:      bool   — did the call succeed?
    - data:    dict   — structured payload for the agent's Observation
    - summary: str    — one-line human-readable summary for the reasoning trace

Tools implemented:
    1. get_thread_history(sender_email)       — all emails from a sender
    2. search_knowledge_base(query)           — RAG retrieval
    3. get_contact_profile(email)             — CRM lookup (VIP, ARR, churn risk)
    4. draft_reply(context, tone)             — LLM-generated reply draft
    5. escalate_to_human(email_id, reason)    — creates escalation record
    6. flag_for_legal(email_id, issue_type)   — legal/compliance flag + ticket
"""

import os
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Any
import httpx
from sqlalchemy.orm import Session

from db.models import Email, Contact, Action, Thread
from rag.pipeline import retrieve, format_rag_context

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")


# ─────────────────────────────────────────
# ToolResult
# ─────────────────────────────────────────

@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    summary: str = ""
    error: Optional[str] = None


# ─────────────────────────────────────────
# Tool 1 — get_thread_history
# ─────────────────────────────────────────

async def get_thread_history(
    sender_email: str,
    db: Session,
    limit: int = 20,
) -> ToolResult:
    """
    Fetches all emails from a sender, oldest first.
    Includes classification results if already classified.

    Used by the agent to understand a customer's full history,
    detect unanswered threads, and spot sentiment deterioration.
    """
    try:
        emails = (
            db.query(Email)
            .filter(Email.sender == sender_email)
            .order_by(Email.timestamp.asc())
            .limit(limit)
            .all()
        )

        if not emails:
            return ToolResult(
                ok=True,
                data={"emails": [], "count": 0},
                summary=f"No emails found from {sender_email}",
            )

        email_list = [
            {
                "message_id":     e.message_id,
                "subject":        e.subject or "(no subject)",
                "body_preview":   (e.body or "")[:300],
                "timestamp":      e.timestamp.isoformat() if e.timestamp else None,
                "category":       e.category,
                "sentiment_score": e.sentiment_score,
                "urgency":        e.urgency,
                "requires_human": e.requires_human,
                "status":         e.status,
            }
            for e in emails
        ]

        # Detect sentiment deterioration: 3+ consecutive negatives
        scores = [e.sentiment_score for e in emails if e.sentiment_score is not None]
        deteriorating = (
            len(scores) >= 3 and all(s < -0.2 for s in scores[-3:])
        )

        # Count unanswered emails (Received/Escalated with no reply action)
        unanswered = sum(
            1 for e in emails
            if e.status in ("Received", "Escalated") and not e.requires_human
        )

        return ToolResult(
            ok=True,
            data={
                "emails":               email_list,
                "count":                len(emails),
                "sentiment_deteriorating": deteriorating,
                "recent_scores":        scores[-5:] if scores else [],
                "unanswered_count":     unanswered,
            },
            summary=(
                f"{len(emails)} emails from {sender_email}. "
                f"Sentiment deteriorating: {deteriorating}. "
                f"Unanswered: {unanswered}."
            ),
        )

    except Exception as e:
        logger.error(f"get_thread_history failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"Error fetching thread history: {e}")


# ─────────────────────────────────────────
# Tool 2 — search_knowledge_base
# ─────────────────────────────────────────

async def search_knowledge_base(
    query: str,
    db: Session,
    top_k: int = 3,
) -> ToolResult:
    """
    RAG search over the knowledge base.
    Returns the top-K most relevant policy chunks.

    Used by the agent to look up pricing rules, SLA policies,
    refund terms, GDPR obligations, escalation procedures, etc.
    """
    try:
        chunks = retrieve(query, top_k=top_k, db=db)

        if not chunks:
            return ToolResult(
                ok=True,
                data={"chunks": [], "formatted": "No relevant policy context found."},
                summary="No relevant chunks found in knowledge base.",
            )

        chunk_list = [
            {
                "source_doc":       c.source_doc,
                "similarity_score": c.similarity_score,
                "text":             c.chunk_text,
            }
            for c in chunks
        ]

        return ToolResult(
            ok=True,
            data={
                "chunks":    chunk_list,
                "formatted": format_rag_context(chunks),
                "sources":   list({c.source_doc for c in chunks}),
            },
            summary=(
                f"Retrieved {len(chunks)} chunks from: "
                + ", ".join(f"{c.source_doc}({c.similarity_score})" for c in chunks)
            ),
        )

    except Exception as e:
        logger.error(f"search_knowledge_base failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"RAG search error: {e}")


# ─────────────────────────────────────────
# Tool 3 — get_contact_profile
# ─────────────────────────────────────────

async def get_contact_profile(
    email_address: str,
    db: Session,
) -> ToolResult:
    """
    Looks up a contact's CRM profile.
    Returns VIP status, ARR, churn risk, and recent activity.

    Used by the agent to decide escalation priority and
    tailor reply tone (VIP gets white-glove treatment).
    """
    try:
        contact = (
            db.query(Contact)
            .filter(Contact.email == email_address)
            .first()
        )

        if not contact:
            return ToolResult(
                ok=True,
                data={"found": False, "email": email_address},
                summary=f"No CRM profile found for {email_address}.",
            )

        # Derive a VIP flag from status or account value
        is_vip = (
            contact.status == "VIP"
            or (contact.account_value or 0) > 10_000
        )

        profile = {
            "found":            True,
            "email":            contact.email,
            "name":             contact.name,
            "company":          contact.company,
            "status":           contact.status,
            "account_value":    contact.account_value,
            "churn_risk_score": contact.churn_risk_score,
            "is_vip":           is_vip,
            "created_at":       contact.created_at.isoformat() if contact.created_at else None,
            "last_contact_at":  contact.last_contact_at.isoformat() if contact.last_contact_at else None,
        }

        risk_label = (
            "HIGH" if (contact.churn_risk_score or 0) > 0.7
            else "MEDIUM" if (contact.churn_risk_score or 0) > 0.4
            else "LOW"
        )

        return ToolResult(
            ok=True,
            data=profile,
            summary=(
                f"{contact.name or email_address} @ {contact.company or 'unknown company'}. "
                f"ARR=${contact.account_value or 0:,.0f}. "
                f"Churn risk: {risk_label} ({contact.churn_risk_score or 0:.2f}). "
                f"VIP: {is_vip}."
            ),
        )

    except Exception as e:
        logger.error(f"get_contact_profile failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"CRM lookup error: {e}")


# ─────────────────────────────────────────
# Tool 4 — draft_reply
# ─────────────────────────────────────────

async def draft_reply(
    context: str,
    tone: str = "professional",
    policy_context: str = "",
    contact_name: Optional[str] = None,
) -> ToolResult:
    """
    Generates a reply draft using the local Ollama LLM.

    Args:
        context:        Description of the situation + what the reply should address
        tone:           One of: professional, empathetic, firm, concise
        policy_context: Relevant policy text to reference (from RAG)
        contact_name:   Customer name for personalisation

    Returns:
        ToolResult with data["draft"] containing the reply text
    """
    greeting = f"Dear {contact_name}" if contact_name else "Dear Customer"

    system = """You are a senior customer success manager drafting a reply email.
Write clearly, professionally, and with empathy.
Be specific — reference exact policy details when provided.
Never make promises that aren't supported by the policy context.
Do NOT include a subject line. Start directly with the greeting.
Keep the reply under 200 words unless the situation demands more detail.
End with: Best regards,\nCustomer Success Team"""

    user = f"""Draft a {tone} email reply for the following situation:

SITUATION:
{context}

{f'RELEVANT POLICY:{chr(10)}{policy_context}' if policy_context else ''}

Start with: {greeting},
"""

    try:
        url = f"{OLLAMA_BASE_URL}/api/chat"
        payload = {
            "model":  OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "options": {"temperature": 0.3, "num_predict": 800},
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        draft = data["message"]["content"].strip()

        return ToolResult(
            ok=True,
            data={"draft": draft, "tone": tone},
            summary=f"Draft reply generated ({len(draft)} chars, tone={tone}).",
        )

    except Exception as e:
        logger.error(f"draft_reply failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"Draft generation error: {e}")


# ─────────────────────────────────────────
# Tool 5 — escalate_to_human
# ─────────────────────────────────────────

async def escalate_to_human(
    email_id: int,
    reason: str,
    escalation_target: str,
    db: Session,
    proposed_reply: Optional[str] = None,
) -> ToolResult:
    """
    Creates an escalation Action record and marks the email as Escalated.

    Args:
        email_id:          DB primary key of the email
        reason:            Why it's being escalated (goes into reasoning log)
        escalation_target: Who to escalate to (e.g. "customer_success@company.com")
        proposed_reply:    Optional draft the human can edit before sending
    """
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            return ToolResult(ok=False, error=f"Email {email_id} not found", summary="Escalation failed — email not found.")

        email.status       = "Escalated"
        email.requires_human = True
        db.commit()

        action = Action(
            email_id=email_id,
            agent_reasoning_log=[{
                "step":        "escalate_to_human",
                "thought":     f"Escalating to {escalation_target}",
                "action":      "escalate_to_human()",
                "observation": {
                    "reason":             reason,
                    "escalation_target":  escalation_target,
                    "has_proposed_reply": proposed_reply is not None,
                },
            }],
            action_type="Escalate",
            proposed_content=proposed_reply,
            is_approved=False,
        )
        db.add(action)
        db.commit()

        return ToolResult(
            ok=True,
            data={
                "email_id":          email_id,
                "escalation_target": escalation_target,
                "reason":            reason,
            },
            summary=f"Escalated email {email_id} to {escalation_target}. Reason: {reason}",
        )

    except Exception as e:
        logger.error(f"escalate_to_human failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"Escalation error: {e}")


# ─────────────────────────────────────────
# Tool 6 — flag_for_legal
# ─────────────────────────────────────────

async def flag_for_legal(
    email_id: int,
    issue_type: str,
    db: Session,
    notes: str = "",
) -> ToolResult:
    """
    Flags an email for the legal/compliance team and creates an Action record.

    issue_type examples: "GDPR-Article20", "GDPR-Article17", "ransomware",
                         "cease-and-desist", "litigation-threat", "HIPAA-BAA"

    Used for: GDPR requests, legal threats, compliance obligations.
    Always sets requires_human=True and creates a Legal-Flag action.
    """
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            return ToolResult(ok=False, error=f"Email {email_id} not found", summary="Legal flag failed — email not found.")

        email.status        = "Escalated"
        email.requires_human = True
        db.commit()

        legal_note = (
            f"[LEGAL FLAG] Issue type: {issue_type}. "
            + (f"Notes: {notes}" if notes else "")
        )

        action = Action(
            email_id=email_id,
            agent_reasoning_log=[{
                "step":        "flag_for_legal",
                "thought":     f"This email requires legal/compliance review: {issue_type}",
                "action":      "flag_for_legal()",
                "observation": {
                    "issue_type": issue_type,
                    "notes":      notes,
                    "target":     "legal@company.com",
                },
            }],
            action_type="Legal-Flag",
            proposed_content=legal_note,
            is_approved=False,
        )
        db.add(action)
        db.commit()

        return ToolResult(
            ok=True,
            data={
                "email_id":   email_id,
                "issue_type": issue_type,
                "target":     "legal@company.com",
            },
            summary=f"Legal flag created for email {email_id}. Issue: {issue_type}. Routed to legal@company.com.",
        )

    except Exception as e:
        logger.error(f"flag_for_legal failed: {e}", exc_info=True)
        return ToolResult(ok=False, error=str(e), summary=f"Legal flag error: {e}")