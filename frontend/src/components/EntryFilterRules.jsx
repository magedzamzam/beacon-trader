import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Field, Input, Select, Toggle, Button } from "./form";
import { api } from "../lib/api";

/**
 * EntryFilterRules (#84/#127) — one unified list for every entry-filtration rule.
 * Add a rule, pick its type (Trend Alignment · ADX Regime · Session), set its
 * condition + action (skip / scale ×factor). Scalable: a new filter is one entry
 * in RULE_TYPES below + a matching evaluator in execution.strategy.apply_filter_rules.
 *
 * `rules` is the flat array stored under entry_filters.rules. NOTE: the parent
 * (Strategies) maps a `trend_alignment` rule to/from the legacy
 * entry_filters.trend_alignment block on save/load, so the executor's trend path
 * is unchanged — this component only owns the UI shape.
 */
const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

// The rule-type registry. `blank` is the default `when` block for a new rule of
// that type; `fields` renders the type-specific inputs. Add a type here to extend.
export const RULE_TYPES = {
  trend_alignment: {
    label: "Trend Alignment",
    hint: "skip / de-size counter-trend entries — held ~95% of losses (#48/#79)",
    blank: { type: "trend_alignment", timeframe: "4h", ema_period: 200, require_slope: true,
             slope_lookback: 10, min_dist_atr: 0.5, require_htf_concordance: false, htf_timeframe: "1h" },
  },
  adx_regime: {
    label: "ADX Regime",
    hint: "act on the per-TF ADX trend state — 4h-trending is the top edge-killer (#127)",
    blank: { type: "adx_regime", timeframe: "4h", trending: true, min_adx: "" },
  },
  session_in: {
    label: "Session",
    hint: "act only during the named trading sessions",
    blank: { type: "session_in", sessions: [] },
  },
  mc_probability: {
    label: "Monte Carlo (geometry)",
    hint: "act on what the SL/TP layout is worth with NO channel skill — a HIGH P(win) means a far stop and a near target, not an edge. SHADOW: inert until graduated.",
    blank: { type: "mc_probability", max_expected_r: 0, min_p_win: "", max_p_win: "", min_rr: "" },
  },
  turtle_signal: {
    label: "Turtle (Donchian)",
    hint: "act on whether the 55-bar breakout system agrees with the channel's direction. SHADOW: inert until graduated.",
    blank: { type: "turtle_signal", agrees: false, position: "", variant: "signal" },
  },
  indicator: {
    label: "Indicator (any)",
    hint: "gate on ANY registry indicator / timeframe / field (#167). Ships in SHADOW mode: it is computed and logged as filter_shadow, and changes nothing until you switch it to LIVE — which needs N≥30 direction-folded, a 90% CI clear of the base rate, and replication across ≥2 epochs.",
    blank: { type: "indicator", id: "rsi", timeframe: "1h", field: "value", op: "gte", value: 70 },
    defaultMode: "shadow",
  },
};

// `op` values understood by execution.strategy._match_indicator. `arity` drives
// which value input is rendered: none / one scalar / a [lo, hi] pair.
const OPS = [
  { op: "gt", label: ">", arity: 1 }, { op: "gte", label: "≥", arity: 1 },
  { op: "lt", label: "<", arity: 1 }, { op: "lte", label: "≤", arity: 1 },
  { op: "eq", label: "=", arity: 1 }, { op: "ne", label: "≠", arity: 1 },
  { op: "between", label: "between", arity: 2 },
  { op: "outside", label: "outside", arity: 2 },
  { op: "is_true", label: "is true", arity: 0 },
  { op: "is_false", label: "is false", arity: 0 },
];
const opArity = (op) => (OPS.find((o) => o.op === op)?.arity ?? 1);

// A rule with no explicit `mode` inherits the evaluator's type-dependent default:
// SHADOW for the generic indicator gate (a new rule must not be able to change
// live behaviour by existing), LIVE for the types that predate it and are already
// deployed. Mirrors execution.strategy.rule_mode — keep the two in step.
const effectiveMode = (r) => r?.mode || RULE_TYPES[r?.when?.type]?.defaultMode || "live";

const newRule = (type) => ({
  enabled: true, name: "", when: { ...RULE_TYPES[type].blank },
  action: "scale", factor: 0.5, mode: RULE_TYPES[type].defaultMode || "live",
});

/** The generic gate (#167): pick any registry indicator, timeframe, output field,
 *  operator, and what to compare against. Nothing here is per-indicator code —
 *  the option lists come from /ta/catalog, so a newly registered indicator shows
 *  up here on its own. */
function IndicatorFields({ when, set, setMany, catalog }) {
  const inds = catalog?.indicators || [];
  const spec = inds.find((i) => i.id === when.id);
  const fields = spec?.outputs?.length ? spec.outputs : ["value"];
  const arity = opArity(when.op ?? "gte");
  const refKind = when.ref == null || when.ref === "" ? "value"
    : typeof when.ref === "string" ? "price" : "indicator";
  const refSpec = inds.find((i) => i.id === when.ref?.id);
  const pair = Array.isArray(when.value) ? when.value : ["", ""];
  const setPair = (i, v) => set("value", i === 0 ? [v, pair[1]] : [pair[0], v]);
  const setRefKind = (k) => set("ref", k === "value" ? null : k === "price" ? "price"
    : { id: when.id, timeframe: when.timeframe, field: "value" });

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Field label="Indicator" hint={inds.length ? `${inds.length} in the registry` : "loading catalog…"}>
          {/* one setMany, not three sets: each `set` rebuilds `when` from the same
              render's copy, so chained calls would clobber each other. */}
          <Select value={when.id ?? ""}
            onChange={(e) => setMany({ id: e.target.value, field: "value", params: undefined })}>
            {!spec && when.id ? <option value={when.id}>{when.id}</option> : null}
            {inds.map((i) => <option key={i.id} value={i.id}>{i.label} · {i.category}</option>)}
          </Select></Field>
        <Field label="Timeframe" hint="bars are fetched once per TF, only when a rule needs them">
          <Select value={when.timeframe ?? "4h"} onChange={(e) => set("timeframe", e.target.value)}>
            {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select></Field>
        <Field label="Field" hint="which output of this indicator">
          <Select value={when.field ?? "value"} onChange={(e) => set("field", e.target.value)}>
            {fields.map((f) => <option key={f} value={f}>{f}</option>)}
          </Select></Field>
        <Field label="Operator">
          <Select value={when.op ?? "gte"} onChange={(e) => set("op", e.target.value)}>
            {OPS.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
          </Select></Field>
      </div>
      {arity === 2 && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label="Low"><Input type="number" step="any" value={pair[0] ?? ""} onChange={(e) => setPair(0, e.target.value)} /></Field>
          <Field label="High"><Input type="number" step="any" value={pair[1] ?? ""} onChange={(e) => setPair(1, e.target.value)} /></Field>
        </div>
      )}
      {arity === 1 && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label="Compare to" hint="a fixed number, the live price, or another indicator's field">
            <Select value={refKind} onChange={(e) => setRefKind(e.target.value)}>
              <option value="value">a fixed value</option>
              <option value="price">the live price</option>
              <option value="indicator">another indicator</option>
            </Select></Field>
          {refKind === "value" && (
            <Field label="Value" hint="numbers compare numerically; text fields (cross, trend) compare as text">
              <Input value={when.value ?? ""} onChange={(e) => set("value", e.target.value)} /></Field>
          )}
          {refKind === "indicator" && (
            <>
              <Field label="Ref indicator">
                <Select value={when.ref?.id ?? ""} onChange={(e) => set("ref", { ...when.ref, id: e.target.value, field: "value" })}>
                  {inds.map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
                </Select></Field>
              <Field label="Ref timeframe" hint="blank = same TF as above">
                <Select value={when.ref?.timeframe ?? ""} onChange={(e) => set("ref", { ...when.ref, timeframe: e.target.value || undefined })}>
                  <option value="">same</option>
                  {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
                </Select></Field>
              <Field label="Ref field">
                <Select value={when.ref?.field ?? "value"} onChange={(e) => set("ref", { ...when.ref, field: e.target.value })}>
                  {(refSpec?.outputs?.length ? refSpec.outputs : ["value"]).map((f) => <option key={f} value={f}>{f}</option>)}
                </Select></Field>
            </>
          )}
        </div>
      )}
      {!!spec?.params?.length && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {spec.params.map((p) => (
            <Field key={p.name} label={p.name} hint={`default ${p.default} · ${p.min}–${p.max}`}>
              <Input type="number" step={p.type === "float" ? "any" : "1"}
                value={when.params?.[p.name] ?? p.default}
                onChange={(e) => set("params", { ...(when.params || {}), [p.name]: Number(e.target.value) })} />
            </Field>
          ))}
        </div>
      )}
    </div>
  );
}

function RuleFields({ when, set, setMany, catalog }) {
  const t = when?.type;
  if (t === "indicator") return <IndicatorFields when={when} set={set} setMany={setMany} catalog={catalog} />;
  if (t === "trend_alignment") {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Field label="Timeframe" hint="trend TF, e.g. 4h"><Input value={when.timeframe ?? "4h"} onChange={(e) => set("timeframe", e.target.value)} /></Field>
        <Field label="EMA period"><Input type="number" value={when.ema_period ?? 200} onChange={(e) => set("ema_period", Number(e.target.value))} /></Field>
        <Field label="Min distance (ATR)" hint="#79 · price ≥ this many ATR beyond the EMA"><Input type="number" step="0.1" value={when.min_dist_atr ?? 0.5} onChange={(e) => set("min_dist_atr", Number(e.target.value))} /></Field>
        <Field label="Slope lookback (bars)" hint="#79 · bars back to measure EMA slope"><Input type="number" value={when.slope_lookback ?? 10} onChange={(e) => set("slope_lookback", Number(e.target.value))} /></Field>
        <Field label="HTF concordance TF" hint="#79 · TF that must agree when concordance is on">
          <Select value={when.htf_timeframe ?? "1h"} onChange={(e) => set("htf_timeframe", e.target.value)}>
            {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select></Field>
        <label className="flex items-center gap-2 text-xs text-muted mt-5">Require EMA slope
          <Toggle checked={when.require_slope !== false} onChange={(v) => set("require_slope", v)} /></label>
        <label className="flex items-center gap-2 text-xs text-muted mt-5">Require HTF concordance
          <Toggle checked={!!when.require_htf_concordance} onChange={(v) => set("require_htf_concordance", v)} /></label>
      </div>
    );
  }
  if (t === "adx_regime") {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <Field label="Timeframe" hint="which TF's ADX (e.g. 4h)">
          <Select value={when.timeframe ?? "4h"} onChange={(e) => set("timeframe", e.target.value)}>
            {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select></Field>
        <Field label="Regime" hint="match when the TF's ADX says…">
          <Select value={when.trending === false ? "ranging" : "trending"} onChange={(e) => set("trending", e.target.value === "trending")}>
            <option value="trending">trending (ADX &gt; 25)</option><option value="ranging">ranging</option>
          </Select></Field>
        <Field label="Min ADX (optional)" hint="also require ADX ≥ this"><Input type="number" step="1" value={when.min_adx ?? ""} onChange={(e) => set("min_adx", e.target.value)} /></Field>
      </div>
    );
  }
  if (t === "mc_probability") {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <Field label="Max expected R" hint="match when the null's E[R] is ≤ this. 0 = 'loses money without channel skill'">
          <Input type="number" step="0.05" value={when.max_expected_r ?? ""} onChange={(e) => set("max_expected_r", e.target.value)} /></Field>
        <Field label="Min expected R" hint="match when E[R] ≥ this">
          <Input type="number" step="0.05" value={when.min_expected_r ?? ""} onChange={(e) => set("min_expected_r", e.target.value)} /></Field>
        <Field label="Min P(win) geometry" hint="0–1 · P(TP1 before SL) with no skill assumed">
          <Input type="number" step="0.05" value={when.min_p_win ?? ""} onChange={(e) => set("min_p_win", e.target.value)} /></Field>
        <Field label="Max P(win) geometry" hint="0–1">
          <Input type="number" step="0.05" value={when.max_p_win ?? ""} onChange={(e) => set("max_p_win", e.target.value)} /></Field>
        <Field label="Min RR to TP1" hint="reward:risk of the posted levels">
          <Input type="number" step="0.1" value={when.min_rr ?? ""} onChange={(e) => set("min_rr", e.target.value)} /></Field>
        <Field label="Max RR to TP1" hint="reward:risk of the posted levels">
          <Input type="number" step="0.1" value={when.max_rr ?? ""} onChange={(e) => set("max_rr", e.target.value)} /></Field>
      </div>
    );
  }
  if (t === "turtle_signal") {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <Field label="Agreement" hint="match when the breakout system does / does not back this direction">
          <Select value={when.agrees === true ? "yes" : when.agrees === false ? "no" : "any"}
            onChange={(e) => set("agrees", e.target.value === "any" ? null : e.target.value === "yes")}>
            <option value="any">any</option><option value="yes">agrees with signal</option>
            <option value="no">disagrees with signal</option>
          </Select></Field>
        <Field label="Position" hint="match only when the Turtle holds this side">
          <Select value={when.position ?? ""} onChange={(e) => set("position", e.target.value)}>
            <option value="">any</option><option value="long">long</option>
            <option value="short">short</option><option value="flat">flat</option>
          </Select></Field>
        <Field label="Variant" hint="reference = the source script (never goes flat); flat = its documented intent">
          <Select value={when.variant ?? "signal"} onChange={(e) => set("variant", e.target.value)}>
            <option value="signal">reference (stop-and-reverse)</option>
            <option value="signal_flat">exits to flat</option>
          </Select></Field>
      </div>
    );
  }
  if (t === "session_in") {
    return (
      <Field label="Sessions" hint="comma-separated, e.g. London, New York">
        <Input value={(when.sessions || []).join(", ")}
          onChange={(e) => set("sessions", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
      </Field>
    );
  }
  return null;
}

export default function EntryFilterRules({ rules, onChange }) {
  const list = rules || [];
  const [catalog, setCatalog] = useState(null);
  // Only the generic indicator gate needs the registry; don't pull it otherwise.
  const needsCatalog = list.some((r) => r?.when?.type === "indicator");
  useEffect(() => {
    if (!needsCatalog || catalog) return;
    api.taCatalog().then(setCatalog).catch(() => setCatalog({ indicators: [] }));
  }, [needsCatalog, catalog]);

  const patch = (i, r) => onChange(list.map((x, j) => (j === i ? r : x)));
  const patchWhen = (i, k, v) => patch(i, { ...list[i], when: { ...list[i].when, [k]: v } });
  const patchWhenMany = (i, obj) => patch(i, { ...list[i], when: { ...list[i].when, ...obj } });
  const setType = (i, type) => patch(i, {
    ...list[i], when: { ...RULE_TYPES[type].blank },
    mode: RULE_TYPES[type].defaultMode || "live",
  });
  const live = list.filter((r) => r?.enabled !== false && effectiveMode(r) === "live").length;
  const shadow = list.filter((r) => r?.enabled !== false && effectiveMode(r) === "shadow").length;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-muted">Add a rule:</span>
        {Object.entries(RULE_TYPES).map(([type, { label }]) => (
          <Button key={type} variant="ghost" onClick={() => onChange([...list, newRule(type)])}>
            <Plus className="w-3.5 h-3.5 inline -mt-0.5" /> {label}
          </Button>
        ))}
        {!!list.length && (
          // Every simultaneous LIVE gate is another multiple comparison. Keep the
          // count visible so nobody has to count JSON to know how many are armed.
          <span className="ml-auto text-[11px] text-muted">
            <b className={live ? "text-warn" : ""}>{live}</b> live · {shadow} shadow
          </span>
        )}
      </div>
      {!list.length ? (
        <div className="text-[11px] text-muted">No filters — every signal trades at full size. Add a rule above, e.g. <i>Trend Alignment → skip</i> or <i>ADX Regime (4h trending) → skip</i>.</div>
      ) : (
        <div className="space-y-2">
          {list.map((r, i) => {
            const type = r.when?.type || "session_in";
            return (
              <div key={i} className="rounded-lg border border-edge bg-panel2/40 p-3 space-y-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <Toggle checked={r.enabled !== false} onChange={(v) => patch(i, { ...r, enabled: v })} />
                  <Select value={type} onChange={(e) => setType(i, e.target.value)}>
                    {Object.entries(RULE_TYPES).map(([t, { label }]) => <option key={t} value={t}>{label}</option>)}
                  </Select>
                  <span className="text-muted text-xs">→</span>
                  <Select value={r.action || "scale"} onChange={(e) => patch(i, { ...r, action: e.target.value })}>
                    <option value="skip">skip</option><option value="scale">scale</option>
                  </Select>
                  <Select value={effectiveMode(r)} onChange={(e) => patch(i, { ...r, mode: e.target.value })}
                    title="shadow = computed and logged as filter_shadow, changes nothing. live = actually skips/scales.">
                    <option value="shadow">shadow</option><option value="live">live</option>
                  </Select>
                  {r.action === "scale" && (
                    <input type="number" step="0.05" value={r.factor ?? 0.5}
                      onChange={(e) => patch(i, { ...r, factor: Number(e.target.value) })}
                      className="w-20 bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-beacon" title="size ×factor" />
                  )}
                  <input placeholder="label (optional)" value={r.name || ""} onChange={(e) => patch(i, { ...r, name: e.target.value })}
                    className="w-40 bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-beacon" />
                  <button onClick={() => onChange(list.filter((_, j) => j !== i))} className="ml-auto text-short" title="remove rule"><Trash2 className="w-4 h-4" /></button>
                </div>
                <p className="text-[11px] text-muted">{RULE_TYPES[type]?.hint}</p>
                <RuleFields when={r.when} set={(k, v) => patchWhen(i, k, v)}
                  setMany={(obj) => patchWhenMany(i, obj)} catalog={catalog} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
