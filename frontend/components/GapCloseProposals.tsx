"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  GapClose,
  GapCloseKind,
  GapClosePatch,
  GapCloseStatus,
  Suggestion,
} from "@/lib/types";
import { GAP_CLOSE_KINDS } from "@/lib/types";
import {
  deleteGapClose,
  gapCloseCvs,
  gapCloseRefineCvs,
  getGapCloseStatus,
  getSuggestions,
  reviewGapClose,
} from "@/lib/api";
import StageStrip from "./StageStrip";

const GAP_STAGES = [
  { key: "analyze", label: "analyze the jobs" },
  { key: "propose", label: "close gaps (skills, rewrites, new bullets)" },
];

const GAP_STAGES_REFINED = [
  { key: "analyze", label: "analyze the jobs" },
  { key: "propose", label: "propose → review → re-propose" },
];

/** A run is finished only when the backend says so — not when the UI got bored waiting. */
const RUNNING_PHASES: GapCloseStatus["phase"][] = ["starting", "analyzing", "proposing", "refining"];
const isRunning = (s: GapCloseStatus | null): boolean =>
  s !== null && RUNNING_PHASES.includes(s.phase);

/** "12m 30s" — the analyze batch can run long enough that seconds alone stop meaning much. */
function humanSeconds(total: number): string {
  const s = Math.max(0, Math.round(total));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/* Phase 6.6 — gap-close proposals.
 *
 * The Gap-closer proposes without a truth constraint: skills the résumé never mentions,
 * aggressive rewrites, brand-new bullets. Nothing here reaches a CV on its own. Accepting
 * an item is the candidate asserting they can back it in an interview, and THAT is what
 * admits it past the evidence gate (as `user_confirmed`) when "Tailor only" runs.
 *
 * So this UI has one job above all others: make the commitment legible before the click.
 * Every item shows its interview talking point, items the résumé doesn't support are marked
 * as needing the candidate's word, and the wording is editable — accepting someone else's
 * sentence about your own career is exactly the thing to make easy to fix.
 */

const KIND_LABEL: Record<GapCloseKind, string> = {
  claimed_skills: "Skills to claim",
  bullet_rewrites: "Bullet rewrites",
  project_rewrites: "Project bullet rewrites",
  new_bullets: "New bullets",
};

/** The item's editable body text, per kind — what the textarea edits and what we send back. */
function bodyOf(kind: GapCloseKind, item: Record<string, unknown>): string {
  if (kind === "claimed_skills") return String(item.skill ?? "");
  if (kind === "bullet_rewrites" || kind === "project_rewrites") return String(item.after ?? "");
  return String(item.text ?? "");
}
function bodyField(kind: GapCloseKind): "skill" | "after" | "text" {
  if (kind === "claimed_skills") return "skill";
  return kind === "bullet_rewrites" || kind === "project_rewrites" ? "after" : "text";
}

function countAccepted(gc: GapClose | null): number {
  if (!gc) return 0;
  return GAP_CLOSE_KINDS.reduce(
    (n, k) => n + (gc[k] ?? []).filter((i) => i.accepted === true).length,
    0,
  );
}
function countTotal(gc: GapClose | null): number {
  if (!gc) return 0;
  return GAP_CLOSE_KINDS.reduce(
    (n, k) => n + (gc[k] ?? []).length,
    0,
  );
}
/** Items still awaiting a decision — what makes a card "needs your attention". */
function countUndecided(gc: GapClose | null): number {
  if (!gc) return 0;
  return GAP_CLOSE_KINDS.reduce(
    (n, k) => n + (gc[k] ?? []).filter((i) => i.accepted === null || i.accepted === undefined).length,
    0,
  );
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function ItemRow({
  kind,
  item,
  decided,
  onPatch,
  busy,
}: {
  kind: GapCloseKind;
  item: Record<string, unknown> & { id: string; accepted: boolean | null };
  /** Optimistic override: what the user just clicked, before the server confirms it. */
  decided: boolean | null | undefined;
  onPatch: (patch: GapClosePatch) => void;
  busy: boolean;
}) {
  const body = bodyOf(kind, item);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(body);
  useEffect(() => setText(body), [body]);

  // The click wins the render until the refresh lands, so a long review list feels instant.
  const effective = decided === undefined ? item.accepted : decided;
  const accepted = effective === true;
  const rejected = effective === false;
  // in_resume is only meaningful for skills; false means nothing in the résumé backs it.
  const needsWord = kind === "claimed_skills" ? item.in_resume === false : true;
  // A project rewrite is keyed by project name; a role rewrite by employer. Same badge.
  const roleRef =
    typeof item.role_ref === "string"
      ? item.role_ref
      : typeof item.project_ref === "string"
        ? item.project_ref
        : null;

  return (
    <li
      className={`rounded-lg border px-3 py-2 transition-all ${
        accepted
          ? "border-emerald-300 bg-emerald-50/60"
          : rejected
            ? "border-slate-200 bg-slate-50/60 opacity-55"
            : "border-slate-200 bg-white hover:border-slate-300"
      }`}
    >
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          {roleRef && (
            <span className="mr-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
              {roleRef}
            </span>
          )}
          {editing ? (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={kind === "claimed_skills" ? 1 : 3}
              className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1 text-sm text-slate-900 outline-none transition focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
            />
          ) : (
            <span className="text-sm text-slate-800">
              {body || <em className="text-slate-400">(empty)</em>}
            </span>
          )}
          {(kind === "bullet_rewrites" || kind === "project_rewrites") && typeof item.before === "string" && item.before && !editing && (
            <p className="mt-0.5 text-xs text-slate-400 line-through decoration-slate-300">
              {item.before}
            </p>
          )}
          {typeof item.closes === "string" && item.closes && (
            <p className="mt-1 text-xs text-slate-500">closes: {item.closes}</p>
          )}
          {typeof item.interview_prep === "string" && item.interview_prep && (
            <p className="mt-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-900">
              <span className="font-semibold">You must be able to say:</span> {item.interview_prep}
            </p>
          )}
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {needsWord && (
              <span className="rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                your word is the only evidence
              </span>
            )}
            {item.user_edited === true && (
              <span className="rounded-full border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
                edited by you
              </span>
            )}
            {accepted && (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                you vouched for this
              </span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 flex-col gap-1">
          {editing ? (
            <>
              <button
                disabled={busy}
                onClick={() => {
                  onPatch({ kind, id: item.id, [bodyField(kind)]: text } as GapClosePatch);
                  setEditing(false);
                }}
                className="rounded-md bg-sky-600 px-2.5 py-1 text-xs font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-95 disabled:opacity-40"
              >
                Save
              </button>
              <button
                onClick={() => {
                  setText(body);
                  setEditing(false);
                }}
                className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50"
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              {/* Not disabled while in flight: the optimistic state already reflects the
                  click, so blocking the button would only make it feel slower. */}
              <button
                onClick={() => onPatch({ kind, id: item.id, accepted: true })}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition active:scale-95 ${
                  accepted
                    ? "bg-emerald-600 text-white shadow-sm"
                    : "border border-emerald-200 bg-emerald-50 text-emerald-700 hover:border-emerald-300 hover:bg-emerald-100"
                }`}
                title="I can back this in an interview"
              >
                {accepted ? "✓ Accepted" : "Accept"}
              </button>
              <button
                onClick={() => onPatch({ kind, id: item.id, accepted: false })}
                className={`rounded-md border px-2.5 py-1 text-xs font-medium transition active:scale-95 ${
                  rejected
                    ? "border-slate-300 bg-slate-100 text-slate-700"
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50"
                }`}
              >
                {rejected ? "Rejected" : "Reject"}
              </button>
              <button
                disabled={busy}
                onClick={() => setEditing(true)}
                className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-40"
              >
                Edit
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  );
}

function JobProposals({
  suggestion,
  title,
  open,
  onToggleOpen,
  onReviewed,
  selected,
  onToggleSelected,
}: {
  suggestion: Suggestion;
  title: string;
  /** Accordion state, owned by the list so only one job's proposals are ever expanded. */
  open: boolean;
  onToggleOpen: () => void;
  onReviewed: () => Promise<void> | void;
  /** Ticked. Selection is owned by the list, so it survives a card collapsing, and it
   *  drives both actions: tailor these jobs, or delete these proposals. */
  selected: boolean;
  onToggleSelected: () => void;
}) {
  const gc = suggestion.gap_close_json;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Clicks not yet confirmed by the server, keyed "kind:id". Applied over the stored state so
  // the row responds on click; dropped once the refresh lands, reverted if the write fails.
  const [pending, setPending] = useState<Record<string, boolean | null>>({});

  const submit = async (patches: GapClosePatch[]) => {
    if (patches.length === 0) return;
    const decisions = patches.filter((p) => "accepted" in p);
    const optimistic: Record<string, boolean | null> = {};
    for (const p of decisions) optimistic[`${p.kind}:${p.id}`] = p.accepted ?? null;

    setPending((prev) => ({ ...prev, ...optimistic }));
    setBusy(true);
    setError(null);
    try {
      await reviewGapClose(suggestion.id, patches);
      await onReviewed();          // parent state now matches; the overlay can go
      setPending((prev) => {
        const next = { ...prev };
        for (const k of Object.keys(optimistic)) delete next[k];
        return next;
      });
    } catch (e) {
      setError(String(e));
      setPending((prev) => {       // revert — never leave a click looking saved when it wasn't
        const next = { ...prev };
        for (const k of Object.keys(optimistic)) delete next[k];
        return next;
      });
    } finally {
      setBusy(false);
    }
  };

  const kinds = GAP_CLOSE_KINDS.filter(
    (k) => (gc?.[k] ?? []).length > 0,
  );
  const total = countTotal(gc);
  // Counts follow the optimistic view too, so the header moves with the click.
  const accepted = GAP_CLOSE_KINDS.reduce(
    (n, k) =>
      n +
      (gc?.[k] ?? []).filter((i) => {
        const p = pending[`${k}:${i.id}`];
        return (p === undefined ? i.accepted : p) === true;
      }).length,
    0,
  );
  const undecided = GAP_CLOSE_KINDS.reduce(
    (n, k) =>
      n +
      (gc?.[k] ?? []).filter((i) => {
        const p = pending[`${k}:${i.id}`];
        const eff = p === undefined ? i.accepted : p;
        return eff === null || eff === undefined;
      }).length,
    0,
  );
  if (total === 0) return null;

  const allPatches = (value: boolean): GapClosePatch[] =>
    kinds.flatMap((k) => (gc?.[k] ?? []).map((i) => ({ kind: k, id: i.id, accepted: value })));

  return (
    <div
      className={`rounded-xl border bg-white p-3 transition-all ${
        open ? "border-amber-300 shadow-card-hover" : "border-slate-200 shadow-card hover:border-slate-300"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          aria-label={`Select ${title} for deletion`}
          title="Select this job's proposals"
          className="h-3.5 w-3.5 shrink-0 cursor-pointer rounded border-slate-300 text-rose-600 focus:ring-rose-400"
        />
        <button
          onClick={onToggleOpen}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-900"
        >
          <svg
            className={`h-3.5 w-3.5 shrink-0 transition-transform duration-200 ${
              open ? "rotate-90 text-amber-600" : "text-slate-400"
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
          <span className="truncate">{title}</span>
        </button>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium transition ${
            accepted > 0
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-slate-200 bg-slate-50 text-slate-500"
          }`}
        >
          {accepted} of {total} accepted
        </span>
        {undecided > 0 ? (
          <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
            {undecided} awaiting you
          </span>
        ) : (
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium text-slate-500">
            reviewed
          </span>
        )}
        <div className="ml-auto flex gap-1.5">
          <button
            disabled={busy}
            onClick={() => submit(allPatches(true))}
            className="rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 active:scale-95 disabled:opacity-40"
            title="I can back every item here"
          >
            Accept all
          </button>
          <button
            disabled={busy}
            onClick={() => submit(allPatches(false))}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 active:scale-95 disabled:opacity-40"
          >
            Reject all
          </button>
        </div>
      </div>

      {error && <p className="mt-2 text-xs text-rose-600">{error}</p>}

      {open && (
        <div className="screen-enter mt-3 space-y-3">
          {kinds.map((kind) => (
            <div key={kind}>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
                {KIND_LABEL[kind]}
              </p>
              <ul className="space-y-1.5">
                {(gc?.[kind] ?? []).map((item) => (
                  <ItemRow
                    key={item.id}
                    kind={kind}
                    item={item as never}
                    decided={pending[`${kind}:${item.id}`]}
                    busy={busy}
                    onPatch={(p) => submit([p])}
                  />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** The detail line under the stage strip: which phase, how far in, and what it is waiting on.
 *  The analyze phase reports the batch it is blocked on, because that is where the minutes go. */
function ProgressDetail({ status }: { status: GapCloseStatus | null }) {
  if (!status) return null;
  const failed = Object.keys(status.errors ?? {}).length;
  const pct = status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;
  return (
    <div className="mt-2 rounded-lg border border-amber-200 bg-white/70 px-3 py-2 text-xs text-slate-600">
      {status.phase === "analyzing" ? (
        <p>
          Analyzing <span className="font-medium text-slate-900">{status.analyzing}</span> job(s)
          with no analysis yet
          {status.batch_id ? (
            <>
              {" "}
              — sent as one model batch, waiting for it to come back
              {typeof status.waited_seconds === "number"
                ? ` (${humanSeconds(status.waited_seconds)} so far)`
                : ""}
              .
            </>
          ) : (
            "."
          )}
        </p>
      ) : status.phase === "proposing" ? (
        <p>
          Proposing — <span className="font-medium text-slate-900">{status.done}</span> of{" "}
          {status.total} job(s) done.
        </p>
      ) : status.phase === "starting" ? (
        <p>Starting…</p>
      ) : status.phase === "failed" ? (
        <p className="text-rose-700">Failed: {status.error ?? "unknown error"}</p>
      ) : (
        <p>
          Finished — <span className="font-medium text-slate-900">{status.done}</span> of{" "}
          {status.total} job(s) proposed{failed > 0 ? `, ${failed} unfinished` : ""}.
        </p>
      )}
      {status.total > 0 && (
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-amber-100">
          <div
            className="h-full rounded-full bg-amber-500 transition-all duration-500"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {status.dropped > 0 && (
        <p className="mt-1.5 text-amber-700">
          {status.dropped} of the {status.requested} job(s) you selected were left out — one run
          is capped at {status.total}.
        </p>
      )}
    </div>
  );
}

export default function GapCloseProposals({
  selectedJobIds,
  jobTitleById,
  onSelectJobs,
}: {
  selectedJobIds: string[];
  jobTitleById: (id: string) => string;
  /** Select these jobs and open Tailored CVs. Accepted proposals are stored against the
   *  job, not against the tick in the table, so this is what hands them to "Tailor only"
   *  without hunting down the same rows again. */
  onSelectJobs: (ids: string[]) => void;
}) {
  const [rows, setRows] = useState<Suggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>("");
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [proposed, setProposed] = useState(false);   // proposals observed landing this run
  const [refining, setRefining] = useState(false);   // this run is the reviewed variant
  // View-only: suggestion ids cleared from the list. Nothing is deleted, and a job reappears
  // by itself if it stops being fully reviewed (e.g. its proposals were regenerated).
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  // Accordion: one job's proposals expanded at a time. These cards used to all render open,
  // so the first one is opened for you when a set arrives — otherwise the panel would look
  // empty after a run. Collapsing it does NOT spring back open.
  const [openJobId, setOpenJobId] = useState<string | null>(null);
  const seenAny = useRef(false);
  // Live server-side progress. This is what makes the analyze step visible: it produces no
  // proposals at all until it finishes, so without it the panel looked idle for the whole wait.
  const [status, setStatus] = useState<GapCloseStatus | null>(null);
  // Proposal sets ticked, by suggestion id. Drives both actions below: send these jobs to
  // be tailored, or delete their proposals. Ticking is not itself destructive.
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  // One watcher at a time, and it must stop when the panel unmounts.
  const watching = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      setRows(await getSuggestions());
    } catch {
      /* ignore — the panel just stays as-is */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const withProposals = useMemo(
    () => rows.filter((s) => s.job_id && countTotal(s.gap_close_json) > 0),
    [rows],
  );
  const totalAccepted = useMemo(
    () => withProposals.reduce((n, s) => n + countAccepted(s.gap_close_json), 0),
    [withProposals],
  );
  // The jobs with something to actually fold into a draft — i.e. exactly what "Tailor only"
  // is for. Offered as a one-click selection because it is otherwise only reconstructable by
  // remembering which rows were ticked, and the table's ticks don't survive a sign-out.
  const acceptedJobIds = useMemo(
    () =>
      withProposals
        .filter((s) => countAccepted(s.gap_close_json) > 0)
        .map((s) => s.job_id as string),
    [withProposals],
  );
  // Fully reviewed = every item decided, so the card has nothing left to ask of you.
  const reviewed = useMemo(
    () => withProposals.filter((s) => countUndecided(s.gap_close_json) === 0),
    [withProposals],
  );
  const visible = useMemo(
    () =>
      withProposals.filter(
        (s) => !(hidden.has(s.id) && countUndecided(s.gap_close_json) === 0),
      ),
    [withProposals, hidden],
  );
  const hiddenCount = withProposals.length - visible.length;

  // Open the first card the very first time proposals show up (e.g. on page load with a set
  // already stored). Guarded by a ref so a deliberate collapse is never undone.
  useEffect(() => {
    if (!seenAny.current && visible.length > 0) {
      seenAny.current = true;
      setOpenJobId(visible[0].id);
    }
  }, [visible]);

  /* Watch a run to completion — however long that takes.
   *
   * This used to give up after 80 × 3s and tell you to go check the worker, which was the
   * worst possible answer: above `analyze_batch_threshold` jobs the run goes through the
   * Anthropic Batch API and legitimately takes far longer than four minutes, so the panel
   * abandoned a perfectly healthy run and showed nothing. The backend now publishes a durable
   * progress record, so the exit condition is the *run's* phase, not our patience.
   *
   * Proposals are re-read on every tick too, so each job's card appears as it lands rather
   * than all at once at the end. */
  const watchRun = useCallback(async () => {
    if (watching.current) return;
    watching.current = true;
    setBusy(true);
    try {
      for (;;) {
        const st = await getGapCloseStatus().catch(() => null);
        if (!mounted.current) return;
        if (st) setStatus(st);

        const fresh = await getSuggestions().catch(() => null);
        if (!mounted.current) return;
        if (fresh) {
          setRows(fresh);
          if (!seenAny.current) {
            const first = fresh.find((s) => s.job_id && countTotal(s.gap_close_json) > 0);
            if (first) {
              seenAny.current = true;
              setOpenJobId(first.id);
            }
          }
        }

        if (!isRunning(st)) {
          if (st?.phase === "done") {
            setProposed(true);
            const failed = Object.keys(st.errors ?? {}).length;
            setMsg(
              failed === 0
                ? `Done — proposals ready for ${st.done} of ${st.total} job(s). Review each item below.`
                : `Done — ${st.done} of ${st.total} job(s) proposed, ${failed} could not be completed.`,
            );
          } else if (st?.phase === "failed") {
            setMsg(`Run failed: ${st.error ?? "unknown error"}`);
          }
          return;
        }
        await sleep(3000);
      }
    } finally {
      watching.current = false;
      if (mounted.current) {
        setBusy(false);
        setStartedAt(null);
      }
    }
  }, []);

  /* A run outlives the page that started it, so on mount we ask whether one is already going
   * and re-attach to it. Reloading mid-run now shows the run, not an empty panel. */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const st = await getGapCloseStatus().catch(() => null);
      if (cancelled || !st) return;
      setStatus(st);
      if (isRunning(st)) {
        setStartedAt(Date.parse(st.started_at) || Date.now());
        void watchRun();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [watchRun]);

  /* Both buttons run the same pipeline; `refined` only selects which endpoint and is
     kept in state so the progress strip can name the stage honestly. */
  const propose = async (refined = false) => {
    setBusy(true);
    setRefining(refined);
    setProposed(false);
    setStartedAt(Date.now());
    setMsg("");
    setStatus(null);
    try {
      await (refined ? gapCloseRefineCvs(selectedJobIds) : gapCloseCvs(selectedJobIds));
      setHidden(new Set());   // a fresh run deserves a fresh look
      seenAny.current = false;
      await watchRun();
    } catch (e) {
      setMsg(String(e));
      setBusy(false);
      setStartedAt(null);
    }
  };

  const toggleTicked = (id: string) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  /* Tailor exactly what is ticked, rather than every job that has an accepted item. A
     20-job review is normally worked through in batches, and tailoring costs tokens per
     job, so "all or nothing" was the wrong granularity. */
  const tickedJobIds = useMemo(
    () => visible.filter((s) => ticked.has(s.id)).map((s) => s.job_id as string),
    [visible, ticked],
  );

  const removeSelected = async () => {
    const ids = [...ticked];
    if (ids.length === 0) return;
    const label = ids.length === 1 ? "this job's proposals" : `proposals for ${ids.length} jobs`;
    if (!window.confirm(`Delete ${label}? Accepted items are discarded with them. The jobs, and any suggestions from "Analyze & suggest", are not affected.`))
      return;
    setDeleting(true);
    setMsg("");
    try {
      const { deleted } = await deleteGapClose(ids);
      setTicked(new Set());
      setOpenJobId((cur) => (cur && ids.includes(cur) ? null : cur));
      await refresh();
      setMsg(`Deleted proposals for ${deleted} job(s).`);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section className="rounded-2xl border border-amber-200 bg-amber-50/40 p-4 shadow-card">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Gap-close proposals <span className="text-amber-700">(you vouch for these)</span>
          </h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-600">
            This agent proposes what would win the job, including skills and bullets your CV
            doesn’t support — it isn’t limited to what your résumé proves. Nothing here reaches a
            CV until you accept it, and accepting means <em>you</em> can back it in an interview.
            Accepted items flow into <span className="font-medium text-slate-900">“Tailor only”</span> and
            are recorded on the draft as user-confirmed. “Prepare CVs” ignores them entirely and
            stays strictly résumé-evidenced.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button
            disabled={busy || selectedJobIds.length === 0}
            onClick={() => propose(false)}
            className="flex items-center gap-2 rounded-lg border border-amber-300 bg-white px-3.5 py-2 text-sm font-medium text-amber-800 shadow-sm transition hover:bg-amber-50 active:scale-[0.98] disabled:opacity-40"
            title={
              selectedJobIds.length === 0
                ? "Select jobs in the table first"
                : "One pass: propose gap-closing skills and bullets for the selected jobs"
            }
          >
            {busy && !refining && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-amber-300 border-t-amber-700" />
            )}
            {busy && !refining
              ? `Closing gaps for ${selectedJobIds.length}…`
              : `Propose for ${selectedJobIds.length}`}
          </button>
          <button
            disabled={busy || selectedJobIds.length === 0}
            onClick={() => propose(true)}
            className="flex items-center gap-2 rounded-lg bg-amber-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-amber-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-amber-600"
            title={
              selectedJobIds.length === 0
                ? "Select jobs in the table first"
                : "Three passes: propose, have a second agent review each item for defensibility and writing quality, then re-propose against its findings. Costs more per job."
            }
          >
            {busy && refining && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            )}
            {busy && refining
              ? `Reviewing ${selectedJobIds.length}…`
              : `Propose + review for ${selectedJobIds.length}`}
          </button>
        </div>
      </div>

      {(busy || isRunning(status)) && (
        <div className="mb-3">
          <StageStrip
            title={`Closing gaps for ${status?.total ?? selectedJobIds.length} job(s)`}
            stages={refining ? GAP_STAGES_REFINED : GAP_STAGES}
            startedAt={startedAt}
            tone="amber"
            stateOf={(k) => {
              // Both stages are reported by the runner now, so neither has to be guessed.
              if (k === "analyze") return status && status.phase !== "analyzing" && status.phase !== "starting" ? "done" : "active";
              if (status?.phase === "done") return "done";
              return status?.phase === "proposing" || status?.phase === "refining" ? "active" : "pending";
            }}
            note="Runs to completion — there is no time limit, and closing this page doesn't stop it."
          />
          <ProgressDetail status={status} />
        </div>
      )}

      {msg && !busy && <p className="mb-2 mt-2 text-xs text-slate-600">{msg}</p>}

      {withProposals.length > 0 && (
        <div className="mb-2 mt-3 flex flex-wrap items-center gap-2 border-t border-amber-200/70 pt-3 text-xs">
          <span className="text-slate-600">
            <span className="font-medium text-slate-900">{withProposals.length}</span> job(s) ·{" "}
            <span className="font-medium text-emerald-700">{totalAccepted}</span> item(s) accepted ·{" "}
            {reviewed.length} fully reviewed
          </span>
          {/* View only — nothing is deleted, and a card returns by itself if it stops being
              fully reviewed (regenerated proposals arrive undecided). */}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/* Deletion is destructive and permanent, so it is always explicit: tick the jobs
                you mean, then confirm. Nothing here is removed by tidying the view. */}
            <button
              onClick={() =>
                setTicked((prev) =>
                  prev.size === visible.length
                    ? new Set()
                    : new Set(visible.map((s) => s.id)),
                )
              }
              disabled={visible.length === 0}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-40"
            >
              {ticked.size === visible.length && visible.length > 0
                ? "Select none"
                : `Select all (${visible.length})`}
            </button>
            <button
              onClick={() => onSelectJobs(tickedJobIds)}
              disabled={tickedJobIds.length === 0}
              className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 active:scale-95 disabled:opacity-40 disabled:hover:bg-emerald-50"
              title="Select just these jobs and open Tailored CVs, ready for “Tailor only”"
            >
              Tailor ticked ({tickedJobIds.length})
            </button>
            <button
              onClick={removeSelected}
              disabled={ticked.size === 0 || deleting}
              className="flex items-center gap-1.5 rounded-md border border-rose-200 bg-rose-50 px-2 py-1 font-medium text-rose-700 transition hover:border-rose-300 hover:bg-rose-100 disabled:opacity-40 disabled:hover:bg-rose-50"
              title="Delete the ticked proposal sets — the jobs themselves are untouched"
            >
              {deleting && (
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-rose-300 border-t-rose-700" />
              )}
              {deleting ? "Deleting…" : `Delete ticked (${ticked.size})`}
            </button>
            <button
              onClick={() => setHidden(new Set(reviewed.map((s) => s.id)))}
              disabled={reviewed.length === 0 || hiddenCount === reviewed.length}
              className="rounded-md border border-slate-200 bg-white px-2 py-1 font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-40"
              title="Hide jobs you've finished reviewing — view only, nothing is deleted"
            >
              Clear reviewed from view ({reviewed.length})
            </button>
          </div>
          {hiddenCount > 0 && (
            <button
              onClick={() => setHidden(new Set())}
              className="rounded-md border border-amber-300 bg-white px-2 py-1 font-medium text-amber-700 transition hover:bg-amber-50"
            >
              Show all ({hiddenCount} hidden)
            </button>
          )}
        </div>
      )}

      {withProposals.length === 0 ? (
        <div className="rounded-lg border border-dashed border-amber-300 bg-white/60 py-8 text-center">
          <p className="text-sm font-medium text-slate-600">
            {busy ? "Working…" : "No proposals yet."}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            {busy
              ? "Proposals will appear here as each job finishes."
              : "Select jobs on the Dashboard, then “Propose”."}
          </p>
        </div>
      ) : (
        <>
          <div className="space-y-2">
            {visible.length === 0 && (
              <p className="py-4 text-center text-xs text-slate-500">
                Every job here is fully reviewed and cleared from view — “Show all” to bring
                them back.
              </p>
            )}
            {visible.map((s) => (
              <JobProposals
                key={s.id}
                suggestion={s}
                title={jobTitleById(s.job_id as string)}
                open={openJobId === s.id}
                onToggleOpen={() => setOpenJobId((cur) => (cur === s.id ? null : s.id))}
                onReviewed={refresh}
                selected={ticked.has(s.id)}
                onToggleSelected={() => toggleTicked(s.id)}
              />
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs leading-relaxed text-slate-600">
            {totalAccepted === 0 ? (
              <p>Nothing accepted yet — “Tailor only” will run on your résumé evidence alone.</p>
            ) : (
              <>
                <p className="min-w-0 flex-1">
                  <span className="font-semibold text-emerald-700">
                    {totalAccepted} item(s) accepted
                  </span>{" "}
                  across {acceptedJobIds.length} job(s). Run “Tailor only” in Tailored CVs to build
                  drafts that include them, then review, edit, approve and export as usual.
                </p>
                {/* Accepting an item never selects its job, and “Tailor only” is disabled
                    on an empty selection — so without this the accepted work is stranded. */}
                <button
                  onClick={() => onSelectJobs(acceptedJobIds)}
                  className="shrink-0 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 font-medium text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 active:scale-95"
                  title="Select every job with an accepted item and open Tailored CVs. To do a subset, tick the cards above and use “Tailor ticked”."
                >
                  Tailor all {acceptedJobIds.length} job(s)
                </button>
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}
