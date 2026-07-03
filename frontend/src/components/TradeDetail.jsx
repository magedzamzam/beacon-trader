import { useEffect, useState } from "react";
import { Modal } from "./form";
import { Badge, Th, Td, Empty } from "./ui";
import { api } from "../lib/api";
import { money, tone } from "../pages/_useData";

/**
 * TradeDetail — a modal that shows one trade's legs, the broker's authoritative
 * activity timeline (from the position_activities audit: fills, SL/TP edits,
 * SL/TP/user closes with exact P&L), and the internal execution events. Lets you
 * eyeball what actually happened to each position without querying the API.
 */
const SRC_TONE = { SL: "short", TP: "long", PROFIT: "long", USER: "warn", SYSTEM: "muted" };
const EVENT_TONE = {
  placed: "beacon", filled: "long", closed: "muted", sl_moved: "long",
  reject: "short", cancelled_at_broker: "warn", expired: "warn",
  ai_blocked: "short", fx_unavailable: "short",
  closed_by_user: "muted", cancelled_by_user: "warn", sl_moved_by_user: "long",
};
const statusTone = (s) => ({ open: "beacon", working: "warn", pending: "muted",
  closed: "muted", cancelled: "muted", expired: "muted", rejected: "short" }[s] || "muted");
const outcomeTone = (o) => o === "tp_hit" ? "long" : o === "sl_hit" ? "short"
  : o === "breakeven" ? "warn" : "muted";
const shortId = (id) => id ? `…${String(id).slice(-8)}` : "—";
const when = (s) => (s || "").slice(0, 19).replace("T", " ");

function Section({ title, children }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5">{title}</div>
      <div className="border border-edge rounded-lg overflow-x-auto">{children}</div>
    </div>
  );
}

const OPEN_STATUS = new Set(["open", "working", "pending"]);

/** Roll a trade's legs up into per-outcome counts + realized P&L. */
function summarize(legs) {
  const g = { tp: { n: 0, pl: 0 }, sl: { n: 0, pl: 0 },
              be: { n: 0, pl: 0 }, other: { n: 0, pl: 0 }, open: 0 };
  for (const l of legs || []) {
    if (OPEN_STATUS.has(l.status)) { g.open++; continue; }
    const pl = l.realized_pl != null ? Number(l.realized_pl) : 0;
    if (l.outcome === "tp_hit") { g.tp.n++; g.tp.pl += pl; }
    else if (l.outcome === "sl_hit") { g.sl.n++; g.sl.pl += pl; }
    else if (l.outcome === "breakeven") { g.be.n++; g.be.pl += pl; }
    else { g.other.n++; g.other.pl += pl; }
  }
  return g;
}

const DOT = { long: "bg-long", short: "bg-short", warn: "bg-warn", beacon: "bg-beacon" };

function Stat({ label, tone: t, n, pl }) {
  return (
    <div className="bg-panel2 border border-edge rounded-lg px-3 py-2">
      <div className="flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${DOT[t] || "bg-muted"}`} />
        <span className="text-[10px] uppercase tracking-wider text-muted">{label}</span>
      </div>
      <div className="flex items-baseline gap-2 mt-0.5">
        <span className="num text-lg font-semibold leading-none">{n}</span>
        {pl != null && <span className={`num text-xs text-${tone(pl)}`}>{money(pl)}</span>}
      </div>
    </div>
  );
}

export default function TradeDetail({ tradeId, onClose }) {
  const [t, setT] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    setT(null); setErr(null);
    api.tradeDetail(tradeId).then(d => alive && setT(d)).catch(e => alive && setErr(e.message));
    return () => { alive = false; };
  }, [tradeId]);

  return (
    <Modal title={`Trade #${tradeId}`} onClose={onClose} wide>
      {err && <div className="text-xs text-short bg-short/10 rounded-lg px-3 py-2">{err}</div>}
      {!t ? <Empty>Loading…</Empty> : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{t.symbol}</span>
            <Badge dot tone={t.direction === "BUY" ? "long" : "short"}>{t.direction}</Badge>
            <Badge tone={t.status === "closed" ? "muted" : "beacon"}>{t.status}</Badge>
            <span className="ml-auto num text-xs text-muted">Realized P&L{" "}
              <span className={`text-${tone(t.realized_pl)} font-semibold`}>{money(t.realized_pl)}</span>
            </span>
          </div>

          {(() => {
            const s = summarize(t.legs);
            return (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                <Stat label="TP hits" tone="long" n={s.tp.n} pl={s.tp.pl} />
                <Stat label="SL hits" tone="short" n={s.sl.n} pl={s.sl.pl} />
                <Stat label="Break-even" tone="warn" n={s.be.n} pl={s.be.pl} />
                <Stat label="Open" tone="beacon" n={s.open} pl={null} />
              </div>
            );
          })()}

          <Section title="Legs">
            <table className="w-full">
              <thead><tr>
                <Th right>TP#</Th><Th>Type</Th><Th right>Entry</Th><Th right>TP</Th><Th right>SL</Th>
                <Th>Status</Th><Th>Outcome</Th><Th right>P&L</Th>
              </tr></thead>
              <tbody>
                {t.legs.map(l => (
                  <tr key={l.id} className="border-b border-edge/50">
                    <Td right mono>{l.tp_index}</Td>
                    <Td><Badge tone={l.order_type === "MARKET" ? "warn" : "muted"}>{l.order_type}</Badge></Td>
                    <Td right mono>{Number(l.entry).toFixed(2)}</Td>
                    <Td right mono>{Number(l.tp).toFixed(2)}</Td>
                    <Td right mono>{Number(l.sl).toFixed(2)}{l.sl_moved &&
                      <span className="text-long text-[10px]"> ↑moved</span>}</Td>
                    <Td><Badge tone={statusTone(l.status)}>{l.status}</Badge></Td>
                    <Td>{l.outcome ? <Badge tone={outcomeTone(l.outcome)}>{l.outcome}</Badge> : "—"}</Td>
                    <Td right mono>{l.realized_pl != null
                      ? <span className={`text-${tone(l.realized_pl)}`}>{money(l.realized_pl)}</span> : "—"}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="Broker activity — truth">
            {!t.activities?.length ? <Empty>No broker activity recorded yet.</Empty> : (
              <table className="w-full">
                <thead><tr>
                  <Th>When</Th><Th>Type</Th><Th>Source</Th><Th>Status</Th><Th>Deal</Th><Th right>P&L</Th>
                </tr></thead>
                <tbody>
                  {t.activities.map(a => (
                    <tr key={a.id} className="border-b border-edge/50">
                      <Td mono>{when(a.at)}</Td>
                      <Td><span className="text-xs">{a.type || "—"}</span></Td>
                      <Td><Badge tone={SRC_TONE[(a.source || "").toUpperCase()] || "muted"}>{a.source || "—"}</Badge></Td>
                      <Td><span className="text-xs text-muted">{a.status || "—"}</span></Td>
                      <Td mono><span className="text-[11px] text-muted" title={a.deal_id || ""}>{shortId(a.deal_id)}</span></Td>
                      <Td right mono>{a.realized_pl != null
                        ? <span className={`text-${tone(a.realized_pl)}`}>{money(a.realized_pl)} {a.currency || ""}</span>
                        : "—"}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>

          <Section title="Execution events">
            {!t.events?.length ? <Empty>No events.</Empty> : (
              <table className="w-full">
                <thead><tr><Th>When</Th><Th>Event</Th><Th right>Leg</Th><Th>Detail</Th></tr></thead>
                <tbody>
                  {t.events.map(e => (
                    <tr key={e.id} className="border-b border-edge/50 align-top">
                      <Td mono>{when(e.ts)}</Td>
                      <Td><Badge tone={EVENT_TONE[e.kind] || "muted"}>{e.kind}</Badge></Td>
                      <Td right mono>{e.leg_id ? `#${e.leg_id}` : "—"}</Td>
                      <Td><code className="text-[11px] text-muted break-all">{JSON.stringify(e.payload)}</code></Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
        </div>
      )}
    </Modal>
  );
}
