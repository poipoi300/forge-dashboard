// The overview: every project at a glance as cards or a timetable, with
// search, facet filters, sort, grouping, saved views, and keyboard travel.
// The whole view lives in the URL, so a view is a link.
import { useQuery } from "@tanstack/react-query";
import {
  Bookmark,
  FolderPlus,
  LayoutGrid,
  ListFilter,
  Rows3,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearch } from "wouter";
import { getConfig } from "@/api/client";
import { useServiceState } from "@/api/stream";
import { usePreferences } from "@/app/preferences";
import { usePageTitle } from "@/app/title";
import { EmptyState, LoadingRows } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { ActionButton, useActionRunner } from "@/schema/ActionButton";
import {
  arrange,
  EMPTY_VIEW,
  facetValues,
  GROUP_KEYS,
  type GroupKey,
  isFiltered,
  parseView,
  SORT_KEYS,
  type SortKey,
  serializeView,
  type ViewState,
} from "./filters";
import { ProjectCard } from "./ProjectCard";
import { ProjectTable } from "./ProjectTable";
import { useSummarySchema } from "./summarySchema";

const SORT_LABELS: Record<SortKey, string> = {
  changed: "Last change",
  name: "Name",
  health: "Health",
  progress: "V1 progress",
  issues: "Open issues",
};
const GROUP_LABELS: Record<GroupKey, string> = {
  none: "No grouping",
  status: "Status",
  kind: "Kind",
  health: "Health",
  root: "Root folder",
};
const FACETS = [
  { key: "status", label: "Status" },
  { key: "health", label: "Health" },
  { key: "kind", label: "Kind" },
  { key: "language", label: "Language" },
  { key: "tag", label: "Tag" },
] as const;
const FLAGS = [
  { key: "failing", label: "Failing" },
  { key: "hasIssues", label: "Has open issues" },
  { key: "fork", label: "Forks" },
  { key: "running", label: "Services running" },
  { key: "attention", label: "Needs attention" },
] as const;

function useView(): [ViewState, (next: ViewState) => void] {
  const search = useSearch();
  const [location, navigate] = useLocation();
  const view = useMemo(() => parseView(search), [search]);
  const setView = useCallback(
    (next: ViewState) => navigate(`${location}${serializeView(next)}`, { replace: true }),
    [location, navigate],
  );
  return [view, setView];
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function FilterMenu({
  view,
  setView,
  projects,
}: {
  view: ViewState;
  setView: (next: ViewState) => void;
  projects: Parameters<typeof facetValues>[0];
}) {
  const active =
    FACETS.reduce((sum, facet) => sum + view[facet.key].length, 0) +
    FLAGS.filter((flag) => view[flag.key]).length;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" aria-label="Filters">
          <ListFilter />
          Filter
          {active ? (
            <span className="figures rounded-sm bg-ink px-1 text-2xs text-surface">{active}</span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[34rem] max-w-[92vw] p-0">
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 p-3 sm:grid-cols-3">
          {FACETS.map((facet) => {
            const values = facetValues(projects, facet.key);
            if (!values.length) return null;
            return (
              <fieldset key={facet.key} className="flex min-w-0 flex-col gap-1">
                <legend className="mb-1 text-2xs font-semibold text-ink-3">{facet.label}</legend>
                {values.map((value) => (
                  <label key={value} className="flex min-w-0 items-center gap-2 text-xs">
                    <Checkbox
                      checked={view[facet.key].includes(value)}
                      onCheckedChange={() =>
                        setView({ ...view, [facet.key]: toggle(view[facet.key], value) })
                      }
                    />
                    <span className="truncate">{value}</span>
                  </label>
                ))}
              </fieldset>
            );
          })}
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-2xs font-semibold text-ink-3">Only</legend>
            {FLAGS.map((flag) => (
              <label key={flag.key} className="flex items-center gap-2 text-xs">
                <Checkbox
                  checked={view[flag.key]}
                  onCheckedChange={(checked) => setView({ ...view, [flag.key]: checked === true })}
                />
                {flag.label}
              </label>
            ))}
          </fieldset>
        </div>
        <div className="flex justify-end border-t border-rule px-3 py-2">
          <Button
            variant="ghost"
            size="xs"
            disabled={!isFiltered(view)}
            onClick={() => setView({ ...EMPTY_VIEW, sort: view.sort, group: view.group })}
          >
            Clear filters
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function ActiveFilters({ view, setView }: { view: ViewState; setView: (next: ViewState) => void }) {
  const chips: { key: string; label: string; clear: () => void }[] = [];
  for (const facet of FACETS) {
    for (const value of view[facet.key]) {
      chips.push({
        key: `${facet.key}:${value}`,
        label: `${facet.label.toLowerCase()}: ${value}`,
        clear: () =>
          setView({ ...view, [facet.key]: view[facet.key].filter((item) => item !== value) }),
      });
    }
  }
  for (const flag of FLAGS) {
    if (view[flag.key])
      chips.push({
        key: flag.key,
        label: flag.label.toLowerCase(),
        clear: () => setView({ ...view, [flag.key]: false }),
      });
  }
  if (!chips.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          onClick={chip.clear}
          className="inline-flex items-center gap-1 rounded-sm border border-rule-strong bg-surface px-1.5 py-0.5 text-2xs text-ink-2 hover:border-ink-3 hover:text-ink"
        >
          {chip.label}
          <X aria-label="remove" className="size-3" />
        </button>
      ))}
    </div>
  );
}

function SavedViews({ view, setView }: { view: ViewState; setView: (next: ViewState) => void }) {
  const saved = usePreferences((preferences) => preferences.savedViews);
  const saveView = usePreferences((preferences) => preferences.saveView);
  const deleteView = usePreferences((preferences) => preferences.deleteView);
  const [naming, setNaming] = useState<string | null>(null);
  const current = serializeView(view);
  return (
    <DropdownMenu onOpenChange={(open) => !open && setNaming(null)}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          <Bookmark />
          Views
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="text-2xs text-ink-3">Saved views</DropdownMenuLabel>
        {saved.length ? (
          saved.map((item) => (
            <DropdownMenuItem
              key={item.name}
              onSelect={() => setView(parseView(item.query))}
              className="group/view"
            >
              <span className={cn("flex-1 truncate", item.query === current && "font-semibold")}>
                {item.name}
              </span>
              <button
                type="button"
                aria-label={`Delete view ${item.name}`}
                onClick={(event) => {
                  event.stopPropagation();
                  deleteView(item.name);
                }}
                className="rounded-sm p-0.5 text-ink-3 opacity-0 hover:text-signal-ink group-hover/view:opacity-100 group-focus/view:opacity-100"
              >
                <Trash2 className="size-3.5" />
              </button>
            </DropdownMenuItem>
          ))
        ) : (
          <p className="px-2 py-1.5 text-xs text-ink-3">
            None yet. Save the current filters to come back to them.
          </p>
        )}
        <DropdownMenuSeparator />
        {naming === null ? (
          <DropdownMenuItem
            onSelect={(event) => {
              event.preventDefault();
              setNaming("");
            }}
          >
            Save current view…
          </DropdownMenuItem>
        ) : (
          <form
            className="flex gap-1 p-1"
            onSubmit={(event) => {
              event.preventDefault();
              if (!naming.trim()) return;
              saveView({ name: naming.trim(), query: current });
              setNaming(null);
            }}
          >
            <Input
              autoFocus
              value={naming}
              onChange={(event) => setNaming(event.target.value)}
              onKeyDown={(event) => event.stopPropagation()}
              placeholder="View name"
              className="h-7 text-xs"
            />
            <Button type="submit" size="xs">
              Save
            </Button>
          </form>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) ||
    target.closest("[role=dialog],[role=menu]") !== null
  );
}

export function Overview() {
  usePageTitle("Overview");
  const projectsState = useServiceState("projects");
  const discovery = useServiceState("discovery");
  const layout = useSummarySchema();
  const [view, setView] = useView();
  const mode = usePreferences((preferences) => preferences.overview);
  const setMode = usePreferences((preferences) => preferences.setOverview);
  const density = usePreferences((preferences) => preferences.density);
  const setDensity = usePreferences((preferences) => preferences.setDensity);
  const tableColumns = usePreferences((preferences) => preferences.tableColumns);
  const setTableColumns = usePreferences((preferences) => preferences.setTableColumns);
  const [, navigate] = useLocation();
  const run = useActionRunner();
  const search = useRef<HTMLInputElement>(null);
  const items = useRef(new Map<string, HTMLElement>());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const roots = useQuery({
    queryKey: ["config", "discovery"],
    queryFn: () => getConfig("discovery"),
  });
  const rootList = (roots.data?.roots as string[] | undefined) ?? [];

  const projects = projectsState?.projects ?? [];
  const groups = useMemo(() => arrange(projects, view, rootList), [projects, view, rootList]);
  const ordered = useMemo(() => groups.flatMap((group) => group.projects), [groups]);
  const columns = tableColumns ?? layout?.columns ?? [];

  const registerItem = useCallback((id: string, element: HTMLElement | null) => {
    if (element) items.current.set(id, element);
    else items.current.delete(id);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "/" && !isTyping(event.target)) {
        event.preventDefault();
        search.current?.focus();
        return;
      }
      if (event.key === "Escape" && event.target === search.current) {
        if (view.q) setView({ ...view, q: "" });
        search.current?.blur();
        return;
      }
      if (isTyping(event.target) || !ordered.length) return;
      const index = ordered.findIndex((project) => project.id === selectedId);
      const move = (delta: number) => {
        event.preventDefault();
        const next =
          ordered[Math.max(0, Math.min(ordered.length - 1, index < 0 ? 0 : index + delta))];
        if (!next) return;
        setSelectedId(next.id);
        const element = items.current.get(next.id);
        element?.focus({ preventScroll: true });
        element?.scrollIntoView({ block: "nearest" });
      };
      if (
        event.key === "j" ||
        event.key === "ArrowDown" ||
        (mode === "grid" && event.key === "ArrowRight")
      )
        move(1);
      else if (
        event.key === "k" ||
        event.key === "ArrowUp" ||
        (mode === "grid" && event.key === "ArrowLeft")
      )
        move(-1);
      else if (event.key === "Enter" && index >= 0 && ordered[index])
        navigate(`/p/${ordered[index].id}`);
      else if (event.key === "f" && index >= 0 && ordered[index]) {
        const project = ordered[index];
        run({
          service: "projects",
          action: "set-favorite",
          context: { projectId: project.id },
          input: { favorite: !project.favorite },
        });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [ordered, selectedId, mode, navigate, run, view, setView]);

  const loading = !projectsState || !layout || !projectsState.loaded;
  const total = projects.length;

  return (
    <div className="flex flex-col gap-3 px-5 py-4">
      <h1 className="sr-only">Overview</h1>
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative flex min-w-56 flex-1 items-center sm:max-w-sm">
          <Search aria-hidden className="pointer-events-none absolute left-2 size-3.5 text-ink-3" />
          <span className="sr-only">Search projects</span>
          <Input
            ref={search}
            value={view.q}
            onChange={(event) => setView({ ...view, q: event.target.value })}
            placeholder="Search name, path, tag, language"
            className="h-8 pl-7 text-sm"
          />
          <kbd className="pointer-events-none absolute right-2 rounded-[2px] border border-rule px-1 font-sans text-2xs text-ink-3">
            /
          </kbd>
        </label>
        <FilterMenu view={view} setView={setView} projects={projects} />
        <Select
          value={view.sort}
          onValueChange={(sort) => setView({ ...view, sort: sort as SortKey })}
        >
          <SelectTrigger size="sm" className="w-36" aria-label="Sort by">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SORT_KEYS.map((key) => (
              <SelectItem key={key} value={key}>
                {SORT_LABELS[key]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={view.group}
          onValueChange={(group) => setView({ ...view, group: group as GroupKey })}
        >
          <SelectTrigger size="sm" className="w-36" aria-label="Group by">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {GROUP_KEYS.map((key) => (
              <SelectItem key={key} value={key}>
                {GROUP_LABELS[key]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="ml-auto flex items-center gap-2">
          <SavedViews view={view} setView={setView} />
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={mode}
            onValueChange={(value) => value && setMode(value as "grid" | "table")}
            aria-label="Layout"
          >
            <ToggleGroupItem value="grid" aria-label="Cards" title="Cards">
              <LayoutGrid />
            </ToggleGroupItem>
            <ToggleGroupItem value="table" aria-label="Table" title="Table">
              <Rows3 />
            </ToggleGroupItem>
          </ToggleGroup>
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={density}
            onValueChange={(value) => value && setDensity(value as "comfortable" | "compact")}
            aria-label="Density"
          >
            <ToggleGroupItem value="comfortable" className="px-2 text-xs">
              Roomy
            </ToggleGroupItem>
            <ToggleGroupItem value="compact" className="px-2 text-xs">
              Dense
            </ToggleGroupItem>
          </ToggleGroup>
          {mode === "table" && layout ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  Columns
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
                {layout.fields
                  .filter((field) => !field.ui.hidden && field.name !== "name")
                  .map((field) => (
                    <DropdownMenuItem
                      key={field.name}
                      onSelect={(event) => {
                        event.preventDefault();
                        const next = columns.includes(field.name)
                          ? columns.filter((name) => name !== field.name)
                          : [...columns, field.name];
                        setTableColumns(next);
                      }}
                    >
                      <Checkbox checked={columns.includes(field.name)} tabIndex={-1} aria-hidden />
                      {field.ui.short_label ?? field.name}
                    </DropdownMenuItem>
                  ))}
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={() => setTableColumns(null)}>
                  Reset to default
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <ActiveFilters view={view} setView={setView} />
        <p className="figures text-xs text-ink-3" aria-live="polite">
          {loading
            ? "loading"
            : isFiltered(view)
              ? `${ordered.length} of ${total} projects`
              : `${total} projects`}
          {projectsState?.hidden.length ? ` · ${projectsState.hidden.length} hidden` : ""}
          {discovery?.scan.running ? ` · scanning ${discovery.scan.directories} folders` : ""}
        </p>
      </div>

      {loading ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(21rem,1fr))] gap-3">
          {Array.from({ length: 6 }, (_, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: placeholders
            <div key={index} className="rounded-md border border-rule bg-surface p-3.5">
              <LoadingRows rows={4} />
            </div>
          ))}
        </div>
      ) : !total ? (
        <EmptyState
          title={discovery?.scan.running ? "Looking for projects…" : "No projects found yet"}
          action={
            <div className="flex gap-2">
              <ActionButton
                service="discovery"
                action="add-project"
                icon={<FolderPlus />}
                label="Add a project folder"
              />
              <ActionButton
                service="discovery"
                action="rescan"
                label="Scan again"
                variant="ghost"
              />
            </div>
          }
        >
          {discovery?.scan.running
            ? `Scanned ${discovery.scan.directories} folders so far${discovery.scan.current ? `, now in ${discovery.scan.current}` : ""}.`
            : `The scan covers ${rootList.join(", ") || "no folders"}. A project is a folder with a forge.project.toml, forge itself, or a repo whose AGENTS.md and gates come from forge. Add one by hand, or change the folders in Settings.`}
        </EmptyState>
      ) : !ordered.length ? (
        <EmptyState
          title="No project matches this view"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => setView({ ...EMPTY_VIEW, sort: view.sort, group: view.group })}
            >
              Clear search and filters
            </Button>
          }
        />
      ) : mode === "table" && layout ? (
        <ProjectTable
          groups={groups}
          layout={layout}
          columns={columns}
          selectedId={selectedId}
          onSelect={setSelectedId}
          registerRow={registerItem}
        />
      ) : layout ? (
        <div className="flex flex-col gap-5">
          {groups.map((group) => (
            <section
              key={group.key}
              aria-label={group.label || "Projects"}
              className="flex flex-col gap-2"
            >
              {group.label ? (
                <h2 className="flex items-baseline gap-2 border-b border-rule-strong pb-1 text-xs font-semibold text-ink-2">
                  {group.label}
                  <span className="figures font-normal text-ink-3">{group.projects.length}</span>
                </h2>
              ) : null}
              <div className="grid grid-cols-[repeat(auto-fill,minmax(21rem,1fr))] items-start gap-3 compact:grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] compact:gap-2">
                {group.projects.map((project) => (
                  <ProjectCard
                    key={project.id}
                    ref={(element) => registerItem(project.id, element)}
                    project={project}
                    layout={layout}
                    selected={selectedId === project.id}
                    onFocus={() => setSelectedId(project.id)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : null}
    </div>
  );
}
