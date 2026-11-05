// Query actions through react-query. A live query re-runs whenever its
// owning service's state revision moves, so read-only views follow the
// server without polling and without a second cache to keep in sync.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { type ActionInput, type ActionOutput, type ActionPath, call } from "./client";
import { useStream } from "./stream";

function serviceOf(path: string): string {
  const match = /^\/api\/services\/([^/]+)\/actions\//.exec(path);
  if (!match?.[1]) throw new Error(`not an action path: ${path}`);
  return match[1];
}

export interface QueryOptions {
  enabled?: boolean;
  /** Refetch when the owning service publishes a new state revision. */
  live?: boolean;
  /** Refetch when any of these services publish a new revision. */
  follow?: string[];
  refetchInterval?: number;
}

export function useRevision(services: string[]): string {
  return useStream((store) => services.map((service) => store.states[service]?.rev ?? 0).join("."));
}

export function useAction<P extends ActionPath>(
  path: P,
  input: ActionInput<P>,
  options: QueryOptions = {},
) {
  const followed = options.live
    ? [serviceOf(path), ...(options.follow ?? [])]
    : (options.follow ?? []);
  const revision = useRevision(followed);
  const identity = JSON.stringify([path, input]);
  return useQuery<ActionOutput<P>, Error, ActionOutput<P>, unknown[]>({
    queryKey: [path, input, revision],
    queryFn: () => call(path, input),
    enabled: options.enabled ?? true,
    // Keep showing the last answer while a new revision loads, but never another input's answer.
    placeholderData: (previous, previousQuery) =>
      previousQuery && JSON.stringify(previousQuery.queryKey.slice(0, 2)) === identity
        ? keepPreviousData(previous)
        : undefined,
    refetchInterval: options.refetchInterval,
  });
}
