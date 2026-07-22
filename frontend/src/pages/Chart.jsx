import { useEffect, useRef, useState } from "react";
import { createChart, ColorType, LineStyle } from "lightweight-charts";
import { Card, Empty } from "../components/ui";
import { Select, ErrorNote } from "../components/form";
import { api } from "../lib/api";

const RESOLUTIONS = [
  ["MINUTE", "1m"], ["MINUTE_5", "5m"], ["MINUTE_15", "15m"],
  ["HOUR", "1h"], ["HOUR_4", "4h"], ["DAY", "1D"],
];
const OPEN = new Set(["open", "working", "pending"]);

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export default function Chart({ account }) {
  const wrap = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const linesRef = useRef([]);
  const [resolution, setResolution] = useState("MINUTE_5");
  const [err, setErr] = useState(null);
  const [last, setLast] = useState(null);

  // build chart once
  useEffect(() => {
    if (!wrap.current) return;
    const chart = createChart(wrap.current, {
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: cssVar("--muted") },
      grid: { vertLines: { color: cssVar("--edge") }, horzLines: { color: cssVar("--edge") } },
      rightPriceScale: { borderColor: cssVar("--edge") },
      timeScale: { borderColor: cssVar("--edge"), timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      autoSize: true,
    });
    const series = chart.addCandlestickSeries({
      upColor: cssVar("--long"), downColor: cssVar("--short"),
      wickUpColor: cssVar("--long"), wickDownColor: cssVar("--short"),
      borderVisible: false,
    });
    chartRef.current = chart; seriesRef.current = series;
    return () => { chart.remove(); chartRef.current = null; };
  }, []);

  // load candles + position lines
  useEffect(() => {
    let alive = true;
    const draw = async () => {
      try {
        const res = await api.candles("XAUUSD", resolution, 300);
        if (!alive || !seriesRef.current) return;
        const data = (res.bars || [])
          .map(b => ({ time: Math.floor(Date.parse(b.t) / 1000), open: b.o, high: b.h, low: b.l, close: b.c }))
          .filter(b => Number.isFinite(b.time))
          .sort((a, b) => a.time - b.time);
        // de-dup identical timestamps (lightweight-charts requires strictly ascending)
        const seen = new Set(); const clean = [];
        for (const d of data) { if (!seen.has(d.time)) { seen.add(d.time); clean.push(d); } }
        seriesRef.current.setData(clean);
        setErr(null);
        await drawPositions();
      } catch (e) { if (alive) setErr(e.message); }
    };
    draw();
    const t = setInterval(draw, 20000);
    return () => { alive = false; clearInterval(t); };
    // eslint-disable-next-line
  }, [resolution, account]);   // #118 redraw overlays when the account filter changes

  const drawPositions = async () => {
    const series = seriesRef.current; if (!series) return;
    linesRef.current.forEach(l => series.removePriceLine(l));
    linesRef.current = [];
    try {
      const trades = await api.trades(account);   // #118 overlay only the selected account's positions
      const legs = [];
      trades.forEach(t => t.legs.filter(l => OPEN.has(l.status)).forEach(l => legs.push({ t, l })));
      const seenEntry = new Set();
      legs.forEach(({ t, l }) => {
        const ek = `${l.entry}-${t.direction}`;
        if (!seenEntry.has(ek)) {
          seenEntry.add(ek);
          linesRef.current.push(series.createPriceLine({
            price: +l.entry, color: cssVar("--beacon-2"), lineWidth: 1,
            lineStyle: LineStyle.Solid, axisLabelVisible: true, title: `${t.direction} entry` }));
        }
        linesRef.current.push(series.createPriceLine({
          price: +l.tp, color: cssVar("--long"), lineWidth: 1,
          lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: `TP${l.tp_index}` }));
      });
      // one SL line (shared)
      if (legs[0]) {
        linesRef.current.push(series.createPriceLine({
          price: +legs[0].l.sl, color: cssVar("--short"), lineWidth: 1,
          lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "SL" }));
      }
    } catch { /* positions optional */ }
  };

  // live last price
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try { const q = await api.quote("XAUUSD"); if (alive) setLast(q.last || q.bid); } catch {}
    };
    poll(); const t = setInterval(poll, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <div className="space-y-3">
      <ErrorNote>{err}</ErrorNote>
      <Card className="p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-baseline gap-3">
            <span className="font-medium">XAUUSD</span>
            <span className="num text-2xl font-semibold">{last != null ? Number(last).toFixed(2) : "—"}</span>
          </div>
          <div className="w-28">
            <Select value={resolution} onChange={e => setResolution(e.target.value)}>
              {RESOLUTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
          </div>
        </div>
        <div ref={wrap} style={{ height: 460, width: "100%" }} />
        <div className="mt-2 text-xs text-muted flex gap-4">
          <span><span className="inline-block w-3 h-0.5 align-middle" style={{ background: "var(--beacon-2)" }} /> entry</span>
          <span><span className="inline-block w-3 h-0.5 align-middle" style={{ background: "var(--long)" }} /> TP</span>
          <span><span className="inline-block w-3 h-0.5 align-middle" style={{ background: "var(--short)" }} /> SL</span>
          <span className="ml-auto">lines reflect current open positions</span>
        </div>
      </Card>
    </div>
  );
}
