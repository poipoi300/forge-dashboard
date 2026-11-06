import { describe, expect, test } from "bun:test";
import { act, screen, waitFor } from "@testing-library/react";
import { Route } from "wouter";
import type { Schemas } from "@/api/client";
import { type LogState, useJobLog, useServiceLog } from "@/api/logs";
import { errorText } from "@/components/states";
import { deferred, makeJobLogChunk, makeServiceLogs } from "./api-support";
import { isPresent } from "./components-support";
import {
  emitEvent,
  installHarness,
  MockApiError,
  mockApi,
  renderWithProviders,
  settle,
} from "./harness";

installHarness();

const JOB_LOG = "/api/services/jobs/actions/log" as const;
const SERVICE_LOGS = "/api/services/services/actions/logs" as const;

function LogOutput({ state }: { state: LogState }) {
  return (
    <>
      <output aria-label="log state">{state.loading ? "loading" : "ready"}</output>
      {state.error ? <p role="alert">{errorText(state.error)}</p> : null}
      <output aria-label="lines">{state.lines.join("\n")}</output>
    </>
  );
}

function JobLogView({ jobId }: { jobId: string | null }) {
  return <LogOutput state={useJobLog(jobId)} />;
}

/** The job probe reads its id from the router, so a navigation switches jobs. */
function JobLogRoute() {
  return <Route path="/jobs/:id">{(params) => <JobLogView jobId={params.id} />}</Route>;
}

function ServiceLogView({
  serviceId,
  intervalMs,
}: {
  serviceId: string | null;
  intervalMs: number;
}) {
  return <LogOutput state={useServiceLog("alpha", serviceId, intervalMs)} />;
}

function linesText(): string {
  return screen.getByLabelText("lines").textContent ?? "";
}

describe("useJobLog", () => {
  test("reads the job's log from the start and reports ready once it has arrived", async () => {
    const api = mockApi().action(JOB_LOG, (input) =>
      makeJobLogChunk(input.job_id, input.offset, ["one", "two"], { done: true }),
    );
    renderWithProviders(<JobLogView jobId="job-1" />);
    await waitFor(() => expect(linesText()).toBe("one\ntwo"));
    expect(screen.getByLabelText("log state").textContent).toBe("ready");
    expect(api.sent(JOB_LOG)).toEqual([{ job_id: "job-1", offset: 0, limit: 50_000 }]);
  });

  test("reports loading until the first read returns", async () => {
    const read = deferred<Schemas["JobLogChunk"]>();
    mockApi().action(JOB_LOG, () => read.promise);
    renderWithProviders(<JobLogView jobId="job-1" />);
    expect(screen.getByLabelText("log state").textContent).toBe("loading");
    await act(async () => read.resolve(makeJobLogChunk("job-1", 0, ["a"])));
    await waitFor(() => expect(screen.getByLabelText("log state").textContent).toBe("ready"));
  });

  test("appends job log events that continue the log", async () => {
    mockApi().action(JOB_LOG, (input) => makeJobLogChunk(input.job_id, 0, ["a", "b", "c"]));
    renderWithProviders(<JobLogView jobId="job-1" />);
    await waitFor(() => expect(linesText()).toBe("a\nb\nc"));

    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 3, lines: ["d"] } });
    expect(linesText()).toBe("a\nb\nc\nd");
  });

  test("keeps only the event lines the log does not already hold", async () => {
    mockApi().action(JOB_LOG, (input) => makeJobLogChunk(input.job_id, 0, ["a", "b", "c"]));
    renderWithProviders(<JobLogView jobId="job-1" />);
    await waitFor(() => expect(linesText()).toBe("a\nb\nc"));

    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 1, lines: ["b", "c", "d"] } });
    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 0, lines: ["a"] } });
    expect(linesText()).toBe("a\nb\nc\nd");
  });

  test("applies events that arrive before the first read returns, without repeating lines", async () => {
    const read = deferred<Schemas["JobLogChunk"]>();
    mockApi().action(JOB_LOG, () => read.promise);
    renderWithProviders(<JobLogView jobId="job-1" />);

    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 2, lines: ["c", "d"] } });
    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 3, lines: ["d"] } });
    await act(async () => read.resolve(makeJobLogChunk("job-1", 0, ["a", "b", "c"])));

    await waitFor(() => expect(linesText()).toBe("a\nb\nc\nd"));
  });

  test("ignores events for other jobs", async () => {
    mockApi().action(JOB_LOG, (input) => makeJobLogChunk(input.job_id, 0, ["a"]));
    renderWithProviders(<JobLogView jobId="job-1" />);
    await waitFor(() => expect(linesText()).toBe("a"));

    emitEvent({ type: "job.log", data: { job_id: "job-2", offset: 1, lines: ["other"] } });
    expect(linesText()).toBe("a");
  });

  test("shows the server's reason when the first read fails", async () => {
    mockApi().action(JOB_LOG, () => {
      throw new MockApiError(404, "no such job");
    });
    renderWithProviders(<JobLogView jobId="job-404" />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("no such job")).toBeTruthy();
    expect(screen.getByLabelText("log state").textContent).toBe("ready");
  });

  test("drops a read that finishes after the viewer has moved to another job", async () => {
    const late = deferred<Schemas["JobLogChunk"]>();
    mockApi().action(JOB_LOG, (input) =>
      input.job_id === "job-1" ? late.promise : makeJobLogChunk(input.job_id, 0, ["fresh"]),
    );
    const { navigate } = renderWithProviders(<JobLogRoute />, { path: "/jobs/job-1" });
    navigate("/jobs/job-2");
    await waitFor(() => expect(linesText()).toBe("fresh"));

    await act(async () => late.resolve(makeJobLogChunk("job-1", 0, ["stale"])));
    await settle();
    expect(linesText()).toBe("fresh");
  });

  test("switches to the new job's log and ignores the previous job's events", async () => {
    mockApi().action(JOB_LOG, (input) =>
      makeJobLogChunk(input.job_id, 0, [input.job_id === "job-1" ? "a" : "x"]),
    );
    const { navigate } = renderWithProviders(<JobLogRoute />, { path: "/jobs/job-1" });
    await waitFor(() => expect(linesText()).toBe("a"));

    navigate("/jobs/job-2");
    await waitFor(() => expect(linesText()).toBe("x"));

    emitEvent({ type: "job.log", data: { job_id: "job-1", offset: 1, lines: ["late"] } });
    expect(linesText()).toBe("x");
    emitEvent({ type: "job.log", data: { job_id: "job-2", offset: 1, lines: ["y"] } });
    expect(linesText()).toBe("x\ny");
  });

  test("makes no request while there is no job", async () => {
    const api = mockApi();
    renderWithProviders(<JobLogView jobId={null} />);
    await settle();
    expect(api.callsTo("POST", JOB_LOG)).toEqual([]);
  });
});

describe("useServiceLog", () => {
  test("reads a service's output from the start, then only the lines past its offset", async () => {
    const api = mockApi().action(SERVICE_LOGS, (input) => {
      if (input.offset === 0) return makeServiceLogs(["boot"], { next_offset: 1, total: 1 });
      if (input.offset === 1) return makeServiceLogs(["ready"], { next_offset: 2, total: 2 });
      return makeServiceLogs([], { next_offset: 2, total: 2 });
    });
    renderWithProviders(<ServiceLogView serviceId="web" intervalMs={5} />);
    await waitFor(() => expect(linesText()).toBe("boot\nready"));
    expect(api.sent(SERVICE_LOGS).slice(0, 2)).toEqual([
      { project_id: "alpha", service_id: "web", offset: 0 },
      { project_id: "alpha", service_id: "web", offset: 1 },
    ]);
  });

  test("starts again from the top when the service's buffer restarted", async () => {
    mockApi().action(SERVICE_LOGS, (input) => {
      if (input.offset === 0)
        return makeServiceLogs(["a", "b", "c", "d", "e"], { next_offset: 5, total: 5 });
      if (input.offset === 5) return makeServiceLogs(["x", "y"], { next_offset: 2, total: 2 });
      return makeServiceLogs([], { next_offset: 2, total: 2 });
    });
    renderWithProviders(<ServiceLogView serviceId="web" intervalMs={5} />);
    await waitFor(() => expect(linesText()).toBe("x\ny"));
  });

  test("keeps its lines through a failed read, then appends once reads recover", async () => {
    const recovery = deferred<Schemas["ServiceLogs"]>();
    let reads = 0;
    mockApi().action(SERVICE_LOGS, () => {
      reads += 1;
      if (reads === 1) return makeServiceLogs(["boot"], { next_offset: 1, total: 1 });
      if (reads === 2) throw new MockApiError(500, "log store unavailable");
      return recovery.promise;
    });
    renderWithProviders(<ServiceLogView serviceId="web" intervalMs={5} />);
    expect(await screen.findByText("log store unavailable")).toBeTruthy();
    expect(linesText()).toBe("boot");

    await act(async () =>
      recovery.resolve(makeServiceLogs(["recovered"], { next_offset: 2, total: 2 })),
    );
    await waitFor(() => expect(linesText()).toBe("boot\nrecovered"));
    expect(isPresent(screen.queryByRole("alert"))).toBe(false);
  });

  test("makes no request while there is no service", async () => {
    const api = mockApi();
    renderWithProviders(<ServiceLogView serviceId={null} intervalMs={5} />);
    await settle();
    expect(api.callsTo("POST", SERVICE_LOGS)).toEqual([]);
  });
});
