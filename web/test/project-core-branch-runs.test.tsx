import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { BranchRuns } from "@/features/project/BranchRuns";
import { formField, installHarness, MockApiError, mockApi, renderWithProviders } from "./harness";
import { type BranchRun, deferred, makeBranchRun } from "./project-core-support";

installHarness();

const BRANCH_RUNS = "/api/services/agents/actions/branch-runs" as const;
const BRANCH_DIFF = "/api/services/agents/actions/branch-diff" as const;
const MERGE = "/api/services/agents/actions/merge-branch" as const;
const OPEN_PR = "/api/services/agents/actions/open-pr" as const;
const DISCARD = "/api/services/agents/actions/discard-branch" as const;

const EMPTY_MESSAGE = "Delegate an issue first.";

const SOURCE_DIFF = [
  "diff --git a/src/app.py b/src/app.py",
  "index 1111111..2222222 100644",
  "--- a/src/app.py",
  "+++ b/src/app.py",
  "@@ -1,2 +1,2 @@",
  " import os",
  '-print("old")',
  '+print("new")',
  "",
].join("\n");

const MODE_ONLY_DIFF = [
  "diff --git a/run.sh b/run.sh",
  "old mode 100644",
  "new mode 100755",
  "",
].join("\n");

function renderRuns(runs: BranchRun[], kinds = ["delegate", "update-deps"]) {
  const api = mockApi().action(BRANCH_RUNS, { runs });
  const rendered = renderWithProviders(
    <BranchRuns projectId="alpha" kinds={kinds} empty={EMPTY_MESSAGE} />,
  );
  return { api, ...rendered };
}

function card(title: string): HTMLElement {
  const item = screen.getByText(title).closest("li");
  if (!item) throw new Error(`no run card titled ${title}`);
  return item;
}

describe("BranchRuns list", () => {
  test("asks for the project's branch runs of every kind", async () => {
    const { api } = renderRuns([makeBranchRun(1)]);
    await screen.findByText("Branch run 1");
    expect(api.sent(BRANCH_RUNS)).toEqual([{ project_id: "alpha", kind: null }]);
  });

  test("shows loading rows until the runs arrive", async () => {
    const gate = deferred<{ runs: BranchRun[] }>();
    mockApi().action(BRANCH_RUNS, () => gate.promise);
    renderWithProviders(
      <BranchRuns projectId="alpha" kinds={["delegate"]} empty={EMPTY_MESSAGE} />,
    );
    expect(await screen.findByRole("status", { name: "Loading" })).toBeTruthy();
    gate.resolve({ runs: [makeBranchRun(1)] });
    expect(await screen.findByText("Branch run 1")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
  });

  test("shows the server's reason when the runs cannot be loaded and retries on request", async () => {
    let failing = true;
    const api = mockApi().action(BRANCH_RUNS, () => {
      if (failing) throw new MockApiError(500, "agents service is down");
      return { runs: [makeBranchRun(1)] };
    });
    const { user } = renderWithProviders(
      <BranchRuns projectId="alpha" kinds={["delegate"]} empty={EMPTY_MESSAGE} />,
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("agents service is down");
    failing = false;
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Branch run 1")).toBeTruthy();
    expect(api.callsTo("POST", BRANCH_RUNS)).toHaveLength(2);
  });

  test("shows the empty message when the project has no runs", async () => {
    renderRuns([]);
    expect(await screen.findByText(EMPTY_MESSAGE)).toBeTruthy();
    expect(screen.queryByRole("list")).toBeNull();
  });

  test("shows only the runs of the requested kinds", async () => {
    renderRuns([
      makeBranchRun(1, { kind: "delegate", title: "Fix the flaky test" }),
      makeBranchRun(2, { kind: "standards", title: "Bring up to standards" }),
      makeBranchRun(3, { kind: "update-deps", title: "Bump dependencies" }),
    ]);
    expect(await screen.findByText("Fix the flaky test")).toBeTruthy();
    expect(screen.getByText("Bump dependencies")).toBeTruthy();
    expect(screen.queryByText("Bring up to standards")).toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  test("shows the empty message when only other kinds exist", async () => {
    renderRuns([makeBranchRun(1, { kind: "adopt", title: "Adopt alpha" })]);
    expect(await screen.findByText(EMPTY_MESSAGE)).toBeTruthy();
    expect(screen.queryByText("Adopt alpha")).toBeNull();
  });
});

describe("BranchRuns card content", () => {
  test("describes the branch, its base, the files changed and when it last moved", async () => {
    renderRuns([makeBranchRun(1, { files_changed: 3, kind: "update-deps" })]);
    await screen.findByText("Branch run 1");
    const item = card("Branch run 1");
    expect(item.textContent).toContain("update-deps · 1 h ago");
    expect(item.textContent).toContain("forge/run-1 from main@01234567 · 3 files");
    expect(item.textContent).not.toContain("agent ×");
  });

  test("counts the agent's attempts when it ran", async () => {
    renderRuns([makeBranchRun(1, { agent_attempts: 2 })]);
    expect((await screen.findByText(/forge\/run-1 from/)).textContent).toContain("· agent ×2");
  });

  test("compares standards before and after, listing what still fails", async () => {
    renderRuns([
      makeBranchRun(1, {
        compliance_before: { passing: 4, total: 10 },
        compliance_after: { passing: 8, total: 10 },
        still_failing: ["ruff", "mypy"],
      }),
    ]);
    const line = await screen.findByText(/^standards 4\/10/);
    expect(line.textContent).toBe("standards 4/10 → 8/10 · still failing: ruff, mypy");
  });

  test("omits the failing list when every standard passes", async () => {
    renderRuns([
      makeBranchRun(1, {
        compliance_before: { passing: 9, total: 10 },
        compliance_after: { passing: 10, total: 10 },
      }),
    ]);
    const line = await screen.findByText(/^standards 9\/10/);
    expect(line.textContent).toBe("standards 9/10 → 10/10");
  });

  test("skips the comparison when either score is missing", async () => {
    renderRuns([
      makeBranchRun(1, { compliance_before: { passing: 4, total: 10 }, compliance_after: null }),
    ]);
    await screen.findByText("Branch run 1");
    expect(screen.queryByText(/^standards/)).toBeNull();
  });

  test("shows the summary and the error of a run", async () => {
    renderRuns([
      makeBranchRun(1, { state: "failed", summary: "Updated 3 packages", error: "tests failed" }),
    ]);
    expect(await screen.findByText("Updated 3 packages")).toBeTruthy();
    expect(screen.getByText("tests failed")).toBeTruthy();
  });

  test("marks each check by its outcome", async () => {
    renderRuns([
      makeBranchRun(1, {
        checks: [
          { check_id: "lint", label: "Lint", outcome: "passed" },
          { check_id: "types", label: "Types", outcome: "failed" },
          { check_id: "e2e", label: "End to end", outcome: "never" },
        ],
      }),
    ]);
    const tone = async (label: string) =>
      (await screen.findByText(label)).closest("[data-tone]")?.getAttribute("data-tone");
    expect(await tone("Lint")).toBe("good");
    expect(await tone("Types")).toBe("bad");
    expect(await tone("End to end")).toBe("neutral");
  });

  test.each([
    ["running", "info"],
    ["ready", "info"],
    ["merged", "good"],
    ["pr-opened", "good"],
    ["discarded", "neutral"],
    ["failed", "bad"],
  ] as const)("shows a %s run with the %s tone", async (state, tone) => {
    renderRuns([makeBranchRun(1, { state })]);
    const mark = (await screen.findByText(state)).closest("[data-tone]");
    expect(mark?.getAttribute("data-tone")).toBe(tone);
  });

  test("links to the pull request once one is open", async () => {
    renderRuns([
      makeBranchRun(1, { state: "pr-opened", pr_url: "https://github.com/acme/alpha/pull/12" }),
    ]);
    const link = await screen.findByRole("link", { name: "Pull request" });
    expect(link.getAttribute("href")).toBe("https://github.com/acme/alpha/pull/12");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  test("offers no decisions on a run that is not ready", async () => {
    renderRuns([makeBranchRun(1, { state: "merged" })]);
    await screen.findByText("Branch run 1");
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open PR…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
    expect(screen.getByRole("button", { name: "Show diff" })).toBeTruthy();
  });
});

describe("BranchRuns decisions", () => {
  test("merging asks for confirmation and then sends the run id", async () => {
    const run = makeBranchRun(7);
    const { api, user } = renderRuns([run]);
    api.action(MERGE, { ...run, state: "merged" });
    await user.click(await screen.findByRole("button", { name: "Merge" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.callsTo("POST", MERGE)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Merge" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(MERGE)).toEqual([{ run_id: 7 }]);
  });

  test("a merge reports the action as done instead of announcing a job, even when the run carries a job id", async () => {
    const run = makeBranchRun(7, { job_id: "job-3" });
    const { api, user } = renderRuns([run]);
    api.action(MERGE, { ...run, state: "merged" });
    await user.click(await screen.findByRole("button", { name: "Merge" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Merge" }));
    expect(await screen.findByText("Merge: done")).toBeTruthy();
    expect(screen.queryByText(/^Started:/)).toBeNull();
    expect(screen.queryByRole("button", { name: "View job" })).toBeNull();
  });

  test("a failed merge keeps the dialog open and shows the reason", async () => {
    const { api, user } = renderRuns([makeBranchRun(7)]);
    api.action(MERGE, () => {
      throw new MockApiError(409, "branch no longer fast-forwards");
    });
    await user.click(await screen.findByRole("button", { name: "Merge" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Merge" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe(
      "branch no longer fast-forwards",
    );
  });

  test("opening a pull request sends the run id with the title and body typed in", async () => {
    const run = makeBranchRun(8);
    const { api, user } = renderRuns([run]);
    api.action(OPEN_PR, {
      ...run,
      state: "pr-opened",
      pr_url: "https://github.com/acme/alpha/pull/3",
    });
    await user.click(await screen.findByRole("button", { name: "Open PR…" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(formField("title", dialog).getByRole("textbox"), "Bump dependencies");
    await user.type(formField("body", dialog).getByRole("textbox"), "Updated by the agent");
    await user.click(within(dialog).getByRole("button", { name: "Open PR" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(OPEN_PR)).toEqual([
      { run_id: 8, title: "Bump dependencies", body: "Updated by the agent" },
    ]);
  });

  test("discarding needs the branch name typed before anything is sent", async () => {
    const run = makeBranchRun(9, { branch: "forge/delegate-9" });
    const { api, user } = renderRuns([run]);
    api.action(DISCARD, { ...run, state: "discarded" });
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("forge/delegate-9")).toBeTruthy();
    const confirm = within(dialog).getByRole("button", { name: "Discard" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "alpha");
    expect(confirm.disabled).toBe(true);
    await user.clear(within(dialog).getByRole("textbox"));
    await user.type(within(dialog).getByRole("textbox"), "forge/delegate-9");
    expect(confirm.disabled).toBe(false);
    expect(api.callsTo("POST", DISCARD)).toEqual([]);
    await user.click(confirm);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(DISCARD)).toEqual([{ run_id: 9 }]);
  });

  test("decisions apply to the card they sit on", async () => {
    const first = makeBranchRun(1, { title: "First run" });
    const second = makeBranchRun(2, { title: "Second run" });
    const { api, user } = renderRuns([first, second]);
    api.action(MERGE, { ...second, state: "merged" });
    await screen.findByText("Second run");
    await user.click(within(card("Second run")).getByRole("button", { name: "Merge" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Merge" }));
    await waitFor(() => expect(api.sent(MERGE)).toEqual([{ run_id: 2 }]));
  });
});

describe("BranchRuns diff", () => {
  test("loads the diff only when asked and shows it per file", async () => {
    const { api, user } = renderRuns([makeBranchRun(5)]);
    api.action(BRANCH_DIFF, { run: makeBranchRun(5), diff: SOURCE_DIFF });
    await screen.findByText("Branch run 5");
    expect(api.callsTo("POST", BRANCH_DIFF)).toEqual([]);
    const toggle = screen.getByRole("button", { name: "Show diff" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await user.click(toggle);
    expect(await screen.findByText("src/app.py")).toBeTruthy();
    expect(screen.getByText('print("new")')).toBeTruthy();
    expect(api.sent(BRANCH_DIFF)).toEqual([{ run_id: 5 }]);
    expect(screen.getByRole("button", { name: "Hide diff" }).getAttribute("aria-expanded")).toBe(
      "true",
    );
  });

  test("hides the diff again", async () => {
    const { api, user } = renderRuns([makeBranchRun(5)]);
    api.action(BRANCH_DIFF, { run: makeBranchRun(5), diff: SOURCE_DIFF });
    await user.click(await screen.findByRole("button", { name: "Show diff" }));
    await screen.findByText("src/app.py");
    await user.click(screen.getByRole("button", { name: "Hide diff" }));
    expect(screen.queryByText("src/app.py")).toBeNull();
    expect(screen.getByRole("button", { name: "Show diff" })).toBeTruthy();
  });

  test("says so when the branch has no changes", async () => {
    const { api, user } = renderRuns([makeBranchRun(5)]);
    api.action(BRANCH_DIFF, { run: makeBranchRun(5), diff: "" });
    await user.click(await screen.findByRole("button", { name: "Show diff" }));
    expect(await screen.findByText("The branch has no changes.")).toBeTruthy();
  });

  test("notes a change that carries no lines to show", async () => {
    const { api, user } = renderRuns([makeBranchRun(5)]);
    api.action(BRANCH_DIFF, { run: makeBranchRun(5), diff: MODE_ONLY_DIFF });
    await user.click(await screen.findByRole("button", { name: "Show diff" }));
    expect(await screen.findByText("Binary or mode-only change.")).toBeTruthy();
    expect(screen.getByText("run.sh")).toBeTruthy();
  });

  test("shows why the diff could not be loaded", async () => {
    const { api, user } = renderRuns([makeBranchRun(5)]);
    api.action(BRANCH_DIFF, () => {
      throw new MockApiError(404, "worktree is gone");
    });
    await user.click(await screen.findByRole("button", { name: "Show diff" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("worktree is gone");
  });

  test("each card toggles its own diff", async () => {
    const { api, user } = renderRuns([
      makeBranchRun(1, { title: "First run" }),
      makeBranchRun(2, { title: "Second run" }),
    ]);
    api.action(BRANCH_DIFF, { run: makeBranchRun(2), diff: SOURCE_DIFF });
    await screen.findByText("First run");
    await user.click(within(card("Second run")).getByRole("button", { name: "Show diff" }));
    await screen.findByText("src/app.py");
    expect(within(card("First run")).queryByText("src/app.py")).toBeNull();
    expect(api.sent(BRANCH_DIFF)).toEqual([{ run_id: 2 }]);
  });
});
