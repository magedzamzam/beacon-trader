import { TrendingUp, Target, Layers, Activity, Gauge, CheckCheck, ArrowRight } from "lucide-react";
import { Card, KPI, Table, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

/** Small "View more →" link that jumps to a detailed page via the app nav. */
function ViewMore({ onClick }) {
  if (!onClick) return null;
  return (
    <button onClick={onClick}
      className="text-xs text-beacon hover:underline inline-flex items-center gap-1">
      View more <ArrowRight className="w-3 h-3" />
    </button>
  );
}

export default function Dashboard({ setView }) {
  const { data: kpi, error } = useData(api.dashboard);
  const { data: trades } = useData(api.trades);
  const { data: perf } = useData(api.perfSummary);
  const { data: bySrc } = useData(api.perfBySource);
  const go = (v) => setView && setView(v);

  if (error) return <Card><Empty>Can't reach the API. Check your token and that the API is up.</Empty></Card>;
  if (!kpi) return <Card><Empty>Loading…</Empty></Card>;

  const recent = (trades || []).slice(0, 8);
  const topSrc = (bySrc || []).slice(0, 5);

  return (
    <div className="space-y-6">
      {/* Performance metrics — the full snapshot, no separate Performance page needed */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <KPI label="Realized P&L" grad={kpi.total_pl >= 0 ? "b" : "c"} icon={TrendingUp}
          value={money(kpi.total_pl)} sub="closed legs" />
        <KPI label="Win rate" grad="a" icon={Target} value={`${kpi.win_rate}%`}
          sub={perf ? `${perf.wins}W / ${perf.losses}L` : "TP hits / closed"} />
        <KPI label="Profit factor" grad="a" icon={Gauge}
          value={perf?.profit_factor ?? "—"} sub="gross win / loss" />
        <KPI label="Open positions" grad="d" icon={Layers}
          value={kpi.open_legs} sub={`${kpi.open_trades} trades`} />
        <KPI label="Total trades" grad="a" icon={Activity} value={kpi.total_trades} sub="all time" />
        <KPI label="Closed legs" grad="b" icon={CheckCheck}
          value={perf?.closed_legs ?? "—"} sub="settled" />
      </div>

      {/* Performance by source preview */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Performance by source</div>
          <ViewMore onClick={() => go("performance")} />
        </div>
        {!topSrc.length ? <Empty>No closed legs to evaluate yet.</Empty> : (
          <Table>
            <thead><tr>
              <Th>Source</Th><Th right>P&L</Th><Th right>TP1</Th><Th right>TP2</Th><Th right>SL hits</Th>
            </tr></thead>
            <tbody>
              {topSrc.map(s => (
                <tr key={s.source_id} className="row-hover">
                  <Td>{s.name}</Td>
                  <Td right mono><span className={`text-${tone(s.pl)}`}>{money(s.pl)}</span></Td>
                  <Td right mono>{s.tp_hits?.[1] || 0}</Td>
                  <Td right mono>{s.tp_hits?.[2] || 0}</Td>
                  <Td right mono><span className="text-short">{s.sl_hits}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {/* Recent trades */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Recent trades</div>
          <ViewMore onClick={() => go("history")} />
        </div>
        {recent.length === 0 ? <Empty>No trades yet. Send a signal to the manual desk to see the fanout.</Empty> : (
          <Table>
            <thead><tr>
              <Th>#</Th><Th>Symbol</Th><Th>Side</Th><Th>Status</Th>
              <Th right>Legs</Th><Th right>Risk</Th><Th right>P&L</Th>
            </tr></thead>
            <tbody>
              {recent.map(t => (
                <tr key={t.id} className="row-hover">
                  <Td mono>{t.id}</Td>
                  <Td>{t.symbol}</Td>
                  <Td><Badge dot tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
                  <Td><Badge dot tone={t.status === "closed" ? "muted" : "beacon"}>{t.status}</Badge></Td>
                  <Td right mono>{t.legs.length}</Td>
                  <Td right mono>{t.planned_risk != null ? Number(t.planned_risk).toFixed(2) : "—"}</Td>
                  <Td right mono><span className={`text-${tone(t.realized_pl)}`}>{money(t.realized_pl)}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
