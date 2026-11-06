import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { StandardsPage } from "@/features/fleet/StandardsPage";
import { makeProject, makeProjectsState } from "./factories";
import {
  makeComplianceCheck,
  makeComplianceReport,
  makeProjectStandards,
  makeStandardsState,
} from "./fleet-support";
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

const ADOPT = "/api/services/standards/actions/adopt" as const;
const BRING_UP = "/api/services/standards/actions/bring-up" as const;

function standardsRow(name: string): HTMLElement {
  const row = within(screen.getByRole("table")).getByText(name).closest("tr");
  if (!row) throw new Error(`no table row for ${name}`);
  return row;
}

function rowNames(): string[] {
  return within(screen.getByRole("table"))
    .getAllByRole("link")
    .map((link) => link.textContent ?? "");
}

describe("StandardsPage header", () => {
  test("names the forge commit the fleet is measured against", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({ standards: makeStandardsState([]) });
    expect(
      await screen.findByText("Measured against forge at 01234567: Tighten the gates"),
    ).toBeTruthy();
  });

  test("measures against an unknown head without inventing one", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      standards: makeStandardsState([], { forge_head: null, forge_head_subject: null }),
    });
    expect(await screen.findByText("Measured against forge at ?:")).toBeTruthy();
  });

  test("points at Settings when the forge repo was not found", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({ standards: makeStandardsState([], { forge_repo: null }) });
    expect(
      await screen.findByText("The forge repo was not found; set it in Settings, under Discovery."),
    ).toBeTruthy();
  });

  test("shows the not-found note and the empty state before the standards state arrives", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    expect(screen.getByText("Nothing measured yet")).toBeTruthy();
    expect(screen.getByText(/The forge repo was not found/)).toBeTruthy();
  });
});

describe("StandardsPage rows", () => {
  test("says nothing is measured when no project matches a standards entry", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([
        makeProject("forge", { role: "standards-source" }),
        makeProject("alpha"),
      ]),
      standards: makeStandardsState([
        makeProjectStandards("forge", { report: makeComplianceReport([]) }),
        makeProjectStandards("unregistered"),
      ]),
    });
    expect(await screen.findByText("Nothing measured yet")).toBeTruthy();
    expect(screen.getByText(/Compliance is computed for every project/)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  test("shows passing over total, a meter, and the titles of failing checks", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha", { name: "Alpha" })]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([
            makeComplianceCheck("agents-md", "pass", "AGENTS.md present"),
            makeComplianceCheck("ruff", "fail", "ruff configured"),
            makeComplianceCheck("mypy", "fail", "mypy strict"),
            makeComplianceCheck("docs", "n/a", "docs folder"),
          ]),
          behind: 4,
          evaluated_at: NOW_SECONDS - 7200,
        }),
      ]),
    });
    await screen.findByRole("table");
    const row = within(standardsRow("Alpha"));
    expect(row.getByText("1/3")).toBeTruthy();
    const meter = row.getByRole("meter", { name: "Alpha compliance" });
    expect(meter.getAttribute("aria-valuenow")).toBe("33");
    expect(row.getByText("ruff configured")).toBeTruthy();
    expect(row.getByText("mypy strict")).toBeTruthy();
    expect(row.queryByText("docs folder")).toBeNull();
    expect(row.getByText("4")).toBeTruthy();
    expect(row.getByText(timeText("2 h ago"))).toBeTruthy();
    await user.click(row.getByRole("link", { name: "Alpha" }));
    expect(location()).toBe("/p/alpha/standards");
  });

  test("lists the first three failing checks and counts the rest", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport(
            ["one", "two", "three", "four", "five"].map((title) =>
              makeComplianceCheck(title, "fail", `check ${title}`),
            ),
          ),
        }),
      ]),
    });
    await screen.findByRole("table");
    const row = within(standardsRow("alpha"));
    expect(row.getByText("check one")).toBeTruthy();
    expect(row.getByText("check three")).toBeTruthy();
    expect(row.queryByText("check four")).toBeNull();
    expect(row.getByText("and 2 more")).toBeTruthy();
  });

  test("shows none for failing checks of a fully compliant project that is current", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([makeComplianceCheck("ruff", "pass")]),
          behind: null,
          evaluated_at: null,
        }),
      ]),
    });
    await screen.findByRole("table");
    const row = within(standardsRow("alpha"));
    expect(row.getByText("1/1")).toBeTruthy();
    expect(row.getByRole("meter").getAttribute("aria-valuenow")).toBe("100");
    expect(row.getByText("none")).toBeTruthy();
    expect(row.getByText("—")).toBeTruthy();
    expect(row.getByText("never")).toBeTruthy();
    expect(row.queryByRole("button")).toBeNull();
  });

  test("handles a report with no applicable checks as an empty meter", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", { report: makeComplianceReport([]) }),
      ]),
    });
    await screen.findByRole("table");
    const row = within(standardsRow("alpha"));
    expect(row.getByText("0/0")).toBeTruthy();
    expect(row.getByRole("meter").getAttribute("aria-valuenow")).toBe("0");
  });

  test("shows the evaluation error, or not checked, when there is no report", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha"), makeProject("beta")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", { error: "worktree locked" }),
        makeProjectStandards("beta"),
      ]),
    });
    await screen.findByRole("table");
    expect(within(standardsRow("alpha")).getByText("worktree locked")).toBeTruthy();
    expect(within(standardsRow("beta")).getByText("not checked")).toBeTruthy();
  });

  test("lists the least compliant projects first and unchecked ones before them", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    const report = (passing: number, failing: number) =>
      makeComplianceReport([
        ...Array.from({ length: passing }, (_, index) =>
          makeComplianceCheck(`pass-${index}`, "pass"),
        ),
        ...Array.from({ length: failing }, (_, index) =>
          makeComplianceCheck(`fail-${index}`, "fail"),
        ),
      ]);
    seedStream({
      projects: makeProjectsState(
        ["full", "half", "mostly", "unchecked"].map((id) => makeProject(id)),
      ),
      standards: makeStandardsState([
        makeProjectStandards("full", { report: report(4, 0) }),
        makeProjectStandards("mostly", { report: report(3, 1) }),
        makeProjectStandards("unchecked"),
        makeProjectStandards("half", { report: report(2, 2) }),
      ]),
    });
    await screen.findByRole("table");
    expect(rowNames()).toEqual(["unchecked", "half", "mostly", "full"]);
  });

  test("marks a project without a manifest and distinguishes shared names by id", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([
        makeProject("tool-a", { name: "tool", role: "unmanifested" }),
        makeProject("tool-b", { name: "tool" }),
      ]),
      standards: makeStandardsState([
        makeProjectStandards("tool-a"),
        makeProjectStandards("tool-b"),
      ]),
    });
    await screen.findByRole("table");
    expect(within(standardsRow("tool (tool-a)")).getByText("no manifest")).toBeTruthy();
    expect(within(standardsRow("tool (tool-b)")).queryByText("no manifest")).toBeNull();
  });
});

describe("StandardsPage fixes", () => {
  test("adopts an unmanifested project after the dialog confirms it", async () => {
    const api = mockApi().action(ADOPT, {
      job_id: "job-adopt",
      kind: "standards.adopt",
      title: "Adopt alpha",
    });
    const { user, location } = renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([
        makeProject("alpha", { role: "unmanifested" }),
        makeProject("beta", { role: "unmanifested" }),
      ]),
      standards: makeStandardsState([makeProjectStandards("alpha"), makeProjectStandards("beta")]),
    });
    await screen.findByRole("table");
    await settle();
    await user.click(within(standardsRow("beta")).getByRole("button", { name: "Adopt" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/writes forge.project.toml/)).toBeTruthy();
    expect(api.sent(ADOPT)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Adopt" }));
    expect(await screen.findByText("Started: Adopt alpha")).toBeTruthy();
    expect(api.sent(ADOPT)).toEqual([{ project_id: "beta" }]);
    await user.click(screen.getByRole("button", { name: "View job" }));
    expect(location()).toBe("/jobs/job-adopt");
  });

  test("brings a failing project up to standards after confirmation", async () => {
    const api = mockApi().action(BRING_UP, {
      job_id: "job-up",
      kind: "standards.bring-up",
      title: "Bring up alpha",
    });
    const { user } = renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([makeComplianceCheck("ruff", "fail")]),
        }),
      ]),
    });
    await screen.findByRole("table");
    await settle();
    await user.click(within(standardsRow("alpha")).getByRole("button", { name: "Bring up" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Bring up to standards" }));
    expect(await screen.findByText("Started: Bring up alpha")).toBeTruthy();
    expect(api.sent(BRING_UP)).toEqual([{ project_id: "alpha" }]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("offers bring-up to a passing project that is behind forge", async () => {
    mockApi();
    renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha"), makeProject("beta")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([makeComplianceCheck("ruff", "pass")]),
          behind: 3,
        }),
        makeProjectStandards("beta", {
          report: makeComplianceReport([makeComplianceCheck("ruff", "pass")]),
          behind: 0,
        }),
      ]),
    });
    await screen.findByRole("table");
    expect(within(standardsRow("alpha")).getByRole("button", { name: "Bring up" })).toBeTruthy();
    expect(within(standardsRow("beta")).queryByRole("button")).toBeNull();
  });

  test("keeps the confirmation open with the server reason when a fix is refused", async () => {
    mockApi().action(BRING_UP, () => {
      throw new MockApiError(409, "a bring-up is already running for alpha");
    });
    const { user } = renderWithProviders(<StandardsPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha")]),
      standards: makeStandardsState([
        makeProjectStandards("alpha", {
          report: makeComplianceReport([makeComplianceCheck("ruff", "fail")]),
        }),
      ]),
    });
    await screen.findByRole("table");
    await settle();
    await user.click(within(standardsRow("alpha")).getByRole("button", { name: "Bring up" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Bring up to standards" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe(
      "a bring-up is already running for alpha",
    );
  });
});
