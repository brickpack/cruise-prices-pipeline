/**
 * charts.js — Price history charts using Chart.js
 *
 * Exports:
 *   initCharts(manifest)  — call once after data is loaded
 *
 * Listens for the 'voyage-selected' CustomEvent dispatched by filters.js
 * and renders a price history line chart for the selected voyage.
 */

import { state, escapeHtml, cruiseLineName } from './app.js';

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let _manifest = null;
let _chartInstance = null;

// Cache of loaded historical data files to avoid re-fetching
// Key: "YYYY-MM-DD/cruise_line", Value: array of voyage records
const _dataCache = new Map();

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initCharts(manifest) {
  _manifest = manifest;
  document.addEventListener('voyage-selected', e => {
    loadAndRenderChart(e.detail.voyageId);
  });
}

// ---------------------------------------------------------------------------
// Main chart renderer
// ---------------------------------------------------------------------------

async function loadAndRenderChart(voyageId) {
  const voyage = state.voyages.find(v => v.voyage_id === voyageId);
  if (!voyage) return;

  updateChartHeader(voyage);
  showChartLoading();

  try {
    const history = await buildPriceHistory(voyageId, voyage.cruise_line);
    if (history.length < 2) {
      showChartPlaceholder(
        history.length === 0
          ? 'No historical data available yet — check back after more scrapes have run.'
          : 'Only one data point so far — price history will appear once more scrapes accumulate.'
      );
      return;
    }

    renderChart(history, voyage);
  } catch (err) {
    console.error('Chart error:', err);
    showChartPlaceholder(`Failed to load price history: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

/**
 * Build a time-series of prices for a voyage by loading historical data files.
 *
 * @param {string} voyageId
 * @param {string} cruiseLine  e.g. "explora_journeys"
 * @returns {Array<{date: string, categories: Object<string, number|null>}>}
 */
async function buildPriceHistory(voyageId, cruiseLine) {
  const dates = getAvailableDates(cruiseLine);
  if (dates.length === 0) return [];

  const history = [];

  // Load each date's data file (with concurrency limit of 4)
  const chunkSize = 4;
  for (let i = 0; i < dates.length; i += chunkSize) {
    const chunk = dates.slice(i, i + chunkSize);
    const results = await Promise.all(
      chunk.map(date => loadDayData(date, cruiseLine))
    );

    results.forEach((dayVoyages, idx) => {
      const date = chunk[idx];
      const match = dayVoyages.find(v => v.voyage_id === voyageId);
      if (!match) return;

      // Build a {categoryCode: price} map for this date
      const categories = {};
      for (const cat of (match.cabin_categories ?? [])) {
        if (cat.price_per_person != null) {
          categories[cat.category_code] = cat.price_per_person;
        }
      }

      if (Object.keys(categories).length > 0) {
        history.push({ date, categories });
      }
    });
  }

  return history.sort((a, b) => a.date.localeCompare(b.date));
}

/**
 * Return the list of scrape dates that have data for a given cruise line,
 * newest-to-oldest, using the manifest.
 */
function getAvailableDates(cruiseLine) {
  if (!_manifest?.dates) {
    // If no manifest, fall back to scanning what we know from state
    return state.latest?.scrape_date ? [state.latest.scrape_date] : [];
  }

  return Object.entries(_manifest.dates)
    .filter(([, data]) => data[cruiseLine])
    .map(([date]) => date)
    .sort(); // chronological
}

/**
 * Load data/YYYY-MM-DD/{cruise_line}.json and return its voyages array.
 * Results are cached per date+line.
 */
async function loadDayData(date, cruiseLine) {
  const cacheKey = `${date}/${cruiseLine}`;
  if (_dataCache.has(cacheKey)) {
    return _dataCache.get(cacheKey);
  }

  const url = `../data/${date}/${cruiseLine}.json`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      _dataCache.set(cacheKey, []);
      return [];
    }
    const payload = await res.json();
    const voyages = payload?.voyages ?? (Array.isArray(payload) ? payload : []);
    _dataCache.set(cacheKey, voyages);
    return voyages;
  } catch {
    _dataCache.set(cacheKey, []);
    return [];
  }
}

// ---------------------------------------------------------------------------
// Chart rendering
// ---------------------------------------------------------------------------

function renderChart(history, voyage) {
  // Collect all cabin category codes that appear across all dates
  const allCodes = [...new Set(history.flatMap(h => Object.keys(h.categories)))];

  const labels = history.map(h => h.date);

  // Assign a color to each cabin category
  const palette = [
    '#c8a951', // gold
    '#64a8d8', // blue
    '#2ecc71', // green
    '#e74c3c', // red
    '#9b59b6', // purple
    '#e67e22', // orange
    '#1abc9c', // teal
  ];

  const datasets = allCodes.map((code, i) => ({
    label: code,
    data: history.map(h => h.categories[code] ?? null),
    borderColor: palette[i % palette.length],
    backgroundColor: palette[i % palette.length] + '22',
    borderWidth: 2,
    pointRadius: 4,
    pointHoverRadius: 6,
    tension: 0.3,
    spanGaps: true,
  }));

  showChartCanvas();

  // Destroy previous chart instance if any
  if (_chartInstance) {
    _chartInstance.destroy();
    _chartInstance = null;
  }

  const canvas = document.getElementById('price-chart');
  if (!canvas) return;

  _chartInstance = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          labels: {
            color: '#9aafbf',
            font: { size: 12 },
            padding: 16,
          },
        },
        tooltip: {
          backgroundColor: '#1a2e45',
          titleColor: '#e8e4dc',
          bodyColor: '#9aafbf',
          borderColor: 'rgba(200, 169, 81, 0.3)',
          borderWidth: 1,
          padding: 12,
          callbacks: {
            label(ctx) {
              const val = ctx.parsed.y;
              if (val == null) return `${ctx.dataset.label}: —`;
              return `${ctx.dataset.label}: $${val.toLocaleString()} pp`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#6a859a', font: { size: 11 } },
          grid:  { color: 'rgba(255,255,255,0.05)' },
        },
        y: {
          ticks: {
            color: '#6a859a',
            font: { size: 11 },
            callback: val => `$${val.toLocaleString()}`,
          },
          grid: { color: 'rgba(255,255,255,0.05)' },
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// UI state helpers
// ---------------------------------------------------------------------------

function updateChartHeader(voyage) {
  const nameEl = document.getElementById('chart-voyage-name');
  const metaEl = document.getElementById('chart-voyage-meta');
  if (nameEl) nameEl.textContent = voyage.voyage_name;
  if (metaEl) {
    metaEl.textContent = [
      cruiseLineName(voyage.cruise_line),
      voyage.ship_name,
      voyage.departure_port,
      voyage.departure_date,
      voyage.duration_nights ? `${voyage.duration_nights} nights` : null,
    ].filter(Boolean).join(' · ');
  }
}

function showChartLoading() {
  const placeholder = document.getElementById('chart-placeholder');
  const canvasWrap  = document.getElementById('chart-canvas-wrap');
  if (placeholder) { placeholder.style.display = 'flex'; placeholder.textContent = 'Loading price history…'; }
  if (canvasWrap)  canvasWrap.style.display = 'none';
}

function showChartPlaceholder(msg) {
  const placeholder = document.getElementById('chart-placeholder');
  const canvasWrap  = document.getElementById('chart-canvas-wrap');
  if (placeholder) { placeholder.style.display = 'flex'; placeholder.textContent = msg; }
  if (canvasWrap)  canvasWrap.style.display = 'none';
  if (_chartInstance) { _chartInstance.destroy(); _chartInstance = null; }
}

function showChartCanvas() {
  const placeholder = document.getElementById('chart-placeholder');
  const canvasWrap  = document.getElementById('chart-canvas-wrap');
  if (placeholder) placeholder.style.display = 'none';
  if (canvasWrap)  canvasWrap.style.display = 'block';
}
