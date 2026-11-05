// Live log sources: a job's log is fetched once and then grows from `job.log`
// events; a supervised service's log is read incrementally by offset.
import { useEffect, useState } from "react";
import { call } from "./client";
import { onEvent } from "./stream";

const KEPT_LINES = 20_000;

function append(previous: string[], lines: string[]): string[] {
  const next = previous.concat(lines);
  return next.length > KEPT_LINES ? next.slice(-KEPT_LINES) : next;
}

export interface LogState {
  lines: string[];
  error: unknown;
  loading: boolean;
}

export function useJobLog(jobId: string | null): LogState {
  const [state, setState] = useState<LogState>({ lines: [], error: null, loading: true });
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let offset = 0;
    const pending: { offset: number; lines: string[] }[] = [];
    let loaded = false;
    setState({ lines: [], error: null, loading: true });
    const take = (chunkOffset: number, lines: string[]) => {
      // Events can overlap the initial read; keep only lines past what we have.
      const skip = offset - chunkOffset;
      if (skip >= lines.length) return;
      const fresh = skip > 0 ? lines.slice(skip) : lines;
      offset = Math.max(offset, chunkOffset) + fresh.length;
      setState((current) => ({ ...current, lines: append(current.lines, fresh) }));
    };
    const unsubscribe = onEvent((event) => {
      if (event.type !== "job.log" || event.data.job_id !== jobId) return;
      const chunk = { offset: Number(event.data.offset), lines: event.data.lines as string[] };
      if (loaded) take(chunk.offset, chunk.lines);
      else pending.push(chunk);
    });
    call("/api/services/jobs/actions/log", { job_id: jobId, offset: 0, limit: 50_000 })
      .then((chunk) => {
        if (cancelled) return;
        offset = 0;
        take(chunk.offset, chunk.lines);
        loaded = true;
        for (const item of pending) take(item.offset, item.lines);
        setState((current) => ({ ...current, loading: false }));
      })
      .catch((error: unknown) => !cancelled && setState({ lines: [], error, loading: false }));
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [jobId]);
  return state;
}

export function useServiceLog(
  projectId: string,
  serviceId: string | null,
  intervalMs = 1000,
): LogState {
  const [state, setState] = useState<LogState>({ lines: [], error: null, loading: true });
  useEffect(() => {
    if (!serviceId) return;
    let cancelled = false;
    let offset = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setState({ lines: [], error: null, loading: true });
    const poll = async () => {
      try {
        const chunk = await call("/api/services/services/actions/logs", {
          project_id: projectId,
          service_id: serviceId,
          offset,
        });
        if (cancelled) return;
        // The ring buffer dropped lines we never saw, or the service restarted with a fresh buffer.
        const restarted = chunk.total < offset;
        offset = chunk.next_offset;
        setState((current) => ({
          lines: restarted ? chunk.lines : append(current.lines, chunk.lines),
          error: null,
          loading: false,
        }));
      } catch (error) {
        if (!cancelled) setState((current) => ({ ...current, error, loading: false }));
      }
      if (!cancelled) timer = setTimeout(poll, intervalMs);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [projectId, serviceId, intervalMs]);
  return state;
}
