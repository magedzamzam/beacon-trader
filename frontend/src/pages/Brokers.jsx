import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData } from "./_useData";

export default function Brokers() {
  const { data: brokers } = useData(api.brokers);
  const { data: accounts } = useData(api.accounts);
  if (!brokers) return <Card><Empty>Loading…</Empty></Card>;
  return (
    <div className="space-y-6">
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Brokers</div>
        {!brokers.length ? <Empty>No brokers. Seed the DB or add one.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge"><Th>Name</Th><Th>Type</Th><Th>Mode</Th><Th>State</Th></tr></thead>
            <tbody>{brokers.map(b => (
              <tr key={b.id} className="border-b border-edge/60">
                <Td>{b.name}</Td><Td>{b.type}</Td>
                <Td><Badge tone={b.is_demo ? "warn" : "short"}>{b.is_demo ? "DEMO" : "LIVE"}</Badge></Td>
                <Td><Badge tone={b.enabled ? "long" : "muted"}>{b.enabled ? "enabled" : "off"}</Badge></Td>
              </tr>))}
            </tbody>
          </table>
        )}
      </Card>
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Accounts</div>
        {!accounts || !accounts.length ? <Empty>No accounts.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge"><Th>Name</Th><Th>Account</Th><Th>Ccy</Th><Th>Trading</Th></tr></thead>
            <tbody>{accounts.map(a => (
              <tr key={a.id} className="border-b border-edge/60">
                <Td>{a.name}</Td><Td mono>{a.broker_account_id}</Td><Td>{a.currency}</Td>
                <Td><Badge tone={a.enabled ? "long" : "muted"}>{a.enabled ? "on" : "off"}</Badge></Td>
              </tr>))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
