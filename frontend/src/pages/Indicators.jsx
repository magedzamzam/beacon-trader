import { useEffect, useState } from "react";
import { Plus, Trash2, LineChart } from "lucide-react";
import { Card, Badge, Empty } from "../components/ui";
import { Button, Select, ErrorNote } from "../components/form";
import { api } from "../lib/api";

/**
 * Indicators — configure which technical indicators (and their params) are
 * captured per signal, across which timeframes. Reads the backend registry
 * catalog so the set is never hardcoded in the UI; saves to the `ta` setting.
 */

// Category → Badge tone, so each indicator group reads with a consistent color
// cue (#120). Categories come from the backend registry (trend / momentum /
// volatility / volume / structure); unknown ones fall back to muted.
const CAT_TONE = {
  trend: "beacon", momentum: "violet", volatility: "warn",
  volume: "long", structure: "short",
};
const catTone = (c) => CAT_TONE[c] || "muted";
// Left-accent border per tone (mirrors the Badge palette).
const ACCENT = {
  beacon: "border-l-beacon/50", violet: "border-l-violet/50", warn: "border-l-warn/50",
  long: "border-l-long/50", short: "border-l-short/50", muted: "border-l-edge",
};
export default function Indicators() {
  const [cat, setCat] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(false);
  const [addId, setAddId] = useState("");

  useEffect(() => {
    Promise.all([api.taCatalog(), api.taConfig()])
      .then(([c, cf]) => { setCat(c); setCfg({ timeframes: cf.timeframes || [], indicators: cf.indicators || [] }); })
      .catch(e => setErr(e.message));
  }, []);

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!cat || !cfg) return <Card><Empty>Loading…</Empty></Card>;

  const specById = Object.fromEntries(cat.indicators.map(i => [i.id, i]));
  const touch = () => setSaved(false);

  const toggleTf = (tf) => {
    setCfg(c => ({ ...c, timeframes: c.timeframes.includes(tf)
      ? c.timeframes.filter(x => x !== tf) : [...c.timeframes, tf] }));
    touch();
  };
  const addIndicator = () => {
    const spec = specById[addId];
    if (!spec) return;
    const params = {};
    (spec.params || []).forEach(p => { params[p.name] = p.default; });
    setCfg(c => ({ ...c, indicators: [...c.indicators, { id: addId, params }] }));
    setAddId(""); touch();
  };
  const removeIndicator = (idx) =>
    { setCfg(c => ({ ...c, indicators: c.indicators.filter((_, i) => i !== idx) })); touch(); };
  const setParam = (idx, name, val) => {
    setCfg(c => {
      const inds = [...c.indicators];
      inds[idx] = { ...inds[idx], params: { ...inds[idx].params, [name]: val } };
      return { ...c, indicators: inds };
    });
    touch();
  };
  const save = async () => {
    try {
      const res = await api.saveTaConfig(cfg);
      setCfg({ timeframes: res.timeframes || [], indicators: res.indicators || [] });
      setSaved(true);
    } catch (e) { setErr(e.message); }
  };

  // Preserve each indicator's original index (the `cfg.indicators` array position
  // that remove/setParam operate on) before grouping for display, so grouping
  // stays purely visual and the saved payload shape is untouched (#120).
  const rows = cfg.indicators.map((ind, idx) => ({ ind, idx, spec: specById[ind.id] }));
  const groups = [];
  for (const r of rows) {
    const c = r.spec?.category || "other";
    let g = groups.find(x => x.category === c);
    if (!g) { g = { category: c, rows: [] }; groups.push(g); }
    g.rows.push(r);
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2">
            <LineChart className="w-4 h-4 text-beacon" /> Indicators captured per signal
          </div>
          {saved && <span className="text-xs text-long">Saved</span>}
        </div>

        {/* Add + Save toolbar at the top of the card so the primary action is
            reachable without scrolling past a long list (#120). */}
        <div className="px-5 py-3 border-b border-edge bg-panel2/60 flex flex-wrap items-center gap-2">
          <div className="w-64 max-w-full">
            <Select value={addId} onChange={e => setAddId(e.target.value)}>
              <option value="">Add indicator…</option>
              {cat.indicators.map(i => <option key={i.id} value={i.id}>{i.label} · {i.category}</option>)}
            </Select>
          </div>
          <Button onClick={addIndicator} disabled={!addId}><Plus className="w-4 h-4 inline -mt-0.5" /> Add</Button>
          <div className="flex-1" />
          <Button onClick={save}>Save configuration</Button>
        </div>

        <div className="p-5 space-y-5">
          <div className="text-[11px] text-muted max-w-2xl">
            A technical snapshot is recorded for every signal across the timeframes and
            indicators below, for later correlation with trade outcomes. Fully configurable —
            add, tune, or remove anything; nothing is hardcoded.
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">Timeframes</div>
            <div className="flex flex-wrap gap-2">
              {cat.timeframes.map(tf => (
                <button key={tf} onClick={() => toggleTf(tf)}
                  className={`px-3 py-1.5 rounded-lg text-sm border transition ${cfg.timeframes.includes(tf)
                    ? "bg-beacon/15 text-beacon border-beacon/40"
                    : "bg-panel2 text-muted border-edge hover:text-ink"}`}>{tf}</button>
              ))}
            </div>
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">
              Indicators ({cfg.indicators.length})
            </div>
            {!cfg.indicators.length ? (
              <div className="border border-dashed border-edge rounded-lg py-8 text-center">
                <div className="text-sm text-muted">No indicators configured yet.</div>
                <div className="text-xs text-muted mt-1">
                  Use <span className="text-ink font-medium">Add indicator…</span> at the top of this card to capture one per signal.
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                {groups.map(g => {
                  const tone = catTone(g.category);
                  return (
                    <div key={g.category}>
                      <div className="flex items-center gap-2 mb-2">
                        <Badge tone={tone} dot>{g.category}</Badge>
                        <span className="text-[11px] text-muted">{g.rows.length}</span>
                      </div>
                      <div className="space-y-2">
                        {g.rows.map(({ ind, idx, spec }) => (
                          <div key={idx}
                            className={`flex flex-wrap items-center gap-x-3 gap-y-2 border border-edge border-l-2 ${ACCENT[tone]} rounded-lg px-3 py-2 bg-panel2`}>
                            <span className="text-sm font-medium">{spec?.label || ind.id}</span>
                            {(spec?.params || []).map(p => (
                              <label key={p.name} className="flex items-center gap-1.5 text-xs text-muted">
                                {p.name}
                                <input type="number" step={p.type === "float" ? "0.5" : "1"}
                                  min={p.min} max={p.max}
                                  value={ind.params?.[p.name] ?? p.default}
                                  onChange={e => setParam(idx, p.name,
                                    p.type === "float" ? parseFloat(e.target.value) : parseInt(e.target.value, 10))}
                                  className="w-16 bg-panel border border-edge rounded px-2 py-1 text-ink num outline-none focus:border-beacon" />
                              </label>
                            ))}
                            <button onClick={() => removeIndicator(idx)}
                              className="ml-auto text-short hover:bg-short/10 rounded p-1" title="Remove">
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}
