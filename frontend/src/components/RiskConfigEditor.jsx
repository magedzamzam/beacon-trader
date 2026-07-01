import { Field, Select, NumberInput } from "./form";

/* value shape (matches beacon_core.risk.sizing.RiskConfig):
   { basis, value, allocation, per_tp_percent: {1:..,2:..,3:..} } */
export default function RiskConfigEditor({ value, onChange }) {
  const v = value || {};
  const set = (k, val) => onChange({ ...v, [k]: val });
  const per = v.per_tp_percent || {};
  const setPer = (i, val) => onChange({ ...v, per_tp_percent: { ...per, [i]: val } });
  const allocation = v.allocation || "even";

  return (
    <div className="space-y-3 border border-edge rounded-lg p-3 bg-panel2">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Basis" hint="how the per-signal budget is set">
          <Select value={v.basis || "capital_percent"} onChange={e => set("basis", e.target.value)}>
            <option value="capital_percent">% of equity</option>
            <option value="fixed_cash">fixed cash</option>
          </Select>
        </Field>
        <Field label={v.basis === "fixed_cash" ? "Cash ($)" : "Percent (%)"}>
          <NumberInput value={v.value ?? ""} onChange={e => set("value", e.target.value)} />
        </Field>
      </div>
      <Field label="Allocation" hint="how the budget spreads across legs">
        <Select value={allocation} onChange={e => set("allocation", e.target.value)}>
          <option value="even">even — budget / N legs</option>
          <option value="per_tp">per-TP — each leg risks its own %</option>
        </Select>
      </Field>
      {allocation === "per_tp" && (
        <div>
          <div className="text-xs uppercase tracking-wider text-muted mb-1.5">Per-TP risk (% of equity)</div>
          <div className="grid grid-cols-4 gap-2">
            {[1, 2, 3, 4, 5, 6, 7, 8].map(i => (
              <div key={i}>
                <div className="text-[10px] text-muted mb-1">TP{i}</div>
                <NumberInput placeholder="—" value={per[i] ?? ""} onChange={e => setPer(i, e.target.value)} />
              </div>
            ))}
          </div>
          <div className="text-[11px] text-warn mt-2">
            Note: with range entries, each entry leg takes the full per-TP risk — exposure multiplies.
          </div>
        </div>
      )}
    </div>
  );
}
