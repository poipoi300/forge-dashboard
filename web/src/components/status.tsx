// Status is a glyph plus a word, never colour alone. One encoding per state.
import {
  CircleCheck,
  CircleDashed,
  CircleDot,
  Clock3,
  Info,
  OctagonAlert,
  TriangleAlert,
} from "lucide-react";
import type { ComponentType, ReactNode, SVGProps } from "react";
import { cn } from "@/lib/utils";
import type { Tone } from "@/schema/schema";

type Icon = ComponentType<
  SVGProps<SVGSVGElement> & { size?: number | string; strokeWidth?: number }
>;

export const TONE_ICON: Record<Tone, Icon> = {
  good: CircleCheck,
  warn: TriangleAlert,
  bad: OctagonAlert,
  info: CircleDot,
  neutral: CircleDashed,
};

export const TONE_TEXT: Record<Tone, string> = {
  good: "text-clear-ink",
  warn: "text-amber-ink",
  bad: "text-signal-ink",
  info: "text-info-ink",
  neutral: "text-ink-3",
};

const TONE_ICON_COLOR: Record<Tone, string> = {
  good: "text-clear",
  warn: "text-amber-glyph",
  bad: "text-signal",
  info: "text-info",
  neutral: "text-idle",
};

export function StatusMark({
  tone,
  children,
  className,
  title,
  strong = false,
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
  title?: string;
  strong?: boolean;
}) {
  const Glyph = TONE_ICON[tone];
  return (
    <span
      title={title}
      data-tone={tone}
      className={cn(
        "inline-flex min-w-0 items-center gap-1 whitespace-nowrap text-xs",
        strong ? TONE_TEXT[tone] : "text-ink-2",
        strong && "font-semibold",
        className,
      )}
    >
      <Glyph
        aria-hidden
        className={cn("size-3.5 shrink-0", TONE_ICON_COLOR[tone])}
        strokeWidth={2.25}
      />
      <span className="truncate">{children}</span>
    </span>
  );
}

/** A neutral label chip for categorical facts (kind, language, stage) that carry no state. */
export function Chip({
  children,
  className,
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded-sm border border-rule px-1.5 text-2xs leading-[1.125rem] text-ink-2",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** The board's delay mark: "+3" in signal red, used for lateness and overdue counts. */
export function Delay({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="figures inline-flex items-center gap-0.5 text-xs font-semibold text-signal-ink"
    >
      <Clock3 aria-hidden className="size-3" strokeWidth={2.5} />
      {children}
    </span>
  );
}

export function InfoNote({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-ink-3">
      <Info aria-hidden className="size-3.5" />
      {children}
    </span>
  );
}
