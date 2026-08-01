"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Health = { status: string; service: string; version: string };
type State =
  | { kind: "loading" }
  | { kind: "ok"; data: Health }
  | { kind: "error"; message: string };

export default function HealthStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_URL}/health`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: Health = await res.json();
        if (!cancelled) setState({ kind: "ok", data });
      } catch (err) {
        if (!cancelled)
          setState({
            kind: "error",
            message: err instanceof Error ? err.message : "unreachable",
          });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const dotColor =
    state.kind === "ok"
      ? "bg-emerald-500"
      : state.kind === "error"
        ? "bg-rose-500"
        : "bg-amber-500";

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
      <div className="flex items-center gap-3">
        <span className="relative flex h-3 w-3">
          {state.kind === "loading" && (
            <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${dotColor} opacity-60`} />
          )}
          <span className={`relative inline-flex h-3 w-3 rounded-full ${dotColor}`} />
        </span>
        <span className="font-medium text-slate-900">Backend API</span>
        <span className="ml-auto font-mono text-xs text-slate-400">{API_URL}</span>
      </div>
      <div className="mt-3 text-sm text-slate-600">
        {state.kind === "loading" && <span>Checking…</span>}
        {state.kind === "ok" && (
          <span>
            <span className="font-medium text-emerald-600">ok</span> — {state.data.service} v
            {state.data.version}
          </span>
        )}
        {state.kind === "error" && (
          <span className="text-rose-600">
            unreachable ({state.message}). Is the backend running on {API_URL}?
          </span>
        )}
      </div>
    </div>
  );
}
