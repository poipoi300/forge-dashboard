// Typed fixture builders. Every builder returns a complete API object with
// sensible defaults so a test states only the fields it is about.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export type ProjectSummary = Schemas["ProjectSummary"];

export function makeProject(id: string, overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    id,
    name: id,
    summary: `${id} summary`,
    role: "managed",
    kind: "service",
    languages: ["python"],
    tags: [],
    path: `C:/code/${id}`,
    machine: "workstation",
    favorite: false,
    status: "active",
    status_note: null,
    health: { level: "good", reasons: [] },
    last_change: { at: NOW_SECONDS - 3600, source: "commit" },
    v1: null,
    checks: null,
    ci: null,
    issues: null,
    git: null,
    fork: null,
    standards: null,
    services: [],
    deployments: [],
    metrics: [],
    agent: { active: false, last_session_at: null, session_count: 0 },
    manifest: null,
    attention: [],
    ...overrides,
  };
}

export function makeProjectsState(
  projects: ProjectSummary[],
  overrides: Partial<Schemas["ProjectsState"]> = {},
): Schemas["ProjectsState"] {
  return {
    projects,
    totals: {
      projects: projects.length,
      failing: projects.filter((project) => project.health.level === "failing").length,
      attention: projects.filter((project) => project.attention.length > 0).length,
      running_services: 0,
    },
    hidden: [],
    loaded: true,
    ...overrides,
  };
}
