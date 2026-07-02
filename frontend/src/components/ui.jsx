export function Card({ children, className = "" }) {
  return <div className={`card ${className}`}>{children}</div>;
}

const GRAD = { a: "grad-a", b: "grad-b", c: "grad-c", d: "grad-d" };

export function KPI({ label, value, sub, grad = "a", icon: Icon }) {
  return (
    <div className="card p-4 relative overflow-hidden">
      <div className={`absolute -right-6 -top-6 w-24 h-24 rounded-full opacity-20 ${GRAD[grad]}`} />
      <div className="flex items-center justify-between">
        <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
        {Icon && <span className={`w-7 h-7 rounded-lg grid place-items-center text-white ${GRAD[grad]}`}>
          <Icon className="w-4 h-4" /></span>}
      </div>
      <div className="mt-2 num text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  );
}

export function Badge({ children, tone = "muted", dot }) {
  const map = {
    long: "bg-long/15 text-long", short: "bg-short/15 text-short",
    beacon: "bg-beacon/15 text-beacon", warn: "bg-warn/15 text-warn",
    violet: "bg-violet/15", muted: "bg-panel2 text-muted",
  };
  const dotc = { long: "bg-long", short: "bg-short", beacon: "bg-beacon", warn: "bg-warn", muted: "bg-muted" }[tone];
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${map[tone] || map.muted}`}>
      {dot && <span className={`w-1.5 h-1.5 rounded-full ${dotc}`} />}{children}
    </span>
  );
}

export function Table({ children }) {
  return <div className="overflow-x-auto"><table className="w-full border-separate border-spacing-0">{children}</table></div>;
}
export function Th({ children, right }) {
  return <th className={`px-4 py-2.5 text-[11px] uppercase tracking-wider text-muted font-medium border-b border-edge ${right ? "text-right" : "text-left"}`}>{children}</th>;
}
export function Td({ children, right, mono }) {
  return <td className={`px-4 py-2.5 text-sm border-b border-edge/50 ${right ? "text-right" : ""} ${mono ? "num" : ""}`}>{children}</td>;
}
export function Empty({ children }) {
  return <div className="p-10 text-center text-sm text-muted">{children}</div>;
}
