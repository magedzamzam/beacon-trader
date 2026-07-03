import { useEffect, useState } from "react";
import { RefreshCw, Sparkles } from "lucide-react";
import { Card, Th, Td, Badge, Empty } from "../components/ui";
import { Button, Field, Input, Select, Toggle, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const VERDICT_TONE = {
  approve: "long", good: "long", caution: "warn", mixed: "warn",
  reject: "short", bad: "short",
};
const KIND_LABEL = {
  signal_validation: "Signal", execution_review: "Execution", outcome_analysis: "Outcome",
};

export default function AI() {
  const [cfg, setCfg] = useState(null);
  const [assess, setAssess] = useState([]);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  const [apiKey, setApiKey] = useState("");

  const load = async () => {
    try {
      setCfg(await api.aiConfig());
      setAssess(await api.aiAssessments("?limit=100"));
      setErr(null);
    } catch (e) { setErr(e.message); }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setMsg(null);
    try {
      const body = { ...cfg };
      if (apiKey) body.api_key = apiKey;
      const res = await api.saveAiConfig(body);
      setCfg({ ...cfg, ...res });
      setApiKey("");
      setMsg("Saved.");
    } catch (e) { setErr(e.message); }
  };

  if (!cfg) return <Card><Empty>Loading…</Empty></Card>;
  const set = (k, v) => setCfg(c => ({ ...c, [k]: v }));

  return (
    <div className="space-y-6">
      <ErrorNote>{err}</ErrorNote>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2"><Sparkles className="w-4 h-4 text-beacon" /> AI validation</div>
          {msg && <span className="text-xs text-long">{msg}</span>}
        </div>
        <div className="p-5 space-y-4">
          {!cfg.has_secret_key && (
            <div className="text-xs text-warn bg-warn/10 rounded-lg px-3 py-2">
              SECRET_KEY is not set — you can toggle features but cannot store an API key encrypted.
              Set SECRET_KEY (or use ANTHROPIC_API_KEY in the environment) and restart.
            </div>
          )}
          <div className="flex gap-8 flex-wrap">
            <Field label="AI enabled"><Toggle checked={cfg.enabled} onChange={v => set("enabled", v)} label={cfg.enabled ? "on" : "off"} /></Field>
            <Field label="API key set"><Badge tone={cfg.has_api_key ? "long" : "muted"}>{cfg.has_api_key ? "yes" : "no"}</Badge></Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Model" hint="Anthropic model id"><Input value={cfg.model} onChange={e => set("model", e.target.value)} /></Field>
            <Field label="API key" hint={cfg.has_api_key ? "leave blank to keep current key" : "stored encrypted in the DB"}>
              <Input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder="sk-ant-…" />
            </Field>
          </div>
          <div className="flex gap-8 flex-wrap">
            <Field label="Validate signals"><Toggle checked={cfg.validate_signals} onChange={v => set("validate_signals", v)} /></Field>
            <Field label="Review executions"><Toggle checked={cfg.review_execution} onChange={v => set("review_execution", v)} /></Field>
            <Field label="Analyze outcomes"><Toggle checked={cfg.analyze_outcomes} onChange={v => set("analyze_outcomes", v)} /></Field>
          </div>

          <div className="border-t border-edge pt-4">
            <div className="text-xs uppercase tracking-wider text-muted mb-1.5">Signal validation (fast path)</div>
            <div className="text-[11px] text-muted mb-3 max-w-2xl">
              Free-text signals (Telegram / TradingView text) are validated and <b>corrected</b> by a
              fast model before they trade — the local parser can misread levels (e.g. a “(1540 pips)”
              distance read as a take-profit). Manual and structured signals are treated as confirmed
              and skip this. Tune below for replies under 5 seconds; if the model can’t answer in time
              the signal still trades on the parser output but is flagged “unvalidated”.
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Validation model" hint="a fast model for the hot path (e.g. Haiku)">
                <Input value={cfg.validation_model || ""} onChange={e => set("validation_model", e.target.value)} />
              </Field>
              <Field label="Timeout (seconds)" hint="fail-open (flagged) if exceeded">
                <Input type="number" step="0.5" min="1" value={cfg.validation_timeout_seconds ?? 5}
                  onChange={e => set("validation_timeout_seconds", parseFloat(e.target.value) || 5)} />
              </Field>
            </div>
            <div className="flex gap-8 flex-wrap mt-3">
              <Field label="Extended thinking" hint="off = faster replies">
                <Toggle checked={!!cfg.validation_thinking} onChange={v => set("validation_thinking", v)}
                  label={cfg.validation_thinking ? "on" : "off"} />
              </Field>
            </div>
          </div>

          <div className="flex gap-8 flex-wrap items-end">
            <Field label="Gate execution" hint="block trades the AI rejects"><Toggle checked={cfg.gate_execution} onChange={v => set("gate_execution", v)} /></Field>
            <Field label="Min confidence to gate">
              <Input type="number" step="0.05" min="0" max="1" value={cfg.min_confidence}
                onChange={e => set("min_confidence", parseFloat(e.target.value) || 0)} />
            </Field>
          </div>
          <div className="flex justify-end"><Button onClick={save}>Save AI settings</Button></div>
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Recent assessments</div>
          <Button variant="ghost" onClick={load}><RefreshCw className="w-4 h-4" /></Button>
        </div>
        {!assess.length ? <Empty>No AI assessments yet. Enable AI and signals will be judged as they arrive.</Empty> : (
          <table className="w-full">
            <thead><tr className="border-b border-edge">
              <Th>When</Th><Th>Kind</Th><Th right>Ref</Th><Th>Verdict</Th><Th right>Conf</Th><Th right>Score</Th><Th>Rationale</Th>
            </tr></thead>
            <tbody>
              {assess.map(a => (
                <tr key={a.id} className="border-b border-edge/60 align-top">
                  <Td mono>{(a.created_at || "").slice(0, 16).replace("T", " ")}</Td>
                  <Td>{KIND_LABEL[a.kind] || a.kind}</Td>
                  <Td right mono>{a.signal_id ? `sig #${a.signal_id}` : a.trade_id ? `trade #${a.trade_id}` : "—"}</Td>
                  <Td><Badge tone={VERDICT_TONE[a.verdict] || "muted"}>{a.verdict || "—"}</Badge></Td>
                  <Td right mono>{a.confidence != null ? a.confidence.toFixed(2) : "—"}</Td>
                  <Td right mono>{a.score != null ? Math.round(a.score) : "—"}</Td>
                  <Td><div className="max-w-lg text-xs text-muted">{a.rationale}</div></Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
