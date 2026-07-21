import { useState } from "react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import TradeDetail from "../components/TradeDetail";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

export default function History() {
  const { data: trades } = useData(api.trades);
  const [detail, setDetail] = useState(null);
  if (!trades) return <Card><Empty>Loading…</Empty></Card>;
  const rows = [];
  trades.forEach(t => t.legs.filter(l => l.status === "closed").forEach(l => rows.push({ t, l })));
  if (!rows.length) return <Card><Empty>No closed legs yet.</Empty></Card>;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Closed legs</div>
      <Table minW={880}>
        <thead><tr className="border-b border-edge">
          <Th>Trade</Th><Th>Signal</Th><Th>Symbol</Th><Th>Channel</Th><Th>Side</Th><Th right>TP#</Th>
          <Th right>Entry</Th><Th right>Close</Th><Th>Outcome</Th><Th right>P&L</Th>
        </tr></thead>
        <tbody>
          {rows.map(({ t, l }) => (
            <tr key={l.id} className="border-b border-edge/60">
              <Td mono><button className="text-beacon hover:underline" onClick={() => setDetail(t.id)}>{t.id}</button></Td>
              <Td mono>{t.signal_id != null ? `#${t.signal_id}` : "—"}</Td><Td>{t.symbol}</Td>
              <Td>
                <span className="truncate">{t.source_name || "—"}</span>
                {t.source_kind && <span className="text-[10px] text-muted ml-1">{t.source_kind}</span>}
              </Td>
              <Td><Badge tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
              <Td right mono>{l.tp_index}</Td>
              <Td right mono>{Number(l.entry).toFixed(2)}</Td>
              <Td right mono>{l.close_price != null ? Number(l.close_price).toFixed(2) : "—"}</Td>
              <Td><Badge tone={l.outcome === "tp_hit" ? "long" : l.outcome === "sl_hit" ? "short" : "muted"}>
                {l.outcome || "—"}</Badge></Td>
              <Td right mono><span className={`text-${tone(l.realized_pl)}`}>{money(l.realized_pl)}</span></Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {detail && <TradeDetail tradeId={detail} onClose={() => setDetail(null)} />}
    </Card>
  );
}
