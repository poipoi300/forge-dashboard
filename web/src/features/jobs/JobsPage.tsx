// Jobs: what is running and waiting now, recent history, and one job's live
// log with its result. Long work everywhere in the dashboard lands here.
import { Ban } from "lucide-react";
import { Link } from "wouter";
import type { Schemas } from "@/api/client";
import { useJobLog } from "@/api/logs";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { Meter, RelativeTime } from "@/components/data";
import { LogView } from "@/components/LogView";
import { EmptyState, ErrorState, QueryState, Section } from "@/components/states";
import { duration, useNow } from "@/lib/time";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import { JobStatusMark } from "./JobStatus";

type Job = Schemas["JobSummary"];

function Elapsed({ job }: { job: Job }) {
  const now = useNow();
  if (!job.started_at) return <span className="text-ink-3">waiting</span>;
  const end = job.ended_at ?? now / 1000;
  return <span className="figures">{duration(Math.max(0, end - job.started_at))}</span>;
}

function JobRow({ job, selected }: { job: Job; selected?: boolean }) {
  return (
    <li>
      <Link
        href={`/jobs/${job.id}`}
        className={cn(
          "grid grid-cols-[minmax(0,1fr)_7rem_5rem_6rem] items-center gap-3 px-3 py-1.5 hover:bg-hover",
          selected && "bg-select",
        )}
      >
        <span className="min-w-0">
          <span className="block truncate text-sm">{job.title}</span>
          <span className="block truncate text-2xs text-ink-3">
            {job.project_id ?? "dashboard"} ·{" "}
            {job.waiting_for
              ? `waiting for ${job.waiting_for}`
              : (job.progress_label ?? job.summary ?? job.error ?? job.kind)}
          </span>
        </span>
        <JobStatusMark status={job.status} />
        <span className="text-right text-xs">
          <Elapsed job={job} />
        </span>
        <span className="text-right text-2xs text-ink-3">
          <RelativeTime at={job.created_at} />
        </span>
      </Link>
      {job.status === "running" && job.progress !== null ? (
        <Meter
          value={job.progress}
          label={`${job.title} progress`}
          className="h-0.5 rounded-none"
        />
      ) : null}
    </li>
  );
}

function JobDetail({ jobId }: { jobId: string }) {
  const detail = useAction("/api/services/jobs/actions/get", { job_id: jobId }, { live: true });
  const log = useJobLog(jobId);
  if (detail.error) return <ErrorState error={detail.error} title="This job is not known" />;
  const job = detail.data?.job;
  return (
    <div className="flex min-h-0 flex-col gap-3">
      {job ? (
        <header className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="text-base font-semibold">{job.title}</h2>
            <p className="flex flex-wrap items-center gap-x-3 text-xs text-ink-3">
              <JobStatusMark status={job.status} />
              <span>
                {job.kind}
                {job.project_id ? (
                  <>
                    {" · "}
                    <Link href={`/p/${job.project_id}`} className="hover:underline">
                      {job.project_id}
                    </Link>
                  </>
                ) : null}
              </span>
              <span>
                <Elapsed job={job} />
              </span>
            </p>
            {job.summary ? <p className="mt-1 text-sm">{job.summary}</p> : null}
            {job.error ? <p className="mt-1 text-sm text-signal-ink">{job.error}</p> : null}
          </div>
          {job.status === "running" || job.status === "queued" ? (
            <ActionButton
              service="jobs"
              action="cancel"
              input={{ job_id: job.id }}
              size="xs"
              icon={<Ban />}
              label="Cancel"
            />
          ) : null}
        </header>
      ) : null}
      <LogView
        lines={log.lines}
        label="Job log"
        live={job?.status === "running"}
        className="h-[min(36rem,60vh)]"
      />
      {detail.data?.result ? (
        <details>
          <summary className="cursor-pointer text-xs text-ink-2">Result</summary>
          <pre className="mt-1 max-h-72 overflow-auto rounded-sm bg-surface-sunk px-3 py-2 font-mono text-2xs">
            {JSON.stringify(detail.data.result, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

export function JobsPage({ jobId }: { jobId: string | undefined }) {
  const state = useServiceState("jobs");
  const history = useAction(
    "/api/services/jobs/actions/history",
    { project_id: null, kind: null, limit: 80 },
    { follow: ["jobs"] },
  );
  const active = (state?.jobs ?? []).filter(
    (job) => job.status === "running" || job.status === "queued",
  );
  const done = (history.data?.jobs ?? []).filter(
    (job) => job.status !== "running" && job.status !== "queued",
  );
  return (
    <div className="grid gap-6 px-5 py-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="flex min-w-0 flex-col gap-6">
        <Section
          title="Now"
          description={state ? `${state.running} running · ${state.queued} waiting` : undefined}
        >
          {active.length ? (
            <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
              {active.map((job) => (
                <JobRow key={job.id} job={job} selected={job.id === jobId} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-3">Nothing running.</p>
          )}
        </Section>
        <Section title="Recent">
          <QueryState query={history} rows={8}>
            {() =>
              done.length ? (
                <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
                  {done.map((job) => (
                    <JobRow key={job.id} job={job} selected={job.id === jobId} />
                  ))}
                </ul>
              ) : (
                <EmptyState title="No jobs yet">
                  Checks, syncs, bring-ups, and actions each run as a job with a live log.
                </EmptyState>
              )
            }
          </QueryState>
        </Section>
      </div>
      <div className="min-w-0 xl:sticky xl:top-14 xl:self-start">
        {jobId ? (
          <JobDetail key={jobId} jobId={jobId} />
        ) : (
          <p className="pt-8 text-sm text-ink-3">Select a job for its log.</p>
        )}
      </div>
    </div>
  );
}
