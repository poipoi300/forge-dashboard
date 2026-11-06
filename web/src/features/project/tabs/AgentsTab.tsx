// Agent sessions: Claude Code sessions recorded for this folder, claude
// processes running in it now, and the dashboard's own agent runs with their
// full transcripts.
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useAction } from "@/api/query";
import { RelativeTime } from "@/components/data";
import { Markdown } from "@/components/Markdown";
import { EmptyState, QueryState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { duration } from "@/lib/time";
import { cn } from "@/lib/utils";
import { BranchRuns } from "../BranchRuns";
import type { TabProps } from "../ProjectPage";

type AgentRun = Schemas["AgentRunRecord"];
type Content = {
  type?: string;
  text?: string;
  name?: string;
  input?: unknown;
  content?: unknown;
  is_error?: boolean;
};

function asContent(record: Record<string, unknown>): Content[] {
  const message = record.message as { content?: unknown } | undefined;
  const content = message?.content;
  if (typeof content === "string") return [{ type: "text", text: content }];
  return Array.isArray(content) ? (content as Content[]) : [];
}

function resultText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((part) => (part && typeof part === "object" && "text" in part ? String(part.text) : ""))
      .join("\n");
  return JSON.stringify(content, null, 2);
}

/** One stream-json record from `claude -p --output-format stream-json`. */
function TranscriptRecord({ record }: { record: Record<string, unknown> }) {
  const type = String(record.type ?? "");
  if (type === "system") {
    return (
      <p className="text-2xs text-ink-3">
        session started{record.model ? ` · ${String(record.model)}` : ""}
        {record.cwd ? ` · ${String(record.cwd)}` : ""}
      </p>
    );
  }
  if (type === "result") {
    return (
      <div className="rounded-sm border border-rule bg-surface-sunk px-3 py-2 text-xs">
        <p className="font-medium">
          finished: {String(record.subtype ?? "")}
          {typeof record.num_turns === "number" ? ` · ${record.num_turns} turns` : ""}
          {typeof record.total_cost_usd === "number"
            ? ` · $${record.total_cost_usd.toFixed(3)}`
            : ""}
          {typeof record.duration_ms === "number"
            ? ` · ${duration(record.duration_ms / 1000)}`
            : ""}
        </p>
        {typeof record.result === "string" ? (
          <Markdown className="mt-1">{record.result}</Markdown>
        ) : null}
      </div>
    );
  }
  if (type === "assistant" || type === "user") {
    return (
      <div className="flex flex-col gap-1">
        {asContent(record).map((part, index) => {
          const key = `${type}-${index}`;
          if (part.type === "text" && part.text) {
            return type === "assistant" ? (
              <Markdown key={key}>{part.text}</Markdown>
            ) : (
              <p key={key} className="rounded-sm bg-hover px-2 py-1 text-xs whitespace-pre-wrap">
                {part.text}
              </p>
            );
          }
          if (part.type === "tool_use") {
            return (
              <details key={key} className="rounded-sm border border-rule">
                <summary className="cursor-pointer px-2 py-0.5 font-mono text-2xs text-info-ink">
                  {part.name}
                </summary>
                <pre className="max-h-60 overflow-auto px-2 py-1 font-mono text-2xs">
                  {JSON.stringify(part.input, null, 2)}
                </pre>
              </details>
            );
          }
          if (part.type === "tool_result") {
            return (
              <details
                key={key}
                className={cn(
                  "rounded-sm border",
                  part.is_error ? "border-signal/40" : "border-rule",
                )}
              >
                <summary
                  className={cn(
                    "cursor-pointer px-2 py-0.5 text-2xs",
                    part.is_error ? "text-signal-ink" : "text-ink-3",
                  )}
                >
                  {part.is_error ? "tool error" : "tool result"}
                </summary>
                <pre className="max-h-60 overflow-auto px-2 py-1 font-mono text-2xs whitespace-pre-wrap">
                  {resultText(part.content)}
                </pre>
              </details>
            );
          }
          return null;
        })}
      </div>
    );
  }
  return (
    <pre className="overflow-auto font-mono text-2xs text-ink-3">{JSON.stringify(record)}</pre>
  );
}

function Transcript({ run }: { run: AgentRun }) {
  const [limit, setLimit] = useState(200);
  const transcript = useAction(
    "/api/services/agents/actions/transcript",
    { run_id: run.id, offset: 0, limit },
    { follow: ["agents"] },
  );
  return (
    <QueryState query={transcript} rows={8}>
      {(data) => (
        <div className="flex flex-col gap-3">
          {data.prompt ? (
            <details>
              <summary className="cursor-pointer text-xs text-ink-2">
                The brief the agent was given
              </summary>
              <pre className="mt-1 max-h-72 overflow-auto rounded-sm bg-surface-sunk px-3 py-2 font-mono text-2xs whitespace-pre-wrap">
                {data.prompt}
              </pre>
            </details>
          ) : null}
          {data.records.map((record, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript
            <TranscriptRecord key={index} record={record as Record<string, unknown>} />
          ))}
          {!data.done ? (
            <button
              type="button"
              className="self-start text-xs text-info-ink hover:underline"
              onClick={() => setLimit(limit + 200)}
            >
              Show more
            </button>
          ) : null}
        </div>
      )}
    </QueryState>
  );
}

export function AgentsTab({ project }: TabProps) {
  const sessions = useAction(
    "/api/services/agents/actions/sessions",
    { project_id: project.id },
    { follow: ["agents"] },
  );
  const runs = useAction(
    "/api/services/agents/actions/runs",
    { project_id: project.id, limit: 30 },
    { follow: ["agents"] },
  );
  const [selected, setSelected] = useState<string | null>(null);
  const selectedRun = runs.data?.runs.find((run) => run.id === selected);

  return (
    <div className="flex flex-col gap-8">
      <div className="grid grid-cols-[minmax(0,1fr)] gap-8 xl:grid-cols-2">
        <Section
          title="Claude Code sessions"
          description="Read from ~/.claude/projects for this folder."
        >
          <QueryState query={sessions}>
            {(data) => (
              <div className="flex flex-col gap-3">
                {data.processes.length ? (
                  <ul className="flex flex-col gap-1 rounded-md border border-info/30 bg-info-wash px-3 py-2">
                    {data.processes.map((process) => (
                      <li key={process.pid} className="text-xs">
                        <StatusMark tone="info" strong>
                          claude running (pid {process.pid})
                        </StatusMark>{" "}
                        <span className="text-ink-3">
                          since <RelativeTime at={process.started_at} /> in {process.cwd}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {data.sessions.length ? (
                  <ul className="flex flex-col divide-y divide-rule">
                    {data.sessions.map((session) => (
                      <li
                        key={session.id}
                        className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 py-1.5"
                      >
                        <span className="min-w-0">
                          <span className="block truncate text-sm">
                            {session.first_prompt ?? "(no prompt recorded)"}
                          </span>
                          <span className="block text-2xs text-ink-3">
                            {session.message_count} messages · {duration(session.duration_s)}
                          </span>
                        </span>
                        <span className="text-right text-2xs text-ink-3">
                          <RelativeTime at={session.start} />
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-ink-3">
                    No Claude Code sessions recorded for this folder.
                  </p>
                )}
              </div>
            )}
          </QueryState>
        </Section>
        <Section title="Dashboard agent runs">
          <QueryState query={runs}>
            {(data) =>
              data.runs.length ? (
                <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
                  {data.runs.map((run) => (
                    <li key={run.id}>
                      <button
                        type="button"
                        onClick={() => setSelected(run.id)}
                        className={cn(
                          "grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 px-3 py-1.5 text-left hover:bg-hover",
                          selected === run.id && "bg-select",
                        )}
                      >
                        <span className="min-w-0">
                          <span className="block truncate text-sm">{run.kind}</span>
                          <span className="block text-2xs text-ink-3">
                            {run.model ?? "default model"}
                            {run.turns !== null ? ` · ${run.turns} turns` : ""}
                            {run.cost_usd !== null ? ` · $${run.cost_usd.toFixed(3)}` : ""}
                            {run.duration_ms !== null
                              ? ` · ${duration(run.duration_ms / 1000)}`
                              : ""}
                          </span>
                        </span>
                        <span className="flex flex-col items-end gap-0.5">
                          <StatusMark
                            tone={
                              run.outcome === "succeeded"
                                ? "good"
                                : run.outcome === "running"
                                  ? "info"
                                  : run.outcome === "cancelled"
                                    ? "neutral"
                                    : "bad"
                            }
                            strong={run.outcome === "failed"}
                          >
                            {run.outcome}
                          </StatusMark>
                          <span className="text-2xs text-ink-3">
                            <RelativeTime at={run.started_at} />
                          </span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No agent runs">
                  Fork conflict resolution, bring-up, adopt, and delegated issues run the agent
                  here.
                </EmptyState>
              )
            }
          </QueryState>
        </Section>
      </div>
      {selectedRun ? (
        <Section title={`Transcript: ${selectedRun.kind}`}>
          <Transcript key={selectedRun.id} run={selectedRun} />
        </Section>
      ) : null}
      <Section title="Delegated work">
        <BranchRuns
          projectId={project.id}
          kinds={["delegate", "update-deps"]}
          empty="Delegate an issue from the Issues tab, or update dependencies from Actions."
        />
      </Section>
    </div>
  );
}
