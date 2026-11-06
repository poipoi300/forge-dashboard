import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { ForksPage } from "@/features/fleet/ForksPage";
import { makeProject, makeProjectsState } from "./factories";
import { makeFork, makeForkRun, makeSchedulerState } from "./fleet-support";
import {
  installHarness,
  MockApiError,
  mockApi,
  NOW_SECONDS,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";
import { waitUntilGone } from "./jobs-support";

installHarness();

const SYNC = "/api/services/forks/actions/sync" as const;
const APPLY = "/api/services/forks/actions/apply" as const;
const RETRY = "/api/services/forks/actions/retry-agent" as const;
const ABORT = "/api/services/forks/actions/abort" as const;
const PAUSE = "/api/services/scheduler/actions/pause" as const;
const RESUME = "/api/services/scheduler/actions/resume" as const;

function forkRow(name: string): HTMLElement {
  const row = within(screen.getByRole("table")).getByText(name).closest("tr");
  if (!row) throw new Error(`no table row for ${name}`);
  return row;
}

function waitingSection(): HTMLElement {
  const section = screen.getByRole("heading", { name: "Waiting on you" }).closest("section");
  if (!section) throw new Error("no Waiting on you section");
  return section;
}

function seedProjects(...ids: string[]): void {
  seedStream({ projects: makeProjectsState(ids.map((id) => makeProject(id))) });
}

describe("ForksPage table", () => {
  test("explains that nothing is tracked when no project is a fork", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("alpha");
    seedStream({
      forks: {
        forks: [makeFork("alpha", { is_fork: false, upstream_url: null, source: "none" })],
      },
    });
    expect(await screen.findByText("No forks")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("Waiting on you")).toBeNull();
  });

  test("lists a fork with its upstream, branch, counts and the project link", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<ForksPage />);
    seedProjects("alpha");
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", {
            upstream_url: "https://www.github.com/acme/widgets",
            upstream_branch: "develop",
            target_branch: "trunk",
            behind: 7,
            ahead: 2,
          }),
        ],
      },
    });
    await screen.findByRole("table");
    const row = forkRow("alpha");
    expect(within(row).getByText("trunk")).toBeTruthy();
    const upstream = within(row).getByText("acme/widgets@develop");
    expect(upstream.getAttribute("title")).toBe("https://www.github.com/acme/widgets");
    expect(within(row).getByText("7")).toBeTruthy();
    expect(within(row).getByText("2")).toBeTruthy();
    await user.click(within(row).getByRole("link", { name: "alpha" }));
    expect(location()).toBe("/p/alpha/fork");
  });

  test("tracks a project that only has an upstream remote and shows unknown counts as dashes", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("beta");
    seedStream({
      forks: {
        forks: [
          makeFork("beta", {
            is_fork: false,
            source: "upstream-remote",
            upstream_url: "git@example.com:acme/beta.git",
            upstream_branch: null,
            behind: null,
            ahead: null,
          }),
        ],
      },
    });
    await screen.findByRole("table");
    const row = forkRow("beta");
    expect(within(row).getByText("git@example.com:acme/beta.git")).toBeTruthy();
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  test("shows a dash when a tracked fork has no upstream url", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("gamma");
    seedStream({
      forks: { forks: [makeFork("gamma", { upstream_url: null, upstream_branch: null })] },
    });
    await screen.findByRole("table");
    const row = forkRow("gamma");
    expect(row.querySelectorAll("td")[1]?.textContent).toBe("—");
  });

  test("distinguishes projects that share a name by appending their id", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedStream({
      projects: makeProjectsState([
        makeProject("widgets-a", { name: "widgets" }),
        makeProject("widgets-b", { name: "widgets" }),
      ]),
      forks: { forks: [makeFork("widgets-a"), makeFork("widgets-b")] },
    });
    expect(await screen.findByText("widgets (widgets-a)")).toBeTruthy();
    expect(screen.getByText("widgets (widgets-b)")).toBeTruthy();
  });

  test("falls back to the project id for a fork whose project is not registered", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedStream({ forks: { forks: [makeFork("orphan")] } });
    expect(await screen.findByRole("link", { name: "orphan" })).toBeTruthy();
  });

  test("predicts a clean sync or lists the files that would conflict", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("alpha", "beta", "gamma");
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", {
            prediction: { clean: true, conflicted_files: [], checked_at: NOW_SECONDS },
          }),
          makeFork("beta", {
            prediction: {
              clean: false,
              conflicted_files: ["a.py", "b.py"],
              checked_at: NOW_SECONDS,
            },
          }),
          makeFork("gamma", { prediction: null }),
        ],
      },
    });
    expect(await within(forkRow("alpha")).findByText("clean")).toBeTruthy();
    const conflicts = within(forkRow("beta")).getByText("2 conflicts");
    expect(conflicts.closest("[title]")?.getAttribute("title")).toBe("a.py, b.py");
    expect(within(forkRow("gamma")).queryByText("clean")).toBeNull();
  });

  test("shows the last run's state with its age, or never, and the next scheduled sync or manual", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("alpha", "beta");
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", {
            last_run: makeForkRun("run-1", "alpha", {
              state: "synced",
              started_at: NOW_SECONDS - 7200,
            }),
            next_sync_at: NOW_SECONDS + 3 * 3600,
          }),
          makeFork("beta", { last_run: null, next_sync_at: null }),
        ],
      },
    });
    await screen.findByRole("table");
    const alpha = within(forkRow("alpha"));
    expect(alpha.getByText("synced")).toBeTruthy();
    expect(alpha.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(alpha.getByText(timeText("in 3 h"))).toBeTruthy();
    const beta = within(forkRow("beta"));
    expect(beta.getByText("never")).toBeTruthy();
    expect(beta.getByText("manual")).toBeTruthy();
  });

  test("starts a sync for exactly the clicked fork and announces the job", async () => {
    const api = mockApi().action(SYNC, {
      job_id: "job-sync",
      kind: "forks.sync",
      title: "Sync alpha",
    });
    const { user, location } = renderWithProviders(<ForksPage />);
    seedProjects("alpha", "beta");
    seedStream({ forks: { forks: [makeFork("alpha"), makeFork("beta")] } });
    await settle();
    await user.click(within(forkRow("beta")).getByRole("button", { name: "Sync" }));
    expect(await screen.findByText("Started: Sync alpha")).toBeTruthy();
    expect(api.sent(SYNC)).toEqual([{ project_id: "beta" }]);
    await user.click(screen.getByRole("button", { name: "View job" }));
    expect(location()).toBe("/jobs/job-sync");
  });

  test("reports a failed sync as a toast", async () => {
    mockApi().action(SYNC, () => {
      throw new MockApiError(409, "a sync is already running");
    });
    const { user } = renderWithProviders(<ForksPage />);
    seedProjects("alpha");
    seedStream({ forks: { forks: [makeFork("alpha")] } });
    await settle();
    await user.click(within(forkRow("alpha")).getByRole("button", { name: "Sync" }));
    expect(await screen.findByText("Sync fork failed")).toBeTruthy();
    expect(screen.getByText("a sync is already running")).toBeTruthy();
  });
});

describe("ForksPage scheduler control", () => {
  test("offers to pause all schedules while the scheduler runs", async () => {
    const api = mockApi().action(PAUSE, { ok: true, message: "Schedules paused" });
    const { user } = renderWithProviders(<ForksPage />);
    seedStream({ scheduler: makeSchedulerState(false) });
    await settle();
    expect(screen.queryByText(/scheduler is paused/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume schedules" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Pause all schedules" }));
    expect(await screen.findByText("Schedules paused")).toBeTruthy();
    expect(api.sent(PAUSE)).toEqual([{}]);
  });

  test("says the scheduler is paused and offers to resume it", async () => {
    const api = mockApi().action(RESUME, { ok: true, message: "Schedules resumed" });
    const { user } = renderWithProviders(<ForksPage />);
    seedStream({ scheduler: makeSchedulerState(true) });
    expect(
      await screen.findByText("The scheduler is paused: no sync runs on its own."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause all schedules" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Resume schedules" }));
    expect(await screen.findByText("Schedules resumed")).toBeTruthy();
    expect(api.sent(RESUME)).toEqual([{}]);
  });
});

describe("ForksPage parked runs", () => {
  function seedParkedForks(): void {
    seedProjects("alpha", "beta", "gamma", "delta");
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", {
            last_run: makeForkRun("run-ready", "alpha", {
              state: "ready-to-apply",
              detail: "checks passed; waiting for a clean checkout",
            }),
          }),
          makeFork("beta", {
            last_run: makeForkRun("run-conflict", "beta", {
              state: "conflict",
              detail: "2 files conflict",
            }),
          }),
          makeFork("gamma", {
            last_run: makeForkRun("run-attn", "gamma", {
              state: "needs-attention",
              detail: "agent gave up",
            }),
          }),
          makeFork("delta", { last_run: makeForkRun("run-done", "delta", { state: "synced" }) }),
        ],
      },
    });
  }

  test("lists only parked runs under Waiting on you with their detail and link", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<ForksPage />);
    seedParkedForks();
    await screen.findByRole("heading", { name: "Waiting on you" });
    const waiting = within(waitingSection());
    expect(waiting.getByText("checks passed; waiting for a clean checkout")).toBeTruthy();
    expect(waiting.getByText("2 files conflict")).toBeTruthy();
    expect(waiting.getByText("agent gave up")).toBeTruthy();
    expect(waiting.getByText("ready-to-apply")).toBeTruthy();
    expect(waiting.getByText("conflict")).toBeTruthy();
    expect(waiting.getByText("needs-attention")).toBeTruthy();
    expect(waiting.queryByText("delta")).toBeNull();
    await user.click(waiting.getByRole("link", { name: "beta" }));
    expect(location()).toBe("/p/beta/fork");
  });

  test("offers Apply only for a run that is ready and sends its run id", async () => {
    const api = mockApi().action(APPLY, {
      job_id: "job-apply",
      kind: "forks.apply",
      title: "Apply run-ready",
    });
    const { user } = renderWithProviders(<ForksPage />);
    seedParkedForks();
    await screen.findByRole("heading", { name: "Waiting on you" });
    const waiting = within(waitingSection());
    expect(waiting.getAllByRole("button", { name: "Apply" })).toHaveLength(1);
    await user.click(waiting.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Started: Apply run-ready")).toBeTruthy();
    expect(api.sent(APPLY)).toEqual([{ run_id: "run-ready" }]);
  });

  test("re-runs the agent with the instructions typed into the dialog", async () => {
    const api = mockApi().action(RETRY, {
      job_id: "job-retry",
      kind: "forks.retry",
      title: "Retry run-conflict",
    });
    const { user } = renderWithProviders(<ForksPage />);
    seedParkedForks();
    await screen.findByRole("heading", { name: "Waiting on you" });
    const betaRow = within(waitingSection()).getByRole("link", { name: "beta" }).closest("li");
    if (!betaRow) throw new Error("missing beta row");
    await user.click(within(betaRow).getByRole("button", { name: "Re-run agent…" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(
      within(dialog).getByPlaceholderText("What the agent missed"),
      "keep our version of a.py",
    );
    await user.click(within(dialog).getByRole("button", { name: "Retry agent" }));
    expect(await screen.findByText("Started: Retry run-conflict")).toBeTruthy();
    expect(api.sent(RETRY)).toEqual([
      { run_id: "run-conflict", instructions: "keep our version of a.py" },
    ]);
  });

  test("aborts a run only after its id is typed to confirm", async () => {
    const api = mockApi().action(ABORT, { ok: true, message: "Aborted run-attn" });
    const { user } = renderWithProviders(<ForksPage />);
    seedParkedForks();
    await screen.findByRole("heading", { name: "Waiting on you" });
    const gammaRow = within(waitingSection()).getByRole("link", { name: "gamma" }).closest("li");
    if (!gammaRow) throw new Error("missing gamma row");
    await user.click(within(gammaRow).getByRole("button", { name: "Abort" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("run-attn")).toBeTruthy();
    const submit = within(dialog).getByRole("button", { name: "Abort" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "run-att");
    expect(submit.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "n");
    expect(submit.disabled).toBe(false);
    expect(api.sent(ABORT)).toEqual([]);
    await user.click(submit);
    expect(await screen.findByText("Aborted run-attn")).toBeTruthy();
    expect(api.sent(ABORT)).toEqual([{ run_id: "run-attn" }]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("hides the Waiting on you section once no run is parked", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedParkedForks();
    await screen.findByRole("heading", { name: "Waiting on you" });
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", { last_run: makeForkRun("run-ready", "alpha", { state: "synced" }) }),
        ],
      },
    });
    await settle();
    expect(screen.queryByRole("heading", { name: "Waiting on you" })).toBeNull();
  });

  test("ignores a parked run on a project that is not a tracked fork", async () => {
    mockApi();
    renderWithProviders(<ForksPage />);
    seedProjects("alpha");
    seedStream({
      forks: {
        forks: [
          makeFork("alpha", {
            is_fork: false,
            upstream_url: null,
            last_run: makeForkRun("run-1", "alpha", { state: "conflict" }),
          }),
        ],
      },
    });
    await settle();
    expect(screen.queryByRole("heading", { name: "Waiting on you" })).toBeNull();
  });
});
