import { describe, expect, test } from "bun:test";
import { fireEvent, screen, within } from "@testing-library/react";
import { defaultValue, type JsonSchema } from "@/schema/schema";
import { formField, installHarness } from "./harness";
import { GALLERY_CONFIG, objectSchema, renderForm } from "./schema-support";

installHarness();

const INITIAL = defaultValue(GALLERY_CONFIG, GALLERY_CONFIG) as Record<string, unknown>;

function renderGallery(overrides: Record<string, unknown> = {}) {
  return renderForm(GALLERY_CONFIG, { ...INITIAL, ...overrides });
}

const fieldNames = () =>
  [...document.querySelectorAll("[data-field]")].map((row) => row.getAttribute("data-field"));
const legends = () => [...document.querySelectorAll("legend")].map((legend) => legend.textContent);
const nullable = (schema: JsonSchema): JsonSchema => ({ anyOf: [schema, { type: "null" }] });

describe("SchemaForm layout", () => {
  test("lays the gallery out in groups, in the order the schema gives", () => {
    renderGallery();
    expect(legends()).toEqual([
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
    const choices = screen.getByText("Choices", { selector: "legend" }).closest("fieldset");
    expect(
      [...(choices?.querySelectorAll("[data-field]") ?? [])].map((row) =>
        row.getAttribute("data-field"),
      ),
    ).toEqual(["enabled", "flavor", "strategy", "days", "labels"]);
  });

  test("orders the fields inside a group by their order hint", () => {
    renderGallery();
    const names = fieldNames();
    expect(names.indexOf("title")).toBeLessThan(names.indexOf("notes"));
    expect(names.indexOf("count")).toBeLessThan(names.indexOf("level"));
    expect(names.indexOf("folder")).toBeLessThan(names.indexOf("file"));
  });

  test("labels fields by title or by sentence-cased name", () => {
    renderGallery();
    expect(formField("timeout_s").getByText("Timeout s")).toBeTruthy();
    expect(formField("verbose_level").getByText("Verbose level")).toBeTruthy();
  });

  test("leaves out hidden fields but keeps their values when other fields change", async () => {
    const { user, last } = renderGallery();
    expect(document.querySelector('[data-field="hidden_marker"]')).toBeNull();
    await user.type(formField("title").getByRole("textbox"), "!");
    expect(last().hidden_marker).toBe("internal");
  });

  test("leaves out fields bound to page context and fields the caller omits", () => {
    const schema = objectSchema({
      name: { type: "string" },
      project_id: { type: "string", "x-ui": { bind: "project" } },
      owner: { type: "string" },
    });
    renderForm(schema, {}, { omit: ["owner"] });
    expect(fieldNames()).toEqual(["name"]);
  });

  test("renders fields without a group outside any fieldset", () => {
    const schema = objectSchema({
      loose: { type: "string" },
      grouped: { type: "string", "x-ui": { group: "Box" } },
    });
    renderForm(schema, {});
    expect(legends()).toEqual(["Box"]);
    const loose = document.querySelector('[data-field="loose"]');
    expect(loose?.closest("fieldset")).toBeNull();
    expect(document.querySelector('[data-field="grouped"]')?.closest("fieldset")).toBeTruthy();
  });

  test("starts from an empty record when the value is not an object", async () => {
    const schema = objectSchema({ name: { type: "string" } });
    const { user, last } = renderForm(schema, null);
    await user.type(screen.getByRole("textbox"), "a");
    expect(last()).toEqual({ name: "a" });
  });

  test("treats an array value as an empty record", async () => {
    const schema = objectSchema({ name: { type: "string" } });
    const { user, last } = renderForm(schema, ["stray"]);
    await user.type(screen.getByRole("textbox"), "a");
    expect(last()).toEqual({ name: "a" });
  });

  test("falls back to a text box for a widget it does not know", async () => {
    const { user, last } = renderForm(
      objectSchema({ name: { type: "string", "x-ui": { widget: "hologram" } } }),
      { name: "x" },
    );
    await user.type(screen.getByRole("textbox"), "y");
    expect(last()).toEqual({ name: "xy" });
  });

  test("edits several fields in turn and carries every edit in the final value", async () => {
    const { user, last } = renderGallery();
    await user.type(formField("title").getByRole("textbox"), "!");
    await user.click(formField("enabled").getByRole("switch"));
    await user.click(formField("flavor").getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "Sweet" }));
    await user.click(formField("days").getByRole("checkbox", { name: "fri" }));
    expect(last()).toEqual({
      ...INITIAL,
      title: "Specimen!",
      enabled: false,
      flavor: "sweet",
      days: ["fri"],
    });
  });
});

describe("required marker and help", () => {
  const schema = objectSchema(
    {
      plain_required: { type: "string" },
      defaulted_required: { type: "string", default: "x" },
      nullable_required: nullable({ type: "string" }),
      optional: { type: "string" },
      flag: { type: "boolean", description: "Flag help from the description" },
    },
    { required: ["plain_required", "defaulted_required", "nullable_required"] },
  );

  test("marks only required fields that must be answered", () => {
    renderForm(schema, {});
    expect(formField("plain_required").getByTitle("Required").textContent).toBe("*");
    for (const name of ["defaulted_required", "nullable_required", "optional", "flag"]) {
      expect(formField(name).queryByTitle("Required")).toBeNull();
    }
  });

  test("shows a field's description as help, but prefers the help hint", () => {
    renderForm(
      objectSchema({
        a: { type: "string", description: "from description" },
        b: { type: "string", description: "from description", "x-ui": { help: "from hint" } },
      }),
      {},
    );
    expect(formField("a").getByText("from description")).toBeTruthy();
    expect(formField("b").getByText("from hint")).toBeTruthy();
    expect(formField("b").queryByText("from description")).toBeNull();
  });

  test("shows help beside a boolean's label", () => {
    renderForm(schema, {});
    expect(formField("flag").getByText("Flag help from the description")).toBeTruthy();
  });

  test("shows the help of an advanced field once revealed", async () => {
    const { user } = renderGallery();
    await user.click(screen.getByRole("button", { name: "Advanced" }));
    expect(formField("tuning").getByText("Only for experts.")).toBeTruthy();
  });
});

describe("advanced fields", () => {
  test("stay collapsed behind a button until asked for", async () => {
    const { user } = renderGallery();
    const toggle = screen.getByRole("button", { name: "Advanced" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[data-field="tuning"]')).toBeNull();
    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(formField("tuning")).toBeTruthy();
    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[data-field="tuning"]')).toBeNull();
  });

  test("can be edited once revealed", async () => {
    const { user, last } = renderGallery();
    await user.click(screen.getByRole("button", { name: "Advanced" }));
    fireEvent.change(formField("tuning").getByRole("spinbutton"), { target: { value: "0.75" } });
    expect(last()).toEqual({ ...INITIAL, tuning: 0.75 });
  });

  test("offer no button when nothing is advanced", () => {
    renderForm(objectSchema({ name: { type: "string" } }), {});
    expect(screen.queryByRole("button", { name: "Advanced" })).toBeNull();
  });
});

describe("conditional fields", () => {
  test("appear and disappear with the field they depend on", async () => {
    const { user, last } = renderGallery();
    expect(formField("verbose_level")).toBeTruthy();
    await user.click(formField("enabled").getByRole("switch"));
    expect(document.querySelector('[data-field="verbose_level"]')).toBeNull();
    expect(last()).toEqual({ ...INITIAL, enabled: false });
    await user.click(formField("enabled").getByRole("switch"));
    expect(formField("verbose_level").getByRole("spinbutton")).toBeTruthy();
  });

  test("start hidden when the condition does not hold", () => {
    renderGallery({ enabled: false });
    expect(document.querySelector('[data-field="verbose_level"]')).toBeNull();
  });
});

describe("nested objects", () => {
  const schema = objectSchema({
    server: objectSchema(
      {
        host: { type: "string", "x-ui": { group: "Network" } },
        port: { type: "integer", default: 80 },
      },
      { required: ["host"] },
    ),
  });

  test("render their fields inline without group headings", () => {
    renderForm(schema, { server: { host: "h", port: 1 } });
    expect(fieldNames()).toEqual(["server", "host", "port"]);
    expect(legends()).toEqual([]);
  });

  test("emit the whole parent with only the changed child replaced", async () => {
    const { user, last } = renderForm(schema, { server: { host: "h", port: 1 } });
    await user.type(formField("host").getByRole("textbox"), "x");
    expect(last()).toEqual({ server: { host: "hx", port: 1 } });
  });

  test("build the child from nothing when the parent has no value yet", async () => {
    const { user, last } = renderForm(schema, {});
    await user.type(formField("host").getByRole("textbox"), "x");
    expect(last()).toEqual({ server: { host: "x" } });
  });
});

describe("discriminated unions", () => {
  const target = (kind: string) => screen.getByRole("radio", { name: kind });
  const withTarget = (value: Record<string, unknown>) => renderGallery({ target: value });

  test("show the chosen variant selected and only that variant's fields", () => {
    withTarget({ kind: "email", address: "me@example.com" });
    expect(target("Email target").getAttribute("aria-checked")).toBe("true");
    expect(target("Webhook target").getAttribute("aria-checked")).toBe("false");
    expect(formField("address").getByRole("textbox")).toHaveProperty("value", "me@example.com");
    expect(document.querySelector('[data-field="url"]')).toBeNull();
    expect(document.querySelector('[data-field="kind"]')).toBeNull();
  });

  test("keep the discriminator when a variant field is edited", async () => {
    const { user, last } = withTarget({ kind: "email", address: "me@example.com" });
    await user.type(formField("address").getByRole("textbox"), "x");
    expect(last().target).toEqual({ kind: "email", address: "me@example.comx" });
  });

  test("switch to another variant by starting it from its own defaults", async () => {
    const { user, last } = withTarget({ kind: "email", address: "edited@example.com" });
    await user.click(target("Webhook target"));
    expect(last()).toEqual({
      ...INITIAL,
      target: { kind: "webhook", url: "https://example.com/hook", secret: "" },
    });
    expect(target("Webhook target").getAttribute("aria-checked")).toBe("true");
    expect(formField("url").getByRole("textbox")).toHaveProperty(
      "value",
      "https://example.com/hook",
    );
    expect(document.querySelector('[data-field="address"]')).toBeNull();
    expect(formField("secret").getByRole("button", { name: "Show value" })).toBeTruthy();
  });

  test("switch back to the first variant with its defaults, not the earlier edits", async () => {
    const { user, last } = withTarget({ kind: "email", address: "edited@example.com" });
    await user.click(target("Webhook target"));
    await user.click(target("Email target"));
    expect(last().target).toEqual({ kind: "email", address: "me@example.com" });
  });

  test("emit nothing when the selected variant is clicked again", async () => {
    const { user, changes } = withTarget({ kind: "email", address: "a" });
    await user.click(target("Email target"));
    expect(changes).toEqual([]);
  });

  test("offer every variant by its title", () => {
    withTarget({ kind: "email", address: "a" });
    const union = within(document.querySelector("[data-widget=union]") as HTMLElement);
    expect(union.getAllByRole("radio").map((radio) => radio.textContent)).toEqual([
      "Email target",
      "Webhook target",
    ]);
  });

  test("show the first variant's fields for a tag it does not know", () => {
    withTarget({ kind: "sms", address: "555" });
    expect(target("Email target").getAttribute("aria-checked")).toBe("false");
    expect(target("Webhook target").getAttribute("aria-checked")).toBe("false");
    expect(formField("address").getByRole("textbox")).toHaveProperty("value", "555");
  });

  test("select the first variant when there is no value yet", () => {
    renderGallery();
    expect(target("Email target").getAttribute("aria-checked")).toBe("true");
    expect(formField("address")).toBeTruthy();
  });

  test("carry the displayed variant's tag when its fields are edited first", async () => {
    const { user, last } = renderGallery();
    await user.type(formField("address").getByRole("textbox"), "x");
    expect(last().target).toMatchObject({ kind: "email" });
  });

  test("work inside a union that is not discriminated by a declared property", async () => {
    const schema: JsonSchema = {
      ...objectSchema({
        shape: {
          anyOf: [{ $ref: "#/$defs/Circle" }, { $ref: "#/$defs/Square" }],
        },
      }),
      $defs: {
        Circle: {
          title: "Circle",
          properties: { type: { const: "circle" }, radius: { type: "number", default: 1 } },
        },
        Square: {
          title: "Square",
          properties: { type: { const: "square" }, side: { type: "number", default: 2 } },
        },
      },
    };
    const { user, last } = renderForm(schema, { shape: { type: "circle", radius: 5 } });
    expect(formField("radius")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Square" }));
    expect(last()).toEqual({ shape: { type: "square", side: 2 } });
  });
});

describe("lists of objects", () => {
  const rows = [
    { name: "web", port: 80, enabled: true },
    { name: "api", port: 8000, enabled: false },
  ];
  const endpoints = () => within(document.querySelector('[data-field="endpoints"]') as HTMLElement);

  test("render one editable row per entry", () => {
    renderGallery({ endpoints: rows });
    expect(
      endpoints()
        .getAllByRole("textbox")
        .map((box) => (box as HTMLInputElement).value),
    ).toEqual(["web", "api"]);
    expect(
      endpoints()
        .getAllByRole("spinbutton")
        .map((box) => (box as HTMLInputElement).value),
    ).toEqual(["80", "8000"]);
    expect(
      endpoints()
        .getAllByRole("switch")
        .map((box) => box.getAttribute("aria-checked")),
    ).toEqual(["true", "false"]);
  });

  test("replace only the edited row", async () => {
    const { user, last } = renderGallery({ endpoints: rows });
    await user.type(endpoints().getAllByRole("textbox")[1] as HTMLElement, "2");
    expect(last().endpoints).toEqual([rows[0], { name: "api2", port: 8000, enabled: false }]);
  });

  test("replace nested toggles in the right row", async () => {
    const { user, last } = renderGallery({ endpoints: rows });
    await user.click(endpoints().getAllByRole("switch")[1] as HTMLElement);
    expect(last().endpoints).toEqual([rows[0], { name: "api", port: 8000, enabled: true }]);
  });

  test("append a row built from the item's defaults", async () => {
    const { user, last } = renderGallery({ endpoints: rows });
    await user.click(endpoints().getByRole("button", { name: "Add" }));
    expect(last().endpoints).toEqual([...rows, { name: "local", port: 8080, enabled: true }]);
    expect(endpoints().getAllByRole("textbox")).toHaveLength(3);
  });

  test("remove the chosen row", async () => {
    const { user, last } = renderGallery({ endpoints: rows });
    await user.click(endpoints().getAllByRole("button", { name: "Remove row" })[0] as HTMLElement);
    expect(last().endpoints).toEqual([rows[1]]);
    expect(endpoints().getAllByRole("textbox")).toHaveLength(1);
  });

  test("start with only the add button when there are no rows", async () => {
    const { user, last } = renderGallery();
    expect(endpoints().queryByRole("textbox")).toBeNull();
    expect(endpoints().queryByRole("button", { name: "Remove row" })).toBeNull();
    await user.click(endpoints().getByRole("button", { name: "Add" }));
    expect(last().endpoints).toEqual([{ name: "local", port: 8080, enabled: true }]);
  });

  test("show an empty row for an entry that is not an object", () => {
    renderGallery({ endpoints: [null] });
    expect(endpoints().getByRole("textbox")).toHaveProperty("value", "");
  });
});
