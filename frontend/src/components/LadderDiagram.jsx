/**
 * LadderDiagram (#268) — the rung table, drawn.
 *
 * `LadderEditor` explains a fundamentally SPATIAL object in prose: a stack of
 * orders on a price axis, described as "price reaches MID → STOP at ENTRY-FROM
 * waits at MID and rests the order back at the entry". Checking that a ladder
 * does what you intend meant mentally reconstructing STOP → ENTRY-FROM → MID →
 * ENTRY-TO → TP1..n geometry from a table of dropdowns.
 *
 * Each rung names both the level it WAITS at (`when`) and the level it PLACES at
 * (`level`), plus the TP it targets — everything needed to draw it. So each rung
 * is a diamond at its trigger, a circle at its placement, a dashed line between
 * them, and a line up to its target.
 *
 * IT IS A SCHEMATIC, and says so on its face. The levels are the ORDER
 * PLACEMENT vocabulary, not live quotes, so it is always populated and can never
 * show stale market data — which is why it does not repeat the dead-data problem
 * that retired the Analytics placement charts (#175). It draws only the config
 * being edited.
 *
 * Drawn long/upward. A SELL ladder is the mirror image and the same relations
 * hold, which is why the axis carries no numbers.
 *
 * Plain SVG, no dependency: the app bundles `lightweight-charts`, but a static
 * schematic does not warrant a chart engine. Colours are the app's own CSS
 * variables, so it follows the theme in both directions.
 */
const W_BASE = 118;      // axis + labels
const W_RUNG = 34;       // per rung column
const H = 260;
const PAD_T = 16;
const PAD_B = 20;
const AXIS_X = 92;

// Illustrative fractions of the schematic axis: 0 is the stop, 1 the top TP.
// Spread so that MID, which is DERIVED from ENTRY-TO and STOP, still lands far
// enough below ENTRY-FROM for both labels to be readable.
const F_STOP = 0.04;
const F_FROM_2Z = 0.36;
const F_TO_2Z = 0.48;
const F_ONE_ZONE = 0.42;   // a 1-Zone signal: ENTRY-TO *is* ENTRY-FROM
const F_TP_LO = 0.60;
const F_TP_HI = 0.96;

const ORDER_COLOR = {
  POSITION: "var(--beacon)",
  LIMIT: "var(--long)",
  STOP: "var(--warn)",
};

const y = (f) => PAD_T + (1 - f) * (H - PAD_T - PAD_B);

export default function LadderDiagram({ rows, zone = 2, tps }) {
  const list = (rows || []).filter(Boolean);

  // TP count: what the cell is FOR when the grid says so, else the deepest
  // target actually written. A row targeting a TP the signal does not have is
  // simply not created, so the diagram should not invent one either.
  const nTp = Math.max(
    1,
    tps || list.reduce((m, r) => Math.max(m, r.action === "cancel_all" ? 0 : (r.target || 0)), 0) || 1,
  );

  const oneZone = Number(zone) === 1;
  const fFrom = oneZone ? F_ONE_ZONE : F_FROM_2Z;
  const fTo = oneZone ? F_ONE_ZONE : F_TO_2Z;
  // MID is halfway from the FAR entry edge to the stop — the same definition the
  // note under the table gives, so the two cannot say different things.
  const fMid = (fTo + F_STOP) / 2;
  const fTp = (k) =>
    nTp === 1 ? (F_TP_LO + F_TP_HI) / 2
      : F_TP_LO + ((k - 1) / (nTp - 1)) * (F_TP_HI - F_TP_LO);

  const LEVEL_F = { ENTRY_FROM: fFrom, ENTRY_TO: fTo, MID: fMid };
  // `signal` is not a price — it means "immediately, at signal time". Drawing it
  // on the axis would put a wait marker at a level nothing is waiting for, so a
  // signal-triggered rung gets NO diamond: its order goes out at once.
  const WHEN_F = {
    entry_from: fFrom, entry_to: fTo, mid: fMid, tp1: fTp(1),
  };

  const w = W_BASE + Math.max(3, list.length) * W_RUNG;
  const rungX = (i) => AXIS_X + 22 + i * W_RUNG;

  const tick = (f, color, text, dash) => (
    <g key={text}>
      <line x1={AXIS_X} x2={w - 6} y1={y(f)} y2={y(f)} stroke={color}
            strokeWidth="1" strokeDasharray={dash} opacity="0.55" />
      <text x={AXIS_X - 6} y={y(f) + 3} textAnchor="end" fontSize="8.5"
            fill="var(--muted)">{text}</text>
    </g>
  );

  return (
    <div className="space-y-1" style={{ maxWidth: "100%" }}>
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${w} ${H}`} width={w} height={H}
             style={{ maxWidth: "100%", height: "auto" }}
             role="img"
             aria-label="Schematic of where each rung waits and where it places its order">
          {/* the entry band: shaded between the two edges, one line on a 1-Zone signal */}
          {!oneZone && (
            <rect x={AXIS_X} y={y(fTo)} width={w - AXIS_X - 6} height={y(fFrom) - y(fTo)}
                  fill="var(--beacon)" opacity="0.10" />
          )}

          {Array.from({ length: nTp }, (_, i) => i + 1).map((k) =>
            tick(fTp(k), "var(--long)", `TP${k}`, "2 3"))}
          {!oneZone && tick(fTo, "var(--beacon)", "ENTRY-TO", "4 3")}
          {tick(fFrom, "var(--beacon)", oneZone ? "ENTRY" : "ENTRY-FROM", "4 3")}
          {tick(fMid, "var(--muted)", "MID", "1 3")}
          {tick(F_STOP, "var(--short)", "STOP", "4 3")}

          {/* the price axis itself */}
          <line x1={AXIS_X} x2={AXIS_X} y1={PAD_T - 6} y2={H - PAD_B + 6}
                stroke="var(--edge)" strokeWidth="1" />

          {list.map((r, i) => {
            const x = rungX(i);
            const fWhen = WHEN_F[r.when];
            const yWhen = fWhen == null ? null : y(fWhen);
            if (r.action === "cancel_all") {
              // Not a placement — a pull. Drawn as a strike at its trigger, so
              // it can never be misread as another order going out.
              const yc = yWhen == null ? y(fTo) : yWhen;
              return (
                <g key={i}>
                  <line x1={x - 7} x2={x + 7} y1={yc - 7} y2={yc + 7}
                        stroke="var(--short)" strokeWidth="2" strokeLinecap="round" />
                  <line x1={x - 7} x2={x + 7} y1={yc + 7} y2={yc - 7}
                        stroke="var(--short)" strokeWidth="2" strokeLinecap="round" />
                  <text x={x} y={H - PAD_B + 13} textAnchor="middle" fontSize="7.5"
                        fill="var(--short)">pull</text>
                </g>
              );
            }
            const yLevel = y(LEVEL_F[r.level] ?? fMid);
            const yTarget = y(fTp(Math.min(r.target || 1, nTp)));
            const color = ORDER_COLOR[r.order] || "var(--beacon)";
            return (
              <g key={i}>
                {/* what it targets */}
                <line x1={x} x2={x} y1={yLevel} y2={yTarget} stroke={color}
                      strokeWidth="1.5" opacity="0.45" />
                <polygon points={`${x},${yTarget} ${x - 3.5},${yTarget + 6} ${x + 3.5},${yTarget + 6}`}
                         fill={color} opacity="0.75" />
                {/* where it waits -> where it places; often different prices,
                    which is the whole thing the prose had to spell out */}
                {yWhen != null && Math.abs(yWhen - yLevel) > 1 && (
                  <line x1={x} x2={x} y1={yWhen} y2={yLevel} stroke={color}
                        strokeWidth="1" strokeDasharray="2 2" opacity="0.8" />
                )}
                {yWhen != null && (
                  <polygon points={`${x},${yWhen - 4} ${x + 4},${yWhen} ${x},${yWhen + 4} ${x - 4},${yWhen}`}
                           fill="var(--panel)" stroke={color} strokeWidth="1.2" />
                )}
                <circle cx={x} cy={yLevel} r="4.5" fill={color} />
                <text x={x} y={H - PAD_B + 13} textAnchor="middle" fontSize="7.5"
                      fill="var(--muted)">{r.order?.[0] || "?"}{r.target}</text>
              </g>
            );
          })}
        </svg>
      </div>
      <div className="text-[10px] text-muted leading-relaxed">
        Schematic — spacing is illustrative, not to scale, and carries no prices.
        <span className="mx-1">◇</span>waits (no diamond = goes out at signal time) ·
        <span className="mx-1">●</span>places ·
        <span className="mx-1">▲</span>targets ·
        <span className="mx-1" style={{ color: "var(--beacon)" }}>POSITION</span>
        <span className="mx-1" style={{ color: "var(--long)" }}>LIMIT</span>
        <span className="mx-1" style={{ color: "var(--warn)" }}>STOP</span>
        · drawn long; a sell ladder is the mirror image.
      </div>
    </div>
  );
}
