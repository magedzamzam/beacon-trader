import { useEffect, useState } from "react";
import { FlaskConical, ArrowRight, ChevronRight, ChevronDown } from "lucide-react";
import { Card, Table, Th, Td, Badge } from "../components/ui";
import { ErrorNote, Button, Field, Input, Select, NumberInput } from "../components/form";
import { api } from "../lib/api";

/**
 * What-if — would we have made money doing it differently?
 *
 * THE ONE QUESTION THIS PAGE ANSWERS: "I had 100 signals from Quartz Elite. What
 * if we'd filtered by RSI, or skipped a session, or exited later — would that
 * have made us profitable?"
 *
 * It runs the same signals twice, as they were and with one change, and shows
 * the two side by side with a sentence at the bottom.
 *
 * DELIBERATELY NOT HERE: credible intervals, best-of-N inflation, held-out vs
 * in-sample, de-lever nulls. Those are the right tools for RULING on a live A/B,
 * where a wrong call compounds into the control with no rollback — and the wrong
 * tools for "is this worth trying?", where they bury the answer. The quant view
 * still exists on /analytics/execution-geometry and in the harness; it is simply
 * not what this screen is for.
 *
 * Nothing here executes: launching queues a job for the CPU-capped worker.
 */
const money = (v) => (v == null ? "—" :
  (v >= 0 ? "+" : "") + Number(v).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

// The default window is RELATIVE, never a literal. Two hardcoded dates went
// stale the day after they were written and then silently dropped a further day
// of signals every day after that — an operator who never touched the fields got
// a verdict computed on a truncated book with no cue that the tail was missing.
// UTC on purpose: the form sends these back as `T00:00:00Z`/`T23:59:59Z` and the
// ledger is UTC, so a local-date reading would shift the window by a day.
const DEFAULT_WINDOW_DAYS = 30;
const isoDay = (d) => d.toISOString().slice(0, 10);
const defaultWindow = () => {
  const now = new Date();
  return { from: isoDay(new Date(now.getTime() - DEFAULT_WINDOW_DAYS * 864e5)),
           to: isoDay(now) };
};

const TRAVEL_LABEL = {
  straight_to_sl: "straight to SL",
  ranged: "ranged, no direction",
  went_our_way_then_reversed: "went +1R then reversed",
  ran_to_target: "ran to target",
  unknown: "unknown",
};

// One-click starting points. Each one just drops a row into the same builder
// the operator would have filled in by hand — a shortcut, never a second path.
const QUICK = [
  { label: "RSI", cond: { kind: "indicator", id: "rsi", field: "value", op: "lt", value: 60, timeframe: "15m" } },
  { label: "Fair value gap", cond: { kind: "indicator", id: "fvg", field: "present", op: "is_true", timeframe: "15m" } },
  { label: "Order block", cond: { kind: "indicator", id: "order_block", field: "dist_pct", op: "lte", value: 0.5, timeframe: "15m" } },
  { label: "Trending", cond: { kind: "regime", trending: true, timeframe: "4h" } },
  { label: "Session", cond: { kind: "session", sessions: ["London"] } },
];

const OPS = [
  { v: "lt", label: "is below" }, { v: "lte", label: "is at or below" },
  { v: "gt", label: "is above" }, { v: "gte", label: "is at or above" },
  { v: "eq", label: "is" }, { v: "ne", label: "is not" },
  { v: "is_true", label: "is there" }, { v: "is_false", label: "is not there" },
];
const BOOL_OPS = ["is_true", "is_false"];
const SESSIONS = ["Asian (Tokyo)", "London", "New York"];

// Exits: the three triggers and four targets `strategy/rules.py` already runs.
const TRIGGERS = [
  { v: "tp", label: "a target is hit" },
  { v: "points", label: "price moves (in price)" },
  { v: "r", label: "profit reaches (xR)" },
];
const ACTIONS = [
  { v: "breakeven", label: "breakeven" },
  { v: "previous_tp", label: "the previous target" },
  { v: "tp", label: "a target" },
];
// Written INTO state when the trigger changes. A default that only ever reaches
// the input's `value` ships as undefined, and the worker reads that as zero.
const TRIGGER_DEFAULTS = { tp: { index: 1 }, points: { points: 30 }, r: { r: 1 } };

// A step with a zero distance is refused by the API, so say so here rather than
// letting the operator queue a run that 400s.
const stepIsComplete = (s) => {
  const t = s.when || {};
  if (t.kind === "points") return Number(t.points) > 0;
  if (t.kind === "r") return Number(t.r) > 0;
  return true;
};

const EXIT_MODES = [
  { v: "", label: "Leave it as it is" },
  { v: "let_it_run", label: "Never move the stop" },
  { v: "custom", label: "Build my own" },
];

export default function Replay() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [catalog, setCatalog] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [runs, setRuns] = useState([]);
  const [openRun, setOpenRun] = useState(null);
  const [report, setReport] = useState(null);
  const [err, setErr] = useState(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    api.sources().then(setSources).catch(() => {});
    api.accounts().then(setAccounts).catch(() => {});
    // The indicator list IS the TA registry, so the builder offers exactly what
    // the engine can compute — no hand-kept menu to drift out of sync.
    api.taCatalog().then(setCatalog).catch(() => {});
  }, []);

  // Self-scheduling poll rather than a fixed interval: a run takes minutes, so
  // the page watches closely while the worker is busy and goes quiet when it is
  // not. A failed poll leaves the last good view up — a backtest page that
  // blanks itself on one dropped request is worse than a slightly stale one.
  useEffect(() => {
    let alive = true, timer = null;
    const load = async () => {
      try {
        const [j, r] = await Promise.all([api.replayJobs(15), api.replayRuns(25)]);
        if (!alive) return;
        setJobs(j.jobs || []);
        setRuns(r.runs || []);
        const busy = (j.jobs || []).some(
          x => x.status === "queued" || x.status === "running");
        timer = setTimeout(load, busy ? 4000 : 20000);
      } catch {
        if (alive) timer = setTimeout(load, 20000);
      }
    };
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [refresh]);

  useEffect(() => {
    if (!openRun) { setReport(null); return; }
    api.replayRun(openRun)
      .then(d => setReport(d.summary?.whatif || null))
      .catch(e => setErr(e.message));
  }, [openRun]);

  return (
    <div className="space-y-4">
      <NewBacktest sources={sources} accounts={accounts} catalog={catalog}
        onQueued={() => setRefresh(n => n + 1)} />
      {err && <ErrorNote>{err}</ErrorNote>}
      <History jobs={jobs} runs={runs} openRun={openRun} onOpen={setOpenRun}
        onChanged={() => setRefresh(n => n + 1)} />
      {report && <Report r={report} runId={openRun} />}
    </div>
  );
}

// --- step 1-4: the form -------------------------------------------------------
function NewBacktest({ sources, accounts, catalog, onQueued }) {
  const [scopeType, setScopeType] = useState("source");
  const [sourceId, setSourceId] = useState("");
  const [accountId, setAccountId] = useState("");
  // Lazy initialiser: evaluated once on mount, so the window is anchored to the
  // day the page was opened rather than re-derived on every render.
  const [from, setFrom] = useState(() => defaultWindow().from);
  const [to, setTo] = useState(() => defaultWindow().to);
  const [conds, setConds] = useState([]);            // free-form entry conditions
  const [atr, setAtr] = useState("");                // the one non-indicator filter
  const [exitMode, setExitMode] = useState("");
  const [steps, setSteps] = useState([]);            // free-form exit ladder
  const [risk, setRisk] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const setCond = (i, patch) =>
    setConds(cs => cs.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const setStep = (i, patch) =>
    setSteps(ss => ss.map((s, j) => (j === i ? { ...s, ...patch } : s)));

  const run = async () => {
    setBusy(true); setMsg(null); setErr(null);
    try {
      // Stop distance has no `when.type` in the live filtration grammar, so it
      // stays a named preset the worker applies on the signal set itself.
      const filters = atr
        ? [{ kind: "min_stop_atr", value: Number(atr) }] : [];
      const scope = scopeType === "source" ? { type: "source", source_id: sourceId }
        : scopeType === "account" ? { type: "account", account_id: accountId }
        : { type: "manual" };
      const label = scopeType === "source"
        ? (sources.find(s => String(s.id) === String(sourceId))?.name || "source")
        : scopeType === "account" ? "account" : "all sources";
      const res = await api.replayEnqueue(`what-if · ${label}`, {
        mode: "whatif", scope, symbol: "XAUUSD",
        from: from ? `${from}T00:00:00Z` : undefined,
        to: to ? `${to}T23:59:59Z` : undefined,
        changes: {
          filters,
          conditions: conds.length ? conds : undefined,
          exit: exitMode === "let_it_run" ? "let_it_run" : undefined,
          exit_steps: exitMode === "custom" && steps.length ? steps : undefined,
          risk_percent: risk ? Number(risk) : undefined,
        },
        // No variants list: the browser names the question, the worker builds
        // both arms from the live config. A page that authored arms is how the
        // last launch button produced runs that took zero trades.
      });
      // The route reports a missing grant as a 202 with `available: false`,
      // because it is the expected state on a box where replay was never set
      // up. Saying "Queued as #null" there would send the operator looking for
      // a job that was never created.
      if (res.available === false) { setErr(res.note || res.reason); return; }
      setMsg(`Queued as #${res.job_id}. It'll appear in history when it finishes.`);
      onQueued?.();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const ready = scopeType === "manual" ||
    (scopeType === "source" && sourceId) || (scopeType === "account" && accountId);
  const badStep = exitMode === "custom" && steps.some(s => !stepIsComplete(s));
  const nothingChanged = !conds.length && !atr && !risk &&
    !(exitMode === "let_it_run" || (exitMode === "custom" && steps.length));

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        <FlaskConical className="w-4 h-4 text-beacon" /> Start a new backtest
      </div>
      <div className="px-4 py-2 text-[11px] text-muted border-b border-edge">
        Runs your real signals twice — as they actually went, and with one change — and tells you
        which made more money.
      </div>

      <div className="px-4 py-4 space-y-4">
        <Step n="1" title="What are we testing?">
          <div className="flex flex-wrap gap-2">
            {[["source", "One signal source"], ["account", "A whole account"],
              ["manual", "Everything"]].map(([k, l]) => (
              <Button key={k} variant={scopeType === k ? "primary" : "ghost"}
                onClick={() => setScopeType(k)}>{l}</Button>
            ))}
          </div>
          {scopeType === "source" && (
            <Select value={sourceId} onChange={e => setSourceId(e.target.value)}>
              <option value="">choose a channel…</option>
              {sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          )}
          {scopeType === "account" && (
            <Select value={accountId} onChange={e => setAccountId(e.target.value)}>
              <option value="">choose an account…</option>
              {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </Select>
          )}
        </Step>

        <Step n="2" title="Over what dates?">
          <div className="flex flex-wrap gap-3">
            <Field label="From"><Input type="date" value={from}
              onChange={e => setFrom(e.target.value)} /></Field>
            <Field label="To"><Input type="date" value={to}
              onChange={e => setTo(e.target.value)} /></Field>
          </div>
        </Step>

        <Step n="3" title="Only take the trade when…">
          <div className="space-y-2">
            {conds.map((c, i) => (
              <ConditionRow key={i} c={c} catalog={catalog}
                onChange={p => setCond(i, p)}
                onRemove={() => setConds(cs => cs.filter((_, j) => j !== i))} />
            ))}
            {!conds.length && (
              <div className="text-[11px] text-muted">
                Nothing set — every signal is taken, exactly as today.
              </div>
            )}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <span className="text-[11px] text-muted">add:</span>
              {QUICK.map(q => (
                <Button key={q.label} variant="ghost"
                  onClick={() => setConds(cs => [...cs, { ...q.cond }])}>
                  + {q.label}</Button>
              ))}
              <Button variant="ghost" onClick={() => setConds(cs => [...cs,
                { kind: "indicator", id: "rsi", field: "value", op: "lt",
                  value: 60, timeframe: "15m" }])}>+ anything else…</Button>
            </div>
            {conds.length > 1 && (
              <div className="text-[11px] text-muted">All of them must hold.</div>
            )}
          </div>
        </Step>

        <Step n="4" title="And handle the exit like this">
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2">
              {EXIT_MODES.map(m => (
                <Button key={m.v} variant={exitMode === m.v ? "primary" : "ghost"}
                  onClick={() => setExitMode(m.v)}>{m.label}</Button>
              ))}
            </div>
            {exitMode === "custom" && (
              <div className="space-y-2">
                {steps.map((s, i) => (
                  <StepRow key={i} s={s} onChange={p => setStep(i, p)}
                    onRemove={() => setSteps(ss => ss.filter((_, j) => j !== i))} />
                ))}
                <Button variant="ghost" onClick={() => setSteps(ss => [...ss,
                  { when: { kind: "tp", ...TRIGGER_DEFAULTS.tp },
                    then: { kind: "breakeven" } }])}>
                  + add a step</Button>
                {steps.some(s => !stepIsComplete(s)) && (
                  <div className="text-[11px] text-warn">
                    A step needs a distance above zero, or it fires the moment
                    the trade stops losing.
                  </div>
                )}
                {!steps.length && (
                  <div className="text-[11px] text-warn">
                    Add at least one step, or nothing changes.
                  </div>
                )}
              </div>
            )}
          </div>
        </Step>

        <Step n="5" title="Anything else? (optional)">
          <div className="flex flex-wrap gap-3">
            <Field label="Skip stops tighter than" hint="× ATR · blank = keep them all">
              <NumberInput value={atr} step="0.25" placeholder="off"
                onChange={e => setAtr(e.target.value)} />
            </Field>
            <Field label="Risk per trade (%)" hint="blank = leave it as it is">
              <NumberInput value={risk} step="0.25" placeholder="unchanged"
                onChange={e => setRisk(e.target.value)} />
            </Field>
          </div>
        </Step>
      </div>

      <div className="px-4 py-3 border-t border-edge flex items-center gap-3 flex-wrap">
        <Button onClick={run} disabled={busy || !ready || nothingChanged || badStep}>
          {busy ? "Queueing…" : "Run it"}</Button>
        {!ready && <span className="text-[11px] text-warn">Pick what to test first.</span>}
        {ready && nothingChanged &&
          <span className="text-[11px] text-warn">Change at least one thing, or both runs are identical.</span>}
        {msg && <span className="text-[11px] text-beacon">{msg}</span>}
        {err && <span className="text-[11px] text-short">{err}</span>}
      </div>
    </Card>
  );
}

// One entry condition. The indicator list is the TA registry itself (45 of them,
// FVG and order blocks included) — the page offers whatever the engine can
// actually compute rather than a menu that has to be kept in sync by hand.
function ConditionRow({ c, catalog, onChange, onRemove }) {
  const inds = catalog?.indicators || [];
  const spec = inds.find(i => i.id === c.id);
  const fields = spec?.outputs || ["value"];
  const tfs = catalog?.timeframes || ["15m"];
  const boolOp = BOOL_OPS.includes(c.op);

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm bg-panel2/40 rounded-lg px-2 py-1.5">
      {c.kind === "indicator" && (<>
        <Select value={c.id} onChange={e => {
          const s = inds.find(i => i.id === e.target.value);
          // Field names differ per indicator, so a stale one would silently read
          // as missing and the condition would never hold.
          onChange({ id: e.target.value, field: (s?.outputs || ["value"])[0] });
        }}>
          {inds.map(i => <option key={i.id} value={i.id}>{i.label}</option>)}
        </Select>
        {fields.length > 1 && (
          <Select value={c.field} onChange={e => onChange({ field: e.target.value })}>
            {fields.map(f => <option key={f} value={f}>{f.replace(/_/g, " ")}</option>)}
          </Select>
        )}
        <Select value={c.timeframe} onChange={e => onChange({ timeframe: e.target.value })}>
          {tfs.map(t => <option key={t} value={t}>{t}</option>)}
        </Select>
        <Select value={c.op} onChange={e => onChange({ op: e.target.value })}>
          {OPS.map(o => <option key={o.v} value={o.v}>{o.label}</option>)}
        </Select>
        {!boolOp && (
          <input type="number" step="any" value={c.value ?? ""}
            onChange={e => onChange({ value: e.target.value === "" ? "" : Number(e.target.value) })}
            className="w-24 bg-panel border border-edge rounded px-2 py-1 num text-xs" />
        )}
      </>)}

      {(c.kind === "session" || c.kind === "not_session") && (<>
        <Select value={c.kind}
          onChange={e => onChange({ kind: e.target.value })}>
          <option value="session">it is</option>
          <option value="not_session">it is not</option>
        </Select>
        <Select value={(c.sessions || [])[0] || ""}
          onChange={e => onChange({ sessions: [e.target.value] })}>
          {SESSIONS.map(s => <option key={s} value={s}>{s}</option>)}
        </Select>
      </>)}

      {c.kind === "regime" && (<>
        <span className="text-muted text-xs">the market is</span>
        <Select value={c.trending ? "1" : "0"}
          onChange={e => onChange({ trending: e.target.value === "1" })}>
          <option value="1">trending</option>
          <option value="0">ranging</option>
        </Select>
        <Select value={c.timeframe || "4h"}
          onChange={e => onChange({ timeframe: e.target.value })}>
          {tfs.map(t => <option key={t} value={t}>{t}</option>)}
        </Select>
      </>)}

      <Button variant="danger" onClick={onRemove}>remove</Button>
    </div>
  );
}

// One exit step. `previous target` is only offered on a TP trigger because the
// engine reads that target off the TP that fired — on a price or R trigger it
// resolves to nothing and the step silently does nothing.
function StepRow({ s, onChange, onRemove }) {
  const t = s.when || {}, a = s.then || {};
  const setWhen = (p) => onChange({ when: { ...t, ...p } });
  const setThen = (p) => onChange({ then: { ...a, ...p } });
  const actions = t.kind === "tp" ? ACTIONS : ACTIONS.filter(x => x.v !== "previous_tp");

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm bg-panel2/40 rounded-lg px-2 py-1.5">
      <span className="text-muted text-xs">when</span>
      <Select value={t.kind} onChange={e => {
        // Seed the new trigger's default INTO state, never only into the input's
        // display. A displayed-but-unwritten 30 shipped `points: undefined`,
        // which the worker read as 0 — a stop that moves to breakeven the moment
        // price is not losing. On GOLD VIP that took stop-outs from 27 to 72 and
        // the verdict blamed the exit the operator thought they had built.
        const kind = e.target.value;
        onChange({ when: { ...TRIGGER_DEFAULTS[kind], kind } });
        if (kind !== "tp" && a.kind === "previous_tp") setThen({ kind: "breakeven" });
      }}>
        {TRIGGERS.map(x => <option key={x.v} value={x.v}>{x.label}</option>)}
      </Select>
      {t.kind === "tp" && (
        <Select value={t.index ?? 1}
          onChange={e => setWhen({ index: Number(e.target.value) })}>
          {[1, 2, 3, 4, 5].map(i => <option key={i} value={i}>TP{i}</option>)}
        </Select>
      )}
      {t.kind === "points" && (
        <input type="number" step="any" min="0.01" value={t.points ?? ""}
          onChange={e => setWhen({ points: Number(e.target.value) })}
          className="w-24 bg-panel border border-edge rounded px-2 py-1 num text-xs" />
      )}
      {t.kind === "r" && (
        <input type="number" step="0.1" min="0.1" value={t.r ?? ""}
          onChange={e => setWhen({ r: Number(e.target.value) })}
          className="w-24 bg-panel border border-edge rounded px-2 py-1 num text-xs" />
      )}
      <span className="text-muted text-xs">move the stop to</span>
      <Select value={a.kind} onChange={e => setThen({ kind: e.target.value })}>
        {actions.map(x => <option key={x.v} value={x.v}>{x.label}</option>)}
      </Select>
      {a.kind === "tp" && (
        <Select value={a.index || 1}
          onChange={e => setThen({ index: Number(e.target.value) })}>
          {[1, 2, 3, 4, 5].map(i => <option key={i} value={i}>TP{i}</option>)}
        </Select>
      )}
      <Button variant="danger" onClick={onRemove}>remove</Button>
    </div>
  );
}

function Step({ n, title, children }) {
  return (
    <div className="flex gap-3">
      <div className="w-6 h-6 shrink-0 rounded-full bg-panel2 grid place-items-center text-[11px] num text-muted">{n}</div>
      <div className="flex-1 space-y-2">
        <div className="text-sm">{title}</div>
        {children}
      </div>
    </div>
  );
}

// --- history ------------------------------------------------------------------
const JOB_TONE = { queued: "muted", running: "beacon", done: "long",
                   failed: "short", cancelled: "warn" };

function History({ jobs, runs, openRun, onOpen, onChanged }) {
  const running = jobs.filter(j => j.status === "queued" || j.status === "running");
  const done = runs.filter(r => r.label && r.label.startsWith("what-if"));
  // Per-row, because the reason a cancel failed belongs next to the job it
  // failed on — and it is nearly always the 409 below, which is information.
  const [cancelErr, setCancelErr] = useState({});
  const [cancelling, setCancelling] = useState(null);
  if (!running.length && !done.length) return null;

  // The worker runs ONE job at a time, so a mis-scoped sweep does not merely
  // waste its own minutes — it blocks every run behind it. Offered on `queued`
  // only, exactly mirroring the route: a RUNNING job is deliberately left to
  // the stale-job reaper and returns 409. A job that starts between render and
  // click hits that 409, which is the truth and is shown, not swallowed.
  const cancel = async (id) => {
    setCancelling(id);
    setCancelErr(e => ({ ...e, [id]: null }));
    try {
      await api.replayCancelJob(id);
      onChanged?.();
    } catch (e) {
      setCancelErr(err => ({ ...err, [id]: e.message || "could not cancel" }));
    } finally {
      setCancelling(null);
    }
  };

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">History</div>
      {running.map(j => (
        <div key={j.id} className="px-4 py-2 border-b border-edge/60 text-[11px] flex items-center gap-2">
          <Badge tone={JOB_TONE[j.status]}>{j.status}</Badge>
          <span>{j.label}</span>
          <span className="text-muted">{j.progress || "…"}</span>
          {cancelErr[j.id] && <span className="text-warn">{cancelErr[j.id]}</span>}
          {j.status === "queued" && (
            <button onClick={() => cancel(j.id)} disabled={cancelling === j.id}
              className="ml-auto text-muted hover:text-short disabled:opacity-50">
              {cancelling === j.id ? "cancelling…" : "cancel"}</button>
          )}
        </div>
      ))}
      {!!done.length && (
        <Table minW={640}>
          <thead><tr className="border-b border-edge">
            <Th>Backtest</Th><Th>Window</Th><Th></Th>
          </tr></thead>
          <tbody>
            {done.map(r => (
              <tr key={r.id} onClick={() => onOpen(r.id)}
                className={`border-b border-edge/60 row-hover cursor-pointer ${
                  r.id === openRun ? "bg-beacon/5" : ""}`}>
                <Td>{r.label}<span className="block text-[10px] text-muted num">
                  #{r.id} · {String(r.finished_at || "").slice(0, 16).replace("T", " ")}</span></Td>
                <Td mono>
                  {String(r.from || "").slice(0, 10)} → {String(r.to || "").slice(0, 10)}</Td>
                <Td><ArrowRight className="w-4 h-4 text-muted" /></Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

// --- the answer ---------------------------------------------------------------
function Row({ label, a, b, indent, strong }) {
  return (
    <tr className="border-b border-edge/40">
      <Td><span className={`${indent ? "pl-4 text-muted" : ""} ${strong ? "font-medium" : ""}`}>
        {label}</span></Td>
      <Td right mono>{a}</Td>
      <Td right mono>{b}</Td>
    </tr>
  );
}

function Report({ r, runId }) {
  const b = r.baseline || {}, w = r.whatif || {}, v = r.verdict || {};
  const travelKeys = [...new Set([...Object.keys(b.travel || {}),
                                  ...Object.keys(w.travel || {})])];
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">
        {r.scope}
        <span className="text-muted font-normal text-xs num">
          {" · "}{String(r.from || "").slice(0, 10)} → {String(r.to || "").slice(0, 10)}</span>
      </div>
      <Table minW={640}>
        <thead><tr className="border-b border-edge">
          <Th></Th><Th right>Your setup now</Th><Th right>What-if: {r.change}</Th>
        </tr></thead>
        <tbody>
          <Row label="Signals" a={b.signals} b={w.signals} />
          <Row label="Executed" a={b.executed} b={w.executed} />
          <Row label="Skipped" a={b.skipped} b={w.skipped} />
          <Row indent label="· filtered by rule" a={b.skipped_by_rule} b={w.skipped_by_rule} />
          <Row indent label="· never filled" a={b.skipped_no_fill} b={w.skipped_no_fill} />
          {(b.skipped_other > 0 || w.skipped_other > 0) && (
            <Row indent label="· blocked before entry" a={b.skipped_other} b={w.skipped_other} />
          )}
          <Row strong label="Profit / loss"
            a={<span className={b.profit >= 0 ? "text-long" : "text-short"}>{money(b.profit)}</span>}
            b={<span className={w.profit >= 0 ? "text-long" : "text-short"}>{money(w.profit)}</span>} />
          {r.actual?.trades > 0 && (
            <Row label="Actually traded" indent
              a={<span className={r.actual.profit >= 0 ? "text-long" : "text-short"}>
                {money(r.actual.profit)}</span>}
              b={<span className="text-muted">{r.actual.trades} real orders</span>} />
          )}
          <Row label="Wins / losses" a={`${b.wins} / ${b.losses}`} b={`${w.wins} / ${w.losses}`} />
          <Row label="TP1 hit" a={b.tp1} b={w.tp1} />
          <Row label="TP2 hit" a={b.tp2} b={w.tp2} />
          <Row label="TP3 hit" a={b.tp3} b={w.tp3} />
          <Row label="Stopped out" a={b.stopped_out} b={w.stopped_out} />
          <tr className="border-b border-edge/40">
            <Td><span className="font-medium">How price moved</span></Td><Td /><Td />
          </tr>
          {travelKeys.map(k => (
            <Row key={k} indent label={TRAVEL_LABEL[k] || k}
              a={(b.travel || {})[k] || 0} b={(w.travel || {})[k] || 0} />
          ))}
        </tbody>
      </Table>
      <div className={`px-4 py-3 border-t border-edge ${v.better ? "text-long" : "text-short"}`}>
        <div className="text-xs font-medium uppercase tracking-wider text-muted mb-1">Verdict</div>
        <div className="text-sm">{v.headline}</div>
      </div>
      {(r.caveats || []).map((c, i) => (
        <div key={i} className="px-4 py-2 text-[11px] text-warn border-t border-edge">{c}</div>
      ))}
      <Trades runId={runId} />
      <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">{r.note}</div>
    </Card>
  );
}

// --- the receipts -------------------------------------------------------------
// "How do I know the engine is telling the truth?" — by looking at what it says
// it did. Every run already stores one row per signal per arm; this just shows
// them. Collapsed by default: it is a spot-check you open when a number looks
// wrong, not something to read every time.
const OUTCOME = { tp_hit: "hit", sl_hit: "stopped out", breakeven: "closed at breakeven" };

// Tailwind's JIT cannot emit a class assembled by interpolation (`text-${tone}`
// produces nothing), so every tone resolves to a literal that appears here.
const TONE = { long: "text-long", short: "text-short", muted: "text-muted" };

function tradeLine(row) {
  if (!row.taken) return { what: `skipped — ${row.not_taken_reason || "no reason given"}`, tone: "muted" };
  if (!row.ever_filled) return { what: "never filled", tone: "muted" };
  const legs = row.legs || [];
  const best = Math.max(0, ...legs.filter(l => l.outcome === "tp_hit")
    .map(l => l.tp_index || 0));
  if (best) return { what: `TP${best} hit`, tone: "long" };
  if (legs.some(l => l.outcome === "sl_hit")) return { what: "stopped out", tone: "short" };
  if (legs.some(l => l.outcome === "breakeven")) return { what: "closed at breakeven", tone: "muted" };
  return { what: "still open at the end", tone: "muted" };
}

const lots = (row) => (row.legs || []).reduce((n, l) => n + (Number(l.lot) || 0), 0);
const px = (v) => (v == null ? "—" : Number(v).toFixed(2));

function Trades({ runId }) {
  const [open, setOpen] = useState(false);
  const [arm, setArm] = useState("whatif");
  const [rows, setRows] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!open || !runId) return;
    setRows(null); setErr(null);
    api.replayResults(runId, { variant: arm, limit: 500 })
      .then(d => setRows(d.rows || []))
      .catch(e => setErr(e.message));
  }, [open, arm, runId]);

  if (!runId) return null;
  // TRADES FIRST. Stored order is by row id, which on this book puts the
  // onboarding backfill — 33 signals that never filled — at the top and buries
  // every real trade below them. The spot-check is worthless if you have to
  // scroll past 33 blanks to reach something that happened.
  const rank = (r) => (r.taken && r.ever_filled ? 0 : r.taken ? 1 : 2);
  const sorted = (rows || []).slice().sort(
    (a, b) => rank(a) - rank(b) || String(a.signal_at || "").localeCompare(String(b.signal_at || "")));
  const filled = sorted.filter(r => r.taken && r.ever_filled).length;
  const unfilled = sorted.filter(r => r.taken && !r.ever_filled).length;
  const skipped = sorted.length - filled - unfilled;

  return (
    <div className="border-t border-edge">
      <button onClick={() => setOpen(o => !o)}
        className="w-full px-4 py-2 flex items-center gap-2 text-[11px] text-muted hover:text-ink">
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Show me the trades
      </button>

      {open && (
        <div className="px-4 pb-3 space-y-2">
          <div className="flex gap-2">
            {[["baseline", "Your setup now"], ["whatif", "What-if"]].map(([v, l]) => (
              <Button key={v} variant={arm === v ? "primary" : "ghost"}
                onClick={() => { setArm(v); setExpanded(null); }}>{l}</Button>
            ))}
          </div>
          {err && <div className="text-[11px] text-short">{err}</div>}
          {rows === null && !err && <div className="text-[11px] text-muted">loading…</div>}
          {rows && !rows.length && <div className="text-[11px] text-muted">No rows stored for this run.</div>}
          {rows && !!rows.length && (
            <>
              <div className="text-[11px] text-muted">
                {filled} traded · {unfilled} never filled · {skipped} skipped
              </div>
              <div className="divide-y divide-edge/40 rounded-lg bg-panel2/30">
                {sorted.map(row => {
                  const o = tradeLine(row);
                  const isOpen = expanded === row.id;
                  const size = lots(row);
                  return (
                    <div key={row.id}>
                      <button onClick={() => setExpanded(isOpen ? null : row.id)}
                        className="w-full px-3 py-1.5 flex items-center gap-3 text-xs row-hover text-left">
                        <span className="num text-muted w-14 shrink-0">#{row.signal_id}</span>
                        <span className={`w-10 shrink-0 ${row.direction === "SELL" ? "text-short" : "text-long"}`}>
                          {row.direction || "—"}</span>
                        <span className="num text-muted w-24 shrink-0">
                          {String(row.signal_at || "").slice(5, 16).replace("T", " ")}</span>
                        <span className={`flex-1 ${TONE[o.tone]}`}>{o.what}</span>
                        {/* Money only where there WAS money. A never-filled
                            row showed "+0.00", which reads as a flat trade
                            rather than as no trade at all. Its size still
                            shows — that is what we would have traded. */}
                        <span className={`num w-20 text-right ${
                          !row.ever_filled ? "text-muted"
                            : row.realized_pl >= 0 ? "text-long" : "text-short"}`}>
                          {row.ever_filled ? money(row.realized_pl) : ""}</span>
                        <span className="num w-16 text-right text-muted">
                          {size ? `${size.toFixed(2)} lot` : ""}</span>
                      </button>
                      {isOpen && !!(row.legs || []).length && (
                        <div className="px-3 pb-2 space-y-1">
                          {row.legs.map((l, i) => (
                            <div key={i} className="text-[11px] num text-muted flex flex-wrap gap-x-3 pl-14">
                              <span>leg {i + 1}</span>
                              <span>{l.order_type}</span>
                              <span>{Number(l.lot || 0).toFixed(2)} lots</span>
                              <span>in {px(l.entry)}</span>
                              <span>filled {px(l.fill_price)}</span>
                              <span>tp {px(l.tp)}</span>
                              <span>sl {px(l.sl)}{l.sl_moved ? " (moved)" : ""}</span>
                              <span>out {px(l.close_price)}</span>
                              {/* Outcome LABEL only. Leg-level money is not
                                  trustworthy (CLAUDE.md §2.5); the trade's P&L
                                  on the row above is. */}
                              <span className="text-ink">
                                {OUTCOME[l.outcome] || l.outcome || l.status}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="text-[11px] text-muted">
                These are the engine's own records. Pick one you remember and check
                the prices against the chart.
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
