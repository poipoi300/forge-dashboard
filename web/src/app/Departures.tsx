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
  const seconds = new Date(now).getSeconds();
  const angle = seconds * 6;
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
        style={{ transition: live ? "transform 120ms ease-out" : undefined }}
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

function Counter({
  value,
  label,
  href,
  alert = false,
}: {
  value: number | undefined;
  label: string;
  href: string;
  alert?: boolean;
}) {
  const hot = alert && (value ?? 0) > 0;
  return (
    <Link
      href={href}
      className="group flex min-w-0 flex-col rounded-sm px-1 py-0.5 hover:bg-board-2 focus-visible:outline-offset-0"
    >
      <span
        className={cn(
          "figures-open text-[1.75rem] leading-8 font-semibold",
          hot ? "text-signal" : "text-board-ink",
          value === undefined && "text-board-ink-2",
        )}
      >
        {value ?? "–"}
      </span>
      <span className={cn("truncate text-2xs", hot ? "text-board-ink" : "text-board-ink-2")}>
        {label}
        {hot ? <span className="sr-only"> (needs attention)</span> : null}
      </span>
    </Link>
  );
}

function BoardRow({
  time,
  title,
  detail,
  href,
}: {
  time: string;
  title: string;
  detail?: string | null;
  href: string;
}) {
  return (
    <Link
      href={href}
      className="grid grid-cols-[4.5rem_1fr] items-baseline gap-2 rounded-sm px-1 py-px hover:bg-board-2"
    >
      <span className="figures truncate text-sm font-semibold text-board-ink">{time}</span>
      <span className="truncate text-xs text-board-ink">
        {title}
        {detail ? <span className="text-board-ink-2"> · {detail}</span> : null}
      </span>
    </Link>
  );
}

export function Departures({ compact = false }: { compact?: boolean }) {
  const now = useNow();
  const connection = useStream((stream) => stream.connection);
  const projects = useServiceState("projects");
  const scheduler = useServiceState("scheduler");
  const jobs = useServiceState("jobs");
  const totals = projects?.totals;
  const next = (scheduler?.queue ?? []).slice(0, compact ? 1 : 3);
  const boarding = (jobs?.jobs ?? []).filter((job) => job.status === "running");
  const waiting = jobs?.queued ?? 0;
  const live = connection === "live";

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
        <span
          className={cn(
            "text-xs",
            (totals?.failing ?? 0) > 0 ? "text-board-ink" : "text-board-ink-2",
          )}
        >
          <span
            className={cn(
              "figures text-sm font-semibold",
              (totals?.failing ?? 0) > 0 ? "text-signal" : "text-board-ink",
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
      className="grid grid-cols-1 gap-x-6 gap-y-3 border-b border-board-rule bg-board px-5 py-3 text-board-ink lg:grid-cols-[auto_auto_minmax(0,1fr)_minmax(0,1fr)]"
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
      <div className="grid grid-cols-4 gap-1 border-board-rule lg:border-l lg:pl-5">
        <Counter value={totals?.projects} label="projects" href="/" />
        <Counter value={totals?.failing} label="failing" href="/?health=failing" alert />
        <Counter value={totals?.running_services} label="services up" href="/services" />
        <Counter value={totals?.attention} label="need attention" href="/?attention=1" alert />
      </div>
      <div className="min-w-0 border-board-rule lg:border-l lg:pl-5">
        <h2 className="mb-1 text-2xs text-board-ink-2">Next departures</h2>
        {next.length ? (
          next.map((entry) => (
            <BoardRow
              key={entry.id}
              time={entry.next_run_at ? relative(entry.next_run_at, now) : "manual"}
              title={entry.title}
              detail={entry.project_id}
              href={entry.project_id ? `/p/${entry.project_id}` : "/jobs"}
            />
          ))
        ) : (
          <p className="px-1 text-xs text-board-ink-2">
            Nothing scheduled. Fork syncs and checks can run on a schedule.
          </p>
        )}
      </div>
      <div className="min-w-0 border-board-rule lg:border-l lg:pl-5">
        <h2 className="mb-1 text-2xs text-board-ink-2">
          Boarding now
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
        ) : (
          <p className="px-1 text-xs text-board-ink-2">No jobs running.</p>
        )}
      </div>
    </section>
  );
}
