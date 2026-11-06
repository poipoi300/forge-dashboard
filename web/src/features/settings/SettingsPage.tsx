// Settings: every service's configuration as a form built from its config
// schema, plus GitHub sign-in, backups, report export, hidden projects, and
// the widget gallery that exercises the schema renderer end to end.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, KeyRound, Loader2, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { ApiError, getConfig, putConfig } from "@/api/client";
import { asSchema, type ServiceDescriptor, useDescriptors } from "@/api/descriptors";
import { useAction } from "@/api/query";
import { useAnyServiceState, useServiceState } from "@/api/stream";
import { usePageTitle } from "@/app/title";
import { RelativeTime } from "@/components/data";
import { ErrorState, LoadingRows, QueryState, Section } from "@/components/states";
import { StatusMark } from "@/components/status";
import { Button } from "@/components/ui/button";
import { bytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ActionButton } from "@/schema/ActionButton";
import { SchemaForm } from "@/schema/SchemaForm";
import { SchemaView } from "@/schema/SchemaView";
import { fields, type JsonSchema } from "@/schema/schema";

function ServiceConfig({ descriptor }: { descriptor: ServiceDescriptor }) {
  const schema = asSchema(descriptor.config_schema);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["config", descriptor.id],
    queryFn: () => getConfig(descriptor.id),
  });
  const [draft, setDraft] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (query.data) setDraft(query.data);
  }, [query.data]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(query.data);

  if (query.error) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.data || draft === null) return <LoadingRows rows={3} />;
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const saved = await putConfig(descriptor.id, draft);
      queryClient.setQueryData(["config", descriptor.id], saved);
      toast(`${descriptor.title} settings saved`);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : String(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <SchemaForm schema={schema} root={schema} value={draft} onChange={setDraft} />
      {error ? (
        <p
          role="alert"
          className="rounded-md border border-signal/40 bg-signal-wash px-3 py-2 text-sm text-signal-ink"
        >
          {error}
        </p>
      ) : null}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={!dirty || busy}>
          {busy ? <Loader2 className="animate-spin" /> : <Save />}
          Save
        </Button>
        {dirty ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setDraft(query.data)}>
            Revert
          </Button>
        ) : null}
      </div>
    </form>
  );
}

function GitHubAuth() {
  const github = useServiceState("github");
  if (!github) return <LoadingRows rows={2} />;
  return (
    <div className="flex flex-col gap-3">
      <p className="flex flex-wrap items-center gap-3 text-sm">
        <StatusMark tone={github.authenticated ? "good" : "warn"} strong={!github.authenticated}>
          {github.authenticated ? "signed in" : "not signed in"}
        </StatusMark>
        <span className="text-xs text-ink-3">
          token from {String(github.auth_source).replaceAll("-", " ")}
        </span>
        {github.rate_remaining !== null && github.rate_limit !== null ? (
          <span className="figures text-xs text-ink-3">
            {github.rate_remaining} of {github.rate_limit} requests left
            {github.rate_reset_at ? (
              <>
                , resets <RelativeTime at={github.rate_reset_at} />
              </>
            ) : null}
          </span>
        ) : null}
      </p>
      <p className="text-xs text-ink-3">
        The token is found without asking: GITHUB_TOKEN, then git's credential helper, then the
        system keyring. Save one here to keep it in the keyring.
      </p>
      <div className="flex gap-1.5">
        <ActionButton
          service="github"
          action="save-token"
          size="sm"
          icon={<KeyRound />}
          label="Save a token"
        />
        <ActionButton
          service="github"
          action="clear-token"
          size="sm"
          variant="ghost"
          label="Forget the stored token"
        />
      </div>
    </div>
  );
}

function Backups() {
  const settings = useServiceState("settings");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1.5">
        <ActionButton service="settings" action="backup" size="sm" label="Back up now" />
        <ActionButton
          service="settings"
          action="export-report"
          size="sm"
          icon={<Download />}
          label="Export report…"
        />
        <Button asChild variant="ghost" size="sm">
          <a href="/api/report.md" download>
            Download report (Markdown)
          </a>
        </Button>
      </div>
      {settings?.backups.length ? (
        <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule bg-surface">
          {settings.backups.map((backup) => (
            <li key={backup.name} className="flex items-center justify-between gap-3 px-3 py-1.5">
              <span className="min-w-0">
                <code className="block truncate font-mono text-xs">{backup.name}</code>
                <span className="figures text-2xs text-ink-3">
                  {bytes(backup.size_bytes)} · <RelativeTime at={backup.created_at} />
                </span>
              </span>
              <ActionButton
                service="settings"
                action="restore"
                input={{ name: backup.name }}
                size="xs"
                variant="ghost"
                label="Restore"
                confirmText={backup.name}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-ink-3">No backups yet.</p>
      )}
    </div>
  );
}

function HiddenProjects() {
  const projects = useServiceState("projects");
  const all = useAction("/api/services/projects/actions/list-all", {}, { live: true });
  const hidden = projects?.hidden ?? [];
  if (!hidden.length) return <p className="text-sm text-ink-3">No hidden projects.</p>;
  return (
    <QueryState query={all}>
      {(data) => (
        <ul className="flex flex-col divide-y divide-rule">
          {hidden.map((id) => {
            const project = data.projects.find((item) => item.id === id);
            return (
              <li key={id} className="flex items-center justify-between gap-3 py-1.5">
                <span className="text-sm">{project?.name ?? id}</span>
                <ActionButton
                  service="projects"
                  action="save-settings"
                  context={{ projectId: id }}
                  input={{ settings: { hidden: false, editor_command: [] } }}
                  size="xs"
                  label="Show again"
                />
              </li>
            );
          })}
        </ul>
      )}
    </QueryState>
  );
}

function Gallery({ descriptor }: { descriptor: ServiceDescriptor }) {
  const state = useAnyServiceState("gallery");
  const stateSchema = asSchema(descriptor.state_schema);
  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-ink-3">
        A service that exists to exercise the schema renderer: its state shows every display format,
        and its actions every dialog kind.
      </p>
      {state ? <SchemaView schema={stateSchema} root={stateSchema} value={state} /> : null}
      <div className="flex flex-wrap gap-1.5">
        {descriptor.actions.map((action) => (
          <ActionButton key={action.id} service="gallery" action={action.id} size="xs" />
        ))}
      </div>
    </div>
  );
}

const SPECIAL = new Set(["gallery"]);

export function SettingsPage() {
  usePageTitle("Settings");
  const descriptors = useDescriptors();
  const configurable = useMemo(
    () =>
      [...(descriptors?.values() ?? [])].filter((descriptor) => {
        const schema = asSchema(descriptor.config_schema) as JsonSchema;
        return !SPECIAL.has(descriptor.id) && fields(schema, schema).length > 0;
      }),
    [descriptors],
  );
  const [active, setActive] = useState<string>("discovery");
  const gallery = descriptors?.get("gallery");
  const sections = [
    ...configurable.map((descriptor) => ({ id: descriptor.id, label: descriptor.title })),
    { id: "github-auth", label: "GitHub sign-in" },
    { id: "backups", label: "Backups and reports" },
    { id: "hidden", label: "Hidden projects" },
    ...(gallery ? [{ id: "gallery", label: "Widget gallery" }] : []),
  ];
  const current = sections.find((section) => section.id === active) ?? sections[0];
  const descriptor = current ? descriptors?.get(current.id) : undefined;

  if (!descriptors) return <LoadingRows rows={8} className="p-5" />;
  return (
    <div className="max-w-[100rem] grid gap-6 px-5 py-4 md:grid-cols-[13rem_minmax(0,1fr)]">
      <h1 className="sr-only">Settings</h1>
      <nav
        aria-label="Settings sections"
        className="flex flex-col gap-0.5 md:sticky md:top-14 md:self-start"
      >
        {sections.map((section) => (
          <button
            key={section.id}
            type="button"
            onClick={() => setActive(section.id)}
            aria-current={current?.id === section.id ? "page" : undefined}
            className={cn(
              "rounded-sm px-2.5 py-1 text-left text-sm text-ink-2 hover:bg-hover hover:text-ink",
              current?.id === section.id && "bg-select text-ink",
            )}
          >
            {section.label}
          </button>
        ))}
      </nav>
      <div className="max-w-3xl">
        {current ? (
          <Section
            title={current.label}
            description={
              descriptor && !SPECIAL.has(descriptor.id) ? descriptor.description : undefined
            }
          >
            {current.id === "github-auth" ? (
              <GitHubAuth />
            ) : current.id === "backups" ? (
              <Backups />
            ) : current.id === "hidden" ? (
              <HiddenProjects />
            ) : current.id === "gallery" && gallery ? (
              <Gallery descriptor={gallery} />
            ) : descriptor ? (
              <ServiceConfig key={descriptor.id} descriptor={descriptor} />
            ) : null}
          </Section>
        ) : null}
      </div>
    </div>
  );
}
