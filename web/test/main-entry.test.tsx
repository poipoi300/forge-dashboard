import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { act, screen } from "@testing-library/react";
import * as reactDomClient from "react-dom/client";
import * as preferences from "@/app/preferences";
import { startClock } from "@/lib/time";
import { installHarness, mockApi, settle } from "./harness";

installHarness();

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(): void {}
  close(): void {}
}

describe("the application entry point", () => {
  const realWebSocket = globalThis.WebSocket;
  const roots: reactDomClient.Root[] = [];
  const restores: (() => void)[] = [];
  const intervalDelays: number[] = [];

  beforeEach(() => {
    FakeWebSocket.instances = [];
    intervalDelays.length = 0;
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const interval = spyOn(globalThis, "setInterval").mockImplementation(((
      _callback: () => void,
      delay: number,
    ) => {
      intervalDelays.push(delay);
      return 1;
    }) as unknown as typeof setInterval);
    const realCreateRoot = reactDomClient.createRoot;
    const create = spyOn(reactDomClient, "createRoot").mockImplementation((container, options) => {
      const root = realCreateRoot(container, options);
      roots.push(root);
      return root;
    });
    const realApplyPreferences = preferences.applyPreferences;
    const apply = spyOn(preferences, "applyPreferences").mockImplementation(() => {
      const stop = realApplyPreferences();
      restores.push(stop);
      return stop;
    });
    restores.push(
      () => interval.mockRestore(),
      () => create.mockRestore(),
      () => apply.mockRestore(),
    );
  });

  afterEach(async () => {
    await act(async () => {
      for (const root of roots.splice(0)) root.unmount();
    });
    // main.tsx discards the stop function, so release the shared clock timer the same way a caller would.
    for (const restore of restores.splice(0)) restore();
    startClock()();
    globalThis.WebSocket = realWebSocket;
    document.body.innerHTML = "";
  });

  test("refuses to start when the page has no root element", async () => {
    document.body.innerHTML = "";
    const specifier = "../src/main.tsx?without-root";
    await expect(import(specifier)).rejects.toThrow("index.html is missing #root");
  });

  test("mounts the app, applies the saved preferences and opens the live stream and the clock", async () => {
    const api = mockApi();
    api.config("discovery", { roots: [] });
    document.body.innerHTML = '<div id="root"></div>';
    const specifier = "../src/main.tsx?with-root";
    await act(async () => {
      await import(specifier);
    });
    await settle();

    expect(screen.getByRole("navigation", { name: "Sections" })).toBeTruthy();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.density).toBe("comfortable");
    expect(FakeWebSocket.instances.map((socket) => socket.url)).toEqual([
      "ws://localhost/api/events",
    ]);
    expect(intervalDelays).toEqual([1000]);
  });
});
