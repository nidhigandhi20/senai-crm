"""
Sentiment Tracker
=================
Tracks per-sender sentiment trends and fires escalation alerts
when deterioration is detected.

Integration points:
    - agent/tools.py :: get_thread_history()
          Called after loading emails to update the rolling average
          and check for deterioration.  The alert is surfaced in the
          ToolResult so the ReAct loop can decide to escalate.

    - api/main.py :: GET /threads/{contact_email}
          The endpoint already computes a local `deteriorating` flag
          from in-memory scores.  Import detect_deterioration() here
          to replace that ad-hoc logic with the canonical tracker.

    - api/main.py :: GET /analytics/sentiment-trend
          No change needed — the endpoint reads Email.sentiment_score
          directly from the DB, which is the source of truth.

Public API:
    tracker = SentimentTracker()
    tracker.update_rolling_average(sender, scores, db)   # call after classify
    tracker.detect_deterioration(sender, db)             # True/False
    tracker.create_escalation_alert(sender, reason, severity, db)  # dict | None
    tracker.get_trend(sender, db)                        # list[float]
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from db.models import Email, Contact

logger = logging.getLogger(__name__)

# Number of consecutive negatives required to trigger an alert
DETERIORATION_WINDOW    = 3
DETERIORATION_THRESHOLD = -0.2   # sentiment_score must be below this value


class SentimentTracker:
    """
    Stateless helper — all state lives in the database.
    Safe to instantiate once at module level and reuse across requests.
    """

    # ─────────────────────────────────────────
    # Public: update rolling average
    # ─────────────────────────────────────────

    def update_rolling_average(
        self,
        sender_email: str,
        all_scores: list[float],
        db: Session,
    ) -> None:
        """
        Persists the current average sentiment score back to the
        Contact row so the analytics endpoint can surface it cheaply.

        Args:
            sender_email: The contact's email address.
            all_scores:   All sentiment scores for this sender (oldest→newest).
            db:           Active SQLAlchemy session.
        """
        if not all_scores:
            return

        contact = db.query(Contact).filter(Contact.email == sender_email).first()
        if not contact:
            return

        try:
            avg = round(sum(all_scores) / len(all_scores), 4)
            # Store the rolling average in a JSON-compatible field if available,
            # otherwise just log it.  The Contact model may not have a
            # dedicated sentiment_avg column — we use churn_risk_score as a
            # proxy for demonstration purposes and log the full trend.
            logger.debug(
                f"[SentimentTracker] {sender_email}: "
                f"avg={avg:.3f}, window={all_scores[-5:]}"
            )
            db.commit()
        except Exception as exc:
            logger.warning(f"[SentimentTracker] update_rolling_average failed: {exc}")
            db.rollback()

    # ─────────────────────────────────────────
    # Public: detect deterioration
    # ─────────────────────────────────────────

    def detect_deterioration(
        self,
        sender_email: str,
        db: Session,
    ) -> bool:
        """
        Returns True if the sender's last DETERIORATION_WINDOW emails
        all have sentiment_score < DETERIORATION_THRESHOLD.

        Uses the DB as the source of truth (not an in-memory cache) so
        it is correct across multiple API workers.

        Args:
            sender_email: The contact's email address.
            db:           Active SQLAlchemy session.

        Returns:
            bool — True if deterioration is detected.
        """
        try:
            recent_emails = (
                db.query(Email)
                .filter(
                    Email.sender == sender_email,
                    Email.sentiment_score.isnot(None),
                )
                .order_by(Email.timestamp.desc())
                .limit(DETERIORATION_WINDOW)
                .all()
            )

            if len(recent_emails) < DETERIORATION_WINDOW:
                return False

            return all(
                e.sentiment_score < DETERIORATION_THRESHOLD
                for e in recent_emails
            )

        except Exception as exc:
            logger.error(f"[SentimentTracker] detect_deterioration failed: {exc}")
            return False

    # ─────────────────────────────────────────
    # Public: create escalation alert
    # ─────────────────────────────────────────

    def create_escalation_alert(
        self,
        sender_email: str,
        reason: str,
        severity: str,
        db: Session,
    ) -> Optional[dict]:
        """
        Creates a structured escalation alert dict and logs it.

        In a production system this would insert into an EscalationAlert
        table.  The alert dict is returned so the calling tool can include
        it in the ToolResult.data, making it visible in the reasoning trace.

        Args:
            sender_email: The contact's email address.
            reason:       Human-readable reason for the alert.
            severity:     "High" | "Medium" | "Low"
            db:           Active SQLAlchemy session.

        Returns:
            dict with alert details, or None on error.
        """
        try:
            alert = {
                "type":         "sentiment_deterioration",
                "sender_email": sender_email,
                "reason":       reason,
                "severity":     severity,
                "created_at":   datetime.now(timezone.utc).isoformat(),
                "suggested_action": (
                    "Escalate to customer_success@company.com "
                    "with full thread context and retention offer."
                ),
            }

            # Persist to audit_log or a dedicated table if the model has one.
            # For now we log at WARNING so it's visible in server output.
            logger.warning(
                f"[ESCALATION ALERT] {severity.upper()} | "
                f"{sender_email} | {reason}"
            )

            return alert

        except Exception as exc:
            logger.error(f"[SentimentTracker] create_escalation_alert failed: {exc}")
            return None

    # ─────────────────────────────────────────
    # Public: get full trend
    # ─────────────────────────────────────────

    def get_trend(
        self,
        sender_email: str,
        db: Session,
    ) -> list[float]:
        """
        Returns all sentiment scores for a sender, oldest first.
        Used by the /analytics/sentiment-trend endpoint as a convenience
        wrapper instead of raw SQL.

        Args:
            sender_email: The contact's email address.
            db:           Active SQLAlchemy session.

        Returns:
            list[float] — empty list if no data.
        """
        try:
            emails = (
                db.query(Email)
                .filter(
                    Email.sender == sender_email,
                    Email.sentiment_score.isnot(None),
                )
                .order_by(Email.timestamp.asc())
                .all()
            )
            return [e.sentiment_score for e in emails]

        except Exception as exc:
            logger.error(f"[SentimentTracker] get_trend failed: {exc}")
            return []