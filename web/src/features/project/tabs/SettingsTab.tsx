// Per-project overrides, each shown as its current value (rendered from the
// setting action's own schema) with an editor built from the same schema.
import { Pencil } from "lucide-react";
import { asSchema, useActionDescriptor } from "@/api/descriptors";
import { useServiceState } from "@/api/stream";
import { Section } from "@/components/states";
import { ActionButton } from "@/schema/ActionButton";
import { SchemaView } from "@/schema/SchemaView";
import { resolve } from "@/schema/schema";
import type { TabProps } from "../ProjectPage";

function SettingRow({
  projectId,
  service,
  action,
  field,
  value,
  title,
  description,
}: {
  projectId: string;
  service: string;
  action: string;
  field: string;
  value: unknown;
  title: string;
  description: string;
}) {
  const descriptor = useActionDescriptor(service, action);
  const root = descriptor ? asSchema(descriptor.input_schema) : null;
  const raw = root?.properties?.[field];
  return (
    <div className="grid gap-3 border-b border-rule py-3 last:border-b-0 md:grid-cols-[14rem_minmax(0,1fr)_auto]">
      <div>
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="text-xs text-ink-3">{description}</p>
      </div>
      <div className="min-w-0">
        {root && raw ? (
          <SchemaView schema={resolve(raw, root).schema} root={root} value={value} />
        ) : null}
      </div>
      <ActionButton
        service={service}
        action={action}
        context={{ projectId }}
        defaults={{ [field]: value }}
        prompt
        size="xs"
        icon={<Pencil />}
        label="Edit"
      />
    </div>
  );
}

export function SettingsTab({ project, detail }: TabProps) {
  const checks = useServiceState("checks")?.projects.find((item) => item.project_id === project.id);
  const metrics = useServiceState("metrics")?.projects.find(
    (item) => item.project_id === project.id,
  );
  const fork = useServiceState("forks")?.forks.find((item) => item.project_id === project.id);
  const overrides = Object.entries(metrics?.interval_overrides ?? {}).map(
    ([metric_id, interval_s]) => ({ metric_id, interval_s }),
  );
  return (
    <div className="flex max-w-5xl flex-col gap-8">
      <Section title="Dashboard settings for this project">
        <div className="flex flex-col">
          <SettingRow
            projectId={project.id}
            service="projects"
            action="save-settings"
            field="settings"
            value={detail.settings}
            title="Visibility and editor"
            description="Hide it from the overview, or open it in a different editor than the default."
          />
          {checks ? (
            <SettingRow
              projectId={project.id}
              service="checks"
              action="set-settings"
              field="settings"
              value={checks.settings}
              title="Check schedule"
              description="When the manifest checks run on their own, and whether on HEAD or the working tree."
            />
          ) : null}
          {detail.manifest?.metrics.length ? (
            <SettingRow
              projectId={project.id}
              service="metrics"
              action="set-intervals"
              field="overrides"
              value={overrides}
              title="Metric intervals"
              description="How often each metric is read; unlisted ones use the manifest's interval."
            />
          ) : null}
          {fork && (fork.is_fork || project.git?.has_upstream_remote) ? (
            <SettingRow
              projectId={project.id}
              service="forks"
              action="set-policy"
              field="policy"
              value={fork.policy}
              title="Fork policy"
              description="Upstream, strategy, schedule, conflict handling, checks, and notifications for syncs."
            />
          ) : null}
        </div>
      </Section>
    </div>
  );
}
