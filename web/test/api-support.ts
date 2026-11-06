// Typed builders for the API payloads the hook and client tests answer with.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export function makeGalleryState(
  overrides: Partial<Schemas["GalleryState"]> = {},
): Schemas["GalleryState"] {
  return {
    updated_at: NOW_SECONDS,
    elapsed_s: 3725,
    size: 5368709120,
    ratio: 87.5,
    progress: 0.6,
    status: "warn",
    commit: "1a2b3c4",
    link: "https://example.com",
    location: "C:/projects/demo",
    notes: "**Markdown** with a [link](https://example.com) and `code`.",
    marker: "active",
    pings: 0,
    ...overrides,
  };
}

export function makeJobLogChunk(
  jobId: string,
  offset: number,
  lines: string[],
  overrides: Partial<Schemas["JobLogChunk"]> = {},
): Schemas["JobLogChunk"] {
  return {
    job_id: jobId,
    offset,
    next_offset: offset + lines.length,
    lines,
    done: false,
    ...overrides,
  };
}

export function makeServiceLogs(
  lines: string[],
  overrides: Partial<Schemas["ServiceLogs"]> = {},
): Schemas["ServiceLogs"] {
  return {
    lines,
    first_offset: 0,
    next_offset: lines.length,
    total: lines.length,
    ...overrides,
  };
}

export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

/** A promise the test settles by hand, to hold a response while the UI is observed mid-load. */
export function deferred<T>(): Deferred<T> {
  const handle: { resolve: ((value: T) => void) | null } = { resolve: null };
  const promise = new Promise<T>((resolve) => {
    handle.resolve = resolve;
  });
  return {
    promise,
    resolve: (value) => {
      if (!handle.resolve) throw new Error("deferred promise has no resolver");
      handle.resolve(value);
    },
  };
}
