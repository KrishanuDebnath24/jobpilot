"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

/** How the agents' answers are drawn. They write markdown — headings, bold, lists,
 *  and GFM tables — which used to land in the chat as literal ** and | characters.
 *  Every element is styled here (no typography plugin) so answers read at chat scale
 *  and a wide table scrolls inside its own bubble instead of stretching the panel.
 *  Raw HTML is NOT enabled: answers quote untrusted job text, so it stays inert. */
const components: Components = {
  h1: ({ children }) => (
    <h3 className="mb-1 mt-3 text-sm font-semibold text-slate-900">{children}</h3>
  ),
  h2: ({ children }) => (
    <h4 className="mb-1 mt-3 text-sm font-semibold text-slate-900">{children}</h4>
  ),
  h3: ({ children }) => (
    <h5 className="mb-1 mt-3 text-[13px] font-semibold text-slate-800">{children}</h5>
  ),
  h4: ({ children }) => (
    <h6 className="mb-1 mt-2 text-[13px] font-semibold text-slate-700">{children}</h6>
  ),
  p: ({ children }) => <p className="my-1.5 leading-relaxed">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-slate-900">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => <ul className="my-1.5 list-disc space-y-1 pl-4 marker:text-slate-300">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 list-decimal space-y-1 pl-4 marker:text-slate-400">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="font-medium text-sky-700 underline decoration-sky-300 underline-offset-2 hover:decoration-sky-600"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-1.5 border-l-2 border-slate-200 pl-2.5 text-slate-500">{children}</blockquote>
  ),
  hr: () => <hr className="my-2.5 border-slate-200" />,
  code: ({ className, children }) =>
    // A fenced block arrives with a language class; a bare span of code doesn't.
    className ? (
      <code className={`block font-mono text-[11px] leading-relaxed ${className}`}>{children}</code>
    ) : (
      <code className="rounded bg-slate-100 px-1 py-px font-mono text-[12px] text-slate-800">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-slate-900 px-2.5 py-2 text-slate-100">{children}</pre>
  ),
  table: ({ children }) => (
    <div className="-mx-1 my-2 overflow-x-auto">
      <table className="w-full border-collapse text-left text-[12px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="border-b border-slate-200">{children}</thead>,
  th: ({ children }) => (
    <th className="whitespace-nowrap px-2 py-1 font-semibold text-slate-500">{children}</th>
  ),
  tr: ({ children }) => <tr className="border-b border-slate-100 last:border-0">{children}</tr>,
  td: ({ children }) => <td className="px-2 py-1 align-top tabular-nums text-slate-700">{children}</td>,
};

/** Renders one message body. Memoized: committed rows never re-parse while the next
 *  answer streams in token by token. */
function MarkdownBody({ text }: { text: string }) {
  return (
    <div className="[&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      {/* gfm: tables + strikethrough. breaks: a single newline stays a line break,
          the way it read before this was markdown at all. */}
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default memo(MarkdownBody);
