"""
Base scraper class providing shared infrastructure for all cruise line scrapers.

Responsibilities:
- Playwright browser lifecycle management
- Network response interception (XHR/fetch capture)
- Rate limiting and request delays
- Retry logic with exponential backoff
- Schema validation against data/schema.json
- Structured logging
- Output file writing
"""

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Response

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

# Repo root is two levels up from this file (scrapers/base_scraper.py → repo root)
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SCHEMA_PATH = DATA_DIR / "schema.json"

# ---------------------------------------------------------------------------
# Shared browser configuration
# ---------------------------------------------------------------------------

# A realistic Chrome on macOS user-agent string.
# Explora Journeys blocks 500+ bot UAs; this mimics a real browser.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


# ---------------------------------------------------------------------------
# BaseScraper
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """
    Abstract base class for cruise line scrapers.

    Subclasses must implement:
      - cruise_line: str          — identifier matching schema enum
      - request_delay: float      — seconds to wait between page loads
      - scrape(page, context)     — core scraping logic, returns list of raw dicts
      - normalize(raw)            — maps raw scraped data to the normalized schema

    Usage (from a concrete subclass)::

        scraper = MyCruiseScraper()
        records = asyncio.run(scraper.run())
    """

    # --- subclass must set these ---
    cruise_line: str = ""
    request_delay: float = 3.0  # seconds between requests

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self._schema = self._load_schema()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> list[dict]:
        """Synchronous entry point. Runs the async scraper and returns records."""
        return asyncio.run(self._run_async())

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def scrape(self, page: Page, context: BrowserContext) -> list[dict]:
        """
        Core scraping logic.

        Args:
            page: Playwright page (browser context already open)
            context: BrowserContext (for opening new pages if needed)

        Returns:
            List of raw dicts to be passed through normalize()
        """
        ...

    @abstractmethod
    def normalize(self, raw: dict) -> dict | None:
        """
        Map a raw scraped record to the normalized voyage schema.

        Return None to skip a record (e.g. if required fields are missing).
        """
        ...

    # ------------------------------------------------------------------
    # Playwright helpers
    # ------------------------------------------------------------------

    async def _run_async(self) -> list[dict]:
        """Launch browser, run scrape(), normalize, validate, return records."""
        self.logger.info("Starting scrape for %s", self.cruise_line)
        start = time.monotonic()

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=True,
                args=BROWSER_ARGS,
            )
            context: BrowserContext = await browser.new_context(
                user_agent=CHROME_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
            )
            # Suppress image/font loading to speed things up
            await context.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf}",
                lambda route: route.abort(),
            )
            page: Page = await context.new_page()

            try:
                raw_records = await self.scrape(page, context)
            except Exception as exc:
                self.logger.error("Scrape failed: %s", exc, exc_info=True)
                raw_records = []
            finally:
                await browser.close()

        elapsed = time.monotonic() - start
        self.logger.info("Browser closed after %.1fs", elapsed)

        records = self._process_records(raw_records)
        self.logger.info(
            "Scrape complete — %d valid records (from %d raw)",
            len(records),
            len(raw_records),
        )
        return records

    async def intercept_json_responses(
        self,
        page: Page,
        url_patterns: list[str],
        navigate_url: str,
        *,
        wait_selector: str | None = None,
        timeout_ms: int = 30_000,
    ) -> list[dict]:
        """
        Navigate to `navigate_url` and capture all JSON responses whose URL
        contains at least one of `url_patterns`.

        Args:
            page: Playwright page
            url_patterns: List of URL substrings to match (e.g. ["/api/voyages"])
            navigate_url: URL to load in the browser
            wait_selector: Optional CSS selector to wait for after navigation
            timeout_ms: Milliseconds to wait for navigation / selector

        Returns:
            List of parsed JSON response bodies
        """
        captured: list[dict] = []

        async def handle_response(response: Response) -> None:
            url = response.url
            if not any(pat in url for pat in url_patterns):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            try:
                body = await response.json()
                self.logger.debug("Captured response from %s", url)
                captured.append({"url": url, "body": body})
            except Exception as exc:
                self.logger.debug("Could not parse JSON from %s: %s", url, exc)

        page.on("response", handle_response)

        self.logger.info("Navigating to %s", navigate_url)
        await page.goto(navigate_url, wait_until="domcontentloaded", timeout=timeout_ms)

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=timeout_ms)
            except Exception:
                self.logger.warning("Selector %r not found after navigation", wait_selector)

        # Give any lazy-loaded XHR requests a moment to complete
        await page.wait_for_timeout(3000)

        page.remove_listener("response", handle_response)
        self.logger.info("Captured %d JSON responses", len(captured))
        return captured

    async def wait(self, seconds: float | None = None) -> None:
        """Async sleep respecting the scraper's request_delay."""
        delay = seconds if seconds is not None else self.request_delay
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def with_retry(self, coro_fn, *args, retries: int = 3, **kwargs):
        """
        Call an async function with exponential backoff retries.

        Args:
            coro_fn: Async callable
            *args: Positional args to pass to coro_fn
            retries: Max attempts
            **kwargs: Keyword args to pass to coro_fn

        Returns:
            Return value of coro_fn on success.

        Raises:
            Exception from the last attempt if all retries fail.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                self.logger.warning(
                    "Attempt %d/%d failed (%s). Retrying in %ds…",
                    attempt, retries, exc, wait,
                )
                await asyncio.sleep(wait)
        raise last_exc

    # ------------------------------------------------------------------
    # Record processing
    # ------------------------------------------------------------------

    def _process_records(self, raw_records: list[dict]) -> list[dict]:
        """Normalize and validate all raw records. Returns only valid ones."""
        now = datetime.now(timezone.utc)
        scrape_date = now.strftime("%Y-%m-%d")
        scrape_timestamp = now.isoformat()

        valid = []
        skipped = 0
        for raw in raw_records:
            try:
                record = self.normalize(raw)
            except Exception as exc:
                self.logger.warning("normalize() raised %s — skipping record", exc)
                skipped += 1
                continue

            if record is None:
                skipped += 1
                continue

            # Inject scrape metadata
            record.setdefault("scrape_date", scrape_date)
            record.setdefault("scrape_timestamp", scrape_timestamp)
            record.setdefault("cruise_line", self.cruise_line)

            errors = self._validate(record)
            if errors:
                self.logger.warning("Validation errors for voyage %r: %s", record.get("voyage_id"), errors)
                skipped += 1
                continue

            valid.append(record)

        if skipped:
            self.logger.warning("Skipped %d invalid/incomplete records", skipped)
        return valid

    def _validate(self, record: dict) -> list[str]:
        """Validate record against schema.json. Returns list of error messages."""
        if self._schema is None:
            return []
        errors = []
        validator = jsonschema.Draft7Validator(self._schema)
        for error in validator.iter_errors(record):
            errors.append(f"{'.'.join(str(p) for p in error.path) or 'root'}: {error.message}")
        return errors

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def write_output(self, records: list[dict], date_str: str | None = None) -> Path:
        """
        Write records to data/YYYY-MM-DD/{cruise_line}.json.

        Args:
            records: Validated voyage records
            date_str: Override date string (YYYY-MM-DD); defaults to today UTC

        Returns:
            Path to the written file
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        out_dir = DATA_DIR / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{self.cruise_line}.json"

        payload = {
            "scrape_date": date_str,
            "cruise_line": self.cruise_line,
            "record_count": len(records),
            "voyages": records,
        }

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

        self.logger.info("Wrote %d records to %s", len(records), out_path)
        return out_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_schema() -> dict | None:
        if SCHEMA_PATH.exists():
            with open(SCHEMA_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        logging.warning("Schema file not found at %s — validation disabled", SCHEMA_PATH)
        return None

    # ------------------------------------------------------------------
    # Utility: safe field extractors
    # ------------------------------------------------------------------

    @staticmethod
    def safe_str(value: Any, fallback: str = "") -> str:
        if value is None:
            return fallback
        return str(value).strip()

    @staticmethod
    def safe_int(value: Any, fallback: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def safe_float(value: Any, fallback: float | None = None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def parse_date(value: Any) -> str | None:
        """
        Try to parse various date formats into YYYY-MM-DD.
        Returns None on failure rather than raising.
        """
        if not value:
            return None
        s = str(value).strip()
        for fmt in (
            "%Y-%m-%d",           # 2025-06-15
            "%m/%d/%Y",           # 06/15/2025
            "%d/%m/%Y",           # 15/06/2025
            "%Y%m%d",             # 20250615
            "%d-%b-%Y",           # 15-Jun-2025
            "%B %d, %Y",          # June 15, 2025  (Oceania API format)
            "%b %d, %Y",          # Jun 15, 2025
            "%B %d %Y",           # June 15 2025
            "%d %B %Y",           # 15 June 2025
        ):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Try ISO 8601 with time component
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            return None
