// Standards: the compliance checklist against forge with each check's detail
// and diff, the forge commits since the last alignment, and the agent runs
// that bring the project up (or adopt it into forge) on a branch.
import { ChevronRight, RefreshCw, Ruler } from "lucide-react";
import { useState } from "react";
import { useServiceState } from "@/api/stream";
import { Meter, RelativeTime } from "@/components/data";
import { EmptyState, Section } from "@/components/states";
import { Chip, StatusMark } from "@/components/status";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import { BranchRuns } from "../BranchRuns";
import type { TabProps } from "../ProjectPage";

export function StandardsTab({ project }: TabProps) {
  const standards = useServiceState("standards");
  const entry = standards?.projects.find((item) => item.project_id === project.id);
  const report = entry?.report;
  const [open, setOpen] = useState<string | null>(null);
  const context = { projectId: project.id };
  const adopt = project.role === "unmanifested";

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-8 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Section
        title="Compliance with forge"
        description={
          standards?.forge_head
            ? `forge at ${standards.forge_head.slice(0, 8)}: ${standards.forge_head_subject ?? ""}`
            : "forge repo not found"
        }
        actions={
          <>
            <ActionButton
              service="standards"
              action="evaluate"
              context={context}
              size="xs"
              variant="ghost"
              icon={<RefreshCw />}
              label="Re-check"
            />
            {adopt ? (
              <ActionButton
                service="standards"
                action="adopt"
                context={context}
                size="xs"
                variant="default"
                icon={<Ruler />}
                label="Adopt into forge"
              />
            ) : (
              <ActionButton
                service="standards"
                action="bring-up"
                context={context}
                size="xs"
                variant="default"
                icon={<Ruler />}
                label="Bring up to standards"
              />
            )}
          </>
        }
      >
        {entry?.error ? <StatusMark tone="bad">{entry.error}</StatusMark> : null}
        {report ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3">
              <span className="figures-open text-[1.75rem] leading-8 font-semibold">
                {report.passing}
                <span className="text-base text-ink-3">/{report.total}</span>
              </span>
              <Meter
                value={report.total ? report.passing / report.total : 0}
                tone={report.passing === report.total ? "good" : "warn"}
                label="Checks passing"
                className="max-w-56"
              />
              <span className="text-2xs text-ink-3">
                checked <RelativeTime at={entry?.evaluated_at} />
              </span>
            </div>
            <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
              {[...report.checks]
                .sort((a, b) => Number(b.result === "fail") - Number(a.result === "fail"))
                .map((check) => {
                  const expandable = Boolean(check.detail || check.diff);
                  return (
                    <li key={check.id}>
                      <button
                        type="button"
                        disabled={!expandable}
                        aria-expanded={open === check.id}
                        onClick={() => setOpen(open === check.id ? null : check.id)}
                        className="grid w-full grid-cols-[1rem_4.5rem_minmax(0,1fr)_auto] items-center gap-2 px-2.5 py-1.5 text-left enabled:hover:bg-hover"
                      >
                        <ChevronRight
                          aria-hidden
                          className={cn(
                            "size-3.5 text-ink-3",
                            !expandable && "invisible",
                            open === check.id && "rotate-90",
                          )}
                        />
                        <StatusMark
                          tone={
                            check.result === "pass"
                              ? "good"
                              : check.result === "fail"
                                ? "bad"
                                : "neutral"
                          }
                          strong={check.result === "fail"}
                        >
                          {check.result}
                        </StatusMark>
                        <span className="truncate text-sm">{check.title}</span>
                        {check.result === "fail" ? (
                          <Chip title="How bring-up fixes it">
                            {check.fix === "deterministic" ? "auto fix" : "agent fix"}
                          </Chip>
                        ) : (
                          <span />
                        )}
                      </button>
                      {open === check.id ? (
                        <div className="flex flex-col gap-2 border-t border-rule bg-surface-sunk px-3 py-2">
                          {check.detail ? (
                            <p className="text-xs text-ink-2">{check.detail}</p>
                          ) : null}
                          {check.diff ? (
                            <pre className="max-h-80 overflow-auto rounded-sm border border-rule bg-surface px-3 py-2 font-mono text-2xs">
                              {check.diff}
                            </pre>
                          ) : null}
                        </div>
                      ) : null}
                    </li>
                  );
                })}
            </ul>
          </div>
        ) : (
          <EmptyState title="Not evaluated yet">
            Compliance is computed once the forge repo is found and the project is read.
          </EmptyState>
        )}
      </Section>

      <div className="flex flex-col gap-8">
        <Section
          title="forge changes since alignment"
          description={
            report?.template_revision
              ? `aligned to ${report.template_revision.slice(0, 8)}`
              : "never aligned"
          }
        >
          {entry?.forge_commits_since.length ? (
            <ul className="flex max-h-72 flex-col divide-y divide-rule overflow-y-auto">
              {entry.forge_commits_since.map((commit) => (
                <li
                  key={commit.short_sha}
                  className="grid grid-cols-[4.5rem_minmax(0,1fr)_auto] gap-2 py-1 text-sm"
                >
                  <code className="font-mono text-2xs text-ink-3">{commit.short_sha}</code>
                  <span className="truncate">{commit.subject}</span>
                  <span className="text-2xs text-ink-3">
                    <RelativeTime at={commit.committed_at} />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-3">
              {report?.template_revision
                ? "Aligned with forge's HEAD."
                : "Bring-up records the forge revision it aligned to."}
            </p>
          )}
        </Section>
        <Section title="Bring-up runs">
          <BranchRuns
            projectId={project.id}
            kinds={["standards", "adopt"]}
            empty="No bring-up has run. It works on a branch in a worktree and waits for you to merge."
          />
        </Section>
      </div>
    </div>
  );
}
