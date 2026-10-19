/**
 * Reading the application log the way the dashboard needs it: per employer, not per job.
 *
 * The question this exists to answer is "have I already applied to this company, and did
 * I send them a different CV last time?" — which is only answerable once applications are
 * grouped by employer and the CVs behind them are compared.
 */
import type { Application, Job } from "./types";

/**
 * One stable key per employer.
 *
 * Mirrors `apply_fill.employer_scope` on the server, and for the same reason: the ATS
 * board slug is what the platform itself calls the employer, whereas the display name
 * arrives as "Stripe", "Stripe, Inc." or "Stripe Payments Europe" depending on which
 * connector found the job. Grouping on the raw name would report three companies where
 * there is one — and quietly miss exactly the duplicate this is meant to catch.
 */
export function employerKey(job: Pick<Job, "company" | "apply_board_slug">): string | null {
  const slug = job.apply_board_slug?.trim().toLowerCase();
  if (slug) return slug;
  // Kept step-for-step in line with `apply_profile.normalise_label`: drop parentheticals,
  // turn punctuation into a space (not into nothing — "Stripe,Inc" must not become one
  // word when the server would read two), collapse, then hyphenate. Diverging here would
  // let the dashboard and /applications/by-company group the same employer differently.
  const name = (job.company ?? "")
    .toLowerCase()
    .replace(/\([^)]*\)/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, "-");
  return name || null;
}

/** What a tailored CV is identified by. Every "original" is the same file, so they
 *  collapse to one variant; two tailored CVs are two variants even for one employer. */
function cvRef(a: Application): string {
  return a.cv_draft_id ?? a.cv_source;
}

export type EmployerHistory = {
  applications: Application[];
  /** Distinct CVs sent to this employer. >1 is the case worth warning about. */
  cvVariants: number;
};

/**
 * Index every application in the pool by employer.
 *
 * Must be built from the *whole* job list rather than the filtered view: an application
 * you made to a company whose other role is currently filtered out of sight is precisely
 * the one you are most likely to duplicate.
 */
export function indexApplications(jobs: Job[]): Map<string, EmployerHistory> {
  const index = new Map<string, EmployerHistory>();
  for (const job of jobs) {
    const app = job.application;
    if (!app) continue;
    const key = employerKey(job);
    if (!key) continue;
    const entry = index.get(key) ?? { applications: [], cvVariants: 0 };
    entry.applications.push(app);
    index.set(key, entry);
  }
  for (const entry of index.values()) {
    entry.cvVariants = new Set(entry.applications.map(cvRef)).size;
    entry.applications.sort((a, b) => (a.applied_at ?? "").localeCompare(b.applied_at ?? ""));
  }
  return index;
}

/** The other applications at this job's employer — this job's own excluded, since a row
 *  telling you that you applied to it is not telling you about a duplicate. */
export function otherApplicationsAt(
  job: Job,
  index: Map<string, EmployerHistory>,
): Application[] {
  const key = employerKey(job);
  if (!key) return [];
  const entry = index.get(key);
  if (!entry) return [];
  return entry.applications.filter((a) => a.job_id !== job.id);
}

export function formatApplied(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const CV_LABEL: Record<Application["cv_source"], string> = {
  tailored: "tailored CV",
  original: "original resume",
  none: "no CV from JobPilot",
};
