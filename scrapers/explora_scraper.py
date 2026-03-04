"""
Explora Journeys scraper.

Strategy: Playwright + network interception targeting the Coveo search API.

Confirmed findings (from live run logs):
- explorajourneys.com/us/en/find-your-journey uses Coveo (a content search
  platform) as its voyage search backend.
- The actual API call is:
    POST https://explorajourneysproduction1ianvud5y.org.coveo.com/rest/search/v2
    ?organizationId=explorajourneysproduction1ianvud5y
- The response contains: totalCount, totalCountFiltered, results[] array
- Each result in results[] is a Coveo document with raw.* fields for metadata
  and numeric pricing fields like raw.priceperguest_doubleoccupancy_full

Confirmed raw.* field names (from live API response logs):
    systitle, sysurihash, urihash, staticoffertitle,
    sailfromportcountry, priceperguest_doubleoccupancy_discount,
    saildays,                           ← duration in days/nights
    sailfromportcountry_ga,
    priceperguest_doubleoccupancy_full,
    dm_mapimage, permanentid, syslanguage,
    ship,                               ← ship name
    dm_primaryimage, title, destinationname,  ← region/destination
    mapimage, currency,
    priceperguest_singleoccupancy_discount, sailtoportcountry,
    sailtoportcountry_ga, sailtoport, primaryimage,
    shipcode,                           ← ship code (fallback ID source)
    sailfromport_ga, destinationid,
    sailtodateday,                      ← return date (day string)
    priceperguest_singleoccupancy_full,
    syssource,
    sailfromport                        ← departure port

Additional fields likely present (beyond the first 30 logged):
    sailfromdatetime    ← departure date as Unix timestamp (ms or s)
    sailtodatetime      ← return date as Unix timestamp
    voyagecode          ← voyage ID (may be beyond first 30 fields)
    permanentid         ← Coveo document ID (confirmed present)

Auth token: fetched from https://explorajourneys.com/bin/coveo/auth/token
→ { accessToken, expiresIn }
"""

import asyncio
import logging
import os
from typing import Any

from playwright.async_api import BrowserContext, Page

from base_scraper import BaseScraper

logger = logging.getLogger(__name__)

LISTING_URL = "https://www.explorajourneys.com/us/en/find-your-journey"

# The confirmed Coveo search API endpoint (org ID embedded in subdomain)
COVEO_URL_PATTERN = "coveo.com/rest/search"
AUTH_TOKEN_URL = "explorajourneys.com/bin/coveo/auth/token"

DEBUG_MODE = os.getenv("EXPLORA_DEBUG", "").lower() in ("1", "true", "yes")


class ExploraJourneysScraper(BaseScraper):
    cruise_line = "explora_journeys"
    request_delay = 3.0

    async def scrape(self, page: Page, context: BrowserContext) -> list[dict]:
        """
        Navigate to Explora's find-your-journey page, intercept the Coveo
        search API response, and collect all paginated voyage results.
        """
        all_results: list[dict] = []
        coveo_responses: list[dict] = []
        coveo_auth_token: list[str] = []  # captured from auth token endpoint
        coveo_request_body: list[dict] = []  # captured from Coveo POST body

        async def capture_coveo(response):
            """Capture Coveo search API responses and auth tokens."""
            url = response.url
            ct = response.headers.get("content-type", "")

            # Capture auth token
            if AUTH_TOKEN_URL in url and "json" in ct:
                try:
                    body = await response.json()
                    token = body.get("accessToken") or body.get("token")
                    if token:
                        coveo_auth_token.append(token)
                        logger.info("Captured Coveo auth token (length=%d)", len(token))
                except Exception as exc:
                    logger.warning("Could not parse auth token response: %s", exc)
                return

            # Capture Coveo search API responses
            if COVEO_URL_PATTERN not in url:
                return
            if "json" not in ct:
                return
            try:
                body = await response.json()
                logger.info("Coveo response: totalCount=%s  results=%s",
                            body.get("totalCount"),
                            len(body.get("results", [])))
                coveo_responses.append({"url": url, "body": body})
            except Exception as exc:
                logger.warning("Could not parse Coveo response: %s", exc)

        page.on("response", capture_coveo)

        logger.info("Navigating to %s", LISTING_URL)
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=45_000)
        # Wait for Coveo search to complete and render results
        await page.wait_for_timeout(8_000)

        page.remove_listener("response", capture_coveo)
        logger.info("Captured %d Coveo responses on initial load", len(coveo_responses))

        if not coveo_responses:
            logger.warning("No Coveo responses intercepted. Trying fallback scroll.")
            # Sometimes Coveo fires late — try scrolling to trigger load
            late_responses: list[dict] = []

            async def capture_late(response):
                if COVEO_URL_PATTERN in response.url and "json" in response.headers.get("content-type", ""):
                    try:
                        body = await response.json()
                        late_responses.append({"url": response.url, "body": body})
                    except Exception:
                        pass

            page.on("response", capture_late)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(5_000)
            page.remove_listener("response", capture_late)
            coveo_responses.extend(late_responses)

        # Extract results from all captured Coveo responses
        for resp in coveo_responses:
            body = resp["body"]
            results = body.get("results", [])
            all_results.extend(results)
            logger.info("Extracted %d results from %s", len(results), resp["url"])

        # Handle pagination: if first response shows there are more results,
        # load subsequent pages via direct Coveo API POST calls
        if coveo_responses:
            first_body = coveo_responses[0]["body"]
            total = first_body.get("totalCount", 0)
            page_size = len(first_body.get("results", []))
            auth_token = coveo_auth_token[0] if coveo_auth_token else None
            if not auth_token:
                logger.info("No Coveo auth token captured — pagination unavailable")
            elif total > page_size and page_size > 0:
                logger.info("Pagination needed: %d total, %d per page", total, page_size)
                all_results.extend(
                    await self._load_more_pages(page, total, page_size, auth_token)
                )

        logger.info("Total Coveo results collected: %d", len(all_results))
        return all_results

    def normalize(self, raw: dict) -> dict | None:
        """
        Map a Coveo search result to the normalized voyage schema.

        Coveo result structure:
        {
          "title": "voyage name",
          "uri": "https://explorajourneys.com/us/en/.../journeys/CODE?id-journey=ID",
          "clickUri": "...",
          "raw": {
            "permanentid": "...",        ← Coveo document ID
            "ship": "EXPLORA I",         ← ship name (confirmed)
            "shipcode": "EX1",           ← ship code
            "sailfromport": "Miami",     ← departure port (confirmed)
            "saildays": 14,              ← duration nights (confirmed)
            "destinationname": "Caribbean", ← region (confirmed)
            "sailfromdatetime": 1739318400,  ← Unix timestamp (s or ms)
            "sailtodatetime": ...,
            "sailtodateday": "20260226", ← return date as YYYYMMDD string
            "priceperguest_doubleoccupancy_full": 4299.0,
            "priceperguest_singleoccupancy_full": 6500.0,
            "currency": "USD",
            ...
          }
        }
        """
        raw_fields = raw.get("raw", {})

        # Log ALL field names on first record (no truncation) to aid debugging
        if not hasattr(self, '_fields_logged'):
            self._fields_logged = True
            logger.info("Coveo result top-level keys: %s", list(raw.keys()))
            logger.info("Coveo result raw.* field names (ALL %d fields): %s",
                        len(raw_fields), list(raw_fields.keys()))
            # Also log a sample of values for key fields
            for key in ("permanentid", "ship", "shipcode", "sailfromport",
                        "saildays", "destinationname", "sailfromdatetime",
                        "sailtodatetime", "sailtodateday", "voyagecode",
                        "title", "currency"):
                if key in raw_fields:
                    logger.info("  raw.%s = %r", key, raw_fields[key])

        # ------------------------------------------------------------------
        # Voyage ID — try multiple field name patterns
        # ------------------------------------------------------------------
        voyage_id = self.safe_str(
            raw_fields.get("voyagecode") or
            raw_fields.get("voyage_code") or
            raw_fields.get("voyageid") or
            raw_fields.get("sailcode") or
            raw_fields.get("permanentid") or
            self._extract_voyage_id_from_uri(raw.get("uri", ""))
        )
        if not voyage_id:
            logger.debug("Skipping Coveo result with no voyage_id; uri=%s", raw.get("uri", ""))
            return None

        # ------------------------------------------------------------------
        # Voyage name
        # ------------------------------------------------------------------
        voyage_name = self.safe_str(
            raw.get("title") or
            raw_fields.get("title") or
            raw_fields.get("systitle") or
            raw_fields.get("staticoffertitle") or
            raw_fields.get("itinerary") or
            raw_fields.get("voyagename"),
            fallback=voyage_id,
        )

        # ------------------------------------------------------------------
        # Ship name — confirmed field: "ship"
        # ------------------------------------------------------------------
        ship_name = self.safe_str(
            raw_fields.get("ship") or
            raw_fields.get("shipname") or
            raw_fields.get("shipcode")
        ) or "Unknown Ship"

        # ------------------------------------------------------------------
        # Departure port — confirmed field: "sailfromport"
        # ------------------------------------------------------------------
        departure_port = self.safe_str(
            raw_fields.get("sailfromport") or
            raw_fields.get("departureport") or
            raw_fields.get("embarkport") or
            raw_fields.get("homeport")
        ) or "Unknown Port"

        # ------------------------------------------------------------------
        # Departure date — sailfromdatetime (Unix ts) or string fallbacks
        # ------------------------------------------------------------------
        departure_date = self._parse_coveo_date(
            raw_fields.get("sailfromdatetime") or
            raw_fields.get("sailfromdatetimems") or
            raw_fields.get("departuredate") or
            raw_fields.get("startdate")
        )
        if not departure_date:
            # Try string date fields
            departure_date = self.parse_date(
                raw_fields.get("sailfromdateday") or
                raw_fields.get("sailfromdate") or
                raw_fields.get("departureDateStr")
            )
        if not departure_date:
            logger.debug("Skipping Coveo result %r — no departure date; raw keys=%s",
                         voyage_id, list(raw_fields.keys()))
            return None

        # ------------------------------------------------------------------
        # Return date — sailtodatetime (Unix ts) or sailtodateday string
        # ------------------------------------------------------------------
        return_date = self._parse_coveo_date(
            raw_fields.get("sailtodatetime") or
            raw_fields.get("sailtodatetimems") or
            raw_fields.get("returndate") or
            raw_fields.get("enddate")
        )
        if not return_date:
            return_date = self.parse_date(
                raw_fields.get("sailtodateday") or
                raw_fields.get("sailtodate") or
                raw_fields.get("returnDateStr")
            )

        # ------------------------------------------------------------------
        # Duration — confirmed field: "saildays"
        # ------------------------------------------------------------------
        duration_nights = self.safe_int(
            raw_fields.get("saildays") or
            raw_fields.get("numberofnights") or
            raw_fields.get("durationnights") or
            raw_fields.get("duration") or
            raw_fields.get("nights")
        )
        if duration_nights is None:
            duration_nights = self._compute_duration(departure_date, return_date)
        if duration_nights is None or duration_nights < 1:
            duration_nights = 1

        # ------------------------------------------------------------------
        # Region — confirmed field: "destinationname"
        # ------------------------------------------------------------------
        region = self.safe_str(
            raw_fields.get("destinationname") or
            raw_fields.get("region") or
            raw_fields.get("itineraryregion") or
            raw_fields.get("destination") or
            raw_fields.get("area")
        ) or "Unknown Region"

        # ------------------------------------------------------------------
        # Voyage URL
        # ------------------------------------------------------------------
        voyage_url = self.safe_str(
            raw.get("clickUri") or
            raw.get("uri")
        )
        if voyage_url and not voyage_url.startswith("http"):
            voyage_url = "https://www.explorajourneys.com" + voyage_url

        # ------------------------------------------------------------------
        # Pricing: Coveo stores prices in flat raw fields
        # ------------------------------------------------------------------
        cabin_categories = self._extract_coveo_pricing(raw_fields)

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

    async def _load_more_pages(
        self, page: Page, total: int, page_size: int, auth_token: str
    ) -> list[dict]:
        """
        Load remaining pages of Coveo search results via direct API POST calls.

        The Coveo search API (/rest/search/v2) is a POST endpoint that requires
        an Authorization: Bearer <token> header. We captured this token during
        the initial page load from /bin/coveo/auth/token.

        Pagination is controlled by the 'firstResult' offset parameter.
        """
        additional: list[dict] = []
        offset = page_size  # start after the first page already loaded

        COVEO_SEARCH_URL = (
            "https://explorajourneysproduction1ianvud5y.org.coveo.com"
            "/rest/search/v2?organizationId=explorajourneysproduction1ianvud5y"
        )

        # Cap total to avoid excessively long scrapes (max 300 voyages)
        max_results = min(total, 300)

        while offset < max_results:
            await self.wait()  # respect request_delay between calls

            logger.info("Coveo: fetching offset %d–%d of %d",
                        offset, min(offset + page_size, max_results), max_results)

            try:
                result = await page.evaluate(
                    """async (args) => {
                        try {
                            const resp = await fetch(args.url, {
                                method: 'POST',
                                headers: {
                                    'Authorization': 'Bearer ' + args.token,
                                    'Content-Type': 'application/json',
                                    'Accept': 'application/json',
                                },
                                body: JSON.stringify({
                                    firstResult: args.firstResult,
                                    numberOfResults: args.numberOfResults,
                                })
                            });
                            if (!resp.ok) {
                                const text = await resp.text();
                                return {error: `HTTP ${resp.status}`, text: text.slice(0, 200)};
                            }
                            return await resp.json();
                        } catch(e) {
                            return {error: String(e)};
                        }
                    }""",
                    {
                        "url": COVEO_SEARCH_URL,
                        "token": auth_token,
                        "firstResult": offset,
                        "numberOfResults": page_size,
                    }
                )

                if not result or "error" in result:
                    logger.warning("Coveo pagination fetch error at offset %d: %s", offset, result)
                    break

                results = result.get("results", [])
                if not results:
                    logger.info("Empty results at offset %d — stopping pagination", offset)
                    break

                additional.extend(results)
                offset += len(results)
                logger.info("Coveo: got %d results at offset %d (total collected: %d)",
                            len(results), offset - len(results), len(additional))

            except Exception as exc:
                logger.warning("Coveo pagination failed at offset %d: %s", offset, exc)
                break

        return additional

    # ------------------------------------------------------------------
    # Coveo-specific helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_coveo_date(value: Any) -> str | None:
        """
        Parse a Coveo date field.

        Coveo can store dates as:
        - Unix timestamp in seconds (e.g. 1739318400)
        - Unix timestamp in milliseconds (e.g. 1739318400000)
        - ISO date string (e.g. "2026-02-12")
        - YYYYMMDD string (e.g. "20260212")
        """
        if value is None:
            return None
        # Try numeric timestamp
        try:
            ts = float(str(value))
            if ts > 0:
                # Coveo uses milliseconds if value > year 2100 in seconds
                # (year 2100 = 4102444800 seconds; ms values are ~1000x larger)
                if ts > 4_102_444_800:
                    ts = ts / 1000.0  # convert ms to seconds
                from datetime import datetime, timezone
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            pass
        # Try YYYYMMDD string (e.g. sailtodateday)
        s = str(value).strip()
        if len(s) == 8 and s.isdigit():
            try:
                from datetime import datetime
                return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
        # Fall back to standard date parsing
        from base_scraper import BaseScraper as _BS
        return _BS.parse_date(value)

    @staticmethod
    def _extract_voyage_id_from_uri(uri: str) -> str | None:
        """Extract voyage ID from Coveo URI query string (?id-journey=EX...)."""
        if not uri:
            return None
        import re
        m = re.search(r"[?&]id-journey=([^&]+)", uri)
        if m:
            return m.group(1)
        # Fall back to last path segment before ?
        parts = uri.split("?")[0].rstrip("/").split("/")
        if parts:
            return parts[-1]
        return None

    def _extract_coveo_pricing(self, raw_fields: dict) -> list[dict]:
        """
        Extract cabin category pricing from Coveo raw.* fields.

        Confirmed Coveo price field patterns:
        - priceperguest_doubleoccupancy_full
        - priceperguest_singleoccupancy_full
        - priceperguest_[category]_full
        """
        categories = []
        currency = self.safe_str(raw_fields.get("currency") or "USD").upper() or "USD"

        # Map known Coveo price field patterns to cabin category names
        price_field_map = [
            ("priceperguest_doubleoccupancy_full", "DOCC", "Double Occupancy"),
            ("priceperguest_singleoccupancy_full", "SING", "Single Occupancy"),
            ("priceperguest_insidestudio_full", "IS", "Interior Studio"),
            ("priceperguest_oceanviewstudio_full", "OS", "Ocean View Studio"),
            ("priceperguest_skysuite_full", "SKY", "Sky Suite"),
            ("priceperguest_oceansuite_full", "OCS", "Ocean Suite"),
            ("priceperguest_penthousesuite_full", "PS", "Penthouse Suite"),
            ("priceperguest_full", "BEST", "Best Available"),
            ("lowestprice", "BEST", "Best Available"),
        ]

        for field, code, name in price_field_map:
            # Try exact match and case-insensitive match
            price = self.safe_float(raw_fields.get(field) or raw_fields.get(field.lower()))
            if price is not None and price > 0:
                categories.append({
                    "category_code": code,
                    "category_name": name,
                    "price_per_person": price,
                    "currency": currency,
                    "availability": "available",
                })

        # Scan all raw fields for unrecognized price fields (fallback)
        if not categories:
            for key, val in raw_fields.items():
                if "price" in key.lower() and "discount" not in key.lower() and isinstance(val, (int, float)) and val > 0:
                    categories.append({
                        "category_code": key[:10].upper(),
                        "category_name": key.replace("_", " ").title(),
                        "price_per_person": float(val),
                        "currency": currency,
                        "availability": "available",
                    })
                    if len(categories) >= 5:
                        break

        if not categories:
            categories = [{
                "category_code": "N/A",
                "category_name": "Price on request",
                "price_per_person": None,
                "currency": currency,
                "availability": "unknown",
            }]

        return categories

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
