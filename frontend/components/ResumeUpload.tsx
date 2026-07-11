"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { StructuredResume } from "@/lib/types";
import { ApiError, RESUME_ACCEPT, RESUME_MAX_BYTES, getMyResume, uploadResume } from "@/lib/api";

/** Attach a CV/résumé (PDF or DOCX). Deterministic parse on the backend seeds the
 *  qualifier config and embeds the resume. Storage requires explicit consent, so the
 *  checkbox is mandatory — the upload button stays disabled until it's ticked. */
export default function ResumeUpload({ onUploaded }: { onUploaded?: () => void }) {
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

  return (
    <section className="rounded-2xl border border-neutral-800 bg-neutral-900/30">
      <div className="border-b border-neutral-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-neutral-200">Your CV / résumé</h2>
        <p className="text-xs text-neutral-500">
          Attach a PDF or DOCX. It seeds your qualifier and powers tailoring — parsed on
          upload, stored only with your consent.
        </p>
      </div>

      <div className="space-y-3 px-4 py-3">
        {current && (
          <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-xs text-neutral-400">
            <span className="text-neutral-300">On file:</span>{" "}
            {name ? <span className="text-neutral-200">{name}</span> : "resume"}
            {skills.length > 0 && (
              <span>
                {" "}
                · {skills.length} skill{skills.length === 1 ? "" : "s"}
                <span className="ml-1 text-neutral-500">({skills.slice(0, 6).join(", ")}
                {skills.length > 6 ? "…" : ""})</span>
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
            className="block w-full text-sm text-neutral-400 file:mr-3 file:cursor-pointer file:rounded-lg file:border file:border-neutral-700 file:bg-neutral-800 file:px-3 file:py-1.5 file:text-sm file:text-neutral-200 hover:file:border-neutral-600 disabled:opacity-50"
          />
        </div>

        <label className="flex cursor-pointer items-start gap-2 text-xs text-neutral-400">
          <input
            type="checkbox"
            checked={consent}
            disabled={busy}
            onChange={(e) => setConsent(e.target.checked)}
            className="mt-0.5 accent-sky-500"
          />
          <span>I consent to JobPilot storing and processing this resume to match and tailor for jobs.</span>
        </label>

        <div className="flex items-center gap-3">
          <button
            onClick={submit}
            disabled={busy || !file || !consent}
            className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-40"
          >
            {busy ? "Uploading…" : current ? "Replace CV" : "Attach CV"}
          </button>
          {file && !busy && <span className="truncate text-xs text-neutral-500">{file.name}</span>}
          {justUploaded && !error && <span className="text-xs text-emerald-300">Uploaded ✓</span>}
        </div>

        {error && <p className="text-xs text-rose-300">{error}</p>}
      </div>
    </section>
  );
}
