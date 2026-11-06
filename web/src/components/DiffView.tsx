// A unified git diff, one block per file, in the interface's own colours.
import { useMemo } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";

export function DiffView({ diff, empty = "No changes." }: { diff: string; empty?: string }) {
  const files = useMemo(
    () => (diff.trim() ? parseDiff(diff, { nearbySequences: "zip" }) : []),
    [diff],
  );
  if (!diff.trim()) return <p className="text-sm text-ink-3">{empty}</p>;
  // Text that is not a unified diff (an error, a summary) is shown as it is, not as a nameless file.
  if (!files.length || files.every((file) => !file.oldPath && !file.newPath)) {
    return (
      <pre className="max-h-96 overflow-auto rounded-md border border-rule bg-surface-sunk px-3 py-2 font-mono text-2xs whitespace-pre-wrap">
        {diff}
      </pre>
    );
  }
  return (
    <div className="diff-board flex flex-col gap-3">
      {files.map((file) => {
        const path = file.type === "delete" ? file.oldPath : file.newPath;
        // A rename with edits arrives as "modify"; the two paths still say it moved.
        const moved =
          file.type !== "add" && file.type !== "delete" && file.oldPath !== file.newPath;
        return (
          <section
            key={`${file.oldRevision}-${file.newRevision}-${path}`}
            className="overflow-hidden rounded-md border border-rule bg-surface"
          >
            <header className="flex items-center justify-between gap-2 border-b border-rule bg-surface-sunk px-3 py-1">
              <code className="truncate font-mono text-xs">
                {moved ? `${file.oldPath} → ${file.newPath}` : path}
              </code>
              <span className="text-2xs text-ink-3">
                {moved && file.type === "modify" ? "renamed, modified" : file.type}
              </span>
            </header>
            {file.hunks.length ? (
              <div className="overflow-x-auto">
                <Diff viewType="unified" diffType={file.type} hunks={file.hunks}>
                  {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
                </Diff>
              </div>
            ) : (
              <p className="px-3 py-2 text-xs text-ink-3">Binary or mode-only change.</p>
            )}
          </section>
        );
      })}
    </div>
  );
}
