// The overview as a timetable: one row per project, columns from the
// summary schema (`ProjectsState.projects` columns, or the viewer's choice).
import { Star } from "lucide-react";
import { Link } from "wouter";
import type { Schemas } from "@/api/client";
import { cn } from "@/lib/utils";
import { SchemaView } from "@/schema/SchemaView";
import { labelOf } from "@/schema/schema";
import { QuickActions } from "../project/QuickActions";
import type { Group } from "./filters";
import type { SummarySchema } from "./summarySchema";

type ProjectSummary = Schemas["ProjectSummary"];

export function ProjectTable({
  groups,
  layout,
  columns,
  selectedId,
  onSelect,
  registerRow,
}: {
  groups: Group[];
  layout: SummarySchema;
  columns: string[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  registerRow: (id: string, element: HTMLElement | null) => void;
}) {
  const chosen = columns.flatMap((name) =>
    layout.fields.filter((field) => field.name === name && name !== "name"),
  );
  const span = chosen.length + 2;
  return (
    <div className="overflow-x-auto rounded-md border border-rule bg-surface">
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 z-10 bg-surface">
          <tr className="border-b border-rule-strong text-left">
            <th scope="col" className="px-3 py-2 text-xs font-medium text-ink-3">
              project
            </th>
            {chosen.map((field) => (
              <th
                key={field.name}
                scope="col"
                className="whitespace-nowrap px-2 py-2 text-xs font-medium text-ink-3"
              >
                {labelOf(field.name, field.schema, true)}
              </th>
            ))}
            <th scope="col" className="px-2 py-2 text-right text-xs font-medium text-ink-3">
              <span className="sr-only">actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => [
            group.label ? (
              <tr key={`group-${group.key}`} className="bg-surface-sunk">
                <th
                  colSpan={span}
                  scope="colgroup"
                  className="px-3 py-1 text-left text-2xs font-semibold text-ink-2"
                >
                  {group.label}{" "}
                  <span className="figures font-normal text-ink-3">{group.projects.length}</span>
                </th>
              </tr>
            ) : null,
            ...group.projects.map((project: ProjectSummary) => (
              <tr
                key={project.id}
                ref={(element) => registerRow(project.id, element)}
                tabIndex={-1}
                onFocus={() => onSelect(project.id)}
                data-selected={selectedId === project.id || undefined}
                className={cn(
                  "border-b border-rule align-top outline-none last:border-b-0 hover:bg-hover data-[selected]:bg-select",
                  project.health.level === "failing" && "shadow-[inset_3px_0_0_var(--signal)]",
                )}
              >
                <td className="max-w-[16rem] px-3 py-1.5 compact:py-1">
                  <div className="flex items-center gap-1.5">
                    {project.favorite ? (
                      <Star
                        aria-label="Pinned"
                        className="size-3 shrink-0 text-amber"
                        fill="currentColor"
                      />
                    ) : null}
                    <Link
                      href={`/p/${project.id}`}
                      className="truncate font-semibold text-ink hover:underline"
                    >
                      {project.name}
                    </Link>
                    <code className="shrink-0 font-mono text-2xs text-ink-3">{project.id}</code>
                  </div>
                  {project.summary ? (
                    <p className="truncate text-2xs text-ink-3 compact:hidden">{project.summary}</p>
                  ) : null}
                </td>
                {chosen.map((field) => (
                  <td
                    key={field.name}
                    className="max-w-[20rem] px-2 py-1.5 whitespace-nowrap compact:py-1"
                  >
                    <SchemaView
                      schema={field.schema}
                      root={layout.root}
                      value={(project as unknown as Record<string, unknown>)[field.name]}
                      dense
                    />
                  </td>
                ))}
                <td className="px-2 py-1 text-right">
                  <div className="flex justify-end">
                    <QuickActions project={project} />
                  </div>
                </td>
              </tr>
            )),
          ])}
        </tbody>
      </table>
    </div>
  );
}
