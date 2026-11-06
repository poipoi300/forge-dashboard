// Small data primitives shared by the schema renderer and bespoke views.
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { homeRelative } from "@/lib/format";
import { absolute, relative, useNow } from "@/lib/time";
import { cn } from "@/lib/utils";
import type { Tone } from "@/schema/schema";

const LEADING_COUNT = /^(in )?(\d+)(.*)$/;

/**
 * A relative time on the shared tick; the absolute time is its title. Only the
 * count is set in condensed tabular figures: condensing the unit too made
 * "20 min ago" read as one word at small sizes.
 */
export function RelativeTime({
  at,
  className,
}: {
  at: number | null | undefined;
  className?: string;
}) {
  const now = useNow();
  if (at === null || at === undefined)
    return <span className={cn("text-ink-3", className)}>never</span>;
  return (
    <time
      dateTime={new Date(at * 1000).toISOString()}
      title={absolute(at)}
      className={cn("whitespace-nowrap", className)}
    >
      <CondensedCount text={relative(at, now)} />
    </time>
  );
}

function CondensedCount({ text }: { text: string }) {
  const match = LEADING_COUNT.exec(text);
  if (!match) return <>{text}</>;
  return (
    <>
      {match[1]}
      <span className="figures">{match[2]}</span>
      {match[3]}
    </>
  );
}

const METER_FILL: Record<Tone, string> = {
  good: "bg-clear",
  warn: "bg-amber",
  bad: "bg-signal",
  info: "bg-info",
  neutral: "bg-ink-2",
};
const METER_TRACK: Record<Tone, string> = {
  good: "bg-clear-wash",
  warn: "bg-amber-wash",
  bad: "bg-signal-wash",
  info: "bg-info-wash",
  neutral: "bg-hover",
};

/** A ratio against a limit: the fill carries severity, the track is a lighter step of the same hue. */
export function Meter({
  value,
  tone = "info",
  label,
  className,
}: {
  value: number;
  tone?: Tone;
  label: string;
  className?: string;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  return (
    // biome-ignore lint/a11y/useSemanticElements: a native <meter> cannot take the same-hue track and severity fill
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      className={cn("h-1.5 w-full overflow-hidden rounded-full", METER_TRACK[tone], className)}
    >
      <div
        className={cn("h-full rounded-full", METER_FILL[tone])}
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  );
}

/**
 * A path as its root plus its last two segments, which is what tells two
 * checkouts apart ("~/…/Projects/Reelcut-export"); the full path is the title.
 */
export function compactPath(path: string): string {
  const relativePath = homeRelative(path);
  const parts = relativePath.split("/").filter(Boolean);
  if (parts.length <= 4) return relativePath;
  const root = relativePath.startsWith("/") ? `/${parts[0]}` : parts[0];
  return [root, "…", ...parts.slice(-2)].join("/");
}

/** A filesystem path shown compactly, with a copy button that appears on hover or focus. */
export function PathText({ path, className }: { path: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(path);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <span className={cn("group relative inline-flex min-w-0 items-center", className)}>
      <span className="truncate font-mono text-2xs text-ink-3" title={path}>
        {compactPath(path)}
      </span>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Path copied" : "Copy path"}
        className="absolute top-1/2 left-full ml-0.5 flex size-6 -translate-y-1/2 items-center justify-center rounded-sm text-ink-3 opacity-0 hover:bg-hover hover:text-ink focus-visible:opacity-100 group-hover:opacity-100"
      >
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      </button>
    </span>
  );
}

/** A 12-point trend in the de-emphasis ink with the latest point marked. Single series, so no legend. */
export function Sparkline({
  values,
  label,
  width = 72,
  height = 18,
}: {
  values: number[];
  label: string;
  width?: number;
  height?: number;
}) {
  const points = values.slice(-24);
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = (width - 4) / (points.length - 1);
  const coords = points.map((value, index) => [
    2 + index * step,
    height - 2 - ((value - min) / span) * (height - 4),
  ]);
  const path = coords
    .map(([x, y], index) => `${index ? "L" : "M"}${x?.toFixed(1)},${y?.toFixed(1)}`)
    .join(" ");
  const last = coords[coords.length - 1] ?? [0, 0];
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${label}: ${points.length} readings, latest ${points[points.length - 1]}`}
      className="shrink-0 overflow-visible"
    >
      <title>{`${label}: ${points.join(", ")}`}</title>
      <path
        d={path}
        fill="none"
        stroke="var(--ink-3)"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        cx={last[0]}
        cy={last[1]}
        r={2.5}
        fill="var(--info)"
        stroke="var(--surface)"
        strokeWidth={1.5}
      />
    </svg>
  );
}
