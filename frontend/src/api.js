// Resolve the API base URL.
//  - In production (Railway), frontend + backend share an origin → use relative
//    paths (empty base). This is the default.
//  - VITE_API_BASE can override (e.g. point at a separately hosted backend).
//  - __PORT_8000__ is replaced with the proxy path on the Perplexity preview deploy.
//  - Otherwise (local dev) fall back to localhost:8000.
const PORT_TOKEN = '__PORT_8000__';
const ENV_BASE = import.meta.env.VITE_API_BASE;
let BASE;
if (ENV_BASE !== undefined && ENV_BASE !== '') {
  BASE = ENV_BASE;
} else if (!PORT_TOKEN.startsWith('__')) {
  BASE = PORT_TOKEN;
} else if (import.meta.env.PROD) {
  BASE = ''; // same-origin (Railway)
} else {
  BASE = 'http://localhost:8000';
}

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

export const api = {
  analyzeSender: (url) => req('/api/sender/analyze', { method: 'POST', body: JSON.stringify({ url }) }),
  listSenders: () => req('/api/senders'),
  getSender: (id) => req('/api/sender/' + id),
  evaluateTarget: (payload) => req('/api/target/evaluate', { method: 'POST', body: JSON.stringify(payload) }),
  listEvaluations: (senderId) => req('/api/evaluations' + (senderId ? `?sender_id=${senderId}` : '')),
  getEvaluation: (id) => req('/api/evaluation/' + id),
};
