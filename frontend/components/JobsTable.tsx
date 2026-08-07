"use client";

import { useEffect, useState } from "react";
import type { CvDraft, Job } from "@/lib/types";
import { ApiError, downloadCv, getBaseCvBlob, getCvDraftBlob } from "@/lib/api";
import {
  AtsBadge,
  Chip,
  DecisionPill,
  DimensionBars,
  LimitedDataBadge,
  ScoreCell,
  SourceBadge,
} from "./ui";

/** CV attached to a job: the tailored PDF if one is exported for this job, else the base
 *  uploaded résumé. Fetched with auth into a blob URL (iframes can't send a bearer token),
 *  shown inline for PDFs, with download buttons. Only mounts when the row is expanded. */
function JobCv({ cvDraft }: { cvDraft: CvDraft | null }) {
  // A tailored CV counts only once exported (pdf_ref); otherwise fall back to the base CV.
  const draftId = cvDraft?.pdf_ref ? cvDraft.id : null;
  const tailored = draftId !== null;
  const [blob, setBlob] = useState<{ url: string; type: string } | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "none" | "error">("loading");

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    setStatus("loading");
    setBlob(null);
    (async () => {
      try {
        const res = draftId ? await getCvDraftBlob(draftId, "pdf") : await getBaseCvBlob();
        if (cancelled) return URL.revokeObjectURL(res.url);
        created = res.url;
        setBlob(res);
        setStatus("ready");
      } catch (e) {
        if (cancelled) return;
        setStatus(e instanceof ApiError && e.status === 404 ? "none" : "error");
      }
    })();
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [draftId]);

  const isPdf = !!blob?.type.includes("pdf");
  const ext = isPdf ? "pdf" : "docx";

  return (
    <div className="space-y-2">
      <h4 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        {tailored ? "Tailored CV for this job" : "Your uploaded CV"}
        {tailored && (
          <span className="rounded-md border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium normal-case text-sky-700">
            tailored · honest
          </span>
        )}
      </h4>
      {status === "loading" && (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-200 border-t-slate-400" />
          Loading CV…
        </div>
      )}
      {status === "none" && (
        <p className="text-sm text-slate-500">
          No CV on file yet — attach one in “Your CV / résumé”.
        </p>
      )}
      {status === "error" && <p className="text-sm text-rose-600">Couldn’t load the CV.</p>}
      {status === "ready" && blob && (
        <>
          {isPdf ? (
            <iframe
              src={blob.url}
              title="CV preview"
              className="h-96 w-full rounded-lg border border-slate-200 bg-white shadow-card"
            />
          ) : (
            <p className="text-sm text-slate-500">
              Inline preview isn’t available for DOCX — download to view.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {tailored ? (
              <>
                {cvDraft?.pdf_ref && (
                  <button
                    onClick={() => downloadCv(cvDraft.id, "pdf")}
                    className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
                  >
                    Download PDF
                  </button>
                )}
                {cvDraft?.docx_ref && (
                  <button
                    onClick={() => downloadCv(cvDraft.id, "docx")}
                    className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
                  >
                    Download DOCX
                  </button>
                )}
              </>
            ) : (
              <a
                href={blob.url}
                download={`cv.${ext}`}
                className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50"
              >
                Download {ext.toUpperCase()}
              </a>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function JobDetail({ job, cvDraft }: { job: Job; cvDraft: CvDraft | null }) {
  const q = job.qualification;
  const rj = q?.reasons_json ?? null;
  return (
    <div className="space-y-4 border-l-2 border-indigo-400 bg-slate-50/80 px-6 py-4">
      {(job.employment_type || job.seniority) && (
        <div className="flex flex-wrap gap-1.5">
          {job.seniority && (
            <span className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600">
              {job.seniority}
            </span>
          )}
          {job.employment_type && (
            <span className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600">
              {job.employment_type.replace(/_/g, " ")}
            </span>
          )}
        </div>
      )}
      <JobCv cvDraft={cvDraft} />

      {!q ? (
        <p className="text-sm text-slate-500">
          Not yet qualified — run Qualify to score this job.
        </p>
      ) : (
        <>
          {rj?.limited_data ? (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              Limited data — this job&apos;s source provided only a short snippet, so the
              score is low-confidence (no full description was available).
            </p>
          ) : null}

          {rj?.gate === "fail" ? (
            <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
              Hard-disqualified by the must-have gate (no LLM scoring performed).
            </p>
          ) : null}

          {rj?.reasons?.length ? (
            <div className="space-y-1.5">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                Why this score
              </h4>
              <ul className="space-y-1 text-sm text-slate-700">
                {rj.reasons.map((r, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-slate-300">•</span>
                    <span>{r}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {rj?.dimension_scores ? (
            <div className="space-y-1.5">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                Dimension breakdown
              </h4>
              <DimensionBars dims={rj.dimension_scores} />
            </div>
          ) : null}

          <div className="flex flex-wrap gap-6">
            {q.matched_skills.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
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
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
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
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Job description
            {job.description_completeness === "snippet" && (
              <span className="ml-2 font-normal normal-case text-amber-600">
                · snippet only — source did not provide the full text
              </span>
            )}
          </h4>
          <div className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm leading-relaxed text-slate-700 shadow-card">
            {job.description}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-4">
        {job.url && (
          <a
            href={job.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm font-medium text-indigo-600 underline decoration-indigo-200 underline-offset-4 transition hover:text-indigo-700 hover:decoration-indigo-400"
          >
            View posting ↗
          </a>
        )}
        {/* The resolved application page, when it differs from the listing link — this is
            the page a later stage would actually fill in. */}
        {job.apply_url && job.apply_url !== job.url && (
          <a
            href={job.apply_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm font-medium text-emerald-700 underline decoration-emerald-200 underline-offset-4 transition hover:decoration-emerald-400"
            title={`Application form${job.apply_board_slug ? ` — ${job.apply_board_slug}` : ""}`}
          >
            Application form ↗
          </a>
        )}
      </div>
    </div>
  );
}

function JobRow({
  job,
  selected,
  onToggleSelect,
  cvDraft,
  open,
  onToggleOpen,
}: {
  job: Job;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  cvDraft?: CvDraft | null;
  /** Accordion: the open row is owned by the table so only one can be expanded. */
  open: boolean;
  onToggleOpen: () => void;
}) {
  const colSpan = onToggleSelect ? 7 : 6;
  return (
    <>
      <tr
        onClick={onToggleOpen}
        className={`group cursor-pointer border-t border-slate-100 transition-colors ${
          open ? "bg-indigo-50/60" : selected ? "bg-indigo-50/30" : "hover:bg-slate-50"
        }`}
      >
        {onToggleSelect && (
          <td className="py-3 pl-4 pr-1" onClick={(e) => e.stopPropagation()}>
            <input
              type="checkbox"
              checked={!!selected}
              onChange={() => onToggleSelect(job.id)}
              className="h-4 w-4 cursor-pointer rounded border-slate-300 accent-indigo-600"
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
            <svg
              className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform duration-200 ${
                open ? "rotate-90 text-indigo-600" : "group-hover:text-slate-600"
              }`}
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="m6 3.5 5 4.5-5 4.5" />
            </svg>
            <span className="font-medium text-slate-900">{job.title ?? "—"}</span>
          </div>
        </td>
        <td className="px-2 py-3 text-slate-700">{job.company ?? "—"}</td>
        <td className="px-2 py-3 text-slate-500">{job.location ?? "—"}</td>
        <td className="px-2 py-3 pr-4">
          <div className="flex flex-wrap items-center gap-1.5">
            <SourceBadge connector={job.connector} />
            {/* Where the form lives, once stage 2 has followed the link. Absent until
                resolved, so an unresolved pool looks exactly as it did before. */}
            <AtsBadge ats={job.apply_ats} slug={job.apply_board_slug} stale={job.apply_stale} />
          </div>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={colSpan} className="p-0">
            <JobDetail job={job} cvDraft={cvDraft ?? null} />
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
  cvDraftByJobId,
}: {
  jobs: Job[];
  selected?: Set<string>;
  onToggleSelect?: (id: string) => void;
  cvDraftByJobId?: Record<string, CvDraft>;
}) {
  // Accordion: one expanded row at a time, so opening a job closes whichever was open.
  const [openId, setOpenId] = useState<string | null>(null);

  if (jobs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 py-16 text-center">
        <p className="text-sm font-medium text-slate-600">No jobs match this filter.</p>
        <p className="mt-1 text-xs text-slate-400">
          Try widening the filters above, or run the pipeline to scout more.
        </p>
      </div>
    );
  }
  return (
    /* The list scrolls inside itself rather than pushing the page down — with 100+ jobs the
       filters and everything below stay reachable. The header is sticky against THIS box. */
    <div className="max-h-[70vh] overflow-auto rounded-xl border border-slate-200 bg-white shadow-card">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50/95 text-xs uppercase tracking-wide text-slate-500 backdrop-blur">
          <tr>
            {onToggleSelect && <th className="py-3 pl-4 pr-1 font-semibold"> </th>}
            <th className={`py-3 pr-2 font-semibold ${onToggleSelect ? "pl-1" : "pl-4"}`}>Score</th>
            <th className="px-2 py-3 font-semibold">Decision</th>
            <th className="px-2 py-3 font-semibold">Title</th>
            <th className="px-2 py-3 font-semibold">Company</th>
            <th className="px-2 py-3 font-semibold">Location</th>
            <th className="px-2 py-3 pr-4 font-semibold">Source</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              selected={selected?.has(job.id)}
              onToggleSelect={onToggleSelect}
              cvDraft={cvDraftByJobId?.[job.id] ?? null}
              open={openId === job.id}
              onToggleOpen={() => setOpenId((cur) => (cur === job.id ? null : job.id))}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
