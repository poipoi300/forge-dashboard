import { describe, expect, test } from "bun:test";
import { screen } from "@testing-library/react";
import { Chip, Delay, InfoNote, StatusMark, TONE_ICON, TONE_TEXT } from "@/components/status";
import type { Tone } from "@/schema/schema";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

const GLYPH_CLASS: Record<Tone, string> = {
  good: "lucide-circle-check",
  warn: "lucide-triangle-alert",
  bad: "lucide-octagon-alert",
  info: "lucide-circle-dot",
  neutral: "lucide-circle-dashed",
};

function glyphOf(word: string): Element {
  const glyph = screen.getByText(word).parentElement?.querySelector("svg");
  if (!glyph) throw new Error(`no glyph beside ${word}`);
  return glyph;
}

describe("StatusMark", () => {
  test.each<Tone>(["good", "warn", "bad", "info", "neutral"])(
    "pairs the %s tone's glyph with its word",
    (tone) => {
      renderWithProviders(<StatusMark tone={tone}>Stage word</StatusMark>);
      expect(glyphOf("Stage word").getAttribute("class")).toContain(GLYPH_CLASS[tone]);
      expect(screen.getByText("Stage word").closest("[data-tone]")?.getAttribute("data-tone")).toBe(
        tone,
      );
    },
  );

  test("keeps the glyph out of the accessibility tree so the word carries the meaning", () => {
    renderWithProviders(<StatusMark tone="bad">Build failed</StatusMark>);
    expect(glyphOf("Build failed").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("Build failed")).toBeTruthy();
  });

  test("shows the title as a tooltip on the whole mark", () => {
    renderWithProviders(
      <StatusMark tone="warn" title="Two gates are stale">
        Stale
      </StatusMark>,
    );
    expect(screen.getByText("Stale").closest("[title]")?.getAttribute("title")).toBe(
      "Two gates are stale",
    );
  });

  test("keeps the word in the secondary ink unless the mark is strong", () => {
    renderWithProviders(<StatusMark tone="bad">Quiet</StatusMark>);
    const quiet = screen.getByText("Quiet").closest("[data-tone]");
    expect(quiet?.className).toContain("text-ink-2");
    expect(quiet?.className).not.toContain("font-semibold");
  });

  test("takes the tone's own ink and weight when the mark is strong", () => {
    renderWithProviders(
      <StatusMark tone="bad" strong>
        Failing
      </StatusMark>,
    );
    const failing = screen.getByText("Failing").closest("[data-tone]");
    expect(failing?.className.split(" ")).toContain(TONE_TEXT.bad);
    expect(failing?.className).toContain("font-semibold");
  });

  test("adds the caller's class to the mark", () => {
    renderWithProviders(
      <StatusMark tone="good" className="justify-end">
        Passing
      </StatusMark>,
    );
    expect(screen.getByText("Passing").closest("[data-tone]")?.className).toContain("justify-end");
  });

  test("maps every tone to a distinct glyph", () => {
    const glyphs = Object.values(TONE_ICON);
    expect(new Set(glyphs).size).toBe(glyphs.length);
  });
});

describe("Chip", () => {
  test("labels a categorical fact without implying a state", () => {
    renderWithProviders(<Chip title="The project's kind">service</Chip>);
    const chip = screen.getByText("service");
    expect(chip.getAttribute("title")).toBe("The project's kind");
    expect(chip.className).toContain("text-ink-2");
  });
});

describe("Delay", () => {
  test("marks lateness with a clock glyph and the amount in signal ink", () => {
    renderWithProviders(<Delay title="Three runs behind schedule">+3</Delay>);
    const delay = screen.getByText("+3").closest("[title]");
    expect(delay?.getAttribute("title")).toBe("Three runs behind schedule");
    expect(delay?.className).toContain("text-signal-ink");
    expect(delay?.querySelector("svg")?.getAttribute("class")).toContain("lucide-clock-3");
  });
});

describe("InfoNote", () => {
  test("adds a quiet information glyph beside the note", () => {
    renderWithProviders(<InfoNote>Reads the last 24 hours</InfoNote>);
    const note = screen.getByText("Reads the last 24 hours");
    expect(note.querySelector("svg")?.getAttribute("class")).toContain("lucide-info");
  });
});
