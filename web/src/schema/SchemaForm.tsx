// A form for any object schema: fields grouped by `x-ui.group`, ordered, with
// advanced fields collapsed, `visible_if` honoured, discriminated unions as a
// type selector plus the chosen variant's sub-form, and lists of objects as
// editable rows. New model fields appear here with no frontend change.
import { ChevronDown, Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import {
  defaultValue,
  type Field,
  fields,
  isVisible,
  type JsonSchema,
  kindOf,
  labelOf,
  resolve,
  variants,
} from "./schema";
import { inferWidget, PathListWidget, WIDGETS } from "./widgets";

type Record_ = Record<string, unknown>;

export interface FormProps {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
  /** Fields not to render (bound from page context, or owned by the surrounding UI). */
  omit?: string[];
}

function asRecord(value: unknown): Record_ {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record_) : {};
}

function Help({ text }: { text?: string | undefined }) {
  return text ? <p className="text-xs text-ink-3">{text}</p> : null;
}

function UnionField({
  field,
  root,
  value,
  onChange,
}: {
  field: Field;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const union = variants(field.schema, root);
  if (!union) return null;
  const current = asRecord(value);
  const tag = String(current[union.property] ?? union.variants[0]?.tag ?? "");
  const variant = union.variants.find((item) => item.tag === tag) ?? union.variants[0];
  return (
    <div className="flex flex-col gap-3" data-widget="union">
      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        value={tag}
        onValueChange={(next) => {
          const chosen = union.variants.find((item) => item.tag === next);
          if (chosen)
            onChange({
              ...asRecord(defaultValue(chosen.schema, root)),
              [union.property]: chosen.tag,
            });
        }}
        className="self-start"
      >
        {union.variants.map((item) => (
          <ToggleGroupItem key={item.tag} value={item.tag} className="px-3">
            {item.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {variant ? (
        <ObjectFields
          schema={variant.schema}
          root={root}
          value={current}
          onChange={(next) => onChange({ ...asRecord(next), [union.property]: variant.tag })}
          omit={[union.property]}
          nested
        />
      ) : null}
    </div>
  );
}

function ObjectListField({
  field,
  root,
  value,
  onChange,
}: {
  field: Field;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const items = field.schema.items ? resolve(field.schema.items, root).schema : {};
  const rows = Array.isArray(value) ? value : [];
  return (
    <div className="flex flex-col gap-2" data-widget="object-list">
      {rows.map((row, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows are edited in place by position
        <div key={index} className="flex gap-2 rounded-md border border-rule bg-surface-sunk p-3">
          <div className="flex-1">
            <ObjectFields
              schema={items}
              root={root}
              value={row}
              nested
              onChange={(next) =>
                onChange(rows.map((item, position) => (position === index ? next : item)))
              }
            />
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Remove row"
            onClick={() => onChange(rows.filter((_, position) => position !== index))}
          >
            <Trash2 />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        onClick={() => onChange([...rows, defaultValue(items, root)])}
      >
        <Plus />
        Add
      </Button>
    </div>
  );
}

export function FieldInput({
  field,
  root,
  value,
  onChange,
}: {
  field: Field;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const id = useId();
  const kind = kindOf(field.schema, root);
  if (kind === "union")
    return <UnionField field={field} root={root} value={value} onChange={onChange} />;
  if (kind === "object") {
    return (
      <ObjectFields schema={field.schema} root={root} value={value} onChange={onChange} nested />
    );
  }
  if (kind === "array") {
    const items = field.schema.items ? resolve(field.schema.items, root).schema : {};
    if (kindOf(items, root) === "object") {
      return <ObjectListField field={field} root={root} value={value} onChange={onChange} />;
    }
  }
  const widget = inferWidget(field, root);
  if (widget === "path-dir-list" || widget === "path-file-list") {
    return <PathListWidget field={field} root={root} value={value} onChange={onChange} id={id} />;
  }
  const Widget = WIDGETS[widget] ?? WIDGETS.text;
  return Widget ? (
    <Widget field={field} root={root} value={value} onChange={onChange} id={id} />
  ) : null;
}

function FieldRow({
  field,
  root,
  value,
  onChange,
}: {
  field: Field;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const kind = kindOf(field.schema, root);
  const block = kind === "object" || kind === "union" || (kind === "array" && field.ui.columns);
  const label = labelOf(field.name, field.schema);
  const help = field.ui.help ?? field.schema.description;
  if (kind === "boolean") {
    return (
      <div className="flex items-start justify-between gap-6 py-1" data-field={field.name}>
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium">{label}</span>
          <Help text={help} />
        </div>
        <FieldInput field={field} root={root} value={value} onChange={onChange} />
      </div>
    );
  }
  return (
    <div className={cn("flex flex-col gap-1.5", block && "gap-2")} data-field={field.name}>
      <span className="text-sm font-medium">
        {label}
        {field.required && !field.nullable && field.schema.default === undefined ? (
          <span className="ml-1 text-signal-ink" title="Required">
            *
          </span>
        ) : null}
      </span>
      <FieldInput field={field} root={root} value={value} onChange={onChange} />
      <Help text={help} />
    </div>
  );
}

function ObjectFields({
  schema,
  root,
  value,
  onChange,
  omit = [],
  nested = false,
}: FormProps & { nested?: boolean }) {
  const record = asRecord(value);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const visible = fields(schema, root).filter(
    (field) =>
      !field.ui.hidden && !field.ui.bind && !omit.includes(field.name) && isVisible(field, record),
  );
  const regular = visible.filter((field) => !field.ui.advanced);
  const advanced = visible.filter((field) => field.ui.advanced);
  const groups = new Map<string, Field[]>();
  for (const field of regular) {
    const key = field.ui.group ?? "";
    groups.set(key, [...(groups.get(key) ?? []), field]);
  }
  const setField = (name: string, next: unknown) => onChange({ ...record, [name]: next });
  const renderFields = (list: Field[]) =>
    list.map((field) => (
      <FieldRow
        key={field.name}
        field={field}
        root={root}
        value={record[field.name]}
        onChange={(next) => setField(field.name, next)}
      />
    ));
  return (
    <div className={cn("flex flex-col", nested ? "gap-3" : "gap-6")}>
      {[...groups.entries()].map(([group, list]) =>
        group && !nested ? (
          <fieldset key={group} className="flex flex-col gap-4">
            <legend className="mb-3 w-full border-b border-rule pb-1.5 text-sm font-semibold">
              {group}
            </legend>
            {renderFields(list)}
          </fieldset>
        ) : (
          <div key={group || "_"} className="flex flex-col gap-4">
            {renderFields(list)}
          </div>
        ),
      )}
      {advanced.length ? (
        <div className="flex flex-col gap-4">
          <button
            type="button"
            className="inline-flex items-center gap-1 self-start text-xs font-medium text-ink-3 hover:text-ink"
            aria-expanded={showAdvanced}
            onClick={() => setShowAdvanced(!showAdvanced)}
          >
            <ChevronDown
              className={cn("size-3.5 transition-transform", showAdvanced && "rotate-180")}
            />
            Advanced
          </button>
          {showAdvanced ? renderFields(advanced) : null}
        </div>
      ) : null}
    </div>
  );
}

export function SchemaForm(props: FormProps) {
  return <ObjectFields {...props} />;
}
