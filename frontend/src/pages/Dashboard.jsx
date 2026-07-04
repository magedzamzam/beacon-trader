import { TrendingUp, Target, Layers, Activity, Gauge, CheckCheck,
         ArrowRight, Wallet, Scale, Percent, Coins } from "lucide-react";
import { Card, KPI, Table, Th, Td, Badge, Empty } from "../components/ui";
import LineChart from "../components/LineChart";
import SessionStrip from "../components/SessionStrip";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

/** Plain absolute number (no leading "+", used for balances). */
const fmt = (n) => (n == null ? "—" :
  Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

/** Small "View more →" link that jumps to a detailed page via the app nav. */
function ViewMore({ onClick }) {
  if (!onClick) return null;
  return (
    <button onClick={onClick}
      className="text-xs text-beacon hover:underline inline-flex items-center gap-1">
      View more <ArrowRight className="w-3 h-3" />
    </button>
  );
}

export default function Dashboard({ setView, account }) {
  const acct = account || "";
  const { data: kpi, error } = useData(() => api.dashboard(acct), [acct]);
  const { data: perf } = useData(() => api.perfSummary(acct), [acct]);
  const { data: bySrc } = useData(() => api.perfBySource(acct), [acct]);
  const { data: curve } = useData(() => api.equityCurve(acct), [acct]);
  const { data: trades } = useData(api.trades);
  const { data: acctPerf } = useData(() => (acct ? api.accountPerformance(acct) : Promise.resolve(null)), [acct]);
  const go = (v) => setView && setView(v);

  if (error) return <Card><Empty>Can't reach the API. Check your token and that the API is up.</Empty></Card>;
  if (!kpi) return <Card><Empty>Loading…</Empty></Card>;

  const recent = (trades || [])
    .filter(t => !acct || String(t.account_id) === String(acct))
    .slice(0, 8);
  const topSrc = (bySrc || []).slice(0, 5);
  const last = curve && curve.length ? curve[curve.length - 1].pl : null;

  return (
    <div className="space-y-6">
      <SessionStrip />

      {/* Account overview — only when a specific account is filtered */}
      {acct && acctPerf && (
        <Card className="p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm font-medium flex items-center gap-2">
              <Coins className="w-4 h-4 text-beacon" /> Account overview — {acctPerf.name}
              <Badge>{acctPerf.currency}</Badge>
            </div>
            {acctPerf.balance == null &&
              <span className="text-[11px] text-warn">live balance unavailable — broker not connected</span>}
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KPI label="Balance" grad="a" icon={Wallet} value={fmt(acctPerf.balance)} sub={acctPerf.currency} />
            <KPI label="Equity" grad="d" icon={Scale} value={fmt(acctPerf.equity)} sub="incl. open P&L" />
            <KPI label="Available" grad="b" icon={Coins} value={fmt(acctPerf.available)} sub="free margin" />
            <KPI label="P&L %" grad={acctPerf.pl_pct >= 0 ? "b" : "c"} icon={Percent}
              value={acctPerf.pl_pct == null ? "—" : `${acctPerf.pl_pct >= 0 ? "+" : ""}${acctPerf.pl_pct}%`}
              sub="realized vs start" />
          </div>
        </Card>
      )}

      {/* Performance metrics — scoped to the selected account (or the whole book) */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <KPI label="Realized P&L" grad={kpi.total_pl >= 0 ? "b" : "c"} icon={TrendingUp}
          value={money(kpi.total_pl)} sub="closed legs" />
        <KPI label="Win rate" grad="a" icon={Target} value={`${kpi.win_rate}%`}
          sub={perf ? `${perf.wins}W / ${perf.losses}L` : "TP hits / closed"} />
        <KPI label="Profit factor" grad="a" icon={Gauge}
          value={perf?.profit_factor ?? "—"} sub="gross win / loss" />
        <KPI label="Open positions" grad="d" icon={Layers}
          value={kpi.open_legs} sub={`${kpi.open_trades} trades`} />
        <KPI label="Total trades" grad="a" icon={Activity} value={kpi.total_trades} sub="all time" />
        <KPI label="Closed legs" grad="b" icon={CheckCheck}
          value={perf?.closed_legs ?? "—"} sub="settled" />
      </div>

      {/* Growth curve — cumulative realized P&L over time */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">
            Account growth {acct ? "" : "(all accounts)"}
            <span className="text-muted font-normal"> · cumulative realized P&L</span>
          </div>
          {last != null && (
            <span className={`num text-sm font-semibold text-${tone(last)}`}>{money(last)}</span>
          )}
        </div>
        <div className="p-2 pt-3">
          <LineChart data={curve || []} valueKey="pl" />
        </div>
      </Card>

      {/* Performance by source preview */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Performance by source</div>
          <ViewMore onClick={() => go("performance")} />
        </div>
        {!topSrc.length ? <Empty>No closed legs to evaluate yet.</Empty> : (
          <Table>
            <thead><tr>
              <Th>Source</Th><Th right>P&L</Th><Th right>TP1</Th><Th right>TP2</Th><Th right>SL hits</Th>
            </tr></thead>
            <tbody>
              {topSrc.map(s => (
                <tr key={s.source_id} className="row-hover">
                  <Td>{s.name}</Td>
                  <Td right mono><span className={`text-${tone(s.pl)}`}>{money(s.pl)}</span></Td>
                  <Td right mono>{s.tp_hits?.[1] || 0}</Td>
                  <Td right mono>{s.tp_hits?.[2] || 0}</Td>
                  <Td right mono><span className="text-short">{s.sl_hits}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {/* Recent trades */}
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Recent trades</div>
          <ViewMore onClick={() => go("history")} />
        </div>
        {recent.length === 0 ? <Empty>No trades yet. Send a signal to the manual desk to see the fanout.</Empty> : (
          <Table>
            <thead><tr>
              <Th>#</Th><Th>Symbol</Th><Th>Side</Th><Th>Status</Th>
              <Th right>Legs</Th><Th right>Risk</Th><Th right>P&L</Th>
            </tr></thead>
            <tbody>
              {recent.map(t => (
                <tr key={t.id} className="row-hover">
                  <Td mono>{t.id}</Td>
                  <Td>{t.symbol}</Td>
                  <Td><Badge dot tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge></Td>
                  <Td><Badge dot tone={t.status === "closed" ? "muted" : "beacon"}>{t.status}</Badge></Td>
                  <Td right mono>{t.legs.length}</Td>
                  <Td right mono>{t.planned_risk != null ? Number(t.planned_risk).toFixed(2) : "—"}</Td>
                  <Td right mono><span className={`text-${tone(t.realized_pl)}`}>{money(t.realized_pl)}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
