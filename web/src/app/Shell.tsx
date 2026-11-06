// The application shell: the board-coloured rail with every section, the
// departures strip, connection and degraded-state banners, and the routed page.
import { Bell, type LayoutGrid, Menu, Monitor, Moon, Sun, WifiOff } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link, useLocation } from "wouter";
import { useServiceState, useStream } from "@/api/stream";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { Departures } from "./Departures";
import { PAGES, type PageId } from "./pages";
import { type ThemeChoice, usePreferences } from "./preferences";

interface NavItem {
  href: string;
  label: string;
  icon: typeof LayoutGrid;
  count?: number;
  /** What the count counts, for its tooltip and screen readers. */
  countLabel?: string;
  alert?: boolean;
}

function useNavItems(): NavItem[] {
  const jobs = useServiceState("jobs");
  const forks = useServiceState("forks");
  const standards = useServiceState("standards");
  const services = useServiceState("services");
  const parked = (forks?.forks ?? []).filter((fork) =>
    ["conflict", "needs-attention", "ready-to-apply"].includes(fork.last_run?.state ?? ""),
  ).length;
  const drifting = (standards?.projects ?? []).filter(
    (item) => (item.report?.passing ?? 0) < (item.report?.total ?? 0),
  ).length;
  const running = (services?.services ?? []).filter(
    (item) => item.state === "running" || item.state === "external",
  ).length;
  const counts: Partial<Record<PageId, Pick<NavItem, "count" | "countLabel" | "alert">>> = {
    jobs: { count: (jobs?.running ?? 0) + (jobs?.queued ?? 0), countLabel: "running or waiting" },
    forks: { count: parked, countLabel: "parked syncs waiting on you", alert: parked > 0 },
    standards: { count: drifting, countLabel: "projects behind forge's standards" },
    services: { count: running, countLabel: "services up" },
  };
  return PAGES.map((page) => ({
    href: page.href,
    label: page.id === "services" ? "Services" : page.label,
    icon: page.icon,
    ...counts[page.id],
  }));
}

function NavLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-8 items-center gap-2.5 rounded-sm px-2.5 text-sm text-board-ink-2 hover:bg-board-2 hover:text-board-ink",
        active && "bg-board-3 text-board-ink",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" strokeWidth={1.75} />
      <span className="flex-1 truncate">{item.label}</span>
      {item.count ? (
        <span
          title={`${item.count} ${item.countLabel ?? ""}`.trim()}
          className={cn(
            "figures text-xs",
            item.alert ? "font-semibold text-board-signal" : "text-board-ink-2",
          )}
        >
          {item.count}
          <span className="sr-only"> {item.countLabel}</span>
        </span>
      ) : null}
    </Link>
  );
}

const THEMES: { value: ThemeChoice; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "Follow system", icon: Monitor },
];

function ThemeSwitch() {
  const theme = usePreferences((preferences) => preferences.theme);
  const setTheme = usePreferences((preferences) => preferences.setTheme);
  return (
    <fieldset className="flex rounded-sm border border-board-rule p-0.5">
      <legend className="sr-only">Theme</legend>
      {THEMES.map((option) => {
        const Icon = option.icon;
        return (
          <label
            key={option.value}
            title={option.label}
            className={cn(
              "flex flex-1 cursor-pointer items-center justify-center rounded-[2px] py-1 text-board-ink-2 hover:text-board-ink has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-info",
              theme === option.value && "bg-board-3 text-board-ink",
            )}
          >
            <input
              type="radio"
              name="theme"
              value={option.value}
              checked={theme === option.value}
              onChange={() => setTheme(option.value)}
              className="sr-only"
            />
            <Icon aria-hidden className="size-3.5" />
            <span className="sr-only">{option.label}</span>
          </label>
        );
      })}
    </fieldset>
  );
}

function Rail({ onNavigate }: { onNavigate?: () => void }) {
  const [location] = useLocation();
  const items = useNavItems();
  const unread = useServiceState("events")?.unread ?? 0;
  const isActive = (href: string) =>
    href === "/" ? location === "/" || location.startsWith("/p/") : location.startsWith(href);
  return (
    <div className="flex h-full flex-col gap-4 bg-board px-2.5 py-3">
      <Link href="/" onClick={onNavigate} className="flex items-center gap-2 px-2 py-1">
        <span
          aria-hidden
          className="relative inline-flex size-5 items-center justify-center rounded-[3px] bg-board-ink"
        >
          <span className="size-2 rounded-full bg-signal" />
        </span>
        <span className="text-sm font-semibold tracking-tight text-board-ink">forge dashboard</span>
      </Link>
      <nav aria-label="Sections" className="flex flex-col gap-0.5">
        {items.map((item) => (
          <NavLink
            key={item.href}
            item={item}
            active={isActive(item.href)}
            onNavigate={onNavigate}
          />
        ))}
      </nav>
      <div className="mt-auto flex flex-col gap-2">
        <Link
          href="/activity?notifications=1"
          onClick={onNavigate}
          className="flex h-8 items-center gap-2.5 rounded-sm px-2.5 text-sm text-board-ink-2 hover:bg-board-2 hover:text-board-ink"
        >
          <Bell aria-hidden className="size-4" strokeWidth={1.75} />
          <span className="flex-1">Notifications</span>
          {unread ? (
            <span
              className="figures text-xs font-semibold text-board-signal"
              title={`${unread} unread`}
            >
              {unread}
            </span>
          ) : null}
        </Link>
        <p className="px-2.5 text-2xs text-board-ink-2">
          <kbd className="rounded-[2px] border border-board-rule px-1 font-sans">Ctrl K</kbd>{" "}
          commands
        </p>
        <ThemeSwitch />
      </div>
    </div>
  );
}

function ConnectionBanner() {
  const connection = useStream((stream) => stream.connection);
  if (connection !== "offline") return null;
  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-signal/30 bg-signal-wash px-4 py-1.5 text-xs text-signal-ink"
    >
      <WifiOff aria-hidden className="size-3.5" />
      The dashboard API is unreachable; retrying. What you see is the last state received.
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const overview = location === "/";
  return (
    <div className="flex min-h-dvh">
      <aside className="sticky top-0 hidden h-dvh w-52 shrink-0 md:block">
        <Rail />
      </aside>
      <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
        <SheetContent side="left" className="w-60 border-board-rule bg-board p-0">
          <SheetTitle className="sr-only">Sections</SheetTitle>
          <Rail onNavigate={() => setMenuOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="sticky top-0 z-20">
          <div className="flex items-center gap-2 bg-board px-3 py-2 md:hidden">
            <button
              type="button"
              aria-label="Open sections"
              onClick={() => setMenuOpen(true)}
              className="rounded-sm p-1 text-board-ink hover:bg-board-2"
            >
              <Menu className="size-5" />
            </button>
            <span className="text-sm font-semibold text-board-ink">forge dashboard</span>
          </div>
          <Departures compact={!overview} />
          <ConnectionBanner />
        </div>
        <main className="flex-1">{children}</main>
      </div>
    </div>
  );
}
