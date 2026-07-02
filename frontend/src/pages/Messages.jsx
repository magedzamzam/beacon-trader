import { useEffect, useState } from "react";
import { RefreshCw, DownloadCloud } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Modal, Button, Select, Toggle, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const STATUS_TONE = { parsed: "long", rejected: "short", duplicate: "warn", none: "muted" };
const firstLine = (t) => (t || "").split("\n").find(l => l.trim()) || "—";
const when = (m) => (m.message_date || m.created_at || "").slice(0, 16).replace("T", " ");

export default function Messages() {
  const [channels, setChannels] = useState([]);
  const [rows, setRows] = useState(null);
  const [sourceId, setSourceId] = useState("");
  const [onlySignals, setOnlySignals] = useState(false);
  const [err, setErr] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [open, setOpen] = useState(null);   // message shown in the modal

  const load = async () => {
    try {
      setChannels(await api.channels());
      const params = new URLSearchParams();
      if (sourceId) params.set("source_id", sourceId);
      if (onlySignals) params.set("only_signals", "true");
      const q = params.toString() ? `?${params.toString()}` : "";
      setRows(await api.messages(q));
      setErr(null);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [sourceId, onlySignals]);

  const sync = async () => {
    setSyncing(true);
    try { await api.syncMessages(300); setTimeout(load, 2500); }
    catch (e) { setErr(e.message); }
    finally { setTimeout(() => setSyncing(false), 2500); }
  };

  return (
    <div className="space-y-4">
      <ErrorNote>{err}</ErrorNote>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {channels.map(c => (
          <div key={c.source_id ?? "none"} className="card p-4">
            <div className="text-sm font-medium">{c.name}</div>
            <div className="text-[11px] text-muted num mt-0.5">{c.external_id || "—"}</div>
            <div className="mt-3 flex items-center gap-4">
              <div><div className="num text-xl font-semibold">{c.messages}</div><div className="text-[10px] uppercase tracking-wider text-muted">messages</div></div>
              <div><div className="num text-xl font-semibold text-beacon">{c.signals}</div><div className="text-[10px] uppercase tracking-wider text-muted">signals</div></div>
            </div>
          </div>
        ))}
        {!channels.length && <div className="text-sm text-muted col-span-3 px-1">No messages captured yet. Add a Telegram source and click “Sync history”.</div>}
      </div>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-3 flex-wrap">
          <div className="text-sm font-medium">Message history</div>
          <div className="flex items-center gap-3">
            <Select value={sourceId} onChange={e => setSourceId(e.target.value)}>
              <option value="">All channels</option>
              {channels.map(c => <option key={c.source_id ?? "none"} value={c.source_id || ""}>{c.name}</option>)}
            </Select>
            <Toggle checked={onlySignals} onChange={setOnlySignals} label="signals only" />
            <Button variant="ghost" onClick={load} title="Refresh"><RefreshCw className="w-4 h-4" /></Button>
            <Button onClick={sync} title="Backfill recent history from Telegram">
              <DownloadCloud className="w-4 h-4 inline -mt-0.5" /> {syncing ? "Syncing…" : "Sync history"}
            </Button>
          </div>
        </div>
        {!rows ? <Empty>Loading…</Empty> : !rows.length ? <Empty>No messages.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>When</Th><Th>Channel</Th><Th>Message</Th><Th>Parse</Th><Th right>Signal</Th>
            </tr></thead>
            <tbody>
              {rows.map(m => (
                <tr key={m.id} onClick={() => setOpen(m)}
                    className="border-b border-edge/60 row-hover cursor-pointer">
                  <Td mono>{when(m)}</Td>
                  <Td>{m.source_name}</Td>
                  <Td><div className="max-w-md truncate text-sm">{firstLine(m.text)}</div></Td>
                  <Td><Badge tone={STATUS_TONE[m.parse_status] || "muted"}>{m.parse_status}</Badge></Td>
                  <Td right mono>{m.signal_id ? `#${m.signal_id}` : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {open && (
        <Modal title="Message" onClose={() => setOpen(null)} wide>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <Badge>{open.source_name}</Badge>
            {open.sender && <span className="num">{open.sender}</span>}
            <span className="num">{when(open)}</span>
            <Badge tone={STATUS_TONE[open.parse_status] || "muted"}>{open.parse_status}</Badge>
            {open.signal_id && <Badge tone="beacon">signal #{open.signal_id}</Badge>}
          </div>
          {open.reject_reason && (
            <div className="text-xs text-short bg-short/10 rounded-lg px-3 py-2">{open.reject_reason}</div>
          )}
          <div className="whitespace-pre-wrap text-sm bg-panel2 border border-edge rounded-lg p-3 max-h-[50vh] overflow-auto">
            {open.text || "—"}
          </div>
        </Modal>
      )}
    </div>
  );
}
