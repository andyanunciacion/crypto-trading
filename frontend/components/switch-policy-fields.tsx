"use client";

import { SWITCH_POLICIES, type SwitchPolicy } from "@/lib/bots";

/**
 * What a switch does with the bot's open position (D51): three radios, each saying in a
 * line what happens to it, and whether the bot holds one now. Shared by Edit
 * (`EditStrategySheet`), Roll back (`RollbackDialog`), auto-switching (`AutoSwitchSheet`)
 * and an auto-switching backtest (`BacktestForm`), which says its own `note` instead.
 */
export function SwitchPolicyFields({
  idPrefix,
  policy,
  holding = false,
  note,
  onChange,
}: {
  idPrefix: string;
  policy: SwitchPolicy;
  holding?: boolean;
  note?: string;
  onChange: (policy: SwitchPolicy) => void;
}) {
  const noteId = `${idPrefix}-policy-note`;
  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={noteId}>
      <legend className="mb-2 text-sm font-semibold">Its open position</legend>
      <p id={noteId} className="text-muted-foreground -mt-2 text-xs">
        {note ??
          (holding
            ? "It holds a position now: this decides what happens to it."
            : "It holds no position now. The choice is kept for its next switch.")}
      </p>
      {SWITCH_POLICIES.map((p) => (
        <label key={p.value} className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name={`${idPrefix}-policy`}
            value={p.value}
            checked={policy === p.value}
            onChange={() => onChange(p.value)}
            className="accent-primary mt-0.5 size-4 shrink-0"
          />
          <span>
            <span className="font-medium">{p.label}</span>
            <span className="text-muted-foreground">: {p.hint}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}
