"""
Classifier Schemas
==================
Pydantic models for:
- EmailInput: what the classifier receives
- ClassificationResult: what the LLM returns (structured JSON)
- ThreadMessage: a single email in thread history

These are used for:
1. Validating LLM JSON output before writing to DB
2. Type-safe passing between classifier → agent → API
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime


# ─────────────────────────────────────────
# Input schemas
# ─────────────────────────────────────────

class ThreadMessage(BaseModel):
    """One email in the thread history passed to the classifier."""
    message_id: str
    sender: str
    subject: Optional[str] = None
    body: Optional[str] = None
    timestamp: datetime
    sentiment_score: Optional[float] = None   # filled if already classified


class EmailInput(BaseModel):
    """
    Everything the classifier needs to classify one email.
    Assembled by the ingestion pipeline before calling classify().
    """
    message_id: str
    sender: str
    subject: Optional[str] = ""
    body: Optional[str] = ""
    timestamp: datetime
    thread_id: str
    thread_history: list[ThreadMessage] = Field(default_factory=list)
    # thread_history includes all PRIOR emails in the thread,
    # oldest first. The current email is NOT in this list.


# ─────────────────────────────────────────
# Output schemas
# ─────────────────────────────────────────

class DetectedEntities(BaseModel):
    """Named entities extracted from the email body."""
    order_ids:          list[str] = Field(default_factory=list)
    ticket_ids:         list[str] = Field(default_factory=list)
    monetary_amounts:   list[str] = Field(default_factory=list)
    deadlines:          list[str] = Field(default_factory=list)
    products_mentioned: list[str] = Field(default_factory=list)

    @field_validator(
        "order_ids", "ticket_ids", "monetary_amounts",
        "deadlines", "products_mentioned",
        mode="before"
    )
    @classmethod
    def coerce_to_strings(cls, v):
        if not isinstance(v, list):
            return []
        # Convert each item to string, drop Nones
        return [str(item) for item in v if item is not None]

class ClassificationResult(BaseModel):
    """
    Structured output from the LLM classifier.
    Every field maps directly to a column in the emails table.

    The LLM is instructed to return exactly this JSON schema.
    We validate it here before writing to the database.
    """

    # Core classification
    category: str = Field(
        description="One of: Complaint, Inquiry, Bug Report, Feature Request, "
                    "Compliance, Legal, Billing, Spam, Internal, Other"
    )
    sentiment: str = Field(
        description="One of: Positive, Neutral, Negative, Mixed"
    )
    sentiment_score: float = Field(
        description="Float from -1.0 (very negative) to +1.0 (very positive)"
    )
    urgency: str = Field(
        description="One of: Critical, High, Medium, Low"
    )

    # Human review decision
    requires_human: bool = Field(
        description="True if this email needs human review before any action"
    )
    escalation_reason: Optional[str] = Field(
        default=None,
        description="Why human review is needed. Required if requires_human=True."
    )

    # Agent output
    suggested_reply: Optional[str] = Field(
        default=None,
        description="Draft reply text. Only provided if requires_human=False."
    )

    # Confidence — below 0.70 forces requires_human=True
    confidence: float = Field(
        description="LLM confidence in this classification, 0.0 to 1.0"
    )

    # Entities
    detected_entities: DetectedEntities = Field(
        default_factory=DetectedEntities
    )

    # Which policy docs the LLM cited (from RAG context)
    policy_citations: list[str] = Field(
        default_factory=list,
        description="List of source_doc filenames cited in the response"
    )

    @field_validator("sentiment_score")
    @classmethod
    def clamp_sentiment(cls, v: float) -> float:
        return max(-1.0, min(1.0, v))

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        valid = {
            "Complaint", "Inquiry", "Bug Report", "Feature Request",
            "Compliance", "Legal", "Billing", "Spam", "Internal", "Other"
        }
        if v not in valid:
            return "Other"
        return v

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        valid = {"Critical", "High", "Medium", "Low"}
        if v not in valid:
            return "Medium"
        return v

    @field_validator("sentiment")
    @classmethod
    def validate_sentiment(cls, v: str) -> str:
        valid = {"Positive", "Neutral", "Negative", "Mixed"}
        if v not in valid:
            return "Neutral"
        return v

    def apply_safety_rules(self) -> "ClassificationResult":
        """
        Hard-coded safety rules that override LLM output.
        Called after validation, before writing to DB.
        """
        # Rule 1: low confidence → human review
        if self.confidence < 0.70:
            self.requires_human = True
            if not self.escalation_reason:
                self.escalation_reason = (
                    f"Low confidence ({self.confidence:.2f}) — human review required."
                )

        # Rule 2: Critical urgency → always human review
        if self.urgency == "Critical":
            self.requires_human = True
            if not self.escalation_reason:
                self.escalation_reason = "Critical urgency — never auto-reply."

        # Rule 3: Legal or Compliance → always human review
        if self.category in ("Legal", "Compliance"):
            self.requires_human = True
            if not self.escalation_reason:
                self.escalation_reason = (
                    f"{self.category} category requires legal/compliance team review."
                )

        # Rule 4: Complaint with review threat keywords → cap urgency at High
        # The LLM tends to over-escalate review threats to Critical.
        # Critical is reserved for outages, data loss, security threats.
        if self.category == "Complaint" and self.urgency == "Critical":
            self.urgency = "High"

        # Rule 5: Strip hallucinated policy citations — only allow real filenames
        valid_citations = {
            "pricing_policy.md",
            "sla_policy.md",
            "refund_policy.md",
            "api_docs.md",
            "compliance_faq.md",
            "escalation_matrix.md",
        }
        self.policy_citations = [
            c for c in self.policy_citations if c in valid_citations
        ]

        # Rule 6: If requires_human, wipe suggested_reply
        if self.requires_human:
            self.suggested_reply = None

        return self