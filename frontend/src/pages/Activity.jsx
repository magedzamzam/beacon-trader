import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Button, ErrorNote } from "../components/form";
import TradeDetail from "../components/TradeDetail";
import { api } from "../lib/api";

// The execution workflow, end to end: every decision the platform made and
// every broker interaction, newest first. Colour-code the notable kinds.
const TONE = {
  placed: "beacon", filled: "long", closed: "muted", sl_moved: "long",
  reject: "short", cancelled_at_broker: "warn", expired: "warn",
  ai_blocked: "short", fx_unavailable: "short",
  closed_by_user: "muted", cancelled_by_user: "warn", sl_moved_by_user: "long",
};

export default function Activity() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const [detail, setDetail] = useState(null);

  const load = async () => {
    try { setRows(await api.events("?limit=300")); setErr(null); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-3">
      <ErrorNote>{err}</ErrorNote>
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Execution activity</div>
          <Button variant="ghost" onClick={load}><RefreshCw className="w-4 h-4" /></Button>
        </div>
        {!rows ? <Empty>Loading…</Empty> : !rows.length ? <Empty>No activity yet.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>When</Th><Th>Event</Th><Th right>Trade</Th><Th right>Leg</Th><Th>Detail</Th>
            </tr></thead>
            <tbody>
              {rows.map(e => (
                <tr key={e.id} className="border-b border-edge/60 align-top">
                  <Td mono>{(e.ts || "").slice(0, 19).replace("T", " ")}</Td>
                  <Td><Badge tone={TONE[e.kind] || "muted"}>{e.kind}</Badge></Td>
                  <Td right mono>{e.trade_id
                    ? <button className="text-beacon hover:underline" onClick={() => setDetail(e.trade_id)}>#{e.trade_id}</button>
                    : "—"}</Td>
                  <Td right mono>{e.leg_id ? `#${e.leg_id}` : "—"}</Td>
                  <Td><code className="text-[11px] text-muted break-all">{JSON.stringify(e.payload)}</code></Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {detail && <TradeDetail tradeId={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}
