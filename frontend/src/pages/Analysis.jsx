import { useEffect, useState } from "react";
import { Sigma } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
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
  const load = () => { setData(null); setErr(null); setGate(null);
    api.bayesAnalysis(minN, range.range, account).then(setData).catch(e => setErr(e.message));
    api.bayesGateReport(minN, range.range, account).then(setGate).catch(() => setGate(null)); };
  // refetch on range OR account change (#83 per-account A/B); "Apply" refetches on min-n
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [range.fromIso, range.toIso, account]);

  const base = data?.base_rate;
  return (
    <div className="space-y-4">
      <RangeFilter state={range} variant="coarse" />
      {err && <ErrorNote>{err}</ErrorNote>}
      {!err && data && <ExecutionTaxCard tax={data.execution_tax} />}
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
              <Th>Condition</Th><Th right>n</Th><Th right>Raw</Th><Th right>Posterior</Th>
              <Th>90% CI</Th><Th right>Lift</Th>
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
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Recent signals — Naive-Bayes P(win)</div>
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
        Learned-P(win) gate
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
            <Th>Channel</Th><Th right>n</Th><Th right>Signal-quality WR</Th>
            <Th right>Bot-realized WR</Th><Th right>Execution tax</Th>
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
