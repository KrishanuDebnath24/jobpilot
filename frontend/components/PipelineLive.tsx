"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PipelineEvent, PipelineExecution } from "@/lib/types";
import {
  getPipelineStatus,
  runPipeline,
  runStage,
  streamPipeline,
} from "@/lib/api";
import { timeAgo } from "./ui";

type StageState = {
  status: string; // idle | queued | running | completed | failed
  processed?: number;
  total?: number;
  counts?: Record<string, unknown>;
};
type Live = {
  pipelineId: string | null;
  status: string; // idle | running | completed | failed
  scout: StageState;
  qualify: StageState;
  updatedAt: string | null;
};

const IDLE: Live = {
  pipelineId: null,
  status: "idle",
  scout: { status: "idle" },
  qualify: { status: "idle" },
  updatedAt: null,
};

/** Fold a status snapshot (from /pipeline/status) into the live view. */
function fromSnapshot(p: PipelineExecution): Live {
  if (!p) return IDLE;
  const stage = (s?: { status: string; counts: Record<string, unknown> | null }): StageState =>
    s
      ? {
          status: s.status,
          counts: s.counts ?? undefined,
          processed: (s.counts?.processed as number) ?? undefined,
          total: (s.counts?.total as number) ?? undefined,
        }
      : { status: "idle" };
  return {
    pipelineId: p.pipeline_id,
    status: p.status,
    scout: stage(p.stages.scout),
    qualify: stage(p.stages.qualify),
    updatedAt: p.finished_at ?? p.created_at,
  };
}

/** Apply one live event to the current view. */
function reduce(prev: Live, ev: PipelineEvent): Live {
  const next: Live = { ...prev, updatedAt: ev.ts };
  const c = ev.counts ?? {};
  if (ev.pipeline_id) next.pipelineId = ev.pipeline_id;
  if (ev.stage === "pipeline") {
    if (ev.kind === "started") next.status = "running";
    if (ev.kind === "completed") next.status = "completed";
    if (ev.kind === "failed") next.status = "failed";
    return next;
  }
  const stage: StageState = { ...(prev[ev.stage as "scout" | "qualify"] ?? { status: "idle" }) };
  if (ev.kind === "started") {
    stage.status = "running";
    if (typeof c.total === "number") stage.total = c.total as number;
  } else if (ev.kind === "progress") {
    stage.status = "running";
    stage.processed = c.processed as number;
    stage.total = c.total as number;
  } else if (ev.kind === "completed") {
    stage.status = "completed";
    stage.counts = c;
  } else if (ev.kind === "failed") {
    stage.status = "failed";
  }
  next[ev.stage as "scout" | "qualify"] = stage;
  if (next.status === "idle") next.status = "running";
  return next;
}

function Dot({ status }: { status: string }) {
  const color =
    status === "completed"
      ? "bg-emerald-500"
      : status === "running" || status === "queued"
        ? "bg-amber-500 animate-pulse"
        : status === "failed"
          ? "bg-rose-500"
          : "bg-neutral-600";
  return <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} />;
}

function StageCard({
  title,
  s,
  summary,
}: {
  title: string;
  s: StageState;
  summary: React.ReactNode;
}) {
  const showBar = s.status === "running" && s.total && s.total > 0;
  const frac = showBar ? Math.min(1, (s.processed ?? 0) / (s.total as number)) : 0;
  return (
    <div className="flex-1 rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
      <div className="mb-2 flex items-center gap-2">
        <Dot status={s.status} />
        <h3 className="text-sm font-medium">{title}</h3>
        <span className="ml-auto text-xs capitalize text-neutral-500">{s.status}</span>
      </div>
      <div className="text-sm text-neutral-300">{summary}</div>
      {showBar && (
        <div className="mt-3">
          <div className="mb-1 flex justify-between text-[11px] text-neutral-500">
            <span>
              {s.processed ?? 0} / {s.total}
            </span>
            <span>{Math.round(frac * 100)}%</span>
          </div>
          <span className="block h-1.5 overflow-hidden rounded-full bg-neutral-800">
            <span
              className="block h-full rounded-full bg-amber-500 transition-all"
              style={{ width: `${Math.round(frac * 100)}%` }}
            />
          </span>
        </div>
      )}
    </div>
  );
}

export default function PipelineLive({ onJobsChanged }: { onJobsChanged: () => void }) {
  const [live, setLive] = useState<Live>(IDLE);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;

  const running = live.status === "running";

  const syncSnapshot = useCallback(async () => {
    try {
      const { pipeline } = await getPipelineStatus();
      setLive(fromSnapshot(pipeline));
    } catch {
      /* ignore — stream will populate */
    }
  }, []);

  // On mount: fetch current status, then subscribe to the live stream. On every
  // (re)connect, re-sync the snapshot so a mid-run refresh stays accurate.
  useEffect(() => {
    syncSnapshot();
    const stop = streamPipeline(
      (ev) => {
        setLive((prev) => reduce(prev, ev));
        if (ev.stage === "scout" && ev.kind === "completed") onJobsChanged();
        if (ev.stage === "qualify" && (ev.kind === "completed" || ev.kind === "progress"))
          onJobsChanged();
        if (ev.stage === "pipeline" && (ev.kind === "completed" || ev.kind === "failed")) {
          onJobsChanged();
          syncSnapshot();
        }
      },
      (isConnected) => {
        setConnected(isConnected);
        if (isConnected) syncSnapshot();
      },
    );
    return stop;
  }, [syncSnapshot, onJobsChanged]);

  const onRunPipeline = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const res = await runPipeline();
      if (res.rejected) {
        setNotice(res.reason ?? "a pipeline is already running");
      } else {
        setLive((prev) => ({ ...IDLE, pipelineId: res.pipeline_id ?? null, status: "running",
          scout: { status: "queued" }, qualify: { status: "idle" }, updatedAt: new Date().toISOString() }));
      }
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onRunStage = async (stage: "scout" | "qualify") => {
    setBusy(true);
    setNotice(null);
    try {
      const res = await runStage(stage);
      if (res.rejected) setNotice(res.reason ?? "a pipeline is already running");
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy(false);
    }
  };

  const scoutSummary =
    live.scout.status === "completed" && live.scout.counts
      ? `${(live.scout.counts.new as number) ?? 0} new · ${(live.scout.counts.fetched as number) ?? 0} fetched`
      : live.scout.status === "running"
        ? "scouting sources…"
        : "—";
  const qualSummary =
    live.qualify.status === "completed" && live.qualify.counts
      ? `${(live.qualify.counts.qualified as number) ?? 0} qualified · ${(live.qualify.counts.disqualified as number) ?? 0} disqualified`
      : live.qualify.status === "running"
        ? "scoring jobs…"
        : "—";

  return (
    <section className="rounded-2xl border border-neutral-800 bg-neutral-900/30 p-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Dot status={running ? "running" : live.status} />
        <h2 className="text-sm font-semibold">Autonomous pipeline</h2>
        <span className="text-xs text-neutral-500">
          {running
            ? "Scout → Qualify running…"
            : live.status === "completed"
              ? `last run ${timeAgo(live.updatedAt)}`
              : live.status === "failed"
                ? "last run failed"
                : "idle — one trigger runs Scout → Qualify"}
        </span>
        <span
          className={`ml-auto flex items-center gap-1.5 text-[11px] ${connected ? "text-emerald-500" : "text-neutral-600"}`}
          title={connected ? "live stream connected" : "reconnecting…"}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-500" : "bg-neutral-600"}`} />
          {connected ? "live" : "offline"}
        </span>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row">
        <StageCard title="Scout" s={live.scout} summary={scoutSummary} />
        <StageCard title="Qualify" s={live.qualify} summary={qualSummary} />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          onClick={onRunPipeline}
          disabled={busy || running}
          className="rounded-lg border border-emerald-600/40 bg-emerald-600/15 px-3.5 py-1.5 text-sm font-medium text-emerald-200 transition hover:bg-emerald-600/25 disabled:opacity-40"
        >
          {running ? "Running…" : "Run pipeline"}
        </button>
        <button
          onClick={() => onRunStage("scout")}
          disabled={busy || running}
          className="rounded-lg border border-neutral-700 bg-neutral-800/60 px-3 py-1.5 text-xs transition hover:border-neutral-500 disabled:opacity-40"
        >
          Scout only
        </button>
        <button
          onClick={() => onRunStage("qualify")}
          disabled={busy || running}
          className="rounded-lg border border-neutral-700 bg-neutral-800/60 px-3 py-1.5 text-xs transition hover:border-neutral-500 disabled:opacity-40"
        >
          Qualify only
        </button>
        {notice && <span className="text-xs text-amber-400">{notice}</span>}
      </div>
    </section>
  );
}
