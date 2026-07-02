import { TrendingUp, Target, Layers, Activity } from "lucide-react";
import { Card, KPI, Table, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

export default function Dashboard() {
  const { data: kpi, error } = useData(api.dashboard);
  const { data: trades } = useData(api.trades);

  if (error) return <Card><Empty>Can't reach the API. Check your token and that the API is up.</Empty></Card>;
  if (!kpi) return <Card><Empty>Loading…</Empty></Card>;

  const recent = (trades || []).slice(0, 8);
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPI label="Realized P&L" grad={kpi.total_pl >= 0 ? "b" : "c"} icon={TrendingUp}
          value={money(kpi.total_pl)} sub="closed legs" />
        <KPI label="Win rate" grad="a" icon={Target} value={`${kpi.win_rate}%`} sub="TP hits / closed" />
        <KPI label="Open positions" grad="d" icon={Layers} value={kpi.open_legs} sub={`${kpi.open_trades} trades`} />
        <KPI label="Total trades" grad="a" icon={Activity} value={kpi.total_trades} sub="all time" />
      </div>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Recent trades</div>
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
