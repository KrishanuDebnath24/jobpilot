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
    <section className="flex h-[32rem] flex-col rounded-2xl border border-neutral-800 bg-neutral-900/30">
      <div className="border-b border-neutral-800 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-neutral-200">Concierge</h2>
          {selectedJobIds.length > 0 && (
            <span
              className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-200"
              title="These jobs are the target when you say 'my selected jobs'"
            >
              {selectedJobIds.length} selected
            </span>
          )}
        </div>
        <p className="text-xs text-neutral-500">
          Reconfigure and run your pipeline, review gates, or act on your selected jobs —
          “analyze/tailor/prepare/delete my selected jobs”, or “delete the disqualified ones”.
        </p>
      </div>

      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {rows.length === 0 && (
          <p className="mt-8 text-center text-sm text-neutral-500">
            Try: “only remote roles, weight Python higher, then run the pipeline”.
          </p>
        )}
        {rows.map((m) => (
          <div key={m.id} className={m.role === "user" ? "text-right" : "text-left"}>
            <div
              className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
                m.role === "user"
                  ? "bg-emerald-500/15 text-emerald-100"
                  : "bg-neutral-800/60 text-neutral-200"
              }`}
            >
              {m.content || (m.role === "assistant" ? "…" : "")}
            </div>
            {m.events && m.events.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1 text-left">
                {m.events.map((ev, i) => (
                  <span
                    key={i}
                    className="rounded-md border border-neutral-800 bg-neutral-900/60 px-1.5 py-0.5 font-mono text-[10px] text-neutral-400"
                  >
                    {ev.tool}
                    {ev.status && ev.status !== "ok" ? `·${ev.status}` : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
        {busy && <div className="text-left text-xs text-neutral-500">● thinking…</div>}
      </div>

      {error && <p className="px-4 pb-1 text-xs text-rose-300">{error}</p>}
      <div className="flex gap-2 border-t border-neutral-800 p-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Message the Concierge…"
          disabled={busy || !sessionId}
          className="flex-1 rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 outline-none focus:border-neutral-600 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={busy || !sessionId || !input.trim()}
          className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200 transition hover:bg-emerald-500/20 disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </section>
  );
}
