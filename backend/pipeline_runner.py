"""
Pipeline Runner
===============
Classifies all unprocessed emails in the database.

Runs the full pipeline for every email with status="Received":
  1. Heuristic pre-filter (< 10ms per email)
  2. LLM classification via Ollama (only for emails that pass the filter)
  3. Safety rules
  4. Writes results + reasoning trace to DB

Usage:
    # Classify everything not yet processed
    python -m pipeline_runner

    # Dry run — show what would be processed without doing it
    python -m pipeline_runner --dry-run

    # Classify a specific message_id
    python -m pipeline_runner --message-id msg_033

    # Classify a specific email by DB id
    python -m pipeline_runner --email-id 42

    # Limit how many emails to process (useful for testing)
    python -m pipeline_runner --limit 10

    # Re-process already-classified emails (resets status to Received first)
    python -m pipeline_runner --reprocess

    # Concurrency (default: 1 — Ollama is slow, parallelism rarely helps)
    python -m pipeline_runner --concurrency 3
"""

import asyncio
import argparse
import logging
import sys
import os
import time
from datetime import datetime

# ── path fix so this runs from the project root ───────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.orm import Session
from db.database import SessionLocal
from db.models import Email
from classifier.engine import classify_email, classify_by_message_id

# ─────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline_runner")


# ─────────────────────────────────────────
# Stats tracker
# ─────────────────────────────────────────
class RunStats:
    def __init__(self):
        self.total      = 0
        self.classified = 0
        self.prefiltered = 0
        self.failed     = 0
        self.skipped    = 0
        self.start_time = time.time()

        # Breakdown counters
        self.by_category: dict[str, int] = {}
        self.by_queue:    dict[str, int] = {}   # prefilter queues

    def record_classified(self, category: str):
        self.classified += 1
        self.by_category[category] = self.by_category.get(category, 0) + 1

    def record_prefiltered(self, queue: str):
        self.prefiltered += 1
        self.by_queue[queue] = self.by_queue.get(queue, 0) + 1

    def record_failed(self):
        self.failed += 1

    def record_skipped(self):
        self.skipped += 1

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def print_summary(self):
        elapsed = self.elapsed()
        processed = self.classified + self.prefiltered
        rate = processed / elapsed if elapsed > 0 else 0

        print("\n" + "═" * 55)
        print("  PIPELINE RUN COMPLETE")
        print("═" * 55)
        print(f"  Total emails found  : {self.total}")
        print(f"  LLM classified      : {self.classified}")
        print(f"  Pre-filtered        : {self.prefiltered}")
        print(f"  Failed              : {self.failed}")
        print(f"  Skipped (dry-run)   : {self.skipped}")
        print(f"  Elapsed             : {elapsed:.1f}s")
        print(f"  Throughput          : {rate:.2f} emails/s")

        if self.by_category:
            print("\n  Category breakdown:")
            for cat, count in sorted(self.by_category.items(), key=lambda x: -x[1]):
                print(f"    {cat:<20} {count}")

        if self.by_queue:
            print("\n  Pre-filter queues:")
            for queue, count in sorted(self.by_queue.items(), key=lambda x: -x[1]):
                print(f"    {queue:<20} {count}")

        print("═" * 55 + "\n")


# ─────────────────────────────────────────
# Core runner
# ─────────────────────────────────────────
async def run_pipeline(
    limit: int | None = None,
    concurrency: int = 1,
    dry_run: bool = False,
    reprocess: bool = False,
) -> RunStats:
    """
    Classifies all unprocessed emails.

    Args:
        limit:       Max emails to process (None = all)
        concurrency: Number of emails to classify in parallel
        dry_run:     If True, print what would be processed but don't do it
        reprocess:   If True, re-classify emails that are already classified
                     (resets status to "Received" first)

    Returns:
        RunStats with counts and timing
    """
    db: Session = SessionLocal()
    stats = RunStats()

    try:
        # ── Build query ────────────────────────────────────────────────
        query = db.query(Email)

        if reprocess:
            # Re-classify everything (reset first)
            logger.info("--reprocess: resetting all emails to Received status")
            db.query(Email).update({"status": "Received"})
            db.commit()
        else:
            # Only unprocessed emails
            query = query.filter(Email.status == "Received")

        # Order oldest first so threads are processed in order
        query = query.order_by(Email.timestamp.asc())

        if limit:
            query = query.limit(limit)

        emails = query.all()
        stats.total = len(emails)

        if stats.total == 0:
            logger.info("No unprocessed emails found. Run seed_db.py first.")
            return stats

        logger.info(
            f"Found {stats.total} email(s) to process"
            + (f" (limit={limit})" if limit else "")
            + (" [DRY RUN]" if dry_run else "")
        )

        if dry_run:
            for email in emails:
                stats.skipped += 1
                print(
                    f"  [DRY RUN] Would classify: {email.message_id} "
                    f"from {email.sender} — '{email.subject or '(no subject)'}'"
                )
            return stats

        # ── Process emails ─────────────────────────────────────────────
        if concurrency == 1:
            # Sequential — simplest, safest with a local Ollama model
            for i, email in enumerate(emails, 1):
                await _process_one(email.id, db, stats, i, stats.total)
        else:
            # Parallel with semaphore — useful if Ollama can handle concurrency
            semaphore = asyncio.Semaphore(concurrency)
            tasks = [
                _process_one_with_semaphore(email.id, semaphore, stats, i, stats.total)
                for i, email in enumerate(emails, 1)
            ]
            await asyncio.gather(*tasks)

    finally:
        db.close()

    return stats


async def _process_one(
    email_id: int,
    db: Session,
    stats: RunStats,
    current: int,
    total: int,
) -> None:
    """
    Processes a single email and updates stats.
    Uses a fresh DB session per email to avoid transaction conflicts.
    """
    # Each email gets its own session — prevents one failure from
    # poisoning the entire batch's transaction state
    email_db: Session = SessionLocal()

    try:
        email = email_db.query(Email).filter(Email.id == email_id).first()
        if not email:
            logger.warning(f"Email {email_id} disappeared — skipping")
            stats.record_skipped()
            return

        logger.info(
            f"[{current}/{total}] Processing {email.message_id} "
            f"from {email.sender}"
        )

        result = await classify_email(email_id, email_db)

        if result is None:
            # Pre-filtered (security/spam/legal) — not a failure
            # Re-fetch to get the updated status the prefilter wrote
            email_db.expire(email)
            email = email_db.query(Email).filter(Email.id == email_id).first()
            queue = _infer_prefilter_queue(email)
            stats.record_prefiltered(queue)
            logger.info(
                f"[{current}/{total}] Pre-filtered {email.message_id} "
                f"→ status={email.status}"
            )
        else:
            stats.record_classified(result.category)
            logger.info(
                f"[{current}/{total}] ✓ {email.message_id} → "
                f"{result.category} / {result.urgency} / "
                f"confidence={result.confidence:.2f} / "
                f"requires_human={result.requires_human}"
            )

    except Exception as e:
        logger.error(
            f"[{current}/{total}] ✗ Failed email_id={email_id}: {e}",
            exc_info=True,
        )
        stats.record_failed()
    finally:
        email_db.close()


async def _process_one_with_semaphore(
    email_id: int,
    semaphore: asyncio.Semaphore,
    stats: RunStats,
    current: int,
    total: int,
) -> None:
    """Wraps _process_one with a semaphore for controlled concurrency."""
    async with semaphore:
        db: Session = SessionLocal()
        try:
            await _process_one(email_id, db, stats, current, total)
        finally:
            db.close()


def _infer_prefilter_queue(email: Email) -> str:
    """Infer which prefilter queue caught this email from its status."""
    if email is None:
        return "unknown"
    if email.status == "Ignored":
        return "spam"
    if email.status == "Escalated":
        return "security/legal"
    return "internal"


# ─────────────────────────────────────────
# Single-email runners (for testing)
# ─────────────────────────────────────────
async def run_single_by_id(email_id: int) -> None:
    """Classify one email by its DB primary key."""
    db: Session = SessionLocal()
    try:
        result = await classify_email(email_id, db)
        if result:
            _print_result(result)
        else:
            print(f"Email {email_id} was pre-filtered or not found.")
    finally:
        db.close()


async def run_single_by_message_id(message_id: str) -> None:
    """Classify one email by its message_id string (e.g. 'msg_033')."""
    db: Session = SessionLocal()
    try:
        result = await classify_by_message_id(message_id, db)
        if result:
            _print_result(result)
        else:
            print(f"Email '{message_id}' was pre-filtered or not found.")
    finally:
        db.close()


def _print_result(result) -> None:
    """Pretty-print a ClassificationResult."""
    print("\n── Classification Result ──────────────────────────")
    print(f"  Category        : {result.category}")
    print(f"  Urgency         : {result.urgency}")
    print(f"  Sentiment       : {result.sentiment} ({result.sentiment_score:+.2f})")
    print(f"  Confidence      : {result.confidence:.2f}")
    print(f"  Requires human  : {result.requires_human}")
    if result.escalation_reason:
        print(f"  Escalation      : {result.escalation_reason}")
    if result.policy_citations:
        print(f"  Policy cited    : {', '.join(result.policy_citations)}")
    if result.suggested_reply:
        preview = result.suggested_reply[:120].replace("\n", " ")
        print(f"  Draft reply     : {preview}...")
    entities = result.detected_entities
    if any([entities.monetary_amounts, entities.deadlines, entities.order_ids]):
        print(f"  Entities        : {entities.model_dump()}")
    print("───────────────────────────────────────────────────\n")


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="SenAI email classification pipeline runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m pipeline_runner                          # classify all unprocessed
  python -m pipeline_runner --dry-run                # preview without classifying
  python -m pipeline_runner --limit 5                # classify first 5
  python -m pipeline_runner --message-id msg_033     # single email by message_id
  python -m pipeline_runner --email-id 42            # single email by DB id
  python -m pipeline_runner --reprocess              # re-classify everything
  python -m pipeline_runner --concurrency 3          # parallel (use with care)
        """,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be processed without classifying"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of emails to classify"
    )
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Number of parallel classification workers (default: 1)"
    )
    parser.add_argument(
        "--reprocess", action="store_true",
        help="Re-classify emails regardless of current status"
    )
    parser.add_argument(
        "--message-id", type=str, default=None,
        help="Classify a single email by message_id (e.g. msg_033)"
    )
    parser.add_argument(
        "--email-id", type=int, default=None,
        help="Classify a single email by DB primary key"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    print(f"\nSenAI Pipeline Runner  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("─" * 55)

    # Single-email modes
    if args.message_id:
        print(f"Single email mode: message_id={args.message_id}")
        await run_single_by_message_id(args.message_id)
        return

    if args.email_id:
        print(f"Single email mode: email_id={args.email_id}")
        await run_single_by_id(args.email_id)
        return

    # Batch mode
    stats = await run_pipeline(
        limit=args.limit,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        reprocess=args.reprocess,
    )

    stats.print_summary()

    # Exit with non-zero if there were failures
    if stats.failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())