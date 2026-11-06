// Charts for check history and metric history. One series per chart, so the
// title names it and there is no legend. Marks follow the dataviz specs: a
// 2px line, 8px end and status markers ringed in the surface colour,
// hairline grid, one y-axis, a crosshair that snaps to the nearest reading,
// and a table view carrying every value without hover.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { absolute } from "@/lib/time";
import { cn } from "@/lib/utils";

export interface TrendPoint {
  /** Epoch seconds. */
  at: number;
  value: number;
  /** A failing reading draws its marker in signal red with a cross. */
  bad?: boolean;
  note?: string;
}

function niceStep(span: number, ticks: number): number {
  const raw = span / Math.max(1, ticks);
  const magnitude = 10 ** Math.floor(Math.log10(raw || 1));
  const residual = raw / magnitude;
  const nice = residual > 5 ? 10 : residual > 2 ? 5 : residual > 1 ? 2 : 1;
  return nice * magnitude;
}

/** Round axis ticks spanning [min, max]; counts (`integer`) never get fractional steps. */
export function niceTicks(min: number, max: number, count = 4, integer = false): number[] {
  if (min === max) {
    // A flat series gets a band around its value; a non-negative one never dips below zero.
    const pad = Math.abs(min) || 1;
    return niceTicks(min >= 0 ? Math.max(0, min - pad) : min - pad, max + pad, count, integer);
  }
  const step = integer ? Math.max(1, niceStep(max - min, count)) : niceStep(max - min, count);
  const start = Math.floor(min / step) * step;
  const ticks: number[] = [];
  for (let tick = start; tick <= max + step * 0.5; tick += step)
    ticks.push(Number(tick.toPrecision(12)));
  return ticks;
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    observer.observe(element);
    setWidth(element.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

const PAD = { top: 10, right: 14, bottom: 22, left: 44 };

export function TrendChart({
  title,
  points,
  format,
  height = 160,
  zeroBased = true,
  className,
}: {
  title: string;
  points: TrendPoint[];
  format: (value: number) => string;
  height?: number;
  zeroBased?: boolean;
  className?: string;
}) {
  const [frame, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const titleId = useId();
  const sorted = useMemo(() => [...points].sort((a, b) => a.at - b.at), [points]);

  const geometry = useMemo(() => {
    if (sorted.length === 0 || width === 0) return null;
    const values = sorted.map((point) => point.value);
    const ticks = niceTicks(
      zeroBased ? Math.min(0, ...values) : Math.min(...values),
      Math.max(...values),
      4,
      values.every(Number.isInteger),
    );
    const low = ticks[0] ?? 0;
    const high = ticks[ticks.length - 1] ?? 1;
    const first = sorted[0]?.at ?? 0;
    const last = sorted[sorted.length - 1]?.at ?? 1;
    const innerWidth = Math.max(10, width - PAD.left - PAD.right);
    const innerHeight = height - PAD.top - PAD.bottom;
    const x = (at: number) =>
      PAD.left + (last === first ? innerWidth / 2 : ((at - first) / (last - first)) * innerWidth);
    const y = (value: number) =>
      PAD.top + innerHeight - ((value - low) / (high - low || 1)) * innerHeight;
    return { ticks, x, y, first, last, innerHeight };
  }, [sorted, width, height, zeroBased]);

  const nearest = (clientX: number, element: SVGSVGElement) => {
    if (!geometry) return;
    const left = element.getBoundingClientRect().left;
    let best = 0;
    let distance = Number.POSITIVE_INFINITY;
    sorted.forEach((point, index) => {
      const gap = Math.abs(geometry.x(point.at) - (clientX - left));
      if (gap < distance) {
        distance = gap;
        best = index;
      }
    });
    setHover(best);
  };

  const active = hover !== null ? sorted[hover] : undefined;
  const lastPoint = sorted[sorted.length - 1];
  const path = geometry
    ? sorted
        .map(
          (point, index) =>
            `${index ? "L" : "M"}${geometry.x(point.at).toFixed(1)},${geometry.y(point.value).toFixed(1)}`,
        )
        .join(" ")
    : "";

  return (
    <figure className={cn("flex min-w-0 flex-col gap-1", className)} aria-labelledby={titleId}>
      <figcaption className="flex items-baseline justify-between gap-2">
        <span id={titleId} className="text-xs font-medium text-ink-2">
          {title}
        </span>
        <button
          type="button"
          onClick={() => setShowTable((value) => !value)}
          className="text-2xs text-ink-3 hover:text-ink"
        >
          {showTable ? "Chart" : "Table"}
        </button>
      </figcaption>
      {showTable ? (
        <div className="max-h-48 overflow-auto rounded-sm border border-rule">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-rule text-left text-ink-3">
                <th className="px-2 py-1 font-medium">when</th>
                <th className="px-2 py-1 text-right font-medium">value</th>
              </tr>
            </thead>
            <tbody>
              {[...sorted].reverse().map((point) => (
                <tr
                  key={`${point.at}-${point.value}`}
                  className="border-b border-rule last:border-b-0"
                >
                  <td className="px-2 py-0.5">{absolute(point.at)}</td>
                  <td className="figures px-2 py-0.5 text-right">
                    {format(point.value)}
                    {point.bad ? <span className="ml-1 text-signal-ink">failed</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={frame} className="relative w-full" style={{ height }}>
          {sorted.length === 0 ? (
            <p className="absolute inset-0 flex items-center justify-center text-xs text-ink-3">
              No readings yet.
            </p>
          ) : geometry ? (
            <svg
              width={width}
              height={height}
              role="img"
              aria-label={`${title}: ${sorted.length} readings, latest ${lastPoint ? format(lastPoint.value) : ""}`}
              className="block touch-none"
              onPointerMove={(event) => nearest(event.clientX, event.currentTarget)}
              onPointerLeave={() => setHover(null)}
            >
              {geometry.ticks.map((tick) => (
                <g key={tick}>
                  <line
                    x1={PAD.left}
                    x2={width - PAD.right}
                    y1={geometry.y(tick)}
                    y2={geometry.y(tick)}
                    stroke="var(--rule)"
                    strokeWidth={1}
                  />
                  <text
                    x={PAD.left - 6}
                    y={geometry.y(tick)}
                    dy="0.32em"
                    textAnchor="end"
                    className="figures fill-ink-3 text-[10px]"
                  >
                    {format(tick)}
                  </text>
                </g>
              ))}
              <text x={PAD.left} y={height - 6} className="fill-ink-3 text-[10px]">
                {absolute(geometry.first)}
              </text>
              <text
                x={width - PAD.right}
                y={height - 6}
                textAnchor="end"
                className="fill-ink-3 text-[10px]"
              >
                {absolute(geometry.last)}
              </text>
              <path
                d={path}
                fill="none"
                stroke="var(--info)"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {sorted.map((point) =>
                point.bad ? (
                  <g
                    key={`bad-${point.at}`}
                    transform={`translate(${geometry.x(point.at)},${geometry.y(point.value)})`}
                  >
                    <circle r={5} fill="var(--signal)" stroke="var(--surface)" strokeWidth={2} />
                    <path
                      d="M-2,-2L2,2M2,-2L-2,2"
                      stroke="var(--surface)"
                      strokeWidth={1.5}
                      strokeLinecap="round"
                    />
                  </g>
                ) : null,
              )}
              {lastPoint && !lastPoint.bad ? (
                <circle
                  cx={geometry.x(lastPoint.at)}
                  cy={geometry.y(lastPoint.value)}
                  r={4}
                  fill="var(--info)"
                  stroke="var(--surface)"
                  strokeWidth={2}
                />
              ) : null}
              {active ? (
                <g pointerEvents="none">
                  <line
                    x1={geometry.x(active.at)}
                    x2={geometry.x(active.at)}
                    y1={PAD.top}
                    y2={PAD.top + geometry.innerHeight}
                    stroke="var(--ink-3)"
                    strokeWidth={1}
                  />
                  <circle
                    cx={geometry.x(active.at)}
                    cy={geometry.y(active.value)}
                    r={4}
                    fill="var(--info)"
                    stroke="var(--surface)"
                    strokeWidth={2}
                  />
                </g>
              ) : null}
            </svg>
          ) : null}
          {active && geometry ? (
            <div
              role="status"
              className="pointer-events-none absolute top-0 z-10 rounded-sm border border-rule bg-surface px-2 py-1 text-xs shadow-sm"
              style={{
                left: Math.min(Math.max(geometry.x(active.at) + 8, 0), Math.max(0, width - 160)),
              }}
            >
              <div className="flex items-center gap-1.5">
                <span
                  aria-hidden
                  className="h-0.5 w-3 rounded-full"
                  style={{ background: active.bad ? "var(--signal)" : "var(--info)" }}
                />
                <span className="figures font-semibold text-ink">{format(active.value)}</span>
                {active.bad ? <span className="text-signal-ink">failed</span> : null}
              </div>
              <div className="text-2xs text-ink-3">{absolute(active.at)}</div>
              {active.note ? <div className="text-2xs text-ink-2">{active.note}</div> : null}
            </div>
          ) : null}
        </div>
      )}
    </figure>
  );
}

/** Pass/fail per run as a strip of columns: a pass is a short clear column, a failure a full-height signal one. */
export function RunStrip({
  runs,
  label,
}: {
  runs: { at: number; passed: boolean; note?: string }[];
  label: string;
}) {
  const sorted = [...runs].sort((a, b) => a.at - b.at).slice(-60);
  const passed = sorted.filter((run) => run.passed).length;
  return (
    <div className="flex flex-col gap-1">
      <div
        className="flex h-6 items-end gap-0.5"
        role="img"
        aria-label={`${label}: ${passed} of ${sorted.length} runs passed`}
      >
        {sorted.map((run) => (
          <span
            key={run.at}
            title={`${run.passed ? "passed" : "failed"} · ${absolute(run.at)}${run.note ? ` · ${run.note}` : ""}`}
            className={cn("w-1.5 rounded-t-[2px]", run.passed ? "h-2.5 bg-clear" : "h-6 bg-signal")}
          />
        ))}
      </div>
      <span className="figures text-2xs text-ink-3">
        {passed} of {sorted.length} passed
      </span>
    </div>
  );
}
