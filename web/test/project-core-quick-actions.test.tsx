import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { QuickActions } from "@/features/project/QuickActions";
import { makeProject, type ProjectSummary } from "./factories";
import { installHarness, MockApiError, mockApi, renderWithProviders, settle } from "./harness";
import { makeServiceBrief } from "./project-core-support";

installHarness();

const LAUNCH = "/api/services/services/actions/launch" as const;
const RUN_CHECKS = "/api/services/checks/actions/run" as const;
const SYNC_FORK = "/api/services/forks/actions/sync" as const;
const OPEN_EDITOR = "/api/services/settings/actions/open-in-editor" as const;
const OPEN_TERMINAL = "/api/services/settings/actions/open-in-terminal" as const;
const OPEN_FOLDER = "/api/services/settings/actions/open-folder" as const;
const BRING_UP = "/api/services/standards/actions/bring-up" as const;
const ADOPT = "/api/services/standards/actions/adopt" as const;

const CHECK_FACTS: NonNullable<ProjectSummary["checks"]> = {
  result: "passing",
  passed: 3,
  failed: 0,
  total: 3,
  tests_passed: null,
  tests_failed: null,
  ran_at: null,
  duration_s: null,
  sha: null,
  stale: false,
  coverage_percent: null,
};

const FORK_FACTS: NonNullable<ProjectSummary["fork"]> = {
  is_fork: true,
  upstream: "upstream/repo",
  behind_upstream: 2,
  ahead_of_upstream: 0,
  last_sync: "never",
  last_sync_at: null,
  next_sync_at: null,
};

const GITHUB_ISSUES: NonNullable<ProjectSummary["issues"]> = {
  github: "ok",
  repo: "acme/alpha",
  open_issues: 4,
  open_prs: 1,
  top_labels: [],
  newest_issue: null,
  html_url: "https://github.com/acme/alpha",
  milestone_open: null,
  milestone_closed: null,
};

async function renderActions(project: ProjectSummary, size?: "xs" | "sm") {
  const rendered = renderWithProviders(<QuickActions project={project} size={size} />);
  await settle();
  return rendered;
}

async function openMenu(
  project: ProjectSummary,
  user: ReturnType<typeof renderWithProviders>["user"],
) {
  await user.click(screen.getByRole("button", { name: `More actions for ${project.name}` }));
  return screen.findByRole("menu");
}

function menuLabels(menu: HTMLElement): string[] {
  return within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent ?? "");
}

describe("QuickActions buttons", () => {
  test("offers only the overflow menu for a project with no services, checks or fork", async () => {
    mockApi();
    await renderActions(makeProject("alpha"));
    expect(
      screen.getAllByRole("button").map((button) => button.getAttribute("aria-label")),
    ).toEqual(["More actions for alpha"]);
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("links to the UI of a running service instead of offering to launch it", async () => {
    mockApi();
    await renderActions(
      makeProject("alpha", {
        services: [
          makeServiceBrief("web", "running", { url: "http://localhost:5173" }),
          makeServiceBrief("api", "running", { url: "http://localhost:8000" }),
        ],
      }),
      "sm",
    );
    const link = screen.getByRole("link", { name: "Open UI" });
    expect(link.getAttribute("href")).toBe("http://localhost:5173");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.queryByRole("button", { name: "Launch" })).toBeNull();
  });

  test("treats a service run by someone else as open too", async () => {
    mockApi();
    await renderActions(
      makeProject("alpha", {
        services: [makeServiceBrief("web", "external", { url: "http://localhost:3000" })],
      }),
    );
    expect(screen.getByRole("link", { name: "Open UI" }).getAttribute("href")).toBe(
      "http://localhost:3000",
    );
  });

  test("offers Launch when no service is running with a URL", async () => {
    mockApi();
    await renderActions(
      makeProject("alpha", {
        services: [
          makeServiceBrief("web", "stopped", { url: "http://localhost:5173" }),
          makeServiceBrief("worker", "running"),
        ],
      }),
    );
    expect(screen.queryByRole("link", { name: "Open UI" })).toBeNull();
    expect(screen.getByRole("button", { name: "Launch" })).toBeTruthy();
  });

  test("launching a project posts its id and links to the started job", async () => {
    const api = mockApi().action(LAUNCH, {
      job_id: "job-9",
      kind: "launch",
      title: "Launch alpha",
    });
    const { user, location } = await renderActions(
      makeProject("alpha", { services: [makeServiceBrief("web", "stopped")] }),
    );
    await user.click(screen.getByRole("button", { name: "Launch" }));
    expect(await screen.findByText("Started: Launch alpha")).toBeTruthy();
    expect(api.sent(LAUNCH)).toEqual([{ project_id: "alpha" }]);
    await user.click(await screen.findByRole("button", { name: "View job" }));
    expect(location()).toBe("/jobs/job-9");
  });

  test("reports why a launch failed", async () => {
    mockApi().action(LAUNCH, () => {
      throw new MockApiError(409, "port 5173 is taken");
    });
    const { user } = await renderActions(
      makeProject("alpha", { services: [makeServiceBrief("web", "stopped")] }),
    );
    await user.click(screen.getByRole("button", { name: "Launch" }));
    expect(await screen.findByText("Launch failed")).toBeTruthy();
    expect(await screen.findByText("port 5173 is taken")).toBeTruthy();
  });

  test("runs every check on request when the project has checks", async () => {
    const api = mockApi().action(RUN_CHECKS, { job_id: "job-1", kind: "checks", title: "Checks" });
    const { user } = await renderActions(makeProject("alpha", { checks: CHECK_FACTS }));
    await user.click(screen.getByRole("button", { name: "Run checks" }));
    expect(await screen.findByText("Started: Checks")).toBeTruthy();
    expect(api.sent(RUN_CHECKS)).toEqual([{ project_id: "alpha", check_id: null, on_head: null }]);
  });

  test("hides Run checks for a project without checks", async () => {
    mockApi();
    await renderActions(makeProject("alpha", { checks: null }));
    expect(screen.queryByRole("button", { name: "Run checks" })).toBeNull();
  });

  test("syncs a fork on request", async () => {
    const api = mockApi().action(SYNC_FORK, { job_id: "job-2", kind: "fork-sync", title: "Sync" });
    const { user } = await renderActions(makeProject("alpha", { fork: FORK_FACTS }));
    await user.click(screen.getByRole("button", { name: "Sync fork" }));
    expect(await screen.findByText("Started: Sync")).toBeTruthy();
    expect(api.sent(SYNC_FORK)).toEqual([{ project_id: "alpha" }]);
  });

  test("hides Sync fork for a project that is not a fork", async () => {
    mockApi();
    await renderActions(makeProject("alpha", { fork: { ...FORK_FACTS, is_fork: false } }));
    expect(screen.queryByRole("button", { name: "Sync fork" })).toBeNull();
  });
});

describe("QuickActions overflow menu", () => {
  test("lists the open actions and bring-up for a managed project", async () => {
    mockApi();
    const project = makeProject("alpha", { role: "managed" });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    expect(menuLabels(menu)).toEqual([
      "Open in editor",
      "Open in terminal",
      "Open folder",
      "Bring up to standards",
    ]);
  });

  test("offers adoption instead of bring-up for a project without a manifest", async () => {
    mockApi();
    const project = makeProject("alpha", { role: "unmanifested" });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    expect(menuLabels(menu)).toEqual([
      "Open in editor",
      "Open in terminal",
      "Open folder",
      "Adopt into forge",
    ]);
  });

  test("offers neither adoption nor bring-up for the standards source itself", async () => {
    mockApi();
    const project = makeProject("forge", { role: "standards-source" });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    expect(menuLabels(menu)).toEqual(["Open in editor", "Open in terminal", "Open folder"]);
  });

  test("adds a GitHub link after a separator when the project has a repository", async () => {
    mockApi();
    const project = makeProject("alpha", { issues: GITHUB_ISSUES });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    expect(menuLabels(menu).at(-1)).toBe("Open on GitHub");
    const link = within(menu).getByRole("menuitem", { name: "Open on GitHub" });
    expect(link.getAttribute("href")).toBe("https://github.com/acme/alpha");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(within(menu).getAllByRole("separator")).toHaveLength(1);
  });

  test("has no separator or GitHub link without a repository URL", async () => {
    mockApi();
    const project = makeProject("alpha", { issues: { ...GITHUB_ISSUES, html_url: null } });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    expect(menuLabels(menu)).not.toContain("Open on GitHub");
    expect(within(menu).queryByRole("separator")).toBeNull();
  });

  test.each([
    ["Open in editor", OPEN_EDITOR],
    ["Open in terminal", OPEN_TERMINAL],
    ["Open folder", OPEN_FOLDER],
  ] as const)("%s launches the tool in the project root without asking", async (label, path) => {
    const api = mockApi().action(path, { argv: ["code", "."], cwd: "C:/code/alpha", pid: 4242 });
    const project = makeProject("alpha");
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    await user.click(within(menu).getByRole("menuitem", { name: label }));
    expect(await screen.findByText(`${label}: done`)).toBeTruthy();
    expect(api.sent(path)).toEqual([{ project_id: "alpha", path: null }]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("bring-up waits for confirmation, then starts the job", async () => {
    const api = mockApi().action(BRING_UP, {
      job_id: "job-3",
      kind: "standards",
      title: "Bring up alpha",
    });
    const project = makeProject("alpha");
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    await user.click(within(menu).getByRole("menuitem", { name: "Bring up to standards" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.callsTo("POST", BRING_UP)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Bring up to standards" }));
    expect(await screen.findByText("Started: Bring up alpha")).toBeTruthy();
    expect(api.sent(BRING_UP)).toEqual([{ project_id: "alpha" }]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("cancelling the bring-up confirmation sends nothing", async () => {
    const api = mockApi();
    const project = makeProject("alpha");
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    await user.click(within(menu).getByRole("menuitem", { name: "Bring up to standards" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", BRING_UP)).toEqual([]);
  });

  test("adoption waits for confirmation, then starts the job", async () => {
    const api = mockApi().action(ADOPT, {
      job_id: "job-4",
      kind: "standards",
      title: "Adopt alpha",
    });
    const project = makeProject("alpha", { role: "unmanifested" });
    const { user } = await renderActions(project);
    const menu = await openMenu(project, user);
    await user.click(within(menu).getByRole("menuitem", { name: "Adopt into forge" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Adopt" }));
    expect(await screen.findByText("Started: Adopt alpha")).toBeTruthy();
    expect(api.sent(ADOPT)).toEqual([{ project_id: "alpha" }]);
  });
});
