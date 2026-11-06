// Shared pieces for the schema renderer tests: the gallery service's real schemas,
// small schema builders, and a controlled form that records every value it emits.
import { type ReactNode, useState } from "react";
import type { Schemas } from "@/api/client";
import { asSchema } from "@/api/descriptors";
import { SchemaForm } from "@/schema/SchemaForm";
import { type Field, fields, type JsonSchema } from "@/schema/schema";
import services from "./fixtures/services.json";
import { MockApiError, type Rendered, renderWithProviders } from "./harness";

const gallery = services.find((descriptor) => descriptor.id === "gallery");
if (!gallery) throw new Error("test/fixtures/services.json has no gallery service");

/** Every widget kind, exactly as the backend publishes it. */
export const GALLERY_CONFIG: JsonSchema = asSchema(gallery.config_schema);
/** Every display format, exactly as the backend publishes it. */
export const GALLERY_STATE: JsonSchema = asSchema(gallery.state_schema);

/** An object schema from its properties. */
export function objectSchema(
  properties: Record<string, JsonSchema>,
  extra: Partial<JsonSchema> = {},
): JsonSchema {
  return { type: "object", properties, ...extra };
}

/** The single field of a one-property object schema, resolved the way the form sees it. */
export function onlyField(property: JsonSchema, required = false): Field {
  const schema = objectSchema({ subject: property }, required ? { required: ["subject"] } : {});
  const field = fields(schema, schema)[0];
  if (!field) throw new Error("schema produced no field");
  return field;
}

export interface FormRender extends Rendered {
  /** Every value the form emitted, oldest first. */
  changes: unknown[];
  /** The most recent emission. */
  last: () => Record<string, unknown>;
}

interface ControlledFormProps {
  schema: JsonSchema;
  root: JsonSchema;
  initial: unknown;
  omit?: string[];
  record: (value: unknown) => void;
}

function ControlledForm({ schema, root, initial, omit, record }: ControlledFormProps): ReactNode {
  const [value, setValue] = useState(initial);
  return (
    <SchemaForm
      schema={schema}
      root={root}
      value={value}
      omit={omit}
      onChange={(next) => {
        record(next);
        setValue(next);
      }}
    />
  );
}

/** Render a form whose state follows its own onChange, recording each emitted value. */
export function renderForm(
  schema: JsonSchema,
  initial: unknown,
  options: { root?: JsonSchema; omit?: string[]; path?: string } = {},
): FormRender {
  const changes: unknown[] = [];
  const rendered = renderWithProviders(
    <ControlledForm
      schema={schema}
      root={options.root ?? schema}
      initial={initial}
      omit={options.omit}
      record={(value) => changes.push(value)}
    />,
    { dialogHost: false, ...(options.path ? { path: options.path } : {}) },
  );
  return {
    ...rendered,
    changes,
    last: () => {
      const value = changes.at(-1);
      if (typeof value !== "object" || value === null) throw new Error("form emitted no object");
      return value as Record<string, unknown>;
    },
  };
}

export const BROWSE = "/api/services/discovery/actions/browse" as const;

export function folderEntry(name: string, path: string, isProject = false): Schemas["FolderEntry"] {
  return { name, path, is_project: isProject };
}

export function folderListing(
  path: string | null,
  parent: string | null,
  entries: Schemas["FolderEntry"][],
): Schemas["FolderListing"] {
  return { path, parent, entries };
}

/** A `discovery.browse` responder over an in-memory folder tree keyed by path (`null` is the machine root). */
export function browseTree(
  tree: Record<string, Schemas["FolderListing"]>,
  root: Schemas["FolderListing"],
): (input: Schemas["BrowseInput"]) => Schemas["FolderListing"] {
  return (input) => {
    if (input.path === null || input.path === undefined) return root;
    const listing = tree[input.path];
    if (!listing) throw new MockApiError(404, `no such folder: ${input.path}`);
    return listing;
  };
}
