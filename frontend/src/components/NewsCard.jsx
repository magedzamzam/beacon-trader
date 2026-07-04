import { Newspaper } from "lucide-react";
import { Badge } from "./ui";

const IMPACT_TONE = { high: "short", medium: "warn", low: "muted" };
const fmtMin = (m) => (m == null ? "" : m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`);

/** NewsCard — the current high-impact economic event: name + impact (+ currency
 *  and countdown). Red when we're inside a news blackout window. */
export default function NewsCard({ status }) {
  const n = status?.news;
  if (!n) return null;
  const e = n.in_blackout ? n.active : n.next;

  return (
    <div className="card p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-medium flex items-center gap-1.5 text-muted">
          <Newspaper className="w-3.5 h-3.5" /> Economic news
        </div>
        {n.in_blackout && <span className="text-[10px] text-short font-medium">⛔ BLACKOUT</span>}
      </div>

      {!e ? (
        <div className="text-sm text-muted py-2">No high-impact events upcoming.</div>
      ) : (
        <div>
          <div className="flex items-start justify-between gap-2">
            <div className="text-sm font-medium leading-snug">{e.title || "—"}</div>
            {e.impact && <Badge tone={IMPACT_TONE[e.impact] || "muted"}>{e.impact}</Badge>}
          </div>
          <div className="mt-1 text-[11px] text-muted flex items-center gap-2">
            {e.ccy && <span className="num">{e.ccy}</span>}
            <span>·</span>
            <span className={n.in_blackout ? "text-short" : ""}>
              {n.in_blackout ? "in progress" : `in ${fmtMin(e.in_min)}`}
            </span>
            <span className="num">{(e.ts || "").slice(11, 16)} UTC</span>
          </div>
        </div>
      )}
    </div>
  );
}
