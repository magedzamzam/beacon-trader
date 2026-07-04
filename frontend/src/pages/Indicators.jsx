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

  return (
    <div className="space-y-4">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2">
            <LineChart className="w-4 h-4 text-beacon" /> Indicators captured per signal
          </div>
          {saved && <span className="text-xs text-long">Saved</span>}
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
            {!cfg.indicators.length ? <div className="text-xs text-muted">None yet — add one below.</div> : (
              <div className="space-y-2">
                {cfg.indicators.map((ind, idx) => {
                  const spec = specById[ind.id];
                  return (
                    <div key={idx} className="flex flex-wrap items-center gap-x-3 gap-y-2 border border-edge rounded-lg px-3 py-2 bg-panel2">
                      <Badge>{spec?.category || "?"}</Badge>
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
                  );
                })}
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="w-64">
              <Select value={addId} onChange={e => setAddId(e.target.value)}>
                <option value="">Add indicator…</option>
                {cat.indicators.map(i => <option key={i.id} value={i.id}>{i.label} · {i.category}</option>)}
              </Select>
            </div>
            <Button onClick={addIndicator} disabled={!addId}><Plus className="w-4 h-4 inline -mt-0.5" /> Add</Button>
            <div className="flex-1" />
            <Button onClick={save}>Save configuration</Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
