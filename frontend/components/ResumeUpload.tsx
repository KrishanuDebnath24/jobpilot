"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { StructuredResume } from "@/lib/types";
import { ApiError, RESUME_ACCEPT, RESUME_MAX_BYTES, getMyResume, uploadResume } from "@/lib/api";

/** Attach a CV/résumé (PDF or DOCX). Deterministic parse on the backend seeds the
 *  qualifier config and embeds the resume. Storage requires explicit consent, so the
 *  checkbox is mandatory — the upload button stays disabled until it's ticked.
 *
 *  `bare` drops the card chrome and heading for callers that already supply their own
 *  (the CV popup titles the whole dialog "Your CV / résumé"). */
export default function ResumeUpload({
  onUploaded,
  bare = false,
}: {
  onUploaded?: () => void;
  bare?: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<StructuredResume | null>(null);
  const [justUploaded, setJustUploaded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadCurrent = useCallback(async () => {
    try {
      const { resume } = await getMyResume();
      setCurrent(resume?.structured ?? null);
    } catch {
      /* a missing resume isn't an error worth surfacing here */
    }
  }, []);

  useEffect(() => {
    loadCurrent();
  }, [loadCurrent]);

  const pick = (f: File | null) => {
    setError(null);
    setJustUploaded(false);
    if (f && f.size > RESUME_MAX_BYTES) {
      setError(`File is too large (max ${(RESUME_MAX_BYTES / 1024 / 1024).toFixed(0)} MB).`);
      setFile(null);
      return;
    }
    setFile(f);
  };

  const submit = useCallback(async () => {
    if (!file || !consent || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await uploadResume(file, consent);
      setCurrent(res.structured);
      setJustUploaded(true);
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [file, consent, busy, onUploaded]);

  const skills = current?.skills ?? [];
  const name = current?.contact?.name;

  const body = (
    <div className={bare ? "space-y-3" : "space-y-3 px-4 py-3"}>
      {current && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          <span className="font-semibold">On file:</span>{" "}
          {name ? <span className="font-medium">{name}</span> : "resume"}
          {skills.length > 0 && (
            <span>
              {" "}
              · {skills.length} skill{skills.length === 1 ? "" : "s"}
              <span className="ml-1 text-emerald-700/80">
                ({skills.slice(0, 6).join(", ")}
                {skills.length > 6 ? "…" : ""})
              </span>
            </span>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={inputRef}
          type="file"
          accept={RESUME_ACCEPT}
          disabled={busy}
          onChange={(e) => pick(e.target.files?.[0] ?? null)}
          className="block w-full cursor-pointer rounded-lg border border-dashed border-slate-300 bg-slate-50/50 p-2 text-sm text-slate-500 transition hover:border-slate-400 file:mr-3 file:cursor-pointer file:rounded-lg file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-white hover:file:bg-slate-800 disabled:opacity-50"
        />
      </div>

      <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-600">
        <input
          type="checkbox"
          checked={consent}
          disabled={busy}
          onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-slate-300 accent-sky-600"
        />
        <span>I consent to JobPilot storing and processing this resume to match and tailor for jobs.</span>
      </label>

      <div className="flex items-center gap-3">
        <button
          onClick={submit}
          disabled={busy || !file || !consent}
          className="flex items-center gap-2 rounded-lg bg-sky-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-sky-600"
        >
          {busy && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
          )}
          {busy ? "Uploading…" : current ? "Replace CV" : "Attach CV"}
        </button>
        {file && !busy && <span className="truncate text-xs text-slate-500">{file.name}</span>}
        {justUploaded && !error && (
          <span className="text-xs font-medium text-emerald-600">Uploaded ✓</span>
        )}
      </div>

      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}
    </div>
  );

  if (bare) return body;

  return (
    <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card transition-shadow hover:shadow-card-hover">
      <div className="border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">Your CV / résumé</h2>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Attach a PDF or DOCX. It seeds your qualifier and powers tailoring — parsed on
          upload, stored only with your consent.
        </p>
      </div>
      {body}
    </section>
  );
}
