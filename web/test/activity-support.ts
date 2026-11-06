// Fixture builders for the activity feed and the notification center.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";

export type ActivityEntry = Schemas["ActivityEntry"];
export type Notification = Schemas["Notification"];

export function makeActivityEntry(
  id: number,
  overrides: Partial<ActivityEntry> = {},
): ActivityEntry {
  return {
    id,
    ts: NOW_SECONDS,
    service: "git",
    type: "git.commit",
    project_id: null,
    severity: "info",
    title: `Event ${id}`,
    data: {},
    ...overrides,
  };
}

/** A page of the timeline; pass `next` to say older entries exist before that (ts, id). */
export function makeActivityPage(
  entries: ActivityEntry[],
  next: { before: number; beforeId: number } | null = null,
): Schemas["ActivityPage"] {
  return {
    entries,
    next_before: next?.before ?? null,
    next_before_id: next?.beforeId ?? null,
  };
}

export function makeNotification(id: number, overrides: Partial<Notification> = {}): Notification {
  return {
    id,
    ts: NOW_SECONDS - 300,
    kind: "ci_failure",
    title: `Notification ${id}`,
    severity: "info",
    project_id: null,
    read: false,
    ...overrides,
  };
}

export function makeNotificationPage(notifications: Notification[]): Schemas["NotificationPage"] {
  return {
    notifications,
    unread: notifications.filter((item) => !item.read).length,
    next_before_id: null,
  };
}

export function makeEventsState(
  overrides: Partial<Schemas["EventsState"]> = {},
): Schemas["EventsState"] {
  return {
    unread: 0,
    notifications: [],
    notification_failures: 0,
    last_notification_error: null,
    ...overrides,
  };
}
