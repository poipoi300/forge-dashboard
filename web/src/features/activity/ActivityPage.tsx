// The notification center beside the fleet-wide timeline.
import { CheckCheck } from "lucide-react";
import { Link } from "wouter";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { RelativeTime } from "@/components/data";
import { QueryState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";
import { ActivityFeed } from "./ActivityFeed";

const SEVERITY_TONE: Record<string, Tone> = {
  info: "neutral",
  success: "good",
  warning: "warn",
  error: "bad",
};

function Notifications() {
  const events = useServiceState("events");
  const page = useAction(
    "/api/services/events/actions/notifications",
    { unread_only: false, before_id: null, limit: 100 },
    { live: true },
  );
  return (
    <Section
      title="Notifications"
      description={
        events?.notification_failures
          ? `${events.notification_failures} desktop notifications failed: ${events.last_notification_error ?? ""}`
          : undefined
      }
      actions={
        events?.unread ? (
          <ActionButton
            service="events"
            action="mark-read"
            input={{ ids: [], mark_all: true }}
            size="xs"
            icon={<CheckCheck />}
            label={`Mark ${events.unread} read`}
          />
        ) : null
      }
    >
      <QueryState query={page}>
        {(data) =>
          data.notifications.length ? (
            <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
              {data.notifications.map((item) => (
                <li
                  key={item.id}
                  className={cn(
                    "flex items-start justify-between gap-3 px-3 py-2",
                    !item.read && "bg-info-wash/60",
                  )}
                >
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <StatusMark
                      tone={SEVERITY_TONE[item.severity] ?? "neutral"}
                      strong={!item.read}
                    >
                      {item.title}
                    </StatusMark>
                    <span className="text-2xs text-ink-3">
                      {item.kind}
                      {item.project_id ? (
                        <>
                          {" · "}
                          <Link href={`/p/${item.project_id}`} className="hover:underline">
                            {item.project_id}
                          </Link>
                        </>
                      ) : null}{" "}
                      · <RelativeTime at={item.ts} />
                    </span>
                  </span>
                  {!item.read ? (
                    <ActionButton
                      service="events"
                      action="mark-read"
                      input={{ ids: [item.id], mark_all: false }}
                      size="xs"
                      variant="ghost"
                      label="Read"
                    />
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-3">
              No notifications. CI failures, fork conflicts, crashes, and check regressions land
              here.
            </p>
          )
        }
      </QueryState>
    </Section>
  );
}

export function ActivityPage() {
  return (
    <div className="grid gap-8 px-5 py-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <Notifications />
      <Section title="Everything, as it happened">
        <ActivityFeed projectId={null} showProject />
      </Section>
    </div>
  );
}
