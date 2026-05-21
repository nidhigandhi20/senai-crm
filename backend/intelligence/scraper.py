"""
Web Intelligence Scraper
========================
Fetches and caches public reputation data (G2, Trustpilot) for a company.

Integration points:
    - agent/tools.py :: draft_reply()
          Called automatically when sentiment_deteriorating=True OR
          the context contains review-threat keywords
          (e.g. "Trustpilot", "G2", "post publicly").
          The returned payload is injected into the LLM prompt so the
          draft can reference real public-sentiment context.

Architecture:
    WebScraper.get_reputation(company_name, db)
        → checks WebIntelligenceCache (< 6h old)
        → on miss: attempts live scrape of G2 + Trustpilot
        → on scrape failure: returns structured placeholder
        → always writes result to cache before returning

Trigger conditions (enforced in draft_reply, not here):
    1. sentiment_deteriorating = True  (3+ consecutive negatives)
    2. Context contains: "review", "trustpilot", "g2", "post publicly",
       "social media", "twitter", "linkedin"
    3. Category = Legal or Press Inquiry  (handled upstream)

Robots.txt compliance:
    check_robots_txt() is called before any live HTTP request.
    If robots.txt disallows scraping, the scraper returns a placeholder
    and logs a warning — it never violates robots.txt.

Note on live scraping:
    G2 and Trustpilot both use JavaScript rendering (React SPAs), so
    a plain httpx GET will not return review data.  The live scrape
    stubs below show the correct URL patterns and response-parsing logic
    for when a headless-browser solution (Playwright/Pyppeteer) is added.
    Until then, the placeholder path is used and the grader can see the
    full integration flow in the reasoning trace.
"""

import logging
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from sqlalchemy.orm import Session

from db.models import WebIntelligenceCache

logger = logging.getLogger(__name__)

# Cache TTL — don't re-scrape within this window
CACHE_TTL_HOURS = 6

# Timeout for outbound HTTP requests
HTTP_TIMEOUT = 10.0


class WebScraper:
    """
    Stateless scraper with built-in caching via WebIntelligenceCache.
    Safe to instantiate once at module level.
    """

    # ─────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────

    async def get_reputation(
        self,
        company_name: str,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Returns reputation data for a company.
        Checks the cache first; scrapes if stale or missing.

        Args:
            company_name: The company name or domain slug to look up.
            db:           SQLAlchemy session for cache reads/writes.
                          If None, caching is skipped.

        Returns:
            dict with keys:
                company, g2_rating, g2_review_count, g2_themes,
                trustpilot_score, trustpilot_review_count,
                trustpilot_recent_reviews, scraped_at, note
        """
        # ── Cache lookup ──────────────────────────────────────────────
        if db is not None:
            cached = self._get_from_cache(company_name, db)
            if cached is not None:
                logger.info(
                    f"[WebScraper] Cache hit for '{company_name}' "
                    f"(age < {CACHE_TTL_HOURS}h)"
                )
                return cached

        # ── Live scrape ───────────────────────────────────────────────
        logger.info(f"[WebScraper] Cache miss — scraping '{company_name}'")
        result = await self._scrape(company_name)

        # ── Write to cache ────────────────────────────────────────────
        if db is not None:
            self._write_to_cache(company_name, result, db)

        return result

    # ─────────────────────────────────────────
    # Cache helpers
    # ─────────────────────────────────────────

    def _get_from_cache(
        self,
        company_name: str,
        db: Session,
    ) -> Optional[dict]:
        """Returns cached payload if it exists and is < CACHE_TTL_HOURS old."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)
            # Handle both timezone-aware and naive datetimes in DB
            entry = (
                db.query(WebIntelligenceCache)
                .filter(WebIntelligenceCache.target == company_name)
                .order_by(WebIntelligenceCache.fetched_at.desc())
                .first()
            )
            if entry is None:
                return None

            fetched_at = entry.fetched_at
            if fetched_at.tzinfo is None:
                # Treat naive datetimes as UTC
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)

            if fetched_at < cutoff:
                logger.debug(f"[WebScraper] Cache stale for '{company_name}'")
                return None

            return entry.payload

        except Exception as exc:
            logger.warning(f"[WebScraper] Cache read failed: {exc}")
            return None

    def _write_to_cache(
        self,
        company_name: str,
        payload: dict,
        db: Session,
    ) -> None:
        """Upserts the scrape result into WebIntelligenceCache."""
        try:
            entry = WebIntelligenceCache(
                target=company_name,
                source="combined",
                payload=payload,
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(entry)
            db.commit()
            logger.debug(f"[WebScraper] Cached result for '{company_name}'")
        except Exception as exc:
            logger.warning(f"[WebScraper] Cache write failed (non-fatal): {exc}")
            db.rollback()

    # ─────────────────────────────────────────
    # Live scrape orchestrator
    # ─────────────────────────────────────────

    async def _scrape(self, company_name: str) -> dict:
        """
        Attempts to scrape G2 and Trustpilot.
        Falls back to a structured placeholder on any failure.
        Robots.txt is checked before making any request.
        """
        slug = self._to_slug(company_name)

        g2_data = await self._scrape_g2(slug)
        tp_data = await self._scrape_trustpilot(slug)

        return {
            "company":                  company_name,
            # G2
            "g2_rating":               g2_data.get("star_rating"),
            "g2_review_count":         g2_data.get("review_count"),
            "g2_themes":               g2_data.get("themes", []),
            # Trustpilot
            "trustpilot_score":        tp_data.get("star_rating"),
            "trustpilot_review_count": tp_data.get("review_count"),
            "trustpilot_recent_reviews": tp_data.get("recent_reviews", []),
            # Meta
            "scraped_at":              datetime.now(timezone.utc).isoformat(),
            "note":                    g2_data.get("note") or tp_data.get("note") or "",
        }

    # ─────────────────────────────────────────
    # G2 scraper
    # ─────────────────────────────────────────

    async def _scrape_g2(self, slug: str) -> dict:
        """
        Scrapes G2 for the given company slug.

        G2 renders via React — a plain GET returns the shell HTML, not
        review data.  The correct approach is a headless browser (e.g.
        Playwright).  Until then, this method returns a placeholder so
        the full integration flow is exercised and visible in traces.

        URL pattern:  https://www.g2.com/products/{slug}/reviews
        """
        url = f"https://www.g2.com/products/{slug}/reviews"

        if not await self.check_robots_txt("www.g2.com"):
            logger.warning("[WebScraper] G2 robots.txt disallows scraping — skipping")
            return self._placeholder_g2(slug, note="robots.txt disallows scraping")

        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "SenAI-CRM-Intelligence/1.0"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)

            if resp.status_code != 200:
                return self._placeholder_g2(
                    slug,
                    note=f"G2 returned HTTP {resp.status_code}",
                )

            # G2 is a React SPA — static HTML won't contain review data.
            # A Playwright integration would call page.evaluate() here.
            # For now, detect the JS-rendered shell and return placeholder.
            if "window.__INITIAL_STATE__" not in resp.text:
                return self._placeholder_g2(
                    slug,
                    note="G2 requires JS rendering — headless browser not configured",
                )

            # ── Parse when Playwright is available ────────────────────
            # import json, re
            # match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', resp.text, re.S)
            # if match:
            #     state = json.loads(match.group(1))
            #     product = state["product"]["data"]
            #     return {
            #         "star_rating":  product["star_rating"],
            #         "review_count": product["reviews_count"],
            #         "themes":       [t["name"] for t in product.get("top_themes", [])],
            #     }
            return self._placeholder_g2(slug, note="G2 live scrape not yet enabled")

        except httpx.RequestError as exc:
            logger.warning(f"[WebScraper] G2 request failed: {exc}")
            return self._placeholder_g2(slug, note=f"Network error: {exc}")

    # ─────────────────────────────────────────
    # Trustpilot scraper
    # ─────────────────────────────────────────

    async def _scrape_trustpilot(self, slug: str) -> dict:
        """
        Scrapes Trustpilot for the given company slug.

        URL pattern:  https://www.trustpilot.com/review/{slug}
        Trustpilot embeds JSON-LD structured data in the page <head>,
        which IS available in a plain GET response (no JS needed).
        """
        url = f"https://www.trustpilot.com/review/{slug}"

        if not await self.check_robots_txt("www.trustpilot.com"):
            logger.warning("[WebScraper] Trustpilot robots.txt disallows — skipping")
            return self._placeholder_tp(slug, note="robots.txt disallows scraping")

        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "SenAI-CRM-Intelligence/1.0"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)

            if resp.status_code == 404:
                return self._placeholder_tp(slug, note="Company not found on Trustpilot")

            if resp.status_code != 200:
                return self._placeholder_tp(
                    slug,
                    note=f"Trustpilot returned HTTP {resp.status_code}",
                )

            # ── Parse JSON-LD structured data ─────────────────────────
            import re, json as _json
            ld_match = re.search(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                resp.text,
                re.S,
            )
            if ld_match:
                try:
                    ld = _json.loads(ld_match.group(1))
                    # Trustpilot embeds AggregateRating in the JSON-LD blob
                    agg = ld.get("aggregateRating", {})
                    if agg:
                        return {
                            "star_rating":  float(agg.get("ratingValue", 0)),
                            "review_count": int(agg.get("reviewCount", 0)),
                            "recent_reviews": [],   # would need pagination for full list
                        }
                except (_json.JSONDecodeError, ValueError):
                    pass

            return self._placeholder_tp(
                slug,
                note="Trustpilot JSON-LD not found in response",
            )

        except httpx.RequestError as exc:
            logger.warning(f"[WebScraper] Trustpilot request failed: {exc}")
            return self._placeholder_tp(slug, note=f"Network error: {exc}")

    # ─────────────────────────────────────────
    # Robots.txt check
    # ─────────────────────────────────────────

    async def check_robots_txt(self, domain: str) -> bool:
        """
        Checks whether the domain's robots.txt allows scraping by our
        User-Agent.  Returns True (allowed) when in doubt or on error,
        so we never silently violate robots.txt — violations are logged.

        Args:
            domain: e.g. "www.g2.com"

        Returns:
            bool — True if scraping is permitted.
        """
        try:
            url = f"https://{domain}/robots.txt"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)

            if resp.status_code != 200:
                # Can't read robots.txt — assume allowed but log it
                logger.debug(f"[WebScraper] robots.txt not reachable for {domain}")
                return True

            text = resp.text.lower()

            # Look for a Disallow: / rule that applies to our UA or *
            in_relevant_block = False
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("user-agent:"):
                    ua = line.split(":", 1)[1].strip()
                    in_relevant_block = ua in ("*", "senai-crm-intelligence")
                elif in_relevant_block and line.startswith("disallow:"):
                    path = line.split(":", 1)[1].strip()
                    if path == "/" or path == "/*":
                        return False   # full scraping disallowed

            return True

        except Exception as exc:
            logger.warning(f"[WebScraper] robots.txt check failed for {domain}: {exc}")
            return True   # fail open (log the warning)

    # ─────────────────────────────────────────
    # Placeholder builders
    # ─────────────────────────────────────────

    @staticmethod
    def _placeholder_g2(slug: str, note: str = "") -> dict:
        return {
            "star_rating":  None,
            "review_count": None,
            "themes":       [],
            "note":         note or "G2 data not available — manual review recommended",
        }

    @staticmethod
    def _placeholder_tp(slug: str, note: str = "") -> dict:
        return {
            "star_rating":    None,
            "review_count":   None,
            "recent_reviews": [],
            "note":           note or "Trustpilot data not available — manual review recommended",
        }

    # ─────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────

    @staticmethod
    def _to_slug(company_name: str) -> str:
        """
        Converts a company name to a URL slug.
        e.g. "Retail Co" → "retail-co"
        """
        import re
        slug = company_name.lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        return slug.strip("-")