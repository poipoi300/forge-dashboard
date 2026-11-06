// Shared test harness: a routing fetch mock that records what the UI sent, a
// provider stack matching src/main.tsx, and store resets between tests.

import { afterEach, beforeEach } from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { Toaster, toast } from "sonner";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ActionInput, ActionOutput, ActionPath, Schemas } from "@/api/client";
import { applyMessage, type ServiceStates, useStream } from "@/api/stream";
import { App } from "@/app/App";
import { usePreferences } from "@/app/preferences";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useClock } from "@/lib/time";
import { ActionDialogHost } from "@/schema/ActionButton";
import { NOW_MS, NOW_SECONDS } from "./constants";
import services from "./fixtures/services.json";

export { NOW_MS, NOW_SECONDS };

export class MockApiError {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    this.status = status;
    this.detail = detail;
  }
}

export interface RecordedCall {
  method: string;
  path: string;
  body: unknown;
}

type Responder<Input, Output> =
  | Output
  | ((input: Input, call: RecordedCall) => Output | Promise<Output>);

/** Routes `METHOD path` to canned JSON and records every request the UI makes. */
export class MockApi {
  readonly calls: RecordedCall[] = [];
  readonly unrouted: RecordedCall[] = [];
  private readonly routes = new Map<string, Responder<unknown, unknown>>();

  constructor() {
    this.route("GET", "/api/services", services);
  }

  route<Output>(
    method: "GET" | "POST" | "PUT",
    path: string,
    respond: Responder<unknown, Output>,
  ): this {
    this.routes.set(`${method} ${path}`, respond as Responder<unknown, unknown>);
    return this;
  }

  /** Answer a service action; input and output are typed from the generated OpenAPI paths. */
  action<P extends ActionPath>(path: P, respond: Responder<ActionInput<P>, ActionOutput<P>>): this {
    return this.route("POST", path, respond as Responder<unknown, unknown>);
  }

  config(service: string, config: Record<string, unknown>): this {
    return this.route("GET", `/api/services/${service}/config`, config);
  }

  /** Requests to one route, in order. */
  callsTo(method: string, path: string): RecordedCall[] {
    return this.calls.filter((call) => call.method === method && call.path === path);
  }

  /** The request bodies sent to one action, in order. */
  sent(path: ActionPath): unknown[] {
    return this.callsTo("POST", path).map((call) => call.body);
  }

  async handle(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
      "http://localhost",
    );
    const call: RecordedCall = {
      method: init?.method ?? "GET",
      path: url.pathname,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    this.calls.push(call);
    const respond = this.routes.get(`${call.method} ${call.path}`);
    if (respond === undefined) {
      this.unrouted.push(call);
      return json(404, { detail: `no mock for ${call.method} ${call.path}` });
    }
    try {
      const output = typeof respond === "function" ? await respond(call.body, call) : respond;
      return json(200, output);
    } catch (failure) {
      if (failure instanceof MockApiError) return json(failure.status, { detail: failure.detail });
      throw failure;
    }
  }
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const realFetch = globalThis.fetch;

/** Install a fresh MockApi as `fetch`; the harness restores the real one after each test. */
export function mockApi(): MockApi {
  const api = new MockApi();
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    api.handle(input, init)) as typeof fetch;
  return api;
}

export function newQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

/** Put states into the live stream store as the first hello would. */
export function seedStream(
  states: Partial<{ [K in keyof ServiceStates]: ServiceStates[K] }>,
  rev = 1,
): void {
  act(() =>
    applyMessage(
      {
        kind: "hello",
        states: Object.entries(states).map(([service, state]) => ({ service, rev, state })),
      },
      () => {},
    ),
  );
}

export function emitEvent(overrides: Partial<Schemas["EventEnvelope"]> & { type: string }): void {
  act(() =>
    applyMessage(
      {
        kind: "event",
        event: {
          seq: 1,
          ts: NOW_SECONDS,
          service: "jobs",
          project_id: null,
          title: overrides.type,
          severity: "info",
          data: {},
          persist: false,
          notify_kind: null,
          dedupe_key: null,
          ...overrides,
        },
      },
      () => {},
    ),
  );
}

export interface RenderOptions {
  path?: string;
  queryClient?: QueryClient;
  /** Mount the shared action dialog host; the full App mounts its own. Default true. */
  dialogHost?: boolean;
}

export interface Rendered {
  user: ReturnType<typeof userEvent.setup>;
  queryClient: QueryClient;
  navigate: (path: string) => void;
  history: string[];
  location: () => string;
}

function Providers({
  children,
  queryClient,
  hook,
  dialogHost,
}: {
  children: ReactNode;
  queryClient: QueryClient;
  hook: ReturnType<typeof memoryLocation>["hook"];
  dialogHost: boolean;
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <Router hook={hook}>
          {children}
          {dialogHost ? <ActionDialogHost /> : null}
          <Toaster />
        </Router>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

/** Render a component under the app's providers with an in-memory router. */
export function renderWithProviders(ui: ReactElement, options: RenderOptions = {}): Rendered {
  const queryClient = options.queryClient ?? newQueryClient();
  const memory = memoryLocation({ path: options.path ?? "/", record: true });
  const user = userEvent.setup();
  render(
    <Providers queryClient={queryClient} hook={memory.hook} dialogHost={options.dialogHost ?? true}>
      {ui}
    </Providers>,
  );
  return {
    user,
    queryClient,
    navigate: (path) => act(() => memory.navigate(path)),
    history: memory.history ?? [],
    location: () => (memory.history ?? []).at(-1) ?? "/",
  };
}

/** Mount the whole routed application (shell, pages, palette, dialog host) at a path. */
export function renderApp(path = "/", options: Omit<RenderOptions, "path"> = {}): Rendered {
  return renderWithProviders(<App />, { ...options, path, dialogHost: false });
}

/** Resolve pending promises and effects so queued fetches settle. */
export async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** The open dialog, once present. */
export async function findDialog(): Promise<HTMLElement> {
  return screen.findByRole("dialog");
}

export function inDialog(): ReturnType<typeof within> {
  return within(screen.getByRole("dialog"));
}

function resetStores(): void {
  useStream.setState({ connection: "live", connectedAt: null, states: {}, events: [] });
  localStorage.clear();
  usePreferences.setState({
    theme: "system",
    density: "comfortable",
    overview: "grid",
    savedViews: [],
    tableColumns: null,
  });
  useClock.setState({ now: NOW_MS });
}

/** Register per-test setup and teardown. Call once at the top of every test file. */
export function installHarness(): void {
  beforeEach(() => {
    resetStores();
  });
  afterEach(async () => {
    const open = document.querySelector<HTMLElement>("[role=dialog]");
    if (open) {
      const cancel = within(open).queryByRole("button", { name: "Cancel" });
      if (cancel) await userEvent.click(cancel);
    }
    cleanup();
    toast.dismiss();
    globalThis.fetch = realFetch;
  });
}

/** Queries scoped to one form row (`data-field`): schema forms do not link a label to its control. */
export function formField(
  name: string,
  scope: HTMLElement = document.body,
): ReturnType<typeof within> {
  const row = scope.querySelector<HTMLElement>(`[data-field="${name}"]`);
  if (!row) throw new Error(`no form field named ${name}`);
  return within(row);
}

/**
 * Matches a `<time>` element by its whole text. RelativeTime sets the count in its own span, so
 * `getByText("2 h ago")` finds no single element carrying the phrase.
 */
export function timeText(text: string): (content: string, element: Element | null) => boolean {
  return (_content, element) => element?.tagName === "TIME" && element.textContent === text;
}
