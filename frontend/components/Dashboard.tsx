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
import Chat from "./Chat";
import Ask from "./Ask";
import Gates from "./Gates";
import TailoredCVs from "./TailoredCVs";
import CostPanel from "./CostPanel";
import ResumeUpload from "./ResumeUpload";
import SearchSettings, { type SearchSettingsMode } from "./SearchSettings";
import QualifierSettings from "./QualifierSettings";

type Decision = "all" | "qualified" | "disqualified" | "unscored";

export default function Dashboard() {
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
    return <main className="grid min-h-screen place-items-center text-neutral-500">Loading…</main>;
  }
  if (!authed) return <LoginGate onAuthed={onAuthed} />;

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6 flex items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">JobPilot</h1>
          <p className="text-sm text-neutral-500">
            Autonomous scout → qualify — fit guidance, not a verdict.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3 text-sm text-neutral-400">
          <span>{email}</span>
          <button
            onClick={logout}
            className="rounded-lg border border-neutral-800 px-2.5 py-1 text-xs transition hover:border-neutral-600"
          >
            Sign out
          </button>
        </div>
      </header>

      <PipelineLive onJobsChanged={loadJobs} />

      {/* The two conversational agents, side by side: the Concierge acts; Ask answers
          (read-only). Distinct privilege sets — Ask has no action tools. */}
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Chat onChanged={loadJobs} selectedJobIds={[...selected]} />
        <Ask />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Gates onChanged={loadJobs} />
        <CostPanel />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <ResumeUpload onUploaded={onResumeUploaded} />
        <section className="rounded-2xl border border-neutral-800 bg-neutral-900/30">
          <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-2.5">
            <div>
              <h2 className="text-sm font-semibold text-neutral-200">Search settings</h2>
              <p className="text-xs text-neutral-500">What Scout searches for.</p>
            </div>
            <button
              onClick={() => openSettings("edit")}
              className="rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 transition hover:border-neutral-500"
            >
              Edit
            </button>
          </div>
          <dl className="space-y-1.5 px-4 py-3 text-xs">
            <div className="flex gap-2">
              <dt className="w-20 shrink-0 text-neutral-500">Roles</dt>
              <dd className="text-neutral-300">{config?.roles?.join(", ") || "—"}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-20 shrink-0 text-neutral-500">Locations</dt>
              <dd className="text-neutral-300">{config?.locations?.join(", ") || "—"}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-20 shrink-0 text-neutral-500">Keywords</dt>
              <dd className="text-neutral-300">{config?.keywords?.join(", ") || "—"}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-20 shrink-0 text-neutral-500">Sources</dt>
              <dd className="text-neutral-300">{config?.sources?.join(", ") || "—"}</dd>
            </div>
          </dl>
        </section>
      </div>

      <div className="mt-4">
        <section className="rounded-2xl border border-neutral-800 bg-neutral-900/30">
          <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-2.5">
            <div>
              <h2 className="text-sm font-semibold text-neutral-200">Scoring &amp; filters</h2>
              <p className="text-xs text-neutral-500">
                How Qualify scores jobs — and the must-have hard filter.
              </p>
            </div>
            <button
              onClick={() => setQualifierOpen(true)}
              className="rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 transition hover:border-neutral-500"
            >
              Edit
            </button>
          </div>
          <dl className="grid gap-1.5 px-4 py-3 text-xs sm:grid-cols-2">
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-neutral-500">Must-have</dt>
              <dd className="text-neutral-300">{qualifierConfig?.must_have?.join(", ") || "—"}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-neutral-500">Threshold</dt>
              <dd className="text-neutral-300">{qualifierConfig?.threshold ?? "—"}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-neutral-500">Nice-to-have</dt>
              <dd className="truncate text-neutral-300">
                {qualifierConfig?.nice_to_have?.join(", ") || "—"}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-neutral-500">Weights</dt>
              <dd className="text-neutral-300">
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
        <p className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
          {error}
        </p>
      )}

      <div className="mb-3 mt-6 flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg border border-neutral-800 p-0.5 text-sm">
          {(["all", "qualified", "disqualified", "unscored"] as Decision[]).map((d) => (
            <button
              key={d}
              onClick={() => setDecision(d)}
              className={`rounded-md px-3 py-1 capitalize transition ${
                decision === d ? "bg-neutral-800 text-neutral-100" : "text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {d}
            </button>
          ))}
        </div>
        <select
          value={source}
          onChange={(e) => setSource(e.target.value)}
          className="rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-1.5 text-sm text-neutral-300 outline-none focus:border-neutral-600"
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
            className="rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-1.5 text-sm capitalize text-neutral-300 outline-none focus:border-neutral-600"
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
            className="rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-1.5 text-sm capitalize text-neutral-300 outline-none focus:border-neutral-600"
          >
            <option value="all">All levels</option>
            {seniorities.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
        <span className="ml-auto text-sm text-neutral-500">
          {visible.length} of {jobs.length} jobs · {qualifiedCount} qualified · sorted by score
        </span>
      </div>

      <JobsTable
        jobs={visible}
        selected={selected}
        onToggleSelect={toggleSelect}
        cvDraftByJobId={cvDraftByJobId}
      />

      <Suggestions
        selectedJobIds={[...selected]}
        qualifiedCount={qualifiedCount}
        jobTitleById={(id) => jobs.find((j) => j.id === id)?.title ?? "Job"}
      />

      <TailoredCVs
        selectedJobIds={[...selected]}
        jobTitleById={(id) => jobs.find((j) => j.id === id)?.title ?? "Job"}
      />

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
    </main>
  );
}
