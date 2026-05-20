from sqlalchemy import (
    Column, String, Float, Boolean, Integer,
    DateTime, Text,JSON, ForeignKey, Index
)
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector
from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB


# ─────────────────────────────────────────
# Base — every model inherits from this
# ─────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────
# 1. Contact
#    One row per unique sender email.
#    Tracks VIP status, account value,
#    and churn risk (updates as sentiment drops).
# ─────────────────────────────────────────
class Contact(Base):
    __tablename__ = "contacts"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    email            = Column(String, unique=True, nullable=False)
    name             = Column(String, nullable=True)
    company          = Column(String, nullable=True)
    status           = Column(String, default="Active")   # VIP | Blocked | Active | Churned
    account_value    = Column(Float,  default=0.0)
    churn_risk_score = Column(Float,  default=0.0)        # 0.0 (safe) → 1.0 (churning)
    created_at       = Column(DateTime, default=datetime.utcnow)
    last_contact_at  = Column(DateTime, nullable=True)

    # Relationships — navigate to related objects without extra queries
    threads = relationship("Thread", back_populates="contact")


# ─────────────────────────────────────────
# 2. Thread
#    Groups emails into conversations.
#    One thread = one ongoing exchange
#    with a sender (e.g. thread_bob_outage).
# ─────────────────────────────────────────
class Thread(Base):
    __tablename__ = "threads"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    thread_id       = Column(String, unique=True, nullable=False)   # e.g. "thread_bob_outage"
    subject         = Column(String, nullable=True)
    sender_email    = Column(String, ForeignKey("contacts.email"), nullable=False)
    first_seen_at   = Column(DateTime, default=datetime.utcnow)
    last_updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    status          = Column(String, default="Open")      # Open | Resolved | Escalated | Ignored
    assigned_to     = Column(String, nullable=True)       # team member name or ID

    # Relationships
    contact = relationship("Contact", back_populates="threads")
    emails  = relationship(
        "Email",
        back_populates="thread",
        order_by="Email.timestamp"   # always return emails oldest → newest
    )


# ─────────────────────────────────────────
# 3. Email
#    Core table. One row per email.
#    AI-generated fields are null on arrival,
#    filled in after classification.
# ─────────────────────────────────────────
class Email(Base):
    __tablename__ = "emails"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    thread_id       = Column(Integer, ForeignKey("threads.id"), nullable=False)
    message_id      = Column(String, unique=True, nullable=False)   # e.g. "msg_060"
    sender          = Column(String, nullable=False)
    subject         = Column(String, nullable=True)
    body            = Column(Text,   nullable=True)
    timestamp       = Column(DateTime, nullable=False)

    # Filled in after AI classification
    sentiment_score = Column(Float,   nullable=True)   # -1.0 (very negative) → +1.0 (positive)
    category        = Column(String,  nullable=True)   # Complaint | Inquiry | Bug Report | etc.
    urgency         = Column(String,  nullable=True)   # Critical | High | Medium | Low
    requires_human  = Column(Boolean, default=False)
    confidence      = Column(Float,   nullable=True)   # 0.0 → 1.0, below 0.7 = human review
    raw_entities    = Column(JSONB, default=dict)    # order_ids, amounts, deadlines, etc.
    status          = Column(String,  default="Received")  # Received | Processing | Replied | Escalated | Ignored

    # Relationships
    thread  = relationship("Thread", back_populates="emails")
    actions = relationship("Action", back_populates="email")

# Indexes defined at table level
    __table_args__ = (
        # Speeds up sentiment trend query: "all emails from sender X ordered by time"
        Index("ix_emails_sender_timestamp", "sender", "timestamp"),
        # NOTE: GIN index on raw_entities is added manually in the migration
        # because SQLAlchemy cannot generate jsonb_ops operator class syntax
    )


# ─────────────────────────────────────────
# 4. Action
#    Every decision the agent made.
#    The agent_reasoning_log stores the full
#    Thought → Action → Observation chain.
#    This is what makes the agent auditable.
# ─────────────────────────────────────────
class Action(Base):
    __tablename__ = "actions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    email_id            = Column(Integer, ForeignKey("emails.id"), nullable=False)
    agent_reasoning_log = Column(JSON,    default=list)    # [{thought, action, observation}, ...]
    action_type         = Column(String,  nullable=True)   # Auto-Reply | Escalate | Legal-Flag | Ticket-Created | Ignored
    proposed_content    = Column(Text,    nullable=True)   # the draft reply text
    is_approved         = Column(Boolean, default=False)
    approved_by         = Column(String,  nullable=True)   # "agent" or human user ID
    executed_at         = Column(DateTime, nullable=True)

    # Relationship
    email = relationship("Email", back_populates="actions")


# ─────────────────────────────────────────
# 5. KnowledgeChunk
#    RAG pipeline data.
#    Each row is one ~400-token chunk
#    of a policy document plus its embedding.
#    The Vector(384) column is what pgvector
#    uses for similarity search.
# ─────────────────────────────────────────
class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    source_doc = Column(String, nullable=False)    # e.g. "refund_policy.md"
    chunk_text = Column(Text,   nullable=False)    # the actual policy text
    embedding  = Column(Vector(384), nullable=True) # 384 dims = all-MiniLM-L6-v2 output size
    created_at = Column(DateTime, default=datetime.utcnow)

    # NOTE: The IVFFlat index for this column is added manually in the
    # Alembic migration file because SQLAlchemy cannot autogenerate it.
    # See migrations/versions/xxx_create_all_tables.py → upgrade()


# ─────────────────────────────────────────
# 6. WebIntelligenceCache
#    Stores scraped public sentiment data
#    with a 6-hour expiry to avoid
#    hitting rate limits repeatedly.
# ─────────────────────────────────────────
class WebIntelligenceCache(Base):
    __tablename__ = "web_intelligence_cache"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    source_url    = Column(String,  nullable=True)
    target_entity = Column(String,  nullable=False)   # company name being monitored
    scraped_data  = Column(JSON,    default=dict)     # {rating, review_count, themes, raw}
    scraped_at    = Column(DateTime, default=datetime.utcnow)
    expires_at    = Column(DateTime, nullable=False)  # scraped_at + 6 hours


# ─────────────────────────────────────────
# 7. AuditLog
#    Immutable record of every state change.
#    Required for compliance (GDPR) and
#    for debugging agent decisions.
#    Nothing is ever deleted from this table.
# ─────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    entity_type  = Column(String,  nullable=False)   # "email" | "thread" | "contact" | "action"
    entity_id    = Column(Integer, nullable=False)   # the ID of whatever changed
    action       = Column(String,  nullable=False)   # "status_changed" | "agent_decision" | "human_approved"
    performed_by = Column(String,  nullable=False)   # "agent" or a user ID string
    timestamp    = Column(DateTime, default=datetime.utcnow)
    diff         = Column(JSON,    default=dict)     # {"before": "Received", "after": "Escalated"}