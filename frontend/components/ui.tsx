import type { DimensionScores } from "@/lib/types";

export function pct(score: number | null | undefined): string {
  return score == null ? "—" : `${Math.round(score * 100)}`;
}

function scoreTone(score: number | null | undefined): string {
  if (score == null) return "text-slate-400";
  if (score >= 0.7) return "text-emerald-600";
  if (score >= 0.5) return "text-amber-600";
  return "text-rose-600";
}

function scoreBar(score: number | null | undefined): string {
  if (score == null) return "bg-slate-300";
  if (score >= 0.7) return "bg-emerald-500";
  if (score >= 0.5) return "bg-amber-500";
  return "bg-rose-500";
}

/** Score as a number + a thin proportional bar. */
export function ScoreCell({ score }: { score: number | null | undefined }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`w-7 text-right font-mono text-sm font-semibold tabular-nums ${scoreTone(score)}`}>
        {pct(score)}
      </span>
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
        <span
          className={`block h-full rounded-full transition-all duration-500 ${scoreBar(score)}`}
          style={{ width: `${score == null ? 0 : Math.round(score * 100)}%` }}
        />
      </span>
    </div>
  );
}

export function DecisionPill({ decision }: { decision: string | null | undefined }) {
  const map: Record<string, string> = {
    qualified: "border-emerald-200 bg-emerald-50 text-emerald-700",
    disqualified: "border-rose-200 bg-rose-50 text-rose-700",
  };
  const cls = decision ? map[decision] : "border-slate-200 bg-slate-50 text-slate-500";
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {decision ?? "unscored"}
    </span>
  );
}

/** Addendum 2.2: flags a score computed on aggregator snippet text only, so a
 * confident-looking number is never shown as if it rode on a full description. */
export function LimitedDataBadge() {
  return (
    <span
      title="Scored on a short snippet — the source text was limited, so treat this score as low-confidence."
      className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700"
    >
      limited data
    </span>
  );
}

export function Chip({ label, tone }: { label: string; tone: "match" | "miss" }) {
  const cls =
    tone === "match"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : "border-amber-200 bg-amber-50 text-amber-700";
  return <span className={`rounded-md border px-1.5 py-0.5 text-xs ${cls}`}>{label}</span>;
}

const DIM_LABELS: Record<keyof DimensionScores, string> = {
  skills: "Skills",
  experience: "Experience",
  roles_match: "Roles match",
};

export function DimensionBars({ dims }: { dims: DimensionScores }) {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
      {(Object.keys(DIM_LABELS) as (keyof DimensionScores)[]).map((k) => {
        const v = dims[k];
        return (
          <div key={k} className="space-y-1">
            <div className="flex justify-between text-xs text-slate-500">
              <span>{DIM_LABELS[k]}</span>
              <span className="font-mono tabular-nums text-slate-700">{pct(v)}</span>
            </div>
            <span className="block h-1 overflow-hidden rounded-full bg-slate-200">
              <span
                className={`block h-full rounded-full transition-all duration-500 ${scoreBar(v)}`}
                style={{ width: `${v == null ? 0 : Math.round(v * 100)}%` }}
              />
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function SourceBadge({ connector }: { connector: string | null }) {
  return (
    <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-xs text-slate-500">
      {connector ?? "—"}
    </span>
  );
}
