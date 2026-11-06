// Fixture builders for the fleet pages: forks, standards, services and machine, forge runs.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export type ForkStatus = Schemas["ForkStatus"];
export type ForkRun = Schemas["ForkRun"];
export type ProjectStandards = Schemas["ProjectStandards"];
export type ProjectServiceStatus = Schemas["ProjectServiceStatus"];
export type MachineState = Schemas["MachineState"];
export type ForgeRun = Schemas["ForgeRun"];
export type StepSummary = Schemas["StepSummary"];

export function makeForkPolicy(
  overrides: Partial<Schemas["ForkPolicy"]> = {},
): Schemas["ForkPolicy"] {
  return {
    strategy: "merge",
    auto_push: false,
    conflicts: "agent",
    max_agent_attempts: 2,
    ...overrides,
  };
}

export function makeForkRun(
  id: string,
  projectId: string,
  overrides: Partial<ForkRun> = {},
): ForkRun {
  return {
    id,
    project_id: projectId,
    job_id: `job-${id}`,
    state: "synced",
    strategy: "merge",
    upstream_ref: "upstream/main",
    upstream_sha: "a".repeat(40),
    target_branch: "main",
    target_sha: "b".repeat(40),
    branch: null,
    worktree: null,
    started_at: NOW_SECONDS - 7200,
    ended_at: NOW_SECONDS - 7100,
    duration_s: 100,
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

export function makeFork(projectId: string, overrides: Partial<ForkStatus> = {}): ForkStatus {
  return {
    project_id: projectId,
    is_fork: true,
    source: "github",
    upstream_url: `https://github.com/upstream/${projectId}`,
    upstream_branch: "main",
    target_branch: "main",
    behind: 0,
    ahead: 0,
    fetched_at: NOW_SECONDS - 600,
    prediction: null,
    last_run: null,
    next_sync_at: null,
    policy: makeForkPolicy(),
    error: null,
    ...overrides,
  };
}

export function makeComplianceCheck(
  id: string,
  result: Schemas["ComplianceCheck"]["result"],
  title = id,
): Schemas["ComplianceCheck"] {
  return { id, title, result, detail: "", fix: "deterministic", diff: null };
}

export function makeComplianceReport(
  checks: Schemas["ComplianceCheck"][],
): Schemas["ComplianceReport"] {
  return {
    checks,
    passing: checks.filter((check) => check.result === "pass").length,
    total: checks.filter((check) => check.result !== "n/a").length,
    template_revision: null,
    forge_head: null,
  };
}

export function makeProjectStandards(
  projectId: string,
  overrides: Partial<ProjectStandards> = {},
): ProjectStandards {
  return {
    project_id: projectId,
    report: null,
    forge_commits_since: [],
    behind: 0,
    evaluated_at: NOW_SECONDS - 300,
    error: null,
    ...overrides,
  };
}

export function makeStandardsState(
  projects: ProjectStandards[],
  overrides: Partial<Schemas["StandardsState"]> = {},
): Schemas["StandardsState"] {
  return {
    forge_repo: "C:/code/forge",
    forge_head: "0123456789abcdef",
    forge_head_subject: "Tighten the gates",
    projects,
    ...overrides,
  };
}

export function makeServiceStatus(
  projectId: string,
  serviceId: string,
  overrides: Partial<ProjectServiceStatus> = {},
): ProjectServiceStatus {
  return {
    project_id: projectId,
    service_id: serviceId,
    label: serviceId,
    description: null,
    state: "stopped",
    detail: "",
    pid: null,
    host: "127.0.0.1",
    port: null,
    url: null,
    started_at: null,
    restarts: 0,
    machine: "workstation",
    owner: "none",
    external: null,
    blocked_by: null,
    ...overrides,
  };
}

export function makeMachine(overrides: Partial<MachineState> = {}): MachineState {
  return {
    hostname: "workstation",
    operating_system: "Windows 11",
    cpu_count: 16,
    cpu_percent: null,
    ram_used: null,
    ram_total: null,
    gpus: [],
    gpu_error: null,
    disks: [],
    ports: [],
    conflicts: [],
    hardware_sampled_at: NOW_SECONDS - 5,
    ports_sampled_at: NOW_SECONDS - 10,
    ...overrides,
  };
}

export function makeStep(
  stem: string,
  index: number,
  overrides: Partial<StepSummary> = {},
): StepSummary {
  return {
    stem,
    index,
    name: stem,
    ok: true,
    exit_code: 0,
    timed_out: false,
    started_at: NOW_SECONDS - 3600,
    duration_s: 30,
    harness: "claude",
    command: "claude -p",
    workspace: `C:/runs/${stem}`,
    workspace_exists: true,
    stdout_bytes: 10,
    stderr_bytes: 0,
    ...overrides,
  };
}

export function makeForgeRun(
  name: string,
  steps: StepSummary[],
  overrides: Partial<ForgeRun> = {},
): ForgeRun {
  return {
    name,
    root: "C:/runs/eval-1",
    steps,
    unreadable: [],
    total_duration_s: steps.reduce((total, step) => total + step.duration_s, 0),
    last_activity: NOW_SECONDS - 1800,
    ...overrides,
  };
}

export function makeForgeRunsState(
  runs: ForgeRun[],
  overrides: Partial<Schemas["ForgeRunsState"]> = {},
): Schemas["ForgeRunsState"] {
  return {
    forge_repo: "C:/code/forge",
    notice: null,
    roots: [{ path: "C:/runs/eval-1", origin: "forge-toml", exists: true, projects: runs.length }],
    runs,
    refreshed_at: NOW_SECONDS - 60,
    ...overrides,
  };
}

export function makeSchedulerState(paused: boolean): Schemas["SchedulerState"] {
  return { paused, entries: [], queue: [] };
}

export function makeStepOutput(
  lines: string[],
  which: "stdout" | "stderr" = "stdout",
  totalLines = lines.length,
): Schemas["StepOutput"] {
  return { which, lines, offset: 0, total_lines: totalLines, size_bytes: lines.join("\n").length };
}
