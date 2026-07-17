import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, ShieldAlert, Search } from "lucide-react";
import { Card, Badge, Empty } from "../components/ui";
import { GLOSSARY, SECTIONS, GUARDRAILS } from "../lib/glossary";

/**
 * Help / Glossary (#105) — plain-language explanations for every non-obvious
 * field on the platform, so a statistic can't be misread into a bad trade.
 *
 * Content lives in lib/glossary.js (sourced from the implementation) and is shared
 * with the inline ⓘ hints, so the two can never disagree. Entries are anchored
 * (#glossary-<id>) so an ⓘ can deep-link straight to its term.
 */
export default function Help() {
  const [q, setQ] = useState("");
  const [highlight, setHighlight] = useState(null);
  const refs = useRef({});

  // Deep-link support: an ⓘ sets #glossary-<id>, we scroll to + flash that entry.
  useEffect(() => {
    let timer;
    const jump = () => {
      const id = (window.location.hash || "").replace(/^#glossary-/, "");
      if (!id) return;
      setQ("");                                   // never let a filter hide the target
      setHighlight(id);
      requestAnimationFrame(() => {
        refs.current[id]?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      clearTimeout(timer);
      timer = setTimeout(() => setHighlight(null), 2500);
    };
    jump();
    window.addEventListener("hashchange", jump);
    return () => { window.removeEventListener("hashchange", jump); clearTimeout(timer); };
  }, []);

  const needle = q.trim().toLowerCase();
  const matches = useMemo(() => GLOSSARY.filter((g) => !needle ||
    [g.term, g.what, g.read, g.not, g.act].filter(Boolean).join(" ").toLowerCase().includes(needle)), [needle]);
  const sections = SECTIONS.filter((s) => matches.some((g) => g.section === s.id));

  return (
    <div className="space-y-5">
      <Card>
        <div className="px-4 py-3 border-b border-edge flex items-center gap-2 flex-wrap">
          <BookOpen className="w-4 h-4 text-beacon" />
          <span className="text-sm font-medium">Help &amp; Glossary</span>
          <span className="text-[11px] text-muted">· what every field means, how to read it, and when to act</span>
          <div className="ml-auto relative">
            <Search className="w-3.5 h-3.5 text-muted absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search a term…"
              className="bg-panel2 border border-edge rounded-lg pl-8 pr-2.5 py-1.5 text-sm w-56 outline-none focus:border-beacon" />
          </div>
        </div>
        <div className="px-4 py-2 text-[11px] text-muted">
          Every entry answers the same four questions: <b>what it is</b> · <b>how to read it</b> ·
          <b> what it does NOT mean</b> · <b>when to act</b>. The wording is taken from the actual
          implementation, so it stays true to what the code does.
        </div>
      </Card>

      {/* The three rules that matter more than any single number */}
      <Card>
        <div className="px-4 py-3 border-b border-edge text-sm font-medium flex items-center gap-2">
          <ShieldAlert className="w-4 h-4 text-warn" /> Read these three first
        </div>
        <div className="p-4 grid grid-cols-1 lg:grid-cols-3 gap-3">
          {GUARDRAILS.map((g) => (
            <div key={g.title} className="rounded-lg border border-warn/30 bg-warn/5 p-3">
              <div className="text-xs font-medium text-warn">{g.title}</div>
              <div className="text-[11px] text-muted mt-1 leading-relaxed">{g.body}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Section jump-links */}
      {!needle && (
        <div className="flex flex-wrap gap-1.5">
          {SECTIONS.map((s) => (
            <a key={s.id} href={`#section-${s.id}`}
              className="text-[11px] px-2 py-1 rounded-full border border-edge text-muted hover:border-beacon hover:text-beacon">
              {s.title}
            </a>
          ))}
        </div>
      )}

      {!sections.length && <Card><Empty>No term matches “{q}”.</Empty></Card>}

      {sections.map((s) => (
        <Card key={s.id}>
          <div id={`section-${s.id}`} className="px-4 py-3 border-b border-edge scroll-mt-4">
            <div className="text-sm font-medium">{s.title}</div>
            <div className="text-[11px] text-muted mt-0.5">{s.blurb}</div>
          </div>
          <div className="divide-y divide-edge/60">
            {matches.filter((g) => g.section === s.id).map((g) => (
              <div key={g.id} id={`glossary-${g.id}`} ref={(el) => (refs.current[g.id] = el)}
                className={`px-4 py-3 scroll-mt-4 transition-colors ${highlight === g.id ? "bg-beacon/10" : ""}`}>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium">{g.term}</span>
                  {highlight === g.id && <Badge tone="beacon">linked</Badge>}
                </div>
                <dl className="mt-1.5 space-y-1 text-[11px] leading-relaxed">
                  <div><dt className="inline text-muted">What it is — </dt><dd className="inline">{g.what}</dd></div>
                  <div><dt className="inline text-long">How to read it — </dt><dd className="inline">{g.read}</dd></div>
                  {g.not && <div><dt className="inline text-warn">What it does NOT mean — </dt><dd className="inline">{g.not}</dd></div>}
                  {g.act && <div><dt className="inline text-beacon">When to act — </dt><dd className="inline">{g.act}</dd></div>}
                </dl>
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}
