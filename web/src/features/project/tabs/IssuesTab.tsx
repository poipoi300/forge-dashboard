// Issues and pull requests from GitHub: filterable lists, an issue with its
// rendered body and comments, and every write (create, comment, close,
// reopen, label, delegate to an agent) through the schema-built dialogs.
import { Bot, ExternalLink, MessageSquare, Plus, Tag } from "lucide-react";
import { useState } from "react";
import { useAction } from "@/api/query";
import { RelativeTime } from "@/components/data";
import { Markdown } from "@/components/Markdown";
import { EmptyState, QueryState, Section } from "@/components/states";
import { Chip, StatusMark } from "@/components/status";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { TabProps } from "../ProjectPage";

type IssueState = "open" | "closed" | "all";

function IssueDetail({ projectId, number }: { projectId: string; number: number }) {
  const detail = useAction(
    "/api/services/github/actions/issue",
    { project_id: projectId, number },
    { follow: ["github"] },
  );
  const context = { projectId };
  return (
    <QueryState query={detail} rows={6}>
      {(data) => (
        <article className="flex flex-col gap-3 rounded-md border border-rule bg-surface p-4">
          <header className="flex flex-col gap-1">
            <h3 className="text-base font-semibold">
              {data.issue.title}{" "}
              <span className="font-normal text-ink-3">#{data.issue.number}</span>
            </h3>
            <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
              <StatusMark tone={data.issue.state === "open" ? "good" : "neutral"}>
                {data.issue.state}
              </StatusMark>
              <span>
                {data.issue.author ?? "someone"} opened <RelativeTime at={data.issue.created_at} />
              </span>
              {data.issue.milestone ? <Chip>{data.issue.milestone}</Chip> : null}
              {data.issue.labels.map((label) => (
                <Chip key={label}>{label}</Chip>
              ))}
              <a
                href={data.issue.html_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-info-ink hover:underline"
              >
                on GitHub <ExternalLink className="size-3" />
              </a>
            </div>
          </header>
          <div className="flex flex-wrap gap-1.5">
            <ActionButton
              service="github"
              action="comment"
              context={context}
              input={{ number }}
              size="xs"
              icon={<MessageSquare />}
              label="Comment"
            />
            <ActionButton
              service="github"
              action="set-labels"
              context={context}
              input={{ number }}
              prompt
              size="xs"
              icon={<Tag />}
              label="Labels"
            />
            {data.issue.state === "open" ? (
              <ActionButton
                service="github"
                action="close-issue"
                context={context}
                input={{ number }}
                size="xs"
                label="Close"
              />
            ) : (
              <ActionButton
                service="github"
                action="reopen-issue"
                context={context}
                input={{ number }}
                size="xs"
                label="Reopen"
              />
            )}
            <ActionButton
              service="agents"
              action="delegate-issue"
              context={context}
              input={{ issue_number: number }}
              size="xs"
              variant="default"
              icon={<Bot />}
              label="Delegate to agent"
              title="Run the agent on a branch in an isolated worktree; you review the branch before anything merges"
            />
          </div>
          {data.body ? (
            <Markdown>{data.body}</Markdown>
          ) : (
            <p className="text-sm text-ink-3">No description.</p>
          )}
          {data.comments.length ? (
            <ol className="flex flex-col gap-3 border-t border-rule pt-3">
              {data.comments.map((comment) => (
                <li key={comment.id} className="flex flex-col gap-1">
                  <p className="text-xs text-ink-3">
                    <span className="font-medium text-ink-2">{comment.author ?? "someone"}</span> ·{" "}
                    <RelativeTime at={comment.created_at} />
                  </p>
                  <Markdown>{comment.body}</Markdown>
                </li>
              ))}
              {data.comments_truncated ? (
                <li className="text-xs text-ink-3">Older comments are on GitHub.</li>
              ) : null}
            </ol>
          ) : null}
        </article>
      )}
    </QueryState>
  );
}

function PullRequests({ projectId }: { projectId: string }) {
  const pulls = useAction(
    "/api/services/github/actions/pulls",
    { project_id: projectId, state: "open", limit: 30 },
    { follow: ["github"] },
  );
  return (
    <QueryState query={pulls}>
      {(data) =>
        data.pulls.length ? (
          <ul className="flex flex-col divide-y divide-rule">
            {data.pulls.map((pull) => (
              <li key={pull.number} className="flex items-center justify-between gap-3 py-1.5">
                <a
                  href={pull.html_url}
                  target="_blank"
                  rel="noreferrer"
                  className="min-w-0 hover:underline"
                >
                  <span className="block truncate text-sm">
                    {pull.title} <span className="text-ink-3">#{pull.number}</span>
                  </span>
                  <span className="block truncate font-mono text-2xs text-ink-3">
                    {pull.head_branch} → {pull.base_branch}
                    {pull.draft ? " · draft" : ""}
                  </span>
                </a>
                {pull.checks ? (
                  <StatusMark
                    tone={
                      pull.checks === "passing"
                        ? "good"
                        : pull.checks === "failing"
                          ? "bad"
                          : pull.checks === "pending"
                            ? "info"
                            : "neutral"
                    }
                    strong={pull.checks === "failing"}
                  >
                    checks {pull.checks}
                  </StatusMark>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-3">No open pull requests.</p>
        )
      }
    </QueryState>
  );
}

export function IssuesTab({ project }: TabProps) {
  const [state, setState] = useState<IssueState>("open");
  const [labels, setLabels] = useState("");
  const [assignee, setAssignee] = useState("");
  const [milestone, setMilestone] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const labelList = labels
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const issues = useAction(
    "/api/services/github/actions/issues",
    {
      project_id: project.id,
      state,
      labels: labelList,
      assignee: assignee || null,
      milestone: milestone || null,
      limit: 50,
    },
    { follow: ["github"], enabled: project.issues?.github === "ok" },
  );

  if (project.issues?.github !== "ok") {
    return (
      <EmptyState title="GitHub is not reachable for this project">
        {project.issues?.github === "unauthenticated"
          ? "Sign in to GitHub in Settings; the token is read from git's credential helper or stored in the system keyring."
          : project.issues?.github === "rate-limited"
            ? "The GitHub rate limit is spent. Issues come back when it resets."
            : "The last GitHub request failed. It retries on the next refresh."}
      </EmptyState>
    );
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <div className="flex min-w-0 flex-col gap-6">
        <Section
          title="Issues"
          actions={
            <ActionButton
              service="github"
              action="create-issue"
              context={{ projectId: project.id }}
              size="xs"
              icon={<Plus />}
              label="New issue"
            />
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <ToggleGroup
              type="single"
              size="sm"
              variant="outline"
              value={state}
              onValueChange={(value) => value && setState(value as IssueState)}
            >
              <ToggleGroupItem value="open" className="px-2 text-xs">
                Open
              </ToggleGroupItem>
              <ToggleGroupItem value="closed" className="px-2 text-xs">
                Closed
              </ToggleGroupItem>
              <ToggleGroupItem value="all" className="px-2 text-xs">
                All
              </ToggleGroupItem>
            </ToggleGroup>
            <Input
              aria-label="Labels"
              value={labels}
              onChange={(event) => setLabels(event.target.value)}
              placeholder="labels, comma separated"
              className="h-7 w-44 text-xs"
            />
            <Input
              aria-label="Assignee"
              value={assignee}
              onChange={(event) => setAssignee(event.target.value)}
              placeholder="assignee"
              className="h-7 w-28 text-xs"
            />
            <Input
              aria-label="Milestone"
              value={milestone}
              onChange={(event) => setMilestone(event.target.value)}
              placeholder="milestone"
              className="h-7 w-28 text-xs"
            />
          </div>
          <QueryState query={issues} rows={6}>
            {(data) =>
              data.issues.length ? (
                <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
                  {data.issues.map((issue) => (
                    <li key={issue.number}>
                      <button
                        type="button"
                        onClick={() => setSelected(issue.number)}
                        className={cn(
                          "flex w-full flex-col gap-0.5 px-3 py-2 text-left hover:bg-hover",
                          selected === issue.number && "bg-select",
                        )}
                      >
                        <span className="flex items-baseline justify-between gap-2">
                          <span className="truncate text-sm">
                            {issue.title} <span className="text-ink-3">#{issue.number}</span>
                          </span>
                          <span className="shrink-0 text-2xs text-ink-3">
                            <RelativeTime at={issue.updated_at} />
                          </span>
                        </span>
                        <span className="flex flex-wrap items-center gap-1 text-2xs text-ink-3">
                          {issue.state === "closed" ? <span>closed ·</span> : null}
                          {issue.author ?? ""}
                          {issue.comments ? ` · ${issue.comments} comments` : ""}
                          {issue.labels.map((label) => (
                            <Chip key={label}>{label}</Chip>
                          ))}
                        </span>
                      </button>
                    </li>
                  ))}
                  {data.truncated ? (
                    <li className="px-3 py-1.5 text-2xs text-ink-3">More on GitHub.</li>
                  ) : null}
                </ul>
              ) : (
                <p className="text-sm text-ink-3">No issues match.</p>
              )
            }
          </QueryState>
        </Section>
        <Section title="Open pull requests">
          <PullRequests projectId={project.id} />
        </Section>
      </div>
      <div className="min-w-0">
        {selected !== null ? (
          <IssueDetail key={selected} projectId={project.id} number={selected} />
        ) : (
          <p className="pt-9 text-sm text-ink-3">
            Select an issue to read it, comment, or delegate it to an agent.
          </p>
        )}
      </div>
    </div>
  );
}
