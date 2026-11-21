// Fixture builders and page helpers for the project page and its tabs. Every
// builder returns a complete API object, so a test states only what it is about.
import type { ActionOutput, Schemas } from "@/api/client";
import type { ServiceStates } from "@/api/stream";
import { NOW_SECONDS } from "./constants";
import { makeProject, makeProjectsState, type ProjectSummary } from "./factories";
import { type MockApi, mockApi, type Rendered, renderApp, seedStream, settle } from "./harness";

export type ProjectDetail = Schemas["ProjectDetail"];
export type BranchRun = Schemas["BranchRun"];
export type AgentRunRecord = Schemas["AgentRunRecord"];
export type ForkRun = Schemas["ForkRun"];
export type ForkStatus = Schemas["ForkStatus"];
export type ActionInfo = Schemas["ActionInfo"];
export type JobSummary = Schemas["JobSummary"];
export type ServiceBrief = Schemas["ServiceBrief"];

export const DETAIL_PATH = "/api/services/projects/actions/detail" as const;
export const LAUNCH_GUIDE_PATH = "/api/services/projects/actions/launch-guide" as const;

export function makeDetail(
  project: ProjectSummary,
  overrides: Partial<ProjectDetail> = {},
): ProjectDetail {
  return {
    summary: project,
    manifest: null,
    manifest_issues: [],
    has_manifest_file: false,
    is_git: false,
    links: { github: null, homepage: null, docs: null },
    settings: { hidden: false },
    ...overrides,
  };
}

export function makeManifest(
  project: ProjectSummary,
  overrides: Partial<NonNullable<ProjectDetail["manifest"]>> = {},
): NonNullable<ProjectDetail["manifest"]> {
  return {
    schema: 1,
    project: {
      name: project.name,
      id: project.id,
      summary: project.summary ?? "",
      kind: "service",
      languages: project.languages,
      tags: [],
      default_branch: "main",
      homepage: null,
      docs: null,
    },
    v1: { source: "milestones", target_date: null, github_milestone: null, milestones: [] },
    requirements: null,
    checks: [],
    services: [],
    deployments: [],
    metrics: [],
    actions: [],
    launch: null,
    forge: null,
    ...overrides,
  };
}

export function makeMetricDefinition(
  id: string,
): NonNullable<ProjectDetail["manifest"]>["metrics"][number] {
  return {
    id,
    label: id,
    description: null,
    unit: null,
    display: "number",
    interval_s: 300,
    pinned: false,
    thresholds: null,
    source: { kind: "command", argv: ["true"], cwd: ".", timeout_s: 30 },
  };
}

export function makeServiceBrief(
  id: string,
  state: ServiceBrief["state"],
  overrides: Partial<ServiceBrief> = {},
): ServiceBrief {
  return {
    id,
    label: id,
    state,
    address: null,
    url: null,
    machine: "workstation",
    ...overrides,
  };
}

export function makeActionInfo(id: string, overrides: Partial<ActionInfo> = {}): ActionInfo {
  return {
    id,
    label: id,
    description: null,
    params: null,
    confirm: false,
    danger: false,
    requires_service: null,
    ...overrides,
  };
}

export function makeBranchRun(id: number, overrides: Partial<BranchRun> = {}): BranchRun {
  return {
    id,
    project_id: "alpha",
    kind: "delegate",
    title: `Branch run ${id}`,
    branch: `forge/run-${id}`,
    worktree: `C:/data/worktrees/run-${id}`,
    base_branch: "main",
    base_sha: "0123456789abcdef0123456789abcdef01234567",
    state: "ready",
    created_at: NOW_SECONDS - 7200,
    updated_at: NOW_SECONDS - 3600,
    job_id: null,
    agent_run_ids: [],
    summary: null,
    checks: [],
    files_changed: 0,
    diffstat: "",
    pr_url: null,
    error: null,
    agent_attempts: 0,
    compliance_before: null,
    compliance_after: null,
    still_failing: [],
    deterministic_fixes: [],
    hooks_installed: null,
    ...overrides,
  };
}

export function makeAgentRun(id: string, overrides: Partial<AgentRunRecord> = {}): AgentRunRecord {
  return {
    id,
    job_id: `job-${id}`,
    kind: "bring-up",
    project_id: "alpha",
    started_at: NOW_SECONDS - 600,
    ended_at: null,
    model: null,
    outcome: "running",
    exit_code: null,
    cost_usd: null,
    turns: null,
    duration_ms: null,
    transcript_path: `C:/data/transcripts/${id}.jsonl`,
    ...overrides,
  };
}

export function makeForkRun(id: string, overrides: Partial<ForkRun> = {}): ForkRun {
  return {
    id,
    project_id: "alpha",
    job_id: `job-${id}`,
    state: "synced",
    strategy: "merge",
    upstream_ref: "upstream/main",
    upstream_sha: "abcdef0123456789",
    target_branch: "main",
    target_sha: null,
    branch: null,
    worktree: null,
    started_at: NOW_SECONDS - 1800,
    ended_at: null,
    duration_s: null,
    conflicted_files: [],
    agent_attempts: 0,
    agent_run_ids: [],
    agent_cost_usd: null,
    agent_turns: null,
    checks: [],
    result_sha: null,
    pushed: false,
    detail: null,
    ...overrides,
  };
}

export function makeForkStatus(projectId: string, overrides: Partial<ForkStatus> = {}): ForkStatus {
  return {
    project_id: projectId,
    is_fork: true,
    source: "upstream-remote",
    upstream_url: "https://github.com/upstream/repo.git",
    upstream_branch: "main",
    target_branch: "main",
    behind: 0,
    ahead: 0,
    fetched_at: NOW_SECONDS - 600,
    prediction: null,
    last_run: null,
    next_sync_at: null,
    policy: { strategy: "merge", auto_push: false, conflicts: "agent", max_agent_attempts: 2 },
    error: null,
    ...overrides,
  };
}

export function makeJobSummary(id: string, overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id,
    kind: "check",
    title: `Job ${id}`,
    project_id: "alpha",
    status: "succeeded",
    created_at: NOW_SECONDS - 900,
    started_at: null,
    ended_at: null,
    progress: null,
    progress_label: null,
    summary: null,
    error: null,
    waiting_for: null,
    ...overrides,
  };
}

export function makeSessionsOutput(
  overrides: Partial<ActionOutput<"/api/services/agents/actions/sessions">> = {},
): ActionOutput<"/api/services/agents/actions/sessions"> {
  return { sessions: [], processes: [], ...overrides };
}

/** A promise settled from outside, for holding a response back until the test lets it go. */
export function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/** Answer the detail and the launch guide (the default tab) for a project. */
export function routeProject(
  api: MockApi,
  project: ProjectSummary,
  detail: Partial<ProjectDetail> = {},
): MockApi {
  return api.action(DETAIL_PATH, makeDetail(project, detail)).action(LAUNCH_GUIDE_PATH, {
    source: "readme",
    markdown: "Run it with uv.",
    path: null,
    primary_service: null,
    problem: null,
  });
}

export interface ProjectScenario {
  /** The summary the stream publishes for the project (the page reads it from there). */
  project?: ProjectSummary;
  detail?: Partial<ProjectDetail>;
  /** Other services' live state, published in the same first hello. */
  streams?: Partial<ServiceStates>;
}

export interface OpenedProject {
  api: MockApi;
  rendered: Rendered;
  project: ProjectSummary;
}

/** Render the whole app at a project path, publish the first hello, and let the fetches settle. */
export async function openProjectTab(
  tab: string | null,
  scenario: ProjectScenario = {},
  api: MockApi = mockApi(),
): Promise<OpenedProject> {
  const project = scenario.project ?? makeProject("alpha");
  routeProject(api, project, scenario.detail);
  const rendered = renderApp(tab === null ? "/p/alpha" : `/p/alpha/${tab}`);
  seedStream({ ...scenario.streams, projects: makeProjectsState([project]) });
  await settle();
  return { api, rendered, project };
}
