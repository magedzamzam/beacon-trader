import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { Card, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";

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

/** Configuration → System Health: live status of the services behind the bot. */
export default function SystemHealth() {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    let alive = true;
    const poll = () => api.health().then(h => alive && setHealth(h))
      .catch(() => alive && setHealth({ ok: false, services: {} }));
    poll();
    const t = setInterval(poll, 8000);
    return () => { alive = false; clearInterval(t); };
  }, []);

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
