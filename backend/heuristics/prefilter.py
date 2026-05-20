"""
Heuristic Pre-Filter
====================
Fast keyword-based routing that runs BEFORE the LLM classifier.
Takes < 10ms per email and handles the clearest-cut cases immediately.

Routing queues:
  - security: ransomware, extortion, data breach — bypass LLM, alert security team
  - spam:     SEO pitches, cold outreach, Nigerian prince — mark Ignored, no reply
  - legal:    cease and desist, lawsuit threats — bypass LLM, route to legal
  - internal: emails from internal domains — route to internal queue
  - normal:   everything else — proceed to LLM classifier

Usage:
    from heuristics.prefilter import prefilter
    result = prefilter(email)
    if result["queue"] != "normal":
        # handle immediately, skip LLM
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Keyword lists
# ─────────────────────────────────────────

SECURITY_KEYWORDS = [
    # Ransomware / extortion
    "send btc", "send bitcoin", "pay btc", "pay bitcoin",
    "ransom", "ransomware",
    "pay or we", "pay or i", "pay or they",
    "exfiltrated", "exfiltrate",
    "we have your data", "i have your data", "stolen your data",
    "publish your data", "leak your data", "release your data",
    "2 btc", "0.5 btc", "1 btc",
    # Data breach
    "data breach", "security breach", "unauthorized access",
    "suspicious login", "account compromised",
    "credentials exposed", "password leaked",
    # Threats
    "ddos", "denial of service",
    "zero day", "0day", "exploit",
]

SPAM_KEYWORDS = [
    # SEO / marketing pitches
    "boost your seo", "boost seo", "improve your rankings",
    "first page of google", "page 1 of google",
    "increase your traffic", "drive traffic",
    "backlink", "link building",
    # Generic cold outreach spam
    "limited time offer", "limited offer", "act now",
    "exclusive deal", "special promotion",
    "you've been selected", "you have been selected",
    "congratulations you", "you won", "you've won",
    "nigerian prince", "foreign prince",
    "wire transfer", "western union",
    "make money fast", "work from home opportunity",
    "cryptocurrency investment", "crypto opportunity",
    "unsubscribe from this list",
    "this is not spam",
    "per our last conversation" ,  # common cold outreach opener
    # Mass mailer tells
    "if you no longer wish to receive",
    "to opt out of future emails",
    "you are receiving this because you signed up",
]

LEGAL_KEYWORDS = [
    "cease and desist",
    "c&d", "c & d",
    "legal action",
    "lawsuit",
    "litigation",
    "my attorney",
    "our attorneys",
    "my lawyer",
    "our lawyers",
    "sue you", "will sue", "taking you to court",
    "file a complaint",
    "report you to",
    "regulatory complaint",
    "arbitration",
    "class action",
]

URGENCY_KEYWORDS = [
    "urgent", "urgently",
    "asap", "a.s.a.p",
    "immediately",
    "critical", "p0", "p-0",
    "emergency",
    "breach",
    "deadline",
    "legal",
    "lawsuit",
    "escalate",
    "threatening",
    "cancel immediately",
    "cancelling today",
    "canceling today",
]

# Internal sender domains — adjust to match your org
INTERNAL_DOMAINS = {
    "internal.com",
    "mycompany.com",
    "senai.io",
}


# ─────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────

@dataclass
class PrefilterResult:
    """
    Output of the heuristic pre-filter.

    queue:          Where to route this email
    urgency_score:  0.0–1.0 urgency signal (used to prioritise within queues)
    matched_rules:  Which keyword lists fired (for audit trail)
    should_alert:   Whether to ping an ops/security channel immediately
    alert_target:   Who to alert (e.g. security@company.com)
    skip_llm:       True if the LLM classifier should be skipped entirely
    note:           Short human-readable reason
    """
    queue: str                          # security | spam | legal | internal | normal
    urgency_score: float = 0.0          # 0.0 – 1.0
    matched_rules: list[str] = field(default_factory=list)
    should_alert: bool = False
    alert_target: Optional[str] = None
    skip_llm: bool = False
    note: str = ""


# ─────────────────────────────────────────
# Main prefilter function
# ─────────────────────────────────────────

def prefilter(
    sender: str,
    subject: str,
    body: str,
) -> PrefilterResult:
    """
    Runs fast keyword checks on an email and returns a routing decision.

    Args:
        sender:  Sender email address
        subject: Email subject line
        body:    Email body text

    Returns:
        PrefilterResult with queue assignment and urgency score

    Processing order (highest priority first):
        1. Security / ransomware  → security queue, skip LLM
        2. Legal threats          → legal queue, skip LLM
        3. Spam signals           → spam queue, skip LLM
        4. Internal sender        → internal queue
        5. Urgency signals        → normal queue with elevated urgency_score
        6. Default                → normal queue
    """

    # Normalise text for matching — lowercase, collapse whitespace
    text_blob = _normalise(f"{subject} {body}")
    sender_lower = sender.lower().strip()
    subject_lower = subject.lower().strip() if subject else ""

    matched: list[str] = []
    urgency_score = 0.0

    # ── 1. Security / ransomware ──────────────────────────────────────
    sec_hits = _match_keywords(text_blob, SECURITY_KEYWORDS)
    if sec_hits:
        matched.extend(sec_hits)
        logger.warning(
            f"[PREFILTER] SECURITY hit on '{sender}': {sec_hits}"
        )
        return PrefilterResult(
            queue="security",
            urgency_score=1.0,
            matched_rules=matched,
            should_alert=True,
            alert_target="security@company.com",
            skip_llm=True,
            note=(
                f"Security keywords detected: {', '.join(sec_hits)}. "
                "Routed to security queue. NO auto-reply sent. "
                "Security team alerted."
            ),
        )

    # ── 2. Legal threats ──────────────────────────────────────────────
    legal_hits = _match_keywords(text_blob, LEGAL_KEYWORDS)
    if legal_hits:
        matched.extend(legal_hits)
        logger.warning(
            f"[PREFILTER] LEGAL hit on '{sender}': {legal_hits}"
        )
        return PrefilterResult(
            queue="legal",
            urgency_score=0.95,
            matched_rules=matched,
            should_alert=True,
            alert_target="legal@company.com",
            skip_llm=True,
            note=(
                f"Legal keywords detected: {', '.join(legal_hits)}. "
                "Routed to legal queue. NO auto-reply sent."
            ),
        )

    # ── 3. Spam ───────────────────────────────────────────────────────
    spam_hits = _match_keywords(text_blob, SPAM_KEYWORDS)
    if spam_hits:
        # Require at least 2 spam signals OR one very strong one to avoid
        # false-positives (e.g. "limited offer" appearing in legitimate emails)
        strong_spam = {"boost seo", "boost your seo", "nigerian prince",
                       "wire transfer", "unsubscribe from this list",
                       "you've been selected", "cryptocurrency investment"}
        strong_hits = [h for h in spam_hits if h in strong_spam]

        if len(spam_hits) >= 2 or strong_hits:
            matched.extend(spam_hits)
            logger.info(f"[PREFILTER] SPAM hit on '{sender}': {spam_hits}")
            return PrefilterResult(
                queue="spam",
                urgency_score=0.0,
                matched_rules=matched,
                should_alert=False,
                skip_llm=True,
                note=(
                    f"Spam keywords detected: {', '.join(spam_hits)}. "
                    "Marked Ignored. No reply sent."
                ),
            )

    # ── 4. Internal sender ────────────────────────────────────────────
    sender_domain = _extract_domain(sender_lower)
    if sender_domain in INTERNAL_DOMAINS:
        return PrefilterResult(
            queue="internal",
            urgency_score=0.1,
            matched_rules=["internal_domain"],
            skip_llm=False,   # still classify, but know it's internal
            note=f"Internal sender domain: {sender_domain}",
        )

    # ── 5. Urgency signal boost ───────────────────────────────────────
    # Doesn't change queue, but boosts urgency_score so the LLM queue
    # can prioritise this email for faster processing.
    urgency_hits = _match_keywords(text_blob, URGENCY_KEYWORDS)
    if urgency_hits:
        matched.extend(urgency_hits)
        # Scale: 1 hit → 0.3, 2 hits → 0.55, 3+ hits → 0.75+
        urgency_score = min(0.3 * len(urgency_hits) * 0.7 + 0.3, 0.9)
        logger.info(
            f"[PREFILTER] Urgency signals on '{sender}': "
            f"{urgency_hits} → score={urgency_score:.2f}"
        )

    # ── 6. Normal — pass to LLM ───────────────────────────────────────
    return PrefilterResult(
        queue="normal",
        urgency_score=urgency_score,
        matched_rules=matched,
        skip_llm=False,
        note="No heuristic matches — route to LLM classifier.",
    )


# ─────────────────────────────────────────
# Integration helpers
# ─────────────────────────────────────────

def prefilter_to_db_status(result: PrefilterResult) -> str:
    """
    Maps a PrefilterResult queue to the Email.status DB value.

    Queues that skip the LLM need an immediate status assignment:
      security → "Escalated"
      legal    → "Escalated"
      spam     → "Ignored"
      internal → "Received"  (still goes through classifier)
      normal   → "Received"
    """
    mapping = {
        "security": "Escalated",
        "legal":    "Escalated",
        "spam":     "Ignored",
        "internal": "Received",
        "normal":   "Received",
    }
    return mapping.get(result.queue, "Received")


def should_alert_ops(result: PrefilterResult) -> bool:
    """Returns True if an immediate ops/security alert should be sent."""
    return result.should_alert


# ─────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase + collapse whitespace for reliable keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _match_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    Returns all keywords from the list that appear in the text.
    Uses word-boundary matching to avoid false positives
    (e.g. "legal" inside "illegal" or "paralegal").
    """
    hits = []
    for kw in keywords:
        # For multi-word phrases, do substring match (word boundaries would
        # break phrases like "cease and desist"). For single words, use
        # word boundary to avoid partial matches.
        if " " in kw:
            if kw in text:
                hits.append(kw)
        else:
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, text):
                hits.append(kw)
    return hits


def _extract_domain(email: str) -> str:
    """Extracts the domain from an email address."""
    if "@" in email:
        return email.split("@", 1)[1].strip()
    return ""

# paste this at the bottom of prefilter.py temporarily, then run:
# python -m heuristics.prefilter

if __name__ == "__main__":
    # Test ransomware (msg_038)
    r = prefilter(
        sender="attacker@unknown.com",
        subject="Send 2 BTC or we publish exfiltrated data",
        body="We have exfiltrated your customer database. Send 2 BTC or we publish it."
    )
    print(r)  # should be queue=security, skip_llm=True

    # Test spam
    r = prefilter(
        sender="marketer@seo.biz",
        subject="Boost your SEO today",
        body="We can get you to the first page of Google. Limited time offer."
    )
    print(r)  # should be queue=spam

    # Test normal
    r = prefilter(
        sender="alice.smith@greenlight-npo.org",
        subject="How much to add 5 seats mid-cycle?",
        body="Hi, we need 5 more seats. What's the pro-rata charge?"
    )
    print(r)  # should be queue=normal, urgency_score > 0