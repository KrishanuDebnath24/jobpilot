"use client";

import { useCallback, useEffect, useState } from "react";
import type { ApplyPreview, ResumeVersion } from "@/lib/types";
import {
  applyResume,
  downloadResumeVersion,
  downloadResumeVersionFile,
  getResumeVersions,
  prepareApply,
} from "@/lib/api";
import ResumeUpload from "./ResumeUpload";

/** Everything about your CV in one dialog, opened from the header button.
 *
 *  Top half is the résumé on file + the upload control. Below it sits the assemble-from-
 *  accepted-suggestions flow (previously in the gates panel): preview what the accepted
 *  suggestions would change, apply it as a new version, then download that version. The
 *  loop only makes sense next to the upload control — a suggested version becomes the
 *  working CV only when you re-upload the file you downloaded. */
export default function CvPanel({
  onClose,
  onUploaded,
  onChanged,
}: {
  onClose: () => void;
  onUploaded?: () => void;
  onChanged?: () => void;
}) {
  const [versions, setVersions] = useState<ResumeVersion[]>([]);
  const [preview, setPreview] = useState<ApplyPreview | null>(null);
  const [confirmSkills, setConfirmSkills] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<string>("");

  const loadVersions = useCallback(async () => {
    try {
      setVersions(await getResumeVersions());
    } catch {
      /* a versions hiccup shouldn't wedge the dialog */
    }
  }, []);

  useEffect(() => {
    loadVersions();
  }, [loadVersions]);

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
      setMsg(
        r.applied
          ? `New résumé version created — download it as PDF/DOCX below.${
              r.pruned_versions ? ` Replaced ${r.pruned_versions} earlier suggested version(s).` : ""
            }`
          : "Nothing to apply.",
      );
      setPreview(null);
      setConfirmSkills(new Set());
      await loadVersions();
      onChanged?.();
    } catch (e) {
      setMsg(String(e));
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/25 px-4 py-10 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="screen-enter w-full max-w-lg space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl shadow-slate-900/10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight text-slate-900">Your CV / résumé</h2>
          <p className="text-sm leading-relaxed text-slate-500">
            Attach a PDF or DOCX. It seeds your qualifier and powers tailoring — parsed on upload,
            stored only with your consent.
          </p>
        </div>

        <ResumeUpload bare onUploaded={onUploaded} />

        {/* The assemble/export loop, under the résumé it builds from. */}
        <div className="space-y-3 border-t border-slate-100 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={doPrepare}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
            >
              Preview resume from accepted suggestions
            </button>
            {msg && <span className="text-xs text-slate-500">{msg}</span>}
          </div>

          {preview && (
            <div className="pop-in space-y-3 rounded-lg border border-slate-200 bg-slate-50/70 p-3 text-sm">
              <p className="text-xs text-slate-500">
                {preview.accepted_count} accepted suggestion(s) · {preview.applied_rewrites.length}{" "}
                bullet rewrite(s) will apply.
              </p>
              {preview.withheld_skills.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
                    Confirm skills you actually hold (only these get added)
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {preview.withheld_skills.map((s) => (
                      <label
                        key={s}
                        className={`flex cursor-pointer items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition ${
                          confirmSkills.has(s)
                            ? "border-emerald-300 bg-emerald-50 text-emerald-800"
                            : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                        }`}
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
                          className="h-3.5 w-3.5 cursor-pointer rounded border-slate-300 accent-emerald-600"
                        />
                        {s}
                      </label>
                    ))}
                  </div>
                  <button
                    onClick={doPrepare}
                    className="mt-2 text-xs font-medium text-emerald-700 hover:underline"
                  >
                    Re-preview with confirmed skills
                  </button>
                </div>
              )}
              {preview.project_guidance.length > 0 && (
                <p className="text-xs text-slate-500">
                  {preview.project_guidance.length} project idea(s) surfaced as guidance (not
                  inserted).
                </p>
              )}
              <button
                onClick={doApply}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700 active:scale-[0.98]"
              >
                Apply as new version
              </button>
              <p className="text-xs leading-relaxed text-slate-500">
                Saves a new résumé version you can download as PDF/DOCX below, and replaces any
                earlier suggested version (each one is rebuilt from your upload, so the older one
                holds nothing this doesn’t). Your uploaded résumé is never touched or removed —
                re-upload the file to attach it to your jobs.
              </p>
            </div>
          )}

          {versions.length > 0 && (
            <>
              {/* The one thing that surprises everyone: an applied version is an EXPORT, not a
                  promotion. Scoring and tailoring keep using the last file you uploaded, so the
                  round trip has to be spelled out where the download buttons are. */}
              <p className="rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-relaxed text-sky-900">
                <span className="font-semibold">To attach a suggested résumé to your jobs:</span>{" "}
                download it as PDF or DOCX below, then re-upload that file above. Uploading is what
                makes a résumé the working one — the CV attached to every job stays the last file
                you uploaded manually until you do. Downloading alone changes nothing.
              </p>
              <ul className="space-y-1">
                {versions.map((v) => (
                  <li
                    key={v.id}
                    className="flex flex-wrap items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-slate-500 transition hover:bg-slate-50"
                  >
                    <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-slate-500">
                      {v.source}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-slate-700">
                      {v.label ?? "resume"}
                    </span>
                    {/* Same A4/ATS layout as the tailored CVs — rendered by the same Tailor. */}
                    <button
                      onClick={() => downloadResumeVersionFile(v.id, "pdf")}
                      className="rounded-md border border-slate-200 bg-white px-2 py-0.5 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50"
                    >
                      PDF
                    </button>
                    <button
                      onClick={() => downloadResumeVersionFile(v.id, "docx")}
                      className="rounded-md border border-slate-200 bg-white px-2 py-0.5 font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50"
                    >
                      DOCX
                    </button>
                    <button
                      onClick={() => downloadResumeVersion(v.id)}
                      className="text-slate-400 hover:text-slate-600 hover:underline"
                      title="Plain-text rendering"
                    >
                      .txt
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        <div className="flex justify-end border-t border-slate-100 pt-4">
          <button
            onClick={onClose}
            className="rounded-lg border border-slate-200 bg-white px-3.5 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98]"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
