import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Card, Empty } from "../components/ui";
import { Button, Toggle, ErrorNote } from "../components/form";
import SessionTimeline from "../components/SessionTimeline";
import NewsCard from "../components/NewsCard";
import { api } from "../lib/api";

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

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2"><SessionTimeline status={status} /></div>
        <NewsCard status={status} />
      </div>
      <div className="text-[11px] text-muted">
        The <b>news blackout</b> now gates new entries (#77) when <i>gate entries</i> is on — tiered: a wider window
        for CPI/NFP/FOMC-grade releases, the tight window for other high-impact. Open positions are never touched.
        Session/holiday gating remains intelligence-only.
      </div>

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
                  <label className="text-xs text-muted flex items-center gap-1" title="#81 · risk size × this while active (1 = full, 0.5 = half). London/NY overlap multiplies.">risk ×
                    <input type="number" step="0.05" min="0" max="1" value={s.risk_mult ?? 1}
                      onChange={e => setSession(i, "risk_mult", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
                  <div className="ml-auto"><Toggle checked={s.enabled} onChange={v => setSession(i, "enabled", v)} label={s.enabled ? "on" : "off"} /></div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">News blackout</div>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
              <Toggle checked={cfg.news.enabled} onChange={v => setNews("enabled", v)} label={cfg.news.enabled ? "enabled" : "off"} />
              <Toggle checked={cfg.news.gate_entries ?? true} onChange={v => setNews("gate_entries", v)} label={(cfg.news.gate_entries ?? true) ? "gates entries" : "observe only"} />
              <label className="text-xs text-muted flex items-center gap-1">before (min)
                <input type="number" min="0" value={cfg.news.before_min} onChange={e => setNews("before_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">after (min)
                <input type="number" min="0" value={cfg.news.after_min} onChange={e => setNews("after_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">major before
                <input type="number" min="0" value={cfg.news.major_before_min ?? 30} onChange={e => setNews("major_before_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
              <label className="text-xs text-muted flex items-center gap-1">major after
                <input type="number" min="0" value={cfg.news.major_after_min ?? 15} onChange={e => setNews("major_after_min", +e.target.value)} className={`w-14 ${smallInput}`} /></label>
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
