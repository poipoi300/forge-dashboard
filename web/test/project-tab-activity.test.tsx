import { afterEach, beforeEach, describe, expect, setSystemTime, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { NOW_MS, NOW_SECONDS } from "./constants";
import { emitEvent, installHarness, MockApiError, mockApi } from "./harness";
import { openProjectTab } from "./project-core-support";

installHarness();

const ACTIVITY = "/api/services/events/actions/activity" as const;

type Entry = Schemas["ActivityEntry"];

beforeEach(() => setSystemTime(new Date(NOW_MS)));
afterEach(() => setSystemTime());

function makeEntry(id: number, overrides: Partial<Entry> = {}): Entry {
  return {
    id,
    ts: NOW_SECONDS,
    service: "git",
    type: "git.commit",
    project_id: "alpha",
    severity: "info",
    title: `Entry ${id}`,
    data: {},
    ...overrides,
  };
}

function page(
  entries: Entry[],
  next: { before: number; before_id: number } | null = null,
): Schemas["ActivityPage"] {
  return {
    entries,
    next_before: next?.before ?? null,
    next_before_id: next?.before_id ?? null,
  };
}

describe("ActivityTab", () => {
  test("asks for the newest page of this project's timeline", async () => {
    const api = mockApi().action(ACTIVITY, page([makeEntry(1)]));
    await openProjectTab("activity", {}, api);
    expect(await screen.findByText("Entry 1")).toBeTruthy();
    expect(api.sent(ACTIVITY)).toEqual([
      { project_id: "alpha", services: [], types: [], before: null, before_id: null, limit: 60 },
    ]);
  });

  test("groups entries under the day they happened on, naming the service", async () => {
    const api = mockApi().action(
      ACTIVITY,
      page([
        makeEntry(2, { title: "Pushed to origin", service: "git" }),
        makeEntry(1, { title: "Checks passed", service: "checks", ts: NOW_SECONDS - 86_400 }),
      ]),
    );
    await openProjectTab("activity", {}, api);
    const today = await screen.findByRole("region", { name: "Today" });
    expect(within(today).getByText("Pushed to origin")).toBeTruthy();
    expect(within(today).getByText("git")).toBeTruthy();
    const yesterday = screen.getByRole("region", { name: "Yesterday" });
    expect(within(yesterday).getByText("Checks passed")).toBeTruthy();
    expect(within(yesterday).getByText("checks")).toBeTruthy();
  });

  test("does not link entries to the project page they already sit on", async () => {
    const api = mockApi().action(ACTIVITY, page([makeEntry(1)]));
    await openProjectTab("activity", {}, api);
    await screen.findByText("Entry 1");
    expect(screen.queryByRole("link", { name: "alpha" })).toBeNull();
  });

  test.each([
    ["info", "neutral"],
    ["success", "good"],
    ["warning", "warn"],
    ["error", "bad"],
  ] as const)("marks a %s entry with the %s tone", async (severity, tone) => {
    const api = mockApi().action(
      ACTIVITY,
      page([makeEntry(1, { severity, title: "Something happened" })]),
    );
    await openProjectTab("activity", {}, api);
    const mark = (await screen.findByText("Something happened")).closest("[data-tone]");
    expect(mark?.getAttribute("data-tone")).toBe(tone);
  });

  test("narrows the timeline to the sources that are switched on", async () => {
    const api = mockApi().action(ACTIVITY, page([makeEntry(1)]));
    const { rendered } = await openProjectTab("activity", {}, api);
    await screen.findByText("Entry 1");
    const git = screen.getByRole("button", { name: "Git" });
    expect(git.getAttribute("aria-pressed")).toBe("false");
    await rendered.user.click(git);
    await rendered.user.click(screen.getByRole("button", { name: "Checks" }));
    await waitFor(() =>
      expect(api.sent(ACTIVITY).at(-1)).toMatchObject({ services: ["git", "checks"] }),
    );
    expect(screen.getByRole("button", { name: "Git" }).getAttribute("aria-pressed")).toBe("true");
    await rendered.user.click(screen.getByRole("button", { name: "Git" }));
    await waitFor(() => expect(api.sent(ACTIVITY).at(-1)).toMatchObject({ services: ["checks"] }));
    expect(screen.getByRole("button", { name: "Git" }).getAttribute("aria-pressed")).toBe("false");
  });

  test("says nothing has been recorded for an empty timeline", async () => {
    const api = mockApi().action(ACTIVITY, page([]));
    await openProjectTab("activity", {}, api);
    expect(await screen.findByText("Nothing recorded yet")).toBeTruthy();
  });

  test("shows why the timeline failed and retries on request", async () => {
    let failing = true;
    const api = mockApi().action(ACTIVITY, () => {
      if (failing) throw new MockApiError(500, "event log is locked");
      return page([makeEntry(1)]);
    });
    const { rendered } = await openProjectTab("activity", {}, api);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("event log is locked");
    failing = false;
    await rendered.user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Entry 1")).toBeTruthy();
  });

  test("loads the next older page from the cursor the server returned", async () => {
    const api = mockApi().action(ACTIVITY, (input) =>
      (input as { before: number | null }).before === null
        ? page([makeEntry(2)], { before: 1_000, before_id: 2 })
        : page([makeEntry(1, { ts: NOW_SECONDS - 86_400 })]),
    );
    const { rendered } = await openProjectTab("activity", {}, api);
    await screen.findByText("Entry 2");
    await rendered.user.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Entry 1")).toBeTruthy();
    expect(api.sent(ACTIVITY).at(-1)).toMatchObject({ before: 1_000, before_id: 2 });
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
    expect(screen.getByText("Entry 2")).toBeTruthy();
  });

  test("reloads when a recorded event arrives but not for a transient one", async () => {
    const api = mockApi().action(ACTIVITY, page([makeEntry(1)]));
    await openProjectTab("activity", {}, api);
    await screen.findByText("Entry 1");
    expect(api.callsTo("POST", ACTIVITY)).toHaveLength(1);
    emitEvent({ type: "job.log", persist: false });
    emitEvent({ type: "git.commit", persist: false });
    await waitFor(() => expect(api.callsTo("POST", ACTIVITY)).toHaveLength(1));
    emitEvent({ type: "git.commit", persist: true });
    await waitFor(() => expect(api.callsTo("POST", ACTIVITY)).toHaveLength(2));
  });
});
