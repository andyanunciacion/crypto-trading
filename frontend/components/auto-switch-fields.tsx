"use client";

import { allValid } from "@/components/strategy-setup";
import { StrategyParamsForm } from "@/components/strategy-params-form";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import {
  REGIMES,
  fieldProblem,
  initialValues,
  toSettings,
  type AutoSwitchSettings,
  type Catalog,
  type Choice,
  type FieldSpec,
  type MappedRegime,
} from "@/lib/bots";

/** What each regime starts as when auto-switching is first set up. */
const STARTING: Record<MappedRegime, string> = {
  trending_up: "ema_crossover",
  trending_down: "stay_flat",
  ranging: "rsi_mean_reversion",
  high_volatility: "stay_flat",
};

const MIN_HOLD: FieldSpec = {
  name: "min_hold",
  type: "integer",
  default: "3",
  minimum: "1",
  exclusive_minimum: false,
  maximum: "100",
  exclusive_maximum: false,
  description: null,
};

type Row = { strategy: string; values: Record<string, string> };

/** What the inputs hold: each regime's strategy and params, and the minimum hold, as typed. */
export type AutoSwitchInputs = { rows: Record<MappedRegime, Row>; minHold: string };

/** The inputs for `choice`'s params: `params`' values where it has them. */
function valuesFor(choice: Choice | undefined, params: Record<string, unknown> | undefined) {
  const fields = choice?.fields ?? [];
  const values = initialValues(fields);
  for (const field of fields) {
    const value = params?.[field.name];
    if (typeof value === "number" || typeof value === "string") values[field.name] = String(value);
  }
  return values;
}

/** `stored`'s mapping and hold, or the starting mapping (each with its defaults) and 3. */
export function initialAutoSwitch(
  catalog: Catalog,
  stored: AutoSwitchSettings | null,
): AutoSwitchInputs {
  const choose = (name: string) =>
    catalog.strategies.find((s) => s.name === name) ?? catalog.strategies[0];
  const rows = Object.fromEntries(
    REGIMES.map(({ value: regime }) => {
      const mapped = stored?.mapping[regime];
      const choice = choose(mapped?.strategy ?? STARTING[regime]);
      return [regime, { strategy: choice?.name ?? "", values: valuesFor(choice, mapped?.params) }];
    }),
  ) as Record<MappedRegime, Row>;
  return { rows, minHold: String(stored?.min_hold ?? MIN_HOLD.default) };
}

function fieldsOf(catalog: Catalog, row: Row): FieldSpec[] {
  return catalog.strategies.find((s) => s.name === row.strategy)?.fields ?? [];
}

/** The mapping and minimum hold as the engine takes them, or null while anything is invalid. */
export function autoSwitchPayload(
  catalog: Catalog,
  inputs: AutoSwitchInputs,
): Pick<AutoSwitchSettings, "mapping" | "min_hold"> | null {
  const valid =
    fieldProblem(MIN_HOLD, inputs.minHold) === null &&
    REGIMES.every(({ value }) => {
      const row = inputs.rows[value];
      return row.strategy !== "" && allValid(fieldsOf(catalog, row), row.values);
    });
  if (!valid) return null;
  return {
    mapping: Object.fromEntries(
      REGIMES.map(({ value }) => {
        const row = inputs.rows[value];
        return [
          value,
          { strategy: row.strategy, params: toSettings(fieldsOf(catalog, row), row.values) },
        ];
      }),
    ) as AutoSwitchSettings["mapping"],
    min_hold: Number(inputs.minHold.trim()),
  };
}

/**
 * Auto-switching's inputs (D54): a strategy for each regime, drawn from the engine's own
 * strategies, and how many closed candles in a row a new regime must read before a
 * switch. Shared by a bot's `AutoSwitchSheet` and the Backtests page's form (P6-5c), so
 * both are set up alike.
 */
export function AutoSwitchFields({
  idPrefix,
  catalog,
  inputs,
  onChange,
  timeframe,
  switcher,
}: {
  idPrefix: string;
  catalog: Catalog;
  inputs: AutoSwitchInputs;
  onChange: (inputs: AutoSwitchInputs) => void;
  timeframe: string;
  /** Who switches, for the hint: "the bot" or "the run". */
  switcher: string;
}) {
  const holdProblem = fieldProblem(MIN_HOLD, inputs.minHold);
  const holdId = `${idPrefix}-min-hold`;

  function setRow(regime: MappedRegime, row: Row) {
    onChange({ ...inputs, rows: { ...inputs.rows, [regime]: row } });
  }

  return (
    <>
      {REGIMES.map(({ value: regime, label, hint }) => {
        const row = inputs.rows[regime];
        const hintId = `${idPrefix}-${regime}-hint`;
        return (
          <fieldset key={regime} className="flex flex-col gap-3" aria-describedby={hintId}>
            <legend className="mb-2 text-sm font-semibold">{label}</legend>
            <p id={hintId} className="text-muted-foreground -mt-2 text-xs">
              {hint}
            </p>
            <NativeSelect
              aria-label={`Strategy when ${label.toLowerCase()}`}
              value={row.strategy}
              onChange={(event) => {
                const next = event.target.value;
                const choice = catalog.strategies.find((s) => s.name === next);
                setRow(regime, { strategy: next, values: valuesFor(choice, undefined) });
              }}
            >
              {catalog.strategies.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.title}
                </option>
              ))}
            </NativeSelect>
            <StrategyParamsForm
              idPrefix={`${idPrefix}-${regime}`}
              fields={fieldsOf(catalog, row)}
              values={row.values}
              onChange={(values) => setRow(regime, { ...row, values })}
            />
          </fieldset>
        );
      })}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={holdId}>Minimum hold, in candles</Label>
        <Input
          id={holdId}
          inputMode="numeric"
          value={inputs.minHold}
          aria-invalid={holdProblem !== null}
          aria-describedby={`${holdId}-hint`}
          onChange={(event) => onChange({ ...inputs, minHold: event.target.value })}
          className="w-32 font-mono"
        />
        <p id={`${holdId}-hint`} className="text-muted-foreground text-xs">
          {holdProblem !== null && <span className="text-loss">{holdProblem} · </span>}
          How many closed {timeframe} candles in a row a new regime must read before {switcher}{" "}
          switches (1–100). Longer is steadier, but later: the detector already sees a turn a few
          candles late.
        </p>
      </div>
    </>
  );
}
