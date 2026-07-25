import { Plus, Trash2 } from "lucide-react";
import { Field, Input, Select, Toggle, Button } from "./form";

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
};

const newRule = (type) => ({ enabled: true, name: "", when: { ...RULE_TYPES[type].blank }, action: "scale", factor: 0.5 });

function RuleFields({ when, set }) {
  const t = when?.type;
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
  const patch = (i, r) => onChange(list.map((x, j) => (j === i ? r : x)));
  const patchWhen = (i, k, v) => patch(i, { ...list[i], when: { ...list[i].when, [k]: v } });
  const setType = (i, type) => patch(i, { ...list[i], when: { ...RULE_TYPES[type].blank } });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-muted">Add a rule:</span>
        {Object.entries(RULE_TYPES).map(([type, { label }]) => (
          <Button key={type} variant="ghost" onClick={() => onChange([...list, newRule(type)])}>
            <Plus className="w-3.5 h-3.5 inline -mt-0.5" /> {label}
          </Button>
        ))}
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
                <RuleFields when={r.when} set={(k, v) => patchWhen(i, k, v)} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
