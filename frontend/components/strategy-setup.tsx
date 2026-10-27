"use client";

import type { ReactNode } from "react";

import { StrategyParamsForm } from "@/components/strategy-params-form";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import {
  STOP_SLOTS,
  fieldProblem,
  initialValues,
  toSettings,
  type Catalog,
  type Choice,
  type FieldSpec,
  type Settings,
} from "@/lib/bots";

type Values = Record<string, string>;
type SlotKey = (typeof STOP_SLOTS)[number]["key"];

/** What the inputs hold: every value a string, as typed. */
export type Setup = {
  strategy: string;
  params: Values;
  sizingMode: string;
  sizing: Values;
  stopKinds: Record<SlotKey, string>; // "" for none
  stopValues: Record<SlotKey, Values>;
  stopsShared: Values; // e.g. atr_period, shown when an ATR stop is chosen
};

/** The strategy, sizing and stops as the engine takes them (bots.sizing, .stops). */
export type SetupPayload = {
  strategy: string;
  params: Settings;
  sizing: Settings;
  stops: Record<string, Settings | number>;
};

function choice(choices: Choice[], name: string): Choice | undefined {
  return choices.find((c) => c.name === name);
}

/** Every value shown is valid (the hints under each input say what's wrong). */
export function allValid(fields: FieldSpec[], values: Values): boolean {
  return fields.every((f) => fieldProblem(f, values[f.name] ?? "") === null);
}

/** The first strategy with its defaults, all-in, no stops. */
export function initialSetup(catalog: Catalog): Setup {
  const first = catalog.strategies[0];
  return {
    strategy: first?.name ?? "",
    params: first ? initialValues(first.fields) : {},
    sizingMode: "all_in",
    sizing: initialValues(choice(catalog.sizing, "all_in")?.fields ?? []),
    stopKinds: { stop_loss: "", take_profit: "", trailing: "" },
    stopValues: { stop_loss: {}, take_profit: {}, trailing: {} },
    stopsShared: initialValues(catalog.stops_fields),
  };
}

function chosen(catalog: Catalog, setup: Setup) {
  return {
    strategy: choice(catalog.strategies, setup.strategy),
    sizing: choice(catalog.sizing, setup.sizingMode),
    stops: STOP_SLOTS.map(({ key }) => ({
      key,
      chosen:
        setup.stopKinds[key] === ""
          ? undefined
          : choice(catalog.stops[key] ?? [], setup.stopKinds[key]),
    })),
    usesAtr: Object.values(setup.stopKinds).includes("atr"),
  };
}

/** The payload, or null while anything shown is invalid. */
export function setupPayload(catalog: Catalog, setup: Setup): SetupPayload | null {
  const { strategy, sizing, stops, usesAtr } = chosen(catalog, setup);
  const valid =
    strategy !== undefined &&
    allValid(strategy.fields, setup.params) &&
    sizing !== undefined &&
    allValid(sizing.fields, setup.sizing) &&
    stops.every(({ key, chosen }) => !chosen || allValid(chosen.fields, setup.stopValues[key])) &&
    (!usesAtr || allValid(catalog.stops_fields, setup.stopsShared));
  if (!valid) return null;
  const stopSettings: Record<string, Settings | number> = {};
  for (const { key, chosen } of stops) {
    if (chosen) {
      stopSettings[key] = {
        kind: chosen.name,
        ...toSettings(chosen.fields, setup.stopValues[key]),
      };
    }
  }
  if (usesAtr) Object.assign(stopSettings, toSettings(catalog.stops_fields, setup.stopsShared));
  return {
    strategy: setup.strategy,
    params: toSettings(strategy.fields, setup.params),
    sizing: { mode: setup.sizingMode, ...toSettings(sizing.fields, setup.sizing) },
    stops: stopSettings,
  };
}

/**
 * The strategy and its params, the sizing and the stops, each drawn from the engine's
 * catalog (D47): for a new bot and for a backtest (D48), so both are set up alike.
 */
export function StrategySetup({
  catalog,
  setup,
  onChange,
  sizingNote,
}: {
  catalog: Catalog;
  setup: Setup;
  onChange: (setup: Setup) => void;
  sizingNote: ReactNode;
}) {
  const { strategy, sizing, stops, usesAtr } = chosen(catalog, setup);
  const change = (changes: Partial<Setup>) => onChange({ ...setup, ...changes });

  return (
    <>
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-sm font-semibold">Strategy</legend>
        <NativeSelect
          aria-label="Strategy"
          value={setup.strategy}
          onChange={(event) =>
            change({
              strategy: event.target.value,
              params: initialValues(choice(catalog.strategies, event.target.value)?.fields ?? []),
            })
          }
        >
          {catalog.strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.title}
            </option>
          ))}
        </NativeSelect>
        {strategy && (
          <StrategyParamsForm
            idPrefix="param"
            fields={strategy.fields}
            values={setup.params}
            onChange={(params) => change({ params })}
          />
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-sm font-semibold">Sizing</legend>
        <p className="text-muted-foreground -mt-2 text-xs">{sizingNote}</p>
        <NativeSelect
          aria-label="Sizing"
          value={setup.sizingMode}
          onChange={(event) =>
            change({
              sizingMode: event.target.value,
              sizing: initialValues(choice(catalog.sizing, event.target.value)?.fields ?? []),
            })
          }
        >
          {catalog.sizing.map((m) => (
            <option key={m.name} value={m.name}>
              {m.title}
            </option>
          ))}
        </NativeSelect>
        {sizing && sizing.fields.length > 0 && (
          <StrategyParamsForm
            idPrefix="sizing"
            fields={sizing.fields}
            values={setup.sizing}
            onChange={(values) => change({ sizing: values })}
          />
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-4">
        <legend className="mb-2 text-sm font-semibold">Stops</legend>
        {STOP_SLOTS.map(({ key, label, hint }) => {
          const stop = stops.find((s) => s.key === key)?.chosen;
          return (
            <div key={key} className="flex flex-col gap-2">
              <Label htmlFor={`stop-${key}`}>{label}</Label>
              <NativeSelect
                id={`stop-${key}`}
                value={setup.stopKinds[key]}
                aria-describedby={`stop-${key}-hint`}
                onChange={(event) => {
                  const kind = event.target.value;
                  const fields = choice(catalog.stops[key] ?? [], kind)?.fields ?? [];
                  change({
                    stopKinds: { ...setup.stopKinds, [key]: kind },
                    stopValues: { ...setup.stopValues, [key]: initialValues(fields) },
                  });
                }}
              >
                <option value="">None</option>
                {(catalog.stops[key] ?? []).map((k) => (
                  <option key={k.name} value={k.name}>
                    {k.title}
                  </option>
                ))}
              </NativeSelect>
              <p id={`stop-${key}-hint`} className="text-muted-foreground text-xs">
                {hint}
              </p>
              {stop && (
                <StrategyParamsForm
                  idPrefix={`stop-${key}`}
                  fields={stop.fields}
                  values={setup.stopValues[key]}
                  onChange={(values) =>
                    change({ stopValues: { ...setup.stopValues, [key]: values } })
                  }
                />
              )}
            </div>
          );
        })}
        {usesAtr && (
          <StrategyParamsForm
            idPrefix="stops"
            fields={catalog.stops_fields}
            values={setup.stopsShared}
            onChange={(stopsShared) => change({ stopsShared })}
          />
        )}
      </fieldset>
    </>
  );
}
