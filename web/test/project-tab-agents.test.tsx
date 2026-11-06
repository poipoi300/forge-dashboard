import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";
import { makeProject, makeProjectsState } from "./factories";
import { installHarness, MockApiError, mockApi, seedStream, timeText } from "./harness";
import {
  type AgentRunRecord,
  type BranchRun,
  makeAgentRun,
  makeBranchRun,
  makeSessionsOutput,
  openProjectTab,
} from "./project-core-support";

installHarness();

const SESSIONS = "/api/services/agents/actions/sessions" as const;
const RUNS = "/api/services/agents/actions/runs" as const;
const TRANSCRIPT = "/api/services/agents/actions/transcript" as const;
const BRANCH_RUNS = "/api/services/agents/actions/branch-runs" as const;

type Sessions = Schemas["Sessions"];
type TranscriptPage = Schemas["Transcript"];
type TranscriptRecord = TranscriptPage["records"][number];

interface AgentsScenario {
  sessions?: Sessions;
  runs?: AgentRunRecord[];
  transcript?: (runId: string, limit: number) => TranscriptPage;
  branchRuns?: BranchRun[];
}

function transcriptPage(
  runId: string,
  records: TranscriptRecord[],
  overrides: Partial<TranscriptPage> = {},
): TranscriptPage {
  return {
    run_id: runId,
    records,
    next_offset: records.length,
    done: true,
    prompt: null,
    ...overrides,
  };
}

async function openAgents(scenario: AgentsScenario = {}) {
  const api = mockApi()
    .action(SESSIONS, scenario.sessions ?? makeSessionsOutput())
    .action(RUNS, { runs: scenario.runs ?? [] })
    .action(BRANCH_RUNS, { runs: scenario.branchRuns ?? [] })
    .action(TRANSCRIPT, (input) =>
      scenario.transcript
        ? scenario.transcript(input.run_id, input.limit ?? 200)
        : transcriptPage(input.run_id, []),
    );
  const opened = await openProjectTab("agents", {}, api);
  return { ...opened };
}

describe("AgentsTab Claude Code sessions", () => {
  test("asks for the sessions of this project's folder", async () => {
    const { api } = await openAgents();
    await screen.findByText("No Claude Code sessions recorded for this folder.");
    expect(api.sent(SESSIONS)).toEqual([{ project_id: "alpha" }]);
  });

  test("says when no session was recorded", async () => {
    await openAgents();
    expect(
      await screen.findByText("No Claude Code sessions recorded for this folder."),
    ).toBeTruthy();
  });

  test("lists sessions with their first prompt, message count, length and age", async () => {
    await openAgents({
      sessions: makeSessionsOutput({
        sessions: [
          {
            id: "s1",
            start: NOW_SECONDS - 7200,
            end: NOW_SECONDS - 6900,
            duration_s: 252,
            first_prompt: "Add a retry to the uploader",
            cwd: "C:/code/alpha",
            message_count: 12,
            transcript_path: "C:/claude/s1.jsonl",
          },
          {
            id: "s2",
            start: NOW_SECONDS - 86_400 * 3,
            end: NOW_SECONDS - 86_400 * 3 + 30,
            duration_s: 30,
            first_prompt: null,
            cwd: null,
            message_count: 2,
            transcript_path: "C:/claude/s2.jsonl",
          },
        ],
      }),
    });
    expect(await screen.findByText("Add a retry to the uploader")).toBeTruthy();
    expect(screen.getByText("12 messages · 4 min 12 s")).toBeTruthy();
    expect(screen.getByText("(no prompt recorded)")).toBeTruthy();
    expect(screen.getByText("2 messages · 30 s")).toBeTruthy();
    expect(screen.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(screen.getByText(timeText("3 d ago"))).toBeTruthy();
  });

  test("announces claude processes running in the folder right now", async () => {
    await openAgents({
      sessions: makeSessionsOutput({
        processes: [
          {
            pid: 4242,
            started_at: NOW_SECONDS - 600,
            cwd: "C:/code/alpha",
            cmdline: ["claude"],
          },
          {
            pid: 77,
            started_at: NOW_SECONDS - 60,
            cwd: "C:/code/alpha/web",
            cmdline: ["claude", "-p"],
          },
        ],
      }),
    });
    const first = await screen.findByText("claude running (pid 4242)");
    expect(first.closest("li")?.textContent).toBe(
      "claude running (pid 4242) since 10 min ago in C:/code/alpha",
    );
    expect(screen.getByText("claude running (pid 77)").closest("li")?.textContent).toBe(
      "claude running (pid 77) since 1 min ago in C:/code/alpha/web",
    );
  });

  test("shows why the sessions could not be read and retries on request", async () => {
    let failing = true;
    const api = mockApi()
      .action(SESSIONS, () => {
        if (failing) throw new MockApiError(500, "claude projects dir missing");
        return makeSessionsOutput();
      })
      .action(RUNS, { runs: [] })
      .action(BRANCH_RUNS, { runs: [] });
    const { rendered } = await openProjectTab("agents", {}, api);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("claude projects dir missing");
    failing = false;
    await rendered.user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText("No Claude Code sessions recorded for this folder."),
    ).toBeTruthy();
  });
});

describe("AgentsTab dashboard agent runs", () => {
  test("asks for the project's thirty latest agent runs", async () => {
    const { api } = await openAgents();
    await screen.findByText("No agent runs");
    expect(api.sent(RUNS)).toEqual([{ project_id: "alpha", limit: 30 }]);
  });

  test("explains what produces agent runs when there are none", async () => {
    await openAgents();
    expect(await screen.findByText("No agent runs")).toBeTruthy();
    expect(screen.getByText(/Fork conflict resolution, bring-up, adopt/)).toBeTruthy();
  });

  test("describes each run with its model, turns, cost, length and outcome", async () => {
    await openAgents({
      runs: [
        makeAgentRun("run-1", {
          kind: "bring-up",
          model: "claude-sonnet-5-5",
          turns: 12,
          cost_usd: 0.4567,
          duration_ms: 125_000,
          outcome: "succeeded",
        }),
        makeAgentRun("run-2", { kind: "fork-conflict", outcome: "running" }),
      ],
    });
    const first = (await screen.findByText("bring-up")).closest("button") as HTMLElement;
    expect(first.textContent).toContain("claude-sonnet-5-5 · 12 turns · $0.457 · 2 min 5 s");
    expect(first.textContent).toContain("succeeded");
    expect(first.textContent).toContain("10 min ago");
    const second = screen.getByText("fork-conflict").closest("button") as HTMLElement;
    expect(second.textContent).toContain("default model");
    expect(second.textContent).not.toContain("turns");
    expect(second.textContent).not.toContain("$");
  });

  test.each([
    ["succeeded", "good"],
    ["running", "info"],
    ["cancelled", "neutral"],
    ["failed", "bad"],
    ["timeout", "bad"],
    ["harness-missing", "bad"],
  ] as const)("marks a %s run with the %s tone", async (outcome, tone) => {
    await openAgents({ runs: [makeAgentRun("run-1", { outcome })] });
    const run = (await screen.findByText("bring-up")).closest("button") as HTMLElement;
    const mark = within(run).getByText(outcome).closest("[data-tone]");
    expect(mark?.getAttribute("data-tone")).toBe(tone);
  });

  test("shows why the runs could not be read", async () => {
    const api = mockApi()
      .action(SESSIONS, makeSessionsOutput())
      .action(RUNS, () => {
        throw new MockApiError(500, "run store locked");
      })
      .action(BRANCH_RUNS, { runs: [] });
    await openProjectTab("agents", {}, api);
    expect((await screen.findByText("run store locked")).closest("[role=alert]")).toBeTruthy();
  });

  test("shows no transcript until a run is chosen", async () => {
    const { api } = await openAgents({ runs: [makeAgentRun("run-1")] });
    await screen.findByText("bring-up");
    expect(screen.queryByText(/^Transcript:/)).toBeNull();
    expect(api.callsTo("POST", TRANSCRIPT)).toEqual([]);
  });
});

describe("AgentsTab transcript", () => {
  async function openTranscript(
    records: TranscriptRecord[],
    overrides: Partial<TranscriptPage> = {},
  ) {
    const opened = await openAgents({
      runs: [makeAgentRun("run-1", { kind: "bring-up" })],
      transcript: (runId) => transcriptPage(runId, records, overrides),
    });
    await opened.rendered.user.click(await screen.findByText("bring-up"));
    await screen.findByText("Transcript: bring-up");
    return opened;
  }

  test("asks for the first two hundred records of the chosen run", async () => {
    const { api } = await openTranscript([{ type: "system" }]);
    await screen.findByText("session started");
    expect(api.sent(TRANSCRIPT)).toEqual([{ run_id: "run-1", offset: 0, limit: 200 }]);
  });

  test("shows the brief the agent was given", async () => {
    await openTranscript([], { prompt: "Bring this repo up to standards." });
    const summary = await screen.findByText("The brief the agent was given");
    expect(summary.closest("details")?.querySelector("pre")?.textContent).toBe(
      "Bring this repo up to standards.",
    );
  });

  test("omits the brief when the run recorded none", async () => {
    await openTranscript([{ type: "system" }], { prompt: null });
    await screen.findByText("session started");
    expect(screen.queryByText("The brief the agent was given")).toBeNull();
  });

  test("announces the session with its model and working directory when known", async () => {
    await openTranscript([
      { type: "system", model: "claude-sonnet-5-5", cwd: "C:/data/worktrees/run-1" },
      { type: "system" },
    ]);
    expect(
      await screen.findByText("session started · claude-sonnet-5-5 · C:/data/worktrees/run-1"),
    ).toBeTruthy();
    expect(screen.getByText("session started")).toBeTruthy();
  });

  test("renders the assistant's text as markdown and the user's text as plain text", async () => {
    await openTranscript([
      { type: "user", message: { content: "Fix the build **now**" } },
      {
        type: "assistant",
        message: { content: [{ type: "text", text: "I will look at **the build**." }] },
      },
      { type: "assistant", message: { content: "A plain string reply" } },
    ]);
    const user = await screen.findByText("Fix the build **now**");
    expect(user.tagName).toBe("P");
    const bold = screen.getByText("the build");
    expect(bold.tagName).toBe("STRONG");
    expect(screen.getByText("A plain string reply")).toBeTruthy();
  });

  test("shows a tool call as a collapsible block with its input", async () => {
    await openTranscript([
      {
        type: "assistant",
        message: { content: [{ type: "tool_use", name: "Bash", input: { command: "ls -la" } }] },
      },
    ]);
    const summary = await screen.findByText("Bash");
    const details = summary.closest("details") as HTMLElement;
    expect(details.querySelector("pre")?.textContent).toBe('{\n  "command": "ls -la"\n}');
  });

  test("shows tool results as text, joined text parts, or JSON, flagging errors", async () => {
    await openTranscript([
      {
        type: "user",
        message: {
          content: [
            { type: "tool_result", content: "plain output" },
            {
              type: "tool_result",
              is_error: true,
              content: [
                { type: "text", text: "line one" },
                { type: "text", text: "line two" },
              ],
            },
            { type: "tool_result", content: [{ type: "image" }, null] },
            { type: "tool_result", content: { exit_code: 2 } },
          ],
        },
      },
    ]);
    await screen.findByText("tool error");
    const blocks = Array.from(document.querySelectorAll("details")).map((details) => ({
      label: details.querySelector("summary")?.textContent,
      body: details.querySelector("pre")?.textContent,
    }));
    expect(blocks).toEqual([
      { label: "tool result", body: "plain output" },
      { label: "tool error", body: "line one\nline two" },
      { label: "tool result", body: "\n" },
      { label: "tool result", body: '{\n  "exit_code": 2\n}' },
    ]);
  });

  test("skips message parts it has no view for and empty text", async () => {
    await openTranscript([
      {
        type: "assistant",
        message: { content: [{ type: "thinking" }, { type: "text", text: "" }] },
      },
      { type: "assistant" },
      { type: "user", message: {} },
      { type: "system" },
    ]);
    await screen.findByText("session started");
    expect(document.querySelectorAll("details")).toHaveLength(0);
  });

  test("summarises how the run finished with turns, cost, length and result", async () => {
    await openTranscript([
      {
        type: "result",
        subtype: "success",
        num_turns: 3,
        total_cost_usd: 0.1234,
        duration_ms: 65_000,
        result: "All **standards** now pass.",
      },
    ]);
    const finished = await screen.findByText(/^finished: success/);
    expect(finished.textContent).toBe("finished: success · 3 turns · $0.123 · 1 min 5 s");
    expect(screen.getByText("standards").tagName).toBe("STRONG");
  });

  test("summarises a finish that carries only a subtype", async () => {
    await openTranscript([{ type: "result", subtype: "error_max_turns" }]);
    expect((await screen.findByText(/^finished:/)).textContent).toBe("finished: error_max_turns");
  });

  test("shows records of an unknown type as raw JSON", async () => {
    await openTranscript([{ type: "queue-operation", id: 7 }, { id: 8 }]);
    expect(await screen.findByText('{"type":"queue-operation","id":7}')).toBeTruthy();
    expect(screen.getByText('{"id":8}')).toBeTruthy();
  });

  test("offers more records only while the transcript is not complete", async () => {
    await openTranscript([{ type: "system" }], { done: true });
    await screen.findByText("session started");
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  test("loads two hundred more records each time Show more is pressed", async () => {
    const { api, rendered } = await openTranscript([{ type: "system" }], { done: false });
    await rendered.user.click(await screen.findByRole("button", { name: "Show more" }));
    await waitFor(() =>
      expect(api.sent(TRANSCRIPT).at(-1)).toEqual({ run_id: "run-1", offset: 0, limit: 400 }),
    );
    await rendered.user.click(await screen.findByRole("button", { name: "Show more" }));
    await waitFor(() =>
      expect(api.sent(TRANSCRIPT).at(-1)).toEqual({ run_id: "run-1", offset: 0, limit: 600 }),
    );
  });

  test("shows why the transcript could not be read", async () => {
    const api = mockApi()
      .action(SESSIONS, makeSessionsOutput())
      .action(RUNS, { runs: [makeAgentRun("run-1")] })
      .action(BRANCH_RUNS, { runs: [] })
      .action(TRANSCRIPT, () => {
        throw new MockApiError(404, "transcript file was removed");
      });
    const { rendered } = await openProjectTab("agents", {}, api);
    await rendered.user.click(await screen.findByText("bring-up"));
    expect(
      (await screen.findByText("transcript file was removed")).closest("[role=alert]"),
    ).toBeTruthy();
  });

  test("switches to another run's transcript and starts it from the top", async () => {
    const { api, rendered } = await openAgents({
      runs: [makeAgentRun("run-1", { kind: "bring-up" }), makeAgentRun("run-2", { kind: "adopt" })],
      transcript: (runId, limit) =>
        transcriptPage(runId, [{ type: "system", model: `model-of-${runId}` }], {
          done: limit > 200,
        }),
    });
    await rendered.user.click(await screen.findByText("bring-up"));
    await rendered.user.click(await screen.findByRole("button", { name: "Show more" }));
    await screen.findByText("session started · model-of-run-1");
    await rendered.user.click(screen.getByText("adopt"));
    expect(await screen.findByText("Transcript: adopt")).toBeTruthy();
    expect(await screen.findByText("session started · model-of-run-2")).toBeTruthy();
    expect(api.sent(TRANSCRIPT).at(-1)).toEqual({ run_id: "run-2", offset: 0, limit: 200 });
  });

  test("picks up new records when the agents service publishes a new revision", async () => {
    let records: TranscriptRecord[] = [{ type: "system", model: "first" }];
    const { rendered } = await openAgents({
      runs: [makeAgentRun("run-1")],
      transcript: (runId) => transcriptPage(runId, records),
    });
    await rendered.user.click(await screen.findByText("bring-up"));
    await screen.findByText("session started · first");
    records = [...records, { type: "result", subtype: "success" }];
    seedStream(
      {
        projects: makeProjectsState([makeProject("alpha")]),
        agents: {
          harness_available: true,
          harness_executable: "claude",
          runs: [],
          branch_runs: [],
          insight: [],
        },
      },
      2,
    );
    expect(await screen.findByText("finished: success")).toBeTruthy();
    expect(screen.getByText("session started · first")).toBeTruthy();
  });
});

describe("AgentsTab delegated work", () => {
  test("asks for the project's branch runs", async () => {
    const { api } = await openAgents();
    await screen.findByText(/Delegate an issue from the Issues tab/);
    expect(api.sent(BRANCH_RUNS)).toEqual([{ project_id: "alpha", kind: null }]);
  });

  test("shows delegated and dependency-update runs but not other kinds", async () => {
    await openAgents({
      branchRuns: [
        makeBranchRun(1, { kind: "delegate", title: "Fix issue 12" }),
        makeBranchRun(2, { kind: "update-deps", title: "Update dependencies" }),
        makeBranchRun(3, { kind: "standards", title: "Standards bring-up" }),
        makeBranchRun(4, { kind: "adopt", title: "Adopt into forge" }),
      ],
    });
    expect(await screen.findByText("Fix issue 12")).toBeTruthy();
    expect(screen.getByText("Update dependencies")).toBeTruthy();
    expect(screen.queryByText("Standards bring-up")).toBeNull();
    expect(screen.queryByText("Adopt into forge")).toBeNull();
  });

  test("points to where delegated work starts when there is none", async () => {
    await openAgents({ branchRuns: [makeBranchRun(3, { kind: "standards" })] });
    expect(
      await screen.findByText(
        "Delegate an issue from the Issues tab, or update dependencies from Actions.",
      ),
    ).toBeTruthy();
  });
});
