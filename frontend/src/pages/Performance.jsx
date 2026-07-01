import { Card, KPI, Th, Td, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

export default function Performance() {
  const { data: sum } = useData(api.perfSummary);
  const { data: bySrc } = useData(api.perfBySource);
  if (!sum) return <Card><Empty>Loading…</Empty></Card>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPI label="Realized P&L" value={money(sum.total_pl)} tone={tone(sum.total_pl)} />
        <KPI label="Win rate" value={`${sum.win_rate}%`} tone="beacon" sub={`${sum.wins}W / ${sum.losses}L`} />
        <KPI label="Profit factor" value={sum.profit_factor ?? "—"} sub="gross win / loss" />
        <KPI label="Closed legs" value={sum.closed_legs} />
      </div>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">By source — which channel actually reaches TP</div>
        {!bySrc || !bySrc.length ? <Empty>No closed legs to evaluate yet.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Source</Th><Th right>P&L</Th><Th right>TP1</Th><Th right>TP2</Th>
              <Th right>TP3+</Th><Th right>SL hits</Th>
            </tr></thead>
            <tbody>
              {bySrc.map(s => {
                const tp3plus = Object.entries(s.tp_hits).filter(([k]) => +k >= 3)
                  .reduce((a, [, v]) => a + v, 0);
                return (
                  <tr key={s.source_id} className="border-b border-edge/60">
                    <Td>{s.name}</Td>
                    <Td right mono><span className={`text-${tone(s.pl)}`}>{money(s.pl)}</span></Td>
                    <Td right mono>{s.tp_hits[1] || 0}</Td>
                    <Td right mono>{s.tp_hits[2] || 0}</Td>
                    <Td right mono>{tp3plus}</Td>
                    <Td right mono><span className="text-short">{s.sl_hits}</span></Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
