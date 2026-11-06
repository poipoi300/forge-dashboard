// Ctrl/Cmd+K: jump to any project or page, or run any action. Actions that
// need a project use the open project, or ask for one as a second step.
import { ArrowRight, FolderGit2, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { type ActionDescriptor, asSchema, useDescriptors } from "@/api/descriptors";
import { useServiceState } from "@/api/stream";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import { useActionRunner } from "@/schema/ActionButton";
import { fields } from "@/schema/schema";
import { PAGES } from "./pages";

interface PaletteAction {
  service: string;
  serviceTitle: string;
  descriptor: ActionDescriptor;
  needsProject: boolean;
}

function projectFromLocation(location: string): string | null {
  const match = /^\/p\/([^/]+)/.exec(location);
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [choosingFor, setChoosingFor] = useState<PaletteAction | null>(null);
  const [location, navigate] = useLocation();
  const descriptors = useDescriptors();
  const projects = useServiceState("projects")?.projects ?? [];
  const run = useActionRunner();
  const currentProject = projectFromLocation(location);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const actions = useMemo<PaletteAction[]>(() => {
    if (!descriptors) return [];
    return [...descriptors.values()].flatMap((descriptor) =>
      descriptor.actions
        .filter((action) => !action.query)
        .map((action) => {
          const schema = asSchema(action.input_schema);
          return {
            service: descriptor.id,
            serviceTitle: descriptor.title,
            descriptor: action,
            needsProject: fields(schema, schema).some((field) => field.ui.bind === "project"),
          };
        }),
    );
  }, [descriptors]);

  const close = () => {
    setOpen(false);
    setChoosingFor(null);
  };
  const start = (action: PaletteAction, projectId: string | null) => {
    close();
    run({
      service: action.service,
      action: action.descriptor.id,
      context: projectId ? { projectId } : {},
    });
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={(value) => (value ? setOpen(true) : close())}
      title="Command palette"
      description="Jump to a project or page, or run an action"
      className="sm:max-w-xl"
    >
      <CommandInput
        placeholder={
          choosingFor
            ? `${choosingFor.descriptor.title}: which project?`
            : "Jump to a project, page, or action…"
        }
      />
      <CommandList className="max-h-[60vh]">
        <CommandEmpty>Nothing matches.</CommandEmpty>
        {choosingFor ? (
          <CommandGroup heading={`Run "${choosingFor.descriptor.title}" on`}>
            {projects.map((project) => (
              <CommandItem
                key={project.id}
                value={`${project.name} ${project.id}`}
                onSelect={() => start(choosingFor, project.id)}
              >
                <FolderGit2 />
                {project.name}
                <code className="font-mono text-2xs text-ink-3">{project.id}</code>
              </CommandItem>
            ))}
          </CommandGroup>
        ) : (
          <>
            <CommandGroup heading="Projects">
              {projects.map((project) => (
                <CommandItem
                  key={project.id}
                  value={`project ${project.name} ${project.id} ${project.tags.join(" ")}`}
                  onSelect={() => {
                    close();
                    navigate(`/p/${project.id}`);
                  }}
                >
                  <FolderGit2 />
                  <span className="truncate">{project.name}</span>
                  <code className="shrink-0 font-mono text-2xs text-ink-3">{project.id}</code>
                  <span className="truncate text-xs text-ink-3">{project.summary}</span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandGroup heading="Pages">
              {PAGES.map((page) => (
                <CommandItem
                  key={page.href}
                  value={`page ${page.label}`}
                  onSelect={() => {
                    close();
                    navigate(page.href);
                  }}
                >
                  <page.icon />
                  {page.label}
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandGroup
              heading={
                currentProject
                  ? `Actions (on ${currentProject} where they take a project)`
                  : "Actions"
              }
            >
              {actions.map((action) => (
                <CommandItem
                  key={`${action.service}.${action.descriptor.id}`}
                  value={`action ${action.serviceTitle} ${action.descriptor.title} ${action.descriptor.description ?? ""}`}
                  onSelect={() => {
                    if (!action.needsProject) start(action, null);
                    else if (currentProject) start(action, currentProject);
                    else setChoosingFor(action);
                  }}
                >
                  <Zap />
                  <span className="truncate">{action.descriptor.title}</span>
                  <span className="truncate text-xs text-ink-3">{action.serviceTitle}</span>
                  {action.needsProject && !currentProject ? (
                    <CommandShortcut>
                      <ArrowRight className="size-3" />
                    </CommandShortcut>
                  ) : null}
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}
      </CommandList>
    </CommandDialog>
  );
}
