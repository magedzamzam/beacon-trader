import { useEffect, useState } from "react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Toggle } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import { api } from "../lib/api";

const REGIME_TONE = { trending: "beacon", ranging: "muted", high_vol: "warn", unknown: "muted" };
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

/** Shadow analytics sidecar (#51/#53): signal↔channel↔regime correlation.
 *  Read-only observability — nothing here gates trading. */
export default function Analytics() {
  const [rep, setRep] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const range = useRange("all");

  const loadCfg = () => api.analyticsConfig().then(setCfg).catch(e => setErr(e.message));
  useEffect(() => { loadCfg(); }, []);
  useEffect(() => {
    setRep(null);
    api.analyticsCorrelation(range.range).then(setRep).catch(e => setErr(e.message));
  }, [range.fromIso, range.toIso]);

  const toggle = async (v) => {
    try { const c = { ...cfg, enabled: v }; setCfg(c); await api.saveAnalyticsConfig(c); }
    catch (e) { setErr(e.message); }
  };

  return (
    <div className="space-y-5">
      {err && <div className="text-sm text-short">{err}</div>}

      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center justify-between">
          <div className="text-sm font-medium">Shadow analytics sidecar</div>
          {cfg && (
            <label className="flex items-center gap-2 text-xs text-muted">
              {cfg.enabled ? "capturing" : "off"}
              <Toggle checked={!!cfg.enabled} onChange={toggle} />
            </label>
          )}
        </div>
        <div className="px-4 py-2 text-[11px] text-muted">
          Per-signal regime · Hurst · Kalman slope · VWAP-z · k-NN, computed side-by-side
          with live trading and <b>never gating it</b>. Win-rates use Beta-Binomial credible
          intervals (small samples shrink toward the {rep ? `${fmt(rep.base_rate * 100, 1)}%` : "base"} rate).
        </div>
      </Card>

      <RangeFilter state={range} variant="coarse" />

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">
          Channel × regime performance {rep && <span className="text-muted font-normal">· {rep.n_labelled} labelled</span>}
        </div>
        {!rep ? <Empty>Loading…</Empty>
          : !rep.by_channel_regime?.length ? <Empty>No labelled analytics yet — accrues as signals capture and trades close.</Empty> : (
          <Table>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th>Regime</Th><Th right>n</Th><Th right>Win%</Th>
              <Th right>90% CI</Th><Th right>Expectancy</Th>
            </tr></thead>
            <tbody>
              {rep.by_channel_regime.map((r, i) => (
                <tr key={i} className="border-b border-edge/60">
                  <Td>{r.channel}</Td>
                  <Td><Badge tone={REGIME_TONE[r.regime] || "muted"}>{r.regime}</Badge></Td>
                  <Td right mono>{r.n}</Td>
                  <Td right mono>{fmt(r.win_rate * 100, 0)}%</Td>
                  <Td right mono>{fmt(r.ci_low * 100, 0)}–{fmt(r.ci_high * 100, 0)}%</Td>
                  <Td right mono><span className={r.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(r.expectancy)}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {rep?.regime_mix_by_channel && Object.keys(rep.regime_mix_by_channel).length > 0 && (
        <Card>
          <div className="px-4 py-3 border-b border-edge text-sm font-medium">Regime mix by channel</div>
          <Table>
            <thead><tr className="border-b border-edge"><Th>Channel</Th><Th>Regimes (signal count)</Th></tr></thead>
            <tbody>
              {Object.entries(rep.regime_mix_by_channel).map(([chan, mix]) => (
                <tr key={chan} className="border-b border-edge/60">
                  <Td>{chan}</Td>
                  <Td><span className="flex flex-wrap gap-1.5">
                    {Object.entries(mix).map(([reg, n]) => (
                      <Badge key={reg} tone={REGIME_TONE[reg] || "muted"}>{reg}: {n}</Badge>
                    ))}
                  </span></Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}
