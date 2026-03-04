/**
 * Cruise Price Alerts — Cloudflare Worker
 *
 * Handles two endpoints:
 *   POST /subscribe   — add a new email alert subscription
 *   GET  /unsubscribe — remove a subscription by token
 *
 * Subscriptions are stored in data/alerts.json in the GitHub repo
 * via the GitHub Contents API. The daily scrape workflow reads this
 * file and sends emails via Resend when criteria are matched.
 *
 * Required Worker environment variables (set in Cloudflare dashboard):
 *   GITHUB_TOKEN   — fine-grained PAT with contents:write on the repo
 *   GITHUB_REPO    — e.g. "brickpack/cruise-prices-pipeline"
 *   ALLOWED_ORIGIN — e.g. "https://brickpack.github.io"
 */

const ALERTS_PATH = 'data/alerts.json';
const GITHUB_API  = 'https://api.github.com';

export default {
  async fetch(request, env) {
    const url    = new URL(request.url);
    const origin = request.headers.get('Origin') ?? '';

    // CORS headers
    const corsHeaders = {
      'Access-Control-Allow-Origin':  env.ALLOWED_ORIGIN ?? '*',
      'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    try {
      if (request.method === 'POST' && url.pathname === '/subscribe') {
        return await handleSubscribe(request, env, corsHeaders);
      }
      if (request.method === 'GET' && url.pathname === '/unsubscribe') {
        return await handleUnsubscribe(url, env, corsHeaders);
      }
      return new Response('Not found', { status: 404, headers: corsHeaders });
    } catch (err) {
      console.error(err);
      return new Response(JSON.stringify({ error: 'Internal error' }), {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
  },
};

// ---------------------------------------------------------------------------
// Subscribe
// ---------------------------------------------------------------------------

async function handleSubscribe(request, env, corsHeaders) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: 'Invalid JSON' }, 400, corsHeaders);
  }

  const email = (body.email ?? '').trim().toLowerCase();
  if (!email || !email.includes('@')) {
    return jsonResponse({ error: 'Invalid email' }, 400, corsHeaders);
  }

  const criteria = body.criteria ?? {};

  // Load current alerts file
  const { content, sha } = await getAlertsFile(env);

  // Check for duplicate
  const existing = content.find(a => a.email === email &&
    JSON.stringify(a.criteria) === JSON.stringify(criteria));
  if (existing) {
    return jsonResponse({ ok: true, message: 'Already subscribed' }, 200, corsHeaders);
  }

  // Add new subscription
  const token = crypto.randomUUID();
  content.push({
    id: token,
    email,
    criteria,
    created_at: new Date().toISOString(),
    last_notified: null,
  });

  await putAlertsFile(env, content, sha);
  return jsonResponse({ ok: true }, 200, corsHeaders);
}

// ---------------------------------------------------------------------------
// Unsubscribe
// ---------------------------------------------------------------------------

async function handleUnsubscribe(url, env, corsHeaders) {
  const token = url.searchParams.get('token');
  if (!token) {
    return new Response('Missing token', { status: 400, headers: corsHeaders });
  }

  const { content, sha } = await getAlertsFile(env);
  const before = content.length;
  const updated = content.filter(a => a.id !== token);

  if (updated.length === before) {
    return new Response('Subscription not found or already removed.', {
      status: 200, headers: { ...corsHeaders, 'Content-Type': 'text/html' },
    });
  }

  await putAlertsFile(env, updated, sha);
  return new Response(
    `<html><body style="font-family:sans-serif;max-width:400px;margin:4rem auto;text-align:center">
      <h2>✓ Unsubscribed</h2>
      <p>You've been removed from cruise price alerts.</p>
    </body></html>`,
    { status: 200, headers: { ...corsHeaders, 'Content-Type': 'text/html' } }
  );
}

// ---------------------------------------------------------------------------
// GitHub helpers
// ---------------------------------------------------------------------------

async function getAlertsFile(env) {
  const resp = await ghRequest(env, 'GET', ALERTS_PATH);
  if (resp.status === 404) {
    return { content: [], sha: null };
  }
  const data = await resp.json();
  const decoded = JSON.parse(atob(data.content.replace(/\n/g, '')));
  return { content: decoded, sha: data.sha };
}

async function putAlertsFile(env, content, sha) {
  const body = {
    message: 'alerts: update subscriptions',
    content: btoa(JSON.stringify(content, null, 2)),
    ...(sha && { sha }),
  };
  const resp = await ghRequest(env, 'PUT', ALERTS_PATH, body);
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`GitHub PUT failed: ${resp.status} ${err}`);
  }
}

async function ghRequest(env, method, path, body) {
  const url = `${GITHUB_API}/repos/${env.GITHUB_REPO}/contents/${path}`;
  return fetch(url, {
    method,
    headers: {
      'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
      'Accept':        'application/vnd.github+json',
      'Content-Type':  'application/json',
      'User-Agent':    'cruise-alerts-worker',
    },
    ...(body && { body: JSON.stringify(body) }),
  });
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function jsonResponse(data, status, corsHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...corsHeaders, 'Content-Type': 'application/json' },
  });
}
