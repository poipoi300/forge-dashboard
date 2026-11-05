// Agent work that lands on a branch (standards bring-up, adopt, delegated
// issues, dependency updates): what it changed, how the checks went, and
// the merge / open PR / discard decision, which stays with the maintainer.
import { GitMerge, GitPullRequest, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useAction } from "@/api/query";
import { DiffView } from "@/components/DiffView";
import { RelativeTime } from "@/components/data";
import { QueryState } from "@/components/states";
import { StatusMark } from "@/components/status";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";

type BranchRun = Schemas["BranchRun"];

const STATE_TONE: Record<string, Tone> = {
  running: "info",
  ready: "info",
  merged: "good",
  "pr-opened": "good",
  discarded: "neutral",
  failed: "bad",
};

function BranchDiff({ runId }: { runId: number }) {
  const diff = useAction(
    "/api/services/agents/actions/branch-diff",
    { run_id: runId },
    { follow: ["agents"] },
  );
  return (
    <QueryState query={diff}>
      {(data) => <DiffView diff={data.diff} empty="The branch has no changes." />}
    </QueryState>
  );
}

function RunCard({ run }: { run: BranchRun }) {
  const [showDiff, setShowDiff] = useState(false);
  const context = { projectId: run.project_id };
  const tone = STATE_TONE[run.state] ?? "neutral";
  return (
    <li className="flex flex-col gap-2 px-3 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <StatusMark tone={tone} strong={tone === "bad"}>
            {run.state}
          </StatusMark>
          <span className="truncate text-sm font-medium">{run.title}</span>
        </span>
        <span className="text-2xs text-ink-3">
          {run.kind} · <RelativeTime at={run.updated_at} />
        </span>
      </div>
      <p className="font-mono text-2xs text-ink-3">
        {run.branch} from {run.base_branch}@{run.base_sha.slice(0, 8)} · {run.files_changed} files
        {run.agent_attempts ? ` · agent ×${run.agent_attempts}` : ""}
      </p>
      {run.compliance_before && run.compliance_after ? (
        <p className="figures text-xs">
          standards {run.compliance_before.passing}/{run.compliance_before.total} →{" "}
          <span className="font-semibold">
            {run.compliance_after.passing}/{run.compliance_after.total}
          </span>
          {run.still_failing.length ? (
            <span className="text-amber-ink"> · still failing: {run.still_failing.join(", ")}</span>
          ) : null}
        </p>
      ) : null}
      {run.summary ? <p className="text-xs text-ink-2">{run.summary}</p> : null}
      {run.error ? <p className="text-xs text-signal-ink">{run.error}</p> : null}
      {run.checks.length ? (
        <div className="flex flex-wrap gap-2">
          {run.checks.map((check) => (
            <StatusMark
              key={check.check_id}
              tone={
                check.outcome === "passed" ? "good" : check.outcome === "never" ? "neutral" : "bad"
              }
            >
              {check.label}
            </StatusMark>
          ))}
        </div>
      ) : null}
      <div className="flex flex-wrap gap-1.5">
        {run.state === "ready" ? (
          <>
            <ActionButton
              service="agents"
              action="merge-branch"
              context={context}
              input={{ run_id: run.id }}
              size="xs"
              variant="default"
              icon={<GitMerge />}
              label="Merge"
            />
            <ActionButton
              service="agents"
              action="open-pr"
              context={context}
              input={{ run_id: run.id }}
              size="xs"
              icon={<GitPullRequest />}
              label="Open PR…"
            />
            <ActionButton
              service="agents"
              action="discard-branch"
              context={context}
              input={{ run_id: run.id }}
              size="xs"
              variant="ghost"
              icon={<Trash2 />}
              label="Discard"
              confirmText={run.branch}
            />
          </>
        ) : null}
        {run.pr_url ? (
          <Button asChild variant="link" size="xs">
            <a href={run.pr_url} target="_blank" rel="noreferrer">
              Pull request
            </a>
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="xs"
          onClick={() => setShowDiff((value) => !value)}
          aria-expanded={showDiff}
        >
          {showDiff ? "Hide diff" : "Show diff"}
        </Button>
      </div>
      {showDiff ? <BranchDiff runId={run.id} /> : null}
    </li>
  );
}

export function BranchRuns({
  projectId,
  kinds,
  empty,
}: {
  projectId: string;
  kinds: string[];
  empty: string;
}) {
  const runs = useAction(
    "/api/services/agents/actions/branch-runs",
    { project_id: projectId, kind: null },
    { follow: ["agents"] },
  );
  return (
    <QueryState query={runs}>
      {(data) => {
        const shown = data.runs.filter((run) => kinds.includes(run.kind));
        return shown.length ? (
          <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
            {shown.map((run) => (
              <RunCard key={run.id} run={run} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-3">{empty}</p>
        );
      }}
    </QueryState>
  );
}
