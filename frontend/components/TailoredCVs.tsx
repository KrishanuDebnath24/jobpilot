"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CvDraft, CvReport } from "@/lib/types";
import {
  approveCvDrafts,
  downloadCv,
  exportCvs,
  getCvDrafts,
  getCvReport,
  rejectCvDrafts,
  requalifyCvs,
  tailorCvs,
} from "@/lib/api";

const STATUS_STYLE: Record<string, string> = {
  draft_ready: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  approved: "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
  rejected: "border-rose-500/30 bg-rose-500/10 text-rose-200",
};

/** Tailored-CV review + approval (Phase 6.5). Tailor selected jobs → review the honest
 *  changes (locked facts untouched, no invented skills) → approve (select-all / mass /
 *  particulars) → export Irish-format PDF/DOCX → re-qualify → see the improvement deltas.
 *  Only user-selected jobs are tailored; rejected drafts never export. */
export default function TailoredCVs({
  selectedJobIds,
  jobTitleById,
}: {
  selectedJobIds: string[];
  jobTitleById: (id: string) => string;
}) {
  const [drafts, setDrafts] = useState<CvDraft[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<CvReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      setDrafts(await getCvDrafts());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    poll.current = setInterval(load, 5000); // pick up drafts as async tasks finish
    return () => {
      if (poll.current) clearInterval(poll.current);
    };
  }, [load]);

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      setBusy(label);
      setError(null);
      try {
        await fn();
        await load();
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const togglePick = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const draftReady = useMemo(() => drafts.filter((d) => d.status === "draft_ready"), [drafts]);
  const approved = useMemo(() => drafts.filter((d) => d.status === "approved"), [drafts]);
  const pickedReady = useMemo(
    () => [...picked].filter((id) => draftReady.some((d) => d.id === id)),
    [picked, draftReady],
  );
  const allReadyPicked = draftReady.length > 0 && pickedReady.length === draftReady.length;

  return (
    <section className="mt-6 rounded-2xl border border-neutral-800 bg-neutral-900/30 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <div>
          <h2 className="text-sm font-semibold text-neutral-200">Tailored CVs</h2>
          <p className="text-xs text-neutral-500">
            Tailor selected jobs honestly (real experience in the job’s language; locked facts never
            change), then approve → export → re-qualify.
          </p>
        </div>
        <button
          onClick={() =>
            run("tailor", () => tailorCvs(selectedJobIds))
          }
          disabled={!!busy || selectedJobIds.length === 0}
          className="ml-auto rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-sm text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-40"
          title={selectedJobIds.length === 0 ? "Select jobs in the table first" : ""}
        >
          {busy === "tailor" ? "Tailoring…" : `Tailor ${selectedJobIds.length} selected`}
        </button>
      </div>

      {drafts.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-neutral-800 pt-3 text-xs">
          <button
            onClick={() => setPicked(new Set(allReadyPicked ? [] : draftReady.map((d) => d.id)))}
            className="rounded-md border border-neutral-700 px-2 py-1 text-neutral-300 hover:border-neutral-500"
          >
            {allReadyPicked ? "Clear" : "Select all"}
          </button>
          <button
            onClick={() => run("approve-sel", () => approveCvDrafts(pickedReady))}
            disabled={!!busy || pickedReady.length === 0}
            className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-emerald-200 disabled:opacity-40"
          >
            Approve selected ({pickedReady.length})
          </button>
          <button
            onClick={() => run("approve-all", () => approveCvDrafts(undefined, true))}
            disabled={!!busy || draftReady.length === 0}
            className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-emerald-200 disabled:opacity-40"
          >
            Approve all
          </button>
          <button
            onClick={() => run("reject-sel", () => rejectCvDrafts(pickedReady))}
            disabled={!!busy || pickedReady.length === 0}
            className="rounded-md border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-rose-200 disabled:opacity-40"
          >
            Reject selected
          </button>
          <span className="mx-1 h-4 w-px bg-neutral-700" />
          <button
            onClick={() => run("export", () => exportCvs())}
            disabled={!!busy || approved.length === 0}
            className="rounded-md border border-neutral-700 px-2 py-1 text-neutral-200 disabled:opacity-40"
          >
            Export approved ({approved.length})
          </button>
          <button
            onClick={() => run("requalify", () => requalifyCvs())}
            disabled={!!busy || approved.length === 0}
            className="rounded-md border border-neutral-700 px-2 py-1 text-neutral-200 disabled:opacity-40"
          >
            Re-qualify approved
          </button>
          <button
            onClick={() => run("report", async () => setReport(await getCvReport()))}
            disabled={!!busy}
            className="rounded-md border border-neutral-700 px-2 py-1 text-neutral-200 disabled:opacity-40"
          >
            Improvement report
          </button>
        </div>
      )}

      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}

      {report && (
        <div className="mt-3 rounded-lg border border-neutral-800 bg-neutral-950/50 p-3 text-sm">
          <p className="text-neutral-300">
            {report.improved} of {report.total} scored higher · {report.unchanged} unchanged ·{" "}
            {report.worse} lower
          </p>
          <ul className="mt-1 space-y-0.5 text-xs text-neutral-400">
            {report.report.map((r) => (
              <li key={r.draft_id}>
                {r.job_title ?? "Job"}
                {r.company ? ` · ${r.company}` : ""}:{" "}
                {r.before_score != null ? Math.round(r.before_score * 100) : "—"} →{" "}
                {r.after_score != null ? Math.round(r.after_score * 100) : "—"}
                {r.delta != null && (
                  <span className={r.delta > 0 ? "text-emerald-300" : r.delta < 0 ? "text-rose-300" : ""}>
                    {" "}
                    ({r.delta > 0 ? "+" : ""}
                    {Math.round(r.delta * 100)})
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 space-y-2">
        {drafts.length === 0 && (
          <p className="py-4 text-center text-sm text-neutral-500">
            No tailored drafts yet. Select jobs in the table, then “Tailor selected”.
          </p>
        )}
        {drafts.map((d) => (
          <DraftCard
            key={d.id}
            draft={d}
            title={jobTitleById(d.job_id)}
            picked={picked.has(d.id)}
            onPick={() => togglePick(d.id)}
            onApprove={() => run("approve1", () => approveCvDrafts([d.id]))}
            onReject={() => run("reject1", () => rejectCvDrafts([d.id]))}
            busy={!!busy}
          />
        ))}
      </div>
    </section>
  );
}

function DraftCard({
  draft,
  title,
  picked,
  onPick,
  onApprove,
  onReject,
  busy,
}: {
  draft: CvDraft;
  title: string;
  picked: boolean;
  onPick: () => void;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const notes = draft.critic_notes_json;
  const changes = draft.tailored_content_json?.change_notes ?? notes?.change_notes ?? [];
  const lockedSafe = notes?.final_ok ?? true;
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {draft.status === "draft_ready" && (
          <input type="checkbox" checked={picked} onChange={onPick} className="accent-sky-500" />
        )}
        <span className="text-sm font-medium text-neutral-200">{title}</span>
        <span className={`rounded-md border px-1.5 py-0.5 text-[10px] ${STATUS_STYLE[draft.status]}`}>
          {draft.status.replace("_", " ")}
        </span>
        {lockedSafe && (
          <span className="rounded-md border border-neutral-700 px-1.5 py-0.5 text-[10px] text-neutral-400">
            🔒 locked facts verified
          </span>
        )}
        {draft.before_score != null && (
          <span className="text-xs text-neutral-500">
            score {Math.round(draft.before_score * 100)}
            {draft.after_score != null && ` → ${Math.round(draft.after_score * 100)}`}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={() => setOpen((o) => !o)} className="text-xs text-neutral-400 hover:text-neutral-200">
            {open ? "Hide" : "Review"}
          </button>
          {draft.status === "draft_ready" && (
            <>
              <button
                onClick={onApprove}
                disabled={busy}
                className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-200 disabled:opacity-40"
              >
                Approve
              </button>
              <button
                onClick={onReject}
                disabled={busy}
                className="rounded-md border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-xs text-rose-200 disabled:opacity-40"
              >
                Reject
              </button>
            </>
          )}
          {draft.pdf_ref && (
            <button
              onClick={() => downloadCv(draft.id, "pdf")}
              className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-200"
            >
              PDF
            </button>
          )}
          {draft.docx_ref && (
            <button
              onClick={() => downloadCv(draft.id, "docx")}
              className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-200"
            >
              DOCX
            </button>
          )}
        </div>
      </div>

      {open && (
        <div className="mt-2 space-y-2 border-t border-neutral-800 pt-2 text-xs">
          {draft.tailored_content_json?.summary && (
            <p className="text-neutral-300">
              <span className="text-neutral-500">Profile: </span>
              {draft.tailored_content_json.summary}
            </p>
          )}
          {changes.length > 0 && (
            <div>
              <p className="mb-0.5 text-neutral-500">What changed and why (grounded in your real experience):</p>
              <ul className="space-y-0.5">
                {changes.map((c, i) => (
                  <li key={i} className="text-neutral-400">
                    <span className="text-neutral-300">{c.field}</span>
                    {c.why ? ` — ${c.why}` : ""}
                    {c.grounded_in ? ` (from: ${c.grounded_in})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {notes?.rounds && notes.rounds.length > 0 && (
            <p className="text-neutral-500">
              Critic: {notes.rounds.map((r) => r.verdict).filter(Boolean).join(" → ") || "reviewed"}
              {notes.caught_before_enforce &&
                (notes.caught_before_enforce.violations.length +
                  notes.caught_before_enforce.fabricated.length >
                  0
                  ? ` · fixed ${
                      notes.caught_before_enforce.violations.length +
                      notes.caught_before_enforce.fabricated.length
                    } integrity issue(s)`
                  : " · clean")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
