"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatEventItem, ChatMessage } from "@/lib/types";
import { createChatSession, getChatMessages, listChatSessions, postChatMessage } from "@/lib/api";

type Row = ChatMessage & { events?: ChatEventItem[] };

/** Concierge chat: configure + run the pipeline, approve gates, tailor/prepare/analyze/
 *  delete the SELECTED jobs, apply to the resume — all by chat. The current dashboard
 *  selection is sent with each message so "my selected jobs" resolves server-side. The
 *  conversation is persisted server-side, so a reload/reconnect re-hydrates the transcript. */
export default function Chat({
  onChanged,
  selectedJobIds = [],
}: {
  onChanged?: () => void;
  selectedJobIds?: string[];
}) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  // Reconnect: reuse the latest session (re-hydrating its transcript) or start one.
  useEffect(() => {
    (async () => {
      try {
        const existing = await listChatSessions();
        const id = existing[0]?.id ?? (await createChatSession()).id;
        setSessionId(id);
        setRows(await getChatMessages(id));
      } catch (e) {
        setError(String(e));
      }
    })();
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [rows, busy]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || !sessionId || busy) return;
    setInput("");
    setError(null);
    setRows((r) => [...r, { id: `local-${Date.now()}`, role: "user", content: text, created_at: null }]);
    setBusy(true);
    try {
      const turn = await postChatMessage(sessionId, text, selectedJobIds);
      setRows((r) => [
        ...r,
        {
          id: `a-${Date.now()}`,
          role: "assistant",
          content: turn.reply,
          created_at: null,
          events: turn.events,
        },
      ]);
      onChanged?.();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [input, sessionId, busy, onChanged, selectedJobIds]);

  return (
    <section className="flex h-[32rem] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card transition-shadow hover:shadow-card-hover">
      <div className="border-b border-slate-100 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="grid h-6 w-6 place-items-center rounded-md bg-emerald-50 text-emerald-600">
            <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
              <path d="M8 1.5a6.5 6.5 0 0 0-5.6 9.79L1.6 14a.5.5 0 0 0 .62.62l2.7-.79A6.5 6.5 0 1 0 8 1.5Z" />
            </svg>
          </span>
          <h2 className="text-sm font-semibold text-slate-900">Concierge</h2>
          {selectedJobIds.length > 0 && (
            <span
              className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700"
              title="These jobs are the target when you say 'my selected jobs'"
            >
              {selectedJobIds.length} selected
            </span>
          )}
        </div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Reconfigure and run your pipeline, review gates, or act on your selected jobs —
          “analyze/tailor/prepare/delete my selected jobs”, or “delete the disqualified ones”.
        </p>
      </div>

      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto bg-slate-50/50 px-4 py-3">
        {rows.length === 0 && (
          <p className="mt-8 text-center text-sm text-slate-400">
            Try: “only remote roles, weight Python higher, then run the pipeline”.
          </p>
        )}
        {rows.map((m) => (
          <div key={m.id} className={m.role === "user" ? "text-right" : "text-left"}>
            <div
              className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm shadow-card ${
                m.role === "user"
                  ? "rounded-br-md bg-emerald-600 text-white"
                  : "rounded-bl-md border border-slate-200 bg-white text-slate-700"
              }`}
            >
              {m.content || (m.role === "assistant" ? "…" : "")}
            </div>
            {m.events && m.events.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1 text-left">
                {m.events.map((ev, i) => (
                  <span
                    key={i}
                    className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                  >
                    {ev.tool}
                    {ev.status && ev.status !== "ok" ? `·${ev.status}` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-left text-xs text-slate-500">
            <span className="flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-emerald-500 [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-emerald-500 [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-emerald-500" />
            </span>
            thinking…
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
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Message the Concierge…"
          disabled={busy || !sessionId}
          className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={busy || !sessionId || !input.trim()}
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-emerald-600"
        >
          Send
        </button>
      </div>
    </section>
  );
}
