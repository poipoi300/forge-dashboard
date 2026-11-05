// Launch and interface: how to run it, one button that does, the supervised
// services with their actual addresses and logs, and the deployments.
import { ExternalLink, Play, RotateCw, Square } from "lucide-react";
import { useState } from "react";
import { useServiceLog } from "@/api/logs";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { RelativeTime } from "@/components/data";
import { LogView } from "@/components/LogView";
import { Markdown } from "@/components/Markdown";
import { EmptyState, QueryState, Section } from "@/components/states";
import { InfoNote, StatusMark } from "@/components/status";
import { Button } from "@/components/ui/button";
import { duration, useNow } from "@/lib/time";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";
import type { TabProps } from "../ProjectPage";

const STATE_TONE: Record<string, Tone> = {
  stopped: "neutral",
  building: "info",
  starting: "info",
  running: "good",
  external: "good",
  backoff: "warn",
  stopping: "neutral",
  failed: "bad",
  "build failed": "bad",
  blocked: "bad",
};
const ACTIVE = new Set(["building", "starting", "running", "backoff", "stopping"]);

function Uptime({ startedAt }: { startedAt: number | null }) {
  const now = useNow();
  if (!startedAt) return <span className="text-ink-3">—</span>;
  return <span className="figures">{duration(Math.max(0, now / 1000 - startedAt))}</span>;
}

function ServiceLog({
  projectId,
  serviceId,
  label,
}: {
  projectId: string;
  serviceId: string;
  label: string;
}) {
  const log = useServiceLog(projectId, serviceId);
  return (
    <LogView
      lines={log.lines}
      label={`${label} log`}
      live
      className="h-80"
      emptyText="The service has not written anything yet."
    />
  );
}

export function LaunchTab({ project, detail }: TabProps) {
  const guide = useAction(
    "/api/services/projects/actions/launch-guide",
    { project_id: project.id },
    { live: true },
  );
  const state = useServiceState("services");
  const services = (state?.services ?? []).filter((service) => service.project_id === project.id);
  const deployments = (state?.deployments ?? []).filter(
    (deployment) => deployment.project_id === project.id,
  );
  const [logFor, setLogFor] = useState<string | null>(null);
  const context = { projectId: project.id };
  const primaryId = guide.data?.primary_service ?? services[0]?.service_id ?? null;
  const primary = services.find((service) => service.service_id === primaryId);
  const openService =
    logFor ?? services.find((service) => ACTIVE.has(service.state))?.service_id ?? null;

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <Section
        title="How to run it"
        actions={
          primary ? (
            primary.url && (primary.state === "running" || primary.state === "external") ? (
              <Button asChild size="sm">
                <a href={primary.url} target="_blank" rel="noreferrer">
                  <ExternalLink />
                  Open {primary.label}
                </a>
              </Button>
            ) : (
              <ActionButton
                service="services"
                action="launch"
                context={context}
                variant="default"
                icon={<Play />}
                label={`Launch ${primary.label}`}
                title="Start its dependencies, wait until it is ready, then open it"
              />
            )
          ) : null
        }
      >
        <QueryState query={guide}>
          {(data) =>
            data.markdown ? (
              <div className="flex flex-col gap-2">
                <InfoNote>
                  {data.source === "readme"
                    ? "From the README"
                    : data.source === "manifest-file"
                      ? "From the manifest's guide file"
                      : "From the manifest"}
                </InfoNote>
                <div className="rounded-md border border-rule bg-surface px-4 py-3">
                  <Markdown>{data.markdown}</Markdown>
                </div>
              </div>
            ) : (
              <EmptyState title="No launch instructions">
                {data.problem ? `${data.problem}. ` : ""}Add{" "}
                <code className="font-mono text-xs">[launch].instructions</code> to
                forge.project.toml, or a Setup or Run section to the README.
              </EmptyState>
            )
          }
        </QueryState>
      </Section>

      <div className="flex min-w-0 flex-col gap-6">
        <Section
          title="Services"
          description={services.length ? "Ports are the ones actually bound." : undefined}
          actions={
            services.length ? (
              <ActionButton
                service="services"
                action="scan-external"
                label="Find running instances"
                size="xs"
                variant="ghost"
              />
            ) : null
          }
        >
          {services.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                    <th className="px-2 py-1.5 font-medium">service</th>
                    <th className="px-2 py-1.5 font-medium">state</th>
                    <th className="px-2 py-1.5 font-medium">address</th>
                    <th className="px-2 py-1.5 text-right font-medium">uptime</th>
                    <th className="px-2 py-1.5 text-right font-medium">restarts</th>
                    <th className="px-2 py-1.5">
                      <span className="sr-only">actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {services.map((service) => {
                    const input = { service_id: service.service_id };
                    const active = ACTIVE.has(service.state);
                    return (
                      <tr
                        key={service.service_id}
                        className={cn(
                          "border-b border-rule align-top last:border-b-0",
                          openService === service.service_id && "bg-select",
                        )}
                      >
                        <td className="px-2 py-1.5">
                          <button
                            type="button"
                            className="text-left font-medium hover:underline"
                            onClick={() => setLogFor(service.service_id)}
                          >
                            {service.label}
                          </button>
                          {service.detail ? (
                            <p className="text-2xs text-ink-3">{service.detail}</p>
                          ) : null}
                          {service.blocked_by ? (
                            <p className="text-2xs text-signal-ink">
                              port held by {service.blocked_by}
                            </p>
                          ) : null}
                        </td>
                        <td className="px-2 py-1.5">
                          <StatusMark
                            tone={STATE_TONE[service.state] ?? "neutral"}
                            strong={STATE_TONE[service.state] === "bad"}
                          >
                            {service.state === "external"
                              ? `running outside (pid ${service.external?.pid ?? "?"})`
                              : service.state}
                          </StatusMark>
                        </td>
                        <td className="px-2 py-1.5">
                          {service.url ? (
                            <a
                              href={service.url}
                              target="_blank"
                              rel="noreferrer"
                              className="font-mono text-xs text-info-ink hover:underline"
                            >
                              {service.host}:{service.port}
                            </a>
                          ) : service.port ? (
                            <code className="font-mono text-xs">
                              {service.host}:{service.port}
                            </code>
                          ) : (
                            <span className="text-ink-3">—</span>
                          )}
                          <p className="text-2xs text-ink-3">{service.machine}</p>
                        </td>
                        <td className="px-2 py-1.5 text-right">
                          <Uptime
                            startedAt={service.state === "running" ? service.started_at : null}
                          />
                        </td>
                        <td className="figures px-2 py-1.5 text-right">{service.restarts}</td>
                        <td className="px-2 py-1 text-right">
                          <div className="flex justify-end gap-1">
                            {active || service.state === "external" ? (
                              <>
                                <ActionButton
                                  service="services"
                                  action="restart-service"
                                  context={context}
                                  input={input}
                                  size="icon-xs"
                                  icon={<RotateCw />}
                                  label={<span className="sr-only">Restart</span>}
                                  title={`Restart ${service.label}`}
                                />
                                <ActionButton
                                  service="services"
                                  action="stop-service"
                                  context={context}
                                  input={input}
                                  size="icon-xs"
                                  icon={<Square />}
                                  label={<span className="sr-only">Stop</span>}
                                  title={`Stop ${service.label}`}
                                />
                              </>
                            ) : (
                              <ActionButton
                                service="services"
                                action="start-service"
                                context={context}
                                input={input}
                                size="icon-xs"
                                icon={<Play />}
                                label={<span className="sr-only">Start</span>}
                                title={`Start ${service.label}`}
                              />
                            )}
                            {service.state === "blocked" ? (
                              <ActionButton
                                service="services"
                                action="release-port"
                                context={context}
                                input={input}
                                size="xs"
                                variant="destructive"
                                label="Free the port"
                                confirmText={String(service.port ?? "confirm")}
                              />
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No services declared">
              Declare long-running processes as{" "}
              <code className="font-mono text-xs">[[services]]</code> in forge.project.toml and the
              dashboard starts, supervises, and opens them.
            </EmptyState>
          )}
          {openService ? (
            <ServiceLog
              key={openService}
              projectId={project.id}
              serviceId={openService}
              label={
                services.find((service) => service.service_id === openService)?.label ?? openService
              }
            />
          ) : null}
        </Section>

        {deployments.length || detail.manifest?.deployments.length ? (
          <Section
            title="Deployments"
            actions={
              <ActionButton
                service="services"
                action="check-deployments"
                label="Check now"
                size="xs"
                variant="ghost"
              />
            }
          >
            <table className="w-full text-sm">
              <tbody>
                {deployments.map((deployment) => (
                  <tr
                    key={deployment.deployment_id}
                    className="border-b border-rule last:border-b-0"
                  >
                    <td className="px-2 py-1.5 font-medium">
                      {deployment.label}
                      <span className="ml-1.5 text-2xs text-ink-3">{deployment.environment}</span>
                    </td>
                    <td className="px-2 py-1.5">
                      <StatusMark
                        tone={
                          deployment.health === "up"
                            ? "good"
                            : deployment.health === "down"
                              ? "bad"
                              : "neutral"
                        }
                        strong={deployment.health === "down"}
                        title={deployment.error ?? undefined}
                      >
                        {deployment.health}
                        {deployment.status_code ? ` (${deployment.status_code})` : ""}
                      </StatusMark>
                    </td>
                    <td className="px-2 py-1.5">
                      <a
                        href={deployment.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-info-ink hover:underline"
                      >
                        {deployment.url.replace(/^https?:\/\//, "")}
                      </a>
                    </td>
                    <td className="px-2 py-1.5 text-xs text-ink-3">{deployment.machine ?? ""}</td>
                    <td className="px-2 py-1.5 text-right text-xs text-ink-3">
                      checked <RelativeTime at={deployment.checked_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        ) : null}
      </div>
    </div>
  );
}
