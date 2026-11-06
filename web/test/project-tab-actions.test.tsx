import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { formField, installHarness, MockApiError, mockApi } from "./harness";
import {
  type ActionInfo,
  deferred,
  type JobSummary,
  makeActionInfo,
  makeJobSummary,
  openProjectTab,
} from "./project-core-support";

installHarness();

const RUN_ACTION = "/api/services/actions/actions/run" as const;
const ARTIFACT_SIZES = "/api/services/actions/actions/artifact-sizes" as const;
const CLEAN_ARTIFACTS = "/api/services/actions/actions/clean-artifacts" as const;
const JOB_HISTORY = "/api/services/jobs/actions/history" as const;
const REFRESH_PROJECT = "/api/services/projects/actions/refresh-project" as const;
const UPDATE_DEPENDENCIES = "/api/services/agents/actions/update-dependencies" as const;
const GIT_FETCH = "/api/services/git/actions/fetch" as const;
const RUN_CHECKS = "/api/services/checks/actions/run" as const;

const STARTED = { job_id: "job-11", kind: "action", title: "Migrate database" };

const PARAMS_SCHEMA = {
  type: "object",
  title: "Params",
  properties: {
    target: { type: "string", title: "Target", default: "staging" },
    count: { type: "integer", title: "Count", default: 3 },
    dry_run: { type: "boolean", title: "Dry run", default: false },
  },
  required: ["target"],
} as const;

interface ActionsScenario {
  actions?: ActionInfo[];
  jobs?: JobSummary[];
  artifacts?: Schemas["ArtifactSizes"];
  /** Whether the project is a git repository; build output is only listed for one by default. */
  isGit?: boolean;
}

async function openActions(scenario: ActionsScenario = {}) {
  const api = mockApi()
    .action(JOB_HISTORY, { jobs: scenario.jobs ?? [] })
    .action(
      ARTIFACT_SIZES,
      scenario.artifacts ?? { project_id: "alpha", total_bytes: 0, artifacts: [] },
    )
    .action(RUN_ACTION, STARTED);
  const opened = await openProjectTab(
    "actions",
    {
      detail: { is_git: scenario.isGit ?? true },
      streams: {
        actions: {
          projects: [{ project_id: "alpha", actions: scenario.actions ?? [] }],
        },
      },
    },
    api,
  );
  return { ...opened };
}

function actionRow(label: string): HTMLElement {
  const row = screen.getByText(label, { selector: "p" }).closest("li");
  if (!row) throw new Error(`no custom action row for ${label}`);
  return row;
}

describe("ActionsTab project actions", () => {
  test("explains how to declare actions when the manifest has none", async () => {
    await openActions();
    expect(await screen.findByText("No custom actions")).toBeTruthy();
    expect(screen.getByText(/Declare scripts the project needs run/)).toBeTruthy();
  });

  test("lists each declared action with its description and requirements", async () => {
    await openActions({
      actions: [
        makeActionInfo("migrate", { label: "Migrate", description: "Apply pending migrations" }),
        makeActionInfo("seed", { label: "Seed", requires_service: "database" }),
        makeActionInfo("wipe", { label: "Wipe data", danger: true }),
      ],
    });
    expect(await screen.findByText("Apply pending migrations")).toBeTruthy();
    expect(within(actionRow("Seed")).getByText("needs database")).toBeTruthy();
    expect(within(actionRow("Wipe data")).getByText("destructive")).toBeTruthy();
    expect(within(actionRow("Migrate")).queryByText(/^needs/)).toBeNull();
    expect(within(actionRow("Migrate")).queryByText("destructive")).toBeNull();
  });

  test("runs a plain action at once and links to the job it started", async () => {
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("migrate", { label: "Migrate" })],
    });
    await rendered.user.click(within(actionRow("Migrate")).getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Started: Migrate database")).toBeTruthy();
    expect(api.sent(RUN_ACTION)).toEqual([
      { project_id: "alpha", action_id: "migrate", params: {} },
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
    await rendered.user.click(await screen.findByRole("button", { name: "View job" }));
    expect(rendered.location()).toBe("/jobs/job-11");
  });

  test("disables the button while the action is starting", async () => {
    const gate = deferred<typeof STARTED>();
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("migrate", { label: "Migrate" })],
    });
    api.action(RUN_ACTION, () => gate.promise);
    const run = within(actionRow("Migrate")).getByRole("button", {
      name: "Run",
    }) as HTMLButtonElement;
    await rendered.user.click(run);
    await waitFor(() => expect(run.disabled).toBe(true));
    gate.resolve(STARTED);
    await waitFor(() => expect(run.disabled).toBe(false));
  });

  test("reloads queries once the action has started", async () => {
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("migrate", { label: "Migrate" })],
    });
    expect(api.callsTo("POST", JOB_HISTORY)).toHaveLength(1);
    await rendered.user.click(within(actionRow("Migrate")).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.callsTo("POST", JOB_HISTORY)).toHaveLength(2));
  });

  test("reports the server's reason when a plain action fails to start", async () => {
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("migrate", { label: "Migrate" })],
    });
    api.action(RUN_ACTION, () => {
      throw new MockApiError(409, "the database service is not running");
    });
    await rendered.user.click(within(actionRow("Migrate")).getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Migrate failed")).toBeTruthy();
    expect(screen.getByText("the database service is not running")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("reports an unexpected failure by its description", async () => {
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("migrate", { label: "Migrate" })],
    });
    api.action(RUN_ACTION, () => {
      throw new Error("socket hang up");
    });
    await rendered.user.click(within(actionRow("Migrate")).getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Migrate failed")).toBeTruthy();
    expect(screen.getByText("Error: socket hang up")).toBeTruthy();
  });
});

describe("ActionsTab parameter dialogs", () => {
  const parameterized = makeActionInfo("deploy", {
    label: "Deploy",
    description: "Ship it",
    params: PARAMS_SCHEMA,
  });

  test("asks for the parameters, starting from their defaults, and sends the edited values", async () => {
    const { api, rendered } = await openActions({ actions: [parameterized] });
    await rendered.user.click(within(actionRow("Deploy")).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Deploy" })).toBeTruthy();
    expect(within(dialog).getByText("Ship it")).toBeTruthy();
    const target = formField("target", dialog).getByRole("textbox") as HTMLInputElement;
    expect(target.value).toBe("staging");
    expect((formField("count", dialog).getByRole("spinbutton") as HTMLInputElement).value).toBe(
      "3",
    );
    expect(api.callsTo("POST", RUN_ACTION)).toEqual([]);
    await rendered.user.clear(target);
    await rendered.user.type(target, "production");
    await rendered.user.click(formField("dry_run", dialog).getByRole("switch"));
    await rendered.user.click(within(dialog).getByRole("button", { name: "Run Deploy" }));
    expect(await screen.findByText("Started: Migrate database")).toBeTruthy();
    expect(api.sent(RUN_ACTION)).toEqual([
      {
        project_id: "alpha",
        action_id: "deploy",
        params: { target: "production", count: 3, dry_run: true },
      },
    ]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("keeps the dialog open with the reason when the run is refused, then succeeds on retry", async () => {
    const { api, rendered } = await openActions({ actions: [parameterized] });
    let refuse = true;
    api.action(RUN_ACTION, () => {
      if (refuse) throw new MockApiError(422, "target is not allowed");
      return STARTED;
    });
    await rendered.user.click(within(actionRow("Deploy")).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await rendered.user.click(within(dialog).getByRole("button", { name: "Run Deploy" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("target is not allowed");
    expect(screen.queryByText("Deploy failed")).toBeNull();
    refuse = false;
    await rendered.user.click(within(dialog).getByRole("button", { name: "Run Deploy" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(RUN_ACTION)).toHaveLength(2);
  });

  test("cancelling sends nothing and reopening starts again from the defaults", async () => {
    const { api, rendered } = await openActions({ actions: [parameterized] });
    const open = () => within(actionRow("Deploy")).getByRole("button", { name: "Run" });
    await rendered.user.click(open());
    let dialog = await screen.findByRole("dialog");
    const target = formField("target", dialog).getByRole("textbox") as HTMLInputElement;
    await rendered.user.clear(target);
    await rendered.user.type(target, "edited");
    await rendered.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", RUN_ACTION)).toEqual([]);
    await rendered.user.click(open());
    dialog = await screen.findByRole("dialog");
    expect((formField("target", dialog).getByRole("textbox") as HTMLInputElement).value).toBe(
      "staging",
    );
  });

  test("asks for confirmation of an action without parameters and sends none", async () => {
    const { api, rendered } = await openActions({
      actions: [
        makeActionInfo("seed", { label: "Seed", description: "Load fixtures", confirm: true }),
      ],
    });
    await rendered.user.click(within(actionRow("Seed")).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelector("[data-field]")).toBeNull();
    expect(api.callsTo("POST", RUN_ACTION)).toEqual([]);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Run Seed" }));
    await waitFor(() => expect(api.sent(RUN_ACTION)).toHaveLength(1));
    expect(api.sent(RUN_ACTION)).toEqual([{ project_id: "alpha", action_id: "seed", params: {} }]);
  });

  test("a destructive action needs its id typed before it can run", async () => {
    const { api, rendered } = await openActions({
      actions: [
        makeActionInfo("wipe", { label: "Wipe data", danger: true, params: PARAMS_SCHEMA }),
      ],
    });
    await rendered.user.click(within(actionRow("Wipe data")).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", {
      name: "Run Wipe data",
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    const typed = within(dialog).getByRole("textbox", { name: /Type wipe to confirm/ });
    await rendered.user.type(typed, "wip");
    expect(confirm.disabled).toBe(true);
    await rendered.user.type(typed, "e");
    expect(confirm.disabled).toBe(false);
    expect(api.callsTo("POST", RUN_ACTION)).toEqual([]);
    await rendered.user.click(confirm);
    await waitFor(() => expect(api.sent(RUN_ACTION)).toHaveLength(1));
    expect(api.sent(RUN_ACTION)).toEqual([
      {
        project_id: "alpha",
        action_id: "wipe",
        params: { target: "staging", count: 3, dry_run: false },
      },
    ]);
  });

  test("submitting a destructive action from the keyboard with the wrong text does nothing", async () => {
    const { api, rendered } = await openActions({
      actions: [makeActionInfo("wipe", { label: "Wipe data", danger: true })],
    });
    await rendered.user.click(within(actionRow("Wipe data")).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await rendered.user.type(within(dialog).getByRole("textbox"), "nope{Enter}");
    expect(api.callsTo("POST", RUN_ACTION)).toEqual([]);
    expect(screen.getByRole("dialog")).toBeTruthy();
    await rendered.user.clear(within(dialog).getByRole("textbox"));
    await rendered.user.type(within(dialog).getByRole("textbox"), "wipe{Enter}");
    await waitFor(() => expect(api.sent(RUN_ACTION)).toHaveLength(1));
  });
});

describe("ActionsTab built-in actions", () => {
  test("refreshes everything for the project", async () => {
    const { api, rendered } = await openActions();
    api.action(REFRESH_PROJECT, { ok: true, message: "Refreshed alpha" });
    await rendered.user.click(await screen.findByRole("button", { name: "Refresh everything" }));
    expect(await screen.findByText("Refreshed alpha")).toBeTruthy();
    expect(api.sent(REFRESH_PROJECT)).toEqual([{ project_id: "alpha" }]);
  });

  test("updates dependencies after confirming with optional instructions", async () => {
    const { api, rendered } = await openActions();
    api.action(UPDATE_DEPENDENCIES, {
      job_id: "job-12",
      kind: "update-deps",
      title: "Update deps",
    });
    await rendered.user.click(await screen.findByRole("button", { name: "Update dependencies…" }));
    const dialog = await screen.findByRole("dialog");
    await rendered.user.type(
      formField("instructions", dialog).getByRole("textbox"),
      "Keep pydantic on v2",
    );
    await rendered.user.click(within(dialog).getByRole("button", { name: "Update dependencies" }));
    expect(await screen.findByText("Started: Update deps")).toBeTruthy();
    expect(api.sent(UPDATE_DEPENDENCIES)).toEqual([
      { project_id: "alpha", instructions: "Keep pydantic on v2" },
    ]);
  });

  test("fetches the project's remotes", async () => {
    const { api, rendered } = await openActions();
    api.action(GIT_FETCH, { job_id: "job-13", kind: "git", title: "Fetch alpha" });
    await rendered.user.click(await screen.findByRole("button", { name: "Fetch" }));
    expect(await screen.findByText("Started: Fetch alpha")).toBeTruthy();
    expect(api.sent(GIT_FETCH)).toEqual([{ project_id: "alpha" }]);
  });

  test("runs every check", async () => {
    const { api, rendered } = await openActions();
    api.action(RUN_CHECKS, { job_id: "job-14", kind: "checks", title: "Checks alpha" });
    const section = (await screen.findByRole("heading", { name: "Built-in" })).closest("section");
    await rendered.user.click(
      within(section as HTMLElement).getByRole("button", { name: "Run checks" }),
    );
    expect(await screen.findByText("Started: Checks alpha")).toBeTruthy();
    expect(api.sent(RUN_CHECKS)).toEqual([{ project_id: "alpha", check_id: null, on_head: null }]);
  });
});

describe("ActionsTab build output", () => {
  const artifacts: Schemas["ArtifactSizes"] = {
    project_id: "alpha",
    total_bytes: 3_700_000,
    artifacts: [
      { path: "node_modules", kind: "dependencies", size_bytes: 2_621_440 },
      { path: ".venv", kind: "virtualenv", size_bytes: 1_048_576 },
      { path: "dist", kind: "build", size_bytes: 512 },
    ],
  };

  test("asks for the sizes of this project's build output", async () => {
    const { api } = await openActions({ artifacts });
    await screen.findByText("node_modules");
    expect(api.sent(ARTIFACT_SIZES)).toEqual([{ project_id: "alpha", allow_without_git: false }]);
  });

  test("lists each artifact with its size and kind, and the total", async () => {
    await openActions({ artifacts });
    const node = (await screen.findByText("node_modules")).closest("li") as HTMLElement;
    expect(node.textContent).toBe("node_modules2.5 MB dependencies");
    expect(screen.getByText(".venv").closest("li")?.textContent).toBe(".venv1.0 MB virtualenv");
    expect(screen.getByText("dist").closest("li")?.textContent).toBe("dist512 B build");
    expect(screen.getByText("3.5 MB in build output")).toBeTruthy();
  });

  test("says when there is no build output", async () => {
    await openActions();
    expect(
      await screen.findByText(
        "No build output found: only folders git ignores entirely are offered.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
  });

  test("shows why the sizes could not be read", async () => {
    const api = mockApi()
      .action(JOB_HISTORY, { jobs: [] })
      .action(ARTIFACT_SIZES, () => {
        throw new MockApiError(500, "disk scan failed");
      });
    await openProjectTab("actions", { detail: { is_git: true } }, api);
    expect((await screen.findByText("disk scan failed")).closest("[role=alert]")).toBeTruthy();
  });

  test("keeps Delete disabled until something is selected", async () => {
    await openActions({ artifacts });
    await screen.findByText("node_modules");
    const remove = screen.getByRole("button", { name: "Delete selected" }) as HTMLButtonElement;
    expect(remove.disabled).toBe(true);
  });

  test("counts the selected artifacts and lets them be unselected again", async () => {
    const { rendered } = await openActions({ artifacts });
    await screen.findByText("node_modules");
    await rendered.user.click(screen.getByRole("checkbox", { name: /node_modules/ }));
    await rendered.user.click(screen.getByRole("checkbox", { name: /\.venv/ }));
    const remove = screen.getByRole("button", { name: "Delete 2" }) as HTMLButtonElement;
    expect(remove.disabled).toBe(false);
    await rendered.user.click(screen.getByRole("checkbox", { name: /node_modules/ }));
    expect(screen.getByRole("button", { name: "Delete 1" })).toBeTruthy();
    await rendered.user.click(screen.getByRole("checkbox", { name: /\.venv/ }));
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeTruthy();
  });

  test("deleting needs the project id typed, sends the chosen paths and clears the selection", async () => {
    const { api, rendered } = await openActions({ artifacts });
    api.action(CLEAN_ARTIFACTS, { job_id: "job-15", kind: "clean", title: "Clean alpha" });
    await screen.findByText("node_modules");
    await rendered.user.click(screen.getByRole("checkbox", { name: /node_modules/ }));
    await rendered.user.click(screen.getByRole("checkbox", { name: /dist/ }));
    await rendered.user.click(screen.getByRole("button", { name: "Delete 2" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", {
      name: "Clean build artifacts",
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await rendered.user.type(within(dialog).getByRole("textbox"), "alpha");
    expect(api.callsTo("POST", CLEAN_ARTIFACTS)).toEqual([]);
    await rendered.user.click(confirm);
    expect(await screen.findByText("Started: Clean alpha")).toBeTruthy();
    expect(api.sent(CLEAN_ARTIFACTS)).toEqual([
      { project_id: "alpha", paths: ["node_modules", "dist"], allow_without_git: false },
    ]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete selected" })).toBeTruthy(),
    );
    expect(
      (screen.getByRole("checkbox", { name: /node_modules/ }) as HTMLInputElement).getAttribute(
        "aria-checked",
      ),
    ).toBe("false");
  });
});

describe("ActionsTab build output without git", () => {
  const artifacts: Schemas["ArtifactSizes"] = {
    project_id: "alpha",
    total_bytes: 1024,
    artifacts: [{ path: "dist", kind: "build", size_bytes: 1024 }],
  };

  test("asks before looking, since nothing marks which folders are disposable", async () => {
    const { api } = await openActions({ isGit: false, artifacts });
    expect(await screen.findByText(/This project has no git/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Look for build output anyway" })).toBeTruthy();
    expect(api.callsTo("POST", ARTIFACT_SIZES)).toEqual([]);
    expect(screen.queryByText("dist")).toBeNull();
  });

  test("lists build output once the viewer opts in and deletes with the opt-in recorded", async () => {
    const { api, rendered } = await openActions({ isGit: false, artifacts });
    api.action(CLEAN_ARTIFACTS, { job_id: "job-15", kind: "clean", title: "Clean alpha" });
    await rendered.user.click(
      await screen.findByRole("button", { name: "Look for build output anyway" }),
    );
    expect(await screen.findByText("dist")).toBeTruthy();
    expect(api.sent(ARTIFACT_SIZES)).toEqual([{ project_id: "alpha", allow_without_git: true }]);
    await rendered.user.click(screen.getByRole("checkbox", { name: /dist/ }));
    await rendered.user.click(screen.getByRole("button", { name: "Delete 1" }));
    const dialog = await screen.findByRole("dialog");
    await rendered.user.type(within(dialog).getByRole("textbox"), "alpha");
    await rendered.user.click(
      within(dialog).getByRole("button", { name: "Clean build artifacts" }),
    );
    await waitFor(() => expect(api.sent(CLEAN_ARTIFACTS)).toHaveLength(1));
    expect(api.sent(CLEAN_ARTIFACTS)).toEqual([
      { project_id: "alpha", paths: ["dist"], allow_without_git: true },
    ]);
  });
});

describe("ActionsTab job history", () => {
  test("asks for this project's forty latest jobs", async () => {
    const { api } = await openActions();
    await screen.findByText("No jobs yet.");
    expect(api.sent(JOB_HISTORY)).toEqual([{ project_id: "alpha", kind: null, limit: 40 }]);
  });

  test("says when the project has run no jobs", async () => {
    await openActions();
    expect(await screen.findByText("No jobs yet.")).toBeTruthy();
  });

  test("lists jobs with their summary, error or kind, status and age", async () => {
    await openActions({
      jobs: [
        makeJobSummary("j1", {
          title: "Nightly checks",
          summary: "12 passed",
          status: "succeeded",
        }),
        makeJobSummary("j2", { title: "Sync fork", error: "merge conflict", status: "failed" }),
        makeJobSummary("j3", { title: "Bring up", kind: "standards", status: "attention" }),
      ],
    });
    const first = (await screen.findByText("Nightly checks")).closest("a") as HTMLElement;
    expect(first.textContent).toContain("12 passed");
    expect(first.textContent).toContain("succeeded");
    expect(first.textContent).toContain("15 min ago");
    expect(screen.getByText("Sync fork").closest("a")?.textContent).toContain("merge conflict");
    const third = screen.getByText("Bring up").closest("a") as HTMLElement;
    expect(third.textContent).toContain("standards");
    expect(third.textContent).toContain("needs you");
  });

  test("opens a job from its row", async () => {
    const { rendered } = await openActions({
      jobs: [makeJobSummary("j9", { title: "Nightly checks" })],
    });
    const link = (await screen.findByText("Nightly checks")).closest("a") as HTMLElement;
    expect(link.getAttribute("href")).toBe("/jobs/j9");
    await rendered.user.click(link);
    expect(rendered.location()).toBe("/jobs/j9");
  });

  test("shows why the history could not be read and retries on request", async () => {
    let failing = true;
    const api = mockApi()
      .action(ARTIFACT_SIZES, { project_id: "alpha", total_bytes: 0, artifacts: [] })
      .action(JOB_HISTORY, () => {
        if (failing) throw new MockApiError(500, "job log offline");
        return { jobs: [makeJobSummary("j1", { title: "Nightly checks" })] };
      });
    const { rendered } = await openProjectTab("actions", {}, api);
    const alert = (await screen.findByText("job log offline")).closest(
      "[role=alert]",
    ) as HTMLElement;
    failing = false;
    await rendered.user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nightly checks")).toBeTruthy();
  });
});
