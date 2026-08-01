"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, QueryCitation } from "@/lib/types";
import { askQuery, createQuerySession, getQueryMessages } from "@/lib/api";

type Row = ChatMessage & { citations?: QueryCitation[]; routed?: boolean };

/** The Query agent surface: read-only Q&A over the user's data + work status. Answers
 *  are grounded and cite the jobs/runs they rest on. It never acts — action requests
 *  are redirected to the Concierge. Its own session (separate from the Concierge chat). */
export default function Ask() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    (async () => {
      try {
        const { id } = await createQuerySession();
        setSessionId(id);
        setRows(await getQueryMessages(id));
      } catch (e) {
        setError(String(e));
      }
    })();
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [rows, busy]);

  const ask = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setError(null);
    setRows((r) => [...r, { id: `local-${Date.now()}`, role: "user", content: question, created_at: null }]);
    setBusy(true);
    try {
      const turn = await askQuery(question, sessionId);
      if (!sessionId) setSessionId(turn.session_id);
      setRows((r) => [
        ...r,
        {
          id: `a-${Date.now()}`,
          role: "assistant",
          content: turn.answer,
          created_at: null,
          citations: turn.citations,
          routed: turn.routed_to_concierge,
        },
      ]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [input, sessionId, busy]);

  return (
    <section className="flex h-[32rem] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card transition-shadow hover:shadow-card-hover">
      <div className="border-b border-slate-100 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="grid h-6 w-6 place-items-center rounded-md bg-sky-50 text-sky-600">
            <svg
              className="h-3.5 w-3.5"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden
            >
              <circle cx="7" cy="7" r="4.5" />
              <path d="m10.5 10.5 3 3" />
            </svg>
          </span>
          <h2 className="text-sm font-semibold text-slate-900">Ask about your data</h2>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-medium text-slate-500">
            read-only
          </span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Read-only Q&amp;A — counts, semantic search, and pipeline status. Cited, never guessed.
        </p>
      </div>

      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto bg-slate-50/50 px-4 py-3">
        {rows.length === 0 && (
          <p className="mt-8 text-center text-sm text-slate-400">
            Try: “how many jobs qualified this week?”, “roles about data platforms”, or “is the
            scout running?”
          </p>
        )}
        {rows.map((m) => (
          <div key={m.id} className={m.role === "user" ? "text-right" : "text-left"}>
            <div
              className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm shadow-card ${
                m.role === "user"
                  ? "rounded-br-md bg-sky-600 text-white"
                  : "rounded-bl-md border border-slate-200 bg-white text-slate-700"
              }`}
            >
              {m.content || (m.role === "assistant" ? "…" : "")}
            </div>
            {m.routed && (
              <p className="mt-1 text-xs text-amber-600">
                That’s an action — ask the Concierge to do it.
              </p>
            )}
            {m.citations && m.citations.length > 0 && (
              <div className="mt-1.5 flex flex-wrap justify-start gap-1.5">
                {m.citations.map((c, i) =>
                  c.type === "job" ? (
                    c.url ? (
                      <a
                        key={i}
                        href={c.url}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-md border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[11px] font-medium text-sky-700 transition hover:border-sky-300 hover:bg-sky-100"
                        title={c.similarity != null ? `similarity ${c.similarity}` : undefined}
                      >
                        {c.title ?? "job"}
                        {c.company ? ` · ${c.company}` : ""}
                      </a>
                    ) : (
                      <span
                        key={i}
                        className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-500"
                      >
                        {c.title ?? "job"}
                        {c.company ? ` · ${c.company}` : ""}
                      </span>
                    )
                  ) : (
                    <span
                      key={i}
                      className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                    >
                      {c.stage}
                      {c.finished_at ? ` · ${new Date(c.finished_at).toLocaleDateString()}` : ""}
                    </span>
                  ),
                )}
              </div>
            )}
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-left text-xs text-slate-500">
            <span className="flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500 [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500 [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500" />
            </span>
            looking…
          </div>
        )}
      </div>

      {error && (
        <p className="border-t border-rose-100 bg-rose-50 px-4 py-1.5 text-xs text-rose-700">{error}</p>
      )}
      <div className="flex gap-2 border-t border-slate-100 bg-white p-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
          placeholder="Ask a question about your data…"
          disabled={busy || !sessionId}
          className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-sky-400 focus:ring-2 focus:ring-sky-100 disabled:opacity-50"
        />
        <button
          onClick={ask}
          disabled={busy || !sessionId || !input.trim()}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-sky-600"
        >
          Ask
        </button>
      </div>
    </section>
  );
}
