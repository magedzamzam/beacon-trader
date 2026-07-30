import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Table, Card, Th, Td, Badge, Empty } from "../components/ui";
import { Toggle, Button } from "../components/form";
import RangeFilter, { useRange } from "../components/RangeFilter";
import HelpHint from "../components/HelpHint";
import { api } from "../lib/api";

const REGIME_TONE = { trending: "beacon", ranging: "muted", high_vol: "warn", unknown: "muted" };
const TF_ORDER = ["1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m"];
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const pct0 = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);

// Multi-TF structure bias from the map. The Structure card that used to share it
// is gone (#175); the summary strip's bias tile is the surviving reader, and the
// map behind it is unchanged.
function biasFrom(map) {
  const structures = map?.structures || {};
  const tfs = TF_ORDER.filter(t => structures[t]);
  const counts = { bull: 0, bear: 0, range: 0 };
  tfs.forEach(t => { const l = structures[t].label; counts[l] = (counts[l] || 0) + 1; });
  const bias = counts.bull > counts.bear ? "bull" : counts.bear > counts.bull ? "bear" : "range";
  return { tfs, counts, bias };
}

/** Shadow analytics sidecar (#51/#53): signal↔channel↔regime correlation, now
 *  fronted by a decision/synthesis layer (#117) — an Act-now zone (weekly channel
 *  verdict + a per-signal combined read) over the raw stat cards, which collapse
 *  into "Details". Read-only observability — nothing here gates trading.
 *
 *  #175 removed the detail views that could not inform a decision: Channel×Regime
 *  and Regime mix cross-tabbed against `regime`, which is 'trending' on every row
 *  captured (#111 — the estimator reads a null field); Trend folded on a 4h-EMA200
 *  alignment that is a perfect relabelling of `direction` (BUY×ALIGNED and
 *  SELL×COUNTER are both EMPTY across the whole dataset); and the Structure /
 *  Placement / Magnets charts, which have never produced an actionable finding.
 *  THE CAPTURE IS UNCHANGED — every estimator still writes `signal_analytics`, and
 *  the >=2-epoch replication history a future gate needs stays intact. What went
 *  away is chart code, not evidence. */
export default function Analytics() {
  const [synth, setSynth] = useState(null);
  const [signalRead, setSignalRead] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const range = useRange("all");

  const [shadow, setShadow] = useState(null);
  // The structure map survives the chart removal: the summary strip's multi-TF
  // bias reads it, and the monitor keeps it fresh on its own schedule.
  const [map, setMap] = useState(null);
  const loadCfg = () => api.analyticsConfig().then(setCfg).catch(e => setErr(e.message));
  const loadMap = () => api.structureMap("XAUUSD").then(setMap).catch(e => setErr(e.message));
  useEffect(() => { loadCfg(); loadMap(); }, []);

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

  useEffect(() => {
    setSynth(null); setShadow(null);
    api.analyticsSynthesis(range.range).then(setSynth).catch(e => setErr(e.message));
    api.analyticsShadowStrategies(range.range).then(setShadow).catch(e => setErr(e.message));
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
          (small samples shrink toward the {synth ? `${fmt(synth.base_rate * 100, 1)}%` : "base"} rate).
        </div>
      </Card>

      <RangeFilter state={range} variant="coarse" />

      {/* ── Summary strip (#123): the handful of numbers that orient the page ── */}
      <AnalyticsSummary synth={synth} map={map} />

      {/* ── Act now ─────────────────────────────────────────────── */}
      <WeeklyVerdictCard synth={synth} />
      <SignalReadCard read={signalRead} />

      {/* ── Details (collapsed) — one analysis panel at a time (#123) ─── */}
      <Collapse title="Details — raw analytics" subtitle="MC / Turtle">
        <DetailsTabs tabs={[
          { key: "shadow_strategies", label: "MC / Turtle",
            node: <ShadowStrategiesCard shadow={shadow} range={range} /> },
        ]} />
      </Collapse>
    </div>
  );
}

// Compact orienting KPI row (#123): base rate, labelled count, significant-channel
// keep/cut, and the current multi-TF bias — one glance = "where do we stand".
// Numbers come straight from the same sources as the cards below.
function AnalyticsSummary({ synth, map }) {
  const { bias, counts } = biasFrom(map);
  const hasMap = map?.version_id != null;
  const sig = (synth?.channels || []).filter(c => c.state === "significant");
  const keep = sig.filter(c => c.verdict === "keep").length;
  const cut = sig.filter(c => c.verdict === "cut").length;
  const biasCls = bias === "bull" ? "text-long" : bias === "bear" ? "text-short" : "text-muted";
  const tiles = [
    { label: "Base rate", value: synth ? pct0(synth.base_rate) : "—" },
    { label: "Labelled", value: synth ? synth.n_labelled : "—",
      sub: synth ? `N≥${synth.significance_n} to signif.` : "" },
    { label: "Significant channels", value: synth ? sig.length : "—",
      sub: synth ? `${keep} keep · ${cut} cut` : "" },
    { label: "Multi-TF bias", value: hasMap ? bias : "—",
      valueCls: hasMap ? biasCls : "", sub: hasMap ? `${counts.bull}▲ / ${counts.bear}▼` : "" },
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {tiles.map(t => (
        <div key={t.label} className="card p-3">
          <div className="text-[10px] uppercase tracking-wider text-muted truncate">{t.label}</div>
          <div className={`mt-1 num text-xl font-semibold ${t.valueCls || ""}`}>{t.value}</div>
          {t.sub && <div className="text-[11px] text-muted mt-0.5 num">{t.sub}</div>}
        </div>
      ))}
    </div>
  );
}

// Tabbed Details (#123): show one analysis panel at a time instead of a five-card
// vertical stack, so Details is a single screen. The chosen tab persists across
// reloads; the strip scrolls horizontally on mobile (same pattern as the
// Configuration tabs). Data is fetched at the page level regardless of tab, so
// switching only mounts/unmounts a panel — no refetch coupling.
function DetailsTabs({ tabs }) {
  const [active, setActive] = useState(() => localStorage.getItem("beacon_analytics_tab") || tabs[0].key);
  const keys = tabs.map(t => t.key);
  const cur = keys.includes(active) ? active : tabs[0].key;
  const pick = (k) => { setActive(k); localStorage.setItem("beacon_analytics_tab", k); };
  const node = tabs.find(t => t.key === cur)?.node;
  return (
    <div>
      <div className="flex gap-1 overflow-x-auto pb-2 -mb-1">
        {tabs.map(t => (
          <button key={t.key} onClick={() => pick(t.key)}
            className={`whitespace-nowrap px-3 py-1.5 rounded-lg text-sm border transition ${
              t.key === cur ? "bg-beacon/15 text-beacon border-beacon/40"
                            : "bg-panel2 text-muted border-edge hover:text-ink"}`}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="pt-2">{node}</div>
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

// Monte Carlo geometry null + Turtle breakout vs outcome. The point of this panel
// is that a raw win-rate is not evidence: a signal with a far stop and a near
// target wins most of the time by arithmetic. `edge` is the realized win-rate
// MINUS what each signal's own SL/TP layout implies with no skill assumed, so it
// is the first number here that can actually be read as channel skill.
const pp = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}pp`);
const sgn = (v, d = 2) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}`);
const edgeCls = (v) => (v == null ? "" : v > 0 ? "text-long" : v < 0 ? "text-short" : "text-muted");

function ShadowStrategiesCard({ shadow, range }) {
  if (!shadow) return <Card><Empty>Loading…</Empty></Card>;
  const mc = shadow.montecarlo || {};
  const tu = shadow.turtle || {};
  const sigN = shadow.significance_n ?? 30;
  const mcChannels = Object.entries(mc.by_channel || {});
  const tuRows = ["agrees", "disagrees"].filter(k => tu.overall?.[k]);

  return (
    <div className="space-y-5">
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
          Monte Carlo — geometry null<HelpHint term="mc_geometry_null" />
          <span className="text-muted font-normal text-[11px]">
            · {mc.n ?? 0} labelled · significant at N≥{sigN}
          </span>
        </div>
        {!mc.n ? (
          <Empty>No labelled signals with a Monte Carlo block yet — accrues as signals capture and trades close.</Empty>
        ) : (
          <>
            {mc.n_horizon_truncated > 0 && (
              <div className="mx-4 mt-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
                <b>{mc.n_horizon_truncated} of {mc.n} signals didn't resolve inside the horizon.</b>{" "}
                Their geometry win-rate is understated, which inflates Edge in the same direction.
                Raise <span className="num">analytics.montecarlo.horizon_bars</span> and let it
                re-accrue before reading these as skill.
              </div>
            )}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 p-4">
              {[
                { label: "Actual win%", value: pct0(mc.actual_win_rate) },
                { label: "Geometry win%", value: pct0(mc.geometry_win_rate), sub: "no skill assumed" },
                { label: "Edge", value: pp(mc.edge), cls: edgeCls(mc.edge), sub: "actual − geometry" },
                { label: "Mean R vs null", value: sgn(mc.r_edge), cls: edgeCls(mc.r_edge),
                  sub: `${sgn(mc.actual_mean_r)} vs ${sgn(mc.null_mean_r)}` },
              ].map(t => (
                <div key={t.label} className="rounded-lg border border-edge bg-panel2/40 p-3">
                  <div className="text-[10px] uppercase tracking-wider text-muted truncate">{t.label}</div>
                  <div className={`mt-1 num text-xl font-semibold ${t.cls || ""}`}>{t.value}</div>
                  {t.sub && <div className="text-[11px] text-muted mt-0.5 num">{t.sub}</div>}
                </div>
              ))}
            </div>

            <Table minW={860}>
              <thead><tr className="border-b border-edge">
                <Th>Channel</Th><Th right>n / {sigN}</Th><Th right>Actual</Th>
                <Th right>Geometry</Th><Th right>Edge</Th>
                <Th right>90% CI<HelpHint term="credible_interval" /></Th><Th>Verdict</Th>
              </tr></thead>
              <tbody>
                {mcChannels.map(([ch, c]) => (
                  <tr key={ch} className={`border-b border-edge/60 ${c.significant ? "" : "opacity-55"}`}>
                    <Td>{ch}</Td>
                    <Td right mono>{c.n}<span className="text-muted">/{sigN}</span></Td>
                    <Td right mono>{pct0(c.actual_win_rate)}</Td>
                    <Td right mono><span className="text-muted">{pct0(c.geometry_win_rate)}</span></Td>
                    <Td right mono><span className={edgeCls(c.edge)}>{pp(c.edge)}</span></Td>
                    <Td right mono>{pct0(c.ci_low)}–{pct0(c.ci_high)}</Td>
                    <Td>{!c.significant
                      ? <span className="text-[10px] text-muted">gathering</span>
                      : <Badge tone={c.beats_null ? "long" : "muted"}>
                          {c.beats_null ? "beats null" : "no edge"}</Badge>}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>

            {!!mc.calibration?.length && (
              <>
                <div className="px-4 pt-3 text-[11px] text-muted">
                  <b>Calibration</b> — is the null itself trustworthy? Actual should track Geometry
                  down the diagonal. A whole column drifting one way means the volatility estimate or
                  the horizon is off, not that the channels found an edge.
                </div>
                <Table minW={620}>
                  <thead><tr className="border-b border-edge">
                    <Th>P(win) bucket</Th><Th right>n</Th><Th right>Geometry</Th>
                    <Th right>Actual</Th><Th right>Edge</Th>
                  </tr></thead>
                  <tbody>
                    {mc.calibration.map(b => (
                      <tr key={b.bucket} className="border-b border-edge/60">
                        <Td mono>{b.bucket}</Td>
                        <Td right mono>{b.n}</Td>
                        <Td right mono><span className="text-muted">{pct0(b.geometry_win_rate)}</span></Td>
                        <Td right mono>{pct0(b.actual_win_rate)}</Td>
                        <Td right mono><span className={edgeCls(b.edge)}>{pp(b.edge)}</span></Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </>
            )}
          </>
        )}
        <div className="px-4 py-2 text-[11px] text-muted">
          A <b>high</b> geometry win% is not good news — it means the stop is far and the target near.
          Read <b>Edge</b> and <b>Mean R vs null</b>, never the raw win-rate. Shadow — nothing gates.
        </div>
      </Card>

      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2 flex-wrap">
          Turtle — 55-bar Donchian breakout<HelpHint term="turtle_signal" />
          <span className="text-muted font-normal text-[11px]">
            · {tu.n_unknown ?? 0} without a reading
            {tu.n_diverges ? ` · ${tu.n_diverges} where the reference variant diverges` : ""}
          </span>
        </div>
        {!tuRows.length ? (
          <Empty>No labelled signals with a Turtle reading yet — needs more than 55 bars of history at signal time.</Empty>
        ) : (
          <Table minW={720}>
            <thead><tr className="border-b border-edge">
              <Th>Breakout system</Th><Th right>n</Th><Th right>Win%</Th>
              <Th right>90% CI</Th><Th right>Net</Th><Th right>Expectancy</Th>
            </tr></thead>
            <tbody>
              {tuRows.map(k => {
                const r = tu.overall[k];
                return (
                  <tr key={k} className="border-b border-edge/60">
                    <Td><Badge tone={k === "agrees" ? "long" : "short"}>{k}</Badge></Td>
                    <Td right mono>{r.n}</Td>
                    <Td right mono>{pct0(r.win_rate)}</Td>
                    <Td right mono>{pct0(r.ci_low)}–{pct0(r.ci_high)}</Td>
                    <Td right mono><span className={r.net >= 0 ? "text-long" : "text-short"}>{fmt(r.net)}</span></Td>
                    <Td right mono><span className={r.expectancy >= 0 ? "text-long" : "text-short"}>{fmt(r.expectancy)}</span></Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
        <div className="px-4 py-2 text-[11px] text-muted">
          "agrees" = the mechanical breakout system held the same side as the channel's call. A channel
          that only wins when it agrees is echoing a free rule; one that wins independently is adding
          something the rule cannot see. Shadow — nothing gates.
        </div>
      </Card>

      <TurtleExitCard range={range} />
    </div>
  );
}

// One of the two mechanisms (#171). `clear` is the honest bar — a mean that does
// not beat its own stderr at N>=30 is not a result, however large it looks.
// Static, because Tailwind's JIT cannot see a class name built at runtime.
const MECH_BORDER = { beacon: "border-beacon/40", violet: "border-violet/40" };

function MechanismCard({ title, tone, blurb, b, extra, sigN }) {
  const state = !b || !b.n ? { tone: "muted", text: "no trades" }
    : !b.significant ? { tone: "muted", text: `n ${b.n}/${sigN}` }
    : !b.clear ? { tone: "muted", text: "inside the noise" }
    : b.mean_delta_r > 0 ? { tone: "long", text: "clears its noise" }
    : { tone: "short", text: "negative, clears its noise" };
  return (
    <div className={`rounded-lg border p-3 bg-panel2/40 ${
      b?.clear ? (MECH_BORDER[tone] || "border-edge") : "border-edge"}`}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-medium">{title}</span>
        <Badge tone={state.tone}>{state.text}</Badge>
      </div>
      <div className={`mt-2 num text-2xl font-semibold ${edgeCls(b?.mean_delta_r)}`}>
        {b?.n ? sgn(b.mean_delta_r) : "—"}
        {b?.stderr != null && <span className="text-[11px] text-muted font-normal"> ± {fmt(b.stderr)}</span>}
      </div>
      <div className="text-[11px] text-muted num">
        {b?.n ? `n ${b.n}` : ""}{extra ? ` · ${extra}` : ""}
      </div>
      <p className="mt-2 text-[11px] text-muted">{blurb}</p>
    </div>
  );
}

// Turtle exit counterfactual (#170, split in #171): would closing on a trend flip have beaten
// where each trade actually closed? This is the evidence that decides whether a
// Turtle exit ever gets wired into the live SL engine — so it stays a backtest
// until it earns that, and it is loaded ON DEMAND because it costs a broker
// bar fetch.
function TurtleExitCard({ range }) {
  const [rep, setRep] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [variant, setVariant] = useState("signal");

  const run = async (v) => {
    setBusy(true); setErr(null);
    try { setRep(await api.analyticsTurtleExit(range?.range || {}, v ?? variant)); }
    catch (e) { setErr(e.message); setRep(null); }
    finally { setBusy(false); }
  };
  useEffect(() => { setRep(null); }, [range?.fromIso, range?.toIso]);

  const o = rep?.overall;
  // #171: `overall` blends two mechanisms and is not actionable on its own, so
  // the verdict is driven by whichever SPLIT block actually clears its noise.
  const er = rep?.exit_rule, ef = rep?.entry_filter;
  const verdict = !o ? null
    : er?.clear && er.mean_delta_r > 0 ? { tone: "long", text: "exit rule looks real" }
    : ef?.clear && ef.mean_delta_r > 0 ? { tone: "beacon", text: "entry filter, not an exit rule" }
    : (er?.clear || ef?.clear) ? { tone: "short", text: "would have hurt" }
    : !o.significant ? { tone: "muted", text: "gathering" }
    : { tone: "muted", text: "inside the noise" };

  return (
    <Card>
      <div className="px-4 py-3 border-b border-edge flex items-center justify-between gap-2 flex-wrap">
        <div className="text-sm font-medium flex items-center gap-2">
          Turtle exit counterfactual<HelpHint term="turtle_exit" />
          {verdict && <Badge tone={verdict.tone}>{verdict.text}</Badge>}
        </div>
        <div className="flex items-center gap-2">
          <select value={variant} onChange={(e) => { setVariant(e.target.value); setRep(null); }}
            className="bg-panel2 border border-edge rounded-lg px-2.5 py-1.5 text-xs outline-none focus:border-beacon">
            <option value="signal">reference (stop-and-reverse)</option>
            <option value="signal_flat">exits to flat</option>
          </select>
          <Button variant="ghost" onClick={() => run()} disabled={busy}>
            {busy ? "Replaying…" : rep ? "Re-run" : "Run backtest"}
          </Button>
        </div>
      </div>

      {err && <div className="px-4 py-3 text-sm text-short">{err}</div>}
      {!rep && !err ? (
        <Empty>
          Replays the 55-bar Donchian across every closed trade and prices the exit a flip would
          have forced. Costs one bar fetch, so it runs on demand — press <b>Run backtest</b>.
        </Empty>
      ) : !o ? (
        <Empty>No closed trades with usable legs in this range.</Empty>
      ) : (
        <>
          {/* The two mechanisms, side by side. They imply very different work,
              so the panel refuses to lead with the blended number (#171). */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-4">
            <MechanismCard
              title="Exit rule" tone="beacon"
              blurb="Turtle BACKED the trade at entry, then turned against it. Only this could justify a close-at-market exit in the live SL engine."
              b={er} extra={er?.n ? `turn rate ${pct0(er.turn_rate)} · ${er.helped}/${er.hurt} helped/hurt` : null}
              sigN={rep.significance_n} />
            <MechanismCard
              title="Entry filter" tone="violet"
              blurb="Turtle ALREADY opposed the trade at entry. Skipping means never taking it, so the counterfactual is R = 0 — this points at the turtle_signal filter that already exists, inert."
              b={ef} extra={ef?.n ? `actual ${sgn(ef.mean_actual_r)} · win ${pct0(ef.win_rate)}` : null}
              sigN={rep.significance_n} />
          </div>

          {rep.by_stop_distance && (
            <>
              <div className="px-4 pt-1 text-[11px] text-muted">
                <b>Stop distance</b> — a 55-bar flip is slow, so it can only beat a stop that sits
                far away. If Δ R lives entirely in the <b>wide</b> band this is a finding about stop
                placement, not about the Turtle.
              </div>
              <Table minW={620}>
                <thead><tr className="border-b border-edge">
                  <Th>Risk band</Th><Th right>n</Th><Th right>Risk range</Th>
                  <Th right>Mean Δ R</Th><Th right>± stderr</Th>
                </tr></thead>
                <tbody>
                  {["narrow", "mid", "wide"].filter(k => rep.by_stop_distance[k]).map(k => {
                    const b = rep.by_stop_distance[k];
                    return (
                      <tr key={k} className="border-b border-edge/60">
                        <Td>{k}</Td>
                        <Td right mono>{b.n}</Td>
                        <Td right mono><span className="text-muted">{fmt(b.risk_lo)}–{fmt(b.risk_hi)}</span></Td>
                        <Td right mono><span className={edgeCls(b.mean_delta_r)}>{sgn(b.mean_delta_r)}</span></Td>
                        <Td right mono><span className="text-muted">{fmt(b.stderr)}</span></Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </>
          )}

          <div className="mx-4 my-3 rounded-lg border border-edge bg-panel2/40 px-3 py-2 text-[11px] text-muted">
            <b>Blended (not actionable):</b> mean Δ R {sgn(o.mean_delta_r)} over {o.n} trades,
            flip rate {pct0(o.flip_rate)}, {o.helped} helped / {o.hurt} hurt. This mixes both
            mechanisms above — read them separately.
          </div>

          <Table minW={860}>
            <thead><tr className="border-b border-edge">
              <Th>Channel</Th><Th right>n</Th><Th right>Flip rate</Th>
              <Th right>Actual R</Th><Th right>Flip-exit R</Th><Th right>Δ R</Th>
              <Th right>Helped / hurt</Th>
            </tr></thead>
            <tbody>
              {Object.entries(rep.by_channel || {}).map(([ch, c]) => (
                <tr key={ch} className={`border-b border-edge/60 ${c.significant ? "" : "opacity-55"}`}>
                  <Td>{ch}</Td>
                  <Td right mono>{c.n}</Td>
                  <Td right mono>{pct0(c.flip_rate)}</Td>
                  <Td right mono>{sgn(c.mean_actual_r)}</Td>
                  <Td right mono>{sgn(c.mean_counterfactual_r)}</Td>
                  <Td right mono><span className={edgeCls(c.mean_delta_r)}>{sgn(c.mean_delta_r)}</span></Td>
                  <Td right mono><span className="text-long">{c.helped}</span>
                    <span className="text-muted"> / </span>
                    <span className="text-short">{c.hurt}</span></Td>
                </tr>
              ))}
            </tbody>
          </Table>

          <div className="px-4 py-2 text-[11px] text-muted">
            {rep.n_evaluated}/{rep.n_trades} closed trades replayed over {rep.n_bars} {rep.timeframe} bars
            {rep.skipped && Object.keys(rep.skipped).length > 0 &&
              ` · skipped: ${Object.entries(rep.skipped).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ")}`}.
            Both R figures are <b>price-basis</b> off the same entry and risk distance — not
            <span className="num"> realized_pl</span>, which spans a multi-leg ladder. A 55-bar flip is a
            <b> slow</b> signal, so it can only beat a stop that sits far away: check any positive result
            is not just an artifact of stop distance. Costs are not modelled, so the extra exit is
            charged no spread. Shadow — nothing gates.
          </div>
        </>
      )}
    </Card>
  );
}
