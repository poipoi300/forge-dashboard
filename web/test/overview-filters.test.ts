import { describe, expect, test } from "bun:test";
import {
  arrange,
  EMPTY_VIEW,
  facetValues,
  isFiltered,
  matches,
  parseView,
  serializeView,
  type ViewState,
} from "@/features/overview/filters";
import { makeProject, type ProjectSummary } from "./factories";

function view(overrides: Partial<ViewState>): ViewState {
  return { ...EMPTY_VIEW, ...overrides };
}

function ids(projects: ProjectSummary[]): string[] {
  return projects.map((project) => project.id);
}

describe("view state in the query string", () => {
  test("an empty search is the default view", () => {
    expect(parseView("")).toEqual(EMPTY_VIEW);
  });

  test("lists, flags, sort, group and text are read from their parameters", () => {
    const parsed = parseView(
      "?q=api&status=active,paused&health=failing&kind=service&language=python,rust&tag=x" +
        "&issues=1&failing=1&fork=1&running=1&attention=1&sort=name&group=kind",
    );
    expect(parsed).toEqual({
      q: "api",
      status: ["active", "paused"],
      health: ["failing"],
      kind: ["service"],
      language: ["python", "rust"],
      tag: ["x"],
      hasIssues: true,
      failing: true,
      fork: true,
      running: true,
      attention: true,
      sort: "name",
      group: "kind",
    });
  });

  test("an unknown sort or group falls back to the defaults", () => {
    const parsed = parseView("?sort=chaos&group=vibes");
    expect(parsed.sort).toBe("changed");
    expect(parsed.group).toBe("none");
  });

  test("empty list items are dropped", () => {
    expect(parseView("?status=active,,paused,").status).toEqual(["active", "paused"]);
  });

  test("serializing then parsing returns the same view", () => {
    const original = view({
      q: "two words",
      status: ["active"],
      tag: ["a", "b"],
      failing: true,
      hasIssues: true,
      sort: "health",
      group: "root",
    });
    expect(parseView(serializeView(original))).toEqual(original);
  });

  test("the default view serializes to nothing so the URL stays clean", () => {
    expect(serializeView(EMPTY_VIEW)).toBe("");
  });

  test("sort and group alone do not count as filtering", () => {
    expect(isFiltered(view({ sort: "name", group: "status" }))).toBe(false);
    expect(isFiltered(view({ q: "x" }))).toBe(true);
    expect(isFiltered(view({ attention: true }))).toBe(true);
  });
});

describe("matching a project against a view", () => {
  const alpha = makeProject("alpha", {
    summary: "Handles payments",
    tags: ["billing"],
    languages: ["python"],
    kind: "service",
    status: "active",
    issues: {
      github: "ok",
      repo: "o/alpha",
      open_issues: 3,
      open_prs: 0,
      top_labels: [],
      newest_issue: null,
      html_url: null,
      milestone_open: null,
      milestone_closed: null,
    },
    fork: {
      is_fork: true,
      upstream: "o/up",
      behind_upstream: 2,
      ahead_of_upstream: 0,
      last_sync: "synced",
      last_sync_at: null,
      next_sync_at: null,
    },
    services: [
      { id: "web", label: "web", state: "running", address: null, url: null, machine: "m" },
    ],
    attention: [{ source: "ci", level: "warn", message: "slow" }],
  });

  test("every search term must appear somewhere in the project's text", () => {
    expect(matches(alpha, view({ q: "payments billing" }))).toBe(true);
    expect(matches(alpha, view({ q: "payments nonsense" }))).toBe(false);
    expect(matches(alpha, view({ q: "C:/CODE/ALPHA" }))).toBe(true);
  });

  test("a facet keeps only projects whose value is selected", () => {
    expect(matches(alpha, view({ status: ["active"] }))).toBe(true);
    expect(matches(alpha, view({ status: ["paused"] }))).toBe(false);
    expect(matches(alpha, view({ health: ["good"] }))).toBe(true);
    expect(matches(alpha, view({ health: ["failing"] }))).toBe(false);
    expect(matches(alpha, view({ kind: ["service"] }))).toBe(true);
    expect(matches(alpha, view({ kind: ["library"] }))).toBe(false);
    expect(matches(alpha, view({ language: ["rust", "python"] }))).toBe(true);
    expect(matches(alpha, view({ language: ["rust"] }))).toBe(false);
    expect(matches(alpha, view({ tag: ["billing"] }))).toBe(true);
    expect(matches(alpha, view({ tag: ["other"] }))).toBe(false);
  });

  test("a project without a kind never matches a kind facet", () => {
    expect(matches(makeProject("bare", { kind: null }), view({ kind: ["service"] }))).toBe(false);
  });

  test("the flags each require their fact", () => {
    expect(matches(alpha, view({ hasIssues: true }))).toBe(true);
    expect(matches(alpha, view({ fork: true }))).toBe(true);
    expect(matches(alpha, view({ running: true }))).toBe(true);
    expect(matches(alpha, view({ attention: true }))).toBe(true);
    expect(matches(alpha, view({ failing: true }))).toBe(false);

    const bare = makeProject("bare");
    expect(matches(bare, view({ hasIssues: true }))).toBe(false);
    expect(matches(bare, view({ fork: true }))).toBe(false);
    expect(matches(bare, view({ running: true }))).toBe(false);
    expect(matches(bare, view({ attention: true }))).toBe(false);
    expect(
      matches(
        makeProject("red", { health: { level: "failing", reasons: [] } }),
        view({ failing: true }),
      ),
    ).toBe(true);
  });

  test("an externally running service counts as running", () => {
    const external = makeProject("ext", {
      services: [
        { id: "x", label: "x", state: "external", address: null, url: null, machine: "m" },
      ],
    });
    expect(matches(external, view({ running: true }))).toBe(true);
    const stopped = makeProject("idle", {
      services: [{ id: "x", label: "x", state: "stopped", address: null, url: null, machine: "m" }],
    });
    expect(matches(stopped, view({ running: true }))).toBe(false);
  });
});

describe("arranging the overview", () => {
  const withVersion = (percent: number): ProjectSummary["v1"] => ({
    done: 1,
    total: 2,
    percent,
    milestones_done: 0,
    milestones_total: 0,
    next_milestone: null,
    target_date: null,
    days_left: null,
    caption: "",
    detail: "",
  });
  const withIssues = (open: number): ProjectSummary["issues"] => ({
    github: "ok",
    repo: null,
    open_issues: open,
    open_prs: null,
    top_labels: [],
    newest_issue: null,
    html_url: null,
    milestone_open: null,
    milestone_closed: null,
  });
  const projects = [
    makeProject("b-old", {
      last_change: { at: 100, source: "commit" },
      v1: withVersion(20),
      issues: withIssues(1),
      health: { level: "warn", reasons: [] },
      status: "paused",
      kind: "library",
      path: "C:/code/b-old",
    }),
    makeProject("a-new", {
      last_change: { at: 900, source: "commit" },
      v1: withVersion(80),
      issues: withIssues(9),
      health: { level: "good", reasons: [] },
      status: "active",
      kind: null,
      path: "C:/code/a-new",
    }),
    makeProject("c-fail", {
      last_change: { at: 500, source: "commit" },
      v1: null,
      issues: null,
      health: { level: "failing", reasons: [] },
      status: "blocked",
      kind: "service",
      path: "D:/elsewhere/c-fail",
    }),
    makeProject("d-pinned", {
      favorite: true,
      last_change: { at: null, source: "unknown" },
      health: { level: "unknown", reasons: [] },
      status: "idea",
      path: "C:/code/d-pinned",
    }),
  ];

  test("projects are ungrouped in one unlabeled group by default, newest change first", () => {
    const groups = arrange(projects, EMPTY_VIEW);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.label).toBe("");
    expect(ids(groups[0]?.projects ?? [])).toEqual(["d-pinned", "a-new", "c-fail", "b-old"]);
  });

  test("favorites lead every sort order", () => {
    for (const sort of ["name", "health", "progress", "issues", "changed"] as const) {
      expect(arrange(projects, view({ sort }))[0]?.projects[0]?.id).toBe("d-pinned");
    }
  });

  test("sorting by name is alphabetical", () => {
    expect(ids(arrange(projects, view({ sort: "name" }))[0]?.projects ?? [])).toEqual([
      "d-pinned",
      "a-new",
      "b-old",
      "c-fail",
    ]);
  });

  test("sorting by health puts failing before warn before unknown before good", () => {
    expect(ids(arrange(projects.slice(0, 3), view({ sort: "health" }))[0]?.projects ?? [])).toEqual(
      ["c-fail", "b-old", "a-new"],
    );
  });

  test("sorting by progress puts the furthest along first and unknown progress last", () => {
    expect(
      ids(arrange(projects.slice(0, 3), view({ sort: "progress" }))[0]?.projects ?? []),
    ).toEqual(["a-new", "b-old", "c-fail"]);
  });

  test("sorting by open issues puts the busiest first and unknown counts last", () => {
    expect(ids(arrange(projects.slice(0, 3), view({ sort: "issues" }))[0]?.projects ?? [])).toEqual(
      ["a-new", "b-old", "c-fail"],
    );
  });

  test("grouping by status follows the lifecycle order, not the alphabet", () => {
    const groups = arrange(projects, view({ group: "status" }));
    expect(groups.map((group) => group.label)).toEqual(["active", "blocked", "paused", "idea"]);
  });

  test("grouping by health follows severity order", () => {
    const groups = arrange(projects, view({ group: "health" }));
    expect(groups.map((group) => group.label)).toEqual(["failing", "warn", "unknown", "good"]);
  });

  test("grouping by kind sorts alphabetically and names projects without a manifest", () => {
    const groups = arrange(projects, view({ group: "kind" }));
    expect(groups.map((group) => group.label)).toEqual(["library", "no manifest", "service"]);
  });

  test("grouping by root uses the longest matching discovery root", () => {
    const groups = arrange(projects, view({ group: "root" }), ["c:/code", "C:\\"]);
    expect(groups.map((group) => group.label)).toEqual(["c:/code", "D:/elsewhere"]);
    expect(ids(groups[0]?.projects ?? [])).toEqual(["d-pinned", "a-new", "b-old"]);
  });

  test("a project outside every root is grouped by its parent folder", () => {
    const groups = arrange(projects, view({ group: "root" }));
    expect(groups.map((group) => group.label)).toEqual(["C:/code", "D:/elsewhere"]);
  });

  test("filtering removes projects before grouping", () => {
    const groups = arrange(projects, view({ group: "status", status: ["active"] }));
    expect(groups).toHaveLength(1);
    expect(ids(groups[0]?.projects ?? [])).toEqual(["a-new"]);
  });
});

describe("facet values", () => {
  const projects = [
    makeProject("a", {
      status: "active",
      kind: "service",
      languages: ["python", "rust"],
      tags: ["x"],
    }),
    makeProject("b", { status: "paused", kind: null, languages: ["python"], tags: ["y", "x"] }),
  ];

  test("each facet lists every distinct value sorted", () => {
    expect(facetValues(projects, "status")).toEqual(["active", "paused"]);
    expect(facetValues(projects, "health")).toEqual(["good"]);
    expect(facetValues(projects, "kind")).toEqual(["service"]);
    expect(facetValues(projects, "language")).toEqual(["python", "rust"]);
    expect(facetValues(projects, "tag")).toEqual(["x", "y"]);
  });
});
