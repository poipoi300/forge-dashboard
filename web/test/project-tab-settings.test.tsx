import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import type { ServiceStates } from "@/api/stream";
import { makeProject } from "./factories";
import services from "./fixtures/services.json";
import { formField, installHarness, MockApiError, mockApi, type Rendered } from "./harness";
import {
  deferred,
  makeForkStatus,
  makeManifest,
  makeMetricDefinition,
  openProjectTab,
} from "./project-core-support";

installHarness();

const SAVE_SETTINGS = "/api/services/projects/actions/save-settings" as const;
const SET_CHECK_SETTINGS = "/api/services/checks/actions/set-settings" as const;
const SET_INTERVALS = "/api/services/metrics/actions/set-intervals" as const;
const SET_POLICY = "/api/services/forks/actions/set-policy" as const;

const CHECK_SETTINGS: Schemas["CheckSettings"] = {
  schedule: { kind: "interval", every_s: 600 },
  on_head: true,
};

function checksState(projectId = "alpha"): ServiceStates["checks"] {
  return {
    projects: [
      {
        project_id: projectId,
        checks: [],
        gates: [],
        gates_error: null,
        coverage: null,
        settings: CHECK_SETTINGS,
      },
    ],
  };
}

function metricsState(overrides: Record<string, number>): ServiceStates["metrics"] {
  return { projects: [{ project_id: "alpha", metrics: [], interval_overrides: overrides }] };
}

function row(title: string): HTMLElement {
  const container = screen.getByRole("heading", { name: title }).parentElement?.parentElement;
  if (!container) throw new Error(`no settings row titled ${title}`);
  return container;
}

function rowTitles(): string[] {
  return screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent ?? "");
}

async function editRow(user: Rendered["user"], title: string): Promise<HTMLElement> {
  await user.click(within(row(title)).getByRole("button", { name: "Edit" }));
  return screen.findByRole("dialog");
}

const SETTINGS_ECHO = (input: { settings: Schemas["ProjectSettings"] }) => input.settings;

describe("SettingsTab rows", () => {
  test("always offers the project's visibility and editor settings, and nothing else by default", async () => {
    await openProjectTab("settings");
    await screen.findByText("Visibility and editor");
    expect(rowTitles()).toEqual(["Visibility and editor"]);
  });

  test("shows the current visibility and editor command rendered from the action's schema", async () => {
    await openProjectTab("settings", {
      detail: { settings: { hidden: true, editor_command: ["code", "-n"] } },
    });
    await screen.findByText("Visibility and editor");
    await waitFor(() => expect(row("Visibility and editor").textContent).toContain("Hidden"));
    const text = row("Visibility and editor").textContent ?? "";
    expect(text).toContain("yes");
    expect(text).toContain("Editor command");
    expect(text).toContain("code");
    expect(text).toContain("-n");
  });

  test("adds the check schedule once the project has check state", async () => {
    await openProjectTab("settings", { streams: { checks: checksState() } });
    await screen.findByText("Check schedule");
    expect(rowTitles()).toEqual(["Visibility and editor", "Check schedule"]);
    await waitFor(() => expect(row("Check schedule").textContent).toContain("On head"));
  });

  test("leaves out the check schedule when the checks state covers other projects only", async () => {
    await openProjectTab("settings", { streams: { checks: checksState("beta") } });
    await screen.findByText("Visibility and editor");
    expect(rowTitles()).toEqual(["Visibility and editor"]);
  });

  test("adds metric intervals only for a manifest that declares metrics", async () => {
    const project = makeProject("alpha");
    await openProjectTab("settings", {
      project,
      detail: { manifest: makeManifest(project, { metrics: [makeMetricDefinition("loc")] }) },
      streams: { metrics: metricsState({ loc: 120 }) },
    });
    await screen.findByText("Metric intervals");
    await waitFor(() => expect(row("Metric intervals").textContent).toContain("loc"));
    expect(row("Metric intervals").textContent).toContain("120");
  });

  test("omits metric intervals for a manifest without metrics", async () => {
    const project = makeProject("alpha");
    await openProjectTab("settings", {
      project,
      detail: { manifest: makeManifest(project) },
      streams: { metrics: metricsState({}) },
    });
    await screen.findByText("Visibility and editor");
    expect(rowTitles()).toEqual(["Visibility and editor"]);
  });

  test("adds the fork policy for a git repository the fork service tracks", async () => {
    await openProjectTab("settings", {
      detail: { is_git: true },
      streams: { forks: { forks: [makeForkStatus("alpha")] } },
    });
    await screen.findByText("Fork policy");
    expect(rowTitles()).toEqual(["Visibility and editor", "Fork policy"]);
  });

  test("omits the fork policy outside a git repository", async () => {
    await openProjectTab("settings", {
      detail: { is_git: false },
      streams: { forks: { forks: [makeForkStatus("alpha")] } },
    });
    await screen.findByText("Visibility and editor");
    expect(rowTitles()).toEqual(["Visibility and editor"]);
  });

  test("omits the fork policy when the fork service tracks another project", async () => {
    await openProjectTab("settings", {
      detail: { is_git: true },
      streams: { forks: { forks: [makeForkStatus("beta")] } },
    });
    await screen.findByText("Visibility and editor");
    expect(rowTitles()).toEqual(["Visibility and editor"]);
  });

  test("keeps Edit disabled and shows no value until the service descriptors arrive", async () => {
    const api = mockApi();
    const descriptors = deferred<typeof services>();
    api.route("GET", "/api/services", () => descriptors.promise);
    await openProjectTab("settings", { detail: { settings: { hidden: true } } }, api);
    await screen.findByText("Visibility and editor");
    const edit = within(row("Visibility and editor")).getByRole("button", {
      name: "Edit",
    }) as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    expect(row("Visibility and editor").textContent).not.toContain("Hidden");
    descriptors.resolve(services);
    await waitFor(() => expect(edit.disabled).toBe(false));
    expect(row("Visibility and editor").textContent).toContain("Hidden");
  });
});

describe("SettingsTab editing", () => {
  test("edits visibility starting from the current value and sends the whole object", async () => {
    const api = mockApi().action(SAVE_SETTINGS, SETTINGS_ECHO);
    const { rendered } = await openProjectTab(
      "settings",
      { detail: { settings: { hidden: true, editor_command: ["code", "-n"] } } },
      api,
    );
    await screen.findByText("Visibility and editor");
    const dialog = await editRow(rendered.user, "Visibility and editor");
    const hidden = formField("hidden", dialog).getByRole("switch");
    expect(hidden.getAttribute("aria-checked")).toBe("true");
    await rendered.user.click(hidden);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Remove -n" }));
    await rendered.user.click(
      within(dialog).getByRole("button", { name: "Save project settings" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(SAVE_SETTINGS)).toEqual([
      { project_id: "alpha", settings: { hidden: false, editor_command: ["code"] } },
    ]);
  });

  test("cancelling an edit sends nothing", async () => {
    const api = mockApi();
    const { rendered } = await openProjectTab("settings", {}, api);
    await screen.findByText("Visibility and editor");
    const dialog = await editRow(rendered.user, "Visibility and editor");
    await rendered.user.click(formField("hidden", dialog).getByRole("switch"));
    await rendered.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", SAVE_SETTINGS)).toEqual([]);
  });

  test("keeps the dialog open and shows the reason when saving fails", async () => {
    const api = mockApi().action(SAVE_SETTINGS, () => {
      throw new MockApiError(422, "editor command must not be empty");
    });
    const { rendered } = await openProjectTab("settings", {}, api);
    await screen.findByText("Visibility and editor");
    const dialog = await editRow(rendered.user, "Visibility and editor");
    await rendered.user.click(
      within(dialog).getByRole("button", { name: "Save project settings" }),
    );
    expect((await within(dialog).findByRole("alert")).textContent).toBe(
      "editor command must not be empty",
    );
  });

  test("edits the check schedule and sends it with the project", async () => {
    const api = mockApi().action(SET_CHECK_SETTINGS, (input) => input.settings);
    const { rendered } = await openProjectTab(
      "settings",
      { streams: { checks: checksState() } },
      api,
    );
    await screen.findByText("Check schedule");
    const dialog = await editRow(rendered.user, "Check schedule");
    const onHead = formField("on_head", dialog).getByRole("switch");
    expect(onHead.getAttribute("aria-checked")).toBe("true");
    await rendered.user.click(onHead);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Check schedule" }));
    await waitFor(() => expect(api.sent(SET_CHECK_SETTINGS)).toHaveLength(1));
    expect(api.sent(SET_CHECK_SETTINGS)).toEqual([
      {
        project_id: "alpha",
        settings: { schedule: { kind: "interval", every_s: 600 }, on_head: false },
      },
    ]);
  });

  test("starts the metric interval editor from the current overrides", async () => {
    const project = makeProject("alpha");
    const api = mockApi().action(SET_INTERVALS, (input) => ({
      project_id: input.project_id,
      overrides: input.overrides ?? [],
    }));
    const { rendered } = await openProjectTab(
      "settings",
      {
        project,
        detail: { manifest: makeManifest(project, { metrics: [makeMetricDefinition("loc")] }) },
        streams: { metrics: metricsState({ loc: 120, coverage: 3600 }) },
      },
      api,
    );
    await screen.findByText("Metric intervals");
    const dialog = await editRow(rendered.user, "Metric intervals");
    await rendered.user.click(
      within(dialog).getAllByRole("button", { name: "Remove row" })[1] as HTMLElement,
    );
    await rendered.user.click(within(dialog).getByRole("button", { name: "Set metric intervals" }));
    await waitFor(() => expect(api.sent(SET_INTERVALS)).toHaveLength(1));
    expect(api.sent(SET_INTERVALS)).toEqual([
      { project_id: "alpha", overrides: [{ metric_id: "loc", interval_s: 120 }] },
    ]);
  });

  test("starts the metric interval editor empty when no overrides are known yet", async () => {
    const project = makeProject("alpha");
    const api = mockApi().action(SET_INTERVALS, (input) => ({
      project_id: input.project_id,
      overrides: input.overrides ?? [],
    }));
    const { rendered } = await openProjectTab(
      "settings",
      {
        project,
        detail: { manifest: makeManifest(project, { metrics: [makeMetricDefinition("loc")] }) },
      },
      api,
    );
    await screen.findByText("Metric intervals");
    const dialog = await editRow(rendered.user, "Metric intervals");
    expect(within(dialog).queryByRole("button", { name: "Remove row" })).toBeNull();
    await rendered.user.click(within(dialog).getByRole("button", { name: "Set metric intervals" }));
    await waitFor(() => expect(api.sent(SET_INTERVALS)).toHaveLength(1));
    expect(api.sent(SET_INTERVALS)).toEqual([{ project_id: "alpha", overrides: [] }]);
  });

  test("edits the fork policy from its current value", async () => {
    const status = makeForkStatus("alpha");
    const api = mockApi().action(SET_POLICY, () => status);
    const { rendered } = await openProjectTab(
      "settings",
      { detail: { is_git: true }, streams: { forks: { forks: [status] } } },
      api,
    );
    await screen.findByText("Fork policy");
    const dialog = await editRow(rendered.user, "Fork policy");
    const autoPush = formField("auto_push", dialog).getByRole("switch");
    expect(autoPush.getAttribute("aria-checked")).toBe("false");
    await rendered.user.click(autoPush);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Fork policy" }));
    await waitFor(() => expect(api.sent(SET_POLICY)).toHaveLength(1));
    expect(api.sent(SET_POLICY)).toEqual([
      { project_id: "alpha", policy: { ...status.policy, auto_push: true } },
    ]);
  });
});
