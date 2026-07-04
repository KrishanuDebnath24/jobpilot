"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { Job, PipelineRun } from "@/lib/types";
import {
  getDashboardJobs,
  getMe,
  getQualifyRuns,
  getScoutRuns,
  getToken,
  runQualify,
  runScout,
  setToken,
} from "@/lib/api";
import LoginGate from "./LoginGate";
import RunHeader from "./RunHeader";
import JobsTable from "./JobsTable";

type Decision = "all" | "qualified" | "disqualified" | "unscored";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitForNewRun(
  fetchRuns: () => Promise<PipelineRun[]>,
  prevId: string | undefined,
  timeoutMs = 180_000,
): Promise<PipelineRun[]> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const runs = await fetchRuns();
    const latest = runs[0];
    if (latest && latest.id !== prevId && ["completed", "failed"].includes(latest.status)) {
      return runs;
    }
    await sleep(3000);
  }
  return fetchRuns();
}

export default function Dashboard() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string>("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [scoutRuns, setScoutRuns] = useState<PipelineRun[]>([]);
  const [qualifyRuns, setQualifyRuns] = useState<PipelineRun[]>([]);
  const [scoutBusy, setScoutBusy] = useState(false);
  const [qualifyBusy, setQualifyBusy] = useState(false);
  const [decision, setDecision] = useState<Decision>("all");
  const [source, setSource] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    const [j, sr, qr] = await Promise.all([getDashboardJobs(), getScoutRuns(), getQualifyRuns()]);
    setJobs(j);
    setScoutRuns(sr);
    setQualifyRuns(qr);
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
    if (authed) loadAll().catch((e) => setError(String(e)));
  }, [authed, loadAll]);

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

  const triggerScout = async () => {
    setScoutBusy(true);
    setError(null);
    try {
      const prev = scoutRuns[0]?.id;
      await runScout();
      setScoutRuns(await waitForNewRun(getScoutRuns, prev));
      setJobs(await getDashboardJobs());
    } catch (e) {
      setError(String(e));
    } finally {
      setScoutBusy(false);
    }
  };

  const triggerQualify = async () => {
    setQualifyBusy(true);
    setError(null);
    try {
      const prev = qualifyRuns[0]?.id;
      await runQualify(true);
      setQualifyRuns(await waitForNewRun(getQualifyRuns, prev));
      setJobs(await getDashboardJobs());
    } catch (e) {
      setError(String(e));
    } finally {
      setQualifyBusy(false);
    }
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
          <p className="text-sm text-neutral-500">Qualifier dashboard — fit guidance, not a verdict.</p>
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

      <RunHeader
        scoutRun={scoutRuns[0] ?? null}
        qualifyRun={qualifyRuns[0] ?? null}
        scoutBusy={scoutBusy}
        qualifyBusy={qualifyBusy}
        onRunScout={triggerScout}
        onRunQualify={triggerQualify}
      />

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

      <JobsTable jobs={visible} />
    </main>
  );
}
