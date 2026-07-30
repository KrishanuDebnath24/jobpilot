"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  GapClose,
  GapCloseKind,
  GapClosePatch,
  Suggestion,
} from "@/lib/types";
import { gapCloseCvs, getSuggestions, reviewGapClose } from "@/lib/api";
import StageStrip from "./StageStrip";

const GAP_STAGES = [
  { key: "analyze", label: "analyze the jobs", unobservable: true },
  { key: "propose", label: "close gaps (skills, rewrites, new bullets)" },
];

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
  new_bullets: "New bullets",
};

/** The item's editable body text, per kind — what the textarea edits and what we send back. */
function bodyOf(kind: GapCloseKind, item: Record<string, unknown>): string {
  if (kind === "claimed_skills") return String(item.skill ?? "");
  if (kind === "bullet_rewrites") return String(item.after ?? "");
  return String(item.text ?? "");
}
function bodyField(kind: GapCloseKind): "skill" | "after" | "text" {
  return kind === "claimed_skills" ? "skill" : kind === "bullet_rewrites" ? "after" : "text";
}

function countAccepted(gc: GapClose | null): number {
  if (!gc) return 0;
  return (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).reduce(
    (n, k) => n + (gc[k] ?? []).filter((i) => i.accepted === true).length,
    0,
  );
}
function countTotal(gc: GapClose | null): number {
  if (!gc) return 0;
  return (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).reduce(
    (n, k) => n + (gc[k] ?? []).length,
    0,
  );
}
/** Items still awaiting a decision — what makes a card "needs your attention". */
function countUndecided(gc: GapClose | null): number {
  if (!gc) return 0;
  return (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).reduce(
    (n, k) => n + (gc[k] ?? []).filter((i) => i.accepted === null || i.accepted === undefined).length,
    0,
  );
}
/** Fingerprint used to detect that a background gap-close run has landed. */
function fingerprint(rows: Suggestion[]): string {
  return rows
    .filter((s) => s.job_id)
    .map((s) => `${s.job_id}:${JSON.stringify(s.gap_close_json ?? null).length}`)
    .sort()
    .join("|");
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
  const roleRef = typeof item.role_ref === "string" ? item.role_ref : null;

  return (
    <li
      className={`rounded-lg border px-3 py-2 transition ${
        accepted
          ? "border-emerald-500/40 bg-emerald-500/5"
          : rejected
            ? "border-neutral-800 bg-neutral-950/40 opacity-45"
            : "border-neutral-800 bg-neutral-950/40"
      }`}
    >
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          {roleRef && (
            <span className="mr-1.5 rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-400">
              {roleRef}
            </span>
          )}
          {editing ? (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={kind === "claimed_skills" ? 1 : 3}
              className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-100 outline-none focus:border-sky-500/60"
            />
          ) : (
            <span className="text-sm text-neutral-200">{body || <em className="text-neutral-500">(empty)</em>}</span>
          )}
          {kind === "bullet_rewrites" && typeof item.before === "string" && item.before && !editing && (
            <p className="mt-0.5 text-xs text-neutral-500 line-through decoration-neutral-700">
              {item.before}
            </p>
          )}
          {typeof item.closes === "string" && item.closes && (
            <p className="mt-1 text-xs text-neutral-500">closes: {item.closes}</p>
          )}
          {typeof item.interview_prep === "string" && item.interview_prep && (
            <p className="mt-1 rounded-md border border-amber-500/20 bg-amber-500/5 px-2 py-1 text-xs text-amber-200/90">
              <span className="font-medium">You must be able to say:</span> {item.interview_prep}
            </p>
          )}
          <div className="mt-1 flex flex-wrap gap-1.5">
            {needsWord && (
              <span className="rounded-full border border-amber-500/30 px-1.5 py-0.5 text-[10px] text-amber-300">
                your word is the only evidence
              </span>
            )}
            {item.user_edited === true && (
              <span className="rounded-full border border-sky-500/30 px-1.5 py-0.5 text-[10px] text-sky-300">
                edited by you
              </span>
            )}
            {accepted && (
              <span className="rounded-full border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-300">
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
                className="rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-200 disabled:opacity-40"
              >
                Save
              </button>
              <button
                onClick={() => {
                  setText(body);
                  setEditing(false);
                }}
                className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400"
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
                className={`rounded-md border px-2 py-0.5 text-xs transition active:scale-95 ${
                  accepted
                    ? "border-emerald-500/50 bg-emerald-500/20 text-emerald-100"
                    : "border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                }`}
                title="I can back this in an interview"
              >
                {accepted ? "✓ Accepted" : "Accept"}
              </button>
              <button
                onClick={() => onPatch({ kind, id: item.id, accepted: false })}
                className={`rounded-md border px-2 py-0.5 text-xs transition active:scale-95 ${
                  rejected
                    ? "border-neutral-600 bg-neutral-800 text-neutral-300"
                    : "border-neutral-700 text-neutral-400 hover:border-neutral-500"
                }`}
              >
                {rejected ? "Rejected" : "Reject"}
              </button>
              <button
                disabled={busy}
                onClick={() => setEditing(true)}
                className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400 hover:border-neutral-500 disabled:opacity-40"
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
  onReviewed,
}: {
  suggestion: Suggestion;
  title: string;
  onReviewed: () => Promise<void> | void;
}) {
  const gc = suggestion.gap_close_json;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
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

  const kinds = (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).filter(
    (k) => (gc?.[k] ?? []).length > 0,
  );
  const total = countTotal(gc);
  // Counts follow the optimistic view too, so the header moves with the click.
  const accepted = (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).reduce(
    (n, k) =>
      n +
      (gc?.[k] ?? []).filter((i) => {
        const p = pending[`${k}:${i.id}`];
        return (p === undefined ? i.accepted : p) === true;
      }).length,
    0,
  );
  const undecided = (["claimed_skills", "bullet_rewrites", "new_bullets"] as GapCloseKind[]).reduce(
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
    <div className="rounded-xl border border-neutral-800 bg-neutral-950/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => setOpen(!open)} className="text-sm font-medium text-neutral-100">
          {open ? "▾" : "▸"} {title}
        </button>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] transition ${
            accepted > 0
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
              : "border-neutral-700 text-neutral-500"
          }`}
        >
          {accepted} of {total} accepted
        </span>
        {undecided > 0 ? (
          <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-300">
            {undecided} awaiting you
          </span>
        ) : (
          <span className="rounded-full border border-neutral-700 px-2 py-0.5 text-[10px] text-neutral-500">
            reviewed
          </span>
        )}
        <div className="ml-auto flex gap-1.5">
          <button
            disabled={busy}
            onClick={() => submit(allPatches(true))}
            className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300 disabled:opacity-40"
            title="I can back every item here"
          >
            Accept all
          </button>
          <button
            disabled={busy}
            onClick={() => submit(allPatches(false))}
            className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400 disabled:opacity-40"
          >
            Reject all
          </button>
        </div>
      </div>

      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}

      {open && (
        <div className="mt-3 space-y-3">
          {kinds.map((kind) => (
            <div key={kind}>
              <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">
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

export default function GapCloseProposals({
  selectedJobIds,
  jobTitleById,
}: {
  selectedJobIds: string[];
  jobTitleById: (id: string) => string;
}) {
  const [rows, setRows] = useState<Suggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>("");
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [proposed, setProposed] = useState(false);   // proposals observed landing this run
  // View-only: suggestion ids cleared from the list. Nothing is deleted, and a job reappears
  // by itself if it stops being fully reviewed (e.g. its proposals were regenerated).
  const [hidden, setHidden] = useState<Set<string>>(new Set());

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

  /* The runner is a Celery task with no pipeline_runs row, so there's nothing to poll by id.
   * Instead: snapshot a fingerprint of the stored proposal sets, trigger, and wait for it to
   * change. That detects a regenerate (same job count, different content) as well as a first
   * run, which a plain "does every job have proposals yet" check would miss. */
  const propose = async () => {
    setBusy(true);
    setProposed(false);
    setStartedAt(Date.now());
    setMsg("");
    try {
      const before = fingerprint(await getSuggestions());
      await gapCloseCvs(selectedJobIds);
      for (let i = 0; i < 80; i++) {
        await sleep(3000);
        const fresh = await getSuggestions();
        if (fingerprint(fresh) !== before) {
          setRows(fresh);
          setProposed(true);
          setHidden(new Set());   // fresh proposals deserve a fresh look
          setMsg("Proposals ready — review each item below.");
          return;
        }
      }
      setMsg(
        "Still running after 4 minutes. If this doesn't resolve, check the Celery worker is " +
          "up — the task is queued either way and will appear here when it finishes.",
      );
      await refresh();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
      setStartedAt(null);
    }
  };

  return (
    <section className="mt-6 rounded-2xl border border-amber-500/20 bg-amber-500/[0.03] p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-sm font-semibold text-neutral-200">
            Gap-close proposals <span className="text-amber-300/80">(you vouch for these)</span>
          </h2>
          <p className="mt-0.5 max-w-3xl text-xs text-neutral-500">
            This agent proposes what would win the job, including skills and bullets your CV
            doesn’t support — it isn’t limited to what your résumé proves. Nothing here reaches a
            CV until you accept it, and accepting means <em>you</em> can back it in an interview.
            Accepted items flow into <span className="text-neutral-300">“Tailor only”</span> and
            are recorded on the draft as user-confirmed. “Prepare CVs” ignores them entirely and
            stays strictly résumé-evidenced.
          </p>
        </div>
        <button
          disabled={busy || selectedJobIds.length === 0}
          onClick={propose}
          className="ml-auto rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-sm text-amber-200 transition hover:bg-amber-500/20 disabled:opacity-40"
          title={
            selectedJobIds.length === 0
              ? "Select jobs in the table first"
              : "Propose gap-closing skills and bullets for the selected jobs"
          }
        >
          {busy
            ? `Closing gaps for ${selectedJobIds.length}…`
            : `Propose for ${selectedJobIds.length} selected`}
        </button>
      </div>

      {busy && (
        <StageStrip
          title={`Closing gaps for ${selectedJobIds.length} job(s)`}
          stages={GAP_STAGES}
          startedAt={startedAt}
          tone="amber"
          stateOf={(k) => {
            if (k === "propose") return proposed ? "done" : "active";
            return "active";   // analyze runs first and can't be observed from here
          }}
          note="Runs the model once per job. ◌ = can't be observed from here, so it is never ticked."
        />
      )}

      {msg && !busy && <p className="mb-2 mt-2 text-xs text-neutral-400">{msg}</p>}

      {withProposals.length > 0 && (
        <div className="mb-2 mt-3 flex flex-wrap items-center gap-2 border-t border-neutral-800 pt-2 text-xs">
          <span className="text-neutral-500">
            {withProposals.length} job(s) · {totalAccepted} item(s) accepted ·{" "}
            {reviewed.length} fully reviewed
          </span>
          {/* View only — nothing is deleted, and a card returns by itself if it stops being
              fully reviewed (regenerated proposals arrive undecided). */}
          <button
            onClick={() => setHidden(new Set(reviewed.map((s) => s.id)))}
            disabled={reviewed.length === 0 || hiddenCount === reviewed.length}
            className="ml-auto rounded-md border border-neutral-700 px-2 py-1 text-neutral-400 hover:border-neutral-500 disabled:opacity-40"
            title="Hide jobs you've finished reviewing — view only, nothing is deleted"
          >
            Clear reviewed from view ({reviewed.length})
          </button>
          {hiddenCount > 0 && (
            <button
              onClick={() => setHidden(new Set())}
              className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-200"
            >
              Show all ({hiddenCount} hidden)
            </button>
          )}
        </div>
      )}

      {withProposals.length === 0 ? (
        <p className="rounded-lg border border-dashed border-neutral-800 py-6 text-center text-xs text-neutral-500">
          {busy
            ? "Working… proposals will appear here as each job finishes."
            : "No proposals yet. Select jobs in the table above, then “Propose”."}
        </p>
      ) : (
        <>
          <div className="space-y-2">
            {visible.length === 0 && (
              <p className="py-4 text-center text-xs text-neutral-500">
                Every job here is fully reviewed and cleared from view — “Show all” to bring
                them back.
              </p>
            )}
            {visible.map((s) => (
              <JobProposals
                key={s.id}
                suggestion={s}
                title={jobTitleById(s.job_id as string)}
                onReviewed={refresh}
              />
            ))}
          </div>
          <p className="mt-3 text-xs text-neutral-500">
            {totalAccepted === 0 ? (
              <>Nothing accepted yet — “Tailor only” will run on your résumé evidence alone.</>
            ) : (
              <>
                <span className="text-emerald-300">{totalAccepted} item(s) accepted.</span> Run
                “Tailor only” in Tailored CVs to build drafts that include them, then review,
                edit, approve and export as usual.
              </>
            )}
          </p>
        </>
      )}
    </section>
  );
}
