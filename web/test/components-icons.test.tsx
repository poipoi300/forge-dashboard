import { describe, expect, test } from "bun:test";
import { GithubMark } from "@/components/icons";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

describe("GithubMark", () => {
  test("draws the mark decoratively in the current text colour", () => {
    renderWithProviders(<GithubMark />);
    const mark = document.querySelector("svg");
    expect(mark?.getAttribute("aria-hidden")).toBe("true");
    expect(mark?.getAttribute("fill")).toBe("currentColor");
    expect(mark?.getAttribute("viewBox")).toBe("0 0 16 16");
  });

  test("passes sizing and classes from the caller through to the svg", () => {
    renderWithProviders(<GithubMark className="size-4" width={20} />);
    const mark = document.querySelector("svg");
    expect(mark?.getAttribute("class")).toBe("size-4");
    expect(mark?.getAttribute("width")).toBe("20");
  });
});
