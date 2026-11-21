// Fixture builders for the git, issues, launch, metrics, quality and standards tabs.
// Every builder returns a complete API object so a test states only what it is about.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";
import { makeProject, type ProjectSummary } from "./factories";

export type ProjectDetail = Schemas["ProjectDetail"];

export function makeIssueFacts(
  github: Schemas["IssueFacts"]["github"],
  overrides: Partial<Schemas["IssueFacts"]> = {},
): Schemas["IssueFacts"] {
  return {
    github,
    repo: "acme/alpha",
    open_issues: null,
    open_prs: null,
    top_labels: [],
    newest_issue: null,
    html_url: null,
    milestone_open: null,
    milestone_closed: null,
    ...overrides,
  };
}

/** A project whose GitHub facts say the API answered. */
export function makeGithubProject(
  id: string,
  overrides: Partial<ProjectSummary> = {},
): ProjectSummary {
  return makeProject(id, { issues: makeIssueFacts("ok"), ...overrides });
}

export function makeDetail(
  project: ProjectSummary,
  overrides: Partial<ProjectDetail> = {},
): ProjectDetail {
  return {
    summary: project,
    manifest: null,
    manifest_issues: [],
    has_manifest_file: false,
    is_git: true,
    links: { github: null, homepage: null, docs: null },
    settings: { hidden: false },
    ...overrides,
  };
}

export function makeMetricDefinition(
  id: string,
  overrides: Partial<Schemas["Metric"]> = {},
): Schemas["Metric"] {
  return {
    id,
    label: id,
    description: null,
    unit: null,
    display: "number",
    interval_s: 600,
    pinned: false,
    thresholds: null,
    source: { kind: "command", argv: ["true"], cwd: ".", timeout_s: 30 },
    ...overrides,
  };
}

export function makeManifest(
  project: ProjectSummary,
  overrides: Partial<Schemas["ProjectManifest"]> = {},
): Schemas["ProjectManifest"] {
  return {
    schema: 1,
    project: {
      name: project.name,
      id: project.id,
      summary: project.summary ?? "",
      kind: "service",
      languages: [],
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

// ---- git -----------------------------------------------------------------

export function makeRepoState(
  projectId: string,
  overrides: Partial<Schemas["RepoState"]> = {},
): Schemas["RepoState"] {
  return {
    project_id: projectId,
    branch: "main",
    head: "a".repeat(40),
    upstream: null,
    dirty: 0,
    ahead: null,
    behind: null,
    unpushed: 0,
    stashes: 0,
    last_commit_subject: null,
    last_commit_at: null,
    last_worktree_change_at: null,
    remotes: {},
    refreshed_at: NOW_SECONDS,
    error: null,
    ...overrides,
  };
}

export function makeFileChange(
  path: string,
  overrides: Partial<Schemas["FileChange"]> = {},
): Schemas["FileChange"] {
  return { path, kind: "modified", staged: false, original_path: null, ...overrides };
}

export function makeCommit(
  shortSha: string,
  overrides: Partial<Schemas["CommitInfo"]> = {},
): Schemas["CommitInfo"] {
  return {
    sha: shortSha.padEnd(40, "0"),
    short_sha: shortSha,
    author: "Ada",
    committed_at: NOW_SECONDS - 7200,
    subject: `Commit ${shortSha}`,
    files_changed: 1,
    insertions: 1,
    deletions: 0,
    ...overrides,
  };
}

export function makeBranch(
  name: string,
  overrides: Partial<Schemas["BranchInfo"]> = {},
): Schemas["BranchInfo"] {
  return {
    name,
    current: false,
    upstream: null,
    ahead: null,
    behind: null,
    gone: false,
    committed_at: NOW_SECONDS - 3600,
    subject: `Tip of ${name}`,
    ...overrides,
  };
}

export function makeWorktree(
  path: string,
  overrides: Partial<Schemas["WorktreeInfo"]> = {},
): Schemas["WorktreeInfo"] {
  return {
    path,
    branch: null,
    head: null,
    detached: false,
    locked: false,
    prunable: false,
    dashboard_owned: false,
    ...overrides,
  };
}

export function makeStash(
  name: string,
  overrides: Partial<Schemas["StashInfo"]> = {},
): Schemas["StashInfo"] {
  return { name, created_at: NOW_SECONDS - 86400, message: `WIP on ${name}`, ...overrides };
}

export function makeTag(
  name: string,
  overrides: Partial<Schemas["TagInfo"]> = {},
): Schemas["TagInfo"] {
  return { name, created_at: NOW_SECONDS - 5 * 86400, short_sha: "abc1234", ...overrides };
}

/** A minimal unified diff touching one file, so the diff view has something to render. */
export function makeUnifiedDiff(path: string): string {
  return [
    `diff --git a/${path} b/${path}`,
    "index 1111111..2222222 100644",
    `--- a/${path}`,
    `+++ b/${path}`,
    "@@ -1,2 +1,2 @@",
    " unchanged line",
    "-old line",
    "+new line",
    "",
  ].join("\n");
}

// ---- GitHub --------------------------------------------------------------

export function makeIssue(
  number: number,
  overrides: Partial<Schemas["GitHubIssue"]> = {},
): Schemas["GitHubIssue"] {
  return {
    number,
    title: `Issue ${number}`,
    state: "open",
    author: "grace",
    labels: [],
    assignees: [],
    milestone: null,
    comments: 0,
    created_at: NOW_SECONDS - 2 * 86400,
    updated_at: NOW_SECONDS - 3600,
    html_url: `https://github.com/acme/alpha/issues/${number}`,
    ...overrides,
  };
}

export function makeIssueDetail(
  issue: Schemas["GitHubIssue"],
  overrides: Partial<Schemas["GitHubIssueDetail"]> = {},
): Schemas["GitHubIssueDetail"] {
  return { issue, body: "", comments: [], comments_truncated: false, ...overrides };
}

export function makeIssueComment(
  id: number,
  overrides: Partial<Schemas["GitHubComment"]> = {},
): Schemas["GitHubComment"] {
  return {
    id,
    author: "linus",
    body: `Comment ${id}`,
    created_at: NOW_SECONDS - 7200,
    html_url: `https://github.com/acme/alpha/issues/1#issuecomment-${id}`,
    ...overrides,
  };
}

export function makePull(
  number: number,
  overrides: Partial<Schemas["GitHubPull"]> = {},
): Schemas["GitHubPull"] {
  return {
    number,
    title: `Pull ${number}`,
    author: "grace",
    draft: false,
    head_branch: `feature-${number}`,
    base_branch: "main",
    head_sha: "b".repeat(40),
    checks: null,
    created_at: NOW_SECONDS - 86400,
    updated_at: NOW_SECONDS - 3600,
    html_url: `https://github.com/acme/alpha/pull/${number}`,
    ...overrides,
  };
}

export function makeRelease(
  id: number,
  overrides: Partial<Schemas["GitHubRelease"]> = {},
): Schemas["GitHubRelease"] {
  return {
    id,
    tag: `v${id}.0.0`,
    name: null,
    draft: false,
    prerelease: false,
    published_at: NOW_SECONDS - 10 * 86400,
    body: null,
    html_url: `https://github.com/acme/alpha/releases/tag/v${id}.0.0`,
    ...overrides,
  };
}

export function makeWorkflowRun(
  id: number,
  overrides: Partial<Schemas["GitHubRun"]> = {},
): Schemas["GitHubRun"] {
  return {
    id,
    workflow: "CI",
    workflow_id: 7,
    number: id,
    attempt: 1,
    event: "push",
    branch: "main",
    sha: "c".repeat(40),
    title: `Run title ${id}`,
    outcome: "success",
    started_at: NOW_SECONDS - 1800,
    duration_s: 95,
    html_url: `https://github.com/acme/alpha/actions/runs/${id}`,
    ...overrides,
  };
}

export function makeWorkflowJob(
  id: number,
  overrides: Partial<Schemas["GitHubJob"]> = {},
): Schemas["GitHubJob"] {
  return {
    id,
    name: `job-${id}`,
    outcome: "success",
    started_at: NOW_SECONDS - 1800,
    duration_s: 60,
    logs_url: null,
    steps: [],
    ...overrides,
  };
}

// ---- checks --------------------------------------------------------------

export function makeCheckRun(
  checkId: string,
  overrides: Partial<Schemas["CheckRunSummary"]> = {},
): Schemas["CheckRunSummary"] {
  return {
    check_id: checkId,
    label: checkId,
    stage: "commit",
    outcome: "passed",
    exit_code: 0,
    tests: null,
    failures: null,
    errors: null,
    skipped: null,
    duration_s: null,
    sha: null,
    dirty: false,
    on_head: true,
    ran_at: NOW_SECONDS - 600,
    stale: false,
    job_id: null,
    ...overrides,
  };
}

export function makeChecksState(
  projectId: string,
  overrides: Partial<Schemas["ProjectChecks"]> = {},
): Schemas["ChecksState"] {
  return {
    projects: [
      {
        project_id: projectId,
        checks: [],
        gates: [],
        gates_error: null,
        coverage: null,
        settings: { on_head: false },
        ...overrides,
      },
    ],
  };
}

export function makeGate(id: string, overrides: Partial<Schemas["Gate"]> = {}): Schemas["Gate"] {
  return { id, name: `Gate ${id}`, stages: ["pre-commit"], ...overrides };
}

export function makeCoverage(
  percent: number,
  overrides: Partial<Schemas["CoverageReport"]> = {},
): Schemas["CoverageReport"] {
  return {
    path: "coverage.xml",
    format: "cobertura",
    percent,
    lines_covered: 80,
    lines_total: 100,
    measured_at: NOW_SECONDS - 1200,
    ...overrides,
  };
}

export function makeHistoryPoint(
  ranAt: number,
  overrides: Partial<Schemas["HistoryPoint"]> = {},
): Schemas["HistoryPoint"] {
  return {
    ran_at: ranAt,
    outcome: "passed",
    duration_s: 12,
    tests: null,
    failures: null,
    sha: null,
    dirty: false,
    ...overrides,
  };
}

export function makeCheckHistory(
  checkId: string,
  points: Schemas["HistoryPoint"][],
  passRate: number | null,
): Schemas["CheckHistory"] {
  return { check_id: checkId, points, pass_rate: passRate };
}

export function makeFailedCase(
  name: string,
  overrides: Partial<Schemas["FailedCase"]> = {},
): Schemas["FailedCase"] {
  return {
    suite: "tests.suite",
    name,
    kind: "failure",
    message: `${name} blew up`,
    detail: "",
    ...overrides,
  };
}

export function makeJunitReport(
  overrides: Partial<Schemas["JUnitReport"]> = {},
): Schemas["JUnitReport"] {
  return {
    tests: 10,
    failures: 0,
    errors: 0,
    skipped: 0,
    duration_s: 3,
    failed: [],
    ...overrides,
  };
}

export function makeLatestReport(
  run: Schemas["CheckRunSummary"],
  overrides: Partial<Schemas["LatestReport"]> = {},
): Schemas["LatestReport"] {
  return { run, report: null, log_tail: [], ...overrides };
}

// ---- services and deployments -------------------------------------------

export function makeServiceStatus(
  projectId: string,
  serviceId: string,
  overrides: Partial<Schemas["ProjectServiceStatus"]> = {},
): Schemas["ProjectServiceStatus"] {
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

export function makeDeploymentStatus(
  projectId: string,
  deploymentId: string,
  overrides: Partial<Schemas["DeploymentStatus"]> = {},
): Schemas["DeploymentStatus"] {
  return {
    project_id: projectId,
    deployment_id: deploymentId,
    label: deploymentId,
    url: `https://${deploymentId}.example.com/`,
    machine: null,
    environment: "production",
    health: "unchecked",
    status_code: null,
    checked_at: null,
    error: null,
    ...overrides,
  };
}

export function makeServicesState(
  services: Schemas["ProjectServiceStatus"][],
  deployments: Schemas["DeploymentStatus"][] = [],
): Schemas["ProjectServicesState"] {
  return { services, deployments };
}

export function makeLaunchGuide(
  overrides: Partial<Schemas["LaunchGuide"]> = {},
): Schemas["LaunchGuide"] {
  return {
    source: "readme",
    markdown: "",
    path: null,
    primary_service: null,
    problem: null,
    ...overrides,
  };
}

export function makeServiceLogs(
  lines: string[],
  overrides: Partial<Schemas["ServiceLogs"]> = {},
): Schemas["ServiceLogs"] {
  return { lines, first_offset: 0, next_offset: lines.length, total: lines.length, ...overrides };
}

/** Answers log reads like the server: only the lines at or past the requested offset. */
export function serviceLogReader(
  linesByService: Record<string, string[]>,
): (input: { service_id: string; offset: number }) => Schemas["ServiceLogs"] {
  return ({ service_id, offset }) => {
    const lines = linesByService[service_id] ?? [];
    return makeServiceLogs(lines.slice(offset), { first_offset: offset });
  };
}

// ---- metrics -------------------------------------------------------------

export function makeReading(
  metricId: string,
  overrides: Partial<Schemas["LatestReading"]> = {},
): Schemas["LatestReading"] {
  return {
    metric_id: metricId,
    label: metricId,
    display: "number",
    unit: null,
    pinned: false,
    value: null,
    status: null,
    detail: null,
    error: null,
    read_at: NOW_SECONDS - 300,
    interval_s: 600,
    ...overrides,
  };
}

export function makeProjectMetrics(
  projectId: string,
  readings: Schemas["LatestReading"][],
): Schemas["ProjectMetrics"] {
  return { project_id: projectId, metrics: readings, interval_overrides: {} };
}

export function makeMetricsState(
  projectId: string,
  readings: Schemas["LatestReading"][],
): Schemas["MetricsState"] {
  return { projects: [makeProjectMetrics(projectId, readings)] };
}

export function makeMetricPoint(
  readAt: number,
  overrides: Partial<Schemas["MetricPoint"]> = {},
): Schemas["MetricPoint"] {
  return { read_at: readAt, value: null, status: null, error: null, ...overrides };
}

// ---- standards -----------------------------------------------------------

export function makeComplianceCheck(
  id: string,
  overrides: Partial<Schemas["ComplianceCheck"]> = {},
): Schemas["ComplianceCheck"] {
  return {
    id,
    title: `Check ${id}`,
    result: "pass",
    detail: "",
    fix: "deterministic",
    diff: null,
    ...overrides,
  };
}

export function makeComplianceReport(
  checks: Schemas["ComplianceCheck"][],
  overrides: Partial<Schemas["ComplianceReport"]> = {},
): Schemas["ComplianceReport"] {
  return {
    checks,
    passing: checks.filter((check) => check.result === "pass").length,
    total: checks.length,
    template_revision: null,
    forge_head: null,
    ...overrides,
  };
}

export function makeProjectStandards(
  projectId: string,
  overrides: Partial<Schemas["ProjectStandards"]> = {},
): Schemas["ProjectStandards"] {
  return {
    project_id: projectId,
    report: null,
    forge_commits_since: [],
    behind: null,
    evaluated_at: NOW_SECONDS - 900,
    error: null,
    ...overrides,
  };
}

export function makeStandardsState(
  projects: Schemas["ProjectStandards"][],
  overrides: Partial<Schemas["StandardsState"]> = {},
): Schemas["StandardsState"] {
  return {
    forge_repo: "C:/code/forge",
    forge_head: "0123456789abcdef0123456789abcdef01234567",
    forge_head_subject: "Tighten the gates",
    projects,
    ...overrides,
  };
}

export function makeBranchRun(
  id: number,
  projectId: string,
  overrides: Partial<Schemas["BranchRun"]> = {},
): Schemas["BranchRun"] {
  return {
    id,
    project_id: projectId,
    kind: "standards",
    title: `Run ${id}`,
    branch: `forge/run-${id}`,
    worktree: `C:/data/worktrees/run-${id}`,
    base_branch: "main",
    base_sha: "d".repeat(40),
    state: "ready",
    created_at: NOW_SECONDS - 7200,
    updated_at: NOW_SECONDS - 3600,
    job_id: null,
    agent_run_ids: [],
    summary: null,
    checks: [],
    files_changed: 2,
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

export function makeGithubState(
  overrides: Partial<Schemas["GitHubState"]> = {},
): Schemas["GitHubState"] {
  return {
    auth_source: "none",
    authenticated: true,
    rate_limit: null,
    rate_remaining: null,
    rate_reset_at: null,
    repos: [],
    ...overrides,
  };
}
