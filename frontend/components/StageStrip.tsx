"use client";

import { useEffect, useState } from "react";

/* Progress feedback for the fire-and-forget CV actions — presentational only.
 *
 * These endpoints return 202 the instant the task is queued, so a button that clears its
 * "busy" state when the request resolves is reporting the *request*, not the work: the label
 * flashes for a moment while the worker runs for another minute. This strip stays up until
 * the UI can actually observe the work landing.
 *
 * The honesty rule it follows: a stage is only ticked when there is real evidence for it in
 * data we already poll (drafts appeared, `pdf_ref` appeared, `after_score` appeared). Stages
 * the frontend cannot observe pulse as "running" and are never claimed as finished, and there
 * is deliberately no percentage anywhere — the backend reports no progress, so any number
 * would be invented.
 */

export type StageState = "done" | "active" | "pending";

export type Stage = {
  key: string;
  label: string;
  /** Stages we can never confirm from polled data — shown as running, never ticked. */
  unobservable?: boolean;
};

const DOT: Record<StageState, string> = {
  done: "text-emerald-400",
  active: "text-sky-300 animate-pulse",
  pending: "text-neutral-600",
};
const TEXT: Record<StageState, string> = {
  done: "text-neutral-300",
  active: "text-sky-200",
  pending: "text-neutral-600",
};

function glyph(state: StageState, unobservable?: boolean): string {
  if (state === "done") return "✓";
  if (state === "active") return "●";
  return unobservable ? "◌" : "○";
}

/** Live elapsed seconds since `since`, ticking once a second. */
function useElapsed(since: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (since === null) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [since]);
  return since === null ? 0 : Math.max(0, Math.round((now - since) / 1000));
}

export function Elapsed({ since }: { since: number | null }) {
  const secs = useElapsed(since);
  if (since === null) return null;
  const mins = Math.floor(secs / 60);
  return <>{mins > 0 ? `${mins}m ${secs % 60}s` : `${secs}s`}</>;
}

export default function StageStrip({
  title,
  stages,
  stateOf,
  startedAt,
  note,
  tone = "sky",
  onDismiss,
}: {
  title: string;
  stages: Stage[];
  stateOf: (key: string) => StageState;
  startedAt: number | null;
  note?: string | null;
  tone?: "sky" | "amber";
  onDismiss?: () => void;
}) {
  const border = tone === "amber" ? "border-amber-500/30" : "border-sky-500/30";
  const bg = tone === "amber" ? "bg-amber-500/[0.04]" : "bg-sky-500/[0.04]";
  const head = tone === "amber" ? "text-amber-200" : "text-sky-200";

  return (
    <div className={`mt-3 rounded-lg border ${border} ${bg} px-3 py-2`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className={`font-medium ${head}`}>
          <span className="mr-1.5 inline-block animate-pulse">●</span>
          {title}
        </span>
        {startedAt !== null && (
          <span className="font-mono tabular-nums text-neutral-500">
            <Elapsed since={startedAt} />
          </span>
        )}
        {onDismiss && (
          <button
            onClick={onDismiss}
            className="ml-auto text-neutral-500 transition hover:text-neutral-300"
            title="Hide this (the work keeps running)"
          >
            ✕
          </button>
        )}
      </div>

      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {stages.map((s) => {
          const st = stateOf(s.key);
          return (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span className={DOT[st]}>{glyph(st, s.unobservable)}</span>
              <span className={TEXT[st]}>{s.label}</span>
            </span>
          );
        })}
      </div>

      {note && <p className="mt-1.5 text-xs text-neutral-500">{note}</p>}
    </div>
  );
}
