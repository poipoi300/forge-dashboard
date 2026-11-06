// Metrics: every manifest metric with its latest reading, a history chart
// for numeric ones, its thresholds, and a refresh. One chart per metric:
// metrics have different units, so they never share an axis.
import { RefreshCw } from "lucide-react";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { TrendChart } from "@/components/charts";
import { RelativeTime } from "@/components/data";
import { QueryState } from "@/components/states";
import { Chip } from "@/components/status";
import { ActionButton } from "@/schema/ActionButton";
import { formatMetric, MetricValue } from "../../overview/MetricValue";
import type { TabProps } from "../ProjectPage";

type Display = "number" | "percent" | "duration" | "bytes" | "text" | "status";

/** A failed reading: its first line, with the full output one click away. */
export function ReadingError({ error }: { error: string }) {
  const first = error.split(/\r?\n/, 1)[0] ?? error;
  return (
    <details className="text-signal-ink">
      <summary className="cursor-pointer truncate">{first}</summary>
      <pre className="mt-1 max-h-60 overflow-auto rounded-sm bg-surface-sunk px-2 py-1 font-mono text-2xs whitespace-pre-wrap text-ink-2">
        {error}
      </pre>
    </details>
  );
}

function MetricHistory({
  projectId,
  metricId,
  display,
  unit,
  label,
}: {
  projectId: string;
  metricId: string;
  display: Display;
  unit: string | null;
  label: string;
}) {
  const history = useAction(
    "/api/services/metrics/actions/history",
    { project_id: projectId, metric_id: metricId, limit: 200 },
    { follow: ["metrics"] },
  );
  return (
    <QueryState query={history}>
      {(data) => {
        const numeric = data.points.filter((point) => typeof point.value === "number");
        // A chart needs two numeric readings; until then the recent readings stand in.
        if (numeric.length < 2) {
          const recent = data.points.slice(-6).reverse();
          return recent.length ? (
            <ul className="flex flex-col gap-0.5 text-xs">
              {recent.map((point) => (
                <li key={point.read_at} className="flex justify-between gap-2">
                  <span className="min-w-0">
                    {point.error ? (
                      <ReadingError error={point.error} />
                    ) : (
                      formatMetric({ display, unit, value: point.value })
                    )}
                  </span>
                  <span className="text-2xs text-ink-3">
                    <RelativeTime at={point.read_at} />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-ink-3">No readings yet.</p>
          );
        }
        return (
          <TrendChart
            title={`${label} over time`}
            points={numeric.map((point) => ({
              at: point.read_at,
              value: point.value as number,
              bad: point.status === "fail",
            }))}
            format={(value) => formatMetric({ display, unit, value })}
            zeroBased={display !== "duration"}
          />
        );
      }}
    </QueryState>
  );
}

export function MetricsTab({ project, detail }: TabProps) {
  const latest =
    useServiceState("metrics")?.projects.find((item) => item.project_id === project.id)?.metrics ??
    [];
  const definitions = detail.manifest?.metrics ?? [];
  const context = { projectId: project.id };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <ActionButton
          service="metrics"
          action="refresh"
          context={context}
          input={{ metric_id: null }}
          size="xs"
          icon={<RefreshCw />}
          label="Refresh all"
        />
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-2">
        {definitions.map((definition) => {
          const reading = latest.find((item) => item.metric_id === definition.id);
          const thresholds = definition.thresholds;
          const bounds = thresholds
            ? [
                thresholds.fail_below !== null && thresholds.fail_below !== undefined
                  ? `fail below ${thresholds.fail_below}`
                  : null,
                thresholds.warn_below !== null && thresholds.warn_below !== undefined
                  ? `warn below ${thresholds.warn_below}`
                  : null,
                thresholds.warn_above !== null && thresholds.warn_above !== undefined
                  ? `warn above ${thresholds.warn_above}`
                  : null,
                thresholds.fail_above !== null && thresholds.fail_above !== undefined
                  ? `fail above ${thresholds.fail_above}`
                  : null,
              ].filter((item) => item !== null)
            : [];
          return (
            <article
              key={definition.id}
              className="flex flex-col gap-3 rounded-md border border-rule bg-surface p-3.5"
            >
              <header className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="flex items-center gap-2 text-sm font-semibold">
                    {definition.label}
                    {definition.pinned ? <Chip>pinned</Chip> : null}
                  </h3>
                  {definition.description ? (
                    <p className="text-xs text-ink-3">{definition.description}</p>
                  ) : null}
                </div>
                <ActionButton
                  service="metrics"
                  action="refresh"
                  context={context}
                  input={{ metric_id: definition.id }}
                  size="icon-xs"
                  variant="ghost"
                  icon={<RefreshCw />}
                  label={<span className="sr-only">Refresh {definition.label}</span>}
                  title={`Read ${definition.label} now`}
                />
              </header>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-lg">
                  {reading ? (
                    <MetricValue metric={reading} />
                  ) : (
                    <span className="text-ink-3">—</span>
                  )}
                </span>
                <span className="text-2xs text-ink-3">
                  {reading?.read_at ? (
                    <>
                      read <RelativeTime at={reading.read_at} /> · every{" "}
                      {Math.round(definition.interval_s / 60)} min
                    </>
                  ) : (
                    `every ${Math.round(definition.interval_s / 60)} min`
                  )}
                </span>
              </div>
              {reading?.detail ? <p className="text-xs text-ink-2">{reading.detail}</p> : null}
              {bounds.length ? (
                <p className="text-2xs text-ink-3">thresholds: {bounds.join(" · ")}</p>
              ) : null}
              <MetricHistory
                projectId={project.id}
                metricId={definition.id}
                display={definition.display}
                unit={definition.unit ?? null}
                label={definition.label}
              />
            </article>
          );
        })}
      </div>
    </div>
  );
}
