import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import { act, screen } from "@testing-library/react";
import { absolute, clockTime, duration, relative, startClock, useClock, useNow } from "@/lib/time";
import { installHarness, NOW_MS, NOW_SECONDS, renderWithProviders } from "./harness";

installHarness();
afterEach(() => mock.restore());

const DAY = 86_400;

describe("relative", () => {
  test.each<[number, string]>([
    [NOW_SECONDS, "just now"],
    [NOW_SECONDS - 44, "just now"],
    [NOW_SECONDS + 44, "under a minute"],
    [NOW_SECONDS - 45, "1 min ago"],
    [NOW_SECONDS - 90, "2 min ago"],
    [NOW_SECONDS - 300, "5 min ago"],
    [NOW_SECONDS - 3600, "1 h ago"],
    [NOW_SECONDS - 9000, "2 h ago"],
    [NOW_SECONDS - DAY, "1 d ago"],
    [NOW_SECONDS - 13 * DAY, "13 d ago"],
    [NOW_SECONDS - 14 * DAY, "2 wk ago"],
    [NOW_SECONDS - 59 * DAY, "8 wk ago"],
    [NOW_SECONDS - 60 * DAY, "2 mo ago"],
    [NOW_SECONDS - 400 * DAY, "13 mo ago"],
  ])("describes an instant %d seconds in the past from the frozen clock", (at, expected) => {
    expect(relative(at, NOW_MS)).toBe(expected);
  });

  test.each<[number, string]>([
    [NOW_SECONDS + 45, "in 1 min"],
    [NOW_SECONDS + 300, "in 5 min"],
    [NOW_SECONDS + 3600, "in 1 h"],
    [NOW_SECONDS + 2 * DAY, "in 2 d"],
  ])("describes an instant %d seconds in the future from the frozen clock", (at, expected) => {
    expect(relative(at, NOW_MS)).toBe(expected);
  });
});

describe("duration", () => {
  test.each<[number, string]>([
    [0, "0 ms"],
    [0.85, "850 ms"],
    [1, "1 s"],
    [42.4, "42 s"],
    [60, "1 min"],
    [240, "4 min"],
    [252, "4 min 12 s"],
    [3599, "59 min 59 s"],
    [3600, "1 h 00 min"],
    [3720, "1 h 02 min"],
    [7260, "2 h 01 min"],
  ])("shows %d seconds as %s", (seconds, expected) => {
    expect(duration(seconds)).toBe(expected);
  });
});

describe("clockTime", () => {
  test("shows the local wall-clock hour and minute, zero-padded", () => {
    expect(clockTime(new Date(2026, 0, 15, 9, 5).getTime())).toBe("09:05");
    expect(clockTime(new Date(2026, 0, 15, 14, 7).getTime())).toBe("14:07");
    expect(clockTime(new Date(2026, 0, 15, 0, 0).getTime())).toBe("00:00");
  });
});

describe("absolute", () => {
  test("gives the full local date and time of an instant", () => {
    const text = absolute(new Date(2026, 0, 15, 14, 7).getTime() / 1000);
    expect(text).toContain("Jan 15, 2026");
    expect(text).toContain("02:07");
  });
});

function NowProbe() {
  return <p>{new Date(useNow()).toISOString()}</p>;
}

describe("useNow", () => {
  test("reads the shared clock and follows its updates", () => {
    renderWithProviders(<NowProbe />);
    expect(screen.getByText("2026-01-15T12:00:00.000Z")).toBeTruthy();
    act(() => useClock.setState({ now: NOW_MS + 60_000 }));
    expect(screen.getByText("2026-01-15T12:01:00.000Z")).toBeTruthy();
  });
});

describe("startClock", () => {
  test("advances the shared clock once a second and clears its timer when stopped", () => {
    // Settle any clock a previous test left running so this test starts from no timer.
    startClock()();

    const realSetInterval = globalThis.setInterval;
    const scheduled: { callback: () => void; delay: number | undefined; handle: unknown }[] = [];
    const schedule = spyOn(globalThis, "setInterval").mockImplementation(((
      callback: () => void,
      delay?: number,
    ) => {
      const handle = realSetInterval(() => {}, 60_000);
      scheduled.push({ callback, delay, handle });
      return handle;
    }) as typeof setInterval);
    const clear = spyOn(globalThis, "clearInterval");
    spyOn(Date, "now").mockReturnValue(NOW_MS + 5000);

    const stop = startClock();
    expect(schedule).toHaveBeenCalledTimes(1);
    expect(scheduled[0]?.delay).toBe(1000);

    const tick = scheduled[0]?.callback;
    if (!tick) throw new Error("startClock did not schedule a tick");
    act(() => tick());
    expect(useClock.getState().now).toBe(NOW_MS + 5000);

    stop();
    expect(clear).toHaveBeenCalledWith(scheduled[0]?.handle);

    const restop = startClock();
    expect(schedule).toHaveBeenCalledTimes(2);
    restop();
  });
});
