// Every supervised service on this machine with its real address, the
// machine itself (CPU, memory, GPUs, disks), and the port map with conflicts.
import { Square } from "lucide-react";
import { Link } from "wouter";
import { useServiceState } from "@/api/stream";
import { Meter, RelativeTime } from "@/components/data";
import { EmptyState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { bytes, percent } from "@/lib/format";
import { ActionButton } from "@/schema/ActionButton";
import type { Tone } from "@/schema/schema";
import { useProjectLabel } from "../project/names";

const STATE_TONE: Record<string, Tone> = {
  running: "good",
  external: "good",
  starting: "info",
  building: "info",
  backoff: "warn",
  failed: "bad",
  "build failed": "bad",
  blocked: "bad",
  stopped: "neutral",
  stopping: "neutral",
};

function usageTone(ratio: number): Tone {
  return ratio >= 0.9 ? "bad" : ratio >= 0.75 ? "warn" : "info";
}

function Gauge({
  label,
  used,
  total,
  text,
}: {
  label: string;
  used: number;
  total: number;
  text: string;
}) {
  const ratio = total ? used / total : 0;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-ink-3">{label}</span>
        <span className="figures text-sm font-semibold">{text}</span>
      </div>
      <Meter value={ratio} tone={usageTone(ratio)} label={`${label} ${percent(ratio * 100)}`} />
    </div>
  );
}

export function ServicesPage() {
  const services = useServiceState("services");
  const machine = useServiceState("machine");
  const nameOf = useProjectLabel();
  const list = services?.services ?? [];
  const live = list.filter((service) => service.state !== "stopped");

  return (
    <div className="flex flex-col gap-8 px-5 py-4">
      <Section
        title="Services"
        description={`${live.length} of ${list.length} declared services are up or starting`}
        actions={
          live.length ? (
            <ActionButton
              service="services"
              action="stop-all"
              size="xs"
              variant="ghost"
              icon={<Square />}
              label="Stop all"
            />
          ) : null
        }
      >
        {list.length ? (
          <div className="overflow-x-auto rounded-md border border-rule bg-surface">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-rule-strong text-left text-xs text-ink-3">
                  <th className="px-3 py-1.5 font-medium">service</th>
                  <th className="px-2 py-1.5 font-medium">project</th>
                  <th className="px-2 py-1.5 font-medium">state</th>
                  <th className="px-2 py-1.5 font-medium">address</th>
                  <th className="px-2 py-1.5 text-right font-medium">pid</th>
                  <th className="px-2 py-1.5 text-right font-medium">restarts</th>
                </tr>
              </thead>
              <tbody>
                {list.map((service) => (
                  <tr
                    key={`${service.project_id}:${service.service_id}`}
                    className="border-b border-rule last:border-b-0"
                  >
                    <td className="px-3 py-1.5 font-medium">{service.label}</td>
                    <td className="px-2 py-1.5">
                      <Link href={`/p/${service.project_id}/launch`} className="hover:underline">
                        {nameOf(service.project_id)}
                      </Link>
                    </td>
                    <td className="px-2 py-1.5">
                      <StatusMark
                        tone={STATE_TONE[service.state] ?? "neutral"}
                        strong={STATE_TONE[service.state] === "bad"}
                        title={service.detail}
                      >
                        {service.state}
                      </StatusMark>
                    </td>
                    <td className="px-2 py-1.5 font-mono text-xs">
                      {service.url ? (
                        <a
                          href={service.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-info-ink hover:underline"
                        >
                          {service.host}:{service.port}
                        </a>
                      ) : service.port ? (
                        `${service.host}:${service.port}`
                      ) : (
                        <span className="text-ink-3">—</span>
                      )}
                    </td>
                    <td className="figures px-2 py-1.5 text-right text-xs">
                      {service.pid ?? service.external?.pid ?? "—"}
                    </td>
                    <td className="figures px-2 py-1.5 text-right text-xs">{service.restarts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No services declared">
            Projects declare their servers and workers as [[services]] in forge.project.toml.
          </EmptyState>
        )}
      </Section>

      {machine ? (
        <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          <Section
            title={machine.hostname}
            description={`${machine.operating_system}${machine.cpu_count ? ` · ${machine.cpu_count} cores` : ""}`}
            actions={
              <ActionButton
                service="machine"
                action="refresh"
                size="xs"
                variant="ghost"
                label="Sample now"
              />
            }
          >
            <div className="flex flex-col gap-3">
              {machine.cpu_percent !== null ? (
                <Gauge
                  label="CPU"
                  used={machine.cpu_percent}
                  total={100}
                  text={percent(machine.cpu_percent)}
                />
              ) : null}
              {machine.ram_used !== null && machine.ram_total ? (
                <Gauge
                  label="Memory"
                  used={machine.ram_used}
                  total={machine.ram_total}
                  text={`${bytes(machine.ram_used)} of ${bytes(machine.ram_total)}`}
                />
              ) : null}
              {machine.gpus.map((gpu) => (
                <div key={gpu.name} className="flex flex-col gap-2">
                  {gpu.load_percent !== null ? (
                    <Gauge
                      label={gpu.name}
                      used={gpu.load_percent}
                      total={100}
                      text={percent(gpu.load_percent)}
                    />
                  ) : null}
                  {gpu.memory_used !== null && gpu.memory_total ? (
                    <Gauge
                      label={`${gpu.name} memory`}
                      used={gpu.memory_used}
                      total={gpu.memory_total}
                      text={`${bytes(gpu.memory_used)} of ${bytes(gpu.memory_total)}`}
                    />
                  ) : null}
                </div>
              ))}
              {machine.gpu_error ? (
                <p className="text-2xs text-ink-3">GPU: {machine.gpu_error}</p>
              ) : null}
              {machine.disks.map((disk) =>
                disk.total ? (
                  <div key={disk.mount} className="flex flex-col gap-0.5">
                    <Gauge
                      label={`Disk ${disk.mount}`}
                      used={disk.total - (disk.free ?? 0)}
                      total={disk.total}
                      text={`${bytes(disk.free ?? 0)} free`}
                    />
                    {disk.project_ids.length ? (
                      <span className="text-2xs text-ink-3">
                        {disk.project_ids.length} projects here
                      </span>
                    ) : null}
                  </div>
                ) : null,
              )}
              <p className="text-2xs text-ink-3">
                sampled <RelativeTime at={machine.hardware_sampled_at} />
              </p>
            </div>
          </Section>

          <Section
            title="Ports"
            description={machine.ports_sampled_at ? undefined : "Not sampled yet."}
          >
            {machine.conflicts.length ? (
              <ul className="flex flex-col gap-1 rounded-md border border-signal/30 bg-signal-wash px-3 py-2">
                {machine.conflicts.map((conflict) => (
                  <li key={`${conflict.port}-${conflict.kind}`} className="text-xs text-signal-ink">
                    port {conflict.port}:{" "}
                    {conflict.kind === "multiple-listeners"
                      ? `${conflict.pids.length} processes listen (${conflict.pids.join(", ")})`
                      : `claimed by ${conflict.project_ids.map((id) => nameOf(id)).join(" and ")}`}
                  </li>
                ))}
              </ul>
            ) : null}
            <div className="max-h-[28rem] overflow-auto rounded-md border border-rule bg-surface">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-surface">
                  <tr className="border-b border-rule-strong text-left text-ink-3">
                    <th className="px-3 py-1 font-medium">port</th>
                    <th className="px-2 py-1 font-medium">address</th>
                    <th className="px-2 py-1 font-medium">process</th>
                    <th className="px-2 py-1 font-medium">project</th>
                  </tr>
                </thead>
                <tbody>
                  {machine.ports.map((entry) => (
                    <tr
                      key={`${entry.address}:${entry.port}:${entry.pid}`}
                      className="border-b border-rule last:border-b-0"
                    >
                      <td className="figures px-3 py-0.5 font-semibold">{entry.port}</td>
                      <td className="px-2 py-0.5 font-mono text-2xs">{entry.address}</td>
                      <td className="px-2 py-0.5">
                        {entry.process ?? "?"}
                        {entry.pid !== null ? (
                          <span className="text-ink-3"> ({entry.pid})</span>
                        ) : null}
                      </td>
                      <td className="px-2 py-0.5">
                        {entry.project_id ? (
                          <Link href={`/p/${entry.project_id}`} className="hover:underline">
                            {nameOf(entry.project_id)}
                          </Link>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      ) : null}
    </div>
  );
}
