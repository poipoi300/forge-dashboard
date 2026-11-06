// One project on the overview, laid out by the summary schema's slots:
// title, subtitle, meta, status, progress, signals, body, footer. A field the
// backend adds with a slot appears here with no edit to this file.
import { Star } from "lucide-react";
import { forwardRef } from "react";
import { Link } from "wouter";
import type { Schemas } from "@/api/client";
import { Sparkline } from "@/components/data";
import { Chip } from "@/components/status";
import { cn } from "@/lib/utils";
import { useActionRunner } from "@/schema/ActionButton";
import { SchemaView } from "@/schema/SchemaView";
import { type Field, fields, type JsonSchema, labelOf, resolve } from "@/schema/schema";
import { QuickActions } from "../project/QuickActions";
import { MetricValue } from "./MetricValue";
import type { SummarySchema } from "./summarySchema";

type ProjectSummary = Schemas["ProjectSummary"];

export function fieldValue(project: ProjectSummary, field: Field): unknown {
  return (project as unknown as Record<string, unknown>)[field.name];
}

function present(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}

/** Whether a signal would show anything: an object shows its `columns`, and empty, null, and false show nothing. */
export function hasContent(field: Field, value: unknown): boolean {
  if (!present(value)) return false;
  const columns = field.ui.columns;
  if (!columns || typeof value !== "object" || Array.isArray(value)) return true;
  const record = value as Record<string, unknown>;
  return columns.some((name) => present(record[name]) && record[name] !== false);
}

function Meta({ project, layout }: { project: ProjectSummary; layout: SummarySchema }) {
  const items = (layout.slots.get("meta") ?? []).flatMap((field) => {
    const value = fieldValue(project, field);
    if (field.ui.hidden || !present(value)) return [];
    // A project managed the normal way says nothing; only the exceptions get a chip.
    if (field.name === "role" && value === "managed") return [];
    const list = Array.isArray(value) ? value : [value];
    return list.map((item) => ({ key: `${field.name}:${String(item)}`, field, item }));
  });
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {items.map(({ key, field, item }) => (
        <Chip key={key} title={labelOf(field.name, field.schema)}>
          {field.ui.enum_labels?.[String(item)] ?? String(item)}
        </Chip>
      ))}
    </div>
  );
}

/** A flat series says nothing a sparkline could add, so it gets none. */
function varies(values: number[]): boolean {
  return values.length >= 2 && Math.min(...values) !== Math.max(...values);
}

/** A list field on the card: one compact line per item from the field's `columns`. */
function BodyList({
  field,
  root,
  items,
}: {
  field: Field;
  root: JsonSchema;
  items: Record<string, unknown>[];
}) {
  const itemSchema = field.schema.items ? resolve(field.schema.items, root).schema : {};
  const itemFields = fields(itemSchema, root);
  const columns = (
    field.ui.columns ?? itemFields.filter((f) => !f.ui.hidden).map((f) => f.name)
  ).flatMap((name) => itemFields.filter((item) => item.name === name));
  const isMetric =
    itemFields.some((f) => f.name === "display") && itemFields.some((f) => f.name === "value");
  return (
    <div className="flex flex-col">
      <span className="text-2xs text-ink-3">{labelOf(field.name, field.schema, true)}</span>
      {items.slice(0, 4).map((item, index) => (
        <div
          key={String(item.id ?? index)}
          className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 border-b border-rule py-0.5 last:border-b-0"
        >
          {isMetric ? (
            <>
              <span className="truncate text-xs">{String(item.label)}</span>
              <span className="flex items-center gap-2">
                {varies((item.history as number[] | undefined) ?? []) ? (
                  <Sparkline
                    values={(item.history as number[] | undefined) ?? []}
                    label={String(item.label)}
                    width={56}
                    height={14}
                  />
                ) : null}
                <MetricValue metric={item as Schemas["PinnedMetric"]} />
              </span>
            </>
          ) : (
            <>
              <span className="truncate text-xs">
                {columns[0] ? (
                  <SchemaView
                    schema={columns[0].schema}
                    root={root}
                    value={item[columns[0].name]}
                    dense
                  />
                ) : null}
              </span>
              <span className="flex min-w-0 items-baseline justify-end gap-2.5">
                {columns.slice(1).map((column) => (
                  <span key={column.name} className="min-w-0 truncate text-xs">
                    <SchemaView
                      schema={column.schema}
                      root={root}
                      value={item[column.name]}
                      dense
                    />
                  </span>
                ))}
              </span>
            </>
          )}
        </div>
      ))}
      {items.length > 4 ? (
        <span className="text-2xs text-ink-3">and {items.length - 4} more</span>
      ) : null}
    </div>
  );
}

export const ProjectCard = forwardRef<
  HTMLElement,
  { project: ProjectSummary; layout: SummarySchema; selected: boolean; onFocus: () => void }
>(function ProjectCard({ project, layout, selected, onFocus }, ref) {
  const run = useActionRunner();
  const { root } = layout;
  const slot = (name: string) => (layout.slots.get(name) ?? []).filter((field) => !field.ui.hidden);
  const subtitle = slot("subtitle")[0];
  const subtitleText = subtitle ? fieldValue(project, subtitle) : null;
  // The first status field leads beside the name; the rest form the status line under it.
  const [lead, ...statusLine] = slot("status");
  const failing = project.health.level === "failing";
  return (
    <article
      ref={ref}
      tabIndex={-1}
      onFocus={onFocus}
      aria-label={project.name}
      data-selected={selected || undefined}
      className={cn(
        "group/card relative flex min-w-0 flex-col gap-2.5 rounded-md border border-rule bg-surface p-3.5 compact:gap-1.5 compact:p-2.5",
        "outline-none data-[selected]:border-info data-[selected]:ring-1 data-[selected]:ring-info",
        failing && "border-l-[3px] border-l-signal",
      )}
    >
      <header className="flex flex-col gap-1">
        <div className="flex items-start gap-2">
          <button
            type="button"
            onClick={() =>
              run({
                service: "projects",
                action: "set-favorite",
                context: { projectId: project.id },
                input: { favorite: !project.favorite },
              })
            }
            aria-pressed={project.favorite}
            aria-label={
              project.favorite ? `Unpin ${project.name}` : `Pin ${project.name} to the top`
            }
            className={cn(
              "relative z-10 -my-0.5 -ml-1.5 flex size-6 shrink-0 items-center justify-center rounded-sm text-ink-3 hover:text-ink",
              project.favorite
                ? "text-amber"
                : "opacity-40 group-hover/card:opacity-100 focus-visible:opacity-100",
            )}
          >
            <Star className="size-3.5" fill={project.favorite ? "currentColor" : "none"} />
          </button>
          <span className="flex min-w-0 flex-1 items-baseline gap-2">
            <Link
              href={`/p/${project.id}`}
              className="min-w-0 truncate text-base font-semibold tracking-tight text-ink after:absolute after:inset-0 after:content-[''] hover:underline"
            >
              {project.name}
            </Link>
            <code className="shrink-0 font-mono text-2xs text-ink-3" title="Project id">
              {project.id}
            </code>
          </span>
          {lead ? (
            <span className="mt-0.5 shrink-0">
              <SchemaView
                schema={lead.schema}
                root={root}
                value={fieldValue(project, lead)}
                dense
              />
            </span>
          ) : null}
        </div>
        {typeof subtitleText === "string" ? (
          <p className="line-clamp-2 text-xs text-ink-2 compact:line-clamp-1" title={subtitleText}>
            {subtitleText}
          </p>
        ) : null}
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
          {statusLine.map((field) => (
            <span
              key={field.name}
              className="relative z-10 inline-flex min-w-0 items-baseline gap-1"
            >
              {field.ui.short_label && field.ui.format !== "badge" && field.ui.format !== "chip" ? (
                <span className="text-2xs text-ink-3">
                  {labelOf(field.name, field.schema, true)}
                </span>
              ) : null}
              <SchemaView
                schema={field.schema}
                root={root}
                value={fieldValue(project, field)}
                dense
              />
            </span>
          ))}
        </div>
      </header>

      <div className="compact:hidden">
        <Meta project={project} layout={layout} />
      </div>

      {slot("progress").map((field) => {
        const value = fieldValue(project, field);
        return present(value) ? (
          <div key={field.name} className="flex flex-col gap-0.5">
            <span className="text-2xs text-ink-3">{labelOf(field.name, field.schema, true)}</span>
            <SchemaView schema={field.schema} root={root} value={value} dense />
          </div>
        ) : null;
      })}

      <dl className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-x-2 gap-y-0.5 border-t border-rule pt-2 compact:pt-1">
        {slot("signals").flatMap((field) => {
          const value = fieldValue(project, field);
          if (!hasContent(field, value)) return [];
          return [
            <dt
              key={`${field.name}-label`}
              className="truncate text-2xs leading-[1.05rem] text-ink-3"
            >
              {labelOf(field.name, field.schema, true)}
            </dt>,
            <dd key={field.name} className="min-w-0 truncate text-xs">
              <SchemaView schema={field.schema} root={root} value={value} dense />
            </dd>,
          ];
        })}
      </dl>

      {slot("body").map((field) => {
        const value = fieldValue(project, field);
        if (!Array.isArray(value) || !value.length) return null;
        return (
          <BodyList
            key={field.name}
            field={field}
            root={root}
            items={value as Record<string, unknown>[]}
          />
        );
      })}

      {project.attention.length ? (
        <ul className="flex flex-col gap-0.5">
          {project.attention.slice(0, 2).map((item) => (
            <li
              key={`${item.source}:${item.message}`}
              className={cn(
                "truncate text-xs",
                item.level === "bad" ? "text-signal-ink" : "text-amber-ink",
              )}
            >
              {item.message}
            </li>
          ))}
        </ul>
      ) : null}

      <footer className="relative z-10 mt-auto flex flex-col gap-1.5 border-t border-rule pt-2 compact:pt-1">
        {/* Paths get a line of their own: their tail is what tells two checkouts apart. */}
        {slot("footer")
          .filter((field) => field.ui.format === "path")
          .map((field) => (
            <span key={field.name} className="flex min-w-0 pr-7">
              <SchemaView
                schema={field.schema}
                root={root}
                value={fieldValue(project, field)}
                dense
              />
            </span>
          ))}
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 text-2xs text-ink-3 compact:hidden">
          {slot("footer")
            .filter((field) => field.ui.format !== "path")
            .map((field) => (
              <span key={field.name} className="inline-flex min-w-0 items-baseline gap-1">
                {field.ui.short_label && typeof fieldValue(project, field) !== "object" ? (
                  <span>{labelOf(field.name, field.schema, true)}</span>
                ) : null}
                <SchemaView
                  schema={field.schema}
                  root={root}
                  value={fieldValue(project, field)}
                  dense
                />
              </span>
            ))}
        </div>
        <QuickActions project={project} />
      </footer>
    </article>
  );
});
