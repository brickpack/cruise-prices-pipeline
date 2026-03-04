/**
 * app.js — Main application entry point
 *
 * Responsibilities:
 * - Load data/latest.json and data/manifest.json
 * - Initialize the tab navigation
 * - Render the Dashboard panel (stats + recent changes)
 * - Expose shared state (voyages, manifest) to other modules
 * - Bootstrap filters.js and charts.js
 */

import { initFilters, renderTable, applyFilterAndShow } from './filters.js';
import { initCharts } from './charts.js';
import { initAlerts } from './alerts.js';

// ---------------------------------------------------------------------------
// Data paths — derived from this script's URL so the base path is always correct
// whether served from / (local) or /cruise-prices-pipeline/ (GitHub Pages)
// ---------------------------------------------------------------------------

const DATA_BASE = new URL('../data', import.meta.url).href;
const LATEST_URL = `${DATA_BASE}/latest.json`;
const MANIFEST_URL = `${DATA_BASE}/manifest.json`;

// ---------------------------------------------------------------------------
// Shared application state
// ---------------------------------------------------------------------------

export const state = {
  /** @type {Object|null} Full latest.json payload */
  latest: null,
  /** @type {Array<Object>} All voyage records */
  voyages: [],
  /** @type {Object|null} manifest.json payload */
  manifest: null,
  /** @type {string|null} Currently selected voyage_id (for chart) */
  selectedVoyageId: null,
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  initTabs();
  await loadData();
});

async function loadData() {
  const loading = document.getElementById('loading');
  const errorBanner = document.getElementById('error-banner');

  try {
    // Fetch in parallel
    const [latestRes, manifestRes] = await Promise.all([
      fetch(LATEST_URL),
      fetch(MANIFEST_URL).catch(() => null), // manifest is optional
    ]);

    if (!latestRes.ok) {
      throw new Error(`Failed to load cruise data (HTTP ${latestRes.status}). Have the scrapers run yet?`);
    }

    state.latest = await latestRes.json();
    state.voyages = state.latest?.voyages ?? [];

    if (manifestRes?.ok) {
      state.manifest = await manifestRes.json();
    }

    loading.style.display = 'none';
    showPanels();

    renderDashboard();
    initFilters(state.voyages);
    initCharts(state.manifest);
    initComparison();
    initAlerts();

    // Update header
    const ts = state.latest?.generated_at;
    if (ts) {
      document.getElementById('last-updated').textContent = formatDateTime(ts);
    }

    // Status badge
    const failures = state.latest?.failures ?? [];
    const statusEl = document.getElementById('data-status');
    if (failures.length > 0) {
      statusEl.className = 'status-badge status-warn';
      statusEl.innerHTML = '<span class="status-dot"></span> Partial';
      statusEl.title = `Failed scrapers: ${failures.join(', ')}`;
    }

  } catch (err) {
    loading.style.display = 'none';
    errorBanner.style.display = 'block';
    errorBanner.textContent = `Error: ${err.message}`;
    document.getElementById('data-status').className = 'status-badge status-err';
    document.getElementById('data-status').innerHTML = '<span class="status-dot"></span> Error';
    console.error('Failed to load cruise data:', err);
  }
}

function showPanels() {
  // Show whichever panel is currently active
  const activeTab = document.querySelector('.nav-tab.active');
  if (activeTab) {
    const panelId = `panel-${activeTab.dataset.tab}`;
    const panel = document.getElementById(panelId);
    if (panel) panel.style.display = 'block';
  }
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

function initTabs() {
  const tabs = document.querySelectorAll('.nav-tab[data-tab]');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => activateTab(tab.dataset.tab));
  });
}

export function activateTab(tabName) {
  // Update tab buttons
  document.querySelectorAll('.nav-tab[data-tab]').forEach(t => {
    const isActive = t.dataset.tab === tabName;
    t.classList.toggle('active', isActive);
    t.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });

  // Show/hide panels
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.style.display = 'none';
    p.classList.remove('active');
  });

  const panel = document.getElementById(`panel-${tabName}`);
  if (panel) {
    panel.style.display = 'block';
    panel.classList.add('active');
  }
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

function renderDashboard() {
  renderStats();
  renderRecentChanges();
}

function renderStats() {
  const grid = document.getElementById('stats-grid');
  if (!grid) return;

  const counts = state.latest?.record_counts ?? {};
  const total = state.latest?.total_records ?? state.voyages.length;
  const scrapeDate = state.latest?.scrape_date ?? '—';

  const exploraCount = counts['explora_journeys'] ?? state.voyages.filter(v => v.cruise_line === 'explora_journeys').length;
  const oceaniaCount = counts['oceania_cruises'] ?? state.voyages.filter(v => v.cruise_line === 'oceania_cruises').length;

  // Compute lowest price across all voyages
  const lowestPrice = computeLowestPrice(state.voyages);
  const priceDrops = countPriceDrops(state.voyages);

  const cards = [
    { label: 'Total Voyages',    value: total,                                              sub: `as of ${scrapeDate}`,   filter: {} },
    { label: 'Explora Journeys', value: exploraCount,                                       sub: 'voyages tracked',        filter: { line: 'explora_journeys' } },
    { label: 'Oceania Cruises',  value: oceaniaCount,                                       sub: 'voyages tracked',        filter: { line: 'oceania_cruises' } },
    { label: 'From Price (pp)',  value: lowestPrice ? `$${lowestPrice.toLocaleString()}` : '—', sub: 'lowest available',  filter: {} },
    { label: 'Discounted',       value: countDiscounted(state.voyages),                     sub: 'voyages on sale',        filter: { onlyDiscounted: true } },
  ];

  grid.innerHTML = cards.map(c => `
    <div class="stat-card stat-card-link" role="button" tabindex="0"
         data-filter='${JSON.stringify(c.filter)}'
         title="View in Price Table">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
    </div>
  `).join('');

  // Wire up click handlers
  grid.querySelectorAll('.stat-card-link').forEach(card => {
    const handler = () => {
      const filter = JSON.parse(card.dataset.filter);
      applyFilterAndShow(filter);
      activateTab('prices');
    };
    card.addEventListener('click', handler);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); } });
  });
}

function renderRecentChanges() {
  const container = document.getElementById('recent-changes');
  if (!container) return;

  const changes = computePriceChanges(state.voyages);

  if (changes.length === 0) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:0.875rem">Price change data will appear after multiple scrapes have accumulated.</p>';
    return;
  }

  // Show top 8 changes (mix of up and down)
  const displayed = changes.slice(0, 8);

  container.innerHTML = displayed.map(c => {
    const direction = c.delta < 0 ? 'price-down' : 'price-up';
    const sign = c.delta < 0 ? '▼' : '▲';
    const delta = Math.abs(c.delta);
    return `
      <div class="change-card ${direction}">
        <div class="change-delta">${sign} $${delta.toLocaleString()}</div>
        <div class="change-info">
          <div class="change-name" title="${c.voyage_name}">${c.voyage_name}</div>
          <div class="change-meta">${cruiseLineName(c.cruise_line)} · ${c.departure_date}</div>
        </div>
      </div>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Comparison view
// ---------------------------------------------------------------------------

function initComparison() {
  const regionSel = document.getElementById('comp-region');
  const monthSel = document.getElementById('comp-month');
  const durationSel = document.getElementById('comp-duration');

  // Populate region dropdown from canonical regions (shared across both lines)
  const regions = [...new Set(state.voyages.map(v => v.region_canonical).filter(Boolean))].sort();
  const months = [...new Set(state.voyages.map(v => v.departure_date?.slice(0, 7)).filter(Boolean))].sort();

  regions.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r; opt.textContent = r;
    regionSel?.appendChild(opt);
  });

  months.forEach(m => {
    const label = formatMonthLabel(m);
    const opt = document.createElement('option');
    opt.value = m; opt.textContent = label;
    monthSel?.appendChild(opt);
  });

  const refresh = () => renderComparison();
  [regionSel, monthSel, durationSel].forEach(el => el.addEventListener('change', refresh));

  renderComparison();
}

function renderComparison() {
  const region = document.getElementById('comp-region')?.value ?? '';
  const month = document.getElementById('comp-month')?.value ?? '';
  const maxDuration = parseInt(document.getElementById('comp-duration')?.value ?? '') || Infinity;

  let voyages = state.voyages.filter(v => {
    if (region && v.region_canonical !== region) return false;
    if (month && !(v.departure_date?.startsWith(month))) return false;
    if (v.duration_nights > maxDuration) return false;
    return true;
  });

  const explora = voyages.filter(v => v.cruise_line === 'explora_journeys');
  const oceania = voyages.filter(v => v.cruise_line === 'oceania_cruises');

  renderComparisonColumn('comparison-explora', explora, 'explora_journeys');
  renderComparisonColumn('comparison-oceania', oceania, 'oceania_cruises');
}

function renderComparisonColumn(containerId, voyages, line) {
  const el = document.getElementById(containerId);
  if (!el) return;

  if (voyages.length === 0) {
    el.innerHTML = '<div class="no-results" style="padding:1.5rem">No voyages match the selected filters.</div>';
    return;
  }

  el.innerHTML = voyages.slice(0, 20).map(v => {
    const price = lowestPrice(v);
    return `
      <div class="comparison-voyage">
        <div class="comparison-voyage-name">${escapeHtml(v.voyage_name)}</div>
        <div class="comparison-voyage-meta">
          ${escapeHtml(v.ship_name || '—')} &middot;
          ${escapeHtml(v.departure_port || '—')} &middot;
          ${v.departure_date || '—'} &middot;
          ${v.duration_nights}N
        </div>
        <div class="comparison-price">${price != null ? `$${price.toLocaleString()}` : 'Price on request'}</div>
      </div>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/**
 * Return the lowest non-null price_per_person across all cabin_categories.
 */
export function lowestPrice(voyage) {
  if (!voyage?.cabin_categories?.length) return null;
  const prices = voyage.cabin_categories
    .map(c => c.price_per_person)
    .filter(p => p != null && p > 0);
  return prices.length ? Math.min(...prices) : null;
}

function computeLowestPrice(voyages) {
  const prices = voyages.map(lowestPrice).filter(p => p != null);
  return prices.length ? Math.min(...prices) : null;
}

/**
 * Price changes are computed by comparing the current voyage prices to
 * what they were in the previous scrape. Since we don't have previous data
 * on first run, we look for a `_prev_price` field that run_all.py could inject.
 * For now this returns an empty array until historical data accumulates.
 */
function computePriceChanges(voyages) {
  return voyages
    .filter(v => v._prev_price != null && v._prev_price !== lowestPrice(v))
    .map(v => ({
      voyage_id: v.voyage_id,
      voyage_name: v.voyage_name,
      cruise_line: v.cruise_line,
      departure_date: v.departure_date,
      delta: (lowestPrice(v) ?? 0) - v._prev_price,
    }))
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
}

function countPriceDrops(voyages) {
  return voyages.filter(v => {
    const curr = lowestPrice(v);
    return curr != null && v._prev_price != null && curr < v._prev_price;
  }).length;
}

function countDiscounted(voyages) {
  return voyages.filter(v => {
    const price = lowestPrice(v);
    const orig  = (v.cabin_categories ?? []).map(c => c.original_price).filter(p => p != null && p > 0);
    const minOrig = orig.length ? Math.min(...orig) : null;
    return price != null && minOrig != null && minOrig > price;
  }).length;
}

export function cruiseLineName(id) {
  return {
    explora_journeys: 'Explora Journeys',
    oceania_cruises: 'Oceania Cruises',
  }[id] ?? id;
}

export function formatDateTime(isoString) {
  try {
    return new Date(isoString).toLocaleString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
    });
  } catch {
    return isoString;
  }
}

export function formatMonthLabel(ym) {
  // ym = "2025-03"
  try {
    const [y, m] = ym.split('-');
    return new Date(parseInt(y), parseInt(m) - 1).toLocaleString('en-US', { month: 'long', year: 'numeric' });
  } catch {
    return ym;
  }
}

export function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
