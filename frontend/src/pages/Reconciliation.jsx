import { Fragment, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Table, Card, KPI, Th, Td, Badge, Empty } from "../components/ui";
import { Button, Toggle } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

const CAT = {
  match: ["Match", "long"],
  no_fill: ["No fill", "short"],
  shortfall_stopped_before_tp: ["Stopped before TP", "warn"],
  shortfall_leg_missing: ["Leg missing", "warn"],
  executed_no_trade: ["No trade placed", "short"],
  not_executed: ["Not executed", "muted"],
  claim_sl: ["Channel SL", "muted"],
};
const catLabel = (c) => (CAT[c]?.[0] || c);
const catTone = (c) => (CAT[c]?.[1] || "muted");
const when = (s) => (s || "").slice(0, 16).replace("T", " ");

export default function Reconciliation({ setView }) {
  const help = () => setView && setView("help");   // ⓘ -> Glossary
  const [includeHistory, setIncludeHistory] = useState(false);
  const [category, setCategory] = useState("");     // "" = all
  const [expanded, setExpanded] = useState(null);
  const [busy, setBusy] = useState(false);
  const range = useRange("all");                    // anchored on Signal.created_at
  const { fromIso, toIso } = range;

  const { data: sum } = useData(() => api.reconciliationSummary(includeHistory, range.range),
    [includeHistory, busy, fromIso, toIso]);
  const { data: rows } = useData(() => api.reconciliationRows({ includeHistory, category, from: fromIso, to: toIso }),
    [includeHistory, category, busy, fromIso, toIso]);

  const refresh = async () => { setBusy(true); try { await api.reconciliationRefresh(); } finally { setBusy(v => !v); } };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <div className="text-sm text-muted">
          Channel <b className="text-ink">claimed</b> vs bot <b className="text-ink">actual</b> — per signal, with a reason for every miss.
        </div>
        <div className="flex-1" />
        <label className="flex items-center gap-2 text-xs text-muted">Include backfill history
          <Toggle checked={includeHistory} onChange={setIncludeHistory} /></label>
        <Button variant="ghost" onClick={refresh} disabled={busy}>
          <RefreshCw className="w-4 h-4 inline -mt-0.5" /> Re-link claims</Button>
      </div>

      <RangeFilter state={range} />

      {/* summary */}
      {sum && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KPI label={<>Match rate<HelpHint term="match_rate" onOpen={help} /></>} value={sum.match_rate != null ? `${sum.match_rate}%` : "—"}
              tone="beacon" sub={`${sum.matched}/${sum.total} signals`} />
            <KPI label={<>No fill<HelpHint term="no_fill" onOpen={help} /></>} value={sum.categories.no_fill || 0} tone="short" sub="placed, never filled" />
            <KPI label={<>Stopped early<HelpHint term="shortfall_stopped_before_tp" onOpen={help} /></>} value={sum.categories.shortfall_stopped_before_tp || 0}
              tone="warn" sub="filled, closed before TP" />
            <KPI label={<>No trade<HelpHint term="executed_no_trade" onOpen={help} /></>}
              value={(sum.categories.executed_no_trade || 0) + (sum.categories.not_executed || 0)}
              tone="muted" sub="signal placed nothing" />
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <button onClick={() => setCategory("")}
              className={`px-2.5 py-1 rounded-lg text-xs ${category === "" ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
              All ({sum.total})
            </button>
            {Object.entries(sum.categories).sort((a, b) => b[1] - a[1]).map(([c, n]) => (
              <button key={c} onClick={() => setCategory(c)}
                className={`px-2.5 py-1 rounded-lg text-xs ${category === c ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
                {catLabel(c)} ({n})
              </button>
            ))}
          </div>

          {!!sum.by_source.length && (
            <Card>
              <div className="px-4 py-3 border-b border-edge text-sm font-medium">Match rate by channel</div>
              <Table>
                <thead><tr className="border-b border-edge"><Th>Channel</Th><Th right>Match</Th><Th right>Total</Th><Th right>Rate</Th></tr></thead>
                <tbody>
                  {sum.by_source.map(s => (
                    <tr key={s.source_id} className="border-b border-edge/60">
                      <Td>{s.name || "—"}</Td>
                      <Td right mono>{s.match}</Td><Td right mono>{s.total}</Td>
                      <Td right mono><span className={s.rate >= 60 ? "text-long" : s.rate >= 30 ? "text-warn" : "text-short"}>
                        {s.rate != null ? `${s.rate}%` : "—"}</span></Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          )}
        </>
      )}

      {/* signal rows */}
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">
          Signals{category ? ` — ${catLabel(category)}` : ""}
        </div>
        {!rows ? <Empty>Loading…</Empty> : !rows.length ? <Empty>No claims linked yet. Try “Re-link claims”.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px]">
              <thead><tr className="border-b border-edge">
                <Th>Time</Th><Th>Channel</Th><Th>Dir</Th><Th right>Claimed</Th><Th right>Bot</Th>
                <Th>Category</Th><Th>Why</Th>
              </tr></thead>
              <tbody>
                {rows.map(r => (
                  <Fragment key={r.signal_id}>
                    <tr className="border-b border-edge/60 row-hover cursor-pointer"
                      onClick={() => setExpanded(expanded === r.signal_id ? null : r.signal_id)}>
                      <Td mono>{when(r.created_at)}</Td>
                      <Td>{r.source_name || "—"}{r.is_history && <span className="text-[10px] text-muted ml-1">hist</span>}</Td>
                      <Td><Badge dot tone={r.direction === "BUY" ? "long" : "short"}>{r.direction}</Badge></Td>
                      <Td right mono>{r.claimed_max_tp ? `TP${r.claimed_max_tp}` : (r.claimed_sl ? "SL" : "—")}</Td>
                      <Td right mono>{r.bot_max_tp ? `TP${r.bot_max_tp}` : (r.bot_any_fill ? "filled" : "—")}</Td>
                      <Td><Badge tone={catTone(r.category)}>{catLabel(r.category)}</Badge></Td>
                      <Td><span className="text-xs text-muted">{r.detail}</span></Td>
                    </tr>
                    {expanded === r.signal_id && (
                      <tr className="border-b border-edge/60 bg-panel2/40">
                        <td colSpan={7} className="px-4 py-3 space-y-3">
                          {r.signal_text && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-muted mb-1">Signal #{r.signal_id}</div>
                              <div className="text-xs whitespace-pre-wrap break-words text-ink/90">{r.signal_text}</div>
                            </div>
                          )}
                          {!!r.claims?.length && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-muted mb-1">Channel follow-ups</div>
                              {r.claims.map((c, i) => (
                                <div key={i} className="text-xs text-ink/90 flex gap-2">
                                  <span className="num text-muted shrink-0">{when(c.at)}</span>
                                  <span className="break-words">{c.text}</span>
                                </div>
                              ))}
                            </div>
                          )}
                          {!!r.legs?.length && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-muted mb-1">Bot legs</div>
                              <div className="flex flex-wrap gap-1.5">
                                {r.legs.map((l, i) => (
                                  <span key={i} className="text-[11px] border border-edge rounded px-1.5 py-0.5 bg-panel">
                                    TP{l.tp_index} <span className="text-muted">{l.status}</span>
                                    {l.outcome && <span className={`ml-1 text-${l.outcome === "tp_hit" ? "long" : l.outcome === "sl_hit" ? "short" : "muted"}`}>{l.outcome}</span>}
                                    {l.realized_pl != null && <span className={`ml-1 text-${tone(l.realized_pl)}`}>{money(l.realized_pl)}</span>}
                                  </span>
                                ))}
                              </div>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
