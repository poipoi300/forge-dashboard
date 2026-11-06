// Rendered markdown for READMEs, launch guides, issues, notes, and transcripts.
// Links open in a new tab; raw HTML in the source is never rendered. Headings
// drop two levels: a document's "# Title" sits inside a page section and must
// not become a second page title.
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("prose-board", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: label }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {label}
            </a>
          ),
          h1: ({ children: text }) => <h3>{text}</h3>,
          h2: ({ children: text }) => <h4>{text}</h4>,
          h3: ({ children: text }) => <h5>{text}</h5>,
          h4: ({ children: text }) => <h6>{text}</h6>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
