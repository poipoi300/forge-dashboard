import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { StandardsTab } from "@/features/project/tabs/StandardsTab";
import { NOW_SECONDS } from "./constants";
import { makeProject, type ProjectSummary } from "./factories";
import {
  installHarness,
  type MockApi,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  timeText,
} from "./harness";
import {
  makeBranchRun,
  makeComplianceCheck,
  makeComplianceReport,
  makeDetail,
  makeProjectStandards,
  makeStandardsState,
} from "./project-tabs-support";

installHarness();

const EVALUATE = "/api/services/standards/actions/evaluate" as const;
const BRING_UP = "/api/services/standards/actions/bring-up" as const;
const ADOPT = "/api/services/standards/actions/adopt" as const;
const BRANCH_RUNS = "/api/services/agents/actions/branch-runs" as const;

function mockStandards(runs: ReturnType<typeof makeBranchRun>[] = []): MockApi {
  return mockApi().action(BRANCH_RUNS, { runs });
}

function renderStandards(subject: ProjectSummary = makeProject("alpha")) {
  return renderWithProviders(<StandardsTab project={subject} detail={makeDetail(subject)} />);
}

function sectionOf(title: string): ReturnType<typeof within> {
  const section = screen.getByRole("heading", { name: title }).closest("section");
  if (!section) throw new Error(`no section titled ${title}`);
  return within(section);
}

function checkButton(title: string): HTMLButtonElement {
  return screen.getByRole("button", { name: new RegExp(title) }) as HTMLButtonElement;
}

function toneOf(element: HTMLElement): string | null | undefined {
  return element.closest("[data-tone]")?.getAttribute("data-tone");
}

describe("StandardsTab before any evaluation", () => {
  test("says forge was not found and nothing has been evaluated", async () => {
    mockStandards();
    renderStandards();
    expect(await screen.findByText("Not evaluated yet")).toBeTruthy();
    expect(screen.getByText("forge repo not found")).toBeTruthy();
    expect(screen.getByText("never aligned")).toBeTruthy();
    expect(screen.getByText("Bring-up records the forge revision it aligned to.")).toBeTruthy();
    expect(screen.queryByRole("meter")).toBeNull();
  });

  test("names forge's head commit once the standards service reports it", async () => {
    mockStandards();
    seedStream({ standards: makeStandardsState([]) });
    renderStandards();
    expect(await screen.findByText("forge at 01234567: Tighten the gates")).toBeTruthy();
    expect(screen.getByText("Not evaluated yet")).toBeTruthy();
  });

  test("names forge's head without a subject when none is known", async () => {
    mockStandards();
    seedStream({ standards: makeStandardsState([], { forge_head_subject: null }) });
    renderStandards();
    expect(await screen.findByText("forge at 01234567:")).toBeTruthy();
  });

  test("ignores another project's evaluation", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("beta", {
          report: makeComplianceReport([makeComplianceCheck("readme", { title: "Has a README" })]),
        }),
      ]),
    });
    renderStandards();
    expect(await screen.findByText("Not evaluated yet")).toBeTruthy();
    expect(screen.queryByText("Has a README")).toBeNull();
  });

  test("shows an evaluation error without a report", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", { error: "forge repo is dirty" }),
      ]),
    });
    renderStandards();
    const error = await screen.findByText("forge repo is dirty");
    expect(toneOf(error)).toBe("bad");
    expect(screen.getByText("Not evaluated yet")).toBeTruthy();
  });
});

describe("StandardsTab compliance report", () => {
  const checks = [
    makeComplianceCheck("readme", { title: "Has a README", result: "pass", fix: "deterministic" }),
    makeComplianceCheck("ci", {
      title: "Runs CI",
      result: "fail",
      fix: "agent",
      detail: "No workflow file found.",
    }),
    makeComplianceCheck("docker", { title: "Has a Dockerfile", result: "n/a" }),
    makeComplianceCheck("agents", {
      title: "AGENTS.md matches",
      result: "fail",
      fix: "deterministic",
      diff: "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n-old\n+new",
    }),
    makeComplianceCheck("hooks", {
      title: "Hooks installed",
      result: "fail",
      detail: "pre-commit not installed",
      diff: "+ install hooks",
    }),
  ];

  function seedReport(overrides = {}) {
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          evaluated_at: NOW_SECONDS - 900,
          report: makeComplianceReport(checks, overrides),
        }),
      ]),
    });
  }

  test("shows the passing tally, a matching meter and when it was checked", async () => {
    mockStandards();
    seedReport();
    renderStandards();
    expect(await screen.findByText("1")).toBeTruthy();
    expect(screen.getByText("/5")).toBeTruthy();
    expect(
      screen.getByRole("meter", { name: "Checks passing" }).getAttribute("aria-valuenow"),
    ).toBe("20");
    expect(screen.getByText(timeText("15 min ago")).parentElement?.textContent).toBe(
      "checked 15 min ago",
    );
  });

  test("fills the meter completely when every check passes", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([makeComplianceCheck("a"), makeComplianceCheck("b")]),
        }),
      ]),
    });
    renderStandards();
    const meter = await screen.findByRole("meter", { name: "Checks passing" });
    expect(meter.getAttribute("aria-valuenow")).toBe("100");
  });

  test("leaves the meter empty for a report with no checks", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", { report: makeComplianceReport([]) }),
      ]),
    });
    renderStandards();
    const meter = await screen.findByRole("meter", { name: "Checks passing" });
    expect(meter.getAttribute("aria-valuenow")).toBe("0");
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  test("marks each check's result with its tone and how bring-up fixes it", async () => {
    mockStandards();
    seedReport();
    renderStandards();
    const readme = checkButton("Has a README");
    expect(toneOf(within(readme).getByText("pass"))).toBe("good");
    expect(within(readme).queryByText(/fix$/)).toBeNull();
    const ci = checkButton("Runs CI");
    expect(toneOf(within(ci).getByText("fail"))).toBe("bad");
    expect(within(ci).getByText("agent fix").getAttribute("title")).toBe("How bring-up fixes it");
    expect(toneOf(within(checkButton("Has a Dockerfile")).getByText("n/a"))).toBe("neutral");
  });

  test("lists failing checks first and keeps the others in report order", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([
            makeComplianceCheck("a", { result: "pass", title: "Alpha ok" }),
            makeComplianceCheck("b", { result: "fail", title: "Bravo broken" }),
            makeComplianceCheck("c", { result: "n/a", title: "Charlie skipped" }),
            makeComplianceCheck("d", { result: "fail", title: "Delta broken" }),
          ]),
        }),
      ]),
    });
    renderStandards();
    await screen.findByText("Bravo broken");
    const titles = screen
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "")
      .map((text) => text.replace(/(pass|fail|n\/a|agent fix|auto fix)/g, "").trim());
    expect(titles).toEqual(["Bravo broken", "Delta broken", "Alpha ok", "Charlie skipped"]);
  });

  test("lets only checks with a detail or a diff be expanded", async () => {
    mockStandards();
    seedReport();
    renderStandards();
    await screen.findByText("Has a README");
    expect(checkButton("Has a README").disabled).toBe(true);
    expect(checkButton("Has a Dockerfile").disabled).toBe(true);
    expect(checkButton("Runs CI").disabled).toBe(false);
    expect(checkButton("AGENTS.md matches").disabled).toBe(false);
  });

  test("expands a check to its detail, its diff, or both, and collapses it again", async () => {
    mockStandards();
    seedReport();
    const { user } = renderStandards();
    const ci = await screen.findByRole("button", { name: /Runs CI/ });
    expect(ci.getAttribute("aria-expanded")).toBe("false");
    await user.click(ci);
    expect(ci.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("No workflow file found.")).toBeTruthy();
    await user.click(ci);
    expect(ci.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("No workflow file found.")).toBeNull();
    await user.click(checkButton("AGENTS.md matches"));
    const diff = screen.getByText(/\+\+\+ b\/AGENTS\.md/);
    expect(diff.tagName).toBe("PRE");
    await user.click(checkButton("Hooks installed"));
    expect(screen.getByText("pre-commit not installed")).toBeTruthy();
    expect(screen.getByText("+ install hooks").tagName).toBe("PRE");
    expect(screen.queryByText(/\+\+\+ b\/AGENTS\.md/)).toBeNull();
  });
});

describe("StandardsTab actions", () => {
  test("re-checks the project's standards on demand", async () => {
    const api = mockStandards().action(EVALUATE, makeProjectStandards("alpha"));
    const { user } = renderStandards();
    await user.click(await screen.findByRole("button", { name: "Re-check" }));
    await waitFor(() => expect(api.sent(EVALUATE)).toEqual([{ project_id: "alpha" }]));
    expect(await screen.findByText("Evaluate standards: done")).toBeTruthy();
  });

  test("brings a managed project up to standards only after confirmation", async () => {
    const api = mockStandards().action(BRING_UP, {
      job_id: "job-1",
      kind: "standards",
      title: "Bring up alpha",
    });
    const { user } = renderStandards(makeProject("alpha", { role: "managed" }));
    expect(screen.queryByRole("button", { name: "Adopt into forge" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Bring up to standards" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(BRING_UP)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Bring up to standards" }));
    await waitFor(() => expect(api.sent(BRING_UP)).toEqual([{ project_id: "alpha" }]));
    expect(await screen.findByText("Started: Bring up alpha")).toBeTruthy();
  });

  test("offers bring-up for the standards source itself", async () => {
    mockStandards();
    renderStandards(makeProject("forge", { role: "standards-source" }));
    expect(await screen.findByRole("button", { name: "Bring up to standards" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Adopt into forge" })).toBeNull();
  });

  test("adopts an unmanifested project into forge instead of bringing it up", async () => {
    const api = mockStandards().action(ADOPT, {
      job_id: "job-2",
      kind: "adopt",
      title: "Adopt beta",
    });
    const { user } = renderStandards(makeProject("beta", { role: "unmanifested" }));
    expect(screen.queryByRole("button", { name: "Bring up to standards" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Adopt into forge" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Adopt" }));
    await waitFor(() => expect(api.sent(ADOPT)).toEqual([{ project_id: "beta" }]));
    expect(await screen.findByText("Started: Adopt beta")).toBeTruthy();
  });
});

describe("StandardsTab forge changes since alignment", () => {
  test("lists the forge commits made since the project was aligned", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([], { template_revision: "89abcdef0123456789" }),
          forge_commits_since: [
            { short_sha: "1111111", subject: "Add a gate", committed_at: NOW_SECONDS - 7200 },
            {
              short_sha: "2222222",
              subject: "Tighten lint",
              committed_at: NOW_SECONDS - 3 * 86400,
            },
          ],
        }),
      ]),
    });
    renderStandards();
    const changes = sectionOf("forge changes since alignment");
    expect(await changes.findByText("Add a gate")).toBeTruthy();
    expect(changes.getByText("aligned to 89abcdef")).toBeTruthy();
    expect(changes.getByText("1111111")).toBeTruthy();
    expect(changes.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(changes.getByText("Tighten lint")).toBeTruthy();
    expect(changes.getByText(timeText("3 d ago"))).toBeTruthy();
    expect(changes.queryByText("Aligned with forge's HEAD.")).toBeNull();
  });

  test("says the project is aligned with forge's head when no commits are pending", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([], { template_revision: "89abcdef0123456789" }),
        }),
      ]),
    });
    renderStandards();
    expect(await screen.findByText("Aligned with forge's HEAD.")).toBeTruthy();
    expect(screen.getByText("aligned to 89abcdef")).toBeTruthy();
  });

  test("explains that bring-up records the revision for a project never aligned", async () => {
    mockStandards();
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("alpha", { report: makeComplianceReport([]) }),
      ]),
    });
    renderStandards();
    expect(await screen.findByText("never aligned")).toBeTruthy();
    expect(screen.getByText("Bring-up records the forge revision it aligned to.")).toBeTruthy();
  });
});

describe("StandardsTab bring-up runs", () => {
  test("explains that no bring-up has run when the project has no standards or adopt runs", async () => {
    const api = mockStandards([
      makeBranchRun(1, "alpha", { kind: "delegate", title: "Fix issue 3" }),
    ]);
    renderStandards();
    expect(
      await screen.findByText(
        "No bring-up has run. It works on a branch in a worktree and waits for you to merge.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Fix issue 3")).toBeNull();
    expect(api.sent(BRANCH_RUNS)).toEqual([{ project_id: "alpha", kind: null }]);
  });

  test("shows only standards and adopt runs", async () => {
    mockStandards([
      makeBranchRun(1, "alpha", { kind: "standards", title: "Bring up alpha" }),
      makeBranchRun(2, "alpha", { kind: "adopt", title: "Adopt alpha" }),
      makeBranchRun(3, "alpha", { kind: "delegate", title: "Fix issue 3" }),
      makeBranchRun(4, "alpha", { kind: "update-deps", title: "Update dependencies" }),
    ]);
    renderStandards();
    const runs = sectionOf("Bring-up runs");
    expect(await runs.findByText("Bring up alpha")).toBeTruthy();
    expect(runs.getByText("Adopt alpha")).toBeTruthy();
    expect(runs.queryByText("Fix issue 3")).toBeNull();
    expect(runs.queryByText("Update dependencies")).toBeNull();
  });

  test("reports runs that cannot be read", async () => {
    mockStandards().action(BRANCH_RUNS, () => {
      throw new MockApiError(500, "runs unavailable");
    });
    renderStandards();
    expect(await sectionOf("Bring-up runs").findByText("runs unavailable")).toBeTruthy();
  });
});
