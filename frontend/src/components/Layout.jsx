import { useEffect, useState } from "react";
import { Activity, Radio, Radar, ListChecks, CandlestickChart,
         MessageSquare, GitBranch, Moon, Sun, KeyRound, LogOut,
         Menu, X, SlidersHorizontal, BarChart3, GitCompare,
         ChevronsLeft, ChevronsRight } from "lucide-react";
import { api, getToken, setToken, clearToken } from "../lib/api";
import { toggleTheme } from "../lib/theme";

// Overview + Live monitoring, then a single Settings entry — every
// broker/account/risk/source/symbol/AI setting lives inside Configuration's tabs.
const NAV = [
  { title: "Overview", items: [
    { id: "dashboard", label: "Dashboard", icon: Activity },
  ]},
  { title: "Live", items: [
    { id: "positions", label: "Positions", icon: Radar },
    { id: "signals", label: "Signals", icon: Radio },
    { id: "chart", label: "Chart", icon: CandlestickChart },
    { id: "messages", label: "Messages", icon: MessageSquare },
    { id: "activity", label: "Activity", icon: GitBranch },
    { id: "history", label: "History", icon: ListChecks },
    { id: "performance", label: "Performance", icon: BarChart3 },
    { id: "reconciliation", label: "Reconciler", icon: GitCompare },
  ]},
  { title: "Settings", items: [
    { id: "configuration", label: "Configuration", icon: SlidersHorizontal },
  ]},
];

// Human-readable header label for a view id.
function viewLabel(view) {
  const item = NAV.flatMap(g => g.items).find(i => i.id === view);
  return item ? item.label : view;
}

function HealthPulse({ collapsed }) {
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
  const text = ok === null ? "checking" : ok ? "all systems live" : "degraded";
  return (
    <div className={`flex items-center gap-2 text-xs text-muted ${collapsed ? "md:justify-center" : ""}`}
         title={collapsed ? text : undefined}>
      <span className="beacon-dot inline-block w-2.5 h-2.5 rounded-full shrink-0"
            style={{ background: color }} />
      <span className={collapsed ? "md:hidden" : ""}>{text}</span>
    </div>
  );
}

// Always-visible broker connectivity + latency (#45). The single most
// operationally critical signal for a live-money bot; a spike/outage should
// jump out. Tone: beacon = ok, warn = high latency, short = down.
function BrokerChip() {
  const [brokers, setBrokers] = useState(null);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try { const h = await api.health(); if (alive) setBrokers(h.brokers || {}); }
      catch { if (alive) setBrokers({}); }
    };
    poll(); const t = setInterval(poll, 8000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!brokers) return null;
  const entries = Object.entries(brokers);
  if (entries.length === 0) return null;
  const anyDown = entries.some(([, s]) => !s?.ok);
  const worstMs = entries.reduce((m, [, s]) => Math.max(m, s?.latency_ms ?? 0), 0);
  const color = anyDown ? "var(--short)" : worstMs >= 1500 ? "var(--warn)" : "var(--beacon)";
  const label = anyDown ? "broker down"
    : entries.length === 1 ? `${worstMs}ms`
    : `${entries.length} brokers · ${worstMs}ms`;
  const title = entries.map(([n, s]) =>
    `${n}: ${s?.ok ? (s.latency_ms + "ms") : (s?.message || "down")}`).join("\n");
  return (
    <div title={title}
      className="hidden sm:flex items-center gap-1.5 text-xs text-muted border border-edge rounded-lg px-2.5 py-1.5">
      <span className="inline-block w-2 h-2 rounded-full shrink-0" style={{ background: color }} />
      <span className="num">{label}</span>
    </div>
  );
}

export default function Layout({ view, setView, children, accounts = [], account = "", setAccount }) {
  const [dark, setDark] = useState(document.documentElement.classList.contains("dark"));
  const [tokenOpen, setTokenOpen] = useState(!getToken());
  const [tok, setTok] = useState(getToken());
  const [navOpen, setNavOpen] = useState(false);
  // Desktop-only: collapse the sidebar to an icon rail (labels on hover).
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("beacon_nav_collapsed") === "1");
  const toggleCollapsed = () => setCollapsed(c => {
    const n = !c; localStorage.setItem("beacon_nav_collapsed", n ? "1" : "0"); return n;
  });

  // Close the mobile drawer on Escape for keyboard/accessibility parity.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (id) => { setView(id); setNavOpen(false); };

  return (
    <div className="min-h-screen flex">
      {/* Backdrop — only present while the drawer is open on mobile */}
      {navOpen && (
        <div onClick={() => setNavOpen(false)}
          className="fixed inset-0 z-30 bg-black/50 md:hidden" aria-hidden="true" />
      )}

      <aside className={`fixed inset-y-0 left-0 z-40 w-60 shrink-0 border-r border-edge bg-panel2
          flex flex-col transition-all duration-200 ease-out
          md:sticky md:top-0 md:h-screen md:self-start md:z-auto md:translate-x-0
          ${collapsed ? "md:w-16" : "md:w-60"}
          ${navOpen ? "translate-x-0" : "-translate-x-full"}`}>
        <div className={`px-5 py-5 flex items-center gap-2.5 border-b border-edge ${collapsed ? "md:px-0 md:justify-center" : ""}`}>
          <Radar className="w-5 h-5 text-beacon shrink-0" />
          <div className={collapsed ? "md:hidden" : ""}>
            <div className="font-semibold tracking-tight leading-none">Beacon</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mt-1">Trader</div>
          </div>
          <button onClick={() => setNavOpen(false)}
            className="ml-auto p-1.5 rounded-lg text-muted hover:text-ink hover:bg-panel md:hidden"
            title="Close menu" aria-label="Close menu">
            <X className="w-4 h-4" />
          </button>
        </div>
        <nav className="p-2 flex-1 overflow-y-auto space-y-3">
          {NAV.map(group => (
            <div key={group.title}>
              <div className={`px-3 pt-1 pb-1.5 text-[10px] uppercase tracking-[0.16em] text-muted ${collapsed ? "md:hidden" : ""}`}>{group.title}</div>
              {group.items.map(({ id, label, icon: Icon }) => (
                <button key={id} onClick={() => go(id)}
                  title={collapsed ? label : undefined}
                  className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm mb-0.5 transition
                    ${collapsed ? "md:justify-center md:px-0" : ""}
                    ${view === id ? "bg-beacon/10 text-beacon" : "text-muted hover:text-ink hover:bg-panel"}`}>
                  <Icon className="w-4 h-4 shrink-0" />
                  <span className={collapsed ? "md:hidden" : ""}>{label}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="p-3 border-t border-edge space-y-2">
          <HealthPulse collapsed={collapsed} />
          <button onClick={toggleCollapsed}
            title={collapsed ? "Expand menu" : "Collapse menu"} aria-label="Toggle menu"
            className={`hidden md:flex items-center gap-2 w-full p-1.5 rounded-lg text-muted hover:text-ink hover:bg-panel text-xs transition
              ${collapsed ? "justify-center" : ""}`}>
            {collapsed ? <ChevronsRight className="w-4 h-4" />
              : <><ChevronsLeft className="w-4 h-4" /> Collapse</>}
          </button>
        </div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 border-b border-edge flex items-center justify-between px-4 md:px-6">
          <div className="flex items-center gap-2 min-w-0">
            <button onClick={() => setNavOpen(true)}
              className="p-2 -ml-2 rounded-lg text-muted hover:text-ink hover:bg-panel md:hidden"
              title="Menu" aria-label="Open menu">
              <Menu className="w-5 h-5" />
            </button>
            <div className="text-sm font-medium capitalize truncate">{viewLabel(view)}</div>
          </div>
          <div className="flex items-center gap-2">
            <BrokerChip />
            {setAccount && (
              <select value={account} onChange={e => setAccount(e.target.value)}
                title="Filter the whole app by account"
                className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-xs text-ink
                           max-w-[40vw] sm:max-w-none outline-none focus:border-beacon">
                <option value="">All accounts</option>
                {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            )}
            <button onClick={() => setTokenOpen(v => !v)}
              className="p-2 rounded-lg text-muted hover:text-ink hover:bg-panel" title="API token">
              <KeyRound className="w-4 h-4" />
            </button>
            <button onClick={() => setDark(toggleTheme())}
              className="p-2 rounded-lg text-muted hover:text-ink hover:bg-panel" title="Theme">
              {dark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>
            <button onClick={() => { clearToken(); location.reload(); }}
              className="p-2 rounded-lg text-muted hover:text-short hover:bg-panel" title="Log out">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </header>

        {tokenOpen && (
          <div className="px-4 md:px-6 py-3 border-b border-edge bg-panel2 flex flex-wrap items-center gap-2 sm:gap-3">
            <span className="text-xs text-muted">API token</span>
            <input value={tok} onChange={e => setTok(e.target.value)} type="password"
              placeholder="paste API_TOKEN"
              className="flex-1 min-w-[10rem] bg-panel border border-edge rounded-lg px-3 py-1.5 text-sm num" />
            <button onClick={() => { setToken(tok); setTokenOpen(false); location.reload(); }}
              className="px-3 py-1.5 rounded-lg bg-beacon/15 text-beacon text-sm font-medium">Save</button>
          </div>
        )}

        <main className="p-4 md:p-6 flex-1 overflow-auto">{children}</main>
      </div>
    </div>
  );
}
