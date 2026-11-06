import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { applyMessage, type EventEnvelope, onEvent, startStream, useStream } from "@/api/stream";
import { NOW_SECONDS } from "./constants";

type SocketEvent = { data: string };

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = 0;
  readonly sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: SocketEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

interface ScheduledTimer {
  callback: () => void;
  delay: number;
  cleared: boolean;
}

function event(seq: number, overrides: Partial<EventEnvelope> = {}): EventEnvelope {
  return {
    seq,
    ts: NOW_SECONDS,
    service: "jobs",
    type: "job.finished",
    project_id: null,
    title: `event ${seq}`,
    severity: "info",
    data: {},
    persist: false,
    notify_kind: null,
    dedupe_key: null,
    ...overrides,
  };
}

function resetStream(): void {
  useStream.setState({ connection: "connecting", connectedAt: null, states: {}, events: [] });
}

beforeEach(resetStream);

describe("applying stream messages", () => {
  test("a hello replaces every service's state with its snapshot", () => {
    applyMessage(
      { kind: "hello", states: [{ service: "jobs", rev: 4, state: { running: 1 } }] },
      () => {},
    );
    applyMessage(
      { kind: "hello", states: [{ service: "forks", rev: 2, state: { forks: [] } }] },
      () => {},
    );
    expect(useStream.getState().states).toEqual({ forks: { rev: 2, state: { forks: [] } } });
  });

  test("a patch for the next revision updates the state in place", () => {
    applyMessage(
      { kind: "hello", states: [{ service: "jobs", rev: 4, state: { running: 1, queued: 0 } }] },
      () => {},
    );
    let resyncs = 0;
    applyMessage(
      {
        kind: "state",
        patch: {
          service: "jobs",
          rev: 5,
          ops: [{ op: "replace", path: "/running", value: 3 }],
        },
      },
      () => {
        resyncs += 1;
      },
    );
    expect(useStream.getState().states.jobs).toEqual({ rev: 5, state: { running: 3, queued: 0 } });
    expect(resyncs).toBe(0);
  });

  test("a patch that skips a revision asks for a resync and leaves the state untouched", () => {
    applyMessage(
      { kind: "hello", states: [{ service: "jobs", rev: 4, state: { running: 1 } }] },
      () => {},
    );
    let resyncs = 0;
    applyMessage(
      {
        kind: "state",
        patch: { service: "jobs", rev: 9, ops: [{ op: "replace", path: "/running", value: 3 }] },
      },
      () => {
        resyncs += 1;
      },
    );
    expect(resyncs).toBe(1);
    expect(useStream.getState().states.jobs).toEqual({ rev: 4, state: { running: 1 } });
  });

  test("a patch for a service never seen asks for a resync", () => {
    let resyncs = 0;
    applyMessage({ kind: "state", patch: { service: "ghost", rev: 1, ops: [] } }, () => {
      resyncs += 1;
    });
    expect(resyncs).toBe(1);
    expect(useStream.getState().states.ghost).toBeUndefined();
  });

  test("events are kept newest first and handed to every listener", () => {
    const heard: string[] = [];
    const stop = onEvent((incoming) => heard.push(incoming.title));
    applyMessage({ kind: "event", event: event(1) }, () => {});
    applyMessage({ kind: "event", event: event(2) }, () => {});
    expect(useStream.getState().events.map((item) => item.seq)).toEqual([2, 1]);
    expect(heard).toEqual(["event 1", "event 2"]);
    stop();
  });

  test("a listener that unsubscribed hears nothing more", () => {
    const heard: number[] = [];
    const stop = onEvent((incoming) => heard.push(incoming.seq));
    applyMessage({ kind: "event", event: event(1) }, () => {});
    stop();
    applyMessage({ kind: "event", event: event(2) }, () => {});
    expect(heard).toEqual([1]);
  });

  test("only the 400 most recent events are retained", () => {
    for (let seq = 1; seq <= 405; seq += 1)
      applyMessage({ kind: "event", event: event(seq) }, () => {});
    const kept = useStream.getState().events;
    expect(kept).toHaveLength(400);
    expect(kept[0]?.seq).toBe(405);
    expect(kept.at(-1)?.seq).toBe(6);
  });
});

/** happy-dom's window control surface, which the DOM `Window` type does not declare. */
const happyDom = (window as unknown as { happyDOM: { setURL(url: string): void } }).happyDOM;

describe("the live connection", () => {
  const realWebSocket = globalThis.WebSocket;
  let timers: ScheduledTimer[];
  const spies: { mockRestore: () => void }[] = [];

  beforeEach(() => {
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    timers = [];
    spies.push(
      spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void, delay: number) => {
        timers.push({ callback, delay, cleared: false });
        return timers.length;
      }) as unknown as typeof setTimeout),
      spyOn(globalThis, "clearTimeout").mockImplementation(((id: number) => {
        const timer = timers[id - 1];
        if (timer) timer.cleared = true;
      }) as unknown as typeof clearTimeout),
    );
  });

  afterEach(() => {
    for (const spy of spies.splice(0)) spy.mockRestore();
    globalThis.WebSocket = realWebSocket;
  });

  function lastSocket(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error("no socket was opened");
    return socket;
  }

  test("connects to the events endpoint on the page's host and reports connecting", () => {
    const stop = startStream();
    expect(lastSocket().url).toBe("ws://localhost/api/events");
    expect(useStream.getState().connection).toBe("connecting");
    stop();
  });

  test("uses a secure socket when the page is served over https", () => {
    happyDom.setURL("https://dash.example.com/");
    try {
      const stop = startStream();
      expect(lastSocket().url).toBe("wss://dash.example.com/api/events");
      stop();
    } finally {
      happyDom.setURL("http://localhost/");
    }
  });

  test("goes live when the socket opens and applies each message it receives", () => {
    const stop = startStream("ws://test/events");
    const socket = lastSocket();
    socket.open();
    expect(useStream.getState().connection).toBe("live");
    expect(useStream.getState().connectedAt).not.toBeNull();
    socket.receive({ kind: "hello", states: [{ service: "jobs", rev: 1, state: { running: 0 } }] });
    expect(useStream.getState().states.jobs?.state).toEqual({ running: 0 });
    stop();
  });

  test("a revision gap sends a resync request over the open socket", () => {
    const stop = startStream("ws://test/events");
    const socket = lastSocket();
    socket.open();
    socket.receive({ kind: "hello", states: [{ service: "jobs", rev: 1, state: {} }] });
    socket.receive({ kind: "state", patch: { service: "jobs", rev: 7, ops: [] } });
    expect(socket.sent).toEqual([JSON.stringify({ kind: "resync" })]);
    stop();
  });

  test("a resync is not sent once the socket has closed", () => {
    const stop = startStream("ws://test/events");
    const socket = lastSocket();
    socket.open();
    socket.receive({ kind: "hello", states: [{ service: "jobs", rev: 1, state: {} }] });
    socket.readyState = 3;
    socket.receive({ kind: "state", patch: { service: "jobs", rev: 7, ops: [] } });
    expect(socket.sent).toEqual([]);
    stop();
  });

  test("a dropped connection goes offline and reconnects after a doubling delay capped at ten seconds", () => {
    const stop = startStream("ws://test/events");
    const delays: number[] = [];
    for (let attempt = 0; attempt < 6; attempt += 1) {
      lastSocket().drop();
      expect(useStream.getState().connection).toBe("offline");
      const retry = timers.at(-1);
      if (!retry) throw new Error("no reconnect was scheduled");
      delays.push(retry.delay);
      retry.callback();
      expect(useStream.getState().connection).toBe("connecting");
    }
    expect(delays).toEqual([1000, 2000, 4000, 8000, 10_000, 10_000]);
    expect(FakeWebSocket.instances).toHaveLength(7);
    stop();
  });

  test("a successful open resets the reconnect delay", () => {
    const stop = startStream("ws://test/events");
    lastSocket().drop();
    timers.at(-1)?.callback();
    lastSocket().drop();
    expect(timers.at(-1)?.delay).toBe(2000);
    timers.at(-1)?.callback();
    lastSocket().open();
    lastSocket().drop();
    expect(timers.at(-1)?.delay).toBe(1000);
    stop();
  });

  test("stopping closes the socket and cancels a pending reconnect", () => {
    const stop = startStream("ws://test/events");
    const first = lastSocket();
    first.drop();
    const pending = timers.at(-1);
    stop();
    expect(pending?.cleared).toBe(true);
    expect(first.closed).toBe(true);
  });

  test("stopping an open connection does not schedule a reconnect when the socket reports closed", () => {
    const stop = startStream("ws://test/events");
    const socket = lastSocket();
    socket.open();
    const scheduledBefore = timers.length;
    stop();
    socket.drop();
    expect(socket.closed).toBe(true);
    expect(timers).toHaveLength(scheduledBefore);
  });
});
