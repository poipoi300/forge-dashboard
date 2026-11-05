// The ProjectSummary schema from the projects service's own description, so
// cards and table columns follow the backend model without a frontend edit.
import { useMemo } from "react";
import { asSchema, useDescriptor } from "@/api/descriptors";
import { type Field, fields, hints, type JsonSchema, resolve } from "@/schema/schema";

export interface SummarySchema {
  root: JsonSchema;
  summary: JsonSchema;
  fields: Field[];
  /** Fields per card slot, in display order. */
  slots: Map<string, Field[]>;
  /** The overview table's default columns, from `ProjectsState.projects`. */
  columns: string[];
}

export function buildSummarySchema(stateSchema: JsonSchema): SummarySchema {
  const root = stateSchema;
  const projects = stateSchema.properties?.projects;
  if (!projects?.items) throw new Error("projects state schema has no projects list");
  const summary = resolve(projects.items, root).schema;
  const all = fields(summary, root);
  const slots = new Map<string, Field[]>();
  for (const field of all) {
    const slot = field.ui.slot;
    if (!slot) continue;
    slots.set(slot, [...(slots.get(slot) ?? []), field]);
  }
  const columns =
    hints(resolve(projects, root).schema).columns ??
    all.filter((field) => !field.ui.hidden).map((f) => f.name);
  return { root, summary, fields: all, slots, columns };
}

export function useSummarySchema(): SummarySchema | undefined {
  const descriptor = useDescriptor("projects");
  return useMemo(
    () => (descriptor ? buildSummarySchema(asSchema(descriptor.state_schema)) : undefined),
    [descriptor],
  );
}
