// Fixture builders for the jobs page.
import { waitFor } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export type JobSummary = Schemas["JobSummary"];

/** A job whose timestamps agree with its status: queued has not started, running has not ended. */
export function makeJob(id: string, overrides: Partial<JobSummary> = {}): JobSummary {
  const status = overrides.status ?? "succeeded";
  const started = status !== "queued";
  const ended = status !== "queued" && status !== "running";
  return {
    id,
    kind: "checks.run",
    title: `Job ${id}`,
    project_id: null,
    status,
    created_at: NOW_SECONDS - 600,
    started_at: started ? NOW_SECONDS - 590 : null,
    ended_at: ended ? NOW_SECONDS - 500 : null,
    progress: null,
    progress_label: null,
    summary: null,
    error: null,
    waiting_for: null,
    ...overrides,
  };
}

export function makeJobsState(
  jobs: JobSummary[],
  overrides: Partial<Schemas["JobsState"]> = {},
): Schemas["JobsState"] {
  return {
    jobs,
    running: jobs.filter((job) => job.status === "running").length,
    queued: jobs.filter((job) => job.status === "queued").length,
    ...overrides,
  };
}

export function makeJobDetail(
  job: JobSummary,
  result: Schemas["JobDetail"]["result"] = null,
): Schemas["JobDetail"] {
  return { job, result };
}

export function makeLogChunk(
  jobId: string,
  lines: string[],
  offset = 0,
  done = false,
): Schemas["JobLogChunk"] {
  return { job_id: jobId, offset, next_offset: offset + lines.length, lines, done };
}

/**
 * Wait until a query stops matching. Prefer this over `waitFor(() => expect(query()).toBeNull())`:
 * every failed poll of that form formats the whole DOM node into the error, which takes seconds.
 */
export async function waitUntilGone(query: () => HTMLElement | null): Promise<void> {
  await waitFor(() => {
    if (query() !== null) throw new Error("element is still present");
  });
}
