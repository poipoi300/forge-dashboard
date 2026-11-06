import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import { act, fireEvent, screen } from "@testing-library/react";
import { Meter, PathText, RelativeTime, Sparkline } from "@/components/data";
import { absolute, useClock } from "@/lib/time";
import type { Tone } from "@/schema/schema";
import { isPresent } from "./components-support";
import { installHarness, NOW_MS, NOW_SECONDS, renderWithProviders, timeText } from "./harness";

installHarness();
afterEach(() => mock.restore());

describe("RelativeTime", () => {
  test("shows how long ago an instant was, with the exact time as its title", () => {
    renderWithProviders(<RelativeTime at={NOW_SECONDS - 3600} />);
    const time = screen.getByText(timeText("1 h ago"));
    expect(time.tagName).toBe("TIME");
    expect(time.getAttribute("title")).toBe(absolute(NOW_SECONDS - 3600));
    expect(time.getAttribute("datetime")).toBe("2026-01-15T11:00:00.000Z");
  });

  test("shows the relative time for an instant that has not happened yet", () => {
    renderWithProviders(<RelativeTime at={NOW_SECONDS + 300} />);
    expect(screen.getByText(timeText("in 5 min"))).toBeTruthy();
  });

  test("says never when there has been no instant at all", () => {
    renderWithProviders(<RelativeTime at={null} />);
    expect(screen.getByText("never").tagName).toBe("SPAN");
  });

  test("says never for a missing instant too", () => {
    renderWithProviders(<RelativeTime at={undefined} />);
    expect(screen.getByText("never")).toBeTruthy();
  });

  test("moves forward with the shared clock on every tick", () => {
    renderWithProviders(<RelativeTime at={NOW_SECONDS - 3600} />);
    expect(screen.getByText(timeText("1 h ago"))).toBeTruthy();
    act(() => useClock.setState({ now: NOW_MS + 3600 * 1000 }));
    expect(screen.getByText(timeText("2 h ago"))).toBeTruthy();
  });

  test("passes its class through to the time element", () => {
    renderWithProviders(<RelativeTime at={NOW_SECONDS} className="text-xs" />);
    expect(screen.getByText("just now").className).toContain("text-xs");
  });
});

describe("Meter", () => {
  test("reports the ratio as a percentage of its limit", () => {
    renderWithProviders(<Meter label="Disk" value={0.25} />);
    const meter = screen.getByRole("meter", { name: "Disk" });
    expect(meter.getAttribute("aria-valuemin")).toBe("0");
    expect(meter.getAttribute("aria-valuemax")).toBe("100");
    expect(meter.getAttribute("aria-valuenow")).toBe("25");
  });

  test("never reports less than empty or more than full", () => {
    renderWithProviders(
      <>
        <Meter label="Over" value={1.7} />
        <Meter label="Under" value={-0.3} />
      </>,
    );
    expect(screen.getByRole("meter", { name: "Over" }).getAttribute("aria-valuenow")).toBe("100");
    expect(screen.getByRole("meter", { name: "Under" }).getAttribute("aria-valuenow")).toBe("0");
  });

  test.each<[Tone, string, string]>([
    ["good", "bg-clear", "bg-clear-wash"],
    ["warn", "bg-amber", "bg-amber-wash"],
    ["bad", "bg-signal", "bg-signal-wash"],
    ["info", "bg-info", "bg-info-wash"],
    ["neutral", "bg-ink-2", "bg-hover"],
  ])("fills a %s meter with its severity colour over a lighter track", (tone, fill, track) => {
    renderWithProviders(<Meter label="Quota" value={0.5} tone={tone} />);
    const meter = screen.getByRole("meter", { name: "Quota" });
    const fillElement = meter.firstElementChild;
    expect(fillElement?.className.split(" ")).toContain(fill);
    expect(meter.className.split(" ")).toContain(track);
    expect(fillElement?.getAttribute("style")).toContain("width: 50%");
  });

  test("defaults to the informational tone", () => {
    renderWithProviders(<Meter label="Quota" value={0.5} />);
    const fillElement = screen.getByRole("meter", { name: "Quota" }).firstElementChild;
    expect(fillElement?.className.split(" ")).toContain("bg-info");
  });
});

describe("PathText", () => {
  test("shows the path home-relative with the full path as its title", () => {
    renderWithProviders(<PathText path="C:/Users/Administrator/Documents/forge" />);
    const shown = screen.getByText("~/Documents/forge");
    expect(shown.getAttribute("title")).toBe("C:/Users/Administrator/Documents/forge");
  });

  test("copies the full path and confirms it, then reverts the confirmation after a moment", async () => {
    const writeText = spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    const realSetTimeout = globalThis.setTimeout;
    const reverts: (() => void)[] = [];
    spyOn(globalThis, "setTimeout").mockImplementation(((handler: () => void, delay?: number) => {
      if (delay !== 1200) return realSetTimeout(handler, delay);
      reverts.push(handler);
      return 0;
    }) as typeof setTimeout);

    // user-event and waitFor detect a replaced setTimeout as jest fake timers, so this test drives events directly.
    renderWithProviders(<PathText path="C:/code/forge" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy path" }));
    await act(async () => {});
    expect(writeText).toHaveBeenCalledWith("C:/code/forge");
    expect(screen.getByRole("button", { name: "Path copied" })).toBeTruthy();

    const revert = reverts[0];
    if (!revert) throw new Error("the copy confirmation did not schedule its revert");
    act(() => revert());
    expect(screen.getByRole("button", { name: "Copy path" })).toBeTruthy();
  });
});

describe("Sparkline", () => {
  test("draws nothing for fewer than two readings", () => {
    renderWithProviders(
      <>
        <Sparkline label="Empty" values={[]} />
        <Sparkline label="Single" values={[4]} />
      </>,
    );
    expect(isPresent(screen.queryByRole("img"))).toBe(false);
  });

  test("describes its readings and marks the latest one", () => {
    renderWithProviders(<Sparkline label="Pings" values={[1, 3, 2]} />);
    const chart = screen.getByRole("img", { name: "Pings: 3 readings, latest 2" });
    expect(chart.querySelector("title")?.textContent).toBe("Pings: 1, 3, 2");
    expect(chart.getAttribute("width")).toBe("72");
    expect(chart.getAttribute("height")).toBe("18");
  });

  test("plots only the most recent 24 readings", () => {
    const values = Array.from({ length: 30 }, (_, index) => index);
    renderWithProviders(<Sparkline label="Pings" values={values} />);
    const chart = screen.getByRole("img", { name: "Pings: 24 readings, latest 29" });
    expect(chart.querySelector("title")?.textContent).toBe(
      `Pings: ${values.slice(-24).join(", ")}`,
    );
  });

  test("keeps a flat series drawable", () => {
    renderWithProviders(<Sparkline label="Flat" values={[5, 5, 5]} width={40} height={12} />);
    const chart = screen.getByRole("img", { name: "Flat: 3 readings, latest 5" });
    expect(chart.getAttribute("width")).toBe("40");
    expect(chart.getAttribute("height")).toBe("12");
    expect(chart.querySelector("path")?.getAttribute("d")).not.toContain("NaN");
  });
});
