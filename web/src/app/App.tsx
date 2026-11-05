// Routes. Every view is addressable: overview filters live in the query
// string, a project's tab and a job's id in the path.
import { Link, Route, Switch } from "wouter";
import { EmptyState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { ActivityPage } from "@/features/activity/ActivityPage";
import { ForksPage } from "@/features/fleet/ForksPage";
import { RunsPage } from "@/features/fleet/RunsPage";
import { ServicesPage } from "@/features/fleet/ServicesPage";
import { StandardsPage } from "@/features/fleet/StandardsPage";
import { JobsPage } from "@/features/jobs/JobsPage";
import { Overview } from "@/features/overview/Overview";
import { ProjectPage } from "@/features/project/ProjectPage";
import { SettingsPage } from "@/features/settings/SettingsPage";
import { ActionDialogHost } from "@/schema/ActionButton";
import { CommandPalette } from "./CommandPalette";
import { Shell } from "./Shell";

function NotFound() {
  return (
    <div className="p-5">
      <EmptyState
        title="There is no page here"
        action={
          <Button asChild variant="outline" size="sm">
            <Link href="/">Go to the overview</Link>
          </Button>
        }
      />
    </div>
  );
}

export function App() {
  return (
    <Shell>
      <Switch>
        <Route path="/" component={Overview} />
        <Route path="/p/:id/:tab?">
          {(params) => (
            <ProjectPage
              key={params.id}
              projectId={decodeURIComponent(params.id)}
              tab={params.tab}
            />
          )}
        </Route>
        <Route path="/jobs/:id?">{(params) => <JobsPage jobId={params.id} />}</Route>
        <Route path="/forks" component={ForksPage} />
        <Route path="/standards" component={StandardsPage} />
        <Route path="/services" component={ServicesPage} />
        <Route path="/runs" component={RunsPage} />
        <Route path="/activity" component={ActivityPage} />
        <Route path="/settings" component={SettingsPage} />
        <Route component={NotFound} />
      </Switch>
      <ActionDialogHost />
      <CommandPalette />
    </Shell>
  );
}
