import { useEffect, useState } from "react";
import { Plus, Trash2, Pencil } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Field, Input, Select, Toggle, Button, ErrorNote } from "../components/form";
import RiskConfigEditor from "../components/RiskConfigEditor";
import SlRulesEditor from "../components/SlRulesEditor";
import { api } from "../lib/api";

const KIND_LABEL = {
  telegram: "Channel ID", tradingview: "Webhook key", api: "API key", manual: "Key (optional)",
};

export default function Sources() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(null);   // source object or "new"

  const load = async () => {
    try { setSources(await api.sources()); setAccounts(await api.accounts()); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Signal sources</div>
          <Button onClick={() => setEditing("new")}><Plus className="w-4 h-4 inline -mt-0.5" /> Add source</Button>
        </div>
        {!sources.length ? <Empty>No sources. Add a Telegram channel or a webhook to start.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Name</Th><Th>Kind</Th><Th>Ref</Th>
              <Th right>Accounts</Th><Th>Trading</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {sources.map(s => (
                <tr key={s.id} className="border-b border-edge/60">
                  <Td>{s.name}</Td>
                  <Td><Badge>{s.kind}</Badge></Td>
                  <Td mono>{s.external_id || "—"}</Td>
                  <Td right mono>{(s.account_map || []).length}</Td>
                  <Td><Toggle checked={s.enabled_for_trading}
                    onChange={async v => { await api.updateSource(s.id, { enabled_for_trading: v }); load(); }} /></Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" onClick={() => setEditing(s)}><Pencil className="w-4 h-4" /></Button>
                      <Button variant="danger" onClick={async () => { await api.deleteSource(s.id); load(); }}><Trash2 className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {editing && <SourceModal source={editing === "new" ? null : editing} accounts={accounts}
        onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

function SourceModal({ source, accounts, onClose, onSaved }) {
  const s = source || {};
  const strat = s.strategy || {};
  const [kind, setKind] = useState(s.kind || "telegram");
  const [name, setName] = useState(s.name || "");
  const [externalId, setExternalId] = useState(s.external_id || "");
  const [ttl, setTtl] = useState(strat.entry_ttl_minutes ?? 60);
  const [trusted, setTrusted] = useState(s.is_trusted || false);
  const [enabled, setEnabled] = useState(s.enabled_for_trading || false);
  const [accountMap, setAccountMap] = useState(s.account_map || []);
  const [useRisk, setUseRisk] = useState(!!(s.risk_config && Object.keys(s.risk_config).length));
  const [risk, setRisk] = useState(s.risk_config && Object.keys(s.risk_config).length
    ? s.risk_config : { basis: "capital_percent", value: "1.0", allocation: "even" });
  const [slRules, setSlRules] = useState(strat.sl_rules || []);
  const [err, setErr] = useState(null);

  const toggleAcct = (id) => setAccountMap(m => m.includes(id) ? m.filter(x => x !== id) : [...m, id]);

  const save = async () => {
    const payload = {
      kind, name, external_id: externalId || null,
      is_trusted: trusted, enabled_for_trading: enabled,
      strategy: { entry_ttl_minutes: +ttl, sl_rules: slRules },
      risk_config: useRisk ? risk : {},
      account_map: accountMap,
    };
    try {
      if (source) await api.updateSource(source.id, payload);
      else await api.createSource(payload);
      onSaved();
    } catch (e) { setErr(e.message); }
  };

  return (
    <Modal title={source ? `Edit ${source.name}` : "Add source"} onClose={onClose} wide>
      <ErrorNote>{err}</ErrorNote>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Kind">
          <Select value={kind} onChange={e => setKind(e.target.value)}>
            <option value="telegram">Telegram channel</option>
            <option value="tradingview">TradingView webhook</option>
            <option value="api">Generic API</option>
            <option value="manual">Manual desk</option>
          </Select>
        </Field>
        <Field label="Name"><Input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. GoldGA" /></Field>
      </div>
      <Field label={KIND_LABEL[kind]}
        hint={kind === "telegram" ? "The channel id, e.g. -1001220837618" : "Used as the webhook auth key in /ingest/tv/<key>"}>
        <Input mono value={externalId} onChange={e => setExternalId(e.target.value)} />
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Entry TTL (min)"
          hint="orders rest as LIMIT; a leg is auto-MARKET if the candle already crossed its entry. Cancels an unfilled limit after N min (0 = never).">
          <Input type="number" value={ttl} onChange={e => setTtl(e.target.value)} />
        </Field>
      </div>

      <div className="flex gap-6">
        <Field label="Trusted"><Toggle checked={trusted} onChange={setTrusted} /></Field>
        <Field label="Enabled for trading"><Toggle checked={enabled} onChange={setEnabled} /></Field>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-muted mb-1.5">Route to accounts</div>
        {accounts.length === 0 ? <div className="text-xs text-muted">No accounts yet — add one under Brokers first.</div> : (
          <div className="grid grid-cols-2 gap-2">
            {accounts.map(a => (
              <label key={a.id} className="flex items-center gap-2 text-sm border border-edge rounded-lg px-3 py-2 bg-panel2 cursor-pointer">
                <input type="checkbox" checked={accountMap.includes(a.id)} onChange={() => toggleAcct(a.id)} />
                {a.name} <span className="text-muted num text-xs">{a.broker_account_id}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <div className="text-xs uppercase tracking-wider text-muted">Risk override</div>
          <Toggle checked={useRisk} onChange={setUseRisk} label={useRisk ? "custom" : "inherit account"} />
        </div>
        {useRisk && <RiskConfigEditor value={risk} onChange={setRisk} />}
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-muted mb-1.5">Stop-loss rules</div>
        <SlRulesEditor rules={slRules} onChange={setSlRules} />
      </div>

      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>{source ? "Save" : "Add source"}</Button>
      </div>
    </Modal>
  );
}
