import { useEffect, useState } from "react";
import { Sigma } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";

const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
const spct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%");
const liftTone = (l) => (l > 0.05 ? "long" : l < -0.05 ? "short" : "muted");

/**
 * Analysis — Bayesian correlation of the captured TA features with trade
 * outcomes (win = realized P&L > 0). A per-condition Beta-Binomial posterior
 * win-rate table (credible intervals shrink thin samples toward the base rate)
 * plus a Naive-Bayes P(win) score for recent signals.
 */
export default function Analysis({ account = "" }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [minN, setMinN] = useState(5);
  const range = useRange("all");

  const [gate, setGate] = useState(null);
  const [ladder, setLadder] = useState(null);
  const [ladderErr, setLadderErr] = useState(null);
  const [basis, setBasis] = useState("signal");
  const load = () => { setData(null); setErr(null); setGate(null);
    api.bayesAnalysis(minN, range.range, account).then(setData).catch(e => setErr(e.message));
    api.bayesGateReport(minN, range.range, account).then(setGate).catch(() => setGate(null)); };
  // refetch on range OR account change (#83 per-account A/B); "Apply" refetches on min-n
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [range.fromIso, range.toIso, account]);
  // The excursion ladder (#182) is account-INDEPENDENT by construction, so it
  // follows the date range and the basis toggle but ignores the account filter.
  useEffect(() => { setLadder(null); setLadderErr(null);
    api.excursionLadder(range.range, basis)
      .then(d => { setLadder(d); setLadderErr(null); })
      .catch(e => setLadderErr(e.message || "failed to load"));
    /* eslint-disable-next-line */ }, [range.fromIso, range.toIso, basis]);

  const base = data?.base_rate;
  return (
    <div className="space-y-4">
      <RangeFilter state={range} variant="coarse" />
      {err && <ErrorNote>{err}</ErrorNote>}
      {!err && data && <ExecutionTaxCard tax={data.execution_tax} />}
      {!err && <ExcursionLadderCard data={ladder} error={ladderErr} basis={basis} onBasis={setBasis} />}
      {!err && gate && <BayesGateCard gate={gate} />}
      {!err && !data && <Card><Empty>Loading…</Empty></Card>}
      {!err && data && !data.ready && (
        <Card><Empty>{data.message || "Not enough data yet."} The Bayesian analysis
          appears once trades close with a captured TA snapshot.</Empty></Card>
      )}
      {!err && data && data.ready && (<>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-3 flex-wrap">
          <div className="text-sm font-medium flex items-center gap-2">
            <Sigma className="w-4 h-4 text-beacon" /> Bayesian correlation
            <span className="text-muted font-normal">· win = realized P&L &gt; 0</span>
          </div>
          <div className="flex items-center gap-3 text-xs text-muted">
            <span className="num">{data.n} trades · {data.wins}W/{data.losses}L · base {pct(base)}</span>
            <HelpHint term="base_rate" />
            <label className="flex items-center gap-1">min n
              <input type="number" min="2" value={minN} onChange={e => setMinN(+e.target.value)}
                className="w-14 bg-panel2 border border-edge rounded px-2 py-1 num outline-none focus:border-beacon" /></label>
            <Button variant="ghost" onClick={load}>Apply</Button>
          </div>
        </div>
        <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
          Posterior win-rate per condition with a 90% credible interval — thin samples are
          shrunk toward the base rate, so 2/2 doesn't read as 100%. Sorted most reliably-better
          first (highest lower bound).
        </div>
        {!data.conditions.length ? <Empty>No conditions meet the min-sample threshold yet.</Empty> : (
          <Table>
            <thead><tr>
              <Th>Condition</Th>
              <Th right>n<HelpHint term="min_n" /></Th>
              <Th right>Raw<HelpHint term="raw_wr" /></Th>
              <Th right>Posterior<HelpHint term="posterior" /></Th>
              <Th>90% CI<HelpHint term="credible_interval" /></Th>
              <Th right>Lift<HelpHint term="lift" /></Th>
            </tr></thead>
            <tbody>
              {data.conditions.map((c, i) => (
                <tr key={i} className="row-hover">
                  <Td><span className="num text-xs">{c.condition}</span></Td>
                  <Td right mono>{c.n}</Td>
                  <Td right mono>{pct(c.raw_wr)}</Td>
                  <Td right mono>{pct(c.mean)}</Td>
                  <Td><span className="num text-xs text-muted">{pct(c.ci_low)}–{pct(c.ci_high)}</span></Td>
                  <Td right mono><span className={`text-${liftTone(c.lift)}`}>{spct(c.lift)}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center">
          Recent signals — Naive-Bayes P(win)<HelpHint term="p_win" /></div>
        {!data.recent.length ? <Empty>No scored signals.</Empty> : (
          <Table>
            <thead><tr>
              <Th>Signal</Th><Th>Symbol</Th><Th>Side</Th><Th right>P(win)</Th><Th>vs base</Th>
            </tr></thead>
            <tbody>
              {data.recent.map(r => (
                <tr key={r.signal_id} className="row-hover">
                  <Td mono>#{r.signal_id}</Td><Td>{r.symbol}</Td>
                  <Td><Badge tone={r.direction === "BUY" ? "long" : "short"}>{r.direction}</Badge></Td>
                  <Td right mono>{r.p_win == null ? "—" : pct(r.p_win)}</Td>
                  <Td>{r.p_win == null ? <span className="text-muted text-xs">—</span>
                    : <Badge tone={r.p_win > base ? "long" : "short"}>{r.p_win > base ? "above" : "below"}</Badge>}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      </>)}
    </div>
  );
}

// Learned-P(win) execution gate (#64) — SHADOW / log-only: what the gate WOULD
// skip/de-size, scored by the trades' ACTUAL realized outcomes. Go live only once
// would_skip expectancy is clearly worse than would_allow.
function BayesGateCard({ gate }) {
  if (!gate) return null;
  const ORDER = ["skip", "desize", "allow", "observe"];
  const LABEL = { skip: "would skip", desize: "would de-size", allow: "would allow", observe: "observe-only" };
  const live = gate.acts_live;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Learned-P(win) gate<HelpHint term="p_win" />
        <Badge tone={live ? "warn" : "muted"}>{live ? "LIVE" : "shadow · log-only"}</Badge>
        {gate.ready && <span className="text-muted font-normal">· {gate.n_scored} scored · signal-quality base {pct(gate.signal_quality_base)}</span>}
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Shadow (#64): buckets are what the gate <b>would</b> do from the signal-quality P(win) + its
        credible interval; the win-rate / expectancy are the <b>actual</b> realized outcomes of those
        trades. Enable live only once <b>would-skip</b> expectancy is clearly worse than <b>would-allow</b>
        at n ≥ min-trades. In-sample — treat as directional.
      </div>
      {!gate.ready ? <Empty>{gate.message || "Not enough labelled trades yet."}</Empty> : (
        <Table minW={640}>
          <thead><tr className="border-b border-edge">
            <Th>Gate decision</Th><Th right>n</Th><Th right>Actual win%</Th><Th right>Actual expectancy</Th>
          </tr></thead>
          <tbody>
            {ORDER.filter(k => gate.would[k]?.n).map(k => {
              const b = gate.would[k];
              return (
                <tr key={k} className="border-b border-edge/60">
                  <Td><Badge tone={k === "skip" ? "short" : k === "allow" ? "long" : "muted"}>{LABEL[k]}</Badge></Td>
                  <Td right mono>{b.n}</Td>
                  <Td right mono>{pct(b.actual_win_rate)} <span className="text-muted">({pct(b.ci_low)}–{pct(b.ci_high)})</span></Td>
                  <Td right mono><span className={b.actual_expectancy >= 0 ? "text-long" : "text-short"}>{b.actual_expectancy}</span></Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

// Per-channel R-ladder (#182) — the exit-independent table. Every other label on
// this page is contaminated: bot-realized bakes in our exit, signal-quality
// depends on channels reporting themselves, and BOTH are confounded by how far
// each channel puts TP1. This one asks only how far price actually travelled
// before the ORIGINAL stop, replayed from the 1m bid/ask candles.
const STATE_TONE = { significant: "beacon", watch: "warn", gathering: "muted" };
const rVal = (v) => (v == null ? "—" : Number(v).toFixed(2) + "R");

// The ladder rung that covers a channel's own TP1 — the rate in THAT column is
// how often its target was actually reachable. This pairing is the whole point
// of the table, so the cell is marked rather than left for the reader to find.
const tp1Rung = (rungs, tp1r) =>
  (tp1r == null ? null : rungs.find(k => parseFloat(k) >= Number(tp1r)) || null);

function ExcursionLadderCard({ data, error, basis, onBasis }) {
  const rungs = data?.ladder || [];
  const rows = data?.channels || [];
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-3 flex-wrap">
        <div className="text-sm font-medium flex items-center gap-2 flex-wrap">
          Per-channel R-ladder<HelpHint term="r_ladder" />
          <Badge tone="muted">shadow · exit-independent</Badge>
          {data && <span className="text-muted font-normal text-xs num">
            · {data.n_labelled} signals · {data.n_channels} channels</span>}
        </div>
        <div className="flex items-center gap-1 text-xs">
          {[["signal", "signal entry"], ["fill", "our fill"]].map(([k, label]) => (
            <Button key={k} variant={basis === k ? "primary" : "ghost"} onClick={() => onBasis(k)}>
              {label}
            </Button>
          ))}
        </div>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        How often price reached X×R in the called direction <b>before the original stop</b>, reconstructed
        from the 1m bid/ask candles — independent of both our exit and the channel's own claims.
        The <span className="text-beacon">highlighted</span> rung is the one covering that channel's own
        median TP1: <b>that</b> number is how often its target was actually reachable. A reach curve that
        falls off before the channel's own TP1 says <i>take profit earlier on this source</i>.
        {basis === "signal"
          ? " Basis: the channel's stated entry — covers signals we skipped or never filled."
          : " Basis: our actual fill — the gap to the signal basis is the execution cost."}
      </div>
      {error ? <div className="p-4"><ErrorNote>{error}</ErrorNote></div>
        : !data ? <Empty>Loading…</Empty>
        : !rows.length ? (
          <Empty>No reconstructed excursions yet. Run <span className="num text-xs">POST
            /analysis/excursion/recompute</span> once — it replays the candle store offline and
            persists a row per signal.</Empty>
        ) : (
          <Table minW={1080}>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th right>n</Th>
              <Th right>TP1<HelpHint term="tp1_distance_r" /></Th>
              {rungs.map(k => <Th key={k} right>{k}R</Th>)}
              <Th right>TP1 first</Th><Th right>Unres.</Th>
              <Th right>MFE</Th><Th right>MAE</Th>
            </tr></thead>
            <tbody>
              {rows.map((r, i) => {
                const mark = tp1Rung(rungs, r.median_tp1_r);
                return (
                  <tr key={i} className="border-b border-edge/60 row-hover">
                    <Td>{r.channel} <Badge tone={STATE_TONE[r.state] || "muted"}>{r.state}</Badge></Td>
                    <Td right mono>{r.n}</Td>
                    <Td right mono>{rVal(r.median_tp1_r)}</Td>
                    {rungs.map(k => (
                      <Td key={k} right mono>
                        {k === mark
                          ? <span className="bg-beacon/15 text-beacon rounded px-1.5 py-0.5">{pct((r.reach || {})[k])}</span>
                          : <span className="text-muted">{pct((r.reach || {})[k])}</span>}
                      </Td>
                    ))}
                    <Td right mono>{pct(r.tp1_before_sl)}</Td>
                    <Td right mono><span className={r.unresolved > 0.1 ? "text-warn" : "text-muted"}>
                      {pct(r.unresolved)}</span></Td>
                    <Td right mono>{rVal(r.median_mfe_r)}</Td>
                    <Td right mono><span className="text-muted">{rVal(r.median_mae_r)}</span></Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      {data && !!rows.length && (
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
          <b>Unres.</b> = hit neither TP1 nor the stop inside the replay horizon; the ladder is capped
          by that horizon, so widening it can only raise these rates. A bar touching both TP1 and the
          stop is scored conservatively as the stop. Not significant below n ≥ {data.significance_n}.
        </div>
      )}
    </Card>
  );
}

// Execution tax (#63): per-channel signal-quality WR (channel claims) vs
// bot-realized WR (our P&L). A positive gap = the setup worked but our execution
// didn't capture it — the backlog-sizing view.
function ExecutionTaxCard({ tax }) {
  if (!tax || !tax.n_labelled) return null;
  const rows = tax.by_channel || [];
  const taxTone = (g) => (g > 0.05 ? "warn" : g < -0.05 ? "long" : "muted");
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Execution tax · signal-quality vs bot-realized
        <span className="text-muted font-normal">· {tax.n_labelled} dual-labelled signals</span>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Signal-quality WR (the channel's own TP1+/SL claims) minus bot-realized WR (our realized P&amp;L).
        A positive <b>tax</b> means the setup worked but execution didn't capture it (fills / TTL / stops) —
        that's the fix-the-execution backlog, not a bad channel. Credible intervals shrink thin samples.
      </div>
      {!rows.length ? <Empty>No signals carry both a channel claim and a closed trade yet.</Empty> : (
        <Table minW={720}>
          <thead><tr className="border-b border-edge">
            <Th>Channel</Th><Th right>n</Th>
            <Th right>Signal-quality WR<HelpHint term="signal_quality_wr" /></Th>
            <Th right>Bot-realized WR<HelpHint term="bot_realized_wr" /></Th>
            <Th right>Execution tax<HelpHint term="execution_tax" /></Th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b border-edge/60">
                <Td>{r.channel}</Td>
                <Td right mono>{r.n}</Td>
                <Td right mono>{pct(r.signal_quality_wr)} <span className="text-muted">({pct(r.sq_ci[0])}–{pct(r.sq_ci[1])})</span></Td>
                <Td right mono>{pct(r.bot_realized_wr)} <span className="text-muted">({pct(r.br_ci[0])}–{pct(r.br_ci[1])})</span></Td>
                <Td right mono><Badge tone={taxTone(r.execution_tax)}>{spct(r.execution_tax)}</Badge></Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
