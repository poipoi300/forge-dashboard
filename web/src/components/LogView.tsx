// An ANSI-aware log on the board ground: child colours preserved, follows the
// tail until the reader scrolls up, filterable, and copyable as plain text.
import Anser from "anser";
import { ArrowDownToLine, Copy, Search } from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const RENDERED_LINES = 4000;
// biome-ignore lint/suspicious/noControlCharactersInRegex: matching the ESC that opens an ANSI sequence is the point
const ANSI_PATTERN = /\u001b\[[0-9;?]*[A-Za-z]/g;

export function stripAnsi(text: string): string {
  return text.replace(ANSI_PATTERN, "");
}

const DECORATION_STYLE: Record<string, React.CSSProperties> = {
  bold: { fontWeight: 600 },
  dim: { opacity: 0.7 },
  italic: { fontStyle: "italic" },
  underline: { textDecoration: "underline" },
  strikethrough: { textDecoration: "line-through" },
};

const LogLine = memo(function LogLine({ line }: { line: string }) {
  const runner = line.startsWith("[runner]") || line.startsWith("[dashboard]");
  const parts = useMemo(() => Anser.ansiToJson(line, { remove_empty: true }), [line]);
  return (
    <div
      className={cn("min-h-[1.25em] whitespace-pre-wrap break-all", runner && "text-board-ink-2")}
    >
      {parts.map((part, index) => {
        const style: React.CSSProperties = {};
        if (part.fg) style.color = `rgb(${part.fg})`;
        if (part.bg) style.backgroundColor = `rgb(${part.bg})`;
        for (const decoration of part.decorations)
          Object.assign(style, DECORATION_STYLE[decoration]);
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: spans of one immutable line
          <span key={index} style={style}>
            {part.content}
          </span>
        );
      })}
    </div>
  );
});

export function LogView({
  lines,
  className,
  label,
  emptyText = "No output yet.",
  live = false,
}: {
  lines: string[];
  className?: string;
  label: string;
  emptyText?: string;
  live?: boolean;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const matching = needle
      ? lines.filter((line) => stripAnsi(line).toLowerCase().includes(needle))
      : lines;
    return {
      hidden: Math.max(0, matching.length - RENDERED_LINES),
      lines: matching.slice(-RENDERED_LINES),
    };
  }, [lines, filter]);

  useEffect(() => {
    const element = scroller.current;
    if (follow && element && shown.lines.length >= 0) element.scrollTop = element.scrollHeight;
  }, [follow, shown]);

  const onScroll = () => {
    const element = scroller.current;
    if (!element) return;
    setFollow(element.scrollHeight - element.scrollTop - element.clientHeight < 24);
  };

  return (
    <div
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-md border border-board-rule bg-board",
        className,
      )}
    >
      <div className="flex items-center gap-2 border-b border-board-rule px-2 py-1">
        <span className="truncate text-2xs text-board-ink-2">
          {label}
          <span className="figures"> · {lines.length} lines</span>
          {live ? <span className="text-board-ink"> · live</span> : null}
        </span>
        <label className="ml-auto flex items-center gap-1 rounded-sm border border-board-rule px-1.5">
          <Search aria-hidden className="size-3 text-board-ink-2" />
          <span className="sr-only">Filter {label}</span>
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="filter"
            className="h-6 w-28 bg-transparent text-2xs text-board-ink outline-none placeholder:text-board-ink-2"
          />
        </label>
        <Button
          variant="ghost"
          size="icon-xs"
          className="text-board-ink-2 hover:bg-board-2 hover:text-board-ink"
          title="Copy as plain text"
          aria-label="Copy log as plain text"
          onClick={() => void navigator.clipboard.writeText(lines.map(stripAnsi).join("\n"))}
        >
          <Copy />
        </Button>
        {!follow ? (
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-board-ink-2 hover:bg-board-2 hover:text-board-ink"
            title="Follow the tail"
            aria-label="Follow the tail"
            onClick={() => setFollow(true)}
          >
            <ArrowDownToLine />
          </Button>
        ) : null}
      </div>
      <div
        ref={scroller}
        onScroll={onScroll}
        role="log"
        aria-label={label}
        aria-live={live ? "polite" : "off"}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable log must be reachable to scroll by keyboard
        tabIndex={0}
        className="min-h-0 flex-1 overflow-auto px-2.5 py-2 font-mono text-[0.71875rem] leading-[1.25] text-board-ink"
      >
        {shown.hidden ? (
          <div className="pb-1 text-board-ink-2">
            {shown.hidden} earlier lines not shown; copy the log for all of it.
          </div>
        ) : null}
        {shown.lines.length ? (
          shown.lines.map((line, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: append-only log, position is identity
            <LogLine key={shown.hidden + index} line={line} />
          ))
        ) : (
          <div className="text-board-ink-2">
            {filter ? "No line matches the filter." : emptyText}
          </div>
        )}
      </div>
    </div>
  );
}
