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

Changes vs v1:
  - Added COLD_OUTREACH_DOMAINS blocklist (msg_039 fix)
  - Added PRESS_PREFIXES list → forces requires_human=True in output (msg_055 fix)
  - Press emails now include a hint in the PrefilterResult for the classifier

Usage:
    from heuristics.prefilter import prefilter
    result = prefilter(sender, subject, body)
    if result.skip_llm:
        # handle immediately, skip LLM
    if result.is_press_inquiry:
        # force requires_human=True in classification
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
    "per our last conversation",   # common cold outreach opener
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

# ── Cold outreach domain blocklist (msg_039 fix) ──────────────────────────
# Domains commonly used for cold outreach / sales automation.
# Emails from these domains are routed to spam even without strong keyword hits,
# since legitimate customers almost never use these services.
COLD_OUTREACH_DOMAINS: set[str] = {
    # Sales automation / sequencing platforms
    "outreach.io",
    "salesloft.com",
    "reply.io",
    "lemlist.com",
    "apollo.io",
    "klenty.com",
    "woodpecker.co",
    "mailshake.com",
    "quickmail.io",
    "persistiq.com",
    "mixmax.com",
    "yesware.com",
    "cirrus-insight.com",
    "snovio.com",
    "hunter.io",
    "growbots.com",
    "overloop.com",
    "autoklose.com",
    "snov.io",
    # Generic disposable / bulk email providers often used for cold outreach
    "mailinator.com",
    "guerrillamail.com",
    "tempmail.com",
    "throwam.com",
    "sharklasers.com",
    "guerrillamailblock.com",
    "grr.la",
    "guerrillamail.info",
    "guerrillamail.biz",
    "guerrillamail.de",
    "guerrillamail.net",
    "guerrillamail.org",
    "spam4.me",
    "trashmail.com",
    "trashmail.me",
    "yopmail.com",
    # Common cold-outreach-only domains (add your own as you encounter them)
    "coldoutreach.co",
    "prospectreach.io",
}

# ── Press inquiry sender prefixes (msg_055 fix) ───────────────────────────
# Emails from these prefixes almost always come from journalists, analysts,
# or PR contacts who require a human response. They should never be auto-replied.
PRESS_PREFIXES: set[str] = {
    "press",
    "media",
    "journalist",
    "reporter",
    "editor",
    "news",
    "pr",
    "communications",
    "comms",
    "analyst",
    "research",
    "ir",          # investor relations
    "investors",
}

# Press-related subject/body keywords — reinforce press routing
PRESS_SUBJECT_KEYWORDS = [
    "press inquiry",
    "media inquiry",
    "journalist",
    "for publication",
    "on the record",
    "off the record",
    "comment from",
    "requesting comment",
    "press release",
    "interview request",
    "media request",
    "publication deadline",
    "journalist inquiry",
    "analyst briefing",
    "investor inquiry",
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

    queue:             Where to route this email
    urgency_score:     0.0–1.0 urgency signal (used to prioritise within queues)
    matched_rules:     Which keyword lists fired (for audit trail)
    should_alert:      Whether to ping an ops/security channel immediately
    alert_target:      Who to alert (e.g. security@company.com)
    skip_llm:          True if the LLM classifier should be skipped entirely
    note:              Short human-readable reason
    is_press_inquiry:  True if this looks like a press/media/analyst contact
                       → classifier must set requires_human=True regardless of content
    """
    queue: str                          # security | spam | legal | internal | normal
    urgency_score: float = 0.0          # 0.0 – 1.0
    matched_rules: list[str] = field(default_factory=list)
    should_alert: bool = False
    alert_target: Optional[str] = None
    skip_llm: bool = False
    note: str = ""
    is_press_inquiry: bool = False      # NEW: force requires_human=True in classifier


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
        3. Cold outreach domain   → spam queue, skip LLM  [NEW]
        4. Press / media inquiry  → normal queue, is_press_inquiry=True [NEW]
        5. Spam signals           → spam queue, skip LLM
        6. Internal sender        → internal queue
        7. Urgency signals        → normal queue with elevated urgency_score
        8. Default                → normal queue
    """

    # Normalise text for matching — lowercase, collapse whitespace
    text_blob     = _normalise(f"{subject} {body}")
    sender_lower  = sender.lower().strip()
    subject_lower = subject.lower().strip() if subject else ""

    matched: list[str] = []
    urgency_score = 0.0

    # Extract sender parts
    sender_domain  = _extract_domain(sender_lower)
    sender_prefix  = _extract_prefix(sender_lower)

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

    # ── 3. Cold outreach domain blocklist [NEW] ───────────────────────
    # Route to spam if the sender's domain is on the cold outreach blocklist.
    # This catches msg_039 and similar domain-based cold outreach that slips
    # through the keyword filter (no strong spam keywords in the body).
    if sender_domain in COLD_OUTREACH_DOMAINS:
        matched.append(f"cold_outreach_domain:{sender_domain}")
        logger.info(
            f"[PREFILTER] COLD OUTREACH domain hit: '{sender}' "
            f"(domain={sender_domain})"
        )
        return PrefilterResult(
            queue="spam",
            urgency_score=0.0,
            matched_rules=matched,
            should_alert=False,
            skip_llm=True,
            note=(
                f"Sender domain '{sender_domain}' is on the cold outreach blocklist. "
                "Marked Ignored. No reply sent."
            ),
        )

    # ── 4. Press / media inquiry detection [NEW] ─────────────────────
    # Press contacts require a human response — never auto-reply.
    # Detection criteria: sender prefix OR subject/body keywords.
    # Does NOT skip LLM (we still want classification), but sets
    # is_press_inquiry=True so the engine forces requires_human=True.
    press_prefix_hit = sender_prefix in PRESS_PREFIXES
    press_subject_hits = _match_keywords(text_blob, PRESS_SUBJECT_KEYWORDS)

    if press_prefix_hit or press_subject_hits:
        press_signals = []
        if press_prefix_hit:
            press_signals.append(f"sender_prefix:{sender_prefix}")
        press_signals.extend(press_subject_hits)

        matched.extend(press_signals)
        logger.info(
            f"[PREFILTER] PRESS INQUIRY detected for '{sender}': {press_signals}"
        )
        return PrefilterResult(
            queue="normal",          # still classify with LLM
            urgency_score=0.5,       # moderate urgency — needs timely human response
            matched_rules=matched,
            should_alert=False,
            skip_llm=False,          # classify, but force requires_human in engine
            is_press_inquiry=True,   # engine reads this and forces requires_human=True
            note=(
                f"Press/media inquiry signals detected: {', '.join(press_signals)}. "
                "Routing to normal queue with requires_human=True override."
            ),
        )

    # ── 5. Spam ───────────────────────────────────────────────────────
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

    # ── 6. Internal sender ────────────────────────────────────────────
    if sender_domain in INTERNAL_DOMAINS:
        return PrefilterResult(
            queue="internal",
            urgency_score=0.1,
            matched_rules=["internal_domain"],
            skip_llm=False,   # still classify, but know it's internal
            note=f"Internal sender domain: {sender_domain}",
        )

    # ── 7. Urgency signal boost ───────────────────────────────────────
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

    # ── 8. Normal — pass to LLM ───────────────────────────────────────
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


def _extract_prefix(email: str) -> str:
    """
    Extracts the local part (before @) from an email address and
    normalises it (lowercase, strips dots/underscores/numbers).

    Examples:
        press@company.com        → "press"
        media.inquiries@abc.org  → "media"   (first word before dot)
        pr_team@startup.io       → "pr"      (first word before underscore)
    """
    if "@" in email:
        local = email.split("@", 1)[0].strip().lower()
        # Take the first segment before a dot, underscore, hyphen, or digit
        first_segment = re.split(r"[._\-0-9]", local)[0]
        return first_segment
    return ""


# ─────────────────────────────────────────
# Engine integration helper
# ─────────────────────────────────────────

def apply_prefilter_overrides(
    result: "ClassificationResult",  # forward ref to avoid circular import
    pf_result: PrefilterResult,
) -> "ClassificationResult":
    """
    Applies any PrefilterResult flags to the ClassificationResult AFTER
    LLM classification. Currently handles:

      - is_press_inquiry=True → force requires_human=True, set urgency=High
        if current urgency is Low/Medium, add escalation_reason.

    Call this in engine.py after _parse_llm_response() and apply_safety_rules():

        result = apply_prefilter_overrides(result, pf_result)

    This keeps the override logic here in prefilter.py (single source of truth)
    rather than scattered through engine.py.
    """
    if not pf_result.is_press_inquiry:
        return result

    result.requires_human = True

    if result.urgency in ("Low", "Medium"):
        result.urgency = "High"

    press_note = (
        "Press/media inquiry detected by pre-filter — "
        "requires human response, no auto-reply permitted."
    )

    if result.escalation_reason:
        result.escalation_reason = f"{result.escalation_reason} | {press_note}"
    else:
        result.escalation_reason = press_note

    # Suggested reply must be cleared (no auto-reply for press)
    result.suggested_reply = None

    logger.info(
        "[PREFILTER] Press inquiry override applied: "
        "requires_human=True, urgency upgraded, suggested_reply cleared."
    )
    return result


# ─────────────────────────────────────────
# Self-test (run: python -m heuristics.prefilter)
# ─────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # Security
        {
            "label": "Ransomware (msg_038)",
            "sender": "attacker@unknown.com",
            "subject": "Send 2 BTC or we publish exfiltrated data",
            "body": "We have exfiltrated your customer database. Send 2 BTC or we publish it.",
            "expect_queue": "security",
        },
        # Spam keyword
        {
            "label": "SEO spam",
            "sender": "marketer@seo.biz",
            "subject": "Boost your SEO today",
            "body": "We can get you to the first page of Google. Limited time offer.",
            "expect_queue": "spam",
        },
        # Cold outreach domain (msg_039 fix)
        {
            "label": "Cold outreach domain (msg_039)",
            "sender": "sales@outreach.io",
            "subject": "Following up on your account",
            "body": "Hi, just wanted to touch base about our enterprise offering.",
            "expect_queue": "spam",
        },
        # Press inquiry — prefix
        {
            "label": "Press inquiry — prefix (msg_055)",
            "sender": "press@techcrunch.com",
            "subject": "Interview request regarding your AI platform",
            "body": "Hi, I'm writing a piece on AI CRM tools and would love a comment.",
            "expect_queue": "normal",
            "expect_press": True,
        },
        # Press inquiry — keyword
        {
            "label": "Press inquiry — keyword",
            "sender": "jane@reuters.com",
            "subject": "Media inquiry",
            "body": "Requesting comment for publication — deadline tomorrow.",
            "expect_queue": "normal",
            "expect_press": True,
        },
        # Normal billing
        {
            "label": "Normal billing (Alice, msg_041)",
            "sender": "alice.smith@greenlight-npo.org",
            "subject": "How much to add 5 seats mid-cycle?",
            "body": "Hi, we need 5 more seats. What's the pro-rata charge?",
            "expect_queue": "normal",
        },
    ]

    print("\n=== Prefilter Self-Test ===\n")
    all_passed = True
    for t in tests:
        r = prefilter(t["sender"], t["subject"], t["body"])
        queue_ok   = r.queue == t.get("expect_queue", "normal")
        press_ok   = r.is_press_inquiry == t.get("expect_press", False)
        status     = "passed" if (queue_ok and press_ok) else "failed"
        if not (queue_ok and press_ok):
            all_passed = False
        print(
            f"{status} {t['label']}\n"
            f"   queue={r.queue}  skip_llm={r.skip_llm}  "
            f"is_press={r.is_press_inquiry}  urgency_score={r.urgency_score:.2f}\n"
            f"   note: {r.note}\n"
        )

    print("All tests passed " if all_passed else "Some tests FAILED")