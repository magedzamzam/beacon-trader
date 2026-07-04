import { useEffect, useState } from "react";
import { Clock } from "lucide-react";
import { api } from "../lib/api";

const fmtMin = (m) => (m == null ? "" : m < 60 ? `${m}m`
  : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`);

/**
 * SessionStrip — a compact one-line dashboard summary of the trading-hours
 * status: current session(s), the news blackout / next high-impact event, and
 * the market's holiday/weekend state. Polls every 30s.
 */
export default function SessionStrip() {
  const [s, setS] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.tradingHoursStatus().then(x => alive && setS(x)).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!s) return null;

  const active = s.sessions?.active || [];
  const n = s.news || {}, h = s.holiday || {};
  return (
    <div className="card p-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
      <span className="flex items-center gap-1.5 text-muted num">
        <Clock className="w-3.5 h-3.5" /> {(s.now_utc || "").slice(11, 16)} UTC
      </span>
      <span className="flex items-center gap-1.5">
        <span className="text-muted uppercase tracking-wider text-[10px]">Session</span>
        {active.length ? active.map(a => (
          <span key={a} className="px-1.5 py-0.5 rounded bg-long/15 text-long">{a}</span>
        )) : <span className="text-muted">between sessions</span>}
      </span>
      <span className="flex items-center gap-1.5">
        <span className="text-muted uppercase tracking-wider text-[10px]">News</span>
        {n.in_blackout ? <span className="text-short">⛔ {n.active?.title}</span>
          : n.next ? <span><span className="text-ink">{n.next.title}</span>{" "}
              <span className="text-muted">({n.next.ccy}) in {fmtMin(n.next.in_min)}</span></span>
          : <span className="text-muted">clear</span>}
      </span>
      <span className="flex items-center gap-1.5">
        <span className="text-muted uppercase tracking-wider text-[10px]">Market</span>
        {h.is_holiday ? <span className="text-warn">{h.holiday_name}</span>
          : h.is_weekend ? <span className="text-warn">Weekend</span>
          : <span className="text-long">open</span>}
      </span>
    </div>
  );
}
