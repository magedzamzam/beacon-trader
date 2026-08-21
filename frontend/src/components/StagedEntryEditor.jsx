import { Field, Input } from "./form";

/**
 * StagedEntryEditor (#129, cut down by #250) — what is left of the staged-entry
 * config sub-panel. Rendered only when entry_style === "staged".
 *
 *   EXECUTE(T1 toe-in) → MONITOR → DECIDE(T2 runner / reclaim) → EXECUTE
 *
 * This used to be fifteen fields and a live "tranche map" that re-implemented
 * beacon_core.execution.staging.partition_tps in JavaScript, with a comment
 * asking whoever touched it to keep the two copies in step by hand. Thirteen of
 * those settings had never been changed from their defaults by anyone, live or
 * in replay, so #250 froze them as constants in the engine. With the partition
 * fixed there is nothing for a preview to preview, and no second copy of the
 * partition rule to keep in sync.
 *
 * The two fields that remain are not tuning numbers. They are the #158 brakes:
 * both OFF by default, and they exist because a tranche deploys late by design
 * and its legs start a fresh TTL clock at that moment, so a staged order can
 * outlive the control account's by the pending wait.
 */
export default function StagedEntryEditor({ value, onChange }) {
  const v = value || {};
  const setNum = (k) => (e) => onChange(k, e.target.value);
  return (
    <div className="rounded-lg border border-beacon/30 p-4 space-y-4 bg-beacon/[0.03]">
      <p className="text-[11px] text-muted">
        <b className="text-beacon">Confirmation-staged entry.</b> Deploy the signal in tranches so a
        straight-to-SL move is caught on partial size. Same signal, same SL, same intended total
        risk — only <i>when/if</i> each leg deploys changes. The nearest TP deploys immediately, the
        farthest waits at the deep edge, and the middle of the ladder is deferred behind a
        break-then-reclaim. That shape is fixed: there is nothing to tune.
      </p>

      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-muted">Order-age limits (minutes) — both off by default</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Field label="Deployed leg TTL"
                 hint="how long a late-deployed leg may rest · 0 = inherit the entry TTL">
            <Input type="number" min="0" value={v.deployed_ttl_minutes ?? ""} onChange={setNum("deployed_ttl_minutes")} />
          </Field>
          <Field label="Max entry age"
                 hint="hard ceiling measured from the signal · 0 = off">
            <Input type="number" min="0" value={v.max_entry_age_minutes ?? ""} onChange={setNum("max_entry_age_minutes")} />
          </Field>
        </div>
        <p className="text-[11px] text-muted">
          A tranche deploys late by design, so its legs get a fresh TTL clock from that moment — an
          unfilled staged order can otherwise outlive the control account's by the whole pending
          wait. These bound that window; leaving both at 0 keeps the behaviour #129 shipped with.
        </p>
      </div>
    </div>
  );
}
