"""
seed.py
=======
Run once to populate the database with the 60 test emails.

Usage:
    python seed.py

What it does:
    1. Creates all tables (safe to run on empty DB)
    2. Inserts contacts (one per unique sender, with known VIP data)
    3. Inserts threads (one per unique thread_id)
    4. Inserts emails (all 60, AI fields left null — classifier fills those)

Idempotent: re-running skips rows that already exist.
"""

import json
import os
import sys
from datetime import datetime, timezone
import sys, os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── path fix so this runs from the project root ───────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from db.models import Base, Contact, Thread, Email
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/senai_crm"
)
JSON_PATH = os.getenv("EMAIL_DATA_PATH", "email-data-advanced.json")

# ── Known VIP / high-value contacts to seed with extra data ──────────
KNOWN_CONTACTS = {
    "bob.jones@enterprise.net": {
        "name": "Bob Jones",
        "company": "Enterprise Inc.",
        "status": "VIP",
        "account_value": 50000.0,
        "churn_risk_score": 0.7,
    },
    "karen.w@retail-co.com": {
        "name": "Karen W",
        "company": "Retail Co.",
        "status": "Active",
        "account_value": 5000.0,
        "churn_risk_score": 0.85,
    },
    "eleanor.voss@healthcare-group.org": {
        "name": "Eleanor Voss",
        "company": "Healthcare Group",
        "status": "Active",
        "account_value": 200000.0,
        "churn_risk_score": 0.4,
    },
    "procurement@bigcorp-global.com": {
        "name": "BigCorp Procurement",
        "company": "BigCorp Global",
        "status": "Active",
        "account_value": 2400000.0,
        "churn_risk_score": 0.1,
    },
    "alice.smith@greenlight-npo.org": {
        "name": "Alice Smith",
        "company": "Greenlight NPO",
        "status": "Active",
        "account_value": 3000.0,
        "churn_risk_score": 0.1,
    },
}

# ── Threads that should start pre-escalated ───────────────────────────
ESCALATED_THREADS = {
    "thread_security_001",
    "thread_security_002",
    "thread_legal_001",
}

IGNORED_THREADS = {
    "thread_spam_001",
    "thread_spam_002",
    "thread_spam_003",
    "thread_spam_004",
}


def parse_timestamp(ts: str) -> datetime:
    """Parse ISO timestamp string → aware datetime → strip tz for Postgres."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.replace(tzinfo=None)  # store as naive UTC (matches your model)


def seed():
    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(bind=engine)  # creates tables if they don't exist
    Session = sessionmaker(bind=engine)
    db = Session()

    # ── Load JSON ─────────────────────────────────────────────────────
    with open(JSON_PATH) as f:
        emails_data = json.load(f)

    print(f"Loaded {len(emails_data)} emails from {JSON_PATH}")

    # ── 1. Build unique contacts ──────────────────────────────────────
    # Collect first-seen and last-seen timestamps per sender
    sender_times: dict[str, list[str]] = {}
    for e in emails_data:
        sender_times.setdefault(e["sender"], []).append(e["timestamp"])

    contacts_inserted = 0
    contacts_skipped = 0

    for sender_email, timestamps in sender_times.items():
        # Skip if already in DB (idempotent)
        existing = db.query(Contact).filter(Contact.email == sender_email).first()
        if existing:
            contacts_skipped += 1
            continue

        known = KNOWN_CONTACTS.get(sender_email, {})
        timestamps_sorted = sorted(timestamps)

        contact = Contact(
            email=sender_email,
            name=known.get("name"),
            company=known.get("company"),
            status=known.get("status", "Active"),
            account_value=known.get("account_value", 0.0),
            churn_risk_score=known.get("churn_risk_score", 0.0),
            created_at=parse_timestamp(timestamps_sorted[0]),
            last_contact_at=parse_timestamp(timestamps_sorted[-1]),
        )
        db.add(contact)
        contacts_inserted += 1

    db.commit()
    print(f"Contacts: {contacts_inserted} inserted, {contacts_skipped} skipped")

    # ── 2. Build unique threads ───────────────────────────────────────
    # Group emails by thread_id to find subject, first/last timestamps
    thread_map: dict[str, dict] = {}
    for e in emails_data:
        tid = e["thread_id"]
        if tid not in thread_map:
            thread_map[tid] = {
                "subject": e["subject"],
                "sender_email": e["sender"],
                "first_seen_at": e["timestamp"],
                "last_updated_at": e["timestamp"],
            }
        else:
            if e["timestamp"] < thread_map[tid]["first_seen_at"]:
                thread_map[tid]["first_seen_at"] = e["timestamp"]
                thread_map[tid]["sender_email"] = e["sender"]  # original sender
                thread_map[tid]["subject"] = e["subject"]
            if e["timestamp"] > thread_map[tid]["last_updated_at"]:
                thread_map[tid]["last_updated_at"] = e["timestamp"]

    threads_inserted = 0
    threads_skipped = 0

    for tid, tdata in thread_map.items():
        existing = db.query(Thread).filter(Thread.thread_id == tid).first()
        if existing:
            threads_skipped += 1
            continue

        if tid in ESCALATED_THREADS:
            status = "Escalated"
        elif tid in IGNORED_THREADS:
            status = "Ignored"
        else:
            status = "Open"

        thread = Thread(
            thread_id=tid,
            subject=tdata["subject"],
            sender_email=tdata["sender_email"],
            first_seen_at=parse_timestamp(tdata["first_seen_at"]),
            last_updated_at=parse_timestamp(tdata["last_updated_at"]),
            status=status,
            assigned_to=None,
        )
        db.add(thread)
        threads_inserted += 1

    db.commit()
    print(f"Threads:  {threads_inserted} inserted, {threads_skipped} skipped")

    # ── 3. Insert emails ──────────────────────────────────────────────
    # Build thread_id string → DB row id lookup
    thread_lookup: dict[str, int] = {
        t.thread_id: t.id
        for t in db.query(Thread).all()
    }

    emails_inserted = 0
    emails_skipped = 0

    for e in emails_data:
        existing = db.query(Email).filter(Email.message_id == e["message_id"]).first()
        if existing:
            emails_skipped += 1
            continue

        thread_pk = thread_lookup.get(e["thread_id"])
        if thread_pk is None:
            print(f"  WARNING: no thread found for {e['thread_id']} — skipping {e['message_id']}")
            continue

        email_row = Email(
            thread_id=thread_pk,          # FK to threads.id (integer)
            message_id=e["message_id"],
            sender=e["sender"],
            subject=e.get("subject"),
            body=e.get("body"),
            timestamp=parse_timestamp(e["timestamp"]),
            # AI fields — all null, filled by classifier after seeding
            sentiment_score=None,
            category=None,
            urgency=None,
            requires_human=None,
            confidence=None,
            raw_entities=None,
            status="Received",
        )
        db.add(email_row)
        emails_inserted += 1

    db.commit()
    print(f"Emails:   {emails_inserted} inserted, {emails_skipped} skipped")
    print("\nDone. Run your classifier to fill in the AI fields.")
    db.close()


if __name__ == "__main__":
    seed()