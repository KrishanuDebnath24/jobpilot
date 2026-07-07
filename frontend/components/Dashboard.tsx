"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Job } from "@/lib/types";
import { getDashboardJobs, getMe, getToken, setToken } from "@/lib/api";
import LoginGate from "./LoginGate";
import PipelineLive from "./PipelineLive";
import JobsTable from "./JobsTable";
import Suggestions from "./Suggestions";

type Decision = "all" | "qualified" | "disqualified" | "unscored";

export default function Dashboard() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string>("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [decision, setDecision] = useState<Decision>("all");
  const [source, setSource] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const loadingJobs = useRef(false);

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
    if (authed) loadJobs();
  }, [authed, loadJobs]);

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

  const visible = useMemo(() => {
    const filtered = jobs.filter((j) => {
      const d = j.qualification?.decision ?? "unscored";
      if (decision === "qualified" && d !== "qualified") return false;
      if (decision === "disqualified" && d !== "disqualified") return false;
      if (decision === "unscored" && j.qualification) return false;
      if (source !== "all" && j.connector !== source) return false;
      return true;
    });
    return filtered.sort(
      (a, b) => (b.qualification?.score ?? -1) - (a.qualification?.score ?? -1),
    );
  }, [jobs, decision, source]);

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
        <span className="ml-auto text-sm text-neutral-500">
          {visible.length} of {jobs.length} jobs · {qualifiedCount} qualified · sorted by score
        </span>
      </div>

      <JobsTable jobs={visible} selected={selected} onToggleSelect={toggleSelect} />

      <Suggestions
        selectedJobIds={[...selected]}
        qualifiedCount={qualifiedCount}
        jobTitleById={(id) => jobs.find((j) => j.id === id)?.title ?? "Job"}
      />
    </main>
  );
}
