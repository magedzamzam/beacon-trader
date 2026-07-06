import { Fragment, useEffect, useState } from "react";
import { Bell, Mail, Send, MessageCircle, Smartphone, Webhook, BellRing } from "lucide-react";
import { Card, Empty } from "../components/ui";
import { Button, Field, Input, NumberInput, Select, Toggle, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const CHANNEL_ICON = {
  email: Mail, telegram: Send, whatsapp: MessageCircle,
  sms: Smartphone, webhook: Webhook, push: BellRing,
};

/**
 * Notifications — configure delivery channels (Email / Telegram / WhatsApp /
 * SMS / Webhook / Push) and route each event type to one or more channels.
 * Configuration only for now: nothing is dispatched yet. Secrets are write-only
 * (encrypted server-side); the UI only ever learns whether one is set.
 */
export default function Notifications() {
  const [cat, setCat] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [secrets, setSecrets] = useState({});   // "channel.field" -> typed value
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);

  const load = async () => {
    try {
      const [c, g] = await Promise.all([api.notificationsCatalog(), api.notificationsConfig()]);
      setCat(c); setCfg(g); setErr(null);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  if (err && !cfg) return <ErrorNote>{err}</ErrorNote>;
  if (!cat || !cfg) return <Card><Empty>Loading…</Empty></Card>;

  const touch = () => setSaved(false);
  const setEnabled = (chId, v) => { setCfg(c => ({ ...c, channels: { ...c.channels, [chId]: { ...c.channels[chId], enabled: v } } })); touch(); };
  const setField = (chId, name, v) => { setCfg(c => ({ ...c, channels: { ...c.channels, [chId]: { ...c.channels[chId], [name]: v } } })); touch(); };
  const setSecret = (chId, name, v) => { setSecrets(s => ({ ...s, [`${chId}.${name}`]: v })); touch(); };
  const toggleRoute = (eventId, chId) => {
    setCfg(c => {
      const cur = c.routing[eventId] || [];
      const next = cur.includes(chId) ? cur.filter(x => x !== chId) : [...cur, chId];
      return { ...c, routing: { ...c.routing, [eventId]: next } };
    });
    touch();
  };

  const save = async () => {
    setErr(null);
    try {
      const channels = {};
      for (const ch of cat.channels) {
        const c = cfg.channels[ch.id] || {};
        const out = { enabled: !!c.enabled };
        for (const f of ch.fields) {
          if (f.secret) {
            const key = `${ch.id}.${f.name}`;
            if (secrets[key]) out[f.name] = secrets[key];   // only send new secrets
          } else {
            out[f.name] = c[f.name];
          }
        }
        channels[ch.id] = out;
      }
      const res = await api.saveNotificationsConfig({ channels, routing: cfg.routing });
      setCfg(res); setSecrets({}); setSaved(true);
    } catch (e) { setErr(e.message); }
  };

  const field = (ch, f) => {
    const c = cfg.channels[ch.id] || {};
    if (f.type === "bool")
      return <Toggle checked={!!c[f.name]} onChange={v => setField(ch.id, f.name, v)} label={c[f.name] ? "on" : "off"} />;
    if (f.secret) {
      const key = `${ch.id}.${f.name}`;
      return <Input type="password" autoComplete="new-password" value={secrets[key] || ""}
        onChange={e => setSecret(ch.id, f.name, e.target.value)}
        placeholder={c[`has_${f.name}`] ? "•••••••• saved — leave blank to keep" : (f.placeholder || "")} />;
    }
    if (f.type === "select")
      return <Select value={c[f.name] ?? f.default ?? ""} onChange={e => setField(ch.id, f.name, e.target.value)}>
        {(f.options || []).map(o => <option key={o} value={o}>{o}</option>)}</Select>;
    if (f.type === "number")
      return <NumberInput value={c[f.name] ?? ""} placeholder={f.placeholder || ""}
        onChange={e => setField(ch.id, f.name, e.target.value === "" ? "" : Number(e.target.value))} />;
    return <Input value={c[f.name] ?? ""} placeholder={f.placeholder || ""}
      onChange={e => setField(ch.id, f.name, e.target.value)} />;
  };

  return (
    <div className="space-y-5">
      <ErrorNote>{err}</ErrorNote>
      {!cfg.has_secret_key && (
        <div className="text-xs text-warn bg-warn/10 rounded-lg px-3 py-2">
          SECRET_KEY is not set — you can configure channels and routing, but secret fields
          (tokens, passwords) can't be stored encrypted. Set SECRET_KEY and restart.
        </div>
      )}

      {/* ---- Channels ---- */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm font-medium flex items-center gap-2"><Bell className="w-4 h-4 text-beacon" /> Channels</div>
          <span className="text-[11px] text-muted">Configuration only — delivery isn't wired up yet.</span>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {cat.channels.map(ch => {
            const Icon = CHANNEL_ICON[ch.id] || Bell;
            const c = cfg.channels[ch.id] || {};
            return (
              <Card key={ch.id}>
                <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
                  <div className="text-sm font-medium flex items-center gap-2">
                    <Icon className={`w-4 h-4 ${c.enabled ? "text-beacon" : "text-muted"}`} />
                    {ch.label}
                    <span className="text-[10px] uppercase tracking-wider text-muted border border-edge rounded px-1.5 py-0.5">{ch.hint}</span>
                  </div>
                  <Toggle checked={!!c.enabled} onChange={v => setEnabled(ch.id, v)} label={c.enabled ? "enabled" : "off"} />
                </div>
                <div className={`p-4 grid grid-cols-1 sm:grid-cols-2 gap-3 ${c.enabled ? "" : "opacity-60"}`}>
                  {ch.fields.map(f => (
                    <div key={f.name} className={f.type === "bool" ? "sm:col-span-2 flex items-center justify-between" : ""}>
                      {f.type === "bool"
                        ? <><span className="text-xs uppercase tracking-wider text-muted">{f.label}</span>{field(ch, f)}</>
                        : <Field label={f.label}>{field(ch, f)}</Field>}
                    </div>
                  ))}
                </div>
              </Card>
            );
          })}
        </div>
      </div>

      {/* ---- Routing matrix ---- */}
      <Card>
        <div className="px-4 py-3 border-b border-edge">
          <div className="text-sm font-medium">Event routing</div>
          <div className="text-[11px] text-muted mt-0.5">
            Tick the channels each event should be sent to. A channel must be <b>enabled</b> above to actually deliver.
          </div>
        </div>
        <div className="p-4 overflow-x-auto">
          <table className="w-full text-sm border-collapse min-w-[560px]">
            <thead>
              <tr className="text-muted">
                <th className="text-left font-medium py-2 pr-3 sticky left-0 bg-panel">Event</th>
                {cat.channels.map(ch => {
                  const on = cfg.channels[ch.id]?.enabled;
                  return (
                    <th key={ch.id} className="px-2 py-2 font-medium text-center">
                      <div className={`flex items-center justify-center gap-1 ${on ? "" : "text-muted/60"}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${on ? "bg-long" : "bg-edge"}`} />
                        <span className="text-[11px]">{ch.label}</span>
                      </div>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {cat.event_groups.map(g => (
                <Fragment key={g.group}>
                  <tr>
                    <td colSpan={cat.channels.length + 1}
                      className="pt-4 pb-1 text-[10px] uppercase tracking-wider text-muted sticky left-0 bg-panel">
                      {g.group}
                    </td>
                  </tr>
                  {g.events.map(e => (
                    <tr key={e.id} className="border-t border-edge/60 hover:bg-panel2/40">
                      <td className="py-2 pr-3 sticky left-0 bg-panel">{e.label}</td>
                      {cat.channels.map(ch => (
                        <td key={ch.id} className="text-center py-2">
                          <input type="checkbox" className="w-4 h-4 accent-beacon cursor-pointer align-middle"
                            checked={(cfg.routing[e.id] || []).includes(ch.id)}
                            onChange={() => toggleRoute(e.id, ch.id)} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="flex items-center justify-end gap-3">
        {saved && <span className="text-xs text-long">Saved.</span>}
        <Button onClick={save}>Save notifications</Button>
      </div>

      {/* ---- Still-planned capabilities ---- */}
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Planned capabilities</div>
        <ul className="p-4 space-y-1.5 text-sm text-muted">
          {[
            "Severity thresholds and quiet hours",
            "Daily / weekly performance digest scheduling",
            "Escalation when a broker connection drops",
          ].map(x => (
            <li key={x} className="flex items-start gap-2">
              <span className="mt-1.5 w-1 h-1 rounded-full bg-muted shrink-0" />{x}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
