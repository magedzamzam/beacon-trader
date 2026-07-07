import { useMemo, useState } from "react";
import { Card, KPI, Th, Td, Badge, Empty } from "../components/ui";
import { api } from "../lib/api";
import { useData, money, tone } from "./_useData";

const PRESETS = [
  ["all", "All time"], ["today", "Today"], ["7d", "Last 7 days"],
  ["this_week", "This week"], ["last_week", "Last week"],
  ["this_month", "This month"], ["last_month", "Last month"],
  ["this_quarter", "This quarter"], ["last_quarter", "Last quarter"],
  ["this_year", "This year"], ["custom", "Custom"],
];

// Return [from, to) as Date objects (or null = unbounded), anchored on the
// leg CLOSE time. Uses local-time day/week/month boundaries; toISOString()
// preserves the exact instant for the API (which compares in UTC).
function rangeFor(id, custom) {
  const now = new Date();
  const sod = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
  const addD = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const sow = (d) => addD(sod(d), -((new Date(d).getDay() + 6) % 7));   // Monday
  const som = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
  const soq = (d) => new Date(d.getFullYear(), Math.floor(d.getMonth() / 3) * 3, 1);
  const soy = (d) => new Date(d.getFullYear(), 0, 1);
  switch (id) {
    case "today": return [sod(now), now];
    case "7d": return [addD(now, -7), now];
    case "this_week": return [sow(now), now];
    case "last_week": return [addD(sow(now), -7), sow(now)];
    case "this_month": return [som(now), now];
    case "last_month": { const s = som(now); return [new Date(s.getFullYear(), s.getMonth() - 1, 1), s]; }
    case "this_quarter": return [soq(now), now];
    case "last_quarter": { const s = soq(now); return [new Date(s.getFullYear(), s.getMonth() - 3, 1), s]; }
    case "this_year": return [soy(now), now];
    case "custom": {
      const f = custom.from ? new Date(custom.from + "T00:00:00") : null;
      const t = custom.to ? addD(new Date(custom.to + "T00:00:00"), 1) : null;  // inclusive end day
      return [f, t];
    }
    default: return [null, null];   // all time
  }
}

export default function Performance({ account = "" }) {
  const [preset, setPreset] = useState("all");
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [from, to] = useMemo(() => rangeFor(preset, custom), [preset, custom]);
  const fromIso = from ? from.toISOString() : "";
  const toIso = to ? to.toISOString() : "";
  const range = { from: fromIso, to: toIso };

  const { data: sum } = useData(() => api.perfSummary(account, range), [account, fromIso, toIso]);
  const { data: bySrc } = useData(() => api.perfBySource(account, range), [account, fromIso, toIso]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-1.5">
        {PRESETS.map(([id, label]) => (
          <button key={id} onClick={() => setPreset(id)}
            className={`px-2.5 py-1 rounded-lg text-xs transition
              ${preset === id ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
            {label}
          </button>
        ))}
        {preset === "custom" && (
          <div className="flex items-center gap-1.5 ml-1">
            <input type="date" value={custom.from}
              onChange={e => setCustom(c => ({ ...c, from: e.target.value }))}
              className="bg-panel2 border border-edge rounded-lg px-2 py-1 text-xs num outline-none focus:border-beacon" />
            <span className="text-muted text-xs">→</span>
            <input type="date" value={custom.to}
              onChange={e => setCustom(c => ({ ...c, to: e.target.value }))}
              className="bg-panel2 border border-edge rounded-lg px-2 py-1 text-xs num outline-none focus:border-beacon" />
          </div>
        )}
      </div>

      {!sum ? <Card><Empty>Loading…</Empty></Card> : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KPI label="Realized P&L" value={money(sum.total_pl)} tone={tone(sum.total_pl)} />
            <KPI label="Win rate" value={`${sum.win_rate}%`} tone="beacon" sub={`${sum.wins}W / ${sum.losses}L`} />
            <KPI label="Profit factor" value={sum.profit_factor ?? "—"} sub="gross win / loss" />
            <KPI label="Closed legs" value={sum.closed_legs} />
          </div>

          <Card>
            <div className="px-4 py-3 border-b border-edge">
              <div className="text-sm font-medium">By source — which channel actually reaches TP</div>
              <div className="text-[11px] text-muted mt-0.5">
                Win rate shows a 90% credible interval. Sources below the significance threshold
                are dimmed and tagged — read their verdict as provisional, not proven.
              </div>
            </div>
            {!bySrc || !bySrc.length ? <Empty>No closed legs in this range.</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px]">
                  <thead><tr className="border-b border-edge">
                    <Th>Source</Th><Th>Sample</Th><Th right>Win %</Th><Th right>P&L</Th>
                    <Th right>TP1</Th><Th right>TP2</Th><Th right>TP3+</Th><Th right>SL hits</Th>
                  </tr></thead>
                  <tbody>
                    {bySrc.map(s => {
                      const tp3plus = Object.entries(s.tp_hits).filter(([k]) => +k >= 3)
                        .reduce((a, [, v]) => a + v, 0);
                      return (
                        <tr key={s.source_id}
                          className={`border-b border-edge/60 ${s.significant ? "" : "opacity-60"}`}>
                          <Td>{s.name}</Td>
                          <Td>
                            <span className="num text-xs">{s.n_trades}</span>
                            {!s.significant && <span className="text-[10px] text-muted num">/{s.min_trades}</span>}
                            {s.significant
                              ? <Badge tone="beacon">significant</Badge>
                              : <Badge tone="warn">low-N</Badge>}
                          </Td>
                          <Td right mono>
                            {s.win_rate != null ? `${s.win_rate}%` : "—"}
                            {s.ci && <span className="block text-[10px] text-muted">CI {s.ci.low}–{s.ci.high}%</span>}
                          </Td>
                          <Td right mono><span className={`text-${tone(s.pl)}`}>{money(s.pl)}</span></Td>
                          <Td right mono>{s.tp_hits[1] || 0}</Td>
                          <Td right mono>{s.tp_hits[2] || 0}</Td>
                          <Td right mono>{tp3plus}</Td>
                          <Td right mono><span className="text-short">{s.sl_hits}</span></Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
