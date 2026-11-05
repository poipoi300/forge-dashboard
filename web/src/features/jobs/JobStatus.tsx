// A job's status as glyph and word.
import { StatusMark } from "@/components/status";
import type { Tone } from "@/schema/schema";

const JOB_TONE: Record<string, Tone> = {
  queued: "neutral",
  running: "info",
  succeeded: "good",
  failed: "bad",
  cancelled: "neutral",
  attention: "warn",
};

const JOB_LABEL: Record<string, string> = { attention: "needs you" };

export function JobStatusMark({ status }: { status: string }) {
  const tone = JOB_TONE[status] ?? "neutral";
  return (
    <StatusMark tone={tone} strong={tone === "bad" || tone === "warn"}>
      {JOB_LABEL[status] ?? status}
    </StatusMark>
  );
}
