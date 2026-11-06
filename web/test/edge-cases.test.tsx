import { describe, expect, test } from "bun:test";
import { screen } from "@testing-library/react";
import { lateMinutes } from "@/app/Departures";
import { DiffView } from "@/components/DiffView";
import { parentOf } from "@/components/FolderPicker";
import { duration, relative } from "@/lib/time";
import { describeCron } from "@/schema/cron";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

describe("relative times and durations round before they pick a unit", () => {
  test("59 and a half minutes reads as an hour, not 60 minutes", () => {
    expect(relative(1_000_000 - 3570, 1_000_000 * 1000)).toBe("1 h ago");
    expect(relative(1_000_000 - 3500, 1_000_000 * 1000)).toBe("58 min ago");
  });

  test("a duration just under a unit rounds into the next one", () => {
    expect(duration(59.6)).toBe("1 min");
    expect(duration(0.9996)).toBe("1 s");
  });
});

describe("cron expressions outside their ranges are not schedules", () => {
  test.each([
    ["0 0 * * 8"],
    ["99 99 * * *"],
    ["0 24 * * *"],
    ["0 0 0 * *"],
    ["0 0 * 13 *"],
    ["*/0 * * * *"],
    ["5-2 * * * *"],
  ])("%s is refused", (expression) => {
    expect(describeCron(expression)).toBeNull();
  });

  test("valid ranges, lists, and steps still read", () => {
    expect(describeCron("*/15 * * * *")).toBe("Every 15 minutes");
    expect(describeCron("0 9 * * 1-5")).toBe("Weekdays at 09:00");
    expect(describeCron("0,30 8-18 * * 7")).toBe("Custom schedule: 0,30 8-18 * * 7");
  });
});

describe("the folder above a path", () => {
  test("climbs one level, stops at drive and filesystem roots", () => {
    expect(parentOf("C:\\Users\\me\\repo")).toBe("C:\\Users\\me");
    expect(parentOf("C:\\Users")).toBe("C:\\");
    expect(parentOf("C:\\")).toBeNull();
    expect(parentOf("/home/me/repo/")).toBe("/home/me");
    expect(parentOf("/home")).toBeNull();
  });
});

describe("late departures", () => {
  test("count whole minutes past due, and nothing for on-time or unscheduled runs", () => {
    const now = 1_000_000 * 1000;
    expect(lateMinutes(1_000_000 - 185, now)).toBe(3);
    expect(lateMinutes(1_000_000 - 30, now)).toBeNull();
    expect(lateMinutes(1_000_000 + 600, now)).toBeNull();
    expect(lateMinutes(null, now)).toBeNull();
  });
});

describe("diffs that are not plain edits", () => {
  test("a rename with edits shows both paths", () => {
    const diff = `diff --git a/old/name.ts b/new/name.ts
similarity index 90%
rename from old/name.ts
rename to new/name.ts
index 1111111..2222222 100644
--- a/old/name.ts
+++ b/new/name.ts
@@ -1,2 +1,2 @@
-export const a = 1;
+export const a = 2;
 export const b = 3;
`;
    renderWithProviders(<DiffView diff={diff} />);
    expect(screen.getByText("old/name.ts → new/name.ts")).toBeTruthy();
  });

  test("text that is not a unified diff is shown as written", () => {
    renderWithProviders(<DiffView diff={"fatal: bad revision 'nope'"} />);
    expect(screen.getByText("fatal: bad revision 'nope'")).toBeTruthy();
    expect(screen.queryByText("Binary or mode-only change.")).toBeNull();
  });
});
