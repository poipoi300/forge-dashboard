// Viewer preferences kept in the browser: theme, density, overview view,
// favourites' order lives on the server; these do not.
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemeChoice = "system" | "light" | "dark";
export type Density = "comfortable" | "compact";
export type OverviewMode = "grid" | "table";

export interface SavedView {
  name: string;
  query: string;
}

interface Preferences {
  theme: ThemeChoice;
  density: Density;
  overview: OverviewMode;
  savedViews: SavedView[];
  tableColumns: string[] | null;
  setTheme: (theme: ThemeChoice) => void;
  setDensity: (density: Density) => void;
  setOverview: (mode: OverviewMode) => void;
  saveView: (view: SavedView) => void;
  deleteView: (name: string) => void;
  setTableColumns: (columns: string[] | null) => void;
}

export const usePreferences = create<Preferences>()(
  persist(
    (set) => ({
      theme: "system",
      density: "comfortable",
      overview: "grid",
      savedViews: [],
      tableColumns: null,
      setTheme: (theme) => set({ theme }),
      setDensity: (density) => set({ density }),
      setOverview: (overview) => set({ overview }),
      saveView: (view) =>
        set((state) => ({
          savedViews: [...state.savedViews.filter((item) => item.name !== view.name), view],
        })),
      deleteView: (name) =>
        set((state) => ({ savedViews: state.savedViews.filter((item) => item.name !== name) })),
      setTableColumns: (tableColumns) => set({ tableColumns }),
    }),
    { name: "forge-dashboard-preferences" },
  ),
);

export function resolvedTheme(choice: ThemeChoice, prefersDark: boolean): "light" | "dark" {
  if (choice === "system") return prefersDark ? "dark" : "light";
  return choice;
}

/** Keep `data-theme` and `data-density` on <html> in step with the preferences and the OS. */
export function applyPreferences(): () => void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    const { theme, density } = usePreferences.getState();
    document.documentElement.dataset.theme = resolvedTheme(theme, media.matches);
    document.documentElement.dataset.density = density;
  };
  apply();
  media.addEventListener("change", apply);
  const unsubscribe = usePreferences.subscribe(apply);
  return () => {
    media.removeEventListener("change", apply);
    unsubscribe();
  };
}
