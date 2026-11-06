// forge's own evaluation runs: each project run as a trajectory of steps,
// with step outcome, duration, harness, and its captured output.
import { useState } from "react";
import { Link } from "wouter";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { usePageTitle } from "@/app/title";
import { PathText, RelativeTime } from "@/components/data";
import { EmptyState, QueryState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { duration } from "@/lib/time";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";

interface StepRef {
  root: string;
  project: string;
  step: string;
  title: string;
}

function StepOutput({ step }: { step: StepRef }) {
  const [which, setWhich] = useState<"stdout" | "stderr">("stdout");
  const output = useAction("/api/services/forge_runs/actions/step-output", {
    root: step.root,
    project: step.project,
    step: step.step,
    which,
    offset: 0,
    limit: 2000,
  });
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{step.title}</span>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={which}
          onValueChange={(value) => value && setWhich(value as "stdout" | "stderr")}
        >
          <ToggleGroupItem value="stdout" className="px-2 text-xs">
            stdout
          </ToggleGroupItem>
          <ToggleGroupItem value="stderr" className="px-2 text-xs">
            stderr
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      <QueryState query={output} rows={8}>
        {(data) => (
          <pre className="max-h-[32rem] overflow-auto rounded-md bg-board px-3 py-2 font-mono text-2xs whitespace-pre-wrap text-board-ink">
            {data.lines.length ? data.lines.join("\n") : `No ${which}.`}
            {data.total_lines > data.lines.length
              ? `\n… ${data.total_lines - data.lines.length} more lines`
              : ""}
          </pre>
        )}
      </QueryState>
    </div>
  );
}

export function RunsPage() {
  usePageTitle("Forge runs");
  const state = useServiceState("forge_runs");
  const [selected, setSelected] = useState<StepRef | null>(null);
  const runs = state?.runs ?? [];
  return (
    <div className="max-w-[100rem] grid gap-6 px-5 py-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <h1 className="sr-only">Forge runs</h1>
      <Section
        title="Forge runs"
        description={
          state?.notice ??
          (state?.roots.length
            ? `${state.roots.filter((root) => root.exists).length} run roots`
            : undefined)
        }
        actions={
          <ActionButton
            service="forge_runs"
            action="refresh"
            size="xs"
            variant="ghost"
            label="Re-read"
          />
        }
      >
        {runs.length ? (
          <ul className="flex flex-col gap-3">
            {runs.map((run) => {
              const passed = run.steps.filter((step) => step.ok).length;
              return (
                <li
                  key={`${run.root}:${run.name}`}
                  className="rounded-md border border-rule bg-surface"
                >
                  <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule px-3 py-1.5">
                    <span className="font-medium">{run.name}</span>
                    <span className="figures text-2xs text-ink-3">
                      {passed}/{run.steps.length} steps ok · {duration(run.total_duration_s)} ·{" "}
                      <RelativeTime at={run.last_activity} />
                    </span>
                  </header>
                  <ol className="flex flex-col">
                    {run.steps.map((step) => (
                      <li key={step.stem}>
                        <button
                          type="button"
                          onClick={() =>
                            setSelected({
                              root: run.root,
                              project: run.name,
                              step: step.stem,
                              title: `${run.name} · ${step.name}`,
                            })
                          }
                          className={cn(
                            "grid w-full grid-cols-[1.5rem_minmax(0,1fr)_6rem_4.5rem] items-center gap-2 px-3 py-1 text-left hover:bg-hover",
                            selected?.step === step.stem &&
                              selected.project === run.name &&
                              "bg-select",
                          )}
                        >
                          <span className="figures text-2xs text-ink-3">{step.index}</span>
                          <span className="min-w-0">
                            <span className="block truncate text-sm">{step.name}</span>
                            <span className="block truncate text-2xs text-ink-3">
                              {step.harness}
                            </span>
                          </span>
                          <StatusMark tone={step.ok ? "good" : "bad"} strong={!step.ok}>
                            {step.ok
                              ? "ok"
                              : step.timed_out
                                ? "timed out"
                                : `exit ${step.exit_code ?? "?"}`}
                          </StatusMark>
                          <span className="figures text-right text-xs">
                            {duration(step.duration_s)}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ol>
                  {run.unreadable.length ? (
                    <p className="border-t border-rule px-3 py-1 text-2xs text-amber-ink">
                      {run.unreadable.length} step records could not be read:{" "}
                      {run.unreadable.map((item) => item.problem).join("; ")}
                    </p>
                  ) : null}
                  <footer className="border-t border-rule px-3 py-1">
                    <PathText path={run.root} />
                  </footer>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState title="No forge runs found">
            forge writes step-isolated runs under the roots in its forge.toml. Run roots:{" "}
            {state?.roots.map((root) => root.path).join(", ") || "none known"}.{" "}
            <Link href="/settings" className="text-info-ink hover:underline">
              Add a root
            </Link>
          </EmptyState>
        )}
      </Section>
      <div className="min-w-0 xl:sticky xl:top-14 xl:self-start">
        {selected ? (
          <StepOutput key={`${selected.project}:${selected.step}`} step={selected} />
        ) : (
          <p className="pt-8 text-sm text-ink-3">Select a step for its output.</p>
        )}
      </div>
    </div>
  );
}
