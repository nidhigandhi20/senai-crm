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
    7. create_internal_ticket(title, body, assignee, priority)
                                              — creates support/compliance/eng ticket

Integrations:
    - SentimentTracker:  called inside get_thread_history to detect
                         deterioration and enrich the observation with an
                         alert when 3+ consecutive negatives are found.
    - WebScraper:        called inside draft_reply when churn-risk signals
                         are present (sentiment_deteriorating=True OR the
                         context mentions "review"/"Trustpilot"/"G2").
                         The scraped reputation data is injected into the
                         LLM prompt so the draft can reference it.
"""

import os
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
import httpx
from sqlalchemy.orm import Session

from db.models import Email, Contact, Action, Thread
from rag.pipeline import retrieve, format_rag_context
from sentiment.tracker import SentimentTracker
from intelligence.scraper import WebScraper

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

# Initialise module-level singletons (stateless, safe to share)
_sentiment_tracker = SentimentTracker()
_web_scraper       = WebScraper()


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

    ── Tracker integration ──────────────────────────────────────────────
    After loading emails, SentimentTracker.update_rolling_average() is
    called so the contact's stored sentiment trend stays current.
    detect_deterioration() is then called to emit an escalation alert
    when 3+ consecutive negatives are found.  The alert is surfaced in
    the ToolResult data so the ReAct loop can decide whether to escalate.
    ─────────────────────────────────────────────────────────────────────
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
                "message_id":      e.message_id,
                "subject":         e.subject or "(no subject)",
                "body_preview":    (e.body or "")[:300],
                "timestamp":       e.timestamp.isoformat() if e.timestamp else None,
                "category":        e.category,
                "sentiment_score": e.sentiment_score,
                "urgency":         e.urgency,
                "requires_human":  e.requires_human,
                "status":          e.status,
            }
            for e in emails
        ]

        # ── SentimentTracker: update rolling average ──────────────────
        scores = [e.sentiment_score for e in emails if e.sentiment_score is not None]

        if scores:
            _sentiment_tracker.update_rolling_average(sender_email, scores, db)

        # ── SentimentTracker: detect deterioration ────────────────────
        deteriorating = _sentiment_tracker.detect_deterioration(sender_email, db)

        # Emit an escalation alert record when deterioration is found
        escalation_alert = None
        if deteriorating:
            escalation_alert = _sentiment_tracker.create_escalation_alert(
                sender_email=sender_email,
                reason="3+ consecutive negative emails detected",
                severity="High",
                db=db,
            )
            logger.warning(
                f"[SentimentTracker] Deterioration alert raised for {sender_email}"
            )

        # Count unanswered emails (Received/Escalated with no approved reply)
        unanswered = sum(
            1 for e in emails
            if e.status in ("Received", "Escalated") and not e.requires_human
        )

        return ToolResult(
            ok=True,
            data={
                "emails":                  email_list,
                "count":                   len(emails),
                "sentiment_deteriorating": deteriorating,
                "recent_scores":           scores[-5:] if scores else [],
                "unanswered_count":        unanswered,
                "escalation_alert":        escalation_alert,
            },
            summary=(
                f"{len(emails)} emails from {sender_email}. "
                f"Sentiment deteriorating: {deteriorating}. "
                f"Unanswered: {unanswered}."
                + (" ⚠ ESCALATION ALERT raised." if escalation_alert else "")
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
                + ", ".join(f"{c.source_doc}({c.similarity_score:.2f})" for c in chunks)
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
            "HIGH"   if (contact.churn_risk_score or 0) > 0.7
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

# Keywords that signal the agent should fetch web reputation data
# before drafting, so the reply can be informed by public sentiment.
_REPUTATION_TRIGGER_KEYWORDS = {
    "review", "trustpilot", "g2", "g2crowd", "public review",
    "post publicly", "social media", "twitter", "linkedin",
}


async def draft_reply(
    context: str,
    tone: str = "professional",
    policy_context: str = "",
    contact_name: Optional[str] = None,
    # Extra kwargs the agent may pass
    sender_email: Optional[str] = None,
    company_name: Optional[str] = None,
    sentiment_deteriorating: bool = False,
    db: Optional[Session] = None,
) -> ToolResult:
    """
    Generates a reply draft using the local Ollama LLM.

    ── Scraper integration ───────────────────────────────────────────────
    Web reputation data is fetched (via WebScraper) BEFORE calling the
    LLM when either of these conditions is true:

      1. sentiment_deteriorating=True  — tracker already confirmed 3+
         consecutive negatives; we need public sentiment context.
      2. The context string contains review-threat keywords
         (e.g. "Trustpilot", "G2", "post a review").

    The reputation payload (G2 rating, Trustpilot score, key themes) is
    injected into the LLM prompt so the draft can reference it and the
    agent's reasoning trace shows exactly what intelligence was used.
    ─────────────────────────────────────────────────────────────────────

    Args:
        context:                 Description of the situation (MUST include specific
                                 numbers: plan name, seats, prices, discount %, days remaining)
        tone:                    professional | empathetic | firm | concise
        policy_context:          Relevant policy text from RAG
        contact_name:            Customer name for personalisation
        sender_email:            Sender address (used to look up company if
                                 company_name is not provided)
        company_name:            Company to scrape reputation for
        sentiment_deteriorating: Flag from get_thread_history observation
        db:                      DB session (needed for scraper cache)

    Returns:
        ToolResult with data["draft"] and optional data["web_intel"]
    """
    greeting = f"Dear {contact_name}" if contact_name else "Dear Customer"

    # ── Decide whether to fetch web intelligence ──────────────────────
    context_lower = context.lower()
    needs_web_intel = sentiment_deteriorating or any(
        kw in context_lower for kw in _REPUTATION_TRIGGER_KEYWORDS
    )

    web_intel_section = ""
    web_intel_payload = None

    if needs_web_intel and (company_name or sender_email):
        target = company_name or (sender_email.split("@")[-1].split(".")[0] if sender_email else "unknown")
        logger.info(f"[draft_reply] Fetching web reputation for: {target}")

        try:
            web_intel_payload = await _web_scraper.get_reputation(
                company_name=target,
                db=db,
            )

            # Build a concise summary for the LLM prompt
            g2    = web_intel_payload.get("g2_rating")
            tp    = web_intel_payload.get("trustpilot")
            note  = web_intel_payload.get("note", "")
            themes = web_intel_payload.get("themes", [])

            intel_lines = [f"Company: {target}"]
            if g2:
                intel_lines.append(f"G2 rating: {g2}/5")
            if tp:
                intel_lines.append(f"Trustpilot score: {tp}/5")
            if themes:
                intel_lines.append(f"Common review themes: {', '.join(themes)}")
            if note:
                intel_lines.append(f"Note: {note}")

            web_intel_section = (
                "\nMARKET INTELLIGENCE (public reputation data):\n"
                + "\n".join(intel_lines)
                + "\n"
            )

        except Exception as exc:
            logger.warning(f"[draft_reply] Web intel fetch failed (non-fatal): {exc}")
            web_intel_section = "\nMARKET INTELLIGENCE: Not available at this time.\n"

    # ── Build prompt ──────────────────────────────────────────────────
    system = """You are a senior customer success manager drafting a reply email.
Write clearly, professionally, and with empathy.
Be specific — reference exact policy details, dollar amounts, and seat counts when provided.
Never make promises that aren't supported by the policy context.
Do NOT include a subject line. Start directly with the greeting.
Keep the reply under 250 words unless the situation demands more detail.

CRITICAL SAFETY RULES:
- NEVER say "our chatbot made an error" or "the chatbot was wrong" — liability risk.
- NEVER say "we take full responsibility" for operational issues — liability risk.
- NEVER fabricate technical details (e.g. "new caching system", "server infrastructure fix").
- NEVER blame the customer for not responding or not understanding.

IMPORTANT TONE RULES:
- If unanswered emails or churn threat: be empathetic and apologetic, NOT defensive.
- Acknowledge their frustration: "I understand your frustration"
- Use: "We appreciate your patience" instead of admitting fault.
- Use: "Let me clarify our policy" instead of "our system was wrong".
- For chatbot issues: "There may have been confusion in how the information was presented"
  instead of "the chatbot gave wrong info".

RETENTION OFFER RULES:
- If churn risk high: include specific retention offer (e.g. "We'd like to offer you one month free").
- Format: "As a gesture of goodwill, we'd like to offer [specific benefit]"
- Never say "I'm sorry for the error" — say "I appreciate your patience"

PRO-RATA BILLING RULES:
- If context includes pro-rata calculation: show the actual formula with numbers.
- Format: (days_remaining / total_days) × per_seat_price × new_seats = $X.XX
- Apply discounts LAST: show discounted total separately.
- Example: "5 new seats × $12/seat × (10/30 days) = $20 | with 30% discount = $14"

End with: Best regards,\nCustomer Success Team"""

    user = f"""Draft a {tone} email reply for the following situation:

SITUATION:
{context}

{f'RELEVANT POLICY:{chr(10)}{policy_context}' if policy_context else ''}
{web_intel_section}
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

        result_data: dict = {"draft": draft, "tone": tone}
        if web_intel_payload:
            result_data["web_intel"] = web_intel_payload
            result_data["web_intel_used"] = True

        return ToolResult(
            ok=True,
            data=result_data,
            summary=(
                f"Draft reply generated ({len(draft)} chars, tone={tone}"
                + (", web intel injected" if web_intel_payload else "")
                + ")."
            ),
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
    """
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            return ToolResult(ok=False, error=f"Email {email_id} not found", summary="Escalation failed — email not found.")

        email.status        = "Escalated"
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
    """
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if not email:
            return ToolResult(ok=False, error=f"Email {email_id} not found", summary="Legal flag failed — email not found.")

        email.status         = "Escalated"
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


# ─────────────────────────────────────────
# Tool 7 — create_internal_ticket
# ─────────────────────────────────────────

# Valid assignee → team name mappings for audit readability
_TEAM_NAMES = {
    "dpo@company.com":         "Data Protection / Legal",
    "legal@company.com":       "Legal",
    "security@company.com":    "Security",
    "engineering@company.com": "Engineering",
    "product@company.com":     "Product",
    "support@company.com":     "Customer Support",
    "customer_success@company.com": "Customer Success",
    "ops@company.com":         "Operations",
}

_VALID_PRIORITIES = {"low", "medium", "high", "critical"}


async def create_internal_ticket(
    title: str,
    body: str,
    assignee: str,
    priority: str = "medium",
    db: Optional[Session] = None,
    email_id: Optional[int] = None,
) -> ToolResult:
    """
    Creates an internal support/engineering/compliance ticket.

    In a production system this would POST to Jira, Linear, or an internal
    ticketing system. Here we:
      1. Validate inputs
      2. Generate a ticket ID
      3. Persist an Action record with action_type="Ticket-Created"
      4. Return the ticket ID for the agent's reasoning trace

    Args:
        title:    Ticket title (required, max 200 chars)
        body:     Ticket body with context (required)
        assignee: Team email or address (e.g. dpo@company.com)
        priority: low | medium | high | critical
        db:       SQLAlchemy session (required if email_id provided)
        email_id: Optional FK to link ticket to an email

    Returns:
        ToolResult with data["ticket_id"] and data["assignee"]
    """
    # ── Validate inputs ───────────────────────────────────────────────
    if not title or not title.strip():
        return ToolResult(
            ok=False,
            error="Ticket title is required.",
            summary="Ticket creation failed — empty title.",
        )

    if not body or not body.strip():
        return ToolResult(
            ok=False,
            error="Ticket body is required.",
            summary="Ticket creation failed — empty body.",
        )

    priority_clean = priority.lower().strip()
    if priority_clean not in _VALID_PRIORITIES:
        priority_clean = "medium"
        logger.warning(f"create_internal_ticket: invalid priority '{priority}' — defaulting to 'medium'")

    title_clean = title.strip()[:200]

    # ── Generate ticket ID ────────────────────────────────────────────
    # Format: TKT-{YYYYMMDD}-{6-char hex}
    ticket_id = f"TKT-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    team_name = _TEAM_NAMES.get(assignee.lower(), assignee)

    logger.info(
        f"[create_internal_ticket] Ticket {ticket_id} created: "
        f"'{title_clean}' → {team_name} (priority={priority_clean})"
    )

    # ── Persist to DB if session provided ────────────────────────────
    if db is not None and email_id is not None:
        try:
            ticket_log = {
                "ticket_id": ticket_id,
                "title":     title_clean,
                "body":      body[:2000],
                "assignee":  assignee,
                "team":      team_name,
                "priority":  priority_clean,
                "created_at": datetime.utcnow().isoformat(),
                "status":    "open",
            }

            action = Action(
                email_id=email_id,
                agent_reasoning_log=[{
                    "step":        "create_internal_ticket",
                    "thought":     f"Creating {priority_clean}-priority ticket for {team_name}",
                    "action":      "create_internal_ticket()",
                    "observation": ticket_log,
                }],
                action_type="Ticket-Created",
                proposed_content=f"[{ticket_id}] {title_clean}\n\nAssigned to: {team_name}\nPriority: {priority_clean}\n\n{body[:500]}",
                is_approved=True,   # tickets are auto-created, no approval gate
                executed_at=datetime.utcnow(),
            )
            db.add(action)
            db.commit()

        except Exception as e:
            # Non-fatal — ticket ID is still returned so the agent can continue
            logger.error(f"create_internal_ticket DB persist failed: {e}", exc_info=True)

    return ToolResult(
        ok=True,
        data={
            "ticket_id":  ticket_id,
            "title":      title_clean,
            "assignee":   assignee,
            "team":       team_name,
            "priority":   priority_clean,
            "status":     "open",
            "created_at": datetime.utcnow().isoformat(),
        },
        summary=(
            f"Ticket {ticket_id} created: '{title_clean}' "
            f"→ {team_name} (priority={priority_clean})."
        ),
    )
