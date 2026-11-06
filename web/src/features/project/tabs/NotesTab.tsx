// Free-form markdown notes, stored in dashboard state, edited beside a preview.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ApiError, call } from "@/api/client";
import { useAction } from "@/api/query";
import { RelativeTime } from "@/components/data";
import { Markdown } from "@/components/Markdown";
import { QueryState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { TabProps } from "../ProjectPage";

function Editor({
  projectId,
  saved,
  updatedAt,
}: {
  projectId: string;
  saved: string;
  updatedAt: number | null | undefined;
}) {
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  const queryClient = useQueryClient();
  useEffect(() => setDraft(saved), [saved]);
  const dirty = draft !== saved;
  const save = async () => {
    setBusy(true);
    try {
      await call("/api/services/projects/actions/save-notes", {
        project_id: projectId,
        markdown: draft,
      });
      await queryClient.invalidateQueries();
      toast("Notes saved");
    } catch (failure) {
      toast.error("Notes were not saved", {
        description: failure instanceof ApiError ? failure.detail : String(failure),
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-2">
      <div className="flex flex-col gap-2">
        <label htmlFor="notes" className="text-xs text-ink-3">
          Markdown
        </label>
        <Textarea
          id="notes"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "s") {
              event.preventDefault();
              if (dirty) void save();
            }
          }}
          className="min-h-80 font-mono text-xs"
          placeholder="Decisions, next steps, links. Ctrl+S saves."
        />
        <div className="flex items-center justify-between">
          <span className="text-2xs text-ink-3">
            {dirty ? (
              "unsaved changes"
            ) : updatedAt ? (
              <>
                saved <RelativeTime at={updatedAt} />
              </>
            ) : (
              "never saved"
            )}
          </span>
          <Button size="sm" disabled={!dirty || busy} onClick={() => void save()}>
            Save notes
          </Button>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-xs text-ink-3">Preview</span>
        <div className="min-h-80 rounded-md border border-rule bg-surface px-4 py-3">
          {draft.trim() ? (
            <Markdown>{draft}</Markdown>
          ) : (
            <p className="text-sm text-ink-3">Nothing written yet.</p>
          )}
        </div>
      </div>
    </div>
  );
}

export function NotesTab({ project }: TabProps) {
  const notes = useAction("/api/services/projects/actions/notes", { project_id: project.id });
  return (
    <QueryState query={notes}>
      {(data) => (
        <Editor projectId={project.id} saved={data.markdown} updatedAt={data.updated_at} />
      )}
    </QueryState>
  );
}
