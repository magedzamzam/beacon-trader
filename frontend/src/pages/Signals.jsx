import { useEffect, useState } from "react";
import { Plus, RotateCcw, Sparkles, Layers } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote, Modal, Field, Input, NumberInput, Select } from "../components/form";
import { api } from "../lib/api";

const AI_TONE = { approve: "long", caution: "warn", reject: "short" };
const STRUCT_TONE = { bull: "long", bear: "short", range: "muted" };
const ALIGN_TONE = { aligned: "long", counter: "short", mixed: "warn" };
const TF_ORDER = ["1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m"];

export default function Signals() {
  const [data, setData] = useState(null);
  const [sources, setSources] = useState([]);
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState(null);
  const [add, setAdd] = useState(false);
  const [busy, setBusy] = useState(null);
  const [structId, setStructId] = useState(null);   // signal id whose structure panel is open
  const [reinitSig, setReinitSig] = useState(null); // signal pending re-initiate confirmation
  const [msg, setMsg] = useState(null);             // success feedback

  const load = async () => {
    try {
      const q = filter ? `?source_id=${filter}` : "";
      setData(await api.signals(q));
      setSources(await api.sources());
      setErr(null);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filter]);

  const doReinit = async (sig) => {
    setBusy(sig.id); setMsg(null);
    try {
      const r = await api.reinitiate(sig.id);
      setMsg(r?.message || `Re-initiated as signal #${r?.signal_id}.`);
      setReinitSig(null);
      await load();
    } catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  };
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
      {msg && (
        <div className="rounded-lg px-4 py-2 text-sm bg-long/15 text-long border border-long/30 flex items-center justify-between">
          <span>{msg}</span>
          <button onClick={() => setMsg(null)} className="text-xs opacity-70 hover:opacity-100">dismiss</button>
        </div>
      )}
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
          <Table minW={920}>
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
                      <Button variant="ghost" onClick={() => setStructId(s.id)} title="Structure & magnets at signal time">
                        <Layers className="w-4 h-4" /></Button>
                      <Button variant="ghost" onClick={() => runAi(s.id)} title="Run AI validation">
                        <Sparkles className={`w-4 h-4 ${busy === s.id ? "animate-pulse" : ""}`} /></Button>
                      <Button variant="ghost" onClick={() => setReinitSig(s)} title="Re-initiate — re-open as a fresh trade"><RotateCcw className="w-4 h-4" /></Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {add && <ManualModal sources={sources} onClose={() => setAdd(false)} onSaved={() => { setAdd(false); load(); }} />}
      {structId != null && <SignalStructureModal signalId={structId} onClose={() => setStructId(null)} />}
      {reinitSig && (
        <ReinitConfirmModal sig={reinitSig} busy={busy === reinitSig.id}
          onConfirm={() => doReinit(reinitSig)} onClose={() => setReinitSig(null)} />
      )}
    </div>
  );
}

// Confirm before re-initiating — this places FRESH live orders on the mapped
// accounts, so it must not fire accidentally (#66).
function ReinitConfirmModal({ sig, busy, onConfirm, onClose }) {
  return (
    <Modal title={`Re-initiate signal #${sig.id}?`} onClose={onClose}>
      <p className="text-sm text-muted">
        This re-opens the signal as a <b className="text-ink">fresh trade</b> and places
        <b className="text-ink"> live orders</b> on the mapped accounts. A new signal is
        created (the original is untouched); it still passes the trust, risk and AI gates.
      </p>
      <div className="mt-3 rounded-lg bg-panel2 border border-edge px-3 py-2 text-xs num">
        <Badge dot tone={sig.direction === "BUY" ? "long" : "short"}>{sig.direction}</Badge>
        {" "}<b>{sig.symbol}</b> · entry {sig.entry_from}{sig.entry_to !== sig.entry_from ? `–${sig.entry_to}` : ""} ·
        SL {sig.sl} · TP {(sig.tps || []).join(" / ")} · {sig.order_type}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
        <Button onClick={onConfirm} disabled={busy}>{busy ? "Re-initiating…" : "Re-open & place orders"}</Button>
      </div>
    </Modal>
  );
}

// Per-signal reference into the market-structure/magnet map (#61 Layer B): what
// state each timeframe was in and where price sat relative to the magnets when
// the signal fired. Shadow observability — nothing here gated the trade.
function SignalStructureModal({ signalId, onClose }) {
  const [row, setRow] = useState(undefined);   // undefined=loading, null=none
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.signalAnalytics(signalId)
      .then(r => setRow(r?.analytics?.structure_magnet || null))
      .catch(e => { setErr(e.message); setRow(null); });
  }, [signalId]);

  const perTf = row?.per_tf || {};
  const tfs = TF_ORDER.filter(t => perTf[t]);
  const nz = row?.nearest_zone;
  return (
    <Modal title={`Structure & magnets — signal #${signalId}`} onClose={onClose}>
      {err && <ErrorNote>{err}</ErrorNote>}
      {row === undefined ? <Empty>Loading…</Empty>
        : !row ? <Empty>No structure snapshot for this signal — it fired before a map existed, or capture is off.</Empty> : (
        <div className="space-y-4">
          <div className="flex items-center gap-3 text-xs">
            <span className="text-muted">HTF alignment</span>
            <Badge tone={ALIGN_TONE[row.htf_alignment] || "muted"}>{row.htf_alignment}</Badge>
            <span className="text-muted">· map v{row.map_version_id}</span>
          </div>

          <Table>
            <thead><tr className="border-b border-edge">
              <Th>TF</Th><Th>Structure</Th><Th right>Prem/Disc</Th><Th>Nearest Fib</Th><Th right>dist (ATR)</Th>
            </tr></thead>
            <tbody>
              {tfs.map(tf => {
                const s = perTf[tf];
                const f = s.nearest_fib;
                return (
                  <tr key={tf} className="border-b border-edge/60">
                    <Td mono>{tf.toUpperCase()}</Td>
                    <Td><Badge tone={STRUCT_TONE[s.label] || "muted"}>{s.label}</Badge></Td>
                    <Td right mono>{s.premium_discount == null ? "—" : `${Math.round(s.premium_discount * 100)}%`}</Td>
                    <Td mono>{f ? `${f.ratio ?? "—"} @ ${f.price}` : "—"}</Td>
                    <Td right mono>{f?.dist_atr == null ? "—" : f.dist_atr}</Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>

          <div className="text-xs">
            <div className="text-muted mb-1">Nearest magnet zone</div>
            {!nz ? <span className="text-muted">—</span> : (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 num">
                <span>band <b>{nz.band[0]}–{nz.band[1]}</b></span>
                <span>side <Badge tone={nz.inside ? "beacon" : "muted"}>{nz.side}</Badge></span>
                <span>dist <b>{nz.dist_atr == null ? "—" : `${nz.dist_atr} ATR`}</b></span>
                <span>score <b>{nz.score}</b></span>
              </div>
            )}
          </div>
          <div className="text-[11px] text-muted">
            Shadow observability — this snapshot did not gate the trade (measure-before-gate).
          </div>
        </div>
      )}
    </Modal>
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
