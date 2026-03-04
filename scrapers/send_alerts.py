"""
send_alerts.py — Check price alert subscriptions and send email notifications.

Run after the daily scrape. Reads:
  - data/latest.json       — today's scrape results
  - data/alerts.json       — subscriber list (managed by Cloudflare Worker)

For each subscription, finds matching voyages that have dropped in price
since the subscriber was last notified, then sends a digest email via Resend.

Required environment variables:
  RESEND_API_KEY   — Resend API key (set as GitHub Actions secret)
  RESEND_FROM      — Sender address, e.g. "Cruise Alerts <alerts@yourdomain.com>"
  SITE_URL         — Frontend URL, e.g. "https://brickpack.github.io/cruise-prices-pipeline"
  WORKER_URL       — Cloudflare Worker base URL for unsubscribe links
"""

import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("send_alerts")

RESEND_API_URL = "https://api.resend.com/emails"
DATA_DIR       = Path(__file__).parent.parent / "data"
ALERTS_FILE    = DATA_DIR / "alerts.json"
LATEST_FILE    = DATA_DIR / "latest.json"


def load_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def matches_criteria(voyage: dict, criteria: dict) -> bool:
    """Return True if a voyage satisfies all filter criteria."""
    if criteria.get("cruise_line") and voyage.get("cruise_line") != criteria["cruise_line"]:
        return False
    if criteria.get("region") and voyage.get("region") != criteria["region"]:
        return False
    if criteria.get("departure_month"):
        dep = voyage.get("departure_date", "")
        if not dep.startswith(criteria["departure_month"]):
            return False
    if criteria.get("max_duration_nights"):
        if (voyage.get("duration_nights") or 0) > criteria["max_duration_nights"]:
            return False
    if criteria.get("max_price"):
        price = lowest_price(voyage)
        if price is not None and price > criteria["max_price"]:
            return False
    return True


def lowest_price(voyage: dict) -> float | None:
    prices = [
        c["price_per_person"]
        for c in voyage.get("cabin_categories", [])
        if c.get("price_per_person") is not None
    ]
    return min(prices) if prices else None


def lowest_original_price(voyage: dict) -> float | None:
    prices = [
        c["original_price"]
        for c in voyage.get("cabin_categories", [])
        if c.get("original_price") is not None
    ]
    return min(prices) if prices else None


def format_voyage_row(voyage: dict, site_url: str) -> str:
    """Format a single voyage as an HTML table row for the email."""
    price    = lowest_price(voyage)
    orig     = lowest_original_price(voyage)
    line     = "Explora Journeys" if voyage.get("cruise_line") == "explora_journeys" else "Oceania Cruises"
    dep      = voyage.get("departure_date", "—")
    nights   = voyage.get("duration_nights", "—")
    region   = voyage.get("region", "—")
    name     = voyage.get("voyage_name", "—")
    ship     = voyage.get("ship_name", "—")
    url      = voyage.get("voyage_url", site_url)

    price_str = f"${price:,.0f}" if price else "—"
    if orig and orig > (price or 0):
        pct = round((1 - price / orig) * 100)
        orig_str = f'<span style="text-decoration:line-through;color:#888">${orig:,.0f}</span> '
        badge    = f'<span style="color:#4ade80;font-weight:700"> −{pct}%</span>'
        price_display = f'{orig_str}{price_str}{badge}'
    else:
        price_display = price_str

    return f"""
    <tr>
      <td style="padding:10px 12px;border-bottom:1px solid #1e3a55">
        <a href="{url}" style="color:#c8a951;font-weight:600;text-decoration:none">{name}</a><br>
        <small style="color:#aaa">{ship} · {line}</small>
      </td>
      <td style="padding:10px 12px;border-bottom:1px solid #1e3a55;color:#aaa">{region}</td>
      <td style="padding:10px 12px;border-bottom:1px solid #1e3a55;color:#aaa">{dep}</td>
      <td style="padding:10px 12px;border-bottom:1px solid #1e3a55;color:#aaa;text-align:center">{nights}</td>
      <td style="padding:10px 12px;border-bottom:1px solid #1e3a55;font-family:monospace">{price_display}</td>
    </tr>"""


def build_email_html(voyages: list[dict], subscription: dict, site_url: str, worker_url: str) -> str:
    """Build the HTML email body."""
    rows = "\n".join(format_voyage_row(v, site_url) for v in voyages[:20])
    more = f"<p style='color:#aaa;font-size:0.85rem'>…and {len(voyages) - 20} more. <a href='{site_url}' style='color:#c8a951'>View all on the tracker.</a></p>" if len(voyages) > 20 else ""
    unsubscribe_url = f"{worker_url}/unsubscribe?token={subscription['id']}"

    criteria = subscription.get("criteria", {})
    criteria_parts = []
    if criteria.get("cruise_line"):
        criteria_parts.append(f"Line: {criteria['cruise_line'].replace('_', ' ').title()}")
    if criteria.get("region"):
        criteria_parts.append(f"Region: {criteria['region']}")
    if criteria.get("departure_month"):
        criteria_parts.append(f"Month: {criteria['departure_month']}")
    if criteria.get("max_price"):
        criteria_parts.append(f"Max price: ${criteria['max_price']:,.0f}")
    if criteria.get("max_duration_nights"):
        criteria_parts.append(f"Max duration: {criteria['max_duration_nights']} nights")
    criteria_str = " · ".join(criteria_parts) if criteria_parts else "All voyages"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1b2a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:680px;margin:0 auto;padding:32px 16px">

    <!-- Header -->
    <div style="text-align:center;margin-bottom:28px">
      <h1 style="color:#c8a951;font-size:1.4rem;margin:0 0 6px">🚢 Cruise Price Alerts</h1>
      <p style="color:#8ba0b8;font-size:0.85rem;margin:0">{len(voyages)} voyage{"s" if len(voyages) != 1 else ""} matching your criteria · {date.today().isoformat()}</p>
      <p style="color:#8ba0b8;font-size:0.8rem;margin:6px 0 0;font-style:italic">{criteria_str}</p>
    </div>

    <!-- Table -->
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#1a2e45;border-radius:8px;overflow:hidden">
      <thead>
        <tr style="background:#243d57">
          <th style="padding:10px 12px;text-align:left;color:#c8a951;font-size:0.75rem;font-weight:700;text-transform:uppercase">Voyage</th>
          <th style="padding:10px 12px;text-align:left;color:#c8a951;font-size:0.75rem;font-weight:700;text-transform:uppercase">Region</th>
          <th style="padding:10px 12px;text-align:left;color:#c8a951;font-size:0.75rem;font-weight:700;text-transform:uppercase">Departure</th>
          <th style="padding:10px 12px;text-align:center;color:#c8a951;font-size:0.75rem;font-weight:700;text-transform:uppercase">Nights</th>
          <th style="padding:10px 12px;text-align:left;color:#c8a951;font-size:0.75rem;font-weight:700;text-transform:uppercase">From (pp)</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>

    {more}

    <!-- CTA -->
    <div style="text-align:center;margin:28px 0">
      <a href="{site_url}" style="display:inline-block;background:#c8a951;color:#0d1b2a;font-weight:700;padding:12px 28px;border-radius:6px;text-decoration:none;font-size:0.9rem">
        View Full Price Tracker →
      </a>
    </div>

    <!-- Footer -->
    <div style="text-align:center;color:#4a6a8a;font-size:0.75rem;border-top:1px solid #1e3a55;padding-top:16px">
      Data scraped daily from public cruise line websites · Not affiliated with any cruise line<br>
      <a href="{unsubscribe_url}" style="color:#4a6a8a">Unsubscribe</a>
    </div>

  </div>
</body>
</html>"""


def send_email(to: str, subject: str, html: str, api_key: str, from_addr: str) -> bool:
    """Send an email via Resend API. Returns True on success."""
    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("Sent alert to %s", to)
            return True
        else:
            logger.warning("Resend error for %s: %s %s", to, resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("Failed to send to %s: %s", to, exc)
        return False


def main() -> int:
    api_key    = os.getenv("RESEND_API_KEY")
    from_addr  = os.getenv("RESEND_FROM", "Cruise Price Alerts <alerts@cruise-prices.dev>")
    site_url   = os.getenv("SITE_URL", "https://brickpack.github.io/cruise-prices-pipeline")
    worker_url = os.getenv("WORKER_URL", "https://cruise-alerts.YOUR_SUBDOMAIN.workers.dev")

    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping alert emails")
        return 0

    if not ALERTS_FILE.exists():
        logger.info("No alerts.json found — no subscriptions yet")
        return 0

    if not LATEST_FILE.exists():
        logger.error("data/latest.json not found")
        return 1

    alerts  = load_json(ALERTS_FILE)
    latest  = load_json(LATEST_FILE)
    voyages = latest.get("voyages", [])

    if not alerts:
        logger.info("No active subscriptions")
        return 0

    logger.info("Checking %d subscriptions against %d voyages", len(alerts), len(voyages))

    sent_count = 0
    updated    = False

    for sub in alerts:
        criteria = sub.get("criteria", {})
        email    = sub.get("email")
        if not email:
            continue

        matching = [v for v in voyages if matches_criteria(v, criteria)]
        if not matching:
            logger.debug("No matches for %s", email)
            continue

        logger.info("%d matches for %s", len(matching), email)

        subject = f"🚢 {len(matching)} cruise voyage{'s' if len(matching) != 1 else ''} matching your alert"
        html    = build_email_html(matching, sub, site_url, worker_url)

        if send_email(email, subject, html, api_key, from_addr):
            sub["last_notified"] = datetime.utcnow().isoformat() + "Z"
            updated = True
            sent_count += 1

    if updated:
        save_json(ALERTS_FILE, alerts)
        logger.info("Updated last_notified timestamps in alerts.json")

    logger.info("Alert run complete — sent %d emails", sent_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
