import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { LogView, stripAnsi } from "@/components/LogView";
import { isPresent } from "./components-support";
import { installHarness, renderWithProviders } from "./harness";

installHarness();
afterEach(() => mock.restore());

const RED = "\u001b[31mERROR\u001b[0m";

function GrowingLog({ initial }: { initial: string[] }) {
  const [lines, setLines] = useState(initial);
  return (
    <>
      <button
        type="button"
        onClick={() => setLines((current) => [...current, `appended ${current.length + 1}`])}
      >
        Append
      </button>
      <LogView lines={lines} label="job" />
    </>
  );
}

/** happy-dom has no layout, so the scroll geometry is fixed by the test. */
function fixScrollGeometry(element: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(element, "clientHeight", { configurable: true, value: clientHeight });
  Object.defineProperty(element, "scrollTop", { configurable: true, writable: true, value: 0 });
}

function followButton(): HTMLElement | null {
  return screen.queryByRole("button", { name: "Follow the tail" });
}

describe("stripAnsi", () => {
  test("removes colour and style sequences and keeps the text", () => {
    expect(stripAnsi("\u001b[1;31mred\u001b[0m plain")).toBe("red plain");
  });

  test("removes private-mode sequences such as cursor visibility", () => {
    expect(stripAnsi("\u001b[?25lready\u001b[?25h")).toBe("ready");
  });
});

describe("LogView", () => {
  test("labels the log, counts its lines and lists every line", () => {
    renderWithProviders(<LogView label="build" lines={["compiling", "linking", "done"]} />);
    expect(screen.getByText("· 3 lines")).toBeTruthy();
    const log = screen.getByRole("log", { name: "build" });
    expect(log.textContent).toBe("compilinglinkingdone");
  });

  test("shows the default empty text until a line arrives", () => {
    renderWithProviders(<LogView label="job" lines={[]} />);
    expect(screen.getByText("No output yet.")).toBeTruthy();
    expect(screen.getByText("· 0 lines")).toBeTruthy();
  });

  test("shows the caller's empty text when it has one", () => {
    renderWithProviders(
      <LogView label="job" lines={[]} emptyText="The service has not written anything yet." />,
    );
    expect(screen.getByText("The service has not written anything yet.")).toBeTruthy();
    expect(isPresent(screen.queryByText("No output yet."))).toBe(false);
  });

  test("filters lines case-insensitively, matching the text without colour codes", async () => {
    const { user } = renderWithProviders(
      <LogView label="job" lines={["[runner] starting", "build ok", `${RED} boom`]} />,
    );
    await user.type(screen.getByRole("textbox", { name: "Filter job" }), "error");
    expect(screen.getByRole("log", { name: "job" }).textContent).toBe("ERROR boom");
    expect(screen.getByText("· 3 lines")).toBeTruthy();
  });

  test("says when no line matches the filter and restores every line once it is cleared", async () => {
    const { user } = renderWithProviders(<LogView label="job" lines={["alpha", "beta"]} />);
    const filter = screen.getByRole("textbox", { name: "Filter job" });
    await user.type(filter, "gamma");
    expect(screen.getByText("No line matches the filter.")).toBeTruthy();
    await user.clear(filter);
    expect(screen.getByRole("log", { name: "job" }).textContent).toBe("alphabeta");
  });

  test("copies every line as plain text without colour codes", async () => {
    const writeText = spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    const { user } = renderWithProviders(<LogView label="job" lines={[RED, "plain"]} />);
    await user.click(screen.getByRole("button", { name: "Copy log as plain text" }));
    expect(writeText).toHaveBeenCalledWith("ERROR\nplain");
  });

  test("announces new lines politely only while the log is live", () => {
    renderWithProviders(<LogView label="job" lines={["x"]} live />);
    expect(screen.getByText("· live")).toBeTruthy();
    expect(screen.getByRole("log", { name: "job" }).getAttribute("aria-live")).toBe("polite");
  });

  test("does not announce or mark a finished log as live", () => {
    renderWithProviders(<LogView label="job" lines={["x"]} />);
    expect(isPresent(screen.queryByText("· live"))).toBe(false);
    expect(screen.getByRole("log", { name: "job" }).getAttribute("aria-live")).toBe("off");
  });

  test("renders ANSI colours and decorations as styled text", () => {
    renderWithProviders(
      <LogView
        label="job"
        lines={[
          "\u001b[1mbold\u001b[0m \u001b[3mitalic\u001b[0m \u001b[4munder\u001b[0m \u001b[9mstruck\u001b[0m \u001b[2mdim\u001b[0m \u001b[31mred\u001b[0m",
        ]}
      />,
    );
    expect(screen.getByText("bold").style.fontWeight).toBe("600");
    expect(screen.getByText("italic").style.fontStyle).toBe("italic");
    expect(screen.getByText("under").style.textDecoration).toBe("underline");
    expect(screen.getByText("struck").style.textDecoration).toBe("line-through");
    expect(screen.getByText("dim").style.opacity).toBe("0.7");
    expect(screen.getByText("red").style.color).toMatch(/^rgb\(/);
  });

  test("dims the runner's own lines and leaves the program's output at full ink", () => {
    renderWithProviders(<LogView label="job" lines={["[runner] restarting", "program output"]} />);
    const runnerRow = screen.getByText("[runner] restarting").closest("div");
    const programRow = screen.getByText("program output").closest("div");
    expect(runnerRow?.className).toContain("text-board-ink-2");
    expect(programRow?.className).not.toContain("text-board-ink-2");
  });

  test("shows only the newest 4000 lines and says how many earlier ones are hidden", () => {
    const lines = Array.from({ length: 4005 }, (_, index) => `line ${index}`);
    renderWithProviders(<LogView label="job" lines={lines} />);
    expect(screen.getByText("5 earlier lines not shown; copy the log for all of it.")).toBeTruthy();
    expect(screen.getByText("line 5")).toBeTruthy();
    expect(screen.getByText("line 4004")).toBeTruthy();
    expect(isPresent(screen.queryByText("line 4"))).toBe(false);
    expect(screen.getByText("· 4005 lines")).toBeTruthy();
  });

  test("follows the tail, stops when the reader scrolls up, and resumes from the follow button", async () => {
    const { user } = renderWithProviders(<GrowingLog initial={["one", "two"]} />);
    const log = screen.getByRole("log", { name: "job" });
    fixScrollGeometry(log, 1000, 200);

    log.scrollTop = 800;
    fireEvent.scroll(log);
    expect(isPresent(followButton())).toBe(false);

    log.scrollTop = 0;
    fireEvent.scroll(log);
    expect(isPresent(followButton())).toBe(true);
    await user.click(screen.getByRole("button", { name: "Append" }));
    expect(log.scrollTop).toBe(0);

    await user.click(screen.getByRole("button", { name: "Follow the tail" }));
    expect(log.scrollTop).toBe(1000);
    expect(isPresent(followButton())).toBe(false);
  });

  test("keeps following while the reader is within a line of the bottom", async () => {
    const { user } = renderWithProviders(<GrowingLog initial={["one"]} />);
    const log = screen.getByRole("log", { name: "job" });
    fixScrollGeometry(log, 1000, 200);
    log.scrollTop = 790;
    fireEvent.scroll(log);
    await user.click(screen.getByRole("button", { name: "Append" }));
    expect(log.scrollTop).toBe(1000);
    expect(isPresent(followButton())).toBe(false);
  });
});
