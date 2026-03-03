"""
Oceania Cruises scraper.

Strategy: Playwright + network response interception.

Site assessment:
- oceaniacruises.com/cruise-finder is fully JS-rendered; no voyage data in static HTML.
- The HTML contains Angular/Vue template placeholders ({{item.text}}) confirming
  client-side rendering.
- robots.txt specifies Crawl-delay: 10 — we honor this with a 10s request delay.
- /plan-a-cruise*, /myaccount/*, /agent/* are disallowed; /cruise-finder is permitted.
- Backend is NCLH proprietary; API endpoint likely under /api/ (discovered at runtime).

Approach:
1. Navigate to the cruise finder page.
2. Intercept XHR/fetch responses containing cruise data.
3. Handle any pagination or "show more" functionality.
4. Map intercepted JSON → normalized voyage schema.

NOTE: Run with OCEANIA_DEBUG=1 to log all intercepted response URLs.
"""

import logging
import os
from typing import Any

from playwright.async_api import BrowserContext, Page

from base_scraper import BaseScraper

logger = logging.getLogger(__name__)

LISTING_URL = "https://www.oceaniacruises.com/cruise-finder"

# URL fragments that are likely to appear in cruise data API calls.
# Expand based on what you observe in browser DevTools Network tab.
API_URL_PATTERNS = [
    "/api/",
    "/cruises",
    "/finder",
    "/search",
    "/sailings",
    "/voyages",
    "/itineraries",
    "/availability",
    "/results",
    "oceaniacruises.com/api",
]

# CSS selector to wait for — cruise cards should appear before we stop capturing
CRUISE_CARD_SELECTOR = (
    "[class*='cruise'], [class*='sailing'], [class*='itinerary'], "
    "[class*='result'], article, .card"
)

DEBUG_MODE = os.getenv("OCEANIA_DEBUG", "").lower() in ("1", "true", "yes")

# Oceania's crawl-delay per robots.txt
CRAWL_DELAY_SECONDS = 10.0


class OceaniaCruisesScraper(BaseScraper):
    cruise_line = "oceania_cruises"
    request_delay = CRAWL_DELAY_SECONDS

    async def scrape(self, page: Page, context: BrowserContext) -> list[dict]:
        """
        Navigate to Oceania's cruise finder and capture API responses.
        Returns raw voyage dicts to be passed through normalize().
        """
        all_raw: list[dict] = []

        # --- Step 1: Initial page load with response interception ---
        responses = await self.intercept_json_responses(
            page=page,
            url_patterns=API_URL_PATTERNS,
            navigate_url=LISTING_URL,
            wait_selector=None,
            timeout_ms=60_000,  # Oceania can be slow to load
        )

        if DEBUG_MODE:
            self._log_all_responses(responses)

        voyage_responses = self._filter_voyage_responses(responses)
        logger.info("Found %d potential cruise API responses on initial load", len(voyage_responses))
        all_raw.extend(voyage_responses)

        # --- Step 2: Wait an extra moment for lazy-loaded content ---
        # Oceania's cruise finder may load results in batches
        await page.wait_for_timeout(5000)

        # Capture any late-arriving responses by scrolling to trigger lazy loading
        extra_responses: list[dict] = []

        async def capture_late(resp):
            if any(p in resp.url for p in API_URL_PATTERNS):
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        body = await resp.json()
                        extra_responses.append({"url": resp.url, "body": body})
                    except Exception:
                        pass

        page.on("response", capture_late)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3000)
        page.remove_listener("response", capture_late)

        extra_voyage = self._filter_voyage_responses(extra_responses)
        if extra_voyage:
            logger.info("Captured %d additional responses after scroll", len(extra_voyage))
            all_raw.extend(extra_voyage)

        # --- Step 3: Pagination ---
        if voyage_responses:
            all_raw.extend(
                await self._load_remaining_pages(page, voyage_responses[0])
            )

        # --- Step 4: Fallback ---
        if not all_raw:
            logger.warning(
                "No cruise data captured via interception. "
                "Attempting fallback DOM extraction."
            )
            dom_records = await self._dom_fallback(page)
            all_raw.extend(dom_records)

        return self._extract_cruises(all_raw)

    def normalize(self, raw: dict) -> dict | None:
        """
        Map a raw cruise record from Oceania's API to the normalized schema.

        Oceania uses NCLH's proprietary API; field names are unknown until
        a live capture. This implementation covers common naming patterns.
        """
        def get(*keys, default=None):
            for key in keys:
                val = self._deep_get(raw, key)
                if val is not None:
                    return val
            return default

        voyage_id = self.safe_str(
            get("cruiseCode", "voyageCode", "code", "id", "sailingCode",
                "itineraryCode", "packageCode"),
        )
        if not voyage_id:
            logger.debug("Skipping Oceania record with no ID: keys=%s", list(raw.keys())[:5])
            return None

        voyage_name = self.safe_str(
            get("cruiseName", "voyageName", "name", "title", "itineraryName",
                "destinationName", "description"),
            fallback=voyage_id,
        )

        ship_name = self.safe_str(
            get("shipName", "ship", "vessel", "shipCode"),
        )

        departure_port = self.safe_str(
            get("departurePort", "embarkPort", "embarkation", "startPort",
                "homePort", "fromPort", "embarkCity"),
        )

        departure_date = self.parse_date(
            get("departureDate", "startDate", "sailDate", "embarkDate",
                "fromDate", "departDate")
        )
        if not departure_date:
            logger.debug("Skipping Oceania record %r — no departure date", voyage_id)
            return None

        return_date = self.parse_date(
            get("returnDate", "endDate", "disembarkDate", "arrivalDate",
                "toDate", "debarkDate")
        )

        duration_nights = self.safe_int(
            get("durationNights", "duration", "nights", "numNights",
                "voyageDuration", "lengthOfCruise")
        )
        if duration_nights is None:
            duration_nights = self._compute_duration(departure_date, return_date)
        if duration_nights is None:
            duration_nights = 0

        region = self.safe_str(
            get("region", "destination", "area", "zone", "itineraryRegion",
                "destinationRegion", "cruiseArea"),
        )

        voyage_url = self.safe_str(
            get("url", "cruiseUrl", "voyageUrl", "link", "detailUrl", "bookingUrl"),
        )
        if voyage_url and not voyage_url.startswith("http"):
            voyage_url = "https://www.oceaniacruises.com" + voyage_url

        cabin_categories = self._extract_cabin_categories(raw)
        if not cabin_categories:
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
        """Keep only responses that look like cruise listing data."""
        voyage_responses = []
        for resp in responses:
            body = resp.get("body", {})

            if isinstance(body, list) and body:
                if self._looks_like_cruise(body[0]):
                    voyage_responses.append(resp)
                    continue

            if isinstance(body, dict):
                for key in ("cruises", "voyages", "sailings", "results",
                            "items", "data", "itineraries", "packages"):
                    inner = body.get(key)
                    if isinstance(inner, list) and inner and self._looks_like_cruise(inner[0]):
                        voyage_responses.append(resp)
                        break

        return voyage_responses

    def _looks_like_cruise(self, obj: Any) -> bool:
        """Heuristic: does this dict resemble a cruise/sailing record?"""
        if not isinstance(obj, dict):
            return False
        cruise_keys = {
            "cruiseCode", "voyageCode", "code", "sailCode",
            "departureDate", "startDate", "embarkDate", "sailDate",
            "shipName", "ship", "vessel",
            "duration", "nights", "durationNights",
            "itineraryCode", "destinationName",
        }
        return bool(cruise_keys & set(obj.keys()))

    async def _load_remaining_pages(self, page: Page, first_response: dict) -> list[dict]:
        """Fetch remaining pages if the API paginates results."""
        additional: list[dict] = []
        body = first_response.get("body", {})

        if not isinstance(body, dict):
            return additional

        total = (
            body.get("totalCount") or body.get("total") or
            body.get("totalResults") or body.get("recordCount")
        )
        results_key = next(
            (k for k in ("cruises", "voyages", "sailings", "results", "items", "data")
             if isinstance(body.get(k), list)),
            None,
        )
        page_size = body.get("pageSize") or body.get("limit") or (
            len(body[results_key]) if results_key else None
        )

        if not total or not page_size or total <= page_size:
            return additional

        num_pages = (total + page_size - 1) // page_size
        logger.info("Oceania pagination: %d total, ~%d pages", total, num_pages)

        for page_num in range(2, num_pages + 1):
            new_responses: list[dict] = []

            async def capture(resp, _nr=new_responses):
                if any(p in resp.url for p in API_URL_PATTERNS):
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        try:
                            body = await resp.json()
                            _nr.append({"url": resp.url, "body": body})
                        except Exception:
                            pass

            page.on("response", capture)

            # Try clicking "load more" or "next page" button
            load_more = await page.query_selector(
                "button:has-text('Load more'), button:has-text('Show more'), "
                "[class*='load-more'], [class*='loadMore'], "
                "[class*='next-page'], [aria-label='Next page']"
            )
            if not load_more:
                page.remove_listener("response", capture)
                break

            await load_more.click()
            await page.wait_for_timeout(CRAWL_DELAY_SECONDS * 1000)
            page.remove_listener("response", capture)

            additional.extend(self._filter_voyage_responses(new_responses))

        return additional

    async def _dom_fallback(self, page: Page) -> list[dict]:
        """Last-resort DOM extraction if network interception yields nothing."""
        logger.info("Oceania: attempting DOM fallback")
        records = []
        try:
            # Check for JSON-LD
            json_ld_els = await page.query_selector_all('script[type="application/ld+json"]')
            for el in json_ld_els:
                import json
                text = await el.text_content()
                try:
                    data = json.loads(text)
                    if isinstance(data, list):
                        records.extend(data)
                    elif isinstance(data, dict):
                        records.append(data)
                except Exception:
                    pass

            # Check for window state objects
            for var in ("__INITIAL_STATE__", "__NEXT_DATA__", "__APP_STATE__", "OCI"):
                try:
                    data = await page.evaluate(f"() => window['{var}']")
                    if data:
                        records.append({"_source": var, "data": data})
                        logger.info("Oceania: found data in window['%s']", var)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Oceania DOM fallback failed: %s", exc)
        return records

    def _extract_cruises(self, responses: list[dict]) -> list[dict]:
        """Flatten response bodies into individual cruise records."""
        cruises = []
        seen_ids: set[str] = set()

        for resp in responses:
            body = resp.get("body", {}) if isinstance(resp, dict) else resp
            items = []

            if isinstance(body, list):
                items = body
            elif isinstance(body, dict):
                for key in ("cruises", "voyages", "sailings", "results",
                            "items", "data", "itineraries"):
                    inner = body.get(key)
                    if isinstance(inner, list):
                        items = inner
                        break
                else:
                    if self._looks_like_cruise(body):
                        items = [body]

            for item in items:
                # Deduplicate by any available ID field
                item_id = (
                    item.get("cruiseCode") or item.get("voyageCode") or
                    item.get("code") or item.get("id")
                )
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(str(item_id))
                cruises.append(item)

        return cruises

    def _extract_cabin_categories(self, raw: dict) -> list[dict]:
        """Extract cabin/stateroom pricing from a raw cruise record."""
        categories = []

        cabin_data = None
        for key in ("cabinCategories", "stateroomCategories", "categories", "cabins",
                    "staterooms", "prices", "pricing", "rates", "fares", "grades"):
            val = raw.get(key)
            if isinstance(val, list) and val:
                cabin_data = val
                break

        if not cabin_data:
            # Try extracting a single lowest price
            price = self.safe_float(
                self._deep_get(raw, "fromPrice", "lowestPrice", "price",
                               "priceFrom", "startingFrom", "startingPrice")
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
                cat.get("categoryCode") or cat.get("gradeCode") or
                cat.get("code") or cat.get("id") or cat.get("stateroomCode")
            )
            name = self.safe_str(
                cat.get("categoryName") or cat.get("gradeName") or
                cat.get("name") or cat.get("description") or code
            )
            price = self.safe_float(
                cat.get("pricePerPerson") or cat.get("price") or
                cat.get("fromPrice") or cat.get("rate") or cat.get("fare")
            )
            currency = self.safe_str(
                cat.get("currency") or raw.get("currency") or "USD"
            ).upper() or "USD"

            avail_raw = str(
                cat.get("availability") or cat.get("status") or
                cat.get("available") or ""
            ).lower()
            if any(k in avail_raw for k in ("sold", "unavailable", "closed", "full", "0")):
                availability = "sold_out"
            elif any(k in avail_raw for k in ("wait", "request")):
                availability = "waitlist"
            elif avail_raw in ("", "none", "unknown", "null"):
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
        for resp in responses:
            body = resp.get("body", {})
            if isinstance(body, dict):
                keys = list(body.keys())[:10]
            elif isinstance(body, list):
                keys = f"[list of {len(body)} items]"
            else:
                keys = type(body).__name__
            logger.debug("INTERCEPT %s  keys=%s", resp.get("url", "?"), keys)
