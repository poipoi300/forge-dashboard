import { describe, expect, test } from "bun:test";
import { act, screen, waitFor, within } from "@testing-library/react";
import { usePreferences } from "@/app/preferences";
import { makeDiscoveryState, makeRichProject } from "./app-support";
import { makeProject, makeProjectsState, type ProjectSummary } from "./factories";
import services from "./fixtures/services.json";
import { installHarness, type MockApi, mockApi, renderApp, seedStream, settle } from "./harness";

installHarness();

const SET_FAVORITE = "/api/services/projects/actions/set-favorite" as const;
const RESCAN = "/api/services/discovery/actions/rescan" as const;

interface Opened {
  api: MockApi;
  rendered: ReturnType<typeof renderApp>;
}

async function openOverview(
  projects: ProjectSummary[],
  options: { path?: string; roots?: string[] } = {},
): Promise<Opened> {
  const api = mockApi();
  api.config("discovery", { roots: options.roots ?? ["C:/code"] });
  const rendered = renderApp(options.path ?? "/");
  seedStream({ projects: makeProjectsState(projects), discovery: makeDiscoveryState() });
  await settle();
  return { api, rendered };
}

const fleet = (): ProjectSummary[] => [
  makeProject("alpha", {
    name: "Alpha",
    status: "active",
    kind: "service",
    languages: ["python"],
    tags: ["billing"],
    last_change: { at: 3000, source: "commit" },
    health: { level: "good", reasons: [] },
  }),
  makeProject("beta", {
    name: "Beta",
    status: "paused",
    kind: "library",
    languages: ["rust"],
    tags: ["tools"],
    last_change: { at: 2000, source: "commit" },
    health: { level: "failing", reasons: [] },
    attention: [{ source: "ci", level: "bad", message: "CI is red" }],
  }),
  makeProject("gamma", {
    name: "Gamma",
    status: "active",
    kind: null,
    languages: ["python", "rust"],
    tags: [],
    last_change: { at: 1000, source: "commit" },
    health: { level: "warn", reasons: [] },
  }),
];

function cards(): string[] {
  return screen.queryAllByRole("article").map((card) => card.getAttribute("aria-label") ?? "");
}

function searchBox(): HTMLInputElement {
  return screen.getByRole("textbox", { name: /^Search projects/ }) as HTMLInputElement;
}

function rowAt(index: number): HTMLElement {
  const row = screen.getAllByRole("row")[index];
  if (!row) throw new Error(`no table row ${index}`);
  return row;
}

function card(name: string): ReturnType<typeof within> {
  return within(screen.getByRole("article", { name }));
}

describe("before there is anything to show", () => {
  test("says it is loading until the first projects state arrives", async () => {
    mockApi().config("discovery", { roots: [] });
    renderApp("/");
    await settle();
    expect(screen.getByText("loading")).toBeTruthy();
    expect(cards()).toEqual([]);
  });

  test("keeps saying loading while the first scan has not finished", async () => {
    mockApi().config("discovery", { roots: [] });
    renderApp("/");
    seedStream({ projects: makeProjectsState([makeProject("a")], { loaded: false }) });
    await settle();
    expect(screen.getByText("loading")).toBeTruthy();
    expect(cards()).toEqual([]);
  });

  test("with no projects it explains where it looked and offers to add or rescan", async () => {
    const { api, rendered } = await openOverview([], { roots: ["C:/code", "D:/work"] });
    expect(screen.getByText("No projects found yet")).toBeTruthy();
    expect(screen.getByText(/The scan covers C:\/code, D:\/work\./)).toBeTruthy();
    api.action(RESCAN, { job_id: "scan-1", kind: "discovery.rescan", title: "Rescanning" });
    await rendered.user.click(screen.getByRole("button", { name: "Scan again" }));
    expect(await screen.findByText("Started: Rescanning")).toBeTruthy();
    expect(api.sent(RESCAN)).toEqual([{}]);
  });

  test("says no folders are scanned when none are configured", async () => {
    await openOverview([], { roots: [] });
    expect(screen.getByText(/The scan covers no folders\./)).toBeTruthy();
  });

  test("asking to add a project folder opens its form", async () => {
    const { rendered } = await openOverview([]);
    await rendered.user.click(screen.getByRole("button", { name: "Add a project folder" }));
    const title = services
      .find((service) => service.id === "discovery")
      ?.actions.find((action) => action.id === "add-project")?.title;
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: title })).toBeTruthy();
  });

  test("while a scan runs it reports its progress instead of saying nothing was found", async () => {
    const api = mockApi();
    api.config("discovery", { roots: ["C:/code"] });
    renderApp("/");
    seedStream({
      projects: makeProjectsState([]),
      discovery: makeDiscoveryState({
        scan: {
          running: true,
          directories: 12,
          found: 0,
          current: "C:/code/deep",
          started_at: 1,
          finished_at: null,
          duration_s: null,
        },
      }),
    });
    await settle();
    expect(screen.getByText("Looking for projects…")).toBeTruthy();
    expect(screen.getByText("Scanned 12 folders so far, now in C:/code/deep.")).toBeTruthy();
    expect(screen.getByText(/scanning 12 folders/)).toBeTruthy();
  });

  test("a scan that has not reported a folder yet omits the current location", async () => {
    const api = mockApi();
    api.config("discovery", { roots: [] });
    renderApp("/");
    seedStream({
      projects: makeProjectsState([]),
      discovery: makeDiscoveryState({
        scan: {
          running: true,
          directories: 0,
          found: 0,
          current: null,
          started_at: 1,
          finished_at: null,
          duration_s: null,
        },
      }),
    });
    await settle();
    expect(screen.getByText("Scanned 0 folders so far.")).toBeTruthy();
  });
});

describe("the project cards", () => {
  test("show every project, newest change first, with pinned projects leading", async () => {
    await openOverview([...fleet(), makeProject("pinned", { name: "Pinned", favorite: true })]);
    expect(cards()).toEqual(["Pinned", "Alpha", "Beta", "Gamma"]);
    expect(screen.getByText("4 projects")).toBeTruthy();
  });

  test("link each project's name to its page", async () => {
    await openOverview(fleet());
    expect(card("Beta").getByRole("link", { name: "Beta" }).getAttribute("href")).toBe("/p/beta");
  });

  test("lay out a fully described project from its schema", async () => {
    await openOverview([makeRichProject()]);
    const alpha = card("Alpha");
    const text = screen.getByRole("article", { name: "Alpha" }).textContent ?? "";
    expect(alpha.getByText("Handles payments")).toBeTruthy();
    expect(alpha.getByText("active")).toBeTruthy();
    expect(alpha.getAllByText("failing")).toHaveLength(2);
    expect(alpha.getByText("no manifest")).toBeTruthy();
    expect(alpha.getByText("service")).toBeTruthy();
    expect(alpha.getByText("python")).toBeTruthy();
    expect(alpha.getByText("rust")).toBeTruthy();
    expect(alpha.getByText("30%")).toBeTruthy();
    expect(alpha.getByText("3 of 10 done")).toBeTruthy();
    expect(alpha.getByRole("meter")).toBeTruthy();
    for (const label of ["checks", "CI", "issues", "git", "fork", "standards"]) {
      expect(alpha.getByText(label, { selector: "dt" })).toBeTruthy();
    }
    expect(text).toContain("passing");
    expect(text).toContain("coverage92%");
    expect(text).toContain("dirty3");
    expect(text).toContain("conflict");
    expect(text).toContain("C:/code/alpha");
    expect(text).toContain("workstation");
  });

  test("hide signals that are zero, null or empty", async () => {
    await openOverview([makeRichProject({ fork: null, standards: null })]);
    const alpha = card("Alpha");
    expect(alpha.queryByText("fork", { selector: "dt" })).toBeNull();
    expect(alpha.queryByText("standards", { selector: "dt" })).toBeNull();
    expect(alpha.queryByText("stashes")).toBeNull();
  });

  test("say nothing about the role of a managed project but name an unmanifested one", async () => {
    await openOverview([
      makeProject("m-1", { name: "Managed", role: "managed" }),
      makeProject("l-1", { name: "Loose", role: "unmanifested" }),
    ]);
    expect(card("Managed").queryByText("managed")).toBeNull();
    expect(card("Loose").getByText("no manifest")).toBeTruthy();
  });

  test("list services, deployments and pinned metrics in the body", async () => {
    await openOverview([makeRichProject()]);
    const alpha = card("Alpha");
    expect(alpha.getByText("Services")).toBeTruthy();
    expect(alpha.getByText("running")).toBeTruthy();
    expect(alpha.getByText("stopped")).toBeTruthy();
    expect(alpha.getByText("Deployments")).toBeTruthy();
    expect(alpha.getByText("prod")).toBeTruthy();
    expect(alpha.getByText("Metrics")).toBeTruthy();
    expect(alpha.getByText("Lines")).toBeTruthy();
    expect(alpha.getByText("1,200 loc")).toBeTruthy();
    expect(alpha.getByText("56%")).toBeTruthy();
    expect(alpha.getByText("error")).toBeTruthy();
  });

  test("show only the first four items of a long list and say how many more there are", async () => {
    const many = Array.from({ length: 6 }, (_, index) => ({
      id: `svc-${index}`,
      label: `svc-${index}`,
      state: "stopped" as const,
      address: null,
      url: null,
      machine: "workstation",
    }));
    await openOverview([makeRichProject({ services: many })]);
    const alpha = card("Alpha");
    expect(alpha.getByText("svc-3")).toBeTruthy();
    expect(alpha.queryByText("svc-4")).toBeNull();
    expect(alpha.getByText("and 2 more")).toBeTruthy();
  });

  test("show the first two attention items and no more", async () => {
    await openOverview([makeRichProject()]);
    const alpha = card("Alpha");
    expect(alpha.getByText("CI is red")).toBeTruthy();
    expect(alpha.getByText("Uncommitted work")).toBeTruthy();
    expect(alpha.queryByText("Third message")).toBeNull();
  });

  test("offer Open UI for a running service with an address and Launch when services are only declared", async () => {
    await openOverview([
      makeRichProject({ id: "up", name: "Up" }),
      makeProject("down", {
        name: "Down",
        services: [
          { id: "web", label: "web", state: "stopped", address: null, url: null, machine: "m" },
        ],
      }),
    ]);
    expect(card("Up").getByRole("link", { name: "Open UI" }).getAttribute("href")).toBe(
      "http://127.0.0.1:8000",
    );
    expect(card("Down").getByRole("button", { name: "Launch" })).toBeTruthy();
  });

  test("pin and unpin a project with the star, sending the new favorite state", async () => {
    const { api, rendered } = await openOverview([
      makeProject("alpha", { name: "Alpha", favorite: false }),
      makeProject("beta", { name: "Beta", favorite: true }),
    ]);
    api.action(SET_FAVORITE, { ok: true, message: null });
    const pin = card("Alpha").getByRole("button", { name: "Pin Alpha to the top" });
    expect(pin.getAttribute("aria-pressed")).toBe("false");
    await rendered.user.click(pin);
    const unpin = card("Beta").getByRole("button", { name: "Unpin Beta" });
    expect(unpin.getAttribute("aria-pressed")).toBe("true");
    await rendered.user.click(unpin);
    await waitFor(() => expect(api.sent(SET_FAVORITE)).toHaveLength(2));
    expect(api.sent(SET_FAVORITE)).toEqual([
      { project_id: "alpha", favorite: true },
      { project_id: "beta", favorite: false },
    ]);
  });
});

describe("searching", () => {
  test("typing narrows the cards and records the search in the address", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.type(searchBox(), "rust");
    expect(rendered.location()).toBe("/?q=rust");
    expect(cards()).toEqual(["Beta", "Gamma"]);
    expect(screen.getByText("2 of 3 projects")).toBeTruthy();
  });

  test("a search in the address is applied on arrival", async () => {
    await openOverview(fleet(), { path: "/?q=billing" });
    expect(cards()).toEqual(["Alpha"]);
    expect(searchBox().value).toBe("billing");
  });

  test("the slash key focuses the search box from anywhere on the page", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("/");
    expect(document.activeElement).toBe(searchBox());
    expect(rendered.location()).toBe("/");
  });

  test("a slash typed inside the search box is part of the search", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.type(searchBox(), "a/b");
    expect(rendered.location()).toBe("/?q=a%2Fb");
  });

  test("Escape in the search box clears the search and leaves the box", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?q=alp" });
    const search = searchBox();
    await rendered.user.click(search);
    await rendered.user.keyboard("{Escape}");
    expect(rendered.location()).toBe("/");
    expect(document.activeElement).not.toBe(search);
    expect(cards()).toHaveLength(3);
  });

  test("Escape in an empty search box just leaves it", async () => {
    const { rendered } = await openOverview(fleet());
    const search = searchBox();
    await rendered.user.click(search);
    await rendered.user.keyboard("{Escape}");
    expect(document.activeElement).not.toBe(search);
    expect(rendered.history).toEqual(["/"]);
  });

  test("a search that matches nothing offers to clear the view", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?q=nothing&sort=name&group=kind" });
    expect(screen.getByText("No project matches this view")).toBeTruthy();
    await rendered.user.click(screen.getByRole("button", { name: "Clear search and filters" }));
    expect(rendered.location()).toBe("/?sort=name&group=kind");
    expect(cards()).toHaveLength(3);
  });
});

describe("filtering", () => {
  async function openFilters(rendered: Opened["rendered"]): Promise<ReturnType<typeof within>> {
    await rendered.user.click(screen.getByRole("button", { name: "Filters" }));
    return within(
      await screen
        .findByRole("group", { name: "Status" })
        .then((g) => g.parentElement as HTMLElement),
    );
  }

  test("the popover lists each facet's values found in the fleet", async () => {
    const { rendered } = await openOverview(fleet());
    const panel = await openFilters(rendered);
    const status = within(panel.getByRole("group", { name: "Status" }));
    expect(status.getAllByRole("checkbox")).toHaveLength(2);
    expect(status.getByText("active")).toBeTruthy();
    expect(status.getByText("paused")).toBeTruthy();
    expect(within(panel.getByRole("group", { name: "Kind" })).getByText("library")).toBeTruthy();
    expect(
      within(panel.getByRole("group", { name: "Language" })).getAllByRole("checkbox"),
    ).toHaveLength(2);
    expect(within(panel.getByRole("group", { name: "Tag" })).getAllByRole("checkbox")).toHaveLength(
      2,
    );
    expect(
      within(panel.getByRole("group", { name: "Only" })).getAllByRole("checkbox"),
    ).toHaveLength(5);
  });

  test("a facet with no values in the fleet is left out", async () => {
    const { rendered } = await openOverview([makeProject("a", { tags: [], kind: null })]);
    await rendered.user.click(screen.getByRole("button", { name: "Filters" }));
    await screen.findByRole("group", { name: "Status" });
    expect(screen.queryByRole("group", { name: "Tag" })).toBeNull();
    expect(screen.queryByRole("group", { name: "Kind" })).toBeNull();
  });

  test("ticking a value filters the cards, shows a chip and counts the active filters", async () => {
    const { rendered } = await openOverview(fleet());
    const panel = await openFilters(rendered);
    await rendered.user.click(
      within(panel.getByRole("group", { name: "Status" })).getByRole("checkbox", {
        name: "paused",
      }),
    );
    expect(rendered.location()).toBe("/?status=paused");
    expect(cards()).toEqual(["Beta"]);
    expect(screen.getByRole("button", { name: /status: paused/ })).toBeTruthy();
    expect(within(screen.getByRole("button", { name: "Filters" })).getByText("1")).toBeTruthy();
  });

  test("unticking removes the value again", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?language=python,rust" });
    const panel = await openFilters(rendered);
    await rendered.user.click(
      within(panel.getByRole("group", { name: "Language" })).getByRole("checkbox", {
        name: "rust",
      }),
    );
    expect(rendered.location()).toBe("/?language=python");
  });

  test("the Only flags each narrow the view and show as chips", async () => {
    const { rendered } = await openOverview(fleet());
    const panel = await openFilters(rendered);
    const only = within(panel.getByRole("group", { name: "Only" }));
    await rendered.user.click(only.getByRole("checkbox", { name: "Failing" }));
    expect(rendered.location()).toBe("/?failing=1");
    expect(cards()).toEqual(["Beta"]);
    await rendered.user.click(only.getByRole("checkbox", { name: "Failing" }));
    expect(rendered.location()).toBe("/");
    await rendered.user.click(only.getByRole("checkbox", { name: "Needs attention" }));
    expect(rendered.location()).toBe("/?attention=1");
    expect(screen.getByRole("button", { name: /needs attention/ })).toBeTruthy();
  });

  test("removing a chip drops that one filter and keeps the others", async () => {
    const { rendered } = await openOverview(fleet(), {
      path: "/?status=active&tag=billing&fork=1",
    });
    expect(screen.getByRole("button", { name: /^forks/ })).toBeTruthy();
    await rendered.user.click(screen.getByRole("button", { name: /tag: billing/ }));
    expect(rendered.location()).toBe("/?status=active&fork=1");
    await rendered.user.click(screen.getByRole("button", { name: /^forks/ }));
    expect(rendered.location()).toBe("/?status=active");
  });

  test("Clear filters empties every filter but keeps the sort and grouping", async () => {
    const { rendered } = await openOverview(fleet(), {
      path: "/?status=active&q=al&failing=1&sort=name&group=kind",
    });
    await rendered.user.click(screen.getByRole("button", { name: "Filters" }));
    await rendered.user.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(rendered.location()).toBe("/?sort=name&group=kind");
  });

  test("Clear filters is disabled when nothing is filtered", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?sort=name" });
    await rendered.user.click(screen.getByRole("button", { name: "Filters" }));
    const clear = (await screen.findByRole("button", {
      name: "Clear filters",
    })) as HTMLButtonElement;
    expect(clear.disabled).toBe(true);
  });
});

describe("sorting and grouping", () => {
  test("choosing a sort order reorders the cards and records it in the address", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.click(screen.getByRole("combobox", { name: "Sort by" }));
    await rendered.user.click(await screen.findByRole("option", { name: "Name" }));
    expect(rendered.location()).toBe("/?sort=name");
    expect(cards()).toEqual(["Alpha", "Beta", "Gamma"]);
    await rendered.user.click(screen.getByRole("combobox", { name: "Sort by" }));
    await rendered.user.click(await screen.findByRole("option", { name: "Health" }));
    expect(cards()).toEqual(["Beta", "Gamma", "Alpha"]);
  });

  test("grouping by status shows a headed section per status with its count", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.click(screen.getByRole("combobox", { name: "Group by" }));
    await rendered.user.click(await screen.findByRole("option", { name: "Status" }));
    expect(rendered.location()).toBe("/?group=status");
    const sections = screen.getAllByRole("region");
    const active = within(screen.getByRole("region", { name: "active" }));
    expect(active.getByRole("heading").textContent).toBe("active2");
    expect(
      within(screen.getByRole("region", { name: "paused" })).getByRole("article", { name: "Beta" }),
    ).toBeTruthy();
    expect(sections.length).toBeGreaterThanOrEqual(2);
  });

  test("the sort and group in the address are reflected in the selectors", async () => {
    await openOverview(fleet(), { path: "/?sort=issues&group=root" });
    expect(screen.getByRole("combobox", { name: "Sort by" }).textContent).toBe("Open issues");
    expect(screen.getByRole("combobox", { name: "Group by" }).textContent).toBe("Root folder");
  });

  test("grouping by root uses the discovery roots from the configuration", async () => {
    await openOverview(
      [
        makeProject("alpha", { name: "Alpha", path: "C:/code/alpha" }),
        makeProject("other", { name: "Other", path: "D:/misc/other" }),
      ],
      { path: "/?group=root", roots: ["C:/code"] },
    );
    await waitFor(() => expect(screen.getByRole("region", { name: "C:/code" })).toBeTruthy());
    expect(screen.getByRole("region", { name: "D:/misc" })).toBeTruthy();
  });
});

describe("the table layout", () => {
  async function openTable(projects = fleet(), path = "/"): Promise<Opened> {
    usePreferences.setState({ overview: "table" });
    return openOverview(projects, { path });
  }

  test("shows one row per project under the schema's default columns", async () => {
    await openTable();
    const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
    expect(headers).toEqual([
      "project",
      "status",
      "health",
      "changed",
      "V1",
      "checks",
      "CI",
      "issues",
      "git",
      "Services",
      "standards",
      "agent",
      "actions",
    ]);
    expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(screen.queryAllByRole("article")).toHaveLength(0);
  });

  test("names each project with a link and its summary, and marks pinned ones", async () => {
    await openTable([makeProject("alpha", { name: "Alpha", favorite: true, summary: "Payments" })]);
    const row = within(rowAt(1));
    expect(row.getByRole("link", { name: "Alpha" }).getAttribute("href")).toBe("/p/alpha");
    expect(row.getByText("Payments")).toBeTruthy();
    expect(row.getByLabelText("Pinned")).toBeTruthy();
  });

  test("does not mark unpinned projects and omits a missing summary", async () => {
    await openTable([makeProject("alpha", { name: "Alpha", favorite: false, summary: null })]);
    const row = within(rowAt(1));
    expect(row.queryByLabelText("Pinned")).toBeNull();
  });

  test("has quick actions on every row", async () => {
    await openTable();
    expect(screen.getAllByRole("button", { name: /More actions for/ })).toHaveLength(3);
  });

  test("shows group header rows with their counts when grouped", async () => {
    await openTable(fleet(), "/?group=status");
    const groups = screen.getAllByRole("columnheader", { name: /^(active|paused)/ });
    expect(groups.map((header) => header.textContent)).toEqual(["active 2", "paused 1"]);
  });

  test("the layout toggle switches between cards and the table and remembers the choice", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.click(screen.getByRole("radio", { name: "Table" }));
    expect(usePreferences.getState().overview).toBe("table");
    expect(screen.getAllByRole("columnheader").length).toBeGreaterThan(0);
    await rendered.user.click(screen.getByRole("radio", { name: "Cards" }));
    expect(usePreferences.getState().overview).toBe("grid");
    expect(cards()).toHaveLength(3);
  });

  test("the column picker appears only in table mode", async () => {
    await openOverview(fleet());
    expect(screen.queryByRole("button", { name: "Columns" })).toBeNull();
    await act(async () => usePreferences.getState().setOverview("table"));
    expect(screen.getByRole("button", { name: "Columns" })).toBeTruthy();
  });

  test("unticking a column hides it and ticking another adds it", async () => {
    const { rendered } = await openTable();
    await rendered.user.click(screen.getByRole("button", { name: "Columns" }));
    await rendered.user.click(await screen.findByRole("menuitem", { name: "agent" }));
    await rendered.user.click(screen.getByRole("menuitem", { name: "kind" }));
    expect(usePreferences.getState().tableColumns).toEqual([
      "name",
      "status",
      "health",
      "last_change",
      "v1",
      "checks",
      "ci",
      "issues",
      "git",
      "services",
      "standards",
      "kind",
    ]);
    await rendered.user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
    expect(headers).not.toContain("agent");
    expect(headers).toContain("kind");
  });

  test("the picker never offers the name column or hidden fields", async () => {
    const { rendered } = await openTable();
    await rendered.user.click(screen.getByRole("button", { name: "Columns" }));
    await screen.findByRole("menuitem", { name: "agent" });
    expect(screen.queryByRole("menuitem", { name: "name" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "favorite" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "attention" })).toBeNull();
  });

  test("Reset to default restores the schema's own columns", async () => {
    const { rendered } = await openTable();
    usePreferences.setState({ tableColumns: ["status"] });
    await settle();
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
    await rendered.user.click(screen.getByRole("button", { name: "Columns" }));
    await rendered.user.click(await screen.findByRole("menuitem", { name: "Reset to default" }));
    expect(usePreferences.getState().tableColumns).toBeNull();
    expect(screen.getAllByRole("columnheader")).toHaveLength(13);
  });
});

describe("density", () => {
  test("Dense and Roomy switch the density preference", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.click(screen.getByRole("radio", { name: "Dense" }));
    expect(usePreferences.getState().density).toBe("compact");
    await rendered.user.click(screen.getByRole("radio", { name: "Roomy" }));
    expect(usePreferences.getState().density).toBe("comfortable");
  });
});

describe("saved views", () => {
  async function openViews(rendered: Opened["rendered"]): Promise<void> {
    await rendered.user.click(screen.getByRole("button", { name: "Views" }));
    await screen.findByRole("menu");
  }

  test("say so when none are saved yet", async () => {
    const { rendered } = await openOverview(fleet());
    await openViews(rendered);
    expect(screen.getByText(/None yet\./)).toBeTruthy();
  });

  test("save the current filters under a name and list them afterwards", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?status=paused&sort=name" });
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("menuitem", { name: "Save current view…" }));
    await rendered.user.type(screen.getByPlaceholderText("View name"), "  Parked  {Enter}");
    expect(usePreferences.getState().savedViews).toEqual([
      { name: "Parked", query: "?status=paused&sort=name" },
    ]);
    expect(screen.queryByPlaceholderText("View name")).toBeNull();
    expect(screen.getByRole("menuitem", { name: /Parked/ })).toBeTruthy();
  });

  test("a blank name saves nothing", async () => {
    const { rendered } = await openOverview(fleet());
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("menuitem", { name: "Save current view…" }));
    await rendered.user.type(screen.getByPlaceholderText("View name"), "   {Enter}");
    expect(usePreferences.getState().savedViews).toEqual([]);
    expect(screen.getByPlaceholderText("View name")).toBeTruthy();
  });

  test("the Save button saves the typed name", async () => {
    const { rendered } = await openOverview(fleet(), { path: "/?failing=1" });
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("menuitem", { name: "Save current view…" }));
    await rendered.user.type(screen.getByPlaceholderText("View name"), "Red");
    await rendered.user.click(screen.getByRole("button", { name: "Save" }));
    expect(usePreferences.getState().savedViews).toEqual([{ name: "Red", query: "?failing=1" }]);
  });

  test("choosing a saved view applies its filters", async () => {
    usePreferences.setState({
      savedViews: [{ name: "Parked", query: "?status=paused&sort=name" }],
    });
    const { rendered } = await openOverview(fleet());
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("menuitem", { name: /Parked/ }));
    expect(rendered.location()).toBe("/?status=paused&sort=name");
    expect(cards()).toEqual(["Beta"]);
  });

  test("deleting a saved view removes it without applying it", async () => {
    usePreferences.setState({
      savedViews: [
        { name: "Parked", query: "?status=paused" },
        { name: "Red", query: "?failing=1" },
      ],
    });
    const { rendered } = await openOverview(fleet());
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("button", { name: "Delete view Parked" }));
    expect(usePreferences.getState().savedViews).toEqual([{ name: "Red", query: "?failing=1" }]);
    expect(rendered.location()).toBe("/");
  });

  test("closing the menu mid-name forgets the half-typed name", async () => {
    const { rendered } = await openOverview(fleet());
    await openViews(rendered);
    await rendered.user.click(screen.getByRole("menuitem", { name: "Save current view…" }));
    await rendered.user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await openViews(rendered);
    expect(screen.getByRole("menuitem", { name: "Save current view…" })).toBeTruthy();
  });
});

describe("keyboard travel", () => {
  test("j and k move between cards in display order and focus them", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("j");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
    await rendered.user.keyboard("j");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Beta" }));
    await rendered.user.keyboard("k");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
    expect(screen.getByRole("article", { name: "Alpha" }).hasAttribute("data-selected")).toBe(true);
  });

  test("the arrow keys travel too, and the horizontal ones only on cards", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
    await rendered.user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Beta" }));
    await rendered.user.keyboard("{ArrowLeft}");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
    await rendered.user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
  });

  test("travel stops at the first and last project", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("jjjjj");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Gamma" }));
    await rendered.user.keyboard("kkkkk");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Alpha" }));
  });

  test("table rows are travelled the same way but sideways arrows do nothing", async () => {
    usePreferences.setState({ overview: "table" });
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("j");
    const first = rowAt(1);
    expect(document.activeElement).toBe(first);
    expect(first.hasAttribute("data-selected")).toBe(true);
    await rendered.user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(first);
    await rendered.user.keyboard("{ArrowLeft}");
    expect(document.activeElement).toBe(first);
    await rendered.user.keyboard("j");
    expect(document.activeElement).toBe(rowAt(2));
  });

  test("Enter opens the selected project", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("jj{Enter}");
    expect(rendered.location()).toBe("/p/beta");
  });

  test("Enter with nothing selected does nothing", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("{Enter}");
    expect(rendered.history).toEqual(["/"]);
  });

  test("f toggles the selected project's pin", async () => {
    const { api, rendered } = await openOverview(fleet());
    api.action(SET_FAVORITE, { ok: true, message: null });
    await rendered.user.keyboard("jf");
    await waitFor(() => expect(api.sent(SET_FAVORITE)).toHaveLength(1));
    expect(api.sent(SET_FAVORITE)).toEqual([{ project_id: "alpha", favorite: true }]);
  });

  test("f with nothing selected does nothing", async () => {
    const { api, rendered } = await openOverview(fleet());
    await rendered.user.keyboard("f");
    expect(api.callsTo("POST", SET_FAVORITE)).toEqual([]);
  });

  test("shortcuts are ignored while typing in a field", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.type(searchBox(), "jf");
    expect(rendered.location()).toBe("/?q=jf");
    expect(document.activeElement).toBe(searchBox());
  });

  test("shortcuts are ignored while a dialog is open", async () => {
    const { rendered } = await openOverview([]);
    await rendered.user.click(screen.getByRole("button", { name: "Add a project folder" }));
    await screen.findByRole("dialog");
    await rendered.user.keyboard("/");
    const search = screen.getByPlaceholderText("Search name, path, tag, language");
    expect(document.activeElement).not.toBe(search);
  });

  test("modifier combinations are left to the browser", async () => {
    const { rendered } = await openOverview(fleet());
    await rendered.user.keyboard("{Control>}j{/Control}");
    await rendered.user.keyboard("{Alt>}j{/Alt}");
    expect(document.activeElement).toBe(document.body);
  });

  test("keys do nothing when no project is shown", async () => {
    const { rendered } = await openOverview([]);
    await rendered.user.keyboard("jk{Enter}");
    expect(rendered.history).toEqual(["/"]);
  });

  test("the first card is selected when it receives focus another way", async () => {
    const { rendered } = await openOverview(fleet());
    await act(async () => screen.getByRole("article", { name: "Gamma" }).focus());
    await rendered.user.keyboard("k");
    expect(document.activeElement).toBe(screen.getByRole("article", { name: "Beta" }));
  });
});

describe("the count line", () => {
  test("says how many projects are hidden by the viewer", async () => {
    const api = mockApi();
    api.config("discovery", { roots: [] });
    renderApp("/");
    seedStream({
      projects: makeProjectsState(fleet(), { hidden: ["x", "y"] }),
      discovery: makeDiscoveryState(),
    });
    await settle();
    expect(screen.getByText("3 projects · 2 hidden")).toBeTruthy();
  });

  test("says how many of the total match while a filter is on", async () => {
    await openOverview(fleet(), { path: "/?status=active" });
    expect(screen.getByText("2 of 3 projects")).toBeTruthy();
  });

  test("mentions an ongoing scan alongside the count", async () => {
    const api = mockApi();
    api.config("discovery", { roots: [] });
    renderApp("/");
    seedStream({
      projects: makeProjectsState(fleet()),
      discovery: makeDiscoveryState({
        scan: {
          running: true,
          directories: 40,
          found: 3,
          current: null,
          started_at: 1,
          finished_at: null,
          duration_s: null,
        },
      }),
    });
    await settle();
    expect(screen.getByText("3 projects · scanning 40 folders")).toBeTruthy();
  });
});
