import type { PipelineRun } from "@/lib/types";
import { timeAgo } from "./ui";

function StatusDot({ status }: { status: string | undefined }) {
  const color =
    status === "completed"
      ? "bg-emerald-500"
      : status === "running"
        ? "bg-amber-500 animate-pulse"
        : status === "failed"
          ? "bg-rose-500"
          : "bg-neutral-600";
  return <span className={`h-2 w-2 rounded-full ${color}`} />;
}

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="flex flex-col">
      <span className={`font-mono text-lg tabular-nums ${tone ?? "text-neutral-100"}`}>{value}</span>
      <span className="text-[11px] uppercase tracking-wide text-neutral-500">{label}</span>
    </div>
  );
}

function RunCard({
  title,
  run,
  busy,
  onRun,
  children,
}: {
  title: string;
  run: PipelineRun | null;
  busy: boolean;
  onRun: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="flex-1 rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
      <div className="mb-3 flex items-center gap-2">
        <StatusDot status={busy ? "running" : run?.status} />
        <h3 className="text-sm font-medium">{title}</h3>
        <span className="text-xs text-neutral-500">
          {busy ? "running…" : run ? timeAgo(run.finished_at ?? run.created_at) : "never run"}
        </span>
        <button
          onClick={onRun}
          disabled={busy}
          className="ml-auto rounded-lg border border-neutral-700 bg-neutral-800/60 px-3 py-1 text-xs font-medium transition hover:border-neutral-500 hover:bg-neutral-800 disabled:opacity-50"
        >
          {busy ? "Running…" : `Run ${title}`}
        </button>
      </div>
      <div className="flex gap-6">{children}</div>
    </div>
  );
}

export default function RunHeader({
  scoutRun,
  qualifyRun,
  scoutBusy,
  qualifyBusy,
  onRunScout,
  onRunQualify,
}: {
  scoutRun: PipelineRun | null;
  qualifyRun: PipelineRun | null;
  scoutBusy: boolean;
  qualifyBusy: boolean;
  onRunScout: () => void;
  onRunQualify: () => void;
}) {
  const sc = scoutRun?.counts ?? {};
  const qc = qualifyRun?.counts ?? {};
  return (
    <div className="flex flex-col gap-4 sm:flex-row">
      <RunCard title="Scout" run={scoutRun} busy={scoutBusy} onRun={onRunScout}>
        <Stat label="fetched" value={sc.fetched ?? "—"} />
        <Stat label="new" value={sc.new ?? "—"} tone="text-emerald-400" />
        <Stat label="deduped" value={(sc as { deduped?: number }).deduped ?? "—"} tone="text-neutral-400" />
      </RunCard>
      <RunCard title="Qualify" run={qualifyRun} busy={qualifyBusy} onRun={onRunQualify}>
        <Stat label="qualified" value={qc.qualified ?? "—"} tone="text-emerald-400" />
        <Stat label="disqualified" value={qc.disqualified ?? "—"} tone="text-rose-400" />
        <Stat label="gated out" value={qc.gated_out ?? "—"} tone="text-neutral-400" />
      </RunCard>
    </div>
  );
}
