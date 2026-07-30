import { Fragment, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Table, Card, KPI, Th, Td, Badge, Empty } from "../components/ui";
import { Button, Toggle, Select } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import TradeDetail from "../components/TradeDetail";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

const CAT = {
  match: ["Match", "long"],
  no_fill: ["No fill", "short"],
  shortfall_stopped_before_tp: ["Stopped before TP", "warn"],
  shortfall_leg_missing: ["Leg missing", "warn"],
  executed_no_trade: ["No-trade bug", "short"],
  not_executed: ["Protected / not-traded", "muted"],
  claim_sl: ["Channel SL", "muted"],
  no_claim: ["Channel silent", "violet"],
};
const catLabel = (c) => (CAT[c]?.[0] || c);
const catTone = (c) => (CAT[c]?.[1] || "muted");
const when = (s) => (s || "").slice(0, 16).replace("T", " ");

// Operator outcome-override options (#136 pt3) — force-tag a misparsed follow-up.
const OVERRIDE_OPTS = [
  ["", "— parsed —"], ["sl_hit", "SL Hit"], ["tp1", "TP1"], ["tp2", "TP2"],
  ["tp3", "TP3"], ["tp4", "TP4"], ["tp5", "TP5"], ["all_tp", "All TP"],
  ["breakeven", "Breakeven"],
];

// How the bot's trade ENDED (#174). This column used to print "filled" for
// anything that reached no TP, so 78 stop-outs and a pile of breakevens read as
// an ambiguous mid-flight state — on trades that had been closed for days.
// `bot_exit` comes from the leg outcomes, which were always there.
const EXIT = {
  sl: ["SL", "text-short"],
  breakeven: ["BE", "text-muted"],
  open: ["open", "text-warn"],
  closed: ["closed", "text-muted"],
};

function BotResult({ row }) {
  if (row.bot_max_tp) return <span className="text-long">TP{row.bot_max_tp}</span>;
  const [label, cls] = EXIT[row.bot_exit] || [];
  if (label) return <span className={cls}>{label}</span>;
  // bot_exit === "none", or an older payload without the field
  return <span className="text-muted">{row.bot_any_fill ? "filled" : "—"}</span>;
}

// Claim-linker staleness (#173). Silence here is indistinguishable from "the
// channels said nothing", which is exactly how three days of dead linking went
// unnoticed — so it gets its own banner rather than a number in a corner.
const STALE_HOURS = 12;

function LinkerHealth({ linker }) {
  if (!linker || linker.hwm == null) return null;
  const unscanned = linker.unscanned || 0;
  const last = linker.last_claim_at ? new Date(linker.last_claim_at) : null;
  const hoursAgo = last ? (Date.now() - last.getTime()) / 3.6e6 : null;
  const stale = hoursAgo != null && hoursAgo > STALE_HOURS;
  if (!unscanned && !stale) return null;
  return (
    <div className="rounded-lg border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">
      <b>Claim linking is behind.</b>{" "}
      {unscanned > 0 && <>
        <span className="num">{unscanned}</span> message{unscanned === 1 ? "" : "s"} unscanned
        (high-water mark <span className="num">{linker.hwm}</span> of{" "}
        <span className="num">{linker.max_message_id}</span>).{" "}
      </>}
      {stale && <>Last claim linked <span className="num">{Math.floor(hoursAgo)}h</span> ago.{" "}</>}
      Every number below is stale until this clears — signals will read as
      "Channel silent" that the channel actually reported on. Run{" "}
      <span className="num">POST /reconciliation/refresh?full=true</span> and check the api logs
      for <span className="num">link_claims: message … failed</span>.
    </div>
  );
}

export default function Reconciliation() {
  const [includeHistory, setIncludeHistory] = useState(false);
  const [category, setCategory] = useState("");     // "" = all
  const [expanded, setExpanded] = useState(null);
  const [busy, setBusy] = useState(false);
  const [tradeId, setTradeId] = useState(null);     // trade whose TradeDetail modal is open (#136 pt1)
  const range = useRange("all");                    // anchored on Signal.created_at
  const { fromIso, toIso } = range;

  const { data: sum } = useData(() => api.reconciliationSummary(includeHistory, range.range),
    [includeHistory, busy, fromIso, toIso]);
  const { data: rows } = useData(() => api.reconciliationRows({ includeHistory, category, from: fromIso, to: toIso }),
    [includeHistory, category, busy, fromIso, toIso]);

  const refresh = async () => { setBusy(true); try { await api.reconciliationRefresh(); } finally { setBusy(v => !v); } };
  // Set/clear a claim's operator override, then re-pull so the category recomputes.
  const setOverride = async (claimId, outcome) => {
    try { await api.reconciliationSetOverride(claimId, { override_outcome: outcome || null }); }
    finally { setBusy(v => !v); }
  };

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
            <KPI label={<>Match rate<HelpHint term="match_rate" /></>} value={sum.match_rate != null ? `${sum.match_rate}%` : "—"}
              tone="beacon" sub={`${sum.matched}/${sum.comparable ?? sum.evaluable ?? sum.total} scored by the channel`} />
            <KPI label={<>Claim coverage<HelpHint term="claim_coverage" /></>}
              value={sum.claim_coverage != null ? `${sum.claim_coverage}%` : "—"}
              tone={sum.claim_coverage != null && sum.claim_coverage < 80 ? "warn" : "muted"}
              sub={`${sum.uncomparable ?? 0} traded, channel silent`} />
            <KPI label={<>No fill<HelpHint term="no_fill" /></>} value={sum.categories.no_fill || 0} tone="short" sub="placed, never filled" />
            <KPI label={<>Protected<HelpHint term="not_executed" /></>}
              value={sum.protected ?? ((sum.categories.executed_no_trade || 0) + (sum.categories.not_executed || 0))}
              tone="muted" sub="not traded — excluded from rate" />
          </div>

          {/* #173: the linker wedged for three days and nothing said so — the bot
              kept trading, the page kept rendering, claims just stopped. Loudest
              banner on the page, because everything below is stale when it fires. */}
          <LinkerHealth linker={sum.linker} />

          {/* #172: the match rate only covers the signals a channel chose to
              report. Show what the silent ones actually did, right next to it. */}
          {sum.unclaimed_outcome?.n > 0 && (
            <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
              <b>The match rate only sees {sum.claim_coverage}% of traded signals.</b>{" "}
              The {sum.unclaimed_outcome.n} the channels went silent on returned{" "}
              <span className="num">{sum.unclaimed_outcome.win_rate}%</span> win /{" "}
              <span className="num">{sum.unclaimed_outcome.net}</span> net, against{" "}
              <span className="num">{sum.claimed_outcome?.win_rate}%</span> /{" "}
              <span className="num">{sum.claimed_outcome?.net}</span> for the ones they reported.
              Channels announce wins and go quiet on losses, so read the rate as
              "of what they told us", never as overall performance.
            </div>
          )}

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
                      <Td right mono><BotResult row={r} /></Td>
                      <Td><Badge tone={catTone(r.category)}>{catLabel(r.category)}</Badge></Td>
                      <Td><span className="text-xs text-muted">{r.detail}</span></Td>
                    </tr>
                    {expanded === r.signal_id && (
                      <tr className="border-b border-edge/60 bg-panel2/40">
                        <td colSpan={7} className="px-4 py-3 space-y-3">
                          {/* clickable Signal # + Trade # -> shared TradeDetail modal (#136 pt1) */}
                          <div className="flex flex-wrap items-center gap-2 text-xs">
                            {(r.trade_ids || []).length
                              ? <button className="text-beacon hover:underline num" title="Open trade"
                                  onClick={() => setTradeId(r.trade_ids[0])}>Signal #{r.signal_id}</button>
                              : <span className="num text-muted">Signal #{r.signal_id}</span>}
                            {(r.trade_ids || []).map(tid => (
                              <button key={tid} className="text-beacon hover:underline num" title="Open trade"
                                onClick={() => setTradeId(tid)}>Trade #{tid}</button>
                            ))}
                            {r.protected && r.protected_reason &&
                              <span className="text-[11px] text-muted">· protected: {r.protected_reason}</span>}
                          </div>
                          {r.signal_text && (
                            <div className="text-xs whitespace-pre-wrap break-words text-ink/90">{r.signal_text}</div>
                          )}
                          {!!r.claims?.length && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-muted mb-1">Channel follow-ups</div>
                              {r.claims.map((c, i) => (
                                <div key={c.id ?? i} className="text-xs text-ink/90 flex flex-wrap items-center gap-2 py-0.5">
                                  <span className="num text-muted shrink-0">{when(c.at)}</span>
                                  <span className="break-words flex-1 min-w-[8rem]">{c.text}</span>
                                  {/* operator outcome override (#136 pt3) */}
                                  <span className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                                    <span className="text-[10px] text-muted">outcome</span>
                                    <Select value={c.override_outcome || ""}
                                      onChange={(e) => c.id != null && setOverride(c.id, e.target.value)}>
                                      {OVERRIDE_OPTS.map(([v, lbl]) => <option key={v} value={v}>{lbl}</option>)}
                                    </Select>
                                    {c.override_outcome && <span className="text-[10px] text-beacon" title="operator override active">●</span>}
                                  </span>
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

      {tradeId != null && <TradeDetail tradeId={tradeId} onClose={() => setTradeId(null)} />}
    </div>
  );
}
