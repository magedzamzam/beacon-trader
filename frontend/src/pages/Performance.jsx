import { Card, KPI, Table, Th, Td, Badge, Empty } from "../components/ui";
import { ErrorNote } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

const pct1 = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
const r3 = (v) => (v == null ? "—" : Number(v).toFixed(3));

export default function Performance({ account = "" }) {
  const range = useRange("all");           // anchored on leg CLOSE time
  const { fromIso, toIso } = range;

  const { data: sum } = useData(() => api.perfSummary(account, range.range), [account, fromIso, toIso]);
  const { data: bySrc } = useData(() => api.perfBySource(account, range.range), [account, fromIso, toIso]);
  // The A/B ruling instrument (#80/#85) + the de-lever verdict (#188). It is
  // arm-vs-arm, so it deliberately ignores the global account filter.
  const { data: geo, error: geoErr } = useData(() => api.executionGeometry(range.range),
                                               [fromIso, toIso]);

  return (
    <div className="space-y-6">
      <RangeFilter state={range} />

      {!sum ? <Card><Empty>Loading…</Empty></Card> : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KPI label="Realized P&L" value={money(sum.total_pl)} tone={tone(sum.total_pl)} />
            <KPI label="Win rate" value={`${sum.win_rate}%`} tone="beacon" sub={`${sum.wins}W / ${sum.losses}L`} />
            <KPI label={<>Profit factor<HelpHint term="profit_factor" /></>} value={sum.profit_factor ?? "—"} sub="gross win / loss" />
            <KPI label="Closed legs" value={sum.closed_legs} />
          </div>

          <Card>
            <div className="px-4 py-3 border-b border-edge">
              <div className="text-sm font-medium">By source — which channel actually reaches TP</div>
              <div className="text-[11px] text-muted mt-0.5">
                Win rate shows a 90% credible interval. Sources below the significance threshold
                are dimmed and tagged — read their verdict as provisional, not proven.
              </div>
            </div>
            {!bySrc || !bySrc.length ? <Empty>No closed legs in this range.</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px]">
                  <thead><tr className="border-b border-edge">
                    <Th>Source</Th><Th>Sample<HelpHint term="min_n" /></Th><Th right>Win %<HelpHint term="raw_wr" /></Th><Th right>P&L<HelpHint term="expectancy" /></Th>
                    <Th right>TP1</Th><Th right>TP2</Th><Th right>TP3+</Th><Th right>SL hits</Th>
                  </tr></thead>
                  <tbody>
                    {bySrc.map(s => {
                      const tp3plus = Object.entries(s.tp_hits).filter(([k]) => +k >= 3)
                        .reduce((a, [, v]) => a + v, 0);
                      return (
                        <tr key={s.source_id}
                          className={`border-b border-edge/60 ${s.significant ? "" : "opacity-60"}`}>
                          <Td>{s.name}</Td>
                          <Td>
                            <span className="num text-xs">{s.n_trades}</span>
                            {!s.significant && <span className="text-[10px] text-muted num">/{s.min_trades}</span>}
                            {s.significant
                              ? <Badge tone="beacon">significant</Badge>
                              : <Badge tone="warn">low-N</Badge>}
                          </Td>
                          <Td right mono>
                            {s.win_rate != null ? `${s.win_rate}%` : "—"}
                            {s.ci && <span className="block text-[10px] text-muted">CI {s.ci.low}–{s.ci.high}%</span>}
                          </Td>
                          <Td right mono><span className={`text-${tone(s.pl)}`}>{money(s.pl)}</span></Td>
                          <Td right mono>{s.tp_hits[1] || 0}</Td>
                          <Td right mono>{s.tp_hits[2] || 0}</Td>
                          <Td right mono>{tp3plus}</Td>
                          <Td right mono><span className="text-short">{s.sl_hits}</span></Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}

      <ExecutionGeometryCard data={geo} error={geoErr} />
    </div>
  );
}

// Payoff-geometry A/B in R-multiples (#80/#85), with the de-lever verdict (#188).
//
// This is the instrument the weekly promote/hold ruling is made from, and until
// now it had NO UI at all — the route was served and nothing on the platform
// called it. The verdict leads, because the failure mode it guards is an arm
// that posts a better R purely by risking less: that passes every robustness
// check in the manual, and under the compounding rule it would promote into the
// control permanently with no automatic rollback.
const VERDICT_TONE = {
  NO_SKILL_DEMONSTRATED: "short",
  OUTSIDE_DELEVER_NULL: "beacon",
  UNDECIDABLE: "warn",
};
const VERDICT_LABEL = {
  NO_SKILL_DEMONSTRATED: "no skill — de-levering",
  OUTSIDE_DELEVER_NULL: "outside the de-lever null",
  UNDECIDABLE: "undecidable",
};

function ExecutionGeometryCard({ data, error }) {
  const arms = data?.by_arm || [];
  const del = data?.delever || {};
  const control = del.control_account_id;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Execution geometry A/B<HelpHint term="expectancy" />
        <Badge tone="muted">shadow · read-only</Badge>
        {control != null && <span className="text-muted font-normal text-xs">
          · control = acct #{control}</span>}
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        R = realized P&L ÷ <b>planned</b> risk, so arms trading different nominal sizes stay
        comparable. But an arm that does not <b>deploy</b> its planned risk gets a better R for
        free — so each non-control arm is also tested against a <b>de-lever null</b>: the
        control's own P&L scaled by the measured deployment ratio, which has zero selection
        skill by construction. An arm inside that band has demonstrated nothing, however
        robust its ΔR looks. Judge only at N ≥ 30 per arm.
      </div>
      {error ? <div className="p-4"><ErrorNote>{error}</ErrorNote></div>
        : !data ? <Empty>Loading…</Empty>
        : !arms.length ? <Empty>No closed trades with a planned risk in this range.</Empty> : (<>
          <Table minW={1000}>
            <thead><tr className="border-b border-edge">
              <Th>Arm</Th><Th right>n</Th>
              <Th right>Win %<HelpHint term="raw_wr" /></Th>
              <Th right>Expectancy R<HelpHint term="expectancy" /></Th>
              <Th right>Payoff<HelpHint term="payoff" /></Th>
              <Th right>BE legs</Th><Th right>→TP3</Th>
              <Th right>Deployed ÷ planned</Th>
              <Th right>Avg R on deployed</Th>
            </tr></thead>
            <tbody>
              {arms.map(a => (
                <tr key={String(a.account_id)} className="border-b border-edge/60">
                  <Td>{a.account}{a.account_id === control && <> <Badge tone="muted">control</Badge></>}
                    {!!a.arms?.length && <span className="block text-[10px] text-muted">{a.arms.join(", ")}</span>}</Td>
                  <Td right mono>{a.n_trades}</Td>
                  <Td right mono>{pct1(a.win_rate)}
                    {a.win_rate_ci && <span className="block text-[10px] text-muted">
                      CI {pct1(a.win_rate_ci[0])}–{pct1(a.win_rate_ci[1])}</span>}</Td>
                  <Td right mono><span className={a.expectancy_R >= 0 ? "text-long" : "text-short"}>
                    {r3(a.expectancy_R)}</span></Td>
                  <Td right mono>{r3(a.payoff_ratio)}</Td>
                  <Td right mono>{pct1(a.breakeven_leg_rate)}</Td>
                  <Td right mono>{pct1(a.pct_winners_reach_tp3)}</Td>
                  {/* The two columns that separate skill from de-levering. */}
                  <Td right mono><span className={a.deployed_ratio != null && a.deployed_ratio < 0.9 ? "text-warn" : ""}>
                    {a.deployed_ratio == null ? "—" : r3(a.deployed_ratio)}</span></Td>
                  <Td right mono>{r3(a.avg_R_deployed)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          {Object.entries(del.arms || {}).map(([acct, v]) => (
            <div key={acct} className="px-4 py-3 border-t border-edge text-[11px]">
              <div className="flex items-center gap-2 flex-wrap">
                <b>acct #{acct} vs control</b>
                <Badge tone={VERDICT_TONE[v.verdict] || "muted"}>
                  {VERDICT_LABEL[v.verdict] || v.verdict}</Badge>
                <span className="num text-muted">n={v.n}</span>
                {v.deployed_ratio != null &&
                  <span className="num text-muted">· deployed {r3(v.deployed_ratio)}×</span>}
                {v.capture_asymmetry != null &&
                  <span className="num text-muted">· capture win {r3(v.win_capture)} / loss {r3(v.loss_capture)}
                    {" "}(asym {r3(v.capture_asymmetry)})</span>}
              </div>
              <div className="mt-1 text-muted">{v.reason}</div>
              {v.observed_dR?.mean != null && (
                <div className="mt-1 num text-muted">
                  observed ΔR {r3(v.observed_dR.mean)}
                  {" "}[{r3(v.observed_dR.ci_low)}, {r3(v.observed_dR.ci_high)}]
                  {" · "}de-lever null {r3(v.delever_null_dR?.mean)}
                  {" "}[{r3(v.delever_null_dR?.ci_low)}, {r3(v.delever_null_dR?.ci_high)}]
                  {" · "}{v.observed_dR.n_blocks} day-block(s)
                  {v.observed_dR.degenerate && <b className="text-warn"> · degenerate: one block has
                    no between-block variance, so this interval is a point, not a range</b>}
                </div>
              )}
            </div>
          ))}
          {!Object.keys(del.arms || {}).length && (
            <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
              Only one arm in range — a de-lever verdict needs a control and at least one
              other arm trading the same signals.
            </div>
          )}
        </>)}
    </Card>
  );
}
