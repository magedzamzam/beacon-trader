import { useMemo, useState } from "react";

/**
 * Shared date-range filter (#58) — one implementation for Performance,
 * Reconciliation, Analytics, and Bayesian Analysis. `useRange()` owns the
 * preset/custom state and derives `{ fromIso, toIso, range }` (UTC ISO, so the
 * backend `parse_iso_utc` is always given a `Z` instant); <RangeFilter> renders
 * the pill bar + Custom picker bound to that state.
 */
// Full set — for sum/tally pages (Performance, Reconciliation): any granularity
// is a meaningful question.
export const PRESETS = [
  ["all", "All time"], ["today", "Today"], ["7d", "Last 7 days"],
  ["this_week", "This week"], ["last_week", "Last week"],
  ["this_month", "This month"], ["last_month", "Last month"],
  ["this_quarter", "This quarter"], ["last_quarter", "Last quarter"],
  ["this_year", "This year"], ["custom", "Custom"],
];

// Coarse set — for inference pages (Bayesian, Analytics). Fine-grained windows
// give tiny per-cell n (credible intervals blow out to ~[0,1], a sparse slice
// masquerades as an edge), so Today / Last 7 days / This week / Last week are
// omitted (#58). These pages default to "all" for maximum statistical power.
export const COARSE_PRESETS = [
  ["all", "All time"], ["this_month", "This month"], ["last_month", "Last month"],
  ["this_quarter", "This quarter"], ["last_quarter", "Last quarter"],
  ["this_year", "This year"], ["custom", "Custom"],
];

const PRESET_SETS = { full: PRESETS, coarse: COARSE_PRESETS };

// [from, to) as Date objects (or null = unbounded), local-time boundaries.
export function rangeFor(id, custom = { from: "", to: "" }) {
  const now = new Date();
  const sod = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
  const addD = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const sow = (d) => addD(sod(d), -((new Date(d).getDay() + 6) % 7));   // Monday
  const som = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
  const soq = (d) => new Date(d.getFullYear(), Math.floor(d.getMonth() / 3) * 3, 1);
  const soy = (d) => new Date(d.getFullYear(), 0, 1);
  switch (id) {
    case "today": return [sod(now), now];
    case "7d": return [addD(now, -7), now];
    case "this_week": return [sow(now), now];
    case "last_week": return [addD(sow(now), -7), sow(now)];
    case "this_month": return [som(now), now];
    case "last_month": { const s = som(now); return [new Date(s.getFullYear(), s.getMonth() - 1, 1), s]; }
    case "this_quarter": return [soq(now), now];
    case "last_quarter": { const s = soq(now); return [new Date(s.getFullYear(), s.getMonth() - 3, 1), s]; }
    case "this_year": return [soy(now), now];
    case "custom": {
      const f = custom.from ? new Date(custom.from + "T00:00:00") : null;
      const t = custom.to ? addD(new Date(custom.to + "T00:00:00"), 1) : null;  // inclusive end day
      return [f, t];
    }
    default: return [null, null];   // all time
  }
}

export function useRange(initial = "all") {
  const [preset, setPreset] = useState(initial);
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [from, to] = useMemo(() => rangeFor(preset, custom), [preset, custom]);
  const fromIso = from ? from.toISOString() : "";
  const toIso = to ? to.toISOString() : "";
  return { preset, setPreset, custom, setCustom, fromIso, toIso,
           range: { from: fromIso, to: toIso } };
}

export default function RangeFilter({ state, variant = "full" }) {
  const { preset, setPreset, custom, setCustom } = state;
  const presets = PRESET_SETS[variant] || PRESETS;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {presets.map(([id, label]) => (
        <button key={id} onClick={() => setPreset(id)}
          className={`px-2.5 py-1 rounded-lg text-xs transition
            ${preset === id ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
          {label}
        </button>
      ))}
      {preset === "custom" && (
        <div className="flex items-center gap-1.5 ml-1">
          <input type="date" value={custom.from}
            onChange={e => setCustom(c => ({ ...c, from: e.target.value }))}
            className="bg-panel2 border border-edge rounded-lg px-2 py-1 text-xs num outline-none focus:border-beacon" />
          <span className="text-muted text-xs">→</span>
          <input type="date" value={custom.to}
            onChange={e => setCustom(c => ({ ...c, to: e.target.value }))}
            className="bg-panel2 border border-edge rounded-lg px-2 py-1 text-xs num outline-none focus:border-beacon" />
        </div>
      )}
    </div>
  );
}
