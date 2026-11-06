import { describe, expect, test } from "bun:test";
import { describeCron } from "@/schema/cron";
import {
  bindContext,
  defaultValue,
  deref,
  enumLabel,
  fields,
  hints,
  humanize,
  inputFields,
  isVisible,
  type JsonSchema,
  kindOf,
  labelOf,
  resolve,
  toneFor,
  variants,
} from "@/schema/schema";
import { installHarness } from "./harness";
import { GALLERY_CONFIG, GALLERY_STATE, objectSchema, onlyField } from "./schema-support";

installHarness();

const NO_ROOT: JsonSchema = {};

describe("deref", () => {
  test("returns a schema without a reference untouched", () => {
    const schema: JsonSchema = { type: "string" };
    expect(deref(schema, NO_ROOT)).toBe(schema);
  });

  test("follows a reference and lets the referring site's keywords win", () => {
    const root: JsonSchema = {
      $defs: { Name: { type: "string", title: "Name", description: "from target" } },
    };
    expect(deref({ $ref: "#/$defs/Name", description: "from site" }, root)).toEqual({
      type: "string",
      title: "Name",
      description: "from site",
    });
  });

  test("merges x-ui hints from the target and the referring site, site first", () => {
    const root: JsonSchema = {
      $defs: { Shared: { type: "string", "x-ui": { widget: "text", help: "target help" } } },
    };
    expect(
      deref({ $ref: "#/$defs/Shared", "x-ui": { help: "site help", group: "G" } }, root)["x-ui"],
    ).toEqual({ widget: "text", help: "site help", group: "G" });
  });

  test("keeps the hints of a target when the site adds none, and vice versa", () => {
    const root: JsonSchema = {
      $defs: { Hinted: { type: "string", "x-ui": { unit: "s" } }, Plain: { type: "string" } },
    };
    expect(deref({ $ref: "#/$defs/Hinted" }, root)["x-ui"]).toEqual({ unit: "s" });
    expect(deref({ $ref: "#/$defs/Plain", "x-ui": { unit: "ms" } }, root)["x-ui"]).toEqual({
      unit: "ms",
    });
    expect(deref({ $ref: "#/$defs/Plain" }, root)["x-ui"]).toBeUndefined();
  });

  test("follows a chain of references to the final definition", () => {
    const root: JsonSchema = {
      $defs: { Outer: { $ref: "#/$defs/Inner" }, Inner: { type: "integer" } },
    };
    expect(deref({ $ref: "#/$defs/Outer" }, root)).toEqual({ type: "integer" });
  });

  test("reads definitions-style and components-style references from $defs", () => {
    const root: JsonSchema = { $defs: { Thing: { type: "boolean" } } };
    expect(deref({ $ref: "#/definitions/Thing" }, root)).toEqual({ type: "boolean" });
    expect(deref({ $ref: "#/components/schemas/Thing" }, root)).toEqual({ type: "boolean" });
  });

  test("throws on a circular reference instead of looping", () => {
    const root: JsonSchema = {
      $defs: { Ping: { $ref: "#/$defs/Pong" }, Pong: { $ref: "#/$defs/Ping" } },
    };
    expect(() => deref({ $ref: "#/$defs/Ping" }, root)).toThrow("circular $ref #/$defs/Ping");
  });

  test("throws on a reference that names no definition", () => {
    expect(() => deref({ $ref: "#/$defs/Missing" }, { $defs: {} })).toThrow(
      "unresolved $ref #/$defs/Missing",
    );
    expect(() => deref({ $ref: "#/$defs/Missing" }, NO_ROOT)).toThrow("unresolved $ref");
  });
});

describe("resolve", () => {
  test("leaves a plain schema as it is and not nullable", () => {
    const schema: JsonSchema = { type: "string" };
    expect(resolve(schema, NO_ROOT)).toEqual({ schema, nullable: false });
  });

  test("unwraps anyOf of a type and null into the type, marking it nullable", () => {
    const resolved = resolve(
      { anyOf: [{ type: "string", minLength: 2 }, { type: "null" }], default: null, title: "Note" },
      NO_ROOT,
    );
    expect(resolved.nullable).toBe(true);
    expect(resolved.schema).toEqual({ type: "string", minLength: 2, default: null, title: "Note" });
  });

  test("lets the wrapper's title, description, default and hints override the inner schema", () => {
    const { schema } = resolve(
      {
        oneOf: [
          { type: "integer", title: "Inner", "x-ui": { unit: "s", help: "inner" } },
          { type: "null" },
        ],
        title: "Outer",
        description: "outer description",
        default: 5,
        "x-ui": { help: "outer" },
      },
      NO_ROOT,
    );
    expect(schema).toEqual({
      type: "integer",
      title: "Outer",
      description: "outer description",
      default: 5,
      "x-ui": { unit: "s", help: "outer" },
    });
  });

  test("resolves a referenced option inside a nullable wrapper", () => {
    const root: JsonSchema = { $defs: { Port: { type: "integer", minimum: 1 } } };
    const resolved = resolve({ anyOf: [{ $ref: "#/$defs/Port" }, { type: "null" }] }, root);
    expect(resolved.nullable).toBe(true);
    expect(resolved.schema).toEqual({ type: "integer", minimum: 1 });
  });

  test("unwraps a union of one real option even when nothing is nullable", () => {
    const resolved = resolve({ anyOf: [{ type: "string" }] }, NO_ROOT);
    expect(resolved).toEqual({ schema: { type: "string" }, nullable: false });
  });

  test("keeps several real options as a union and drops only the null", () => {
    const resolved = resolve(
      { oneOf: [{ type: "string" }, { type: "integer" }, { type: "null" }] },
      NO_ROOT,
    );
    expect(resolved.nullable).toBe(true);
    expect(resolved.schema.anyOf).toEqual([{ type: "string" }, { type: "integer" }]);
    expect(resolved.schema.oneOf).toBeUndefined();
  });

  test("keeps a union of several real options as it is when none is null", () => {
    const schema: JsonSchema = { anyOf: [{ type: "string" }, { type: "integer" }] };
    expect(resolve(schema, NO_ROOT)).toEqual({ schema, nullable: false });
  });

  test("does not unwrap a discriminated union", () => {
    const schema: JsonSchema = {
      oneOf: [{ type: "object" }, { type: "null" }],
      discriminator: { propertyName: "kind" },
    };
    expect(resolve(schema, NO_ROOT)).toEqual({ schema, nullable: false });
  });

  test("merges a single allOf entry (pydantic's wrapper for a described reference)", () => {
    const root: JsonSchema = {
      $defs: { Mode: { type: "string", enum: ["a", "b"], title: "Mode" } },
    };
    const { schema } = resolve(
      {
        allOf: [{ $ref: "#/$defs/Mode" }],
        default: "a",
        description: "Pick one",
        "x-ui": { widget: "select" },
      },
      root,
    );
    expect(schema).toMatchObject({
      type: "string",
      enum: ["a", "b"],
      default: "a",
      description: "Pick one",
      "x-ui": { widget: "select" },
    });
  });

  test("does not merge an allOf with several entries", () => {
    const schema: JsonSchema = { allOf: [{ type: "string" }, { minLength: 1 }] };
    expect(resolve(schema, NO_ROOT).schema).toEqual(schema);
  });

  test("reads a type array containing null as nullable with the remaining type", () => {
    const single = resolve({ type: ["string", "null"], default: "x" }, NO_ROOT);
    expect(single).toEqual({ schema: { type: "string", default: "x" }, nullable: true });
    const several = resolve({ type: ["string", "integer", "null"] }, NO_ROOT);
    expect(several).toEqual({ schema: { type: ["string", "integer"] }, nullable: true });
  });

  test("leaves a type array without null alone", () => {
    const schema: JsonSchema = { type: ["string", "integer"] };
    expect(resolve(schema, NO_ROOT)).toEqual({ schema, nullable: false });
  });
});

describe("variants", () => {
  const root: JsonSchema = {
    $defs: {
      Email: {
        title: "EmailTarget",
        properties: { kind: { const: "email" }, address: { type: "string" } },
      },
      Hook: { properties: { kind: { enum: ["web_hook"] }, url: { type: "string" } } },
    },
  };

  test("reads the discriminator named by the schema and labels each variant by its title", () => {
    const union = variants(GALLERY_CONFIG.properties?.target ?? {}, GALLERY_CONFIG);
    expect(union?.property).toBe("kind");
    expect(union?.variants.map(({ tag, label }) => ({ tag, label }))).toEqual([
      { tag: "email", label: "Email target" },
      { tag: "webhook", label: "Webhook target" },
    ]);
  });

  test("falls back to the humanized tag when a variant has no title", () => {
    const union = variants(
      { oneOf: [{ $ref: "#/$defs/Hook" }, { properties: { kind: { const: "plain" } } }] },
      root,
    );
    expect(union?.variants.map((variant) => variant.label)).toEqual(["Web hook", "Plain"]);
  });

  test("infers the discriminator when every option has a constant for the same property", () => {
    const union = variants({ anyOf: [{ $ref: "#/$defs/Email" }, { $ref: "#/$defs/Hook" }] }, root);
    expect(union?.property).toBe("kind");
    expect(union?.variants.map((variant) => variant.tag)).toEqual(["email", "web_hook"]);
  });

  test("reads the tag from the default when the discriminator names a property without a constant", () => {
    const union = variants(
      {
        discriminator: { propertyName: "type" },
        oneOf: [
          { properties: { type: { type: "string", default: "left" } } },
          { properties: { type: { type: "string", default: "right" } } },
        ],
      },
      NO_ROOT,
    );
    expect(union?.variants.map((variant) => variant.tag)).toEqual(["left", "right"]);
  });

  test("is not a discriminated union with fewer than two options", () => {
    expect(variants({ oneOf: [{ properties: { kind: { const: "a" } } }] }, NO_ROOT)).toBeNull();
    expect(variants({ type: "object" }, NO_ROOT)).toBeNull();
  });

  test("is not a discriminated union when no property is constant in every option", () => {
    expect(
      variants(
        {
          anyOf: [
            { properties: { kind: { const: "a" }, extra: { type: "string" } } },
            { properties: { extra: { type: "string" } } },
          ],
        },
        NO_ROOT,
      ),
    ).toBeNull();
    expect(variants({ anyOf: [{ type: "string" }, { type: "integer" }] }, NO_ROOT)).toBeNull();
  });

  test("is not a discriminated union when a tag is not a string", () => {
    expect(
      variants(
        { anyOf: [{ properties: { kind: { const: 1 } } }, { properties: { kind: { const: 2 } } }] },
        NO_ROOT,
      ),
    ).toBeNull();
  });
});

describe("kindOf", () => {
  const cases: [string, JsonSchema, string][] = [
    [
      "union for a discriminated union",
      { oneOf: [{ properties: { k: { const: "a" } } }, { properties: { k: { const: "b" } } }] },
      "union",
    ],
    ["enum for an enumeration", { type: "string", enum: ["a", "b"] }, "enum"],
    ["enum for a constant", { const: "only" }, "enum"],
    ["object for an object with properties", { type: "object", properties: { a: {} } }, "object"],
    ["object for properties without a declared type", { properties: { a: {} } }, "object"],
    ["map for an object without properties", { type: "object" }, "map"],
    [
      "map for an object with typed additional properties",
      { type: "object", additionalProperties: { type: "string" } },
      "map",
    ],
    [
      "object for a closed object without properties",
      { type: "object", additionalProperties: false },
      "object",
    ],
    ["array", { type: "array", items: { type: "string" } }, "array"],
    ["string", { type: "string" }, "string"],
    ["integer", { type: "integer" }, "integer"],
    ["number", { type: "number" }, "number"],
    ["boolean", { type: "boolean" }, "boolean"],
    ["the first type of a type array", { type: ["integer", "string"] }, "integer"],
    ["any for a schema that says nothing", {}, "any"],
    ["any for null", { type: "null" }, "any"],
  ];
  for (const [name, schema, kind] of cases) {
    test(`is ${name}`, () => {
      expect(kindOf(schema, NO_ROOT)).toBe(kind as ReturnType<typeof kindOf>);
    });
  }
});

describe("humanize and labelOf", () => {
  test("sentence-cases snake_case and camelCase names", () => {
    expect(humanize("project_id")).toBe("Project id");
    expect(humanize("lastSeenAt")).toBe("Last seen at");
    expect(humanize("EmailTarget")).toBe("Email target");
    expect(humanize("  padded  ")).toBe("Padded");
  });

  test("labels a field by its explicit title", () => {
    expect(labelOf("timeout_s", { title: "Timeout (seconds)" })).toBe("Timeout (seconds)");
  });

  test("ignores the title pydantic derives from the name and humanizes the name instead", () => {
    expect(labelOf("project_id", { title: "Project Id" })).toBe("Project id");
    expect(labelOf("project_id", {})).toBe("Project id");
  });

  test("prefers the short label on dense surfaces only", () => {
    const schema: JsonSchema = {
      title: "Commits ahead of upstream",
      "x-ui": { short_label: "ahead" },
    };
    expect(labelOf("ahead_count", schema, true)).toBe("ahead");
    expect(labelOf("ahead_count", schema)).toBe("Commits ahead of upstream");
  });

  test("keeps an explicitly empty short label empty on dense surfaces", () => {
    expect(labelOf("note", { "x-ui": { short_label: "" } }, true)).toBe("");
  });

  test("falls back to the normal label on dense surfaces without a short label", () => {
    expect(labelOf("note_text", {}, true)).toBe("Note text");
  });
});

describe("fields", () => {
  test("orders fields by group first appearance, then order, then declaration", () => {
    const schema = objectSchema({
      loose_a: { type: "string" },
      second: { type: "string", "x-ui": { group: "Late", order: 2 } },
      first: { type: "string", "x-ui": { group: "Early", order: 1 } },
      late_first: { type: "string", "x-ui": { group: "Late", order: 1 } },
      loose_b: { type: "string" },
      unordered_one: { type: "string", "x-ui": { group: "Late" } },
      unordered_two: { type: "string", "x-ui": { group: "Late" } },
    });
    expect(fields(schema, schema).map((field) => field.name)).toEqual([
      "loose_a",
      "loose_b",
      "late_first",
      "second",
      "unordered_one",
      "unordered_two",
      "first",
    ]);
  });

  test("resolves each field and reports required, nullable, hints and declaration index", () => {
    const schema: JsonSchema = {
      type: "object",
      required: ["name"],
      properties: {
        name: { type: "string" },
        port: { $ref: "#/$defs/Port" },
        note: { anyOf: [{ type: "string" }, { type: "null" }], "x-ui": { help: "optional" } },
      },
      $defs: { Port: { type: "integer", "x-ui": { unit: "tcp" } } },
    };
    const byName = Object.fromEntries(fields(schema, schema).map((field) => [field.name, field]));
    expect(byName.name).toMatchObject({ required: true, nullable: false, index: 0 });
    expect(byName.port).toMatchObject({
      required: false,
      index: 1,
      schema: { type: "integer" },
      ui: { unit: "tcp" },
    });
    expect(byName.note).toMatchObject({ nullable: true, index: 2, ui: { help: "optional" } });
  });

  test("is empty for a schema without properties", () => {
    expect(fields({ type: "object" }, NO_ROOT)).toEqual([]);
  });

  test("lays out the gallery's configuration by group", () => {
    const groups = fields(GALLERY_CONFIG, GALLERY_CONFIG)
      .filter((field) => field.ui.group !== undefined)
      .map((field) => field.ui.group);
    expect([...new Set(groups)]).toEqual([
      "Text",
      "Numbers",
      "Choices",
      "Paths",
      "Appearance",
      "Timing",
      "Secrets",
      "Advanced",
      "Delivery",
    ]);
  });
});

describe("defaultValue", () => {
  const cases: [string, JsonSchema, unknown][] = [
    ["a declared default", { type: "string", default: "preset" }, "preset"],
    ["a declared default of null", { type: "string", default: null }, null],
    [
      "null for a nullable value without a default",
      { anyOf: [{ type: "string" }, { type: "null" }] },
      null,
    ],
    ["an empty string", { type: "string" }, ""],
    ["zero for a number", { type: "number" }, 0],
    ["the minimum for an integer with one", { type: "integer", minimum: 3 }, 3],
    ["false for a boolean", { type: "boolean" }, false],
    ["the first member of an enumeration", { enum: ["x", "y"] }, "x"],
    ["null for an empty enumeration", { enum: [] }, null],
    ["the constant for a constant", { const: "fixed" }, "fixed"],
    ["an empty array", { type: "array", items: { type: "string" } }, []],
    ["an empty map", { type: "object", additionalProperties: { type: "string" } }, {}],
    ["null for a schema that says nothing", {}, null],
  ];
  for (const [name, schema, expected] of cases) {
    test(`is ${name}`, () => {
      expect(defaultValue(schema, NO_ROOT)).toEqual(expected);
    });
  }

  test("copies a declared default instead of sharing it", () => {
    const schema: JsonSchema = { type: "array", items: { type: "string" }, default: ["a"] };
    const value = defaultValue(schema, NO_ROOT) as string[];
    value.push("b");
    expect(schema.default).toEqual(["a"]);
  });

  test("builds an object from its required, defaulted and constant fields only", () => {
    const schema = objectSchema(
      {
        id: { type: "string" },
        retries: { type: "integer", default: 3 },
        tag: { const: "fixed" },
        optional: { type: "string" },
        nested: objectSchema({ inner: { type: "boolean" } }, { required: ["inner"] }),
      },
      { required: ["id", "nested"] },
    );
    expect(defaultValue(schema, schema)).toEqual({
      id: "",
      retries: 3,
      tag: "fixed",
      nested: { inner: false },
    });
  });

  test("starts a discriminated union from its first variant", () => {
    expect(defaultValue(GALLERY_CONFIG.properties?.target ?? {}, GALLERY_CONFIG)).toEqual({
      kind: "email",
      address: "me@example.com",
    });
  });

  test("starts the gallery's configuration from its published defaults", () => {
    expect(defaultValue(GALLERY_CONFIG, GALLERY_CONFIG)).toMatchObject({
      title: "Specimen",
      count: 3,
      enabled: true,
      flavor: "plain",
      timeout_s: 900,
      cron: "0 6 * * *",
    });
  });
});

describe("isVisible, inputFields and bindContext", () => {
  const dependent = onlyField({
    type: "string",
    "x-ui": { visible_if: { field: "mode", equals: "advanced" } },
  });

  test("shows a field without a condition whatever the siblings hold", () => {
    expect(isVisible(onlyField({ type: "string" }), {})).toBe(true);
  });

  test("shows a conditional field only while the sibling equals the expected value", () => {
    expect(isVisible(dependent, { mode: "advanced" })).toBe(true);
    expect(isVisible(dependent, { mode: "simple" })).toBe(false);
    expect(isVisible(dependent, {})).toBe(false);
  });

  const schema = objectSchema({
    name: { type: "string" },
    secret_marker: { type: "string", "x-ui": { hidden: true } },
    project_id: { type: "string", "x-ui": { bind: "project" } },
    path: { type: "string", "x-ui": { bind: "project", hidden: false } },
    note: { type: "string" },
  });

  test("lists the fields the user fills in, leaving out hidden and bound ones", () => {
    expect(inputFields(schema, schema).map((field) => field.name)).toEqual(["name", "note"]);
  });

  test("fills every bound field from the page context without touching the original value", () => {
    const original = { name: "n", project_id: "stale" };
    const bound = bindContext(schema, schema, original, { projectId: "alpha" });
    expect(bound).toEqual({ name: "n", project_id: "alpha", path: "alpha" });
    expect(original).toEqual({ name: "n", project_id: "stale" });
  });

  test("leaves bound fields alone when the page has no project", () => {
    expect(bindContext(schema, schema, { project_id: "kept" }, {})).toEqual({ project_id: "kept" });
  });

  test("returns a value that is not an object as it is", () => {
    expect(bindContext(schema, schema, null, { projectId: "alpha" })).toBeNull();
    expect(bindContext(schema, schema, "text", { projectId: "alpha" })).toBe("text");
    expect(bindContext(schema, schema, undefined, { projectId: "alpha" })).toBeUndefined();
  });
});

describe("toneFor and enumLabel", () => {
  const status: JsonSchema = {
    type: "string",
    "x-ui": {
      tones: { failing: "bad", good: "good", true: "info" },
      enum_labels: { failing: "Failing", good: "Good" },
    },
  };

  test("maps a value to its declared tone", () => {
    expect(toneFor(status, "failing")).toBe("bad");
    expect(toneFor(status, "good")).toBe("good");
  });

  test("has no tone for an undeclared value or a schema without tones", () => {
    expect(toneFor(status, "mystery")).toBeUndefined();
    expect(toneFor({ type: "string" }, "good")).toBeUndefined();
  });

  test("looks tones up by the value's string form", () => {
    expect(toneFor(status, true)).toBe("info");
  });

  test("labels a value by its declared label and otherwise by the value itself", () => {
    expect(enumLabel(status, "failing")).toBe("Failing");
    expect(enumLabel(status, "other")).toBe("other");
    expect(enumLabel({ type: "string" }, 7)).toBe("7");
  });

  test("reads hints from a schema that carries none as empty", () => {
    expect(hints({ type: "string" })).toEqual({});
    expect(hints(GALLERY_STATE.properties?.status ?? {}).format).toBe("badge");
  });
});

describe("describeCron", () => {
  const cases: [string, string][] = [
    ["*/5 * * * *", "Every 5 minutes"],
    ["7 * * * *", "Every hour at :07"],
    ["30 */2 * * *", "Every 2 hours at :30"],
    ["0 6 * * *", "Every day at 06:00"],
    ["15 14 * * 1", "Every Monday at 14:15"],
    ["0 0 * * 0", "Every Sunday at 00:00"],
    ["0 0 * * 7", "Every Sunday at 00:00"],
    ["45 18 * * 5", "Every Friday at 18:45"],
    ["0 9 * * 1-5", "Weekdays at 09:00"],
    ["0 9 15 * *", "Day 15 of every month at 09:00"],
    ["  0   6 * * *  ", "Every day at 06:00"],
  ];
  for (const [expression, sentence] of cases) {
    test(`reads "${expression}" as "${sentence}"`, () => {
      expect(describeCron(expression)).toBe(sentence);
    });
  }

  const customSchedules = [
    "0 9 1 6 *",
    "0,30 * * * *",
    "*/5 9 * * *",
    "0 0 * 3 1",
    "5 */2 1 * *",
    "0 0 * * 1,3",
  ];
  for (const expression of customSchedules) {
    test(`calls "${expression}" a custom schedule`, () => {
      expect(describeCron(expression)).toBe(`Custom schedule: ${expression}`);
    });
  }

  test("repeats a custom schedule trimmed of surrounding whitespace", () => {
    expect(describeCron("  0 9 1 6 *  ")).toBe("Custom schedule: 0 9 1 6 *");
  });

  const malformed = [
    "",
    "   ",
    "* * * *",
    "* * * * * *",
    "a b c d e",
    "*/ * * * *",
    "0 6 * * mon",
    "0-5-9 * * * *",
  ];
  for (const expression of malformed) {
    test(`rejects the malformed expression "${expression}"`, () => {
      expect(describeCron(expression)).toBeNull();
    });
  }
});
