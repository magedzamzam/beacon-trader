import { Clock } from "lucide-react";

const SESSION_COLOR = { asian: "var(--violet)", london: "var(--beacon-2)", newyork: "var(--long)" };
const colorFor = (id) => SESSION_COLOR[id] || "var(--beacon)";
const fmtMin = (m) => (m == null ? "" : m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`);

/** Clip a [start,end] hour window (may run < 0 or > 24) to the visible [0,24]
 *  day, including any part that wraps around midnight. */
function segments(start, end) {
  const segs = [];
  for (const off of [-24, 0, 24]) {
    const a = Math.max(0, start + off), b = Math.min(24, end + off);
    if (b > a) segs.push([a, b]);
  }
  return segs;
}

/**
 * SessionTimeline — a compact Forex-style session timeline (0–24h UTC) with a
 * bar per market and a live "now" line. Bars dim when the market is closed
 * (weekend / US holiday).
 */
export default function SessionTimeline({ status }) {
  if (!status?.sessions) return null;
  const { windows, now_hour_utc } = status.sessions;
  const closed = status.holiday?.is_weekend || status.holiday?.is_holiday;
  const closedLabel = status.holiday?.is_holiday ? status.holiday.holiday_name : "Weekend";
  const nowLeft = `${(now_hour_utc / 24) * 100}%`;

  return (
    <div className="card p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-medium flex items-center gap-1.5 text-muted">
          <Clock className="w-3.5 h-3.5" /> Trading sessions
        </div>
        {closed
          ? <span className="text-[10px] text-warn">markets closed · {closedLabel}</span>
          : <span className="text-[10px] text-muted num">{(status.now_utc || "").slice(11, 16)} UTC</span>}
      </div>

      <div className="flex">
        <div className="w-24 shrink-0">
          <div className="h-4" />
          {windows.map(w => (
            <div key={w.id} className="h-6 flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: colorFor(w.id) }} />
              <span className="text-[11px] truncate">{w.label}</span>
            </div>
          ))}
        </div>

        <div className="flex-1 relative min-w-0">
          {/* hour axis */}
          <div className="h-4 relative text-[9px] text-muted">
            {[0, 6, 12, 18, 24].map(h => (
              <span key={h} className="absolute -translate-x-1/2" style={{ left: `${(h / 24) * 100}%` }}>{h}</span>
            ))}
          </div>
          {/* session tracks */}
          {windows.map(w => (
            <div key={w.id} className="h-6 relative">
              <div className="absolute inset-y-1 inset-x-0 rounded bg-panel2" />
              {segments(w.start_hour_utc, w.end_hour_utc).map((seg, i) => (
                <div key={i} className="absolute inset-y-1 rounded"
                  style={{ left: `${(seg[0] / 24) * 100}%`, width: `${((seg[1] - seg[0]) / 24) * 100}%`,
                           background: colorFor(w.id),
                           opacity: closed || !w.enabled ? 0.22 : (w.active ? 0.9 : 0.4) }} />
              ))}
            </div>
          ))}
          {/* now line */}
          <div className="absolute w-px bg-beacon" style={{ left: nowLeft, top: "16px", bottom: 0 }} />
          <div className="absolute w-1.5 h-1.5 rounded-full bg-beacon -translate-x-1/2"
               style={{ left: nowLeft, top: "13px" }} />
        </div>
      </div>

      <div className="mt-1.5 text-[10px] text-muted">
        {status.sessions.active?.length
          ? <>Active: {windows.filter(w => w.active).map(w => `${w.label} (closes ${fmtMin(w.closes_in_min)})`).join(" · ")}</>
          : <>Between sessions · next {(() => {
              const nx = windows.filter(w => w.opens_in_min != null).sort((a, b) => a.opens_in_min - b.opens_in_min)[0];
              return nx ? `${nx.label} in ${fmtMin(nx.opens_in_min)}` : "—";
            })()}</>}
      </div>
    </div>
  );
}
