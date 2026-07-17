import { Info } from "lucide-react";
import { byId } from "../lib/glossary";

/**
 * HelpHint (#105) — an ⓘ next to a non-obvious metric.
 *   hover  -> the plain-language explanation (what it is + how to read it + the
 *             common misreading), as a native tooltip
 *   click  -> opens the full entry on the Help page (deep-linked + highlighted)
 *
 * Copy comes from lib/glossary.js — the SAME source the Help page renders, so a
 * tooltip can never drift from the glossary (or the implementation it was sourced
 * from). Unknown ids render nothing rather than a dangling icon.
 *
 * Deliberately a NATIVE `title` rather than a styled popover: these sit inside
 * table headers, and `Table` wraps its content in `overflow-x-auto`, which clips
 * absolutely-positioned bubbles. A native tooltip can't be clipped.
 */
export default function HelpHint({ term, onOpen }) {
  const g = byId(term);
  if (!g) return null;

  const tip = [
    g.term,
    "",
    g.what,
    "",
    `HOW TO READ: ${g.read}`,
    g.not ? `\nNOT: ${g.not}` : "",
    "\n(click for the full entry)",
  ].filter(Boolean).join("\n");

  const go = (e) => {
    e.stopPropagation();
    // Deep-link so the Help page scrolls to and highlights this entry.
    try { window.location.hash = `glossary-${g.id}`; } catch { /* noop */ }
    if (onOpen) onOpen();
  };

  return (
    <button type="button" onClick={go} title={tip} aria-label={`What is ${g.term}? Open the glossary.`}
      className="text-muted hover:text-beacon transition align-middle ml-1 inline-flex normal-case">
      <Info className="w-3.5 h-3.5" />
    </button>
  );
}
