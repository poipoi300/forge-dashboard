// Read-only rendering of any value from its schema and `x-ui` format.
import { Check, Minus } from "lucide-react";
import type { ReactNode } from "react";
import { Meter, PathText, RelativeTime } from "@/components/data";
import { Markdown } from "@/components/Markdown";
import { Chip, StatusMark, TONE_TEXT } from "@/components/status";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { bytes, count, percent } from "@/lib/format";
import { duration } from "@/lib/time";
import { cn } from "@/lib/utils";
import {
  enumLabel,
  fields,
  hints,
  type JsonSchema,
  kindOf,
  labelOf,
  resolve,
  type Tone,
  toneFor,
  variants,
} from "./schema";

export interface ViewProps {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  /** Dense rendering for cards and table cells: inline objects, short labels. */
  dense?: boolean;
}

const EMPTY = <span className="text-ink-3">—</span>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function Badge({ schema, value }: { schema: JsonSchema; value: unknown }) {
  const tone: Tone = toneFor(schema, value) ?? "neutral";
  return (
    <StatusMark tone={tone} strong={tone === "bad"}>
      {enumLabel(schema, value)}
    </StatusMark>
  );
}

/** An object rendered as a badge from its `level` field, with the rest on hover. */
function ObjectBadge({
  schema,
  root,
  value,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: Record<string, unknown>;
}) {
  const all = fields(schema, root);
  const level = all.find((field) => field.name === "level");
  if (!level) return <InlineObject schema={schema} root={root} value={value} />;
  const rest = all.filter((field) => field.name !== "level" && !field.ui.hidden);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="-my-1 inline-flex rounded-sm py-1 text-left focus-visible:outline-2"
        >
          <Badge schema={level.schema} value={value.level} />
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom" align="start" className="max-w-sm">
        <div className="flex flex-col gap-1">
          {rest.map((field) => (
            <SchemaView
              key={field.name}
              schema={field.schema}
              root={root}
              value={value[field.name]}
            />
          ))}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

/** An object rendered as a progress meter from `done`/`total`, captioned by `caption`. */
function ObjectProgress({ value }: { value: Record<string, unknown> }) {
  const done = Number(value.done ?? 0);
  const total = Number(value.total ?? 0);
  const caption = typeof value.caption === "string" ? value.caption : `${done}/${total}`;
  const detail = typeof value.detail === "string" ? value.detail : "";
  const ratio = total ? done / total : 0;
  return (
    <div className="flex min-w-24 flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="figures text-sm font-semibold">{percent(ratio * 100)}</span>
        <span className="figures truncate text-2xs text-ink-3">{caption}</span>
      </div>
      <Meter
        value={ratio}
        tone={ratio >= 1 ? "good" : "info"}
        label={`Progress ${percent(ratio * 100)}`}
      />
      {detail ? <span className="truncate text-2xs text-ink-2">{detail}</span> : null}
    </div>
  );
}

/**
 * An object on one line: `columns` picks the fields, else every visible one.
 * Dense booleans read as flags: a true one shows its label alone, a false one is left out.
 */
function InlineObject({
  schema,
  root,
  value,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: Record<string, unknown>;
}) {
  const all = fields(schema, root);
  const columns = hints(schema).columns;
  const chosen = columns
    ? columns.flatMap((name) => all.filter((field) => field.name === name))
    : all.filter((field) => !field.ui.hidden);
  const shown = chosen.filter((field) => {
    const item = value[field.name];
    if (item == null || item === false) return false;
    if (item === 0 && field.ui.hide_zero) return false;
    return !(Array.isArray(item) && item.length === 0);
  });
  if (!shown.length) return EMPTY;
  return (
    <span className="inline-flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-0.5">
      {shown.map((field) =>
        value[field.name] === true ? (
          <span
            key={field.name}
            className={cn("text-2xs font-medium", TONE_TEXT[toneFor(field.schema, true) ?? "warn"])}
            title={field.ui.help}
          >
            {labelOf(field.name, field.schema, true)}
          </span>
        ) : (
          <span
            key={field.name}
            className="inline-flex min-w-0 items-baseline gap-1"
            title={field.ui.help}
          >
            {labelOf(field.name, field.schema, true) ? (
              <span className="text-2xs text-ink-3">{labelOf(field.name, field.schema, true)}</span>
            ) : null}
            <SchemaView schema={field.schema} root={root} value={value[field.name]} dense />
          </span>
        ),
      )}
    </span>
  );
}

function DefinitionList({
  schema,
  root,
  value,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: Record<string, unknown>;
}) {
  const shown = fields(schema, root).filter((field) => !field.ui.hidden);
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-1.5">
      {shown.map((field) => (
        <div key={field.name} className="contents">
          <dt className="text-xs text-ink-3">{labelOf(field.name, field.schema)}</dt>
          <dd className="min-w-0 text-sm">
            <SchemaView schema={field.schema} root={root} value={value[field.name]} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function SchemaTable({
  schema,
  root,
  rows,
  columns,
  empty = "Nothing here yet.",
  onRow,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  rows: unknown[];
  columns?: string[];
  empty?: ReactNode;
  onRow?: (row: Record<string, unknown>, index: number) => void;
}) {
  const all = fields(schema, root);
  const chosen = (
    columns ??
    hints(schema).columns ??
    all.filter((f) => !f.ui.hidden).map((f) => f.name)
  ).flatMap((name) => all.filter((field) => field.name === name));
  if (!rows.length) return <p className="py-3 text-sm text-ink-3">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-rule-strong text-left">
            {chosen.map((field) => (
              <th
                key={field.name}
                scope="col"
                className="whitespace-nowrap px-2 py-1.5 text-xs font-medium text-ink-3"
              >
                {labelOf(field.name, field.schema, true)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const record = isRecord(row) ? row : {};
            return (
              <tr
                // biome-ignore lint/suspicious/noArrayIndexKey: rows have no identity field in the generic case
                key={index}
                onClick={onRow ? () => onRow(record, index) : undefined}
                className={cn(
                  "border-b border-rule last:border-b-0",
                  onRow && "cursor-pointer hover:bg-hover",
                )}
              >
                {chosen.map((field) => (
                  <td key={field.name} className="max-w-[28rem] px-2 py-1.5 align-top">
                    <SchemaView
                      schema={field.schema}
                      root={root}
                      value={record[field.name]}
                      dense
                    />
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function SchemaView({ schema: raw, root, value, dense = false }: ViewProps) {
  const { schema } = resolve(raw, root);
  const ui = hints(schema);
  if (value === null || value === undefined) {
    return ui.format === "relative-time" ? <RelativeTime at={null} /> : EMPTY;
  }
  const kind = kindOf(schema, root);
  switch (ui.format) {
    case "relative-time":
      return typeof value === "number" ? <RelativeTime at={value} /> : EMPTY;
    case "duration":
      return (
        <span className="figures">
          {typeof value === "number" ? duration(value) : String(value)}
        </span>
      );
    case "bytes":
      return (
        <span className="figures">{typeof value === "number" ? bytes(value) : String(value)}</span>
      );
    case "percent":
      return (
        <span className="figures">
          {typeof value === "number" ? percent(value, value < 10 ? 1 : 0) : "—"}
        </span>
      );
    case "progress":
      if (isRecord(value)) return <ObjectProgress value={value} />;
      if (typeof value === "number") {
        const ratio = value > 1 ? value / 100 : value;
        return (
          <Meter value={ratio} label={`Progress ${percent(ratio * 100)}`} className="min-w-16" />
        );
      }
      return EMPTY;
    case "badge":
      if (isRecord(value)) return <ObjectBadge schema={schema} root={root} value={value} />;
      return <Badge schema={schema} value={value} />;
    case "code":
      return <code className="break-all font-mono text-xs text-ink-2">{String(value)}</code>;
    case "url": {
      const url = String(value);
      return (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="truncate text-info-ink underline-offset-2 hover:underline"
        >
          {url.replace(/^https?:\/\//, "").replace(/\/$/, "")}
        </a>
      );
    }
    case "path":
      return <PathText path={String(value)} />;
    case "chip": {
      // A marker someone set (a lifecycle status), not a measurement: neutral unless it asks for help.
      const tone = toneFor(schema, value);
      return (
        <Chip
          className={cn(
            tone === "bad" && "border-signal/40 text-signal-ink",
            tone === "warn" && "border-amber/50 text-amber-ink",
          )}
        >
          {enumLabel(schema, value)}
        </Chip>
      );
    }
    case "markdown":
      return dense ? (
        <span className="truncate">{String(value)}</span>
      ) : (
        <Markdown>{String(value)}</Markdown>
      );
  }
  if (kind === "boolean") {
    return value ? (
      <span className="inline-flex items-center gap-1 text-xs">
        <Check aria-hidden className="size-3.5 text-clear" />
        yes
      </span>
    ) : (
      <span className="inline-flex items-center gap-1 text-xs text-ink-3">
        <Minus aria-hidden className="size-3.5" />
        no
      </span>
    );
  }
  if (kind === "enum") return <span>{enumLabel(schema, value)}</span>;
  if (kind === "integer" || kind === "number") {
    const unit = ui.unit ? <span className="ml-0.5 text-2xs text-ink-3">{ui.unit}</span> : null;
    return (
      <span className="figures">
        {typeof value === "number" ? count(value) : String(value)}
        {unit}
      </span>
    );
  }
  if (kind === "array" && Array.isArray(value)) {
    const items = schema.items ? resolve(schema.items, root).schema : {};
    if (kindOf(items, root) === "object") {
      if (dense) {
        if (!value.length) return EMPTY;
        // One line per item from the list's `columns`, values only: a table column already names them.
        const itemFields = fields(items, root);
        const columns = (
          ui.columns ?? itemFields.filter((field) => !field.ui.hidden).map((field) => field.name)
        ).flatMap((name) => itemFields.filter((field) => field.name === name));
        return (
          <span className="flex flex-col gap-0.5">
            {value.map((item, index) => {
              const record = isRecord(item) ? item : {};
              return (
                <span
                  // biome-ignore lint/suspicious/noArrayIndexKey: generic list rendering, items carry no identity
                  key={index}
                  className="inline-flex min-w-0 items-baseline gap-2 whitespace-nowrap"
                >
                  {columns.map((column) => (
                    <SchemaView
                      key={column.name}
                      schema={column.schema}
                      root={root}
                      value={record[column.name]}
                      dense
                    />
                  ))}
                </span>
              );
            })}
          </span>
        );
      }
      return <SchemaTable schema={items} root={root} rows={value} columns={ui.columns} />;
    }
    if (!value.length) return EMPTY;
    return (
      <span className="inline-flex flex-wrap gap-1">
        {value.map((item, index) => (
          <span
            // biome-ignore lint/suspicious/noArrayIndexKey: scalar lists repeat values
            key={index}
            className="rounded-sm border border-rule px-1.5 text-2xs leading-[1.125rem] text-ink-2"
          >
            <SchemaView schema={items} root={root} value={item} dense />
          </span>
        ))}
      </span>
    );
  }
  if ((kind === "object" || kind === "union") && isRecord(value)) {
    // A union has no properties of its own: show the fields of the variant the value's tag names.
    const union = kind === "union" ? variants(schema, root) : null;
    const shown =
      union?.variants.find((variant) => variant.tag === value[union.property])?.schema ?? schema;
    return dense ? (
      <InlineObject schema={shown} root={root} value={value} />
    ) : (
      <DefinitionList schema={shown} root={root} value={value} />
    );
  }
  if (kind === "map" && isRecord(value)) {
    const entries = Object.entries(value);
    if (!entries.length) return EMPTY;
    return (
      <span className="inline-flex flex-col gap-0.5">
        {entries.map(([key, item]) => (
          <span key={key} className="text-xs">
            <span className="text-ink-3">{key}</span>{" "}
            <code className="font-mono">{JSON.stringify(item)}</code>
          </span>
        ))}
      </span>
    );
  }
  return <span className={cn(dense && "truncate")}>{String(value)}</span>;
}
