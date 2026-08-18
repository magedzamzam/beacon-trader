import { useEffect, useState } from "react";
import { Plus, Trash2, Pencil } from "lucide-react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Field, Input, Select, Toggle, Button, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const KIND_LABEL = {
  telegram: "Channel ID", tradingview: "Webhook key", api: "API key", manual: "Key (optional)",
  engine: "External id (unused)",
};

// The strategy that produced the evidence behind Lever 5 (replay run 43), offered
// as a starting point so nobody authors a generator from a blank box. Edit it
// freely -- the API validates on save and returns every problem at once.
const REFERENCE_GENERATOR = {
  timeframe: "15m",
  long: { when: { all: [
    { type: "indicator", id: "macd", timeframe: "15m", field: "cross", op: "eq", value: "up" },
    { type: "indicator", id: "rsi", timeframe: "15m", field: "value", op: "lt", value: 70 },
    { any: [
      { type: "indicator", id: "fvg", timeframe: "15m", field: "present", op: "is_true" },
      { type: "indicator", id: "order_block", timeframe: "15m", field: "present", op: "is_true" },
    ] },
  ] } },
  short: { when: { all: [
    { type: "indicator", id: "macd", timeframe: "15m", field: "cross", op: "eq", value: "down" },
    { type: "indicator", id: "rsi", timeframe: "15m", field: "value", op: "gt", value: 30 },
  ] } },
  entry: { type: "close" },
  sl: { type: "atr_mult", timeframe: "1h", period: 14, mult: 1.5 },
  tps: [{ type: "r_mult", r: 1.0 }, { type: "r_mult", r: 2.0 }, { type: "r_mult", r: 3.0 }],
  cooldown_bars: 60,
  max_signals_per_day: 8,
};

export default function Sources() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(null);   // source object or "new"
  const [showArchived, setShowArchived] = useState(false);

  const load = async () => {
    try { setSources(await api.sources(showArchived)); setAccounts(await api.accounts()); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, [showArchived]);

  const remove = async (s) => {
    if (!window.confirm(`Archive source “${s.name}”? It's removed from active lists and stops trading, `
      + `but its trade history and per-source attribution are kept. You can restore it later.`)) return;
    setErr(null);
    try { await api.deleteSource(s.id); await load(); }
    catch (e) { setErr(e.message); }
  };

  const restore = async (s) => {
    setErr(null);
    try { await api.updateSource(s.id, { archived: false }); await load(); }
    catch (e) { setErr(e.message); }
  };

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-3 flex-wrap">
          <div className="text-sm font-medium">Signal sources</div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-muted">Show archived
              <Toggle checked={showArchived} onChange={setShowArchived} /></label>
            <Button onClick={() => setEditing("new")}><Plus className="w-4 h-4 inline -mt-0.5" /> Add source</Button>
          </div>
        </div>
        {!sources.length ? <Empty>No sources. Add a Telegram channel or a webhook to start.</Empty> : (
          <Table>
            <thead><tr className="border-b border-edge">
              <Th>Name</Th><Th>Kind</Th><Th>Ref</Th>
              <Th right>Accounts</Th><Th>Trading</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {sources.map(s => (
                <tr key={s.id} className={`border-b border-edge/60 ${s.archived ? "opacity-50" : ""}`}>
                  <Td>{s.name}{s.archived && <span className="text-[10px] text-muted ml-1">archived</span>}</Td>
                  <Td><Badge>{s.kind}</Badge></Td>
                  <Td mono>{s.external_id || "—"}</Td>
                  <Td right mono>{(s.account_map || []).length}</Td>
                  <Td>{s.archived
                    ? <span className="text-xs text-muted">—</span>
                    : <Toggle checked={s.enabled_for_trading}
                        onChange={async v => { await api.updateSource(s.id, { enabled_for_trading: v }); load(); }} />}</Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      {s.archived ? (
                        <Button variant="ghost" onClick={() => restore(s)}>Restore</Button>
                      ) : (
                        <>
                          <Button variant="ghost" onClick={() => setEditing(s)}><Pencil className="w-4 h-4" /></Button>
                          <Button variant="danger" onClick={() => remove(s)}><Trash2 className="w-4 h-4" /></Button>
                        </>
                      )}
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {editing && <SourceModal source={editing === "new" ? null : editing} accounts={accounts}
        onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

function SourceModal({ source, accounts, onClose, onSaved }) {
  const s = source || {};
  const [kind, setKind] = useState(s.kind || "telegram");
  const [name, setName] = useState(s.name || "");
  const [externalId, setExternalId] = useState(s.external_id || "");
  const [trusted, setTrusted] = useState(s.is_trusted || false);
  const [enabled, setEnabled] = useState(s.enabled_for_trading || false);
  const [accountMap, setAccountMap] = useState(s.account_map || []);
  const [err, setErr] = useState(null);
  // An ENGINE source's strategy IS the source — unlike a channel, where entry /
  // filtration / exit live on the Strategies page. So this box is only shown,
  // and only sent, for kind='engine'.
  const [genText, setGenText] = useState(
    JSON.stringify((s.strategy || {}).generator || REFERENCE_GENERATOR, null, 2));
  const isEngine = kind === "engine";

  const toggleAcct = (id) => setAccountMap(m => m.includes(id) ? m.filter(x => x !== id) : [...m, id]);

  const save = async () => {
    // For a CHANNEL a source is identity + trust + routing: entry/filtration/exit
    // live on the Strategies page and risk on Risk & Limits (#84), so `strategy`
    // is omitted and the PATCH preserves whatever is already there.
    // For an ENGINE the generator config IS the strategy, so it is sent — and
    // only then, so a channel can never have its SL rules wiped by this screen.
    const payload = {
      kind, name, external_id: externalId || null,
      is_trusted: trusted, enabled_for_trading: enabled,
      account_map: accountMap,
    };
    if (isEngine) {
      let parsed;
      try { parsed = JSON.parse(genText); }
      catch (e) {
        // Caught here rather than sent: a JSON syntax error would come back as an
        // opaque 422 about the request body, not about the strategy.
        setErr(`Generator config is not valid JSON — ${e.message}`);
        return;
      }
      payload.strategy = { ...(s.strategy || {}), generator: parsed };
    }
    try {
      if (source) await api.updateSource(source.id, payload);
      else await api.createSource(payload);
      onSaved();
    } catch (e) { setErr(e.message); }
  };

  return (
    <Modal title={source ? `Edit ${source.name}` : "Add source"} onClose={onClose} wide>
      <ErrorNote>{err}</ErrorNote>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Kind">
          <Select value={kind} onChange={e => setKind(e.target.value)}>
            <option value="telegram">Telegram channel</option>
            <option value="tradingview">TradingView webhook</option>
            <option value="api">Generic API</option>
            <option value="manual">Manual desk</option>
            <option value="engine">Engine (own signals)</option>
          </Select>
        </Field>
        <Field label="Name"><Input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. GoldGA" /></Field>
      </div>
      {!isEngine && (
        <Field label={KIND_LABEL[kind]}
          hint={kind === "telegram" ? "The channel id, e.g. -1001220837618" : "Used as the webhook auth key in /ingest/tv/<key>"}>
          <Input mono value={externalId} onChange={e => setExternalId(e.target.value)} />
        </Field>
      )}

      {isEngine && (
        <Field label="Generator strategy (JSON)"
          hint="The conditions, the geometry and the caps. Saved straight to the engine — no deploy. Validated on save; every problem comes back at once.">
          <textarea
            value={genText}
            onChange={e => setGenText(e.target.value)}
            spellCheck={false}
            rows={18}
            className="w-full bg-panel2 border border-edge rounded-lg px-3 py-2 text-xs num text-ink focus:border-beacon outline-none"
          />
        </Field>
      )}
      {/* Outside the Field on purpose: Field wraps its children in a <label>, and
          a button nested in a label also focuses the labelled control on click. */}
      {isEngine && (
        <div className="flex items-center gap-3">
          <Button variant="ghost" onClick={() => setGenText(JSON.stringify(REFERENCE_GENERATOR, null, 2))}>
            Load reference strategy
          </Button>
          <span className="text-[11px] text-muted">
            MACD cross + RSI ceiling + FVG/OB, 1.5×ATR stop, 1R/2R/3R ladder.
          </span>
        </div>
      )}

      <div className="flex gap-6">
        <Field label="Trusted"><Toggle checked={trusted} onChange={setTrusted} /></Field>
        <Field label="Enabled for trading"><Toggle checked={enabled} onChange={setEnabled} /></Field>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-muted mb-1.5">Route to accounts</div>
        {accounts.length === 0 ? <div className="text-xs text-muted">No accounts yet — add one under Brokers first.</div> : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {accounts.map(a => (
              <label key={a.id} className="flex items-center gap-2 text-sm border border-edge rounded-lg px-3 py-2 bg-panel2 cursor-pointer">
                <input type="checkbox" checked={accountMap.includes(a.id)} onChange={() => toggleAcct(a.id)} />
                {a.name} <span className="text-muted num text-xs">{a.broker_account_id}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      <div className="text-[11px] text-muted border-t border-edge pt-3">
        {isEngine ? (
          <>
            An engine writes <b>shadow</b> signals only — they are scored forward but
            <b> never traded</b>, whatever “Enabled for trading” says, until the Lever-5
            evidence is in. It cannot place an order: nothing in the producer can reach
            the execution queue. Expect roughly one signal a day.
          </>
        ) : (
          <>
            Entry, filtration and exit (SL) rules now live on the <b>Strategies</b> page (per account × source);
            risk lives on <b>Risk &amp; Limits</b>. A source here is just identity, trust, and routing.
          </>
        )}
      </div>

      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>{source ? "Save" : "Add source"}</Button>
      </div>
    </Modal>
  );
}
