import { useEffect, useState } from "react";
import { Card, Empty } from "./ui";
import { Select } from "./form";
import { api } from "../lib/api";

/**
 * Structure placement chart (#122): a spatial view of where magnet zones sit
 * relative to price and each other, overlaid on candles — the complement to the
 * inside/outside stat tables ("does it pay?" vs "where is it?"). Read-only shadow;
 * nothing gates.
 *
 * NOTE on classification: the issue framed zones as FVG / Order-Block / Fib, but
 * magnet-zone `members[].kind` are actually Fib (`fib_retracement`/`fib_extension`)
 * and Swing (`swing_high`/`swing_low`) only — the map is built from the Fib ladder
 * + swing pivots (`analysis/structure.py`), it does not carry FVG/OB. FVG/OB live
 * in the separate inside/outside cut (`Structure analyses`). So zones are colored
 * and isolatable by their real dominant family: Fib vs Swing.
 */

const RESOLUTIONS = [["HOUR", "1h"], ["HOUR_4", "4h"], ["DAY", "1D"]];

// member kind -> family; families we can color / isolate.
const FAMILY = {
  fib_retracement: "fib", fib_extension: "fib",
  swing_high: "swing", swing_low: "swing",
};
const FAMILY_META = {
  fib: { label: "Fib", color: "var(--violet)" },
  swing: { label: "Swing", color: "var(--beacon)" },
  other: { label: "Other", color: "var(--muted)" },
};

function dominantFamily(members) {
  const c = { fib: 0, swing: 0 };
  (members || []).forEach(m => { const f = FAMILY[m?.kind]; if (f) c[f] += 1; });
  if (c.fib === 0 && c.swing === 0) return "other";
  return c.fib >= c.swing ? "fib" : "swing";
}

// SVG geometry (viewBox units; the element scales to container width).
const W = 820, H = 360, LG = 54, RM = 10, TM = 10, BM = 22;
const PW = W - LG - RM, PH = H - TM - BM;

export default function StructureMapChart({ map, price }) {
  const [resolution, setResolution] = useState("HOUR");
  const [bars, setBars] = useState(null);      // null=loading, []=none
  const [show, setShow] = useState({ fib: true, swing: true });

  useEffect(() => {
    let alive = true;
    setBars(null);
    api.candles("XAUUSD", resolution, 160)
      .then(res => {
        if (!alive) return;
        const data = (res.bars || [])
          .map(b => ({ t: Math.floor(Date.parse(b.t) / 1000), o: +b.o, h: +b.h, l: +b.l, c: +b.c }))
          .filter(b => Number.isFinite(b.t) && Number.isFinite(b.c))
          .sort((a, b) => a.t - b.t);
        setBars(data);
      })
      .catch(() => { if (alive) setBars([]); });   // market closed / broker down -> bands still render
    return () => { alive = false; };
  }, [resolution]);

  const zones = (map?.zones || []).map(z => ({ ...z, family: dominantFamily(z.members) }));
  const visible = zones.filter(z => z.family === "other" || show[z.family]);

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-2 flex-wrap">
        <div className="text-sm font-medium">
          Structure placement — magnet zones on candles
          <span className="text-muted font-normal text-[11px]"> · XAUUSD · where zones sit</span>
        </div>
        <div className="w-24">
          <Select value={resolution} onChange={e => setResolution(e.target.value)}>
            {RESOLUTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </Select>
        </div>
      </div>

      {!map || map.version_id == null ? (
        <Empty>No structure map yet — recompute it in the Structure map card.</Empty>
      ) : !zones.length ? (
        <Empty>No magnet zones to place.</Empty>
      ) : (
        <div className="p-4 space-y-3">
          {/* legend + isolation toggles */}
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            {["fib", "swing"].map(f => {
              const n = zones.filter(z => z.family === f).length;
              const on = show[f];
              return (
                <button key={f} onClick={() => setShow(s => ({ ...s, [f]: !s[f] }))}
                  className={`flex items-center gap-1.5 px-2 py-1 rounded-md border transition ${
                    on ? "border-edge text-ink" : "border-edge text-muted opacity-50"}`}
                  title={on ? `Hide ${FAMILY_META[f].label} zones` : `Show ${FAMILY_META[f].label} zones`}>
                  <span className="w-2.5 h-2.5 rounded-sm" style={{ background: FAMILY_META[f].color }} />
                  {FAMILY_META[f].label} <span className="num text-muted">{n}</span>
                </button>
              );
            })}
            {zones.some(z => z.family === "other") && (
              <span className="flex items-center gap-1.5 px-2 py-1 text-muted">
                <span className="w-2.5 h-2.5 rounded-sm" style={{ background: FAMILY_META.other.color }} />
                Other <span className="num">{zones.filter(z => z.family === "other").length}</span>
              </span>
            )}
            <span className="ml-auto text-muted">{bars === null ? "loading candles…" : `${bars.length} bars`}</span>
          </div>

          <PlacementSvg bars={bars || []} zones={visible} price={price} />

          <div className="text-[11px] text-muted">
            Boxes span each zone's real <b>[low, high]</b>, colored by dominant member family
            (<span style={{ color: "var(--violet)" }}>Fib</span> ·
            <span style={{ color: "var(--beacon)" }}> Swing</span>); fill intensity = confluence strength.
            Toggle a family to isolate it. The map carries Fib + swing levels only — FVG/OB placement isn't
            in this data (see the inside/outside cut for those). Shadow — nothing gates.
          </div>
        </div>
      )}
    </Card>
  );
}

function PlacementSvg({ bars, zones, price }) {
  // Price domain = union of candle range, visible zone bands, and live price.
  const vals = [];
  bars.forEach(b => { vals.push(b.h, b.l); });
  zones.forEach(z => { vals.push(z.band[0], z.band[1]); });
  if (price != null) vals.push(price);
  if (!vals.length) return <Empty>Nothing to plot.</Empty>;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!(hi > lo)) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.04;
  lo -= pad; hi += pad;
  const span = hi - lo;
  const yOf = (p) => TM + ((hi - p) / span) * PH;

  const n = Math.max(1, bars.length);
  const slot = PW / n;
  const bw = Math.max(1, Math.min(9, slot * 0.62));
  const xOf = (i) => LG + (i + 0.5) * slot;

  const maxScore = Math.max(1, ...zones.map(z => z.score));

  // A few horizontal price gridlines / axis labels.
  const ticks = 4;
  const gridY = Array.from({ length: ticks + 1 }, (_, k) => lo + (span * k) / ticks);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto" }}
      role="img" aria-label={`Structure placement chart: ${zones.length} zones over ${bars.length} candles`}>
      {/* price grid + axis */}
      {gridY.map((p, k) => (
        <g key={`g${k}`}>
          <line x1={LG} x2={W - RM} y1={yOf(p)} y2={yOf(p)} stroke="var(--edge)" strokeWidth="0.5" />
          <text x={LG - 4} y={yOf(p) + 3} textAnchor="end" fontSize="9" fill="var(--muted)">{p.toFixed(0)}</text>
        </g>
      ))}

      {/* zone bands */}
      {zones.map(z => {
        const y = yOf(z.band[1]);
        const h = Math.max(1.2, yOf(z.band[0]) - yOf(z.band[1]));
        const meta = FAMILY_META[z.family] || FAMILY_META.other;
        const op = 0.14 + 0.34 * Math.min(1, z.score / maxScore);
        return (
          <g key={`z${z.rank}`}>
            <rect x={LG} y={y} width={PW} height={h} fill={meta.color} opacity={op} />
            <line x1={LG} x2={W - RM} y1={yOf(z.mid)} y2={yOf(z.mid)} stroke={meta.color} strokeWidth="0.6" opacity="0.8" />
            <text x={LG + 4} y={yOf(z.mid) - 2} fontSize="8.5" fill={meta.color}>
              {meta.label} · {z.n_timeframes}TF · {Math.round(z.score)}
            </text>
          </g>
        );
      })}

      {/* candles */}
      {bars.map((b, i) => {
        const x = xOf(i);
        const up = b.c >= b.o;
        const col = up ? "var(--long)" : "var(--short)";
        const yO = yOf(b.o), yC = yOf(b.c);
        const top = Math.min(yO, yC);
        const bh = Math.max(0.8, Math.abs(yC - yO));
        return (
          <g key={`c${i}`}>
            <line x1={x} x2={x} y1={yOf(b.h)} y2={yOf(b.l)} stroke={col} strokeWidth="0.7" />
            <rect x={x - bw / 2} y={top} width={bw} height={bh} fill={col} />
          </g>
        );
      })}

      {/* live price */}
      {price != null && (
        <g>
          <line x1={LG} x2={W - RM} y1={yOf(price)} y2={yOf(price)}
            stroke="var(--beacon)" strokeWidth="1" strokeDasharray="4 3" />
          <text x={W - RM} y={yOf(price) - 3} textAnchor="end" fontSize="9" fill="var(--beacon)"
            fontWeight="600">{price.toFixed(2)}</text>
        </g>
      )}
    </svg>
  );
}
