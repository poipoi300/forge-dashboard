// One shared clock: every relative time and countdown on the page re-renders
// from the same one-second tick, so they all change in the same frame.
import { create } from "zustand";

interface Clock {
  now: number;
}

export const useClock = create<Clock>(() => ({ now: Date.now() }));

let timer: ReturnType<typeof setInterval> | null = null;

export function startClock(): () => void {
  if (timer === null) timer = setInterval(() => useClock.setState({ now: Date.now() }), 1000);
  return () => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };
}

/** Milliseconds since the epoch, updated once a second for every subscriber at once. */
export function useNow(): number {
  return useClock((clock) => clock.now);
}

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** Timetable-style distance between an epoch-seconds instant and now: "12 min", "3 h", "in 5 min". */
export function relative(atSeconds: number, nowMs: number): string {
  const delta = nowMs / 1000 - atSeconds;
  const future = delta < 0;
  const seconds = Math.abs(delta);
  let text: string;
  if (seconds < 45) text = future ? "under a minute" : "just now";
  else if (seconds < HOUR) text = `${Math.round(seconds / MINUTE)} min`;
  else if (seconds < DAY) text = `${Math.floor(seconds / HOUR)} h`;
  else if (seconds < DAY * 14) text = `${Math.floor(seconds / DAY)} d`;
  else if (seconds < DAY * 60) text = `${Math.floor(seconds / (DAY * 7))} wk`;
  else text = `${Math.floor(seconds / (DAY * 30))} mo`;
  if (seconds < 45) return text;
  return future ? `in ${text}` : `${text} ago`;
}

/** A compact duration: "850 ms", "42 s", "4 min 12 s", "1 h 02 min". */
export function duration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < MINUTE) return `${Math.round(seconds)} s`;
  if (seconds < HOUR) {
    const minutes = Math.floor(seconds / MINUTE);
    const rest = Math.round(seconds % MINUTE);
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(seconds / HOUR);
  const minutes = Math.round((seconds % HOUR) / MINUTE);
  return `${hours} h ${String(minutes).padStart(2, "0")} min`;
}

/** Wall-clock "14:07" for the departures board. */
export function clockTime(nowMs: number): string {
  const date = new Date(nowMs);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** Full local timestamp for titles and tooltips. */
export function absolute(atSeconds: number): string {
  return new Date(atSeconds * 1000).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
