// Actions: the manifest's custom actions as forms built from their own
// params schema, the built-in project actions, build-artifact cleanup, and
// every job this project has run.
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, RefreshCw, Trash2, Wrench } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Link, useLocation } from "wouter";
import { ApiError, call, type Schemas } from "@/api/client";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { RelativeTime } from "@/components/data";
import { EmptyState, QueryState, Section } from "@/components/states";
import { Chip } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { bytes } from "@/lib/format";
import { ActionButton } from "@/schema/ActionButton";
import { SchemaForm } from "@/schema/SchemaForm";
import { defaultValue, type JsonSchema } from "@/schema/schema";
import { JobStatusMark } from "../../jobs/JobStatus";
import type { TabProps } from "../ProjectPage";

type ActionInfo = Schemas["ActionInfo"];

function CustomAction({ projectId, action }: { projectId: string; action: ActionInfo }) {
  const schema = (action.params ?? null) as JsonSchema | null;
  const [open, setOpen] = useState(false);
  const [params, setParams] = useState<unknown>({});
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const [, navigate] = useLocation();
  const needsDialog = Boolean(schema) || action.confirm || action.danger;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const job = await call("/api/services/actions/actions/run", {
        project_id: projectId,
        action_id: action.id,
        params: schema ? (params as Record<string, unknown>) : {},
      });
      setOpen(false);
      toast(`Started: ${job.title}`, {
        action: { label: "View job", onClick: () => navigate(`/jobs/${job.job_id}`) },
      });
      await queryClient.invalidateQueries();
    } catch (failure) {
      const message = failure instanceof ApiError ? failure.detail : String(failure);
      if (needsDialog) setError(message);
      else toast.error(`${action.label} failed`, { description: message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex items-center justify-between gap-3 py-2">
      <div className="min-w-0">
        <p className="flex items-center gap-2 text-sm font-medium">
          {action.label}
          {action.requires_service ? (
            <Chip title="Runs against this service, which must be running">
              needs {action.requires_service}
            </Chip>
          ) : null}
          {action.danger ? (
            <Chip className="border-signal/40 text-signal-ink">destructive</Chip>
          ) : null}
        </p>
        {action.description ? <p className="text-xs text-ink-3">{action.description}</p> : null}
      </div>
      <Button
        variant={action.danger ? "destructive" : "outline"}
        size="sm"
        disabled={busy}
        onClick={() => {
          if (!needsDialog) {
            void submit();
            return;
          }
          setParams(schema ? defaultValue(schema, schema) : {});
          setTyped("");
          setError(null);
          setOpen(true);
        }}
      >
        {busy ? <Loader2 className="animate-spin" /> : <Play />}
        Run
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-xl">
          <form
            className="flex flex-col gap-5"
            onSubmit={(event) => {
              event.preventDefault();
              if (!(action.danger && typed !== action.id)) void submit();
            }}
          >
            <DialogHeader>
              <DialogTitle>{action.label}</DialogTitle>
              {action.description ? (
                <DialogDescription>{action.description}</DialogDescription>
              ) : null}
            </DialogHeader>
            {schema ? (
              <SchemaForm schema={schema} root={schema} value={params} onChange={setParams} />
            ) : null}
            {action.danger ? (
              <label className="flex flex-col gap-1.5 text-sm">
                <span>
                  Type{" "}
                  <code className="rounded-sm bg-hover px-1 font-mono text-xs">{action.id}</code> to
                  confirm.
                </span>
                <Input value={typed} onChange={(event) => setTyped(event.target.value)} autoFocus />
              </label>
            ) : null}
            {error ? (
              <p
                role="alert"
                className="rounded-md border border-signal/40 bg-signal-wash px-3 py-2 text-sm text-signal-ink"
              >
                {error}
              </p>
            ) : null}
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant={action.danger ? "destructive" : "default"}
                disabled={busy || (action.danger && typed !== action.id)}
              >
                {busy ? <Loader2 className="animate-spin" /> : null}
                Run {action.label}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </li>
  );
}

function Artifacts({ projectId, isGit }: { projectId: string; isGit: boolean }) {
  // Without git nothing says which folders are disposable, so the user opts in explicitly.
  const [allowWithoutGit, setAllowWithoutGit] = useState(false);
  const sizes = useAction(
    "/api/services/actions/actions/artifact-sizes",
    { project_id: projectId, allow_without_git: allowWithoutGit },
    { follow: ["jobs"] },
  );
  const [chosen, setChosen] = useState<string[]>([]);
  if (!isGit && !allowWithoutGit) {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="text-sm text-ink-3">
          This project has no git, so nothing marks which folders are build output. Only folders you
          knowingly delete are offered.
        </p>
        <Button variant="outline" size="xs" onClick={() => setAllowWithoutGit(true)}>
          Look for build output anyway
        </Button>
      </div>
    );
  }
  return (
    <QueryState query={sizes}>
      {(data) =>
        data.artifacts.length ? (
          <div className="flex flex-col gap-2">
            <ul className="flex flex-col divide-y divide-rule">
              {data.artifacts.map((artifact) => (
                <li key={artifact.path} className="flex items-center gap-2 py-1">
                  <Checkbox
                    id={`artifact-${artifact.path}`}
                    checked={chosen.includes(artifact.path)}
                    onCheckedChange={(checked) =>
                      setChosen(
                        checked === true
                          ? [...chosen, artifact.path]
                          : chosen.filter((item) => item !== artifact.path),
                      )
                    }
                  />
                  <label
                    htmlFor={`artifact-${artifact.path}`}
                    className="flex min-w-0 flex-1 justify-between gap-2"
                  >
                    <span className="truncate font-mono text-xs">{artifact.path}</span>
                    <span className="figures shrink-0 text-xs">
                      {bytes(artifact.size_bytes)}{" "}
                      <span className="text-ink-3">{artifact.kind}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <div className="flex items-center justify-between">
              <span className="figures text-xs text-ink-3">
                {bytes(data.total_bytes)} in build output
              </span>
              <ActionButton
                service="actions"
                action="clean-artifacts"
                context={{ projectId }}
                input={{ paths: chosen, allow_without_git: allowWithoutGit }}
                disabled={!chosen.length}
                size="xs"
                variant="destructive"
                icon={<Trash2 />}
                label={chosen.length ? `Delete ${chosen.length}` : "Delete selected"}
                onDone={() => setChosen([])}
              />
            </div>
          </div>
        ) : (
          <p className="text-sm text-ink-3">
            No build output found: only folders git ignores entirely are offered.
          </p>
        )
      }
    </QueryState>
  );
}

export function ActionsTab({ project, detail }: TabProps) {
  const custom =
    useServiceState("actions")?.projects.find((item) => item.project_id === project.id)?.actions ??
    [];
  const jobs = useAction(
    "/api/services/jobs/actions/history",
    { project_id: project.id, kind: null, limit: 40 },
    { follow: ["jobs"] },
  );
  const context = { projectId: project.id };
  return (
    <div className="grid gap-8 xl:grid-cols-2">
      <div className="flex flex-col gap-8">
        <Section
          title="Project actions"
          description="Declared as [[actions]] in forge.project.toml."
        >
          {custom.length ? (
            <ul className="flex flex-col divide-y divide-rule">
              {custom.map((action) => (
                <CustomAction key={action.id} projectId={project.id} action={action} />
              ))}
            </ul>
          ) : (
            <EmptyState title="No custom actions">
              Declare scripts the project needs run (migrate, seed, deploy) as{" "}
              <code className="font-mono text-xs">[[actions]]</code>, with a JSON Schema for their
              parameters, and they appear here as forms.
            </EmptyState>
          )}
        </Section>
        <Section title="Built-in">
          <div className="flex flex-wrap gap-1.5">
            <ActionButton
              service="projects"
              action="refresh-project"
              context={context}
              size="sm"
              icon={<RefreshCw />}
              label="Refresh everything"
            />
            <ActionButton
              service="agents"
              action="update-dependencies"
              context={context}
              size="sm"
              icon={<Wrench />}
              label="Update dependencies…"
            />
            <ActionButton service="git" action="fetch" context={context} size="sm" label="Fetch" />
            <ActionButton
              service="checks"
              action="run"
              context={context}
              input={{ check_id: null, on_head: null }}
              size="sm"
              label="Run checks"
            />
          </div>
        </Section>
        <Section title="Build output">
          <Artifacts projectId={project.id} isGit={detail.is_git} />
        </Section>
      </div>
      <Section title="Job history">
        <QueryState query={jobs}>
          {(data) =>
            data.jobs.length ? (
              <ul className="flex flex-col divide-y divide-rule">
                {data.jobs.map((job) => (
                  <li key={job.id}>
                    <Link
                      href={`/jobs/${job.id}`}
                      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-1.5 hover:bg-hover"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm">{job.title}</span>
                        <span className="block truncate text-2xs text-ink-3">
                          {job.summary ?? job.error ?? job.kind}
                        </span>
                      </span>
                      <span className="flex items-center gap-2 text-2xs text-ink-3">
                        <JobStatusMark status={job.status} />
                        <RelativeTime at={job.created_at} />
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-ink-3">No jobs yet.</p>
            )
          }
        </QueryState>
      </Section>
    </div>
  );
}
