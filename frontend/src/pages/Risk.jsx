import { useEffect, useState } from "react";
import { Pencil } from "lucide-react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
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
      <RiskLimitsCard />
      <AccountSourceRiskCard accounts={accounts} sources={sources} />
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Account limits</div>
        {!accounts.length ? <Empty>No accounts. Add one under Brokers.</Empty> : (
          <Table>
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
          </Table>
        )}
      </Card>

      {edit && <RiskModal account={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); load(); }} />}
    </div>
  );
}

function RiskLimitsCard() {
  const [cfg, setCfg] = useState(null);
  const [status, setStatus] = useState(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);
  const loadStatus = () => api.riskStatus().then(setStatus).catch(() => {});
  useEffect(() => { api.riskLimits().then(setCfg).catch(e => setErr(e.message)); loadStatus(); }, []);
  if (!cfg) return null;
  const set = (k, v) => { setCfg(c => ({ ...c, [k]: v })); setSaved(false); };
  const num = (k, v) => set(k, v === "" ? "" : Number(v));
  const save = async () => {
    try { setCfg(await api.saveRiskLimits(cfg)); setSaved(true); loadStatus(); } catch (e) { setErr(e.message); }
  };
  const disarm = async () => {   // one-click: fully disable the daily floor
    try { setCfg(await api.saveRiskLimits({ ...cfg, daily_loss_limit: 0 })); setSaved(true); loadStatus(); }
    catch (e) { setErr(e.message); }
  };
  const blockedAccts = (status?.accounts || []).filter(a => a.blocked);
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
        <div className="text-sm font-medium">Risk limits &amp; kill switch</div>
        <div className="flex items-center gap-2">
          {status?.blocked && <Badge tone="short">trading blocked</Badge>}
          {!cfg.enabled && <Badge tone="warn">limits off</Badge>}
          {cfg.trading_halted && <Badge tone="warn">halted</Badge>}
          {saved && <span className="text-xs text-long">Saved</span>}
        </div>
      </div>
      <div className="p-4 space-y-4">
        <ErrorNote>{err}</ErrorNote>

        {status?.blocked && (
          <div className="rounded-lg px-3 py-2 text-xs bg-short/15 text-short border border-short/30">
            <div className="font-medium">Trading is currently blocked:</div>
            {blockedAccts.map(a => (
              <div key={a.account_id} className="num mt-0.5"><b>{a.name}</b> — {a.reason}</div>
            ))}
            <button onClick={disarm} className="mt-1.5 underline">Set daily-loss limit to 0 (disarm now)</button>
          </div>
        )}

        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <label className="flex items-center gap-2 text-sm">Enforce limits
            <Toggle checked={!!cfg.enabled} onChange={v => set("enabled", v)} /></label>
          <label className="flex items-center gap-2 text-sm">
            <span className={cfg.trading_halted ? "text-warn font-medium" : ""}>Kill switch — halt all new trades</span>
            <Toggle checked={!!cfg.trading_halted} onChange={v => set("trading_halted", v)} /></label>
        </div>
        <div className="text-[11px] text-muted -mt-2">
          Turning <b>Enforce limits</b> off disables the daily-loss floor and every cap (the kill-switch is a
          separate button that still halts when on). All limits are stored here — nothing is hardcoded.
          To disarm only the floor while keeping caps, set the daily-loss limit to <b>0</b>.
          {status?.configured === false && " No risk_limits saved yet — a conservative fail-safe is active until you Save."}
        </div>
        <div className={`grid grid-cols-1 sm:grid-cols-2 gap-3 ${cfg.enabled ? "" : "opacity-60"}`}>
          <Field label="Daily loss limit (account ccy)">
            <Input type="number" value={cfg.daily_loss_limit} onChange={e => num("daily_loss_limit", e.target.value)} /></Field>
          <Field label="Per-signal ceiling (× daily limit)" hint="0.5 = one trade may risk ≤ 50% of the daily cap">
            <Input type="number" step="0.05" value={cfg.per_signal_max_pct_of_daily}
              onChange={e => num("per_signal_max_pct_of_daily", e.target.value)} /></Field>
          <Field label="Max open risk / account">
            <Input type="number" value={cfg.max_open_risk_per_account} onChange={e => num("max_open_risk_per_account", e.target.value)} /></Field>
          <Field label="Max open risk / symbol">
            <Input type="number" value={cfg.max_open_risk_per_symbol} onChange={e => num("max_open_risk_per_symbol", e.target.value)} /></Field>
          <Field label="Per-signal risk cap (% equity)" hint="#78 · one signal's whole fanout (all entry×TP legs) is scaled to ≤ this % of equity · 0 = off">
            <Input type="number" step="0.25" value={cfg.max_signal_risk_pct ?? 2.0}
              onChange={e => num("max_signal_risk_pct", e.target.value)} /></Field>
        </div>
        <div className="flex justify-end"><Button onClick={save}>Save risk limits</Button></div>
      </div>
    </Card>
  );
}

// Entry chase guard (#67) — a MARKET-hint signal only fills at market when price
// is near the signalled entry; beyond the tolerance it rests a LIMIT (or skips).
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

// Per-(account, source) risk override (#84) — risk relocated here from Sources.
// The overall per-account risk stays in "Account limits" above; this is the
// per-channel sizing for a specific account. Resolution: override -> account risk.
function AccountSourceRiskCard({ accounts = [], sources = [] }) {
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState({ account_id: "", source_id: "", enabled: true,
    risk_config: { basis: "capital_percent", value: "1.0", allocation: "even" } });
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = () => api.riskOverrides().then(setRows).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  const acctName = (id) => accounts.find((a) => String(a.id) === String(id))?.name || `#${id}`;
  const srcName = (id) => sources.find((s) => String(s.id) === String(id))?.name || `#${id}`;
  const set = (k, v) => { setForm((f) => ({ ...f, [k]: v })); setSaved(false); };

  const save = async () => {
    setErr(null); setSaved(false);
    if (!form.account_id || !form.source_id) { setErr("Pick an account and a source."); return; }
    try {
      await api.saveRiskOverride({ account_id: +form.account_id, source_id: +form.source_id,
        enabled: form.enabled, risk_config: form.risk_config });
      setSaved(true); load();
    } catch (e) { setErr(e.message); }
  };
  const edit = (r) => { setSaved(false); setForm({ account_id: String(r.account_id),
    source_id: String(r.source_id), enabled: r.enabled,
    risk_config: (r.risk_config && Object.keys(r.risk_config).length) ? r.risk_config
      : { basis: "capital_percent", value: "1.0", allocation: "even" } }); };
  const del = async (id) => { try { await api.deleteRiskOverride(id); load(); } catch (e) { setErr(e.message); } };

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Per-(account × source) risk
        <span className="text-muted font-normal"> · overrides the account's own risk for one channel</span></div>
      <div className="p-4 space-y-3">
        <ErrorNote>{err}</ErrorNote>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Field label="Account">
            <select value={form.account_id} onChange={(e) => set("account_id", e.target.value)}
              className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm w-full outline-none focus:border-beacon">
              <option value="">— pick —</option>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></Field>
          <Field label="Signal source">
            <select value={form.source_id} onChange={(e) => set("source_id", e.target.value)}
              className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm w-full outline-none focus:border-beacon">
              <option value="">— pick —</option>
              {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select></Field>
          <Field label="Enabled"><Toggle checked={form.enabled} onChange={(v) => set("enabled", v)} label={form.enabled ? "on" : "off"} /></Field>
        </div>
        <RiskConfigEditor value={form.risk_config} onChange={(v) => set("risk_config", v)} />
        <div className="flex items-center justify-end gap-3">
          {saved && <span className="text-xs text-long">Saved</span>}
          <Button onClick={save}>Save risk override</Button>
        </div>
      </div>
      {rows.length > 0 && (
        <Table>
          <thead><tr className="border-b border-edge"><Th>Account</Th><Th>Source</Th><Th>Risk</Th><Th>State</Th><Th right></Th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b border-edge/60">
                <Td>{acctName(r.account_id)}</Td>
                <Td>{srcName(r.source_id)}</Td>
                <Td className="text-xs">{summarize(r.risk_config)}</Td>
                <Td><Badge tone={r.enabled ? "long" : "muted"}>{r.enabled ? "on" : "off"}</Badge></Td>
                <Td right>
                  <button onClick={() => edit(r)} className="text-xs text-beacon hover:underline mr-3">edit</button>
                  <button onClick={() => del(r.id)} className="text-xs text-short hover:underline">remove</button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
