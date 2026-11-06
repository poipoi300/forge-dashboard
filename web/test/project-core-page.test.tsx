import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { NOW_SECONDS } from "./constants";
import { makeProject, makeProjectsState } from "./factories";
import services from "./fixtures/services.json";
import {
  formField,
  installHarness,
  MockApiError,
  mockApi,
  renderApp,
  seedStream,
  settle,
} from "./harness";
import {
  DETAIL_PATH,
  deferred,
  makeDetail,
  makeManifest,
  makeMetricDefinition,
  openProjectTab,
  routeProject,
} from "./project-core-support";

installHarness();

const SET_STATUS = "/api/services/projects/actions/set-status" as const;
const NOTES = "/api/services/projects/actions/notes" as const;

function tabNames(): string[] {
  return screen.getAllByRole("tab").map((tab) => tab.textContent ?? "");
}

function selectedTab(): string | undefined {
  return screen.getAllByRole("tab").find((tab) => tab.getAttribute("aria-selected") === "true")
    ?.textContent;
}

describe("ProjectPage loading and missing states", () => {
  test("shows loading rows until the projects service has published", async () => {
    mockApi();
    renderApp("/p/alpha");
    expect(await screen.findByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  test("keeps loading while the projects service has not finished its first scan", async () => {
    mockApi();
    renderApp("/p/alpha");
    seedStream({ projects: makeProjectsState([makeProject("alpha")], { loaded: false }) });
    await settle();
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "alpha" })).toBeNull();
  });

  test("says the project is missing and links back to the overview", async () => {
    const api = mockApi();
    api.config("discovery", { roots: [] });
    const { user, location } = renderApp("/p/ghost");
    seedStream({ projects: makeProjectsState([makeProject("alpha")]) });
    expect(await screen.findByText('No project "ghost"')).toBeTruthy();
    expect(screen.getByText(/Hidden projects are listed in Settings/)).toBeTruthy();
    expect(api.callsTo("POST", DETAIL_PATH)).toEqual([]);
    await user.click(screen.getByRole("link", { name: "Back to the overview" }));
    expect(location()).toBe("/");
  });

  test("shows loading rows while the project detail is being fetched", async () => {
    const api = mockApi();
    const gate = deferred<ReturnType<typeof makeDetail>>();
    const project = makeProject("alpha");
    routeProject(api, project).action(DETAIL_PATH, () => gate.promise);
    renderApp("/p/alpha");
    seedStream({ projects: makeProjectsState([project]) });
    await settle();
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "alpha" })).toBeNull();
    gate.resolve(makeDetail(project));
    expect(await screen.findByRole("heading", { name: "alpha" })).toBeTruthy();
  });

  test("shows the server's reason when the detail fails and retries on request", async () => {
    const api = mockApi();
    const project = makeProject("alpha");
    let failing = true;
    routeProject(api, project).action(DETAIL_PATH, () => {
      if (failing) throw new MockApiError(500, "manifest store unavailable");
      return makeDetail(project);
    });
    const { user } = renderApp("/p/alpha");
    seedStream({ projects: makeProjectsState([project]) });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("manifest store unavailable");
    failing = false;
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "alpha" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.callsTo("POST", DETAIL_PATH)).toHaveLength(2);
  });

  test("asks for the detail of the project named in the URL", async () => {
    const { api } = await openProjectTab(null);
    expect(api.sent(DETAIL_PATH)).toEqual([{ project_id: "alpha" }]);
  });
});

describe("ProjectPage header", () => {
  test("shows the name, summary, location, machine and age of the last change", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        name: "Alpha Service",
        summary: "Serves the alpha traffic",
        machine: "build-box",
      }),
    });
    expect(await screen.findByRole("heading", { name: "Alpha Service" })).toBeTruthy();
    expect(screen.getByText("Serves the alpha traffic")).toBeTruthy();
    expect(screen.getByText("on build-box")).toBeTruthy();
    expect(screen.getByText("C:/code/alpha")).toBeTruthy();
    expect(screen.getByText(/changed/).textContent).toBe("changed 1 h ago");
    expect(screen.queryByText(/uncommitted edits/)).toBeNull();
    expect(screen.queryByText("no manifest")).toBeNull();
  });

  test("omits the summary line when the project has none", async () => {
    await openProjectTab(null, { project: makeProject("alpha", { summary: null }) });
    await screen.findByRole("heading", { name: "alpha" });
    expect(document.querySelector("header p")).toBeNull();
  });

  test("flags a last change that is only uncommitted edits", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        last_change: { at: NOW_SECONDS - 120, source: "working-tree" },
      }),
    });
    expect((await screen.findByText(/changed/)).textContent).toBe(
      "changed 2 min ago (uncommitted edits)",
    );
  });

  test("marks an unmanifested project", async () => {
    await openProjectTab(null, { project: makeProject("alpha", { role: "unmanifested" }) });
    expect(await screen.findByText("no manifest")).toBeTruthy();
  });

  test("links to the breadcrumb root, GitHub, homepage and docs in new tabs", async () => {
    const { rendered } = await openProjectTab(null, {
      detail: {
        links: {
          github: "https://github.com/acme/alpha",
          homepage: "https://alpha.example.com",
          docs: "https://alpha.example.com/docs",
        },
      },
    });
    const github = await screen.findByRole("link", { name: "GitHub" });
    expect(github.getAttribute("href")).toBe("https://github.com/acme/alpha");
    expect(github.getAttribute("target")).toBe("_blank");
    expect(screen.getByRole("link", { name: "Homepage" }).getAttribute("href")).toBe(
      "https://alpha.example.com",
    );
    expect(screen.getByRole("link", { name: "Docs" }).getAttribute("href")).toBe(
      "https://alpha.example.com/docs",
    );
    const breadcrumb = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(breadcrumb.textContent).toBe("Overview / alpha");
    await rendered.user.click(within(breadcrumb).getByRole("link", { name: "Overview" }));
    expect(rendered.location()).toBe("/");
  });

  test("shows no external links when the project declares none", async () => {
    await openProjectTab(null);
    await screen.findByRole("heading", { name: "alpha" });
    expect(screen.queryByRole("link", { name: "GitHub" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Homepage" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Docs" })).toBeNull();
  });

  test("reports the manifest problems with their location and line", async () => {
    await openProjectTab(null, {
      detail: {
        manifest_issues: [
          { location: "checks[0].argv", line: 12, message: "argv must not be empty" },
          { location: "", line: null, message: "unknown key" },
        ],
      },
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "forge.project.toml has 2 problems: checks[0].argv (line 12): argv must not be empty; file: unknown key",
    );
  });

  test("says problem in the singular for a single manifest problem", async () => {
    await openProjectTab(null, {
      detail: { manifest_issues: [{ location: "v1", line: null, message: "no milestones" }] },
    });
    expect((await screen.findByRole("alert")).textContent).toBe(
      "forge.project.toml has 1 problem: v1: no milestones",
    );
  });

  test("truncates a long list of manifest problems after three", async () => {
    await openProjectTab(null, {
      detail: {
        manifest_issues: ["a", "b", "c", "d"].map((key) => ({
          location: key,
          line: null,
          message: `bad ${key}`,
        })),
      },
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "forge.project.toml has 4 problems: a: bad a; b: bad b; c: bad c",
    );
  });

  test("renders no problem banner for a clean manifest", async () => {
    await openProjectTab(null);
    await screen.findByRole("heading", { name: "alpha" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("shows the health verdict from the summary schema once descriptors arrive", async () => {
    const api = mockApi();
    const descriptors = deferred<typeof services>();
    api.route("GET", "/api/services", () => descriptors.promise);
    await openProjectTab(null, {}, api);
    await screen.findByRole("heading", { name: "alpha" });
    expect(screen.queryByText("healthy")).toBeNull();
    descriptors.resolve(services);
    expect(await screen.findByText("healthy")).toBeTruthy();
  });

  test("includes the quick actions for the project", async () => {
    await openProjectTab(null);
    expect(await screen.findByRole("button", { name: "More actions for alpha" })).toBeTruthy();
  });
});

describe("ProjectPage header summary", () => {
  test("writes out why the project is unhealthy, leaving out reasons that are fine", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        health: {
          level: "failing",
          reasons: [
            { source: "ci", level: "failing", message: "CI is red" },
            { source: "checks", level: "warn", message: "tests are stale" },
            { source: "manifest", level: "good", message: "manifest is valid" },
          ],
        },
      }),
    });
    expect(await screen.findByText("CI is red · tests are stale")).toBeTruthy();
    expect(screen.queryByText(/manifest is valid/)).toBeNull();
  });

  test("shows no reason line for a healthy project", async () => {
    await openProjectTab(null);
    await screen.findByRole("tablist");
    expect(screen.queryByText(/ · /)).toBeNull();
  });

  test("answers the at-a-glance questions in one line: V1 progress, signals and services", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        v1: {
          done: 3,
          total: 4,
          percent: 75,
          milestones_done: 1,
          milestones_total: 2,
          next_milestone: null,
          target_date: null,
          days_left: null,
          caption: "3 of 4 done",
          detail: "",
        },
        checks: {
          result: "failing",
          passed: 3,
          failed: 1,
          total: 4,
          tests_passed: 10,
          tests_failed: 1,
          ran_at: NOW_SECONDS - 600,
          duration_s: 5,
          sha: null,
          stale: false,
          coverage_percent: null,
        },
        services: [
          {
            id: "web",
            label: "web",
            state: "running",
            address: "127.0.0.1:8000",
            url: null,
            machine: "workstation",
          },
          {
            id: "db",
            label: "db",
            state: "stopped",
            address: null,
            url: null,
            machine: "workstation",
          },
        ],
      }),
    });
    expect(await screen.findByText("75%")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "V1 progress" }).getAttribute("aria-valuenow")).toBe(
      "75",
    );
    expect(screen.getByText("3 of 4 done")).toBeTruthy();
    expect(screen.getByText("checks", { selector: "span" })).toBeTruthy();
    expect(screen.getByText("127.0.0.1:8000")).toBeTruthy();
    expect(screen.getByText("stopped")).toBeTruthy();
  });

  test("shows a finished V1 as complete", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        v1: {
          done: 4,
          total: 4,
          percent: 100,
          milestones_done: 0,
          milestones_total: 0,
          next_milestone: null,
          target_date: null,
          days_left: null,
          caption: "shipped",
          detail: "",
        },
      }),
    });
    expect(await screen.findByText("100%")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "V1 progress" }).getAttribute("aria-valuenow")).toBe(
      "100",
    );
  });
});

describe("ProjectPage status marker", () => {
  test("shows the status and its note, with the note in the tooltip", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", { status: "blocked", status_note: "waiting on vendor" }),
    });
    const button = await screen.findByRole("button", { name: /change; one of/ });
    expect(button.textContent).toContain("blocked");
    expect(button.textContent).toContain("waiting on vendor");
    expect(button.getAttribute("title")).toBe("blocked: waiting on vendor");
    expect(button.textContent).toContain("idea, planning, active, blocked");
  });

  test("uses a generic tooltip when there is no note", async () => {
    await openProjectTab(null);
    const button = await screen.findByRole("button", { name: /change; one of/ });
    expect(button.getAttribute("title")).toBe("Set the status marker");
  });

  test("the set-status dialog starts from the current status and note and sends the edited ones", async () => {
    const { api, rendered } = await openProjectTab(null, {
      project: makeProject("alpha", { status: "blocked", status_note: "waiting on vendor" }),
    });
    api.action(SET_STATUS, { ok: true, message: null });
    await rendered.user.click(await screen.findByRole("button", { name: /change; one of/ }));
    const dialog = await screen.findByRole("dialog");
    const note = formField("note", dialog).getByRole("textbox") as HTMLInputElement;
    expect(note.value).toBe("waiting on vendor");
    expect(formField("status", dialog).getByRole("combobox").textContent).toBe("blocked");
    await rendered.user.clear(note);
    await rendered.user.type(note, "vendor replied");
    await rendered.user.click(formField("status", dialog).getByRole("combobox"));
    await rendered.user.click(await screen.findByRole("option", { name: "active" }));
    await rendered.user.click(within(dialog).getByRole("button", { name: "Set status" }));
    await waitFor(() => expect(api.sent(SET_STATUS)).toHaveLength(1));
    expect(api.sent(SET_STATUS)[0]).toEqual({
      project_id: "alpha",
      status: "active",
      note: "vendor replied",
    });
  });

  test("opens the set-status dialog and sends nothing until it is confirmed", async () => {
    const { api, rendered } = await openProjectTab(null);
    await rendered.user.click(await screen.findByRole("button", { name: /change; one of/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Set status" })).toBeTruthy();
    expect(api.callsTo("POST", SET_STATUS)).toEqual([]);
    await rendered.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", SET_STATUS)).toEqual([]);
  });
});

describe("ProjectPage tabs", () => {
  test("opens the first tab when the URL names none", async () => {
    await openProjectTab(null);
    await screen.findByRole("tablist");
    expect(selectedTab()).toBe("Launch");
    expect(await screen.findByText("Run it with uv.")).toBeTruthy();
  });

  test("opens the tab named in the URL", async () => {
    const api = mockApi().action(NOTES, { markdown: "", updated_at: null });
    await openProjectTab("notes", {}, api);
    await waitFor(() => expect(selectedTab()).toBe("Notes"));
    expect(await screen.findByText("never saved")).toBeTruthy();
  });

  test("falls back to the first tab for a tab the project does not have", async () => {
    await openProjectTab("git");
    await screen.findByRole("tablist");
    expect(selectedTab()).toBe("Launch");
  });

  test("falls back to the first tab for an unknown tab name", async () => {
    await openProjectTab("nonsense");
    await screen.findByRole("tablist");
    expect(selectedTab()).toBe("Launch");
  });

  test("navigates to the tab that is clicked and shows its content", async () => {
    const api = mockApi().action(NOTES, { markdown: "# Plans", updated_at: null });
    const { rendered } = await openProjectTab(null, {}, api);
    await rendered.user.click(await screen.findByRole("tab", { name: "Notes" }));
    expect(rendered.location()).toBe("/p/alpha/notes");
    expect(await screen.findByRole("heading", { name: "Plans" })).toBeTruthy();
    expect(selectedTab()).toBe("Notes");
  });

  test("offers the tabs every project has when it is not a git repo", async () => {
    await openProjectTab(null);
    await screen.findByRole("tablist");
    expect(tabNames()).toEqual([
      "Launch",
      "Quality",
      "Issues",
      "Standards",
      "Actions",
      "Agents",
      "Activity",
      "Notes",
      "Settings",
    ]);
  });

  test("hides the Issues tab for a project that is not on GitHub", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        issues: {
          github: "not-on-github",
          repo: null,
          open_issues: null,
          open_prs: null,
          top_labels: [],
          newest_issue: null,
          html_url: null,
          milestone_open: null,
          milestone_closed: null,
        },
      }),
    });
    await screen.findByRole("tablist");
    expect(tabNames()).not.toContain("Issues");
  });

  test("adds the Git tab for a git repository but not Fork without an upstream", async () => {
    await openProjectTab(null, { detail: { is_git: true } });
    await screen.findByRole("tablist");
    expect(tabNames()).toContain("Git");
    expect(tabNames()).not.toContain("Fork");
  });

  test("adds the Fork tab for a git repository marked as a fork", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        fork: {
          is_fork: true,
          upstream: "upstream/repo",
          behind_upstream: null,
          ahead_of_upstream: null,
          last_sync: "never",
          last_sync_at: null,
          next_sync_at: null,
        },
      }),
      detail: { is_git: true },
    });
    await screen.findByRole("tablist");
    expect(tabNames().slice(3, 5)).toEqual(["Git", "Fork"]);
  });

  test("adds the Fork tab for a git repository with an upstream remote", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        git: {
          branch: "main",
          head_sha: null,
          dirty: 0,
          ahead: null,
          behind: null,
          unpushed: 0,
          stashes: 0,
          last_commit_at: null,
          last_worktree_change_at: null,
          origin_url: null,
          github_repo: null,
          has_upstream_remote: true,
        },
      }),
      detail: { is_git: true },
    });
    await screen.findByRole("tablist");
    expect(tabNames()).toContain("Fork");
  });

  test("never offers Fork outside a git repository, even for a marked fork", async () => {
    await openProjectTab(null, {
      project: makeProject("alpha", {
        fork: {
          is_fork: true,
          upstream: null,
          behind_upstream: null,
          ahead_of_upstream: null,
          last_sync: "never",
          last_sync_at: null,
          next_sync_at: null,
        },
      }),
    });
    await screen.findByRole("tablist");
    expect(tabNames()).not.toContain("Fork");
  });

  test("adds the Metrics tab when the manifest declares metrics", async () => {
    const project = makeProject("alpha");
    await openProjectTab(null, {
      project,
      detail: { manifest: makeManifest(project, { metrics: [makeMetricDefinition("loc")] }) },
    });
    await screen.findByRole("tablist");
    expect(tabNames()).toContain("Metrics");
  });

  test("omits the Metrics tab for a manifest without metrics", async () => {
    const project = makeProject("alpha");
    await openProjectTab(null, { project, detail: { manifest: makeManifest(project) } });
    await screen.findByRole("tablist");
    expect(tabNames()).not.toContain("Metrics");
  });
});

describe("ProjectPage live updates", () => {
  test("refetches the detail when the projects service publishes a new revision", async () => {
    const api = mockApi();
    const project = makeProject("alpha");
    let issues: { location: string; line: number | null; message: string }[] = [];
    routeProject(api, project).action(DETAIL_PATH, () =>
      makeDetail(project, { manifest_issues: issues }),
    );
    renderApp("/p/alpha");
    seedStream({ projects: makeProjectsState([project]) });
    await screen.findByRole("heading", { name: "alpha" });
    expect(screen.queryByRole("alert")).toBeNull();
    issues = [{ location: "v1", line: null, message: "no milestones" }];
    seedStream({ projects: makeProjectsState([project]) }, 2);
    expect((await screen.findByRole("alert")).textContent).toContain("no milestones");
    expect(api.callsTo("POST", DETAIL_PATH)).toHaveLength(2);
  });

  test("keeps the page up while the detail refetches for a new revision", async () => {
    const api = mockApi();
    const project = makeProject("alpha");
    const second = deferred<ReturnType<typeof makeDetail>>();
    let calls = 0;
    routeProject(api, project).action(DETAIL_PATH, () => {
      calls += 1;
      return calls === 1 ? makeDetail(project) : second.promise;
    });
    renderApp("/p/alpha");
    seedStream({ projects: makeProjectsState([project]) });
    await screen.findByRole("heading", { name: "alpha" });
    seedStream({ projects: makeProjectsState([project]) }, 2);
    await settle();
    expect(screen.getByRole("heading", { name: "alpha" })).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
    second.resolve(makeDetail(project));
  });
});
