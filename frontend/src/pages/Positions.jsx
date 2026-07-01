import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

const OPEN = new Set(["open", "working", "pending"]);

export default function Positions() {
  const { data: trades } = useData(api.trades);
  if (!trades) return <Card><Empty>Loading…</Empty></Card>;

  const rows = [];
  trades.forEach(t => t.legs.filter(l => OPEN.has(l.status)).forEach(l => rows.push({ t, l })));
  if (!rows.length) return <Card><Empty>No open positions.</Empty></Card>;

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Open legs</div>
      <table className="w-full">
        <thead><tr className="border-b border-edge">
          <Th>Trade</Th><Th>Symbol</Th><Th>Side</Th><Th>Type</Th><Th right>TP#</Th>
          <Th right>Entry</Th><Th right>SL</Th><Th right>TP</Th><Th right>Lot</Th><Th>State</Th>
        </tr></thead>
        <tbody>
          {rows.map(({ t, l }) => (
            <tr key={l.id} className="border-b border-edge/60">
              <Td mono>{t.id}</Td><Td>{t.symbol}</Td>
              <Td><Badge tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
              <Td>{l.order_type}</Td><Td right mono>{l.tp_index}</Td>
              <Td right mono>{Number(l.entry).toFixed(2)}</Td>
              <Td right mono>{Number(l.sl).toFixed(2)}{l.sl_moved && <span className="text-beacon"> •</span>}</Td>
              <Td right mono>{Number(l.tp).toFixed(2)}</Td>
              <Td right mono>{Number(l.lot).toFixed(2)}</Td>
              <Td><Badge tone="beacon">{l.status}</Badge></Td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="px-4 py-2 text-xs text-muted border-t border-edge">
        <span className="text-beacon">•</span> = stop-loss moved by a rule
      </div>
    </Card>
  );
}
