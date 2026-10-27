"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { boundsHint, fieldProblem, type FieldSpec } from "@/lib/bots";

/**
 * Inputs for a list of settings, drawn from the engine's description of a pydantic
 * model (D47): a strategy's params, a sizing mode's or a stop's. Each shows its
 * description and bounds, and says what's wrong as you type; the engine checks again.
 * Values stay strings, so a decimal never passes through a float.
 */
export function StrategyParamsForm({
  fields,
  values,
  onChange,
  idPrefix,
}: {
  fields: FieldSpec[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  idPrefix: string;
}) {
  if (fields.length === 0) {
    return <p className="text-muted-foreground text-sm">Nothing to set.</p>;
  }
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {fields.map((spec) => {
        const id = `${idPrefix}-${spec.name}`;
        const value = values[spec.name] ?? "";
        const problem = fieldProblem(spec, value);
        const hint = [spec.description, boundsHint(spec)].filter(Boolean).join(" · ");
        return (
          <div key={spec.name} className="flex flex-col gap-1.5">
            <Label htmlFor={id}>{spec.name}</Label>
            <Input
              id={id}
              name={spec.name}
              inputMode={spec.type === "integer" ? "numeric" : "decimal"}
              value={value}
              aria-invalid={problem !== null}
              aria-describedby={`${id}-hint`}
              onChange={(event) => onChange({ ...values, [spec.name]: event.target.value })}
              className="font-mono"
            />
            <p id={`${id}-hint`} className="text-muted-foreground text-xs">
              {problem === null ? hint : <span className="text-loss">{problem}</span>}
              {problem !== null && hint && <> · {hint}</>}
            </p>
          </div>
        );
      })}
    </div>
  );
}
