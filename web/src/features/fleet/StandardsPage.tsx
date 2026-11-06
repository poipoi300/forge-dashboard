// The fleet against forge's standards: every project's compliance, what
// fails, how far behind forge's template it is, and the one-click fix.
import { Ruler } from "lucide-react";
import { Link } from "wouter";
import { useServiceState } from "@/api/stream";
import { usePageTitle } from "@/app/title";
import { Meter, RelativeTime } from "@/components/data";
import { EmptyState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { ActionButton } from "@/schema/ActionButton";
import { useProjectLabel } from "../project/names";

export function StandardsPage() {
  usePageTitle("Standards");
  const standards = useServiceState("standards");
  const projects = useServiceState("projects")?.projects ?? [];
  const nameOf = useProjectLabel();
  const rows = (standards?.projects ?? [])
    .map((entry) => ({
      entry,
      project: projects.find((project) => project.id === entry.project_id),
    }))
    .filter((row) => row.project && row.project.role !== "standards-source")
    .sort((a, b) => {
      const ratio = (row: typeof a) =>
        row.entry.report?.total ? row.entry.report.passing / row.entry.report.total : -1;
      return ratio(a) - ratio(b);
    });

  return (
    <div className="max-w-[100rem] flex flex-col gap-6 px-5 py-4">
      <h1 className="sr-only">Standards</h1>
      <Section
        title="Standards"
        description={
          standards?.forge_repo
            ? `Measured against forge at ${standards.forge_head?.slice(0, 8) ?? "?"}: ${standards.forge_head_subject ?? ""}`
            : "The forge repo was not found; set it in Settings, under Discovery."
        }
      >
        {rows.length ? (
          <div className="overflow-x-auto rounded-md border border-rule bg-surface">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                  <th className="px-3 py-1.5 font-medium">project</th>
                  <th className="px-2 py-1.5 font-medium">compliance</th>
                  <th className="px-2 py-1.5 font-medium">failing</th>
                  <th className="px-2 py-1.5 text-right font-medium">forge commits since</th>
                  <th className="px-2 py-1.5 font-medium">checked</th>
                  <th className="px-2 py-1.5">
                    <span className="sr-only">fix</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ entry, project }) => {
                  if (!project) return null;
                  const report = entry.report;
                  const failing = report?.checks.filter((check) => check.result === "fail") ?? [];
                  return (
                    <tr
                      key={entry.project_id}
                      className="border-b border-rule align-top last:border-b-0"
                    >
                      <td className="px-3 py-1.5">
                        <Link
                          href={`/p/${project.id}/standards`}
                          className="font-medium hover:underline"
                        >
                          {nameOf(project.id)}
                        </Link>
                        {project.role === "unmanifested" ? (
                          <span className="block text-2xs text-amber-ink">no manifest</span>
                        ) : null}
                      </td>
                      <td className="w-48 px-2 py-1.5">
                        {report ? (
                          <span className="flex items-center gap-2">
                            <span className="figures w-10 text-xs font-semibold">
                              {report.passing}/{report.total}
                            </span>
                            <Meter
                              value={report.total ? report.passing / report.total : 0}
                              tone={report.passing === report.total ? "good" : "warn"}
                              label={`${project.name} compliance`}
                            />
                          </span>
                        ) : entry.error ? (
                          <StatusMark tone="bad">{entry.error}</StatusMark>
                        ) : (
                          <span className="text-ink-3">not checked</span>
                        )}
                      </td>
                      <td className="max-w-[22rem] px-2 py-1.5 text-xs text-ink-2">
                        {failing.length ? (
                          <ul className="flex flex-col">
                            {failing.slice(0, 3).map((check) => (
                              <li key={check.id} className="truncate">
                                {check.title}
                              </li>
                            ))}
                            {failing.length > 3 ? (
                              <li className="text-ink-3">and {failing.length - 3} more</li>
                            ) : null}
                          </ul>
                        ) : (
                          <span className="text-clear-ink">none</span>
                        )}
                      </td>
                      <td className="figures px-2 py-1.5 text-right">{entry.behind ?? "—"}</td>
                      <td className="px-2 py-1.5 text-xs">
                        <RelativeTime at={entry.evaluated_at} />
                      </td>
                      <td className="px-2 py-1 text-right">
                        {project.role === "unmanifested" ? (
                          <ActionButton
                            service="standards"
                            action="adopt"
                            context={{ projectId: project.id }}
                            size="xs"
                            icon={<Ruler />}
                            label="Adopt"
                          />
                        ) : failing.length || (entry.behind ?? 0) > 0 ? (
                          <ActionButton
                            service="standards"
                            action="bring-up"
                            context={{ projectId: project.id }}
                            size="xs"
                            icon={<Ruler />}
                            label="Bring up"
                          />
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="Nothing measured yet">
            Compliance is computed for every project once the forge repo is known.
          </EmptyState>
        )}
      </Section>
    </div>
  );
}
