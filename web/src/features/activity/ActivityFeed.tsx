// One timeline of everything recorded: commits, CI runs, checks, syncs,
// actions, status changes, issues. Live: a newly recorded event refetches.
import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "wouter";
import { call, type Schemas } from "@/api/client";
import { onEvent } from "@/api/stream";
import { EmptyState, ErrorState, LoadingRows } from "@/components/states";
import { StatusMark } from "@/components/status";
import { Button } from "@/components/ui/button";
import { clockTime } from "@/lib/time";
import { cn } from "@/lib/utils";
import type { Tone } from "@/schema/schema";

type Entry = Schemas["ActivityEntry"];

const SEVERITY_TONE: Record<string, Tone> = {
  info: "neutral",
  success: "good",
  warning: "warn",
  error: "bad",
};

export const ACTIVITY_SOURCES = [
  { id: "git", label: "Git" },
  { id: "github", label: "GitHub" },
  { id: "checks", label: "Checks" },
  { id: "forks", label: "Forks" },
  { id: "standards", label: "Standards" },
  { id: "actions", label: "Actions" },
  { id: "agents", label: "Agents" },
  { id: "services", label: "Services" },
  { id: "projects", label: "Status" },
  { id: "jobs", label: "Jobs" },
] as const;

function dayLabel(ts: number): string {
  const date = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function usePersistedTick(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => onEvent((event) => event.persist && setTick((value) => value + 1)), []);
  return tick;
}

export function ActivityFeed({
  projectId,
  showProject = false,
}: {
  projectId: string | null;
  showProject?: boolean;
}) {
  const [services, setServices] = useState<string[]>([]);
  const tick = usePersistedTick();
  const query = useInfiniteQuery({
    queryKey: ["activity", projectId, services, tick],
    queryFn: ({ pageParam }) =>
      call("/api/services/events/actions/activity", {
        project_id: projectId,
        services,
        types: [],
        before: pageParam?.before ?? null,
        before_id: pageParam?.beforeId ?? null,
        limit: 60,
      }),
    initialPageParam: null as { before: number; beforeId: number } | null,
    getNextPageParam: (page) =>
      page.next_before !== null && page.next_before_id !== null
        ? { before: page.next_before, beforeId: page.next_before_id }
        : undefined,
    placeholderData: keepPreviousData,
  });
  const entries = query.data?.pages.flatMap((page) => page.entries) ?? [];
  const days: { label: string; entries: Entry[] }[] = [];
  for (const entry of entries) {
    const label = dayLabel(entry.ts);
    const last = days[days.length - 1];
    if (last?.label === label) last.entries.push(entry);
    else days.push({ label, entries: [entry] });
  }

  return (
    <div className="flex flex-col gap-3">
      <fieldset className="flex flex-wrap gap-1">
        <legend className="sr-only">Sources</legend>
        {ACTIVITY_SOURCES.map((source) => {
          const on = services.includes(source.id);
          return (
            <button
              key={source.id}
              type="button"
              aria-pressed={on}
              onClick={() =>
                setServices(
                  on ? services.filter((item) => item !== source.id) : [...services, source.id],
                )
              }
              className={cn(
                "rounded-sm border px-1.5 py-0.5 text-2xs",
                on
                  ? "border-ink bg-ink text-surface"
                  : "border-rule-strong text-ink-2 hover:border-ink-3",
              )}
            >
              {source.label}
            </button>
          );
        })}
      </fieldset>
      {query.error ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.isPending ? (
        <LoadingRows rows={6} />
      ) : !entries.length ? (
        <EmptyState title="Nothing recorded yet">
          Commits, CI runs, checks, syncs, and status changes show up here as they happen.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-4">
          {days.map((day) => (
            <section key={day.label} aria-label={day.label}>
              <h3 className="sticky top-20 z-[1] mb-1 bg-paper py-0.5 text-2xs font-semibold text-ink-3">
                {day.label}
              </h3>
              <ol className="flex flex-col">
                {day.entries.map((entry) => (
                  <li
                    key={entry.id}
                    className="grid grid-cols-[3.25rem_minmax(0,1fr)] gap-2 border-l border-rule py-1 pl-3"
                  >
                    <span className="figures text-xs text-ink-3">
                      {clockTime(entry.ts * 1000).slice(0, 5)}
                    </span>
                    <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                      <StatusMark
                        tone={SEVERITY_TONE[entry.severity] ?? "neutral"}
                        strong={entry.severity === "error"}
                      >
                        {entry.title}
                      </StatusMark>
                      <span className="text-2xs text-ink-3">
                        {entry.service}
                        {showProject && entry.project_id ? (
                          <>
                            {" · "}
                            <Link href={`/p/${entry.project_id}`} className="hover:underline">
                              {entry.project_id}
                            </Link>
                          </>
                        ) : null}
                      </span>
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          ))}
          {query.hasNextPage ? (
            <Button
              variant="outline"
              size="sm"
              className="self-start"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              Older
            </Button>
          ) : null}
        </div>
      )}
    </div>
  );
}
