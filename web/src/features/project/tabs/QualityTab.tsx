// Quality: manifest checks with history and JUnit detail, the pre-commit
// gates, coverage, and GitHub Actions runs with their jobs and steps.
import { ChevronRight, ExternalLink, Play, RotateCw } from "lucide-react";
import { useState } from "react";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { RunStrip, TrendChart } from "@/components/charts";
import { Meter, RelativeTime } from "@/components/data";
import { EmptyState, QueryState, Section } from "@/components/states";
import { Chip, StatusMark } from "@/components/status";
import { Input } from "@/components/ui/input";
import { percent } from "@/lib/format";
import { duration } from "@/lib/time";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";
import type { TabProps } from "../ProjectPage";

const OUTCOME_TONE: Record<string, Tone> = {
  passed: "good",
  success: "good",
  failed: "bad",
  failure: "bad",
  error: "bad",
  timeout: "bad",
  timed_out: "bad",
  running: "info",
  in_progress: "info",
  queued: "info",
  never: "neutral",
  cancelled: "neutral",
  skipped: "neutral",
};

const OUTCOME_LABEL: Record<string, string> = { never: "not run yet" };

export function Outcome({ value }: { value: string }) {
  const tone = OUTCOME_TONE[value] ?? "neutral";
  return (
    <StatusMark tone={tone} strong={tone === "bad"}>
      {OUTCOME_LABEL[value] ?? value.replaceAll("_", " ")}
    </StatusMark>
  );
}

const GATES_SHOWN = 6;

function CheckDetail({
  projectId,
  checkId,
  label,
}: {
  projectId: string;
  checkId: string;
  label: string;
}) {
  const history = useAction(
    "/api/services/checks/actions/history",
    { project_id: projectId, check_id: checkId, limit: 100 },
    { follow: ["checks"] },
  );
  const latest = useAction(
    "/api/services/checks/actions/latest",
    { project_id: projectId, check_id: checkId },
    { follow: ["checks"] },
  );
  const [open, setOpen] = useState<string | null>(null);
  return (
    <section
      aria-label={`History of ${label}`}
      className="grid gap-4 rounded-md border border-rule bg-surface p-3 lg:grid-cols-2"
    >
      <h3 className="text-xs font-medium text-ink-2 lg:col-span-2">History of {label}</h3>
      <QueryState query={history}>
        {(data) =>
          data.points.length === 0 ? (
            <p className="text-sm text-ink-3">
              No runs recorded yet. Run it to start its pass rate and duration history.
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              <div className="flex items-baseline justify-between">
                <span className="text-xs text-ink-3">pass rate</span>
                <span className="figures text-lg font-semibold">
                  {data.pass_rate === null ? "—" : percent(data.pass_rate * 100)}
                </span>
              </div>
              <RunStrip
                label="Runs"
                runs={data.points.map((point) => ({
                  at: point.ran_at,
                  passed: point.outcome === "passed",
                  note: point.sha ?? undefined,
                }))}
              />
              <TrendChart
                title="Duration per run"
                points={data.points.map((point) => ({
                  at: point.ran_at,
                  value: point.duration_s,
                  bad: point.outcome !== "passed",
                  note:
                    point.tests !== null
                      ? `${point.tests} tests, ${point.failures ?? 0} failing`
                      : undefined,
                }))}
                format={(value) => duration(value)}
              />
            </div>
          )
        }
      </QueryState>
      <QueryState query={latest}>
        {(data) => (
          <div className="flex min-w-0 flex-col gap-2">
            {data.report ? (
              <>
                <p className="figures text-xs text-ink-2">
                  {data.report.tests} tests · {data.report.failures} failures · {data.report.errors}{" "}
                  errors · {data.report.skipped} skipped
                </p>
                {data.report.failed.length ? (
                  <ul className="flex flex-col divide-y divide-rule rounded-sm border border-rule">
                    {data.report.failed.map((failure) => {
                      const key = `${failure.suite}::${failure.name}`;
                      return (
                        <li key={key}>
                          <button
                            type="button"
                            aria-expanded={open === key}
                            onClick={() => setOpen(open === key ? null : key)}
                            className="flex w-full items-start gap-1.5 px-2 py-1.5 text-left hover:bg-hover"
                          >
                            <ChevronRight
                              aria-hidden
                              className={cn(
                                "mt-0.5 size-3.5 shrink-0 text-ink-3",
                                open === key && "rotate-90",
                              )}
                            />
                            <span className="min-w-0">
                              <span className="block truncate font-mono text-xs text-signal-ink">
                                {failure.name}
                              </span>
                              <span className="block truncate text-2xs text-ink-3">
                                {failure.suite} · {failure.message}
                              </span>
                            </span>
                          </button>
                          {open === key ? (
                            <pre className="max-h-72 overflow-auto bg-surface-sunk px-3 py-2 font-mono text-2xs whitespace-pre-wrap">
                              {failure.detail || failure.message}
                            </pre>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="text-xs text-clear-ink">No failing tests in the last report.</p>
                )}
              </>
            ) : (
              <p className="text-xs text-ink-3">
                This check writes no JUnit report; its exit code decides.
              </p>
            )}
            {data.log_tail.length ? (
              <details>
                <summary className="cursor-pointer text-xs text-ink-2">Last output lines</summary>
                <pre className="mt-1 max-h-64 overflow-auto rounded-sm bg-board px-3 py-2 font-mono text-2xs text-board-ink">
                  {data.log_tail.join("\n")}
                </pre>
              </details>
            ) : null}
          </div>
        )}
      </QueryState>
    </section>
  );
}

function CiRuns({ projectId }: { projectId: string }) {
  const [branch, setBranch] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const runs = useAction(
    "/api/services/github/actions/runs",
    { project_id: projectId, branch: branch || null, limit: 30 },
    { follow: ["github"] },
  );
  const run = useAction(
    "/api/services/github/actions/run",
    { project_id: projectId, run_id: selected ?? 0 },
    { enabled: selected !== null },
  );
  return (
    <div className="flex flex-col gap-3">
      <label className="flex items-center gap-2 text-xs text-ink-3">
        Branch
        <Input
          value={branch}
          onChange={(event) => setBranch(event.target.value)}
          placeholder="all branches"
          className="h-7 w-44 text-xs"
        />
      </label>
      <QueryState query={runs} errorTitle="GitHub runs could not be read">
        {(data) =>
          data.runs.length ? (
            <div className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
              <div className="min-w-0 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                      <th className="px-2 py-1 font-medium">workflow</th>
                      <th className="px-2 py-1 font-medium">outcome</th>
                      <th className="px-2 py-1 font-medium">branch</th>
                      <th className="px-2 py-1 text-right font-medium">took</th>
                      <th className="px-2 py-1 text-right font-medium">when</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.runs.map((item) => (
                      <tr
                        key={item.id}
                        onClick={() => setSelected(item.id)}
                        className={cn(
                          "cursor-pointer border-b border-rule last:border-b-0 hover:bg-hover",
                          selected === item.id && "bg-select",
                        )}
                      >
                        <td className="max-w-[14rem] px-2 py-1">
                          <span className="block truncate">{item.workflow}</span>
                          <span className="block truncate text-2xs text-ink-3">
                            #{item.number} {item.title}
                          </span>
                        </td>
                        <td className="px-2 py-1">
                          <Outcome value={item.outcome} />
                        </td>
                        <td className="px-2 py-1 font-mono text-2xs">{item.branch}</td>
                        <td className="figures px-2 py-1 text-right text-xs whitespace-nowrap">
                          {item.duration_s !== null ? duration(item.duration_s) : "—"}
                        </td>
                        <td className="px-2 py-1 text-right text-xs">
                          <RelativeTime at={item.started_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="min-w-0">
                {selected === null ? (
                  <p className="text-xs text-ink-3">Select a run for its jobs and steps.</p>
                ) : (
                  <QueryState query={run}>
                    {(detail) => (
                      <div className="flex flex-col gap-2 rounded-md border border-rule bg-surface p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <a
                            href={detail.run.html_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-sm font-medium hover:underline"
                          >
                            {detail.run.workflow} #{detail.run.number}
                            <ExternalLink className="size-3" />
                          </a>
                          {OUTCOME_TONE[detail.run.outcome] === "bad" ? (
                            <ActionButton
                              service="github"
                              action="rerun-failed-jobs"
                              context={{ projectId }}
                              input={{ run_id: detail.run.id }}
                              size="xs"
                              icon={<RotateCw />}
                              label="Re-run failed jobs"
                            />
                          ) : null}
                        </div>
                        {detail.jobs.map((job) => (
                          <div
                            key={job.id}
                            className="flex flex-col gap-0.5 border-t border-rule pt-1.5"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate text-xs font-medium">{job.name}</span>
                              <span className="flex items-center gap-2">
                                <Outcome value={job.outcome} />
                                {job.logs_url ? (
                                  <a
                                    href={job.logs_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-2xs text-info-ink hover:underline"
                                  >
                                    logs
                                  </a>
                                ) : null}
                              </span>
                            </div>
                            <ol className="flex flex-col">
                              {job.steps.map((step) => (
                                <li
                                  key={step.number}
                                  className="flex justify-between gap-2 pl-2 text-2xs text-ink-2"
                                >
                                  <span className="truncate">
                                    {step.number}. {step.name}
                                  </span>
                                  <Outcome value={step.outcome} />
                                </li>
                              ))}
                            </ol>
                          </div>
                        ))}
                      </div>
                    )}
                  </QueryState>
                )}
              </div>
            </div>
          ) : (
            <EmptyState title="No workflow runs">
              {branch
                ? `Nothing ran on ${branch}.`
                : "GitHub Actions has no runs for this repository."}
            </EmptyState>
          )
        }
      </QueryState>
    </div>
  );
}

export function QualityTab({ project }: TabProps) {
  const checksState = useServiceState("checks");
  const entry = checksState?.projects.find((item) => item.project_id === project.id);
  const [selected, setSelected] = useState<string | null>(null);
  const [showAllGates, setShowAllGates] = useState(false);
  const context = { projectId: project.id };
  const current =
    selected ??
    entry?.checks.find((check) => check.outcome !== "passed" && check.outcome !== "never")
      ?.check_id ??
    entry?.checks[0]?.check_id ??
    null;
  const onGitHub = project.issues?.github === "ok";

  return (
    <div className="flex flex-col gap-8">
      <Section
        title="Checks"
        description="Declared in forge.project.toml; results are kept per commit."
        actions={
          entry?.checks.length ? (
            <ActionButton
              service="checks"
              action="run"
              context={context}
              input={{ check_id: null, on_head: null }}
              size="xs"
              icon={<Play />}
              label="Run all"
            />
          ) : null
        }
      >
        {entry?.checks.length ? (
          <>
            <div className="min-w-0 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                    <th className="px-2 py-1 font-medium">check</th>
                    <th className="px-2 py-1 font-medium">result</th>
                    <th className="px-2 py-1 text-right font-medium">tests</th>
                    <th className="px-2 py-1 text-right font-medium">took</th>
                    <th className="px-2 py-1 font-medium">commit</th>
                    <th className="px-2 py-1 text-right font-medium">ran</th>
                    <th className="px-2 py-1">
                      <span className="sr-only">run</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {entry.checks.map((check) => (
                    <tr
                      key={check.check_id}
                      onClick={() => setSelected(check.check_id)}
                      className={cn(
                        "cursor-pointer border-b border-rule last:border-b-0 hover:bg-hover",
                        current === check.check_id && "bg-select",
                      )}
                    >
                      <td className="px-2 py-1.5">
                        <span className="font-medium">{check.label}</span>{" "}
                        <Chip>{check.stage}</Chip>
                      </td>
                      <td className="px-2 py-1.5">
                        <span className="flex items-center gap-2">
                          <Outcome value={check.outcome} />
                          {check.stale ? (
                            <span
                              className="text-2xs font-medium text-amber-ink"
                              title="HEAD moved or the tree changed since this ran"
                            >
                              stale
                            </span>
                          ) : null}
                        </span>
                      </td>
                      <td className="figures px-2 py-1.5 text-right text-xs whitespace-nowrap">
                        {check.tests !== null
                          ? `${check.tests - (check.failures ?? 0) - (check.errors ?? 0)}/${check.tests}`
                          : "—"}
                      </td>
                      <td className="figures px-2 py-1.5 text-right text-xs whitespace-nowrap">
                        {check.duration_s !== null ? duration(check.duration_s) : "—"}
                      </td>
                      <td className="px-2 py-1.5 font-mono text-2xs text-ink-2">
                        {check.sha?.slice(0, 8) ?? "—"}
                        {check.dirty ? <span className="text-amber-ink"> +edits</span> : null}
                      </td>
                      <td className="px-2 py-1.5 text-right text-xs">
                        <RelativeTime at={check.ran_at} />
                      </td>
                      <td className="px-2 py-1 text-right">
                        <ActionButton
                          service="checks"
                          action="run"
                          context={context}
                          input={{ check_id: check.check_id, on_head: null }}
                          size="icon-xs"
                          variant="ghost"
                          icon={<Play />}
                          label={<span className="sr-only">Run {check.label}</span>}
                          title={`Run ${check.label}`}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {current ? (
              <CheckDetail
                key={current}
                projectId={project.id}
                checkId={current}
                label={entry.checks.find((check) => check.check_id === current)?.label ?? current}
              />
            ) : null}
          </>
        ) : (
          <EmptyState title="No checks declared">
            Add <code className="font-mono text-xs">[[checks]]</code> to forge.project.toml (tests,
            lint, type checks) to run them from here and track their history.
          </EmptyState>
        )}
      </Section>

      <div className="grid gap-8 lg:grid-cols-2">
        <Section
          title="Gates"
          description="From .pre-commit-config.yaml."
          actions={
            entry?.gates.length ? (
              <>
                <ActionButton
                  service="checks"
                  action="run-commit-gates"
                  context={context}
                  size="xs"
                  label="Commit gates"
                  icon={<Play />}
                />
                <ActionButton
                  service="checks"
                  action="run-push-gate"
                  context={context}
                  size="xs"
                  label="Push gate"
                  icon={<Play />}
                />
              </>
            ) : null
          }
        >
          {entry?.gates.length ? (
            <ul className="flex flex-col divide-y divide-rule">
              {(showAllGates ? entry.gates : entry.gates.slice(0, GATES_SHOWN)).map((gate) => (
                <li
                  key={gate.id}
                  className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 py-1"
                >
                  <span className="min-w-0">
                    <code className="font-mono text-xs whitespace-nowrap">{gate.id}</code>
                    <span className="ml-2 truncate text-xs text-ink-3">{gate.name}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    {gate.stages.map((stage) => (
                      <Chip key={stage}>{stage}</Chip>
                    ))}
                    <ActionButton
                      service="checks"
                      action="run-gate"
                      context={context}
                      input={{ gate_id: gate.id }}
                      size="icon-xs"
                      variant="ghost"
                      icon={<Play />}
                      label={<span className="sr-only">Run {gate.id}</span>}
                      title={`Run ${gate.id}`}
                    />
                  </span>
                </li>
              ))}
              {entry.gates.length > GATES_SHOWN ? (
                <li className="py-1">
                  <button
                    type="button"
                    onClick={() => setShowAllGates(!showAllGates)}
                    aria-expanded={showAllGates}
                    className="py-1 text-xs text-info-ink hover:underline"
                  >
                    {showAllGates ? "Show fewer" : `Show all ${entry.gates.length} gates`}
                  </button>
                </li>
              ) : null}
            </ul>
          ) : (
            <EmptyState
              title={entry?.gates_error ? "The gates could not be read" : "No pre-commit gates"}
            >
              {entry?.gates_error ?? "This repository has no .pre-commit-config.yaml."}
            </EmptyState>
          )}
        </Section>

        <Section title="Coverage">
          {entry?.coverage ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-baseline justify-between">
                <span className="figures-open text-[1.75rem] leading-8 font-semibold">
                  {percent(entry.coverage.percent, 1)}
                </span>
                <span className="figures text-xs text-ink-3">
                  {entry.coverage.lines_covered} of {entry.coverage.lines_total} lines ·{" "}
                  {entry.coverage.format}
                </span>
              </div>
              <Meter
                value={entry.coverage.percent / 100}
                tone={
                  entry.coverage.percent >= 90
                    ? "good"
                    : entry.coverage.percent >= 70
                      ? "warn"
                      : "bad"
                }
                label="Line coverage"
              />
              <p className="text-2xs text-ink-3">
                measured <RelativeTime at={entry.coverage.measured_at} /> from {entry.coverage.path}
              </p>
            </div>
          ) : (
            <EmptyState title="No coverage report">
              A check with a coverage report in the manifest fills this in after it runs.
            </EmptyState>
          )}
        </Section>
      </div>

      <Section title="CI" description={onGitHub ? "GitHub Actions" : undefined}>
        {onGitHub ? (
          <CiRuns projectId={project.id} />
        ) : (
          <EmptyState title="CI is read from GitHub">
            {project.issues?.github === "unauthenticated"
              ? "Sign in to GitHub in Settings to read workflow runs."
              : project.issues?.github === "rate-limited"
                ? "GitHub's rate limit is spent; runs come back after it resets."
                : "This repository has no GitHub remote."}
          </EmptyState>
        )}
      </Section>
    </div>
  );
}
