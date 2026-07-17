import { useEffect, useMemo, useState } from "react";
import { GitBranch, Trash2, LogIn, Filter, LogOut, Plus } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Field, Input, Select, Toggle, Button, ErrorNote } from "../components/form";
import SlRulesEditor from "../components/SlRulesEditor";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";

/**
 * Strategies (#84) — one execution strategy per (Account, Source), in three pillars:
 *   Entry Strategy · Entry Filtration · Exit Strategy.
 * Scope is (account, source), either "Any" — the most-specific enabled match wins,
 * so you get defaults for free. The executor snapshots the resolved exit rules at
 * entry, so edits only affect FUTURE trades (running A/B arms stay frozen). Compare
 * arms via the account filter on Bayesian Analysis / Performance.
 */
const mv = (target, extra = {}) => ({ type: "move_sl_to", target, ...extra });
const tpH = (i) => ({ type: "tp_hit", index: i });
const SL_PRESETS = {
  "BE at TP1 → trail": [{ trigger: tpH(1), action: mv("entry") }, { trigger: tpH(2), action: mv("previous_tp") }, { trigger: tpH(3), action: mv("previous_tp") }],
  "BE at TP2 → trail": [{ trigger: tpH(2), action: mv("entry") }, { trigger: tpH(3), action: mv("previous_tp") }, { trigger: tpH(4), action: mv("previous_tp") }],
  "BE at TP3 → trail": [{ trigger: tpH(3), action: mv("entry") }, { trigger: tpH(4), action: mv("previous_tp") }, { trigger: tpH(5), action: mv("previous_tp") }],
  "Tighten: +30pts → BE": [{ trigger: { type: "price_move", points: 30 }, action: mv("entry") }, { trigger: tpH(2), action: mv("previous_tp") }],
};
// Mirrors beacon_core.ta.registry.AVAILABLE_TIMEFRAMES (the TFs the trend read supports).
const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const BLANK = () => ({
  id: null, account_id: "", source_id: "", label: "", enabled: true,
  entry: { ttl_minutes: "", honor_market_hint: true, chase_tolerance_r: "", chase_tolerance_atr: "", beyond_tolerance: "limit", max_tp_distance_pct: "" },
  trend: { enabled: false, timeframe: "4h", ema_period: 200, mode: "skip", desize_factor: 0.25,
           require_slope: true, slope_lookback: 10, min_dist_atr: 0.5,
           require_htf_concordance: false, htf_timeframe: "1h" },
  rules: [],
  exit: { sl_rules: [], cancel_pending_on_stop: true },
});
const num = (v) => (v === "" || v == null ? undefined : Number(v));
const INPUT = "w-full bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-beacon";
const TABS = [["entry", "Entry Strategy", LogIn], ["filter", "Entry Filtration", Filter], ["exit", "Exit Strategy", LogOut]];

export default function Strategies() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [all, setAll] = useState([]);
  const [form, setForm] = useState(BLANK());
  const [tab, setTab] = useState("entry");
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = () => api.strategies().then(setAll).catch((e) => setErr(e.message));
  useEffect(() => {
    api.sources().then(setSources).catch((e) => setErr(e.message));
    api.accounts().then(setAccounts).catch((e) => setErr(e.message));
    load();
  }, []);

  const srcName = (id) => (id == null || id === "" ? "Any source" : sources.find((s) => String(s.id) === String(id))?.name || `#${id}`);
  const acctName = (id) => (id == null || id === "" ? "Any account" : accounts.find((a) => String(a.id) === String(id))?.name || `#${id}`);
  const setF = (k, v) => { setForm((f) => ({ ...f, [k]: v })); setSaved(false); };
  const setSub = (grp, k, v) => { setForm((f) => ({ ...f, [grp]: { ...f[grp], [k]: v } })); setSaved(false); };

  // Load an existing strategy row into the editor (or a blank at the chosen scope).
  const editRow = (row) => {
    setSaved(false); setTab("entry");
    const ep = row.entry_policy || {}, ef = row.entry_filters || {}, xp = row.exit_policy || {};
    setForm({
      id: row.id, account_id: row.account_id ?? "", source_id: row.source_id ?? "",
      label: row.label || "", enabled: row.enabled,
      entry: { ...BLANK().entry, ...Object.fromEntries(Object.entries(ep).map(([k, v]) => [k, v ?? ""])) },
      trend: { ...BLANK().trend, ...(ef.trend_alignment || {}) },
      rules: Array.isArray(ef.rules) ? ef.rules : [],
      exit: { sl_rules: Array.isArray(xp.sl_rules) ? xp.sl_rules : [],
              cancel_pending_on_stop: xp.cancel_pending_on_stop !== false },
    });
  };
  const newAt = () => { setForm(BLANK()); setTab("entry"); setSaved(false); };

  const save = async () => {
    setErr(null); setSaved(false);
    const sl_rules = form.exit.sl_rules.length ? form.exit.sl_rules : null;   // [] = inherit default
    const entry_policy = {
      ttl_minutes: num(form.entry.ttl_minutes), honor_market_hint: form.entry.honor_market_hint,
      chase_tolerance_r: num(form.entry.chase_tolerance_r), chase_tolerance_atr: num(form.entry.chase_tolerance_atr),
      beyond_tolerance: form.entry.beyond_tolerance, max_tp_distance_pct: num(form.entry.max_tp_distance_pct),
    };
    const body = {
      account_id: form.account_id === "" ? null : form.account_id,
      source_id: form.source_id === "" ? null : form.source_id,
      label: form.label || null, enabled: form.enabled,
      entry_policy,
      entry_filters: { trend_alignment: form.trend, rules: form.rules },
      exit_policy: { sl_rules, cancel_pending_on_stop: form.exit.cancel_pending_on_stop },
    };
    try { const r = await api.saveStrategy(body); setSaved(true); await load(); editRow(r); }
    catch (e) { setErr(e.message); }
  };
  const del = async (id) => { try { await api.deleteStrategy(id); if (form.id === id) newAt(); await load(); } catch (e) { setErr(e.message); } };

  const addRule = () => setF("rules", [...form.rules, { enabled: true, name: "", when: { type: "session_in", sessions: [] }, action: "scale", factor: 0.5 }]);
  const setRule = (i, patch) => setF("rules", form.rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const delRule = (i) => setF("rules", form.rules.filter((_, j) => j !== i));

  const scopeLabel = `${acctName(form.account_id)} · ${srcName(form.source_id)}`;
  return (
    <div className="space-y-5">
      {/* Editor */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center gap-2 flex-wrap">
          <GitBranch className="w-4 h-4 text-beacon" />
          <span className="text-sm font-medium">{form.id ? "Edit strategy" : "New strategy"}</span>
          <span className="text-muted text-xs">· {scopeLabel}</span>
          <div className="ml-auto flex items-center gap-2">
            {saved && <span className="text-xs text-long">Saved</span>}
            <Button variant="ghost" onClick={newAt}>New</Button>
            <Button onClick={save}>Save strategy</Button>
          </div>
        </div>
        <div className="px-4 py-3 border-b border-edge grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label={<>Account<HelpHint term="strategy_scope" /></>} hint="Any = applies to every account">
            <Select value={form.account_id} onChange={(e) => setF("account_id", e.target.value)}>
              <option value="">Any account</option>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </Select>
          </Field>
          <Field label="Signal source" hint="Any = applies to every channel">
            <Select value={form.source_id} onChange={(e) => setF("source_id", e.target.value)}>
              <option value="">Any source</option>
              {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </Field>
          <Field label="Label" hint="e.g. 'BE@TP2 arm'"><Input value={form.label} onChange={(e) => setF("label", e.target.value)} /></Field>
          <Field label="Enabled"><Toggle checked={form.enabled} onChange={(v) => setF("enabled", v)} label={form.enabled ? "on" : "off"} /></Field>
        </div>

        {/* pillar tabs */}
        <div className="px-4 pt-3 flex gap-1.5">
          {TABS.map(([id, label, Icon]) => (
            <button key={id} onClick={() => setTab(id)}
              className={`px-3 py-1.5 rounded-t-lg text-xs font-medium flex items-center gap-1.5 border-b-2 ${tab === id ? "border-beacon text-beacon" : "border-transparent text-muted hover:text-fg"}`}>
              <Icon className="w-3.5 h-3.5" /> {label}
            </button>
          ))}
        </div>
        <ErrorNote>{err}</ErrorNote>

        {tab === "entry" && (
          <div className="p-4 space-y-3">
            <p className="text-[11px] text-muted"><HelpHint term="entry_policy_help" /> How the entry order is placed — TTL for working orders and the chase-guard (#67). Empty ⇒ the global default. Source-agnostic, so future non-Telegram entry types plug in here.</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <Field label="Entry TTL (min)" hint="working-order expiry"><Input type="number" value={form.entry.ttl_minutes} onChange={(e) => setSub("entry", "ttl_minutes", e.target.value)} /></Field>
              <Field label="Chase tolerance (× |entry−SL|)" hint="how far past the level a MARKET hint may still fill"><Input type="number" step="0.05" value={form.entry.chase_tolerance_r} onChange={(e) => setSub("entry", "chase_tolerance_r", e.target.value)} /></Field>
              <Field label="Chase tolerance (× ATR)" hint="0 = disabled; larger of the two wins"><Input type="number" step="0.1" value={form.entry.chase_tolerance_atr} onChange={(e) => setSub("entry", "chase_tolerance_atr", e.target.value)} /></Field>
              <Field label="Beyond tolerance" hint="what to do when entry is too far to fill at market">
                <Select value={form.entry.beyond_tolerance} onChange={(e) => setSub("entry", "beyond_tolerance", e.target.value)}>
                  <option value="limit">rest as LIMIT</option><option value="market">fill at MARKET</option><option value="skip">skip</option>
                </Select></Field>
              <Field label="Max TP distance (× entry)" hint="drop parse-artifact TPs this far away"><Input type="number" step="0.05" value={form.entry.max_tp_distance_pct} onChange={(e) => setSub("entry", "max_tp_distance_pct", e.target.value)} /></Field>
              <Field label="Honor MARKET hint"><Toggle checked={form.entry.honor_market_hint} onChange={(v) => setSub("entry", "honor_market_hint", v)} label={form.entry.honor_market_hint ? "on" : "off"} /></Field>
            </div>
          </div>
        )}

        {tab === "filter" && (
          <div className="p-4 space-y-4">
            <p className="text-[11px] text-muted"><HelpHint term="filtration_help" /> Rules that can <b>skip</b>, <b>de-size</b>, or <b>up-size</b> a trade from Analytics / session / structure signals. Fail-open: a rule whose inputs aren't available yet is a no-op.</p>
            <div className="rounded-lg border border-edge p-3 space-y-3">
              <div className="flex items-center gap-2 text-sm font-medium">Trend alignment
                <Toggle checked={form.trend.enabled} onChange={(v) => setSub("trend", "enabled", v)} label={form.trend.enabled ? "on" : "off"} />
                <span className="text-[11px] text-muted">counter-trend entries held ~95% of losses (#48/#79)</span></div>
              <div className={`grid grid-cols-2 lg:grid-cols-4 gap-3 ${form.trend.enabled ? "" : "opacity-60"}`}>
                <Field label="Timeframe" hint="trend timeframe, e.g. 4h"><Input value={form.trend.timeframe} onChange={(e) => setSub("trend", "timeframe", e.target.value)} /></Field>
                <Field label="EMA period"><Input type="number" value={form.trend.ema_period} onChange={(e) => setSub("trend", "ema_period", Number(e.target.value))} /></Field>
                <Field label="Counter-trend action" hint="skip = reject · desize = trade smaller"><Select value={form.trend.mode} onChange={(e) => setSub("trend", "mode", e.target.value)}><option value="skip">skip</option><option value="desize">desize</option></Select></Field>
                <Field label="De-size factor" hint="counter-trend size × this (desize mode)"><Input type="number" step="0.05" value={form.trend.desize_factor} onChange={(e) => setSub("trend", "desize_factor", Number(e.target.value))} /></Field>
                <Field label="Min distance (ATR)" hint="#79 · price must be ≥ this many ATR beyond the EMA (skip the chop band)"><Input type="number" step="0.1" value={form.trend.min_dist_atr} onChange={(e) => setSub("trend", "min_dist_atr", Number(e.target.value))} /></Field>
                <Field label="Slope lookback (bars)" hint="#79 · bars back used to measure the EMA slope"><Input type="number" value={form.trend.slope_lookback ?? 10} onChange={(e) => setSub("trend", "slope_lookback", Number(e.target.value))} /></Field>
                <Field label="HTF concordance TF" hint="#79 · the timeframe that must agree when concordance is on">
                  <Select value={form.trend.htf_timeframe ?? "1h"} onChange={(e) => setSub("trend", "htf_timeframe", e.target.value)}>
                    {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </Select></Field>
              </div>
              <div className={`flex flex-wrap gap-x-8 gap-y-2 ${form.trend.enabled ? "" : "opacity-60"}`}>
                <label className="flex items-center gap-2 text-xs text-muted">Require EMA slope (#79)
                  <Toggle checked={form.trend.require_slope} onChange={(v) => setSub("trend", "require_slope", v)} /></label>
                <label className="flex items-center gap-2 text-xs text-muted">Require HTF concordance (#79)
                  <Toggle checked={form.trend.require_htf_concordance ?? false} onChange={(v) => setSub("trend", "require_htf_concordance", v)} /></label>
              </div>
            </div>
            <div className="rounded-lg border border-edge p-3 space-y-2">
              <div className="flex items-center justify-between"><span className="text-sm font-medium">Custom rules</span>
                <Button variant="ghost" onClick={addRule}><Plus className="w-3.5 h-3.5 inline -mt-0.5" /> Add rule</Button></div>
              {!form.rules.length ? <div className="text-[11px] text-muted">No custom rules. Add one, e.g. <i>when session_in [New York] → scale ×0.5</i>.</div> : (
                <div className="space-y-2">
                  {form.rules.map((r, i) => (
                    <div key={i} className="flex items-center gap-2 flex-wrap text-xs bg-panel2 rounded-lg px-2.5 py-2">
                      <Toggle checked={r.enabled !== false} onChange={(v) => setRule(i, { enabled: v })} />
                      <input placeholder="name" value={r.name || ""} onChange={(e) => setRule(i, { name: e.target.value })} className={`${INPUT} w-28`} />
                      <span className="text-muted">when</span>
                      <select value={r.when?.type || "session_in"} onChange={(e) => setRule(i, { when: e.target.value === "always" ? { type: "always" } : { type: "session_in", sessions: r.when?.sessions || [] } })} className={`${INPUT} w-32`}>
                        <option value="session_in">session in</option><option value="always">always</option>
                      </select>
                      {r.when?.type === "session_in" && (
                        <input placeholder="London, New York" value={(r.when.sessions || []).join(", ")}
                          onChange={(e) => setRule(i, { when: { type: "session_in", sessions: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) } })} className={`${INPUT} w-44`} />)}
                      <span className="text-muted">→</span>
                      <select value={r.action || "scale"} onChange={(e) => setRule(i, { action: e.target.value })} className={`${INPUT} w-24`}><option value="scale">scale</option><option value="skip">skip</option></select>
                      {r.action === "scale" && <input type="number" step="0.05" value={r.factor ?? 0.5} onChange={(e) => setRule(i, { factor: Number(e.target.value) })} className={`${INPUT} w-20`} />}
                      <button onClick={() => delRule(i)} className="ml-auto text-short"><Trash2 className="w-3.5 h-3.5" /></button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {tab === "exit" && (
          <div className="p-4 space-y-3">
            <p className="text-[11px] text-muted"><HelpHint term="exit_policy_help" /> The stop-loss ratchet + cancel-pending behaviour. Snapshotted at entry, so this trade's arm is frozen. No rules ⇒ the channel / global default.</p>
            <label className="flex items-center gap-2 text-sm">Cancel pending orders on stop
              <Toggle checked={form.exit.cancel_pending_on_stop} onChange={(v) => setSub("exit", "cancel_pending_on_stop", v)} /></label>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-muted">Presets:</span>
              {Object.keys(SL_PRESETS).map((n) => (
                <button key={n} onClick={() => setSub("exit", "sl_rules", SL_PRESETS[n].map((r) => ({ ...r })))}
                  className="text-[11px] px-2 py-0.5 rounded-full border border-edge text-muted hover:border-beacon hover:text-beacon">{n}</button>
              ))}
            </div>
            <SlRulesEditor rules={form.exit.sl_rules} onChange={(v) => setSub("exit", "sl_rules", v)} />
          </div>
        )}
      </Card>

      {/* Existing strategies */}
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Configured strategies <span className="text-muted font-normal">· most-specific scope wins</span></div>
        {!all.length ? <Empty>No strategies yet — every trade uses the global/source default. Create one above.</Empty> : (
          <Table minW={820}>
            <thead><tr className="border-b border-edge"><Th>Account</Th><Th>Source</Th><Th>Label</Th><Th>Pillars</Th><Th>State</Th><Th right>v</Th><Th right></Th></tr></thead>
            <tbody>
              {all.map((s) => (
                <tr key={s.id} className="border-b border-edge/60">
                  <Td>{acctName(s.account_id)}</Td>
                  <Td>{srcName(s.source_id)}</Td>
                  <Td>{s.label || <span className="text-muted">—</span>}</Td>
                  <Td><span className="flex gap-1">
                    {s.entry_policy && <Badge tone="beacon">entry</Badge>}
                    {s.entry_filters && (s.entry_filters.trend_alignment?.enabled || s.entry_filters.rules?.length) ? <Badge tone="violet">filter</Badge> : null}
                    {s.exit_policy?.sl_rules && <Badge tone="long">exit</Badge>}
                  </span></Td>
                  <Td><Badge tone={s.enabled ? "long" : "muted"}>{s.enabled ? "on" : "off"}</Badge></Td>
                  <Td right mono>{s.version}</Td>
                  <Td right>
                    <button onClick={() => editRow(s)} className="text-xs text-beacon hover:underline mr-3">edit</button>
                    <button onClick={() => del(s.id)} className="text-xs text-short hover:underline"><Trash2 className="w-3 h-3 inline" /></button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
