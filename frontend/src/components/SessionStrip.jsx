import { useEffect, useState } from "react";
import { api } from "../lib/api";
import SessionTimeline from "./SessionTimeline";
import NewsCard from "./NewsCard";

/**
 * SessionStrip — dashboard trading-hours widget: a session timeline card plus a
 * news card. Fetches the status once and polls every 30s.
 */
export default function SessionStrip() {
  const [s, setS] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.tradingHoursStatus().then(x => alive && setS(x)).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!s) return null;
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div className="lg:col-span-2"><SessionTimeline status={s} /></div>
      <NewsCard status={s} />
    </div>
  );
}
