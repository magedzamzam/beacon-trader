import { useState } from "react";
import { DollarSign } from "lucide-react";
import { Card } from "../../components/ui";
import { Field, Select, Toggle, Button } from "../../components/form";

/**
 * Currency & FX settings.
 *
 * NOTE: There is no backend endpoint for platform-wide currency preferences yet,
 * so these are persisted to localStorage as a working preview. When the backend
 * gains a /settings/currency endpoint (see docs/CONFIGURATION.md), swap the
 * load()/save() helpers for api calls. Per-account trading currency continues to
 * live on each account (Brokers & Accounts tab) — this controls how figures are
 * *reported and displayed* across the dashboard.
 */
const KEY = "beacon_currency_prefs";
const CURRENCIES = ["USD", "EUR", "GBP", "CHF", "JPY", "AUD", "CAD", "NZD", "AED", "SAR", "SGD", "HKD", "ZAR"];
const DEFAULTS = {
  base: "USD", display: "USD", position: "prefix",
  grouping: true, fxSource: "broker", autoConvert: true,
};

function load() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; }
  catch { return { ...DEFAULTS }; }
}

export default function Currency() {
  const [prefs, setPrefs] = useState(load);
  const [saved, setSaved] = useState(false);
  const set = (k, v) => { setPrefs(p => ({ ...p, [k]: v })); setSaved(false); };
  const save = () => { localStorage.setItem(KEY, JSON.stringify(prefs)); setSaved(true); };

  return (
    <div className="space-y-6">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2">
            <DollarSign className="w-4 h-4 text-beacon" /> Currency &amp; FX
          </div>
          {saved && <span className="text-xs text-long">Saved locally</span>}
        </div>

        <div className="p-5 space-y-5">
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Base / reporting currency" hint="P&L and equity are normalized to this currency">
              <Select value={prefs.base} onChange={e => set("base", e.target.value)}>
                {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
              </Select>
            </Field>
            <Field label="Display currency" hint="What figures render in across the UI">
              <Select value={prefs.display} onChange={e => set("display", e.target.value)}>
                {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
              </Select>
            </Field>
          </div>

          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Symbol position">
              <Select value={prefs.position} onChange={e => set("position", e.target.value)}>
                <option value="prefix">Prefix — $1,000.00</option>
                <option value="suffix">Suffix — 1,000.00 $</option>
                <option value="code">Code — 1,000.00 USD</option>
              </Select>
            </Field>
            <Field label="FX rate source" hint="Where cross-currency conversion rates come from">
              <Select value={prefs.fxSource} onChange={e => set("fxSource", e.target.value)}>
                <option value="broker">Broker quotes</option>
                <option value="ecb">ECB reference (daily)</option>
                <option value="manual">Manual override</option>
              </Select>
            </Field>
          </div>

          <div className="flex flex-wrap gap-8">
            <Field label="Thousands grouping">
              <Toggle checked={prefs.grouping} onChange={v => set("grouping", v)}
                label={prefs.grouping ? "1,000.00" : "1000.00"} />
            </Field>
            <Field label="Auto-convert accounts" hint="Convert non-base accounts into base for totals">
              <Toggle checked={prefs.autoConvert} onChange={v => set("autoConvert", v)}
                label={prefs.autoConvert ? "on" : "off"} />
            </Field>
          </div>

          <div className="text-[11px] text-warn bg-warn/10 rounded-lg px-3 py-2">
            Preferences are saved in this browser only. Backend persistence and live FX
            conversion are pending — see <span className="num">docs/CONFIGURATION.md</span>.
          </div>

          <div className="flex justify-end">
            <Button onClick={save}>Save currency settings</Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
