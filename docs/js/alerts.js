/**
 * alerts.js — Email alert signup
 *
 * POSTs subscription criteria to a Cloudflare Worker endpoint which
 * writes the subscription to data/alerts.json via the GitHub API.
 * The daily scrape workflow then checks alerts and sends emails via Resend.
 */

import { state, formatMonthLabel } from './app.js';

// Set this to your deployed Cloudflare Worker URL after deployment
export const ALERTS_ENDPOINT = 'https://cruise-alerts.YOUR_SUBDOMAIN.workers.dev/subscribe';

export function initAlerts() {
  populateAlertDropdowns();
  attachAlertFormListener();
}

function populateAlertDropdowns() {
  const voyages = state.voyages ?? [];

  const regions = [...new Set(voyages.map(v => v.region).filter(Boolean))].sort();
  const months  = [...new Set(voyages.map(v => v.departure_date?.slice(0, 7)).filter(Boolean))].sort();

  const regionSel = document.getElementById('alert-region');
  const monthSel  = document.getElementById('alert-month');

  regions.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r;
    opt.textContent = r;
    regionSel?.appendChild(opt);
  });

  months.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = formatMonthLabel(m);
    monthSel?.appendChild(opt);
  });
}

function attachAlertFormListener() {
  const form = document.getElementById('alert-form');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const email    = document.getElementById('alert-email')?.value?.trim();
    const line     = document.getElementById('alert-line')?.value;
    const region   = document.getElementById('alert-region')?.value;
    const month    = document.getElementById('alert-month')?.value;
    const maxPrice = document.getElementById('alert-max-price')?.value;
    const maxDur   = document.getElementById('alert-max-duration')?.value;

    if (!email) {
      document.getElementById('alert-email')?.focus();
      return;
    }

    const btnText  = document.getElementById('alert-btn-text');
    const btn      = document.getElementById('alert-submit');
    const success  = document.getElementById('alert-success');
    const errEl    = document.getElementById('alert-error');

    btn.disabled = true;
    btnText.textContent = 'Subscribing…';
    success.style.display = 'none';
    errEl.style.display   = 'none';

    const payload = {
      email,
      criteria: {
        ...(line      && { cruise_line: line }),
        ...(region    && { region }),
        ...(month     && { departure_month: month }),
        ...(maxPrice  && { max_price: parseFloat(maxPrice) }),
        ...(maxDur    && { max_duration_nights: parseInt(maxDur) }),
      },
    };

    try {
      const resp = await fetch(ALERTS_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      success.style.display = 'block';
      form.reset();
    } catch (err) {
      console.error('Alert signup failed:', err);
      errEl.style.display = 'block';
    } finally {
      btn.disabled = false;
      btnText.textContent = 'Subscribe to Alerts';
    }
  });
}
