# Cruise Price Tracker

A data engineering pipeline that scrapes daily cruise prices from **Explora Journeys** and **Oceania Cruises**, stores historical data as JSON files in this repo, and serves a price-tracking dashboard via **GitHub Pages**.

No paid infrastructure — GitHub Actions (free tier) for scheduling, GitHub Pages for hosting.

---

## Live Dashboard

**https://brickpack.github.io/cruise-prices-pipeline/**

---

## Architecture

```
/
├── .github/workflows/scrape.yml   # Daily cron at 6 AM UTC
├── scrapers/
│   ├── base_scraper.py            # Shared Playwright + schema validation logic
│   ├── explora_scraper.py         # Explora Journeys scraper (Coveo API)
│   ├── oceania_scraper.py         # Oceania Cruises scraper (NCLH API)
│   ├── run_all.py                 # Orchestrator
│   ├── send_alerts.py             # Email alert sender (Resend API)
│   └── requirements.txt
├── cloudflare-worker/
│   ├── worker.js                  # Subscription endpoint (POST /subscribe, GET /unsubscribe)
│   └── wrangler.toml              # Cloudflare Worker deployment config
├── data/
│   ├── latest.json                # Most recent combined data (also copied to docs/data/)
│   ├── manifest.json              # Index of available historical files
│   ├── alerts.json                # Email alert subscriptions (managed by Worker)
│   ├── schema.json                # JSON Schema for voyage records
│   └── YYYY-MM-DD/
│       ├── explora_journeys.json
│       └── oceania_cruises.json
└── docs/                          # GitHub Pages root
    ├── index.html
    ├── css/styles.css
    ├── data/                      # Copy of latest.json + manifest.json (served by Pages)
    └── js/
        ├── app.js
        ├── filters.js
        ├── charts.js
        └── alerts.js              # Alert signup form logic
```

---

## Site Assessment (Phase 0)

| Cruise Line | Listing URL | Rendering | Strategy |
|---|---|---|---|
| Explora Journeys | `/us/en/find-your-journey` | JS-rendered | Playwright + network interception |
| Oceania Cruises | `/cruise-finder` | JS-rendered | Playwright + network interception |

Both sites are fully JS-rendered. The scrapers use **Playwright network response interception** — more reliable than DOM scraping because it captures the raw API JSON rather than HTML elements that break when CSS classes change.

**Important:** Explora Journeys' booking subdomain (`booking.explorajourneys.com/touchb2c/`) uses Flutter/CanvasKit (canvas-only rendering, no DOM). The scrapers target the main marketing site only.

---

## Setup

### Local development

```bash
# 1. Install Python dependencies
pip install -r scrapers/requirements.txt

# 2. Install Playwright + Chromium
playwright install chromium --with-deps

# 3. Run scrapers
python scrapers/run_all.py

# 4. Check output
ls data/$(date +%Y-%m-%d)/
cat data/latest.json | python -m json.tool | head -50

# 5. Preview frontend
python -m http.server 8080 --directory docs
# Then open http://localhost:8080/
```

### Debugging scrapers

Run with debug logging to see all intercepted network responses:

```bash
EXPLORA_DEBUG=1 python -c "
import asyncio, sys
sys.path.insert(0, 'scrapers')
from explora_scraper import ExploraJourneysScraper
records = ExploraJourneysScraper().run()
print(f'{len(records)} records')
"

OCEANIA_DEBUG=1 python -c "
import asyncio, sys
sys.path.insert(0, 'scrapers')
from oceania_scraper import OceaniaCruisesScraper
records = OceaniaCruisesScraper().run()
print(f'{len(records)} records')
"
```

If a scraper returns 0 records, the API endpoint URLs may have changed. Run the debug command and look for JSON responses that contain voyage/pricing data — then update the `API_URL_PATTERNS` list in the relevant scraper file.

---

## GitHub Actions Setup

The workflow (`.github/workflows/scrape.yml`) runs automatically at 6 AM UTC daily.

**Required setup:**
1. Push this repo to GitHub
2. Go to **Settings → Actions → General → Workflow permissions** → enable "Read and write permissions"
3. Go to **Settings → Pages → Source** → set to "Deploy from a branch" → Branch: `main`, Folder: `/docs`

The workflow uses the built-in `GITHUB_TOKEN` — no personal access token needed.

**Manual trigger:** Go to Actions → "Daily Cruise Price Scrape" → "Run workflow".

---

## Price Alert Setup (optional)

Users can subscribe to email alerts via the **🔔 Alerts** tab on the dashboard. Alerts are sent daily after the scrape if matching voyages exist.

### 1. Deploy the Cloudflare Worker

The Worker receives form submissions and writes subscriptions to `data/alerts.json` via the GitHub API.

```bash
cd cloudflare-worker
npx wrangler deploy

# Set the required secrets:
npx wrangler secret put GITHUB_TOKEN    # Fine-grained PAT: contents:write on this repo
npx wrangler secret put GITHUB_REPO    # Value: brickpack/cruise-prices-pipeline
npx wrangler secret put ALLOWED_ORIGIN # Value: https://brickpack.github.io
```

### 2. Update the Worker URL in the frontend

Edit `docs/js/alerts.js` and replace `YOUR_SUBDOMAIN` in `ALERTS_ENDPOINT` with your actual Cloudflare Worker subdomain (shown after `wrangler deploy`).

### 3. Add GitHub Actions secrets

In **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `RESEND_API_KEY` | API key from [resend.com](https://resend.com) (free: 3,000 emails/month) |
| `RESEND_FROM` | Sender address, e.g. `Cruise Alerts <alerts@yourdomain.com>` |
| `WORKER_URL` | Your Cloudflare Worker base URL (for unsubscribe links in emails) |

The `RESEND_API_KEY` secret is optional — if absent, the alert step is skipped silently and the rest of the scrape workflow runs normally.

---

## Data Schema

All voyage records (both cruise lines) share the same normalized schema defined in `data/schema.json`:

```json
{
  "scrape_date": "2025-03-01",
  "scrape_timestamp": "2025-03-01T06:12:34+00:00",
  "cruise_line": "explora_journeys",
  "voyage_id": "EP20250811BCNCV1",
  "voyage_name": "Mediterranean Odyssey",
  "ship_name": "EXPLORA I",
  "departure_port": "Barcelona",
  "departure_date": "2025-08-11",
  "return_date": "2025-08-25",
  "duration_nights": 14,
  "region": "Mediterranean",
  "cabin_categories": [
    {
      "category_code": "IS",
      "category_name": "Interior Studio",
      "price_per_person": 4299.00,
      "currency": "USD",
      "availability": "available"
    }
  ],
  "voyage_url": "https://www.explorajourneys.com/..."
}
```

---

## Adding a New Cruise Line

1. Create `scrapers/new_line_scraper.py` extending `BaseScraper`
2. Set `cruise_line = "new_line"` (add to `data/schema.json` enum)
3. Implement `scrape()` and `normalize()`
4. Import and add to the `SCRAPERS` list in `run_all.py`

---

## Notes on Anti-Bot Measures

- **Explora Journeys** blocks 500+ bot user-agents in `robots.txt`. The scraper uses a real Chrome user-agent string.
- **Oceania Cruises** specifies `Crawl-delay: 10` in `robots.txt`. The scraper honors this with a 10-second delay.
- Both scrapers use `headless=True` with `--disable-blink-features=AutomationControlled` to reduce detection signals.
- Scrapers run once per day — a respectful crawl rate for public listing pages.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 0 records from a scraper | API endpoint URL changed | Run with `EXPLORA_DEBUG=1` / `OCEANIA_DEBUG=1` to see all intercepted responses; update `API_URL_PATTERNS` |
| Playwright timeout | Slow page load or bot blocking | Increase `timeout_ms` in scraper; check if site is returning 403 |
| Schema validation errors | API response format changed | Check logged warnings; update `normalize()` method |
| Frontend shows no data | `data/latest.json` not found | Run scrapers first; check GitHub Pages is serving from `/docs` |
| Chart shows no history | Not enough scrapes yet | Charts need 2+ data points; wait for daily runs to accumulate |
