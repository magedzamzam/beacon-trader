import { useEffect, useState } from "react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Toggle, Button } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
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

  const [trend, setTrend] = useState(null);
  const [map, setMap] = useState(null);
  const [price, setPrice] = useState(null);
  const [mapBusy, setMapBusy] = useState(false);
  const loadCfg = () => api.analyticsConfig().then(setCfg).catch(e => setErr(e.message));
  const loadMap = () => api.structureMap("XAUUSD").then(setMap).catch(e => setErr(e.message));
  const loadPrice = () => api.quote("XAUUSD")
    .then(q => setPrice(q.last ?? (q.bid != null && q.offer != null ? (q.bid + q.offer) / 2 : null)))
    .catch(() => setPrice(null));   // market closed / broker down -> ladder still renders
  useEffect(() => { loadCfg(); loadMap(); loadPrice(); }, []);
  const recompute = async () => {
    setMapBusy(true);
    try { await api.structureRecompute(); await loadMap(); await loadPrice(); }
    catch (e) { setErr(e.message); } finally { setMapBusy(false); }
  };
  useEffect(() => {
    setRep(null); setStruct(null); setTrend(null);
    api.analyticsCorrelation(range.range).then(setRep).catch(e => setErr(e.message));
    api.analyticsStructure(range.range).then(setStruct).catch(e => setErr(e.message));
    api.analyticsTrendAlignment(range.range).then(setTrend).catch(e => setErr(e.message));
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
          Per-signal regime<HelpHint term="regime" /> · Hurst<HelpHint term="hurst" /> ·
          Kalman slope<HelpHint term="kalman_slope" /> · VWAP-z<HelpHint term="vwap_z" /> ·
          k-NN<HelpHint term="knn" />, computed side-by-side with live trading and
          <b>never gating it</b>. Win-rates use Beta-Binomial credible intervals
          (small samples shrink toward the {rep ? `${fmt(rep.base_rate * 100, 1)}%` : "base"} rate).
        </div>
      </Card>

      <StructureMapCard map={map} price={price} busy={mapBusy} onRecompute={recompute} />

      <RangeFilter state={range} variant="coarse" />

      <TrendAlignmentCard trend={trend} />

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium">
          Channel × regime performance {rep && <span className="text-muted font-normal">· {rep.n_labelled} labelled</span>}
        </div>
        {!rep ? <Empty>Loading…</Empty>
          : !rep.by_channel_regime?.length ? <Empty>No labelled analytics yet — accrues as signals capture and trades close.</Empty> : (
          <Table minW={860}>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th>Regime<HelpHint term="regime" /></Th><Th right>n</Th><Th right>Win%</Th>
              <Th right>90% CI<HelpHint term="credible_interval" /></Th>
              <Th right>Expectancy<HelpHint term="expectancy" /></Th>
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

// Trend-alignment vs outcome (#72): the aligned-vs-counter split the #48 filter
// gates on, as a first-class metric. 'counter' is the population the enabled
// filter skips/de-sizes — this card is how we watch that the edge holds.
function TrendAlignmentCard({ trend }) {
  const ORDER = ["aligned", "counter"];
  const rows = trend ? ORDER.filter(k => trend.overall?.[k]) : [];
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
        Trend alignment vs outcome
        {trend && <span className="text-muted font-normal">
          · {trend.timeframe?.toUpperCase()} EMA{trend.ema_period} · {trend.n_labelled} labelled
          {trend.n_unknown_trend ? ` · ${trend.n_unknown_trend} trend-unknown` : ""}</span>}
      </div>
      {!trend ? <Empty>Loading…</Empty>
        : !rows.length ? <Empty>No labelled trades with a captured {trend.timeframe?.toUpperCase()} trend yet — accrues as signals capture and trades close.</Empty> : (
        <Table minW={720}>
          <thead><tr className="border-b border-edge">
            <Th>Alignment</Th><Th right>n</Th><Th right>Win%</Th>
            <Th right>90% CI</Th><Th right>Net</Th><Th right>Expectancy</Th>
          </tr></thead>
          <tbody>
            {rows.map(k => {
              const r = trend.overall[k];
              return (
                <tr key={k} className="border-b border-edge/60">
                  <Td><Badge tone={k === "aligned" ? "long" : "short"}>{k}</Badge></Td>
                  <Td right mono>{r.n}</Td>
                  <Td right mono>{fmt(r.win_rate * 100, 0)}%</Td>
                  <Td right mono>{fmt(r.ci_low * 100, 0)}–{fmt(r.ci_high * 100, 0)}%</Td>
                  <Td right mono><span className={r.net >= 0 ? "text-long" : "text-short"}>{fmt(r.net)}</span></Td>
                  <Td right mono><span className={r.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(r.expectancy)}</span></Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
      <div className="px-4 py-2 text-[11px] text-muted">
        Counter-trend = entry fighting the higher-TF trend; the enabled #48 filter skips or de-sizes these.
        Shadow metric — the filter itself acts at placement. Small samples shrink toward the base rate.
      </div>
    </Card>
  );
}

// Persistent market-structure + Fib magnet map (#61) — a decision-oriented view:
// a one-glance multi-TF bias strip + a levels ladder of the STRONGEST magnet
// zones above/below the live price (not a dump of every zone).
const TOP_ZONES = 8;

function StructureMapCard({ map, price, busy, onRecompute }) {
  const structures = map?.structures || {};
  const tfs = TF_ORDER.filter(t => structures[t]);
  const counts = { bull: 0, bear: 0, range: 0 };
  tfs.forEach(t => { counts[structures[t].label] = (counts[structures[t].label] || 0) + 1; });
  const bias = counts.bull > counts.bear ? "bull" : counts.bear > counts.bull ? "bear" : "range";

  // Keep only the strongest zones, then order high → low for the ladder. When we
  // know the live price, pick the strongest from EACH side and always include the
  // nearest zone above/below — so a score-ranked list can't hide one side (#116):
  // dense below-price structure otherwise buries every resistance overhead.
  const allZones = map?.zones || [];
  let strongest;
  if (price != null && allZones.length) {
    const half = Math.ceil(TOP_ZONES / 2);
    const byScore = (a, b) => b.score - a.score;
    const above = allZones.filter(z => z.mid > price).sort(byScore);
    const below = allZones.filter(z => z.mid <= price).sort(byScore);
    const pick = (arr, nearest) => {
      const top = arr.slice(0, half);
      if (nearest && !top.includes(nearest)) top.push(nearest);  // never hide the nearest side
      return top;
    };
    const nearestAbove = [...above].sort((a, b) => a.mid - b.mid)[0];   // lowest above price
    const nearestBelow = [...below].sort((a, b) => b.mid - a.mid)[0];   // highest below price
    strongest = [...pick(above, nearestAbove), ...pick(below, nearestBelow)];
  } else {
    strongest = [...allZones].sort((a, b) => b.score - a.score).slice(0, TOP_ZONES);
  }
  const maxScore = Math.max(1, ...strongest.map(z => z.score));
  const ladder = [...strongest].sort((a, b) => b.mid - a.mid);
  const priceShown = price != null && ladder.length > 0;

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-2 flex-wrap">
        <div className="text-sm font-medium flex items-center gap-2">
          Structure &amp; magnets<HelpHint term="magnet_zone" /> · XAUUSD
          {map?.version_id != null && (
            <Badge tone={STRUCT_TONE[bias]}>{bias} bias · {counts.bull}▲/{counts.bear}▼</Badge>
          )}
          {price != null && <span className="text-muted font-normal num">price {fmt(price, 2)}</span>}
        </div>
        <Button variant="ghost" onClick={onRecompute} disabled={busy}>
          {busy ? "Recomputing…" : "Recompute"}
        </Button>
      </div>

      {!map ? <Empty>Loading…</Empty>
        : map.version_id == null ? (
          <Empty>No map computed yet — click <b>Recompute</b> (needs an enabled account + a XAUUSD symbol map).</Empty>
        ) : (
        <div className="p-4 space-y-4">
          {/* one-glance multi-TF structure */}
          <div className="flex flex-wrap gap-1.5">
            {tfs.map(tf => {
              const s = structures[tf];
              return (
                <span key={tf}
                  className={`px-2 py-1 rounded-md text-[11px] num border ${
                    s.label === "bull" ? "border-long/40 text-long"
                    : s.label === "bear" ? "border-short/40 text-short"
                    : "border-edge text-muted"}`}
                  title={`${tf.toUpperCase()} · ${s.label} · P/D ${s.premium_discount == null ? "—" : Math.round(s.premium_discount * 100) + "%"}`}>
                  {tf.toUpperCase()}
                </span>
              );
            })}
          </div>

          {/* levels ladder — strongest zones, resistance above price / support below */}
          {!ladder.length ? <Empty>No magnet zones.</Empty> : (
            <div className="space-y-1">
              {priceShown && price > ladder[0].mid && <PriceRow price={price} />}
              {ladder.map((z, i) => {
                const above = price != null && z.mid > price;
                const prev = ladder[i - 1];
                const showPrice = priceShown && prev && prev.mid > price && z.mid <= price;
                const pct = price != null ? Math.abs(z.mid - price) / price * 100 : null;
                return (
                  <div key={z.rank}>
                    {showPrice && <PriceRow price={price} />}
                    <div className="flex items-center gap-3">
                      <span className={`w-20 shrink-0 num text-sm text-right ${above ? "text-short" : price != null ? "text-long" : ""}`}>
                        {fmt(z.mid, 2)}
                      </span>
                      <span className="flex-1 h-2 rounded-full bg-panel2 overflow-hidden">
                        <span className="block h-full rounded-full bg-beacon/70"
                          style={{ width: `${Math.max(6, (z.score / maxScore) * 100)}%` }} />
                      </span>
                      <span className="w-28 shrink-0 text-[11px] text-muted num text-right">
                        {z.n_timeframes} TF · {fmt(z.score, 0)}{pct != null ? ` · ${fmt(pct, 1)}%` : ""}
                      </span>
                    </div>
                  </div>
                );
              })}
              {priceShown && price < ladder[ladder.length - 1].mid && <PriceRow price={price} />}
            </div>
          )}
          <div className="text-[11px] text-muted">
            Bar = confluence strength (Σ weight across timeframes). <span className="text-short">Red</span> = above price
            (resistance), <span className="text-long">green</span> = below (support). Shadow only — nothing gates.
          </div>
        </div>
      )}
    </Card>
  );
}

function PriceRow({ price }) {
  return (
    <div className="flex items-center gap-3 my-1">
      <span className="w-20 shrink-0 num text-sm text-right font-semibold text-beacon">{price.toFixed(2)}</span>
      <span className="flex-1 border-t border-dashed border-beacon/50" />
      <span className="w-28 shrink-0 text-[11px] text-beacon text-right">◀ price</span>
    </div>
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
