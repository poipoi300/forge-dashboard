// Running any service action from its descriptor. One host owns the dialog
// (input form from `input_schema`, confirmation for `confirm`, typed
// confirmation for `danger`), so buttons, menus, and the command palette all
// trigger actions the same way and a dialog outlives the menu that opened it.
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { toast } from "sonner";
import { useLocation } from "wouter";
import { create } from "zustand";
import { ApiError, callDynamic } from "@/api/client";
import {
  type ActionDescriptor,
  asSchema,
  useActionDescriptor,
  useDescriptors,
} from "@/api/descriptors";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SchemaForm } from "./SchemaForm";
import { bindContext, defaultValue, inputFields, type PageContext } from "./schema";

type Variant = "default" | "outline" | "ghost" | "secondary" | "destructive" | "link";
type Size = "default" | "xs" | "sm" | "lg" | "icon" | "icon-xs" | "icon-sm";

export interface ActionRequest {
  service: string;
  action: string;
  context?: PageContext;
  /** Values set by the surrounding UI; the dialog asks only for the rest. */
  input?: Record<string, unknown>;
  /** Starting values for fields the dialog still lets the user edit (current settings, say). */
  defaults?: Record<string, unknown>;
  /** Always open the dialog, even when every field has a value. */
  prompt?: boolean;
  /** The text a danger action asks the user to type; defaults to the project id or "confirm". */
  confirmText?: string;
  onDone?: (output: unknown) => void;
}

interface Pending {
  id: number;
  request: ActionRequest;
  descriptor: ActionDescriptor;
}

interface ActionStore {
  pending: Pending | null;
  busy: Record<string, number>;
  open: (pending: Omit<Pending, "id">) => void;
  close: () => void;
  mark: (key: string, delta: number) => void;
}

const useActionStore = create<ActionStore>((set) => ({
  pending: null,
  busy: {},
  open: (pending) =>
    set((state) => ({ pending: { ...pending, id: (state.pending?.id ?? 0) + 1 } })),
  close: () => set({ pending: null }),
  mark: (key, delta) =>
    set((state) => ({ busy: { ...state.busy, [key]: (state.busy[key] ?? 0) + delta } })),
}));

function busyKey(request: ActionRequest): string {
  return `${request.service}.${request.action}.${request.context?.projectId ?? ""}`;
}

function isJobRef(value: unknown): value is { job_id: string; title: string } {
  return typeof value === "object" && value !== null && "job_id" in value && "title" in value;
}

function needsDialog(request: ActionRequest, descriptor: ActionDescriptor): boolean {
  const schema = asSchema(descriptor.input_schema);
  const given = request.input ?? {};
  const askable = inputFields(schema, schema).filter((field) => !(field.name in given));
  return Boolean(request.prompt || askable.length > 0 || descriptor.confirm || descriptor.danger);
}

function initialInput(
  request: ActionRequest,
  descriptor: ActionDescriptor,
): Record<string, unknown> {
  const schema = asSchema(descriptor.input_schema);
  return {
    ...(defaultValue(schema, schema) as Record<string, unknown>),
    ...request.defaults,
    ...request.input,
  };
}

async function execute(
  request: ActionRequest,
  descriptor: ActionDescriptor,
  payload: unknown,
  queryClient: QueryClient,
  navigate: (path: string) => void,
): Promise<void> {
  const schema = asSchema(descriptor.input_schema);
  const key = busyKey(request);
  const store = useActionStore.getState();
  store.mark(key, 1);
  try {
    const output = await callDynamic(
      request.service,
      request.action,
      bindContext(schema, schema, payload, request.context ?? {}),
    );
    if (descriptor.long_running && isJobRef(output)) {
      const jobId = output.job_id;
      toast(`Started: ${output.title}`, {
        action: { label: "View job", onClick: () => navigate(`/jobs/${jobId}`) },
      });
    } else if (
      output &&
      typeof output === "object" &&
      "message" in output &&
      typeof output.message === "string"
    ) {
      toast(output.message);
    } else if (!descriptor.query) {
      toast(`${descriptor.title}: done`);
    }
    await queryClient.invalidateQueries();
    request.onDone?.(output);
  } finally {
    useActionStore.getState().mark(key, -1);
  }
}

/** Start an action: run it at once when it needs nothing more, else open the shared dialog. */
export function useActionRunner(): (request: ActionRequest) => void {
  const descriptors = useDescriptors();
  const queryClient = useQueryClient();
  const [, navigate] = useLocation();
  const open = useActionStore((store) => store.open);
  return (request) => {
    const descriptor = descriptors
      ?.get(request.service)
      ?.actions.find((item) => item.id === request.action);
    if (!descriptor) {
      toast.error(`Unknown action ${request.service}.${request.action}`);
      return;
    }
    if (needsDialog(request, descriptor)) {
      open({ request, descriptor });
      return;
    }
    execute(request, descriptor, initialInput(request, descriptor), queryClient, navigate).catch(
      (failure: unknown) => {
        const message = failure instanceof ApiError ? failure.detail : String(failure);
        toast.error(`${descriptor.title} failed`, { description: message });
      },
    );
  };
}

function ActionDialog({ pending }: { pending: Pending }) {
  const { request, descriptor } = pending;
  const close = useActionStore((store) => store.close);
  const queryClient = useQueryClient();
  const [, navigate] = useLocation();
  const schema = asSchema(descriptor.input_schema);
  const given = request.input ?? {};
  const askable = inputFields(schema, schema).filter((field) => !(field.name in given));
  const [value, setValue] = useState<unknown>(() => initialInput(request, descriptor));
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expected = request.confirmText ?? request.context?.projectId ?? "confirm";
  const confirmBlocked = descriptor.danger && typed !== expected;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await execute(request, descriptor, value, queryClient, navigate);
      close();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : String(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!confirmBlocked) void submit();
        }}
      >
        <DialogHeader>
          <DialogTitle>{descriptor.title}</DialogTitle>
          {descriptor.description ? (
            <DialogDescription>{descriptor.description}</DialogDescription>
          ) : null}
        </DialogHeader>
        {askable.length ? (
          <SchemaForm
            schema={schema}
            root={schema}
            value={value}
            onChange={setValue}
            omit={Object.keys(given)}
          />
        ) : null}
        {descriptor.danger ? (
          <label className="flex flex-col gap-1.5 text-sm">
            <span>
              Type <code className="rounded-sm bg-hover px-1 font-mono text-xs">{expected}</code> to
              confirm.
            </span>
            <Input value={typed} onChange={(event) => setTyped(event.target.value)} autoFocus />
          </label>
        ) : null}
        {error ? (
          <p
            role="alert"
            className="rounded-md border border-signal/40 bg-signal-wash px-3 py-2 text-sm text-signal-ink"
          >
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant={descriptor.danger ? "destructive" : "default"}
            disabled={busy || confirmBlocked}
          >
            {busy ? <Loader2 className="animate-spin" /> : null}
            {descriptor.title}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

/** Mounted once at the app root: the dialog for whichever action is waiting on input or confirmation. */
export function ActionDialogHost() {
  const pending = useActionStore((store) => store.pending);
  const close = useActionStore((store) => store.close);
  return (
    <Dialog open={pending !== null} onOpenChange={(open) => !open && close()}>
      {pending ? <ActionDialog key={pending.id} pending={pending} /> : null}
    </Dialog>
  );
}

export interface ActionButtonProps extends ActionRequest {
  label?: ReactNode;
  icon?: ReactNode;
  variant?: Variant;
  size?: Size;
  className?: string;
  title?: string;
  disabled?: boolean;
}

export function ActionButton({
  label,
  icon,
  variant = "outline",
  size = "sm",
  className,
  title,
  disabled,
  ...request
}: ActionButtonProps) {
  const descriptor = useActionDescriptor(request.service, request.action);
  const run = useActionRunner();
  const busy = useActionStore((store) => (store.busy[busyKey(request)] ?? 0) > 0);
  return (
    <Button
      variant={variant}
      size={size}
      className={className}
      title={title ?? descriptor?.description ?? undefined}
      disabled={!descriptor || disabled || busy}
      onClick={(event) => {
        // The click is the action: it must not also select the row or card that holds the button.
        event.stopPropagation();
        run(request);
      }}
      data-action={`${request.service}.${request.action}`}
    >
      {busy ? <Loader2 className="animate-spin" /> : icon}
      {label ?? descriptor?.title ?? request.action}
    </Button>
  );
}
