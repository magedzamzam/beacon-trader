import { useEffect, useState } from "react";
import { FlaskConical, ArrowRight } from "lucide-react";
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

const TRAVEL_LABEL = {
  straight_to_sl: "straight to SL",
  ranged: "ranged, no direction",
  went_our_way_then_reversed: "went +1R then reversed",
  ran_to_target: "ran to target",
  unknown: "unknown",
};

const FILTERS = [
  { kind: "rsi_below", label: "Only take signals with RSI below", value: 70, num: true },
  { kind: "rsi_above", label: "Only take signals with RSI above", value: 30, num: true },
  { kind: "min_stop_atr", label: "Skip signals whose stop is under N× ATR", value: 1.0, num: true },
  { kind: "only_trending", label: "Only trade when the market is trending" },
  { kind: "only_ranging", label: "Only trade when the market is ranging" },
  { kind: "skip_session", label: "Skip a session", sessions: ["New York"] },
];

const EXITS = [
  { v: "", label: "leave the exit as it is" },
  { v: "be_at_tp1", label: "move stop to breakeven at TP1" },
  { v: "be_at_tp2", label: "move stop to breakeven at TP2" },
  { v: "let_it_run", label: "never move the stop" },
];

export default function Replay() {
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [runs, setRuns] = useState([]);
  const [openRun, setOpenRun] = useState(null);
  const [report, setReport] = useState(null);
  const [err, setErr] = useState(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    api.sources().then(setSources).catch(() => {});
    api.accounts().then(setAccounts).catch(() => {});
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
      <NewBacktest sources={sources} accounts={accounts}
        onQueued={() => setRefresh(n => n + 1)} />
      {err && <ErrorNote>{err}</ErrorNote>}
      <History jobs={jobs} runs={runs} openRun={openRun} onOpen={setOpenRun} />
      {report && <Report r={report} />}
    </div>
  );
}

// --- step 1-4: the form -------------------------------------------------------
function NewBacktest({ sources, accounts, onQueued }) {
  const [scopeType, setScopeType] = useState("source");
  const [sourceId, setSourceId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [from, setFrom] = useState("2026-07-05");
  const [to, setTo] = useState("2026-07-30");
  const [picked, setPicked] = useState({});          // kind -> value
  const [exit, setExit] = useState("");
  const [risk, setRisk] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const toggle = (f) => setPicked(p => {
    const n = { ...p };
    if (f.kind in n) delete n[f.kind];
    else n[f.kind] = f.value ?? true;
    return n;
  });

  const run = async () => {
    setBusy(true); setMsg(null); setErr(null);
    try {
      const filters = Object.entries(picked).map(([kind, value]) => {
        const def = FILTERS.find(f => f.kind === kind);
        if (kind === "skip_session") return { kind, sessions: def.sessions };
        return def?.num ? { kind, value: Number(value) } : { kind };
      });
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
          exit: exit || undefined,
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
  const nothingChanged = !Object.keys(picked).length && !exit && !risk;

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

        <Step n="3" title="What would we do differently?">
          <div className="space-y-2">
            {FILTERS.map(f => (
              <label key={f.kind} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={f.kind in picked}
                  onChange={() => toggle(f)} className="accent-beacon" />
                <span>{f.label}</span>
                {f.num && f.kind in picked && (
                  <input type="number" step="0.1" value={picked[f.kind]}
                    onChange={e => setPicked(p => ({ ...p, [f.kind]: e.target.value }))}
                    className="w-20 bg-panel2 border border-edge rounded px-2 py-0.5 num text-xs" />
                )}
              </label>
            ))}
            <div className="flex flex-wrap gap-3 pt-1">
              <Field label="Exit">
                <Select value={exit} onChange={e => setExit(e.target.value)}>
                  {EXITS.map(o => <option key={o.v} value={o.v}>{o.label}</option>)}
                </Select>
              </Field>
              <Field label="Risk per trade (%)" hint="blank = leave it as it is">
                <NumberInput value={risk} step="0.25" placeholder="unchanged"
                  onChange={e => setRisk(e.target.value)} />
              </Field>
            </div>
          </div>
        </Step>
      </div>

      <div className="px-4 py-3 border-t border-edge flex items-center gap-3 flex-wrap">
        <Button onClick={run} disabled={busy || !ready || nothingChanged}>
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

function History({ jobs, runs, openRun, onOpen }) {
  const running = jobs.filter(j => j.status === "queued" || j.status === "running");
  const done = runs.filter(r => r.label && r.label.startsWith("what-if"));
  if (!running.length && !done.length) return null;
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">History</div>
      {running.map(j => (
        <div key={j.id} className="px-4 py-2 border-b border-edge/60 text-[11px] flex items-center gap-2">
          <Badge tone={JOB_TONE[j.status]}>{j.status}</Badge>
          <span>{j.label}</span>
          <span className="text-muted">{j.progress || "…"}</span>
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

function Report({ r }) {
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
      <div className="px-4 py-2 text-[11px] text-muted border-t border-edge">{r.note}</div>
    </Card>
  );
}
