"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CvDraft, Job, QualifierConfig, SearchConfig, SearchConfigProposal } from "@/lib/types";
import {
  deriveSearchConfig,
  getBaseResumeId,
  getCvDrafts,
  getDashboardJobs,
  getMe,
  getQualifierConfig,
  getSearchConfig,
  getToken,
  setToken,
} from "@/lib/api";
import LoginGate from "./LoginGate";
import PipelineLive from "./PipelineLive";
import JobsTable from "./JobsTable";
import Suggestions from "./Suggestions";
import GapCloseProposals from "./GapCloseProposals";
import Chat from "./Chat";
import Ask from "./Ask";
import Gates from "./Gates";
import TailoredCVs from "./TailoredCVs";
import CostPanel from "./CostPanel";
import ResumeUpload from "./ResumeUpload";
import SearchSettings, { type SearchSettingsMode } from "./SearchSettings";
import QualifierSettings from "./QualifierSettings";
import ApplicationProfileEditor from "./ApplicationProfile";

type Decision = "all" | "qualified" | "disqualified" | "unscored";

/* The four screens, in the order you work through them. Everything after Dashboard is a long
 * review surface you go through job by job, so each gets its own screen instead of stacking
 * below the jobs table. Screens are swapped with CSS, not by unmounting: analyze/suggest and
 * gap-close runs poll for minutes and the CV panel polls every 5s, so navigating away must
 * not tear any of them down. */
type View = "dashboard" | "suggest" | "gap-close" | "tailored" | "apply-profile";

/** Inline glyphs — no icon dependency, and they inherit currentColor. */
function NavIcon({ name, className = "" }: { name: View; className?: string }) {
  const common = {
    className: `h-4 w-4 shrink-0 ${className}`,
    viewBox: "0 0 20 20",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  if (name === "dashboard")
    return (
      <svg {...common}>
        <rect x="2.5" y="2.5" width="6" height="6" rx="1.5" />
        <rect x="11.5" y="2.5" width="6" height="6" rx="1.5" />
        <rect x="2.5" y="11.5" width="6" height="6" rx="1.5" />
        <rect x="11.5" y="11.5" width="6" height="6" rx="1.5" />
      </svg>
    );
  if (name === "suggest")
    return (
      <svg {...common}>
        <path d="M10 2.5 11.9 7l4.6.4-3.5 3 1 4.5L10 12.5 5.9 15l1-4.5-3.4-3L8.1 7Z" />
      </svg>
    );
  if (name === "gap-close")
    return (
      <svg {...common}>
        <path d="M7.5 10a2.5 2.5 0 0 1 2.5-2.5h3.5a2.5 2.5 0 0 1 0 5H12" />
        <path d="M12.5 10a2.5 2.5 0 0 1-2.5 2.5H6.5a2.5 2.5 0 0 1 0-5H8" />
      </svg>
    );
  if (name === "apply-profile")
    return (
      <svg {...common}>
        <path d="M10 10.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" />
        <path d="M3.5 17c.6-3 3.3-4.5 6.5-4.5s5.9 1.5 6.5 4.5" />
      </svg>
    );
  return (
    <svg {...common}>
      <path d="M11.5 2.5H6a1.5 1.5 0 0 0-1.5 1.5v12A1.5 1.5 0 0 0 6 17.5h8a1.5 1.5 0 0 0 1.5-1.5V6.5Z" />
      <path d="M11.5 2.5v4h4M7.5 11h5M7.5 14h3" />
    </svg>
  );
}

const NAV: { key: View; label: string; hint: string }[] = [
  { key: "dashboard", label: "Dashboard", hint: "Pipeline, agents, jobs" },
  { key: "suggest", label: "Analyze & suggest", hint: "Grounded, no fabrication" },
  { key: "gap-close", label: "Gap-close proposals", hint: "Vouch, item by item" },
  { key: "tailored", label: "Tailored CVs", hint: "Draft, approve, export" },
  { key: "apply-profile", label: "Application details", hint: "Pre-configured answers" },
];

/** Shown only when its screen is active; the others stay mounted and keep running. */
function Screen({ active, children }: { active: boolean; children: React.ReactNode }) {
  return <div className={active ? "screen-enter" : "hidden"}>{children}</div>;
}

/** A labelled metric in the header strip — the pool at a glance, on every screen. */
function Stat({
  label,
  value,
  tone = "slate",
}: {
  label: string;
  value: React.ReactNode;
  tone?: "slate" | "emerald" | "indigo";
}) {
  const valueTone =
    tone === "emerald" ? "text-emerald-600" : tone === "indigo" ? "text-indigo-600" : "text-slate-900";
  return (
    <div className="min-w-[5.5rem] rounded-xl border border-slate-200 bg-white px-3 py-2 shadow-card">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p className={`mt-0.5 text-lg font-semibold tabular-nums leading-none ${valueTone}`}>{value}</p>
    </div>
  );
}

export default function Dashboard() {
  const [view, setView] = useState<View>("dashboard");
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string>("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [decision, setDecision] = useState<Decision>("all");
  const [source, setSource] = useState<string>("all");
  const [employment, setEmployment] = useState<string>("all");
  const [seniority, setSeniority] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const loadingJobs = useRef(false);

  // Search config + the settings modal (onboarding / edit / review) that manages it.
  const [config, setConfig] = useState<SearchConfig | null>(null);
  const [settings, setSettings] = useState<{
    open: boolean;
    mode: SearchSettingsMode;
    proposal: SearchConfigProposal | null;
    seq: number; // bumped per open so the modal remounts and re-inits its state
  }>({ open: false, mode: "edit", proposal: null, seq: 0 });

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await getSearchConfig();
      setConfig(cfg);
      // No id == never saved a config → block on onboarding until they set one.
      if (!cfg.id) {
        setSettings((s) => ({ open: true, mode: "onboarding", proposal: null, seq: s.seq + 1 }));
      }
    } catch {
      /* a config fetch hiccup shouldn't wedge the dashboard */
    }
  }, []);

  // Qualifier config: scoring weights/threshold + the must-have hard filter.
  const [qualifierConfig, setQualifierConfig] = useState<QualifierConfig | null>(null);
  const [qualifierOpen, setQualifierOpen] = useState(false);

  const loadQualifierConfig = useCallback(async () => {
    try {
      setQualifierConfig(await getQualifierConfig());
    } catch {
      /* best-effort; never wedge the dashboard */
    }
  }, []);

  const openSettings = useCallback((mode: SearchSettingsMode, proposal: SearchConfigProposal | null = null) => {
    setSettings((s) => ({ open: true, mode, proposal, seq: s.seq + 1 }));
  }, []);

  const onConfigSaved = useCallback((cfg: SearchConfig) => {
    setConfig(cfg);
    setSettings((s) => ({ ...s, open: false, proposal: null }));
  }, []);

  // Per-job tailored CVs to attach to job rows + the current uploaded-résumé id (a tailored
  // CV is "attached" only if exported and built from the current uploaded résumé — matches
  // the CV-aware qualifier; a newer UPLOAD makes older drafts stale, so they fall away here
  // too, while applying suggestions — an assembled version — never affects this).
  const [cvDrafts, setCvDrafts] = useState<CvDraft[]>([]);
  const [resumeId, setResumeId] = useState<string | null>(null);
  // Bumped after a Concierge turn so the Tailored-CVs panel reloads immediately (its
  // drafts change synchronously now — no need to wait for its 5s poll or a page refresh).
  const [cvRefresh, setCvRefresh] = useState(0);

  const loadCvContext = useCallback(async () => {
    try {
      const [drafts, id] = await Promise.all([getCvDrafts(), getBaseResumeId()]);
      setCvDrafts(drafts);
      setResumeId(id);
    } catch {
      /* CV context is best-effort; never wedge the dashboard */
    }
  }, []);

  const cvDraftByJobId = useMemo(() => {
    const map: Record<string, CvDraft> = {};
    for (const d of cvDrafts) {
      const current = !resumeId || d.base_resume_version === resumeId;
      if (d.status === "approved" && d.pdf_ref && current && !map[d.job_id]) {
        map[d.job_id] = d; // getCvDrafts is newest-first → keep the latest per job
      }
    }
    return map;
  }, [cvDrafts, resumeId]);

  // After a résumé (re)upload, propose a fresh canonical config and open the review dialog.
  const onResumeUploaded = useCallback(async () => {
    loadCvContext(); // new upload → refresh résumé id so stale tailored CVs drop off the rows
    try {
      const { proposal, has_resume } = await deriveSearchConfig();
      if (has_resume) openSettings("review", proposal);
    } catch {
      /* propose is best-effort; the user can still edit config manually */
    }
  }, [openSettings, loadCvContext]);

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Single-flight job reload — PipelineLive calls this live during a run.
  const loadJobs = useCallback(async () => {
    if (loadingJobs.current) return;
    loadingJobs.current = true;
    try {
      setJobs(await getDashboardJobs());
    } catch (e) {
      setError(String(e));
    } finally {
      loadingJobs.current = false;
    }
  }, []);

  // A Concierge action can change jobs AND CV drafts (tailor/edit/qualify/delete), so refresh
  // the table, the per-row CV context, and signal the Tailored-CVs panel to reload — no manual
  // page refresh.
  const onConciergeChanged = useCallback(() => {
    loadJobs();
    loadCvContext();
    setCvRefresh((k) => k + 1);
  }, [loadJobs, loadCvContext]);

  useEffect(() => {
    (async () => {
      if (!getToken()) return setAuthed(false);
      try {
        const me = await getMe();
        setEmail(me.email);
        setAuthed(true);
      } catch {
        setToken(null);
        setAuthed(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (authed) {
      loadJobs();
      loadConfig();
      loadQualifierConfig();
    }
  }, [authed, loadJobs, loadConfig, loadQualifierConfig]);

  // Poll the CV context so tailored CVs attach to rows as the Prepare/Export tasks finish.
  useEffect(() => {
    if (!authed) return;
    loadCvContext();
    const t = setInterval(loadCvContext, 8000);
    return () => clearInterval(t);
  }, [authed, loadCvContext]);

  const onAuthed = async () => {
    try {
      const me = await getMe();
      setEmail(me.email);
    } catch {
      /* ignore */
    }
    setAuthed(true);
  };

  const logout = () => {
    setToken(null);
    setAuthed(false);
  };

  const sources = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.connector).filter(Boolean))).sort() as string[],
    [jobs],
  );
  // Employment/seniority options are whatever Scout actually classified across the pool
  // (so the dropdowns never offer a value that would match nothing).
  const employmentTypes = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.employment_type).filter(Boolean))).sort() as string[],
    [jobs],
  );
  const seniorities = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.seniority).filter(Boolean))).sort() as string[],
    [jobs],
  );

  const visible = useMemo(() => {
    const filtered = jobs.filter((j) => {
      const d = j.qualification?.decision ?? "unscored";
      if (decision === "qualified" && d !== "qualified") return false;
      if (decision === "disqualified" && d !== "disqualified") return false;
      if (decision === "unscored" && j.qualification) return false;
      if (source !== "all" && j.connector !== source) return false;
      if (employment !== "all" && j.employment_type !== employment) return false;
      if (seniority !== "all" && j.seniority !== seniority) return false;
      return true;
    });
    return filtered.sort(
      (a, b) => (b.qualification?.score ?? -1) - (a.qualification?.score ?? -1),
    );
  }, [jobs, decision, source, employment, seniority]);

  const qualifiedCount = jobs.filter((j) => j.qualification?.decision === "qualified").length;

  if (authed === null) {
    return (
      <main className="grid min-h-screen place-items-center bg-white">
        <div className="flex flex-col items-center gap-3">
          <span className="h-8 w-8 animate-spin rounded-full border-2 border-slate-200 border-t-indigo-600" />
          <p className="text-sm text-slate-500">Loading…</p>
        </div>
      </main>
    );
  }
  if (!authed) return <LoginGate onAuthed={onAuthed} />;

  const current = NAV.find((n) => n.key === view) ?? NAV[0];
  const selectClass =
    "cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-700 shadow-card transition hover:border-slate-300 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100";

  return (
    <div className="min-h-screen bg-slate-50/60 md:flex">
      <aside className="z-20 shrink-0 border-b border-slate-200 bg-white md:sticky md:top-0 md:flex md:h-screen md:w-64 md:flex-col md:border-b-0 md:border-r">
        <div className="flex items-center gap-2.5 px-5 pb-3 pt-5">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-600 text-sm font-bold text-white shadow-sm">
            JP
          </span>
          <div>
            <h1 className="text-[15px] font-semibold leading-tight tracking-tight text-slate-900">
              JobPilot
            </h1>
            <p className="text-[11px] leading-tight text-slate-400">scout → qualify → tailor</p>
          </div>
        </div>

        <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:overflow-visible">
          {NAV.map((n) => {
            const active = view === n.key;
            return (
              <button
                key={n.key}
                onClick={() => setView(n.key)}
                aria-current={active ? "page" : undefined}
                className={`group relative flex shrink-0 items-start gap-2.5 rounded-lg px-3 py-2 text-left transition-colors duration-150 ${
                  active
                    ? "bg-indigo-50 text-indigo-700"
                    : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                }`}
              >
                {active && (
                  <span className="absolute inset-y-1.5 left-0 hidden w-0.5 rounded-full bg-indigo-600 md:block" />
                )}
                <NavIcon
                  name={n.key}
                  className={`mt-0.5 ${active ? "text-indigo-600" : "text-slate-400 group-hover:text-slate-500"}`}
                />
                <span className="min-w-0">
                  <span className="block whitespace-nowrap text-sm font-medium">{n.label}</span>
                  <span
                    className={`hidden text-xs md:block ${active ? "text-indigo-500/80" : "text-slate-400"}`}
                  >
                    {n.hint}
                  </span>
                </span>
              </button>
            );
          })}
        </nav>

        {/* Jobs are selected on the Dashboard screen but both other screens act on that
            selection — so keep the count visible wherever you are. */}
        <div className="mt-auto hidden px-3 pb-4 md:block">
          <div
            className={`rounded-lg border px-3 py-2 transition-colors ${
              selected.size > 0
                ? "border-indigo-200 bg-indigo-50/70"
                : "border-dashed border-slate-200 bg-slate-50/50"
            }`}
          >
            <p
              className={`text-xs font-medium ${
                selected.size > 0 ? "text-indigo-700" : "text-slate-500"
              }`}
            >
              {selected.size} job{selected.size === 1 ? "" : "s"} selected
            </p>
            {selected.size === 0 ? (
              <p className="mt-0.5 text-[11px] leading-snug text-slate-400">
                Pick some on Dashboard to act on them.
              </p>
            ) : (
              <button
                onClick={() => setSelected(new Set())}
                className="mt-1 text-[11px] font-medium text-indigo-600 underline-offset-2 hover:underline"
              >
                Clear selection
              </button>
            )}
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1">
        <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/85 backdrop-blur-md">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-6 py-4">
            <div className="min-w-0">
              <h1 className="text-xl font-semibold tracking-tight text-slate-900">{current.label}</h1>
              <p className="text-sm text-slate-500">
                {view === "dashboard"
                  ? "Autonomous scout → qualify — fit guidance, not a verdict."
                  : current.hint}
              </p>
            </div>
            <div className="ml-auto flex items-center gap-3">
              <span className="hidden text-sm text-slate-500 sm:inline">{email}</span>
              <button
                onClick={logout}
                className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-card transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 active:scale-[0.98]"
              >
                Sign out
              </button>
            </div>
          </div>
        </header>

        <div className="mx-auto max-w-6xl px-6 py-6">
          <Screen active={view === "dashboard"}>
            <div className="mb-5 flex flex-wrap gap-2.5">
              <Stat label="Jobs" value={jobs.length} />
              <Stat label="Qualified" value={qualifiedCount} tone="emerald" />
              <Stat label="Selected" value={selected.size} tone="indigo" />
              <Stat label="Tailored CVs" value={Object.keys(cvDraftByJobId).length} />
            </div>

            <PipelineLive onJobsChanged={loadJobs} />

            {/* The two conversational agents, side by side: the Concierge acts; Ask answers
                (read-only). Distinct privilege sets — Ask has no action tools. */}
            <div className="mt-5 grid gap-4 lg:grid-cols-2">
              <Chat onChanged={onConciergeChanged} selectedJobIds={[...selected]} />
              <Ask />
            </div>
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <Gates onChanged={loadJobs} />
              <CostPanel />
            </div>
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <ResumeUpload onUploaded={onResumeUploaded} />
              <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card transition-shadow hover:shadow-card-hover">
                <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-900">Search settings</h2>
                    <p className="text-xs text-slate-500">What Scout searches for.</p>
                  </div>
                  <button
                    onClick={() => openSettings("edit")}
                    className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
                  >
                    Edit
                  </button>
                </div>
                <dl className="space-y-2 px-4 py-3 text-xs">
                  <div className="flex gap-2">
                    <dt className="w-20 shrink-0 font-medium text-slate-400">Roles</dt>
                    <dd className="text-slate-700">{config?.roles?.join(", ") || "—"}</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-20 shrink-0 font-medium text-slate-400">Locations</dt>
                    <dd className="text-slate-700">{config?.locations?.join(", ") || "—"}</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-20 shrink-0 font-medium text-slate-400">Keywords</dt>
                    <dd className="text-slate-700">{config?.keywords?.join(", ") || "—"}</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-20 shrink-0 font-medium text-slate-400">Sources</dt>
                    <dd className="text-slate-700">{config?.sources?.join(", ") || "—"}</dd>
                  </div>
                </dl>
              </section>
            </div>

            <div className="mt-4">
              <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card transition-shadow hover:shadow-card-hover">
                <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-900">Scoring &amp; filters</h2>
                    <p className="text-xs text-slate-500">
                      How Qualify scores jobs — and the must-have hard filter.
                    </p>
                  </div>
                  <button
                    onClick={() => setQualifierOpen(true)}
                    className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
                  >
                    Edit
                  </button>
                </div>
                <dl className="grid gap-2 px-4 py-3 text-xs sm:grid-cols-2">
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 font-medium text-slate-400">Must-have</dt>
                    <dd className="text-slate-700">{qualifierConfig?.must_have?.join(", ") || "—"}</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 font-medium text-slate-400">Threshold</dt>
                    <dd className="text-slate-700">{qualifierConfig?.threshold ?? "—"}</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 font-medium text-slate-400">Nice-to-have</dt>
                    <dd className="truncate text-slate-700">
                      {qualifierConfig?.nice_to_have?.join(", ") || "—"}
                    </dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 font-medium text-slate-400">Weights</dt>
                    <dd className="text-slate-700">
                      {qualifierConfig?.weights_json
                        ? `skills ${qualifierConfig.weights_json.skills ?? "—"} · exp ${
                            qualifierConfig.weights_json.experience ?? "—"
                          } · roles ${qualifierConfig.weights_json.roles_match ?? "—"}`
                        : "—"}
                    </dd>
                  </div>
                </dl>
              </section>
            </div>

            {error && (
              <p className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {error}
              </p>
            )}

            <div className="mb-3 mt-6 flex flex-wrap items-center gap-3">
              <div className="flex rounded-lg border border-slate-200 bg-white p-0.5 text-sm shadow-card">
                {(["all", "qualified", "disqualified", "unscored"] as Decision[]).map((d) => (
                  <button
                    key={d}
                    onClick={() => setDecision(d)}
                    className={`rounded-md px-3 py-1 font-medium capitalize transition-colors ${
                      decision === d
                        ? "bg-slate-900 text-white shadow-sm"
                        : "text-slate-500 hover:bg-slate-50 hover:text-slate-900"
                    }`}
                  >
                    {d}
                  </button>
                ))}
              </div>
              <select
                value={source}
                onChange={(e) => setSource(e.target.value)}
                className={selectClass}
              >
                <option value="all">All sources</option>
                {sources.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              {employmentTypes.length > 0 && (
                <select
                  value={employment}
                  onChange={(e) => setEmployment(e.target.value)}
                  className={`${selectClass} capitalize`}
                >
                  <option value="all">All types</option>
                  {employmentTypes.map((t) => (
                    <option key={t} value={t}>
                      {t.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
              )}
              {seniorities.length > 0 && (
                <select
                  value={seniority}
                  onChange={(e) => setSeniority(e.target.value)}
                  className={`${selectClass} capitalize`}
                >
                  <option value="all">All levels</option>
                  {seniorities.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              )}
              <span className="ml-auto text-sm text-slate-500">
                <span className="font-medium text-slate-900">{visible.length}</span> of {jobs.length} jobs ·{" "}
                <span className="font-medium text-emerald-600">{qualifiedCount}</span> qualified · sorted by
                score
              </span>
            </div>

            <JobsTable
              jobs={visible}
              selected={selected}
              onToggleSelect={toggleSelect}
              cvDraftByJobId={cvDraftByJobId}
            />
          </Screen>

          <Screen active={view === "suggest"}>
            <Suggestions
              selectedJobIds={[...selected]}
              qualifiedCount={qualifiedCount}
              jobTitleById={(id) => jobs.find((j) => j.id === id)?.title ?? "Job"}
            />
          </Screen>

          {/* Phase 6.6 — its own screen now, but the same place in the flow: propose
              aggressively, vouch item by item, then tailor. */}
          <Screen active={view === "gap-close"}>
            <GapCloseProposals
              selectedJobIds={[...selected]}
              jobTitleById={(id) => jobs.find((j) => j.id === id)?.title ?? "Job"}
            />
          </Screen>

          <Screen active={view === "tailored"}>
            <TailoredCVs
              selectedJobIds={[...selected]}
              // The whole job: the edit and review panels show its description beside the CV.
              jobById={(id) => jobs.find((j) => j.id === id) ?? null}
              refreshSignal={cvRefresh}
            />
          </Screen>

          {/* Apply agent, stage 1: the pre-configured facts and banked answers an apply
              agent is permitted to submit. No browser automation exists yet — this screen
              is the store those later stages will read from. */}
          <Screen active={view === "apply-profile"}>
            <ApplicationProfileEditor />
          </Screen>

          {settings.open && (
            <SearchSettings
              key={settings.seq}
              mode={settings.mode}
              initial={config}
              proposal={settings.proposal}
              onClose={() => setSettings((s) => ({ ...s, open: false, proposal: null }))}
              onSaved={onConfigSaved}
            />
          )}

          {qualifierOpen && (
            <QualifierSettings
              initial={qualifierConfig}
              onClose={() => setQualifierOpen(false)}
              onSaved={(cfg) => {
                setQualifierConfig(cfg);
                setQualifierOpen(false);
                loadJobs(); // a "Save & re-qualify" rescored the pool → refresh the table
              }}
            />
          )}
        </div>
      </main>
    </div>
  );
}
