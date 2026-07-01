import { useEffect, useState } from "react";
export function useData(fn, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    fn().then(d => alive && setData(d)).catch(e => alive && setError(e.message));
    return () => { alive = false; };
    // eslint-disable-next-line
  }, deps);
  return { data, error };
}
export const money = (n) => (n == null ? "—" :
  (n >= 0 ? "+" : "") + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
export const tone = (n) => (n == null ? "ink" : n > 0 ? "long" : n < 0 ? "short" : "ink");
