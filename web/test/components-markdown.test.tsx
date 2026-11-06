import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { Markdown } from "@/components/Markdown";
import { isPresent } from "./components-support";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

describe("Markdown", () => {
  test("renders paragraphs as paragraphs", () => {
    renderWithProviders(<Markdown>{"# Launch guide\n\nRun the server first."}</Markdown>);
    expect(screen.getByText("Run the server first.").tagName).toBe("P");
  });

  test("demotes headings two levels so a document title never becomes a second page title", () => {
    renderWithProviders(<Markdown>{"# One\n\n## Two\n\n### Three\n\n#### Four"}</Markdown>);
    expect(screen.queryAllByRole("heading", { level: 1 })).toHaveLength(0);
    expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
    expect(screen.getByRole("heading", { level: 3, name: "One" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 4, name: "Two" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 5, name: "Three" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 6, name: "Four" })).toBeTruthy();
  });

  test("opens links in a new tab without leaking the opener", () => {
    renderWithProviders(<Markdown>{"See [the docs](https://example.com/docs)."}</Markdown>);
    const link = screen.getByRole("link", { name: "the docs" });
    expect(link.getAttribute("href")).toBe("https://example.com/docs");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  test("turns a bare URL into a link that opens in a new tab", () => {
    renderWithProviders(<Markdown>{"Visit https://example.org today."}</Markdown>);
    const link = screen.getByRole("link", { name: "https://example.org" });
    expect(link.getAttribute("target")).toBe("_blank");
  });

  test("drops a javascript: link target so the link cannot run script", () => {
    renderWithProviders(<Markdown>{"[click me](javascript:alert(1))"}</Markdown>);
    const link = screen.getByText("click me");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).not.toContain("javascript");
  });

  test("shows raw HTML from the source as text instead of rendering it", () => {
    renderWithProviders(
      <Markdown>{"Before <b>bold</b> and <script>run()</script> after"}</Markdown>,
    );
    expect(screen.getByText(/<b>bold<\/b>/)).toBeTruthy();
    expect(isPresent(document.querySelector("b"))).toBe(false);
    expect(isPresent(document.querySelector("script"))).toBe(false);
  });

  test("renders GFM tables with their header and body cells", () => {
    renderWithProviders(
      <Markdown>{"| Gate | Result |\n| --- | --- |\n| ruff | pass |\n| mypy | fail |"}</Markdown>,
    );
    const table = screen.getByRole("table");
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((header) => header.textContent)).toEqual(["Gate", "Result"]);
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[2]?.textContent).toBe("mypyfail");
  });

  test("renders bulleted lists and GFM strikethrough", () => {
    renderWithProviders(<Markdown>{"- one\n- ~~two~~\n- three"}</Markdown>);
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["one", "two", "three"]);
    expect(screen.getByText("two").tagName).toBe("DEL");
  });
});
