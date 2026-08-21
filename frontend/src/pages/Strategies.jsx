import { useEffect, useState } from "react";
import { GitBranch, Trash2, LogIn, Filter, LogOut } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Field, Input, Select, Toggle, Button, ErrorNote, ConfigRow } from "../components/form";
import SlRulesEditor from "../components/SlRulesEditor";
import StagedEntryEditor from "../components/StagedEntryEditor";
import LadderEditor, { DEFAULT_LADDER } from "../components/LadderEditor";
import EntryFilterRules from "../components/EntryFilterRules";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";

// Trend Alignment is stored in the legacy entry_filters.trend_alignment block so
// the executor's trend path is unchanged; the UI presents it as one rule among the
// unified list. These map the block <-> a trend_alignment rule on load/save.
const num = (v) => (v === "" || v == null ? undefined : Number(v));
const trendBlockToRule = (b) => ({
  enabled: b.enabled !== false, name: "",
  when: { type: "trend_alignment", timeframe: b.timeframe ?? "4h", ema_period: b.ema_period ?? 200,
          require_slope: b.require_slope !== false, slope_lookback: b.slope_lookback ?? 10,
          min_dist_atr: b.min_dist_atr ?? 0.5, require_htf_concordance: !!b.require_htf_concordance,
          htf_timeframe: b.htf_timeframe ?? "1h" },
  action: b.mode === "skip" ? "skip" : "scale", factor: b.desize_factor ?? 0.25,
});
const trendRuleToBlock = (r) => ({
  enabled: r.enabled !== false, timeframe: r.when.timeframe ?? "4h", ema_period: num(r.when.ema_period) ?? 200,
  mode: r.action === "skip" ? "skip" : "desize", desize_factor: num(r.factor) ?? 0.25,
  require_slope: r.when.require_slope !== false, slope_lookback: num(r.when.slope_lookback) ?? 10,
  min_dist_atr: num(r.when.min_dist_atr) ?? 0.5, require_htf_concordance: !!r.when.require_htf_concordance,
  htf_timeframe: r.when.htf_timeframe ?? "1h",
});

// Mirrors beacon_core.execution.staging.DEFAULT_STAGED (keep in sync). Drives the
// #129 confirmation-staged entry; inert unless entry_style === "staged".
// #250 froze the thirteen tuning knobs that used to live here as engine constants
// — nobody had ever changed one — leaving only the two #158 order-age brakes.
const STAGED_DEFAULTS = {
  deployed_ttl_minutes: 0, max_entry_age_minutes: 0,
};

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
  // #251: named for what it does. `points` is raw INSTRUMENT PRICE, so 30 on
  // gold is a $30 move — about 2.5x a typical channel stop, not 3 pips.
  "Tighten: +$30 move → BE": [{ trigger: { type: "price_move", points: 30 }, action: mv("entry") }, { trigger: tpH(2), action: mv("previous_tp") }],
  "Early BE @ 0.6R (hold)": [{ trigger: { type: "be_lock_at_r", r: 0.6 }, action: mv("entry") }],   // #109
};
// What each entry setting falls back to when you switch it ON, and the value the
// executor uses while it is OFF. OFF means NOT SET: the key is absent from
// entry_policy, so the #104 cascade fills it from the next-less-specific row.
// The old page wrote all six keys on every save, which silently made every
// strategy a full override and left the cascade with nothing to inherit.
const ENTRY_DEFAULTS = {
  ttl_minutes: 60, chase_tolerance_r: 0.25, chase_tolerance_atr: 0,
  max_tp_distance_pct: 0.5, beyond_tolerance: "limit", honor_market_hint: true,
  sl_distance: 3,
};
const BLANK = () => ({
  id: null, account_id: "", source_id: "", label: "", enabled: true,
  entry: { ...ENTRY_DEFAULTS, entry_style: "", ladder: DEFAULT_LADDER.map((r) => ({ ...r })),
           staged: { ...STAGED_DEFAULTS } },
  entryOn: {},        // which entry settings this strategy actually sets
  rules: [],          // unified entry-filter rules (Trend Alignment / ADX Regime / Session)
  exit: { sl_rules: [], cancel_pending_on_stop: true },
  exitOn: {},         // same, for the exit pillar
});

// #251: every distance field on this page is in raw INSTRUMENT PRICE UNITS, and
// none of them used to say so — "30 pts" reads as 3 pips and means a $30 move on
// gold. `value_per_point` is money per 1.0 price move per 1.0 lot; it happens to
// be 1.0 for XAUUSD, but that is a property of the instrument, not a rule, so the
// echo is computed rather than assumed.
const DEFAULT_UNIT = { symbol: "price", valuePerPoint: 1 };
const unitLabel = (u) => `${u.symbol} price, 1 = $${Number(u.valuePerPoint).toFixed(2)}`;
const priceMoveNote = (v, u) => {
  const n = Number(v);
  if (v === "" || v == null || !isFinite(n) || n <= 0) return null;
  return `${u.symbol} moves ${n.toFixed(2)} → about $${(n * Number(u.valuePerPoint)).toFixed(2)} per 1.00 lot`;
};
const TABS = [["entry", "Entry Strategy", LogIn], ["filter", "Entry Filtration", Filter], ["exit", "Exit Strategy", LogOut]];

export default function Strategies() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [unit, setUnit] = useState(DEFAULT_UNIT);       // #251 price-unit label
  const [all, setAll] = useState([]);
  const [form, setForm] = useState(BLANK());
  const [tab, setTab] = useState("entry");
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = () => api.strategies().then(setAll).catch((e) => setErr(e.message));
  useEffect(() => {
    api.sources().then(setSources).catch((e) => setErr(e.message));
    api.accounts().then(setAccounts).catch((e) => setErr(e.message));
    // One instrument today (XAUUSD). If that ever stops being true the label
    // falls back to the neutral "price" rather than naming the wrong symbol.
    api.symbols().then((rows) => {
      const names = [...new Set((rows || []).map((r) => r.internal_symbol))];
      if (names.length === 1) setUnit({ symbol: names[0], valuePerPoint: rows[0].value_per_point ?? 1 });
    }).catch(() => {});
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
    // A key PRESENT in the stored policy is a key this strategy sets; anything
    // absent is inherited and stays switched off with its default behind it.
    const set = (k) => ep[k] !== undefined && ep[k] !== null && ep[k] !== "";
    const entryOn = {};
    for (const k of ["ttl_minutes", "chase_tolerance_r", "chase_tolerance_atr",
                     "max_tp_distance_pct", "beyond_tolerance", "honor_market_hint",
                     "sl_distance"]) {
      if (set(k)) entryOn[k] = true;
    }
    if (ep.entry_style === "staged") entryOn.staged = true;
    setForm({
      id: row.id, account_id: row.account_id ?? "", source_id: row.source_id ?? "",
      label: row.label || "", enabled: row.enabled,
      entry: { ...ENTRY_DEFAULTS,
               ...Object.fromEntries(Object.entries(ep)
                 .filter(([, v]) => v !== null && v !== "")
                 .map(([k, v]) => [k, v])),
               entry_style: ep.entry_style || "",
               ladder: Array.isArray(ep.ladder) && ep.ladder.length
                 ? ep.ladder.map((r) => ({ ...r })) : DEFAULT_LADDER.map((r) => ({ ...r })),
               staged: { ...STAGED_DEFAULTS, ...(ep.staged || {}) } },
      entryOn,
      // Unified rule list: the legacy trend_alignment block becomes the first rule.
      rules: [...(ef.trend_alignment ? [trendBlockToRule(ef.trend_alignment)] : []),
              ...(Array.isArray(ef.rules) ? ef.rules : [])],
      exit: { sl_rules: Array.isArray(xp.sl_rules) ? xp.sl_rules : [],
              cancel_pending_on_stop: xp.cancel_pending_on_stop !== false },
      exitOn: { sl_rules: !!(xp.sl_rules && xp.sl_rules.length),
                cancel_pending_on_stop: xp.cancel_pending_on_stop === false },
    });
  };
  const newAt = () => { setForm(BLANK()); setTab("entry"); setSaved(false); };

  const save = async () => {
    setErr(null); setSaved(false);
    const xon = form.exitOn || {};
    // [] / off = inherit the channel or global default rather than pin an empty set.
    const sl_rules = xon.sl_rules && form.exit.sl_rules.length ? form.exit.sl_rules : null;
    // ONLY the settings that are switched on. A key left off is absent from the
    // payload, so it inherits down the #104 cascade instead of being pinned to a
    // copy of the default.
    const on = form.entryOn || {};
    const entry_policy = {};
    for (const k of ["ttl_minutes", "chase_tolerance_r", "chase_tolerance_atr",
                     "max_tp_distance_pct", "sl_distance"]) {
      if (on[k]) entry_policy[k] = num(form.entry[k]);
    }
    if (on.beyond_tolerance) entry_policy.beyond_tolerance = form.entry.beyond_tolerance;
    if (on.honor_market_hint) entry_policy.honor_market_hint = !!form.entry.honor_market_hint;
    if (on.staged) {                       // #250: staged entry is on/off + a ladder
      entry_policy.entry_style = "staged";
      entry_policy.ladder = form.entry.ladder;
      const staged = {};
      for (const [k, val] of Object.entries(form.entry.staged || {})) {
        const nv = num(val);
        if (nv !== undefined) staged[k] = nv;
      }
      entry_policy.staged = staged;
    }
    // Split the unified rule list back into storage: the Trend Alignment rule ->
    // the legacy trend_alignment block (executor path unchanged); the rest ->
    // entry_filters.rules. ADX rules get their `when` cleaned (min_adx coerced).
    const trendRule = form.rules.find((r) => r.when?.type === "trend_alignment");
    const otherRules = form.rules.filter((r) => r.when?.type !== "trend_alignment").map((r) => {
      if (r.when?.type !== "adx_regime") return r;
      const w = { type: "adx_regime", timeframe: r.when.timeframe || "4h", trending: r.when.trending !== false };
      const ma = num(r.when.min_adx); if (ma !== undefined) w.min_adx = ma;
      return { ...r, when: w };
    });
    const body = {
      account_id: form.account_id === "" ? null : form.account_id,
      source_id: form.source_id === "" ? null : form.source_id,
      label: form.label || null, enabled: form.enabled,
      entry_policy,
      entry_filters: { trend_alignment: trendRule ? trendRuleToBlock(trendRule) : { enabled: false },
                       rules: otherRules },
      // Only sent when switched on: leaving it off inherits (the engine default
      // is true), rather than writing a copy of the default onto every strategy.
      exit_policy: { sl_rules, ...(xon.cancel_pending_on_stop
        ? { cancel_pending_on_stop: !!form.exit.cancel_pending_on_stop } : {}) },
    };
    try { const r = await api.saveStrategy(body); setSaved(true); await load(); editRow(r); }
    catch (e) { setErr(e.message); }
  };
  const del = async (id) => { try { await api.deleteStrategy(id); if (form.id === id) newAt(); await load(); } catch (e) { setErr(e.message); } };

  const scopeLabel = `${acctName(form.account_id)} · ${srcName(form.source_id)}`;
  return (
    <div className="space-y-5">
      <ResolvePreview accounts={accounts} sources={sources} />
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
          <div className="p-4 space-y-2">
            <p className="text-[11px] text-muted"><HelpHint term="entry_policy_help" /> How the entry order is placed.
              Every setting is <b>off</b> until you switch it on, and off means <b>not set</b> — the value is
              inherited from the next-less-specific strategy, down to the (Any, Any) row. Turning one on here
              overrides it for this scope only.</p>

            <ConfigRow label="Staged entry — the ladder"
                       hint="deploy the signal in rungs instead of all at once (#250)"
                       summary="off · single shot"
                       active={!!form.entryOn.staged}
                       onChange={(v) => setSub("entryOn", "staged", v)}>
              <div className="space-y-3 pt-2">
                <LadderEditor rows={form.entry.ladder}
                              onChange={(rows) => setSub("entry", "ladder", rows)} />
                <StagedEntryEditor value={form.entry.staged}
                  onChange={(k, val) => setSub("entry", "staged", { ...form.entry.staged, [k]: val })} />
              </div>
            </ConfigRow>

            {/* #249: measured from the signal's FAR entry edge (entry_to) — two
                thirds of signals are a zone, and a stop measured from the near
                edge sits inside it and gets dropped as "sl on wrong side of
                entry". Applied before sizing, so a tighter stop trades a LARGER
                lot at the same cash risk. Staged and single-shot alike. */}
            <ConfigRow label="Modify SL — trade our own stop distance"
                       hint={"replaces the channel's stop · " + unitLabel(unit)}
                       summary="off · the channel's stop"
                       active={!!form.entryOn.sl_distance}
                       onChange={(v) => setSub("entryOn", "sl_distance", v)}>
              <div className="pt-2 space-y-1.5">
                <div className="max-w-[16rem]">
                  <Input type="number" step="0.1" min="0" value={form.entry.sl_distance}
                         onChange={(e) => setSub("entry", "sl_distance", e.target.value)} />
                </div>
                <p className="text-[11px] text-warn">
                  {priceMoveNote(form.entry.sl_distance, unit)
                    ? <>Stop set {Number(form.entry.sl_distance).toFixed(2)} from the signal's entry
                        ({priceMoveNote(form.entry.sl_distance, unit)}) — the lot is sized from it, so a
                        tighter stop trades a <b>bigger position</b> at the same cash risk.</>
                    : "Enter a distance greater than zero."}
                </p>
              </div>
            </ConfigRow>

            <ConfigRow label="Entry TTL" hint="how long a working order may rest, in minutes"
                       summary="off · inherited"
                       active={!!form.entryOn.ttl_minutes}
                       onChange={(v) => setSub("entryOn", "ttl_minutes", v)}>
              <div className="max-w-[16rem] pt-2">
                <Input type="number" value={form.entry.ttl_minutes}
                       onChange={(e) => setSub("entry", "ttl_minutes", e.target.value)} />
              </div>
            </ConfigRow>

            <ConfigRow label="Chase tolerance (× |entry−SL|)"
                       hint="how far past the level a MARKET hint may still fill (#67)"
                       summary="off · inherited"
                       active={!!form.entryOn.chase_tolerance_r}
                       onChange={(v) => setSub("entryOn", "chase_tolerance_r", v)}>
              <div className="max-w-[16rem] pt-2">
                <Input type="number" step="0.05" value={form.entry.chase_tolerance_r}
                       onChange={(e) => setSub("entry", "chase_tolerance_r", e.target.value)} />
              </div>
            </ConfigRow>

            <ConfigRow label="Chase tolerance (× ATR)" hint="the larger of the two tolerances wins"
                       summary="off · inherited"
                       active={!!form.entryOn.chase_tolerance_atr}
                       onChange={(v) => setSub("entryOn", "chase_tolerance_atr", v)}>
              <div className="max-w-[16rem] pt-2">
                <Input type="number" step="0.1" value={form.entry.chase_tolerance_atr}
                       onChange={(e) => setSub("entry", "chase_tolerance_atr", e.target.value)} />
              </div>
            </ConfigRow>

            <ConfigRow label="Beyond tolerance" hint="what to do when the entry is too far to fill at market"
                       summary="off · inherited"
                       active={!!form.entryOn.beyond_tolerance}
                       onChange={(v) => setSub("entryOn", "beyond_tolerance", v)}>
              <div className="max-w-[16rem] pt-2">
                <Select value={form.entry.beyond_tolerance}
                        onChange={(e) => setSub("entry", "beyond_tolerance", e.target.value)}>
                  <option value="limit">rest as LIMIT</option>
                  <option value="market">fill at MARKET</option>
                  <option value="skip">skip the trade</option>
                </Select>
              </div>
            </ConfigRow>

            <ConfigRow label="Max TP distance (× entry)" hint="drop parse-artifact TPs this far from entry"
                       summary="off · inherited"
                       active={!!form.entryOn.max_tp_distance_pct}
                       onChange={(v) => setSub("entryOn", "max_tp_distance_pct", v)}>
              <div className="max-w-[16rem] pt-2">
                <Input type="number" step="0.05" value={form.entry.max_tp_distance_pct}
                       onChange={(e) => setSub("entry", "max_tp_distance_pct", e.target.value)} />
              </div>
            </ConfigRow>

            <ConfigRow label="Honor MARKET hint" hint={"a signal that says BUY NOW may fill at market"}
                       summary="off · inherited"
                       active={!!form.entryOn.honor_market_hint}
                       onChange={(v) => setSub("entryOn", "honor_market_hint", v)}>
              <div className="pt-2">
                <Toggle checked={!!form.entry.honor_market_hint}
                        onChange={(v) => setSub("entry", "honor_market_hint", v)}
                        label={form.entry.honor_market_hint ? "honor it" : "always rest a LIMIT"} />
              </div>
            </ConfigRow>
          </div>
        )}

        {tab === "filter" && (
          <div className="p-4 space-y-3">
            <p className="text-[11px] text-muted"><HelpHint term="filtration_help" /> Nothing filters until you add a rule.
              Each rule can <b>skip</b> or <b>scale</b> (de-size ×factor) a trade, and is either <b>shadow</b> — computed
              and logged as <code>filter_shadow</code>, changing nothing — or <b>live</b>. New Indicator rules start in
              shadow on purpose. Fail-open: a rule whose inputs aren't available is a no-op. Most-specific scope wins.</p>
            <EntryFilterRules rules={form.rules} onChange={(rules) => setF("rules", rules)} />
          </div>
        )}

        {tab === "exit" && (
          <div className="p-4 space-y-2">
            <p className="text-[11px] text-muted"><HelpHint term="exit_policy_help" /> The stop-loss ratchet and what
              happens to pending orders. Snapshotted at entry, so a running trade's arm stays frozen. Off means
              inherited — the channel or global default.</p>

            <ConfigRow label="Stop-loss ratchet rules"
                       hint="move the stop as targets are hit — the engine only ever tightens"
                       summary="off · inherited"
                       active={!!form.exitOn.sl_rules}
                       onChange={(v) => setSub("exitOn", "sl_rules", v)}>
              <div className="space-y-2 pt-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs text-muted">Presets:</span>
                  {Object.keys(SL_PRESETS).map((n) => (
                    <button key={n} onClick={() => setSub("exit", "sl_rules", SL_PRESETS[n].map((r) => ({ ...r })))}
                      className="text-[11px] px-2 py-0.5 rounded-full border border-edge text-muted hover:border-beacon hover:text-beacon">{n}</button>
                  ))}
                </div>
                <SlRulesEditor rules={form.exit.sl_rules} unit={unit}
                               onChange={(v) => setSub("exit", "sl_rules", v)} />
              </div>
            </ConfigRow>

            <ConfigRow label="Cancel pending orders on stop"
                       hint="when the stop takes one leg, pull the rest of the fanout"
                       summary="off · inherited (on)"
                       active={!!form.exitOn.cancel_pending_on_stop}
                       onChange={(v) => setSub("exitOn", "cancel_pending_on_stop", v)}>
              <div className="pt-2">
                <Toggle checked={!!form.exit.cancel_pending_on_stop}
                        onChange={(v) => setSub("exit", "cancel_pending_on_stop", v)}
                        label={form.exit.cancel_pending_on_stop ? "cancel them" : "leave them working"} />
              </div>
            </ConfigRow>
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
                    {s.entry_policy?.entry_style === "staged" && <Badge tone="warn">staged</Badge>}
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


// GET /strategies/resolve — the cascade PREVIEW (#84). Served since #84 with no
// caller, which is the one place it was most needed: the whole point of this
// page is a four-level cascade ((acct,src) > (acct,*) > (*,src) > (*,*)) with
// per-pillar inheritance, and until now the only way to know which row actually
// governs a given trade was to simulate the precedence in your head.
//
// It answers the question the editor cannot: not "what did I write" but "what
// will RUN". `null` is a real answer — it means no enabled row matches and the
// global defaults apply.
function ResolvePreview({ accounts, sources }) {
  const [acct, setAcct] = useState("");
  const [src, setSrc] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!acct || !src) { setRes(null); return; }
    let alive = true;
    api.resolveStrategy(acct, src)
      .then(d => alive && (setRes(d), setErr(null)))
      .catch(e => alive && setErr(e.message));
    return () => { alive = false; };
  }, [acct, src]);

  const r = res?.resolved;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        What actually runs
        <span className="text-muted font-normal text-xs">
          · resolve the cascade for one (account, source)</span>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Pick a pair and this shows the <b>most-specific enabled</b> strategy a trade on it would
        run under — the row that wins <span className="num">(acct,src) &gt; (acct,*) &gt; (*,src)
        &gt; (*,*)</span>. Answers "what will run", which is not the same question as "what did
        I write".
      </div>
      <div className="px-4 py-3 flex items-end gap-3 flex-wrap">
        <Field label="Account">
          <Select value={acct} onChange={e => setAcct(e.target.value)}>
            <option value="">choose…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select>
        </Field>
        <Field label="Source">
          <Select value={src} onChange={e => setSrc(e.target.value)}>
            <option value="">choose…</option>
            {sources.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}
          </Select>
        </Field>
        {err && <span className="text-[11px] text-short">{err}</span>}
      </div>
      {acct && src && res && (
        <div className="px-4 pb-4 text-[11px]">
          {!r ? (
            <span className="text-warn">No enabled strategy matches this pair — the global
              defaults apply.</span>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="beacon">{r.label || `strategy #${r.id}`}</Badge>
              <span className="num text-muted">
                scope ({r.account_id ?? "any"}, {r.source_id ?? "any"}) · v{r.version}
              </span>
              {["entry_policy", "entry_filters", "exit_policy"].map(k => (
                <Badge key={k} tone={r[k] && Object.keys(r[k]).length ? "muted" : "warn"}>
                  {k.replace("_", " ")}: {r[k] && Object.keys(r[k]).length ? "set" : "inherited"}
                </Badge>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
