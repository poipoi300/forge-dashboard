// One component per `x-ui` widget. Each is a controlled input over one field's value.
import { Eye, EyeOff, FolderOpen, Plus, X } from "lucide-react";
import { type KeyboardEvent, useId, useState } from "react";
import { FolderPicker } from "@/components/FolderPicker";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { describeCron } from "./cron";
import { enumLabel, type Field, hints, type JsonSchema, resolve } from "./schema";

export interface WidgetProps {
  field: Field;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
  id: string;
}

const asString = (value: unknown): string =>
  value === null || value === undefined ? "" : String(value);

function nullableText(field: Field, text: string): unknown {
  return text === "" && field.nullable ? null : text;
}

export function TextWidget({ field, value, onChange, id }: WidgetProps) {
  return (
    <Input
      id={id}
      data-widget="text"
      value={asString(value)}
      placeholder={field.ui.placeholder}
      maxLength={field.schema.maxLength}
      onChange={(event) => onChange(nullableText(field, event.target.value))}
    />
  );
}

export function TextareaWidget({ field, value, onChange, id }: WidgetProps) {
  return (
    <Textarea
      id={id}
      data-widget="textarea"
      value={asString(value)}
      placeholder={field.ui.placeholder}
      rows={4}
      onChange={(event) => onChange(nullableText(field, event.target.value))}
    />
  );
}

export function NumberWidget({ field, value, onChange, id }: WidgetProps) {
  const integer = field.schema.type === "integer";
  return (
    <div className="flex items-center gap-2">
      <Input
        id={id}
        data-widget="number"
        type="number"
        inputMode={integer ? "numeric" : "decimal"}
        className="figures w-32"
        value={value === null || value === undefined ? "" : String(value)}
        min={field.schema.minimum ?? field.schema.exclusiveMinimum}
        max={field.schema.maximum ?? field.schema.exclusiveMaximum}
        step={field.ui.step ?? (integer ? 1 : "any")}
        placeholder={field.ui.placeholder}
        onChange={(event) => {
          const text = event.target.value;
          if (text === "") onChange(field.nullable ? null : 0);
          else onChange(integer ? Number.parseInt(text, 10) : Number.parseFloat(text));
        }}
      />
      {field.ui.unit ? <span className="text-xs text-ink-3">{field.ui.unit}</span> : null}
    </div>
  );
}

export function SliderWidget({ field, value, onChange, id }: WidgetProps) {
  const min = field.ui.min ?? field.schema.minimum ?? 0;
  const max = field.ui.max ?? field.schema.maximum ?? 100;
  const current = typeof value === "number" ? value : min;
  return (
    <div className="flex items-center gap-3" data-widget="slider">
      <Slider
        id={id}
        min={min}
        max={max}
        step={field.ui.step ?? 1}
        value={[current]}
        onValueChange={([next]) => onChange(next ?? min)}
        className="max-w-64"
      />
      <span className="figures w-8 text-right text-sm">{current}</span>
    </div>
  );
}

export function ToggleWidget({ value, onChange, id }: WidgetProps) {
  return (
    <Switch
      id={id}
      data-widget="toggle"
      checked={Boolean(value)}
      onCheckedChange={(checked) => onChange(checked)}
    />
  );
}

function enumValues(schema: JsonSchema): string[] {
  return (schema.enum ?? []).map(String);
}

export function SelectWidget({ field, value, onChange, id }: WidgetProps) {
  const options = enumValues(field.schema);
  const none = "__none__";
  return (
    <Select
      value={value === null || value === undefined ? none : String(value)}
      onValueChange={(next) => onChange(next === none ? null : next)}
    >
      <SelectTrigger id={id} data-widget="select" className="w-56">
        <SelectValue placeholder={field.ui.placeholder ?? "Choose…"} />
      </SelectTrigger>
      <SelectContent>
        {field.nullable ? <SelectItem value={none}>None</SelectItem> : null}
        {options.map((option) => (
          <SelectItem key={option} value={option}>
            {enumLabel(field.schema, option)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function RadioCardsWidget({ field, value, onChange, id }: WidgetProps) {
  const descriptions = field.ui.enum_descriptions ?? {};
  return (
    <RadioGroup
      id={id}
      data-widget="radio-cards"
      value={asString(value)}
      onValueChange={(next) => onChange(next)}
      className="grid gap-2 sm:grid-cols-2"
    >
      {enumValues(field.schema).map((option) => (
        <label
          key={option}
          htmlFor={`${id}-${option}`}
          className={cn(
            "flex cursor-pointer gap-2.5 rounded-md border border-rule p-2.5 hover:bg-hover",
            asString(value) === option && "border-ink bg-surface-sunk",
          )}
        >
          <RadioGroupItem id={`${id}-${option}`} value={option} className="mt-0.5" />
          <span className="flex flex-col gap-0.5">
            <span className="text-sm font-medium">{enumLabel(field.schema, option)}</span>
            {descriptions[option] ? (
              <span className="text-xs text-ink-3">{descriptions[option]}</span>
            ) : null}
          </span>
        </label>
      ))}
    </RadioGroup>
  );
}

export function MultiselectWidget({ field, root, value, onChange, id }: WidgetProps) {
  const items = field.schema.items ? resolve(field.schema.items, root).schema : {};
  const chosen = new Set(Array.isArray(value) ? value.map(String) : []);
  const toggle = (option: string, on: boolean) => {
    const next = new Set(chosen);
    if (on) next.add(option);
    else next.delete(option);
    onChange(enumValues(items).filter((item) => next.has(item)));
  };
  return (
    <div id={id} data-widget="multiselect" className="flex flex-wrap gap-x-4 gap-y-2">
      {enumValues(items).map((option) => (
        <label
          key={option}
          htmlFor={`${id}-${option}`}
          className="inline-flex cursor-pointer items-center gap-2 text-sm"
        >
          <Checkbox
            id={`${id}-${option}`}
            checked={chosen.has(option)}
            onCheckedChange={(checked) => toggle(option, checked === true)}
          />
          {enumLabel(items, option)}
        </label>
      ))}
    </div>
  );
}

export function TagsWidget({ field, value, onChange, id }: WidgetProps) {
  const tags = Array.isArray(value) ? value.map(String) : [];
  const [draft, setDraft] = useState("");
  const add = () => {
    const text = draft.trim();
    if (!text) return;
    onChange([...tags, text]);
    setDraft("");
  };
  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      add();
    } else if (event.key === "Backspace" && !draft && tags.length) {
      onChange(tags.slice(0, -1));
    }
  };
  return (
    <div
      data-widget="tags"
      className="flex min-h-9 flex-wrap items-center gap-1 rounded-md border border-input bg-surface px-1.5 py-1 focus-within:ring-2 focus-within:ring-ring/40"
    >
      {tags.map((tag, index) => (
        <span
          // biome-ignore lint/suspicious/noArrayIndexKey: tags may repeat, position is the identity
          key={index}
          className="inline-flex items-center gap-1 rounded-sm bg-hover px-1.5 py-0.5 font-mono text-xs"
        >
          {tag}
          <button
            type="button"
            aria-label={`Remove ${tag}`}
            className="text-ink-3 hover:text-ink"
            onClick={() => onChange(tags.filter((_, position) => position !== index))}
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        id={id}
        value={draft}
        placeholder={tags.length ? "" : (field.ui.placeholder ?? "Type and press Enter")}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKey}
        onBlur={add}
        className="min-w-24 flex-1 bg-transparent px-1 text-sm outline-none"
      />
    </div>
  );
}

export function PathWidget({ field, value, onChange, id }: WidgetProps) {
  const [open, setOpen] = useState(false);
  const directory = field.ui.widget !== "path-file";
  return (
    <div className="flex gap-2" data-widget={field.ui.widget ?? "path-dir"}>
      <Input
        id={id}
        className="font-mono text-xs"
        value={asString(value)}
        placeholder={field.ui.placeholder ?? (directory ? "C:/path/to/folder" : "C:/path/to/file")}
        onChange={(event) => onChange(nullableText(field, event.target.value))}
      />
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
        <FolderOpen />
        Browse
      </Button>
      <FolderPicker
        open={open}
        onOpenChange={setOpen}
        start={asString(value) || null}
        onPick={(path) => {
          onChange(path);
          setOpen(false);
        }}
      />
    </div>
  );
}

/** A list of paths, each with its own folder picker (`widget: path-dir` on a list of strings). */
export function PathListWidget({ field, root, value, onChange, id }: WidgetProps) {
  const paths = Array.isArray(value) ? value.map(String) : [];
  const itemField: Field = {
    ...field,
    schema: { type: "string", "x-ui": { widget: field.ui.widget } },
    nullable: false,
  };
  return (
    <div className="flex flex-col gap-2" data-widget={`${field.ui.widget}-list`}>
      {paths.map((path, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: list position is the identity while editing
        <div key={index} className="flex items-start gap-2">
          <div className="flex-1">
            <PathWidget
              field={itemField}
              root={root}
              id={`${id}-${index}`}
              value={path}
              onChange={(next) =>
                onChange(paths.map((item, position) => (position === index ? String(next) : item)))
              }
            />
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Remove path"
            onClick={() => onChange(paths.filter((_, position) => position !== index))}
          >
            <X />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        onClick={() => onChange([...paths, ""])}
      >
        <Plus />
        Add folder
      </Button>
    </div>
  );
}

export function ColorWidget({ value, onChange, id }: WidgetProps) {
  const color = asString(value) || "#000000";
  return (
    <div className="flex items-center gap-2" data-widget="color">
      <input
        id={id}
        type="color"
        value={color}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-10 cursor-pointer rounded-sm border border-input bg-surface"
      />
      <Input
        className="w-28 font-mono text-xs"
        value={color}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

const DURATION_UNITS = [
  { label: "seconds", seconds: 1 },
  { label: "minutes", seconds: 60 },
  { label: "hours", seconds: 3600 },
  { label: "days", seconds: 86400 },
];

export function DurationWidget({ field, value, onChange, id }: WidgetProps) {
  const total = typeof value === "number" ? value : 0;
  const natural = [...DURATION_UNITS]
    .reverse()
    .find((unit) => total >= unit.seconds && total % unit.seconds === 0);
  const [unit, setUnit] = useState(natural?.seconds ?? 1);
  return (
    <div className="flex items-center gap-2" data-widget="duration">
      <Input
        id={id}
        type="number"
        min={0}
        className="figures w-28"
        value={total ? String(total / unit) : field.nullable && value === null ? "" : "0"}
        onChange={(event) => {
          const amount = Number.parseFloat(event.target.value);
          onChange(Number.isFinite(amount) ? Math.round(amount * unit) : field.nullable ? null : 0);
        }}
      />
      <Select value={String(unit)} onValueChange={(next) => setUnit(Number(next))}>
        <SelectTrigger className="w-28" aria-label="Unit">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {DURATION_UNITS.map((option) => (
            <SelectItem key={option.seconds} value={String(option.seconds)}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function CronWidget({ field, value, onChange, id }: WidgetProps) {
  const expression = asString(value);
  const description = describeCron(expression);
  return (
    <div className="flex flex-col gap-1" data-widget="cron">
      <Input
        id={id}
        className="w-56 font-mono text-xs"
        value={expression}
        placeholder={field.ui.placeholder ?? "m h dom mon dow"}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={description === null}
      />
      <span className={cn("text-xs", description === null ? "text-signal-ink" : "text-ink-3")}>
        {description ?? "Five fields: minute, hour, day of month, month, day of week."}
      </span>
    </div>
  );
}

export function SecretWidget({ field, value, onChange, id }: WidgetProps) {
  const [shown, setShown] = useState(false);
  return (
    <div className="flex gap-2" data-widget="secret">
      <Input
        id={id}
        type={shown ? "text" : "password"}
        autoComplete="off"
        className="font-mono text-xs"
        value={asString(value)}
        placeholder={field.ui.placeholder}
        onChange={(event) => onChange(nullableText(field, event.target.value))}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={shown ? "Hide value" : "Show value"}
        onClick={() => setShown(!shown)}
      >
        {shown ? <EyeOff /> : <Eye />}
      </Button>
    </div>
  );
}

export function CodeWidget({ field, value, onChange, id }: WidgetProps) {
  return (
    <Textarea
      id={id}
      data-widget="code"
      spellCheck={false}
      rows={6}
      className="font-mono text-xs"
      value={asString(value)}
      placeholder={field.ui.placeholder}
      onChange={(event) => onChange(nullableText(field, event.target.value))}
    />
  );
}

export function JsonWidget({ value, onChange, id }: WidgetProps) {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-1" data-widget="json">
      <Textarea
        id={id}
        spellCheck={false}
        rows={6}
        className="font-mono text-xs"
        value={text}
        aria-invalid={error !== null}
        onChange={(event) => {
          setText(event.target.value);
          try {
            onChange(JSON.parse(event.target.value || "null"));
            setError(null);
          } catch (failure) {
            setError(failure instanceof Error ? failure.message : "Invalid JSON");
          }
        }}
      />
      {error ? <span className="text-xs text-signal-ink">{error}</span> : null}
    </div>
  );
}

const KNOWN_MODELS = ["haiku", "sonnet", "opus"];

export function ModelPickerWidget({ field, value, onChange, id }: WidgetProps) {
  const listId = useId();
  return (
    <div className="flex flex-col gap-1" data-widget="model-picker">
      <Input
        id={id}
        list={listId}
        className="w-56"
        value={asString(value)}
        placeholder={field.ui.placeholder ?? "CLI default"}
        onChange={(event) => onChange(nullableText(field, event.target.value))}
      />
      <datalist id={listId}>
        {KNOWN_MODELS.map((model) => (
          <option key={model} value={model} />
        ))}
      </datalist>
    </div>
  );
}

export const WIDGETS: Record<string, (props: WidgetProps) => React.ReactNode> = {
  text: TextWidget,
  textarea: TextareaWidget,
  number: NumberWidget,
  slider: SliderWidget,
  toggle: ToggleWidget,
  select: SelectWidget,
  "radio-cards": RadioCardsWidget,
  multiselect: MultiselectWidget,
  tags: TagsWidget,
  "path-dir": PathWidget,
  "path-file": PathWidget,
  color: ColorWidget,
  duration: DurationWidget,
  cron: CronWidget,
  secret: SecretWidget,
  code: CodeWidget,
  json: JsonWidget,
  "model-picker": ModelPickerWidget,
};

/** The widget a field gets when its schema names none. */
export function inferWidget(field: Field, root: JsonSchema): string {
  const explicit = hints(field.schema).widget;
  const schema = field.schema;
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (type === "array" && (explicit === "path-dir" || explicit === "path-file"))
    return `${explicit}-list`;
  if (explicit) return explicit;
  if (schema.enum) {
    return schema.enum.length <= 3 && field.ui.enum_descriptions ? "radio-cards" : "select";
  }
  if (type === "boolean") return "toggle";
  if (type === "integer" || type === "number") return "number";
  if (type === "array") {
    const items = schema.items ? resolve(schema.items, root).schema : {};
    if (items.enum) return "multiselect";
    if (items.type === "string") return "tags";
    return "json";
  }
  if (type === "object" && !schema.properties) return "json";
  return "text";
}
