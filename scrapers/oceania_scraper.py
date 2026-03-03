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
        pagination = first_body.get("pagination", {})
        total_pages = pagination.get("totalPages") or pagination.get("pages") or 1
        total_count = pagination.get("totalCount") or pagination.get("total") or len(page1_results)
        page_size = pagination.get("pageSize") or pagination.get("size") or len(page1_results)

        logger.info("Oceania pagination: %d results, %d pages (pageSize=%d)",
                    total_count, total_pages, page_size)

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

        departure_date = self.parse_date(
            raw.get("embarkDate") or raw.get("departureDate") or raw.get("startDate")
        )
        if not departure_date:
            logger.debug("Skipping Oceania record %r — no departure date", voyage_id)
            return None

        return_date = self.parse_date(
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
        Load remaining pages of cruise results.
        Strategy: click Next Page button and intercept the resulting API call.
        """
        additional: list[dict] = []

        for page_num in range(2, total_pages + 1):
            await self.wait(CRAWL_DELAY_SECONDS)  # honor crawl-delay

            new_responses: list[dict] = []

            async def capture_page(response, _nr=new_responses):
                if CRUISE_API_PATTERN in response.url and "json" in response.headers.get("content-type", ""):
                    try:
                        body = await response.json()
                        _nr.append({"url": response.url, "body": body})
                    except Exception:
                        pass

            page.on("response", capture_page)

            # Try to click a pagination button
            next_btn = await page.query_selector(
                "button[aria-label='Next page'], button[aria-label='Next'], "
                "[class*='next-page']:not([disabled]), [class*='pagination-next']:not([disabled]), "
                "button:has-text('Next')"
            )
            if not next_btn:
                page.remove_listener("response", capture_page)
                logger.info("No next-page button found at page %d — stopping", page_num)
                break

            await next_btn.click()
            await page.wait_for_timeout(5_000)
            page.remove_listener("response", capture_page)

            for resp in new_responses:
                results = resp["body"].get("results", [])
                additional.extend(results)
                logger.info("Page %d: %d results", page_num, len(results))

        return additional

    # ------------------------------------------------------------------
    # Cabin categories
    # ------------------------------------------------------------------

    def _extract_cabin_categories(self, raw: dict) -> list[dict]:
        """
        Extract pricing from Oceania API record.

        Confirmed price fields:
        - faresFrom: lowest fare (any category)
        - minBrochureFare: brochure price
        - minPromotionalFare: promotional price
        - minCruiseOnlyFare: cruise-only price (no airfare)
        """
        categories = []

        price_fields = [
            ("minCruiseOnlyFare", "CRZONLY", "Cruise Only"),
            ("minPromotionalFare", "PROMO", "Promotional Fare"),
            ("faresFrom", "BEST", "Best Available"),
            ("minBrochureFare", "BROCHURE", "Brochure Fare"),
        ]

        for field, code, name in price_fields:
            val = raw.get(field)
            price = self.safe_float(val)
            if price is not None and price > 0:
                categories.append({
                    "category_code": code,
                    "category_name": name,
                    "price_per_person": price,
                    "currency": "USD",
                    "availability": "available",
                })

        # Deduplicate if multiple fields have the same price
        seen_prices: set[float] = set()
        deduped = []
        for cat in categories:
            p = cat["price_per_person"]
            if p not in seen_prices:
                seen_prices.add(p)
                deduped.append(cat)

        if not deduped:
            deduped = [{
                "category_code": "N/A",
                "category_name": "Price on request",
                "price_per_person": None,
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
