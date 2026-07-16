import { useEffect, useState } from "react";
import { GitBranch, Trash2 } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Field, Input, Select, Toggle, Button, ErrorNote } from "../components/form";
import { api } from "../lib/api";

/**
 * Strategies — per-(source, account) execution-policy overrides (#83).
 *
 * The SAME signal fanned out to two accounts can run DIFFERENT stop/ratchet rules,
 * so you can A/B exit strategies on identical signals, market and instant. The
 * executor snapshots the resolved rules onto each trade at entry, so editing here
 * only affects FUTURE trades — running A/B arms stay frozen (clean attribution).
 * Filter the Bayesian Analysis / Performance pages by account to compare arms.
 */
const mv = (target, extra = {}) => ({ type: "move_sl_to", target, ...extra });
const tp = (i) => ({ type: "tp_hit", index: i });
const PRESETS = {
  "BE at TP1 → trail": [{ trigger: tp(1), action: mv("entry") }, { trigger: tp(2), action: mv("previous_tp") }, { trigger: tp(3), action: mv("previous_tp") }],
  "BE at TP2 → trail": [{ trigger: tp(2), action: mv("entry") }, { trigger: tp(3), action: mv("previous_tp") }, { trigger: tp(4), action: mv("previous_tp") }],
  "BE at TP3 → trail": [{ trigger: tp(3), action: mv("entry") }, { trigger: tp(4), action: mv("previous_tp") }, { trigger: tp(5), action: mv("previous_tp") }],
  "Tighten early: +30pts → BE": [{ trigger: { type: "price_move", points: 30 }, action: mv("entry") }, { trigger: tp(2), action: mv("previous_tp") }],
};
const BLANK = { source_id: "", account_id: "", label: "", enabled: true, sl_json: "" };

export default function Strategies() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [form, setForm] = useState(BLANK);
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = () => api.abPolicies().then(setPolicies).catch((e) => setErr(e.message));
  useEffect(() => {
    api.sources().then(setSources).catch((e) => setErr(e.message));
    api.accounts().then(setAccounts).catch((e) => setErr(e.message));
    load();
  }, []);

  const srcName = (id) => sources.find((s) => String(s.id) === String(id))?.name || `#${id}`;
  const acctName = (id) => accounts.find((a) => String(a.id) === String(id))?.name || `#${id}`;
  const set = (k, v) => { setForm((f) => ({ ...f, [k]: v })); setSaved(false); };

  const save = async () => {
    setErr(null); setSaved(false);
    if (!form.source_id || !form.account_id) { setErr("Pick a channel and an account."); return; }
    let sl_rules = null;
    const t = (form.sl_json || "").trim();
    if (t) { try { sl_rules = JSON.parse(t); } catch { setErr("sl_rules is not valid JSON."); return; } }
    try {
      await api.saveAbPolicy({ source_id: +form.source_id, account_id: +form.account_id,
        label: form.label || null, enabled: form.enabled, sl_rules });
      setSaved(true); setForm(BLANK); load();
    } catch (e) { setErr(e.message); }
  };
  const edit = (p) => { setSaved(false); setForm({ source_id: String(p.source_id),
    account_id: String(p.account_id), label: p.label || "", enabled: p.enabled,
    sl_json: p.sl_rules ? JSON.stringify(p.sl_rules, null, 2) : "" }); };
  const del = async (id) => { try { await api.deleteAbPolicy(id); load(); } catch (e) { setErr(e.message); } };

  const inputCls = "w-full bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-beacon";
  return (
    <div className="space-y-5">
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
          <GitBranch className="w-4 h-4 text-beacon" /> Exit-rule A/B — per-(channel, account) SL override
        </div>
        <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
          The same signal opens on every mapped account at the same instant; give each account its own
          SL ratchet here to A/B exits on identical signals. Empty / disabled ⇒ the channel's default rule.
          Rules snapshot at entry, so edits only affect new trades. Compare arms via the account filter on
          <b> Bayesian Analysis</b> / <b>Performance</b>.
        </div>
        <div className="p-4 space-y-3">
          <ErrorNote>{err}</ErrorNote>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <Field label="Channel (source)">
              <Select value={form.source_id} onChange={(e) => set("source_id", e.target.value)}>
                <option value="">— pick —</option>
                {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
            </Field>
            <Field label="Account">
              <Select value={form.account_id} onChange={(e) => set("account_id", e.target.value)}>
                <option value="">— pick —</option>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </Select>
            </Field>
            <Field label="Arm label" hint="e.g. 'BE@TP2 arm'">
              <Input value={form.label} onChange={(e) => set("label", e.target.value)} />
            </Field>
            <Field label="Enabled" hint="off ⇒ falls back to the channel default">
              <Toggle checked={form.enabled} onChange={(v) => set("enabled", v)} label={form.enabled ? "override on" : "off"} />
            </Field>
          </div>
          <div>
            <div className="flex items-center gap-2 flex-wrap mb-1.5">
              <span className="text-xs text-muted">sl_rules (JSON) — presets:</span>
              {Object.keys(PRESETS).map((name) => (
                <button key={name} onClick={() => set("sl_json", JSON.stringify(PRESETS[name], null, 2))}
                  className="text-[11px] px-2 py-0.5 rounded-full border border-edge text-muted hover:border-beacon hover:text-beacon">
                  {name}</button>
              ))}
            </div>
            <textarea value={form.sl_json} onChange={(e) => set("sl_json", e.target.value)}
              rows={8} spellCheck={false} placeholder="[] or leave empty for the channel default"
              className={`${inputCls} font-mono text-xs`} />
          </div>
          <div className="flex items-center justify-end gap-3">
            {saved && <span className="text-xs text-long">Saved</span>}
            {form.source_id && form.account_id &&
              <span className="text-[11px] text-muted mr-auto">{srcName(form.source_id)} → {acctName(form.account_id)}</span>}
            <Button onClick={save}>Save override</Button>
          </div>
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Configured overrides</div>
        {!policies.length ? <Empty>No per-account overrides yet — every account runs its channel's default SL rule.</Empty> : (
          <Table minW={720}>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th>Account</Th><Th>Arm</Th><Th right>Rules</Th><Th>State</Th><Th right>v</Th><Th right></Th>
            </tr></thead>
            <tbody>
              {policies.map((p) => (
                <tr key={p.id} className="border-b border-edge/60">
                  <Td>{srcName(p.source_id)}</Td>
                  <Td>{acctName(p.account_id)}</Td>
                  <Td>{p.label || <span className="text-muted">—</span>}</Td>
                  <Td right mono>{Array.isArray(p.sl_rules) ? `${p.sl_rules.length} rule(s)` : "default"}</Td>
                  <Td><Badge tone={p.enabled ? "long" : "muted"}>{p.enabled ? "on" : "off"}</Badge></Td>
                  <Td right mono>{p.version}</Td>
                  <Td right>
                    <button onClick={() => edit(p)} className="text-xs text-beacon hover:underline mr-3">edit</button>
                    <button onClick={() => del(p.id)} className="text-xs text-short hover:underline inline-flex items-center gap-1">
                      <Trash2 className="w-3 h-3" /></button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
