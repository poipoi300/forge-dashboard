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
  else if (Math.round(seconds / MINUTE) < 60) text = `${Math.round(seconds / MINUTE)} min`;
  // Past the minute rounding, anything under two hours is "1 h" (3570 s included).
  else if (seconds < DAY) text = `${Math.max(1, Math.floor(seconds / HOUR))} h`;
  else if (seconds < DAY * 14) text = `${Math.floor(seconds / DAY)} d`;
  else if (seconds < DAY * 60) text = `${Math.floor(seconds / (DAY * 7))} wk`;
  else text = `${Math.floor(seconds / (DAY * 30))} mo`;
  if (seconds < 45) return text;
  return future ? `in ${text}` : `${text} ago`;
}

/** A compact duration: "850 ms", "42 s", "4 min 12 s", "1 h 02 min". */
export function duration(seconds: number): string {
  // Round to the shown precision first, then split, so no part ever reads "60".
  if (Math.round(seconds * 1000) < 1000) return `${Math.round(seconds * 1000)} ms`;
  const wholeSeconds = Math.round(seconds);
  if (wholeSeconds < MINUTE) return `${wholeSeconds} s`;
  if (wholeSeconds < HOUR) {
    const minutes = Math.floor(wholeSeconds / MINUTE);
    const rest = wholeSeconds % MINUTE;
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const wholeMinutes = Math.round(seconds / MINUTE);
  const hours = Math.floor(wholeMinutes / 60);
  return `${hours} h ${String(wholeMinutes % 60).padStart(2, "0")} min`;
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
