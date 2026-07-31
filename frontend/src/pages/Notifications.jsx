import { Fragment, useEffect, useRef, useState } from "react";
import { Bell, Mail, Send, MessageCircle, Smartphone, Webhook, BellRing, MessageSquare } from "lucide-react";
import { Card, Empty, Table, Th, Td } from "../components/ui";
import { Button, Field, Input, NumberInput, Select, Toggle, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const _inputCls = "w-full bg-panel2 border border-edge rounded-lg px-3 py-2 text-sm text-ink focus:border-beacon outline-none";

// Client-side mirror of templates.render (core) for the live preview: same
// `{token}` grammar + safe dotted key-walk. Kept trivially in sync — the tokens
// come from the server field descriptor, so the preview can't invent fields.
const _walk = (obj, path) =>
  path.split(".").reduce((o, k) => (o && typeof o === "object" && k in o ? o[k] : undefined), obj);
const renderPreview = (tmpl, sample) =>
  (tmpl || "").replace(/\{([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\}/g, (_, p) => {
    const v = _walk(sample, p);
    return v === undefined || v === null || v === "" ? "" : String(v);
  });

const CHANNEL_ICON = {
  email: Mail, telegram: Send, whatsapp: MessageCircle,
  sms: Smartphone, webhook: Webhook, push: BellRing,
};
// channels whose delivery (sender) is implemented — others are config-only
const BUILT = new Set(["email", "telegram", "sms"]);

/**
 * Notifications — configure delivery channels (Email / Telegram / WhatsApp /
 * SMS / Webhook / Push) and route each event type to one or more channels.
 * Email / Telegram / SMS deliver live (see `BUILT`) — the other channels store
 * config only, for now. Secrets are write-only (encrypted server-side); the UI
 * only ever learns whether one is set.
 */
export default function Notifications() {
  const [cat, setCat] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [secrets, setSecrets] = useState({});   // "channel.field" -> typed value
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState({});   // channelId -> bool
  const [testRes, setTestRes] = useState({});   // channelId -> {ok} | {ok:false,error}
  const [selEvent, setSelEvent] = useState(null);   // event id being template-edited
  const [evTestCh, setEvTestCh] = useState("");     // "" = routed channels
  const [evTesting, setEvTesting] = useState(false);
  const [evTestRes, setEvTestRes] = useState(null); // {results} | {error}
  const subjectRef = useRef(null);
  const bodyRef = useRef(null);
  const focusRef = useRef("body");   // which box a clicked chip inserts into

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
      const res = await api.saveNotificationsConfig({
        channels, routing: cfg.routing, templates: cfg.templates || {} });
      setCfg(res); setSecrets({}); setSaved(true);
      return true;
    } catch (e) { setErr(e.message); return false; }
  };

  // ---- per-event message templates (#139) ----
  const setTemplate = (eventId, part, value) => {
    setCfg(c => {
      const t = { ...(c.templates || {}) };
      const entry = { ...(t[eventId] || {}) };
      if (value) entry[part] = value; else delete entry[part];
      if (Object.keys(entry).length) t[eventId] = entry; else delete t[eventId];
      return { ...c, templates: t };
    });
    touch();
  };
  // Insert `{token}` at the caret of the subject/body box (click-a-chip path).
  const insertToken = (eventId, part, token) => {
    const el = (part === "subject" ? subjectRef : bodyRef).current;
    const cur = ((cfg.templates || {})[eventId] || {})[part] || "";
    const start = el && el.selectionStart != null ? el.selectionStart : cur.length;
    const end = el && el.selectionEnd != null ? el.selectionEnd : start;
    const tok = `{${token}}`;
    const next = cur.slice(0, start) + tok + cur.slice(end);
    setTemplate(eventId, part, next);
    requestAnimationFrame(() => {
      if (el) { el.focus(); const p = start + tok.length; el.setSelectionRange(p, p); }
    });
  };
  // Drop a dragged chip at the drop point (falls back to the caret / end).
  const dropToken = (eventId, part, e) => {
    e.preventDefault();
    const tok = e.dataTransfer.getData("text/plain");
    if (!tok) return;
    const el = (part === "subject" ? subjectRef : bodyRef).current;
    const cur = ((cfg.templates || {})[eventId] || {})[part] || "";
    let pos = cur.length;
    try {
      if (document.caretPositionFromPoint) {
        const cp = document.caretPositionFromPoint(e.clientX, e.clientY);
        if (cp && typeof cp.offset === "number") pos = cp.offset;
      } else if (el && el.selectionStart != null) { pos = el.selectionStart; }
    } catch { /* fall back to end */ }
    setTemplate(eventId, part, cur.slice(0, pos) + tok + cur.slice(pos));
  };
  // Fire a real notification for this event (sample data) so you can see the
  // template land in a channel. Saves first so the send uses what you just typed.
  const sendEventTest = async (eventId) => {
    setEvTestRes(null); setEvTesting(true);
    try {
      if (!(await save())) { setEvTesting(false); return; }
      const body = { event_id: eventId, send: true };
      if (evTestCh) body.channel_id = evTestCh;
      const res = await api.testNotificationEvent(body);
      setEvTestRes({ results: res.results || {} });
    } catch (e) {
      setEvTestRes({ error: e.message });
    } finally { setEvTesting(false); }
  };

  // Save first (so freshly-typed secrets are persisted+encrypted), then test.
  const test = async (chId) => {
    setTestRes(r => ({ ...r, [chId]: null }));
    setTesting(t => ({ ...t, [chId]: true }));
    try {
      if (!(await save())) return;
      await api.testNotificationChannel(chId);
      setTestRes(r => ({ ...r, [chId]: { ok: true } }));
    } catch (e) {
      setTestRes(r => ({ ...r, [chId]: { ok: false, error: e.message } }));
    } finally {
      setTesting(t => ({ ...t, [chId]: false }));
    }
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
          <span className="text-[11px] text-muted">Email, Telegram and SMS deliver live. WhatsApp / Webhook / Push are config-only for now.</span>
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
                <div className="px-4 py-2.5 border-t border-edge flex items-center gap-3 min-h-[44px]">
                  {BUILT.has(ch.id) ? (
                    <>
                      <Button variant="ghost" onClick={() => test(ch.id)} disabled={!c.enabled || testing[ch.id]}>
                        {testing[ch.id] ? "Sending…" : "Save & send test"}
                      </Button>
                      {testRes[ch.id]?.ok && <span className="text-xs text-long">✓ Test sent</span>}
                      {testRes[ch.id] && !testRes[ch.id].ok &&
                        <span className="text-xs text-short truncate">✗ {testRes[ch.id].error}</span>}
                    </>
                  ) : (
                    <span className="text-[11px] text-muted">Delivery for this channel isn't built yet — config is saved.</span>
                  )}
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

      {/* ---- Message templates (#139) ---- */}
      {(() => {
        const events = cat.event_groups.flatMap(g => g.events);
        const curEvent = selEvent || events[0]?.id;
        const curTmpl = (cfg.templates || {})[curEvent] || {};
        // honest per-event field list + preview sample (only what this event carries)
        const fields = (cat.fields_by_event && cat.fields_by_event[curEvent]) || cat.fields || [];
        const sample = (cat.samples && cat.samples[curEvent]) || cat.sample || {};
        const emitted = !cat.emitted || cat.emitted[curEvent] !== false;
        const testable = cat.channels.filter(ch => cfg.channels[ch.id]?.enabled && BUILT.has(ch.id));
        const box = (part, ref, rows, placeholder) => (
          <textarea
            ref={ref} rows={rows} className={`${_inputCls} font-mono`} placeholder={placeholder}
            value={curTmpl[part] || ""}
            onFocus={() => { focusRef.current = part; }}
            onChange={e => setTemplate(curEvent, part, e.target.value)}
            onDragOver={e => e.preventDefault()}
            onDrop={e => dropToken(curEvent, part, e)} />
        );
        return (
          <Card>
            <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
              <div className="text-sm font-medium flex items-center gap-2">
                <MessageSquare className="w-4 h-4 text-beacon" /> Message templates
              </div>
              <span className="text-[11px] text-muted">
                Customize the wording per event. Leave blank to keep the default.
              </span>
            </div>
            <div className="p-4 space-y-4">
              <div className="flex items-center gap-3">
                <span className="text-xs uppercase tracking-wider text-muted">Event</span>
                <div className="max-w-xs w-full">
                  <Select value={curEvent}
                    onChange={e => { setSelEvent(e.target.value); setEvTestRes(null); }}>
                    {cat.event_groups.map(g => (
                      <optgroup key={g.group} label={g.group}>
                        {g.events.map(ev => (
                          <option key={ev.id} value={ev.id}>
                            {ev.label}{(cfg.templates || {})[ev.id] ? " •" : ""}
                            {cat.emitted && cat.emitted[ev.id] === false ? " (not emitted)" : ""}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </Select>
                </div>
              </div>

              {!emitted && (
                <div className="text-xs text-warn bg-warn/10 rounded-lg px-3 py-2">
                  This event isn't fired by any service yet — you can pre-write a template,
                  but nothing will send until it's wired up. It carries no fields.
                </div>
              )}

              {/* field chips — click to insert at the caret, or drag into a box.
                  Only the fields THIS event actually carries are shown. */}
              <div>
                <div className="text-[11px] text-muted mb-1.5">
                  Fields available for <b>{events.find(e => e.id === curEvent)?.label}</b> — click
                  to insert at the cursor of the last-focused box, or drag one into the subject or body.
                </div>
                {fields.length ? (
                  <div className="flex flex-wrap gap-1.5">
                    {fields.map(f => (
                      <button
                        key={f.token} type="button" draggable
                        onDragStart={e => e.dataTransfer.setData("text/plain", `{${f.token}}`)}
                        onClick={() => insertToken(curEvent, focusRef.current, f.token)}
                        title={`{${f.token}} — e.g. ${f.example}`}
                        className="text-[11px] border border-edge rounded-full px-2 py-0.5 text-muted hover:text-ink hover:border-beacon cursor-grab active:cursor-grabbing">
                        {f.label}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="text-[11px] text-muted italic">This event carries no fields.</div>
                )}
              </div>

              <Field label="Subject">{box("subject", subjectRef, 1, "Default headline (emoji + direction + symbol + P&L)")}</Field>
              <Field label="Body">{box("body", bodyRef, 4, "Default aligned detail block")}</Field>

              {/* live preview against the sample object */}
              <div>
                <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5">Preview (sample data)</div>
                <div className="bg-panel2 border border-edge rounded-lg p-3 text-sm space-y-1">
                  <div className="font-medium">
                    {curTmpl.subject ? renderPreview(curTmpl.subject, sample)
                      : <span className="text-muted italic">default headline</span>}
                  </div>
                  <pre className="whitespace-pre-wrap font-mono text-xs text-muted">
                    {curTmpl.body ? renderPreview(curTmpl.body, sample) : "default detail block"}
                  </pre>
                </div>
                <div className="text-[11px] text-muted mt-1.5">
                  A field with no value in a real event renders empty — never an error.
                </div>
              </div>

              {/* send a real test notification for this event (sample data) */}
              <div className="border-t border-edge pt-4 flex flex-wrap items-center gap-3">
                <span className="text-xs uppercase tracking-wider text-muted">Send test</span>
                <div className="w-44">
                  <Select value={evTestCh} onChange={e => setEvTestCh(e.target.value)}>
                    <option value="">Routed channels</option>
                    {testable.map(ch => <option key={ch.id} value={ch.id}>{ch.label}</option>)}
                  </Select>
                </div>
                <Button variant="ghost" disabled={!emitted || evTesting}
                  onClick={() => sendEventTest(curEvent)}>
                  {evTesting ? "Sending…" : "Save & send test"}
                </Button>
                {evTestRes?.error && <span className="text-xs text-short truncate">✗ {evTestRes.error}</span>}
                {evTestRes?.results && Object.keys(evTestRes.results).length === 0 &&
                  <span className="text-xs text-muted">No channels routed for this event — pick one on the left.</span>}
                {evTestRes?.results && Object.entries(evTestRes.results).map(([ch, st]) => (
                  <span key={ch} className={`text-xs ${st === "ok" ? "text-long" : "text-short"}`}>
                    {st === "ok" ? "✓" : "✗"} {ch}{st === "ok" ? "" : `: ${st}`}
                  </span>
                ))}
                <span className="text-[11px] text-muted w-full">
                  Uses sample data so wording + delivery are verified instantly. To test a real
                  historical trade, call <code>POST /notifications/test-event</code> with a <code>trade_id</code>.
                </span>
              </div>
            </div>
          </Card>
        );
      })()}

      <div className="flex items-center justify-end gap-3">
        {saved && <span className="text-xs text-long">Saved.</span>}
        <Button onClick={save}>Save notifications</Button>
      </div>

      <Deliveries />

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

// A dispatch status is "ok", "disabled", "no_sender", or "error: <reason>".
const _statusDot = (s) =>
  s === "ok" ? "bg-long" : String(s).startsWith("error") ? "bg-short" : "bg-muted";
const _statusText = (s) =>
  s === "ok" ? "text-long" : String(s).startsWith("error") ? "text-short" : "text-muted";
const _clock = (ts) => {
  const d = new Date(ts);
  return isNaN(d) ? "—" : d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

/**
 * Recent deliveries — the per-channel outcome dispatch already computes and used
 * to only log. Answers "did my last SL-hit alert actually reach Telegram?"
 * without tailing a box, and gives the #76 class of silent drop a permanent
 * visible tripwire. Read-only telemetry over a bounded tail (last 500 rows).
 */
function Deliveries() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.notificationDeliveries(50)
      .then(d => { if (alive) { setRows(d.deliveries || []); setErr(null); } })
      .catch(e => alive && setErr(e.message));
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
        <div>
          <div className="text-sm font-medium">Recent deliveries</div>
          <div className="text-[11px] text-muted mt-0.5">
            What each routed channel did with the last 50 dispatches. Refreshes every 15s.
          </div>
        </div>
        <Button variant="ghost" onClick={() => api.notificationDeliveries(50)
          .then(d => setRows(d.deliveries || [])).catch(e => setErr(e.message))}>Refresh</Button>
      </div>
      {err && <div className="px-4 pt-3"><ErrorNote>{err}</ErrorNote></div>}
      {rows === null ? <Empty>Loading…</Empty>
        : rows.length === 0 ? <Empty>Nothing dispatched yet.</Empty>
        : (
          <Table minW={560}>
            <thead><tr><Th>Time</Th><Th>Event</Th><Th>Channels</Th></tr></thead>
            <tbody>
              {rows.map(r => {
                const chans = Object.entries(r.results || {});
                return (
                  <tr key={r.id}>
                    <Td mono>{_clock(r.ts)}</Td>
                    <Td>
                      <div>{r.label}</div>
                      {r.subject && <div className="text-[11px] text-muted truncate max-w-[22rem]">{r.subject}</div>}
                    </Td>
                    <Td>
                      {chans.length === 0
                        ? <span className="text-xs text-muted">not routed</span>
                        : (
                          <div className="flex flex-wrap gap-x-4 gap-y-1">
                            {chans.map(([ch, status]) => (
                              <span key={ch} className="inline-flex items-center gap-1.5 text-xs" title={String(status)}>
                                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${_statusDot(status)}`} />
                                <span className="text-muted">{ch}</span>
                                <span className={`${_statusText(status)} truncate max-w-[14rem]`}>{status}</span>
                              </span>
                            ))}
                          </div>
                        )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
    </Card>
  );
}
