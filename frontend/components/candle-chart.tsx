"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { useDarkTheme } from "@/components/theme-toggle";
import type { Live } from "@/components/use-live-chart";
import { formatDecimal } from "@/lib/format";
import {
  decimalsOf,
  switchLabel,
  tradeLabel,
  type Candle,
  type SwitchMarker,
  type TradeMarker,
} from "@/lib/market";

/**
 * Manila is UTC+8 all year (no daylight saving). The chart labels its axis in UTC, so
 * every time is shifted by 8 hours: the axis then reads Manila's clock (UX7), and day
 * ticks fall on Manila's midnights.
 */
const MANILA_OFFSET_S = 8 * 3600;

const STEP_S: Record<string, number> = {
  "1m": 60,
  "5m": 300,
  "15m": 900,
  "1h": 3600,
  "4h": 14400,
  "1d": 86400,
};

export function chartTime(iso: string): UTCTimestamp {
  return (Date.parse(iso) / 1000 + MANILA_OFFSET_S) as UTCTimestamp;
}

/** The open time of the candle `iso` falls in, as a chart time. */
function barTime(iso: string, timeframe: string): UTCTimestamp {
  const step = STEP_S[timeframe] ?? 60;
  const s = Date.parse(iso) / 1000;
  return (s - (s % step) + MANILA_OFFSET_S) as UTCTimestamp;
}

/**
 * Indicator line colors: the first two slots of the dataviz reference palette (blue,
 * orange), a pair validated for color-blind separation in both themes. Bollinger's upper
 * and lower bands are one line's two edges, so they share a color.
 */
export const LINE_SLOTS = { light: ["#2a78d6", "#eb6834"], dark: ["#3987e5", "#d95926"] };

/** How far from a marker's center a click still picks it: about the drawn arrow (size 1.5). */
const MARKER_HIT_PX = { x: 8, y: 12 };
const LINE_SLOT: Record<string, number> = {
  fast_ema: 0,
  slow_ema: 1,
  upper: 0,
  middle: 1,
  lower: 0,
  rsi: 0,
};

type Colors = {
  background: string;
  text: string;
  muted: string;
  grid: string;
  up: string;
  down: string;
  upForming: string;
  downForming: string;
  volume: string;
  marker: string;
  switchMarker: string;
  lines: string[];
};

let probe: CanvasRenderingContext2D | null = null;

/** A CSS color variable (an oklch() in globals.css) as rgba(), which the chart reads. */
export function token(name: string, alpha = 1): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  probe ??= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (probe === null) return value;
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = value;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${((a / 255) * alpha).toFixed(3)})`;
}

function readColors(dark: boolean): Colors {
  return {
    background: token("--background"),
    text: token("--muted-foreground"),
    muted: token("--muted-foreground", 0.8),
    grid: token("--border", dark ? 0.6 : 0.8),
    up: token("--profit"),
    down: token("--loss"),
    // The candle still forming is drawn faint: its prices aren't final.
    upForming: token("--profit", 0.4),
    downForming: token("--loss", 0.4),
    volume: token("--muted-foreground", 0.25),
    marker: token("--foreground"),
    switchMarker: token("--primary"),
    lines: dark ? LINE_SLOTS.dark : LINE_SLOTS.light,
  };
}

function bar(candle: Candle, colors: Colors | null, forming: boolean): CandlestickData<Time> {
  const point: CandlestickData<Time> = {
    time: chartTime(candle.ts),
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
  };
  if (forming && colors) {
    const color =
      Number(candle.close) >= Number(candle.open) ? colors.upForming : colors.downForming;
    return { ...point, color, wickColor: color, borderColor: color };
  }
  return point;
}

function volumeBar(candle: Candle, colors: Colors | null) {
  return { time: chartTime(candle.ts), value: Number(candle.volume), color: colors?.volume };
}

/**
 * The trades, at their price, and the strategy switches (P6-4), as squares above the
 * candle they were made in. A switch has no id or price, so a click never picks one:
 * the switch log on the bot's page is where each is explained.
 */
function markersOf(
  trades: TradeMarker[],
  switches: SwitchMarker[],
  timeframe: string,
  colors: Colors | null,
): SeriesMarker<Time>[] {
  const tradeMarkers = trades.map((trade): SeriesMarker<Time> => ({
    id: String(trade.order_id),
    time: barTime(trade.ts, timeframe),
    position: "atPriceMiddle",
    price: Number(trade.price),
    shape: trade.side === "buy" ? "arrowUp" : "arrowDown",
    color: colors?.marker ?? "#888",
    text: tradeLabel(trade),
    size: 1.5,
  }));
  const switchMarkers = switches.map((s): SeriesMarker<Time> => ({
    time: barTime(s.ts, timeframe),
    position: "aboveBar",
    shape: "square",
    color: colors?.switchMarker ?? "#888",
    text: switchLabel(s),
    size: 1,
  }));
  return [...tradeMarkers, ...switchMarkers].sort((a, b) => Number(a.time) - Number(b.time));
}

type Drawn = {
  chart: IChartApi;
  candles: ISeriesApi<"Candlestick">;
  volume: ISeriesApi<"Histogram">;
  lines: Map<string, ISeriesApi<"Line">>;
  markers: ISeriesMarkersPluginApi<Time>;
  /** How many of `live.candles` and `live.points` are drawn. */
  candleCount: number;
  pointCount: number;
  /** The forming candle drawn last, if any. */
  forming: Candle | null;
};

/** For the e2e test, which can't see into a canvas: where a trade's marker is drawn. */
export type MarkerProbe = HTMLDivElement & {
  markerPoint?: (orderId: number) => { x: number; y: number } | null;
};

type Hovered = { candle: Candle; values: Record<string, number>; forming: boolean };

/**
 * A bot's candles (Lightweight Charts, D46), with its strategy's indicators, volume and
 * trade markers, kept current from `live`. The newest candle may be the forming one,
 * drawn faint until the stored candle replaces it. Clicking a marker calls `onSelect`
 * with its order; the trades table under the chart does the same from the keyboard.
 * Above the chart, a legend shows the candle under the cursor (the newest otherwise)
 * with every line's value there.
 */
export function CandleChart({
  live,
  onSelect,
}: {
  live: Live;
  onSelect: (orderId: number) => void;
}) {
  const container = useRef<MarkerProbe>(null);
  const drawn = useRef<Drawn | null>(null);
  const colors = useRef<Colors | null>(null);
  const select = useRef(onSelect);
  useEffect(() => {
    select.current = onSelect;
  }, [onSelect]);
  const dark = useDarkTheme();
  const [hoverTime, setHoverTime] = useState<number | null>(null);
  const { bot, indicators } = live;
  const oscillator = indicators?.pane === "oscillator";

  // The price's precision, from how the exchange writes it (e.g. "81819.48" → 2).
  const precision = useMemo(
    () => Math.max(0, ...live.candles.slice(-50).map((c) => decimalsOf(c.close))),
    [live.candles],
  );

  // Create the chart once; the page remounts it for another bot.
  useEffect(() => {
    const element = container.current;
    if (element === null) return;
    const chart = createChart(element, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });
    const candles = chart.addSeries(CandlestickSeries, { priceLineVisible: false });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const lines = new Map<string, ISeriesApi<"Line">>();
    for (const name of indicators?.names ?? []) {
      const line = chart.addSeries(
        LineSeries,
        {
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        },
        indicators?.pane === "oscillator" ? 1 : 0,
      );
      lines.set(name, line);
    }
    const first = lines.values().next().value;
    if (indicators?.pane === "oscillator" && first) {
      for (const [title, price] of Object.entries(indicators.guides)) {
        first.createPriceLine({
          price,
          title,
          lineStyle: LineStyle.Dashed,
          lineWidth: 1,
          axisLabelVisible: true,
        });
      }
      // The price pane two thirds, the oscillator one third.
      const [price, oscillatorPane] = chart.panes();
      price?.setStretchFactor(2);
      oscillatorPane?.setStretchFactor(1);
    }
    const markers = createSeriesMarkers(candles, []);
    drawn.current = {
      chart,
      candles,
      volume,
      lines,
      markers,
      candleCount: 0,
      pointCount: 0,
      forming: null,
    };

    // Where a marker is drawn, in the price pane's pixels.
    const pointOf = (marker: SeriesMarker<Time>) => {
      if (marker.price === undefined) return null;
      const x = chart.timeScale().timeToCoordinate(marker.time);
      const y = candles.priceToCoordinate(marker.price);
      return x === null || y === null ? null : { x, y };
    };
    chart.subscribeClick((param: MouseEventParams<Time>) => {
      const hovered = param.hoveredInfo;
      if (hovered?.objectKind === "series-marker" && hovered.objectId !== undefined) {
        select.current(Number(hovered.objectId));
        return;
      }
      // Lightweight Charts hit-tests the series on top first, and the indicator lines
      // are above the candles: a click on a marker an EMA passes through reports the
      // line. So look for a marker under the click ourselves.
      if (param.point === undefined || (param.paneIndex ?? 0) !== 0) return;
      const { x, y } = param.point;
      let nearest: { id: string; distance: number } | null = null;
      for (const marker of markers.markers()) {
        const at = pointOf(marker);
        if (at === null || marker.id === undefined) continue;
        const dx = Math.abs(at.x - x);
        const dy = Math.abs(at.y - y);
        if (dx > MARKER_HIT_PX.x || dy > MARKER_HIT_PX.y) continue;
        const distance = Math.hypot(dx, dy);
        if (nearest === null || distance < nearest.distance) nearest = { id: marker.id, distance };
      }
      if (nearest !== null) select.current(Number(nearest.id));
    });
    chart.subscribeCrosshairMove((param) => {
      setHoverTime(param.time === undefined ? null : Number(param.time));
    });
    element.markerPoint = (orderId) => {
      const marker = markers.markers().find((m) => m.id === String(orderId));
      return marker === undefined ? null : pointOf(marker);
    };

    return () => {
      delete element.markerPoint;
      drawn.current = null;
      chart.remove();
    };
    // The layout of the lines is fixed for a bot; the page remounts for another.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The theme's colors, again whenever it's toggled.
  useEffect(() => {
    const d = drawn.current;
    if (d === null) return;
    const c = readColors(dark);
    colors.current = c;
    d.chart.applyOptions({
      layout: { textColor: c.text, panes: { separatorColor: c.grid } },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    });
    d.candles.applyOptions({
      upColor: c.up,
      downColor: c.down,
      wickUpColor: c.up,
      wickDownColor: c.down,
      borderVisible: false,
    });
    let i = 0;
    for (const [name, line] of d.lines) {
      line.applyOptions({ color: c.lines[LINE_SLOT[name] ?? i % c.lines.length] });
      for (const guide of line.priceLines()) guide.applyOptions({ color: c.muted });
      i += 1;
    }
    // Redrawn with the new colors: the volume bars, the forming candle and the markers.
    d.volume.setData(live.candles.slice(0, d.candleCount).map((candle) => volumeBar(candle, c)));
    if (d.forming) {
      d.candles.update(bar(d.forming, c, true));
      d.volume.update(volumeBar(d.forming, c));
    }
    d.markers.setMarkers(markersOf(live.trades, live.switches, bot.timeframe, c));
    // Only on a theme change; the data effect below keeps everything else current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dark]);

  useEffect(() => {
    drawn.current?.candles.applyOptions({
      priceFormat: { type: "price", precision, minMove: 10 ** -precision },
    });
  }, [precision]);

  // Draw what's new: closed candles and their points, the forming candle, the markers.
  useEffect(() => {
    const d = drawn.current;
    const element = container.current;
    if (d === null || element === null) return;
    const c = colors.current;
    const fresh = live.candles.slice(d.candleCount);
    // A candle can only be added after the newest bar. If the next candle was already
    // forming before this one was stored, draw them all again (once per close at most).
    const behind = d.forming !== null && fresh.some((candle) => candle.ts < d.forming!.ts);
    if (d.candleCount === 0 || behind) {
      d.candles.setData(live.candles.map((candle) => bar(candle, c, false)));
      d.volume.setData(live.candles.map((candle) => volumeBar(candle, c)));
      d.forming = null;
    } else {
      for (const candle of fresh) {
        d.candles.update(bar(candle, c, false));
        d.volume.update(volumeBar(candle, c));
      }
    }
    d.candleCount = live.candles.length;

    const newPoints = live.points.slice(d.pointCount);
    for (const [name, line] of d.lines) {
      const data = (d.pointCount === 0 ? live.points : newPoints)
        .filter((point) => name in point.values)
        .map((point) => ({ time: chartTime(point.ts), value: point.values[name] }));
      if (d.pointCount === 0) line.setData(data);
      else for (const point of data) line.update(point);
    }
    d.pointCount = live.points.length;

    const newest = live.candles.at(-1);
    const forming = live.forming;
    if (forming && (!newest || forming.ts > newest.ts)) {
      d.candles.update(bar(forming, c, true));
      d.volume.update(volumeBar(forming, c));
      d.forming = forming;
    }
    const last = d.forming ?? newest;
    // What the e2e test reads to see the chart move (a canvas can't be read).
    if (last) element.dataset.lastBar = `${last.ts} ${last.close}`;
  }, [live.candles, live.points, live.forming]);

  useEffect(() => {
    drawn.current?.markers.setMarkers(
      markersOf(live.trades, live.switches, bot.timeframe, colors.current),
    );
  }, [live.trades, live.switches, bot.timeframe]);

  // The legend: the candle under the cursor, or the newest.
  const byTime = useMemo(() => {
    const map = new Map<number, Candle>();
    for (const candle of live.candles) map.set(chartTime(candle.ts), candle);
    return map;
  }, [live.candles]);
  const valuesByTime = useMemo(() => {
    const map = new Map<number, Record<string, number>>();
    for (const point of live.points) map.set(chartTime(point.ts), point.values);
    return map;
  }, [live.points]);
  const newestTime = live.candles.at(-1) ? chartTime(live.candles.at(-1)!.ts) : null;
  const forming =
    live.forming && (newestTime === null || chartTime(live.forming.ts) > newestTime)
      ? live.forming
      : null;
  const closedAt = (time: number | null): Hovered | null => {
    const candle = time === null ? undefined : byTime.get(time);
    return candle ? { candle, values: valuesByTime.get(time!) ?? {}, forming: false } : null;
  };
  const hovered: Hovered | null =
    closedAt(hoverTime) ??
    (forming ? { candle: forming, values: {}, forming: true } : closedAt(newestTime));
  const lineColors = dark ? LINE_SLOTS.dark : LINE_SLOTS.light;
  const quote = bot.symbol.split("/")[1] ?? "";
  const price = (value: string) => formatDecimal(value, { min: precision, max: precision });

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs" aria-live="off">
        {hovered && (
          <span className="text-muted-foreground font-mono tabular-nums">
            {hovered.forming && (
              <span className="text-foreground mr-2 font-sans font-semibold">forming</span>
            )}
            O <span className="text-foreground">{price(hovered.candle.open)}</span> H{" "}
            <span className="text-foreground">{price(hovered.candle.high)}</span> L{" "}
            <span className="text-foreground">{price(hovered.candle.low)}</span> C{" "}
            <span className="text-foreground">{price(hovered.candle.close)}</span>{" "}
            <span className="font-sans">{quote}</span> · V{" "}
            <span className="text-foreground">
              {formatDecimal(hovered.candle.volume, { min: 0, max: 4 })}
            </span>
          </span>
        )}
        {indicators?.names.map((name, i) => {
          const value = hovered?.values[name];
          return (
            <span key={name} className="flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-0.5 w-4 rounded-full"
                style={{ background: lineColors[LINE_SLOT[name] ?? i % lineColors.length] }}
              />
              <span className="text-muted-foreground">{name}</span>
              <span className="font-mono tabular-nums">
                {value === undefined
                  ? "—"
                  : value.toFixed(indicators.pane === "oscillator" ? 2 : precision)}
              </span>
            </span>
          );
        })}
      </div>
      <div
        ref={container}
        data-testid="candle-chart"
        role="img"
        aria-label={`${bot.symbol} ${bot.timeframe} candles with ${bot.strategy ?? "no"} indicators, trade markers and strategy switch markers; the trades are also listed in the table below, and the switches on the bot's page`}
        className={oscillator ? "h-[560px]" : "h-[480px]"}
      />
    </div>
  );
}
