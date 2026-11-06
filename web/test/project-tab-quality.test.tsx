import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { QualityTab } from "@/features/project/tabs/QualityTab";
import { NOW_SECONDS } from "./constants";
import { makeProject, type ProjectSummary } from "./factories";
import {
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
  makeCheckHistory,
  makeCheckRun,
  makeChecksState,
  makeCoverage,
  makeDetail,
  makeFailedCase,
  makeGate,
  makeGithubProject,
  makeHistoryPoint,
  makeIssueFacts,
  makeJunitReport,
  makeLatestReport,
  makeWorkflowJob,
  makeWorkflowRun,
} from "./project-tabs-support";

installHarness();

const HISTORY = "/api/services/checks/actions/history" as const;
const LATEST = "/api/services/checks/actions/latest" as const;
const RUN_CHECKS = "/api/services/checks/actions/run" as const;
const RUN_COMMIT_GATES = "/api/services/checks/actions/run-commit-gates" as const;
const RUN_PUSH_GATE = "/api/services/checks/actions/run-push-gate" as const;
const RUN_GATE = "/api/services/checks/actions/run-gate" as const;
const RUNS = "/api/services/github/actions/runs" as const;
const RUN = "/api/services/github/actions/run" as const;
const RERUN = "/api/services/github/actions/rerun-failed-jobs" as const;

interface QualityData {
  history?: Record<string, Schemas["CheckHistory"]>;
  latest?: Record<string, Schemas["LatestReport"]>;
  runs?: Schemas["GitHubRun"][];
  details?: Record<number, Schemas["GitHubRunDetail"]>;
}

/** Route the per-check reads and the GitHub CI reads; unspecified checks answer empty. */
function mockQuality(data: QualityData = {}): MockApi {
  return mockApi()
    .action(HISTORY, (input) => {
      const checkId = (input as { check_id: string }).check_id;
      return data.history?.[checkId] ?? makeCheckHistory(checkId, [], null);
    })
    .action(LATEST, (input) => {
      const checkId = (input as { check_id: string }).check_id;
      return data.latest?.[checkId] ?? makeLatestReport(makeCheckRun(checkId));
    })
    .action(RUNS, { repo: "acme/alpha", workflows: [], runs: data.runs ?? [] })
    .action(RUN, (input) => {
      const runId = (input as { run_id: number }).run_id;
      const detail = data.details?.[runId];
      if (!detail) throw new MockApiError(404, `no run ${runId}`);
      return detail;
    });
}

function seedChecks(overrides: Partial<Schemas["ProjectChecks"]>): void {
  seedStream({ checks: makeChecksState("alpha", overrides) });
}

function renderQuality(subject: ProjectSummary = makeProject("alpha")) {
  return renderWithProviders(<QualityTab project={subject} detail={makeDetail(subject)} />);
}

function sectionOf(title: string): ReturnType<typeof within> {
  const section = screen.getByRole("heading", { name: title }).closest("section");
  if (!section) throw new Error(`no section titled ${title}`);
  return within(section);
}

function checkRow(label: string): ReturnType<typeof within> {
  const row = sectionOf("Checks").getByText(label).closest("tr");
  if (!row) throw new Error(`no check row ${label}`);
  return within(row);
}

function toneOf(element: HTMLElement): string | null | undefined {
  return element.closest("[data-tone]")?.getAttribute("data-tone");
}

describe("QualityTab checks table", () => {
  test("invites declaring checks and offers no run-all while none exist", async () => {
    mockQuality();
    renderQuality();
    expect(await screen.findByText("No checks declared")).toBeTruthy();
    expect(screen.getByText("[[checks]]")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run all" })).toBeNull();
  });

  test("treats a project whose checks list is empty as having no checks", async () => {
    mockQuality();
    seedChecks({ checks: [] });
    renderQuality();
    expect(await screen.findByText("No checks declared")).toBeTruthy();
  });

  test("ignores the checks of other projects", async () => {
    mockQuality();
    seedStream({
      checks: {
        projects: [
          ...makeChecksState("beta", { checks: [makeCheckRun("unit", { label: "Beta tests" })] })
            .projects,
          ...makeChecksState("alpha", { checks: [makeCheckRun("lint", { label: "Alpha lint" })] })
            .projects,
        ],
      },
    });
    renderQuality();
    expect(await screen.findByText("Alpha lint")).toBeTruthy();
    expect(screen.queryByText("Beta tests")).toBeNull();
  });

  test("shows each check's stage, outcome, test tally, duration, commit and age", async () => {
    mockQuality();
    seedChecks({
      checks: [
        makeCheckRun("unit", {
          label: "Unit tests",
          stage: "commit",
          outcome: "failed",
          tests: 10,
          failures: 1,
          errors: 1,
          duration_s: 252,
          sha: "0123456789abcdef",
          dirty: true,
          ran_at: NOW_SECONDS - 7200,
        }),
        makeCheckRun("lint", {
          label: "Lint",
          stage: "push",
          outcome: "passed",
          tests: 4,
          failures: null,
          errors: null,
          duration_s: 0.5,
          sha: "fedcba9876543210",
          ran_at: NOW_SECONDS - 600,
        }),
        makeCheckRun("types", {
          label: "Types",
          outcome: "never",
          tests: null,
          duration_s: null,
          sha: null,
          ran_at: null,
        }),
      ],
    });
    renderQuality();
    await screen.findByText("Unit tests");
    const unit = checkRow("Unit tests");
    expect(unit.getByText("commit")).toBeTruthy();
    expect(unit.getByText("8/10")).toBeTruthy();
    expect(unit.getByText("4 min 12 s")).toBeTruthy();
    expect(unit.getByText(/01234567/).textContent).toBe("01234567 +edits");
    expect(unit.getByText(timeText("2 h ago"))).toBeTruthy();
    const lint = checkRow("Lint");
    expect(lint.getByText("push")).toBeTruthy();
    expect(lint.getByText("4/4")).toBeTruthy();
    expect(lint.getByText("500 ms")).toBeTruthy();
    expect(lint.getByText("fedcba98").textContent).toBe("fedcba98");
    expect(lint.queryByText("+edits")).toBeNull();
    const types = checkRow("Types");
    expect(types.getAllByText("—")).toHaveLength(3);
    expect(types.getByText("never")).toBeTruthy();
  });

  test("marks every outcome with its tone and wording", async () => {
    mockQuality();
    const outcomes = [
      ["passed", "passed", "good"],
      ["failed", "failed", "bad"],
      ["error", "error", "bad"],
      ["timeout", "timeout", "bad"],
      ["running", "running", "info"],
      ["never", "not run yet", "neutral"],
    ] as const;
    seedChecks({
      checks: outcomes.map(([outcome]) =>
        makeCheckRun(`c-${outcome}`, { label: `Check ${outcome}`, outcome }),
      ),
    });
    renderQuality();
    await screen.findByText("Check passed");
    for (const [outcome, wording, tone] of outcomes) {
      const row = checkRow(`Check ${outcome}`);
      expect(toneOf(row.getByText(wording))).toBe(tone);
    }
  });

  test("flags a result that is stale because HEAD moved", async () => {
    mockQuality();
    seedChecks({
      checks: [
        makeCheckRun("unit", { label: "Unit tests", stale: true }),
        makeCheckRun("lint", { label: "Lint", stale: false }),
      ],
    });
    renderQuality();
    await screen.findByText("Unit tests");
    expect(checkRow("Unit tests").getByText("stale").getAttribute("title")).toBe(
      "HEAD moved or the tree changed since this ran",
    );
    expect(checkRow("Lint").queryByText("stale")).toBeNull();
  });

  test("runs all checks, or one check, with the exact request body", async () => {
    const api = mockQuality().action(RUN_CHECKS, {
      job_id: "job-1",
      kind: "checks",
      title: "Checks",
    });
    seedChecks({
      checks: [
        makeCheckRun("unit", { label: "Unit tests" }),
        makeCheckRun("lint", { label: "Lint" }),
      ],
    });
    const { user } = renderQuality();
    await user.click(await screen.findByRole("button", { name: "Run all" }));
    await waitFor(() =>
      expect(api.sent(RUN_CHECKS)).toEqual([
        { project_id: "alpha", check_id: null, on_head: null },
      ]),
    );
    await user.click(checkRow("Lint").getByRole("button", { name: "Run Lint" }));
    await waitFor(() => expect(api.sent(RUN_CHECKS)).toHaveLength(2));
    expect(api.sent(RUN_CHECKS)[1]).toEqual({
      project_id: "alpha",
      check_id: "lint",
      on_head: null,
    });
    expect(await screen.findAllByText("Started: Checks")).toHaveLength(2);
  });
});

describe("QualityTab check detail", () => {
  test("opens the first check that is neither passing nor unrun", async () => {
    const api = mockQuality();
    seedChecks({
      checks: [
        makeCheckRun("lint", { label: "Lint", outcome: "passed" }),
        makeCheckRun("docs", { label: "Docs", outcome: "never" }),
        makeCheckRun("unit", { label: "Unit tests", outcome: "failed" }),
        makeCheckRun("types", { label: "Types", outcome: "error" }),
      ],
    });
    renderQuality();
    await screen.findByText("Unit tests");
    await waitFor(() => expect(api.sent(HISTORY)).toHaveLength(1));
    expect(api.sent(HISTORY)).toEqual([{ project_id: "alpha", check_id: "unit", limit: 100 }]);
    expect(api.sent(LATEST)).toEqual([{ project_id: "alpha", check_id: "unit" }]);
  });

  test("opens the first check when every one passed or never ran", async () => {
    const api = mockQuality();
    seedChecks({
      checks: [
        makeCheckRun("lint", { label: "Lint", outcome: "passed" }),
        makeCheckRun("docs", { label: "Docs", outcome: "never" }),
      ],
    });
    renderQuality();
    await screen.findByText("Lint");
    await waitFor(() => expect(api.sent(HISTORY)).toHaveLength(1));
    expect(api.sent(HISTORY)).toEqual([{ project_id: "alpha", check_id: "lint", limit: 100 }]);
  });

  test("switches the detail to the check whose row is clicked", async () => {
    const api = mockQuality();
    seedChecks({
      checks: [
        makeCheckRun("lint", { label: "Lint", outcome: "failed" }),
        makeCheckRun("docs", { label: "Docs", outcome: "passed" }),
      ],
    });
    const { user } = renderQuality();
    await screen.findByText("Docs");
    await user.click(checkRow("Docs").getByText("Docs"));
    await waitFor(() =>
      expect(api.sent(HISTORY).map((body) => (body as { check_id: string }).check_id)).toContain(
        "docs",
      ),
    );
    expect(api.sent(LATEST).map((body) => (body as { check_id: string }).check_id)).toContain(
      "docs",
    );
  });

  test("shows loading rows for both halves of the detail while they load", async () => {
    mockQuality();
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    renderQuality();
    expect(screen.getByText("Unit tests")).toBeTruthy();
    expect(screen.getAllByRole("status", { name: "Loading" })).toHaveLength(2);
    expect(await screen.findByText(/No runs recorded yet/)).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
  });

  test("tells the user to run a check that has no history, and that it writes no JUnit report", async () => {
    mockQuality();
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    renderQuality();
    expect(
      await screen.findByText(
        "No runs recorded yet. Run it to start its pass rate and duration history.",
      ),
    ).toBeTruthy();
    expect(
      await screen.findByText("This check writes no JUnit report; its exit code decides."),
    ).toBeTruthy();
    expect(screen.queryByText("Last output lines")).toBeNull();
  });

  test("shows the pass rate, the pass-fail strip and the duration trend of a check's history", async () => {
    mockQuality({
      history: {
        unit: makeCheckHistory(
          "unit",
          [
            makeHistoryPoint(NOW_SECONDS - 7200, {
              outcome: "passed",
              duration_s: 12,
              tests: 10,
              failures: 0,
            }),
            makeHistoryPoint(NOW_SECONDS - 3600, {
              outcome: "failed",
              duration_s: 95,
              tests: 10,
              failures: 2,
              sha: "abc",
            }),
            makeHistoryPoint(NOW_SECONDS - 1800, { outcome: "passed", duration_s: 14 }),
          ],
          0.6667,
        ),
      },
    });
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    const { user } = renderQuality();
    expect(await screen.findByText("67%")).toBeTruthy();
    expect(screen.getByText("pass rate")).toBeTruthy();
    expect(screen.getByRole("img", { name: "Runs: 2 of 3 runs passed" })).toBeTruthy();
    expect(screen.getByText("2 of 3 passed")).toBeTruthy();
    const trend = screen.getByRole("figure", { name: "Duration per run" });
    await user.click(within(trend).getByRole("button", { name: "Table" }));
    const rows = within(trend).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]?.textContent).toContain("14 s");
    expect(rows[1]?.textContent).toContain("1 min 35 s");
    expect(rows[1]?.textContent).toContain("failed");
    expect(rows[2]?.textContent).toContain("12 s");
  });

  test("shows a dash when a check has history but no pass rate yet", async () => {
    mockQuality({
      history: {
        unit: makeCheckHistory("unit", [makeHistoryPoint(NOW_SECONDS - 600)], null),
      },
    });
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    renderQuality();
    expect(await screen.findByText("pass rate")).toBeTruthy();
    expect(screen.getByText("pass rate").nextElementSibling?.textContent).toBe("—");
  });

  test("summarises the JUnit report and lets each failing test be expanded to its detail", async () => {
    const run = makeCheckRun("unit", { label: "Unit tests", outcome: "failed" });
    mockQuality({
      latest: {
        unit: makeLatestReport(run, {
          report: makeJunitReport({
            tests: 10,
            failures: 2,
            errors: 1,
            skipped: 3,
            failed: [
              makeFailedCase("test_parse", {
                suite: "tests.parser",
                message: "assertion failed",
                detail: "Traceback (most recent call last)\n  File parser.py",
              }),
              makeFailedCase("test_empty", {
                suite: "tests.parser",
                message: "no output",
                detail: "",
              }),
            ],
          }),
        }),
      },
    });
    seedChecks({ checks: [run] });
    const { user } = renderQuality();
    expect(await screen.findByText("10 tests · 2 failures · 1 errors · 3 skipped")).toBeTruthy();
    const parse = screen.getByRole("button", { name: /test_parse/ });
    expect(parse.getAttribute("aria-expanded")).toBe("false");
    expect(within(parse).getByText("tests.parser · assertion failed")).toBeTruthy();
    await user.click(parse);
    expect(parse.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/Traceback \(most recent call last\)/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /test_empty/ }));
    expect(parse.getAttribute("aria-expanded")).toBe("false");
    const empty = screen.getByRole("button", { name: /test_empty/ });
    expect(empty.getAttribute("aria-expanded")).toBe("true");
    expect(empty.parentElement?.querySelector("pre")?.textContent).toBe("no output");
    expect(screen.queryByText(/Traceback/)).toBeNull();
    await user.click(empty);
    expect(empty.getAttribute("aria-expanded")).toBe("false");
    expect(empty.parentElement?.querySelector("pre")).toBeNull();
  });

  test("says no tests failed when the last report has none failing", async () => {
    const run = makeCheckRun("unit", { label: "Unit tests", outcome: "error" });
    mockQuality({ latest: { unit: makeLatestReport(run, { report: makeJunitReport() }) } });
    seedChecks({ checks: [run] });
    renderQuality();
    expect(await screen.findByText("No failing tests in the last report.")).toBeTruthy();
  });

  test("offers the tail of the last output in a collapsible block", async () => {
    const run = makeCheckRun("unit", { label: "Unit tests", outcome: "failed" });
    mockQuality({
      latest: { unit: makeLatestReport(run, { log_tail: ["collecting", "1 failed"] }) },
    });
    seedChecks({ checks: [run] });
    renderQuality();
    const summary = await screen.findByText("Last output lines");
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.closest("details")?.querySelector("pre")?.textContent).toBe(
      "collecting\n1 failed",
    );
  });

  test("reports history and latest-report reads that fail", async () => {
    mockQuality()
      .action(HISTORY, () => {
        throw new MockApiError(500, "history unavailable");
      })
      .action(LATEST, () => {
        throw new MockApiError(500, "report unavailable");
      });
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    renderQuality();
    expect(await screen.findByText("history unavailable")).toBeTruthy();
    expect(await screen.findByText("report unavailable")).toBeTruthy();
  });

  test("re-reads the open check when the checks service publishes a new revision", async () => {
    const api = mockQuality();
    seedChecks({ checks: [makeCheckRun("unit", { label: "Unit tests", outcome: "failed" })] });
    renderQuality();
    await screen.findByText(/No runs recorded yet/);
    expect(api.callsTo("POST", HISTORY)).toHaveLength(1);
    seedStream(
      {
        checks: makeChecksState("alpha", { checks: [makeCheckRun("unit", { outcome: "failed" })] }),
      },
      2,
    );
    await waitFor(() => expect(api.callsTo("POST", HISTORY)).toHaveLength(2));
    await waitFor(() => expect(api.callsTo("POST", LATEST)).toHaveLength(2));
  });
});

describe("QualityTab gates", () => {
  test("says there is no pre-commit config and offers no gate actions", async () => {
    mockQuality();
    renderQuality();
    expect(await screen.findByText("No pre-commit gates")).toBeTruthy();
    expect(screen.getByText("This repository has no .pre-commit-config.yaml.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Commit gates" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Push gate" })).toBeNull();
  });

  test("says why the gates could not be read instead of claiming there are none", async () => {
    mockQuality();
    seedChecks({ gates_error: ".pre-commit-config.yaml: mapping values are not allowed here" });
    renderQuality();
    expect(await screen.findByText("The gates could not be read")).toBeTruthy();
    expect(
      screen.getByText(".pre-commit-config.yaml: mapping values are not allowed here"),
    ).toBeTruthy();
    expect(screen.queryByText("No pre-commit gates")).toBeNull();
  });

  test("lists each gate with its stages and runs one gate by id", async () => {
    const api = mockQuality().action(RUN_GATE, {
      job_id: "job-2",
      kind: "checks",
      title: "Gate ruff",
    });
    seedChecks({
      gates: [
        makeGate("ruff", { name: "Lint with ruff", stages: ["pre-commit", "pre-push"] }),
        makeGate("mypy", { name: "Type check", stages: ["pre-commit"] }),
      ],
    });
    const { user } = renderQuality();
    const gates = sectionOf("Gates");
    expect(await gates.findByText("ruff")).toBeTruthy();
    expect(gates.getByText("Lint with ruff")).toBeTruthy();
    expect(gates.getByText("pre-push")).toBeTruthy();
    expect(gates.getAllByText("pre-commit")).toHaveLength(2);
    await user.click(gates.getByRole("button", { name: "Run ruff" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Fixers rewrite files in the working tree/)).toBeTruthy();
    expect(api.callsTo("POST", RUN_GATE)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Run gate" }));
    await waitFor(() =>
      expect(api.sent(RUN_GATE)).toEqual([{ project_id: "alpha", gate_id: "ruff" }]),
    );
  });

  test("shows the first six gates and folds the rest away until asked", async () => {
    mockQuality();
    seedChecks({
      gates: ["one", "two", "three", "four", "five", "six", "seven", "eight"].map((id) =>
        makeGate(id),
      ),
    });
    const { user } = renderQuality();
    const gates = sectionOf("Gates");
    expect(await gates.findByText("six")).toBeTruthy();
    expect(gates.queryByText("seven")).toBeNull();
    await user.click(gates.getByRole("button", { name: "Show all 8 gates" }));
    expect(gates.getByText("eight")).toBeTruthy();
    await user.click(gates.getByRole("button", { name: "Show fewer" }));
    expect(gates.queryByText("seven")).toBeNull();
  });

  test("lists six gates without a toggle", async () => {
    mockQuality();
    seedChecks({ gates: ["one", "two", "three", "four", "five", "six"].map((id) => makeGate(id)) });
    renderQuality();
    const gates = sectionOf("Gates");
    expect(await gates.findByText("six")).toBeTruthy();
    expect(gates.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  test("runs the commit gates and the push gate for the project", async () => {
    const api = mockQuality()
      .action(RUN_COMMIT_GATES, { job_id: "job-3", kind: "checks", title: "Commit gates" })
      .action(RUN_PUSH_GATE, { job_id: "job-4", kind: "checks", title: "Push gate" });
    seedChecks({ gates: [makeGate("ruff")] });
    const { user } = renderQuality();
    await user.click(await screen.findByRole("button", { name: "Commit gates" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Run all commit gates",
      }),
    );
    await waitFor(() => expect(api.sent(RUN_COMMIT_GATES)).toEqual([{ project_id: "alpha" }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(screen.getByRole("button", { name: "Push gate" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Run push gate" }),
    );
    await waitFor(() => expect(api.sent(RUN_PUSH_GATE)).toEqual([{ project_id: "alpha" }]));
  });
});

describe("QualityTab coverage", () => {
  test("says no coverage report exists until a check produces one", async () => {
    mockQuality();
    renderQuality();
    expect(await screen.findByText("No coverage report")).toBeTruthy();
  });

  test("reports the percentage, line counts, format, source and age with a matching meter", async () => {
    mockQuality();
    seedChecks({
      coverage: makeCoverage(95.46, {
        lines_covered: 954,
        lines_total: 1000,
        format: "lcov",
        path: "coverage/lcov.info",
        measured_at: NOW_SECONDS - 1200,
      }),
    });
    renderQuality();
    const coverage = sectionOf("Coverage");
    expect(await coverage.findByText("95.5%")).toBeTruthy();
    expect(coverage.getByText("954 of 1000 lines · lcov")).toBeTruthy();
    expect(coverage.getByText("coverage/lcov.info", { exact: false }).textContent).toBe(
      "measured 20 min ago from coverage/lcov.info",
    );
    const meter = coverage.getByRole("meter", { name: "Line coverage" });
    expect(meter.getAttribute("aria-valuenow")).toBe("95");
  });

  for (const percent of [75, 40]) {
    test(`fills the meter to ${percent}% for ${percent}% line coverage`, async () => {
      mockQuality();
      seedChecks({ coverage: makeCoverage(percent) });
      renderQuality();
      const coverage = sectionOf("Coverage");
      const meter = await coverage.findByRole("meter", { name: "Line coverage" });
      expect(meter.getAttribute("aria-valuenow")).toBe(String(percent));
      expect(coverage.getByText(`${percent}.0%`)).toBeTruthy();
    });
  }
});

describe("QualityTab CI without GitHub", () => {
  const cases: [string, ProjectSummary, RegExp][] = [
    [
      "unauthenticated",
      makeProject("alpha", { issues: makeIssueFacts("unauthenticated") }),
      /Sign in to GitHub in Settings to read workflow runs\./,
    ],
    [
      "rate limited",
      makeProject("alpha", { issues: makeIssueFacts("rate-limited") }),
      /GitHub's rate limit is spent; runs come back after it resets\./,
    ],
    [
      "not on GitHub",
      makeProject("alpha", { issues: makeIssueFacts("not-on-github") }),
      /This repository has no GitHub remote\./,
    ],
    ["unknown", makeProject("alpha"), /This repository has no GitHub remote\./],
  ];
  for (const [status, subject, message] of cases) {
    test(`explains why CI cannot be read for a ${status} project and never asks for runs`, async () => {
      const api = mockQuality();
      renderQuality(subject);
      expect(await screen.findByText("CI is read from GitHub")).toBeTruthy();
      expect(screen.getByText(message)).toBeTruthy();
      await settle();
      expect(api.callsTo("POST", RUNS)).toEqual([]);
    });
  }
});

describe("QualityTab CI runs", () => {
  const github = makeGithubProject("alpha");

  test("asks for the latest runs of every branch and describes the section as GitHub Actions", async () => {
    const api = mockQuality();
    renderQuality(github);
    expect(await screen.findByText("GitHub Actions")).toBeTruthy();
    await screen.findByText("No workflow runs");
    expect(api.sent(RUNS)).toEqual([{ project_id: "alpha", branch: null, limit: 30 }]);
    expect(api.callsTo("POST", RUN)).toEqual([]);
    expect(screen.getByText("GitHub Actions has no runs for this repository.")).toBeTruthy();
  });

  test("lists each run with workflow, title, outcome, branch, duration and age", async () => {
    mockQuality({
      runs: [
        makeWorkflowRun(12, {
          workflow: "CI",
          number: 12,
          title: "Fix parser",
          outcome: "success",
          branch: "main",
          duration_s: 95,
          started_at: NOW_SECONDS - 1800,
        }),
        makeWorkflowRun(11, { outcome: "failure", branch: "dev", duration_s: null, title: null }),
        makeWorkflowRun(10, { outcome: "in_progress" }),
        makeWorkflowRun(9, { outcome: "timed_out" }),
        makeWorkflowRun(8, { outcome: "cancelled" }),
        makeWorkflowRun(7, { outcome: "queued" }),
        makeWorkflowRun(6, { outcome: "skipped" }),
        makeWorkflowRun(5, { outcome: "startup_failure" }),
      ],
    });
    renderQuality(github);
    const first = (await screen.findByText("#12 Fix parser")).closest("tr") as HTMLElement;
    expect(screen.getByText("Select a run for its jobs and steps.")).toBeTruthy();
    const firstRow = within(first);
    expect(firstRow.getByText("CI")).toBeTruthy();
    expect(toneOf(firstRow.getByText("success"))).toBe("good");
    expect(firstRow.getByText("main")).toBeTruthy();
    expect(firstRow.getByText("1 min 35 s")).toBeTruthy();
    expect(firstRow.getByText(timeText("30 min ago"))).toBeTruthy();
    const ci = within(
      screen.getByRole("heading", { name: "CI" }).closest("section") as HTMLElement,
    );
    expect(toneOf(ci.getByText("failure"))).toBe("bad");
    expect(toneOf(ci.getByText("in progress"))).toBe("info");
    expect(toneOf(ci.getByText("timed out"))).toBe("bad");
    expect(toneOf(ci.getByText("cancelled"))).toBe("neutral");
    expect(toneOf(ci.getByText("queued"))).toBe("info");
    expect(toneOf(ci.getByText("skipped"))).toBe("neutral");
    expect(toneOf(ci.getByText("startup failure"))).toBe("neutral");
    const noDuration = (ci.getByText("dev").closest("tr") as HTMLElement).querySelectorAll("td")[3];
    expect(noDuration?.textContent).toBe("—");
  });

  test("filters runs by branch and says when nothing ran on it", async () => {
    const api = mockQuality();
    const { user } = renderQuality(github);
    await screen.findByText("No workflow runs");
    await user.type(screen.getByPlaceholderText("all branches"), "dev");
    expect(await screen.findByText("Nothing ran on dev.")).toBeTruthy();
    expect(api.sent(RUNS).at(-1)).toEqual({ project_id: "alpha", branch: "dev", limit: 30 });
    expect(screen.getByPlaceholderText("all branches")).toBeTruthy();
  });

  test("reports runs that cannot be read with a specific title", async () => {
    mockQuality().action(RUNS, () => {
      throw new MockApiError(502, "GitHub is down");
    });
    renderQuality(github);
    expect(await screen.findByText("GitHub runs could not be read")).toBeTruthy();
    expect(screen.getByText("GitHub is down")).toBeTruthy();
  });

  test("shows a selected run's workflow link, jobs, log links and steps", async () => {
    const api = mockQuality({
      runs: [makeWorkflowRun(12, { workflow: "CI", number: 12, title: "Fix parser" })],
      details: {
        12: {
          run: makeWorkflowRun(12, {
            workflow: "CI",
            number: 12,
            html_url: "https://github.com/acme/alpha/actions/runs/12",
          }),
          jobs: [
            makeWorkflowJob(1, {
              name: "test",
              outcome: "success",
              logs_url: "https://github.com/acme/alpha/runs/1",
              steps: [
                { number: 1, name: "Checkout", outcome: "success" },
                { number: 2, name: "Run tests", outcome: "skipped" },
              ],
            }),
            makeWorkflowJob(2, { name: "lint", outcome: "success", logs_url: null }),
          ],
        },
      },
    });
    const { user } = renderQuality(github);
    await user.click((await screen.findByText("#12 Fix parser")).closest("tr") as HTMLElement);
    const link = await screen.findByRole("link", { name: /CI #12/ });
    expect(link.getAttribute("href")).toBe("https://github.com/acme/alpha/actions/runs/12");
    expect(screen.getByText("test")).toBeTruthy();
    expect(screen.getByText("lint")).toBeTruthy();
    expect(screen.getByRole("link", { name: "logs" }).getAttribute("href")).toBe(
      "https://github.com/acme/alpha/runs/1",
    );
    expect(screen.getAllByRole("link", { name: "logs" })).toHaveLength(1);
    expect(screen.getByText("1. Checkout")).toBeTruthy();
    expect(screen.getByText("2. Run tests")).toBeTruthy();
    expect(screen.queryByText("Select a run for its jobs and steps.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Re-run failed jobs" })).toBeNull();
    expect(api.sent(RUN)).toEqual([{ project_id: "alpha", run_id: 12 }]);
  });

  test("offers to re-run failed jobs only for a failing run and sends its run id after confirmation", async () => {
    const failing = makeWorkflowRun(12, { outcome: "failure", title: "Broken" });
    const api = mockQuality({
      runs: [failing, makeWorkflowRun(13, { outcome: "success", title: "Fine" })],
      details: {
        12: { run: failing, jobs: [makeWorkflowJob(1, { outcome: "failure" })] },
        13: {
          run: makeWorkflowRun(13, { outcome: "success" }),
          jobs: [makeWorkflowJob(2, { outcome: "success" })],
        },
      },
    }).action(RERUN, { ok: true, message: "re-running" });
    const { user } = renderQuality(github);
    await user.click((await screen.findByText("#13 Fine")).closest("tr") as HTMLElement);
    await screen.findByRole("link", { name: /CI #13/ });
    expect(screen.queryByRole("button", { name: "Re-run failed jobs" })).toBeNull();
    await user.click(screen.getByText("#12 Broken").closest("tr") as HTMLElement);
    await user.click(await screen.findByRole("button", { name: "Re-run failed jobs" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(RERUN)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Re-run failed jobs" }));
    await waitFor(() => expect(api.sent(RERUN)).toEqual([{ project_id: "alpha", run_id: 12 }]));
    expect(await screen.findByText("re-running")).toBeTruthy();
  });

  test("reports a run detail that cannot be read", async () => {
    mockQuality({ runs: [makeWorkflowRun(12, { title: "Fix parser" })] });
    const { user } = renderQuality(github);
    await user.click((await screen.findByText("#12 Fix parser")).closest("tr") as HTMLElement);
    expect(await screen.findByText("no run 12")).toBeTruthy();
  });
});
