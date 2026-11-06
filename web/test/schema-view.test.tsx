import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { SchemaTable, SchemaView } from "@/schema/SchemaView";
import type { JsonSchema } from "@/schema/schema";
import { installHarness, NOW_SECONDS, renderWithProviders, timeText } from "./harness";
import { GALLERY_CONFIG, GALLERY_STATE, objectSchema } from "./schema-support";

installHarness();

function show(
  schema: JsonSchema,
  value: unknown,
  options: { dense?: boolean; root?: JsonSchema } = {},
) {
  return renderWithProviders(
    <SchemaView
      schema={schema}
      root={options.root ?? schema}
      value={value}
      dense={options.dense}
    />,
    { dialogHost: false },
  );
}

function showTable(
  schema: JsonSchema,
  rows: unknown[],
  props: Partial<ComponentProps<typeof SchemaTable>> = {},
) {
  return renderWithProviders(<SchemaTable schema={schema} root={schema} rows={rows} {...props} />, {
    dialogHost: false,
  });
}

/** The value cell next to a definition-list label. */
function definition(label: string): HTMLElement {
  const term = screen.getByText(label, { selector: "dt" });
  const description = term.nextElementSibling;
  if (!(description instanceof HTMLElement)) throw new Error(`no definition for ${label}`);
  return description;
}

const formatted = (format: string, type = "string"): JsonSchema => ({
  type,
  "x-ui": { format },
});

describe("SchemaView formats", () => {
  test("shows a marker someone set as a neutral chip with its display label", () => {
    const status: JsonSchema = {
      type: "string",
      enum: ["active", "blocked"],
      "x-ui": {
        format: "chip",
        enum_labels: { active: "in progress", blocked: "stuck" },
        tones: { active: "good", blocked: "bad" },
      },
    };
    show(status, "blocked");
    expect(screen.getByText("stuck")).toBeTruthy();
    expect(screen.queryByText("blocked")).toBeNull();
  });

  test("shows a chip for a value whose tone asks for no emphasis", () => {
    show({ type: "string", "x-ui": { format: "chip", tones: { paused: "warn" } } }, "paused");
    expect(screen.getByText("paused")).toBeTruthy();
    show({ type: "string", "x-ui": { format: "chip" } }, "idea");
    expect(screen.getByText("idea")).toBeTruthy();
  });

  test("shows a timestamp as a distance from now, with the absolute time as its title", () => {
    show(formatted("relative-time", "number"), NOW_SECONDS - 12 * 60);
    const time = screen.getByText(timeText("12 min ago"));
    expect(time.tagName).toBe("TIME");
    expect(time.getAttribute("datetime")).toBe(new Date((NOW_SECONDS - 720) * 1000).toISOString());
    expect(time.getAttribute("title")).toBeTruthy();
  });

  test("says never for a missing timestamp, wherever the format is declared", () => {
    show(formatted("relative-time", "number"), null);
    expect(screen.getByText("never")).toBeTruthy();
  });

  test("reads the format from the inner schema of a nullable wrapper", () => {
    show({ anyOf: [formatted("relative-time", "number"), { type: "null" }] }, undefined);
    expect(screen.getByText("never")).toBeTruthy();
  });

  test("shows a dash for a timestamp that is not a number", () => {
    show(formatted("relative-time", "number"), "yesterday");
    expect(screen.getByText("—")).toBeTruthy();
  });

  test("shows seconds as a compact duration and passes a text duration through", () => {
    show(formatted("duration", "number"), 3725);
    expect(screen.getByText("1 h 02 min")).toBeTruthy();
    show(formatted("duration"), "about a day");
    expect(screen.getByText("about a day")).toBeTruthy();
  });

  test("shows a size in binary units and passes a text size through", () => {
    show(formatted("bytes", "integer"), 5368709120);
    expect(screen.getByText("5.0 GB")).toBeTruthy();
    show(formatted("bytes"), "lots");
    expect(screen.getByText("lots")).toBeTruthy();
  });

  test("shows a percentage with a decimal only below ten", () => {
    show(formatted("percent", "number"), 87.5);
    expect(screen.getByText("88%")).toBeTruthy();
    show(formatted("percent", "number"), 4.5);
    expect(screen.getByText("4.5%")).toBeTruthy();
  });

  test("shows a dash for a percentage that is not a number", () => {
    show(formatted("percent"), "most");
    expect(screen.getByText("—")).toBeTruthy();
  });

  test("shows a progress ratio as a labelled meter", () => {
    show(formatted("progress", "number"), 0.6);
    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-label")).toBe("Progress 60%");
    expect(meter.getAttribute("aria-valuenow")).toBe("60");
  });

  test("reads a progress number above one as a percentage", () => {
    show(formatted("progress", "number"), 45);
    expect(screen.getByRole("meter").getAttribute("aria-label")).toBe("Progress 45%");
  });

  test("treats a progress of exactly one as complete", () => {
    show(formatted("progress", "number"), 1);
    expect(screen.getByRole("meter").getAttribute("aria-label")).toBe("Progress 100%");
  });

  test("shows a dash for a progress value that is neither a number nor an object", () => {
    show(formatted("progress"), "halfway");
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.queryByRole("meter")).toBeNull();
  });

  test("shows a progress object with its percentage, caption and detail", () => {
    show(formatted("progress", "object"), {
      done: 3,
      total: 4,
      caption: "3 of 4 gates",
      detail: "lint is running",
    });
    expect(screen.getByText("75%")).toBeTruthy();
    expect(screen.getByText("3 of 4 gates")).toBeTruthy();
    expect(screen.getByText("lint is running")).toBeTruthy();
    expect(screen.getByRole("meter").getAttribute("aria-label")).toBe("Progress 75%");
  });

  test("captions a progress object with its counts when it names no caption", () => {
    show(formatted("progress", "object"), { done: 2, total: 8 });
    expect(screen.getByText("2/8")).toBeTruthy();
    expect(screen.getByText("25%")).toBeTruthy();
  });

  test("shows a progress object with no total as zero percent without dividing by zero", () => {
    show(formatted("progress", "object"), {});
    expect(screen.getByText("0%")).toBeTruthy();
    expect(screen.getByText("0/0")).toBeTruthy();
    expect(screen.getByRole("meter").getAttribute("aria-valuenow")).toBe("0");
  });

  test("shows code in monospace", () => {
    show(formatted("code"), "1a2b3c4");
    expect(screen.getByText("1a2b3c4").tagName).toBe("CODE");
  });

  test("shows a link without its scheme or trailing slash, opening in a new tab", () => {
    show(formatted("url"), "https://example.com/");
    const link = screen.getByRole("link", { name: "example.com" });
    expect(link.getAttribute("href")).toBe("https://example.com/");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  test("keeps the path of a link and shortens an http one too", () => {
    show(formatted("url"), "http://host.test/docs/page");
    expect(screen.getByRole("link", { name: "host.test/docs/page" })).toBeTruthy();
  });

  test("shows a path relative to the home directory with a way to copy it", () => {
    show(formatted("path"), "C:/Users/me/code/forge");
    expect(screen.getByText("~/code/forge").getAttribute("title")).toBe("C:/Users/me/code/forge");
    expect(screen.getByRole("button", { name: "Copy path" })).toBeTruthy();
  });

  test("renders markdown as formatted text on a full surface", () => {
    show(formatted("markdown"), "some **bold** words");
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  test("shows markdown as its plain source on a dense surface", () => {
    show(formatted("markdown"), "some **bold** words", { dense: true });
    expect(screen.getByText("some **bold** words")).toBeTruthy();
    expect(screen.queryByText("bold", { selector: "strong" })).toBeNull();
  });
});

describe("SchemaView badges", () => {
  const status: JsonSchema = {
    type: "string",
    enum: ["good", "warn", "failing"],
    "x-ui": {
      format: "badge",
      tones: { good: "good", warn: "warn", failing: "bad" },
      enum_labels: { failing: "Failing", good: "All good" },
    },
  };

  test("shows a value as a badge with its label and tone", () => {
    show(status, "failing");
    const badge = screen.getByText("Failing").closest("[data-tone]");
    expect(badge?.getAttribute("data-tone")).toBe("bad");
  });

  test("labels a badge by the enum label and falls back to the raw value", () => {
    show(status, "good");
    expect(screen.getByText("All good").closest("[data-tone]")?.getAttribute("data-tone")).toBe(
      "good",
    );
    show(status, "warn");
    expect(screen.getByText("warn").closest("[data-tone]")?.getAttribute("data-tone")).toBe("warn");
  });

  test("gives a value with no declared tone the neutral tone", () => {
    show(status, "unheard-of");
    expect(screen.getByText("unheard-of").closest("[data-tone]")?.getAttribute("data-tone")).toBe(
      "neutral",
    );
  });

  const health: JsonSchema = {
    type: "object",
    "x-ui": { format: "badge" },
    required: ["level"],
    properties: {
      level: status,
      reasons: { type: "array", items: { type: "string" }, title: "Reasons" },
      internal: { type: "string", "x-ui": { hidden: true } },
    },
  };

  test("shows an object with a level as the level's badge", () => {
    show(health, { level: "failing", reasons: ["tests red"], internal: "x" });
    expect(screen.getByRole("button", { name: /Failing/ })).toBeTruthy();
    expect(screen.queryByText("tests red")).toBeNull();
  });

  test("reveals the rest of the object, minus hidden fields, when the badge is hovered", async () => {
    const { user } = show(health, {
      level: "warn",
      reasons: ["tests red", "lint red"],
      internal: "secret-detail",
    });
    await user.hover(screen.getByRole("button", { name: /warn/ }));
    expect((await screen.findAllByText("tests red")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("lint red").length).toBeGreaterThan(0);
    expect(screen.queryByText("secret-detail")).toBeNull();
  });

  test("shows an object without a level inline instead", () => {
    show(
      {
        type: "object",
        "x-ui": { format: "badge" },
        properties: { owner: { type: "string" } },
      },
      { owner: "mia" },
    );
    expect(screen.getByText("Owner")).toBeTruthy();
    expect(screen.getByText("mia")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("SchemaView plain values", () => {
  test("shows a dash for null and undefined", () => {
    show({ type: "string" }, null);
    expect(screen.getAllByText("—")).toHaveLength(1);
    show({ type: "string" }, undefined);
    expect(screen.getAllByText("—")).toHaveLength(2);
  });

  test("shows booleans as yes and no", () => {
    show({ type: "boolean" }, true);
    expect(screen.getByText("yes")).toBeTruthy();
    show({ type: "boolean" }, false);
    expect(screen.getByText("no")).toBeTruthy();
  });

  test("shows an enum value by its label", () => {
    show({ type: "string", enum: ["a"], "x-ui": { enum_labels: { a: "Alpha" } } }, "a");
    expect(screen.getByText("Alpha")).toBeTruthy();
  });

  test("shows a number with its unit", () => {
    show({ type: "integer", "x-ui": { unit: "items" } }, 12);
    const figure = screen.getByText("12", { exact: false });
    expect(figure.textContent).toBe("12items");
    expect(within(figure).getByText("items")).toBeTruthy();
  });

  test("shows a number without a unit as the bare figure", () => {
    show({ type: "number" }, 3);
    expect(screen.getByText("3").textContent).toBe("3");
  });

  test("shows a numeric field holding text as that text", () => {
    show({ type: "integer" }, "n/a");
    expect(screen.getByText("n/a")).toBeTruthy();
  });

  test("shows text as it is", () => {
    show({ type: "string" }, "plain text");
    expect(screen.getByText("plain text")).toBeTruthy();
  });

  test("shows a value of an unknown type as its string form", () => {
    show({}, 42);
    expect(screen.getByText("42")).toBeTruthy();
  });

  test("shows a list of scalars as chips", () => {
    show({ type: "array", items: { type: "string" } }, ["alpha", "beta"]);
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();
  });

  test("shows list items by their enum labels", () => {
    show(
      {
        type: "array",
        items: { type: "string", enum: ["a"], "x-ui": { enum_labels: { a: "Alpha" } } },
      },
      ["a"],
    );
    expect(screen.getByText("Alpha")).toBeTruthy();
  });

  test("shows an empty list as a dash", () => {
    show({ type: "array", items: { type: "string" } }, []);
    expect(screen.getByText("—")).toBeTruthy();
  });

  test("shows the items of a list that declares no item schema", () => {
    show({ type: "array" }, ["x", 2]);
    expect(screen.getByText("x")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
  });

  test("shows an array-typed field holding something else as that value", () => {
    show({ type: "array", items: { type: "string" } }, "not a list");
    expect(screen.getByText("not a list")).toBeTruthy();
  });

  test("shows a map as key and JSON pairs and an empty map as a dash", () => {
    const map: JsonSchema = { type: "object", additionalProperties: { type: "integer" } };
    show(map, { retries: 3, name: "x" });
    expect(screen.getByText("retries")).toBeTruthy();
    expect(screen.getByText("3", { selector: "code" })).toBeTruthy();
    expect(screen.getByText('"x"', { selector: "code" })).toBeTruthy();
    show(map, {});
    expect(screen.getByText("—")).toBeTruthy();
  });

  test("shows an object that is really a list as its string form", () => {
    show({ type: "object", properties: { a: { type: "string" } } }, ["x"]);
    expect(screen.getByText("x")).toBeTruthy();
  });
});

describe("SchemaView objects", () => {
  const person = objectSchema({
    name: { type: "string" },
    age: { type: "integer", "x-ui": { unit: "yr" } },
    hidden_note: { type: "string", "x-ui": { hidden: true } },
    tags: { type: "array", items: { type: "string" } },
  });

  test("lists an object's visible fields as labelled definitions", () => {
    show(person, { name: "Mia", age: 31, hidden_note: "no", tags: ["a"] });
    expect(definition("Name").textContent).toBe("Mia");
    expect(definition("Age").textContent).toBe("31yr");
    expect(definition("Tags").textContent).toBe("a");
    expect(screen.queryByText("Hidden note")).toBeNull();
    expect(screen.queryByText("no")).toBeNull();
  });

  test("shows a dash for a field the value lacks", () => {
    show(person, { name: "Mia" });
    expect(definition("Age").textContent).toBe("—");
  });

  test("renders every display format of the gallery state with the right text", () => {
    show(GALLERY_STATE, {
      updated_at: NOW_SECONDS - 720,
      elapsed_s: 3725,
      size: 5368709120,
      ratio: 87.5,
      progress: 0.6,
      status: "failing",
      commit: "1a2b3c4",
      link: "https://example.com/",
      location: "C:/Users/me/code",
      pings: 7,
    });
    expect(definition("Updated at").textContent).toBe("12 min ago");
    expect(definition("Elapsed s").textContent).toBe("1 h 02 min");
    expect(definition("Size").textContent).toBe("5.0 GB");
    expect(definition("Ratio").textContent).toBe("88%");
    expect(within(definition("Progress")).getByRole("meter").getAttribute("aria-label")).toBe(
      "Progress 60%",
    );
    expect(
      within(definition("Status"))
        .getByText("failing")
        .closest("[data-tone]")
        ?.getAttribute("data-tone"),
    ).toBe("bad");
    expect(within(definition("Commit")).getByText("1a2b3c4").tagName).toBe("CODE");
    expect(within(definition("Link")).getByRole("link").getAttribute("href")).toBe(
      "https://example.com/",
    );
    expect(definition("Location").textContent).toContain("~/code");
    expect(definition("Pings").textContent).toBe("7");
  });

  test("renders a never-updated gallery state with placeholders", () => {
    show(GALLERY_STATE, {});
    expect(definition("Updated at").textContent).toBe("never");
    expect(definition("Status").textContent).toBe("—");
  });

  test("shows a list of objects as a table on a full surface", () => {
    show(
      {
        type: "array",
        items: objectSchema({ name: { type: "string" }, port: { type: "integer" } }),
      },
      [
        { name: "web", port: 80 },
        { name: "api", port: 8000 },
      ],
    );
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["Name", "Port"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("api")).toBeTruthy();
  });

  test("limits a list table to the columns the list declares", () => {
    show(
      {
        type: "array",
        "x-ui": { columns: ["port"] },
        items: objectSchema({ name: { type: "string" }, port: { type: "integer" } }),
      },
      [{ name: "web", port: 80 }],
    );
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual([
      "Port",
    ]);
  });

  test("shows a list of objects as stacked inline objects on a dense surface", () => {
    show(
      {
        type: "array",
        items: objectSchema({ name: { type: "string" }, port: { type: "integer" } }),
      },
      [
        { name: "web", port: 80 },
        { name: "api", port: 8000 },
      ],
      { dense: true },
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("web")).toBeTruthy();
    expect(screen.getByText("api")).toBeTruthy();
  });

  test("resolves a list of referenced objects", () => {
    const root: JsonSchema = {
      $defs: { Row: objectSchema({ label: { type: "string" } }) },
    };
    show({ type: "array", items: { $ref: "#/$defs/Row" } }, [{ label: "one" }], { root });
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getByText("one")).toBeTruthy();
  });

  test("shows the fields of the variant a discriminated union value belongs to", () => {
    const target = GALLERY_CONFIG.properties?.target ?? {};
    show(target, { kind: "email", address: "mia@example.com" }, { root: GALLERY_CONFIG });
    expect(screen.getByText("mia@example.com")).toBeTruthy();
  });
});

describe("SchemaView dense objects", () => {
  const counts = objectSchema({
    ahead: { type: "integer", "x-ui": { short_label: "ahead", hide_zero: true } },
    behind: { type: "integer", "x-ui": { short_label: "behind", hide_zero: true } },
    total: { type: "integer", "x-ui": { short_label: "total" } },
    dirty: {
      type: "boolean",
      title: "Working tree dirty",
      "x-ui": { short_label: "dirty", help: "Uncommitted changes" },
    },
    clean: { type: "boolean", "x-ui": { short_label: "clean" } },
    branch: { type: "string", "x-ui": { short_label: "" } },
    stashes: { type: "array", items: { type: "string" } },
    note: { type: "string" },
    internal: { type: "string", "x-ui": { hidden: true } },
  });

  test("shows each set field with its short label and leaves out empty ones", () => {
    show(
      counts,
      {
        ahead: 2,
        behind: 0,
        total: 0,
        dirty: true,
        clean: false,
        branch: "main",
        stashes: [],
        note: null,
        internal: "x",
      },
      { dense: true },
    );
    expect(screen.getByText("ahead")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.queryByText("behind")).toBeNull();
    expect(screen.getByText("total")).toBeTruthy();
    expect(screen.getByText("0")).toBeTruthy();
    expect(screen.queryByText("clean")).toBeNull();
    expect(screen.queryByText("Note")).toBeNull();
    expect(screen.queryByText("Stashes")).toBeNull();
    expect(screen.queryByText("x")).toBeNull();
  });

  test("shows a true flag as its label alone with its help as a tooltip", () => {
    show(counts, { dirty: true, ahead: 1 }, { dense: true });
    const flag = screen.getByTitle("Uncommitted changes");
    expect(flag.textContent).toBe("dirty");
  });

  test("shows a value whose short label is empty without a label", () => {
    show(counts, { branch: "main" }, { dense: true });
    expect(screen.getByText("main").parentElement?.children).toHaveLength(1);
  });

  test("shows a dash when no field is set", () => {
    show(counts, { behind: 0, clean: false }, { dense: true });
    expect(screen.getByText("—")).toBeTruthy();
  });

  test("picks and orders fields by the object's columns hint", () => {
    show(
      { ...counts, "x-ui": { columns: ["total", "missing", "ahead"] } },
      { ahead: 1, total: 5, note: "hidden by columns" },
      { dense: true },
    );
    const text = screen.getByText("total").parentElement?.parentElement?.textContent;
    expect(text).toBe("total5ahead1");
    expect(screen.queryByText("hidden by columns")).toBeNull();
  });
});

describe("SchemaTable", () => {
  const rowSchema = objectSchema({
    name: { type: "string", title: "Service name" },
    size: { type: "integer", "x-ui": { format: "bytes", short_label: "Size" } },
    secret: { type: "string", "x-ui": { hidden: true } },
  });
  const rows = [
    { name: "alpha", size: 2048, secret: "s1" },
    { name: "beta", size: null, secret: "s2" },
  ];

  const headers = () => screen.getAllByRole("columnheader").map((header) => header.textContent);

  test("shows one column per visible field, labelled and formatted", () => {
    showTable(rowSchema, rows);
    expect(headers()).toEqual(["Service name", "Size"]);
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
    expect(screen.queryByText("s1")).toBeNull();
    expect(within(screen.getAllByRole("row")[2] as HTMLElement).getByText("—")).toBeTruthy();
  });

  test("shows the columns asked for, in that order, ignoring unknown names", () => {
    showTable(rowSchema, rows, { columns: ["size", "ghost", "name"] });
    expect(headers()).toEqual(["Size", "Service name"]);
  });

  test("uses the schema's own columns hint when no columns are passed", () => {
    showTable({ ...rowSchema, "x-ui": { columns: ["secret"] } }, rows);
    expect(headers()).toEqual(["Secret"]);
  });

  test("says there is nothing to show for no rows, or what the caller says", () => {
    showTable(rowSchema, []);
    expect(screen.getByText("Nothing here yet.")).toBeTruthy();
    showTable(rowSchema, [], { empty: "No services running." });
    expect(screen.getByText("No services running.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  test("reports the clicked row and its position", async () => {
    const clicks: [Record<string, unknown>, number][] = [];
    const { user } = showTable(rowSchema, rows, {
      onRow: (row, index) => clicks.push([row, index]),
    });
    await user.click(screen.getByText("beta"));
    expect(clicks).toEqual([[rows[1] as Record<string, unknown>, 1]]);
  });

  test("shows a row that is not an object as empty cells", async () => {
    const clicks: [Record<string, unknown>, number][] = [];
    const { user } = showTable(rowSchema, ["junk"], {
      onRow: (row, index) => clicks.push([row, index]),
    });
    expect(screen.getAllByText("—")).toHaveLength(2);
    await user.click(screen.getAllByText("—")[0] as HTMLElement);
    expect(clicks).toEqual([[{}, 0]]);
  });
});
