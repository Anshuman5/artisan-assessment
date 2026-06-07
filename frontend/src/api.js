// __PORT_8000__ is replaced with the proxy path at deploy time. During local
// dev it stays literal, so we fall back to localhost:8000.
const PORT_TOKEN = '__PORT_8000__';
const BASE = PORT_TOKEN.startsWith('__') ? 'http://localhost:8000' : PORT_TOKEN;

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
