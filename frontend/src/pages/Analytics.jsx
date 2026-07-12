import { useEffect, useState } from "react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Toggle, Button } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import { api } from "../lib/api";

const REGIME_TONE = { trending: "beacon", ranging: "muted", high_vol: "warn", unknown: "muted" };
const STRUCT_TONE = { bull: "long", bear: "short", range: "muted" };
const TF_ORDER = ["1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m"];
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

/** Shadow analytics sidecar (#51/#53): signal↔channel↔regime correlation.
 *  Read-only observability — nothing here gates trading. */
export default function Analytics() {
  const [rep, setRep] = useState(null);
  const [struct, setStruct] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const range = useRange("all");

  const [map, setMap] = useState(null);
  const [mapBusy, setMapBusy] = useState(false);
  const loadCfg = () => api.analyticsConfig().then(setCfg).catch(e => setErr(e.message));
  const loadMap = () => api.structureMap("XAUUSD").then(setMap).catch(e => setErr(e.message));
  useEffect(() => { loadCfg(); loadMap(); }, []);
  const recompute = async () => {
    setMapBusy(true);
    try { await api.structureRecompute(); await loadMap(); }
    catch (e) { setErr(e.message); } finally { setMapBusy(false); }
  };
  useEffect(() => {
    setRep(null); setStruct(null);
    api.analyticsCorrelation(range.range).then(setRep).catch(e => setErr(e.message));
    api.analyticsStructure(range.range).then(setStruct).catch(e => setErr(e.message));
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

      <StructureMapCard map={map} busy={mapBusy} onRecompute={recompute} />

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

      <StructureCard title="Fair Value Gap — inside vs outside" rows={struct?.fvg} ready={!!struct} />
      <StructureCard title="Order Block — inside vs outside" rows={struct?.ob} ready={!!struct} />
    </div>
  );
}

// Persistent market-structure + Fib magnet map (#61) — Layer A, market-wide per
// symbol. Per-TF bull/bear/range + premium/discount, and the ranked magnet zones.
function StructureMapCard({ map, busy, onRecompute }) {
  const structures = map?.structures || {};
  const tfs = TF_ORDER.filter(t => structures[t]);
  const zones = map?.zones || [];
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-2 flex-wrap">
        <div className="text-sm font-medium">
          Market structure &amp; magnet map · XAUUSD
          {map?.version_id != null && <span className="text-muted font-normal"> · v{map.version_id}</span>}
        </div>
        <Button variant="ghost" onClick={onRecompute} disabled={busy}>
          {busy ? "Recomputing…" : "Recompute"}
        </Button>
      </div>

      {!map ? <Empty>Loading…</Empty>
        : map.version_id == null ? (
          <Empty>No map computed yet — click <b>Recompute</b> (needs an enabled account + a XAUUSD symbol map).</Empty>
        ) : (
        <>
          <div className="px-4 pt-3 text-[11px] text-muted">Structure per timeframe (bull = HH+HL, bear = LH+LL).</div>
          <Table>
            <thead><tr className="border-b border-edge">
              <Th>TF</Th><Th>Structure</Th><Th right>Premium/Discount</Th><Th right>ATR</Th><Th right>Levels</Th>
            </tr></thead>
            <tbody>
              {tfs.map(tf => {
                const s = structures[tf];
                return (
                  <tr key={tf} className="border-b border-edge/60">
                    <Td mono>{tf.toUpperCase()}</Td>
                    <Td><Badge tone={STRUCT_TONE[s.label] || "muted"}>{s.label}</Badge></Td>
                    <Td right mono>{s.premium_discount == null ? "—" : `${fmt(s.premium_discount * 100, 0)}%`}</Td>
                    <Td right mono>{fmt(s.atr, 2)}</Td>
                    <Td right mono>{s.n_levels}</Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>

          <div className="px-4 pt-4 text-[11px] text-muted">
            Magnet zones — cross-timeframe Fib/swing confluence. Score = Σ weight; rank 1 = strongest.
          </div>
          {!zones.length ? <Empty>No zones.</Empty> : (
            <Table>
              <thead><tr className="border-b border-edge">
                <Th right>#</Th><Th>Band</Th><Th right>Mid</Th><Th right>Score</Th><Th right>TFs</Th><Th>Members</Th>
              </tr></thead>
              <tbody>
                {zones.map(z => (
                  <tr key={z.rank} className="border-b border-edge/60">
                    <Td right mono>{z.rank}</Td>
                    <Td mono>{fmt(z.band[0], 2)}–{fmt(z.band[1], 2)}</Td>
                    <Td right mono>{fmt(z.mid, 2)}</Td>
                    <Td right mono>{fmt(z.score, 1)}</Td>
                    <Td right mono>{z.n_timeframes}</Td>
                    <Td><span className="text-[11px] text-muted">
                      {(z.members || []).slice(0, 5).map((m, i) => (
                        <span key={i} className="mr-1.5 whitespace-nowrap">
                          {m.timeframe}:{m.kind === "fib_retracement" ? `fib${m.ratio}`
                            : m.kind === "fib_extension" ? `ext${m.ratio}`
                            : m.kind === "swing_high" ? "SH" : m.kind === "swing_low" ? "SL" : m.kind}
                        </span>
                      ))}
                      {(z.members || []).length > 5 && <span>+{z.members.length - 5}</span>}
                    </span></Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </>
      )}
    </Card>
  );
}

// FVG/OB-vs-outcome cut (#59): win-rate & expectancy inside vs outside the zone,
// overall then per channel/regime, with 90% credible intervals.
function StructureCard({ title, rows, ready }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">{title}</div>
      {!ready ? <Empty>Loading…</Empty>
        : !rows || !rows.length ? <Empty>No labelled structure data yet — accrues as signals capture and trades close.</Empty> : (
        <Table>
          <thead><tr className="border-b border-edge">
            <Th>Scope</Th><Th>Zone</Th><Th right>n</Th><Th right>Win%</Th>
            <Th right>90% CI</Th><Th right>Expectancy</Th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b border-edge/60">
                <Td><span className="text-xs text-muted">{r.scope === "overall" ? "all" : `${r.scope}: ${r.label}`}</span></Td>
                <Td><Badge tone={r.membership === "inside" ? "beacon" : "muted"}>{r.membership}</Badge></Td>
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
  );
}
