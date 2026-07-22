import { useEffect, useState } from "react";
import { RefreshCw, XCircle, Ban, Shield } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote, Modal, Field, NumberInput } from "../components/form";
import TradeDetail from "../components/TradeDetail";
import { api } from "../lib/api";

const OPEN = new Set(["open", "working", "pending"]);

export default function Positions({ account }) {
  const [trades, setTrades] = useState(null);
  const [err, setErr] = useState(null);
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [moveSl, setMoveSl] = useState(false);
  const [detail, setDetail] = useState(null);

  const load = async () => {
    try { setTrades(await api.trades(account)); setErr(null); } catch (e) { setErr(e.message); }
  };
  // Re-fetch (and restart polling) whenever the global account filter changes.
  useEffect(() => { load(); const t = setInterval(load, 6000); return () => clearInterval(t); }, [account]);

  const rows = [];
  (trades || []).forEach(t => t.legs.filter(l => OPEN.has(l.status)).forEach(l => rows.push({ t, l })));
  const openIds = rows.filter(r => r.l.status === "open").map(r => r.l.id);

  const toggle = id => setSel(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const allSelected = rows.length && rows.every(r => sel.has(r.l.id));
  const toggleAll = () => setSel(allSelected ? new Set() : new Set(rows.map(r => r.l.id)));

  const run = async (payload, confirmMsg) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    try { await api.bulkLegs(payload); setSel(new Set()); await load(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const single = async (fn, id) => { setBusy(true); try { await fn(id); await load(); } catch (e) { setErr(e.message); } finally { setBusy(false); } };

  if (!trades) return <Card><Empty>Loading…</Empty></Card>;
  const selIds = [...sel];

  return (
    <div className="space-y-3">
      <ErrorNote>{err}</ErrorNote>

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={load}><RefreshCw className="w-4 h-4 inline -mt-0.5" /> Refresh</Button>
        <div className="flex-1" />
        <span className="text-xs text-muted">{sel.size} selected</span>
        <Button disabled={busy || !sel.size} onClick={() => setMoveSl(true)}>
          <Shield className="w-4 h-4 inline -mt-0.5" /> Move SL</Button>
        <Button variant="danger" disabled={busy || !sel.size}
          onClick={() => run({ action: "cancel", leg_ids: selIds }, "Cancel selected orders?")}>
          <Ban className="w-4 h-4 inline -mt-0.5" /> Cancel sel.</Button>
        <Button variant="danger" disabled={busy || !sel.size}
          onClick={() => run({ action: "close", leg_ids: selIds }, "Close selected positions?")}>
          <XCircle className="w-4 h-4 inline -mt-0.5" /> Close sel.</Button>
        <Button variant="danger" disabled={busy || !openIds.length}
          onClick={() => run({ action: "close" }, "Close ALL open positions across every trade?")}>
          Close All</Button>
      </div>

      <Card>
        {!rows.length ? <Empty>No open positions.</Empty> : (
          <Table minW={860}>
            <thead><tr>
              <Th><input type="checkbox" checked={!!allSelected} onChange={toggleAll} /></Th>
              <Th>Trade</Th><Th>Symbol</Th><Th>Channel</Th><Th>Side</Th><Th>Type</Th><Th right>TP#</Th>
              <Th right>Entry</Th><Th right>SL</Th><Th right>TP</Th><Th right>Lot</Th>
              <Th>State</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {rows.map(({ t, l }) => (
                <tr key={l.id} className="row-hover">
                  <Td><input type="checkbox" checked={sel.has(l.id)} onChange={() => toggle(l.id)} /></Td>
                  <Td mono><button className="text-beacon hover:underline" onClick={() => setDetail(t.id)}>{t.id}</button></Td><Td>{t.symbol}</Td>
                  <Td>
                    <span className="truncate">{t.source_name || "—"}</span>
                    {t.source_kind && <span className="text-[10px] text-muted ml-1">{t.source_kind}</span>}
                  </Td>
                  <Td><Badge dot tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
                  <Td>{l.order_type}</Td><Td right mono>{l.tp_index}</Td>
                  <Td right mono>{Number(l.entry).toFixed(2)}</Td>
                  <Td right mono>{Number(l.sl).toFixed(2)}{l.sl_moved && <span className="text-beacon"> •</span>}</Td>
                  <Td right mono>{Number(l.tp).toFixed(2)}</Td>
                  <Td right mono>{Number(l.lot).toFixed(2)}</Td>
                  <Td><Badge dot tone="beacon">{l.status}</Badge></Td>
                  <Td right>
                    {l.status === "open"
                      ? <Button variant="danger" disabled={busy} onClick={() => single(api.closeLeg, l.id)}><XCircle className="w-4 h-4" /></Button>
                      : <Button variant="danger" disabled={busy} onClick={() => single(api.cancelLeg, l.id)}><Ban className="w-4 h-4" /></Button>}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <div className="px-4 py-2 text-xs text-muted border-t border-edge">
          <span className="text-beacon">•</span> = SL moved by a rule · reconciles with the broker every few seconds
        </div>
      </Card>

      {moveSl && <MoveSlModal count={sel.size} onClose={() => setMoveSl(false)}
        onSubmit={async (sl) => { setMoveSl(false); await run({ action: "move_sl", leg_ids: selIds, sl }); }} />}
      {detail && <TradeDetail tradeId={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

function MoveSlModal({ count, onClose, onSubmit }) {
  const [sl, setSl] = useState("");
  return (
    <Modal title={`Move SL on ${count} position(s)`} onClose={onClose}>
      <Field label="New stop-loss price"><NumberInput value={sl} onChange={e => setSl(e.target.value)} /></Field>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button onClick={() => onSubmit(+sl)}>Move SL</Button>
      </div>
    </Modal>
  );
}
