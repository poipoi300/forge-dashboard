// JSON Schema (as pydantic emits it) plus `x-ui` hints: resolution, unions,
// defaults, and field ordering. Everything the renderers need to build a form
// or a view from a schema alone.

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

export interface UiHints {
  widget?: string;
  group?: string;
  order?: number;
  advanced?: boolean;
  hidden?: boolean;
  unit?: string;
  placeholder?: string;
  help?: string;
  format?: string;
  visible_if?: { field: string; equals: unknown };
  enum_labels?: Record<string, string>;
  enum_descriptions?: Record<string, string>;
  columns?: string[];
  bind?: "project";
  tones?: Record<string, Tone>;
  min?: number;
  max?: number;
  step?: number;
  short_label?: string;
  hide_zero?: boolean;
  slot?: string;
}

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  prefixItems?: JsonSchema[];
  enum?: unknown[];
  const?: unknown;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  default?: unknown;
  format?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  pattern?: string;
  additionalProperties?: boolean | JsonSchema;
  discriminator?: { propertyName: string; mapping?: Record<string, string> };
  "x-ui"?: UiHints;
}

export type Kind =
  | "object"
  | "array"
  | "string"
  | "number"
  | "integer"
  | "boolean"
  | "enum"
  | "union"
  | "map"
  | "any";

/** A schema with references followed, wrappers merged, and nullability made explicit. */
export interface Resolved {
  schema: JsonSchema;
  nullable: boolean;
}

const WRAPPER_KEYS: (keyof JsonSchema)[] = ["title", "description", "default", "x-ui"];

export function deref(schema: JsonSchema, root: JsonSchema): JsonSchema {
  let current = schema;
  const seen = new Set<string>();
  while (current.$ref) {
    const ref = current.$ref;
    if (seen.has(ref)) throw new Error(`circular $ref ${ref}`);
    seen.add(ref);
    const name = ref.replace(/^#\/(\$defs|definitions|components\/schemas)\//, "");
    const target = root.$defs?.[name];
    if (!target) throw new Error(`unresolved $ref ${ref}`);
    const { $ref: _ref, ...siblings } = current;
    const merged: JsonSchema = { ...target, ...siblings };
    if (target["x-ui"] || siblings["x-ui"])
      merged["x-ui"] = { ...target["x-ui"], ...siblings["x-ui"] };
    current = merged;
  }
  return current;
}

function mergeWrapper(inner: JsonSchema, wrapper: JsonSchema): JsonSchema {
  const merged: JsonSchema = { ...inner };
  for (const key of WRAPPER_KEYS) {
    if (wrapper[key] !== undefined) (merged as Record<string, unknown>)[key] = wrapper[key];
  }
  const innerUi = inner["x-ui"];
  const outerUi = wrapper["x-ui"];
  if (innerUi || outerUi) merged["x-ui"] = { ...innerUi, ...outerUi };
  return merged;
}

const isNull = (schema: JsonSchema): boolean => schema.type === "null";

export function resolve(schema: JsonSchema, root: JsonSchema): Resolved {
  let current = deref(schema, root);
  let nullable = false;
  if (current.allOf && current.allOf.length === 1 && current.allOf[0]) {
    current = mergeWrapper(deref(current.allOf[0], root), current);
  }
  const options = current.anyOf ?? current.oneOf;
  if (options && !current.discriminator) {
    const real = options.filter((option) => !isNull(option));
    nullable = real.length < options.length;
    if (real.length === 1 && real[0]) {
      current = mergeWrapper(resolve(real[0], root).schema, current);
      delete current.anyOf;
      delete current.oneOf;
    } else if (nullable) {
      current = { ...current, anyOf: real };
      delete current.oneOf;
    }
  }
  if (Array.isArray(current.type) && current.type.includes("null")) {
    nullable = true;
    const rest = current.type.filter((type) => type !== "null");
    current = { ...current, type: rest.length === 1 ? rest[0] : rest };
  }
  return { schema: current, nullable };
}

export interface Variant {
  tag: string;
  label: string;
  schema: JsonSchema;
}

/** The variants of a discriminated union, tagged by the discriminator's constant value. */
export function variants(
  schema: JsonSchema,
  root: JsonSchema,
): { property: string; variants: Variant[] } | null {
  const options = schema.oneOf ?? schema.anyOf;
  if (!options || options.length < 2) return null;
  const resolved = options.map((option) => deref(option, root));
  const property =
    schema.discriminator?.propertyName ??
    Object.keys(resolved[0]?.properties ?? {}).find((key) =>
      resolved.every((option) => {
        const field = option.properties?.[key];
        return field !== undefined && (field.const !== undefined || field.enum?.length === 1);
      }),
    );
  if (!property) return null;
  const list: Variant[] = [];
  for (const option of resolved) {
    const field = option.properties?.[property];
    const tag = field?.const ?? field?.enum?.[0] ?? field?.default;
    if (typeof tag !== "string") return null;
    list.push({
      tag,
      label: option.title ? humanize(option.title) : humanize(tag),
      schema: option,
    });
  }
  return { property, variants: list };
}

export function kindOf(schema: JsonSchema, root: JsonSchema): Kind {
  if (variants(schema, root)) return "union";
  if (schema.enum) return "enum";
  if (schema.const !== undefined) return "enum";
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (type === "object" || schema.properties) {
    if (!schema.properties && schema.additionalProperties !== false) return "map";
    return "object";
  }
  if (type === "array") return "array";
  if (type === "string") return "string";
  if (type === "integer") return "integer";
  if (type === "number") return "number";
  if (type === "boolean") return "boolean";
  return "any";
}

export function hints(schema: JsonSchema): UiHints {
  return schema["x-ui"] ?? {};
}

export function humanize(name: string): string {
  const spaced = name
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase();
}

/** pydantic's automatic title for a field name: "project_id" becomes "Project Id". */
function automaticTitle(name: string): string {
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** A field's label: an explicit title, else the sentence-cased name; dense surfaces prefer `short_label`. */
export function labelOf(name: string, schema: JsonSchema, dense = false): string {
  const ui = hints(schema);
  // An explicit empty short label leaves the value unlabelled on dense surfaces.
  if (dense && ui.short_label !== undefined) return ui.short_label;
  if (schema.title && schema.title !== automaticTitle(name)) return schema.title;
  return humanize(name);
}

export interface Field {
  name: string;
  schema: JsonSchema;
  nullable: boolean;
  required: boolean;
  ui: UiHints;
  index: number;
}

/** An object's fields in display order: by group (first appearance), then `order`, then declaration. */
export function fields(schema: JsonSchema, root: JsonSchema): Field[] {
  const required = new Set(schema.required ?? []);
  const list = Object.entries(schema.properties ?? {}).map(([name, raw], index) => {
    const { schema: resolved, nullable } = resolve(raw, root);
    return {
      name,
      schema: resolved,
      nullable,
      required: required.has(name),
      ui: hints(resolved),
      index,
    };
  });
  const groupRank = new Map<string, number>();
  for (const field of list) {
    const group = field.ui.group ?? "";
    if (!groupRank.has(group)) groupRank.set(group, groupRank.size);
  }
  return list.sort((a, b) => {
    const groupDelta =
      (groupRank.get(a.ui.group ?? "") ?? 0) - (groupRank.get(b.ui.group ?? "") ?? 0);
    if (groupDelta !== 0) return groupDelta;
    const orderDelta = (a.ui.order ?? 1000) - (b.ui.order ?? 1000);
    return orderDelta !== 0 ? orderDelta : a.index - b.index;
  });
}

/** The value a fresh form starts from: the schema default, else an empty value of the right shape. */
export function defaultValue(raw: JsonSchema, root: JsonSchema): unknown {
  const { schema, nullable } = resolve(raw, root);
  if (schema.default !== undefined) return structuredClone(schema.default);
  if (nullable) return null;
  const kind = kindOf(schema, root);
  if (kind === "union") {
    const union = variants(schema, root);
    const first = union?.variants[0];
    return first ? defaultValue(first.schema, root) : null;
  }
  if (kind === "object") {
    const value: Record<string, unknown> = {};
    for (const field of fields(schema, root)) {
      if (
        field.required ||
        field.schema.default !== undefined ||
        field.schema.const !== undefined
      ) {
        value[field.name] = field.schema.const ?? defaultValue(field.schema, root);
      }
    }
    return value;
  }
  if (kind === "array") return [];
  if (kind === "map") return {};
  if (kind === "boolean") return false;
  if (kind === "enum") return schema.const ?? schema.enum?.[0] ?? null;
  if (kind === "integer" || kind === "number") return schema.minimum ?? 0;
  if (kind === "string") return "";
  return null;
}

/** Whether a field passes its `visible_if` condition against the sibling values. */
export function isVisible(field: Field, siblings: Record<string, unknown>): boolean {
  const condition = field.ui.visible_if;
  if (!condition) return true;
  return siblings[condition.field] === condition.equals;
}

/** Fields the user fills in (not hidden, not bound to page context). */
export function inputFields(schema: JsonSchema, root: JsonSchema): Field[] {
  return fields(schema, root).filter((field) => !field.ui.hidden && !field.ui.bind);
}

export interface PageContext {
  projectId?: string;
}

/** Fill every `bind`-hinted field from the page context. */
export function bindContext(
  schema: JsonSchema,
  root: JsonSchema,
  value: unknown,
  context: PageContext,
): unknown {
  if (!value || typeof value !== "object") return value;
  const result: Record<string, unknown> = { ...(value as Record<string, unknown>) };
  for (const field of fields(schema, root)) {
    if (field.ui.bind === "project" && context.projectId !== undefined)
      result[field.name] = context.projectId;
  }
  return result;
}

export function toneFor(schema: JsonSchema, value: unknown): Tone | undefined {
  const tones = hints(schema).tones;
  if (!tones) return undefined;
  return tones[String(value)];
}

export function enumLabel(schema: JsonSchema, value: unknown): string {
  const labels = hints(schema).enum_labels;
  const key = String(value);
  return labels?.[key] ?? key;
}
