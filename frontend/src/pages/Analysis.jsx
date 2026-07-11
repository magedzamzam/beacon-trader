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
export default function Analysis() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [minN, setMinN] = useState(5);
  const range = useRange("all");

  const load = () => { setData(null); setErr(null);
    api.bayesAnalysis(minN, range.range).then(setData).catch(e => setErr(e.message)); };
  // refetch on range change; the "Apply" button refetches on a min-n change
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [range.fromIso, range.toIso]);

  const base = data?.base_rate;
  return (
    <div className="space-y-4">
      <RangeFilter state={range} />
      {err && <ErrorNote>{err}</ErrorNote>}
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
