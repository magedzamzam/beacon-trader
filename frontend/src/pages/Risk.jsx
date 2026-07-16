import { useEffect, useState } from "react";
import { Pencil } from "lucide-react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Field, Input, Toggle, Button, ErrorNote } from "../components/form";
import RiskConfigEditor from "../components/RiskConfigEditor";
import { api } from "../lib/api";

const summarize = (r) => {
  if (!r || !Object.keys(r).length) return "—";
  const base = r.basis === "fixed_cash" ? `$${r.value}` : `${r.value}% equity`;
  if (r.allocation === "per_tp") {
    const per = Object.entries(r.per_tp_percent || {}).map(([k, v]) => `TP${k}:${v}%`).join(" ");
    return `${base} · per-TP · ${per}`;
  }
  return `${base} · even`;
};

export default function Risk() {
  const [accounts, setAccounts] = useState([]);
  const [sources, setSources] = useState([]);
  const [err, setErr] = useState(null);
  const [edit, setEdit] = useState(null);
  const load = async () => {
    try { setAccounts(await api.accounts()); setSources(await api.sources()); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>
      <RiskLimitsCard />
      <EntryGuardCard />
      <TrendFilterCard />
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Account limits</div>
        {!accounts.length ? <Empty>No accounts. Add one under Brokers.</Empty> : (
          <Table>
            <thead><tr className="border-b border-edge"><Th>Account</Th><Th>Risk / limits</Th><Th>Trading</Th><Th right></Th></tr></thead>
            <tbody>
              {accounts.map(a => (
                <tr key={a.id} className="border-b border-edge/60">
                  <Td>{a.name}</Td>
                  <Td><span className="text-xs num text-muted">{summarize(a.risk_config)}</span></Td>
                  <Td><Badge tone={a.enabled ? "long" : "muted"}>{a.enabled ? "on" : "off"}</Badge></Td>
                  <Td right><Button variant="ghost" onClick={() => setEdit(a)}><Pencil className="w-4 h-4" /></Button></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Per-source overrides</div>
        {!sources.length ? <Empty>No sources.</Empty> : (
          <Table>
            <thead><tr className="border-b border-edge"><Th>Source</Th><Th>Override</Th></tr></thead>
            <tbody>
              {sources.map(s => (
                <tr key={s.id} className="border-b border-edge/60">
                  <Td>{s.name}</Td>
                  <Td><span className="text-xs num text-muted">
                    {s.risk_config && Object.keys(s.risk_config).length ? summarize(s.risk_config) : "inherits account"}
                  </span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">Edit source overrides under Sources.</div>
      </Card>

      {edit && <RiskModal account={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); load(); }} />}
    </div>
  );
}

function RiskLimitsCard() {
  const [cfg, setCfg] = useState(null);
  const [status, setStatus] = useState(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);
  const loadStatus = () => api.riskStatus().then(setStatus).catch(() => {});
  useEffect(() => { api.riskLimits().then(setCfg).catch(e => setErr(e.message)); loadStatus(); }, []);
  if (!cfg) return null;
  const set = (k, v) => { setCfg(c => ({ ...c, [k]: v })); setSaved(false); };
  const num = (k, v) => set(k, v === "" ? "" : Number(v));
  const save = async () => {
    try { setCfg(await api.saveRiskLimits(cfg)); setSaved(true); loadStatus(); } catch (e) { setErr(e.message); }
  };
  const disarm = async () => {   // one-click: fully disable the daily floor
    try { setCfg(await api.saveRiskLimits({ ...cfg, daily_loss_limit: 0 })); setSaved(true); loadStatus(); }
    catch (e) { setErr(e.message); }
  };
  const blockedAccts = (status?.accounts || []).filter(a => a.blocked);
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
        <div className="text-sm font-medium">Risk limits &amp; kill switch</div>
        <div className="flex items-center gap-2">
          {status?.blocked && <Badge tone="short">trading blocked</Badge>}
          {!cfg.enabled && <Badge tone="warn">limits off</Badge>}
          {cfg.trading_halted && <Badge tone="warn">halted</Badge>}
          {saved && <span className="text-xs text-long">Saved</span>}
        </div>
      </div>
      <div className="p-4 space-y-4">
        <ErrorNote>{err}</ErrorNote>

        {status?.blocked && (
          <div className="rounded-lg px-3 py-2 text-xs bg-short/15 text-short border border-short/30">
            <div className="font-medium">Trading is currently blocked:</div>
            {blockedAccts.map(a => (
              <div key={a.account_id} className="num mt-0.5"><b>{a.name}</b> — {a.reason}</div>
            ))}
            <button onClick={disarm} className="mt-1.5 underline">Set daily-loss limit to 0 (disarm now)</button>
          </div>
        )}

        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <label className="flex items-center gap-2 text-sm">Enforce limits
            <Toggle checked={!!cfg.enabled} onChange={v => set("enabled", v)} /></label>
          <label className="flex items-center gap-2 text-sm">
            <span className={cfg.trading_halted ? "text-warn font-medium" : ""}>Kill switch — halt all new trades</span>
            <Toggle checked={!!cfg.trading_halted} onChange={v => set("trading_halted", v)} /></label>
        </div>
        <div className="text-[11px] text-muted -mt-2">
          Turning <b>Enforce limits</b> off disables the daily-loss floor and every cap (the kill-switch is a
          separate button that still halts when on). All limits are stored here — nothing is hardcoded.
          To disarm only the floor while keeping caps, set the daily-loss limit to <b>0</b>.
          {status?.configured === false && " No risk_limits saved yet — a conservative fail-safe is active until you Save."}
        </div>
        <div className={`grid grid-cols-1 sm:grid-cols-2 gap-3 ${cfg.enabled ? "" : "opacity-60"}`}>
          <Field label="Daily loss limit (account ccy)">
            <Input type="number" value={cfg.daily_loss_limit} onChange={e => num("daily_loss_limit", e.target.value)} /></Field>
          <Field label="Per-signal ceiling (× daily limit)" hint="0.5 = one trade may risk ≤ 50% of the daily cap">
            <Input type="number" step="0.05" value={cfg.per_signal_max_pct_of_daily}
              onChange={e => num("per_signal_max_pct_of_daily", e.target.value)} /></Field>
          <Field label="Max open risk / account">
            <Input type="number" value={cfg.max_open_risk_per_account} onChange={e => num("max_open_risk_per_account", e.target.value)} /></Field>
          <Field label="Max open risk / symbol">
            <Input type="number" value={cfg.max_open_risk_per_symbol} onChange={e => num("max_open_risk_per_symbol", e.target.value)} /></Field>
          <Field label="Per-signal risk cap (% equity)" hint="#78 · one signal's whole fanout (all entry×TP legs) is scaled to ≤ this % of equity · 0 = off">
            <Input type="number" step="0.25" value={cfg.max_signal_risk_pct ?? 2.0}
              onChange={e => num("max_signal_risk_pct", e.target.value)} /></Field>
        </div>
        <div className="flex justify-end"><Button onClick={save}>Save risk limits</Button></div>
      </div>
    </Card>
  );
}

// Entry chase guard (#67) — a MARKET-hint signal only fills at market when price
// is near the signalled entry; beyond the tolerance it rests a LIMIT (or skips).
function EntryGuardCard() {
  const [cfg, setCfg] = useState(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);
  useEffect(() => { api.plannerConfig().then(setCfg).catch(e => setErr(e.message)); }, []);
  if (!cfg) return null;
  const set = (k, v) => { setCfg(c => ({ ...c, [k]: v })); setSaved(false); };
  const num = (k, v) => set(k, v === "" ? "" : Number(v));
  const save = async () => {
    try { setCfg(await api.savePlannerConfig(cfg)); setSaved(true); } catch (e) { setErr(e.message); }
  };
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
        <div className="text-sm font-medium">Entry chase guard</div>
        {saved && <span className="text-xs text-long">Saved</span>}
      </div>
      <div className="p-4 space-y-4">
        <ErrorNote>{err}</ErrorNote>
        <p className="text-[11px] text-muted">
          A "BUY/SELL now" (MARKET-hint) signal fills at market <b>only</b> when price is within the chase
          tolerance of the signalled entry. Beyond it, the entry rests as a LIMIT at the level (never chases a
          price it hasn't reached). Tolerance = the larger of <b>× stop-distance</b> and <b>× ATR</b>.
        </p>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <label className="flex items-center gap-2 text-sm">Honor "enter now" (market hint)
            <Toggle checked={!!cfg.honor_market_hint} onChange={v => set("honor_market_hint", v)} /></label>
        </div>
        <div className={`grid grid-cols-1 sm:grid-cols-2 gap-3 ${cfg.honor_market_hint ? "" : "opacity-60"}`}>
          <Field label="Chase tolerance (× |entry − SL|)" hint="0.25 = allow chasing up to a quarter of the stop distance">
            <Input type="number" step="0.05" value={cfg.chase_tolerance_r}
              onChange={e => num("chase_tolerance_r", e.target.value)} /></Field>
          <Field label="Chase tolerance (× ATR)" hint="0 = disabled; larger of the two wins">
            <Input type="number" step="0.05" value={cfg.chase_tolerance_atr}
              onChange={e => num("chase_tolerance_atr", e.target.value)} /></Field>
          <Field label="Beyond tolerance" hint="what to do when the entry is too far to fill at market">
            <select value={cfg.beyond_tolerance} onChange={e => set("beyond_tolerance", e.target.value)}
              className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm w-full outline-none focus:border-beacon">
              <option value="limit">rest a LIMIT at the entry</option>
              <option value="skip">skip the entry</option>
            </select></Field>
          <Field label="Max TP distance (× entry)" hint="drop parse-artifact TPs this far from entry (0.5 = 50%)">
            <Input type="number" step="0.05" value={cfg.max_tp_distance_pct}
              onChange={e => num("max_tp_distance_pct", e.target.value)} /></Field>
        </div>
        <div className="flex justify-end"><Button onClick={save}>Save entry guard</Button></div>
      </div>
    </Card>
  );
}

function TrendFilterCard() {
  const [cfg, setCfg] = useState(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);
  useEffect(() => { api.entryFilters().then(r => setCfg(r.trend_alignment)).catch(e => setErr(e.message)); }, []);
  if (!cfg) return null;
  const set = (k, v) => { setCfg(c => ({ ...c, [k]: v })); setSaved(false); };
  const save = async () => {
    try { const r = await api.saveEntryFilters({ trend_alignment: cfg }); setCfg(r.trend_alignment); setSaved(true); }
    catch (e) { setErr(e.message); }
  };
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
        <div className="text-sm font-medium">Trend-alignment entry filter</div>
        <div className="flex items-center gap-2">
          <Badge tone={cfg.enabled ? "beacon" : "muted"}>{cfg.enabled ? `on · ${cfg.mode}` : "off"}</Badge>
          {saved && <span className="text-xs text-long">Saved</span>}
        </div>
      </div>
      <div className="p-4 space-y-4">
        <ErrorNote>{err}</ErrorNote>
        <p className="text-[11px] text-muted">
          Skips or de-sizes signals whose direction fights the higher-timeframe trend
          ({cfg.timeframe} EMA{cfg.ema_period}). Counter-trend entries held ~95% of the
          book's realized loss. Off by default — validated over a single bearish window,
          so A/B it and re-verify when the trend flips (fail-open on missing data).
          <b> Confirmation (#79):</b> an "aligned" read must also pass the EMA-slope and
          ATR-distance checks below, so a lagging EMA can't green-light the wrong side at
          a regime turn. Re-backtest with confirmation on before enabling.
        </p>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <label className="flex items-center gap-2 text-sm">Enable filter
            <Toggle checked={!!cfg.enabled} onChange={v => set("enabled", v)} /></label>
        </div>
        <div className={`grid grid-cols-1 sm:grid-cols-2 gap-3 ${cfg.enabled ? "" : "opacity-60"}`}>
          <Field label="Trend timeframe" hint="e.g. 4h, 1d">
            <Input value={cfg.timeframe} onChange={e => set("timeframe", e.target.value)} /></Field>
          <Field label="EMA period">
            <Input type="number" value={cfg.ema_period} onChange={e => set("ema_period", Number(e.target.value))} /></Field>
          <Field label="Counter-trend action" hint="skip = reject · desize = trade smaller">
            <select value={cfg.mode} onChange={e => set("mode", e.target.value)}
              className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm w-full outline-none focus:border-beacon">
              <option value="skip">skip</option>
              <option value="desize">desize</option>
            </select></Field>
          <Field label="De-size factor" hint="counter-trend size × this (desize mode)">
            <Input type="number" step="0.05" min="0" max="1" value={cfg.desize_factor}
              onChange={e => set("desize_factor", Number(e.target.value))} /></Field>
          <Field label="Min distance (ATR)" hint="#79 · price must be ≥ this many ATR beyond the EMA (skip the chop band)">
            <Input type="number" step="0.1" min="0" value={cfg.min_dist_atr ?? 0.5}
              onChange={e => set("min_dist_atr", Number(e.target.value))} /></Field>
          <Field label="HTF concordance TF" hint="#79 · timeframe that must agree when concordance is on">
            <Input value={cfg.htf_timeframe ?? "1h"} onChange={e => set("htf_timeframe", e.target.value)} /></Field>
        </div>
        <div className={`flex flex-wrap gap-x-8 gap-y-2 ${cfg.enabled ? "" : "opacity-60"}`}>
          <label className="flex items-center gap-2 text-xs text-muted">Require EMA slope (#79)
            <Toggle checked={cfg.require_slope ?? true} onChange={v => set("require_slope", v)} /></label>
          <label className="flex items-center gap-2 text-xs text-muted">Require HTF concordance
            <Toggle checked={cfg.require_htf_concordance ?? false} onChange={v => set("require_htf_concordance", v)} /></label>
        </div>
        <div className="flex justify-end"><Button onClick={save}>Save entry filter</Button></div>
      </div>
    </Card>
  );
}

function RiskModal({ account, onClose, onSaved }) {
  const [risk, setRisk] = useState(account.risk_config || { basis: "capital_percent", value: "1.0", allocation: "even" });
  const [err, setErr] = useState(null);
  const save = async () => {
    try { await api.updateAccount(account.id, { risk_config: risk }); onSaved(); }
    catch (e) { setErr(e.message); }
  };
  return (
    <Modal title={`Limits — ${account.name}`} onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      <RiskConfigEditor value={risk} onChange={setRisk} />
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>Save</Button>
      </div>
    </Modal>
  );
}
