export function Card({ children, className = "" }) {
  return (
    <div className={`bg-panel border border-edge rounded-xl shadow-panel ${className}`}>
      {children}
    </div>
  );
}

export function KPI({ label, value, sub, tone = "ink" }) {
  const toneClass = { ink: "text-ink", long: "text-long", short: "text-short",
    beacon: "text-beacon" }[tone] || "text-ink";
  return (
    <Card className="p-4">
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-2 num text-2xl font-semibold ${toneClass}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </Card>
  );
}

export function Badge({ children, tone = "muted" }) {
  const map = {
    long: "bg-long/15 text-long", short: "bg-short/15 text-short",
    beacon: "bg-beacon/15 text-beacon", warn: "bg-warn/15 text-warn",
    muted: "bg-panel2 text-muted",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${map[tone] || map.muted}`}>
      {children}
    </span>
  );
}

export function Th({ children, right }) {
  return <th className={`px-3 py-2 text-xs uppercase tracking-wider text-muted font-medium ${right ? "text-right" : "text-left"}`}>{children}</th>;
}
export function Td({ children, right, mono }) {
  return <td className={`px-3 py-2 text-sm ${right ? "text-right" : ""} ${mono ? "num" : ""}`}>{children}</td>;
}

export function Empty({ children }) {
  return <div className="p-8 text-center text-sm text-muted">{children}</div>;
}
