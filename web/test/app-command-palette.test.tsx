import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { makeProject, makeProjectsState } from "./factories";
import { installHarness, mockApi, renderApp, seedStream } from "./harness";
import { routeProject } from "./project-core-support";

installHarness();

const RESCAN = "/api/services/discovery/actions/rescan" as const;
const REFRESH_PROJECT = "/api/services/projects/actions/refresh-project" as const;

const PLACEHOLDER = "Jump to a project, page, or action…";

function seedProjects(): void {
  seedStream({
    projects: makeProjectsState([
      makeProject("alpha", { name: "Alpha", summary: "Handles payments", tags: ["billing"] }),
      makeProject("beta", { name: "Beta", summary: "Internal tools" }),
    ]),
  });
}

async function openPalette(
  user: ReturnType<typeof import("@testing-library/user-event").default.setup>,
) {
  await user.keyboard("{Control>}k{/Control}");
  return screen.findByRole("dialog");
}

describe("opening and closing the palette", () => {
  test("Ctrl+K opens it and pressing it again closes it", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    expect(screen.queryByRole("dialog")).toBeNull();
    const dialog = await openPalette(user);
    expect(within(dialog).getByPlaceholderText(PLACEHOLDER)).toBeTruthy();
    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("Cmd+K opens it for macOS users", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    await user.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  test("other key presses leave it closed", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    await user.keyboard("k");
    await user.keyboard("{Control>}j{/Control}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("Escape closes it", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    await openPalette(user);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("what the palette offers", () => {
  test("groups the fleet's projects, the pages and every runnable action", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    const palette = within(dialog);
    const headings = [...dialog.querySelectorAll("[cmdk-group-heading]")].map(
      (heading) => heading.textContent,
    );
    expect(headings).toEqual(["Projects", "Pages", "Actions"]);
    expect(palette.getByRole("option", { name: /Alpha/ }).textContent).toContain(
      "Handles payments",
    );
    expect(palette.getByRole("option", { name: /Beta/ })).toBeTruthy();
    expect(palette.getByRole("option", { name: "Forge runs" })).toBeTruthy();
    expect(palette.getByRole("option", { name: /Rescan/ }).textContent).toContain("Discovery");
  });

  test("leaves out read-only query actions", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    const palette = within(await openPalette(user));
    await screen.findByRole("option", { name: /Rescan/ });
    expect(palette.queryByRole("option", { name: /Job details/ })).toBeNull();
    expect(palette.queryByRole("option", { name: /Job history/ })).toBeNull();
  });

  test("filters as the viewer types and says so when nothing matches", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    const input = within(dialog).getByPlaceholderText(PLACEHOLDER);
    await user.type(input, "alpha");
    expect(within(dialog).getByRole("option", { name: /Alpha/ })).toBeTruthy();
    expect(within(dialog).queryByRole("option", { name: /Beta/ })).toBeNull();
    await user.clear(input);
    await user.type(input, "zzzzqqqq");
    expect(await within(dialog).findByText("Nothing matches.")).toBeTruthy();
  });

  test("finds a project by one of its tags", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    await user.type(within(dialog).getByPlaceholderText(PLACEHOLDER), "billing");
    expect(within(dialog).getByRole("option", { name: /Alpha/ })).toBeTruthy();
    expect(within(dialog).queryByRole("option", { name: /Beta/ })).toBeNull();
  });

  test("an action that needs a project is marked with an arrow until a project is open", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    const dialog = await openPalette(user);
    const needsProject = await within(dialog).findByRole("option", { name: /Refresh project/ });
    const rescan = within(dialog).getByRole("option", { name: /Rescan/ });
    expect(needsProject.querySelector("svg.size-3")).not.toBeNull();
    expect(rescan.querySelector("svg.size-3")).toBeNull();
  });
});

describe("jumping to a destination", () => {
  test("choosing a project opens its page and closes the palette", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    await user.click(within(dialog).getByRole("option", { name: /Beta/ }));
    expect(location()).toBe("/p/beta");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("choosing a page navigates to it", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    const dialog = await openPalette(user);
    await user.click(within(dialog).getByRole("option", { name: "Standards" }));
    expect(location()).toBe("/standards");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("running an action from the palette", () => {
  test("an action that needs no project runs at once and announces the job it started", async () => {
    const api = mockApi().action(RESCAN, {
      job_id: "scan-1",
      kind: "discovery.rescan",
      title: "Scanning folders",
    });
    const { user } = renderApp("/nope");
    const dialog = await openPalette(user);
    await user.click(await within(dialog).findByRole("option", { name: /Rescan/ }));
    expect(await screen.findByText("Started: Scanning folders")).toBeTruthy();
    expect(api.sent(RESCAN)).toEqual([{}]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("an action that needs a project asks which one as a second step", async () => {
    const api = mockApi().action(REFRESH_PROJECT, { ok: true, message: "Refreshed" });
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    await user.click(await within(dialog).findByRole("option", { name: /Refresh project/ }));
    expect(within(dialog).getByPlaceholderText("Refresh project: which project?")).toBeTruthy();
    expect(within(dialog).getByText('Run "Refresh project" on')).toBeTruthy();
    expect(within(dialog).queryByText("Pages")).toBeNull();
    expect(api.sent(REFRESH_PROJECT)).toEqual([]);
    await user.click(within(dialog).getByRole("option", { name: /^Beta/ }));
    expect(await screen.findByText("Refreshed")).toBeTruthy();
    expect(api.sent(REFRESH_PROJECT)).toEqual([{ project_id: "beta" }]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("the project step can be narrowed by typing a name", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    await user.click(await within(dialog).findByRole("option", { name: /Refresh project/ }));
    await user.type(within(dialog).getByPlaceholderText("Refresh project: which project?"), "alp");
    expect(within(dialog).getByRole("option", { name: /^Alpha/ })).toBeTruthy();
    expect(within(dialog).queryByRole("option", { name: /^Beta/ })).toBeNull();
  });

  test("closing mid-way forgets the chosen action so the palette reopens at the start", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    seedProjects();
    const dialog = await openPalette(user);
    await user.click(await within(dialog).findByRole("option", { name: /Refresh project/ }));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const reopened = within(await openPalette(user));
    expect(reopened.getByPlaceholderText(PLACEHOLDER)).toBeTruthy();
    expect(reopened.getByText("Pages")).toBeTruthy();
  });

  test("on a project's page the action runs on that project without asking", async () => {
    const api = mockApi().action(REFRESH_PROJECT, { ok: true, message: "Refreshed alpha" });
    routeProject(api, makeProject("alpha", { name: "Alpha" }));
    const { user } = renderApp("/p/alpha");
    seedProjects();
    const dialog = await openPalette(user);
    expect(within(dialog).getByText("Actions (on alpha where they take a project)")).toBeTruthy();
    await user.click(await within(dialog).findByRole("option", { name: /Refresh project/ }));
    expect(await screen.findByText("Refreshed alpha")).toBeTruthy();
    expect(api.sent(REFRESH_PROJECT)).toEqual([{ project_id: "alpha" }]);
  });

  test("a project id in the address is decoded before it is used", async () => {
    const api = mockApi().action(REFRESH_PROJECT, { ok: true, message: "Refreshed" });
    routeProject(api, makeProject("my project"));
    const { user } = renderApp("/p/my%20project");
    const dialog = await openPalette(user);
    expect(
      within(dialog).getByText("Actions (on my project where they take a project)"),
    ).toBeTruthy();
    await user.click(await within(dialog).findByRole("option", { name: /Refresh project/ }));
    await waitFor(() => expect(api.sent(REFRESH_PROJECT)).toEqual([{ project_id: "my project" }]));
  });

  test("an action with inputs opens its form instead of running", async () => {
    const api = mockApi();
    const { user } = renderApp("/nope");
    const dialog = await openPalette(user);
    await user.type(within(dialog).getByPlaceholderText(PLACEHOLDER), "Repeat a message");
    await user.click(await within(dialog).findByRole("option", { name: /Echo/ }));
    const form = await screen.findByRole("dialog");
    expect(within(form).getByText("Repeat a message.")).toBeTruthy();
    expect(api.callsTo("POST", "/api/services/gallery/actions/echo")).toEqual([]);
  });
});
