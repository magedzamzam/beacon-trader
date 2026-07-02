/**
 * LineChart — a tiny, dependency-free responsive SVG line/area chart.
 *
 * Built for the dashboard equity curve (cumulative realized P&L over time).
 * Points are equally spaced along x; the y-range always includes zero so the
 * dashed baseline is meaningful. The line/fill colour follows the final value
 * (green when up, red when down) via a Tailwind text-colour + currentColor.
 */
export default function LineChart({ data = [], valueKey = "pl", height = 220 }) {
  const pts = (data || []).filter(d => d && d[valueKey] != null);

  if (pts.length < 2) {
    return (
      <div className="p-10 text-center text-sm text-muted">
        Not enough closed trades yet to plot a curve.
      </div>
    );
  }

  const W = 720, H = height, padX = 6, padT = 10, padB = 10;
  const vals = pts.map(p => Number(p[valueKey]));
  let min = Math.min(0, ...vals);
  let max = Math.max(0, ...vals);
  if (min === max) max = min + 1;

  const innerW = W - padX * 2;
  const innerH = H - padT - padB;
  const x = i => padX + (i / (pts.length - 1)) * innerW;
  const y = v => padT + (1 - (v - min) / (max - min)) * innerH;

  const line = pts.map((_, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(vals[i]).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${y(min).toFixed(1)} L${x(0).toFixed(1)},${y(min).toFixed(1)} Z`;
  const up = vals[vals.length - 1] >= 0;
  const zeroY = y(0).toFixed(1);

  return (
    <div className={up ? "text-long" : "text-short"}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
           preserveAspectRatio="none" role="img" aria-label="Cumulative realized P&L over time">
        <defs>
          <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.22" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1={padX} x2={W - padX} y1={zeroY} y2={zeroY}
              stroke="var(--edge)" strokeWidth="1" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        <path d={area} fill="url(#eqfill)" />
        <path d={line} fill="none" stroke="currentColor" strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}
