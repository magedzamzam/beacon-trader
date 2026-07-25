import { useEffect, useState } from "react";
import { Card, Th, Td, Badge, Empty } from "./ui";
import { api } from "../lib/api";

/**
 * ConfluenceZonePanel (#137) — a reusable, KIND-AGNOSTIC magnet-zone panel.
 *
 * Shows, relative to the current price, the nearest/strongest confluence zones on
 * each side: BUY-Side (discount, below price) and SELL-Side (premium, above price),
 * each the nearest 3. A timeframe selector switches between the all-TF aggregate
 * and a single timeframe's zones. Driven entirely by the `kind` prop:
 *   <ConfluenceZonePanel kind="fvg" />          // today
 *   <ConfluenceZonePanel kind="order_block" />  // later — no code change
 * The component name is deliberately generic; only the card TITLES reference the
 * kind. Read-only shadow analytics — nothing here gates trading.
 */
const KIND_LABEL = { fvg: "FVG", order_block: "Order Block", fib_retracement: "Fib",
  fib_extension: "Fib Ext", swing_high: "Swing", swing_low: "Swing" };
const TF_LABEL = { "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
  "1h": "1H", "4h": "4H", "1d": "1D" };
const STRENGTH_TONE = { HIGH: "beacon", MED: "warn", LOW: "muted" };
const kindLabel = (k) => KIND_LABEL[k] || (k || "").replace(/_/g, " ");
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

// A signed $ distance: + means the zone sits BELOW price (discount), − ABOVE.
const signDist = (d) => (d == null ? "—" : `${d > 0 ? "+" : ""}${Number(d).toFixed(2)}`);

function ZoneCard({ title, accent, zones }) {
  return (
    <Card>
      <div className={`px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2`}>
        <span className={`w-2 h-2 rounded-full ${accent === "buy" ? "bg-long" : "bg-short"}`} />
        {title}
        <span className="text-[11px] text-muted font-normal">
          {accent === "buy" ? "discount · below price" : "premium · above price"}
        </span>
      </div>
      {!zones?.length ? <Empty>No zones this side.</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[420px]">
            <thead><tr className="border-b border-edge">
              <Th right>#</Th><Th>Key-level range ($)</Th><Th>Strength</Th>
              <Th right>Distance ($)</Th><Th>Status</Th>
            </tr></thead>
            <tbody>
              {zones.map((z, i) => (
                <tr key={z.rank ?? i} className="border-b border-edge/60">
                  <Td right mono>{i + 1}</Td>
                  <Td mono>{fmt(z.band?.[0])} – {fmt(z.band?.[1])}</Td>
                  <Td><Badge tone={STRENGTH_TONE[z.strength] || "muted"}>{z.strength}</Badge></Td>
                  <Td right mono><span className={accent === "buy" ? "text-long" : "text-short"}>{signDist(z.distance)}</span></Td>
                  <Td><Badge tone={z.status === "Filled" ? "muted" : "beacon"}>{z.status}</Badge></Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function ConfluenceZonePanel({ kind = "fvg", symbol = "XAUUSD", price = null }) {
  const [data, setData] = useState(null);
  const [tf, setTf] = useState("all");     // "all" = aggregated across timeframes
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.analyticsMagnets(symbol, kind, price)
      .then(d => alive && setData(d)).catch(e => alive && setErr(e.message));
    return () => { alive = false; };
  }, [symbol, kind, price]);

  const lbl = kindLabel(kind);
  // Aggregate (all-TF) vs a single timeframe's slice — same shape either way.
  const slice = tf === "all"
    ? { buy_side: data?.buy_side || [], sell_side: data?.sell_side || [] }
    : (data?.per_tf?.[tf] || { buy_side: [], sell_side: [] });
  const tfs = data?.timeframes || [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">
          {symbol} · {lbl} magnets{data?.reference_price != null ? ` · ref ${fmt(data.reference_price)}` : ""}
        </span>
        <div className="flex-1" />
        <div className="flex flex-wrap items-center gap-1">
          <button onClick={() => setTf("all")}
            className={`px-2 py-1 rounded-lg text-xs ${tf === "all" ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
            All TF
          </button>
          {tfs.map(t => (
            <button key={t} onClick={() => setTf(t)}
              className={`px-2 py-1 rounded-lg text-xs ${tf === t ? "bg-beacon/15 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
              {TF_LABEL[t] || t}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="text-xs text-short bg-short/10 rounded-lg px-3 py-2">{err}</div>}
      {!data ? <Empty>Loading…</Empty>
        : data.version_id == null ? <Empty>No structure map yet — run a recompute on the Structure tab.</Empty>
        : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <ZoneCard title={`BUY-Side ${lbl}`} accent="buy" zones={slice.buy_side} />
            <ZoneCard title={`SELL-Side ${lbl}`} accent="sell" zones={slice.sell_side} />
          </div>
        )}
    </div>
  );
}
