// Server-side folder browser: the dashboard lists folders on its own machine,
// so roots and project paths are picked, not typed.
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, CornerLeftUp, Folder, FolderGit2, HardDrive } from "lucide-react";
import { useEffect, useState } from "react";
import { call } from "@/api/client";
import { errorText } from "@/components/states";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { homeRelative } from "@/lib/format";

/** The folder above `path`, or null at a drive or filesystem root (which goes back to "This machine"). */
export function parentOf(path: string): string | null {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  if (cut <= 0) return null;
  const parent = trimmed.slice(0, cut);
  return /^[A-Za-z]:$/.test(parent) ? `${parent}\\` : parent;
}

export function FolderPicker({
  open,
  onOpenChange,
  start,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  start: string | null;
  onPick: (path: string) => void;
}) {
  const [path, setPath] = useState<string | null>(start);
  useEffect(() => {
    if (open) setPath(start);
  }, [open, start]);
  const listing = useQuery({
    queryKey: ["browse", path],
    queryFn: () => call("/api/services/discovery/actions/browse", { path }),
    enabled: open,
    retry: false,
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Choose a folder</DialogTitle>
          <DialogDescription className="font-mono text-xs">
            {path ? homeRelative(path) : "This machine"}
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-80 overflow-y-auto rounded-md border border-rule">
          {path !== null ? (
            <button
              type="button"
              className="flex w-full items-center gap-2 border-b border-rule px-3 py-1.5 text-left text-sm hover:bg-hover"
              // An unreadable folder has no listing, so its parent comes from the path itself.
              onClick={() => setPath(listing.data ? (listing.data.parent ?? null) : parentOf(path))}
            >
              <CornerLeftUp className="size-4 text-ink-3" />
              Up
            </button>
          ) : null}
          {listing.isError ? (
            <p className="px-3 py-3 text-sm text-signal-ink">
              This folder cannot be opened: {errorText(listing.error)}
            </p>
          ) : listing.data?.entries.length === 0 ? (
            <p className="px-3 py-3 text-sm text-ink-3">No subfolders.</p>
          ) : (
            listing.data?.entries.map((entry) => {
              const Icon = path === null ? HardDrive : entry.is_project ? FolderGit2 : Folder;
              return (
                <button
                  type="button"
                  key={entry.path}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-hover"
                  onClick={() => setPath(entry.path)}
                >
                  <Icon className={entry.is_project ? "size-4 text-info" : "size-4 text-ink-3"} />
                  <span className="flex-1 truncate">{entry.name}</span>
                  <ChevronRight className="size-3.5 text-ink-3" />
                </button>
              );
            })
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={path === null} onClick={() => path && onPick(path)}>
            Use this folder
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
