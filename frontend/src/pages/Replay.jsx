import { useEffect, useState } from "react";
import { FlaskConical, ShieldAlert, TriangleAlert } from "lucide-react";
import { Card, Table, Th, Td, Badge, Empty } from "../components/ui";
import { ErrorNote, Button, Field, Input } from "../components/form";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";

/**
 * Replay workbench (#183) — build, launch and compare backtests from the portal.
 *
 * NOTHING RUNS HERE, AND NOTHING RUNS IN THE API. Launching queues a job; a
 * separate nice'd, CPU-capped worker claims one at a time and executes it. That
 * is what keeps the original objection answered — a 400k-bar sweep must never
 * compete with `monitor` for the box that manages open positions — while still
 * letting the operator drive backtesting without an SSH session. A page full of
 * impatient clicks becomes a queue, not N concurrent sweeps.
 *
 * The platform can REQUEST a run and can never edit one: INSERT/UPDATE on the
 * job queue, SELECT-only on runs and results.
 *
 * THE GUARDRAILS ARE THE PAGE, not a footnote on it. #169 §8 exists because a
 * variant sweep is a false-discovery machine, and a table that lets the eye read
 * an ordering without its N is precisely the failure mode it was written to
 * prevent. So everything below renders WITHOUT interaction, beside the numbers
 * it qualifies:
 *   · held-out vs in-sample, visually distinct — in-sample is not an edge
 *   · variants searched + best-of-N inflation σ
 *   · verdict-withheld rows are tinted and struck, not footnoted
 *   · the caveat counts (never filled / blocked / horizon-capped / ambiguous /
 *     suspect bars) — each is a quantity of "we do not actually know"
 *   · the §5 validation gate verdict; a run whose gate has not PASSED is marked
 *   · a persistent promotion banner
 * A variant report missing any of them is rendered as a warning, not a ranking.
 */
const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
const r2 = (v) => (v == null ? "—" : Number(v).toFixed(3));
const num = (v) => (v == null ? "—" : Number(v).toLocaleString());
const dt = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "—");

// The fields a ranked number is not readable without (§8.6).
const REQUIRED = ["headline_basis", "n_variants_searched", "n_closed", "caveats"];
const missingGuardrails = (rep) =>
  REQUIRED.filter(k => rep?.[k] == null &&
    !(k === "n_variants_searched" && rep?.guardrails?.n_variants_searched != null));

export default function Replay() {
  const [runs, setRuns] = useState(null);
  const [err, setErr] = useState(null);
  const [runId, setRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [runErr, setRunErr] = useState(null);
  const [variant, setVariant] = useState("");
  const [jobs, setJobs] = useState(null);
  const [tick, setTick] = useState(0);

  const reloadRuns = () => api.replayRuns(50)
    .then(d => { setRuns(d); if (d.runs?.length && !runId) setRunId(d.runs[0].id); })
    .catch(e => setErr(e.message));

  useEffect(() => { reloadRuns(); /* eslint-disable-next-line */ }, [tick]);

  // Poll the queue while anything is in flight. A sweep takes minutes, and a
  // page that shows a job as "queued" forever is indistinguishable from a
  // worker that is not running.
  useEffect(() => {
    let alive = true;
    const load = () => api.replayJobs(25).then(d => alive && setJobs(d)).catch(() => {});
    load();
    const busy = (jobs?.jobs || []).some(j => j.status === "queued" || j.status === "running");
    const id = setInterval(() => {
      load();
      if (busy) setTick(t => t + 1);          // a finished job means a new run
    }, busy ? 4000 : 15000);
    return () => { alive = false; clearInterval(id); };
    /* eslint-disable-next-line */
  }, [jobs?.jobs?.map(j => j.status).join(","), tick]);

  useEffect(() => {
    if (!runId) return;
    setRun(null); setRunErr(null); setVariant("");
    api.replayRun(runId)
      .then(d => { setRun(d); setVariant(Object.keys(d.variants || {})[0] || ""); })
      .catch(e => setRunErr(e.message));
  }, [runId]);

  const rep = run?.variants?.[variant] || null;

  return (
    <div className="space-y-4">
      <PromotionBanner />
      {err && <ErrorNote>{err}</ErrorNote>}
      {runs && runs.available === false && <NotGranted info={runs} />}
      <LaunchCard onQueued={() => setTick(t => t + 1)} />
      <QueueCard jobs={jobs} onChanged={() => setTick(t => t + 1)} />
      {runs && runs.available !== false && !runs.runs.length && <NoRuns cmd={runs.run_command} />}
      {runs && !!runs.runs?.length && (
        <RunPicker runs={runs.runs} runId={runId} onPick={setRunId} />
      )}
      {runErr && <ErrorNote>{runErr}</ErrorNote>}
      {runId && !run && !runErr && <Card><Empty>Loading run…</Empty></Card>}
      {run && (<>
        <ValidationCard run={run.run} validation={run.validation} />
        <RankingCard ranking={run.ranking} variants={run.variants}
          selected={variant} onSelect={setVariant} />
        {rep && <GuardrailStrip rep={rep} />}
        {rep && <CaveatsCard caveats={rep.caveats} settings={rep.settings} />}
        {rep && <BySourceCard bySource={rep.by_source} />}
        <DeclinedCard runId={run.run.id} variant={variant} />
      </>)}
    </div>
  );
}

// Persistent, non-dismissable. The one sentence that has to survive every
// reading of this page (CLAUDE.md §2).
function PromotionBanner() {
  return (
    <div className="card px-4 py-3 flex items-start gap-2 border-l-2 border-l-warn">
      <ShieldAlert className="w-4 h-4 text-warn shrink-0 mt-0.5" />
      <div className="text-[11px] text-muted">
        <b className="text-warn">Replay results are hypothesis-generating, not promotion-grade.</b>{" "}
        The live frozen-week A/B/C on a frozen config remains the only thing that promotes a
        config. Nothing on this page changes what trades — the harness has no broker credentials
        and cannot write a trading table, and a queued sweep runs in a CPU-capped worker,
        never in the API and never more than one at a time.
      </div>
    </div>
  );
}

function NotGranted({ info }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        <FlaskConical className="w-4 h-4 text-beacon" /> Replay results not readable
      </div>
      <div className="px-4 py-3 text-[11px] text-muted space-y-2">
        <div>{info.note}</div>
        <pre className="num text-[11px] bg-panel2 rounded p-2 whitespace-pre-wrap">{info.grant_sql}</pre>
        <div className="text-muted">Reported by the database: <span className="num">{info.reason}</span></div>
      </div>
    </Card>
  );
}

// --- launching a run from the portal ------------------------------------------
// The operator asked for backtesting to be fully front-end driven. This QUEUES;
// a separate nice'd, CPU-capped worker executes one job at a time, so a page
// full of impatient clicks becomes a queue rather than N concurrent 400k-bar
// sweeps competing with `monitor` for the box that manages open positions.
// Ladder NAMES only. The browser deliberately does not author `sl_rules`, or
// accounts, or risk — the worker scaffolds all of it from the live tables (see
// `_expand_scaffold`). The first version of this form sent `{name}` and nothing
// else, and the resulting sweep evaluated 1,873 signals, took ZERO, and still
// reported `done` with 5,619 rows: every account lookup missed. A config the
// page invents is a config that can drift from production silently.
const LADDERS = ["be_at_tp1", "be_at_tp2", "runner_no_ratchet"];

function LaunchCard({ onQueued }) {
  const [label, setLabel] = useState("exit ladder A/B/C");
  const [from, setFrom] = useState("2026-07-05");
  const [to, setTo] = useState("2026-07-30");
  const [holdout, setHoldout] = useState("2026-07-22");
  const [arms, setArms] = useState([...LADDERS]);
  const [equity, setEquity] = useState(10000);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const toggle = (a) => setArms(s => s.includes(a) ? s.filter(x => x !== a) : [...s, a]);

  const launch = async () => {
    setBusy(true); setMsg(null); setErr(null);
    try {
      // A high-level ASK. The worker reads accounts, risk, risk_limits, the
      // instrument and the session windows from the live tables and expands
      // this into the full run config, so a portal sweep is always in step with
      // production rather than with whatever this page last hardcoded.
      const config = {
        scaffold: true,
        label, symbol: "XAUUSD", equity: Number(equity) || 10000,
        ladders: arms,
        from: from ? `${from}T00:00:00Z` : undefined,
        to: to ? `${to}T23:59:59Z` : undefined,
        // Without a holdout the headline is IN-SAMPLE and is a description of
        // the past, not an edge — so it is a first-class field, not an option.
        holdout_from: holdout ? `${holdout}T00:00:00Z` : undefined,
        // The enqueue route requires a non-empty `variants` list as its bound;
        // the worker replaces it with the scaffolded arms.
        variants: arms.map(name => ({ name })),
      };
      const res = await api.replayEnqueue(label, config);
      setMsg(`Queued as job #${res.job_id}. A worker runs it — nothing executes in the API.`);
      onQueued?.();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        <FlaskConical className="w-4 h-4 text-beacon" /> Launch a backtest
        <Badge tone="muted">queued · one at a time</Badge>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Queues a sweep over the stored signal history. The accounts, risk, limits and
        instrument are read from the <b>live tables</b> by the worker — this form only picks
        the window and which exit ladders to compare, so a run cannot drift from production. It does <b>not</b> run here — a separate CPU-capped worker picks it
        up, so this never competes with the monitor. Results are
        <b> hypothesis-generating</b>; the live frozen-week A/B/C is still the only thing that
        promotes a config.
      </div>
      <div className="px-4 py-3 grid gap-3 md:grid-cols-2">
        <Field label="Label"><Input value={label} onChange={e => setLabel(e.target.value)} /></Field>
        <Field label="Exit ladders to compare">
          <div className="flex flex-wrap gap-1">
            {LADDERS.map(a => (
              <Button key={a} variant={arms.includes(a) ? "primary" : "ghost"}
                onClick={() => toggle(a)}>{a}</Button>
            ))}
          </div>
        </Field>
        <Field label="From (UTC)"><Input type="date" value={from} onChange={e => setFrom(e.target.value)} /></Field>
        <Field label="To (UTC)"><Input type="date" value={to} onChange={e => setTo(e.target.value)} /></Field>
        <Field label="Held-out from" hint="Everything before this date is in-sample. Without it the headline is not an edge.">
          <Input type="date" value={holdout} onChange={e => setHoldout(e.target.value)} />
        </Field>
        <Field label="Equity per account" hint="Sizing is only comparable to live if the budget is. Equity lives at the broker, not the ledger.">
          <Input type="number" value={equity} onChange={e => setEquity(e.target.value)} />
        </Field>
      </div>
      <div className="px-4 py-3 border-t border-edge flex items-center gap-3 flex-wrap">
        <Button onClick={launch} disabled={busy || !arms.length}>
          {busy ? "Queueing…" : `Queue ${arms.length} variant${arms.length === 1 ? "" : "s"}`}</Button>
        {!arms.length && <span className="text-[11px] text-warn">Pick at least one ladder.</span>}
        {msg && <span className="text-[11px] text-beacon">{msg}</span>}
        {err && <span className="text-[11px] text-short">{err}</span>}
        <span className="text-[11px] text-muted">
          Best-of-N is upward-biased: {arms.length} arms searched carries real luck.
        </span>
      </div>
    </Card>
  );
}

const JOB_TONE = { queued: "muted", running: "beacon", done: "long",
                   failed: "short", cancelled: "warn" };

function QueueCard({ jobs, onChanged }) {
  const rows = jobs?.jobs || [];
  if (jobs && jobs.available === false) return null;
  if (!rows.length) return null;
  const cancel = async (id) => { try { await api.replayCancelJob(id); onChanged?.(); } catch {} };
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">
        Queue<span className="text-muted font-normal text-xs"> · the worker runs one at a time</span>
      </div>
      <Table minW={760}>
        <thead><tr className="border-b border-edge">
          <Th>Job</Th><Th>Status</Th><Th>Progress</Th><Th right>Variants</Th><Th>Run</Th><Th></Th>
        </tr></thead>
        <tbody>
          {rows.map(j => (
            <tr key={j.id} className="border-b border-edge/60">
              <Td><span className="num text-xs">#{j.id}</span> {j.label || "(unlabelled)"}
                <span className="block text-[10px] text-muted">{dt(j.created_at)} · {j.requested_by}</span></Td>
              <Td><Badge tone={JOB_TONE[j.status] || "muted"}>{j.status}</Badge></Td>
              <Td><span className="text-[11px] text-muted">{j.error || j.progress || "—"}</span></Td>
              <Td right mono>{j.n_variants}</Td>
              <Td mono>{j.run_id ? `#${j.run_id}` : "—"}</Td>
              <Td>{j.status === "queued" &&
                <Button variant="ghost" onClick={() => cancel(j.id)}>cancel</Button>}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function NoRuns({ cmd }) {
  return (
    <Card><Empty>
      No replay runs stored yet. The harness is a CLI batch job — run one offline:
      <div className="mt-2"><span className="num text-xs">{cmd}</span></div>
    </Empty></Card>
  );
}

// --- run picker ---------------------------------------------------------------
// Each row carries the reasons NOT to open it: in-sample-only, all verdicts
// withheld, gate not run or failed. A picker that shows only a label and a date
// invites the operator to open the best-sounding run.
function RunPicker({ runs, runId, onPick }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        <FlaskConical className="w-4 h-4 text-beacon" /> Replay runs
        <span className="text-muted font-normal text-xs">· offline sweeps of the signal history</span>
      </div>
      <Table minW={980}>
        <thead><tr className="border-b border-edge">
          <Th>Run</Th><Th>Window</Th>
          <Th>Basis<HelpHint term="headline_basis" /></Th>
          <Th right>Variants<HelpHint term="best_of_n_inflation" /></Th>
          <Th>Verdicts</Th><Th>Gate</Th><Th>Code</Th>
        </tr></thead>
        <tbody>
          {runs.map(r => (
            <tr key={r.id} onClick={() => onPick(r.id)}
              className={`border-b border-edge/60 row-hover cursor-pointer ${r.id === runId ? "bg-beacon/5" : ""}`}>
              <Td>
                <span className="num text-xs">#{r.id}</span> {r.label || "(unlabelled)"}
                <span className="block text-[10px] text-muted">
                  {r.symbol} {r.timeframe} · {r.signal_source} · {r.status}
                </span>
              </Td>
              <Td><span className="num text-[11px] text-muted">{dt(r.from)} → {dt(r.to)}</span></Td>
              <Td><BasisBadge basis={r.headline_basis} /></Td>
              <Td right mono>
                {num(r.n_variants_searched)}
                {r.best_of_n_inflation_sigma != null &&
                  <span className="block text-[10px] text-warn">+{r.best_of_n_inflation_sigma}σ luck</span>}
              </Td>
              <Td>
                {r.n_ranked
                  ? <Badge tone={r.all_verdicts_withheld ? "warn" : r.n_verdicts_withheld ? "muted" : "beacon"}>
                      {r.n_verdicts_withheld}/{r.n_ranked} withheld</Badge>
                  : <span className="text-muted text-xs">—</span>}
              </Td>
              <Td><GateBadge v={r.validation} /></Td>
              <Td><span className="num text-[10px] text-muted">{(r.git_sha || "").slice(0, 7) || "—"}</span></Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

// held_out and in_sample must not look alike — one is a result, the other is a
// description of the data the variant was chosen on.
function BasisBadge({ basis }) {
  if (basis === "held_out") return <Badge tone="beacon">held-out</Badge>;
  if (basis === "in_sample") return <Badge tone="warn">in-sample only</Badge>;
  return <Badge tone="muted">unknown basis</Badge>;
}

// An INHERITED verdict is shown as inherited, never as the run's own. The gate
// validates the simulator — a code version over a set of bars — so a sweep on
// the same git_sha and candle_digest is genuinely covered by it; claiming the
// sweep was itself reconciled against broker truth would be a different and
// false statement.
function GateBadge({ v }) {
  if (!v || !v.ran) return <Badge tone="warn">not validated</Badge>;
  const inherited = v.source === "inherited";
  const label = (v.passed ? "gate passed" : "gate FAILED") + (inherited ? " · inherited" : "");
  return <Badge tone={v.passed ? (inherited ? "muted" : "beacon") : "short"}>{label}</Badge>;
}

// --- §5 validation gate -------------------------------------------------------
function ValidationCard({ run, validation }) {
  // `run.validation` is the flattened verdict and knows whether it was
  // inherited; `validation` is this run's OWN stored block, absent on a sweep.
  const v = run.validation || {};
  const inherited = v.source === "inherited";
  const gate = validation?.gate || (inherited ? { passed: v.passed, failures: v.failures,
                                                  systematic_bias: v.systematic_bias } : null);
  const ran = !!v.ran;
  const passed = !!v.passed;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Simulator validation<GateBadge v={run.validation} />
        <span className="text-muted font-normal text-xs">
          · run #{run.id} · {run.config_digest ? `config ${run.config_digest.slice(0, 8)}` : "no digest"}
          {run.candle_digest ? ` · candles ${run.candle_digest.slice(0, 8)}` : ""}
        </span>
      </div>
      <div className={`px-4 py-2 text-[11px] border-b border-edge ${ran && passed ? "text-muted" : "text-warn"}`}>
        {!ran ? (<>
          <b>This run's simulator was never reconciled against broker truth.</b> A counterfactual
          from an unvalidated harness compounds two unknowns — every variant it ranks inherits
          whatever the simulator gets wrong. Run <span className="num">python main.py validate
          --config runs/live-config.json</span> before acting on anything below.
        </>) : passed ? (<>
          The harness replayed the LIVE config over the LIVE signals and matched broker truth
          within the stated thresholds (leg-outcome agreement and |Δ R|). That makes the
          counterfactuals below readable — it does not make them promotion-grade.
          {inherited && <> <b>This verdict is inherited from run #{v.from_run_id}</b>, which
            gated the same code (<span className="num">{(run.git_sha || "").slice(0, 7)}</span>)
            over the same bars. The gate validates the SIMULATOR, so this run is covered by
            it — but this run was not itself reconciled against broker truth.</>}
        </>) : (<>
          <b>The validation gate FAILED.</b> A harness consistently rosier than live is a blocking
          defect, not a calibration offset. Do not act on a counterfactual from a failed gate.
        </>)}
      </div>
      {ran && !!gate?.failures?.length && (
        <div className="px-4 py-2 text-[11px] text-short border-b border-edge">
          {gate.failures.map((f, i) => <div key={i}>· {f}</div>)}
          {gate.systematic_bias && <div className="mt-1">Systematic bias: <b>{gate.systematic_bias}</b></div>}
        </div>
      )}
    </Card>
  );
}

// --- variant comparison -------------------------------------------------------
// The same R-metric conventions as Performance/the live weekly: the harness
// deliberately emits the same `geometry_ab_rollup` keys so an offline arm reads
// like a live one, and the UI must not undo that.
function RankingCard({ ranking, variants, selected, onSelect }) {
  const rows = ranking || [];
  if (!rows.length) return <Card><Empty>This run stored no variant ranking.</Empty></Card>;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Variant comparison<HelpHint term="expectancy" />
        <span className="text-muted font-normal text-xs">· click a row to open it</span>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Ranked on expectancy in R. Rows below the N ≥ 30 floor are <b className="text-warn">tinted and
        withheld</b> — a variant that wins on 11 trades has not won. Effective N is well below raw N:
        every trade is the same instrument over overlapping windows.
      </div>
      <Table minW={880}>
        <thead><tr className="border-b border-edge">
          <Th>Variant</Th>
          <Th>Basis<HelpHint term="headline_basis" /></Th>
          <Th right>Expectancy R</Th>
          <Th right>n closed<HelpHint term="min_n" /></Th>
          <Th right>Searched<HelpHint term="best_of_n_inflation" /></Th>
          <Th>Verdict</Th>
        </tr></thead>
        <tbody>
          {rows.map(r => {
            const held = r.verdict_withheld;
            return (
              <tr key={r.variant} onClick={() => onSelect(r.variant)}
                className={`border-b border-edge/60 row-hover cursor-pointer
                  ${r.variant === selected ? "bg-beacon/5" : ""} ${held ? "opacity-60" : ""}`}>
                <Td><span className="num text-xs">{r.variant}</span></Td>
                <Td><BasisBadge basis={r.basis} /></Td>
                <Td right mono>
                  <span className={held ? "line-through text-muted" : r.expectancy_R >= 0 ? "text-long" : "text-short"}>
                    {r2(r.expectancy_R)}
                  </span>
                </Td>
                <Td right mono>{num(r.n_closed)}</Td>
                <Td right mono>
                  {num(r.n_variants_searched)}
                  {r.best_of_n_inflation_sigma != null &&
                    <span className="block text-[10px] text-warn">+{r.best_of_n_inflation_sigma}σ</span>}
                </Td>
                <Td>{held
                  ? <Badge tone="warn">withheld · n &lt; 30</Badge>
                  : <Badge tone="muted">readable</Badge>}</Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
      {Object.keys(variants || {}).length > rows.length && (
        <div className="px-4 py-2 text-[11px] text-warn border-t border-edge">
          {Object.keys(variants).length - rows.length} variant(s) produced a report but no ranking row.
        </div>
      )}
    </Card>
  );
}

// The selected variant's headline, with every guardrail beside it. Renders a
// warning instead of a number when a required field is absent.
function GuardrailStrip({ rep }) {
  const missing = missingGuardrails(rep);
  const g = rep.guardrails || {};
  const arms = rep.headline?.by_arm || [];
  if (missing.length) {
    return (
      <Card><div className="px-4 py-3 text-[11px] text-warn flex items-start gap-2">
        <TriangleAlert className="w-4 h-4 shrink-0" />
        <div>This variant's report is missing {missing.join(", ")}, so it is <b>not reportable</b>
          {" "}(#169 §8.6) and is deliberately not rendered as a ranking.</div>
      </div></Card>
    );
  }
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        <span className="num">{rep.variant}</span>
        <BasisBadge basis={rep.headline_basis} />
        {rep.verdict_withheld && <Badge tone="warn">verdict withheld · n={rep.n_closed} &lt; {rep.min_trades_for_verdict}</Badge>}
        <span className="text-muted font-normal text-xs num">
          · searched {g.n_variants_searched}
          {g.best_of_n_inflation_sigma != null && ` · best-of-N inflation ≈ ${g.best_of_n_inflation_sigma}σ`}
        </span>
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        {rep.headline_basis === "held_out"
          ? "Headline is the HELD-OUT slice — signals after the walk-forward split date."
          : "No walk-forward split: this headline is IN-SAMPLE and is not reportable as an edge."}
        {g.regime?.trending_share != null &&
          ` Window regime: ${pct(g.regime.trending_share)} trending / ${pct(g.regime.ranging_share)} ranging (${g.regime.timeframe} ADX) — a result is not evidence of robustness outside this mix.`}
      </div>
      {!arms.length ? <Empty>No filled trades in the headline slice.</Empty> : (
        <Table minW={980}>
          <thead><tr className="border-b border-edge">
            <Th>Arm</Th><Th right>n</Th>
            <Th right>Win %<HelpHint term="raw_wr" /></Th>
            <Th right>Expectancy R<HelpHint term="expectancy" /></Th>
            <Th right>Avg win R</Th><Th right>Avg loss R</Th>
            <Th right>Payoff<HelpHint term="payoff" /></Th>
            <Th right>BE legs</Th>
            <Th right>→TP3</Th><Th right>Net</Th>
          </tr></thead>
          <tbody>
            {arms.map(a => (
              <tr key={String(a.account_id)} className={`border-b border-edge/60 ${rep.verdict_withheld ? "opacity-60" : ""}`}>
                <Td>{a.account}{!!a.arms?.length && <span className="block text-[10px] text-muted">{a.arms.join(", ")}</span>}</Td>
                <Td right mono>{a.n_trades}</Td>
                <Td right mono>{pct(a.win_rate)}
                  {a.win_rate_ci && <span className="block text-[10px] text-muted">CI {pct(a.win_rate_ci[0])}–{pct(a.win_rate_ci[1])}</span>}</Td>
                <Td right mono><span className={a.expectancy_R >= 0 ? "text-long" : "text-short"}>{r2(a.expectancy_R)}</span></Td>
                <Td right mono>{r2(a.avg_win_R)}</Td>
                <Td right mono>{r2(a.avg_loss_R)}</Td>
                <Td right mono>{r2(a.payoff_ratio)}</Td>
                <Td right mono>{pct(a.breakeven_leg_rate)}</Td>
                <Td right mono>{pct(a.pct_winners_reach_tp3)}</Td>
                <Td right mono>{num(a.net_nominal)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

// Each count is a quantity of "we do not actually know". Inline, never a
// footnote — a reader who cannot see them cannot judge the result.
const CAVEAT_FIELDS = [
  ["n_signals_evaluated", "signals evaluated", false],
  ["n_taken", "taken", false],
  ["n_filled", "filled", false],
  ["n_never_filled", "never filled", true],
  ["n_filtered_out", "filtered out", false],
  ["n_blocked_by_risk_limits", "blocked · risk caps", true],
  ["n_blocked_by_breaker", "blocked · breaker", true],
  ["n_no_candle_coverage", "no candle coverage", true],
  ["n_horizon_capped", "horizon-capped", true],
  ["n_same_bar_ambiguous_legs", "same-bar ambiguous legs", true],
  ["suspect_bars_excluded", "suspect bars excluded", true],
];

function CaveatsCard({ caveats, settings }) {
  if (!caveats) return null;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">
        What the simulation could not see
      </div>
      <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-4 gap-3">
        {CAVEAT_FIELDS.map(([k, label, warn]) => {
          const v = caveats[k];
          const hot = warn && Number(v) > 0;
          return (
            <div key={k} className="rounded bg-panel2 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
              <div className={`num text-sm ${hot ? "text-warn" : ""}`}>{num(v)}</div>
            </div>
          );
        })}
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
        {caveats.note}
        {settings && <> Horizon {settings.horizon_bars} bars · slippage {settings.slippage_points} pts ·
          sessions {settings.sessions_modelled ? "modelled" : <b className="text-warn">not modelled</b>}
          {settings.n_session_desized ? ` · ${settings.n_session_desized} session de-sized` : ""}.</>}
      </div>
      {!!Object.keys(caveats.not_taken_breakdown || {}).length && (
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge flex flex-wrap gap-2">
          {Object.entries(caveats.not_taken_breakdown).map(([k, v]) => (
            <Badge key={k} tone="muted">{k}: {v}</Badge>
          ))}
        </div>
      )}
    </Card>
  );
}

// Per-source is not an optional extra: the correct exit almost certainly differs
// by channel (median TP1 ranges 0.15R to 1.00R, #182), so a pooled-only answer
// averages away the thing being measured.
function BySourceCard({ bySource }) {
  const rows = Object.values(bySource || {});
  if (!rows.length) return null;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">
        Per-source breakdown<HelpHint term="tp1_distance_r" />
      </div>
      <Table minW={880}>
        <thead><tr className="border-b border-edge">
          <Th>Source</Th><Th right>n closed</Th><Th right>Win %</Th>
          <Th right>Expectancy R</Th><Th right>Payoff</Th><Th right>Net</Th><Th>Verdict</Th>
        </tr></thead>
        <tbody>
          {rows.map(s => {
            const arm = (s.rollup?.by_arm || [])[0] || {};
            return (
              <tr key={String(s.source_id)}
                className={`border-b border-edge/60 ${s.verdict_withheld ? "opacity-60" : ""}`}>
                <Td>{s.source || `source #${s.source_id ?? "—"}`}</Td>
                <Td right mono>{num(s.n_closed)}</Td>
                <Td right mono>{pct(arm.win_rate)}</Td>
                <Td right mono>
                  <span className={s.verdict_withheld ? "line-through text-muted"
                    : arm.expectancy_R >= 0 ? "text-long" : "text-short"}>{r2(arm.expectancy_R)}</span>
                </Td>
                <Td right mono>{r2(arm.payoff_ratio)}</Td>
                <Td right mono>{num(arm.net_nominal)}</Td>
                <Td>{s.verdict_withheld
                  ? <Badge tone="warn">withheld · n &lt; {s.min_trades_for_verdict}</Badge>
                  : <Badge tone="muted">readable</Badge>}</Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
    </Card>
  );
}

// The declined signals. A variant is defined as much by what it refuses as by
// what it takes, and this is the only view of what a filter rejected.
function DeclinedCard({ runId, variant }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (!runId) return;
    setData(null); setErr(null);
    api.replayResults(runId, { variant, taken: false, reason, limit: 200 })
      .then(setData).catch(e => setErr(e.message));
  }, [runId, variant, reason]);

  const reasons = Object.entries(data?.by_reason || {});
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-3 flex-wrap">
        <div className="text-sm font-medium">Declined signals
          <span className="text-muted font-normal text-xs"> · why this variant did not trade them</span></div>
        <div className="flex items-center gap-1 flex-wrap">
          <Button variant={reason === "" ? "primary" : "ghost"} onClick={() => setReason("")}>all</Button>
          {reasons.map(([k, v]) => (
            <Button key={k} variant={reason === k ? "primary" : "ghost"} onClick={() => setReason(k)}>
              {k} ({v})
            </Button>
          ))}
        </div>
      </div>
      {err ? <div className="p-4"><ErrorNote>{err}</ErrorNote></div>
        : !data ? <Empty>Loading…</Empty>
        : data.available === false ? <Empty>{data.note}</Empty>
        : !data.rows.length ? <Empty>This variant declined no signals.</Empty> : (
          <Table minW={720}>
            <thead><tr className="border-b border-edge">
              <Th>Signal</Th><Th>Source</Th><Th>Account</Th><Th>Reason</Th>
            </tr></thead>
            <tbody>
              {data.rows.map(r => (
                <tr key={r.id} className="border-b border-edge/60">
                  <Td mono>#{r.signal_id}</Td>
                  <Td mono>{r.source_id ?? "—"}</Td>
                  <Td mono>{r.account_id ?? "—"}</Td>
                  <Td><Badge tone="muted">{r.not_taken_reason || "—"}</Badge></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      {data?.total > (data?.rows?.length || 0) && (
        <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">
          Showing {data.rows.length} of {num(data.total)} — narrow by reason to see the rest.
        </div>
      )}
    </Card>
  );
}
