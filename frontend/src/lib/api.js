const BASE = import.meta.env.VITE_API_BASE || "/api";

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
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  health: () => req("/health"),
  dashboard: () => req("/dashboard/summary"),
  trades: () => req("/trades"),
  signals: () => req("/signals"),
  sources: () => req("/sources"),
  brokers: () => req("/brokers"),
  accounts: () => req("/accounts"),
  perfSummary: () => req("/performance/summary"),
  perfBySource: () => req("/performance/by_source"),
};
