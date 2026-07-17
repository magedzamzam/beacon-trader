import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Info } from "lucide-react";
import { byId } from "../lib/glossary";

/**
 * HelpHint (#105) — the ⓘ next to any non-obvious field. Hover/focus (or tap)
 * opens a card explaining it in plain language:
 *   what it is · how to read it · what it does NOT mean · when to act (+ guardrail)
 *
 * Copy comes from lib/glossary.js so the wording stays sourced from the
 * implementation and is written once.
 *
 * Rendered through a PORTAL to <body> with fixed positioning: these sit inside
 * table headers and `Table` wraps its content in `overflow-x-auto`, which would
 * clip a normally-positioned popover. The portal escapes that clipping context
 * entirely. Position is clamped to the viewport and flips above the icon when
 * there isn't room below.
 */
const W = 340;          // popover width (px)
const H_EST = 260;      // rough height used to decide flip-above

export default function HelpHint({ term, className = "" }) {
  const btn = useRef(null);
  const [pos, setPos] = useState(null);          // null = closed
  const g = byId(term);

  const close = () => setPos(null);
  const open = () => {
    const r = btn.current?.getBoundingClientRect();
    if (!r) return;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - W / 2), window.innerWidth - W - 8);
    const roomBelow = window.innerHeight - r.bottom > H_EST;
    setPos(roomBelow
      ? { left, top: r.bottom + 6 }
      : { left, bottom: window.innerHeight - r.top + 6 });
  };

  // Close on Escape, and on any scroll/resize (a fixed popover would otherwise
  // detach from its icon).
  useEffect(() => {
    if (!pos) return;
    const onKey = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [pos]);

  if (!g) return null;                            // unknown id -> render nothing

  return (
    <>
      <button
        ref={btn} type="button"
        onMouseEnter={open} onMouseLeave={close}
        onFocus={open} onBlur={close}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); pos ? close() : open(); }}
        aria-label={`What is ${g.term}?`} aria-expanded={!!pos}
        className={`text-muted hover:text-beacon transition align-middle ml-1 inline-flex ${className}`}
      >
        <Info className="w-3.5 h-3.5" />
      </button>

      {pos && createPortal(
        <div role="tooltip"
          style={{ position: "fixed", left: pos.left, top: pos.top, bottom: pos.bottom, width: W }}
          className="z-[999] rounded-xl border border-edge bg-panel shadow-2xl p-3.5 text-left
                     normal-case tracking-normal font-normal pointer-events-none">
          <div className="text-sm font-medium text-ink">{g.term}</div>
          <div className="text-[11px] text-muted mt-1 leading-relaxed">{g.what}</div>
          <div className="mt-2 text-[11px] leading-relaxed">
            <span className="text-long font-medium">How to read it — </span>
            <span className="text-ink/90">{g.read}</span>
          </div>
          {g.not && (
            <div className="mt-1.5 text-[11px] leading-relaxed">
              <span className="text-warn font-medium">What it does NOT mean — </span>
              <span className="text-ink/90">{g.not}</span>
            </div>
          )}
          {g.act && (
            <div className="mt-1.5 text-[11px] leading-relaxed">
              <span className="text-beacon font-medium">When to act — </span>
              <span className="text-ink/90">{g.act}</span>
            </div>
          )}
          {g.guard && (
            <div className="mt-2.5 rounded-lg border border-warn/30 bg-warn/5 px-2 py-1.5
                            text-[10px] text-warn leading-relaxed">
              {g.guard}
            </div>
          )}
        </div>, document.body)}
    </>
  );
}
