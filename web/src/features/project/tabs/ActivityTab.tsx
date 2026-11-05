// The project's own timeline.
import { ActivityFeed } from "../../activity/ActivityFeed";
import type { TabProps } from "../ProjectPage";

export function ActivityTab({ project }: TabProps) {
  return (
    <div className="max-w-4xl">
      <ActivityFeed projectId={project.id} />
    </div>
  );
}
