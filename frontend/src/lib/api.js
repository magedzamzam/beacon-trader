const BASE = import.meta.env.VITE_API_BASE || "/api";  // nginx proxies /api -> api:8000

export function getToken() { return localStorage.getItem("beacon_token") || ""; }
export function setToken(t) { localStorage.setItem("beacon_token", t); }
export function clearToken() { localStorage.removeItem("beacon_token"); }

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j.detail) detail = j.detail; } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

const post = (p, body) => req(p, { method: "POST", body: JSON.stringify(body) });
const patch = (p, body) => req(p, { method: "PATCH", body: JSON.stringify(body) });
const del = (p) => req(p, { method: "DELETE" });

export const api = {
  // auth
  authStatus: () => req("/auth/status"),
  login: (username, password) => post("/auth/login", { username, password }),
  register: (username, password) => post("/auth/register", { username, password }),
  me: () => req("/auth/me"),
  // reads
  health: () => req("/health"),
  dashboard: (accountId = "") => req(`/dashboard/summary${accountId ? `?account_id=${accountId}` : ""}`),
  trades: () => req("/trades"),
  signals: (q = "") => req(`/signals${q}`),
  sources: () => req("/sources"),
  brokers: () => req("/brokers"),
  accounts: () => req("/accounts"),
  accountPerformance: (id) => req(`/accounts/${id}/performance`),
  symbols: () => req("/symbols"),
  perfSummary: (accountId = "") => req(`/performance/summary${accountId ? `?account_id=${accountId}` : ""}`),
  perfBySource: (accountId = "") => req(`/performance/by_source${accountId ? `?account_id=${accountId}` : ""}`),
  equityCurve: (accountId = "") => req(`/performance/equity_curve${accountId ? `?account_id=${accountId}` : ""}`),
  brokerHealth: (id) => req(`/brokers/${id}/health`),
  brokerLiveAccounts: (id) => req(`/brokers/${id}/accounts`),
  // messages (telegram history)
  messages: (q = "") => req(`/messages${q}`),
  channels: () => req("/messages/channels"),
  syncMessages: (limit = 200) => post(`/messages/sync?limit=${limit}`, {}),
  // execution workflow / audit
  events: (q = "") => req(`/events${q}`),
  tradeDetail: (id) => req(`/trades/${id}`),
  // AI
  aiConfig: () => req("/ai/config"),
  saveAiConfig: (c) => req("/ai/config", { method: "PUT", body: JSON.stringify(c) }),
  aiAssessments: (q = "") => req(`/ai/assessments${q}`),
  aiAssessSignal: (id) => post(`/ai/signals/${id}/assess`, {}),
  aiAssessTrade: (id) => post(`/ai/trades/${id}/assess`, {}),
  // technical-analysis indicator config (per-signal capture)
  taCatalog: () => req("/ta/catalog"),
  taConfig: () => req("/ta/config"),
  saveTaConfig: (c) => req("/ta/config", { method: "PUT", body: JSON.stringify(c) }),
  // Bayesian correlation of captured features with outcomes
  bayesAnalysis: (minN = 5) => req(`/analysis/bayes?min_n=${minN}`),
  // brokers
  createBroker: (b) => post("/brokers", b),
  updateBroker: (id, b) => patch(`/brokers/${id}`, b),
  deleteBroker: (id) => del(`/brokers/${id}`),
  // accounts
  createAccount: (a) => post("/accounts", a),
  updateAccount: (id, a) => patch(`/accounts/${id}`, a),
  deleteAccount: (id) => del(`/accounts/${id}`),
  // symbols
  createSymbol: (s) => post("/symbols", s),
  updateSymbol: (id, s) => patch(`/symbols/${id}`, s),
  deleteSymbol: (id) => del(`/symbols/${id}`),
  // legs
  cancelLeg: (id) => post(`/legs/${id}/cancel`, {}),
  closeLeg: (id) => post(`/legs/${id}/close`, {}),
  bulkLegs: (payload) => post(`/legs/bulk`, payload),
  // signals
  manualSignal: (s) => post(`/signals/manual`, s),
  reinitiate: (id) => post(`/signals/${id}/reinitiate`, {}),
  // market
  candles: (symbol, resolution, max_bars=200) => req(`/market/candles?symbol=${symbol}&resolution=${resolution}&max_bars=${max_bars}`),
  quote: (symbol) => req(`/market/quote?symbol=${symbol}`),
  // sources
  createSource: (s) => post("/sources", s),
  updateSource: (id, s) => patch(`/sources/${id}`, s),
  deleteSource: (id) => del(`/sources/${id}`),
};
