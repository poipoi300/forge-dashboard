// Git: working-tree changes with their diff, branches, recent commits with
// diffstat, worktrees, stashes, tags and releases, and the git actions.
// Pull only fast-forwards; push is always explicit.
import { ArrowDown, ArrowUp, Download, GitBranchPlus, Upload } from "lucide-react";
import { useState } from "react";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { DiffView } from "@/components/DiffView";
import { PathText, RelativeTime } from "@/components/data";
import { EmptyState, QueryState, Section } from "@/components/states";
import { Chip, StatusMark } from "@/components/status";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { TabProps } from "../ProjectPage";

type DiffTarget = { path: string | null; staged: boolean; commit: string | null; title: string };

function DiffPanel({ projectId, target }: { projectId: string; target: DiffTarget }) {
  const diff = useAction(
    "/api/services/git/actions/diff",
    { project_id: projectId, path: target.path, staged: target.staged, commit: target.commit },
    { follow: ["git"] },
  );
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-ink-3">{target.title}</p>
      <QueryState query={diff} rows={8}>
        {(data) => <DiffView diff={data.diff} />}
      </QueryState>
    </div>
  );
}

const KIND_TONE: Record<string, "good" | "bad" | "warn" | "info" | "neutral"> = {
  added: "good",
  untracked: "good",
  deleted: "bad",
  conflicted: "bad",
  modified: "info",
  renamed: "info",
  copied: "info",
  "type-changed": "warn",
};

function Releases({ projectId }: { projectId: string }) {
  const releases = useAction(
    "/api/services/github/actions/releases",
    { project_id: projectId },
    { follow: ["github"] },
  );
  return (
    <QueryState query={releases}>
      {(data) =>
        data.releases.length ? (
          <ul className="flex flex-col gap-0.5 border-t border-rule pt-2">
            {data.releases.slice(0, 8).map((release) => (
              <li key={release.id} className="flex justify-between gap-2 text-xs">
                <a
                  href={release.html_url}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate hover:underline"
                >
                  {release.name || release.tag}
                  {release.prerelease ? <span className="text-ink-3"> · pre-release</span> : null}
                  {release.draft ? <span className="text-ink-3"> · draft</span> : null}
                </a>
                <span className="shrink-0 text-2xs text-ink-3">
                  <RelativeTime at={release.published_at} />
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-ink-3">No GitHub releases.</p>
        )
      }
    </QueryState>
  );
}

export function GitTab({ project }: TabProps) {
  const repo = useServiceState("git")?.repos.find((item) => item.project_id === project.id);
  const context = { projectId: project.id };
  const [target, setTarget] = useState<DiffTarget | null>(null);
  const changes = useAction(
    "/api/services/git/actions/changes",
    { project_id: project.id },
    { follow: ["git"] },
  );
  const branches = useAction(
    "/api/services/git/actions/branches",
    { project_id: project.id },
    { follow: ["git"] },
  );
  const commits = useAction(
    "/api/services/git/actions/commits",
    { project_id: project.id, revision: null, limit: 30 },
    { follow: ["git"] },
  );
  const worktrees = useAction(
    "/api/services/git/actions/worktrees",
    { project_id: project.id },
    { follow: ["git"] },
  );
  const stashes = useAction(
    "/api/services/git/actions/stashes",
    { project_id: project.id },
    { follow: ["git"] },
  );
  const tags = useAction(
    "/api/services/git/actions/tags",
    { project_id: project.id },
    { follow: ["git"] },
  );

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-rule bg-surface px-3 py-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span>
            on <code className="font-mono text-xs font-semibold">{repo?.branch ?? "detached"}</code>
            {repo?.upstream ? <span className="text-ink-3"> tracking {repo.upstream}</span> : null}
          </span>
          {repo?.ahead ? (
            <span className="figures inline-flex items-center gap-0.5 text-xs">
              <ArrowUp className="size-3" />
              {repo.ahead} ahead
            </span>
          ) : null}
          {repo?.behind ? (
            <span className="figures inline-flex items-center gap-0.5 text-xs text-amber-ink">
              <ArrowDown className="size-3" />
              {repo.behind} behind
            </span>
          ) : null}
          <span className="figures text-xs text-ink-3">{repo?.dirty ?? 0} changed files</span>
          {repo?.error ? <StatusMark tone="bad">{repo.error}</StatusMark> : null}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <ActionButton
            service="git"
            action="fetch"
            context={context}
            size="xs"
            icon={<Download />}
            label="Fetch"
          />
          <ActionButton
            service="git"
            action="pull"
            context={context}
            size="xs"
            icon={<ArrowDown />}
            label="Pull (fast-forward)"
          />
          <ActionButton
            service="git"
            action="push"
            context={context}
            input={{ remote: null, branch: null }}
            prompt
            size="xs"
            icon={<Upload />}
            label="Push…"
          />
          <ActionButton
            service="git"
            action="create-branch"
            context={context}
            size="xs"
            icon={<GitBranchPlus />}
            label="New branch"
          />
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <Section title="Working tree">
          <QueryState query={changes}>
            {(data) =>
              data.files.length ? (
                <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
                  <li>
                    <button
                      type="button"
                      onClick={() =>
                        setTarget({
                          path: null,
                          staged: false,
                          commit: null,
                          title: "All unstaged changes",
                        })
                      }
                      className="w-full px-2.5 py-1 text-left text-xs text-info-ink hover:bg-hover"
                    >
                      Show all unstaged changes
                    </button>
                  </li>
                  {data.files.map((file) => (
                    <li key={`${file.path}-${file.staged}`}>
                      <button
                        type="button"
                        onClick={() =>
                          setTarget({
                            path: file.path,
                            staged: file.staged,
                            commit: null,
                            title: `${file.path}${file.staged ? " (staged)" : ""}`,
                          })
                        }
                        className={cn(
                          "flex w-full items-center justify-between gap-2 px-2.5 py-1 text-left hover:bg-hover",
                          target?.path === file.path && "bg-select",
                        )}
                      >
                        <span className="truncate font-mono text-2xs">{file.path}</span>
                        <span className="flex shrink-0 items-center gap-1">
                          {file.staged ? <Chip>staged</Chip> : null}
                          <StatusMark tone={KIND_TONE[file.kind] ?? "neutral"}>
                            {file.kind}
                          </StatusMark>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-ink-3">Clean. Nothing to commit.</p>
              )
            }
          </QueryState>
        </Section>
        <Section title="Diff">
          {target ? (
            <DiffPanel key={JSON.stringify(target)} projectId={project.id} target={target} />
          ) : (
            <p className="text-sm text-ink-3">Pick a file or a commit to see its diff.</p>
          )}
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Recent commits">
          <QueryState query={commits}>
            {(data) => (
              <ul className="flex flex-col divide-y divide-rule">
                {data.commits.map((commit) => (
                  <li key={commit.sha}>
                    <button
                      type="button"
                      onClick={() =>
                        setTarget({
                          path: null,
                          staged: false,
                          commit: commit.sha,
                          title: `${commit.short_sha} ${commit.subject}`,
                        })
                      }
                      className="grid w-full grid-cols-[4.5rem_minmax(0,1fr)_auto] items-baseline gap-2 py-1 text-left hover:bg-hover"
                    >
                      <code className="font-mono text-2xs text-ink-3">{commit.short_sha}</code>
                      <span className="min-w-0">
                        <span className="block truncate text-sm">{commit.subject}</span>
                        <span className="block truncate text-2xs text-ink-3">
                          {commit.author} · <RelativeTime at={commit.committed_at} />
                        </span>
                      </span>
                      <span className="figures text-2xs whitespace-nowrap">
                        {commit.files_changed} files{" "}
                        <span className="text-clear-ink">+{commit.insertions}</span>{" "}
                        <span className="text-signal-ink">−{commit.deletions}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </Section>
        <Section title="Branches">
          <QueryState query={branches}>
            {(data) => (
              <ul className="flex flex-col divide-y divide-rule">
                {data.branches.map((branch) => (
                  <li
                    key={branch.name}
                    className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-2 py-1"
                  >
                    <span className="min-w-0">
                      <code className={cn("font-mono text-xs", branch.current && "font-semibold")}>
                        {branch.current ? "● " : ""}
                        {branch.name}
                      </code>
                      <span className="block truncate text-2xs text-ink-3">
                        {branch.subject} · <RelativeTime at={branch.committed_at} />
                      </span>
                    </span>
                    <span className="figures text-2xs text-ink-2">
                      {branch.gone ? (
                        <span className="text-amber-ink">upstream gone</span>
                      ) : branch.upstream ? (
                        `↑${branch.ahead ?? 0} ↓${branch.behind ?? 0}`
                      ) : (
                        <span className="text-ink-3">local</span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </Section>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Section title="Worktrees">
          <QueryState query={worktrees}>
            {(data) => (
              <ul className="flex flex-col gap-1.5">
                {data.worktrees.map((worktree) => (
                  <li key={worktree.path} className="flex flex-col">
                    <PathText path={worktree.path} />
                    <span className="text-2xs text-ink-3">
                      {worktree.branch ?? (worktree.detached ? "detached" : "")}
                      {worktree.dashboard_owned ? " · dashboard run" : ""}
                      {worktree.locked ? " · locked" : ""}
                      {worktree.prunable ? " · prunable" : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </Section>
        <Section title="Stashes">
          <QueryState query={stashes}>
            {(data) =>
              data.stashes.length ? (
                <ul className="flex flex-col gap-1">
                  {data.stashes.map((stash) => (
                    <li key={stash.name} className="text-xs">
                      <code className="font-mono text-2xs text-ink-3">{stash.name}</code>{" "}
                      {stash.message}
                      <span className="text-2xs text-ink-3">
                        {" "}
                        · <RelativeTime at={stash.created_at} />
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-ink-3">No stashes.</p>
              )
            }
          </QueryState>
        </Section>
        <Section title="Tags and releases">
          <QueryState query={tags}>
            {(data) =>
              data.tags.length ? (
                <div className="flex flex-col gap-1">
                  {data.commits_since_latest !== null ? (
                    <p className="figures text-xs text-ink-2">
                      {data.commits_since_latest} commits since the latest tag
                    </p>
                  ) : null}
                  <ul className="flex flex-col gap-0.5">
                    {data.tags.slice(0, 12).map((tag) => (
                      <li key={tag.name} className="flex justify-between gap-2 text-xs">
                        <code className="font-mono">{tag.name}</code>
                        <span className="text-2xs text-ink-3">
                          {tag.short_sha} · <RelativeTime at={tag.created_at} />
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <EmptyState title="No tags">
                  Tag a release to track the commits since it.
                </EmptyState>
              )
            }
          </QueryState>
          {project.issues?.github === "ok" ? <Releases projectId={project.id} /> : null}
        </Section>
      </div>
    </div>
  );
}
