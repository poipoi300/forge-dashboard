// A unified git diff, one block per file, in the interface's own colours.
import { useMemo } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";

export function DiffView({ diff, empty = "No changes." }: { diff: string; empty?: string }) {
  const files = useMemo(
    () => (diff.trim() ? parseDiff(diff, { nearbySequences: "zip" }) : []),
    [diff],
  );
  if (!files.length) return <p className="text-sm text-ink-3">{empty}</p>;
  return (
    <div className="diff-board flex flex-col gap-3">
      {files.map((file) => {
        const path = file.type === "delete" ? file.oldPath : file.newPath;
        return (
          <section
            key={`${file.oldRevision}-${file.newRevision}-${path}`}
            className="overflow-hidden rounded-md border border-rule bg-surface"
          >
            <header className="flex items-center justify-between gap-2 border-b border-rule bg-surface-sunk px-3 py-1">
              <code className="truncate font-mono text-xs">
                {file.type === "rename" ? `${file.oldPath} → ${file.newPath}` : path}
              </code>
              <span className="text-2xs text-ink-3">{file.type}</span>
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
