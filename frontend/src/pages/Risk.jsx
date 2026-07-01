import { useEffect, useState } from "react";
import { Pencil } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Field, Input, Toggle, Button, ErrorNote } from "../components/form";
import RiskConfigEditor from "../components/RiskConfigEditor";
import { api } from "../lib/api";

const summarize = (r) => {
  if (!r || !Object.keys(r).length) return "—";
  const base = r.basis === "fixed_cash" ? `$${r.value}` : `${r.value}% equity`;
  if (r.allocation === "per_tp") {
    const per = Object.entries(r.per_tp_percent || {}).map(([k, v]) => `TP${k}:${v}%`).join(" ");
    return `${base} · per-TP · ${per}`;
  }
  return `${base} · even`;
};

export default function Risk() {
  const [accounts, setAccounts] = useState([]);
  const [sources, setSources] = useState([]);
  const [err, setErr] = useState(null);
  const [edit, setEdit] = useState(null);
  const load = async () => {
    try { setAccounts(await api.accounts()); setSources(await api.sources()); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Account limits</div>
        {!accounts.length ? <Empty>No accounts. Add one under Brokers.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge"><Th>Account</Th><Th>Risk / limits</Th><Th>Trading</Th><Th right></Th></tr></thead>
            <tbody>
              {accounts.map(a => (
                <tr key={a.id} className="border-b border-edge/60">
                  <Td>{a.name}</Td>
                  <Td><span className="text-xs num text-muted">{summarize(a.risk_config)}</span></Td>
                  <Td><Badge tone={a.enabled ? "long" : "muted"}>{a.enabled ? "on" : "off"}</Badge></Td>
                  <Td right><Button variant="ghost" onClick={() => setEdit(a)}><Pencil className="w-4 h-4" /></Button></Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Per-source overrides</div>
        {!sources.length ? <Empty>No sources.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge"><Th>Source</Th><Th>Override</Th></tr></thead>
            <tbody>
              {sources.map(s => (
                <tr key={s.id} className="border-b border-edge/60">
                  <Td>{s.name}</Td>
                  <Td><span className="text-xs num text-muted">
                    {s.risk_config && Object.keys(s.risk_config).length ? summarize(s.risk_config) : "inherits account"}
                  </span></Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">Edit source overrides under Sources.</div>
      </Card>

      {edit && <RiskModal account={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); load(); }} />}
    </div>
  );
}

function RiskModal({ account, onClose, onSaved }) {
  const [risk, setRisk] = useState(account.risk_config || { basis: "capital_percent", value: "1.0", allocation: "even" });
  const [err, setErr] = useState(null);
  const save = async () => {
    try { await api.updateAccount(account.id, { risk_config: risk }); onSaved(); }
    catch (e) { setErr(e.message); }
  };
  return (
    <Modal title={`Limits — ${account.name}`} onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      <RiskConfigEditor value={risk} onChange={setRisk} />
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>Save</Button>
      </div>
    </Modal>
  );
}
