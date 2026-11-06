import { describe, expect, test } from "bun:test";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { defaultValue, type JsonSchema } from "@/schema/schema";
import { inferWidget } from "@/schema/widgets";
import { formField, installHarness, mockApi } from "./harness";
import {
  BROWSE,
  browseTree,
  folderEntry,
  folderListing,
  GALLERY_CONFIG,
  objectSchema,
  onlyField,
  renderForm,
} from "./schema-support";

installHarness();

const INITIAL = defaultValue(GALLERY_CONFIG, GALLERY_CONFIG) as Record<string, unknown>;

function renderGallery(overrides: Record<string, unknown> = {}) {
  return renderForm(GALLERY_CONFIG, { ...INITIAL, ...overrides });
}

/** A one-field form, so a widget can be driven with a schema written for the case at hand. */
function renderField(property: JsonSchema, initial?: unknown, required = false) {
  const schema = objectSchema({ subject: property }, required ? { required: ["subject"] } : {});
  const rendered = renderForm(schema, initial === undefined ? {} : { subject: initial });
  return { ...rendered, row: formField("subject") };
}

function control(): HTMLInputElement | HTMLTextAreaElement {
  const element = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(
    '[data-field="subject"] input, [data-field="subject"] textarea',
  );
  if (!element) throw new Error("no input in the field");
  return element;
}

const nullable = (schema: JsonSchema): JsonSchema => ({ anyOf: [schema, { type: "null" }] });

describe("text-like widgets", () => {
  test("text edits the string and keeps the other fields untouched", async () => {
    const { user, last } = renderGallery();
    await user.type(formField("title").getByPlaceholderText("A title"), "!");
    expect(last()).toEqual({ ...INITIAL, title: "Specimen!" });
  });

  test("textarea shows its help text and edits the string", async () => {
    const { user, last } = renderGallery();
    expect(formField("notes").getByText("Free-form notes.")).toBeTruthy();
    await user.type(formField("notes").getByRole("textbox"), "line one");
    expect(last()).toEqual({ ...INITIAL, notes: "line one" });
  });

  test("code edits the script as plain text without spell checking", async () => {
    const { user, last } = renderGallery();
    const script = formField("script").getByRole("textbox") as HTMLTextAreaElement;
    expect(script.value).toBe("print('hi')");
    expect(script.getAttribute("spellcheck")).toBe("false");
    await user.type(script, "!");
    expect(last().script).toBe("print('hi')!");
  });

  test("model picker edits the model and offers the known aliases", async () => {
    const { user, last } = renderGallery();
    const input = formField("model").getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("haiku");
    const options = [...document.querySelectorAll(`datalist#${input.getAttribute("list")} option`)];
    expect(options.map((option) => option.getAttribute("value"))).toEqual([
      "haiku",
      "sonnet",
      "opus",
    ]);
    await user.clear(input);
    await user.type(input, "opus");
    expect(last().model).toBe("opus");
  });

  test("model picker offers the CLI default when empty", () => {
    renderField({ type: "string", "x-ui": { widget: "model-picker" } }, "");
    expect(screen.getByPlaceholderText("CLI default")).toBeTruthy();
  });

  const clearable = ["text", "textarea", "secret", "code", "model-picker", "path-dir", "path-file"];
  for (const widget of clearable) {
    test(`${widget} empties to null when the field is nullable`, async () => {
      const { user, last } = renderField(
        { ...nullable({ type: "string" }), "x-ui": { widget } },
        "something",
      );
      await user.clear(control());
      expect(last()).toEqual({ subject: null });
    });

    test(`${widget} empties to an empty string when the field is not nullable`, async () => {
      const { user, last } = renderField({ type: "string", "x-ui": { widget } }, "something");
      await user.clear(control());
      expect(last()).toEqual({ subject: "" });
    });
  }

  test("text limits its length to the schema's maximum and shows the placeholder", () => {
    renderField({ type: "string", maxLength: 12, "x-ui": { placeholder: "short name" } }, "");
    const input = screen.getByPlaceholderText("short name");
    expect(input.getAttribute("maxlength")).toBe("12");
  });
});

describe("number widget", () => {
  test("edits an integer and shows its unit and bounds", async () => {
    const { user, last } = renderGallery();
    const count = formField("count").getByRole("spinbutton") as HTMLInputElement;
    expect(formField("count").getByText("items")).toBeTruthy();
    expect(count.getAttribute("min")).toBe("0");
    expect(count.getAttribute("step")).toBe("1");
    expect(count.getAttribute("inputmode")).toBe("numeric");
    await user.type(count, "5");
    expect(last()).toEqual({ ...INITIAL, count: 35 });
  });

  test("falls back to zero when an integer is cleared", async () => {
    const { user, last } = renderGallery();
    await user.clear(formField("count").getByRole("spinbutton"));
    expect(last()).toEqual({ ...INITIAL, count: 0 });
  });

  test("becomes null when a nullable number is cleared", async () => {
    const { user, last } = renderField(nullable({ type: "integer" }), 4);
    await user.clear(screen.getByRole("spinbutton"));
    expect(last()).toEqual({ subject: null });
  });

  test("shows an empty box for a missing value", () => {
    renderField({ type: "integer" });
    expect((screen.getByRole("spinbutton") as HTMLInputElement).value).toBe("");
  });

  test("parses decimals for a floating point field with free stepping", () => {
    const { last } = renderField({ type: "number", "x-ui": { placeholder: "ratio" } }, 0.5);
    const input = screen.getByPlaceholderText("ratio") as HTMLInputElement;
    expect(input.getAttribute("step")).toBe("any");
    expect(input.getAttribute("inputmode")).toBe("decimal");
    fireEvent.change(input, { target: { value: "2.75" } });
    expect(last()).toEqual({ subject: 2.75 });
  });

  test("takes its bounds from exclusive limits and its step from the hint", () => {
    renderField(
      { type: "number", exclusiveMinimum: 1, exclusiveMaximum: 9, "x-ui": { step: 0.5 } },
      2,
    );
    const input = screen.getByRole("spinbutton");
    expect(input.getAttribute("min")).toBe("1");
    expect(input.getAttribute("max")).toBe("9");
    expect(input.getAttribute("step")).toBe("0.5");
  });
});

describe("slider widget", () => {
  test("moves by one step per key press within the field's bounds", async () => {
    const { user, last } = renderGallery();
    const slider = formField("level").getByRole("slider");
    expect(formField("level").getByText("5")).toBeTruthy();
    slider.focus();
    await user.keyboard("{ArrowRight}");
    expect(last()).toEqual({ ...INITIAL, level: 6 });
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(last().level).toBe(4);
    await user.keyboard("{Home}");
    expect(last().level).toBe(0);
    await user.keyboard("{End}");
    expect(last().level).toBe(10);
    expect(formField("level").getByText("10")).toBeTruthy();
  });

  test("starts at the minimum when the value is missing and honours step and bounds hints", async () => {
    const { user, last } = renderField({
      type: "integer",
      minimum: 0,
      maximum: 100,
      "x-ui": { widget: "slider", min: 20, max: 60, step: 10 },
    });
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("aria-valuenow")).toBe("20");
    expect(slider.getAttribute("aria-valuemax")).toBe("60");
    slider.focus();
    await user.keyboard("{ArrowRight}");
    expect(last()).toEqual({ subject: 30 });
  });

  test("falls back to the schema's bounds and then to 0 to 100", () => {
    renderField({ type: "integer", minimum: 5, maximum: 15, "x-ui": { widget: "slider" } });
    expect(screen.getByRole("slider").getAttribute("aria-valuemin")).toBe("5");
    expect(screen.getByRole("slider").getAttribute("aria-valuemax")).toBe("15");
  });

  test("spans 0 to 100 when nothing bounds it", () => {
    renderField({ type: "integer", "x-ui": { widget: "slider" } }, 40);
    expect(screen.getByRole("slider").getAttribute("aria-valuemin")).toBe("0");
    expect(screen.getByRole("slider").getAttribute("aria-valuemax")).toBe("100");
  });
});

describe("toggle widget", () => {
  test("flips the boolean and reflects the current state", async () => {
    const { user, last } = renderGallery();
    const toggle = formField("enabled").getByRole("switch");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    await user.click(toggle);
    expect(last()).toEqual({ ...INITIAL, enabled: false });
    expect(formField("enabled").getByRole("switch").getAttribute("aria-checked")).toBe("false");
    await user.click(formField("enabled").getByRole("switch"));
    expect(last().enabled).toBe(true);
  });

  test("treats a missing value as off", () => {
    renderField({ type: "boolean" });
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });
});

describe("select widget", () => {
  test("offers every option by its label and sends the chosen value", async () => {
    const { user, last } = renderGallery();
    const trigger = formField("flavor").getByRole("combobox");
    expect(trigger.textContent).toBe("Plain");
    await user.click(trigger);
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "Plain",
      "Spicy",
      "Sweet",
    ]);
    await user.click(screen.getByRole("option", { name: "Spicy" }));
    expect(last()).toEqual({ ...INITIAL, flavor: "spicy" });
  });

  test("falls back to the raw value for options without a label", async () => {
    const { user, last } = renderField({ type: "string", enum: ["alpha", "beta"] }, "alpha");
    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "beta" }));
    expect(last()).toEqual({ subject: "beta" });
  });

  test("lets a nullable select go back to none", async () => {
    const { user, last } = renderField(
      { ...nullable({ type: "string", enum: ["alpha", "beta"] }), "x-ui": { widget: "select" } },
      "alpha",
    );
    await user.click(screen.getByRole("combobox"));
    expect(await screen.findByRole("option", { name: "None" })).toBeTruthy();
    await user.click(screen.getByRole("option", { name: "None" }));
    expect(last()).toEqual({ subject: null });
    expect(screen.getByRole("combobox").textContent).toBe("None");
  });

  test("offers no none option when the field is not nullable", async () => {
    const { user } = renderField({ type: "string", enum: ["alpha", "beta"] }, "alpha");
    await user.click(screen.getByRole("combobox"));
    await screen.findAllByRole("option");
    expect(screen.queryByRole("option", { name: "None" })).toBeNull();
  });
});

describe("radio-cards widget", () => {
  test("describes each choice, marks the current one and sends the picked one", async () => {
    const { user, last } = renderGallery();
    const scope = formField("strategy");
    expect(scope.getByText("Keep history, add a merge commit.")).toBeTruthy();
    expect(scope.getByText("Replay commits on top.")).toBeTruthy();
    expect(scope.getByRole("radio", { name: /Merge/ }).getAttribute("aria-checked")).toBe("true");
    await user.click(scope.getByRole("radio", { name: /Rebase/ }));
    expect(last()).toEqual({ ...INITIAL, strategy: "rebase" });
    expect(scope.getByRole("radio", { name: /Rebase/ }).getAttribute("aria-checked")).toBe("true");
  });

  test("shows a choice without a description as its label alone", () => {
    renderField({
      type: "string",
      enum: ["a", "b"],
      "x-ui": { widget: "radio-cards", enum_descriptions: { a: "Only a is explained" } },
    });
    expect(screen.getByRole("radio", { name: "b" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /Only a is explained/ })).toBeTruthy();
  });
});

describe("multiselect widget", () => {
  test("checks and unchecks options, always emitting them in the schema's order", async () => {
    const { user, last } = renderGallery();
    const scope = formField("days");
    expect(
      scope.getAllByRole("checkbox").map((box: HTMLElement) => box.getAttribute("aria-checked")),
    ).toEqual(["false", "false", "false", "false", "false"]);
    await user.click(scope.getByRole("checkbox", { name: "wed" }));
    expect(last()).toEqual({ ...INITIAL, days: ["wed"] });
    await user.click(scope.getByRole("checkbox", { name: "mon" }));
    expect(last().days).toEqual(["mon", "wed"]);
    await user.click(scope.getByRole("checkbox", { name: "wed" }));
    expect(last().days).toEqual(["mon"]);
    expect(scope.getByRole("checkbox", { name: "mon" }).getAttribute("aria-checked")).toBe("true");
  });

  test("labels options from the item schema's enum labels", () => {
    renderField(
      {
        type: "array",
        items: { type: "string", enum: ["a", "b"], "x-ui": { enum_labels: { a: "Alpha" } } },
      },
      ["b"],
    );
    expect(screen.getByRole("checkbox", { name: "Alpha" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "b" }).getAttribute("aria-checked")).toBe("true");
  });

  test("resolves referenced item enums", () => {
    const schema: JsonSchema = {
      ...objectSchema({ subject: { type: "array", items: { $ref: "#/$defs/Day" } } }),
      $defs: { Day: { type: "string", enum: ["sat", "sun"] } },
    };
    renderForm(schema, { subject: ["sun"] });
    expect(screen.getByRole("checkbox", { name: "sun" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("checkbox", { name: "sat" }).getAttribute("aria-checked")).toBe(
      "false",
    );
  });
});

describe("tags widget", () => {
  const tagInput = () => formField("labels").getByRole("textbox");

  test("adds a tag on Enter and clears the box", async () => {
    const { user, last } = renderGallery();
    await user.type(tagInput(), "urgent{Enter}");
    expect(last()).toEqual({ ...INITIAL, labels: ["urgent"] });
    expect((tagInput() as HTMLInputElement).value).toBe("");
  });

  test("adds a tag on comma and keeps earlier tags", async () => {
    const { user, last } = renderGallery({ labels: ["one"] });
    await user.type(tagInput(), "two,");
    expect(last().labels).toEqual(["one", "two"]);
  });

  test("adds the pending text as a tag when the box loses focus", async () => {
    const { user, last } = renderGallery();
    await user.type(tagInput(), "pending");
    await user.tab();
    expect(last().labels).toEqual(["pending"]);
  });

  test("ignores blank input", async () => {
    const { user, changes } = renderGallery();
    await user.type(tagInput(), "   {Enter}");
    expect(changes).toEqual([]);
  });

  test("trims the text it adds", async () => {
    const { user, last } = renderGallery();
    await user.type(tagInput(), "  padded  {Enter}");
    expect(last().labels).toEqual(["padded"]);
  });

  test("removes a tag with its button", async () => {
    const { user, last } = renderGallery({ labels: ["a", "b", "c"] });
    await user.click(formField("labels").getByRole("button", { name: "Remove b" }));
    expect(last().labels).toEqual(["a", "c"]);
  });

  test("removes the last tag with Backspace in an empty box only", async () => {
    const { user, last } = renderGallery({ labels: ["a", "b"] });
    await user.click(tagInput());
    await user.keyboard("{Backspace}");
    expect(last().labels).toEqual(["a"]);
    await user.type(tagInput(), "x");
    await user.keyboard("{Backspace}");
    expect(last().labels).toEqual(["a"]);
    expect((tagInput() as HTMLInputElement).value).toBe("");
  });

  test("keeps Backspace harmless when there are no tags", async () => {
    const { user, changes } = renderGallery();
    await user.click(tagInput());
    await user.keyboard("{Backspace}");
    expect(changes).toEqual([]);
  });

  test("invites typing only while there are no tags", () => {
    renderGallery();
    expect(screen.getByPlaceholderText("Type and press Enter")).toBeTruthy();
  });

  test("uses the field's own placeholder when it has one", () => {
    renderField({
      type: "array",
      items: { type: "string" },
      "x-ui": { placeholder: "add a label" },
    });
    expect(screen.getByPlaceholderText("add a label")).toBeTruthy();
  });

  test("shows no placeholder once a tag exists", () => {
    renderGallery({ labels: ["a"] });
    expect(screen.queryByPlaceholderText("Type and press Enter")).toBeNull();
  });
});

describe("color widget", () => {
  const colorInput = () => {
    const picker = document.querySelector<HTMLInputElement>(
      '[data-field="accent"] input[type=color]',
    );
    if (!picker) throw new Error("no colour picker");
    return picker;
  };

  test("keeps the picker and the hex box in step with the value", () => {
    renderGallery();
    expect(colorInput().value).toBe("#3b82f6");
    expect((formField("accent").getByRole("textbox") as HTMLInputElement).value).toBe("#3b82f6");
  });

  test("emits the hex typed into the text box", () => {
    const { last } = renderGallery();
    const hex = formField("accent").getByRole("textbox");
    fireEvent.change(hex, { target: { value: "#ff0000" } });
    expect(last()).toEqual({ ...INITIAL, accent: "#ff0000" });
  });

  test("emits the colour chosen in the picker", () => {
    const { last } = renderGallery();
    fireEvent.change(colorInput(), { target: { value: "#00ff00" } });
    expect(last().accent).toBe("#00ff00");
  });

  test("shows black for an empty value", () => {
    renderField({ type: "string", "x-ui": { widget: "color" } }, "");
    expect(screen.getAllByDisplayValue("#000000")).toHaveLength(2);
  });
});

describe("duration widget", () => {
  const amount = () => formField("timeout_s").getByRole("spinbutton") as HTMLInputElement;
  const unit = () => formField("timeout_s").getByRole("combobox", { name: "Unit" });

  test("shows the value in the largest unit that divides it evenly", () => {
    renderGallery();
    expect(amount().value).toBe("15");
    expect(unit().textContent).toBe("minutes");
  });

  test("shows hours and days when they divide evenly", () => {
    renderField({ type: "integer", "x-ui": { widget: "duration" } }, 7200);
    expect(screen.getByRole("spinbutton")).toHaveProperty("value", "2");
    expect(screen.getByRole("combobox", { name: "Unit" }).textContent).toBe("hours");
  });

  test("shows days for a whole number of days", () => {
    renderField({ type: "integer", "x-ui": { widget: "duration" } }, 172800);
    expect(screen.getByRole("spinbutton")).toHaveProperty("value", "2");
    expect(screen.getByRole("combobox", { name: "Unit" }).textContent).toBe("days");
  });

  test("shows seconds when no larger unit divides the value", () => {
    renderField({ type: "integer", "x-ui": { widget: "duration" } }, 90);
    expect(screen.getByRole("spinbutton")).toHaveProperty("value", "90");
    expect(screen.getByRole("combobox", { name: "Unit" }).textContent).toBe("seconds");
  });

  test("converts the typed amount to seconds in the shown unit", () => {
    const { last } = renderGallery();
    fireEvent.change(amount(), { target: { value: "20" } });
    expect(last()).toEqual({ ...INITIAL, timeout_s: 1200 });
  });

  test("rounds fractional amounts to whole seconds", () => {
    const { last } = renderGallery();
    fireEvent.change(amount(), { target: { value: "1.5" } });
    expect(last().timeout_s).toBe(90);
  });

  test("re-reads the same value in another unit without changing it", async () => {
    const { user, changes, last } = renderGallery({ timeout_s: 3600 });
    expect(amount().value).toBe("1");
    await user.click(unit());
    await user.click(await screen.findByRole("option", { name: "minutes" }));
    expect(amount().value).toBe("60");
    expect(changes).toEqual([]);
    fireEvent.change(amount(), { target: { value: "90" } });
    expect(last().timeout_s).toBe(5400);
  });

  test("falls back to zero when the amount is cleared", async () => {
    const { user, last } = renderGallery();
    await user.clear(amount());
    expect(last().timeout_s).toBe(0);
  });

  test("becomes null when a nullable duration is cleared, and shows an empty box for null", async () => {
    const { user, last } = renderField(
      { ...nullable({ type: "integer" }), "x-ui": { widget: "duration" } },
      null,
    );
    expect(screen.getByRole("spinbutton")).toHaveProperty("value", "");
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "3" } });
    expect(last()).toEqual({ subject: 3 });
    await user.clear(screen.getByRole("spinbutton"));
    expect(last()).toEqual({ subject: null });
  });

  test("shows zero for a missing value that is not nullable", () => {
    renderField({ type: "integer", "x-ui": { widget: "duration" } });
    expect(screen.getByRole("spinbutton")).toHaveProperty("value", "0");
  });
});

describe("cron widget", () => {
  const expression = () => formField("cron").getByRole("textbox") as HTMLInputElement;

  test("describes the schedule in plain language", () => {
    renderGallery();
    expect(expression().value).toBe("0 6 * * *");
    expect(formField("cron").getByText("Every day at 06:00")).toBeTruthy();
    expect(expression().getAttribute("aria-invalid")).toBe("false");
  });

  test("emits the expression as typed and updates the description", async () => {
    const { user, last } = renderGallery();
    await user.clear(expression());
    await user.type(expression(), "*/10 * * * *");
    expect(last()).toEqual({ ...INITIAL, cron: "*/10 * * * *" });
    expect(formField("cron").getByText("Every 10 minutes")).toBeTruthy();
  });

  test("explains the shape when the expression is not five fields", async () => {
    const { user } = renderGallery();
    await user.type(expression(), " oops");
    expect(
      formField("cron").getByText("Five fields: minute, hour, day of month, month, day of week."),
    ).toBeTruthy();
    expect(expression().getAttribute("aria-invalid")).toBe("true");
  });

  test("hints the field layout with the default placeholder or the field's own", () => {
    renderField({ type: "string", "x-ui": { widget: "cron" } }, "");
    expect(screen.getByPlaceholderText("m h dom mon dow")).toBeTruthy();
    renderField({ type: "string", "x-ui": { widget: "cron", placeholder: "0 * * * *" } }, "");
    expect(screen.getByPlaceholderText("0 * * * *")).toBeTruthy();
  });
});

describe("secret widget", () => {
  test("hides the value until asked to show it", async () => {
    renderGallery({ token: "s3cret" });
    const scope = formField("token");
    const input = scope.getByDisplayValue("s3cret") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.getAttribute("autocomplete")).toBe("off");
  });

  test("toggles visibility with a button that says what it will do", async () => {
    const { user } = renderGallery({ token: "s3cret" });
    const scope = formField("token");
    await user.click(scope.getByRole("button", { name: "Show value" }));
    expect((scope.getByDisplayValue("s3cret") as HTMLInputElement).type).toBe("text");
    await user.click(scope.getByRole("button", { name: "Hide value" }));
    expect((scope.getByDisplayValue("s3cret") as HTMLInputElement).type).toBe("password");
  });

  test("edits the secret", async () => {
    const { user, last } = renderGallery();
    await user.type(formField("token").getByDisplayValue(""), "abc");
    expect(last()).toEqual({ ...INITIAL, token: "abc" });
  });
});

describe("json widget", () => {
  const editor = () => formField("extra").getByRole("textbox") as HTMLTextAreaElement;

  test("starts from the pretty-printed value, or an empty object", () => {
    renderGallery();
    expect(editor().value).toBe("{}");
    expect(editor().getAttribute("aria-invalid")).toBe("false");
  });

  test("pretty-prints an existing value", () => {
    renderGallery({ extra: { a: 1, b: [true] } });
    expect(editor().value).toBe(JSON.stringify({ a: 1, b: [true] }, null, 2));
  });

  test("emits the parsed value as soon as the text is valid JSON", async () => {
    const { user, last } = renderGallery();
    await user.clear(editor());
    await user.paste('{"retries": 3, "tags": ["x"]}');
    expect(last()).toEqual({ ...INITIAL, extra: { retries: 3, tags: ["x"] } });
    expect(editor().getAttribute("aria-invalid")).toBe("false");
  });

  test("flags invalid JSON, emits nothing for it, and recovers once it is fixed", async () => {
    const { user, changes, last } = renderGallery();
    await user.clear(editor());
    changes.length = 0;
    await user.paste('{"broken": ');
    expect(changes).toEqual([]);
    expect(editor().getAttribute("aria-invalid")).toBe("true");
    const message = editor().nextElementSibling;
    expect(message?.textContent).toBeTruthy();
    await user.clear(editor());
    await user.paste('{"fixed": true}');
    expect(last().extra).toEqual({ fixed: true });
    expect(editor().getAttribute("aria-invalid")).toBe("false");
    expect(editor().nextElementSibling).toBeNull();
  });

  test("emits null when the text is emptied", async () => {
    const { user, last } = renderGallery();
    await user.clear(editor());
    expect(last()).toEqual({ ...INITIAL, extra: null });
  });

  test("is the editor for a list of non-text items", () => {
    renderField({ type: "array", items: { type: "integer" } }, [1, 2]);
    expect((control() as HTMLTextAreaElement).value).toBe(JSON.stringify([1, 2], null, 2));
    expect(document.querySelector("[data-widget=json]")).toBeTruthy();
  });
});

describe("path widgets", () => {
  test("show a placeholder suited to a folder or a file", () => {
    renderGallery();
    expect(formField("folder").getByPlaceholderText("C:/path/to/folder")).toBeTruthy();
    expect(formField("file").getByPlaceholderText("C:/path/to/file")).toBeTruthy();
  });

  test("let the path be typed", async () => {
    const { user, last } = renderGallery();
    await user.type(formField("folder").getByPlaceholderText("C:/path/to/folder"), "C:/code");
    expect(last()).toEqual({ ...INITIAL, folder: "C:/code" });
  });

  test("fill in the folder chosen in the browser and close it", async () => {
    const tree = {
      "C:/Users/me": folderListing("C:/Users/me", "C:/Users", [
        folderEntry("code", "C:/Users/me/code"),
      ]),
      "C:/Users/me/code": folderListing("C:/Users/me/code", "C:/Users/me", []),
    };
    const api = mockApi().action(
      BROWSE,
      browseTree(tree, folderListing(null, null, [folderEntry("C:/", "C:/Users/me")])),
    );
    const { user, last } = renderGallery({ folder: "C:/Users/me" });
    await user.click(formField("folder").getByRole("button", { name: "Browse" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(await within(dialog).findByRole("button", { name: /code/ }));
    await user.click(within(dialog).getByRole("button", { name: "Use this folder" }));
    expect(last()).toEqual({ ...INITIAL, folder: "C:/Users/me/code" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.sent(BROWSE)).toEqual([{ path: "C:/Users/me" }, { path: "C:/Users/me/code" }]);
  });

  test("start the browser from the machine root when no path is set", async () => {
    const api = mockApi().action(BROWSE, folderListing(null, null, [folderEntry("C:", "C:/")]));
    const { user } = renderGallery();
    await user.click(formField("file").getByRole("button", { name: "Browse" }));
    await within(await screen.findByRole("dialog")).findByRole("button", { name: /C:/ });
    expect(api.sent(BROWSE)).toEqual([{ path: null }]);
  });
});

describe("path list widgets", () => {
  const folders: JsonSchema = {
    type: "array",
    items: { type: "string" },
    "x-ui": { widget: "path-dir" },
  };

  test("shows one path field per entry and adds an empty one", async () => {
    const { user, last } = renderField(folders, ["C:/a", "C:/b"]);
    expect(document.querySelector("[data-widget=path-dir-list]")).toBeTruthy();
    expect(screen.getAllByPlaceholderText("C:/path/to/folder")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Add folder" }));
    expect(last()).toEqual({ subject: ["C:/a", "C:/b", ""] });
  });

  test("edits one path without touching the others", async () => {
    const { user, last } = renderField(folders, ["C:/a", "C:/b"]);
    await user.type(screen.getByDisplayValue("C:/b"), "2");
    expect(last()).toEqual({ subject: ["C:/a", "C:/b2"] });
  });

  test("removes a path", async () => {
    const { user, last } = renderField(folders, ["C:/a", "C:/b"]);
    await user.click(screen.getAllByRole("button", { name: "Remove path" })[0] as HTMLElement);
    expect(last()).toEqual({ subject: ["C:/b"] });
  });

  test("fills a row from the folder browser", async () => {
    mockApi().action(
      BROWSE,
      browseTree(
        { "C:/b": folderListing("C:/b", "C:/", [folderEntry("deep", "C:/b/deep")]) },
        folderListing(null, null, []),
      ),
    );
    const { user, last } = renderField(folders, ["C:/a", "C:/b"]);
    await user.click(screen.getAllByRole("button", { name: "Browse" })[1] as HTMLElement);
    const dialog = await screen.findByRole("dialog");
    await user.click(await within(dialog).findByRole("button", { name: /deep/ }));
    await user.click(within(dialog).getByRole("button", { name: "Use this folder" }));
    expect(last()).toEqual({ subject: ["C:/a", "C:/b/deep"] });
  });

  test("use file pickers for a list of files", () => {
    renderField({ ...folders, "x-ui": { widget: "path-file" } }, ["C:/a.txt"]);
    expect(document.querySelector("[data-widget=path-file-list]")).toBeTruthy();
    expect(screen.getByPlaceholderText("C:/path/to/file")).toBeTruthy();
  });

  test("start empty with only the add button", () => {
    renderField(folders);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Add folder" })).toBeTruthy();
  });
});

describe("inferWidget", () => {
  const cases: [string, JsonSchema, string][] = [
    [
      "an explicit widget over any inference",
      { type: "string", "x-ui": { widget: "textarea" } },
      "textarea",
    ],
    ["a plain string", { type: "string" }, "text"],
    ["a boolean", { type: "boolean" }, "toggle"],
    ["an integer", { type: "integer" }, "number"],
    ["a number", { type: "number" }, "number"],
    ["an enumeration", { type: "string", enum: ["a", "b", "c", "d"] }, "select"],
    ["a short enumeration without descriptions", { type: "string", enum: ["a", "b"] }, "select"],
    [
      "a short enumeration with descriptions",
      { type: "string", enum: ["a", "b"], "x-ui": { enum_descriptions: { a: "A" } } },
      "radio-cards",
    ],
    [
      "a long enumeration with descriptions",
      { type: "string", enum: ["a", "b", "c", "d"], "x-ui": { enum_descriptions: { a: "A" } } },
      "select",
    ],
    [
      "a list of enumerated items",
      { type: "array", items: { type: "string", enum: ["a"] } },
      "multiselect",
    ],
    ["a list of strings", { type: "array", items: { type: "string" } }, "tags"],
    ["a list of numbers", { type: "array", items: { type: "number" } }, "json"],
    ["a list with no item schema", { type: "array" }, "json"],
    ["an object without properties", { type: "object" }, "json"],
    [
      "an object with properties",
      { type: "object", properties: { a: { type: "string" } } },
      "text",
    ],
    ["the first of several types", { type: ["string", "integer"] }, "text"],
    [
      "a list of folders",
      { type: "array", items: { type: "string" }, "x-ui": { widget: "path-dir" } },
      "path-dir-list",
    ],
    [
      "a list of files",
      { type: "array", items: { type: "string" }, "x-ui": { widget: "path-file" } },
      "path-file-list",
    ],
    ["a single folder", { type: "string", "x-ui": { widget: "path-dir" } }, "path-dir"],
  ];
  for (const [name, schema, widget] of cases) {
    test(`picks ${widget} for ${name}`, () => {
      expect(inferWidget(onlyField(schema), {})).toBe(widget);
    });
  }
});
