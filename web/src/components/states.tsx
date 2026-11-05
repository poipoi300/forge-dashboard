// Designed empty, loading, and error states. Every query-backed region uses
// these so a slow or failing source reads the same everywhere.
import { CircleSlash, OctagonAlert, RotateCw } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function EmptyState({
  title,
  children,
  action,
  className,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-2 rounded-md border border-dashed border-rule px-4 py-5",
        className,
      )}
    >
      <p className="flex items-center gap-2 text-sm font-medium text-ink">
        <CircleSlash aria-hidden className="size-4 text-ink-3" />
        {title}
      </p>
      {children ? <div className="max-w-prose text-sm text-ink-2">{children}</div> : null}
      {action}
    </div>
  );
}

export function LoadingRows({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div
      className={cn("flex flex-col gap-2", className)}
      role="status"
      aria-busy="true"
      aria-label="Loading"
    >
      {Array.from({ length: rows }, (_, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: placeholder rows
        <Skeleton key={index} className="h-6 w-full" style={{ opacity: 1 - index * 0.2 }} />
      ))}
    </div>
  );
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function ErrorState({
  error,
  title = "This could not be loaded",
  onRetry,
  className,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-2 rounded-md border border-signal/30 bg-signal-wash px-4 py-3",
        className,
      )}
    >
      <p className="flex items-center gap-2 text-sm font-medium text-signal-ink">
        <OctagonAlert aria-hidden className="size-4" />
        {title}
      </p>
      <p className="max-w-prose break-words text-sm text-ink-2">{errorText(error)}</p>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCw />
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/** Render a query's three states with the shared designs; `children` gets the data. */
export function QueryState<T>({
  query,
  children,
  rows,
  errorTitle,
}: {
  query: { data: T | undefined; error: unknown; isPending: boolean; refetch: () => unknown };
  children: (data: T) => ReactNode;
  rows?: number;
  errorTitle?: string;
}) {
  if (query.error)
    return (
      <ErrorState error={query.error} title={errorTitle} onRetry={() => void query.refetch()} />
    );
  if (query.isPending || query.data === undefined) return <LoadingRows rows={rows} />;
  return <>{children(query.data)}</>;
}

/** A titled section of a page; the heading is the anchor other views link to. */
export function Section({
  title,
  id,
  actions,
  children,
  className,
  description,
}: {
  title: string;
  id?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  description?: ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={id ? `${id}-title` : undefined}
      className={cn("flex flex-col gap-3", className)}
    >
      <header className="flex flex-wrap items-end justify-between gap-2 border-b border-rule-strong pb-1.5">
        <div className="flex min-w-0 flex-col">
          <h2
            id={id ? `${id}-title` : undefined}
            className="text-sm font-semibold tracking-tight text-ink"
          >
            {title}
          </h2>
          {description ? <p className="text-xs text-ink-3">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-1.5">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}
