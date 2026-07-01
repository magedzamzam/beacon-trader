import { X } from "lucide-react";

export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50"
         onClick={onClose}>
      <div onClick={e => e.stopPropagation()}
           className={`bg-panel border border-edge rounded-xl shadow-panel w-full ${wide ? "max-w-2xl" : "max-w-lg"} max-h-[88vh] overflow-auto`}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-edge sticky top-0 bg-panel">
          <div className="font-medium">{title}</div>
          <button onClick={onClose} className="p-1 rounded-md text-muted hover:text-ink hover:bg-panel2">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-5 space-y-4">{children}</div>
      </div>
    </div>
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
