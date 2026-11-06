import { afterEach, beforeEach, describe, expect, setSystemTime, spyOn, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { ActivityFeed } from "@/features/activity/ActivityFeed";
import { makeActivityEntry, makeActivityPage } from "./activity-support";
import {
  emitEvent,
  installHarness,
  MockApiError,
  mockApi,
  NOW_MS,
  NOW_SECONDS,
  renderWithProviders,
  settle,
} from "./harness";
import { waitUntilGone } from "./jobs-support";

installHarness();

// The feed groups by the local calendar day relative to the real clock, so freeze that too.
beforeEach(() => setSystemTime(new Date(NOW_MS)));
afterEach(() => setSystemTime());

const ACTIVITY = "/api/services/events/actions/activity" as const;
const DAY = 86_400;

function twoDigits(value: number): string {
  return String(value).padStart(2, "0");
}

function localClock(seconds: number): string {
  const date = new Date(seconds * 1000);
  return `${twoDigits(date.getHours())}:${twoDigits(date.getMinutes())}`;
}

function section(label: string | RegExp): HTMLElement {
  return screen.getByRole("region", { name: label });
}

describe("ActivityFeed timeline", () => {
  test("asks for the fleet-wide timeline unfiltered, sixty entries at a time", async () => {
    const api = mockApi().action(ACTIVITY, makeActivityPage([]));
    renderWithProviders(<ActivityFeed projectId={null} />);
    expect(await screen.findByText("Nothing recorded yet")).toBeTruthy();
    expect(api.sent(ACTIVITY)).toEqual([
      { project_id: null, services: [], types: [], before: null, before_id: null, limit: 60 },
    ]);
  });

  test("scopes the timeline to one project", async () => {
    const api = mockApi().action(ACTIVITY, makeActivityPage([makeActivityEntry(1)]));
    renderWithProviders(<ActivityFeed projectId="alpha" />);
    await screen.findByText("Event 1");
    expect(api.sent(ACTIVITY)[0]).toMatchObject({ project_id: "alpha" });
  });

  test("shows a loading state until the first page arrives", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi().action(ACTIVITY, async () => {
      await gate;
      return makeActivityPage([makeActivityEntry(1)]);
    });
    renderWithProviders(<ActivityFeed projectId={null} />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    release();
    expect(await screen.findByText("Event 1")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
  });

  test("explains an empty timeline", async () => {
    mockApi().action(ACTIVITY, makeActivityPage([]));
    renderWithProviders(<ActivityFeed projectId={null} />);
    expect(await screen.findByText("Nothing recorded yet")).toBeTruthy();
    expect(screen.getByText(/Commits, CI runs, checks, syncs, and status changes/)).toBeTruthy();
  });

  test("reports a failed load and retries it", async () => {
    let attempts = 0;
    mockApi().action(ACTIVITY, () => {
      attempts += 1;
      if (attempts === 1) throw new MockApiError(500, "event log unavailable");
      return makeActivityPage([makeActivityEntry(1, { title: "Recovered" })]);
    });
    const { user } = renderWithProviders(<ActivityFeed projectId={null} />);
    expect((await screen.findByRole("alert")).textContent).toContain("event log unavailable");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Recovered")).toBeTruthy();
  });

  test("groups entries under Today, Yesterday and dated headings, newest first", async () => {
    mockApi().action(
      ACTIVITY,
      makeActivityPage([
        makeActivityEntry(5, { ts: NOW_SECONDS, title: "Pushed to main" }),
        makeActivityEntry(4, { ts: NOW_SECONDS, title: "Gates passed" }),
        makeActivityEntry(3, { ts: NOW_SECONDS - DAY, title: "Fork synced" }),
        makeActivityEntry(2, { ts: NOW_SECONDS - 5 * DAY, title: "Status changed" }),
        makeActivityEntry(1, { ts: NOW_SECONDS - 20 * DAY, title: "Project added" }),
      ]),
    );
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Pushed to main");
    const today = within(section("Today"));
    expect(today.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Pushed to main"),
      expect.stringContaining("Gates passed"),
    ]);
    expect(within(section("Yesterday")).getByText("Fork synced")).toBeTruthy();
    const headings = screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent);
    expect(headings.slice(0, 2)).toEqual(["Today", "Yesterday"]);
    expect(headings).toHaveLength(4);
    const older = section(headings[2] ?? "");
    expect(within(older).getByText("Status changed")).toBeTruthy();
    expect(headings[2]).not.toMatch(/2025|2026/);
    const previousYear = section(headings[3] ?? "");
    expect(within(previousYear).getByText("Project added")).toBeTruthy();
    expect(headings[3]).toMatch(/2025/);
  });

  test("starts a new group when the day changes between two adjacent entries", async () => {
    const consoleError = spyOn(console, "error");
    mockApi().action(
      ACTIVITY,
      makeActivityPage([
        makeActivityEntry(3, { ts: NOW_SECONDS, title: "Newest" }),
        makeActivityEntry(2, { ts: NOW_SECONDS - DAY, title: "Middle" }),
        makeActivityEntry(1, { ts: NOW_SECONDS, title: "Back to today" }),
      ]),
    );
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Newest");
    expect(screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent)).toEqual([
      "Today",
      "Yesterday",
      "Today",
    ]);
    // A recurring day label must not collide as a React key.
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("same key");
    consoleError.mockRestore();
  });

  test("shows each entry's time of day, title and originating service", async () => {
    mockApi().action(
      ACTIVITY,
      makeActivityPage([
        makeActivityEntry(1, {
          ts: NOW_SECONDS,
          title: "Pushed to main",
          service: "github",
          project_id: "alpha",
        }),
      ]),
    );
    renderWithProviders(<ActivityFeed projectId={null} />);
    const item = (await screen.findByText("Pushed to main")).closest("li");
    if (!item) throw new Error("no timeline item");
    expect(within(item).getByText(localClock(NOW_SECONDS))).toBeTruthy();
    expect(within(item).getByText("github")).toBeTruthy();
  });

  test("links an entry to its project only when the project column is requested", async () => {
    mockApi().action(
      ACTIVITY,
      makeActivityPage([
        makeActivityEntry(2, { title: "With project", project_id: "alpha" }),
        makeActivityEntry(1, { title: "Without project", project_id: null }),
      ]),
    );
    const first = renderWithProviders(<ActivityFeed projectId={null} showProject />);
    await screen.findByText("With project");
    await first.user.click(screen.getByRole("link", { name: "alpha" }));
    expect(first.location()).toBe("/p/alpha");
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  test("leaves the project out of each entry by default", async () => {
    mockApi().action(
      ACTIVITY,
      makeActivityPage([makeActivityEntry(1, { title: "With project", project_id: "alpha" })]),
    );
    renderWithProviders(<ActivityFeed projectId="alpha" />);
    await screen.findByText("With project");
    expect(screen.queryByRole("link")).toBeNull();
  });

  test.each([
    ["info", "neutral"],
    ["success", "good"],
    ["warning", "warn"],
    ["error", "bad"],
  ] as const)("marks a %s entry with the %s tone", async (severity, tone) => {
    mockApi().action(
      ACTIVITY,
      makeActivityPage([makeActivityEntry(1, { title: "Something happened", severity })]),
    );
    renderWithProviders(<ActivityFeed projectId={null} />);
    const mark = (await screen.findByText("Something happened")).closest("[data-tone]");
    expect(mark?.getAttribute("data-tone")).toBe(tone);
  });
});

describe("ActivityFeed filters", () => {
  test("offers a toggle per source, none pressed at first", async () => {
    mockApi().action(ACTIVITY, makeActivityPage([]));
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Nothing recorded yet");
    const labels = within(screen.getByRole("group", { name: "Sources" }))
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(labels).toEqual([
      "Git",
      "GitHub",
      "Checks",
      "Forks",
      "Standards",
      "Actions",
      "Agents",
      "Services",
      "Status",
      "Jobs",
    ]);
    for (const button of screen.getAllByRole("button")) {
      expect(button.getAttribute("aria-pressed")).toBe("false");
    }
  });

  test("requests only the pressed sources and drops one when it is pressed again", async () => {
    const api = mockApi().action(ACTIVITY, makeActivityPage([makeActivityEntry(1)]));
    const { user } = renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Event 1");
    await user.click(screen.getByRole("button", { name: "Checks" }));
    await waitFor(() => expect(api.sent(ACTIVITY)).toHaveLength(2));
    expect(api.sent(ACTIVITY)[1]).toMatchObject({ services: ["checks"] });
    expect(screen.getByRole("button", { name: "Checks" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Forks" }));
    await waitFor(() => expect(api.sent(ACTIVITY)).toHaveLength(3));
    expect(api.sent(ACTIVITY)[2]).toMatchObject({ services: ["checks", "forks"] });
    await user.click(screen.getByRole("button", { name: "Checks" }));
    await waitFor(() => expect(api.sent(ACTIVITY)).toHaveLength(4));
    expect(api.sent(ACTIVITY)[3]).toMatchObject({ services: ["forks"] });
    expect(screen.getByRole("button", { name: "Checks" }).getAttribute("aria-pressed")).toBe(
      "false",
    );
  });

  test("shows the filtered entries the server returns", async () => {
    mockApi().action(ACTIVITY, ({ services = [] }) =>
      makeActivityPage(
        services.includes("git")
          ? [makeActivityEntry(1, { title: "Only git" })]
          : [makeActivityEntry(1, { title: "Only git" }), makeActivityEntry(2, { title: "Other" })],
      ),
    );
    const { user } = renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Other");
    await user.click(screen.getByRole("button", { name: "Git" }));
    await waitUntilGone(() => screen.queryByText("Other"));
    expect(screen.getByText("Only git")).toBeTruthy();
  });
});

describe("ActivityFeed paging", () => {
  test("loads older entries after the cursor the server gave and hides Older at the end", async () => {
    const api = mockApi().action(ACTIVITY, ({ before = null }) =>
      before === null
        ? makeActivityPage([makeActivityEntry(2, { title: "Newer" })], {
            before: 1000,
            beforeId: 2,
          })
        : makeActivityPage([makeActivityEntry(1, { title: "Older entry", ts: before - 1 })]),
    );
    const { user } = renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Newer");
    expect(screen.queryByText("Older entry")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Older entry")).toBeTruthy();
    expect(screen.getByText("Newer")).toBeTruthy();
    expect(api.sent(ACTIVITY)[1]).toMatchObject({ before: 1000, before_id: 2 });
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
  });

  test("has no Older button when the first page is the whole timeline", async () => {
    mockApi().action(ACTIVITY, makeActivityPage([makeActivityEntry(1)]));
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Event 1");
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
  });

  test("disables Older while the next page is loading", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi().action(ACTIVITY, async ({ before }) => {
      if (before === null)
        return makeActivityPage([makeActivityEntry(2)], { before: 1000, beforeId: 2 });
      await gate;
      return makeActivityPage([makeActivityEntry(1, { title: "Page two" })]);
    });
    const { user } = renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Event 2");
    await user.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Older" }) as HTMLButtonElement).disabled).toBe(
        true,
      ),
    );
    release();
    expect(await screen.findByText("Page two")).toBeTruthy();
  });
});

describe("ActivityFeed live updates", () => {
  test("reloads when a persisted event is recorded", async () => {
    let entries = [makeActivityEntry(1, { title: "First" })];
    const api = mockApi().action(ACTIVITY, () => makeActivityPage(entries));
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("First");
    entries = [makeActivityEntry(2, { title: "Second" }), ...entries];
    emitEvent({ type: "git.commit", persist: true });
    expect(await screen.findByText("Second")).toBeTruthy();
    expect(api.sent(ACTIVITY)).toHaveLength(2);
  });

  test("ignores events that are not persisted", async () => {
    const api = mockApi().action(ACTIVITY, makeActivityPage([makeActivityEntry(1)]));
    renderWithProviders(<ActivityFeed projectId={null} />);
    await screen.findByText("Event 1");
    emitEvent({ type: "job.log", persist: false });
    await settle();
    expect(api.sent(ACTIVITY)).toHaveLength(1);
  });
});
