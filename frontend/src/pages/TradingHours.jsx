import { useEffect, useState } from "react";
import { Clock, RefreshCw } from "lucide-react";
import { Card, Empty } from "../components/ui";
import { Button, Toggle, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const fmtMin = (m) => (m == null ? "—" : m < 60 ? `${m}m`
  : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`);
const box = "bg-panel2 border border-edge rounded-lg p-3";
const smallInput = "bg-panel border border-edge rounded px-2 py-1 num outline-none focus:border-beacon";

/**
 * TradingHours — session windows (DST-aware, local market time), a news blackout
 * from a persisted economic calendar, and US holiday/weekend status. Read-only
 * intelligence for now: you can see and configure it; gating trades on it is a
 * documented follow-up.
 */
export default function TradingHours() {
  const [status, setStatus] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = () => api.tradingHoursStatus()
    .then(s => { setStatus(s); setCfg(s.config); }).catch(e => setErr(e.message));
  useEffect(() => {
    load();
    const t = setInterval(() => api.tradingHoursStatus().then(setStatus).catch(() => {}), 30000);
    return () => clearInterval(t);
  }, []);

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!status || !cfg) return <Card><Empty>Loading…</Empty></Card>;

  const touch = () => setSaved(false);
  const setSession = (i, k, v) => { setCfg(c => { const s = [...c.sessions]; s[i] = { ...s[i], [k]: v }; return { ...c, sessions: s }; }); touch(); };
  const setNews = (k, v) => { setCfg(c => ({ ...c, news: { ...c.news, [k]: v } })); touch(); };
  const setHol = (k, v) => { setCfg(c => ({ ...c, holidays: { ...c.holidays, [k]: v } })); touch(); };
  const save = async () => { try { const r = await api.saveTradingHoursConfig(cfg); setCfg(r); setSaved(true); load(); } catch (e) { setErr(e.message); } };
  const refresh = async () => { setBusy(true); try { await api.refreshCalendar(); await load(); } catch (e) { setErr(e.message); } finally { setBusy(false); } };

  const n = status.news, h = status.holiday;
  return (
    <div className="space-y-4">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2"><Clock className="w-4 h-4 text-beacon" /> Live status</div>
          <span className="text-[11px] text-muted num">{(status.now_utc || "").slice(11, 16)} UTC</span>
        </div>
        <div className="p-4 grid sm:grid-cols-3 gap-3">
          <div className={box}>
            <div className="text-[10px] uppercase tracking-wider text-muted mb-2">Sessions</div>
            <div className="flex flex-wrap gap-1.5">
              {status.sessions.windows.map(w => (
                <span key={w.id} title={w.tz}
                  className={`px-2 py-1 rounded text-[11px] ${w.active ? "bg-long/15 text-long" : "bg-panel text-muted"}`}>
                  {w.label}{w.active ? ` · closes ${fmtMin(w.closes_in_min)}` : ` · opens ${fmtMin(w.opens_in_min)}`}
                </span>
              ))}
            </div>
          </div>
          <div className={box}>
            <div className="text-[10px] uppercase tracking-wider text-muted mb-2">News</div>
            {n.in_blackout
              ? <div className="text-sm text-short">⛔ Blackout — {n.active?.title} ({n.active?.ccy})</div>
              : n.next
                ? <div className="text-sm">Next: <span className="text-ink">{n.next.title}</span>{" "}
                    <span className="text-muted">({n.next.ccy}, {n.next.impact})</span> in {fmtMin(n.next.in_min)}</div>
                : <div className="text-sm text-muted">No upcoming high-impact events.</div>}
          </div>
          <div className={box}>
            <div className="text-[10px] uppercase tracking-wider text-muted mb-2">Holiday / Weekend</div>
            {h.is_holiday
              ? <div className="text-sm text-warn">US holiday — {h.holiday_name}</div>
              : h.is_weekend
                ? <div className="text-sm text-warn">Weekend</div>
                : <div className="text-sm text-long">Markets open{h.next_holiday
                    ? <span className="text-muted"> · next {h.next_holiday.name} in {h.next_holiday.in_days}d</span> : ""}</div>}
          </div>
        </div>
        <div className="px-4 py-2 text-[11px] text-warn border-t border-edge">
          Read-only intelligence for now — configure it here; gating trades on session/news/holiday is a documented follow-up.
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Configuration</div>
          {saved && <span className="text-xs text-long">Saved</span>}
        </div>
        <div className="p-5 space-y-6">
          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">Session windows — local market time (DST handled)</div>
            <div className="space-y-2">
              {cfg.sessions.map((s, i) => (
                <div key={s.id} className="flex flex-wrap items-center gap-2 border border-edge rounded-lg px-3 py-2 bg-panel2">
                  <span className="text-sm font-medium w-28">{s.label}</span>
                  <span className="text-[11px] text-muted num w-32">{s.tz}</span>
                  <label className="text-xs text-muted flex items-center gap-1">start
                    <input value={s.start} onChange={e => setSession(i, "start", e.target.value)} className={`w-16 ${smallInput}`} /></label>
                  <label className="text-xs text-muted flex items-center gap-1">end
                    <input value={s.end} onChange={e => setSession(i, "end", e.target.value)} className={`w-16 ${smallInput}`} /></label>
                  <div className="ml-auto"><Toggle checked={s.enabled} onChange={v => setSession(i, "enabled", v)} label={s.enabled ? "on" : "off"} /></div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">News blackout</div>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
              <Toggle checked={cfg.news.enabled} onChange={v => setNews("enabled", v)} label={cfg.news.enabled ? "enabled" : "off"} />
              <label className="text-xs text-muted flex items-center gap-1">before (min)
                <input type="number" min="0" value={cfg.news.before_min} onChange={e => setNews("before_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">after (min)
                <input type="number" min="0" value={cfg.news.after_min} onChange={e => setNews("after_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">impacts
                <input value={cfg.news.impacts.join(",")} placeholder="high"
                  onChange={e => setNews("impacts", e.target.value.split(",").map(x => x.trim().toLowerCase()).filter(Boolean))}
                  className={`w-28 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">currencies
                <input value={cfg.news.currencies.join(",")} placeholder="all"
                  onChange={e => setNews("currencies", e.target.value.split(",").map(x => x.trim().toUpperCase()).filter(Boolean))}
                  className={`w-28 ${smallInput}`} /></label>
              <Button variant="ghost" onClick={refresh} disabled={busy}>
                <RefreshCw className="w-4 h-4 inline -mt-0.5" /> {busy ? "Refreshing…" : "Refresh calendar"}</Button>
            </div>
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">Weekend &amp; holidays</div>
            <div className="flex flex-wrap items-center gap-6">
              <label className="flex items-center gap-2 text-sm">Block weekend
                <Toggle checked={cfg.holidays.block_weekend} onChange={v => setHol("block_weekend", v)} /></label>
              <label className="flex items-center gap-2 text-sm">Block US holidays
                <Toggle checked={cfg.holidays.block_us_holidays} onChange={v => setHol("block_us_holidays", v)} /></label>
            </div>
          </div>

          <div className="flex justify-end"><Button onClick={save}>Save configuration</Button></div>
        </div>
      </Card>
    </div>
  );
}
