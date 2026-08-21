import { X } from "lucide-react";
import { createPortal } from "react-dom";

const MODAL_WIDTH = {
  lg: "max-w-lg", xl: "max-w-2xl", "3xl": "max-w-3xl",
  "4xl": "max-w-4xl", "5xl": "max-w-5xl",
};

export function Modal({ title, onClose, children, wide, size }) {
  // Fixed header + independently scrolling body: the title stays pinned no
  // matter how tall the content is. `size` picks a max width; `wide` is the
  // legacy shortcut for the medium width.
  const width = MODAL_WIDTH[size] || (wide ? "max-w-2xl" : "max-w-lg");
  // Portal to <body> so the fixed overlay is positioned against the viewport,
  // not trapped/clipped by an ancestor that creates a containing block for
  // fixed elements (e.g. the .card `backdrop-filter: blur` on the History page).
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50"
         onClick={onClose}>
      <div onClick={e => e.stopPropagation()}
           className={`bg-panel border border-edge rounded-xl shadow-panel w-full ${width} max-h-[88vh] flex flex-col overflow-hidden`}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-edge shrink-0">
          <div className="font-medium">{title}</div>
          <button onClick={onClose} className="p-1 rounded-md text-muted hover:text-ink hover:bg-panel2">
            <X className="w-4 h-4" />
          </button>
        </div>
        {/* flex-1 + min-h-0 lets this body take the remaining height and scroll,
            so the card stays capped at max-h and the header never overflows. */}
        <div className="p-5 space-y-4 overflow-y-auto flex-1 min-h-0">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function Field({ label, hint, children }) {
  return (
    <label className="block">
      <div className="text-xs uppercase tracking-wider text-muted mb-1.5">{label}</div>
      {children}
      {hint && <div className="text-[11px] text-muted mt-1">{hint}</div>}
    </label>
  );
}

const inputCls = "w-full bg-panel2 border border-edge rounded-lg px-3 py-2 text-sm text-ink focus:border-beacon outline-none";

export function Input({ mono, ...p }) {
  return <input {...p} className={`${inputCls} ${mono ? "num" : ""}`} />;
}
export function NumberInput(p) {
  return <input type="number" step="any" {...p} className={`${inputCls} num`} />;
}
export function Select({ children, ...p }) {
  return <select {...p} className={inputCls}>{children}</select>;
}

export function Toggle({ checked, onChange, label }) {
  return (
    <button type="button" onClick={() => onChange(!checked)}
      className="flex items-center gap-2.5 text-sm">
      <span className={`w-9 h-5 rounded-full transition relative ${checked ? "bg-beacon" : "bg-edge"}`}>
        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${checked ? "left-4.5" : "left-0.5"}`}
              style={{ left: checked ? "18px" : "2px" }} />
      </span>
      {label && <span className="text-muted">{label}</span>}
    </button>
  );
}


/**
 * ConfigRow — one setting, collapsed until you turn it on.
 *
 * The Strategies pillars used to render every field they support, all the time,
 * so a page that configures six things looked identical to one that configures
 * none and you could not tell which values were actually yours. Off here means
 * NOT SET, which is what makes the #104 cascade real: a pillar key this strategy
 * leaves off inherits from the next-less-specific row instead of being written
 * over with a copy of the default.
 *
 * `onChange(next)` is called with the new on/off state; the caller decides what
 * turning it off clears.
 */
export function ConfigRow({ label, hint, active, onChange, summary, children }) {
  return (
    <div className={`rounded-lg border transition-colors ${active ? "border-beacon/40 bg-beacon/[0.03]" : "border-edge bg-panel2/30"}`}>
      <div className="flex items-start gap-3 px-3 py-2.5">
        <div className="pt-0.5"><Toggle checked={active} onChange={onChange} /></div>
        <div className="min-w-0 flex-1">
          <div className="text-sm">{label}</div>
          {hint && <div className="text-[11px] text-muted mt-0.5">{hint}</div>}
        </div>
        {!active && summary && (
          <div className="text-[11px] text-muted shrink-0 pt-0.5">{summary}</div>
        )}
      </div>
      {active && (
        <div className="px-3 pb-3 pt-1 border-t border-edge/50">{children}</div>
      )}
    </div>
  );
}


export function Button({ variant = "primary", children, ...p }) {
  const v = {
    primary: "bg-beacon/15 text-beacon hover:bg-beacon/25",
    ghost: "text-muted hover:text-ink hover:bg-panel2",
    danger: "text-short hover:bg-short/10",
  }[variant];
  return (
    <button {...p} className={`px-3 py-1.5 rounded-lg text-sm font-medium transition ${v} ${p.className || ""}`}>
      {children}
    </button>
  );
}

export function ErrorNote({ children }) {
  if (!children) return null;
  return <div className="text-xs text-short bg-short/10 rounded-lg px-3 py-2">{children}</div>;
}
