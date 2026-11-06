import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";
import { makeProject } from "./factories";
import { formField, installHarness, MockApiError, mockApi } from "./harness";
import {
  type ForkRun,
  type ForkStatus,
  makeForkRun,
  makeForkStatus,
  openProjectTab,
} from "./project-core-support";

installHarness();

const PREVIEW = "/api/services/forks/actions/preview" as const;
const HISTORY = "/api/services/forks/actions/history" as const;
const RUN_DIFF = "/api/services/forks/actions/run-diff" as const;
const SYNC = "/api/services/forks/actions/sync" as const;
const REFRESH = "/api/services/forks/actions/refresh-status" as const;
const SET_POLICY = "/api/services/forks/actions/set-policy" as const;
const APPLY = "/api/services/forks/actions/apply" as const;
const RETRY_AGENT = "/api/services/forks/actions/retry-agent" as const;
const ABORT = "/api/services/forks/actions/abort" as const;

type Commit = Schemas["UpstreamCommit"];
type ChangedFile = Schemas["ChangedFile"];

const FORK_PROJECT = makeProject("alpha", {
  fork: {
    is_fork: true,
    upstream: "upstream/repo",
    behind_upstream: 0,
    ahead_of_upstream: 0,
    last_sync: "never",
    last_sync_at: null,
    next_sync_at: null,
  },
});

const UNIFIED_DIFF = [
  "diff --git a/lib.py b/lib.py",
  "index 1111111..2222222 100644",
  "--- a/lib.py",
  "+++ b/lib.py",
  "@@ -1 +1 @@",
  "-old = 1",
  "+new = 2",
  "",
].join("\n");

interface ForkScenario {
  status?: ForkStatus;
  /** Leave the fork service without any status for this project. */
  untracked?: boolean;
  commits?: Commit[];
  files?: ChangedFile[];
  runs?: ForkRun[];
}

async function openFork(scenario: ForkScenario = {}) {
  const status = scenario.status ?? makeForkStatus("alpha");
  const api = mockApi()
    .action(PREVIEW, {
      status,
      commits: scenario.commits ?? [],
      files: scenario.files ?? [],
    })
    .action(HISTORY, { runs: scenario.runs ?? [] });
  const opened = await openProjectTab(
    "fork",
    {
      project: FORK_PROJECT,
      detail: { is_git: true },
      streams: { forks: { forks: scenario.untracked ? [] : [status] } },
    },
    api,
  );
  return { ...opened, status };
}

function commit(shortSha: string, subject: string): Commit {
  return {
    short_sha: shortSha,
    subject,
    author: "Ada Lovelace",
    committed_at: NOW_SECONDS - 7200,
  };
}

function runItem(text: string): HTMLElement {
  const item = screen.getByText(text).closest("li");
  if (!item) throw new Error(`no run row containing ${text}`);
  return item;
}

describe("ForkTab status panel", () => {
  test("says the fork service has not read upstream yet when it has no status", async () => {
    await openFork({ untracked: true });
    expect(await screen.findByText("No fork status yet")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull();
  });

  test("shows where upstream is, how far behind and ahead, and when it was fetched", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        behind: 3,
        ahead: 1,
        next_sync_at: NOW_SECONDS + 3600,
        source: "github",
      }),
    });
    const upstream = await screen.findByText(/^upstream/);
    expect(upstream.textContent).toBe(
      "upstream https://github.com/upstream/repo.git at main · found via github",
    );
    const counts = screen.getByText("3 behind").parentElement;
    expect(counts?.textContent).toBe("3 behind1 aheadfetched 10 min agonext sync in 1 h");
  });

  test("falls back to question marks and manual sync when upstream facts are unknown", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        upstream_url: null,
        upstream_branch: null,
        behind: null,
        ahead: null,
        fetched_at: null,
        next_sync_at: null,
        source: "none",
      }),
    });
    const upstream = await screen.findByText(/^upstream/);
    expect(upstream.textContent).toBe("upstream not set · found via none");
    const counts = screen.getByText("? behind").parentElement;
    expect(counts?.textContent).toBe("? behind? aheadfetched nevernext sync manual");
  });

  test("reports a clean conflict prediction with when it was checked", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        prediction: { clean: true, conflicted_files: [], checked_at: NOW_SECONDS - 300 },
      }),
    });
    const verdict = await screen.findByText(/a sync merges cleanly/);
    expect(verdict.textContent).toBe("a sync merges cleanly (checked 5 min ago)");
  });

  test("warns about one predicted conflict in the singular", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        prediction: { clean: false, conflicted_files: ["a.py"], checked_at: NOW_SECONDS },
      }),
    });
    expect(await screen.findByText("expect conflicts in 1 file: a.py")).toBeTruthy();
  });

  test("lists only the first four predicted conflicts", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        prediction: {
          clean: false,
          conflicted_files: ["a.py", "b.py", "c.py", "d.py", "e.py"],
          checked_at: NOW_SECONDS,
        },
      }),
    });
    expect(
      await screen.findByText("expect conflicts in 5 files: a.py, b.py, c.py, d.py"),
    ).toBeTruthy();
  });

  test("shows no prediction before one exists", async () => {
    await openFork();
    await screen.findByText(/^upstream/);
    expect(screen.queryByText(/a sync merges cleanly/)).toBeNull();
    expect(screen.queryByText(/expect conflicts/)).toBeNull();
  });

  test("shows the error the fork service recorded", async () => {
    await openFork({ status: makeForkStatus("alpha", { error: "upstream remote unreachable" }) });
    expect(await screen.findByText("upstream remote unreachable")).toBeTruthy();
  });

  test("explains the agent conflict policy with its attempt limit", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        policy: { strategy: "merge", auto_push: false, conflicts: "agent", max_agent_attempts: 3 },
      }),
    });
    const note = await screen.findByText(/Syncs run in an isolated worktree/);
    expect(note.textContent).toContain("Conflicts go to the agent (up to 3 attempts)");
    expect(note.textContent).toContain("then the checks run before anything lands.");
  });

  test("explains that conflicts stop the run when the agent is off, and that results are pushed", async () => {
    await openFork({
      status: makeForkStatus("alpha", {
        policy: { strategy: "rebase", auto_push: true, conflicts: "stop", max_agent_attempts: 2 },
      }),
    });
    const note = await screen.findByText(/Syncs run in an isolated worktree/);
    expect(note.textContent).toContain("(off: the run stops and notifies)");
    expect(note.textContent).toContain("before anything lands and is pushed.");
  });
});

describe("ForkTab actions", () => {
  test("syncs now for this project and starts the job", async () => {
    const { api, rendered } = await openFork();
    api.action(SYNC, { job_id: "job-5", kind: "fork-sync", title: "Sync alpha" });
    const [syncNow] = await screen.findAllByRole("button", { name: "Sync now" });
    await rendered.user.click(syncNow as HTMLElement);
    expect(await screen.findByText("Started: Sync alpha")).toBeTruthy();
    expect(api.sent(SYNC)).toEqual([{ project_id: "alpha" }]);
  });

  test("fetches upstream for this project and refreshes the status", async () => {
    const { api, rendered, status } = await openFork();
    api.action(REFRESH, status);
    await rendered.user.click(await screen.findByRole("button", { name: "Fetch upstream" }));
    expect(await screen.findByText("Refresh fork status: done")).toBeTruthy();
    expect(api.sent(REFRESH)).toEqual([{ project_id: "alpha" }]);
  });

  test("edits the policy starting from the current one", async () => {
    const { api, rendered, status } = await openFork();
    api.action(SET_POLICY, status);
    await rendered.user.click(await screen.findByRole("button", { name: "Policy…" }));
    const dialog = await screen.findByRole("dialog");
    const autoPush = formField("auto_push", dialog).getByRole("switch");
    expect(autoPush.getAttribute("aria-checked")).toBe("false");
    await rendered.user.click(autoPush);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Fork policy" }));
    await waitFor(() => expect(api.sent(SET_POLICY)).toHaveLength(1));
    expect(api.sent(SET_POLICY)).toEqual([
      { project_id: "alpha", policy: { ...status.policy, auto_push: true } },
    ]);
  });

  test("reloads the preview and the runs after an action completes", async () => {
    const { api, rendered } = await openFork();
    api.action(SYNC, { job_id: "job-5", kind: "fork-sync", title: "Sync alpha" });
    expect(api.callsTo("POST", HISTORY)).toHaveLength(1);
    const [syncNow] = await screen.findAllByRole("button", { name: "Sync now" });
    await rendered.user.click(syncNow as HTMLElement);
    await waitFor(() => expect(api.callsTo("POST", HISTORY)).toHaveLength(2));
    expect(api.callsTo("POST", PREVIEW)).toHaveLength(2);
  });
});

describe("ForkTab preview", () => {
  test("asks what a sync would bring without fetching first", async () => {
    const { api } = await openFork();
    await screen.findByText("Up to date with upstream.");
    expect(api.sent(PREVIEW)).toEqual([{ project_id: "alpha" }]);
  });

  test("says the fork is up to date when upstream has nothing new", async () => {
    await openFork({ commits: [] });
    expect(await screen.findByText("Up to date with upstream.")).toBeTruthy();
  });

  test("lists the commits and the files a sync would bring", async () => {
    await openFork({
      commits: [commit("a1b2c3d", "Fix the parser"), commit("e4f5a6b", "Bump the version")],
      files: [
        { status: "M", path: "src/parser.py" },
        { status: "A", path: "src/new.py" },
      ],
    });
    expect(await screen.findByText("Fix the parser")).toBeTruthy();
    expect(screen.getByText("a1b2c3d")).toBeTruthy();
    expect(screen.getByText("Bump the version")).toBeTruthy();
    expect(screen.getAllByText(/^Ada Lovelace/).map((byline) => byline.textContent)).toEqual([
      "Ada Lovelace · 2 h ago",
      "Ada Lovelace · 2 h ago",
    ]);
    const summary = screen.getByText("2 files change");
    const files = within(summary.closest("details") as HTMLElement).getAllByRole("listitem");
    expect(files.map((file) => file.textContent)).toEqual(["Msrc/parser.py", "Asrc/new.py"]);
  });

  test("fetching upstream makes the preview show what that found", async () => {
    const { api, rendered, status } = await openFork();
    await screen.findByText("Up to date with upstream.");
    let fetched = false;
    api
      .action(REFRESH, () => {
        fetched = true;
        return status;
      })
      .action(PREVIEW, () => ({
        status,
        commits: fetched ? [commit("c0ffee1", "Fresh from upstream")] : [],
        files: [],
      }));
    await rendered.user.click(screen.getByRole("button", { name: "Fetch upstream" }));
    expect(await screen.findByText("Fresh from upstream")).toBeTruthy();
    expect(api.sent(REFRESH)).toEqual([{ project_id: "alpha" }]);
  });

  test("warns that the preview may be out of date until upstream has been fetched", async () => {
    await openFork({ status: makeForkStatus("alpha", { fetched_at: null }) });
    expect(
      await screen.findByText("Upstream has not been fetched yet; this may be out of date."),
    ).toBeTruthy();
  });

  test("says nothing about staleness once upstream has been fetched", async () => {
    await openFork();
    await screen.findByText("Up to date with upstream.");
    expect(screen.queryByText(/has not been fetched yet/)).toBeNull();
  });

  test("counts a single changed file in the singular", async () => {
    await openFork({
      commits: [commit("a1b2c3d", "Fix the parser")],
      files: [{ status: "M", path: "src/parser.py" }],
    });
    expect(await screen.findByText("1 file change")).toBeTruthy();
  });

  test("shows why the preview could not be read and retries on request", async () => {
    const api = mockApi();
    let failing = true;
    api
      .action(PREVIEW, () => {
        if (failing) throw new MockApiError(502, "git fetch timed out");
        return { status: makeForkStatus("alpha"), commits: [], files: [] };
      })
      .action(HISTORY, { runs: [] });
    const status = makeForkStatus("alpha");
    const { rendered } = await openProjectTab(
      "fork",
      { project: FORK_PROJECT, detail: { is_git: true }, streams: { forks: { forks: [status] } } },
      api,
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("git fetch timed out");
    failing = false;
    await rendered.user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Up to date with upstream.")).toBeTruthy();
  });
});

describe("ForkTab runs", () => {
  test("invites the first sync when there are no runs", async () => {
    const { api, rendered } = await openFork({ runs: [] });
    expect(await screen.findByText("No syncs yet")).toBeTruthy();
    api.action(SYNC, { job_id: "job-6", kind: "fork-sync", title: "First sync" });
    const emptyState = screen.getByText("No syncs yet").closest("div") as HTMLElement;
    await rendered.user.click(within(emptyState).getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText("Started: First sync")).toBeTruthy();
    expect(api.sent(SYNC)).toEqual([{ project_id: "alpha" }]);
  });

  test("asks for the project's thirty most recent runs", async () => {
    const { api } = await openFork();
    await screen.findByText("No syncs yet");
    expect(api.sent(HISTORY)).toEqual([{ project_id: "alpha", limit: 30 }]);
  });

  test("shows why the runs could not be read", async () => {
    const api = mockApi()
      .action(PREVIEW, { status: makeForkStatus("alpha"), commits: [], files: [] })
      .action(HISTORY, () => {
        throw new MockApiError(500, "run log corrupt");
      });
    await openProjectTab(
      "fork",
      {
        project: FORK_PROJECT,
        detail: { is_git: true },
        streams: { forks: { forks: [makeForkStatus("alpha")] } },
      },
      api,
    );
    expect((await screen.findByRole("alert")).textContent).toContain("run log corrupt");
  });

  test("describes a run with its strategy, timing, agent work, cost and push", async () => {
    await openFork({
      runs: [
        makeForkRun("run-1", {
          strategy: "rebase",
          upstream_ref: "upstream/dev",
          target_branch: "develop",
          duration_s: 42,
          agent_attempts: 2,
          agent_cost_usd: 1.5,
          pushed: true,
          detail: "Landed on develop",
        }),
      ],
    });
    const header = await screen.findByText("rebase upstream/dev into develop");
    const item = header.closest("li") as HTMLElement;
    expect(item.textContent).toContain("30 min ago · 42 s · agent ×2 · $1.50 · pushed");
    expect(within(item).getByText("Landed on develop")).toBeTruthy();
  });

  test("leaves out timing, agent and push facts a run does not have", async () => {
    await openFork({ runs: [makeForkRun("run-1", { agent_cost_usd: 0 })] });
    const item = (await screen.findByText("merge upstream/main into main")).closest(
      "li",
    ) as HTMLElement;
    expect(item.textContent).toContain("30 min ago");
    expect(item.textContent).not.toContain("·");
    expect(item.textContent).not.toContain("conflicts in");
  });

  test("names the files a run conflicted in", async () => {
    await openFork({
      runs: [makeForkRun("run-1", { state: "conflict", conflicted_files: ["a.py", "b.py"] })],
    });
    expect(await screen.findByText("a.py, b.py")).toBeTruthy();
    expect(screen.getByText(/^conflicts in/).textContent).toBe("conflicts in a.py, b.py");
  });

  test.each([
    ["running", "info"],
    ["synced", "good"],
    ["up-to-date", "good"],
    ["conflict", "bad"],
    ["needs-attention", "warn"],
    ["ready-to-apply", "info"],
    ["failed", "bad"],
    ["aborted", "neutral"],
  ] as const)("marks a %s run with the %s tone", async (state, tone) => {
    await openFork({ runs: [makeForkRun("run-1", { state })] });
    const item = (await screen.findByText("merge upstream/main into main")).closest(
      "li",
    ) as HTMLElement;
    expect(within(item).getByText(state).closest("[data-tone]")?.getAttribute("data-tone")).toBe(
      tone,
    );
  });

  test("offers no parked-run decisions for runs that are finished", async () => {
    await openFork({
      runs: [
        makeForkRun("run-1", { state: "synced" }),
        makeForkRun("run-2", { state: "failed" }),
        makeForkRun("run-3", { state: "running" }),
      ],
    });
    await screen.findAllByText("merge upstream/main into main");
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Re-run agent…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Abort" })).toBeNull();
  });

  test.each(["conflict", "needs-attention"] as const)(
    "a %s run can be re-run with the agent or aborted but not applied",
    async (state) => {
      await openFork({ runs: [makeForkRun("run-1", { state })] });
      await screen.findByRole("button", { name: "Re-run agent…" });
      expect(screen.getByRole("button", { name: "Abort" })).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    },
  );

  test("applies a run that is ready, sending only its id", async () => {
    const { api, rendered } = await openFork({
      runs: [makeForkRun("run-9", { state: "ready-to-apply" })],
    });
    api.action(APPLY, { job_id: "job-7", kind: "fork-apply", title: "Apply run-9" });
    await rendered.user.click(await screen.findByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Started: Apply run-9")).toBeTruthy();
    expect(api.sent(APPLY)).toEqual([{ run_id: "run-9" }]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("re-runs the agent with the guidance typed in", async () => {
    const { api, rendered } = await openFork({
      runs: [makeForkRun("run-4", { state: "conflict" })],
    });
    api.action(RETRY_AGENT, { job_id: "job-8", kind: "fork-agent", title: "Retry run-4" });
    await rendered.user.click(await screen.findByRole("button", { name: "Re-run agent…" }));
    const dialog = await screen.findByRole("dialog");
    await rendered.user.type(
      formField("instructions", dialog).getByRole("textbox"),
      "Prefer upstream's version",
    );
    await rendered.user.click(within(dialog).getByRole("button", { name: "Retry agent" }));
    expect(await screen.findByText("Started: Retry run-4")).toBeTruthy();
    expect(api.sent(RETRY_AGENT)).toEqual([
      { run_id: "run-4", instructions: "Prefer upstream's version" },
    ]);
  });

  test("aborting needs the run id typed first", async () => {
    const { api, rendered } = await openFork({
      runs: [makeForkRun("run-5", { state: "needs-attention" })],
    });
    api.action(ABORT, { ok: true, message: "Run aborted" });
    await rendered.user.click(await screen.findByRole("button", { name: "Abort" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Abort" }) as HTMLButtonElement;
    expect(within(dialog).getByText("run-5")).toBeTruthy();
    expect(confirm.disabled).toBe(true);
    await rendered.user.type(within(dialog).getByRole("textbox"), "run-5");
    expect(confirm.disabled).toBe(false);
    expect(api.callsTo("POST", ABORT)).toEqual([]);
    await rendered.user.click(confirm);
    expect(await screen.findByText("Run aborted")).toBeTruthy();
    expect(api.sent(ABORT)).toEqual([{ run_id: "run-5" }]);
  });

  test("each run's decisions act on that run", async () => {
    const { api, rendered } = await openFork({
      runs: [
        makeForkRun("run-1", { state: "ready-to-apply", detail: "first" }),
        makeForkRun("run-2", { state: "ready-to-apply", detail: "second" }),
      ],
    });
    api.action(APPLY, { job_id: "job-1", kind: "fork-apply", title: "Apply" });
    await screen.findByText("second");
    await rendered.user.click(within(runItem("second")).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(api.sent(APPLY)).toEqual([{ run_id: "run-2" }]));
  });
});

describe("ForkTab run diff", () => {
  const branchRun = makeForkRun("run-1", {
    state: "ready-to-apply",
    branch: "forge-sync/20260115",
  });

  test("shows no diff until a run with a branch is selected", async () => {
    const { api } = await openFork({ runs: [branchRun] });
    await screen.findByText("merge upstream/main into main");
    expect(screen.queryByText(/^Diff of/)).toBeNull();
    expect(api.callsTo("POST", RUN_DIFF)).toEqual([]);
  });

  test("shows the diff against the user's branch for the selected run", async () => {
    const { api, rendered } = await openFork({ runs: [branchRun] });
    api.action(RUN_DIFF, { run: branchRun, against: "target", diff: UNIFIED_DIFF });
    await rendered.user.click(await screen.findByText("merge upstream/main into main"));
    expect(await screen.findByText("Diff of forge-sync/20260115")).toBeTruthy();
    expect(await screen.findByText("lib.py")).toBeTruthy();
    expect(api.sent(RUN_DIFF)).toEqual([{ run_id: "run-1", against: "target" }]);
    expect(
      screen.getByRole("radio", { name: "Against your branch" }).getAttribute("aria-checked"),
    ).toBe("true");
  });

  test("switches the comparison to upstream and back", async () => {
    const { api, rendered } = await openFork({ runs: [branchRun] });
    api.action(RUN_DIFF, (input) => ({
      run: branchRun,
      against: input.against ?? "target",
      diff: input.against === "upstream" ? UNIFIED_DIFF : "",
    }));
    await rendered.user.click(await screen.findByText("merge upstream/main into main"));
    expect(await screen.findByText("The run's branch matches.")).toBeTruthy();
    await rendered.user.click(screen.getByRole("radio", { name: "Against upstream" }));
    expect(await screen.findByText("lib.py")).toBeTruthy();
    expect(api.sent(RUN_DIFF).at(-1)).toEqual({ run_id: "run-1", against: "upstream" });
    await rendered.user.click(screen.getByRole("radio", { name: "Against your branch" }));
    expect(await screen.findByText("The run's branch matches.")).toBeTruthy();
    expect(api.sent(RUN_DIFF).at(-1)).toEqual({ run_id: "run-1", against: "target" });
  });

  test("keeps the comparison when the selected toggle is clicked again", async () => {
    const { api, rendered } = await openFork({ runs: [branchRun] });
    api.action(RUN_DIFF, { run: branchRun, against: "upstream", diff: UNIFIED_DIFF });
    await rendered.user.click(await screen.findByText("merge upstream/main into main"));
    await rendered.user.click(await screen.findByRole("radio", { name: "Against upstream" }));
    await rendered.user.click(screen.getByRole("radio", { name: "Against upstream" }));
    await screen.findByText("lib.py");
    expect(
      screen.getByRole("radio", { name: "Against upstream" }).getAttribute("aria-checked"),
    ).toBe("true");
    expect(api.sent(RUN_DIFF).at(-1)).toEqual({ run_id: "run-1", against: "upstream" });
  });

  test("shows why the diff could not be read", async () => {
    const { api, rendered } = await openFork({ runs: [branchRun] });
    api.action(RUN_DIFF, () => {
      throw new MockApiError(404, "branch was deleted");
    });
    await rendered.user.click(await screen.findByText("merge upstream/main into main"));
    expect((await screen.findByRole("alert")).textContent).toContain("branch was deleted");
  });

  test("shows no diff for a selected run that never got a branch", async () => {
    const { api, rendered } = await openFork({
      runs: [makeForkRun("run-2", { state: "up-to-date", branch: null })],
    });
    await rendered.user.click(await screen.findByText("merge upstream/main into main"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/^Diff of/)).toBeNull();
    expect(api.callsTo("POST", RUN_DIFF)).toEqual([]);
  });

  test("follows the selection from one run to another", async () => {
    const second = makeForkRun("run-2", {
      state: "synced",
      branch: "forge-sync/older",
      detail: "older run",
    });
    const { api, rendered } = await openFork({ runs: [branchRun, second] });
    api.action(RUN_DIFF, (input) => ({
      run: input.run_id === "run-1" ? branchRun : second,
      against: "target",
      diff: UNIFIED_DIFF,
    }));
    await rendered.user.click(
      await screen
        .findByText("older run")
        .then((node) =>
          within(node.closest("li") as HTMLElement).getByText("merge upstream/main into main"),
        ),
    );
    expect(await screen.findByText("Diff of forge-sync/older")).toBeTruthy();
    expect(api.sent(RUN_DIFF).at(-1)).toEqual({ run_id: "run-2", against: "target" });
  });
});

describe("ForkTab parked run", () => {
  const parkedStatus = (lastRun: ForkRun) => makeForkStatus("alpha", { last_run: lastRun });

  test("raises an alert naming the conflicted files and offering the parked run's decisions", async () => {
    const run = makeForkRun("run-4", { state: "conflict", conflicted_files: ["a.py", "b.py"] });
    await openFork({ status: parkedStatus(run), runs: [run] });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("conflict");
    expect(alert.textContent).toContain("in a.py, b.py");
    expect(alert.textContent).toContain("The run is parked in its worktree.");
    expect(within(alert).getByRole("button", { name: "Re-run agent…" })).toBeTruthy();
    expect(within(alert).getByRole("button", { name: "Abort" })).toBeTruthy();
    expect(within(alert).queryByRole("button", { name: "Apply" })).toBeNull();
  });

  test("falls back to the run's own detail when no files are named", async () => {
    const run = makeForkRun("run-4", {
      state: "needs-attention",
      detail: "checks failed after merge",
    });
    await openFork({ status: parkedStatus(run), runs: [run] });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("checks failed after merge");
  });

  test("shows only the state when the parked run has neither files nor detail", async () => {
    const run = makeForkRun("run-4", { state: "needs-attention", detail: null });
    await openFork({ status: parkedStatus(run), runs: [run] });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("needs-attention");
  });

  test("a run ready to apply can be applied from the alert", async () => {
    const run = makeForkRun("run-4", { state: "ready-to-apply" });
    const { api, rendered } = await openFork({ status: parkedStatus(run), runs: [run] });
    api.action(APPLY, { job_id: "job-4", kind: "fork-apply", title: "Apply run-4" });
    const alert = await screen.findByRole("alert");
    await rendered.user.click(within(alert).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(api.sent(APPLY)).toEqual([{ run_id: "run-4" }]));
  });

  test("tells the viewer to resolve the parked run before syncing again", async () => {
    const run = makeForkRun("run-4", { state: "conflict" });
    await openFork({ status: parkedStatus(run), runs: [run] });
    const [syncNow] = await screen.findAllByRole("button", { name: "Sync now" });
    expect(syncNow?.getAttribute("title")).toBe(
      "A parked run is waiting: apply, retry, or abort it first",
    );
  });

  test("shows no alert when the last run finished", async () => {
    const run = makeForkRun("run-4", { state: "synced" });
    await openFork({ status: parkedStatus(run), runs: [run] });
    await screen.findAllByRole("button", { name: "Sync now" });
    expect(screen.queryByRole("alert")).toBeNull();
    const [syncNow] = screen.getAllByRole("button", { name: "Sync now" });
    expect(syncNow?.getAttribute("title")).toBe(
      "Merge or rebase upstream in an isolated worktree, then run the checks",
    );
  });
});
