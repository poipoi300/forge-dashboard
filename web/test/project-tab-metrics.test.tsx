import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { MetricsTab } from "@/features/project/tabs/MetricsTab";
import { NOW_SECONDS } from "./constants";
import { makeProject } from "./factories";
import {
  installHarness,
  type MockApi,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  timeText,
} from "./harness";
import {
  makeDetail,
  makeManifest,
  makeMetricDefinition,
  makeMetricPoint,
  makeMetricsState,
  makeProjectMetrics,
  makeReading,
} from "./project-tabs-support";

installHarness();

const HISTORY = "/api/services/metrics/actions/history" as const;
const REFRESH = "/api/services/metrics/actions/refresh" as const;

const project = makeProject("alpha");

type Definition = Schemas["Metric"];
type Points = Schemas["MetricPoint"][];

function mockMetrics(history: Record<string, Points> = {}): MockApi {
  return mockApi().action(HISTORY, (input) => {
    const metricId = (input as { metric_id: string }).metric_id;
    return { metric_id: metricId, points: history[metricId] ?? [] };
  });
}

function renderMetrics(definitions: Definition[]) {
  const detail = makeDetail(project, {
    manifest: definitions.length ? makeManifest(project, { metrics: definitions }) : null,
  });
  return renderWithProviders(<MetricsTab project={project} detail={detail} />);
}

function card(label: string): ReturnType<typeof within> {
  const heading = screen.getByRole("heading", { name: new RegExp(`^${label}`) });
  const article = heading.closest("article");
  if (!article) throw new Error(`no card for ${label}`);
  return within(article);
}

function toneOf(element: HTMLElement): string | null | undefined {
  return element.closest("[data-tone]")?.getAttribute("data-tone");
}

describe("MetricsTab without metrics", () => {
  test("shows only the refresh-all action when the manifest declares no metrics", async () => {
    mockMetrics();
    renderMetrics([]);
    expect(await screen.findByRole("button", { name: "Refresh all" })).toBeTruthy();
    expect(screen.queryByRole("article")).toBeNull();
  });
});

describe("MetricsTab refresh", () => {
  test("refreshes every metric of the project at once", async () => {
    const api = mockMetrics().action(REFRESH, makeProjectMetrics("alpha", []));
    const { user } = renderMetrics([makeMetricDefinition("loc"), makeMetricDefinition("todos")]);
    await user.click(await screen.findByRole("button", { name: "Refresh all" }));
    await waitFor(() =>
      expect(api.sent(REFRESH)).toEqual([{ project_id: "alpha", metric_id: null }]),
    );
    expect(await screen.findByText("Refresh metrics: done")).toBeTruthy();
  });

  test("refreshes just the metric whose button is clicked", async () => {
    const api = mockMetrics().action(REFRESH, makeProjectMetrics("alpha", []));
    const { user } = renderMetrics([
      makeMetricDefinition("loc", { label: "Lines of code" }),
      makeMetricDefinition("todos", { label: "Todos" }),
    ]);
    await user.click(await screen.findByRole("button", { name: "Refresh Todos" }));
    await waitFor(() =>
      expect(api.sent(REFRESH)).toEqual([{ project_id: "alpha", metric_id: "todos" }]),
    );
    expect(
      screen.getByRole("button", { name: "Refresh Lines of code" }).getAttribute("title"),
    ).toBe("Read Lines of code now");
  });
});

describe("MetricsTab cards", () => {
  test("shows a dash and the interval for a metric that was never read", async () => {
    mockMetrics();
    renderMetrics([makeMetricDefinition("loc", { label: "Lines", interval_s: 1800 })]);
    const lines = await screen.findByRole("heading", { name: "Lines" });
    const article = within(lines.closest("article") as HTMLElement);
    expect(article.getByText("—")).toBeTruthy();
    expect(article.getByText("every 30 min")).toBeTruthy();
    expect(article.queryByText(/read /)).toBeNull();
    expect(article.queryByText("pinned")).toBeNull();
    expect(article.queryByText(/thresholds:/)).toBeNull();
  });

  test("shows the description and pinned mark of a definition", async () => {
    mockMetrics();
    renderMetrics([
      makeMetricDefinition("loc", {
        label: "Lines",
        description: "Source lines under src",
        pinned: true,
      }),
    ]);
    await screen.findByRole("heading", { name: /Lines/ });
    const lines = card("Lines");
    expect(lines.getByText("pinned")).toBeTruthy();
    expect(lines.getByText("Source lines under src")).toBeTruthy();
  });

  test("renders each latest reading in its display kind with its age and interval", async () => {
    mockMetrics();
    seedStream({
      metrics: makeMetricsState("alpha", [
        makeReading("loc", { display: "number", unit: "loc", value: 1284 }),
        makeReading("cov", { display: "percent", value: 82 }),
        makeReading("build", { display: "duration", value: 90 }),
        makeReading("size", { display: "bytes", value: 1536 }),
        makeReading("branch", { display: "text", value: "main" }),
        makeReading("clean", { display: "text", value: true }),
        makeReading("unread", { display: "number", value: null }),
        makeReading("beta-only", { display: "number", value: 5 }),
      ]),
    });
    renderMetrics([
      makeMetricDefinition("loc", { label: "Lines", interval_s: 600 }),
      makeMetricDefinition("cov", { label: "Coverage", display: "percent" }),
      makeMetricDefinition("build", { label: "Build time", display: "duration" }),
      makeMetricDefinition("size", { label: "Bundle", display: "bytes" }),
      makeMetricDefinition("branch", { label: "Branch", display: "text" }),
      makeMetricDefinition("clean", { label: "Clean", display: "text" }),
      makeMetricDefinition("unread", { label: "Unread" }),
    ]);
    await screen.findByRole("heading", { name: "Lines" });
    const lines = card("Lines");
    expect(lines.getByText("1,284 loc")).toBeTruthy();
    expect(lines.getByText(timeText("5 min ago"))).toBeTruthy();
    expect(lines.getByText(/every 10 min/)).toBeTruthy();
    expect(card("Coverage").getByText("82%")).toBeTruthy();
    expect(card("Build time").getByText("1 min 30 s")).toBeTruthy();
    expect(card("Bundle").getByText("1.5 KB")).toBeTruthy();
    expect(card("Branch").getByText("main")).toBeTruthy();
    expect(card("Clean").getByText("yes")).toBeTruthy();
    expect(card("Unread").getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.queryByText("beta-only")).toBeNull();
  });

  test("shows this project's reading when another project has a metric with the same id", async () => {
    mockMetrics();
    seedStream({
      metrics: {
        projects: [
          makeProjectMetrics("beta", [makeReading("loc", { value: 111 })]),
          makeProjectMetrics("alpha", [makeReading("loc", { value: 222 })]),
        ],
      },
    });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    const lines = card("Lines");
    expect(await lines.findByText("222")).toBeTruthy();
    expect(lines.queryByText("111")).toBeNull();
  });

  test("shows only the interval for a reading that has no timestamp", async () => {
    mockMetrics();
    seedStream({
      metrics: makeMetricsState("alpha", [makeReading("loc", { value: 7, read_at: null })]),
    });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines", interval_s: 1800 })]);
    const lines = card("Lines");
    expect(await lines.findByText("7")).toBeTruthy();
    expect(lines.getByText("every 30 min")).toBeTruthy();
    expect(lines.queryByText(/read /)).toBeNull();
  });

  test("marks status readings with a tone and the status in the title", async () => {
    mockMetrics();
    seedStream({
      metrics: makeMetricsState("alpha", [
        makeReading("a", { value: 1, status: "ok" }),
        makeReading("b", { value: 2, status: "warn" }),
        makeReading("c", { value: 3, status: "fail" }),
      ]),
    });
    renderMetrics([
      makeMetricDefinition("a", { label: "Alpha" }),
      makeMetricDefinition("b", { label: "Bravo" }),
      makeMetricDefinition("c", { label: "Charlie" }),
    ]);
    await screen.findByRole("heading", { name: "Alpha" });
    const mark = (label: string): HTMLElement => card(label).getByTitle(/^status: /);
    expect(toneOf(mark("Alpha"))).toBe("good");
    expect(mark("Alpha").getAttribute("title")).toBe("status: ok");
    expect(toneOf(mark("Bravo"))).toBe("warn");
    expect(toneOf(mark("Charlie"))).toBe("bad");
    expect(mark("Charlie").getAttribute("title")).toBe("status: fail");
  });

  test("shows a failed reading as an error with the failure in its title and the detail beneath", async () => {
    mockMetrics();
    seedStream({
      metrics: makeMetricsState("alpha", [
        makeReading("loc", {
          value: null,
          error: "command exited 2",
          detail: "cloc is not installed",
        }),
      ]),
    });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    const lines = within(
      (await screen.findByRole("heading", { name: "Lines" })).closest("article") as HTMLElement,
    );
    const error = lines.getByText("error");
    expect(toneOf(error)).toBe("bad");
    expect(error.closest("[title]")?.getAttribute("title")).toBe("command exited 2");
    expect(lines.getByText("cloc is not installed")).toBeTruthy();
  });

  test("lists every threshold the manifest sets, including a zero bound", async () => {
    mockMetrics();
    renderMetrics([
      makeMetricDefinition("all", {
        label: "All bounds",
        thresholds: { fail_below: 50, warn_below: 70, warn_above: 90, fail_above: 99 },
      }),
      makeMetricDefinition("zero", {
        label: "Zero bound",
        thresholds: { fail_below: 0, warn_below: null, warn_above: null, fail_above: null },
      }),
      makeMetricDefinition("none", {
        label: "No bounds",
        thresholds: { fail_below: null, warn_below: null, warn_above: null, fail_above: null },
      }),
    ]);
    await screen.findByRole("heading", { name: "All bounds" });
    expect(
      card("All bounds").getByText(
        "thresholds: fail below 50 · warn below 70 · warn above 90 · fail above 99",
      ),
    ).toBeTruthy();
    expect(card("Zero bound").getByText("thresholds: fail below 0")).toBeTruthy();
    expect(card("No bounds").queryByText(/thresholds:/)).toBeNull();
  });
});

describe("MetricsTab history", () => {
  test("asks for each metric's history separately", async () => {
    const api = mockMetrics();
    renderMetrics([makeMetricDefinition("loc"), makeMetricDefinition("todos")]);
    await screen.findAllByText("No readings yet.");
    expect(api.sent(HISTORY)).toEqual(
      expect.arrayContaining([
        { project_id: "alpha", metric_id: "loc", limit: 200 },
        { project_id: "alpha", metric_id: "todos", limit: 200 },
      ]),
    );
    expect(api.sent(HISTORY)).toHaveLength(2);
  });

  test("shows loading rows while the history is pending", async () => {
    mockMetrics();
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    expect(card("Lines").getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(await card("Lines").findByText("No readings yet.")).toBeTruthy();
  });

  test("says there are no readings when the history is empty", async () => {
    mockMetrics();
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    expect(await card("Lines").findByText("No readings yet.")).toBeTruthy();
  });

  test("reports a history that cannot be read", async () => {
    mockMetrics().action(HISTORY, () => {
      throw new MockApiError(500, "history unavailable");
    });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    expect(await card("Lines").findByText("history unavailable")).toBeTruthy();
  });

  test("lists the latest readings newest first until two numeric ones make a chart", async () => {
    const points: Points = Array.from({ length: 8 }, (_, index) =>
      makeMetricPoint(NOW_SECONDS - (8 - index) * 3600, {
        value: index === 7 ? 42 : `text ${index}`,
      }),
    );
    mockMetrics({ loc: points });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines", unit: "loc" })]);
    const lines = card("Lines");
    expect(await lines.findByText("42 loc")).toBeTruthy();
    const items = lines.getAllByRole("listitem");
    expect(items).toHaveLength(6);
    expect(items[0]?.textContent).toContain("42 loc");
    expect(items[0]?.textContent).toContain("1 h ago");
    expect(items[1]?.textContent).toContain("text 6");
    expect(items[5]?.textContent).toContain("text 2");
    expect(lines.queryByText("text 1")).toBeNull();
    expect(lines.queryByRole("figure")).toBeNull();
  });

  test("shows a failed reading's first line and reveals its full output on demand", async () => {
    mockMetrics({
      loc: [
        makeMetricPoint(NOW_SECONDS - 600, {
          error: "cloc failed\nTraceback line 1\nTraceback line 2",
        }),
      ],
    });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    const lines = card("Lines");
    const summary = await lines.findByText("cloc failed");
    expect(summary.tagName).toBe("SUMMARY");
    const details = summary.closest("details") as HTMLDetailsElement;
    expect(details.querySelector("pre")?.textContent).toBe(
      "cloc failed\nTraceback line 1\nTraceback line 2",
    );
  });

  test("shows a single-line failure without a distinct detail body", async () => {
    mockMetrics({ loc: [makeMetricPoint(NOW_SECONDS - 600, { error: "timed out" })] });
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    const summary = await card("Lines").findByText("timed out", { selector: "summary" });
    expect(summary.closest("details")?.querySelector("pre")?.textContent).toBe("timed out");
  });

  test("draws a chart once two readings are numeric and offers every value as a table", async () => {
    mockMetrics({
      loc: [
        makeMetricPoint(NOW_SECONDS - 7200, { value: 1000 }),
        makeMetricPoint(NOW_SECONDS - 3600, { value: "n/a" }),
        makeMetricPoint(NOW_SECONDS - 1800, { value: 1500, status: "fail" }),
      ],
    });
    const { user } = renderMetrics([makeMetricDefinition("loc", { label: "Lines", unit: "loc" })]);
    const lines = card("Lines");
    expect(await lines.findByRole("figure", { name: "Lines over time" })).toBeTruthy();
    await user.click(lines.getByRole("button", { name: "Table" }));
    const rows = lines.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("1,500 loc");
    expect(rows[0]?.textContent).toContain("failed");
    expect(rows[1]?.textContent).toContain("1,000 loc");
    expect(rows[1]?.textContent).not.toContain("failed");
  });

  test("formats a duration metric's chart values as durations", async () => {
    mockMetrics({
      build: [
        makeMetricPoint(NOW_SECONDS - 7200, { value: 90 }),
        makeMetricPoint(NOW_SECONDS - 3600, { value: 95 }),
      ],
    });
    const { user } = renderMetrics([
      makeMetricDefinition("build", { label: "Build time", display: "duration" }),
    ]);
    const build = card("Build time");
    await build.findByRole("figure", { name: "Build time over time" });
    await user.click(build.getByRole("button", { name: "Table" }));
    expect(build.getByText("1 min 35 s")).toBeTruthy();
    expect(build.getByText("1 min 30 s")).toBeTruthy();
  });

  test("re-reads histories when the metrics service publishes a new revision", async () => {
    const api = mockMetrics();
    renderMetrics([makeMetricDefinition("loc", { label: "Lines" })]);
    await card("Lines").findByText("No readings yet.");
    expect(api.callsTo("POST", HISTORY)).toHaveLength(1);
    seedStream({ metrics: makeMetricsState("alpha", [makeReading("loc", { value: 1 })]) }, 2);
    await waitFor(() => expect(api.callsTo("POST", HISTORY)).toHaveLength(2));
  });
});
