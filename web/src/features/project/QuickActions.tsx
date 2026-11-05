// The routine one-click actions for a project, shared by its card, its table
// row, and its page header. Which ones appear follows the project's facts:
// Launch needs declared services, Sync needs a fork, Adopt replaces Bring up
// when there is no manifest yet.
import {
  Code2,
  ExternalLink,
  FolderOpen,
  GitFork,
  ListChecks,
  MoreHorizontal,
  Play,
  Ruler,
  SquareTerminal,
} from "lucide-react";
import type { Schemas } from "@/api/client";
import { GithubMark } from "@/components/icons";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ActionButton, type ActionRequest, useActionRunner } from "@/schema/ActionButton";

type ProjectSummary = Schemas["ProjectSummary"];

const RUNNING = new Set(["running", "external"]);

export function openUrl(project: ProjectSummary): string | null {
  return project.services.find((service) => RUNNING.has(service.state) && service.url)?.url ?? null;
}

interface MenuEntry {
  key: string;
  label: string;
  icon: typeof Play | typeof GithubMark;
  request?: ActionRequest;
  href?: string;
}

export function menuEntries(project: ProjectSummary): MenuEntry[] {
  const context = { projectId: project.id };
  const entries: MenuEntry[] = [
    {
      key: "editor",
      label: "Open in editor",
      icon: Code2,
      request: { service: "settings", action: "open-in-editor", context, input: { path: null } },
    },
    {
      key: "terminal",
      label: "Open in terminal",
      icon: SquareTerminal,
      request: { service: "settings", action: "open-in-terminal", context, input: { path: null } },
    },
    {
      key: "folder",
      label: "Open folder",
      icon: FolderOpen,
      request: { service: "settings", action: "open-folder", context, input: { path: null } },
    },
  ];
  if (project.role === "unmanifested") {
    entries.push({
      key: "adopt",
      label: "Adopt into forge",
      icon: Ruler,
      request: { service: "standards", action: "adopt", context },
    });
  } else if (project.role === "managed") {
    entries.push({
      key: "standards",
      label: "Bring up to standards",
      icon: Ruler,
      request: { service: "standards", action: "bring-up", context },
    });
  }
  if (project.issues?.html_url)
    entries.push({
      key: "github",
      label: "Open on GitHub",
      icon: GithubMark,
      href: project.issues.html_url,
    });
  return entries;
}

export function QuickActions({
  project,
  size = "xs",
}: {
  project: ProjectSummary;
  size?: "xs" | "sm";
}) {
  const run = useActionRunner();
  const context = { projectId: project.id };
  const url = openUrl(project);
  const iconSize = size === "xs" ? "icon-xs" : "icon-sm";
  return (
    <div className="flex flex-wrap items-center gap-1">
      {url ? (
        <Button asChild variant="outline" size={size}>
          <a href={url} target="_blank" rel="noreferrer">
            <ExternalLink />
            Open UI
          </a>
        </Button>
      ) : project.services.length ? (
        <ActionButton
          service="services"
          action="launch"
          context={context}
          size={size}
          icon={<Play />}
          label="Launch"
        />
      ) : null}
      {project.checks ? (
        <ActionButton
          service="checks"
          action="run"
          context={context}
          input={{ check_id: null, on_head: null }}
          size={size}
          icon={<ListChecks />}
          label="Run checks"
        />
      ) : null}
      {project.fork?.is_fork ? (
        <ActionButton
          service="forks"
          action="sync"
          context={context}
          size={size}
          icon={<GitFork />}
          label="Sync fork"
        />
      ) : null}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size={iconSize} aria-label={`More actions for ${project.name}`}>
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-48">
          {menuEntries(project).map((entry, index, list) => {
            const Icon = entry.icon;
            const separator =
              entry.key === "github" && index > 0 && list[index - 1]?.key !== "github";
            const item = entry.href ? (
              <DropdownMenuItem key={entry.key} asChild>
                <a href={entry.href} target="_blank" rel="noreferrer">
                  <Icon />
                  {entry.label}
                </a>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                key={entry.key}
                onSelect={() => entry.request && run(entry.request)}
              >
                <Icon />
                {entry.label}
              </DropdownMenuItem>
            );
            return separator ? [<DropdownMenuSeparator key={`${entry.key}-rule`} />, item] : item;
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
