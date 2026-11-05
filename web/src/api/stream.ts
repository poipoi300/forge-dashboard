// The one WebSocket: a hello with every service's state, then RFC 6902 patches
// and events. A revision gap or a lagging socket resyncs from a fresh hello.
import { applyPatch, type Operation } from "fast-json-patch";
import { create } from "zustand";
import type { Schemas } from "./client";

export type EventEnvelope = Schemas["EventEnvelope"];
type Snapshot = { rev: number; state: unknown };
export type Connection = "connecting" | "live" | "offline";

const RECENT_EVENTS = 400;

interface StreamState {
  connection: Connection;
  connectedAt: number | null;
  states: Record<string, Snapshot>;
  events: EventEnvelope[];
}

export const useStream = create<StreamState>(() => ({
  connection: "connecting",
  connectedAt: null,
  states: {},
  events: [],
}));

type Listener = (event: EventEnvelope) => void;
const listeners = new Set<Listener>();

/** Subscribe to every event as it arrives; returns the unsubscribe function. */
export function onEvent(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

type Message =
  | { kind: "hello"; states: { service: string; rev: number; state: unknown }[] }
  | { kind: "state"; patch: { service: string; rev: number; ops: Operation[] } }
  | { kind: "event"; event: EventEnvelope };

export function applyMessage(message: Message, resync: () => void): void {
  if (message.kind === "hello") {
    const states: Record<string, Snapshot> = {};
    for (const snapshot of message.states)
      states[snapshot.service] = { rev: snapshot.rev, state: snapshot.state };
    useStream.setState({ states });
    return;
  }
  if (message.kind === "state") {
    const { service, rev, ops } = message.patch;
    const current = useStream.getState().states[service];
    if (!current || rev !== current.rev + 1) {
      resync();
      return;
    }
    const next = applyPatch(current.state, ops, false, false).newDocument;
    useStream.setState((store) => ({
      states: { ...store.states, [service]: { rev, state: next } },
    }));
    return;
  }
  const event = message.event;
  useStream.setState((store) => ({ events: [event, ...store.events].slice(0, RECENT_EVENTS) }));
  for (const listener of listeners) listener(event);
}

export function startStream(url: string = defaultUrl()): () => void {
  let socket: WebSocket | null = null;
  let stopped = false;
  let delay = 1000;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const connect = () => {
    useStream.setState({ connection: "connecting" });
    socket = new WebSocket(url);
    const resync = () =>
      socket?.readyState === WebSocket.OPEN && socket.send(JSON.stringify({ kind: "resync" }));
    socket.onopen = () => {
      delay = 1000;
      useStream.setState({ connection: "live", connectedAt: Date.now() });
    };
    socket.onmessage = (event) => applyMessage(JSON.parse(String(event.data)) as Message, resync);
    socket.onclose = () => {
      useStream.setState({ connection: "offline" });
      if (stopped) return;
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 10_000);
    };
  };
  connect();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    socket?.close();
  };
}

function defaultUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/events`;
}

export interface ServiceStates {
  projects: Schemas["ProjectsState"];
  jobs: Schemas["JobsState"];
  scheduler: Schemas["SchedulerState"];
  discovery: Schemas["DiscoveryState"];
  git: Schemas["GitState"];
  github: Schemas["GitHubState"];
  checks: Schemas["ChecksState"];
  metrics: Schemas["MetricsState"];
  agents: Schemas["AgentsState"];
  forks: Schemas["ForksState"];
  standards: Schemas["StandardsState"];
  machine: Schemas["MachineState"];
  forge_runs: Schemas["ForgeRunsState"];
  services: Schemas["ProjectServicesState"];
  manifests: Schemas["ManifestsState"];
  gallery: Schemas["GalleryState"];
  events: Schemas["EventsState"];
  settings: Schemas["SettingsState"];
  actions: Schemas["ActionsState"];
}

/** A service's live state, or undefined until the first hello arrives. */
export function useServiceState<K extends keyof ServiceStates>(
  service: K,
): ServiceStates[K] | undefined {
  return useStream((store) => store.states[service]?.state as ServiceStates[K] | undefined);
}

/** A service's live state by runtime id (schema-built panels). */
export function useAnyServiceState(service: string): unknown {
  return useStream((store) => store.states[service]?.state);
}
