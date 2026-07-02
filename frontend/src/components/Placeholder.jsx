import { Construction } from "lucide-react";
import { Card, Badge } from "./ui";

/**
 * Placeholder — renders a "planned but not yet implemented" configuration panel.
 *
 * Used across the Configuration page to preview enterprise features that will be
 * wired up later. Every placeholder here is documented in frontend/docs/CONFIGURATION.md
 * with the backend/API work required to make it real. These panels are intentionally
 * non-functional — they exist so the platform's final shape is visible today.
 */
export default function Placeholder({ icon: Icon = Construction, title, description, planned = [], note }) {
  return (
    <Card className="p-5 sm:p-6">
      <div className="flex items-start gap-4">
        <span className="w-11 h-11 rounded-xl grad-d grid place-items-center text-white shrink-0">
          <Icon className="w-5 h-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-base font-semibold">{title}</h3>
            <Badge tone="warn">Planned</Badge>
          </div>
          <p className="text-sm text-muted mt-1 max-w-2xl">{description}</p>

          {planned.length > 0 && (
            <div className="mt-4">
              <div className="text-[11px] uppercase tracking-wider text-muted mb-2">Planned capabilities</div>
              <ul className="grid sm:grid-cols-2 gap-2">
                {planned.map((p, i) => (
                  <li key={i}
                    className="flex items-start gap-2 text-sm bg-panel2 border border-edge rounded-lg px-3 py-2">
                    <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-beacon shrink-0" />
                    <span>{p}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-4 text-[11px] text-muted border-t border-edge pt-3">
            {note || "Not yet wired to the backend — documented in docs/CONFIGURATION.md for implementation."}
          </div>
        </div>
      </div>
    </Card>
  );
}
