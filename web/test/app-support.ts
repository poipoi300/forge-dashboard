// Fixture builders for the application shell and the overview.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export type ScheduleEntry = Schemas["ScheduleEntry"];

export function makeScheduleEntry(
  id: string,
  overrides: Partial<ScheduleEntry> = {},
): ScheduleEntry {
  return {
    id,
    owner: "forks",
    title: `Scheduled ${id}`,
    project_id: null,
    target_service: "forks",
    target_action: "sync",
    payload: {},
    schedule: { kind: "interval", every_s: 3600 },
    max_jitter_s: 0,
    enabled: true,
    next_run_at: NOW_SECONDS + 600,
    last_run_at: null,
    last_job_id: null,
    last_status: null,
    ...overrides,
  };
}

export function makeSchedulerState(
  queue: ScheduleEntry[],
  overrides: Partial<Schemas["SchedulerState"]> = {},
): Schemas["SchedulerState"] {
  return { paused: false, entries: queue, queue, ...overrides };
}

export function makeDiscoveryState(
  overrides: Partial<Schemas["DiscoveryState"]> = {},
): Schemas["DiscoveryState"] {
  return {
    scan: {
      running: false,
      directories: 0,
      found: 0,
      current: null,
      started_at: null,
      finished_at: null,
      duration_s: null,
    },
    scanned_at: null,
    candidates: [],
    forge_repo: null,
    forge_repo_source: "missing",
    watching: 0,
    ...overrides,
  };
}

type ProjectSummary = Schemas["ProjectSummary"];

/** A project with every fact the card can show, for asserting the whole layout at once. */
export function makeRichProject(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    id: "alpha",
    name: "Alpha",
    summary: "Handles payments",
    role: "unmanifested",
    kind: "service",
    languages: ["python", "rust"],
    tags: ["billing"],
    path: "C:/code/alpha",
    machine: "workstation",
    favorite: true,
    status: "active",
    status_note: null,
    health: { level: "failing", reasons: [] },
    last_change: { at: NOW_SECONDS - 3600, source: "commit" },
    v1: {
      done: 3,
      total: 10,
      percent: 30,
      milestones_done: 1,
      milestones_total: 3,
      next_milestone: "Beta",
      target_date: "2026-03-01",
      days_left: 45,
      caption: "3 of 10 done",
      detail: "Next: ship it",
    },
    checks: {
      result: "passing",
      passed: 4,
      failed: 0,
      total: 4,
      tests_passed: 120,
      tests_failed: 0,
      ran_at: NOW_SECONDS - 3600,
      duration_s: 12,
      sha: "abc1234",
      stale: false,
      coverage_percent: 91.5,
    },
    ci: {
      conclusion: "failure",
      workflows: 2,
      failing_workflows: ["ci"],
      last_run_at: NOW_SECONDS - 7200,
      last_run_url: null,
      open_pr_checks: "pending",
    },
    issues: {
      github: "ok",
      repo: "o/alpha",
      open_issues: 7,
      open_prs: 2,
      top_labels: ["bug"],
      newest_issue: "Crash on start",
      html_url: "https://github.com/o/alpha",
      milestone_open: 1,
      milestone_closed: 2,
    },
    git: {
      branch: "main",
      head_sha: "abc",
      dirty: 3,
      ahead: 1,
      behind: 0,
      unpushed: 2,
      stashes: 0,
      last_commit_at: null,
      last_worktree_change_at: null,
      origin_url: null,
      github_repo: null,
      has_upstream_remote: false,
    },
    fork: {
      is_fork: true,
      upstream: "o/up",
      behind_upstream: 5,
      ahead_of_upstream: 0,
      last_sync: "conflict",
      last_sync_at: null,
      next_sync_at: NOW_SECONDS + 3600,
    },
    standards: { passing: 7, total: 9, failing: ["a"], drift: true, forge_commits_behind: 4 },
    services: [
      {
        id: "web",
        label: "web",
        state: "running",
        address: "127.0.0.1:8000",
        url: "http://127.0.0.1:8000",
        machine: "workstation",
      },
      { id: "db", label: "db", state: "stopped", address: null, url: null, machine: "workstation" },
    ],
    deployments: [
      {
        id: "prod",
        label: "prod",
        url: "https://alpha.example.com",
        machine: null,
        environment: "prod",
        health: "up",
      },
    ],
    metrics: [
      {
        id: "loc",
        label: "Lines",
        display: "number",
        unit: "loc",
        value: 1200,
        status: null,
        history: [1, 2, 3],
        error: null,
      },
      {
        id: "cov",
        label: "Cov",
        display: "percent",
        unit: null,
        value: 55.5,
        status: "warn",
        history: [],
        error: null,
      },
      {
        id: "bad",
        label: "Broken",
        display: "number",
        unit: null,
        value: null,
        status: null,
        history: [],
        error: "boom",
      },
    ],
    agent: { active: true, last_session_at: NOW_SECONDS - 100, session_count: 4 },
    manifest: null,
    attention: [
      { source: "ci", level: "bad", message: "CI is red" },
      { source: "git", level: "warn", message: "Uncommitted work" },
      { source: "x", level: "warn", message: "Third message" },
    ],
    ...overrides,
  };
}
