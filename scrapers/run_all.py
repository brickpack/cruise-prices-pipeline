"""
Orchestrator for all cruise price scrapers.

Usage:
    python scrapers/run_all.py

Runs each scraper sequentially, writes per-date JSON files, updates
data/latest.json and data/manifest.json.

Exit codes:
    0 — all scrapers succeeded
    1 — one or more scrapers failed (partial data still written)
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from this directory when running as a script
sys.path.insert(0, str(Path(__file__).parent))

from explora_scraper import ExploraJourneysScraper
from oceania_scraper import OceaniaCruisesScraper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("run_all")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
MANIFEST_PATH = DATA_DIR / "manifest.json"

# ---------------------------------------------------------------------------
# Scrapers to run (add new scrapers here)
# ---------------------------------------------------------------------------

SCRAPERS = [
    ExploraJourneysScraper,
    OceaniaCruisesScraper,
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    run_start = datetime.now(timezone.utc)
    date_str = run_start.strftime("%Y-%m-%d")
    logger.info("=== Cruise price scrape run: %s ===", date_str)

    results: dict[str, list[dict]] = {}
    failures: list[str] = []

    for ScraperClass in SCRAPERS:
        scraper = ScraperClass()
        name = scraper.cruise_line
        logger.info("--- Running %s ---", name)
        try:
            records = scraper.run()
            if records:
                out_path = scraper.write_output(records, date_str=date_str)
                logger.info("%s: wrote %d records to %s", name, len(records), out_path)
            else:
                logger.warning("%s: 0 records returned — no file written", name)
            results[name] = records
        except Exception as exc:
            logger.error("%s: scraper raised an exception: %s", name, exc)
            traceback.print_exc()
            failures.append(name)
            results[name] = []

    # --- Update data/latest.json ---
    all_records: list[dict] = []
    for name, records in results.items():
        all_records.extend(records)

    latest_payload = {
        "generated_at": run_start.isoformat(),
        "scrape_date": date_str,
        "cruise_lines": list(results.keys()),
        "record_counts": {name: len(recs) for name, recs in results.items()},
        "total_records": len(all_records),
        "failures": failures,
        "voyages": all_records,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(latest_payload, fh, indent=2, ensure_ascii=False)
    logger.info("Updated %s (%d total records)", LATEST_PATH, len(all_records))

    # --- Update data/manifest.json ---
    _update_manifest(date_str, results)

    # --- Summary ---
    logger.info("=== Run complete ===")
    logger.info("  Date:    %s", date_str)
    logger.info("  Success: %s", [n for n in results if n not in failures])
    logger.info("  Failed:  %s", failures)
    logger.info("  Total:   %d records", len(all_records))

    return 1 if failures else 0


def _update_manifest(date_str: str, results: dict[str, list[dict]]) -> None:
    """
    Maintain data/manifest.json — an index of all available scrape dates
    and which cruise lines have data for each date.

    The frontend uses this to build historical price charts without having
    to enumerate the filesystem.
    """
    manifest: dict = {}
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as exc:
            logger.warning("Could not read existing manifest: %s", exc)
            manifest = {}

    dates: dict = manifest.get("dates", {})
    entry = dates.get(date_str, {})

    for name, records in results.items():
        if records:
            entry[name] = {
                "record_count": len(records),
                "file": f"{date_str}/{name}.json",
            }

    dates[date_str] = entry
    manifest["dates"] = dict(sorted(dates.items(), reverse=True))  # newest first
    manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
    manifest["cruise_lines"] = list({
        line
        for day_data in manifest["dates"].values()
        for line in day_data.keys()
    })

    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    logger.info("Updated %s", MANIFEST_PATH)


if __name__ == "__main__":
    sys.exit(main())
