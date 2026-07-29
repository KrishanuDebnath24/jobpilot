"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CvDraft, CvReport } from "@/lib/types";
import {
  approveCvDrafts,
  downloadCv,
  exportCvs,
  getCvDrafts,
  getCvReport,
  prepareCvs,
  rejectCvDrafts,
  requalifyCvs,
  tailorCvs,
} from "@/lib/api";

const STATUS_STYLE: Record<string, string> = {
  draft_ready: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  approved: "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
  rejected: "border-rose-500/30 bg-rose-500/10 text-rose-200",
};

/** Aligned-CV review + approval (Phase 6.5). A Rewriter⇄Critic loop reshapes each selected
 *  job's CV until the Critic scores it aligned (or the round cap is hit) → review the
 *  alignment score, rounds, integrity flags and the evidence behind each added skill (locked
 *  facts untouched) → approve (select-all / mass / particulars) → Tailor to Irish-format
 *  PDF/DOCX → re-qualify → see the improvement deltas. Only user-selected jobs run; rejected
 *  drafts never export. */
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
            “Prepare CVs” runs analyze → tailor → export for the selected jobs and attaches an
            honest, job-specific CV to each job row (locked facts never change). The qualifier then
            scores those jobs against their attached CV.
          </p>
        </div>
        <button
          onClick={() => run("prepare", () => prepareCvs(selectedJobIds))}
          disabled={!!busy || selectedJobIds.length === 0}
          className="ml-auto rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-sm text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-40"
          title={
            selectedJobIds.length === 0
              ? "Select jobs in the table first"
              : "Analyze → align (Rewriter⇄Critic) → export, then the CV attaches to each job row"
          }
        >
          {busy === "prepare"
            ? "Preparing CVs…"
            : `Prepare CVs for ${selectedJobIds.length} selected`}
        </button>
        <button
          onClick={() => run("tailor", () => tailorCvs(selectedJobIds))}
          disabled={!!busy || selectedJobIds.length === 0}
          className="rounded-lg border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 transition hover:border-neutral-500 disabled:opacity-40"
          title={selectedJobIds.length === 0 ? "Select jobs in the table first" : "Tailor only (review, then approve/export yourself)"}
        >
          {busy === "tailor" ? "Tailoring…" : "Tailor only"}
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
  const content = draft.tailored_content_json;
  const changes = content?.change_notes ?? notes?.change_notes ?? [];
  // Locked facts are enforced in code (finalize) on every draft — always safe. Legacy rows
  // carried an explicit final_ok; default to true for alignment-loop rows.
  const lockedSafe = notes?.final_ok ?? true;

  // New alignment-loop surface (undefined on legacy drafts → those branches simply hide).
  const alignScore = notes?.alignment_score;
  const aligned = notes?.aligned;
  const roundsRun = notes?.rounds_run ?? notes?.rounds?.length;
  const maxRounds = notes?.max_rounds;
  const threshold = notes?.threshold;
  const flags = notes?.integrity_flags ?? [];
  const evidence = content?.skill_evidence ?? notes?.skill_evidence ?? [];
  const dims = notes?.dimension_scores ?? {};
  const unaddressed = notes?.unaddressed_requirements ?? [];

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
        {alignScore != null && (
          <span
            className={`rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${
              aligned
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                : "border-amber-500/30 bg-amber-500/10 text-amber-200"
            }`}
            title={
              threshold != null
                ? `${aligned ? "Cleared" : "Below"} the ${Math.round(threshold * 100)}% alignment bar`
                : undefined
            }
          >
            {aligned ? "aligned" : "best"} {Math.round(alignScore * 100)}%
          </span>
        )}
        {roundsRun != null && (
          <span className="rounded-md border border-neutral-700 px-1.5 py-0.5 text-[10px] text-neutral-400">
            {roundsRun}
            {maxRounds ? `/${maxRounds}` : ""} round{roundsRun === 1 ? "" : "s"}
          </span>
        )}
        {flags.length > 0 ? (
          <span
            className="rounded-md border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] text-rose-200"
            title={flags.join(" · ")}
          >
            ⚠ {flags.length} integrity flag{flags.length === 1 ? "" : "s"}
          </span>
        ) : (
          lockedSafe && (
            <span className="rounded-md border border-neutral-700 px-1.5 py-0.5 text-[10px] text-neutral-400">
              🔒 locked facts verified
            </span>
          )
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
        <div className="mt-2 space-y-3 border-t border-neutral-800 pt-2 text-xs">
          {/* Alignment summary — how well, over how many rounds */}
          {alignScore != null && (
            <p className="text-neutral-400">
              {aligned ? (
                <span className="text-emerald-300">Aligned</span>
              ) : (
                <span className="text-amber-300">Did not reach the bar</span>
              )}{" "}
              at {Math.round(alignScore * 100)}%
              {threshold != null ? ` (bar ${Math.round(threshold * 100)}%)` : ""}
              {roundsRun != null
                ? ` · ${roundsRun}${maxRounds ? ` of ${maxRounds}` : ""} round${roundsRun === 1 ? "" : "s"}`
                : ""}
              {notes?.final_verdict ? ` · critic verdict: ${notes.final_verdict}` : ""}
            </p>
          )}

          {/* Integrity flags — honesty problems the Critic raised (forces another round) */}
          {flags.length > 0 && (
            <div className="rounded-md border border-rose-500/30 bg-rose-500/10 p-2">
              <p className="mb-0.5 font-medium text-rose-200">⚠ Integrity flags</p>
              <ul className="list-disc space-y-0.5 pl-4 text-rose-200/90">
                {flags.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Per-dimension alignment scores */}
          {Object.keys(dims).length > 0 && (
            <div>
              <p className="mb-1 text-neutral-500">Alignment by dimension</p>
              <div className="space-y-1">
                {Object.entries(dims).map(([k, v]) => (
                  <div key={k} className="flex items-center gap-2">
                    <span className="w-36 truncate capitalize text-neutral-400">{k.replace(/_/g, " ")}</span>
                    <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-800">
                      <span
                        className="block h-full rounded-full bg-sky-500/70"
                        style={{ width: `${Math.round((Number(v) || 0) * 100)}%` }}
                      />
                    </span>
                    <span className="w-8 text-right font-mono tabular-nums text-neutral-500">
                      {Math.round((Number(v) || 0) * 100)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Added-skill evidence — skills not in the base list + the real CV text that backs each */}
          {evidence.length > 0 && (
            <div>
              <p className="mb-0.5 text-neutral-500">
                Skills added for this job{" "}
                <span className="normal-case text-neutral-600">(each grounded in your real CV)</span>
              </p>
              <ul className="space-y-1">
                {evidence.map((e, i) => (
                  <li key={i} className="rounded-md border border-neutral-800/70 px-2 py-1">
                    <span className="font-medium text-neutral-200">{e.skill}</span>
                    {e.basis && (
                      <span className="ml-2 rounded border border-neutral-700 px-1 py-0.5 text-[10px] uppercase tracking-wide text-neutral-400">
                        {e.basis}
                      </span>
                    )}
                    {e.evidence && <span className="mt-0.5 block text-neutral-500">from: “{e.evidence}”</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Genuine gaps left honest — the loop won't claim these */}
          {unaddressed.length > 0 && (
            <p className="text-neutral-500">
              <span className="text-neutral-400">Still unaddressed (kept honest):</span> {unaddressed.join(", ")}
            </p>
          )}

          {/* Tailored profile + traceable change notes */}
          {content?.summary && (
            <p className="text-neutral-300">
              <span className="text-neutral-500">Profile: </span>
              {content.summary}
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

          {/* Legacy critic line — only for pre-alignment-loop drafts (no alignment score) */}
          {alignScore == null && notes?.rounds && notes.rounds.length > 0 && (
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
