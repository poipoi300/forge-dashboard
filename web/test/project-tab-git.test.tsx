import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { GitTab } from "@/features/project/tabs/GitTab";
import { NOW_SECONDS } from "./constants";
import { makeProject } from "./factories";
import {
  formField,
  installHarness,
  type MockApi,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";
import {
  makeBranch,
  makeCommit,
  makeDetail,
  makeFileChange,
  makeGithubProject,
  makeRelease,
  makeRepoState,
  makeStash,
  makeTag,
  makeUnifiedDiff,
  makeWorktree,
} from "./project-tabs-support";

installHarness();

const CHANGES = "/api/services/git/actions/changes" as const;
const BRANCHES = "/api/services/git/actions/branches" as const;
const COMMITS = "/api/services/git/actions/commits" as const;
const WORKTREES = "/api/services/git/actions/worktrees" as const;
const STASHES = "/api/services/git/actions/stashes" as const;
const TAGS = "/api/services/git/actions/tags" as const;
const DIFF = "/api/services/git/actions/diff" as const;
const FETCH = "/api/services/git/actions/fetch" as const;
const PULL = "/api/services/git/actions/pull" as const;
const PUSH = "/api/services/git/actions/push" as const;
const CREATE_BRANCH = "/api/services/git/actions/create-branch" as const;
const RELEASES = "/api/services/github/actions/releases" as const;

interface GitData {
  files?: ReturnType<typeof makeFileChange>[];
  branches?: ReturnType<typeof makeBranch>[];
  commits?: ReturnType<typeof makeCommit>[];
  worktrees?: ReturnType<typeof makeWorktree>[];
  stashes?: ReturnType<typeof makeStash>[];
  tags?: ReturnType<typeof makeTag>[];
  commitsSinceLatest?: number | null;
}

/** Route every read the Git tab makes; unspecified collections answer empty. */
function mockGit(data: GitData = {}): MockApi {
  return mockApi()
    .action(CHANGES, { branch: "main", files: data.files ?? [] })
    .action(BRANCHES, { branches: data.branches ?? [] })
    .action(COMMITS, { commits: data.commits ?? [] })
    .action(WORKTREES, { worktrees: data.worktrees ?? [] })
    .action(STASHES, { stashes: data.stashes ?? [] })
    .action(TAGS, { tags: data.tags ?? [], commits_since_latest: data.commitsSinceLatest ?? null });
}

const project = makeProject("alpha");

function renderGit(subject = project) {
  return renderWithProviders(<GitTab project={subject} detail={makeDetail(subject)} />);
}

function sectionOf(title: string): ReturnType<typeof within> {
  const heading = screen.getByRole("heading", { name: title });
  const section = heading.closest("section");
  if (!section) throw new Error(`no section titled ${title}`);
  return within(section);
}

describe("GitTab header", () => {
  test("shows branch, upstream, divergence and the changed file count from the live git state", async () => {
    mockGit();
    seedStream({
      git: {
        repos: [
          makeRepoState("alpha", {
            branch: "feature/x",
            upstream: "origin/feature/x",
            ahead: 2,
            behind: 3,
            dirty: 5,
          }),
          makeRepoState("beta", { branch: "other", dirty: 9 }),
        ],
      },
    });
    renderGit();
    expect(await screen.findByText("feature/x")).toBeTruthy();
    expect(screen.getByText("tracking origin/feature/x")).toBeTruthy();
    expect(screen.getByText("2 ahead")).toBeTruthy();
    expect(screen.getByText("3 behind")).toBeTruthy();
    expect(screen.getByText("5 changed files")).toBeTruthy();
    expect(screen.queryByText("other")).toBeNull();
  });

  test("omits divergence and tracking text for a branch in sync and shows a repo error", async () => {
    mockGit();
    seedStream({
      git: {
        repos: [makeRepoState("alpha", { ahead: 0, behind: 0, error: "not a git repository" })],
      },
    });
    renderGit();
    expect(await screen.findByText("not a git repository")).toBeTruthy();
    expect(screen.queryByText(/ahead/)).toBeNull();
    expect(screen.queryByText(/behind/)).toBeNull();
    expect(screen.queryByText(/tracking/)).toBeNull();
    expect(screen.getByText("0 changed files")).toBeTruthy();
  });

  test("reads as a detached head with no changes before the git state arrives", async () => {
    mockGit();
    renderGit();
    expect(await screen.findByText("detached")).toBeTruthy();
    expect(screen.getByText("0 changed files")).toBeTruthy();
  });
});

describe("GitTab actions", () => {
  test("fetch runs immediately for this project and announces the started job", async () => {
    const api = mockGit().action(FETCH, { job_id: "job-0", kind: "git", title: "git fetch" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Fetch" }));
    expect(await screen.findByText("Started: git fetch")).toBeTruthy();
    expect(api.sent(FETCH)).toEqual([{ project_id: "alpha" }]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("pull asks for confirmation and only then sends the project", async () => {
    const api = mockGit().action(PULL, {
      job_id: "job-1",
      kind: "git",
      title: "git pull --ff-only",
    });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Pull (fast-forward)" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(PULL)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Pull (fast-forward only)" }));
    await waitFor(() => expect(api.sent(PULL)).toEqual([{ project_id: "alpha" }]));
    expect(await screen.findByText("Started: git pull --ff-only")).toBeTruthy();
  });

  test("push always opens a confirmation dialog and cancelling sends nothing", async () => {
    const api = mockGit();
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Push…" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Push" })).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", PUSH)).toEqual([]);
  });

  test("push sends the project to the push action once confirmed", async () => {
    const api = mockGit().action(PUSH, { job_id: "job-2", kind: "git", title: "git push" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Push…" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Push" }));
    await waitFor(() => expect(api.sent(PUSH)).toHaveLength(1));
    expect(api.sent(PUSH)[0]).toMatchObject({ project_id: "alpha" });
  });

  test("push sends a string remote, defaulting to origin, so the backend accepts the request", async () => {
    const api = mockGit().action(PUSH, { job_id: "job-2", kind: "git", title: "git push" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Push…" }));
    const dialog = await screen.findByRole("dialog");
    expect((formField("remote", dialog).getByRole("textbox") as HTMLInputElement).value).toBe(
      "origin",
    );
    await user.click(within(dialog).getByRole("button", { name: "Push" }));
    await waitFor(() => expect(api.sent(PUSH)).toHaveLength(1));
    expect(api.sent(PUSH)[0]).toEqual({ project_id: "alpha", remote: "origin", branch: null });
  });

  test("push can target another remote typed into the dialog", async () => {
    const api = mockGit().action(PUSH, { job_id: "job-2", kind: "git", title: "git push" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Push…" }));
    const dialog = await screen.findByRole("dialog");
    const remote = formField("remote", dialog).getByRole("textbox");
    await user.clear(remote);
    await user.type(remote, "backup");
    await user.click(within(dialog).getByRole("button", { name: "Push" }));
    await waitFor(() => expect(api.sent(PUSH)).toHaveLength(1));
    expect(api.sent(PUSH)[0]).toEqual({ project_id: "alpha", remote: "backup", branch: null });
  });

  test("new branch collects a name and sends the switch choice with the project", async () => {
    const api = mockGit().action(CREATE_BRANCH, { ok: true, message: "created topic" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "New branch" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("feature/name"), "topic");
    await user.click(formField("switch", dialog).getByRole("switch"));
    await user.click(within(dialog).getByRole("button", { name: "Create branch" }));
    await waitFor(() => expect(api.sent(CREATE_BRANCH)).toHaveLength(1));
    expect(api.sent(CREATE_BRANCH)[0]).toEqual({
      project_id: "alpha",
      name: "topic",
      start_point: null,
      switch: true,
    });
    expect(await screen.findByText("created topic")).toBeTruthy();
  });
});

describe("GitTab working tree and diff", () => {
  test("shows loading rows until the changes arrive", async () => {
    mockGit();
    renderGit();
    expect(sectionOf("Working tree").getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(await sectionOf("Working tree").findByText("Clean. Nothing to commit.")).toBeTruthy();
    expect(sectionOf("Working tree").queryByRole("status", { name: "Loading" })).toBeNull();
  });

  test("says the tree is clean when nothing changed", async () => {
    mockGit();
    renderGit();
    expect(await screen.findByText("Clean. Nothing to commit.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show all unstaged changes" })).toBeNull();
  });

  test("lists each changed file with its kind, tone, and staged marker", async () => {
    mockGit({
      files: [
        makeFileChange("src/added.py", { kind: "added" }),
        makeFileChange("src/new.py", { kind: "untracked" }),
        makeFileChange("src/gone.py", { kind: "deleted" }),
        makeFileChange("src/clash.py", { kind: "conflicted" }),
        makeFileChange("src/edit.py", { kind: "modified", staged: true }),
        makeFileChange("src/moved.py", { kind: "renamed" }),
        makeFileChange("src/copy.py", { kind: "copied" }),
        makeFileChange("src/mode.py", { kind: "type-changed" }),
      ],
    });
    renderGit();
    const tree = sectionOf("Working tree");
    const tones: Record<string, string> = {
      "src/added.py": "good",
      "src/new.py": "good",
      "src/gone.py": "bad",
      "src/clash.py": "bad",
      "src/edit.py": "info",
      "src/moved.py": "info",
      "src/copy.py": "info",
      "src/mode.py": "warn",
    };
    for (const [path, tone] of Object.entries(tones)) {
      const row = (await tree.findByText(path)).closest("button");
      expect(row?.querySelector("[data-tone]")?.getAttribute("data-tone")).toBe(tone);
    }
    const staged = (await tree.findByText("src/edit.py")).closest("button");
    expect(within(staged as HTMLElement).getByText("staged")).toBeTruthy();
    expect(tree.getAllByText("staged")).toHaveLength(1);
  });

  test("shows the diff of one clicked file and asks the server for exactly that file", async () => {
    const api = mockGit({
      files: [makeFileChange("src/edit.py"), makeFileChange("src/edit.py", { staged: true })],
    }).action(DIFF, { diff: makeUnifiedDiff("src/edit.py") });
    const { user } = renderGit();
    expect(await screen.findByText("Pick a file or a commit to see its diff.")).toBeTruthy();
    const rows = await sectionOf("Working tree").findAllByText("src/edit.py");
    await user.click(rows[0] as HTMLElement);
    const diff = sectionOf("Diff");
    expect(await diff.findByText("new line")).toBeTruthy();
    expect(diff.getByText("src/edit.py", { selector: "p" })).toBeTruthy();
    expect(api.sent(DIFF)).toEqual([
      { project_id: "alpha", path: "src/edit.py", staged: false, commit: null },
    ]);
    await user.click(rows[1] as HTMLElement);
    expect(await diff.findByText("src/edit.py (staged)")).toBeTruthy();
    await waitFor(() => expect(api.sent(DIFF)).toHaveLength(2));
    expect(api.sent(DIFF)[1]).toEqual({
      project_id: "alpha",
      path: "src/edit.py",
      staged: true,
      commit: null,
    });
  });

  test("shows every unstaged change when asked", async () => {
    const api = mockGit({ files: [makeFileChange("a.py")] }).action(DIFF, { diff: "" });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: "Show all unstaged changes" }));
    expect(await screen.findByText("All unstaged changes")).toBeTruthy();
    expect(await sectionOf("Diff").findByText("No changes.")).toBeTruthy();
    expect(api.sent(DIFF)).toEqual([
      { project_id: "alpha", path: null, staged: false, commit: null },
    ]);
  });

  test("reports a diff that cannot be read and retries it on request", async () => {
    let attempts = 0;
    const api = mockGit({ files: [makeFileChange("a.py")] }).action(DIFF, () => {
      attempts += 1;
      if (attempts === 1) throw new MockApiError(500, "git diff failed");
      return { diff: makeUnifiedDiff("a.py") };
    });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: /^a\.py/ }));
    const diff = sectionOf("Diff");
    expect(await diff.findByText("git diff failed")).toBeTruthy();
    await user.click(diff.getByRole("button", { name: "Try again" }));
    expect(await diff.findByText("new line")).toBeTruthy();
    expect(api.sent(DIFF)).toHaveLength(2);
  });

  test("reports working-tree changes that cannot be read", async () => {
    mockGit().action(CHANGES, () => {
      throw new MockApiError(500, "cannot read status");
    });
    renderGit();
    expect(await screen.findByText("cannot read status")).toBeTruthy();
  });

  test("renders a binary or mode-only file change without hunks", async () => {
    mockGit({ files: [makeFileChange("logo.png")] }).action(DIFF, {
      diff: [
        "diff --git a/logo.png b/logo.png",
        "index 1..2 100644",
        "Binary files differ",
        "",
      ].join("\n"),
    });
    const { user } = renderGit();
    await user.click(await screen.findByRole("button", { name: /^logo\.png/ }));
    expect(await sectionOf("Diff").findByText("Binary or mode-only change.")).toBeTruthy();
  });
});

describe("GitTab history", () => {
  test("lists recent commits with author, age and diffstat, and shows a commit's diff on click", async () => {
    const api = mockGit({
      commits: [
        makeCommit("abc1234", {
          sha: "abc1234".padEnd(40, "f"),
          subject: "Fix the parser",
          author: "Ada",
          files_changed: 3,
          insertions: 12,
          deletions: 4,
        }),
        makeCommit("def5678", {
          subject: "Second",
          author: "Grace",
          committed_at: NOW_SECONDS - 3 * 86400,
        }),
      ],
    }).action(DIFF, { diff: makeUnifiedDiff("parser.py") });
    const { user } = renderGit();
    const commits = sectionOf("Recent commits");
    const subject = await commits.findByText("Fix the parser");
    expect(commits.getByText("abc1234")).toBeTruthy();
    expect(commits.getByText(/Ada ·/)).toBeTruthy();
    expect(commits.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(commits.getByText(timeText("3 d ago"))).toBeTruthy();
    expect(commits.getByText(/3 files/)).toBeTruthy();
    expect(commits.getByText("+12")).toBeTruthy();
    expect(commits.getByText("−4")).toBeTruthy();
    await user.click(subject);
    expect(await sectionOf("Diff").findByText("abc1234 Fix the parser")).toBeTruthy();
    await waitFor(() => expect(api.sent(DIFF)).toHaveLength(1));
    expect(api.sent(DIFF)[0]).toEqual({
      project_id: "alpha",
      path: null,
      staged: false,
      commit: "abc1234".padEnd(40, "f"),
    });
    expect(api.sent(COMMITS)).toEqual([{ project_id: "alpha", revision: null, limit: 30 }]);
  });

  test("shows each branch's current marker and its upstream relationship", async () => {
    mockGit({
      branches: [
        makeBranch("main", { current: true, upstream: "origin/main", ahead: 1, behind: 2 }),
        makeBranch("stale", { upstream: "origin/stale", gone: true }),
        makeBranch("topic"),
        makeBranch("tracked", { upstream: "origin/tracked" }),
      ],
    });
    renderGit();
    const branches = sectionOf("Branches");
    expect(await branches.findByText("● main")).toBeTruthy();
    expect(branches.getByText("↑1 ↓2")).toBeTruthy();
    expect(branches.getByText("upstream gone")).toBeTruthy();
    expect(branches.getByText("local")).toBeTruthy();
    expect(branches.getByText("↑0 ↓0")).toBeTruthy();
    expect(branches.getByText("Tip of topic", { exact: false })).toBeTruthy();
    expect(branches.getByText("topic")).toBeTruthy();
  });

  test("reports commits and branches that cannot be read", async () => {
    mockApi()
      .action(CHANGES, { branch: "main", files: [] })
      .action(BRANCHES, () => {
        throw new MockApiError(500, "branches unavailable");
      })
      .action(COMMITS, () => {
        throw new MockApiError(500, "log unavailable");
      })
      .action(WORKTREES, { worktrees: [] })
      .action(STASHES, { stashes: [] })
      .action(TAGS, { tags: [], commits_since_latest: null });
    renderGit();
    expect(await sectionOf("Branches").findByText("branches unavailable")).toBeTruthy();
    expect(await sectionOf("Recent commits").findByText("log unavailable")).toBeTruthy();
  });
});

describe("GitTab worktrees, stashes and tags", () => {
  test("describes each worktree by its branch, detached head and flags", async () => {
    mockGit({
      worktrees: [
        makeWorktree("C:/code/alpha", { branch: "main" }),
        makeWorktree("C:/data/runs/one", {
          branch: "forge/one",
          dashboard_owned: true,
          locked: true,
          prunable: true,
        }),
        makeWorktree("C:/code/alpha-detached", { detached: true }),
        makeWorktree("C:/code/alpha-bare"),
      ],
    });
    renderGit();
    const worktrees = sectionOf("Worktrees");
    expect(await worktrees.findByText("C:/code/alpha")).toBeTruthy();
    expect(worktrees.getByText("main")).toBeTruthy();
    expect(worktrees.getByText("forge/one · dashboard run · locked · prunable")).toBeTruthy();
    expect(worktrees.getByText("detached")).toBeTruthy();
    expect(
      worktrees.getByText("C:/code/alpha-bare").parentElement?.nextElementSibling?.textContent,
    ).toBe("");
  });

  test("lists stashes with their message and age, or says there are none", async () => {
    mockGit({ stashes: [makeStash("stash@{0}", { message: "half-done refactor" })] });
    renderGit();
    const stashes = sectionOf("Stashes");
    expect(await stashes.findByText("stash@{0}")).toBeTruthy();
    expect(stashes.getByText(/half-done refactor/)).toBeTruthy();
    expect(stashes.getByText(timeText("1 d ago"))).toBeTruthy();
  });

  test("says there are no stashes for a repo without any", async () => {
    mockGit();
    renderGit();
    expect(await sectionOf("Stashes").findByText("No stashes.")).toBeTruthy();
  });

  test("shows tags in the order given with the count since the latest tag, capped at twelve", async () => {
    mockGit({
      tags: Array.from({ length: 14 }, (_, index) => makeTag(`v1.${index}.0`)),
      commitsSinceLatest: 7,
    });
    renderGit();
    const tags = sectionOf("Tags and releases");
    expect(await tags.findByText("7 commits since the latest tag")).toBeTruthy();
    expect(tags.getByText("v1.0.0")).toBeTruthy();
    expect(tags.getByText("v1.11.0")).toBeTruthy();
    expect(tags.queryByText("v1.12.0")).toBeNull();
    expect(tags.getAllByText(/abc1234 ·/)).toHaveLength(12);
  });

  test("omits the commit count when the latest tag distance is unknown", async () => {
    mockGit({ tags: [makeTag("v1.0.0")], commitsSinceLatest: null });
    renderGit();
    expect(await sectionOf("Tags and releases").findByText("v1.0.0")).toBeTruthy();
    expect(screen.queryByText(/commits since the latest tag/)).toBeNull();
  });

  test("invites tagging a release when the repo has no tags", async () => {
    mockGit();
    renderGit();
    expect(await screen.findByText("No tags")).toBeTruthy();
    expect(screen.getByText("Tag a release to track the commits since it.")).toBeTruthy();
  });

  test("reports worktrees, stashes and tags that cannot be read", async () => {
    mockApi()
      .action(CHANGES, { branch: "main", files: [] })
      .action(BRANCHES, { branches: [] })
      .action(COMMITS, { commits: [] })
      .action(WORKTREES, () => {
        throw new MockApiError(500, "worktrees unavailable");
      })
      .action(STASHES, () => {
        throw new MockApiError(500, "stashes unavailable");
      })
      .action(TAGS, () => {
        throw new MockApiError(500, "tags unavailable");
      });
    renderGit();
    expect(await sectionOf("Worktrees").findByText("worktrees unavailable")).toBeTruthy();
    expect(await sectionOf("Stashes").findByText("stashes unavailable")).toBeTruthy();
    expect(await sectionOf("Tags and releases").findByText("tags unavailable")).toBeTruthy();
  });
});

describe("GitTab releases", () => {
  test("does not ask GitHub for releases when the project is not on a reachable GitHub", async () => {
    const api = mockGit();
    renderGit(makeProject("alpha"));
    await screen.findByText("No tags");
    await settle();
    expect(api.callsTo("POST", RELEASES)).toEqual([]);
  });

  test("lists releases with their pre-release and draft marks, falling back to the tag", async () => {
    const api = mockGit({ tags: [makeTag("v1.0.0")] }).action(RELEASES, {
      repo: "acme/alpha",
      tags: [],
      releases: [
        makeRelease(1, { name: "First light" }),
        makeRelease(2, { name: "", tag: "v2.0.0-rc1", prerelease: true }),
        makeRelease(3, { name: "Next", draft: true, published_at: null }),
        ...Array.from({ length: 7 }, (_, index) => makeRelease(10 + index)),
      ],
    });
    renderGit(makeGithubProject("alpha"));
    const tags = sectionOf("Tags and releases");
    const first = await tags.findByRole("link", { name: "First light" });
    expect(first.getAttribute("href")).toBe("https://github.com/acme/alpha/releases/tag/v1.0.0");
    expect(first.getAttribute("target")).toBe("_blank");
    expect(tags.getByRole("link", { name: /v2\.0\.0-rc1/ }).textContent).toContain("pre-release");
    expect(tags.getByRole("link", { name: /Next/ }).textContent).toContain("draft");
    expect(tags.getByText("never")).toBeTruthy();
    expect(tags.queryByText("v16.0.0")).toBeNull();
    expect(tags.getAllByRole("link")).toHaveLength(8);
    expect(api.sent(RELEASES)).toEqual([{ project_id: "alpha" }]);
  });

  test("says there are no GitHub releases when the list is empty", async () => {
    mockGit().action(RELEASES, { repo: "acme/alpha", tags: [], releases: [] });
    renderGit(makeGithubProject("alpha"));
    expect(await screen.findByText("No GitHub releases.")).toBeTruthy();
  });

  test("reports releases that cannot be read", async () => {
    mockGit().action(RELEASES, () => {
      throw new MockApiError(502, "GitHub is down");
    });
    renderGit(makeGithubProject("alpha"));
    expect(await screen.findByText("GitHub is down")).toBeTruthy();
  });
});

describe("GitTab live refresh", () => {
  test("re-reads the git views when the git service publishes a new revision", async () => {
    const api = mockGit();
    renderGit();
    await screen.findByText("Clean. Nothing to commit.");
    expect(api.callsTo("POST", CHANGES)).toHaveLength(1);
    seedStream({ git: { repos: [makeRepoState("alpha", { dirty: 1 })] } }, 2);
    await waitFor(() => expect(api.callsTo("POST", CHANGES)).toHaveLength(2));
    await waitFor(() => expect(api.callsTo("POST", COMMITS)).toHaveLength(2));
    expect(screen.getByText("1 changed files")).toBeTruthy();
    expect(api.unrouted).toEqual([]);
  });
});
