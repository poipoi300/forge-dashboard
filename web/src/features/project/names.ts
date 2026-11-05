// Project names for lists that mix projects: a name shared by two projects
// (a clone, an export) carries its id so the two stay distinguishable.
import { useMemo } from "react";
import { useServiceState } from "@/api/stream";

export function useProjectLabel(): (projectId: string | null | undefined) => string {
  const projects = useServiceState("projects")?.projects;
  return useMemo(() => {
    const list = projects ?? [];
    const counts = new Map<string, number>();
    for (const project of list) counts.set(project.name, (counts.get(project.name) ?? 0) + 1);
    const labels = new Map(
      list.map((project) => [
        project.id,
        (counts.get(project.name) ?? 0) > 1 ? `${project.name} (${project.id})` : project.name,
      ]),
    );
    return (projectId) => (projectId ? (labels.get(projectId) ?? projectId) : "");
  }, [projects]);
}
