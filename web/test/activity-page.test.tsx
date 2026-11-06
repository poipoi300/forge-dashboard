import { afterEach, beforeEach, describe, expect, setSystemTime, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { ActivityPage } from "@/features/activity/ActivityPage";
import {
  makeActivityEntry,
  makeActivityPage,
  makeEventsState,
  makeNotification,
  makeNotificationPage,
} from "./activity-support";
import {
  installHarness,
  MockApiError,
  mockApi,
  NOW_MS,
  NOW_SECONDS,
  renderApp,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";

installHarness();

beforeEach(() => setSystemTime(new Date(NOW_MS)));
afterEach(() => setSystemTime());

const ACTIVITY = "/api/services/events/actions/activity" as const;
const NOTIFICATIONS = "/api/services/events/actions/notifications" as const;
const MARK_READ = "/api/services/events/actions/mark-read" as const;

function notificationsSection(): HTMLElement {
  const section = screen.getByRole("heading", { name: "Notifications" }).closest("section");
  if (!section) throw new Error("no notifications section");
  return section;
}

function notificationItem(title: string): HTMLElement {
  const item = within(notificationsSection()).getByText(title).closest("li");
  if (!item) throw new Error(`no notification ${title}`);
  return item;
}

function mockActivityPage() {
  return mockApi().action(ACTIVITY, makeActivityPage([]));
}

describe("ActivityPage notifications", () => {
  test("asks for every notification, read or not, a hundred at a time", async () => {
    const api = mockActivityPage().action(NOTIFICATIONS, makeNotificationPage([]));
    renderWithProviders(<ActivityPage />);
    expect(
      await within(notificationsSection()).findByText(/No notifications\. CI failures/),
    ).toBeTruthy();
    expect(api.sent(NOTIFICATIONS)[0]).toEqual({
      unread_only: false,
      before_id: null,
      limit: 100,
    });
  });

  test("shows a loading state until the notifications arrive", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockActivityPage().action(NOTIFICATIONS, async () => {
      await gate;
      return makeNotificationPage([makeNotification(1, { title: "CI failed on alpha" })]);
    });
    renderWithProviders(<ActivityPage />);
    expect(within(notificationsSection()).getByRole("status", { name: "Loading" })).toBeTruthy();
    release();
    expect(await screen.findByText("CI failed on alpha")).toBeTruthy();
  });

  test("reports a failed notification load and retries it", async () => {
    let attempts = 0;
    mockActivityPage().action(NOTIFICATIONS, () => {
      attempts += 1;
      if (attempts === 1) throw new MockApiError(500, "notification store locked");
      return makeNotificationPage([makeNotification(1, { title: "Recovered notification" })]);
    });
    const { user } = renderWithProviders(<ActivityPage />);
    const alert = await within(notificationsSection()).findByRole("alert");
    expect(alert.textContent).toContain("notification store locked");
    await user.click(within(notificationsSection()).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Recovered notification")).toBeTruthy();
  });

  test("lists notifications with kind, project link and age, and singles out the unread", async () => {
    mockActivityPage().action(
      NOTIFICATIONS,
      makeNotificationPage([
        makeNotification(2, {
          title: "CI failed on alpha",
          kind: "ci_failure",
          project_id: "alpha",
          severity: "error",
          ts: NOW_SECONDS - 7200,
        }),
        makeNotification(1, {
          title: "Fork synced",
          kind: "fork_success",
          severity: "success",
          read: true,
          ts: NOW_SECONDS - 3 * 86_400,
        }),
      ]),
    );
    const { user, location } = renderWithProviders(<ActivityPage />);
    await screen.findByText("CI failed on alpha");
    const unread = within(notificationItem("CI failed on alpha"));
    expect(unread.getByText(/ci_failure/)).toBeTruthy();
    expect(unread.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(
      unread.getByText("CI failed on alpha").closest("[data-tone]")?.getAttribute("data-tone"),
    ).toBe("bad");
    await user.click(unread.getByRole("link", { name: "alpha" }));
    expect(location()).toBe("/p/alpha");
    const read = within(notificationItem("Fork synced"));
    expect(read.getByText(/fork_success/)).toBeTruthy();
    expect(read.getByText(timeText("3 d ago"))).toBeTruthy();
    expect(read.queryByRole("link")).toBeNull();
    expect(read.queryByRole("button", { name: "Read" })).toBeNull();
    expect(read.getByText("Fork synced").closest("[data-tone]")?.getAttribute("data-tone")).toBe(
      "good",
    );
  });

  test("marks one notification read with its id", async () => {
    const api = mockActivityPage()
      .action(
        NOTIFICATIONS,
        makeNotificationPage([
          makeNotification(7, { title: "Check regression" }),
          makeNotification(8, { title: "Service crashed", severity: "warning" }),
        ]),
      )
      .action(MARK_READ, { unread: 1 });
    const { user } = renderWithProviders(<ActivityPage />);
    await screen.findByText("Service crashed");
    await settle();
    await user.click(
      within(notificationItem("Service crashed")).getByRole("button", { name: "Read" }),
    );
    await waitFor(() => expect(api.sent(MARK_READ)).toHaveLength(1));
    expect(api.sent(MARK_READ)).toEqual([{ ids: [8], mark_all: false }]);
  });

  test("offers to mark everything read only while something is unread", async () => {
    const api = mockActivityPage()
      .action(NOTIFICATIONS, makeNotificationPage([makeNotification(1), makeNotification(2)]))
      .action(MARK_READ, { unread: 0 });
    const { user } = renderWithProviders(<ActivityPage />);
    await screen.findByText("Notification 1");
    expect(screen.queryByRole("button", { name: /^Mark \d+ read$/ })).toBeNull();
    seedStream({ events: makeEventsState({ unread: 2 }) });
    await settle();
    await user.click(await screen.findByRole("button", { name: "Mark 2 read" }));
    await waitFor(() => expect(api.sent(MARK_READ)).toHaveLength(1));
    expect(api.sent(MARK_READ)).toEqual([{ ids: [], mark_all: true }]);
    seedStream({ events: makeEventsState({ unread: 0 }) }, 2);
    await settle();
    expect(screen.queryByRole("button", { name: /^Mark \d+ read$/ })).toBeNull();
  });

  test("reports desktop notification failures with the last error", async () => {
    mockActivityPage().action(NOTIFICATIONS, makeNotificationPage([]));
    renderWithProviders(<ActivityPage />);
    expect(screen.queryByText(/desktop notifications failed/)).toBeNull();
    seedStream({
      events: makeEventsState({
        notification_failures: 3,
        last_notification_error: "toast service down",
      }),
    });
    expect(
      await screen.findByText("3 desktop notifications failed: toast service down"),
    ).toBeTruthy();
  });

  test("reloads the notifications when the events service publishes a new revision", async () => {
    let notifications = [makeNotification(1, { title: "First notification" })];
    const api = mockActivityPage().action(NOTIFICATIONS, () => makeNotificationPage(notifications));
    renderWithProviders(<ActivityPage />);
    seedStream({ events: makeEventsState({ unread: 1 }) }, 1);
    await screen.findByText("First notification");
    notifications = [makeNotification(2, { title: "Second notification" }), ...notifications];
    seedStream({ events: makeEventsState({ unread: 2 }) }, 2);
    expect(await screen.findByText("Second notification")).toBeTruthy();
    expect(api.sent(NOTIFICATIONS).length).toBeGreaterThanOrEqual(2);
  });
});

describe("ActivityPage timeline", () => {
  test("shows the fleet-wide timeline with each entry's project", async () => {
    const api = mockApi()
      .action(NOTIFICATIONS, makeNotificationPage([]))
      .action(
        ACTIVITY,
        makeActivityPage([
          makeActivityEntry(1, { title: "Gates passed", project_id: "alpha", service: "checks" }),
        ]),
      );
    renderWithProviders(<ActivityPage />);
    expect(await screen.findByRole("heading", { name: "Everything, as it happened" })).toBeTruthy();
    expect(await screen.findByText("Gates passed")).toBeTruthy();
    expect(screen.getByRole("link", { name: "alpha" })).toBeTruthy();
    expect(api.sent(ACTIVITY)[0]).toMatchObject({ project_id: null });
  });

  test("lands on the notification center from the rail's Notifications link", async () => {
    mockApi()
      .action(
        NOTIFICATIONS,
        makeNotificationPage([makeNotification(1, { title: "Fork conflict" })]),
      )
      .action(ACTIVITY, makeActivityPage([]));
    const { user, location } = renderApp("/");
    seedStream({ events: makeEventsState({ unread: 1 }) });
    await user.click(await screen.findByRole("link", { name: /Notifications\s*1/ }));
    expect(location()).toBe("/activity");
    expect(await screen.findByText("Fork conflict")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark 1 read" })).toBeTruthy();
  });
});
