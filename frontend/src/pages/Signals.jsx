import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData } from "./_useData";

export default function Signals() {
  const { data } = useData(api.signals);
  if (!data) return <Card><Empty>Loading…</Empty></Card>;
  if (!data.length) return <Card><Empty>No signals received yet.</Empty></Card>;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Signal feed</div>
      <table className="w-full">
        <thead><tr className="border-b border-edge">
          <Th>#</Th><Th>Provider</Th><Th>Symbol</Th><Th>Side</Th><Th right>Entry</Th><Th right>SL</Th>
          <Th>TPs</Th><Th>Type</Th><Th>Status</Th>
        </tr></thead>
        <tbody>
          {data.map(s => (
            <tr key={s.id} className="border-b border-edge/60">
              <Td mono>{s.id}</Td>
              <Td>{s.source_name}{s.source_kind && <span className="text-[10px] text-muted ml-1">{s.source_kind}</span>}</Td>
              <Td>{s.symbol}</Td>
              <Td><Badge tone={s.direction === "BUY" ? "long" : "short"}>{s.direction}</Badge></Td>
              <Td right mono>{s.entry_from}{s.entry_to !== s.entry_from ? `–${s.entry_to}` : ""}</Td>
              <Td right mono>{s.sl}</Td>
              <Td mono>{(s.tps || []).join(" / ")}</Td>
              <Td>{s.order_type}</Td>
              <Td><Badge tone={s.status === "rejected" ? "short" : s.status === "executed" ? "long" : "beacon"}>
                {s.status}</Badge>{s.reject_reason && <div className="text-[10px] text-muted mt-0.5">{s.reject_reason}</div>}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
