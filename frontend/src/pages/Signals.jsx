import { useEffect, useState } from "react";
import { Plus, RotateCcw, Sparkles } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote, Modal, Field, Input, NumberInput, Select } from "../components/form";
import { api } from "../lib/api";

const AI_TONE = { approve: "long", caution: "warn", reject: "short" };

export default function Signals() {
  const [data, setData] = useState(null);
  const [sources, setSources] = useState([]);
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState(null);
  const [add, setAdd] = useState(false);
  const [busy, setBusy] = useState(null);

  const load = async () => {
    try {
      const q = filter ? `?source_id=${filter}` : "";
      setData(await api.signals(q));
      setSources(await api.sources());
      setErr(null);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filter]);

  const reinit = async (id) => { try { await api.reinitiate(id); await load(); } catch (e) { setErr(e.message); } };
  const runAi = async (id) => {
    setBusy(id);
    try { await api.aiAssessSignal(id); await load(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  };

  if (!data) return <Card><Empty>Loading…</Empty></Card>;
  return (
    <div className="space-y-3">
      <ErrorNote>{err}</ErrorNote>
      <div className="flex justify-between items-center">
        <Select value={filter} onChange={e => setFilter(e.target.value)}>
          <option value="">All channels</option>
          {sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
        <Button onClick={() => setAdd(true)}><Plus className="w-4 h-4 inline -mt-0.5" /> Manual signal</Button>
      </div>
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Signal feed</div>
        {!data.length ? <Empty>No signals received yet.</Empty> : (
          <Table>
            <thead><tr>
              <Th>#</Th><Th>Provider</Th><Th>Symbol</Th><Th>Side</Th><Th right>Entry</Th><Th right>SL</Th>
              <Th>TPs</Th><Th>Type</Th><Th>Status</Th><Th>AI</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {data.map(s => (
                <tr key={s.id} className="row-hover">
                  <Td mono>{s.id}</Td>
                  <Td>{s.source_name}{s.source_kind && <span className="text-[10px] text-muted ml-1">{s.source_kind}</span>}</Td>
                  <Td>{s.symbol}</Td>
                  <Td><Badge dot tone={s.direction === "BUY" ? "long" : "short"}>{s.direction}</Badge></Td>
                  <Td right mono>{s.entry_from}{s.entry_to !== s.entry_from ? `–${s.entry_to}` : ""}</Td>
                  <Td right mono>{s.sl}</Td>
                  <Td mono>{(s.tps || []).join(" / ")}</Td>
                  <Td>{s.order_type}</Td>
                  <Td><Badge dot tone={s.status === "rejected" ? "short" : s.status === "executed" ? "long" : "beacon"}>{s.status}</Badge>
                    {s.reject_reason && <div className="text-[10px] text-muted mt-0.5">{s.reject_reason}</div>}</Td>
                  <Td>{s.ai_verdict
                    ? <Badge tone={AI_TONE[s.ai_verdict] || "muted"}>{s.ai_verdict}{s.ai_confidence != null ? ` ${Math.round(s.ai_confidence * 100)}%` : ""}</Badge>
                    : <span className="text-xs text-muted">—</span>}</Td>
                  <Td right>
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" onClick={() => runAi(s.id)} title="Run AI validation">
                        <Sparkles className={`w-4 h-4 ${busy === s.id ? "animate-pulse" : ""}`} /></Button>
                      <Button variant="ghost" onClick={() => reinit(s.id)} title="Re-initiate"><RotateCcw className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {add && <ManualModal sources={sources} onClose={() => setAdd(false)} onSaved={() => { setAdd(false); load(); }} />}
    </div>
  );
}

function ManualModal({ sources, onClose, onSaved }) {
  const [sourceId, setSourceId] = useState(sources[0]?.id || "");
  const [direction, setDirection] = useState("BUY");
  const [entryFrom, setEntryFrom] = useState("");
  const [entryTo, setEntryTo] = useState("");
  const [sl, setSl] = useState("");
  const [tps, setTps] = useState("");
  const [orderType, setOrderType] = useState("MARKET");
  const [err, setErr] = useState(null);

  const save = async () => {
    try {
      const tpList = tps.split(/[,\s/]+/).filter(Boolean).map(Number);
      const res = await api.manualSignal({
        source_id: +sourceId, symbol: "XAUUSD", direction,
        entry_from: +entryFrom, entry_to: +(entryTo || entryFrom), sl: +sl,
        tps: tpList, order_type: orderType,
      });
      if (!res.accepted) { setErr(`Rejected: ${res.reason}`); return; }
      onSaved();
    } catch (e) { setErr(e.message); }
  };
  return (
    <Modal title="New manual signal" onClose={onClose}>
      <ErrorNote>{err}</ErrorNote>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Source">
          <Select value={sourceId} onChange={e => setSourceId(e.target.value)}>
            {sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </Field>
        <Field label="Direction">
          <Select value={direction} onChange={e => setDirection(e.target.value)}>
            <option value="BUY">BUY</option><option value="SELL">SELL</option>
          </Select>
        </Field>
        <Field label="Entry from"><NumberInput value={entryFrom} onChange={e => setEntryFrom(e.target.value)} /></Field>
        <Field label="Entry to (optional)"><NumberInput value={entryTo} onChange={e => setEntryTo(e.target.value)} /></Field>
        <Field label="Stop loss"><NumberInput value={sl} onChange={e => setSl(e.target.value)} /></Field>
        <Field label="Order type">
          <Select value={orderType} onChange={e => setOrderType(e.target.value)}>
            <option value="MARKET">MARKET</option><option value="LIMIT">LIMIT</option>
          </Select>
        </Field>
      </div>
      <Field label="Take-profits" hint="comma or space separated, e.g. 4110 4112 4114">
        <Input mono value={tps} onChange={e => setTps(e.target.value)} />
      </Field>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={save}>Send signal</Button>
      </div>
    </Modal>
  );
}
