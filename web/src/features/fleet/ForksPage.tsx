// Every fork in one table: how far behind upstream, whether a sync would
// conflict, the last run, the next scheduled one, and the parked runs that
// need a decision.
import { GitMerge, Pause, Play } from "lucide-react";
import { Link } from "wouter";
import { useServiceState } from "@/api/stream";
import { usePageTitle } from "@/app/title";
import { RelativeTime } from "@/components/data";
import { EmptyState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import { useProjectLabel } from "../project/names";
import { ForkRunState, ParkedRunActions } from "../project/tabs/ForkTab";

export function ForksPage() {
  usePageTitle("Forks");
  const forks = useServiceState("forks")?.forks ?? [];
  const scheduler = useServiceState("scheduler");
  const nameOf = useProjectLabel();
  const tracked = forks.filter((fork) => fork.is_fork || fork.upstream_url);
  const parked = tracked.filter(
    (fork) =>
      fork.last_run &&
      ["conflict", "needs-attention", "ready-to-apply"].includes(fork.last_run.state),
  );

  return (
    <div className="max-w-[100rem] flex flex-col gap-8 px-5 py-4">
      <h1 className="sr-only">Forks</h1>
      {parked.length ? (
        <Section
          title="Waiting on you"
          description="Parked runs keep their worktree until you apply, retry, or abort."
        >
          <ul className="flex flex-col divide-y divide-rule rounded-md border border-amber/40 bg-surface">
            {parked.map((fork) =>
              fork.last_run ? (
                <li
                  key={fork.project_id}
                  className="flex flex-wrap items-center justify-between gap-3 px-3 py-2"
                >
                  <span className="flex min-w-0 items-center gap-3">
                    <Link
                      href={`/p/${fork.project_id}/fork`}
                      className="font-medium hover:underline"
                    >
                      {nameOf(fork.project_id)}
                    </Link>
                    <ForkRunState state={fork.last_run.state} />
                    <span className="truncate text-xs text-ink-2">{fork.last_run.detail}</span>
                  </span>
                  <ParkedRunActions run={fork.last_run} projectId={fork.project_id} />
                </li>
              ) : null,
            )}
          </ul>
        </Section>
      ) : null}

      <Section
        title="Forks"
        description={
          scheduler?.paused ? "The scheduler is paused: no sync runs on its own." : undefined
        }
        actions={
          scheduler?.paused ? (
            <ActionButton
              service="scheduler"
              action="resume"
              size="xs"
              icon={<Play />}
              label="Resume schedules"
            />
          ) : (
            <ActionButton
              service="scheduler"
              action="pause"
              size="xs"
              variant="ghost"
              icon={<Pause />}
              label="Pause all schedules"
            />
          )
        }
      >
        {tracked.length ? (
          <div className="overflow-x-auto rounded-md border border-rule bg-surface">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                  <th className="px-3 py-1.5 font-medium">fork</th>
                  <th className="px-2 py-1.5 font-medium">upstream</th>
                  <th className="px-2 py-1.5 text-right font-medium">behind</th>
                  <th className="px-2 py-1.5 text-right font-medium">ahead</th>
                  <th className="px-2 py-1.5 font-medium">prediction</th>
                  <th className="px-2 py-1.5 font-medium">last sync</th>
                  <th className="px-2 py-1.5 font-medium">next</th>
                  <th className="px-2 py-1.5">
                    <span className="sr-only">sync</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {tracked.map((fork) => (
                  <tr key={fork.project_id} className="border-b border-rule last:border-b-0">
                    <td className="px-3 py-1.5">
                      <Link
                        href={`/p/${fork.project_id}/fork`}
                        className="font-medium hover:underline"
                      >
                        {nameOf(fork.project_id)}
                      </Link>
                      <span className="block font-mono text-2xs text-ink-3">
                        {fork.target_branch}
                      </span>
                    </td>
                    <td
                      className="max-w-[16rem] truncate px-2 py-1.5 font-mono text-2xs"
                      title={fork.upstream_url ?? ""}
                    >
                      {fork.upstream_url?.replace(/^https?:\/\/(www\.)?github\.com\//, "") ?? "—"}
                      {fork.upstream_branch ? `@${fork.upstream_branch}` : ""}
                    </td>
                    <td
                      className={cn(
                        "figures px-2 py-1.5 text-right",
                        (fork.behind ?? 0) > 0 && "font-semibold text-amber-ink",
                      )}
                    >
                      {fork.behind ?? "—"}
                    </td>
                    <td className="figures px-2 py-1.5 text-right">{fork.ahead ?? "—"}</td>
                    <td className="px-2 py-1.5">
                      {fork.prediction ? (
                        fork.prediction.clean ? (
                          <StatusMark tone="good">clean</StatusMark>
                        ) : (
                          <StatusMark
                            tone="warn"
                            title={fork.prediction.conflicted_files.join(", ")}
                          >
                            {plural(fork.prediction.conflicted_files.length, "conflict")}
                          </StatusMark>
                        )
                      ) : (
                        <span className="text-ink-3">—</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      {fork.last_run ? (
                        <span className="flex flex-col">
                          <ForkRunState state={fork.last_run.state} />
                          <span className="text-2xs text-ink-3">
                            <RelativeTime at={fork.last_run.started_at} />
                          </span>
                        </span>
                      ) : (
                        <span className="text-ink-3">never</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-xs">
                      {fork.next_sync_at ? (
                        <RelativeTime at={fork.next_sync_at} />
                      ) : (
                        <span className="text-ink-3">manual</span>
                      )}
                    </td>
                    <td className="px-2 py-1 text-right">
                      <ActionButton
                        service="forks"
                        action="sync"
                        context={{ projectId: fork.project_id }}
                        size="xs"
                        icon={<GitMerge />}
                        label="Sync"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No forks">
            A project is a fork when GitHub says so or it has an{" "}
            <code className="font-mono text-xs">upstream</code> remote. Set an upstream from a
            project's Settings tab to track one.
          </EmptyState>
        )}
      </Section>
    </div>
  );
}
