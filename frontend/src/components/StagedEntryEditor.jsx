import { useState } from "react";
import { Field, Input, Slider } from "./form";

/**
 * StagedEntryEditor (#129) — the config sub-panel for the confirmation-staged
 * entry model. Rendered only when entry_style === "staged".
 *
 *   EXECUTE(T1 toe-in) → MONITOR → DECIDE(T2 runner / reclaim) → EXECUTE
 *
 * Self-contained + presentational: `value` is the staged config object, `onChange
 * (key, val)` patches one key. The live "tranche map" mirrors the pure engine's
 * partition (beacon_core.execution.staging.partition_tps) so the operator SEES how
 * their split defers size before saving. Keep this mirror in sync with that fn.
 */

// Mirror of staging.partition_tps (by_tp_tier). Pure; used only for the preview.
function partitionPreview(n, cfg) {
  if (n <= 0) return { toe_in: [], runner: [], reclaim: [] };
  const idx = Array.from({ length: n }, (_, i) => i + 1);
  if (n === 1) return { toe_in: [1], runner: [], reclaim: [] };
  const t = Math.max(1, parseInt(cfg.toe_in_tps, 10) || 1);
  const r = Math.max(0, parseInt(cfg.runner_tps, 10) || 0);
  const toe = idx.slice(0, t);
  let run = r > 0 ? idx.slice(Math.max(t, n - r)) : [];
  run = run.filter((i) => !toe.includes(i));
  let rec = idx.filter((i) => !toe.includes(i) && !run.includes(i));
  const minFrac = Number(cfg.min_deferred_fraction ?? 0);
  if (rec.length === 0 && run.length && minFrac > 0) { rec = [run[run.length - 1]]; run = run.slice(0, -1); }
  const maxFrac = Number(cfg.max_deferred_fraction ?? 1);
  if (maxFrac < 1 && rec.length) {
    const maxRec = Math.floor(maxFrac * n + 1e-9);
    while (rec.length > Math.max(1, maxRec)) run.push(rec.shift());
    run.sort((a, b) => a - b);
  }
  return { toe_in: toe, runner: run, reclaim: rec };
}

const ROLE = {
  toe_in: { label: "toe-in", cls: "bg-long/25 text-long border-long/40", note: "deploys now" },
  runner: { label: "runner", cls: "bg-beacon/20 text-beacon border-beacon/40", note: "at deep edge" },
  reclaim: { label: "reclaim", cls: "bg-warn/20 text-warn border-warn/40", note: "break→reclaim" },
};

function roleOf(tp, p) {
  if (p.toe_in.includes(tp)) return "toe_in";
  if (p.runner.includes(tp)) return "runner";
  return "reclaim";
}

// Deferred share of the ladder -> a risk tone (more deferred = more tail-protected).
function deferredTone(frac) {
  if (frac >= 0.5) return "bg-long";
  if (frac >= 0.25) return "bg-beacon";
  if (frac > 0) return "bg-warn";
  return "bg-edge";
}

function TrancheMap({ cfg }) {
  const [n, setN] = useState(5);
  const p = partitionPreview(n, cfg);
  const deferred = n ? p.reclaim.length / n : 0;
  return (
    <div className="rounded-lg border border-edge p-3 space-y-2 bg-panel2/40">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wider text-muted">Tranche map</span>
        <span className="text-[11px] text-muted">preview a</span>
        <select value={n} onChange={(e) => setN(Number(e.target.value))}
          className="bg-panel2 border border-edge rounded-md px-1.5 py-0.5 text-[11px] outline-none focus:border-beacon">
          {[2, 3, 4, 5].map((k) => <option key={k} value={k}>{k}-TP</option>)}
        </select>
        <span className="text-[11px] text-muted">signal</span>
      </div>
      {/* the TP ladder, each tier coloured by the role it lands in */}
      <div className="flex gap-1.5 flex-wrap">
        {Array.from({ length: n }, (_, i) => i + 1).map((tp) => {
          const role = roleOf(tp, p);
          return (
            <div key={tp} className={`px-2 py-1 rounded-md border text-[11px] num ${ROLE[role].cls}`} title={ROLE[role].note}>
              TP{tp}<span className="text-[10px] opacity-70 ml-1">{ROLE[role].label}</span>
            </div>
          );
        })}
      </div>
      {/* deferred-size heat bar: how much of the ladder waits for confirmation */}
      <div className="flex items-center gap-2">
        <div className="flex-1 h-2 rounded-full bg-edge overflow-hidden">
          <div className={`h-full ${deferredTone(deferred)} transition-all`} style={{ width: `${Math.round(deferred * 100)}%` }} />
        </div>
        <span className="num text-[11px] text-muted w-28 text-right">{Math.round(deferred * 100)}% deferred</span>
      </div>
      <div className="flex gap-3 text-[10px] text-muted">
        {Object.entries(ROLE).map(([k, r]) => (
          <span key={k} className="flex items-center gap-1"><span className={`w-2 h-2 rounded-full ${r.cls.split(" ")[0]}`} />{r.label} · {r.note}</span>
        ))}
      </div>
    </div>
  );
}

export default function StagedEntryEditor({ value, onChange }) {
  const v = value || {};
  const set = (k) => (val) => onChange(k, val);
  const setNum = (k) => (e) => onChange(k, e.target.value);
  const pct = (x) => `${Math.round(Number(x) * 100)}%`;
  return (
    <div className="rounded-lg border border-beacon/30 p-4 space-y-4 bg-beacon/[0.03]">
      <p className="text-[11px] text-muted">
        <b className="text-beacon">Confirmation-staged entry.</b> Deploy the signal in tranches so a straight-to-SL move is
        caught on partial size. Same signal, same SL, same intended total risk — only <i>when/if</i> each leg deploys changes.
        Inert until this strategy's account is set to <span className="num">entry_style: staged</span>.
      </p>

      <TrancheMap cfg={v} />

      {/* Partition */}
      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-muted">Partition — nearest TP → toe-in · farthest → runner · middle → deferred</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label="Toe-in TP tiers" hint="nearest TPs deployed immediately"><Input type="number" min="1" value={v.toe_in_tps ?? ""} onChange={setNum("toe_in_tps")} /></Field>
          <Field label="Runner TP tiers" hint="farthest TPs, at the deep edge"><Input type="number" min="0" value={v.runner_tps ?? ""} onChange={setNum("runner_tps")} /></Field>
          <Field label="Max deferred" hint="cap the reclaim tail's share"><Slider value={v.max_deferred_fraction} onChange={set("max_deferred_fraction")} min={0} max={1} step={0.05} format={pct} /></Field>
          <Field label="Min deferred" hint="always protect some tail"><Slider value={v.min_deferred_fraction} onChange={set("min_deferred_fraction")} min={0} max={1} step={0.05} format={pct} /></Field>
        </div>
      </div>

      {/* Break-then-reclaim geometry */}
      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-muted">Break → reclaim geometry</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label="Break distance (× ATR)" hint="how far beyond the deep edge arms the reclaim"><Slider value={v.reclaim_break_atr} onChange={set("reclaim_break_atr")} min={0} max={1.5} step={0.05} suffix="ATR" /></Field>
          <Field label="Break cap (× stop)" hint="never arm past this fraction of the stop"><Slider value={v.reclaim_break_max_frac_of_stop} onChange={set("reclaim_break_max_frac_of_stop")} min={0} max={1} step={0.05} format={pct} /></Field>
          <Field label="Break cap ($)" hint="hard cap for ATR-spike regimes"><Input type="number" step="0.5" value={v.reclaim_break_abs_cap ?? ""} onChange={setNum("reclaim_break_abs_cap")} /></Field>
          <Field label="Stop offset (× ATR)" hint="buffer past the deep edge on the reclaim trigger"><Slider value={v.stop_offset_atr} onChange={set("stop_offset_atr")} min={0} max={0.5} step={0.05} suffix="ATR" /></Field>
        </div>
      </div>

      {/* TTLs + guardrail */}
      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-muted">Timeouts & guardrail (minutes)</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <Field label="Runner TTL" hint="give up waiting for the deep edge"><Input type="number" value={v.runner_ttl_minutes ?? ""} onChange={setNum("runner_ttl_minutes")} /></Field>
          <Field label="Reclaim pending TTL" hint="give up waiting for a break"><Input type="number" value={v.reclaim_pending_ttl_minutes ?? ""} onChange={setNum("reclaim_pending_ttl_minutes")} /></Field>
          <Field label="Reclaim armed TTL" hint="give up waiting for the reclaim fill"><Input type="number" value={v.reclaim_armed_ttl_minutes ?? ""} onChange={setNum("reclaim_armed_ttl_minutes")} /></Field>
          <Field label="Min stop (× ATR) to stage" hint="tighter stops trade single-shot instead"><Slider value={v.min_stop_atr} onChange={set("min_stop_atr")} min={0} max={2} step={0.1} suffix="ATR" /></Field>
        </div>
      </div>
    </div>
  );
}
