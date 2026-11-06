import { describe, expect, test } from "bun:test";
import { act, screen, waitFor, within } from "@testing-library/react";
import { JobStatusMark } from "@/features/jobs/JobStatus";
import { JobsPage, jobPagePath } from "@/features/jobs/JobsPage";
import { useClock } from "@/lib/time";
import {
  emitEvent,
  installHarness,
  MockApiError,
  mockApi,
  NOW_MS,
  NOW_SECONDS,
  renderApp,
  renderWithProviders,
  seedStream,
  settle,
} from "./harness";
import { makeJob, makeJobDetail, makeJobsState, makeLogChunk, waitUntilGone } from "./jobs-support";

installHarness();

const HISTORY = "/api/services/jobs/actions/history" as const;
const GET = "/api/services/jobs/actions/get" as const;
const LOG = "/api/services/jobs/actions/log" as const;
const CANCEL = "/api/services/jobs/actions/cancel" as const;

function jobRow(title: string): HTMLElement {
  const row = screen.getByText(title).closest("li");
  if (!row) throw new Error(`no list row for ${title}`);
  return row;
}

describe("JobsPage list", () => {
  test("lists running and queued jobs under Now with the running and waiting counts", async () => {
    mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    seedStream({
      jobs: makeJobsState([
        makeJob("job-1", { title: "Run gates", status: "running", started_at: NOW_SECONDS - 90 }),
        makeJob("job-2", {
          title: "Sync fork",
          status: "queued",
          waiting_for: "check slot",
        }),
      ]),
    });
    expect(await screen.findByText("1 running · 1 waiting")).toBeTruthy();
    const running = within(jobRow("Run gates"));
    expect(running.getByText("running")).toBeTruthy();
    expect(running.getByText("1 min 30 s")).toBeTruthy();
    const queued = within(jobRow("Sync fork"));
    expect(queued.getByText("queued")).toBeTruthy();
    expect(queued.getByText("dashboard · waiting for check slot")).toBeTruthy();
    expect(queued.getByText("waiting")).toBeTruthy();
  });

  test("says nothing is running while the stream has no active jobs", async () => {
    mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    expect(screen.getByText("Nothing running.")).toBeTruthy();
    expect(screen.queryByText(/ running · /)).toBeNull();
    seedStream({ jobs: makeJobsState([makeJob("job-1", { status: "succeeded" })]) });
    expect(screen.getByText("Nothing running.")).toBeTruthy();
    expect(screen.getByText("0 running · 0 waiting")).toBeTruthy();
  });

  test("shows a progress meter only for running jobs that report progress", async () => {
    mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    seedStream({
      jobs: makeJobsState([
        makeJob("job-1", {
          title: "Bring up alpha",
          status: "running",
          progress: 0.4,
          progress_label: "step 2 of 5",
        }),
        makeJob("job-2", { title: "Scan roots", status: "running", progress: null }),
        makeJob("job-3", { title: "Waiting job", status: "queued", progress: 0.9 }),
      ]),
    });
    const meters = await screen.findAllByRole("meter");
    expect(meters).toHaveLength(1);
    expect(meters[0]?.getAttribute("aria-label")).toBe("Bring up alpha progress");
    expect(meters[0]?.getAttribute("aria-valuenow")).toBe("40");
    expect(within(jobRow("Bring up alpha")).getByText("dashboard · step 2 of 5")).toBeTruthy();
  });

  test("moves the elapsed time of a running job with the shared clock", async () => {
    mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    seedStream({
      jobs: makeJobsState([
        makeJob("job-1", { title: "Long job", status: "running", started_at: NOW_SECONDS - 30 }),
      ]),
    });
    expect(await screen.findByText("30 s")).toBeTruthy();
    act(() => useClock.setState({ now: NOW_MS + 90_000 }));
    expect(screen.getByText("2 min")).toBeTruthy();
  });

  test("shows a loading state for recent jobs until the history arrives", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi().action(HISTORY, async () => {
      await gate;
      return { jobs: [] };
    });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    release();
    expect(await screen.findByText("No jobs yet")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
  });

  test("explains an empty history", async () => {
    const api = mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    expect(await screen.findByText("No jobs yet")).toBeTruthy();
    expect(
      screen.getByText("Checks, syncs, bring-ups, and actions each run as a job with a live log."),
    ).toBeTruthy();
    expect(api.sent(HISTORY)).toEqual([{ project_id: null, kind: null, limit: 80 }]);
  });

  test("lists finished jobs under Recent with their summary, error and kind as the subtitle", async () => {
    mockApi().action(HISTORY, {
      jobs: [
        makeJob("job-1", {
          title: "Gates alpha",
          project_id: "alpha",
          status: "succeeded",
          summary: "12 passed",
        }),
        makeJob("job-2", {
          title: "Sync beta",
          project_id: "beta",
          status: "failed",
          error: "merge conflict",
        }),
        makeJob("job-3", { title: "Scan", status: "cancelled", kind: "discovery.rescan" }),
        makeJob("job-4", { title: "Agent task", status: "attention", kind: "agent.delegate" }),
        makeJob("job-5", {
          title: "Still going",
          status: "running",
          started_at: NOW_SECONDS - 5,
        }),
      ],
    });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    expect(await screen.findByText("Gates alpha")).toBeTruthy();
    expect(within(jobRow("Gates alpha")).getByText("alpha · 12 passed")).toBeTruthy();
    expect(within(jobRow("Gates alpha")).getByText("succeeded")).toBeTruthy();
    expect(within(jobRow("Sync beta")).getByText("beta · merge conflict")).toBeTruthy();
    expect(within(jobRow("Sync beta")).getByText("failed")).toBeTruthy();
    expect(within(jobRow("Scan")).getByText("dashboard · discovery.rescan")).toBeTruthy();
    expect(within(jobRow("Scan")).getByText("cancelled")).toBeTruthy();
    expect(within(jobRow("Agent task")).getByText("needs you")).toBeTruthy();
    expect(screen.queryByText("Still going")).toBeNull();
  });

  test("reports a failed history load and retries it", async () => {
    let attempts = 0;
    mockApi().action(HISTORY, () => {
      attempts += 1;
      if (attempts === 1) throw new MockApiError(500, "history store offline");
      return { jobs: [makeJob("job-1", { title: "Recovered job" })] };
    });
    const { user } = renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    expect((await screen.findByRole("alert")).textContent).toContain("history store offline");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Recovered job")).toBeTruthy();
  });

  test("refreshes the history when the jobs service publishes a new revision", async () => {
    const api = mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    seedStream({ jobs: makeJobsState([]) }, 1);
    await screen.findByText("No jobs yet");
    const before = api.sent(HISTORY).length;
    seedStream({ jobs: makeJobsState([]) }, 2);
    await waitFor(() => expect(api.sent(HISTORY).length).toBeGreaterThan(before));
  });

  test("shows nothing in the detail pane when there are no jobs to pick", async () => {
    mockApi().action(HISTORY, { jobs: [] });
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    await screen.findByText("No jobs yet");
    expect(screen.queryByRole("log", { name: "Job log" })).toBeNull();
  });

  test("with nothing picked it shows the latest finished job, and clicking a row opens that one", async () => {
    const latest = makeJob("job-9", { title: "Gates alpha", ended_at: NOW_SECONDS - 10 });
    const older = makeJob("job-2", { title: "Sync beta", ended_at: NOW_SECONDS - 500 });
    mockApi()
      .action(HISTORY, { jobs: [latest, older] })
      .action(GET, (input) => makeJobDetail(input.job_id === "job-9" ? latest : older))
      .action(LOG, (input) => makeLogChunk(input.job_id, [`log of ${input.job_id}`]));
    const { user, location } = renderWithProviders(<JobsPage jobId={undefined} />, {
      path: "/jobs",
    });
    expect(await screen.findByText("log of job-9")).toBeTruthy();
    await user.click(await screen.findByText("Sync beta"));
    expect(location()).toBe("/jobs/job-2");
  });

  test("with nothing picked it prefers a running job over finished ones", async () => {
    const running = makeJob("job-5", { title: "Checking gamma", status: "running" });
    const finished = makeJob("job-1", { title: "Old run", ended_at: NOW_SECONDS - 10 });
    const api = mockApi()
      .action(HISTORY, { jobs: [finished] })
      .action(GET, makeJobDetail(running))
      .action(LOG, makeLogChunk("job-5", ["still going"]));
    renderWithProviders(<JobsPage jobId={undefined} />, { path: "/jobs" });
    seedStream({ jobs: makeJobsState([running]) });
    expect(await screen.findByText("still going")).toBeTruthy();
    expect(api.sent(GET)).toEqual([{ job_id: "job-5" }]);
  });
});

describe("JobStatusMark", () => {
  test.each([
    ["queued", "queued"],
    ["running", "running"],
    ["succeeded", "succeeded"],
    ["failed", "failed"],
    ["cancelled", "cancelled"],
    ["attention", "needs you"],
    ["something-new", "something-new"],
  ])("labels the %s status as %s", (status, label) => {
    renderWithProviders(<JobStatusMark status={status} />);
    expect(screen.getByText(label)).toBeTruthy();
  });
});

describe("JobsPage detail", () => {
  test("selects the job named in the URL and shows its header, summary and log", async () => {
    const job = makeJob("job-7", {
      title: "Gates alpha",
      kind: "checks.run",
      project_id: "alpha",
      status: "succeeded",
      summary: "All 12 gates passed",
      started_at: NOW_SECONDS - 100,
      ended_at: NOW_SECONDS - 40,
    });
    const api = mockApi()
      .action(HISTORY, { jobs: [job] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-7", ["ruff ok", "mypy ok"]));
    const { user, location } = renderApp("/jobs/job-7");
    expect(await screen.findByRole("heading", { name: "Gates alpha" })).toBeTruthy();
    const log = screen.getByRole("log", { name: "Job log" });
    expect(await within(log).findByText("mypy ok")).toBeTruthy();
    expect(within(log).getByText("ruff ok")).toBeTruthy();
    expect(screen.getByText(/2 lines/)).toBeTruthy();
    expect(screen.getByText("All 12 gates passed")).toBeTruthy();
    expect(screen.getAllByText("1 min").length).toBeGreaterThan(0);
    expect(api.sent(GET)[0]).toEqual({ job_id: "job-7" });
    expect(api.sent(LOG)[0]).toEqual({ job_id: "job-7", offset: 0, limit: 50000 });
    await user.click(screen.getByRole("link", { name: "alpha" }));
    expect(location()).toBe("/p/alpha/quality");
  });

  test("shows the error of a failed job", async () => {
    const job = makeJob("job-3", { title: "Sync beta", status: "failed", error: "merge conflict" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-3", ["CONFLICT in a.py"]));
    renderWithProviders(<JobsPage jobId="job-3" />, { path: "/jobs/job-3" });
    expect(await screen.findByText("merge conflict")).toBeTruthy();
    expect(screen.getByText("failed")).toBeTruthy();
    expect(screen.getByText("checks.run")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  test("tells the reader the job is not known when the lookup fails", async () => {
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, () => {
        throw new MockApiError(404, "no such job: ghost");
      })
      .action(LOG, makeLogChunk("ghost", []));
    renderWithProviders(<JobsPage jobId="ghost" />, { path: "/jobs/ghost" });
    expect(await screen.findByText("This job is not known")).toBeTruthy();
    expect(screen.getByText("no such job: ghost")).toBeTruthy();
    expect(screen.queryByRole("log")).toBeNull();
  });

  test("shows the result of a finished job as formatted JSON", async () => {
    const job = makeJob("job-4", { title: "Gates alpha" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job, { passed: 12, failed: [] }))
      .action(LOG, makeLogChunk("job-4", []));
    const { user } = renderWithProviders(<JobsPage jobId="job-4" />, { path: "/jobs/job-4" });
    const summary = await screen.findByText("Result");
    await user.click(summary);
    expect(screen.getByText(/"passed": 12/)).toBeTruthy();
    expect(screen.getByText(/"failed": \[\]/)).toBeTruthy();
  });

  test("omits the result section when the job produced none", async () => {
    const job = makeJob("job-4", { title: "Gates alpha" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-4", []));
    renderWithProviders(<JobsPage jobId="job-4" />, { path: "/jobs/job-4" });
    await screen.findByRole("heading", { name: "Gates alpha" });
    expect(screen.queryByText("Result")).toBeNull();
    expect(screen.getByText("No output yet.")).toBeTruthy();
  });

  test("appends job.log events to the log as they arrive and marks a running job live", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", ["line one", "line two"]));
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    const log = await screen.findByRole("log", { name: "Job log" });
    await within(log).findByText("line two");
    expect(screen.getByText(/· live/)).toBeTruthy();
    emitEvent({
      type: "job.log",
      data: { job_id: "job-5", offset: 2, lines: ["line three"] },
    });
    expect(await within(log).findByText("line three")).toBeTruthy();
    expect(screen.getByText(/3 lines/)).toBeTruthy();
  });

  test("ignores job.log events for other jobs and other event types", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", ["only line"]));
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    const log = await screen.findByRole("log", { name: "Job log" });
    await within(log).findByText("only line");
    emitEvent({ type: "job.log", data: { job_id: "job-6", offset: 1, lines: ["foreign"] } });
    emitEvent({
      type: "job.progress",
      data: { job_id: "job-5", offset: 1, lines: ["wrong type"] },
    });
    await settle();
    expect(within(log).queryByText("foreign")).toBeNull();
    expect(within(log).queryByText("wrong type")).toBeNull();
    expect(screen.getByText(/1 lines/)).toBeTruthy();
  });

  test("drops event lines the initial read already contained and keeps the new tail", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", ["a", "b", "c"]));
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    const log = await screen.findByRole("log", { name: "Job log" });
    await within(log).findByText("c");
    emitEvent({ type: "job.log", data: { job_id: "job-5", offset: 1, lines: ["b", "c", "d"] } });
    await within(log).findByText("d");
    expect(within(log).getAllByText("c")).toHaveLength(1);
    expect(screen.getByText(/4 lines/)).toBeTruthy();
    emitEvent({ type: "job.log", data: { job_id: "job-5", offset: 0, lines: ["a", "b"] } });
    await settle();
    expect(screen.getByText(/4 lines/)).toBeTruthy();
  });

  test("keeps events that arrive before the initial read finishes and applies them after it", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, async () => {
        await gate;
        return makeLogChunk("job-5", ["first", "second"]);
      });
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    await screen.findByRole("heading", { name: "Running job" });
    emitEvent({ type: "job.log", data: { job_id: "job-5", offset: 2, lines: ["early"] } });
    release();
    const log = screen.getByRole("log", { name: "Job log" });
    expect(await within(log).findByText("early")).toBeTruthy();
    const text = log.textContent ?? "";
    expect(text.indexOf("first")).toBeLessThan(text.indexOf("second"));
    expect(text.indexOf("second")).toBeLessThan(text.indexOf("early"));
  });

  test("reloads the log and detail when another job is selected", async () => {
    const first = makeJob("job-1", { title: "First job" });
    const second = makeJob("job-2", { title: "Second job" });
    const api = mockApi()
      .action(HISTORY, { jobs: [first, second] })
      .action(GET, ({ job_id }) => makeJobDetail(job_id === "job-1" ? first : second))
      .action(LOG, ({ job_id }) =>
        makeLogChunk(job_id, [job_id === "job-1" ? "log of first" : "log of second"]),
      );
    const { user, location } = renderApp("/jobs/job-1");
    await screen.findByText("log of first");
    await user.click(within(jobRow("Second job")).getByRole("link"));
    expect(location()).toBe("/jobs/job-2");
    await screen.findByText("log of second");
    expect(screen.queryByText("log of first")).toBeNull();
    expect(api.sent(LOG).map((body) => (body as { job_id: string }).job_id)).toEqual([
      "job-1",
      "job-2",
    ]);
  });

  test("refetches the detail when the jobs service publishes a new revision", async () => {
    let status: "running" | "succeeded" = "running";
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, () => makeJobDetail(makeJob("job-5", { title: "Running job", status })))
      .action(LOG, makeLogChunk("job-5", []));
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    seedStream({ jobs: makeJobsState([]) }, 1);
    await screen.findByRole("button", { name: "Cancel" });
    status = "succeeded";
    seedStream({ jobs: makeJobsState([]) }, 2);
    await waitUntilGone(() => screen.queryByRole("button", { name: "Cancel" }));
  });
});

describe("JobsPage cancel", () => {
  test("cancels a running job only after the dialog confirms it and sends its id", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    const api = mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", []))
      .action(CANCEL, { ok: true, message: "Cancelled Running job" });
    const { user } = renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/processes are killed/)).toBeTruthy();
    expect(api.sent(CANCEL)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Cancel job" }));
    expect(await screen.findByText("Cancelled Running job")).toBeTruthy();
    expect(api.sent(CANCEL)).toEqual([{ job_id: "job-5" }]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("sends nothing when the cancel confirmation is dismissed", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    const api = mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", []));
    const { user } = renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitUntilGone(() => screen.queryByRole("dialog"));
    expect(api.callsTo("POST", CANCEL)).toEqual([]);
  });

  test("offers cancellation for a queued job too", async () => {
    const job = makeJob("job-6", {
      title: "Queued job",
      status: "queued",
    });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-6", []));
    renderWithProviders(<JobsPage jobId="job-6" />, { path: "/jobs/job-6" });
    expect(await screen.findByRole("button", { name: "Cancel" })).toBeTruthy();
    expect(screen.queryByText(/· live/)).toBeNull();
  });

  test("keeps the confirmation open with the server reason when cancelling fails", async () => {
    const job = makeJob("job-5", { title: "Running job", status: "running" });
    mockApi()
      .action(HISTORY, { jobs: [] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-5", []))
      .action(CANCEL, () => {
        throw new MockApiError(409, "job already finished");
      });
    const { user } = renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel job" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("job already finished");
  });
});

describe("where a job's work lives on its project page", () => {
  test.each([
    ["forks.sync", "/p/alpha/fork"],
    ["standards.bring-up", "/p/alpha/standards"],
    ["agents.delegate", "/p/alpha/agents"],
    ["checks.run", "/p/alpha/quality"],
    ["services.launch", "/p/alpha/launch"],
    ["actions.run", "/p/alpha/actions"],
    ["git.push", "/p/alpha/git"],
    ["github.refresh", "/p/alpha/issues"],
    ["metrics.refresh", "/p/alpha/metrics"],
    ["checks", "/p/alpha/quality"],
  ])("sends a %s job to %s", (kind, path) => {
    expect(jobPagePath({ kind, project_id: "alpha" })).toBe(path);
  });

  test("falls back to the project's overview tab for a kind with no tab of its own", () => {
    expect(jobPagePath({ kind: "mystery.run", project_id: "alpha" })).toBe("/p/alpha");
  });

  test("has no page for a job that belongs to no project", () => {
    expect(jobPagePath({ kind: "checks.run", project_id: null })).toBeNull();
    expect(jobPagePath({ kind: "checks.run" })).toBeNull();
  });

  test("offers to resolve a parked job on its project page", async () => {
    const job = makeJob("job-4", {
      title: "Sync alpha",
      kind: "forks.sync",
      project_id: "alpha",
      status: "attention",
    });
    mockApi()
      .action(HISTORY, { jobs: [job] })
      .action(GET, makeJobDetail(job))
      .action(LOG, makeLogChunk("job-4", []));
    const { user, location } = renderWithProviders(<JobsPage jobId="job-4" />, {
      path: "/jobs/job-4",
    });
    await user.click(await screen.findByRole("link", { name: "Resolve on the project page" }));
    expect(location()).toBe("/p/alpha/fork");
  });

  test("does not offer that for a parked job without a project or for a finished one", async () => {
    const parked = makeJob("job-5", { kind: "forks.sync", project_id: null, status: "attention" });
    mockApi()
      .action(HISTORY, { jobs: [parked] })
      .action(GET, makeJobDetail(parked))
      .action(LOG, makeLogChunk("job-5", []));
    renderWithProviders(<JobsPage jobId="job-5" />, { path: "/jobs/job-5" });
    await screen.findByRole("heading", { name: parked.title });
    expect(screen.queryByRole("link", { name: "Resolve on the project page" })).toBeNull();
  });
});
