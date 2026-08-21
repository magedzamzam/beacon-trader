import { Plus, Trash2 } from "lucide-react";
import { Select, NumberInput, Button } from "./form";

// Trigger shapes must mirror beacon_core.strategy.rules._triggered.
const NEW_TRIGGER = {
  tp_hit: (t = {}) => ({ type: "tp_hit", index: t.index || 1 }),
  price_move: (t = {}) => ({ type: "price_move", points: t.points || 3 }),
  be_lock_at_r: (t = {}) => ({ type: "be_lock_at_r", r: t.r ?? 0.6 }),   // #109
};

// #251: `points` is raw INSTRUMENT PRICE, not pips. On gold, 30 is a $30 move —
// the old "pts" label said nothing and the built-in preset read as 3 pips while
// doing something ten times further out. `value_per_point` is money per 1.0 price
// move per 1.0 lot (1.0 for XAUUSD, but that is the instrument's property, not a
// rule), so the money echo is computed from it rather than assumed.
const DEFAULT_UNIT = { symbol: "price", valuePerPoint: 1 };
const moneyNote = (v, u) => {
  const n = Number(v);
  if (v === "" || v == null || !isFinite(n) || n <= 0) return null;
  return `= a ${n.toFixed(2)} move (about $${(n * Number(u.valuePerPoint)).toFixed(2)} per 1.00 lot)`;
};

/* rule: {trigger:{type,index|points|r}, action:{type:'move_sl_to', target, index?, value?}} */
export default function SlRulesEditor({ rules, onChange, unit = DEFAULT_UNIT }) {
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
            <Select value={trig.type}
              onChange={e => setTrig((NEW_TRIGGER[e.target.value] || NEW_TRIGGER.tp_hit)(trig))}>
              <option value="tp_hit">TP hit</option>
              <option value="price_move">price moves ({unit.symbol} price)</option>
              <option value="be_lock_at_r">profit reaches (× R)</option>
            </Select>
            {trig.type === "tp_hit" && (
              <div className="w-16"><NumberInput value={trig.index ?? 1}
                onChange={e => setTrig({ ...trig, index: +e.target.value })} /></div>
            )}
            {trig.type === "price_move" && (<>
              <div className="w-20"><NumberInput value={trig.points ?? 3}
                onChange={e => setTrig({ ...trig, points: +e.target.value })} /></div>
              {/* #251: echo the number back in money before it is saved. */}
              <span className="text-[11px] text-muted">{moneyNote(trig.points ?? 3, unit)}</span>
            </>)}
            {trig.type === "be_lock_at_r" && (
              <div className="w-20"><NumberInput step="0.05" value={trig.r ?? 0.6}
                onChange={e => setTrig({ ...trig, r: +e.target.value })} /></div>
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
            {act.target === "number" && (<>
              <div className="w-24"><NumberInput value={act.value ?? 0}
                onChange={e => setAct({ ...act, value: +e.target.value })} /></div>
              {/* an absolute PRICE, not a distance — the other unit on this row */}
              <span className="text-[11px] text-muted">{unit.symbol} price</span>
            </>)}
            <Button variant="danger" onClick={() => remove(i)} className="ml-auto"><Trash2 className="w-4 h-4" /></Button>
          </div>
        );
      })}
      <Button variant="ghost" onClick={add}><Plus className="w-4 h-4 inline -mt-0.5" /> Add rule</Button>
      <div className="text-[11px] text-muted">
        Chain them: TP1 hit → entry, TP2 hit → previous TP (TP1)… The engine only ever tightens.
        <b> × R</b> = move once profit reaches that multiple of the initial risk (|entry − SL|), so it
        self-adapts to each signal's stop — e.g. <i>profit reaches 0.6 × R → entry</i> for an early break-even.
        <br />
        <b>price moves</b> is in {unit.symbol} <b>price units, not pips</b> — on gold <span className="num">30</span> means
        a <span className="num">$30.00</span> move, roughly 2.5× a typical channel stop. Use <b>× R</b> when you want
        something that scales with the signal's own stop.
      </div>
    </div>
  );
}
