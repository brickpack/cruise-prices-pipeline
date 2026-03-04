/**
 * filters.js — Price table with filtering, sorting, and row selection
 *
 * Exports:
 *   initFilters(voyages)  — call once after data is loaded
 *   renderTable()         — re-render with current filter/sort state
 */

import { lowestPrice, cruiseLineName, formatMonthLabel, escapeHtml, activateTab, state } from './app.js';

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let _allVoyages = [];

const _sort = { column: 'departure_date', direction: 'asc' };

const _filters = {
  line: '',
  region: '',
  month: '',
  maxDuration: null,
  maxPrice: null,
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initFilters(voyages) {
  _allVoyages = voyages;
  populateFilterDropdowns(voyages);
  attachFilterListeners();
  attachSortListeners();
  renderTable();
}

// ---------------------------------------------------------------------------
// Filter dropdown population
// ---------------------------------------------------------------------------

function populateFilterDropdowns(voyages) {
  const regions = [...new Set(voyages.map(v => v.region).filter(Boolean))].sort();
  const months  = [...new Set(voyages.map(v => v.departure_date?.slice(0, 7)).filter(Boolean))].sort();

  const regionSel = document.getElementById('filter-region');
  const monthSel  = document.getElementById('filter-month');

  // Populate comparison panel dropdowns too (they share the same data sets)
  const compRegionSel = document.getElementById('comp-region');
  const compMonthSel  = document.getElementById('comp-month');

  regions.forEach(r => {
    appendOption(regionSel, r, r);
    if (compRegionSel) appendOption(compRegionSel, r, r);
  });

  months.forEach(m => {
    const label = formatMonthLabel(m);
    appendOption(monthSel, m, label);
    if (compMonthSel) appendOption(compMonthSel, m, label);
  });
}

function appendOption(selectEl, value, label) {
  if (!selectEl) return;
  const opt = document.createElement('option');
  opt.value = value;
  opt.textContent = label;
  selectEl.appendChild(opt);
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------

function attachFilterListeners() {
  document.getElementById('filter-line')?.addEventListener('change', e => {
    _filters.line = e.target.value;
    renderTable();
  });
  document.getElementById('filter-region')?.addEventListener('change', e => {
    _filters.region = e.target.value;
    renderTable();
  });
  document.getElementById('filter-month')?.addEventListener('change', e => {
    _filters.month = e.target.value;
    renderTable();
  });
  document.getElementById('filter-duration')?.addEventListener('change', e => {
    _filters.maxDuration = e.target.value ? parseInt(e.target.value) : null;
    renderTable();
  });
  document.getElementById('filter-max-price')?.addEventListener('input', e => {
    const val = parseFloat(e.target.value);
    _filters.maxPrice = isNaN(val) ? null : val;
    renderTable();
  });
  document.getElementById('filter-reset')?.addEventListener('click', resetFilters);
}

function attachSortListeners() {
  document.querySelectorAll('.price-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      if (_sort.column === col) {
        _sort.direction = _sort.direction === 'asc' ? 'desc' : 'asc';
      } else {
        _sort.column = col;
        _sort.direction = 'asc';
      }
      updateSortIndicators();
      renderTable();
    });
  });
}

function updateSortIndicators() {
  document.querySelectorAll('.price-table th[data-sort]').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.sort === _sort.column) {
      th.classList.add(_sort.direction === 'asc' ? 'sorted-asc' : 'sorted-desc');
    }
  });
}

function resetFilters() {
  _filters.line = '';
  _filters.region = '';
  _filters.month = '';
  _filters.maxDuration = null;
  _filters.maxPrice = null;

  document.getElementById('filter-line').value      = '';
  document.getElementById('filter-region').value    = '';
  document.getElementById('filter-month').value     = '';
  document.getElementById('filter-duration').value  = '';
  document.getElementById('filter-max-price').value = '';

  renderTable();
}

// ---------------------------------------------------------------------------
// Main render
// ---------------------------------------------------------------------------

export function renderTable() {
  const filtered = applyFilters(_allVoyages);
  const sorted   = applySort(filtered);

  const tbody    = document.getElementById('price-table-body');
  const noResults = document.getElementById('no-results');
  const countEl  = document.getElementById('result-count');

  if (!tbody) return;

  countEl.textContent = `Showing ${sorted.length} of ${_allVoyages.length} voyages`;

  if (sorted.length === 0) {
    tbody.innerHTML = '';
    noResults.style.display = 'block';
    return;
  }

  noResults.style.display = 'none';
  tbody.innerHTML = sorted.map(voyage => buildRow(voyage)).join('');

  // Row click → select voyage for chart
  tbody.querySelectorAll('tr[data-voyage-id]').forEach(row => {
    row.addEventListener('click', () => {
      const voyageId = row.dataset.voyageId;
      selectVoyage(voyageId, row);
    });
  });

  // Re-highlight previously selected row if still visible
  if (state.selectedVoyageId) {
    const sel = tbody.querySelector(`tr[data-voyage-id="${CSS.escape(state.selectedVoyageId)}"]`);
    if (sel) sel.classList.add('selected');
  }
}

// ---------------------------------------------------------------------------
// Filtering & sorting logic
// ---------------------------------------------------------------------------

function applyFilters(voyages) {
  return voyages.filter(v => {
    if (_filters.line && v.cruise_line !== _filters.line) return false;
    if (_filters.region && v.region !== _filters.region) return false;
    if (_filters.month && !(v.departure_date?.startsWith(_filters.month))) return false;
    if (_filters.maxDuration != null && v.duration_nights > _filters.maxDuration) return false;
    if (_filters.maxPrice != null) {
      const price = lowestPrice(v);
      if (price != null && price > _filters.maxPrice) return false;
    }
    return true;
  });
}

function applySort(voyages) {
  const col = _sort.column;
  const dir = _sort.direction === 'asc' ? 1 : -1;

  return [...voyages].sort((a, b) => {
    let va, vb;

    if (col === 'price') {
      va = lowestPrice(a) ?? Infinity;
      vb = lowestPrice(b) ?? Infinity;
    } else if (col === 'duration_nights') {
      va = a.duration_nights ?? 0;
      vb = b.duration_nights ?? 0;
    } else {
      va = (a[col] ?? '').toString().toLowerCase();
      vb = (b[col] ?? '').toString().toLowerCase();
    }

    if (va < vb) return -1 * dir;
    if (va > vb) return  1 * dir;
    return 0;
  });
}

// ---------------------------------------------------------------------------
// Row HTML builder
// ---------------------------------------------------------------------------

function buildRow(voyage) {
  const lineBadge = voyage.cruise_line === 'explora_journeys'
    ? '<span class="badge-line badge-explora">Explora</span>'
    : '<span class="badge-line badge-oceania">Oceania</span>';

  const price     = lowestPrice(voyage);
  const origPrice = lowestOriginalPrice(voyage);
  const prevPrice = voyage._prev_price ?? null;

  let priceHtml = '<span class="price-cell" style="color:var(--text-muted)">—</span>';
  if (price != null) {
    // Discount badge — show when we have a higher original price
    let discountHtml = '';
    if (origPrice != null && origPrice > price) {
      const pct = Math.round((1 - price / origPrice) * 100);
      discountHtml = `
        <span class="orig-price">$${origPrice.toLocaleString()}</span>
        <span class="discount-badge">−${pct}%</span>`;
    }

    // Day-over-day price change delta
    let deltaHtml = '';
    if (prevPrice != null && prevPrice !== price) {
      const delta = price - prevPrice;
      if (delta < 0) {
        deltaHtml = `<span class="price-delta delta-down">▼ $${Math.abs(delta).toLocaleString()}</span>`;
      } else {
        deltaHtml = `<span class="price-delta delta-up">▲ $${delta.toLocaleString()}</span>`;
      }
    }

    priceHtml = `
      <span class="price-cell">$${price.toLocaleString()}</span>
      ${discountHtml}${deltaHtml}`;
  }

  // Best availability across cabin categories
  const avail = bestAvailability(voyage.cabin_categories ?? []);
  const availHtml = `<span class="avail-badge avail-${avail}">${availabilityLabel(avail)}</span>`;

  const voyageLink = voyage.voyage_url
    ? `<a href="${escapeHtml(voyage.voyage_url)}" target="_blank" rel="noopener" title="View on cruise line website" onclick="event.stopPropagation()">${escapeHtml(voyage.voyage_name)}</a>`
    : escapeHtml(voyage.voyage_name);

  return `
    <tr data-voyage-id="${escapeHtml(voyage.voyage_id)}" tabindex="0" role="button" aria-label="View price history for ${escapeHtml(voyage.voyage_name)}">
      <td>${lineBadge}</td>
      <td>${voyageLink}</td>
      <td>${escapeHtml(voyage.ship_name || '—')}</td>
      <td>${escapeHtml(voyage.region || '—')}</td>
      <td>${voyage.departure_date || '—'}</td>
      <td>${voyage.duration_nights ?? '—'}</td>
      <td class="price-col">${priceHtml}</td>
      <td>${availHtml}</td>
    </tr>
  `;
}

/** Return the lowest original (pre-discount) price across all cabin categories, or null. */
function lowestOriginalPrice(voyage) {
  const prices = (voyage.cabin_categories ?? [])
    .map(c => c.original_price)
    .filter(p => p != null && p > 0);
  return prices.length ? Math.min(...prices) : null;
}

// ---------------------------------------------------------------------------
// Row selection → trigger chart
// ---------------------------------------------------------------------------

function selectVoyage(voyageId, clickedRow) {
  // Deselect previous
  document.querySelectorAll('.price-table tbody tr.selected').forEach(r => r.classList.remove('selected'));
  clickedRow.classList.add('selected');

  state.selectedVoyageId = voyageId;

  // Switch to history tab
  activateTab('history');

  // Fire event for charts.js to pick up
  document.dispatchEvent(new CustomEvent('voyage-selected', { detail: { voyageId } }));
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function bestAvailability(cabinCategories) {
  const order = ['available', 'waitlist', 'sold_out', 'unknown'];
  const statuses = cabinCategories.map(c => c.availability ?? 'unknown');
  for (const status of order) {
    if (statuses.includes(status)) return status;
  }
  return 'unknown';
}

function availabilityLabel(status) {
  return {
    available: 'Available',
    waitlist:  'Waitlist',
    sold_out:  'Sold Out',
    unknown:   'Check Site',
  }[status] ?? status;
}
