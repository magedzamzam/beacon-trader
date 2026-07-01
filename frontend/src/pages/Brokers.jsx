import { useEffect, useState } from "react";
import { RefreshCw, Plus, Trash2, Activity, Download, Pencil } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Field, Input, Toggle, Button, Select, ErrorNote } from "../components/form";
import RiskConfigEditor from "../components/RiskConfigEditor";
import { api } from "../lib/api";
import { money } from "./_useData";

const DEFAULT_RISK = { basis: "capital_percent", value: "1.0", allocation: "even" };

export default function Brokers() {
  const [brokers, setBrokers] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [err, setErr] = useState(null);
  const [addBroker, setAddBroker] = useState(false);
  const [liveFor, setLiveFor] = useState(null);      // broker for account picker
  const [editAcct, setEditAcct] = useState(null);
  const [health, setHealth] = useState({});

  const load = async () => {
    try {
      setBrokers(await api.brokers());
      setAccounts(await api.accounts());
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  const checkHealth = async (id) => {
    setHealth(h => ({ ...h, [id]: { loading: true } }));
    try { const res = await api.brokerHealth(id); setHealth(h => ({ ...h, [id]: res })); }
    catch (e) { setHealth(h => ({ ...h, [id]: { ok: false, message: e.message } })); }
  };

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Brokers</div>
          <Button onClick={() => setAddBroker(true)}><Plus className="w-4 h-4 inline -mt-0.5" /> Add broker</Button>
        </div>
        {!brokers.length ? <Empty>No brokers yet. Add one to connect Capital.com.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Name</Th><Th>Type</Th><Th>Mode</Th><Th>State</Th><Th>Connection</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {brokers.map(b => (
                <tr key={b.id} className="border-b border-edge/60">
                  <Td>{b.name}</Td><Td>{b.type}</Td>
                  <Td><Badge tone={b.is_demo ? "warn" : "short"}>{b.is_demo ? "DEMO" : "LIVE"}</Badge></Td>
                  <Td><Badge tone={b.enabled ? "long" : "muted"}>{b.enabled ? "enabled" : "off"}</Badge></Td>
                  <Td>{health[b.id]
                    ? (health[b.id].loading ? <span className="text-muted text-xs">checking…</span>
                      : <span className={`text-xs ${health[b.id].ok ? "text-long" : "text-short"}`}>
                          {health[b.id].ok ? "connected" : (health[b.id].message || "failed")}</span>)
                    : <span className="text-muted text-xs">—</span>}</Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" onClick={() => checkHealth(b.id)} title="Test connection"><Activity className="w-4 h-4" /></Button>
                      <Button variant="ghost" onClick={() => setLiveFor(b)} title="Fetch accounts"><Download className="w-4 h-4" /></Button>
                      <Button variant="danger" onClick={async () => { await api.deleteBroker(b.id); load(); }}><Trash2 className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Accounts</div>
        {!accounts.length ? <Empty>No accounts. Use “Fetch accounts” on a broker to pull them from Capital.com.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Name</Th><Th>Account ID</Th><Th>Ccy</Th><Th>Risk</Th><Th>Trading</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {accounts.map(a => (
                <tr key={a.id} className="border-b border-edge/60">
                  <Td>{a.name}</Td><Td mono>{a.broker_account_id}</Td><Td>{a.currency}</Td>
                  <Td><span className="text-xs num text-muted">
                    {a.risk_config?.basis === "fixed_cash" ? `$${a.risk_config?.value}` : `${a.risk_config?.value || "—"}%`} · {a.risk_config?.allocation || "even"}
                  </span></Td>
                  <Td><Toggle checked={a.enabled} onChange={async v => { await api.updateAccount(a.id, { enabled: v }); load(); }} /></Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" onClick={() => setEditAcct(a)}><Pencil className="w-4 h-4" /></Button>
                      <Button variant="danger" onClick={async () => { await api.deleteAccount(a.id); load(); }}><Trash2 className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {addBroker && <AddBrokerModal onClose={() => setAddBroker(false)} onSaved={() => { setAddBroker(false); load(); }} />}
      {liveFor && <LiveAccountsModal broker={liveFor} existing={accounts} onClose={() => setLiveFor(null)}
        onAdded={() => { load(); }} />}
      {editAcct && <EditAccountModal account={editAcct} onClose={() => setEditAcct(null)}
        onSaved={() => { setEditAcct(null); load(); }} />}
    </div>
  );
}

function AddBrokerModal({ onClose, onSaved }) {
  const [name, setName] = useState("Capital");
  const [isDemo, setIsDemo] = useState(true);
  const [apiKeyEnv, setApiKeyEnv] = useState("CAP_API_KEY");
  const [userEnv, setUserEnv] = useState("CAP_USERNAME");
  const [passEnv, setPassEnv] = useState("CAP_PASSWORD");
  const [err, setErr] = useState(null);

  const save = async () => {
    try {
      await api.createBroker({
        type: "capital.com", name, is_demo: isDemo, enabled: true,
        credentials_ref: { api_key_env: apiKeyEnv, account_username_env: userEnv,
                           account_password_env: passEnv, is_demo: isDemo },
      });
      onSaved();
    } catch (e) { setErr(e.message); }
  };
  return (
    <Modal title="Add broker" onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      <Field label="Name"><Input value={name} onChange={e => setName(e.target.value)} /></Field>
      <Field label="Mode"><Toggle checked={isDemo} onChange={setIsDemo} label={isDemo ? "Demo" : "Live"} /></Field>
      <div className="text-xs text-muted">Credentials are read from these .env variables — the secrets never touch the database.</div>
      <div className="grid grid-cols-3 gap-3">
        <Field label="API key env"><Input mono value={apiKeyEnv} onChange={e => setApiKeyEnv(e.target.value)} /></Field>
        <Field label="Username env"><Input mono value={userEnv} onChange={e => setUserEnv(e.target.value)} /></Field>
        <Field label="Password env"><Input mono value={passEnv} onChange={e => setPassEnv(e.target.value)} /></Field>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>Add broker</Button>
      </div>
    </Modal>
  );
}

function LiveAccountsModal({ broker, existing, onClose, onAdded }) {
  const [live, setLive] = useState(null);
  const [err, setErr] = useState(null);
  const have = new Set(existing.filter(a => a.broker_id === broker.id).map(a => a.broker_account_id));

  useEffect(() => {
    api.brokerLiveAccounts(broker.id).then(setLive).catch(e => setErr(e.message));
  }, [broker.id]);

  const add = async (acc) => {
    try {
      await api.createAccount({
        broker_id: broker.id, broker_account_id: acc.broker_account_id,
        name: acc.name || acc.broker_account_id, currency: acc.currency || "USD",
        enabled: false, risk_config: DEFAULT_RISK,
      });
      onAdded();
    } catch (e) { setErr(e.message); }
  };

  return (
    <Modal title={`Accounts on ${broker.name}`} onClose={onClose} wide>
      <ErrorNote>{err}</ErrorNote>
      {!live ? <div className="text-sm text-muted">Fetching from Capital.com…</div> : (
        live.length === 0 ? <Empty>No accounts returned.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Name</Th><Th>Account ID</Th><Th right>Balance</Th><Th>Ccy</Th><Th right></Th>
            </tr></thead>
            <tbody>
              {live.map(a => (
                <tr key={a.broker_account_id} className="border-b border-edge/60">
                  <Td>{a.name} {a.preferred && <Badge tone="beacon">preferred</Badge>}</Td>
                  <Td mono>{a.broker_account_id}</Td>
                  <Td right mono>{a.balance ? money(+a.balance) : "—"}</Td>
                  <Td>{a.currency}</Td>
                  <Td right>{have.has(a.broker_account_id)
                    ? <span className="text-xs text-muted">added</span>
                    : <Button onClick={() => add(a)}>Add</Button>}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </Modal>
  );
}

function EditAccountModal({ account, onClose, onSaved }) {
  const [name, setName] = useState(account.name);
  const [risk, setRisk] = useState(account.risk_config || DEFAULT_RISK);
  const [enabled, setEnabled] = useState(account.enabled);
  const [err, setErr] = useState(null);
  const save = async () => {
    try { await api.updateAccount(account.id, { name, risk_config: risk, enabled }); onSaved(); }
    catch (e) { setErr(e.message); }
  };
  return (
    <Modal title={`Edit ${account.name}`} onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      <Field label="Name"><Input value={name} onChange={e => setName(e.target.value)} /></Field>
      <Field label="Enabled for trading"><Toggle checked={enabled} onChange={setEnabled} /></Field>
      <div className="text-xs uppercase tracking-wider text-muted">Risk / limits</div>
      <RiskConfigEditor value={risk} onChange={setRisk} />
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>Save</Button>
      </div>
    </Modal>
  );
}
