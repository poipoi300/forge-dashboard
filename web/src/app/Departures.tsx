// The departures board: the live clock, the fleet's headline counts, the next
// scheduled jobs, and the jobs boarding now. It sits on the board ground in
// both themes and is the dashboard's signature.
import { Link } from "wouter";
import { useServiceState, useStream } from "@/api/stream";
import { clockTime, relative, useNow } from "@/lib/time";
import { cn } from "@/lib/utils";

/** A station clock face whose red second disc steps on the shared tick; hollow when the stream is down. */
export function StationClock({ live }: { live: boolean }) {
  const now = useNow();
  // Whole seconds since the epoch, not getSeconds(): the angle never wraps from 354 to 0,
  // so the eased step at the top of the minute moves forward like every other second.
  const angle = Math.floor(now / 1000) * 6;
  return (
    <svg
      viewBox="0 0 32 32"
      className="size-8 shrink-0"
      role="img"
      aria-label={live ? "Live" : "Disconnected"}
    >
      <circle cx="16" cy="16" r="15" fill="var(--board-2)" stroke="var(--board-rule)" />
      {Array.from({ length: 12 }, (_, hour) => (
        <line
          // biome-ignore lint/suspicious/noArrayIndexKey: twelve fixed hour marks
          key={hour}
          x1="16"
          y1="3"
          x2="16"
          y2={hour % 3 === 0 ? 6.5 : 5}
          stroke="var(--board-ink-2)"
          strokeWidth={hour % 3 === 0 ? 1.6 : 1}
          transform={`rotate(${hour * 30} 16 16)`}
        />
      ))}
      <g
        transform={`rotate(${angle} 16 16)`}
        className={
          live
            ? "motion-safe:transition-transform motion-safe:duration-[120ms] motion-safe:ease-out"
            : undefined
        }
      >
        <line x1="16" y1="18.5" x2="16" y2="8" stroke="var(--signal)" strokeWidth="1.2" />
        <circle
          cx="16"
          cy="8"
          r="2.3"
          fill={live ? "var(--signal)" : "none"}
          stroke="var(--signal)"
          strokeWidth="1"
        />
      </g>
      <circle cx="16" cy="16" r="1.2" fill="var(--board-ink)" />
    </svg>
  );
}

type CounterTone = "plain" | "warn" | "bad";

const COUNTER_TONE: Record<CounterTone, string> = {
  plain: "text-board-ink",
  warn: "text-board-amber",
  bad: "text-board-signal",
};

function Counter({
  value,
  label,
  href,
  tone = "plain",
}: {
  value: number | undefined;
  label: string;
  href: string;
  tone?: CounterTone;
}) {
  const hot = tone !== "plain" && (value ?? 0) > 0;
  return (
    <Link
      href={href}
      className="group flex min-w-0 flex-col rounded-sm px-1 py-0.5 hover:bg-board-2 focus-visible:outline-offset-0"
    >
      <span
        className={cn(
          "figures-open text-[1.75rem] leading-8 font-semibold",
          hot ? COUNTER_TONE[tone] : "text-board-ink",
          value === undefined && "text-board-ink-2",
        )}
      >
        {value ?? "–"}
      </span>
      <span className={cn("text-2xs leading-tight", hot ? "text-board-ink" : "text-board-ink-2")}>
        {label}
        {hot ? <span className="sr-only"> (needs attention)</span> : null}
      </span>
    </Link>
  );
}

/** Minutes a scheduled departure is overdue, the board's "+n"; null when it is on time. */
export function lateMinutes(dueAt: number | null | undefined, nowMs: number): number | null {
  if (dueAt === null || dueAt === undefined) return null;
  const minutes = Math.floor((nowMs / 1000 - dueAt) / 60);
  return minutes >= 1 ? minutes : null;
}

function BoardRow({
  time,
  title,
  detail,
  href,
  late,
}: {
  time: string;
  title: string;
  detail?: string | null;
  href: string;
  late?: number | null;
}) {
  return (
    <Link
      href={href}
      className="grid grid-cols-[4.5rem_1fr] items-baseline gap-2 rounded-sm px-1 py-px hover:bg-board-2"
    >
      <span className="figures truncate text-sm font-semibold text-board-ink">{time}</span>
      <span className="truncate text-xs text-board-ink">
        {late ? (
          <span
            className="figures mr-1.5 font-semibold text-board-signal"
            title={`${late} min late`}
          >
            +{late}
          </span>
        ) : null}
        {title}
        {detail ? <span className="text-board-ink-2"> · {detail}</span> : null}
      </span>
    </Link>
  );
}

const ARRIVED: Record<string, string> = {
  succeeded: "arrived",
  failed: "failed",
  cancelled: "cancelled",
  attention: "needs you",
};

export function Departures({ compact = false }: { compact?: boolean }) {
  const now = useNow();
  const connection = useStream((stream) => stream.connection);
  const projects = useServiceState("projects");
  const scheduler = useServiceState("scheduler");
  const jobs = useServiceState("jobs");
  const totals = projects?.totals;
  const next = (scheduler?.queue ?? []).slice(0, compact ? 1 : 3);
  const allJobs = jobs?.jobs ?? [];
  const boarding = allJobs.filter((job) => job.status === "running");
  const arrivals = allJobs
    .filter((job) => job.ended_at !== null && job.status in ARRIVED)
    .sort((a, b) => (b.ended_at ?? 0) - (a.ended_at ?? 0))
    .slice(0, 3);
  const waiting = jobs?.queued ?? 0;
  const live = connection === "live";
  const failing = totals?.failing ?? 0;
  // Red only when a failure is on the list; parked or ready runs alone are amber.
  const attentionBad = (projects?.projects ?? []).some((project) =>
    project.attention.some((item) => item.level === "bad"),
  );

  if (compact) {
    return (
      <div className="flex h-10 items-center gap-4 border-b border-board-rule bg-board px-4 text-board-ink">
        <StationClock live={live} />
        <span className="figures text-lg font-semibold">{clockTime(now)}</span>
        <span className="h-5 border-l border-board-rule" />
        <span className="text-xs text-board-ink-2">
          <span className="figures text-sm font-semibold text-board-ink">
            {totals?.projects ?? "–"}
          </span>{" "}
          projects
        </span>
        <span className={cn("text-xs", failing > 0 ? "text-board-ink" : "text-board-ink-2")}>
          <span
            className={cn(
              "figures text-sm font-semibold",
              failing > 0 ? "text-board-signal" : "text-board-ink",
            )}
          >
            {totals?.failing ?? "–"}
          </span>{" "}
          failing
        </span>
        <span className="text-xs text-board-ink-2">
          <span className="figures text-sm font-semibold text-board-ink">{boarding.length}</span>{" "}
          running
        </span>
        {next[0] ? (
          <span className="hidden min-w-0 truncate text-xs text-board-ink-2 md:inline">
            next{" "}
            <span className="figures font-semibold text-board-ink">
              {next[0].next_run_at ? relative(next[0].next_run_at, now) : ""}
            </span>{" "}
            {next[0].title}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <section
      aria-label="Departures"
      className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-3 border-b border-board-rule bg-board px-5 py-3 text-board-ink lg:grid-cols-[auto_auto_minmax(0,1fr)_minmax(0,1fr)]"
    >
      <div className="flex items-center gap-3">
        <StationClock live={live} />
        <div className="flex flex-col">
          <span className="figures text-[1.75rem] leading-8 font-semibold">{clockTime(now)}</span>
          <span className="text-2xs text-board-ink-2">
            {live ? "live" : connection === "connecting" ? "connecting…" : "offline, retrying"}
          </span>
        </div>
      </div>
      <div className="grid grid-cols-4 gap-1 border-l border-board-rule pl-5">
        <Counter value={totals?.projects} label="projects" href="/" />
        <Counter value={totals?.failing} label="failing" href="/?health=failing" tone="bad" />
        <Counter value={totals?.running_services} label="services up" href="/services" />
        <Counter
          value={totals?.attention}
          label="need attention"
          href="/?attention=1"
          tone={attentionBad ? "bad" : "warn"}
        />
      </div>
      <div className="hidden min-w-0 border-l border-board-rule pl-5 lg:block">
        <h2 className="mb-1 text-2xs text-board-ink-2">Next departures</h2>
        {next.length ? (
          next.map((entry) => (
            <BoardRow
              key={entry.id}
              time={entry.next_run_at ? relative(entry.next_run_at, now) : "manual"}
              title={entry.title}
              detail={entry.project_id}
              href={entry.project_id ? `/p/${entry.project_id}` : "/jobs"}
              late={lateMinutes(entry.next_run_at, now)}
            />
          ))
        ) : (
          <p className="px-1 text-xs text-board-ink-2">Nothing scheduled.</p>
        )}
      </div>
      <div className="hidden min-w-0 border-l border-board-rule pl-5 lg:block">
        <h2 className="mb-1 text-2xs text-board-ink-2">
          {boarding.length ? "Boarding now" : "Recent arrivals"}
          {waiting ? <span className="text-board-ink"> · {waiting} waiting</span> : null}
        </h2>
        {boarding.length ? (
          boarding
            .slice(0, 3)
            .map((job) => (
              <BoardRow
                key={job.id}
                time={
                  job.progress != null
                    ? `${Math.round(job.progress * 100)}%`
                    : job.started_at
                      ? relative(job.started_at, now).replace(" ago", "")
                      : "…"
                }
                title={job.title}
                detail={job.progress_label ?? job.project_id}
                href={`/jobs/${job.id}`}
              />
            ))
        ) : arrivals.length ? (
          arrivals.map((job) => (
            <BoardRow
              key={job.id}
              time={job.ended_at ? clockTime(job.ended_at * 1000).slice(0, 5) : ""}
              title={job.title}
              detail={ARRIVED[job.status]}
              href={`/jobs/${job.id}`}
            />
          ))
        ) : (
          <p className="px-1 text-xs text-board-ink-2">No jobs yet.</p>
        )}
      </div>
    </section>
  );
}
