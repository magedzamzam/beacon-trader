import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Toggle, Button } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import StructureMapChart from "../components/StructureMapChart";
import { api } from "../lib/api";

const REGIME_TONE = { trending: "beacon", ranging: "muted", high_vol: "warn", unknown: "muted" };
const STRUCT_TONE = { bull: "long", bear: "short", range: "muted" };
const TF_ORDER = ["1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m"];
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const pct0 = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);

/** Shadow analytics sidecar (#51/#53): signal↔channel↔regime correlation, now
 *  fronted by a decision/synthesis layer (#117) — an Act-now zone (weekly channel
 *  verdict + a per-signal combined read) over the raw stat cards, which collapse
 *  into "Details". Read-only observability — nothing here gates trading. */
export default function Analytics() {
  const [rep, setRep] = useState(null);
  const [struct, setStruct] = useState(null);
  const [synth, setSynth] = useState(null);
  const [signalRead, setSignalRead] = useState(null);
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

  // Per-signal combined read (#117): compose the latest scored signal's P(win)
  // (#62/#63) with its regime + HTF alignment + nearest magnet — all already
  // captured — into one line. No new estimator: it reads existing outputs.
  useEffect(() => {
    let cancelled = false;
    api.bayesAnalysis(5).then(async (b) => {
      if (cancelled) return;
      const recent = (b?.recent || []).filter(r => r.p_win != null);
      if (!recent.length) { setSignalRead({ base: b?.base_rate ?? null, none: true }); return; }
      const sig = recent[0];
      let analytics = null;
      try { analytics = await api.signalAnalytics(sig.signal_id); } catch { /* no analytics captured */ }
      if (!cancelled) setSignalRead({ base: b?.base_rate ?? null, sig, analytics });
    }).catch(() => { if (!cancelled) setSignalRead(null); });
    return () => { cancelled = true; };
  }, []);

  const recompute = async () => {
    setMapBusy(true);
    try { await api.structureRecompute(); await loadMap(); await loadPrice(); }
    catch (e) { setErr(e.message); } finally { setMapBusy(false); }
  };
  useEffect(() => {
    setRep(null); setStruct(null); setTrend(null); setSynth(null);
    api.analyticsSynthesis(range.range).then(setSynth).catch(e => setErr(e.message));
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

      <RangeFilter state={range} variant="coarse" />

      {/* ── Act now ─────────────────────────────────────────────── */}
      <WeeklyVerdictCard synth={synth} />
      <SignalReadCard read={signalRead} />

      {/* ── Details (collapsed) ─────────────────────────────────── */}
      <Collapse title="Details — raw analytics"
        subtitle="structure map · placement · trend · channel×regime · structure analyses">
        <StructureMapCard map={map} price={price} busy={mapBusy} onRecompute={recompute} />
        <StructureMapChart map={map} price={price} />
        <TrendAlignmentCard trend={trend} />
        <ChannelRegimeCard rep={rep} sigN={synth?.significance_n ?? 30} />
        {rep?.regime_mix_by_channel && Object.keys(rep.regime_mix_by_channel).length > 0 && (
          <RegimeMixCard mix={rep.regime_mix_by_channel} />
        )}
        <StructureAnalyses struct={struct} ready={!!struct} />
      </Collapse>
    </div>
  );
}

// A section toggle for the "Details" zone (#117): raw cards collapsed by default
// so the page is scannable in one screen. Children are the existing stat Cards.
function Collapse({ title, subtitle, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <button onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-1 py-2 text-left">
        <Chevron className="w-4 h-4 text-muted shrink-0" />
        <span className="text-sm font-medium">{title}</span>
        {subtitle && <span className="text-[11px] text-muted ml-auto hidden sm:block">{subtitle}</span>}
      </button>
      {open && <div className="space-y-5 pt-1">{children}</div>}
    </div>
  );
}

// The "so what?" (#117): the weekly per-channel keep / watch / cut verdict with an
// explicit significance state, so the operator doesn't assemble a conclusion from
// five tables. Sub-significance channels are de-emphasised and badged N/threshold,
// never read as a finding — and an honest "no credible edge yet" leads when true.
const VERDICT_TONE = { keep: "long", cut: "short", hold: "muted", watch: "warn", gathering: "muted" };

function WeeklyVerdictCard({ synth }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center gap-2 flex-wrap">
        <div className="text-sm font-medium">Weekly verdict — keep / watch / cut</div>
        {synth && <span className="text-muted font-normal text-[11px]">
          · {synth.n_labelled} labelled · base {pct0(synth.base_rate)} · significant at N≥{synth.significance_n}
        </span>}
      </div>
      {!synth ? <Empty>Loading…</Empty>
        : !synth.channels?.length ? <Empty>No labelled trades yet — verdicts accrue as signals capture and trades close.</Empty> : (
        <>
          {!synth.any_credible_edge && (
            <div className="mx-4 mt-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
              <b>No credible edge yet.</b> Nothing has crossed N={synth.significance_n} closed trades with
              its 90% interval clear of the base rate — everything below is provisional. Keep measuring;
              don't act on a per-channel verdict under the threshold.
            </div>
          )}
          <Table minW={720}>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th>Verdict</Th>
              <Th right>n / {synth.significance_n}</Th>
              <Th right>Win%</Th>
              <Th right>90% CI<HelpHint term="credible_interval" /></Th>
              <Th right>Expectancy<HelpHint term="expectancy" /></Th>
            </tr></thead>
            <tbody>
              {synth.channels.map((c, i) => {
                const provisional = c.state !== "significant";
                return (
                  <tr key={i} className={`border-b border-edge/60 ${provisional ? "opacity-55" : ""}`}>
                    <Td>{c.channel}</Td>
                    <Td>
                      <Badge tone={VERDICT_TONE[c.verdict] || "muted"}>{c.verdict}</Badge>
                      {c.state === "watch" && <span className="ml-1.5 text-[10px] text-muted">provisional</span>}
                    </Td>
                    <Td right mono>
                      <span className={provisional ? "text-muted" : ""}>{c.n}</span>
                      <span className="text-muted">/{synth.significance_n}</span>
                    </Td>
                    <Td right mono>{pct0(c.win_rate)}</Td>
                    <Td right mono>{pct0(c.ci_low)}–{pct0(c.ci_high)}</Td>
                    <Td right mono><span className={c.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(c.expectancy)}</span></Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          <div className="px-4 py-2 text-[11px] text-muted">
            <b>keep</b> = 90% lower bound above base · <b>cut</b> = upper bound below base ·
            <b>hold</b> = significant but straddles base · <b>watch</b>/<b>gathering</b> = below the N floor
            (grayed — not a finding). Shadow — nothing gates.
          </div>
        </>
      )}
    </Card>
  );
}

// Per-signal combined read (#117): one line synthesising the layers that already
// exist for the latest scored signal — P(win) vs base + HTF alignment + regime +
// nearest adverse magnet -> a lean. A heuristic compose of existing outputs, NOT a
// new model; it never gates.
const HTF_TONE = { aligned: "long", counter: "short", mixed: "muted" };
const LEAN_TONE = { TAKE: "long", SKIP: "short", WATCH: "muted" };

function SignalReadCard({ read }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        Per-signal read<HelpHint term="p_win" />
        <span className="text-muted font-normal text-[11px]">· latest scored signal</span>
      </div>
      {!read ? <Empty>Loading…</Empty>
        : read.none ? <Empty>No scored signal yet — a read appears once a signal captures features and the model is ready.</Empty>
        : <SignalReadLine read={read} />}
      <div className="px-4 py-2 text-[11px] text-muted">
        A heuristic compose of the existing P(win) + trend + structure layers — not a new model, and it
        never gates. "Lean" nets P(win)-vs-base, HTF alignment, and a near adverse magnet.
      </div>
    </Card>
  );
}

function SignalReadLine({ read }) {
  const { sig, base } = read;
  const sm = read.analytics?.analytics?.structure_magnet || {};
  const regime = read.analytics?.regime || null;
  const htf = sm.htf_alignment || null;                 // aligned | counter | mixed
  const dir = sig.direction;                            // BUY | SELL
  const res = sm.nearest_resistance;                    // zone above price
  const sup = sm.nearest_support;                       // zone below price
  // Adverse side: a BUY runs INTO resistance above; a SELL into support below.
  const adverse = dir === "BUY" ? res : dir === "SELL" ? sup : null;
  const adverseNear = adverse?.dist_atr != null && adverse.dist_atr <= 0.5;

  let s = 0;
  if (sig.p_win != null && base != null) s += sig.p_win >= base ? 1 : -1;
  if (htf === "aligned") s += 1; else if (htf === "counter") s -= 1;
  if (adverseNear) s -= 1;
  const lean = s > 0 ? "TAKE" : s < 0 ? "SKIP" : "WATCH";

  const distTag = (z) => (z?.dist_atr == null ? "—" : `${z.dist_atr.toFixed(1)} ATR`);

  return (
    <div className="px-4 py-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
      <span className="num text-muted">#{sig.signal_id}</span>
      <span className="font-medium">{sig.symbol}</span>
      <Badge tone={dir === "BUY" ? "long" : "short"}>{dir}</Badge>
      <span className="text-edge">·</span>
      <span>P(win) <span className="num font-medium">{sig.p_win == null ? "—" : pct0(sig.p_win)}</span>
        <span className="text-muted text-xs num"> (base {pct0(base)})</span></span>
      {htf && <><span className="text-edge">·</span>
        <span className="text-xs">HTF <Badge tone={HTF_TONE[htf] || "muted"}>{htf}</Badge></span></>}
      {regime && <><span className="text-edge">·</span>
        <span className="text-xs">regime <Badge tone={REGIME_TONE[regime] || "muted"}>{regime}</Badge></span></>}
      <span className="text-edge">·</span>
      <span className="text-xs text-muted num">
        R <span className={adverse === res && adverseNear ? "text-short" : ""}>{distTag(res)}</span> ·
        S <span className={adverse === sup && adverseNear ? "text-short" : ""}> {distTag(sup)}</span>
      </span>
      <span className="text-edge">·</span>
      <span className="text-xs">lean <Badge tone={LEAN_TONE[lean]}>{lean}</Badge></span>
    </div>
  );
}

// Channel × regime performance — the raw detail behind the weekly verdict. Rows
// under the significance floor are de-emphasised (#117) so a thin cell doesn't
// read as a confident finding.
function ChannelRegimeCard({ rep, sigN }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">
        Channel × regime performance {rep && <span className="text-muted font-normal">· {rep.n_labelled} labelled</span>}
      </div>
      {!rep ? <Empty>Loading…</Empty>
        : !rep.by_channel_regime?.length ? <Empty>No labelled analytics yet — accrues as signals capture and trades close.</Empty> : (
        <Table minW={860}>
          <thead><tr className="border-b border-edge">
            <Th>Channel</Th><Th>Regime<HelpHint term="regime" /></Th>
            <Th right>n / {sigN}</Th><Th right>Win%</Th>
            <Th right>90% CI<HelpHint term="credible_interval" /></Th>
            <Th right>Expectancy<HelpHint term="expectancy" /></Th>
          </tr></thead>
          <tbody>
            {rep.by_channel_regime.map((r, i) => {
              const provisional = r.n < sigN;
              return (
                <tr key={i} className={`border-b border-edge/60 ${provisional ? "opacity-55" : ""}`}>
                  <Td>{r.channel}</Td>
                  <Td><Badge tone={REGIME_TONE[r.regime] || "muted"}>{r.regime}</Badge></Td>
                  <Td right mono>{r.n}<span className="text-muted">/{sigN}</span></Td>
                  <Td right mono>{pct0(r.win_rate)}</Td>
                  <Td right mono>{pct0(r.ci_low)}–{pct0(r.ci_high)}</Td>
                  <Td right mono><span className={r.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(r.expectancy)}</span></Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

function RegimeMixCard({ mix }) {
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium">Regime mix by channel</div>
      <Table>
        <thead><tr className="border-b border-edge"><Th>Channel</Th><Th>Regimes (signal count)</Th></tr></thead>
        <tbody>
          {Object.entries(mix).map(([chan, m]) => (
            <tr key={chan} className="border-b border-edge/60">
              <Td>{chan}</Td>
              <Td><span className="flex flex-wrap gap-1.5">
                {Object.entries(m).map(([reg, n]) => (
                  <Badge key={reg} tone={REGIME_TONE[reg] || "muted"}>{reg}: {n}</Badge>
                ))}
              </span></Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
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
  // Nearest-each-side zones (#116) — emphasised in the map so they stay findable.
  const emph = new Set([map?.nearest_resistance?.rank, map?.nearest_support?.rank]
    .filter(r => r != null));

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

          {/* proportional price-band map — zones drawn at their true [low,high]
              extent, positioned by price (not equal-spaced) (#121) */}
          {!ladder.length ? <Empty>No magnet zones.</Empty>
            : <PriceBandMap zones={ladder} price={price} maxScore={maxScore} emph={emph} />}
          <div className="text-[11px] text-muted">
            Each box spans a zone's real <b>[low, high]</b>, placed proportionally by price; fill
            intensity = confluence strength (Σ weight across timeframes). <span className="text-short">Red</span>
            = above price (resistance), <span className="text-long">green</span> = below (support); a ring marks
            the nearest zone each side. Shadow only — nothing gates.
          </div>
        </div>
      )}
    </Card>
  );
}

// Proportional vertical price map (#121): each magnet zone is a box spanning its
// real [low, high] band, positioned by price (not equal-spaced), so the vertical
// geometry — how far levels sit apart, where price is relative to clustering —
// reads at a glance. Percentage-positioned HTML so it scales on mobile with no
// horizontal overflow, is theme-aware via tokens, and degrades to an sr-only list.
const MAP_H = 320;   // px

function PriceBandMap({ zones, price, maxScore, emph }) {
  // Domain spans every zone's band plus the live price, padded so nothing sits on
  // the edge. `hi` = top of the panel (higher price), `lo` = bottom.
  const vals = zones.flatMap(z => z.band).concat(price != null ? [price] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!(hi > lo)) { hi = hi + 1; lo = lo - 1; }   // single zero-width zone, no price
  const pad = (hi - lo) * 0.06;
  lo -= pad; hi += pad;
  const span = hi - lo;
  const yOf = (p) => ((hi - p) / span) * 100;      // price -> top %

  return (
    <div>
      <div className="relative w-full rounded-lg border border-edge bg-panel2/40 overflow-hidden"
        style={{ height: MAP_H }} role="img"
        aria-label={`Magnet zone price map, ${zones.length} zones${price != null ? `, price ${price.toFixed(2)}` : ""}`}>
        {zones.map(z => {
          const [zl, zh] = z.band;
          const top = yOf(zh);
          const h = Math.max(1.4, yOf(zl) - yOf(zh));   // ensure a hairline for zero-width zones
          const above = price != null && z.mid > price;
          const tone = above ? "short" : price != null ? "long" : "beacon";
          const isEmph = emph.has(z.rank);
          const base = 0.18 + 0.62 * Math.min(1, z.score / maxScore);         // 0.18–0.80
          const intensity = isEmph ? Math.max(0.7, base) : base;              // nearest stands out
          const bg = { short: "var(--short)", long: "var(--long)", beacon: "var(--beacon)" }[tone];
          return (
            <div key={z.rank}
              title={`${fmt(z.mid, 2)} · ${z.n_timeframes} TF · score ${fmt(z.score, 0)} · [${fmt(z.band[0], 2)}, ${fmt(z.band[1], 2)}]`}
              className="absolute left-16 right-2 rounded"
              style={{ top: `${top}%`, height: `${h}%`, background: bg, opacity: intensity,
                       ...(isEmph ? { boxShadow: `0 0 0 1.5px ${bg}` } : {}) }} />
          );
        })}
        {/* per-zone price + meta labels, vertically centred on each band */}
        {zones.map(z => {
          const mid = yOf(z.mid);
          const above = price != null && z.mid > price;
          const toneCls = above ? "text-short" : price != null ? "text-long" : "text-ink";
          return (
            <div key={`lbl-${z.rank}`} className="absolute left-0 right-2 flex items-center justify-between pointer-events-none"
              style={{ top: `${mid}%`, transform: "translateY(-50%)" }}>
              <span className={`num text-[11px] w-16 text-right pr-1 ${toneCls}`}>{fmt(z.mid, 2)}</span>
              <span className="num text-[10px] text-muted pr-1">
                {z.n_timeframes}TF · {fmt(z.score, 0)}
              </span>
            </div>
          );
        })}
        {/* live price — a beacon rule woven through the bands */}
        {price != null && (
          <div className="absolute left-0 right-0 flex items-center pointer-events-none"
            style={{ top: `${yOf(price)}%`, transform: "translateY(-50%)" }}>
            <span className="num text-[11px] font-semibold text-beacon w-16 text-right pr-1">{price.toFixed(2)}</span>
            <span className="flex-1 border-t border-dashed border-beacon/70" />
            <span className="text-[10px] text-beacon pl-1 pr-1">price ▶</span>
          </div>
        )}
      </div>
      {/* Accessible / print fallback: the same zones as an ordered high→low list. */}
      <ul className="sr-only">
        {zones.map(z => (
          <li key={`sr-${z.rank}`}>
            {price != null ? (z.mid > price ? "resistance" : "support") : "zone"} at {fmt(z.mid, 2)},
            band {fmt(z.band[0], 2)} to {fmt(z.band[1], 2)}, {z.n_timeframes} timeframes, score {fmt(z.score, 0)}
            {emph.has(z.rank) ? " (nearest this side)" : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

// One on-demand "Structure analysis" surface (#117): the per-structure cuts (FVG,
// Order Block, and any future ones) as a single accordion — collapsed by default,
// at most one open at a time — instead of one always-visible card each. Adding an
// analysis is one new row, never a new card.
const STRUCTURE_ANALYSES = [
  { key: "fvg", label: "Fair Value Gap — inside vs outside" },
  { key: "ob", label: "Order Block — inside vs outside" },
];

function structureHeadline(rows) {
  if (!rows || !rows.length) return "gathering data (N=0)";
  const ins = rows.find(r => r.scope === "overall" && r.membership === "inside");
  const out = rows.find(r => r.scope === "overall" && r.membership === "outside");
  if (!ins && !out) return `N=${rows.reduce((s, r) => s + (r.n || 0), 0)}`;
  const tag = (r) => (r ? `${pct0(r.win_rate)} (n=${r.n})` : "—");
  return `inside ${tag(ins)} · outside ${tag(out)}`;
}

function StructureAnalyses({ struct, ready }) {
  const [open, setOpen] = useState(null);   // key of the single open analysis (or null)
  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
        Structure analyses
        <span className="text-muted font-normal text-[11px]">· open one on demand</span>
      </div>
      {!ready ? <Empty>Loading…</Empty> : (
        <div className="divide-y divide-edge/60">
          {STRUCTURE_ANALYSES.map(a => {
            const rows = struct?.[a.key];
            const isOpen = open === a.key;
            const Chevron = isOpen ? ChevronDown : ChevronRight;
            return (
              <div key={a.key}>
                <button onClick={() => setOpen(isOpen ? null : a.key)}
                  className="w-full px-4 py-3 flex items-center justify-between gap-3 text-left">
                  <span className="flex items-center gap-2 min-w-0">
                    <Chevron className="w-4 h-4 text-muted shrink-0" />
                    <span className="text-sm">{a.label}</span>
                  </span>
                  <span className="text-[11px] text-muted num truncate">{structureHeadline(rows)}</span>
                </button>
                {isOpen && (
                  <div className="pb-2">
                    {!rows || !rows.length
                      ? <Empty>No labelled structure data yet — accrues as signals capture and trades close.</Empty>
                      : <StructureRows rows={rows} />}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// FVG/OB-vs-outcome cut (#59): win-rate & expectancy inside vs outside the zone,
// overall then per channel/regime, with 90% credible intervals.
function StructureRows({ rows }) {
  return (
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
            <Td right mono>{pct0(r.win_rate)}</Td>
            <Td right mono>{pct0(r.ci_low)}–{pct0(r.ci_high)}</Td>
            <Td right mono><span className={r.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(r.expectancy)}</span></Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
