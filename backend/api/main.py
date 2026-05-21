"""
FastAPI — SenAI REST API
========================
Endpoints:
    POST  /api/ingest                       ← validate + ingest email
    GET   /threads/{contact_email}          ← full thread + actions + reasoning
    POST  /agent/dry-run/{email_id}         ← plan without executing
    GET   /analytics/sentiment-trend        ← time-series sentiment per sender
    GET   /audit/{entity_type}/{entity_id}  ← full audit history
    GET   /rag/search                       ← debug: RAG retrieval
    GET   /health                           ← liveness check

Run with:
    uvicorn api.main:app --reload --port 8000

Integrations added:
    - SentimentTracker is used in GET /threads/{contact_email} to replace
      the inline ad-hoc deterioration logic with the canonical tracker,
      and to expose the full sentiment trend via get_trend().
    - WebScraper is NOT called from the API layer directly — it is only
      triggered from agent/tools.py :: draft_reply() when churn-risk
      conditions are met.  This keeps the API fast and scraping isolated
      to the agent decision loop.
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Email, Action, Contact, Thread
from rag.pipeline import retrieve, format_rag_context
from classifier.engine import classify_email
from sentiment.tracker import SentimentTracker   # ← NEW

logger = logging.getLogger(__name__)

_sentiment_tracker = SentimentTracker()          # module-level singleton

# ─────────────────────────────────────────
# App setup
# ─────────────────────────────────────────

app = FastAPI(
    title="SenAI CRM API",
    description="Autonomous email triage and classification system",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# DB dependency
# ─────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────
# Error envelope helper
# ─────────────────────────────────────────

def error_response(code: str, message: str, status: int = 400, details: dict = None):
    raise HTTPException(
        status_code=status,
        detail={
            "error": {
                "code":    code,
                "message": message,
                "details": details or {},
            }
        },
    )


# ─────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────

class IngestEmailRequest(BaseModel):
    message_id: str
    sender:     str
    subject:    Optional[str] = None
    body:       Optional[str] = None
    thread_id:  Optional[str] = None
    timestamp:  Optional[datetime] = None

    @field_validator("sender")
    @classmethod
    def sender_not_empty(cls, v):
        if not v or "@" not in v:
            raise ValueError("sender must be a valid email address")
        return v.lower().strip()

    @field_validator("message_id")
    @classmethod
    def message_id_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("message_id cannot be empty")
        return v.strip()


class IngestEmailResponse(BaseModel):
    job_id:     int
    message_id: str
    status:     str
    thread_id:  Optional[str] = None


# ─────────────────────────────────────────
# POST /api/ingest
# ─────────────────────────────────────────

@app.post("/api/ingest", response_model=IngestEmailResponse, status_code=202)
async def ingest_email(
    payload: IngestEmailRequest,
    db: Session = Depends(get_db),
):
    """
    Validates, deduplicates, and ingests an email for classification.

    - Rejects duplicates (same message_id)
    - Creates or links a Contact record FIRST (Thread FK depends on it)
    - Creates or links a Thread record
    - Creates the Email row
    - Runs classification synchronously
    - Returns job_id = the new email's DB id

    Status 202 = accepted for processing.
    """
    existing = db.query(Email).filter(Email.message_id == payload.message_id).first()
    if existing:
        error_response(
            "DUPLICATE_MESSAGE_ID",
            f"Email with message_id '{payload.message_id}' already exists.",
            status=409,
            details={"email_id": existing.id, "status": existing.status},
        )

    contact = db.query(Contact).filter(Contact.email == payload.sender).first()
    if not contact:
        contact = Contact(
            email=payload.sender,
            name=None,
            company=None,
            status="Active",
            account_value=None,
            churn_risk_score=None,
        )
        db.add(contact)
        db.flush()

    thread_id = payload.thread_id or payload.message_id
    thread = db.query(Thread).filter(Thread.thread_id == thread_id).first()
    if not thread:
        thread = Thread(
            thread_id=thread_id,
            subject=payload.subject,
            sender_email=payload.sender,
            status="Open",
            first_seen_at=payload.timestamp or datetime.utcnow(),
            last_updated_at=payload.timestamp or datetime.utcnow(),
        )
        db.add(thread)
        db.flush()
    else:
        thread.last_updated_at = payload.timestamp or datetime.utcnow()
        db.flush()

    email = Email(
        message_id=payload.message_id,
        thread_id=thread.id,
        sender=payload.sender,
        subject=payload.subject,
        body=payload.body,
        timestamp=payload.timestamp or datetime.utcnow(),
        status="Received",
    )
    db.add(email)
    db.commit()
    db.refresh(email)

    try:
        await classify_email(email.id, db)
    except Exception as e:
        logger.error(f"Classification failed for email {email.id}: {e}", exc_info=True)

    db.refresh(email)

    return IngestEmailResponse(
        job_id=email.id,
        message_id=email.message_id,
        status=email.status,
        thread_id=thread_id,
    )


# ─────────────────────────────────────────
# GET /threads/{contact_email}
# ─────────────────────────────────────────

@app.get("/threads/{contact_email}")
def get_thread(
    contact_email: str,
    db: Session = Depends(get_db),
):
    """
    Returns the full history for a contact:
    - Contact CRM profile
    - All threads with emails, classification results, and action records
    - Sentiment summary powered by SentimentTracker

    ── Tracker integration ───────────────────────────────────────────────
    Instead of computing deterioration inline from in-memory scores,
    we delegate to SentimentTracker so the logic is consistent with what
    the agent uses in get_thread_history().

    detect_deterioration() reads the last 3 classified emails from the DB
    (same query the agent uses), so the API response always matches the
    agent's assessment.

    get_trend() is also called to return the full score history, which
    the frontend can use to render a sparkline on the contact card.
    ─────────────────────────────────────────────────────────────────────
    """
    contact = db.query(Contact).filter(Contact.email == contact_email).first()

    emails = (
        db.query(Email)
        .filter(Email.sender == contact_email)
        .order_by(Email.timestamp.asc())
        .all()
    )

    if not emails and not contact:
        error_response(
            "CONTACT_NOT_FOUND",
            f"No emails or contact profile found for '{contact_email}'.",
            status=404,
        )

    # ── Group emails by thread ─────────────────────────────────────────
    thread_ids = list({e.thread_id for e in emails})
    threads_data = []

    for tid in thread_ids:
        thread_obj = db.query(Thread).filter(Thread.id == tid).first()
        thread_emails = [e for e in emails if e.thread_id == tid]

        email_records = []
        for e in thread_emails:
            actions = (
                db.query(Action)
                .filter(Action.email_id == e.id)
                .order_by(Action.id.asc())
                .all()
            )
            email_records.append({
                "id":              e.id,
                "message_id":      e.message_id,
                "sender":          e.sender,
                "subject":         e.subject,
                "body":            e.body,
                "timestamp":       e.timestamp.isoformat() if e.timestamp else None,
                "category":        e.category,
                "urgency":         e.urgency,
                "sentiment_score": e.sentiment_score,
                "confidence":      e.confidence,
                "requires_human":  e.requires_human,
                "status":          e.status,
                "raw_entities":    e.raw_entities,
                "actions": [
                    {
                        "id":                  a.id,
                        "action_type":         a.action_type,
                        "proposed_content":    a.proposed_content,
                        "is_approved":         a.is_approved,
                        "approved_by":         a.approved_by,
                        "executed_at":         a.executed_at.isoformat() if a.executed_at else None,
                        "agent_reasoning_log": a.agent_reasoning_log,
                    }
                    for a in actions
                ],
            })

        threads_data.append({
            "thread_id":       thread_obj.thread_id if thread_obj else str(tid),
            "subject":         thread_obj.subject if thread_obj else None,
            "status":          thread_obj.status if thread_obj else None,
            "first_seen_at":   thread_obj.first_seen_at.isoformat() if thread_obj and thread_obj.first_seen_at else None,
            "last_updated_at": thread_obj.last_updated_at.isoformat() if thread_obj and thread_obj.last_updated_at else None,
            "emails":          email_records,
        })

    # ── Sentiment summary — powered by SentimentTracker ───────────────
    # detect_deterioration() uses the same DB query as the agent tool,
    # ensuring the API and agent always agree on the contact's status.
    deteriorating = _sentiment_tracker.detect_deterioration(contact_email, db)

    # get_trend() returns all scores oldest→newest for sparkline rendering
    full_trend = _sentiment_tracker.get_trend(contact_email, db)
    recent_scores = full_trend[-5:] if full_trend else []

    # Escalation alert if deterioration is active
    sentiment_alert = None
    if deteriorating:
        sentiment_alert = _sentiment_tracker.create_escalation_alert(
            sender_email=contact_email,
            reason="3+ consecutive negative emails detected (API /threads view)",
            severity="High",
            db=db,
        )

    return {
        "contact": {
            "email":            contact.email if contact else contact_email,
            "name":             contact.name if contact else None,
            "company":          contact.company if contact else None,
            "status":           contact.status if contact else None,
            "account_value":    contact.account_value if contact else None,
            "churn_risk_score": contact.churn_risk_score if contact else None,
            "is_vip":           (
                contact.status == "VIP" or (contact.account_value or 0) > 10_000
            ) if contact else False,
        },
        "sentiment_summary": {
            "total_emails":    len(emails),
            "avg_sentiment":   round(sum(full_trend) / len(full_trend), 3) if full_trend else None,
            "recent_scores":   recent_scores,
            "full_trend":      full_trend,           # ← new: full history for sparkline
            "deteriorating":   deteriorating,        # ← now from canonical tracker
            "alert":           sentiment_alert,      # ← new: structured alert or None
        },
        "threads": threads_data,
    }


# ─────────────────────────────────────────
# POST /agent/dry-run/{email_id}
# ─────────────────────────────────────────

@app.post("/agent/dry-run/{email_id}")
async def agent_dry_run(
    email_id: int,
    db: Session = Depends(get_db),
):
    """
    Runs the agent in planning mode.
    Does NOT write Action records, update email status, or send replies.
    Returns the full reasoning trace.
    """
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        error_response("EMAIL_NOT_FOUND", f"Email with ID {email_id} not found.", status=404)

    if not email.category:
        error_response(
            "EMAIL_NOT_CLASSIFIED",
            f"Email {email_id} has not been classified yet. Run the pipeline first.",
            status=422,
        )

    try:
        from agent.agent import run_agent
        result = await run_agent(email_id=email_id, dry_run=True, db=db)
        return {
            "dry_run":         True,
            "email_id":        email_id,
            "message_id":      email.message_id,
            "final_decision":  result["final_decision"],
            "decision_reason": result["decision_reason"],
            "steps":           result["steps"],
            "reasoning_trace": result["reasoning_trace"],
            "proposed_reply":  result["proposed_reply"],
            "is_safe":         result["is_safe"],
        }
    except ImportError:
        error_response("AGENT_NOT_AVAILABLE", "Agent module not found.", status=503)
    except Exception as e:
        logger.error(f"Agent dry-run failed for email {email_id}: {e}", exc_info=True)
        error_response("AGENT_ERROR", f"Agent failed: {str(e)}", status=500)


# ─────────────────────────────────────────
# GET /analytics/sentiment-trend
# ─────────────────────────────────────────

@app.get("/analytics/sentiment-trend")
def sentiment_trend(
    sender: Optional[str] = Query(None, description="Filter by sender email"),
    days:   int           = Query(30,   description="Look-back window in days"),
    db:     Session       = Depends(get_db),
):
    """
    Returns time-series sentiment data.

    When a sender is specified, also includes:
    - deteriorating: bool  (from SentimentTracker — same logic as agent)
    - full_trend: list     (all scores, not just the look-back window)
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    query = (
        db.query(Email)
        .filter(
            Email.timestamp >= cutoff,
            Email.sentiment_score.isnot(None),
        )
    )

    if sender:
        query = query.filter(Email.sender == sender)

    emails = query.order_by(Email.timestamp.asc()).all()

    if not emails:
        return {
            "sender":      sender or "all",
            "days":        days,
            "data_points": [],
            "stats":       {"count": 0, "avg": None, "min": None, "max": None},
            # Tracker fields — present even on empty response
            "deteriorating": False,
            "full_trend":    [],
        }

    data_points = [
        {
            "date":            e.timestamp.isoformat() if e.timestamp else None,
            "sender":          e.sender,
            "sentiment_score": e.sentiment_score,
            "email_id":        e.id,
            "message_id":      e.message_id,
            "category":        e.category,
            "subject":         e.subject,
        }
        for e in emails
    ]

    scores = [e.sentiment_score for e in emails]

    # ── SentimentTracker: per-sender enrichment ───────────────────────
    # Only meaningful when filtering to a single sender; for global
    # queries the deterioration concept doesn't apply.
    deteriorating = False
    full_trend: list = []
    if sender:
        deteriorating = _sentiment_tracker.detect_deterioration(sender, db)
        full_trend    = _sentiment_tracker.get_trend(sender, db)

    return {
        "sender":        sender or "all",
        "days":          days,
        "data_points":   data_points,
        "stats": {
            "count": len(scores),
            "avg":   round(sum(scores) / len(scores), 3),
            "min":   round(min(scores), 3),
            "max":   round(max(scores), 3),
        },
        "deteriorating": deteriorating,   # ← new: from canonical tracker
        "full_trend":    full_trend,      # ← new: full history for this sender
    }


# ─────────────────────────────────────────
# GET /audit/{entity_type}/{entity_id}
# ─────────────────────────────────────────

@app.get("/audit/{entity_type}/{entity_id}")
def get_audit(
    entity_type: str,
    entity_id:   int,
    db:          Session = Depends(get_db),
):
    """
    Returns the full audit trail for an email or action.
    entity_type : "email" | "action"
    """
    if entity_type == "email":
        email = db.query(Email).filter(Email.id == entity_id).first()
        if not email:
            error_response("EMAIL_NOT_FOUND", f"Email with ID {entity_id} not found.", status=404)

        actions = (
            db.query(Action)
            .filter(Action.email_id == entity_id)
            .order_by(Action.id.asc())
            .all()
        )

        return {
            "entity_type": "email",
            "entity_id":   entity_id,
            "email": {
                "message_id": email.message_id,
                "sender":     email.sender,
                "subject":    email.subject,
                "category":   email.category,
                "urgency":    email.urgency,
                "status":     email.status,
                "confidence": email.confidence,
                "timestamp":  email.timestamp.isoformat() if email.timestamp else None,
            },
            "audit_trail": [
                {
                    "action_id":           a.id,
                    "action_type":         a.action_type,
                    "is_approved":         a.is_approved,
                    "approved_by":         a.approved_by,
                    "executed_at":         a.executed_at.isoformat() if a.executed_at else None,
                    "proposed_content":    a.proposed_content,
                    "agent_reasoning_log": a.agent_reasoning_log,
                }
                for a in actions
            ],
        }

    elif entity_type == "action":
        action = db.query(Action).filter(Action.id == entity_id).first()
        if not action:
            error_response("ACTION_NOT_FOUND", f"Action with ID {entity_id} not found.", status=404)

        return {
            "entity_type":         "action",
            "entity_id":           entity_id,
            "email_id":            action.email_id,
            "action_type":         action.action_type,
            "is_approved":         action.is_approved,
            "approved_by":         action.approved_by,
            "executed_at":         action.executed_at.isoformat() if action.executed_at else None,
            "proposed_content":    action.proposed_content,
            "agent_reasoning_log": action.agent_reasoning_log,
        }

    else:
        error_response(
            "INVALID_ENTITY_TYPE",
            f"entity_type must be 'email' or 'action', got '{entity_type}'.",
            status=400,
        )


# ─────────────────────────────────────────
# GET /rag/search  (debug)
# ─────────────────────────────────────────

@app.get("/rag/search")
def rag_search(
    q:     str     = Query(..., description="Search query"),
    top_k: int     = Query(3,   description="Number of chunks to return"),
    db:    Session = Depends(get_db),
):
    """Debug endpoint: RAG query with similarity scores."""
    if not q.strip():
        error_response("EMPTY_QUERY", "Query parameter 'q' cannot be empty.")

    try:
        chunks = retrieve(q, top_k=top_k, db=db)
    except Exception as e:
        error_response("RAG_ERROR", f"RAG retrieval failed: {str(e)}", status=500)

    return {
        "query":  q,
        "top_k":  top_k,
        "chunks": [
            {
                "source_doc":       c.source_doc,
                "similarity_score": c.similarity_score,
                "chunk_text":       c.chunk_text,
            }
            for c in chunks
        ],
        "formatted_context": format_rag_context(chunks),
    }


# ─────────────────────────────────────────
# GET /api/status/{job_id}
# ─────────────────────────────────────────

@app.get("/api/status/{job_id}")
def check_status(
    job_id: int,
    db: Session = Depends(get_db),
):
    """
    Check processing status of an ingested email.
    job_id = the email's database ID returned from POST /api/ingest
    """
    email = db.query(Email).filter(Email.id == job_id).first()
    if not email:
        error_response(
            "JOB_NOT_FOUND",
            f"No email found with job_id {job_id}.",
            status=404,
        )

    actions = (
        db.query(Action)
        .filter(Action.email_id == job_id)
        .order_by(Action.id.asc())
        .all()
    )

    return {
        "job_id":          job_id,
        "message_id":      email.message_id,
        "status":          email.status,
        "category":        email.category,
        "urgency":         email.urgency,
        "sentiment_score": email.sentiment_score,
        "confidence":      email.confidence,
        "requires_human":  email.requires_human,
        "timestamp":       email.timestamp.isoformat() if email.timestamp else None,
        "actions_taken":   [
            {
                "id":                  a.id,
                "action_type":         a.action_type,
                "is_approved":         a.is_approved,
                "executed_at":         a.executed_at.isoformat() if a.executed_at else None,
                "proposed_content":    a.proposed_content,
            }
            for a in actions
        ],
        "raw_entities":    email.raw_entities,
    }


# ─────────────────────────────────────────
# GET /dashboard/stats
# ─────────────────────────────────────────

@app.get("/dashboard/stats")
def dashboard_stats(
    days: int = Query(30, description="Look-back window in days"),
    db: Session = Depends(get_db),
):
    """
    Dashboard statistics: counts of emails by status and category.
    Returns:
    - Pending: status in (Received, Processing)
    - Replied: status = Replied
    - Escalated: status = Escalated
    - Critical: urgency = Critical
    - Spam filtered: category = Spam (if exists)
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    try:
        total_emails = db.query(Email).filter(Email.timestamp >= cutoff).count()
        pending = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.status.in_(["Received", "Processing"])
        ).count()
        replied = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.status == "Replied"
        ).count()
        escalated = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.status == "Escalated"
        ).count()
        critical = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.urgency == "Critical"
        ).count()
        spam = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.category == "Spam"
        ).count()

        # Category breakdown
        category_counts = {}
        categories = db.query(Email.category).filter(
            Email.timestamp >= cutoff,
            Email.category.isnot(None)
        ).distinct().all()
        
        for (cat,) in categories:
            if cat:
                count = db.query(Email).filter(
                    Email.timestamp >= cutoff,
                    Email.category == cat
                ).count()
                category_counts[cat] = count

        # Urgency breakdown
        urgency_counts = {}
        urgencies = db.query(Email.urgency).filter(
            Email.timestamp >= cutoff,
            Email.urgency.isnot(None)
        ).distinct().all()
        
        for (urg,) in urgencies:
            if urg:
                count = db.query(Email).filter(
                    Email.timestamp >= cutoff,
                    Email.urgency == urg
                ).count()
                urgency_counts[urg] = count

        # Status breakdown
        status_counts = {}
        statuses = db.query(Email.status).filter(
            Email.timestamp >= cutoff
        ).distinct().all()
        
        for (st,) in statuses:
            if st:
                count = db.query(Email).filter(
                    Email.timestamp >= cutoff,
                    Email.status == st
                ).count()
                status_counts[st] = count

        # Contacts
        unique_senders = db.query(Email.sender).filter(
            Email.timestamp >= cutoff
        ).distinct().count()
        
        vip_contacts = db.query(Contact).filter(
            Contact.status == "VIP"
        ).count()

        # Sentiment stats
        sentiment_emails = db.query(Email).filter(
            Email.timestamp >= cutoff,
            Email.sentiment_score.isnot(None)
        ).all()
        
        avg_sentiment = None
        if sentiment_emails:
            avg_sentiment = round(
                sum(e.sentiment_score for e in sentiment_emails) / len(sentiment_emails),
                3
            )

        return {
            "period_days":    days,
            "total_emails":   total_emails,
            "summary": {
                "pending":    pending,
                "replied":    replied,
                "escalated":  escalated,
                "critical":   critical,
                "spam":       spam,
            },
            "by_category":    category_counts,
            "by_urgency":     urgency_counts,
            "by_status":      status_counts,
            "contacts": {
                "unique_senders": unique_senders,
                "vip_count":      vip_contacts,
            },
            "sentiment": {
                "avg_score": avg_sentiment,
            },
        }

    except Exception as e:
        logger.error(f"Dashboard stats query failed: {e}", exc_info=True)
        error_response("STATS_ERROR", f"Failed to compute statistics: {str(e)}", status=500)


# ─────────────────────────────────────────
# POST /respond/{email_id}
# ─────────────────────────────────────────

class RespondRequest(BaseModel):
    reply_text: str
    approved_by: Optional[str] = None

    @field_validator("reply_text")
    @classmethod
    def reply_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("reply_text cannot be empty")
        return v.strip()


@app.post("/respond/{email_id}")
def send_reply(
    email_id: int,
    payload: RespondRequest,
    db: Session = Depends(get_db),
):
    """
    Send a reply to an email.
    - Creates Action record with action_type = 'Auto-Reply'
    - Updates email status to 'Replied'
    - Appends to thread conversation
    - Triggers audit log entry
    """
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        error_response("EMAIL_NOT_FOUND", f"Email with ID {email_id} not found.", status=404)

    if email.status == "Replied":
        error_response(
            "ALREADY_REPLIED",
            f"Email {email_id} has already been replied to.",
            status=409,
        )

    try:
        # Create action record
        action = Action(
            email_id=email_id,
            action_type="Auto-Reply",
            proposed_content=payload.reply_text,
            is_approved=True,
            approved_by=payload.approved_by or "system",
            executed_at=datetime.utcnow(),
            agent_reasoning_log=[
                {
                    "step": "reply_sent",
                    "content": payload.reply_text,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ],
        )
        db.add(action)
        db.flush()

        # Update email status
        email.status = "Replied"
        email.last_contact_at = datetime.utcnow()

        # Create audit log entry
        from db.models import AuditLog
        audit = AuditLog(
            entity_type="email",
            entity_id=email_id,
            action="reply_sent",
            performed_by=payload.approved_by or "system",
            diff={
                "action_id": action.id,
                "reply_text": payload.reply_text,
                "status_before": "Received/Processing",
                "status_after": "Replied",
            },
        )
        db.add(audit)
        db.commit()

        return {
            "email_id":     email_id,
            "action_id":    action.id,
            "message_id":   email.message_id,
            "status":       email.status,
            "reply_sent":   True,
            "timestamp":    datetime.utcnow().isoformat(),
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to send reply for email {email_id}: {e}", exc_info=True)
        error_response("REPLY_ERROR", f"Failed to send reply: {str(e)}", status=500)


# ─────────────────────────────────────────
# PATCH /drafts/{id}
# ─────────────────────────────────────────

class UpdateDraftRequest(BaseModel):
    proposed_content: str

    @field_validator("proposed_content")
    @classmethod
    def content_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("proposed_content cannot be empty")
        return v.strip()


@app.patch("/drafts/{draft_id}")
def update_draft(
    draft_id: int,
    payload: UpdateDraftRequest,
    db: Session = Depends(get_db),
):
    """
    Edit a proposed auto-reply before sending.
    - Only allows editing non-approved, non-executed actions
    """
    action = db.query(Action).filter(Action.id == draft_id).first()
    if not action:
        error_response("DRAFT_NOT_FOUND", f"Draft with ID {draft_id} not found.", status=404)

    if action.is_approved:
        error_response(
            "DRAFT_ALREADY_APPROVED",
            f"Draft {draft_id} has already been approved and cannot be edited.",
            status=409,
        )

    if action.executed_at:
        error_response(
            "DRAFT_ALREADY_EXECUTED",
            f"Draft {draft_id} has already been executed and cannot be edited.",
            status=409,
        )

    try:
        action.proposed_content = payload.proposed_content
        
        # Add to reasoning log
        if not action.agent_reasoning_log:
            action.agent_reasoning_log = []
        
        action.agent_reasoning_log.append({
            "step": "draft_edited",
            "previous_content": action.proposed_content,
            "new_content": payload.proposed_content,
            "timestamp": datetime.utcnow().isoformat(),
        })

        db.commit()
        db.refresh(action)

        return {
            "draft_id":         draft_id,
            "email_id":         action.email_id,
            "proposed_content": action.proposed_content,
            "is_approved":      action.is_approved,
            "updated_at":       datetime.utcnow().isoformat(),
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update draft {draft_id}: {e}", exc_info=True)
        error_response("DRAFT_UPDATE_ERROR", f"Failed to update draft: {str(e)}", status=500)


# ─────────────────────────────────────────
# POST /drafts/{id}/approve
# ─────────────────────────────────────────

class ApproveDraftRequest(BaseModel):
    approved_by: Optional[str] = None


@app.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: int,
    payload: ApproveDraftRequest,
    db: Session = Depends(get_db),
):
    """
    Approve and send a draft reply.
    - Marks action as approved
    - Sets executed_at timestamp
    - Updates email status to 'Replied'
    - Triggers audit log entry
    """
    action = db.query(Action).filter(Action.id == draft_id).first()
    if not action:
        error_response("DRAFT_NOT_FOUND", f"Draft with ID {draft_id} not found.", status=404)

    if action.is_approved:
        error_response(
            "DRAFT_ALREADY_APPROVED",
            f"Draft {draft_id} has already been approved.",
            status=409,
        )

    if action.executed_at:
        error_response(
            "DRAFT_ALREADY_EXECUTED",
            f"Draft {draft_id} has already been executed.",
            status=409,
        )

    try:
        action.is_approved = True
        action.approved_by = payload.approved_by or "human_review"
        action.executed_at = datetime.utcnow()
        db.flush()

        # Update email status
        email = db.query(Email).filter(Email.id == action.email_id).first()
        if email:
            email.status = "Replied"
            db.flush()

            # Create audit log entry
            from db.models import AuditLog
            audit = AuditLog(
                entity_type="action",
                entity_id=draft_id,
                action="draft_approved_and_executed",
                performed_by=payload.approved_by or "human_review",
                diff={
                    "email_id": action.email_id,
                    "action_type": action.action_type,
                    "is_approved": True,
                    "email_status_updated_to": "Replied",
                },
            )
            db.add(audit)

        db.commit()

        return {
            "draft_id":      draft_id,
            "email_id":      action.email_id,
            "is_approved":   True,
            "executed_at":   action.executed_at.isoformat() if action.executed_at else None,
            "approved_by":   action.approved_by,
            "reply_text":    action.proposed_content,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to approve draft {draft_id}: {e}", exc_info=True)
        error_response("DRAFT_APPROVAL_ERROR", f"Failed to approve draft: {str(e)}", status=500)


# ─────────────────────────────────────────
# GET /analytics/category-breakdown
# ─────────────────────────────────────────

@app.get("/analytics/category-breakdown")
def category_breakdown(
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
):
    """
    Category distribution over configurable date range.
    Returns breakdown of email categories with counts and percentages.
    """
    try:
        query = db.query(Email).filter(Email.category.isnot(None))

        if start_date:
            try:
                start = datetime.fromisoformat(start_date)
                query = query.filter(Email.timestamp >= start)
            except ValueError:
                error_response(
                    "INVALID_START_DATE",
                    f"start_date must be in YYYY-MM-DD format, got '{start_date}'.",
                    status=400,
                )

        if end_date:
            try:
                end = datetime.fromisoformat(end_date)
                # Add one day to include the entire end date
                end = end.replace(hour=23, minute=59, second=59)
                query = query.filter(Email.timestamp <= end)
            except ValueError:
                error_response(
                    "INVALID_END_DATE",
                    f"end_date must be in YYYY-MM-DD format, got '{end_date}'.",
                    status=400,
                )

        emails = query.all()
        total_count = len(emails)

        if total_count == 0:
            return {
                "start_date": start_date,
                "end_date": end_date,
                "total_emails": 0,
                "categories": [],
            }

        # Count by category
        category_counts = {}
        for email in emails:
            cat = email.category or "Uncategorized"
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # Build response with percentages
        categories = [
            {
                "category": cat,
                "count": count,
                "percentage": round((count / total_count) * 100, 2),
            }
            for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_emails": total_count,
            "categories": categories,
        }

    except Exception as e:
        logger.error(f"Category breakdown query failed: {e}", exc_info=True)
        error_response("CATEGORY_ERROR", f"Failed to compute category breakdown: {str(e)}", status=500)


# ─────────────────────────────────────────
# GET /intelligence/reputation
# ─────────────────────────────────────────

@app.get("/intelligence/reputation")
def get_reputation(
    company: Optional[str] = Query(None, description="Company name to query"),
    db: Session = Depends(get_db),
):
    """
    Latest scraped public sentiment for company.
    Returns cached intelligence data with TTL information.
    """
    try:
        from db.models import WebIntelligenceCache
        
        if not company:
            error_response(
                "COMPANY_REQUIRED",
                "Query parameter 'company' is required.",
                status=400,
            )

        # Query cached data
        cache = db.query(WebIntelligenceCache).filter(
            WebIntelligenceCache.target_entity == company
        ).order_by(WebIntelligenceCache.scraped_at.desc()).first()

        if not cache:
            return {
                "company": company,
                "data": None,
                "status": "not_cached",
                "message": f"No cached intelligence for '{company}'. Run scraper first.",
            }

        # Check if expired
        now = datetime.utcnow()
        is_expired = now > cache.expires_at
        time_to_expiry = (cache.expires_at - now).total_seconds()

        return {
            "company":         company,
            "data":            cache.scraped_data,
            "scraped_at":      cache.scraped_at.isoformat(),
            "expires_at":      cache.expires_at.isoformat(),
            "is_expired":      is_expired,
            "time_to_expiry_seconds": max(0, int(time_to_expiry)),
            "status":          "cached_valid" if not is_expired else "cached_expired",
        }

    except Exception as e:
        logger.error(f"Reputation query failed: {e}", exc_info=True)
        error_response("REPUTATION_ERROR", f"Failed to fetch reputation: {str(e)}", status=500)


# ─────────────────────────────────────────
# GET /contacts/{email}
# ─────────────────────────────────────────

@app.get("/contacts/{email}")
def get_contact(
    email: str,
    db: Session = Depends(get_db),
):
    """
    Contact profile with churn risk, account value, open threads.
    Returns comprehensive contact information and status.
    """
    try:
        contact = db.query(Contact).filter(Contact.email == email).first()

        if not contact:
            error_response(
                "CONTACT_NOT_FOUND",
                f"Contact with email '{email}' not found.",
                status=404,
            )

        # Get threads for this contact
        threads = db.query(Thread).filter(Thread.sender_email == email).all()
        open_threads = [t for t in threads if t.status == "Open"]

        # Get recent emails
        recent_emails = (
            db.query(Email)
            .filter(Email.sender == email)
            .order_by(Email.timestamp.desc())
            .limit(5)
            .all()
        )

        # Calculate stats
        total_emails = db.query(Email).filter(Email.sender == email).count()
        avg_sentiment = None
        sentiment_emails = (
            db.query(Email)
            .filter(Email.sender == email, Email.sentiment_score.isnot(None))
            .all()
        )
        if sentiment_emails:
            avg_sentiment = round(
                sum(e.sentiment_score for e in sentiment_emails) / len(sentiment_emails),
                3
            )

        return {
            "email":             contact.email,
            "name":              contact.name,
            "company":           contact.company,
            "status":            contact.status,
            "account_value":     contact.account_value,
            "churn_risk_score":  contact.churn_risk_score,
            "is_vip":            contact.status == "VIP" or (contact.account_value or 0) > 10_000,
            "created_at":        contact.created_at.isoformat() if contact.created_at else None,
            "last_contact_at":   contact.last_contact_at.isoformat() if contact.last_contact_at else None,
            "stats": {
                "total_emails":  total_emails,
                "avg_sentiment": avg_sentiment,
                "total_threads": len(threads),
                "open_threads":  len(open_threads),
            },
            "recent_emails": [
                {
                    "id":       e.id,
                    "message_id": e.message_id,
                    "subject":  e.subject,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "sentiment": e.sentiment_score,
                    "category": e.category,
                    "status":   e.status,
                }
                for e in recent_emails
            ],
            "threads": [
                {
                    "thread_id": t.thread_id,
                    "subject": t.subject,
                    "status": t.status,
                    "first_seen_at": t.first_seen_at.isoformat() if t.first_seen_at else None,
                    "last_updated_at": t.last_updated_at.isoformat() if t.last_updated_at else None,
                }
                for t in threads
            ],
        }

    except Exception as e:
        logger.error(f"Contact query failed: {e}", exc_info=True)
        error_response("CONTACT_ERROR", f"Failed to fetch contact: {str(e)}", status=500)


# ─────────────────────────────────────────
# PATCH /contacts/{email}/status
# ─────────────────────────────────────────

class UpdateContactStatusRequest(BaseModel):
    status: str
    account_value: Optional[float] = None
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        valid_statuses = ["Active", "VIP", "Blocked", "Churned"]
        if v not in valid_statuses:
            raise ValueError(f"status must be one of {valid_statuses}, got '{v}'")
        return v


@app.patch("/contacts/{email}/status")
def update_contact_status(
    email: str,
    payload: UpdateContactStatusRequest,
    db: Session = Depends(get_db),
):
    """
    Update contact status (VIP, Blocked, Churned, Active).
    - Validates status values
    - Updates account_value if provided
    - Triggers audit log entry
    """
    try:
        contact = db.query(Contact).filter(Contact.email == email).first()

        if not contact:
            error_response(
                "CONTACT_NOT_FOUND",
                f"Contact with email '{email}' not found.",
                status=404,
            )

        # Store old values for audit
        old_status = contact.status
        old_account_value = contact.account_value

        # Update contact
        contact.status = payload.status
        if payload.account_value is not None:
            if payload.account_value < 0:
                error_response(
                    "INVALID_ACCOUNT_VALUE",
                    "account_value cannot be negative.",
                    status=400,
                )
            contact.account_value = payload.account_value

        db.flush()

        # Create audit log entry
        from db.models import AuditLog
        audit = AuditLog(
            entity_type="contact",
            entity_id=contact.id,
            action="status_updated",
            performed_by="admin",
            diff={
                "status_before": old_status,
                "status_after": payload.status,
                "account_value_before": old_account_value,
                "account_value_after": contact.account_value,
                "notes": payload.notes,
            },
        )
        db.add(audit)
        db.commit()

        return {
            "email":           contact.email,
            "status":          contact.status,
            "account_value":   contact.account_value,
            "updated_at":      datetime.utcnow().isoformat(),
            "previous_status": old_status,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update contact {email} status: {e}", exc_info=True)
        error_response("CONTACT_UPDATE_ERROR", f"Failed to update contact: {str(e)}", status=500)


# ─────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────

@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness check — verifies DB connection and returns row counts."""
    try:
        email_count  = db.query(Email).count()
        action_count = db.query(Action).count()
        return {
            "status": "ok",
            "db":     "connected",
            "counts": {"emails": email_count, "actions": action_count},
        }
    except Exception as e:
        error_response("DB_ERROR", f"Database unreachable: {str(e)}", status=503)