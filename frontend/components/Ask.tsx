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
    <section className="flex h-[32rem] flex-col rounded-2xl border border-neutral-800 bg-neutral-900/30">
      <div className="border-b border-neutral-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-neutral-200">Ask about your data</h2>
        <p className="text-xs text-neutral-500">
          Read-only Q&amp;A — counts, semantic search, and pipeline status. Cited, never guessed.
        </p>
      </div>

      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {rows.length === 0 && (
          <p className="mt-8 text-center text-sm text-neutral-500">
            Try: “how many jobs qualified this week?”, “roles about data platforms”, or “is the
            scout running?”
          </p>
        )}
        {rows.map((m) => (
          <div key={m.id} className={m.role === "user" ? "text-right" : "text-left"}>
            <div
              className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
                m.role === "user"
                  ? "bg-sky-500/15 text-sky-100"
                  : "bg-neutral-800/60 text-neutral-200"
              }`}
            >
              {m.content || (m.role === "assistant" ? "…" : "")}
            </div>
            {m.routed && (
              <p className="mt-1 text-xs text-amber-300/80">
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
                        className="rounded-md border border-neutral-800 bg-neutral-900/60 px-1.5 py-0.5 text-[11px] text-sky-300 hover:border-sky-500/40"
                        title={c.similarity != null ? `similarity ${c.similarity}` : undefined}
                      >
                        {c.title ?? "job"}
                        {c.company ? ` · ${c.company}` : ""}
                      </a>
                    ) : (
                      <span
                        key={i}
                        className="rounded-md border border-neutral-800 bg-neutral-900/60 px-1.5 py-0.5 text-[11px] text-neutral-400"
                      >
                        {c.title ?? "job"}
                        {c.company ? ` · ${c.company}` : ""}
                      </span>
                    )
                  ) : (
                    <span
                      key={i}
                      className="rounded-md border border-neutral-800 bg-neutral-900/60 px-1.5 py-0.5 font-mono text-[10px] text-neutral-400"
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
        {busy && <div className="text-left text-xs text-neutral-500">● looking…</div>}
      </div>

      {error && <p className="px-4 pb-1 text-xs text-rose-300">{error}</p>}
      <div className="flex gap-2 border-t border-neutral-800 p-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
          placeholder="Ask a question about your data…"
          disabled={busy || !sessionId}
          className="flex-1 rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 outline-none focus:border-neutral-600 disabled:opacity-50"
        />
        <button
          onClick={ask}
          disabled={busy || !sessionId || !input.trim()}
          className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-40"
        >
          Ask
        </button>
      </div>
    </section>
  );
}
