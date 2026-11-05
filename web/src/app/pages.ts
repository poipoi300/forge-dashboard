// The dashboard's top-level sections, shared by the rail and the command palette.
import {
  Activity,
  GitFork,
  LayoutGrid,
  ListChecks,
  Ruler,
  Server,
  Settings,
  TrainTrack,
} from "lucide-react";

export type PageId =
  | "overview"
  | "jobs"
  | "forks"
  | "standards"
  | "services"
  | "runs"
  | "activity"
  | "settings";

export interface Page {
  id: PageId;
  href: string;
  label: string;
  icon: typeof LayoutGrid;
}

export const PAGES: Page[] = [
  { id: "overview", href: "/", label: "Overview", icon: LayoutGrid },
  { id: "jobs", href: "/jobs", label: "Jobs", icon: ListChecks },
  { id: "forks", href: "/forks", label: "Forks", icon: GitFork },
  { id: "standards", href: "/standards", label: "Standards", icon: Ruler },
  { id: "services", href: "/services", label: "Services and machine", icon: Server },
  { id: "runs", href: "/runs", label: "Forge runs", icon: TrainTrack },
  { id: "activity", href: "/activity", label: "Activity", icon: Activity },
  { id: "settings", href: "/settings", label: "Settings", icon: Settings },
];
