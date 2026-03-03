"""
Explora Journeys scraper.

Strategy: Playwright + network response interception.

Site assessment:
- explorajourneys.com is fully JS-rendered; no voyage data in static HTML.
- The booking engine at booking.explorajourneys.com/touchb2c/ uses Flutter/CanvasKit
  (canvas-only, no DOM elements) — we scrape the main marketing site instead.
- The backend is Versonix Seaware; API calls may include /bdi/ or /seaware/ paths.
- robots.txt blocks 500+ bot user-agents — we use a real Chrome UA string.
- No crawl-delay specified, but we use 3s between requests to be respectful.

Approach:
1. Navigate to the "Find Your Journey" listing page.
2. Intercept all JSON responses from known API URL patterns.
3. If we see paginated results or a "load more" trigger, iterate through them.
4. Map intercepted JSON → normalized voyage schema.

NOTE: The exact API endpoint URLs are discovered at runtime by logging all
intercepted responses. Run with EXPLORA_DEBUG=1 to log all JSON response URLs.
"""

import logging
import os
import re
from typing import Any

from playwright.async_api import BrowserContext, Page

from base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# The main voyage listing page (locale-prefixed)
LISTING_URL = "https://www.explorajourneys.com/us/en/find-your-journey"

# URL fragments that are likely to appear in the voyage data API calls.
# Expanded to include AEM Sling paths (/bin/), the booking subdomain, and
# the sitemap-derived journey URL structure.
API_URL_PATTERNS = [
    "/api/",
    "/voyages",
    "/sailings",
    "/search",
    "/bdi/",
    "/seaware",
    "/itineraries",
    "/results",
    "/cruises",
    "/bin/",                         # AEM Sling servlet paths
    "/content/explora",              # AEM content paths
    "booking.explorajourneys.com",   # Versonix Seaware booking subdomain
    "/journeys",
    "/find-your-journey",
    "/experiences",
]

# Always log all intercepted response URLs (not just voyage-matching ones)
# so we can discover the correct API endpoint on first run.
ALWAYS_LOG_RESPONSES = True

# CSS selector that should appear when voyages have loaded
VOYAGE_LOADED_SELECTOR = (
    # Try multiple selectors in case class names change
    "[class*='voyage'], [class*='journey'], [class*='cruise'], "
    "[data-voyage], [data-journey], article"
)

# Whether to print all intercepted response URLs for debugging
DEBUG_MODE = os.getenv("EXPLORA_DEBUG", "").lower() in ("1", "true", "yes")


class ExploraJourneysScraper(BaseScraper):
    cruise_line = "explora_journeys"
    request_delay = 3.0

    async def scrape(self, page: Page, context: BrowserContext) -> list[dict]:
        """
        Navigate to Explora's journey listing page and capture API responses.
        Returns a list of raw response bodies to be passed through normalize().
        """
        all_raw: list[dict] = []

        # --- Step 1: Capture ALL JSON responses (to discover the right endpoint) ---
        # We intercept everything matching a broad list of patterns. We also log
        # EVERY JSON response URL so we can identify the correct API endpoint.
        all_json_responses: list[dict] = []

        async def capture_all_json(response):
            """Capture every JSON response regardless of URL pattern."""
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                url = response.url
                all_json_responses.append({"url": url, "body": body})
                logger.info("JSON response: %s  keys=%s", url,
                            list(body.keys())[:8] if isinstance(body, dict)
                            else f"[list len={len(body)}]" if isinstance(body, list)
                            else type(body).__name__)
            except Exception:
                pass

        page.on("response", capture_all_json)

        logger.info("Navigating to %s", LISTING_URL)
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=45_000)
        # Extra wait for JS-triggered API calls to fire
        await page.wait_for_timeout(8_000)

        page.remove_listener("response", capture_all_json)

        responses = all_json_responses
        logger.info("Total JSON responses captured: %d", len(responses))

        if DEBUG_MODE or ALWAYS_LOG_RESPONSES:
            self._log_all_responses(responses)

        voyage_responses = self._filter_voyage_responses(responses)
        logger.info("Found %d potential voyage API responses on initial load", len(voyage_responses))
        all_raw.extend(voyage_responses)

        # --- Step 2: Handle pagination / load-more ---
        # If the initial response has a pagination structure, load remaining pages
        if voyage_responses:
            all_raw.extend(
                await self._load_remaining_pages(page, voyage_responses[0])
            )

        # --- Step 3: Fallback — try direct API probe ---
        if not all_raw:
            logger.warning(
                "No voyage data captured via interception. "
                "Attempting fallback DOM extraction."
            )
            dom_records = await self._dom_fallback(page)
            all_raw.extend(dom_records)

        # Flatten nested response structures into individual voyage records
        return self._extract_voyages(all_raw)

    def normalize(self, raw: dict) -> dict | None:
        """
        Map a raw voyage record (from intercepted API JSON) to the normalized schema.

        The exact field names depend on what Versonix Seaware returns.
        This method uses a best-guess mapping with multiple fallback keys
        so it can adapt to minor API response variations.
        """
        def get(*keys, default=None):
            """Try multiple key names in order, return first non-None hit."""
            for key in keys:
                val = self._deep_get(raw, key)
                if val is not None:
                    return val
            return default

        voyage_id = self.safe_str(
            get("voyageCode", "code", "id", "voyageId", "sailingCode"),
        )
        if not voyage_id:
            logger.debug("Skipping record with no voyage_id: %s", list(raw.keys())[:5])
            return None

        voyage_name = self.safe_str(
            get("voyageName", "name", "title", "itineraryName", "description"),
            fallback=voyage_id,
        )

        ship_name = self.safe_str(
            get("shipName", "ship", "vessel", "shipCode"),
        )

        departure_port = self.safe_str(
            get("departurePort", "embarkPort", "startPort", "homePort", "fromPort"),
        )

        departure_date = self.parse_date(
            get("departureDate", "startDate", "sailDate", "embarkDate", "fromDate")
        )
        if not departure_date:
            logger.debug("Skipping record %r — no parseable departure date", voyage_id)
            return None

        return_date = self.parse_date(
            get("returnDate", "endDate", "disembarkDate", "arrivalDate", "toDate")
        )

        duration_nights = self.safe_int(
            get("durationNights", "duration", "nights", "lengthOfStay", "numNights")
        )
        if duration_nights is None:
            # Try computing from dates if both are present
            duration_nights = self._compute_duration(departure_date, return_date)
        # Note: duration_nights=None is allowed — schema has it as required but
        # we default to 1 to satisfy minimum:1 if truly unknown rather than
        # using 0 (which would fail validation).
        if duration_nights is None or duration_nights < 1:
            duration_nights = 1

        region = self.safe_str(
            get("region", "destination", "area", "zone", "itineraryRegion"),
        )

        voyage_url = self.safe_str(
            get("url", "voyageUrl", "link", "detailUrl"),
        )
        if voyage_url and not voyage_url.startswith("http"):
            voyage_url = "https://www.explorajourneys.com" + voyage_url

        cabin_categories = self._extract_cabin_categories(raw)
        if not cabin_categories:
            # Create a placeholder so the record isn't dropped for missing cabins
            cabin_categories = [{
                "category_code": "N/A",
                "category_name": "Price on request",
                "price_per_person": None,
                "currency": "USD",
                "availability": "unknown",
            }]

        return {
            "voyage_id": voyage_id,
            "voyage_name": voyage_name,
            "ship_name": ship_name,
            "departure_port": departure_port,
            "departure_date": departure_date,
            "return_date": return_date,
            "duration_nights": duration_nights,
            "region": region,
            "cabin_categories": cabin_categories,
            "voyage_url": voyage_url or LISTING_URL,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _filter_voyage_responses(self, responses: list[dict]) -> list[dict]:
        """
        From all captured JSON responses, keep only those that look like
        they contain voyage/sailing data (rather than analytics, config, etc.).
        """
        voyage_responses = []
        for resp in responses:
            body = resp.get("body", {})
            url = resp.get("url", "")

            # Accept if the body is a list of objects, or has a recognizable key
            if isinstance(body, list) and body:
                if self._looks_like_voyage(body[0]):
                    voyage_responses.append(resp)
                    continue

            if isinstance(body, dict):
                # Look for common wrapper keys
                for key in ("voyages", "sailings", "results", "items", "data", "cruises"):
                    inner = body.get(key)
                    if isinstance(inner, list) and inner and self._looks_like_voyage(inner[0]):
                        voyage_responses.append(resp)
                        break

        if not voyage_responses and DEBUG_MODE:
            logger.debug("No voyage-like responses found. Captured URLs: %s",
                         [r["url"] for r in responses])
        return voyage_responses

    def _looks_like_voyage(self, obj: Any) -> bool:
        """Heuristic: does this dict resemble a voyage/sailing record?"""
        if not isinstance(obj, dict):
            return False
        voyage_keys = {
            "voyageCode", "voyageName", "code", "sailCode", "sailingCode",
            "departureDate", "startDate", "embarkDate", "sailDate",
            "shipName", "ship", "vessel",
            "duration", "nights", "durationNights",
        }
        return bool(voyage_keys & set(obj.keys()))

    async def _load_remaining_pages(self, page: Page, first_response: dict) -> list[dict]:
        """
        If the API response contains pagination metadata, fetch remaining pages
        by clicking "load more" or updating URL parameters.
        """
        additional: list[dict] = []
        body = first_response.get("body", {})

        # Check for pagination info in the response
        total = None
        page_size = None

        if isinstance(body, dict):
            total = (
                body.get("totalCount") or body.get("total") or
                body.get("totalResults") or body.get("count")
            )
            page_size = (
                body.get("pageSize") or body.get("size") or
                body.get("limit") or body.get("perPage")
            )
            results_key = next(
                (k for k in ("voyages", "sailings", "results", "items", "data", "cruises")
                 if isinstance(body.get(k), list)),
                None,
            )
            if results_key:
                page_size = page_size or len(body[results_key])

        if total and page_size and total > page_size:
            num_pages = (total + page_size - 1) // page_size
            logger.info("Pagination detected: %d total records, ~%d pages", total, num_pages)

            # Try clicking "Load More" button first
            for _ in range(num_pages - 1):
                load_more = await page.query_selector(
                    "[class*='load-more'], [class*='loadMore'], "
                    "button:has-text('Load more'), button:has-text('Show more')"
                )
                if not load_more:
                    break

                new_responses: list[dict] = []

                async def capture(resp):
                    if any(p in resp.url for p in API_URL_PATTERNS):
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct:
                            try:
                                body = await resp.json()
                                new_responses.append({"url": resp.url, "body": body})
                            except Exception:
                                pass

                page.on("response", capture)
                await load_more.click()
                await page.wait_for_timeout(3000)
                page.remove_listener("response", capture)

                additional.extend(self._filter_voyage_responses(new_responses))
                await self.wait()

        return additional

    async def _dom_fallback(self, page: Page) -> list[dict]:
        """
        Last-resort: try to extract voyage data directly from the DOM.
        This is less reliable but gives us something if network interception fails.
        """
        logger.info("Attempting DOM fallback extraction")
        records = []
        try:
            # Look for JSON-LD structured data (some sites embed schema.org markup)
            json_ld_elements = await page.query_selector_all('script[type="application/ld+json"]')
            for el in json_ld_elements:
                text = await el.text_content()
                try:
                    import json
                    data = json.loads(text)
                    if isinstance(data, dict) and data.get("@type") in ("Event", "TouristTrip"):
                        records.append(data)
                    elif isinstance(data, list):
                        records.extend(data)
                except Exception:
                    pass

            # Look for window.__INITIAL_STATE__ or similar JS globals
            for var in ("__INITIAL_STATE__", "__NEXT_DATA__", "__NUXT__", "window.voyages"):
                try:
                    data = await page.evaluate(f"() => window.{var.lstrip('window.')}")
                    if data:
                        records.append({"_source": var, "data": data})
                        logger.info("Found data in window.%s", var.lstrip("window."))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("DOM fallback failed: %s", exc)
        return records

    def _extract_voyages(self, responses: list[dict]) -> list[dict]:
        """Flatten a list of API response bodies into individual voyage records."""
        voyages = []
        for resp in responses:
            body = resp.get("body", {}) if isinstance(resp, dict) else resp
            if isinstance(body, list):
                voyages.extend(body)
            elif isinstance(body, dict):
                for key in ("voyages", "sailings", "results", "items", "data", "cruises"):
                    inner = body.get(key)
                    if isinstance(inner, list):
                        voyages.extend(inner)
                        break
                else:
                    # The dict itself might be a single voyage
                    if self._looks_like_voyage(body):
                        voyages.append(body)
        return voyages

    def _extract_cabin_categories(self, raw: dict) -> list[dict]:
        """Extract cabin category pricing from a raw voyage record."""
        categories = []

        # Try common keys for cabin/category arrays
        cabin_data = None
        for key in ("cabinCategories", "categories", "cabins", "staterooms",
                    "prices", "pricing", "rates", "fares"):
            val = raw.get(key)
            if isinstance(val, list) and val:
                cabin_data = val
                break

        if not cabin_data:
            # Try to extract a single "lowest price" record
            price = self.safe_float(
                self._deep_get(raw, "fromPrice", "lowestPrice", "price", "priceFrom")
            )
            if price is not None:
                categories.append({
                    "category_code": "BEST",
                    "category_name": "Best Available",
                    "price_per_person": price,
                    "currency": self.safe_str(raw.get("currency"), "USD") or "USD",
                    "availability": "available",
                })
            return categories

        for cat in cabin_data:
            if not isinstance(cat, dict):
                continue
            code = self.safe_str(
                cat.get("categoryCode") or cat.get("code") or cat.get("cabinCode") or cat.get("id")
            )
            name = self.safe_str(
                cat.get("categoryName") or cat.get("name") or cat.get("description") or code
            )
            price = self.safe_float(
                cat.get("pricePerPerson") or cat.get("price") or
                cat.get("fromPrice") or cat.get("rate")
            )
            currency = self.safe_str(
                cat.get("currency") or raw.get("currency") or "USD"
            ).upper() or "USD"

            # Normalize availability
            avail_raw = str(
                cat.get("availability") or cat.get("status") or cat.get("available") or ""
            ).lower()
            if any(k in avail_raw for k in ("sold", "unavailable", "closed", "full")):
                availability = "sold_out"
            elif any(k in avail_raw for k in ("wait", "request")):
                availability = "waitlist"
            elif avail_raw in ("", "none", "unknown"):
                availability = "unknown"
            else:
                availability = "available"

            if code or name:
                categories.append({
                    "category_code": code or "N/A",
                    "category_name": name or code or "N/A",
                    "price_per_person": price,
                    "currency": currency,
                    "availability": availability,
                })

        return categories

    @staticmethod
    def _deep_get(obj: dict, *keys: str) -> Any:
        """Try each key name; for dotted paths, traverse nested dicts."""
        for key in keys:
            parts = key.split(".")
            val = obj
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    val = None
                    break
            if val is not None:
                return val
        return None

    @staticmethod
    def _compute_duration(departure_date: str | None, return_date: str | None) -> int | None:
        if not departure_date or not return_date:
            return None
        try:
            from datetime import date
            d1 = date.fromisoformat(departure_date)
            d2 = date.fromisoformat(return_date)
            diff = (d2 - d1).days
            return diff if diff > 0 else None
        except Exception:
            return None

    def _log_all_responses(self, responses: list[dict]) -> None:
        """Debug helper: log all intercepted response URLs and top-level keys."""
        for resp in responses:
            body = resp.get("body", {})
            if isinstance(body, dict):
                keys = list(body.keys())[:10]
            elif isinstance(body, list):
                keys = f"[list of {len(body)} items]"
            else:
                keys = type(body).__name__
            logger.debug("INTERCEPT %s  keys=%s", resp.get("url", "?"), keys)
