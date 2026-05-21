"""
LLM Classification Engine
=========================
Classifies emails using a local Ollama model (e.g. llama3.2).

Flow for each email:
  0. Heuristic pre-filter  — instant keyword routing (skip LLM for security/spam/legal)
  1. Build query from subject + body
  2. Retrieve top-3 RAG chunks (policy context)
  3. Load thread history from DB
  4. Build prompt (system + RAG + thread + email)
  5. Call Ollama API
  6. Parse + validate JSON response
  7. Apply safety rules (confidence < 0.70, Critical, Legal)
  8. Write result to emails table
  9. Run sentiment tracker — detect deterioration, trigger escalation alert
 10. Write action record with reasoning trace
 11. (Optional) Run agent if AGENT_ENABLED=true

Usage:
    from classifier.engine import classify_email
    result = await classify_email(email_id=42, db=db)
"""

import os
import json
import re
import logging
import httpx

from sqlalchemy.orm import Session

from db.models import Email, Action
from db.database import SessionLocal
from rag.pipeline import retrieve, format_rag_context
from classifier.schemas import ClassificationResult, DetectedEntities
from classifier.prompts import SYSTEM_PROMPT, build_user_prompt
from heuristics.prefilter import prefilter, prefilter_to_db_status
from sentiment.tracker import SentimentTracker

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
OLLAMA_BASE_URL       = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL          = os.getenv("OLLAMA_MODEL", "llama3.2")
BODY_TRUNCATION_LIMIT = 8000   # chars — truncate very long emails before LLM

# Set to True once agent/agent.py exists
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "false").lower() == "true"

# Module-level sentiment tracker singleton (stateless, safe to share)
_sentiment_tracker = SentimentTracker()


# ─────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────
async def classify_email(email_id: int, db: Session) -> ClassificationResult | None:
    """
    Full classification pipeline for one email.

    Args:
        email_id: Primary key of the email row in the emails table
        db: SQLAlchemy session

    Returns:
        ClassificationResult if successful, None if email not found or pre-filtered

    Side effects:
        - Updates the email row with classification results
        - Calls SentimentTracker to detect deterioration post-classification
        - Creates an Action row with the reasoning trace
        - Optionally runs the agent (if AGENT_ENABLED=true)
    """

    # ── 1. Load email from DB ──────────────────────────────────────────
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        logger.error(f"Email {email_id} not found")
        return None

    # ── IDEMPOTENCY CHECK ──────────────────────────────────────────────
    # If this message_id has already been fully classified (status is not
    # 'Received' or 'Processing'), return early to avoid double-processing
    # duplicate webhook deliveries or retry storms.
    # 'Received' and 'Processing' are the only valid entry states.
    _ALREADY_CLASSIFIED_STATUSES = {"Escalated", "Resolved", "Ignored"}
    if email.status in _ALREADY_CLASSIFIED_STATUSES and email.category is not None:
        logger.info(
            f"[IDEMPOTENCY] Email {email.message_id} already classified "
            f"(status={email.status}, category={email.category}) — skipping duplicate run."
        )
        return None

    # Check for duplicate message_id across all rows (separate ingestion duplicate)
    duplicate = (
        db.query(Email)
        .filter(
            Email.message_id == email.message_id,
            Email.id         != email.id,
            Email.category.isnot(None),   # already classified copy exists
        )
        .first()
    )
    if duplicate:
        logger.warning(
            f"[IDEMPOTENCY] Duplicate message_id '{email.message_id}' detected — "
            f"a classified copy already exists (id={duplicate.id}, "
            f"status={duplicate.status}). Marking this row as duplicate and skipping."
        )
        email.status = "Ignored"
        db.commit()
        return None

    # ── MALFORMED PAYLOAD VALIDATION ──────────────────────────────────
    # Validate required fields and sanitise before reaching the LLM.
    # Malformed inputs are logged and given a safe fallback classification
    # rather than raising an unhandled exception.
    validation_error = _validate_email_payload(email)
    if validation_error:
        logger.error(
            f"[VALIDATION] Email {email.message_id} failed payload validation: "
            f"{validation_error} — routing to human review."
        )
        fallback = _fallback_classification(reason=f"Malformed payload: {validation_error}")
        # Persist a minimal classification so the row is not left in limbo
        email.category       = fallback.category
        email.urgency        = fallback.urgency
        email.requires_human = True
        email.confidence     = 0.0
        email.status         = "Escalated"
        db.commit()
        _write_action(
            email_id=email_id,
            result=fallback,
            rag_chunks=[],
            llm_note=f"Skipped — validation error: {validation_error}",
            db=db,
        )
        return fallback

    # ── 2. Heuristic pre-filter ────────────────────────────────────────
    # Runs BEFORE the LLM. Catches security/spam/legal instantly.
    # Must happen after the null-check above.
    pf_result = prefilter(
        sender=email.sender,
        subject=email.subject or "",
        body=email.body or "",
    )

    if pf_result.skip_llm:
        email.status = prefilter_to_db_status(pf_result)
        db.commit()
        _write_prefilter_action(email_id=email_id, pf_result=pf_result, db=db)
        logger.info(
            f"[PREFILTER] Skipped LLM for {email.message_id} "
            f"(queue={pf_result.queue}): {pf_result.note}"
        )
        return None  # pre-filtered emails don't return a ClassificationResult

    email.status = "Processing"
    db.commit()

    logger.info(f"Classifying email {email.message_id} from {email.sender}")

    try:
        # ── 3. RAG retrieval ───────────────────────────────────────────
        subject   = email.subject or ""
        body      = email.body or ""
        rag_query = f"{subject} {body[:500]}"

        chunks      = retrieve(rag_query, db=db)
        rag_context = format_rag_context(chunks)

        logger.info(
            f"RAG retrieved {len(chunks)} chunks: "
            + ", ".join(f"{c.source_doc}({c.similarity_score})" for c in chunks)
        )

        # ── 4. Thread history ──────────────────────────────────────────
        thread_history = _load_thread_history(email, db)

        # ── 5. Build prompt ────────────────────────────────────────────
        current_email_dict = {
            "sender":    email.sender,
            "subject":   email.subject or "(no subject)",
            "body":      _truncate_body(body),
            "timestamp": email.timestamp.isoformat() if email.timestamp else "unknown",
        }

        user_prompt = build_user_prompt(
            rag_context=rag_context,
            thread_history=thread_history,
            current_email=current_email_dict,
        )

        # ── 6. Call Ollama ─────────────────────────────────────────────
        raw_response, llm_note = await _call_llm(user_prompt)

        # ── 7. Parse + validate ────────────────────────────────────────
        result = _parse_llm_response(raw_response)

        # ── 8. Safety rules ────────────────────────────────────────────
        result = result.apply_safety_rules()

        # ── 9. Write back to email row ─────────────────────────────────
        email.sentiment_score = result.sentiment_score
        email.category        = result.category
        email.urgency         = result.urgency
        email.requires_human  = result.requires_human
        email.confidence      = result.confidence
        email.raw_entities    = result.detected_entities.model_dump()
        email.status          = "Escalated" if result.requires_human else "Received"
        db.commit()

        # ── 10. Sentiment tracker — run after classification ───────────
        # This is the pipeline wiring that connects classification to
        # sentiment monitoring. Previously this only ran inside get_thread_history;
        # now it runs on every classification so the tracker stays current
        # even for emails that don't go through the agent loop.
        _run_sentiment_tracker(email=email, db=db)

        # ── 11. Write reasoning trace ──────────────────────────────────
        _write_action(
            email_id=email_id,
            result=result,
            rag_chunks=chunks,
            llm_note=llm_note,
            db=db,
        )

        logger.info(
            f"Classified {email.message_id}: category={result.category}, "
            f"urgency={result.urgency}, confidence={result.confidence:.2f}, "
            f"requires_human={result.requires_human}"
        )

        # ── 12. Agent (optional) ───────────────────────────────────────
        # Gated behind AGENT_ENABLED env var so classification keeps working
        # before agent/agent.py is built. Flip to true once agent is ready.
        if AGENT_ENABLED:
            try:
                from agent.agent import run_agent
                agent_result = await run_agent(email_id=email_id, db=db)
                logger.info(
                    f"Agent completed for {email.message_id}: "
                    f"decision={agent_result.get('final_decision')}, "
                    f"steps={agent_result.get('steps')}"
                )
            except ImportError:
                logger.warning(
                    "AGENT_ENABLED=true but agent/agent.py not found — skipping"
                )
            except Exception as e:
                logger.error(
                    f"Agent failed for {email.message_id}: {e}", exc_info=True
                )
                # Agent failure must NOT fail classification — log and continue

        return result

    except Exception as e:
        logger.error(f"Classification failed for email {email_id}: {e}", exc_info=True)
        email.status = "Received"
        db.commit()
        raise


# ─────────────────────────────────────────
# Sentiment tracker integration
# ─────────────────────────────────────────

def _run_sentiment_tracker(email: Email, db: Session) -> None:
    """
    Updates the rolling sentiment average for this sender and checks for
    deterioration (3+ consecutive negatives → escalation alert).

    Called after every successful classification so the analytics endpoint
    and agent both see up-to-date deterioration signals.

    Escalation alerts are logged and stored; the agent checks for them
    in get_thread_history() and can act on them in the ReAct loop.
    """
    try:
        # Load all classified emails from this sender (for rolling average)
        all_emails = (
            db.query(Email)
            .filter(
                Email.sender == email.sender,
                Email.sentiment_score.isnot(None),
            )
            .order_by(Email.timestamp.asc())
            .all()
        )

        scores = [e.sentiment_score for e in all_emails]
        if not scores:
            return

        # Update the rolling average for this contact
        _sentiment_tracker.update_rolling_average(email.sender, scores, db)

        # Check for deterioration (3+ consecutive negatives)
        deteriorating = _sentiment_tracker.detect_deterioration(email.sender, db)

        if deteriorating:
            logger.warning(
                f"[SentimentTracker] Deterioration detected for {email.sender} "
                f"after classifying {email.message_id} — "
                f"scores: {scores[-5:]}"
            )

            # Create an escalation alert for the customer_success team
            alert = _sentiment_tracker.create_escalation_alert(
                sender_email=email.sender,
                reason=(
                    f"3+ consecutive negative emails detected. "
                    f"Latest email: {email.message_id} "
                    f"(sentiment={email.sentiment_score:.2f}). "
                    f"Category: {email.category}. Urgency: {email.urgency}."
                ),
                severity="High",
                db=db,
            )

            if alert:
                logger.warning(
                    f"[SentimentTracker] Escalation alert created for {email.sender}: {alert}"
                )

    except Exception as e:
        # Non-fatal — sentiment tracking must not break classification
        logger.error(
            f"[SentimentTracker] Failed for {email.sender} after "
            f"classifying {email.message_id}: {e}",
            exc_info=True,
        )


# ─────────────────────────────────────────
# Ollama API call
# ─────────────────────────────────────────
async def _call_llm(user_prompt: str) -> tuple[str, str]:
    """
    Calls the local Ollama /api/chat endpoint.

    Uses the chat format (system + user messages) so the model
    respects the system prompt's JSON-only instruction.

    Returns:
        (raw_response_text, note_for_audit_log)
    """
    url = f"{OLLAMA_BASE_URL}/api/chat"

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": 1500,
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    raw  = data["message"]["content"].strip()
    note = f"ollama model={OLLAMA_MODEL}"
    return raw, note


# ─────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────
def _parse_llm_response(raw: str) -> ClassificationResult:
    """
    Parses the LLM's raw text into a ClassificationResult.
    Handles JSON wrapped in markdown fences.
    Falls back to safe human-review classification on any error.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$",           "", cleaned)
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}\nRaw: {raw[:500]}")
        return _fallback_classification(reason=f"JSON parse error: {e}")

    try:
        return ClassificationResult(**data)
    except Exception as e:
        logger.error(f"Schema validation failed: {e}\nData: {data}")
        return _fallback_classification(reason=f"Schema error: {e}")


def _fallback_classification(reason: str) -> ClassificationResult:
    """Safe fallback — always routes to human review."""
    return ClassificationResult(
        category="Other",
        sentiment="Neutral",
        sentiment_score=0.0,
        urgency="Medium",
        requires_human=True,
        escalation_reason=f"Classification error — {reason}",
        suggested_reply=None,
        confidence=0.0,
        detected_entities=DetectedEntities(),
        policy_citations=[],
    )


# ─────────────────────────────────────────
# Thread history loader
# ─────────────────────────────────────────
def _load_thread_history(email: Email, db: Session) -> list[dict]:
    """
    Loads all prior emails in this thread, oldest → newest.
    Excludes the current email being classified.
    """
    prior = (
        db.query(Email)
        .filter(
            Email.thread_id == email.thread_id,
            Email.id        != email.id,
            Email.timestamp <  email.timestamp,
        )
        .order_by(Email.timestamp.asc())
        .all()
    )

    return [
        {
            "sender":          e.sender,
            "subject":         e.subject or "(no subject)",
            "body":            _truncate_body(e.body or ""),
            "timestamp":       e.timestamp.isoformat() if e.timestamp else "unknown",
            "sentiment_score": e.sentiment_score,
        }
        for e in prior
    ]


# ─────────────────────────────────────────
# Action / reasoning trace writers
# ─────────────────────────────────────────
def _write_action(
    email_id: int,
    result: ClassificationResult,
    rag_chunks: list,
    llm_note: str,
    db: Session,
) -> None:
    """
    Writes the full Thought → Action → Observation trace to the
    actions table for the LLM classification path.
    """
    reasoning_log = [
        {
            "step":        "prefilter",
            "thought":     "Heuristic pre-filter passed — no security/spam/legal keywords matched",
            "action":      "prefilter(sender, subject, body)",
            "observation": "Routed to LLM classifier",
        },
        {
            "step":        "rag_retrieval",
            "thought":     "Retrieving relevant policy context for this email",
            "action":      "retrieve(subject + body[:500])",
            "observation": [
                {
                    "source_doc":    c.source_doc,
                    "similarity":    c.similarity_score,
                    "chunk_preview": c.chunk_text[:200],
                }
                for c in rag_chunks
            ],
        },
        {
            "step":        "llm_classification",
            "thought":     "Sending email + thread history + RAG context to LLM",
            "action":      f"ollama_api_call({llm_note})",
            "observation": "LLM returned structured JSON classification",
        },
        {
            "step":        "safety_rules",
            "thought":     "Applying hard-coded safety overrides",
            "action":      "apply_safety_rules()",
            "observation": {
                "category":          result.category,
                "sentiment":         result.sentiment,
                "sentiment_score":   result.sentiment_score,
                "urgency":           result.urgency,
                "confidence":        result.confidence,
                "requires_human":    result.requires_human,
                "escalation_reason": result.escalation_reason,
                "policy_citations":  result.policy_citations,
            },
        },
        {
            "step":        "sentiment_tracking",
            "thought":     "Running sentiment tracker to detect deterioration signals",
            "action":      "SentimentTracker.detect_deterioration(sender)",
            "observation": "Sentiment tracker updated; escalation alert raised if 3+ consecutive negatives detected.",
        },
    ]

    if result.category == "Spam":
        action_type = "Ignored"
    elif result.category == "Legal":
        action_type = "Legal-Flag"
    elif result.requires_human:
        action_type = "Escalate"
    elif result.suggested_reply:
        action_type = "Auto-Reply"
    else:
        action_type = "Escalate"

    action = Action(
        email_id=email_id,
        agent_reasoning_log=reasoning_log,
        action_type=action_type,
        proposed_content=result.suggested_reply,
        is_approved=False,
        approved_by=None,
        executed_at=None,
    )
    db.add(action)
    db.commit()


def _write_prefilter_action(
    email_id: int,
    pf_result,          # PrefilterResult
    db: Session,
) -> None:
    """
    Writes a minimal reasoning trace for emails that were stopped
    by the heuristic pre-filter (never reached the LLM).
    """
    queue_to_action = {
        "security": "Legal-Flag",
        "legal":    "Legal-Flag",
        "spam":     "Ignored",
        "internal": "Escalate",
    }

    reasoning_log = [
        {
            "step":        "prefilter",
            "thought":     "Running heuristic keyword checks before LLM",
            "action":      "prefilter(sender, subject, body)",
            "observation": {
                "queue":         pf_result.queue,
                "matched_rules": pf_result.matched_rules,
                "urgency_score": pf_result.urgency_score,
                "should_alert":  pf_result.should_alert,
                "alert_target":  pf_result.alert_target,
                "note":          pf_result.note,
            },
        },
        {
            "step":        "llm_classification",
            "thought":     "Skipped — pre-filter routed this email without LLM",
            "action":      "skip_llm=True",
            "observation": f"Queue={pf_result.queue}. LLM not called.",
        },
    ]

    action = Action(
        email_id=email_id,
        agent_reasoning_log=reasoning_log,
        action_type=queue_to_action.get(pf_result.queue, "Escalate"),
        proposed_content=None,
        is_approved=False,
        approved_by=None,
        executed_at=None,
    )
    db.add(action)
    db.commit()


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def _truncate_body(body: str, limit: int = BODY_TRUNCATION_LIMIT) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + f"\n\n[Body truncated at {limit} characters]"


async def classify_by_message_id(
    message_id: str, db: Session
) -> ClassificationResult | None:
    """
    Convenience wrapper for testing individual emails by message_id.

    Example:
        result = await classify_by_message_id("msg_006", db)
    """
    email = db.query(Email).filter(Email.message_id == message_id).first()
    if not email:
        logger.error(f"No email found with message_id={message_id}")
        return None
    return await classify_email(email.id, db)

def _validate_email_payload(email) -> "str | None":
    """
    Validates required fields on an Email row before classification begins.

    Returns None if valid, or a human-readable error string describing the
    first problem found. Errors are non-fatal — callers route to human review
    rather than raising.

    Checks:
      - sender is present and looks like a plausible email address
      - message_id is non-empty (required for idempotency)
      - body or subject must have some non-whitespace content
      - timestamp must be set (needed for thread ordering + GDPR deadlines)
      - thread_id must be set (needed for thread history queries)
    """
    import re as _re

    # 1. sender required and must be a plausible email address
    if not email.sender or not email.sender.strip():
        return "sender is missing or empty"
    if not _re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.sender.strip()):
        return f"sender '{email.sender}' does not look like a valid email address"

    # 2. message_id required (idempotency key)
    if not email.message_id or not email.message_id.strip():
        return "message_id is missing or empty — cannot guarantee idempotency"

    # 3. at least one of subject or body must have content
    subject_has_content = bool((email.subject or "").strip())
    body_has_content    = bool((email.body    or "").strip())
    if not subject_has_content and not body_has_content:
        return "both subject and body are empty — nothing to classify"

    # 4. timestamp required (thread ordering + GDPR deadline calculation)
    if email.timestamp is None:
        return "timestamp is missing — required for thread ordering and compliance deadlines"

    # 5. thread_id required (thread history queries)
    if not email.thread_id or not str(email.thread_id).strip():
        return "thread_id is missing — required for thread history retrieval"

    return None  # all checks passed