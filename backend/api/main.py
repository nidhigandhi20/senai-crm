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
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional

# ── path fix so imports resolve from project root ─────────────────────
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

logger = logging.getLogger(__name__)

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
    allow_origins=["*"],   # tighten in production
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
    # ── Deduplicate ────────────────────────────────────────────────────
    existing = db.query(Email).filter(Email.message_id == payload.message_id).first()
    if existing:
        error_response(
            "DUPLICATE_MESSAGE_ID",
            f"Email with message_id '{payload.message_id}' already exists.",
            status=409,
            details={"email_id": existing.id, "status": existing.status},
        )

    # ── 1. Ensure contact exists FIRST (threads FK → contacts) ────────
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
        db.flush()  # flush so the contact row exists before thread INSERT

    # ── 2. Ensure thread exists ────────────────────────────────────────
    thread_id = payload.thread_id or payload.message_id  # fallback: own thread
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

    # ── 3. Create email row ────────────────────────────────────────────
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

    # ── 4. Classify synchronously ──────────────────────────────────────
    # Failure here does NOT fail the ingest — email is already saved
    # and can be re-classified via pipeline_runner --email-id <id>
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
    - All threads they've been part of
    - All emails in each thread, with classification results
    - All Action records with reasoning traces

    Used by the frontend Thread Workspace view.
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
            "thread_id":      thread_obj.thread_id if thread_obj else str(tid),
            "subject":        thread_obj.subject if thread_obj else None,
            "status":         thread_obj.status if thread_obj else None,
            "first_seen_at":  thread_obj.first_seen_at.isoformat() if thread_obj and thread_obj.first_seen_at else None,
            "last_updated_at": thread_obj.last_updated_at.isoformat() if thread_obj and thread_obj.last_updated_at else None,
            "emails":         email_records,
        })

    # ── Sentiment summary ──────────────────────────────────────────────
    scores = [e.sentiment_score for e in emails if e.sentiment_score is not None]
    recent_scores = scores[-5:] if scores else []
    deteriorating = len(scores) >= 3 and all(s < -0.2 for s in scores[-3:])

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
            "total_emails":  len(emails),
            "avg_sentiment": round(sum(scores) / len(scores), 3) if scores else None,
            "recent_scores": recent_scores,
            "deteriorating": deteriorating,
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

    Executes the full ReAct loop (tool calls, reasoning) but:
    - Does NOT write any Action records to DB
    - Does NOT update the email status
    - Does NOT send any replies or escalations

    Returns the full reasoning trace so the caller can inspect
    what the agent would have done.
    """
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        error_response(
            "EMAIL_NOT_FOUND",
            f"Email with ID {email_id} not found.",
            status=404,
        )

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
        error_response(
            "AGENT_NOT_AVAILABLE",
            "Agent module not found. Ensure agent/agent.py exists.",
            status=503,
        )
    except Exception as e:
        logger.error(f"Agent dry-run failed for email {email_id}: {e}", exc_info=True)
        error_response(
            "AGENT_ERROR",
            f"Agent failed: {str(e)}",
            status=500,
        )


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

    Query params:
        sender  — optional, filter to one sender
        days    — look-back window (default 30)

    Response includes a data_points list sorted oldest → newest,
    plus aggregate stats (count, avg, min, max).
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

    return {
        "sender":      sender or "all",
        "days":        days,
        "data_points": data_points,
        "stats": {
            "count": len(scores),
            "avg":   round(sum(scores) / len(scores), 3),
            "min":   round(min(scores), 3),
            "max":   round(max(scores), 3),
        },
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
    entity_id   : DB primary key

    For emails  → all Action records with full reasoning traces.
    For actions → the single Action record with its reasoning log.
    """
    if entity_type == "email":
        email = db.query(Email).filter(Email.id == entity_id).first()
        if not email:
            error_response(
                "EMAIL_NOT_FOUND",
                f"Email with ID {entity_id} not found.",
                status=404,
            )

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
            error_response(
                "ACTION_NOT_FOUND",
                f"Action with ID {entity_id} not found.",
                status=404,
            )

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
    """
    Debug endpoint: runs a RAG query and returns the retrieved chunks
    with similarity scores. Useful for testing knowledge base coverage.
    """
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