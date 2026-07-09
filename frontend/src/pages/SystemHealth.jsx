import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { Card, KPI, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData } from "./_useData";

const LAT_PRESETS = [["all", "All time"], ["today", "Today"], ["7d", "7 days"], ["30d", "30 days"]];

function latRange(id) {
  const now = new Date();
  const addD = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const sod = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
  if (id === "today") return [sod(now), null];
  if (id === "7d") return [addD(now, -7), null];
  if (id === "30d") return [addD(now, -30), null];
  return [null, null];
}

function SvcRow({ name, s }) {
  const ok = s?.ok;
  return (
    <div className="flex items-center justify-between px-3 py-2 border border-edge rounded-lg bg-panel2">
      <span className="text-sm capitalize">{name}</span>
      <span className="flex items-center gap-2">
        {s?.age_sec != null && <span className="text-[11px] text-muted num">{s.age_sec}s ago</span>}
        <Badge tone={ok ? "long" : "short"}>{ok ? "up" : "down"}</Badge>
      </span>
    </div>
  );
}

/** Configuration → System Health: live service status (from heartbeats) and the
 *  signal→order execution latency (incl. the AI-validation portion). */
export default function SystemHealth() {
  const [health, setHealth] = useState(null);
  const [preset, setPreset] = useState("all");

  useEffect(() => {
    let alive = true;
    const poll = () => api.health().then(h => alive && setHealth(h))
      .catch(() => alive && setHealth({ ok: false, services: {} }));
    poll();
    const t = setInterval(poll, 8000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const [from, to] = latRange(preset);
  const fromIso = from ? from.toISOString() : "";
  const toIso = to ? to.toISOString() : "";
  const { data: lat } = useData(() => api.execLatency({ from: fromIso, to: toIso }), [fromIso, toIso]);

  return (
    <div className="space-y-5">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2">
            <Activity className="w-4 h-4 text-beacon" /> Services
          </div>
          {health && <Badge tone={health.ok ? "long" : "short"}>
            {health.ok ? "all systems live" : "degraded"}</Badge>}
        </div>
        <div className="p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
          {!health ? <Empty>Checking…</Empty>
            : Object.entries(health.services || {}).map(([name, s]) => <SvcRow key={name} name={name} s={s} />)}
        </div>
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
          Worker liveness from heartbeats (no beat &lt; 30s = down). Polls every 8s.
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-2 flex-wrap">
          <div className="text-sm font-medium">Signal → order latency</div>
          <div className="flex gap-1.5">
            {LAT_PRESETS.map(([id, label]) => (
              <button key={id} onClick={() => setPreset(id)}
                className={`px-2 py-0.5 rounded text-[11px] ${preset === id ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {!lat ? <Empty>Loading…</Empty> : (
          <div className="p-4 grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KPI label="Median" tone="beacon" value={lat.total ? `${lat.total.median}s` : "—"}
              sub={lat.total ? `avg ${lat.total.avg}s` : "received → on broker"} />
            <KPI label="p90" value={lat.total ? `${lat.total.p90}s` : "—"}
              sub={`${lat.n_placed}/${lat.n_signals} signals placed`} />
            <KPI label="AI validation (median)" tone={lat.ai ? "warn" : "muted"}
              value={lat.ai ? `${lat.ai.median}s` : "off / 0s"}
              sub={lat.ai ? `avg ${lat.ai.avg}s · ${lat.ai.n} calls` : "no AI on these signals"} />
            <KPI label="Range" value={lat.total ? `${lat.total.min}–${lat.total.max}s` : "—"} sub="min–max" />
          </div>
        )}
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
          Total = first order placed − signal received. AI portion isolates the validation round-trip
          (0 when AI validation is off). Pick a range to compare before/after a config change.
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">Planned</div>
        <ul className="p-4 space-y-1.5 text-sm text-muted">
          {["Queue depth and processing latency", "Build / version info and changelog",
            "Restart and maintenance-mode controls", "Live log tail and error rate"].map(x => (
            <li key={x} className="flex items-start gap-2">
              <span className="mt-1.5 w-1 h-1 rounded-full bg-muted shrink-0" />{x}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
