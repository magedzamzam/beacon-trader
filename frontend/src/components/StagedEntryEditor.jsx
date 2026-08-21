import { Field, Input } from "./form";

/**
 * StagedEntryEditor (#129, cut down by #250) — the two order-age brakes that sit
 * alongside the ladder. Rendered only when staged entry is switched on.
 *
 * This used to be fifteen fields and a live "tranche map" that re-implemented the
 * partition rule in JavaScript, with a comment asking whoever touched it to keep
 * the two copies in step by hand. Thirteen of those settings had never been
 * changed from their defaults by anyone, live or in replay, so #250 deleted them
 * along with the toe-in / runner / reclaim engine that read them. The ladder
 * table above is the configuration now, and there is no second copy of anything
 * to drift.
 *
 * The two fields that remain are not tuning numbers. They are the #158 brakes:
 * both OFF by default, and they exist because a rung deploys late by design and
 * its legs start a fresh TTL clock at that moment, so a staged order can outlive
 * the control account's by the whole pending wait.
 */
export default function StagedEntryEditor({ value, onChange }) {
  const v = value || {};
  const setNum = (k) => (e) => onChange(k, e.target.value);
  return (
    <div className="rounded-lg border border-edge p-3 space-y-2 bg-panel2/40">
      <div className="text-xs uppercase tracking-wider text-muted">
        Order-age limits (minutes) — both off by default
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Deployed rung TTL"
               hint="how long a late-deployed rung may rest · 0 = inherit the entry TTL">
          <Input type="number" min="0" value={v.deployed_ttl_minutes ?? ""} onChange={setNum("deployed_ttl_minutes")} />
        </Field>
        <Field label="Max entry age"
               hint="hard ceiling measured from the signal · 0 = off">
          <Input type="number" min="0" value={v.max_entry_age_minutes ?? ""} onChange={setNum("max_entry_age_minutes")} />
        </Field>
      </div>
      <p className="text-[11px] text-muted">
        A rung deploys late by design, so its legs get a fresh TTL clock from that moment — an
        unfilled rung can otherwise outlive the control account's order by the whole wait. These
        bound that window; leaving both at 0 keeps the behaviour #129 shipped with.
      </p>
    </div>
  );
}
