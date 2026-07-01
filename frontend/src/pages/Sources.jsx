import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData } from "./_useData";

export default function Sources() {
  const { data } = useData(api.sources);
  if (!data) return <Card><Empty>Loading…</Empty></Card>;
  if (!data.length) return <Card><Empty>No sources. Seed the DB or add one via the API.</Empty></Card>;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Signal sources</div>
      <table className="w-full">
        <thead><tr className="border-b border-edge">
          <Th>Name</Th><Th>Kind</Th><Th>Order</Th><Th>TP strategy</Th>
          <Th>Trading</Th><Th>Trusted</Th>
        </tr></thead>
        <tbody>
          {data.map(s => (
            <tr key={s.id} className="border-b border-edge/60">
              <Td>{s.name}</Td>
              <Td><Badge>{s.kind}</Badge></Td>
              <Td>{s.strategy?.order_position_type || "—"}</Td>
              <Td mono>{s.strategy?.tp_strategy || "—"}</Td>
              <Td><Badge tone={s.enabled_for_trading ? "long" : "muted"}>
                {s.enabled_for_trading ? "on" : "off"}</Badge></Td>
              <Td><Badge tone={s.is_trusted ? "beacon" : "muted"}>
                {s.is_trusted ? "yes" : "no"}</Badge></Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
