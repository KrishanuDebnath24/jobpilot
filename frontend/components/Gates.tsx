"use client";

import { useCallback, useEffect, useState } from "react";
import type { ApplyPreview, PendingGate, ResumeVersion } from "@/lib/types";
import {
  applyResume,
  approveGate,
  downloadResumeVersion,
  getGates,
  getPipelineStatus,
  getResumeVersions,
  prepareApply,
  skipGate,
  updateGate,
} from "@/lib/api";

const GATE_LABELS: Record<string, string> = {
  after_scout: "Pause after Scout",
  after_qualify: "Pause after Qualify",
  after_analyze: "Pause after Analyze",
  before_apply: "Approve before applying to resume",
};

/** Human-in-the-loop gates + apply-to-resume. Pending gates surface here (and in the
 *  Concierge chat); approve/skip resumes the paused pipeline. */
export default function Gates({ onChanged }: { onChanged?: () => void }) {
  const [gates, setGates] = useState<Record<string, boolean>>({});
  const [keys, setKeys] = useState<string[]>([]);
  const [pending, setPending] = useState<PendingGate>(null);
  const [versions, setVersions] = useState<ResumeVersion[]>([]);
  const [preview, setPreview] = useState<ApplyPreview | null>(null);
  const [confirmSkills, setConfirmSkills] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      const g = await getGates();
      setGates(g.gates);
      setKeys(g.keys ?? Object.keys(g.gates));
      setVersions(await getResumeVersions());
    } catch {
      /* ignore */
    }
  }, []);

  const pollGate = useCallback(async () => {
    try {
      setPending((await getPipelineStatus()).pending_gate);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    refresh();
    pollGate();
    const t = setInterval(pollGate, 4000);
    return () => clearInterval(t);
  }, [refresh, pollGate]);

  const toggle = async (key: string) => {
    const next = !gates[key];
    setGates((g) => ({ ...g, [key]: next }));
    await updateGate(key, next);
  };

  const resolve = async (approve: boolean) => {
    if (!pending) return;
    await (approve ? approveGate(pending.gate_id) : skipGate(pending.gate_id));
    setPending(null);
    setTimeout(pollGate, 800);
    onChanged?.();
  };

  const doPrepare = async () => {
    setMsg("Assembling preview…");
    try {
      const p = await prepareApply([...confirmSkills]);
      setPreview(p);
      setMsg("");
    } catch (e) {
      setMsg(String(e));
    }
  };

  const doApply = async () => {
    setMsg("Applying…");
    try {
      const r = await applyResume([...confirmSkills], "Tailored from accepted suggestions");
      setMsg(r.applied ? "New resume version created." : "Nothing to apply.");
      setPreview(null);
      setConfirmSkills(new Set());
      await refresh();
      onChanged?.();
    } catch (e) {
      setMsg(String(e));
    }
  };

  return (
    <section className="space-y-4 rounded-2xl border border-neutral-800 bg-neutral-900/30 p-4">
      <h2 className="text-sm font-semibold text-neutral-200">Review gates &amp; resume</h2>

      {pending && (
        <div className="flex items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2">
          <span className="text-sm text-amber-200">
            Pipeline paused for review: <b>{GATE_LABELS[pending.gate_key] ?? pending.gate_key}</b>
          </span>
          <div className="ml-auto flex gap-2">
            <button
              onClick={() => resolve(true)}
              className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300 hover:bg-emerald-500/20"
            >
              Approve
            </button>
            <button
              onClick={() => resolve(false)}
              className="rounded-md border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400 hover:border-neutral-500"
            >
              Skip
            </button>
          </div>
        </div>
      )}

      <div className="space-y-1.5">
        {keys.map((key) => (
          <label key={key} className="flex items-center gap-2 text-sm text-neutral-300">
            <input type="checkbox" checked={!!gates[key]} onChange={() => toggle(key)} />
            {GATE_LABELS[key] ?? key}
          </label>
        ))}
      </div>

      <div className="border-t border-neutral-800 pt-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={doPrepare}
            className="rounded-lg border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 hover:border-neutral-500"
          >
            Preview resume from accepted suggestions
          </button>
          {msg && <span className="text-xs text-neutral-400">{msg}</span>}
        </div>

        {preview && (
          <div className="mt-3 space-y-3 rounded-lg border border-neutral-800 bg-neutral-950/40 p-3 text-sm">
            <p className="text-xs text-neutral-500">
              {preview.accepted_count} accepted suggestion(s) · {preview.applied_rewrites.length}{" "}
              bullet rewrite(s) will apply.
            </p>
            {preview.withheld_skills.length > 0 && (
              <div>
                <p className="mb-1 text-xs uppercase tracking-wide text-neutral-500">
                  Confirm skills you actually hold (only these get added)
                </p>
                <div className="flex flex-wrap gap-2">
                  {preview.withheld_skills.map((s) => (
                    <label
                      key={s}
                      className="flex items-center gap-1.5 rounded-md border border-neutral-800 px-2 py-1 text-xs text-neutral-300"
                    >
                      <input
                        type="checkbox"
                        checked={confirmSkills.has(s)}
                        onChange={() =>
                          setConfirmSkills((prev) => {
                            const n = new Set(prev);
                            n.has(s) ? n.delete(s) : n.add(s);
                            return n;
                          })
                        }
                      />
                      {s}
                    </label>
                  ))}
                </div>
                <button
                  onClick={doPrepare}
                  className="mt-2 text-xs text-emerald-300 hover:underline"
                >
                  Re-preview with confirmed skills
                </button>
              </div>
            )}
            {preview.project_guidance.length > 0 && (
              <p className="text-xs text-neutral-500">
                {preview.project_guidance.length} project idea(s) surfaced as guidance (not inserted).
              </p>
            )}
            <button
              onClick={doApply}
              className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-200 hover:bg-emerald-500/20"
            >
              Apply as new version
            </button>
          </div>
        )}

        {versions.length > 0 && (
          <ul className="mt-3 space-y-1">
            {versions.map((v) => (
              <li key={v.id} className="flex items-center gap-2 text-xs text-neutral-400">
                <span className="rounded border border-neutral-800 px-1.5 py-0.5 font-mono">
                  {v.source}
                </span>
                <span className="truncate">{v.label ?? "resume"}</span>
                <button
                  onClick={() => downloadResumeVersion(v.id)}
                  className="ml-auto text-emerald-300 hover:underline"
                >
                  download
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
