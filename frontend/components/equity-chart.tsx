"use client";

import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { LINE_SLOTS, chartTime, token } from "@/components/candle-chart";
import { useDarkTheme } from "@/components/theme-toggle";
import type { EquityPoint } from "@/lib/backtests";
import { formatDecimal, formatManila, shiftPoint, type Decimal } from "@/lib/format";

type Drawn = {
  chart: IChartApi;
  equity: ISeriesApi<"Line">;
  drawdown: ISeriesApi<"Area">;
};

/**
 * A backtest's equity curve (D48): equity at each candle's close in the top pane, with
 * the starting cash as a dashed line, and its drawdown (the fall from the highest equity
 * so far) hanging from zero in the pane below: two panes, each with its own scale, never
 * two scales on one. Times read Manila, as on the Market chart. Plotted as numbers; the
 * legend above shows the hovered candle's exact values, from the API's strings.
 */
export function EquityChart({
  points,
  initialCash,
  currency,
}: {
  points: EquityPoint[];
  initialCash: Decimal;
  currency: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const drawn = useRef<Drawn | null>(null);
  const dark = useDarkTheme();
  const [hoverTime, setHoverTime] = useState<number | null>(null);

  useEffect(() => {
    const element = container.current;
    if (element === null) return;
    const chart = createChart(element, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderVisible: false },
      crosshair: { mode: CrosshairMode.Magnet },
    });
    const equity = chart.addSeries(LineSeries, {
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    equity.createPriceLine({
      price: Number(initialCash),
      title: "start",
      lineStyle: LineStyle.Dashed,
      lineWidth: 1,
      axisLabelVisible: true,
    });
    const drawdown = chart.addSeries(
      AreaSeries,
      {
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: "custom", formatter: (value: number) => `${value.toFixed(1)}%` },
      },
      1,
    );
    const [equityPane, drawdownPane] = chart.panes();
    equityPane?.setStretchFactor(3);
    drawdownPane?.setStretchFactor(1);
    chart.subscribeCrosshairMove((param) => {
      setHoverTime(param.time === undefined ? null : Number(param.time));
    });
    drawn.current = { chart, equity, drawdown };
    return () => {
      drawn.current = null;
      chart.remove();
    };
    // The starting cash is fixed for a run; the page remounts for another.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const d = drawn.current;
    if (d === null) return;
    const grid = token("--border", dark ? 0.6 : 0.8);
    d.chart.applyOptions({
      layout: { textColor: token("--muted-foreground"), panes: { separatorColor: grid } },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    });
    d.equity.applyOptions({ color: (dark ? LINE_SLOTS.dark : LINE_SLOTS.light)[0] });
    for (const line of d.equity.priceLines()) {
      line.applyOptions({ color: token("--muted-foreground", 0.8) });
    }
    d.drawdown.applyOptions({
      lineColor: token("--loss"),
      topColor: token("--loss", 0.08),
      bottomColor: token("--loss", 0.3),
    });
  }, [dark]);

  useEffect(() => {
    const d = drawn.current;
    if (d === null) return;
    d.equity.setData(points.map((p) => ({ time: chartTime(p.ts), value: Number(p.equity) })));
    d.drawdown.setData(
      points.map((p) => ({ time: chartTime(p.ts), value: -Number(p.drawdown) * 100 })),
    );
    d.chart.timeScale().fitContent();
  }, [points]);

  const byTime = useMemo(() => new Map(points.map((p) => [Number(chartTime(p.ts)), p])), [points]);
  const shown = (hoverTime === null ? undefined : byTime.get(hoverTime)) ?? points.at(-1);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs" aria-live="off">
        {shown && (
          <span className="text-muted-foreground font-mono tabular-nums">
            <span className="font-sans">{formatManila(shown.ts)}</span> · equity{" "}
            <span className="text-foreground">
              {formatDecimal(shown.equity, { min: 2, max: 2 })}
            </span>{" "}
            <span className="font-sans">{currency}</span> · drawdown{" "}
            <span className="text-foreground">
              {formatDecimal(shiftPoint(shown.drawdown, 2), { min: 2, max: 2 })}%
            </span>
          </span>
        )}
      </div>
      <div
        ref={container}
        data-testid="equity-chart"
        role="img"
        aria-label={`Equity in ${currency} at each candle's close, from ${initialCash} ${currency}, with its drawdown below; the trades are listed in the table below`}
        className="h-[420px]"
      />
    </div>
  );
}
