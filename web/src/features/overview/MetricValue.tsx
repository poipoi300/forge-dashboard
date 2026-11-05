// A custom metric's latest value rendered by its manifest display kind.
import { StatusMark } from "@/components/status";
import { bytes, count, percent } from "@/lib/format";
import { duration } from "@/lib/time";
import type { Tone } from "@/schema/schema";

export interface MetricLike {
  display: "number" | "percent" | "duration" | "bytes" | "text" | "status";
  unit?: string | null;
  value?: boolean | number | string | null;
  status?: "ok" | "warn" | "fail" | null;
  error?: string | null;
}

const STATUS_TONE: Record<string, Tone> = { ok: "good", warn: "warn", fail: "bad" };

export function formatMetric(metric: MetricLike): string {
  const { value, display, unit } = metric;
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value;
  const suffix = unit ? ` ${unit}` : "";
  switch (display) {
    case "percent":
      return percent(value, Math.abs(value) < 10 ? 1 : 0);
    case "duration":
      return duration(value);
    case "bytes":
      return bytes(value);
    case "number":
    case "status":
    case "text":
      return `${count(value)}${suffix}`;
  }
}

export function MetricValue({ metric }: { metric: MetricLike }) {
  if (metric.error) {
    return (
      <StatusMark tone="bad" title={metric.error}>
        error
      </StatusMark>
    );
  }
  const text = formatMetric(metric);
  if (metric.status) {
    const tone = STATUS_TONE[metric.status] ?? "neutral";
    return (
      <StatusMark tone={tone} strong={tone === "bad"} title={`status: ${metric.status}`}>
        <span className="figures">{text}</span>
      </StatusMark>
    );
  }
  return <span className="figures text-xs font-medium">{text}</span>;
}
