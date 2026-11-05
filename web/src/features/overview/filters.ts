// Overview filtering, sorting, and grouping as pure functions over summaries,
// with the whole view state round-tripping through the URL query string.
import type { Schemas } from "@/api/client";

export type ProjectSummary = Schemas["ProjectSummary"];

export const SORT_KEYS = ["changed", "name", "health", "progress", "issues"] as const;
export const GROUP_KEYS = ["none", "status", "kind", "health", "root"] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export type GroupKey = (typeof GROUP_KEYS)[number];

export interface ViewState {
  q: string;
  status: string[];
  health: string[];
  kind: string[];
  language: string[];
  tag: string[];
  hasIssues: boolean;
  failing: boolean;
  fork: boolean;
  running: boolean;
  attention: boolean;
  sort: SortKey;
  group: GroupKey;
}

export const EMPTY_VIEW: ViewState = {
  q: "",
  status: [],
  health: [],
  kind: [],
  language: [],
  tag: [],
  hasIssues: false,
  failing: false,
  fork: false,
  running: false,
  attention: false,
  sort: "changed",
  group: "none",
};

const LIST_KEYS = ["status", "health", "kind", "language", "tag"] as const;
const FLAG_KEYS = {
  hasIssues: "issues",
  failing: "failing",
  fork: "fork",
  running: "running",
  attention: "attention",
} as const;

export function parseView(search: string): ViewState {
  const params = new URLSearchParams(search);
  const view: ViewState = { ...EMPTY_VIEW, q: params.get("q") ?? "" };
  for (const key of LIST_KEYS) {
    const raw = params.get(key);
    view[key] = raw ? raw.split(",").filter(Boolean) : [];
  }
  for (const [flag, param] of Object.entries(FLAG_KEYS) as [keyof typeof FLAG_KEYS, string][]) {
    view[flag] = params.get(param) === "1";
  }
  const sort = params.get("sort");
  view.sort = (SORT_KEYS as readonly string[]).includes(sort ?? "") ? (sort as SortKey) : "changed";
  const group = params.get("group");
  view.group = (GROUP_KEYS as readonly string[]).includes(group ?? "")
    ? (group as GroupKey)
    : "none";
  return view;
}

export function serializeView(view: ViewState): string {
  const params = new URLSearchParams();
  if (view.q) params.set("q", view.q);
  for (const key of LIST_KEYS) if (view[key].length) params.set(key, view[key].join(","));
  for (const [flag, param] of Object.entries(FLAG_KEYS) as [keyof typeof FLAG_KEYS, string][]) {
    if (view[flag]) params.set(param, "1");
  }
  if (view.sort !== "changed") params.set("sort", view.sort);
  if (view.group !== "none") params.set("group", view.group);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(view: ViewState): boolean {
  return serializeView({ ...view, sort: "changed", group: "none" }) !== "";
}

function haystack(project: ProjectSummary): string {
  return [
    project.name,
    project.id,
    project.summary ?? "",
    project.path,
    ...project.tags,
    ...project.languages,
    project.kind ?? "",
  ]
    .join(" ")
    .toLowerCase();
}

const runningStates = new Set(["running", "external"]);

export function matches(project: ProjectSummary, view: ViewState): boolean {
  if (view.q) {
    const terms = view.q.toLowerCase().split(/\s+/).filter(Boolean);
    const text = haystack(project);
    if (!terms.every((term) => text.includes(term))) return false;
  }
  if (view.status.length && !view.status.includes(project.status)) return false;
  if (view.health.length && !view.health.includes(project.health.level)) return false;
  if (view.kind.length && !view.kind.includes(project.kind ?? "")) return false;
  if (
    view.language.length &&
    !project.languages.some((language) => view.language.includes(language))
  )
    return false;
  if (view.tag.length && !project.tags.some((tag) => view.tag.includes(tag))) return false;
  if (view.hasIssues && !((project.issues?.open_issues ?? 0) > 0)) return false;
  if (view.failing && project.health.level !== "failing") return false;
  if (view.fork && !project.fork?.is_fork) return false;
  if (view.running && !project.services.some((service) => runningStates.has(service.state)))
    return false;
  if (view.attention && !project.attention.length) return false;
  return true;
}

const HEALTH_RANK: Record<string, number> = { failing: 0, warn: 1, unknown: 2, good: 3 };

function compare(a: ProjectSummary, b: ProjectSummary, sort: SortKey): number {
  switch (sort) {
    case "name":
      return a.name.localeCompare(b.name);
    case "health":
      return (
        (HEALTH_RANK[a.health.level] ?? 9) - (HEALTH_RANK[b.health.level] ?? 9) ||
        a.name.localeCompare(b.name)
      );
    case "progress":
      return (b.v1?.percent ?? -1) - (a.v1?.percent ?? -1) || a.name.localeCompare(b.name);
    case "issues":
      return (
        (b.issues?.open_issues ?? -1) - (a.issues?.open_issues ?? -1) ||
        a.name.localeCompare(b.name)
      );
    case "changed":
      return (b.last_change.at ?? 0) - (a.last_change.at ?? 0) || a.name.localeCompare(b.name);
  }
}

export interface Group {
  key: string;
  label: string;
  projects: ProjectSummary[];
}

function groupKey(project: ProjectSummary, group: GroupKey, roots: string[]): string {
  switch (group) {
    case "status":
      return project.status;
    case "kind":
      return project.kind ?? "no manifest";
    case "health":
      return project.health.level;
    case "root": {
      const path = project.path.replaceAll("\\", "/");
      const root = roots
        .map((item) => item.replaceAll("\\", "/"))
        .filter((item) => path.toLowerCase().startsWith(item.toLowerCase()))
        .sort((x, y) => y.length - x.length)[0];
      return root ?? path.split("/").slice(0, -1).join("/");
    }
    case "none":
      return "";
  }
}

const GROUP_ORDER: Partial<Record<GroupKey, string[]>> = {
  status: ["active", "blocked", "planning", "maintenance", "paused", "idea", "shipped", "archived"],
  health: ["failing", "warn", "unknown", "good"],
};

/** Filter, sort (favourites first within every group), and group the overview. */
export function arrange(
  projects: ProjectSummary[],
  view: ViewState,
  roots: string[] = [],
): Group[] {
  const kept = projects
    .filter((project) => matches(project, view))
    .sort((a, b) => Number(b.favorite) - Number(a.favorite) || compare(a, b, view.sort));
  if (view.group === "none") return [{ key: "", label: "", projects: kept }];
  const groups = new Map<string, ProjectSummary[]>();
  for (const project of kept) {
    const key = groupKey(project, view.group, roots);
    groups.set(key, [...(groups.get(key) ?? []), project]);
  }
  const order = GROUP_ORDER[view.group];
  return [...groups.entries()]
    .sort(([a], [b]) => {
      if (!order) return a.localeCompare(b);
      return (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99);
    })
    .map(([key, list]) => ({ key, label: key, projects: list }));
}

/** Every value present in the fleet for a facet, for filter menus. */
export function facetValues(
  projects: ProjectSummary[],
  facet: "status" | "health" | "kind" | "language" | "tag",
): string[] {
  const values = new Set<string>();
  for (const project of projects) {
    if (facet === "status") values.add(project.status);
    if (facet === "health") values.add(project.health.level);
    if (facet === "kind" && project.kind) values.add(project.kind);
    if (facet === "language") for (const language of project.languages) values.add(language);
    if (facet === "tag") for (const tag of project.tags) values.add(tag);
  }
  return [...values].sort();
}
