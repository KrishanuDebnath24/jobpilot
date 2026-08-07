"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ApplyCoverage } from "@/lib/types";
import { getApplyCoverage, resolveApplyTargets } from "@/lib/api";

/* Apply agent stage 2: how much of the job pool has a reachable application form.
 *
 * Most connectors store a tracking redirect rather than an application page, so until
 * those links are followed you don't actually know which of your jobs you could apply to.
 * This panel answers that, and lets you kick off the resolution.
 *
 * The run is a queued background task with deliberate pacing (it touches employers'
 * servers), so the button returns immediately and the panel polls coverage while numbers
 * climb. Nothing here submits anything — it only follows links. */

const ATS_LABELS: Record<string, string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  workday: "Workday",
  ashby: "Ashby",
  smartrecruiters: "SmartRecruiters",
  workable: "Workable",
  teamtailor: "Teamtailor",
  personio: "Personio",
  recruitee: "Recruitee",
  bamboohr: "BambooHR",
  icims: "iCIMS",
  taleo: "Taleo",
  rezoomo: "Rezoomo",
  occupop: "Occupop",
  aggregator: "No form behind the listing",
  unknown: "Unrecognised platform",
  blocked: "Blocked by the site",
};

// Not reachable, and shown muted. "blocked" sits here too, but is coloured
// separately below because it says nothing about the job — only about our access.
const DIM = new Set(["aggregator", "unknown", "blocked"]);

export default function ApplyCoveragePanel() {
  const [coverage, setCoverage] = useState<ApplyCoverage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      setCoverage(await getApplyCoverage());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    return () => {
      if (poll.current) clearInterval(poll.current);
    };
  }, [load]);

  // While a run is in flight the numbers climb in the background, so poll rather than
  // leaving a stale count on screen. Stops once nothing is left unresolved.
  const startPolling = useCallback(() => {
    if (poll.current) clearInterval(poll.current);
    poll.current = setInterval(async () => {
      const next = await getApplyCoverage().catch(() => null);
      if (!next) return;
      setCoverage(next);
      if (next.unresolved === 0 && poll.current) {
        clearInterval(poll.current);
        poll.current = null;
        setBusy(false);
      }
    }, 4000);
  }, []);

  const resolve = async (force: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await resolveApplyTargets(200, force);
      startPolling();
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  if (!coverage) return null;

  const { total, resolved, unresolved, reachable, by_ats: byAts } = coverage;
  const pctResolved = total > 0 ? Math.round((resolved / total) * 100) : 0;
  const entries = Object.entries(byAts);
  const max = Math.max(...entries.map(([, n]) => n), 1);

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card transition-shadow hover:shadow-card-hover">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold text-slate-900">Application reachability</h2>
        <span className="text-xs text-slate-500">
          Which jobs have a real form behind the listing link.
        </span>
        <div className="ml-auto flex gap-2">
          <button
            onClick={() => resolve(false)}
            disabled={busy || unresolved === 0}
            className="flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-slate-900"
            title={
              unresolved === 0
                ? "Every job has been resolved"
                : `Follow the listing links for ${unresolved} unresolved job(s)`
            }
          >
            {busy && (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            )}
            {busy ? "Resolving…" : `Resolve ${unresolved || ""}`.trim()}
          </button>
          {resolved > 0 && (
            <button
              onClick={() => resolve(true)}
              disabled={busy}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98] disabled:opacity-40"
              title="Re-follow every link, including ones already resolved. Useful after new platforms are supported."
            >
              Re-check all
            </button>
          )}
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-2xl font-semibold tabular-nums tracking-tight text-emerald-600">
          {reachable}
        </span>
        <span className="text-sm text-slate-600">
          job{reachable === 1 ? "" : "s"} you can apply to
        </span>
        <span className="ml-auto text-xs text-slate-400 tabular-nums">
          {resolved} of {total} checked ({pctResolved}%)
        </span>
      </div>

      <span className="mb-3 block h-1.5 overflow-hidden rounded-full bg-slate-100">
        <span
          className="block h-full rounded-full bg-emerald-500 transition-all duration-700"
          style={{ width: `${pctResolved}%` }}
        />
      </span>

      {entries.length === 0 ? (
        <p className="text-xs text-slate-400">
          Nothing checked yet — “Resolve” follows each listing link to find where its
          application form lives. It only follows links; nothing is submitted.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {entries.map(([ats, count]) => (
            <li key={ats} className="flex items-center gap-2 text-xs">
              <span
                className={`w-40 shrink-0 truncate ${DIM.has(ats) ? "text-slate-400" : "font-medium text-slate-700"}`}
              >
                {ATS_LABELS[ats] ?? ats}
              </span>
              <span className="relative h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                <span
                  className={`absolute inset-y-0 left-0 rounded-full transition-all duration-500 ${
                    DIM.has(ats) ? "bg-slate-300" : "bg-emerald-500"
                  }`}
                  style={{ width: `${Math.max(3, (count / max) * 100)}%` }}
                />
              </span>
              <span className="w-10 shrink-0 text-right tabular-nums text-slate-600">{count}</span>
            </li>
          ))}
        </ul>
      )}

      {error && <p className="mt-2 text-xs text-rose-600">{error}</p>}
    </section>
  );
}
