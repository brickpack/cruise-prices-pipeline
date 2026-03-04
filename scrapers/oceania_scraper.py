"""
Oceania Cruises scraper.

Strategy: Playwright + network interception targeting the confirmed API endpoint.

Confirmed findings (from live run logs):
- The exact API endpoint is:
    GET https://www.oceaniacruises.com/api/cruise-details/v1/cruises
        ?filters=duration|time_frame|not:port|port|ship|marketing_region
        &sort=featured:desc&page=1&pageSize=10
- Response structure: { filters: {...}, results: [...], pagination: {...} }
- Confirmed result field names (from log):
    id, image, mapImage, offerImage, shipImage,
    voyageName, duration, shipName, shipCode,
    startAndEndLabel, faresFrom, isFeatured, specialOffers,
    embarkPortName, debarkPortName, detailsURL,
    embarkDate, debarkDate, primaryRegion,
    minBrochureFare, minPromotionalFare, minCruiseOnlyFare

robots.txt: Crawl-delay: 10 — honored between paginated requests.
"""

import asyncio
import logging
import os
from typing import Any

from playwright.async_api import BrowserContext, Page

from base_scraper import BaseScraper

logger = logging.getLogger(__name__)

LISTING_URL = "https://www.oceaniacruises.com/cruise-finder"

# Confirmed API endpoint pattern (page/pageSize params will vary for pagination)
CRUISE_API_PATTERN = "oceaniacruises.com/api/cruise-details/v1/cruises"

# Base API URL for constructing paginated requests
CRUISE_API_BASE = "https://www.oceaniacruises.com/api/cruise-details/v1/cruises"

DEBUG_MODE = os.getenv("OCEANIA_DEBUG", "").lower() in ("1", "true", "yes")

# Oceania's crawl-delay per robots.txt
CRAWL_DELAY_SECONDS = 10.0


class OceaniaCruisesScraper(BaseScraper):
    cruise_line = "oceania_cruises"
    request_delay = CRAWL_DELAY_SECONDS

    async def scrape(self, page: Page, context: BrowserContext) -> list[dict]:
        """
        Navigate to Oceania's cruise finder, intercept the cruise API, and
        collect all paginated results.
        """
        all_results: list[dict] = []
        api_responses: list[dict] = []
        first_request_url: str | None = None

        async def capture_cruise_api(response):
            nonlocal first_request_url
            if CRUISE_API_PATTERN not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                results_count = len(body.get("results", []))
                pagination = body.get("pagination", {})
                logger.info(
                    "Oceania API: %d results, pagination=%s  url=%s",
                    results_count, pagination, response.url
                )
                api_responses.append({"url": response.url, "body": body})
                if first_request_url is None:
                    first_request_url = response.url
            except Exception as exc:
                logger.warning("Could not parse Oceania API response: %s", exc)

        page.on("response", capture_cruise_api)

        logger.info("Navigating to %s", LISTING_URL)
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
        # Wait for Angular to init and fire the API call (12s to be safe)
        await page.wait_for_timeout(12_000)

        page.remove_listener("response", capture_cruise_api)
        logger.info("Captured %d Oceania API responses on initial load", len(api_responses))

        if not api_responses:
            logger.warning("No Oceania API responses captured. Attempting DOM fallback.")
            dom_records = await self._dom_fallback(page)
            return dom_records

        # Extract page 1 results
        first_body = api_responses[0]["body"]
        page1_results = first_body.get("results", [])
        all_results.extend(page1_results)

        # Check pagination
        # Confirmed API structure: {"page": 1, "perPage": 10, "totalRecords": 610}
        # (NOT totalPages/pageSize — those don't exist in the response)
        pagination = first_body.get("pagination", {})
        logger.info("Oceania pagination object: %s", pagination)

        total_count = (
            pagination.get("totalRecords") or
            pagination.get("totalCount") or
            pagination.get("total") or
            len(page1_results)
        )
        page_size = (
            pagination.get("perPage") or
            pagination.get("pageSize") or
            pagination.get("size") or
            len(page1_results)
        ) or 10

        import math
        total_pages = math.ceil(total_count / page_size) if page_size > 0 else 1

        logger.info("Oceania pagination: %d total records, %d per page → %d pages",
                    total_count, page_size, total_pages)

        # Fetch remaining pages by intercepting new API calls triggered by pagination clicks
        if total_pages > 1:
            all_results.extend(
                await self._load_remaining_pages(page, first_request_url, total_pages)
            )

        logger.info("Total Oceania results collected: %d", len(all_results))
        return all_results

    def normalize(self, raw: dict) -> dict | None:
        """
        Map a confirmed Oceania API result to the normalized schema.

        Confirmed field names from live API response:
        id, voyageName, duration, shipName, shipCode,
        embarkPortName, debarkPortName, detailsURL,
        embarkDate, debarkDate, primaryRegion,
        faresFrom, minBrochureFare, minPromotionalFare, minCruiseOnlyFare
        """
        # Log ALL field names + key values on first record to aid debugging
        if not hasattr(self, '_fields_logged'):
            self._fields_logged = True
            logger.info("Oceania result keys (ALL %d): %s", len(raw), list(raw.keys()))
            for key in ("id", "voyageName", "shipName", "embarkPortName",
                        "embarkDate", "debarkDate", "duration", "primaryRegion",
                        "detailsURL", "faresFrom", "minCruiseOnlyFare"):
                if key in raw:
                    logger.info("  %s = %r", key, raw[key])

        voyage_id = self.safe_str(raw.get("id"))
        if not voyage_id:
            logger.debug("Skipping Oceania record with no id: keys=%s", list(raw.keys())[:5])
            return None

        voyage_name = self.safe_str(
            raw.get("voyageName") or raw.get("name") or raw.get("title"),
            fallback=voyage_id,
        )

        ship_name = self.safe_str(
            raw.get("shipName") or raw.get("shipCode")
        ) or "Unknown Ship"

        departure_port = self.safe_str(
            raw.get("embarkPortName") or raw.get("departurePort") or raw.get("embarkPort")
        ) or "Unknown Port"

        departure_date = self._parse_oceania_date(
            raw.get("embarkDate") or raw.get("departureDate") or raw.get("startDate")
        )
        if not departure_date:
            logger.debug("Skipping Oceania record %r — no departure date (raw embarkDate=%r)",
                         voyage_id, raw.get("embarkDate"))
            return None

        return_date = self._parse_oceania_date(
            raw.get("debarkDate") or raw.get("returnDate") or raw.get("endDate")
        )

        duration_nights = self.safe_int(
            raw.get("duration") or raw.get("durationNights") or raw.get("nights")
        )
        if duration_nights is None:
            duration_nights = self._compute_duration(departure_date, return_date)
        if duration_nights is None or duration_nights < 1:
            duration_nights = 1

        region = self.safe_str(
            raw.get("primaryRegion") or raw.get("region") or raw.get("destination")
        ) or "Unknown Region"

        voyage_url = self.safe_str(raw.get("detailsURL") or raw.get("url") or raw.get("link"))
        if voyage_url and not voyage_url.startswith("http"):
            voyage_url = "https://www.oceaniacruises.com" + voyage_url

        cabin_categories = self._extract_cabin_categories(raw)

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
    # Pagination
    # ------------------------------------------------------------------

    async def _load_remaining_pages(
        self, page: Page, first_url: str | None, total_pages: int
    ) -> list[dict]:
        """
        Load remaining pages of cruise results via direct API fetch calls.

        Strategy: use page.evaluate() to call the Oceania API directly from
        within the browser context (shares cookies/session). This is more
        reliable than trying to click pagination UI buttons.

        The confirmed API endpoint accepts ?page=N&pageSize=10 parameters.
        We cap at a reasonable limit to avoid excessively long runs.
        """
        additional: list[dict] = []

        # Build the base URL from the confirmed API endpoint.
        # Use the filters and sort from the first request if available.
        if first_url:
            import re
            # Extract the filters/sort params from the first URL
            filters_match = re.search(r"filters=[^&]+", first_url)
            sort_match = re.search(r"sort=[^&]+", first_url)
            filters_param = filters_match.group(0) if filters_match else \
                "filters=duration%7Ctime_frame%7Cnot:port%7Cport%7Cship%7Cmarketing_region"
            sort_param = sort_match.group(0) if sort_match else "sort=featured:desc"
        else:
            filters_param = "filters=duration%7Ctime_frame%7Cnot:port%7Cport%7Cship%7Cmarketing_region"
            sort_param = "sort=featured:desc"

        # Cap total_pages to avoid excessively long scrapes
        max_pages = min(total_pages, 100)

        for page_num in range(2, max_pages + 1):
            await self.wait(CRAWL_DELAY_SECONDS)  # honor robots.txt crawl-delay

            api_url = (
                f"{CRUISE_API_BASE}"
                f"?{filters_param}&{sort_param}&page={page_num}&pageSize=10"
            )
            logger.info("Fetching Oceania page %d/%d: %s", page_num, max_pages, api_url)

            try:
                # Use page.evaluate() to fetch from within the browser context
                # (shares session cookies, avoids CORS issues)
                result = await page.evaluate(
                    """async (url) => {
                        try {
                            const resp = await fetch(url, {
                                headers: {
                                    'Accept': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest'
                                }
                            });
                            if (!resp.ok) return {error: resp.status};
                            return await resp.json();
                        } catch(e) {
                            return {error: String(e)};
                        }
                    }""",
                    api_url
                )

                if not result or "error" in result:
                    logger.warning("Oceania page %d fetch error: %s", page_num, result)
                    break

                results = result.get("results", [])
                additional.extend(results)
                logger.info("Page %d: %d results (running total: %d)",
                            page_num, len(results), len(additional))

                if not results:
                    logger.info("Empty results on page %d — stopping", page_num)
                    break

            except Exception as exc:
                logger.warning("Failed to fetch Oceania page %d: %s", page_num, exc)
                break

        return additional

    # ------------------------------------------------------------------
    # Cabin categories
    # ------------------------------------------------------------------

    def _extract_cabin_categories(self, raw: dict) -> list[dict]:
        """
        Extract pricing from Oceania API record.

        Confirmed price fields:
        - faresFrom:          lowest current fare (may reflect a discount)
        - minCruiseOnlyFare:  cruise-only price (no airfare)
        - minPromotionalFare: promotional/sale price
        - minBrochureFare:    original brochure/rack price (used as original_price)
        """
        brochure_price = self._parse_price(raw.get("minBrochureFare"))

        # Current price candidates in preference order
        current_candidates = [
            ("minCruiseOnlyFare", "CRZONLY", "Cruise Only"),
            ("minPromotionalFare", "PROMO",   "Promotional Fare"),
            ("faresFrom",          "BEST",    "Best Available"),
        ]

        categories = []
        for field, code, name in current_candidates:
            current = self._parse_price(raw.get(field))
            if current is not None and current > 0:
                # Show brochure as original_price only when it's higher than current
                original = (
                    brochure_price
                    if brochure_price and brochure_price > current
                    else None
                )
                categories.append({
                    "category_code": code,
                    "category_name": name,
                    "price_per_person": current,
                    "original_price": original,
                    "currency": "USD",
                    "availability": "available",
                })

        # Deduplicate by current price
        seen_prices: set[float] = set()
        deduped = []
        for cat in categories:
            p = cat["price_per_person"]
            if p not in seen_prices:
                seen_prices.add(p)
                deduped.append(cat)

        # Fall back to brochure price alone if no current price found
        if not deduped and brochure_price and brochure_price > 0:
            deduped = [{
                "category_code": "BROCHURE",
                "category_name": "Brochure Fare",
                "price_per_person": brochure_price,
                "original_price": None,
                "currency": "USD",
                "availability": "available",
            }]

        if not deduped:
            deduped = [{
                "category_code": "N/A",
                "category_name": "Price on request",
                "price_per_person": None,
                "original_price": None,
                "currency": "USD",
                "availability": "unknown",
            }]

        return deduped

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _dom_fallback(self, page: Page) -> list[dict]:
        """Last-resort DOM extraction if API interception yields nothing."""
        logger.info("Oceania: attempting DOM fallback")
        records = []
        try:
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

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_price(value: Any) -> float | None:
        """
        Parse an Oceania price field which may be a formatted string like '$5,480'
        or a numeric value like 5480 or 5480.0.
        """
        if value is None:
            return None
        # If it's already a number, return it directly
        if isinstance(value, (int, float)):
            return float(value)
        # Strip currency formatting: '$5,480' → '5480'
        s = str(value).strip()
        s = s.replace('$', '').replace(',', '').replace(' ', '')
        if not s:
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_oceania_date(value: Any) -> str | None:
        """
        Parse an Oceania date field which may be:
        - ISO string: "2025-06-15" or "2025-06-15T00:00:00"
        - Unix timestamp in seconds: 1739318400
        - Unix timestamp in milliseconds: 1739318400000
        - Other string formats: "06/15/2025"
        """
        if value is None:
            return None
        # Try numeric timestamp
        try:
            ts = float(str(value))
            if ts > 0:
                # ms if > year 2100 in seconds
                if ts > 4_102_444_800:
                    ts = ts / 1000.0
                from datetime import datetime, timezone
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            pass
        # Fall back to string date parsing
        from base_scraper import BaseScraper as _BS
        return _BS.parse_date(value)

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
