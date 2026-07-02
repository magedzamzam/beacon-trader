import { Plus, Trash2 } from "lucide-react";
import { Select, NumberInput, Button } from "./form";

/* rule: {trigger:{type,index|points}, action:{type:'move_sl_to', target, index?, value?}} */
export default function SlRulesEditor({ rules, onChange }) {
  const list = rules || [];
  const update = (i, r) => onChange(list.map((x, j) => (j === i ? r : x)));
  const add = () => onChange([...list, {
    trigger: { type: "tp_hit", index: 1 },
    action: { type: "move_sl_to", target: "entry" },
  }]);
  const remove = i => onChange(list.filter((_, j) => j !== i));

  return (
    <div className="space-y-2">
      {list.length === 0 && <div className="text-xs text-muted">No stop-loss rules. Stops stay where the signal set them.</div>}
      {list.map((r, i) => {
        const trig = r.trigger || {}; const act = r.action || {};
        const setTrig = t => update(i, { ...r, trigger: t });
        const setAct = a => update(i, { ...r, action: { type: "move_sl_to", ...a } });
        return (
          <div key={i} className="flex flex-wrap items-center gap-2 border border-edge rounded-xl p-2.5 bg-panel2">
            <span className="text-xs text-muted">When</span>
            <Select value={trig.type} onChange={e => setTrig(
              e.target.value === "tp_hit" ? { type: "tp_hit", index: trig.index || 1 }
                                          : { type: "price_move", points: trig.points || 3 })}>
              <option value="tp_hit">TP hit</option>
              <option value="price_move">price moves (pts)</option>
            </Select>
            {trig.type === "tp_hit" ? (
              <div className="w-16"><NumberInput value={trig.index ?? 1}
                onChange={e => setTrig({ ...trig, index: +e.target.value })} /></div>
            ) : (
              <div className="w-20"><NumberInput value={trig.points ?? 3}
                onChange={e => setTrig({ ...trig, points: +e.target.value })} /></div>
            )}
            <span className="text-xs text-muted">→ move SL to</span>
            <Select value={act.target} onChange={e => {
              const t = e.target.value;
              if (t === "number") setAct({ target: "number", value: act.value || 0 });
              else if (t === "tp") setAct({ target: "tp", index: act.index || 1 });
              else setAct({ target: t });
            }}>
              <option value="entry">entry</option>
              <option value="previous_tp">previous TP</option>
              <option value="tp">TP #</option>
              <option value="number">number</option>
            </Select>
            {act.target === "tp" && (
              <div className="w-16"><NumberInput value={act.index ?? 1}
                onChange={e => setAct({ ...act, index: +e.target.value })} /></div>
            )}
            {act.target === "number" && (
              <div className="w-24"><NumberInput value={act.value ?? 0}
                onChange={e => setAct({ ...act, value: +e.target.value })} /></div>
            )}
            <Button variant="danger" onClick={() => remove(i)} className="ml-auto"><Trash2 className="w-4 h-4" /></Button>
          </div>
        );
      })}
      <Button variant="ghost" onClick={add}><Plus className="w-4 h-4 inline -mt-0.5" /> Add rule</Button>
      <div className="text-[11px] text-muted">
        Chain them: TP1 hit → entry, TP2 hit → previous TP (TP1), TP3 hit → previous TP (TP2). The engine only ever tightens.
      </div>
    </div>
  );
}
