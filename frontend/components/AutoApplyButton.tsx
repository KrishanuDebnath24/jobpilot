"use client";

import { useEffect, useState } from "react";
import type { Job } from "@/lib/types";

/**
 * Whether the JobPilot browser extension is installed.
 *
 * A page cannot interrogate an extension, so the extension announces itself instead: its
 * dashboard content script stamps a version onto `<html>`. That script runs at
 * `document_idle`, which is usually *after* React mounts, so a single read on mount would
 * report "not installed" on every fresh load. Watching the attribute is what makes the
 * button settle into the right state on its own rather than needing a refresh.
 */
function useExtension(): string | null {
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    const read = () => setVersion(document.documentElement.dataset.jobpilotAutoapply ?? null);
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-jobpilot-autoapply"],
    });
    return () => observer.disconnect();
  }, []);

  return version;
}

/** The page the extension should open: the resolved form when stage 2 found one. */
function applyTarget(job: Job): string | null {
  return job.apply_url || job.url || null;
}

/**
 * Carry the job id across to the form page.
 *
 * A fragment rather than a query parameter, because a fragment is never sent to the
 * employer's server — the id is JobPilot's business and has no reason to appear in
 * someone else's access log. The extension reads it and strips it immediately.
 */
function withJobId(url: string, jobId: string): string {
  const separator = url.includes("#") ? "&" : "#";
  return `${url}${separator}jobpilot=${jobId}`;
}

export default function AutoApplyButton({
  job,
  compact = false,
}: {
  job: Job;
  compact?: boolean;
}) {
  const version = useExtension();
  const target = applyTarget(job);
  // "aggregator" is a resolved answer meaning the chain never left a job board, so there
  // is no form behind this listing to fill. Distinct from unresolved, which is worth a try.
  const noForm = job.apply_ats === "aggregator";
  const disabled = !target || noForm;

  const open = () => {
    if (!target) return;
    window.open(withJobId(target, job.id), "_blank", "noopener,noreferrer");
  };

  const title = !target
    ? "This listing has no link to open."
    : noForm
      ? "This listing never resolved to an application form — there is nothing behind it to fill."
      : version
        ? "Opens the application form and fills what you've already told us. Nothing is submitted."
        : "Opens the application form. Install the JobPilot extension to have it filled in for you.";

  if (compact) {
    return (
      <button
        onClick={(e) => {
          e.stopPropagation();
          open();
        }}
        disabled={disabled}
        title={title}
        className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium transition ${
          disabled
            ? "cursor-not-allowed border-slate-200 bg-slate-50 text-slate-300"
            : "border-emerald-200 bg-emerald-50 text-emerald-700 hover:border-emerald-300 hover:bg-emerald-100 active:scale-[0.98]"
        }`}
      >
        {version ? "Auto-apply" : "Apply"} ↗
      </button>
    );
  }

  return (
    <div className="space-y-1.5">
      <button
        onClick={open}
        disabled={disabled}
        title={title}
        className={`inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-semibold transition ${
          disabled
            ? "cursor-not-allowed bg-slate-100 text-slate-400"
            : "bg-emerald-600 text-white shadow-card hover:bg-emerald-700 active:scale-[0.98]"
        }`}
      >
        {version ? "Auto-apply" : "Open application"} ↗
      </button>
      <p className="max-w-md text-xs leading-relaxed text-slate-500">
        {noForm ? (
          <>
            This listing resolved to a job board rather than an application form, so
            there&apos;s no form to fill.
          </>
        ) : version ? (
          <>
            Opens the form and fills every field that traces to your profile or an answer
            you&apos;ve already given, marking each one with where it came from. Anything new
            is left for you — and banked when you submit, so the next form asks less.{" "}
            <span className="font-medium text-slate-600">You press Submit, never us.</span>
          </>
        ) : (
          <>
            Opens the form in a new tab. Install the JobPilot browser extension
            (<code className="rounded bg-slate-100 px-1 py-0.5 text-[11px]">extension/</code> in
            the repo) and it will fill it in for you.
          </>
        )}
      </p>
    </div>
  );
}
