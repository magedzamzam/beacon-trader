import { Plus, Trash2, RotateCcw } from "lucide-react";
import { Select, NumberInput, Button } from "./form";

/**
 * LadderEditor (#250) — the staged entry, as a table you can read.
 *
 *   IF                  THEN     ORDER      LEVEL        TARGET
 *   signal arrives      open     POSITION   ENTRY-FROM   TP1
 *   price reaches MID   open     POSITION   MID          TP2
 *   price reaches MID   open     STOP       ENTRY-FROM   TP3
 *   price reaches TP1   cancel   —          —            —
 *
 * Every row is one order: when to place it, what kind, at which level, for which
 * target. This replaces the thirteen tuning numbers staged entry used to carry,
 * none of which anyone ever changed.
 *
 * Values mirror beacon_core.execution.ladder — keep the four option lists below
 * in step with WHENS / ACTIONS / ORDERS / LEVELS there. A value this offers that
 * the module does not know is a 422 on save, not a silent drop: a ladder missing
 * a rung is a different strategy from the one that was saved.
 */
const WHENS = [
  ["signal", "signal arrives"],
  ["mid", "price reaches MID"],
  ["tp1", "price reaches TP1"],
];
const ACTIONS = [["open", "open"], ["cancel_all", "cancel everything else"]];
const ORDERS = [
  ["POSITION", "POSITION — take it now"],
  ["LIMIT", "LIMIT — rest better than market"],
  ["STOP", "STOP — fills on continuation"],
];
const LEVELS = [
  ["ENTRY_FROM", "ENTRY-FROM"],
  ["ENTRY_TO", "ENTRY-TO"],
  ["MID", "MID"],
];

export const DEFAULT_LADDER = [
  { when: "signal", action: "open", order: "POSITION", level: "ENTRY_FROM", target: 1 },
  { when: "mid", action: "open", order: "POSITION", level: "MID", target: 2 },
  { when: "mid", action: "open", order: "STOP", level: "ENTRY_FROM", target: 3 },
  { when: "tp1", action: "cancel_all" },
];

const label = (list, v) => (list.find(([k]) => k === v) || [, v])[1];

/** The row in plain language, so the table can be checked without decoding it. */
function readback(r) {
  if (r.action === "cancel_all") return `When ${label(WHENS, r.when)}, cancel every other order.`;
  return `When ${label(WHENS, r.when)}, open a ${r.order} at ${label(LEVELS, r.level)} targeting TP${r.target}.`;
}

export default function LadderEditor({ rows, onChange }) {
  const list = rows && rows.length ? rows : DEFAULT_LADDER;
  const update = (i, patch) => onChange(list.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i) => onChange(list.filter((_, j) => j !== i));
  const add = () => onChange([...list, { when: "mid", action: "open", order: "POSITION", level: "MID", target: 1 }]);

  const setAction = (i, action) =>
    update(i, action === "cancel_all"
      ? { action, order: undefined, level: undefined, target: undefined }
      : { action, order: "POSITION", level: "MID", target: 1 });

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full text-xs" style={{ minWidth: 620 }}>
          <thead>
            <tr className="text-muted uppercase tracking-wider text-[10px]">
              <th className="text-left font-medium pb-1.5 pr-2">If</th>
              <th className="text-left font-medium pb-1.5 pr-2">Then</th>
              <th className="text-left font-medium pb-1.5 pr-2">Order</th>
              <th className="text-left font-medium pb-1.5 pr-2">Level</th>
              <th className="text-left font-medium pb-1.5 pr-2">Target</th>
              <th className="pb-1.5"></th>
            </tr>
          </thead>
          <tbody>
            {list.map((r, i) => (
              <tr key={i} className="border-t border-edge/60">
                <td className="py-1.5 pr-2">
                  <Select value={r.when} onChange={(e) => update(i, { when: e.target.value })}>
                    {WHENS.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
                  </Select>
                </td>
                <td className="py-1.5 pr-2">
                  <Select value={r.action} onChange={(e) => setAction(i, e.target.value)}>
                    {ACTIONS.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
                  </Select>
                </td>
                {r.action === "cancel_all" ? (
                  <td className="py-1.5 pr-2 text-muted" colSpan={3}>— everything else is pulled —</td>
                ) : (
                  <>
                    <td className="py-1.5 pr-2">
                      <Select value={r.order} onChange={(e) => update(i, { order: e.target.value })}>
                        {ORDERS.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
                      </Select>
                    </td>
                    <td className="py-1.5 pr-2">
                      <Select value={r.level} onChange={(e) => update(i, { level: e.target.value })}>
                        {LEVELS.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
                      </Select>
                    </td>
                    <td className="py-1.5 pr-2">
                      <div className="w-16">
                        <NumberInput min="1" value={r.target ?? 1}
                          onChange={(e) => update(i, { target: +e.target.value })} />
                      </div>
                    </td>
                  </>
                )}
                <td className="py-1.5 text-right">
                  <Button variant="danger" onClick={() => remove(i)}><Trash2 className="w-3.5 h-3.5" /></Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2">
        <Button variant="ghost" onClick={add}><Plus className="w-4 h-4 inline -mt-0.5" /> Add row</Button>
        <Button variant="ghost" onClick={() => onChange(DEFAULT_LADDER.map((r) => ({ ...r })))}>
          <RotateCcw className="w-3.5 h-3.5 inline -mt-0.5" /> Reset to default ladder
        </Button>
      </div>

      <div className="rounded-lg border border-edge bg-panel2/40 p-2.5 space-y-1">
        {list.map((r, i) => <div key={i} className="text-[11px] text-muted">{readback(r)}</div>)}
      </div>

      <div className="text-[11px] text-muted">
        <b>MID</b> is halfway from the far entry edge to the stop. On a single-level signal
        ENTRY-TO <i>is</i> ENTRY-FROM, so one table covers both shapes. A row targeting a TP the
        signal does not have is simply not created — never an error, never a nearer TP instead.
        <br />
        <b className="text-fg">Total risk is unchanged.</b> Every rung is sized before the first
        order goes out, against the risk the ordinary single-shot entry would have taken on the
        same signal — so if every rung fills and price runs to the stop, the loss is the same
        money. A rung nearer the stop just carries a bigger lot for it.
      </div>
    </div>
  );
}
