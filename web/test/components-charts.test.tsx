import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { niceTicks, RunStrip, TrendChart, type TrendPoint } from "@/components/charts";
import { absolute } from "@/lib/time";
import { isPresent, type ResizeObserverStub, stubResizeObserver } from "./components-support";
import { NOW_SECONDS } from "./constants";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

const formatMs = (value: number) => `${value} ms`;

const OLDEST: TrendPoint = { at: NOW_SECONDS - 7200, value: 10 };
const FAILED: TrendPoint = { at: NOW_SECONDS - 3600, value: 40, bad: true, note: "exit 1" };
const NEWEST: TrendPoint = { at: NOW_SECONDS, value: 25 };
const READINGS: TrendPoint[] = [OLDEST, FAILED, NEWEST];

describe("niceTicks", () => {
  test.each([
    ["a range that starts at zero", 0, 100, [0, 50, 100]],
    ["a small range in steps of five", 0, 10, [0, 5, 10]],
    ["a sub-unit range in halves", 0, 1, [0, 0.5, 1]],
    ["a range that starts off zero in steps of one", 3, 7, [3, 4, 5, 6, 7]],
    ["a range that crosses zero", -10, 10, [-10, -5, 0, 5, 10]],
    ["a range whose step rounds up to ten", 0, 32, [0, 10, 20, 30]],
    ["a range whose step rounds up to two", 0, 6, [0, 2, 4, 6]],
    ["a range whose step is one", 0, 4, [0, 1, 2, 3, 4]],
    ["a large range in steps of half a million", 0, 1_000_000, [0, 500_000, 1_000_000]],
    ["a range that crosses zero in halves", -0.5, 0.5, [-0.5, 0, 0.5]],
  ])("covers %s with round ticks", (_label, min, max, expected) => {
    expect(niceTicks(min, max)).toEqual(expected);
  });

  test("honours a requested tick count", () => {
    expect(niceTicks(0, 100, 10)).toEqual([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]);
  });

  test("gives a flat positive series a band that never dips below zero", () => {
    expect(niceTicks(5, 5)).toEqual([0, 5, 10]);
  });

  test("gives a flat zero series a band above zero", () => {
    expect(niceTicks(0, 0)).toEqual([0, 0.5, 1]);
  });

  test("gives a flat negative series a band below its value", () => {
    expect(niceTicks(-5, -5)).toEqual([-10, -5, 0]);
  });
});

describe("TrendChart", () => {
  let resize: ResizeObserverStub;
  beforeEach(() => {
    resize = stubResizeObserver();
  });
  afterEach(() => resize.restore());

  test("says there are no readings yet when the series is empty", () => {
    renderWithProviders(<TrendChart title="Build time" points={[]} format={formatMs} />);
    expect(screen.getByText("No readings yet.")).toBeTruthy();
    expect(isPresent(screen.queryByRole("img"))).toBe(false);
  });

  test("draws nothing until the container has a measured width", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    expect(isPresent(screen.queryByRole("img"))).toBe(false);
    resize.latest().resize(400);
    expect(screen.getByRole("img", { name: /Build time: 3 readings, latest 25 ms/ })).toBeTruthy();
  });

  test("labels the axis from zero and the first and last reading times", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    resize.latest().resize(400);
    for (const tick of ["0 ms", "10 ms", "20 ms", "30 ms", "40 ms"]) {
      expect(screen.getByText(tick)).toBeTruthy();
    }
    expect(screen.getByText(absolute(NOW_SECONDS - 7200))).toBeTruthy();
    expect(screen.getByText(absolute(NOW_SECONDS))).toBeTruthy();
  });

  test("starts the axis at the lowest reading when it is not zero-based", () => {
    const steady: TrendPoint[] = [
      { at: NOW_SECONDS - 60, value: 50 },
      { at: NOW_SECONDS, value: 60 },
    ];
    renderWithProviders(
      <TrendChart title="Latency" points={steady} format={formatMs} zeroBased={false} />,
    );
    resize.latest().resize(400);
    expect(screen.getByText("50 ms")).toBeTruthy();
    expect(screen.getByText("55 ms")).toBeTruthy();
    expect(screen.getByText("60 ms")).toBeTruthy();
    expect(isPresent(screen.queryByText("0 ms"))).toBe(false);
  });

  test("draws a flat series inside a band around its value", () => {
    const flat: TrendPoint[] = [
      { at: NOW_SECONDS - 120, value: 5 },
      { at: NOW_SECONDS - 60, value: 5 },
      { at: NOW_SECONDS, value: 5 },
    ];
    renderWithProviders(
      <TrendChart title="Queue" points={flat} format={formatMs} zeroBased={false} />,
    );
    resize.latest().resize(400);
    expect(screen.getByRole("img", { name: /latest 5 ms/ })).toBeTruthy();
  });

  test("centres a single reading rather than placing it on an axis edge", () => {
    renderWithProviders(
      <TrendChart title="Single" points={[{ at: NOW_SECONDS, value: 7 }]} format={formatMs} />,
    );
    resize.latest().resize(400);
    expect(screen.getByRole("img", { name: /latest 7 ms/ })).toBeTruthy();
  });

  test("snaps the crosshair and tooltip to the nearest reading under the pointer", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    resize.latest().resize(400);
    const chart = screen.getByRole("img", { name: /Build time/ });
    fireEvent.pointerMove(chart, { clientX: 215 });
    const tooltip = screen.getByRole("status");
    expect(within(tooltip).getByText("40 ms")).toBeTruthy();
    expect(within(tooltip).getByText("failed")).toBeTruthy();
    expect(within(tooltip).getByText("exit 1")).toBeTruthy();
    expect(within(tooltip).getByText(absolute(NOW_SECONDS - 3600))).toBeTruthy();
  });

  test("hides the tooltip when the pointer leaves the chart", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    resize.latest().resize(400);
    const chart = screen.getByRole("img", { name: /Build time/ });
    fireEvent.pointerMove(chart, { clientX: 386 });
    expect(screen.getByRole("status").textContent).toContain("25 ms");
    fireEvent.pointerLeave(chart);
    expect(isPresent(screen.queryByRole("status"))).toBe(false);
  });

  test("redraws at the new width when the container is resized", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    resize.latest().resize(400);
    const chart = screen.getByRole("img", { name: /Build time/ });
    expect(chart.getAttribute("width")).toBe("400");
    resize.latest().resize(640);
    expect(screen.getByRole("img", { name: /Build time/ }).getAttribute("width")).toBe("640");
  });

  test("stops observing the container when the chart unmounts", () => {
    renderWithProviders(<TrendChart title="Build time" points={READINGS} format={formatMs} />);
    const observer = resize.latest();
    expect(observer.targets.length).toBe(1);
    cleanup();
    expect(observer.disconnected).toBe(true);
  });

  test("lists every reading newest first in the table view, marking failures", async () => {
    const { user } = renderWithProviders(
      <TrendChart title="Build time" points={[FAILED, NEWEST, OLDEST]} format={formatMs} />,
    );
    resize.latest().resize(400);
    await user.click(screen.getByRole("button", { name: "Table" }));
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(rows[0]?.textContent).toBe("whenvalue");
    expect(rows[1]?.textContent).toContain("25 ms");
    expect(rows[2]?.textContent).toBe(`${absolute(FAILED.at)}40 msfailed`);
    expect(rows[3]?.textContent).toContain("10 ms");
    expect(isPresent(screen.queryByRole("img"))).toBe(false);
  });

  test("switches back from the table to the chart", async () => {
    const { user } = renderWithProviders(
      <TrendChart title="Build time" points={READINGS} format={formatMs} />,
    );
    resize.latest().resize(400);
    await user.click(screen.getByRole("button", { name: "Table" }));
    await user.click(screen.getByRole("button", { name: "Chart" }));
    expect(screen.getByRole("img", { name: /Build time/ })).toBeTruthy();
  });

  test("shows an empty table with no rows when there are no readings", async () => {
    const { user } = renderWithProviders(
      <TrendChart title="Build time" points={[]} format={formatMs} />,
    );
    await user.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getAllByRole("row")).toHaveLength(1);
  });
});

describe("RunStrip", () => {
  test("states how many runs passed and lists each run oldest first", () => {
    renderWithProviders(
      <RunStrip
        label="Gates"
        runs={[
          { at: NOW_SECONDS - 3600, passed: true },
          { at: NOW_SECONDS - 3 * 3600, passed: true },
          { at: NOW_SECONDS - 2 * 3600, passed: false, note: "3 tests failed" },
        ]}
      />,
    );
    const strip = screen.getByRole("img", { name: "Gates: 2 of 3 runs passed" });
    const runs = Array.from(strip.children);
    expect(runs).toHaveLength(3);
    expect(runs[0]?.getAttribute("title")).toBe(`passed · ${absolute(NOW_SECONDS - 3 * 3600)}`);
    expect(runs[1]?.getAttribute("title")).toBe(
      `failed · ${absolute(NOW_SECONDS - 2 * 3600)} · 3 tests failed`,
    );
    expect(screen.getByText("2 of 3 passed")).toBeTruthy();
  });

  test("shows only the newest 60 runs in the count", () => {
    const runs = Array.from({ length: 62 }, (_, index) => ({
      at: NOW_SECONDS - (62 - index) * 60,
      passed: index >= 2,
    }));
    renderWithProviders(<RunStrip label="Gates" runs={runs} />);
    expect(screen.getByRole("img", { name: "Gates: 60 of 60 runs passed" }).children).toHaveLength(
      60,
    );
    expect(screen.getByText("60 of 60 passed")).toBeTruthy();
  });

  test("reads zero of zero runs for an empty history", () => {
    renderWithProviders(<RunStrip label="Gates" runs={[]} />);
    expect(screen.getByRole("img", { name: "Gates: 0 of 0 runs passed" })).toBeTruthy();
  });
});
