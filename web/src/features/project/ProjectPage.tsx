// A project's page: the header answers what it is and how it is doing, the
// tabs hold one concern each. The tab is part of the URL so every view links.
import { ExternalLink, FileText, House } from "lucide-react";
import type { ComponentType } from "react";
import { Link, useLocation } from "wouter";
import type { Schemas } from "@/api/client";
import { useAction } from "@/api/query";
import { useServiceState } from "@/api/stream";
import { PathText, RelativeTime } from "@/components/data";
import { GithubMark } from "@/components/icons";
import { EmptyState, ErrorState, LoadingRows } from "@/components/states";
import { StatusMark } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useActionRunner } from "@/schema/ActionButton";
import { SchemaView } from "@/schema/SchemaView";
import { useSummarySchema } from "../overview/summarySchema";
import { QuickActions } from "./QuickActions";
import { ActionsTab } from "./tabs/ActionsTab";
import { ActivityTab } from "./tabs/ActivityTab";
import { AgentsTab } from "./tabs/AgentsTab";
import { ForkTab } from "./tabs/ForkTab";
import { GitTab } from "./tabs/GitTab";
import { IssuesTab } from "./tabs/IssuesTab";
import { LaunchTab } from "./tabs/LaunchTab";
import { MetricsTab } from "./tabs/MetricsTab";
import { NotesTab } from "./tabs/NotesTab";
import { QualityTab } from "./tabs/QualityTab";
import { SettingsTab } from "./tabs/SettingsTab";
import { StandardsTab } from "./tabs/StandardsTab";

export type ProjectSummary = Schemas["ProjectSummary"];
export type ProjectDetail = Schemas["ProjectDetail"];

export interface TabProps {
  project: ProjectSummary;
  detail: ProjectDetail;
}

interface TabSpec {
  id: string;
  label: string;
  component: ComponentType<TabProps>;
  shown?: (project: ProjectSummary, detail: ProjectDetail) => boolean;
}

const TABS: TabSpec[] = [
  { id: "launch", label: "Launch", component: LaunchTab },
  { id: "quality", label: "Quality", component: QualityTab },
  {
    id: "issues",
    label: "Issues",
    component: IssuesTab,
    shown: (project) => project.issues?.github !== "not-on-github",
  },
  { id: "git", label: "Git", component: GitTab, shown: (_project, detail) => detail.is_git },
  {
    id: "fork",
    label: "Fork",
    component: ForkTab,
    shown: (project, detail) =>
      detail.is_git &&
      (Boolean(project.fork?.is_fork) || Boolean(project.git?.has_upstream_remote)),
  },
  { id: "standards", label: "Standards", component: StandardsTab },
  {
    id: "metrics",
    label: "Metrics",
    component: MetricsTab,
    shown: (_project, detail) => Boolean(detail.manifest?.metrics.length),
  },
  { id: "actions", label: "Actions", component: ActionsTab },
  { id: "agents", label: "Agents", component: AgentsTab },
  { id: "activity", label: "Activity", component: ActivityTab },
  { id: "notes", label: "Notes", component: NotesTab },
  { id: "settings", label: "Settings", component: SettingsTab },
];

const LIFECYCLE = [
  "idea",
  "planning",
  "active",
  "blocked",
  "paused",
  "maintenance",
  "shipped",
  "archived",
] as const;

function StatusEditor({ project }: { project: ProjectSummary }) {
  const run = useActionRunner();
  const layout = useSummarySchema();
  const field = layout?.fields.find((item) => item.name === "status");
  return (
    <button
      type="button"
      onClick={() =>
        run({
          service: "projects",
          action: "set-status",
          context: { projectId: project.id },
          input: { status: project.status, note: project.status_note ?? null },
          prompt: true,
        })
      }
      title={
        project.status_note ? `${project.status}: ${project.status_note}` : "Set the status marker"
      }
      className="inline-flex items-center gap-1.5 rounded-sm border border-rule px-1.5 py-0.5 hover:border-rule-strong hover:bg-hover"
    >
      {field && layout ? (
        <SchemaView schema={field.schema} root={layout.root} value={project.status} dense />
      ) : (
        project.status
      )}
      {project.status_note ? (
        <span className="max-w-[16rem] truncate text-2xs text-ink-3">{project.status_note}</span>
      ) : null}
      <span className="sr-only">(change; one of {LIFECYCLE.join(", ")})</span>
    </button>
  );
}

function HealthLine({ project }: { project: ProjectSummary }) {
  const layout = useSummarySchema();
  const field = layout?.fields.find((item) => item.name === "health");
  if (!field || !layout) return null;
  return <SchemaView schema={field.schema} root={layout.root} value={project.health} dense />;
}

function Header({ project, detail }: TabProps) {
  const links = [
    detail.links.github ? { href: detail.links.github, label: "GitHub", icon: GithubMark } : null,
    detail.links.homepage ? { href: detail.links.homepage, label: "Homepage", icon: House } : null,
    detail.links.docs ? { href: detail.links.docs, label: "Docs", icon: FileText } : null,
  ].filter((link) => link !== null);
  return (
    <header className="flex flex-col gap-2 border-b border-rule-strong bg-surface px-5 pt-4 pb-3">
      <nav aria-label="Breadcrumb" className="text-2xs text-ink-3">
        <Link href="/" className="hover:text-ink hover:underline">
          Overview
        </Link>
        <span aria-hidden> / </span>
        <span>{project.id}</span>
      </nav>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h1 className="text-xl font-semibold tracking-tight text-ink">{project.name}</h1>
            <StatusEditor project={project} />
            <HealthLine project={project} />
          </div>
          {project.summary ? (
            <p className="max-w-3xl text-sm text-ink-2">{project.summary}</p>
          ) : null}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-3">
            <PathText path={project.path} />
            <span>on {project.machine}</span>
            <span>
              changed <RelativeTime at={project.last_change.at} />
              {project.last_change.source === "working-tree" ? " (uncommitted edits)" : ""}
            </span>
            {project.role === "unmanifested" ? (
              <StatusMark tone="warn">no manifest</StatusMark>
            ) : null}
            {links.map((link) => (
              <a
                key={link.href}
                href={link.href}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-info-ink hover:underline"
              >
                <link.icon className="size-3" />
                {link.label}
                <ExternalLink aria-hidden className="size-2.5" />
              </a>
            ))}
          </div>
        </div>
        <QuickActions project={project} size="sm" />
      </div>
      {detail.manifest_issues.length ? (
        <div
          role="alert"
          className="rounded-sm border border-signal/30 bg-signal-wash px-3 py-1.5 text-xs text-signal-ink"
        >
          forge.project.toml has {detail.manifest_issues.length} problem
          {detail.manifest_issues.length === 1 ? "" : "s"}:{" "}
          {detail.manifest_issues
            .slice(0, 3)
            .map(
              (issue) =>
                `${issue.location || "file"}${issue.line ? ` (line ${issue.line})` : ""}: ${issue.message}`,
            )
            .join("; ")}
        </div>
      ) : null}
    </header>
  );
}

export function ProjectPage({ projectId, tab }: { projectId: string; tab: string | undefined }) {
  const [, navigate] = useLocation();
  const projects = useServiceState("projects");
  const project = projects?.projects.find((item) => item.id === projectId);
  const detail = useAction(
    "/api/services/projects/actions/detail",
    { project_id: projectId },
    { live: true, enabled: Boolean(project) },
  );

  if (!projects?.loaded) return <LoadingRows rows={6} className="p-5" />;
  if (!project) {
    return (
      <div className="p-5">
        <EmptyState
          title={`No project "${projectId}"`}
          action={
            <Button asChild variant="outline" size="sm">
              <Link href="/">Back to the overview</Link>
            </Button>
          }
        >
          It may be hidden, ignored, or gone from disk. Hidden projects are listed in Settings.
        </EmptyState>
      </div>
    );
  }
  if (detail.error)
    return (
      <ErrorState className="m-5" error={detail.error} onRetry={() => void detail.refetch()} />
    );
  if (!detail.data) return <LoadingRows rows={6} className="p-5" />;

  const data = detail.data;
  const tabs = TABS.filter((spec) => spec.shown?.(project, data) ?? true);
  const current = tabs.find((spec) => spec.id === tab) ?? tabs[0];
  if (!current) return null;
  const Body = current.component;

  return (
    <div className="flex flex-col">
      <Header project={project} detail={data} />
      <Tabs
        value={current.id}
        onValueChange={(value) => navigate(`/p/${project.id}/${value}`)}
        className="gap-0"
      >
        <div className="sticky top-10 z-10 overflow-x-auto border-b border-rule bg-paper px-5">
          <TabsList className="h-9 gap-1 rounded-none bg-transparent p-0">
            {tabs.map((spec) => (
              <TabsTrigger
                key={spec.id}
                value={spec.id}
                className="h-9 flex-none rounded-none border-0 border-b-2 border-transparent px-2.5 text-sm text-ink-2 shadow-none data-[state=active]:border-ink data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:shadow-none"
              >
                {spec.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
      </Tabs>
      <div className="px-5 py-4">
        <Body project={project} detail={data} />
      </div>
    </div>
  );
}
