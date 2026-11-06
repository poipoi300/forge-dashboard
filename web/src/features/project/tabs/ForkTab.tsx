// Fork management for one project: where upstream is and how far behind,
// a conflict prediction before syncing, the commits and files a sync would
// bring, the per-fork policy, and every run with its parked-run actions.
import { Bot, Check, GitMerge, Play, Settings2, X } from "lucide-react";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { DiffView } from "@/components/DiffView";
import { compactPath, RelativeTime } from "@/components/data";
import { EmptyState, QueryState, Section } from "@/components/states";
import { InfoNote, StatusMark } from "@/components/status";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { plural } from "@/lib/format";
import { duration } from "@/lib/time";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";
import type { TabProps } from "../ProjectPage";

type ForkRun = Schemas["ForkRun"];

export const FORK_RUN_TONE: Record<string, Tone> = {
  running: "info",
  synced: "good",
  "up-to-date": "good",
  conflict: "warn",
  "needs-attention": "warn",
  "ready-to-apply": "info",
  failed: "bad",
  aborted: "neutral",
};
const PARKED = new Set(["conflict", "needs-attention", "ready-to-apply"]);

/** A run's one-line detail; a conflict is counted here, so the wording never depends on stored text. */
export function runDetail(run: ForkRun): string | null {
  if (run.state === "conflict" && run.conflicted_files.length) {
    return plural(run.conflicted_files.length, "conflicted file");
  }
  return run.detail ?? null;
}

export function ForkRunState({ state }: { state: string }) {
  const tone = FORK_RUN_TONE[state] ?? "neutral";
  return (
    <StatusMark tone={tone} strong={tone === "bad"}>
      {state}
    </StatusMark>
  );
}

/** The actions a parked run offers: apply what is ready, re-run the agent with guidance, or abort. */
export function ParkedRunActions({ run, projectId }: { run: ForkRun; projectId: string }) {
  if (!PARKED.has(run.state)) return null;
  const context = { projectId };
  return (
    <div className="flex flex-wrap gap-1.5">
      {run.state === "ready-to-apply" ? (
        <ActionButton
          service="forks"
          action="apply"
          context={context}
          input={{ run_id: run.id }}
          size="xs"
          variant="default"
          icon={<Check />}
          label="Apply"
        />
      ) : null}
      <ActionButton
        service="forks"
        action="retry-agent"
        context={context}
        input={{ run_id: run.id }}
        size="xs"
        icon={<Bot />}
        label="Re-run agent…"
      />
      <ActionButton
        service="forks"
        action="abort"
        context={context}
        input={{ run_id: run.id }}
        size="xs"
        variant="ghost"
        icon={<X />}
        label="Abort"
        confirmText={run.id}
      />
    </div>
  );
}

function RunDiff({ runId }: { runId: string }) {
  const [against, setAgainst] = useState<"target" | "upstream">("target");
  const diff = useAction(
    "/api/services/forks/actions/run-diff",
    { run_id: runId, against },
    { follow: ["forks"] },
  );
  return (
    <div className="flex flex-col gap-2">
      <ToggleGroup
        type="single"
        size="sm"
        variant="outline"
        value={against}
        onValueChange={(value) => value && setAgainst(value as "target" | "upstream")}
      >
        <ToggleGroupItem value="target" className="px-2 text-xs">
          Against your branch
        </ToggleGroupItem>
        <ToggleGroupItem value="upstream" className="px-2 text-xs">
          Against upstream
        </ToggleGroupItem>
      </ToggleGroup>
      <QueryState query={diff} rows={6}>
        {(data) => <DiffView diff={data.diff} empty="The run's branch matches." />}
      </QueryState>
    </div>
  );
}

function RunRow({
  run,
  projectId,
  selected,
  onSelect,
}: {
  run: ForkRun;
  projectId: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li className={cn("flex flex-col gap-1.5 px-3 py-2", selected && "bg-select")}>
      <button
        type="button"
        onClick={onSelect}
        className="-my-1 flex w-full flex-wrap items-center justify-between gap-2 py-1 text-left"
      >
        <span className="flex items-center gap-2">
          <ForkRunState state={run.state} />
          <span className="text-xs text-ink-2">
            {run.strategy} {run.upstream_ref} into {run.target_branch}
          </span>
        </span>
        <span className="figures text-2xs text-ink-3">
          <RelativeTime at={run.started_at} />
          {run.duration_s !== null ? ` · ${duration(run.duration_s)}` : ""}
          {run.agent_attempts ? ` · agent ×${run.agent_attempts}` : ""}
          {run.agent_cost_usd ? ` · $${run.agent_cost_usd.toFixed(2)}` : ""}
          {run.pushed ? " · pushed" : ""}
        </span>
      </button>
      {runDetail(run) ? <p className="text-xs text-ink-2">{runDetail(run)}</p> : null}
      {run.conflicted_files.length ? (
        <p className="text-2xs text-ink-3">
          conflicts in <span className="font-mono">{run.conflicted_files.join(", ")}</span>
        </p>
      ) : null}
      <ParkedRunActions run={run} projectId={projectId} />
    </li>
  );
}

export function ForkTab({ project }: TabProps) {
  const status = useServiceState("forks")?.forks.find((item) => item.project_id === project.id);
  // A pure read: fetching upstream is the explicit Fetch action, so following the forks
  // state never makes this query change the state it follows.
  const preview = useAction(
    "/api/services/forks/actions/preview",
    { project_id: project.id },
    { follow: ["forks"] },
  );
  const history = useAction(
    "/api/services/forks/actions/history",
    { project_id: project.id, limit: 30 },
    { follow: ["forks"] },
  );
  const [selected, setSelected] = useState<string | null>(null);
  const context = { projectId: project.id };

  if (!status)
    return (
      <EmptyState title="No fork status yet">
        The fork service reads upstream after the first refresh.
      </EmptyState>
    );
  const selectedRun = history.data?.runs.find((run) => run.id === selected) ?? null;
  const parked = status.last_run && PARKED.has(status.last_run.state) ? status.last_run : null;

  return (
    <div className="flex flex-col gap-8">
      {parked ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber/50 bg-amber-wash px-3 py-2"
        >
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="flex items-center gap-2 text-sm font-medium">
              <ForkRunState state={parked.state} />
              {parked.conflicted_files.length
                ? `in ${parked.conflicted_files.join(", ")}`
                : (parked.detail ?? "")}
            </span>
            <span className="text-xs text-ink-2">
              The run is parked in its worktree. Apply it, give the agent another try, or abort it
              before syncing again.
            </span>
          </div>
          <ParkedRunActions run={parked} projectId={project.id} />
        </div>
      ) : null}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-3 rounded-md border border-rule bg-surface p-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="flex flex-col gap-1">
          <p className="text-sm">
            upstream{" "}
            <code className="font-mono text-xs" title={status.upstream_url ?? undefined}>
              {status.upstream_url ? compactPath(status.upstream_url) : "not set"}
            </code>
            {status.upstream_branch ? (
              <>
                {" "}
                at <code className="font-mono text-xs">{status.upstream_branch}</code>
              </>
            ) : null}
            <span className="text-ink-3"> · found via {status.source}</span>
          </p>
          <p className="figures flex flex-wrap gap-x-4 text-sm">
            <span className={cn((status.behind ?? 0) > 0 && "font-semibold text-amber-ink")}>
              {status.behind ?? "?"} behind
            </span>
            <span>{status.ahead ?? "?"} ahead</span>
            <span className="text-ink-3">
              fetched <RelativeTime at={status.fetched_at} />
            </span>
            <span className="text-ink-3">
              next sync {status.next_sync_at ? <RelativeTime at={status.next_sync_at} /> : "manual"}
            </span>
          </p>
          {status.prediction ? (
            status.prediction.clean ? (
              <StatusMark tone="good">
                a sync merges cleanly (checked <RelativeTime at={status.prediction.checked_at} />)
              </StatusMark>
            ) : (
              <StatusMark tone="warn">
                expect conflicts in {status.prediction.conflicted_files.length} file
                {status.prediction.conflicted_files.length === 1 ? "" : "s"}:{" "}
                {status.prediction.conflicted_files.slice(0, 4).join(", ")}
              </StatusMark>
            )
          ) : null}
          {status.error ? <StatusMark tone="bad">{status.error}</StatusMark> : null}
        </div>
        <div className="flex flex-wrap items-start gap-1.5">
          <ActionButton
            service="forks"
            action="sync"
            context={context}
            variant={parked ? "outline" : "default"}
            size="sm"
            icon={<GitMerge />}
            label="Sync now"
            title={
              parked
                ? "A parked run is waiting: apply, retry, or abort it first"
                : "Merge or rebase upstream in an isolated worktree, then run the checks"
            }
          />
          <ActionButton
            service="forks"
            action="set-policy"
            context={context}
            defaults={{ policy: status.policy }}
            prompt
            size="sm"
            icon={<Settings2 />}
            label="Policy…"
          />
        </div>
      </div>

      <InfoNote>
        Syncs run in an isolated worktree on a forge-sync branch; your working tree is never
        touched. Conflicts go to the agent (
        {status.policy.conflicts === "agent"
          ? `up to ${status.policy.max_agent_attempts} attempts`
          : "off: the run stops and notifies"}
        ), then the checks run before anything lands
        {status.policy.auto_push ? " and is pushed" : ""}.
      </InfoNote>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-2">
        <Section
          title="What a sync brings"
          description={
            status.fetched_at
              ? undefined
              : "Upstream has not been fetched yet; this may be out of date."
          }
          actions={
            <ActionButton
              service="forks"
              action="refresh-status"
              context={context}
              size="xs"
              variant="ghost"
              label="Fetch upstream"
              title="Fetch upstream now, then recount and re-predict"
            />
          }
        >
          <QueryState query={preview}>
            {(data) =>
              data.commits.length ? (
                <div className="flex flex-col gap-3">
                  <ul className="flex max-h-80 flex-col divide-y divide-rule overflow-y-auto">
                    {data.commits.map((commit) => (
                      <li
                        key={commit.short_sha}
                        className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2 py-1"
                      >
                        <code className="font-mono text-2xs text-ink-3">{commit.short_sha}</code>
                        <span className="min-w-0">
                          <span className="block truncate text-sm">{commit.subject}</span>
                          <span className="block text-2xs text-ink-3">
                            {commit.author} · <RelativeTime at={commit.committed_at} />
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                  <details>
                    <summary className="cursor-pointer text-xs text-ink-2">
                      {plural(data.files.length, "file")} change
                    </summary>
                    <ul className="mt-1 flex flex-col">
                      {data.files.map((file) => (
                        <li key={file.path} className="font-mono text-2xs">
                          <span className="inline-block w-5 text-ink-3">{file.status}</span>
                          {file.path}
                        </li>
                      ))}
                    </ul>
                  </details>
                </div>
              ) : (
                <p className="text-sm text-clear-ink">Up to date with upstream.</p>
              )
            }
          </QueryState>
        </Section>

        <Section title="Runs">
          <QueryState query={history}>
            {(data) =>
              data.runs.length ? (
                <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
                  {data.runs.map((run) => (
                    <RunRow
                      key={run.id}
                      run={run}
                      projectId={project.id}
                      selected={selected === run.id}
                      onSelect={() => setSelected(run.id)}
                    />
                  ))}
                </ul>
              ) : (
                <EmptyState
                  title="No syncs yet"
                  action={
                    <ActionButton
                      service="forks"
                      action="sync"
                      context={context}
                      size="xs"
                      icon={<Play />}
                      label="Sync now"
                    />
                  }
                />
              )
            }
          </QueryState>
        </Section>
      </div>

      {selectedRun?.branch ? (
        <Section title={`Diff of ${selectedRun.branch}`}>
          <RunDiff runId={selectedRun.id} />
        </Section>
      ) : null}
    </div>
  );
}
