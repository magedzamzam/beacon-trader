import { useEffect, useState } from "react";
import { Activity, BarChart3, Radio, Radar, ListChecks, Rss,
         Building2, Moon, Sun, KeyRound, ShieldCheck, Coins, CandlestickChart } from "lucide-react";
import { api, getToken, setToken } from "../lib/api";
import { toggleTheme } from "../lib/theme";

const NAV = [
  { id: "dashboard", label: "Dashboard", icon: Activity },
  { id: "positions", label: "Positions", icon: Radar },
  { id: "chart", label: "Chart", icon: CandlestickChart },
  { id: "signals", label: "Signals", icon: Radio },
  { id: "history", label: "History", icon: ListChecks },
  { id: "performance", label: "Performance", icon: BarChart3 },
  { id: "risk", label: "Risk", icon: ShieldCheck },
  { id: "sources", label: "Sources", icon: Rss },
  { id: "brokers", label: "Brokers", icon: Building2 },
  { id: "symbols", label: "Symbols", icon: Coins },
];

function HealthPulse() {
  const [ok, setOk] = useState(null);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try { const h = await api.health(); if (alive) setOk(h.ok); }
      catch { if (alive) setOk(false); }
    };
    poll(); const t = setInterval(poll, 8000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const color = ok === null ? "var(--muted)" : ok ? "var(--beacon)" : "var(--short)";
  return (
    <div className="flex items-center gap-2 text-xs text-muted">
      <span className="beacon-dot inline-block w-2.5 h-2.5 rounded-full"
            style={{ background: color }} />
      {ok === null ? "checking" : ok ? "all systems live" : "degraded"}
    </div>
  );
}

export default function Layout({ view, setView, children }) {
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"));
  const [tokenOpen, setTokenOpen] = useState(!getToken());
  const [tok, setTok] = useState(getToken());

  return (
    <div className="min-h-screen flex">
      <aside className="w-60 shrink-0 border-r border-edge bg-panel2 flex flex-col">
        <div className="px-5 py-5 flex items-center gap-2.5 border-b border-edge">
          <Radar className="w-5 h-5 text-beacon" />
          <div>
            <div className="font-semibold tracking-tight leading-none">Beacon</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mt-1">Trader</div>
          </div>
        </div>
        <nav className="p-2 flex-1">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button key={id} onClick={() => setView(id)}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm mb-0.5 transition
                ${view === id ? "bg-beacon/10 text-beacon" : "text-muted hover:text-ink hover:bg-panel"}`}>
              <Icon className="w-4 h-4" /> {label}
            </button>
          ))}
        </nav>
        <div className="p-4 border-t border-edge"><HealthPulse /></div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 border-b border-edge flex items-center justify-between px-6">
          <div className="text-sm font-medium capitalize">{view}</div>
          <div className="flex items-center gap-2">
            <button onClick={() => setTokenOpen(v => !v)}
              className="p-2 rounded-lg text-muted hover:text-ink hover:bg-panel" title="API token">
              <KeyRound className="w-4 h-4" />
            </button>
            <button onClick={() => setDark(toggleTheme())}
              className="p-2 rounded-lg text-muted hover:text-ink hover:bg-panel" title="Theme">
              {dark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>
          </div>
        </header>

        {tokenOpen && (
          <div className="px-6 py-3 border-b border-edge bg-panel2 flex items-center gap-3">
            <span className="text-xs text-muted">API token</span>
            <input value={tok} onChange={e => setTok(e.target.value)} type="password"
              placeholder="paste API_TOKEN"
              className="flex-1 bg-panel border border-edge rounded-lg px-3 py-1.5 text-sm num" />
            <button onClick={() => { setToken(tok); setTokenOpen(false); location.reload(); }}
              className="px-3 py-1.5 rounded-lg bg-beacon/15 text-beacon text-sm font-medium">Save</button>
          </div>
        )}

        <main className="p-6 flex-1 overflow-auto">{children}</main>
      </div>
    </div>
  );
}
