"use client";

import { useState } from "react";
import type { Job } from "@/lib/types";
import {
  Chip,
  DecisionPill,
  DimensionBars,
  LimitedDataBadge,
  ScoreCell,
  SourceBadge,
  pct,
} from "./ui";

function JobDetail({ job }: { job: Job }) {
  const q = job.qualification;
  const rj = q?.reasons_json ?? null;
  return (
    <div className="space-y-4 border-l-2 border-neutral-800 bg-neutral-950/40 px-6 py-4">
      {!q ? (
        <p className="text-sm text-neutral-500">
          Not yet qualified — run Qualify to score this job.
        </p>
      ) : (
        <>
          {rj?.limited_data ? (
            <p className="text-sm text-amber-300">
              Limited data — this job&apos;s source provided only a short snippet, so the
              score is low-confidence (no full description was available).
            </p>
          ) : null}

          {rj?.gate === "fail" ? (
            <p className="text-sm text-rose-300">
              Hard-disqualified by the must-have gate (no LLM scoring performed).
            </p>
          ) : null}

          {rj?.reasons?.length ? (
            <div className="space-y-1.5">
              <h4 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                Why this score
              </h4>
              <ul className="space-y-1 text-sm text-neutral-300">
                {rj.reasons.map((r, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-neutral-600">•</span>
                    <span>{r}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {rj?.dimension_scores ? (
            <div className="space-y-1.5">
              <h4 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                Dimension breakdown
                {rj.semantic_fit != null && (
                  <span className="ml-2 font-normal normal-case text-neutral-400">
                    · semantic fit {pct(rj.semantic_fit)}
                  </span>
                )}
              </h4>
              <DimensionBars dims={rj.dimension_scores} />
            </div>
          ) : null}

          <div className="flex flex-wrap gap-6">
            {q.matched_skills.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                  Matched skills
                </h4>
                <div className="flex flex-wrap gap-1.5">
                  {q.matched_skills.map((s) => (
                    <Chip key={s} label={s} tone="match" />
                  ))}
                </div>
              </div>
            )}
            {q.missing_skills.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                  Missing skills
                </h4>
                <div className="flex flex-wrap gap-1.5">
                  {q.missing_skills.map((s) => (
                    <Chip key={s} label={s} tone="miss" />
                  ))}
                </div>
              </div>
            )}
          </div>
        </>
      )}

      {job.description && (
        <div className="space-y-1.5">
          <h4 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
            Job description
            {job.description_completeness === "snippet" && (
              <span className="ml-2 font-normal normal-case text-amber-300/80">
                · snippet only — source did not provide the full text
              </span>
            )}
          </h4>
          <div className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg border border-neutral-800/70 bg-neutral-900/40 px-3 py-2 text-sm leading-relaxed text-neutral-300">
            {job.description}
          </div>
        </div>
      )}

      {job.url && (
        <a
          href={job.url}
          target="_blank"
          rel="noreferrer"
          className="inline-block text-sm text-neutral-400 underline decoration-neutral-700 underline-offset-4 hover:text-neutral-200"
        >
          View posting ↗
        </a>
      )}
    </div>
  );
}

function JobRow({
  job,
  selected,
  onToggleSelect,
}: {
  job: Job;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const colSpan = onToggleSelect ? 7 : 6;
  return (
    <>
      <tr
        onClick={() => setOpen((o) => !o)}
        className="cursor-pointer border-t border-neutral-800/70 transition hover:bg-neutral-900/50"
      >
        {onToggleSelect && (
          <td className="py-3 pl-4 pr-1" onClick={(e) => e.stopPropagation()}>
            <input
              type="checkbox"
              checked={!!selected}
              onChange={() => onToggleSelect(job.id)}
              className="h-4 w-4 accent-emerald-500"
              aria-label="select job"
            />
          </td>
        )}
        <td className={`py-3 pr-2 ${onToggleSelect ? "pl-1" : "pl-4"}`}>
          <ScoreCell score={job.qualification?.score ?? null} />
        </td>
        <td className="px-2 py-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <DecisionPill decision={job.qualification?.decision} />
            {job.qualification?.reasons_json?.limited_data ? <LimitedDataBadge /> : null}
          </div>
        </td>
        <td className="px-2 py-3">
          <div className="flex items-center gap-2">
            <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
            <span className="font-medium text-neutral-100">{job.title ?? "—"}</span>
          </div>
        </td>
        <td className="px-2 py-3 text-neutral-300">{job.company ?? "—"}</td>
        <td className="px-2 py-3 text-neutral-400">{job.location ?? "—"}</td>
        <td className="px-2 py-3 pr-4">
          <SourceBadge connector={job.connector} />
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={colSpan} className="p-0">
            <JobDetail job={job} />
          </td>
        </tr>
      )}
    </>
  );
}

export default function JobsTable({
  jobs,
  selected,
  onToggleSelect,
}: {
  jobs: Job[];
  selected?: Set<string>;
  onToggleSelect?: (id: string) => void;
}) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-neutral-800 py-16 text-center text-sm text-neutral-500">
        No jobs match this filter.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="sticky top-0 bg-neutral-900/80 text-xs uppercase tracking-wide text-neutral-500 backdrop-blur">
          <tr>
            {onToggleSelect && <th className="py-2.5 pl-4 pr-1 font-medium"> </th>}
            <th className={`py-2.5 pr-2 font-medium ${onToggleSelect ? "pl-1" : "pl-4"}`}>Score</th>
            <th className="px-2 py-2.5 font-medium">Decision</th>
            <th className="px-2 py-2.5 font-medium">Title</th>
            <th className="px-2 py-2.5 font-medium">Company</th>
            <th className="px-2 py-2.5 font-medium">Location</th>
            <th className="px-2 py-2.5 pr-4 font-medium">Source</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              selected={selected?.has(job.id)}
              onToggleSelect={onToggleSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
