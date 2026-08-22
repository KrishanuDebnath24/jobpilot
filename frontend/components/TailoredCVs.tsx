"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CvDraft, CvReport, Job, TailoredContent } from "@/lib/types";
import {
  approveCvDrafts,
  downloadCv,
  editCvDraft,
  exportCvs,
  getCvDrafts,
  getCvReport,
  prepareCvs,
  rejectCvDrafts,
  requalifyCvs,
  rewordCvDraft,
  tailorCvs,
} from "@/lib/api";
import StageStrip, { type Stage } from "./StageStrip";

const STATUS_STYLE: Record<string, string> = {
  draft_ready: "border-amber-200 bg-amber-50 text-amber-700",
  approved: "border-emerald-200 bg-emerald-50 text-emerald-700",
  rejected: "border-rose-200 bg-rose-50 text-rose-700",
};

/* ---------------------------------------------------------------------------- *
 * Tracking the fire-and-forget actions.
 *
 * /cv/tailor, /cv/prepare, /cv/export and /cv/requalify all answer 202 the moment the task
 * is queued, so "await the request" tells us nothing about the work. Instead each action
 * records what the world looked like before it started, and the existing 5s draft poll is
 * used to notice the result landing. Purely a view concern — no endpoint or task behaviour
 * is touched.
 * ---------------------------------------------------------------------------- */
type OpKind = "tailor" | "prepare" | "export" | "requalify";

type Op = {
  kind: OpKind;
  title: string;
  startedAt: number;
  jobCount: number;
  /** Draft ids present before the action fired — anything new is this action's output. */
  before: string[];
  /** Drafts this action should produce files/scores for (export & re-qualify). */
  targets: string[];
  /** Stage keys observed as complete. */
  done: string[];
  /** Set once we stop watching, so the strip can say so instead of pulsing forever. */
  timedOut?: boolean;
};

const OP_STAGES: Record<OpKind, Stage[]> = {
  // Mirrors run_prepare_cvs: analyze → align → approve → export → re-qualify.
  prepare: [
    { key: "analyze", label: "analyze", unobservable: true },
    // Wording stays mode-neutral: whether a Critic grades the draft is a server-side
    // setting (cv_max_rounds), which the client can't see before the run.
    { key: "align", label: "rewrite + honesty gate" },
    { key: "approve", label: "approve", unobservable: true },
    { key: "export", label: "export PDF/DOCX" },
    { key: "requalify", label: "re-qualify" },
  ],
  tailor: [
    { key: "analyze", label: "analyze if needed", unobservable: true },
    // Wording stays mode-neutral: whether a Critic grades the draft is a server-side
    // setting (cv_max_rounds), which the client can't see before the run.
    { key: "align", label: "rewrite + honesty gate" },
    { key: "draft", label: "draft ready to review" },
  ],
  export: [{ key: "export", label: "render PDF/DOCX" }],
  requalify: [{ key: "requalify", label: "re-score against the job" }],
};

const OP_TITLE: Record<OpKind, string> = {
  prepare: "Preparing CVs",
  tailor: "Tailoring",
  export: "Exporting",
  requalify: "Re-qualifying",
};

/** How long to keep watching before admitting we've stopped following it. */
const OP_WATCH_MS = 8 * 60 * 1000;

/** Which stages of `op` the polled drafts now prove are finished. */
function observeOp(op: Op, drafts: CvDraft[]): string[] {
  const done = new Set(op.done);
  const before = new Set(op.before);
  const created = drafts.filter((d) => !before.has(d.id));

  if (op.kind === "tailor" || op.kind === "prepare") {
    // A new draft proves the loop finished for ONE job, so only tick the stage once every
    // selected job has produced one — otherwise a 5-job batch would look finished after the
    // first. Partial progress is reported in the strip's note instead.
    if (created.length >= Math.max(1, op.jobCount)) {
      done.add("align");
      if (op.kind === "tailor") done.add("draft");
    }
  }
  if (op.kind === "prepare") {
    if (created.some((d) => d.status === "approved")) done.add("approve");
    if (created.some((d) => d.pdf_ref)) done.add("export");
    if (created.some((d) => d.after_score !== null)) done.add("requalify");
  }
  // Batch stages tick only when EVERY target has landed — one finished file out of five is
  // not a finished export. Partial counts are surfaced in the strip's note instead.
  if (op.kind === "export" && op.targets.length > 0) {
    if (op.targets.every((id) => drafts.find((d) => d.id === id)?.pdf_ref)) done.add("export");
  }
  if (op.kind === "requalify" && op.targets.length > 0) {
    if (op.targets.every((id) => drafts.find((d) => d.id === id)?.after_score != null)) {
      done.add("requalify");
    }
  }
  return [...done];
}

/** How many of a batch op's targets have visibly landed — for the progress note. */
function opLanded(op: Op, drafts: CvDraft[]): number {
  if (op.kind === "export") {
    return op.targets.filter((id) => drafts.find((d) => d.id === id)?.pdf_ref).length;
  }
  if (op.kind === "requalify") {
    return op.targets.filter((id) => drafts.find((d) => d.id === id)?.after_score != null).length;
  }
  const before = new Set(op.before);
  return drafts.filter((d) => !before.has(d.id)).length;
}

/** An op is finished when every stage we can actually verify has been verified. */
function opComplete(op: Op): boolean {
  const observable = OP_STAGES[op.kind].filter((s) => !s.unobservable).map((s) => s.key);
  return observable.length > 0 && observable.every((k) => op.done.includes(k));
}

/** Aligned-CV review + approval (Phase 6.5). The Rewriter reshapes each selected job's CV
 *  and the deterministic gate re-imposes locked facts and drops un-evidenced skills → review
 *  the evidence behind each added skill (and, when the Critic loop is enabled server-side,
 *  the alignment score, rounds and integrity flags) → approve (select-all / mass /
 *  particulars) → Tailor to Irish-format PDF/DOCX → re-qualify → see the improvement deltas.
 *  Single-pass drafts arrive UNSCORED, which puts the judgement on this review step — the
 *  reason it exists. Only user-selected jobs run; rejected
 *  drafts never export. */
export default function TailoredCVs({
  selectedJobIds,
  jobById,
  refreshSignal = 0,
}: {
  selectedJobIds: string[];
  /** The whole job, not just its title — the review and edit panels show its description
   *  beside the CV. Dashboard already holds these, so this adds no request. */
  jobById: (id: string) => Job | null;
  refreshSignal?: number;
}) {
  const [drafts, setDrafts] = useState<CvDraft[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<CvReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // View-only: draft ids the user has cleared from the list. Never a delete — the drafts and
  // their files are untouched, and "Show all" brings them straight back.
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  // Drafts that appeared during the most recent action, highlighted so a new arrival in a
  // long list is obvious.
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [op, setOp] = useState<Op | null>(null);
  // Accordion: at most one draft expanded, and at most one editor open (which implies its
  // card is expanded). Owned here rather than per-card so opening one closes the rest.
  const [openId, setOpenId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
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

  // Reload immediately when a Concierge action changed drafts (edit/tailor/prepare/approve),
  // so the user sees the update without waiting for the poll or refreshing the page.
  useEffect(() => {
    if (refreshSignal) load();
  }, [refreshSignal, load]);

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

  /** Fire one of the queued actions and keep reporting it until the drafts prove it landed. */
  const startOp = useCallback(
    async (kind: OpKind, jobCount: number, targets: string[], fn: () => Promise<unknown>) => {
      setBusy(kind);
      setError(null);
      setFresh(new Set());
      const before = drafts.map((d) => d.id);
      try {
        await fn();
        setOp({
          kind, title: OP_TITLE[kind], startedAt: Date.now(),
          jobCount, before, targets, done: [],
        });
        await load();
      } catch (e) {
        setError(String(e));
        setOp(null);
      } finally {
        setBusy(null);
      }
    },
    [drafts, load],
  );

  // Watch the polled drafts for evidence the running action has progressed. Runs on every
  // draft refresh, so the strip advances in step with the poll already in place.
  useEffect(() => {
    if (!op || op.timedOut) return;
    const done = observeOp(op, drafts);
    const changed = done.length !== op.done.length;
    const next = changed ? { ...op, done } : op;

    if (changed) {
      const before = new Set(op.before);
      const created = drafts.filter((d) => !before.has(d.id)).map((d) => d.id);
      if (created.length) setFresh(new Set(created));
    }
    if (opComplete(next)) {
      setOp(null);           // resolved — drop the strip, the results are on screen
      return;
    }
    if (Date.now() - op.startedAt > OP_WATCH_MS) {
      setOp({ ...next, timedOut: true });
      return;
    }
    if (changed) setOp(next);
  }, [drafts, op]);

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

  // Approved drafts with no rendered file — the ones that genuinely need exporting. Editing a
  // draft clears its pdf_ref on purpose (the old file no longer matches the content), so an
  // edited draft correctly reappears here. Everything else already has an identical file:
  // re-rendering it is free of tokens but still pointless work, so it needs an explicit ask.
  const needsExport = useMemo(() => approved.filter((d) => !d.pdf_ref), [approved]);
  const alreadyExported = approved.length - needsExport.length;

  // Approved drafts with no after_score — the ones re-qualify hasn't scored yet. Unlike
  // export this one really does cost money (gate-passers go to an LLM refine pass), so the
  // default must not silently re-bill drafts that already have a score. Editing a draft also
  // clears after_score, so an edited draft correctly needs re-scoring again.
  const needsScore = useMemo(() => approved.filter((d) => d.after_score == null), [approved]);
  const alreadyScored = approved.length - needsScore.length;

  // "Finished" = decided one way or the other, so it no longer needs your attention.
  const finished = useMemo(() => drafts.filter((d) => d.status !== "draft_ready"), [drafts]);
  // A hidden id is only honoured while that draft is STILL finished: re-tailoring upserts the
  // same row back to draft_ready, and a fresh draft must never stay invisible because an
  // older version of it was cleared.
  const visible = useMemo(
    () => drafts.filter((d) => !(hidden.has(d.id) && d.status !== "draft_ready")),
    [drafts, hidden],
  );
  const hiddenCount = drafts.length - visible.length;

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">Tailored CVs</h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-500">
            “Prepare CVs” runs analyze → tailor → export for the selected jobs and attaches an
            honest, job-specific CV to each job row (locked facts never change). The qualifier then
            scores those jobs against their attached CV. It stays strictly résumé-evidenced and
            ignores gap-close proposals. “Tailor only” stops at a reviewable draft and{" "}
            <span className="font-medium text-amber-600">does</span> fold in any gap-close item you
            accepted — edit each draft, then approve and export yourself.
          </p>
        </div>
        <button
          onClick={() =>
            startOp("prepare", selectedJobIds.length, [], () => prepareCvs(selectedJobIds))
          }
          disabled={!!busy || !!op || selectedJobIds.length === 0}
          className="ml-auto flex items-center gap-2 rounded-lg bg-sky-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-sky-600"
          title={
            selectedJobIds.length === 0
              ? "Select jobs in the table first"
              : "Analyze → rewrite + honesty gate → export, then the CV attaches to each job row"
          }
        >
          {op?.kind === "prepare" && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
          )}
          {op?.kind === "prepare"
            ? `Preparing ${op.jobCount} …`
            : `Prepare CVs for ${selectedJobIds.length} selected`}
        </button>
        <button
          onClick={() =>
            startOp("tailor", selectedJobIds.length, [], () => tailorCvs(selectedJobIds))
          }
          disabled={!!busy || !!op || selectedJobIds.length === 0}
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98] disabled:opacity-40"
          title={selectedJobIds.length === 0 ? "Select jobs in the table first" : "Tailor only (review, then approve/export yourself)"}
        >
          {op?.kind === "tailor" ? `Tailoring ${op.jobCount} …` : "Tailor only"}
        </button>
      </div>

      {op && (
        <StageStrip
          title={op.timedOut ? `${op.title} — still running in the background` : `${op.title}…`}
          stages={OP_STAGES[op.kind]}
          startedAt={op.timedOut ? null : op.startedAt}
          stateOf={(k) => {
            if (op.done.includes(k)) return "done";
            const keys = OP_STAGES[op.kind].map((s) => s.key);
            const firstOpen = keys.find((x) => !op.done.includes(x));
            return k === firstOpen ? "active" : "pending";
          }}
          note={
            op.timedOut
              ? "Not watching any more — the worker may still finish it. The list refreshes every 5s."
              : [
                  op.jobCount > 0
                    ? `${opLanded(op, drafts)} of ${op.jobCount} draft(s) in`
                    : `${opLanded(op, drafts)} of ${op.targets.length} done`,
                  "◌ = can't be observed from here, so it is never ticked",
                ].join(" · ")
          }
          onDismiss={() => setOp(null)}
        />
      )}

      {drafts.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-slate-50/70 p-2 text-xs">
          <button
            onClick={() => setPicked(new Set(allReadyPicked ? [] : draftReady.map((d) => d.id)))}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95"
          >
            {allReadyPicked ? "Clear" : "Select all"}
          </button>
          <button
            onClick={() => run("approve-sel", () => approveCvDrafts(pickedReady))}
            disabled={!!busy || pickedReady.length === 0}
            className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 active:scale-95 disabled:opacity-40"
          >
            Approve selected ({pickedReady.length})
          </button>
          <button
            onClick={() => run("approve-all", () => approveCvDrafts(undefined, true))}
            disabled={!!busy || draftReady.length === 0}
            className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 active:scale-95 disabled:opacity-40"
          >
            Approve all
          </button>
          <button
            onClick={() => run("reject-sel", () => rejectCvDrafts(pickedReady))}
            disabled={!!busy || pickedReady.length === 0}
            className="rounded-md border border-rose-200 bg-rose-50 px-2 py-1 font-medium text-rose-700 transition hover:border-rose-300 hover:bg-rose-100 active:scale-95 disabled:opacity-40"
          >
            Reject selected
          </button>
          <span className="mx-1 h-4 w-px bg-slate-300" />
          {/* Sends only the ids that lack a file, via the draft_ids parameter /cv/export
              already accepts — so the count means "will be rendered", not "is approved". */}
          <button
            onClick={() =>
              startOp("export", 0, needsExport.map((d) => d.id), () =>
                exportCvs(needsExport.map((d) => d.id)),
              )
            }
            disabled={!!busy || !!op || needsExport.length === 0}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
            title={
              needsExport.length === 0
                ? "Every approved draft already has a PDF/DOCX"
                : `Render ${needsExport.length} approved draft(s) that have no file yet`
            }
          >
            {op?.kind === "export"
              ? `Exporting ${op.targets.length}…`
              : `Export approved (${needsExport.length})`}
          </button>
          {alreadyExported > 0 && (
            <button
              onClick={() => startOp("export", 0, approved.map((d) => d.id), () => exportCvs())}
              disabled={!!busy || !!op}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-500 transition hover:border-slate-300 hover:text-slate-700 active:scale-95 disabled:opacity-40"
              title={`Re-render all ${approved.length} approved draft(s), including the ${alreadyExported} that already have files. No LLM cost — the renderer is deterministic.`}
            >
              Re-render all ({approved.length})
            </button>
          )}
          {/* Scoped by default: re-qualify bills an LLM refine pass per gate-passer, so the
              plain click must never re-score drafts that already carry an after_score. */}
          <button
            onClick={() =>
              startOp("requalify", 0, needsScore.map((d) => d.id), () =>
                requalifyCvs(needsScore.map((d) => d.id)),
              )
            }
            disabled={!!busy || !!op || needsScore.length === 0}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
            title={
              needsScore.length === 0
                ? "Every approved draft already has an after-score"
                : `Score ${needsScore.length} approved draft(s) that have no after-score yet`
            }
          >
            {op?.kind === "requalify"
              ? `Re-qualifying ${op.targets.length}…`
              : `Re-qualify approved (${needsScore.length})`}
          </button>
          {alreadyScored > 0 && (
            <button
              onClick={() =>
                startOp("requalify", 0, approved.map((d) => d.id), () => requalifyCvs())
              }
              disabled={!!busy || !!op}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-500 transition hover:border-amber-300 hover:bg-amber-50 hover:text-amber-700 active:scale-95 disabled:opacity-40"
              title={`Re-score all ${approved.length} approved draft(s), including the ${alreadyScored} already scored. COSTS TOKENS — use after changing your qualifier config (weights, threshold, must-haves), so every draft is judged by the new rules.`}
            >
              Re-score all ({approved.length}) · costs tokens
            </button>
          )}
          <button
            onClick={() => run("report", async () => setReport(await getCvReport()))}
            disabled={!!busy}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
          >
            {busy === "report" ? "Loading…" : "Improvement report"}
          </button>

          {/* Clearing the view. Never a delete — the drafts and their exported files are
              untouched, and a cleared draft reappears by itself if it goes back to
              draft_ready (an edit or a re-tailor), so nothing can hide unfinished work. */}
          <span className="mx-1 h-4 w-px bg-slate-300" />
          <button
            onClick={() => setHidden(new Set(finished.map((d) => d.id)))}
            disabled={finished.length === 0 || hiddenCount === finished.length}
            className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
            title="Hide approved and rejected drafts from this list — view only, nothing is deleted"
          >
            Clear finished from view ({finished.length})
          </button>
          {hiddenCount > 0 && (
            <button
              onClick={() => setHidden(new Set())}
              className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1 font-medium text-sky-700 transition hover:border-sky-300 hover:bg-sky-100 active:scale-95"
            >
              Show all ({hiddenCount} hidden)
            </button>
          )}
        </div>
      )}

      {error && (
        <p className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {report && (
        <div className="pop-in mt-3 rounded-lg border border-slate-200 bg-slate-50/70 p-3 text-sm">
          <p className="font-medium text-slate-900">
            <span className="text-emerald-700">{report.improved}</span> of {report.total} scored higher ·{" "}
            {report.unchanged} unchanged · <span className="text-rose-600">{report.worse}</span> lower
          </p>
          <ul className="mt-1.5 space-y-0.5 text-xs text-slate-600">
            {report.report.map((r) => (
              <li key={r.draft_id}>
                {r.job_title ?? "Job"}
                {r.company ? ` · ${r.company}` : ""}:{" "}
                <span className="font-mono tabular-nums">
                  {r.before_score != null ? Math.round(r.before_score * 100) : "—"} →{" "}
                  {r.after_score != null ? Math.round(r.after_score * 100) : "—"}
                </span>
                {r.delta != null && (
                  <span
                    className={`font-medium ${
                      r.delta > 0 ? "text-emerald-700" : r.delta < 0 ? "text-rose-600" : ""
                    }`}
                  >
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
          <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50/50 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">
              {op ? "Working…" : "No tailored drafts yet."}
            </p>
            <p className="mt-1 text-xs text-slate-400">
              {op
                ? "The first draft will appear here as soon as it lands."
                : "Select jobs on the Dashboard, then “Prepare CVs” or “Tailor only”."}
            </p>
          </div>
        )}
        {drafts.length > 0 && visible.length === 0 && (
          <p className="py-4 text-center text-sm text-slate-500">
            Everything here is finished and cleared from view — “Show all” to bring it back.
          </p>
        )}
        {visible.map((d) => (
          <DraftCard
            key={d.id}
            draft={d}
            job={jobById(d.job_id)}
            title={jobById(d.job_id)?.title ?? "Job"}
            picked={picked.has(d.id)}
            isNew={fresh.has(d.id)}
            open={openId === d.id}
            editing={editingId === d.id}
            onToggleOpen={() =>
              setOpenId((cur) => {
                const next = cur === d.id ? null : d.id;
                // Collapsing a card (or moving to another) must not leave a stray editor open.
                if (next !== d.id) setEditingId(null);
                return next;
              })
            }
            onToggleEdit={() => {
              setOpenId(d.id);                       // editing always shows the card
              setEditingId((cur) => (cur === d.id ? null : d.id));
            }}
            onPick={() => togglePick(d.id)}
            onApprove={() => run("approve1", () => approveCvDrafts([d.id]))}
            onReject={() => run("reject1", () => rejectCvDrafts([d.id]))}
            onChanged={load}
            busy={!!busy}
          />
        ))}
      </div>
    </section>
  );
}

/** The job posting, beside whatever you're doing to the CV.
 *
 *  Editing a CV toward a job you can't see means holding the posting in your head or in
 *  another tab. The description is already loaded on the dashboard (`/dashboard/jobs`
 *  returns it in full), so showing it here costs no request — it is purely a matter of
 *  putting it where the decision is made.
 *
 *  Rendered as TEXT, never as HTML: this string is scraped from third-party sites, so
 *  `dangerouslySetInnerHTML` would hand arbitrary markup from a job board a script tag in
 *  the user's session. `whitespace-pre-wrap` keeps the posting's own line breaks. */
function JobPanel({ job, className = "" }: { job: Job | null; className?: string }) {
  const body = job?.description?.trim();
  // Scout records whether it got the whole posting or only a listing snippet. If the text
  // is partial, say so and link out — otherwise a short description reads as a short job.
  const partial = job?.description_completeness && job.description_completeness !== "full";

  return (
    <div className={`rounded-lg border border-slate-200 bg-slate-50/70 p-2.5 ${className}`}>
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <p className="font-semibold text-slate-700">Job description</p>
        {partial && (
          <span
            className="rounded border border-amber-200 bg-amber-50 px-1 py-0.5 text-[10px] font-medium text-amber-700"
            title="Scout captured only part of this posting — open the original for the full text."
          >
            {job?.description_completeness}
          </span>
        )}
        {job?.url && (
          <a
            href={job.url}
            target="_blank"
            rel="noreferrer noopener"
            className="ml-auto text-[11px] font-medium text-sky-600 hover:text-sky-700 hover:underline"
          >
            open posting ↗
          </a>
        )}
      </div>
      {job?.company && (
        <p className="mb-1 text-slate-500">
          {job.company}
          {job.location ? ` · ${job.location}` : ""}
        </p>
      )}
      {body ? (
        <div className="max-h-[28rem] overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-white p-2 leading-relaxed text-slate-700">
          {body}
        </div>
      ) : (
        <p className="py-4 text-center text-slate-400">
          No description stored for this job{job?.url ? " — open the posting above." : "."}
        </p>
      )}
    </div>
  );
}

/** The CV as it currently stands, read-only — what "compare against the posting" needs.
 *  The review panel used to show only the profile line and the change notes, which is
 *  enough to audit an edit but not enough to judge a fit. */
function CvPreview({ content, className = "" }: { content: TailoredContent; className?: string }) {
  const roles = content?.experience ?? [];
  return (
    <div className={`rounded-lg border border-slate-200 bg-white p-2.5 shadow-card ${className}`}>
      <p className="mb-1.5 font-semibold text-slate-700">Your tailored CV</p>
      <div className="max-h-[28rem] space-y-2.5 overflow-y-auto">
        {content?.summary && <p className="leading-relaxed text-slate-700">{content.summary}</p>}
        {(content?.skills ?? []).length > 0 && (
          <p className="text-slate-600">
            <span className="font-medium text-slate-400">Skills: </span>
            {(content?.skills ?? []).join(" · ")}
          </p>
        )}
        {roles.map((role, i) => (
          <div key={i}>
            <p className="font-medium text-slate-800">
              {role.title} @ {role.company}
              <span className="font-normal text-slate-400">
                {role.location ? ` · ${role.location}` : ""} ({role.dates})
              </span>
            </p>
            <ul className="mt-0.5 space-y-0.5 pl-3">
              {(role.bullets ?? []).map((b, bi) => (
                <li key={bi} className="text-slate-600">
                  • {b}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Direct edits to one draft before approving it (Phase 6.6).
 *
 *  Add/remove is deterministic and costs nothing; only "reword" spends a model call, and
 *  only on the one section named. Every edit sends the draft back to `draft_ready` and
 *  detaches its rendered PDF/DOCX server-side, so an edit can never end up hidden inside an
 *  already-exported file — which is why the panel is offered on approved drafts too. */
function DraftEditor({
  draft,
  onChanged,
}: {
  draft: CvDraft;
  onChanged: () => Promise<void> | void;
}) {
  const content = draft.tailored_content_json;
  const roles = content?.experience ?? [];
  const [skill, setSkill] = useState("");
  const [bullet, setBullet] = useState<Record<number, string>>({});
  const [target, setTarget] = useState<string>("summary");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const act = async (label: string, fn: () => Promise<{ status: string; changes: string[]; warnings: string[] }>) => {
    setBusy(label);
    setNote(null);
    try {
      const res = await fn();
      const parts = [...(res.changes ?? []), ...(res.warnings ?? [])];
      setNote(parts.length ? parts.join(" · ") : res.status);
      await onChanged();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-lg border border-sky-200 bg-sky-50/50 p-2.5">
      <p className="mb-2.5 leading-relaxed text-slate-600">
        Edit before approving — employer, title, location, dates and education are locked and
        can’t be changed here. A skill your résumé doesn’t evidence is recorded as{" "}
        <span className="font-medium text-amber-700">user-confirmed</span> (your word, not a citation).
      </p>

      {/* --- skills --- */}
      <p className="mb-1 font-semibold uppercase tracking-wide text-slate-400">Skills</p>
      <div className="mb-2 flex flex-wrap gap-1">
        {(content?.skills ?? []).map((s) => (
          <span
            key={s}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-slate-700"
          >
            {s}
            <button
              disabled={!!busy}
              title={`Remove ${s}`}
              onClick={() => act("rm-skill", () => editCvDraft(draft.id, { remove_skills: [s] }))}
              className="text-slate-400 transition hover:text-rose-600 disabled:opacity-40"
            >
              ✕
            </button>
          </span>
        ))}
      </div>
      <div className="mb-3 flex gap-1.5">
        <input
          value={skill}
          onChange={(e) => setSkill(e.target.value)}
          placeholder="add a skill…"
          className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
        />
        <button
          disabled={!!busy || !skill.trim()}
          onClick={() =>
            act("add-skill", async () => {
              const r = await editCvDraft(draft.id, { add_skills: [skill.trim()] });
              setSkill("");
              return r;
            })
          }
          className="rounded-md border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
        >
          Add
        </button>
      </div>

      {/* --- bullets, per role (locked header shown for context only) --- */}
      {roles.map((role, i) => (
        <div key={i} className="mb-3">
          <p className="mb-1 font-medium text-slate-600">
            {role.title} @ {role.company}{" "}
            <span className="font-normal text-slate-400">
              ({[role.location, role.dates].filter(Boolean).join(" · ")} — locked)
            </span>
          </p>
          <ul className="mb-1 space-y-1">
            {(role.bullets ?? []).map((b, bi) => (
              <li key={bi} className="flex items-start gap-1.5">
                <span className="min-w-0 flex-1 text-slate-700">• {b}</span>
                <button
                  disabled={!!busy}
                  title="Remove this bullet"
                  onClick={() => act("rm-bullet", () => editCvDraft(draft.id, { remove_bullets: [b] }))}
                  className="shrink-0 text-slate-400 transition hover:text-rose-600 disabled:opacity-40"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
          <div className="flex gap-1.5">
            <input
              value={bullet[i] ?? ""}
              onChange={(e) => setBullet({ ...bullet, [i]: e.target.value })}
              placeholder={`add a bullet to ${role.company ?? "this role"}…`}
              className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
            />
            <button
              disabled={!!busy || !(bullet[i] ?? "").trim()}
              onClick={() =>
                act("add-bullet", async () => {
                  const r = await editCvDraft(draft.id, {
                    add_bullets: [{ text: (bullet[i] ?? "").trim(), role_index: i }],
                  });
                  setBullet({ ...bullet, [i]: "" });
                  return r;
                })
              }
              className="rounded-md border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
            >
              Add
            </button>
          </div>
        </div>
      ))}

      {/* --- reword: the one path that spends a model call --- */}
      <p className="mb-1 font-semibold uppercase tracking-wide text-slate-400">
        Reword to an instruction (one model call)
      </p>
      <div className="flex flex-wrap gap-1.5">
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          className="cursor-pointer rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-700 outline-none transition hover:border-slate-300 focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
        >
          <option value="summary">Profile / summary</option>
          {roles.map((role, i) => (
            <option key={i} value={String(i)}>
              Bullets — {role.company ?? `role ${i + 1}`}
            </option>
          ))}
        </select>
        <input
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="e.g. emphasise Kubernetes and platform ownership"
          className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
        />
        <button
          disabled={!!busy || !instruction.trim()}
          onClick={() =>
            act("reword", async () => {
              const r = await rewordCvDraft(draft.id, {
                target: target === "summary" ? "summary" : "role",
                instruction: instruction.trim(),
                ...(target === "summary" ? {} : { role_index: Number(target) }),
              });
              setInstruction("");
              return r;
            })
          }
          className="rounded-md bg-sky-600 px-2.5 py-1 font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-95 disabled:opacity-40 disabled:hover:bg-sky-600"
        >
          {busy === "reword" ? "Rewording…" : "Reword"}
        </button>
      </div>

      {note && <p className="mt-2 text-slate-600">{note}</p>}
    </div>
  );
}

function DraftCard({
  draft,
  job,
  title,
  picked,
  isNew = false,
  open,
  editing,
  onToggleOpen,
  onToggleEdit,
  onPick,
  onApprove,
  onReject,
  onChanged,
  busy,
}: {
  draft: CvDraft;
  /** The posting this CV targets, shown beside the editor and the review. Null if the job
   *  has since been deleted from the pool — the panels then say so rather than breaking. */
  job: Job | null;
  title: string;
  picked: boolean;
  isNew?: boolean;
  /** Accordion state, owned by the list so only one card is ever expanded. */
  open: boolean;
  editing: boolean;
  onToggleOpen: () => void;
  onToggleEdit: () => void;
  onPick: () => void;
  onApprove: () => void;
  onReject: () => void;
  onChanged: () => Promise<void> | void;
  busy: boolean;
}) {
  const notes = draft.critic_notes_json;
  const content = draft.tailored_content_json;
  const attested = notes?.user_attested;
  const attestedCount =
    (attested?.skills?.length ?? 0) +
    (attested?.new_bullets?.length ?? 0) +
    (attested?.bullet_rewrites?.length ?? 0);
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
    <div
      className={`rounded-xl border bg-white p-3 transition-all ${
        isNew
          ? "pop-in border-sky-300 ring-2 ring-sky-100"
          : open
            ? "border-sky-200 shadow-card-hover"
            : "border-slate-200 shadow-card hover:border-slate-300"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {draft.status === "draft_ready" && (
          <input
            type="checkbox"
            checked={picked}
            onChange={onPick}
            className="h-4 w-4 cursor-pointer rounded border-slate-300 accent-sky-600"
          />
        )}
        <span className="text-sm font-semibold text-slate-900">{title}</span>
        {isNew && (
          <span className="rounded-full border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
            new
          </span>
        )}
        <span
          className={`rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLE[draft.status]}`}
        >
          {draft.status.replace("_", " ")}
        </span>
        {alignScore != null && (
          <span
            className={`rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${
              aligned
                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                : "border-amber-200 bg-amber-50 text-amber-700"
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
        {alignScore == null && notes?.single_pass && (
          <span
            className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-medium text-slate-500"
            title={
              "Written in a single pass, with no Critic grading it. Locked facts and skill " +
              "evidence were still enforced in code — but nothing scored how well this CV " +
              "matches the job, so that judgement is yours."
            }
          >
            not scored
          </span>
        )}
        {roundsRun != null && (
          <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
            {roundsRun}
            {maxRounds ? `/${maxRounds}` : ""} round{roundsRun === 1 ? "" : "s"}
          </span>
        )}
        {flags.length > 0 ? (
          <span
            className="rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-700"
            title={flags.join(" · ")}
          >
            ⚠ {flags.length} integrity flag{flags.length === 1 ? "" : "s"}
          </span>
        ) : (
          lockedSafe && (
            <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
              🔒 locked facts verified
            </span>
          )
        )}
        {draft.before_score != null && (
          <span className="text-xs tabular-nums text-slate-500">
            score {Math.round(draft.before_score * 100)}
            {draft.after_score != null && ` → ${Math.round(draft.after_score * 100)}`}
          </span>
        )}
        {attestedCount > 0 && (
          <span
            className="rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
            title="Includes content you vouched for yourself, not backed by résumé citations"
          >
            {attestedCount} self-attested
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={onToggleOpen}
            className="rounded-md px-2 py-0.5 text-xs font-medium text-slate-500 transition hover:bg-slate-100 hover:text-slate-900"
          >
            {open ? "Hide" : "Review"}
          </button>
          <button
            onClick={onToggleEdit}
            className="rounded-md px-2 py-0.5 text-xs font-medium text-sky-600 transition hover:bg-sky-50 hover:text-sky-700"
          >
            {editing ? "Done editing" : "Edit"}
          </button>
          {draft.status === "draft_ready" && (
            <>
              <button
                onClick={onApprove}
                disabled={busy}
                className="rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white shadow-sm transition hover:bg-emerald-700 active:scale-95 disabled:opacity-40"
              >
                Approve
              </button>
              <button
                onClick={onReject}
                disabled={busy}
                className="rounded-md border border-rose-200 bg-rose-50 px-2.5 py-1 text-xs font-medium text-rose-700 transition hover:border-rose-300 hover:bg-rose-100 active:scale-95 disabled:opacity-40"
              >
                Reject
              </button>
            </>
          )}
          {draft.pdf_ref && (
            <button
              onClick={() => downloadCv(draft.id, "pdf")}
              className="rounded-md border border-slate-200 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95"
            >
              PDF
            </button>
          )}
          {draft.docx_ref && (
            <button
              onClick={() => downloadCv(draft.id, "docx")}
              className="rounded-md border border-slate-200 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95"
            >
              DOCX
            </button>
          )}
        </div>
      </div>

      {open && (
        <div className="screen-enter mt-2.5 space-y-3 border-t border-slate-100 pt-2.5 text-xs">
          {/* Editing: the controls on the left, the posting you're editing toward on the
              right. The posting sticks while the editor scrolls — the editor is the taller
              column, and a requirement you have to scroll away from is one you'll forget. */}
          {editing && (
            <div className="grid gap-3 lg:grid-cols-2">
              <DraftEditor draft={draft} onChanged={onChanged} />
              <JobPanel job={job} className="lg:sticky lg:top-2 lg:self-start" />
            </div>
          )}

          {/* Not editing: the posting beside the CV as it now stands, so the review step is
              an actual comparison rather than a recollection. */}
          {!editing && (
            <div className="grid gap-3 lg:grid-cols-2">
              <JobPanel job={job} />
              <CvPreview content={content} />
            </div>
          )}

          {/* What the candidate vouched for themselves — kept visually distinct from the
              evidence-backed block below, because the warrant is different in kind. */}
          {attestedCount > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5">
              <p className="mb-1 font-semibold text-amber-900">
                You vouched for these ({attestedCount}) — no résumé citation backs them
              </p>
              {(attested?.skills?.length ?? 0) > 0 && (
                <p className="text-amber-800">Skills: {attested?.skills?.join(", ")}</p>
              )}
              {(attested?.new_bullets ?? []).map((b, i) => (
                <p key={`nb${i}`} className="mt-0.5 text-amber-800">
                  + {b.role_ref ? `[${b.role_ref}] ` : ""}
                  {b.text}
                </p>
              ))}
              {(attested?.bullet_rewrites ?? []).map((b, i) => (
                <p key={`rw${i}`} className="mt-0.5 text-amber-800">
                  ↻ {b.role_ref ? `[${b.role_ref}] ` : ""}
                  {b.after}
                </p>
              ))}
              <p className="mt-1 text-amber-700/80">
                {attested?.note ?? "Interview prep on these is on you."}
              </p>
            </div>
          )}

          {/* Alignment summary — how well, over how many rounds */}
          {alignScore != null && (
            <p className="text-slate-600">
              {aligned ? (
                <span className="font-medium text-emerald-700">Aligned</span>
              ) : (
                <span className="font-medium text-amber-700">Did not reach the bar</span>
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
            <div className="rounded-lg border border-rose-200 bg-rose-50 p-2.5">
              <p className="mb-0.5 font-semibold text-rose-800">⚠ Integrity flags</p>
              <ul className="list-disc space-y-0.5 pl-4 text-rose-700">
                {flags.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Per-dimension alignment scores */}
          {Object.keys(dims).length > 0 && (
            <div>
              <p className="mb-1 font-semibold uppercase tracking-wide text-slate-400">
                Alignment by dimension
              </p>
              <div className="space-y-1">
                {Object.entries(dims).map(([k, v]) => (
                  <div key={k} className="flex items-center gap-2">
                    <span className="w-36 truncate capitalize text-slate-600">{k.replace(/_/g, " ")}</span>
                    <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200">
                      <span
                        className="block h-full rounded-full bg-sky-500 transition-all duration-500"
                        style={{ width: `${Math.round((Number(v) || 0) * 100)}%` }}
                      />
                    </span>
                    <span className="w-8 text-right font-mono tabular-nums text-slate-500">
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
              <p className="mb-1 font-semibold uppercase tracking-wide text-slate-400">
                Skills added for this job{" "}
                <span className="font-normal normal-case text-slate-400">(each grounded in your real CV)</span>
              </p>
              <ul className="space-y-1">
                {evidence.map((e, i) => (
                  <li key={i} className="rounded-md border border-slate-200 bg-white px-2 py-1">
                    <span className="font-medium text-slate-900">{e.skill}</span>
                    {e.basis && (
                      <span className="ml-2 rounded border border-slate-200 bg-slate-50 px-1 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">
                        {e.basis}
                      </span>
                    )}
                    {e.evidence && <span className="mt-0.5 block text-slate-500">from: “{e.evidence}”</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Genuine gaps left honest — the loop won't claim these */}
          {unaddressed.length > 0 && (
            <p className="text-slate-500">
              <span className="font-medium text-slate-700">Still unaddressed (kept honest):</span>{" "}
              {unaddressed.join(", ")}
            </p>
          )}

          {/* Traceable change notes. The profile line that used to sit here is now in the
              CvPreview panel above, with the rest of the CV it belongs to. */}
          {changes.length > 0 && (
            <div>
              <p className="mb-1 font-semibold uppercase tracking-wide text-slate-400">
                What changed and why (grounded in your real experience)
              </p>
              <ul className="space-y-0.5">
                {changes.map((c, i) => (
                  <li key={i} className="text-slate-600">
                    <span className="font-medium text-slate-900">{c.field}</span>
                    {c.why ? ` — ${c.why}` : ""}
                    {c.grounded_in ? ` (from: ${c.grounded_in})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Legacy critic line — only for pre-alignment-loop drafts (no alignment score) */}
          {alignScore == null && notes?.rounds && notes.rounds.length > 0 && (
            <p className="text-slate-500">
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
