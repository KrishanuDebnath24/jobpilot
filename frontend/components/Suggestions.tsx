"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PipelineRun, Suggestion } from "@/lib/types";
import {
  getAnalyzeRuns,
  getEnhanceRuns,
  getSuggestions,
  runAnalyze,
  runEnhance,
  setSuggestionStatus,
  type Scope,
} from "@/lib/api";

type Phase = "idle" | "analyzing" | "suggesting" | "error";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Trigger a stage, then poll its runs until a NEW terminal run appears. */
async function runAndWait(
  trigger: () => Promise<unknown>,
  list: () => Promise<PipelineRun[]>,
): Promise<PipelineRun | null> {
  const before = new Set((await list()).map((r) => r.id));
  await trigger();
  for (let i = 0; i < 120; i++) {
    await sleep(3000);
    const fresh = (await list()).filter((r) => !before.has(r.id));
    const done = fresh.find((r) => r.status === "completed" || r.status === "failed");
    if (done) return done;
  }
  return null;
}

function Bar({ rows }: { rows?: { label: string; pct: number }[] }) {
  if (!rows?.length) return null;
  return (
    <div className="space-y-1">
      {rows.slice(0, 6).map((r) => (
        <div key={r.label} className="flex items-center gap-2 text-xs">
          <span className="w-40 truncate text-neutral-300">{r.label}</span>
          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-800">
            <span className="block h-full rounded-full bg-emerald-500/70" style={{ width: `${r.pct}%` }} />
          </span>
          <span className="w-10 text-right font-mono tabular-nums text-neutral-400">{r.pct}%</span>
        </div>
      ))}
    </div>
  );
}

function StatusPill({ status }: { status: Suggestion["status"] }) {
  const map: Record<string, string> = {
    accepted: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
    dismissed: "border-neutral-700 bg-neutral-800/50 text-neutral-500",
    proposed: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  };
  return (
    <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${map[status]}`}>{status}</span>
  );
}

function SuggestionCard({
  s,
  title,
  open,
  onToggleOpen,
  onStatus,
}: {
  s: Suggestion;
  title: string;
  /** Accordion state, owned by the list so only one job's suggestions are ever expanded. */
  open: boolean;
  onToggleOpen: () => void;
  onStatus: (id: string, status: "accepted" | "dismissed") => void;
}) {
  const detail = s.projects_json?.skills_detail ?? [];
  const projects = s.projects_json?.projects ?? [];
  const rewrites = s.bullet_rewrites_json?.bullet_rewrites ?? [];
  const agg = s.projects_json?.aggregate;
  const dim = s.status === "dismissed";
  // Collapsed cards still say what's inside them, so you can triage without expanding.
  const counts = [
    detail.length && `${detail.length} skill${detail.length === 1 ? "" : "s"}`,
    projects.length && `${projects.length} project${projects.length === 1 ? "" : "s"}`,
    rewrites.length && `${rewrites.length} rewrite${rewrites.length === 1 ? "" : "s"}`,
  ].filter(Boolean) as string[];
  return (
    <div className={`rounded-xl border border-neutral-800 bg-neutral-950/40 p-4 ${dim ? "opacity-50" : ""}`}>
      <div className={`flex flex-wrap items-center gap-2 ${open ? "mb-3" : ""}`}>
        <button
          onClick={onToggleOpen}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-2 text-left text-sm font-medium text-neutral-100"
        >
          <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
          <span className="truncate">{title}</span>
        </button>
        <StatusPill status={s.status} />
        <span className="rounded-full border border-neutral-700 px-2 py-0.5 text-[10px] text-neutral-500">
          {counts.length > 0 ? counts.join(" · ") : "nothing to show"}
        </span>
        <div className="ml-auto flex gap-1.5">
          <button
            onClick={() => onStatus(s.id, "accepted")}
            className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300 transition hover:bg-emerald-500/20"
          >
            Accept
          </button>
          <button
            onClick={() => onStatus(s.id, "dismissed")}
            className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400 transition hover:border-neutral-500"
          >
            Dismiss
          </button>
        </div>
      </div>

      {open && (
        <>
          {agg && (
            <div className="mb-3 grid gap-3 rounded-lg border border-neutral-800/70 bg-neutral-900/30 p-3 sm:grid-cols-2">
              <p className="text-xs text-neutral-500 sm:col-span-2">
                Frequencies {agg.basis ?? `computed from ${agg.total_jobs ?? 0} jobs`}
                {agg.snippet_jobs ? (
                  <span className="text-neutral-600"> — {agg.snippet_jobs} snippet-only job(s) excluded</span>
                ) : null}
              </p>
              <div>
                <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">
                  Recurring gaps ({agg.full_text_jobs ?? agg.total_jobs} full-text jobs)
                </p>
                <Bar rows={agg.recurring_gaps} />
              </div>
              <div>
                <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">Most-required skills</p>
                <Bar rows={agg.skill_frequency} />
              </div>
              {agg.certification_frequency && agg.certification_frequency.length > 0 && (
                <div className="sm:col-span-2">
                  <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">Certifications requested</p>
                  <Bar rows={agg.certification_frequency} />
                </div>
              )}
            </div>
          )}

          {detail.length > 0 && (
            <div className="mb-3">
              <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">
                Skills to add <span className="normal-case text-neutral-600">(only if truthfully held)</span>
              </p>
              <ul className="space-y-1.5 text-sm text-neutral-300">
                {detail.map((sk, i) => (
                  <li key={i} className="rounded-md border border-neutral-800/70 px-2.5 py-1.5">
                    <span className="font-medium text-neutral-100">{sk.skill}</span>
                    {sk.frequency && <span className="ml-2 text-xs text-emerald-300">{sk.frequency}</span>}
                    {sk.why && <span className="block text-xs text-neutral-400">why: {sk.why}</span>}
                    {sk.truthful_basis && (
                      <span className="block text-xs text-neutral-500">basis: {sk.truthful_basis}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {projects.length > 0 && (
            <div className="mb-3">
              <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">
                Projects to build a real gap skill
              </p>
              <ul className="space-y-1.5 text-sm text-neutral-300">
                {projects.map((p, i) => (
                  <li key={i} className="rounded-md border border-neutral-800/70 px-2.5 py-1.5">
                    <span className="font-medium text-neutral-100">{p.title}</span>
                    <span className="ml-2 text-xs text-amber-300">builds: {p.builds_skill}</span>
                    <span className="block text-xs text-neutral-400">{p.description}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {rewrites.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs uppercase tracking-wide text-neutral-500">
                Bullet rewrites <span className="normal-case text-neutral-600">(reframe genuine experience)</span>
              </p>
              <ul className="space-y-2 text-sm">
                {rewrites.map((r, i) => (
                  <li key={i} className="rounded-md border border-neutral-800/70 px-2.5 py-1.5">
                    <p className="text-neutral-500 line-through decoration-neutral-700">{r.before}</p>
                    <p className="text-emerald-200">{r.after}</p>
                    <p className="mt-1 text-xs text-neutral-500">grounding: {r.grounding}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function Suggestions({
  selectedJobIds,
  qualifiedCount,
  jobTitleById,
}: {
  selectedJobIds: string[];
  qualifiedCount: number;
  jobTitleById: (id: string) => string;
}) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [msg, setMsg] = useState<string>("");
  // Accordion: one card expanded at a time. These used to all render open, so the first one
  // is opened for you when a set arrives — otherwise the panel would look empty after a run.
  // Guarded by a ref so a deliberate collapse is never undone.
  const [openId, setOpenId] = useState<string | null>(null);
  const seenAny = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setSuggestions(await getSuggestions());
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (scope: Scope, label: string) => {
    setPhase("analyzing");
    setMsg(`Analyzing ${label}…`);
    try {
      const a = await runAndWait(() => runAnalyze(scope), getAnalyzeRuns);
      if (!a || a.status === "failed") throw new Error(a?.error ?? "analyze did not complete");
      setPhase("suggesting");
      setMsg("Generating suggestions…");
      const e = await runAndWait(() => runEnhance(scope), getEnhanceRuns);
      if (!e || e.status === "failed") throw new Error(e?.error ?? "enhance did not complete");
      await refresh();
      setPhase("idle");
      setMsg("Suggestions ready.");
    } catch (err) {
      setPhase("error");
      setMsg(String(err));
    }
  };

  const busy = phase === "analyzing" || phase === "suggesting";
  const aggregate = suggestions.find((s) => s.job_id === null);
  const perJob = suggestions.filter((s) => s.job_id !== null);

  // Open the first card the very first time suggestions show up (e.g. on page load with a set
  // already stored).
  useEffect(() => {
    if (!seenAny.current && suggestions.length > 0) {
      seenAny.current = true;
      setOpenId((aggregate ?? suggestions[0]).id);
    }
  }, [suggestions, aggregate]);

  return (
    <section className="mt-8 rounded-2xl border border-neutral-800 bg-neutral-900/30 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold text-neutral-200">Analyze &amp; suggest</h2>
        <button
          disabled={busy || selectedJobIds.length === 0}
          onClick={() => run({ job_ids: selectedJobIds }, `${selectedJobIds.length} selected`)}
          className="rounded-lg border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 transition hover:border-neutral-500 disabled:opacity-40"
        >
          Analyze &amp; suggest ({selectedJobIds.length} selected)
        </button>
        <button
          disabled={busy || qualifiedCount === 0}
          onClick={() => run({ all_qualified: true }, `all ${qualifiedCount} qualified`)}
          className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-200 transition hover:bg-emerald-500/20 disabled:opacity-40"
        >
          Analyze all qualified ({qualifiedCount})
        </button>
        {msg && (
          <span className={`ml-auto text-sm ${phase === "error" ? "text-rose-300" : "text-neutral-400"}`}>
            {busy && <span className="mr-1 inline-block animate-pulse">●</span>}
            {msg}
          </span>
        )}
      </div>

      {suggestions.length === 0 ? (
        <p className="rounded-lg border border-dashed border-neutral-800 py-8 text-center text-sm text-neutral-500">
          Select jobs above (or use “all qualified”) and generate grounded, non-fabricating suggestions.
        </p>
      ) : (
        <div className="space-y-3">
          {aggregate && (
            <SuggestionCard
              s={aggregate}
              title="Across your qualified set (frequency-prioritized)"
              open={openId === aggregate.id}
              onToggleOpen={() => setOpenId((cur) => (cur === aggregate.id ? null : aggregate.id))}
              onStatus={(id, st) => setSuggestionStatus(id, st).then(refresh)}
            />
          )}
          {perJob.map((s) => (
            <SuggestionCard
              key={s.id}
              s={s}
              title={jobTitleById(s.job_id as string)}
              open={openId === s.id}
              onToggleOpen={() => setOpenId((cur) => (cur === s.id ? null : s.id))}
              onStatus={(id, st) => setSuggestionStatus(id, st).then(refresh)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
