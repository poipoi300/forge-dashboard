import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { applyPreferences, resolvedTheme, usePreferences } from "@/app/preferences";

const DEFAULTS = {
  theme: "system",
  density: "comfortable",
  overview: "grid",
  savedViews: [],
  tableColumns: null,
} as const;

beforeEach(() => {
  localStorage.clear();
  usePreferences.setState({ ...DEFAULTS, savedViews: [] });
});

describe("preference actions", () => {
  test("each setter changes only its own preference", () => {
    const { setTheme, setDensity, setOverview, setTableColumns } = usePreferences.getState();
    setTheme("dark");
    setDensity("compact");
    setOverview("table");
    setTableColumns(["status", "health"]);
    const state = usePreferences.getState();
    expect([state.theme, state.density, state.overview, state.tableColumns]).toEqual([
      "dark",
      "compact",
      "table",
      ["status", "health"],
    ]);
    setTableColumns(null);
    expect(usePreferences.getState().tableColumns).toBeNull();
  });

  test("saving a view under an existing name replaces it and keeps the others", () => {
    const { saveView } = usePreferences.getState();
    saveView({ name: "failing", query: "?failing=1" });
    saveView({ name: "forks", query: "?fork=1" });
    saveView({ name: "failing", query: "?health=failing" });
    expect(usePreferences.getState().savedViews).toEqual([
      { name: "forks", query: "?fork=1" },
      { name: "failing", query: "?health=failing" },
    ]);
  });

  test("deleting a view removes only that name", () => {
    const { saveView, deleteView } = usePreferences.getState();
    saveView({ name: "a", query: "?q=a" });
    saveView({ name: "b", query: "?q=b" });
    deleteView("a");
    expect(usePreferences.getState().savedViews).toEqual([{ name: "b", query: "?q=b" }]);
  });

  test("preferences are written to local storage so they survive a reload", () => {
    usePreferences.getState().setTheme("light");
    const stored = JSON.parse(localStorage.getItem("forge-dashboard-preferences") ?? "null");
    expect(stored.state.theme).toBe("light");
  });
});

describe("resolving the theme", () => {
  test("an explicit choice wins over the operating system", () => {
    expect(resolvedTheme("light", true)).toBe("light");
    expect(resolvedTheme("dark", false)).toBe("dark");
  });

  test("following the system picks dark only when the system prefers it", () => {
    expect(resolvedTheme("system", true)).toBe("dark");
    expect(resolvedTheme("system", false)).toBe("light");
  });
});

describe("keeping the document in step with preferences", () => {
  interface FakeMedia {
    matches: boolean;
    listeners: Set<() => void>;
    addEventListener(type: string, listener: () => void): void;
    removeEventListener(type: string, listener: () => void): void;
  }

  let media: FakeMedia;
  let stop: (() => void) | null = null;
  const restores: (() => void)[] = [];

  beforeEach(() => {
    media = {
      matches: false,
      listeners: new Set(),
      addEventListener: (_type, listener) => media.listeners.add(listener),
      removeEventListener: (_type, listener) => media.listeners.delete(listener),
    };
    const spy = spyOn(window, "matchMedia").mockImplementation(
      () => media as unknown as MediaQueryList,
    );
    restores.push(() => spy.mockRestore());
  });

  afterEach(() => {
    stop?.();
    stop = null;
    for (const restore of restores.splice(0)) restore();
    delete document.documentElement.dataset.theme;
    delete document.documentElement.dataset.density;
  });

  test("the current theme and density are applied immediately", () => {
    usePreferences.setState({ theme: "dark", density: "compact" });
    stop = applyPreferences();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.density).toBe("compact");
  });

  test("a changed preference is reflected on the document", () => {
    stop = applyPreferences();
    expect(document.documentElement.dataset.theme).toBe("light");
    usePreferences.getState().setTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    usePreferences.getState().setDensity("compact");
    expect(document.documentElement.dataset.density).toBe("compact");
  });

  test("following the system re-resolves when the operating system switches", () => {
    stop = applyPreferences();
    expect(document.documentElement.dataset.theme).toBe("light");
    media.matches = true;
    for (const listener of media.listeners) listener();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("stopping detaches from the system and from the store", () => {
    stop = applyPreferences();
    stop();
    stop = null;
    expect(media.listeners.size).toBe(0);
    usePreferences.getState().setTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
