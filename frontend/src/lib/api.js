const BASE = import.meta.env.VITE_API_BASE || "/api";  // nginx proxies /api -> api:8000

export function getToken() { return localStorage.getItem("beacon_token") || ""; }
export function setToken(t) { localStorage.setItem("beacon_token", t); }

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
  // reads
  health: () => req("/health"),
  dashboard: () => req("/dashboard/summary"),
  trades: () => req("/trades"),
  signals: () => req("/signals"),
  sources: () => req("/sources"),
  brokers: () => req("/brokers"),
  accounts: () => req("/accounts"),
  symbols: () => req("/symbols"),
  perfSummary: () => req("/performance/summary"),
  perfBySource: () => req("/performance/by_source"),
  brokerHealth: (id) => req(`/brokers/${id}/health`),
  brokerLiveAccounts: (id) => req(`/brokers/${id}/accounts`),
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
  // sources
  createSource: (s) => post("/sources", s),
  updateSource: (id, s) => patch(`/sources/${id}`, s),
  deleteSource: (id) => del(`/sources/${id}`),
};
