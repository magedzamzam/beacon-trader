import { useEffect, useState } from "react";
import { RefreshCw, XCircle, Ban } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const OPEN = new Set(["open", "working", "pending"]);

export default function Positions() {
  const [trades, setTrades] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null);

  const load = async () => {
    try { setTrades(await api.trades()); setErr(null); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 6000);   // auto-refresh from broker via monitor
    return () => clearInterval(t);
  }, []);

  const act = async (fn, id) => {
    setBusy(id);
    try { await fn(id); await load(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  };

  if (!trades) return <Card><Empty>Loading…</Empty></Card>;
  const rows = [];
  trades.forEach(t => t.legs.filter(l => OPEN.has(l.status)).forEach(l => rows.push({ t, l })));

  return (
    <div className="space-y-3">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Open legs</div>
          <Button variant="ghost" onClick={load}><RefreshCw className="w-4 h-4 inline -mt-0.5" /> Refresh</Button>
        </div>
        {!rows.length ? <Empty>No open positions.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>Trade</Th><Th>Symbol</Th><Th>Side</Th><Th>Type</Th><Th right>TP#</Th>
              <Th right>Entry</Th><Th right>SL</Th><Th right>TP</Th><Th right>Lot</Th>
              <Th>State</Th><Th right>Actions</Th>
            </tr></thead>
            <tbody>
              {rows.map(({ t, l }) => (
                <tr key={l.id} className="border-b border-edge/60">
                  <Td mono>{t.id}</Td><Td>{t.symbol}</Td>
                  <Td><Badge tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
                  <Td>{l.order_type}</Td><Td right mono>{l.tp_index}</Td>
                  <Td right mono>{Number(l.entry).toFixed(2)}</Td>
                  <Td right mono>{Number(l.sl).toFixed(2)}{l.sl_moved && <span className="text-beacon"> •</span>}</Td>
                  <Td right mono>{Number(l.tp).toFixed(2)}</Td>
                  <Td right mono>{Number(l.lot).toFixed(2)}</Td>
                  <Td><Badge tone="beacon">{l.status}</Badge></Td>
                  <Td right>
                    {l.status === "open" ? (
                      <Button variant="danger" disabled={busy === l.id}
                        onClick={() => act(api.closeLeg, l.id)}>
                        <XCircle className="w-4 h-4 inline -mt-0.5" /> Close
                      </Button>
                    ) : (
                      <Button variant="danger" disabled={busy === l.id}
                        onClick={() => act(api.cancelLeg, l.id)}>
                        <Ban className="w-4 h-4 inline -mt-0.5" /> Cancel
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="px-4 py-2 text-xs text-muted border-t border-edge">
          <span className="text-beacon">•</span> = stop-loss moved by a rule · list reconciles with the broker every few seconds
        </div>
      </Card>
    </div>
  );
}
