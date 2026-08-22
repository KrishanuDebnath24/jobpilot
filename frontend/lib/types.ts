export type DimensionScores = {
  skills?: number;
  experience?: number;
  roles_match?: number;
};

export type ReasonsJson = {
  dimension_scores?: DimensionScores;
  weighted_score?: number;
  roles_match?: number | null;
  gate?: string;
  reasons?: string[];
  // Addendum 2.2: scored on aggregator snippet text only (low-confidence).
  limited_data?: boolean;
  resume_version?: string | null;
  config_version?: string | null;
} | null;

export type Qualification = {
  score: number | null;
  decision: "qualified" | "disqualified" | null;
  matched_skills: string[];
  missing_skills: string[];
  reasons_json: ReasonsJson;
} | null;

export type Job = {
  id: string;
  title: string | null;
  company: string | null;
  location: string | null;
  url: string | null;
  description: string | null;
  source: string | null;
  source_type: string | null;
  connector: string | null;
  // Addendum 2.2: full | snippet | hydrated (Level 2 borrowed a full-text twin's body).
  description_completeness?: string | null;
  // Deterministically classified by Scout (no LLM); null when unknown.
  employment_type?: string | null; // full_time|part_time|contract|temporary|internship
  seniority?: string | null; // intern|junior|mid|senior|staff|principal|lead|manager|director
  dedupe_hash: string | null;
  status: string;
  created_at: string | null;
  qualification: Qualification;
  // Apply agent stage 2: where the application form actually lives. Most connectors
  // store a tracking redirect in `url`; these are the terminal page it leads to.
  // `apply_ats` is a platform id, "aggregator" (chain never left a job board) or
  // "unknown". Null means not yet resolved. `apply_stale` = `url` changed since.
  apply_url?: string | null;
  apply_ats?: string | null;
  apply_board_slug?: string | null;
  apply_resolved_at?: string | null;
  apply_stale?: boolean;
};

// Phase 5: Analyzer + Enhancer
export type SkillToAdd = {
  skill: string;
  why?: string;
  frequency?: string;
  truthful_basis?: string;
};
export type ProjectIdea = { title: string; builds_skill: string; description: string };
export type BulletRewrite = { before: string; after: string; grounding: string };
export type AggregateRow = { label: string; count: number; pct: number };
export type Aggregate = {
  total_jobs?: number;
  // Addendum 2.2 (revised): frequency stats are computed from full-text jobs only;
  // `basis` states the honest denominator ("N full-text of M qualified").
  full_text_jobs?: number;
  snippet_jobs?: number;
  basis?: string;
  skill_frequency?: AggregateRow[];
  common_keywords?: AggregateRow[];
  tech_stack_frequency?: AggregateRow[];
  certification_frequency?: AggregateRow[];
  seniority_distribution?: AggregateRow[];
  recurring_gaps?: AggregateRow[];
};

export type Suggestion = {
  id: string;
  job_id: string | null;
  batch_id: string | null;
  skills_to_add: string[];
  projects_json:
    | {
        projects?: ProjectIdea[];
        skills_detail?: SkillToAdd[];
        mode?: string;
        aggregate?: Aggregate;
      }
    | null;
  bullet_rewrites_json: { bullet_rewrites?: BulletRewrite[] } | null;
  gap_close_json: GapClose | null;
  status: "proposed" | "accepted" | "dismissed";
  created_at: string | null;
};

/** Phase 6.6 — the Gap-closer's unconstrained proposals + per-item review state.
 *  `accepted: null` = undecided, `true` = the candidate vouched for it (which is what
 *  admits it past the evidence gate), `false` = rejected. */
export type GapCloseItemBase = {
  id: string;
  accepted: boolean | null;
  closes?: string;
  interview_prep?: string;
  user_edited?: boolean;
};
export type GapCloseSkill = GapCloseItemBase & { skill: string; in_resume?: boolean };
export type GapCloseRewrite = GapCloseItemBase & {
  role_ref?: string;
  before?: string;
  after: string;
};
export type GapCloseNewBullet = GapCloseItemBase & { role_ref?: string; text: string };
export type GapClose = {
  claimed_skills?: GapCloseSkill[];
  bullet_rewrites?: GapCloseRewrite[];
  new_bullets?: GapCloseNewBullet[];
};
export type GapCloseKind = "claimed_skills" | "bullet_rewrites" | "new_bullets";
/** Durable progress of a gap-close run. The runner writes this on every phase change and
 *  after every job, so the panel can show what's happening even across a page reload — and
 *  during the analyze step, which produces nothing observable until it finishes. */
export type GapCloseStatus = {
  phase: "starting" | "analyzing" | "proposing" | "done" | "failed";
  /** Jobs the run is working through (after the gap_close_max_jobs spend cap). */
  total: number;
  /** Jobs selected before the cap; `dropped` > 0 means the tail was not included. */
  requested: number;
  dropped: number;
  done: number;
  job_ids: string[];
  started_at: string;
  finished_at?: string;
  ts: string;
  errors?: Record<string, string>;
  error?: string;
  cost_usd?: number;
  /** Analyze phase only — how many jobs needed analysing first, and the batch it's waiting on. */
  analyzing?: number;
  batch_id?: string;
  batch_status?: string;
  waited_seconds?: number;
};
/** One entry of a review submission — accept/reject plus any edited wording. */
export type GapClosePatch = {
  kind: GapCloseKind;
  id: string;
  accepted?: boolean | null;
  skill?: string;
  text?: string;
  after?: string;
  role_ref?: string;
  interview_prep?: string;
};

export type RunCounts = Record<string, unknown> & {
  scored?: number;
  qualified?: number;
  disqualified?: number;
  gated_out?: number;
  skipped?: number;
  fetched?: number;
  new?: number;
  by_connector?: Record<string, number>;
  // Addendum 2.2 (revised) step 5: per-connector jobs that survive dedup uniquely via
  // that source (the measured keep/drop signal).
  unique_by_connector?: Record<string, number>;
  errors?: Record<string, string>;
  llm_usage?: { estimated_cost_usd?: number };
};

export type PipelineRun = {
  id: string;
  pipeline_id?: string | null;
  stage: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  counts: RunCounts | null;
  error: string | null;
  created_at: string | null;
};

// Phase 4: one autonomous execution (umbrella + its stage runs).
export type PipelineExecution = {
  pipeline_id: string;
  status: string; // running | completed | failed | unknown
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
  stages: { scout?: PipelineRun; qualify?: PipelineRun };
} | null;

// A live event relayed over SSE from the Redis bus.
export type PipelineEvent = {
  type: string; // "scout.completed", "qualify.progress", "gate.pending", …
  stage: string; // scout | qualify | pipeline | gate
  kind: string; // started | progress | completed | failed | pending
  pipeline_id: string | null;
  counts: RunCounts | null;
  ts: string;
  error?: string;
  gate_key?: string;
  gate_id?: string;
};

// Phase 6: Concierge chat + gates + apply-to-resume.
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string | null;
  created_at: string | null;
};
export type ChatEventItem = { tool: string; status: string };
export type ChatTurn = {
  reply: string;
  events: ChatEventItem[];
  pending: { tool: string; args: unknown } | null;
};
// Still reported by /pipeline/status even though the dashboard no longer renders a
// gate panel — a paused run is resolved through the Concierge.
export type PendingGate = { gate_key: string; gate_id: string } | null;
export type ResumeVersion = {
  id: string;
  source: string;
  label: string | null;
  parent_id: string | null;
  created_at: string | null;
};

// Resume upload (POST /resumes) — deterministic parse of an attached CV.
export type StructuredResume = {
  contact?: {
    name?: string | null;
    email?: string | null;
    phone?: string | null;
    location?: string | null;
    links?: string[];
  };
  summary?: string | null;
  skills?: string[];
  experience?: { title?: string | null; company?: string | null; dates?: string | null; bullets?: string[] }[];
  education?: { text?: string }[];
  certifications?: string[];
};
export type ResumeUploadResult = {
  resume_id: string | null;
  stored: boolean;
  mime: string;
  file_ref?: string;
  structured: StructuredResume;
};

// Search config — what Scout fans out on. `id` is absent until the user has saved one
// (we treat "no id" as "not configured yet" → onboarding).
export type SearchConfig = {
  id?: string;
  roles: string[];
  locations: string[];
  keywords: string[];
  sites: string[];
  sources: string[];
  target_employers: string[];
  schedule: string | null;
  created_at?: string | null;
};
// Qualifier config — how Qualify scores jobs + the must_have hard gate.
export type QualifierWeights = {
  skills?: number;
  experience?: number;
  roles_match?: number;
};
export type QualifierConfig = {
  id?: string | null;
  weights_json: QualifierWeights;
  threshold: number | null;
  must_have: string[]; // hard gate: a job missing any of these is disqualified
  nice_to_have: string[];
  created_at?: string | null;
};
// LLM proposal from the resume (review-only; roles are market-canonical).
export type SearchConfigProposal = {
  roles: string[];
  keywords: string[];
  locations: string[];
  seniority?: string | null;
  notes?: string | null;
};
export type ApplyPreview = {
  accepted_count: number;
  applied_rewrites: BulletRewrite[];
  added_skills: string[];
  withheld_skills: string[];
  project_guidance: ProjectIdea[];
  parent_id: string | null;
};

// Phase 7: Query agent (read-only Q&A over data + work status).
export type QueryCitation =
  | {
      type: "job";
      id: string;
      title: string | null;
      company: string | null;
      url: string | null;
      similarity?: number | null;
    }
  | {
      type: "run";
      stage: string;
      status: string | null;
      finished_at: string | null;
      pipeline_id: string | null;
    };
export type QueryTurn = {
  answer: string;
  events: ChatEventItem[];
  citations: QueryCitation[];
  routed_to_concierge: boolean;
  session_id: string;
};

// Phase 6.5: aligned CVs (Rewriter⇄Critic loop).
export type CvChangeNote = { field: string; why?: string; grounded_in?: string };
// An added skill that isn't in the base skills list, with the resume text that supports it.
export type CvSkillEvidence = { skill: string; basis?: string; evidence?: string };
// A Critic issue. New alignment shape is {severity, field, problem, fix}; the legacy
// critic used {category, detail}. Both kept optional so old + new drafts render.
export type CvCriticIssue = {
  severity?: string;
  field?: string;
  problem?: string;
  fix?: string;
  category?: string;
  detail?: string;
};
// One rewrite→critique round of the alignment loop.
export type CvAlignmentRound = {
  round?: number;
  verdict?: string;
  alignment_score?: number;
  dimension_scores?: Record<string, number>;
  unaddressed_requirements?: string[];
  issues?: CvCriticIssue[];
  integrity_flags?: string[];
  code_violations?: string[];
  code_rejected_skills?: string[];
  added_skills?: string[];
};
export type CvCriticNotes = {
  // Single-pass drafts (settings.cv_max_rounds = 0, the default): one Rewriter call and the
  // deterministic gate, no Critic. Every critic-derived field below is then ABSENT rather
  // than zero — the draft is unscored, not badly scored — so each render site must keep
  // gating on `!= null` and must not substitute a 0 default.
  single_pass?: boolean;
  // What the deterministic gate caught, recorded in both modes (it is evidence, not opinion).
  code_violations?: string[];
  code_rejected_skills?: string[];
  // Alignment-loop shape (jobpilot_core.cv_loop + cv_align_runner._notes).
  aligned?: boolean;
  alignment_score?: number;
  threshold?: number;
  rounds_run?: number;
  max_rounds?: number;
  final_verdict?: string;
  dimension_scores?: Record<string, number>;
  matched_requirements?: string[];
  unaddressed_requirements?: string[];
  integrity_flags?: string[];
  added_skills?: string[];
  skill_evidence?: CvSkillEvidence[];
  change_notes?: CvChangeNote[];
  rounds?: CvAlignmentRound[];
  // Phase 6.6: what the candidate personally vouched for on this draft (absent on drafts
  // built purely from résumé-grounded content, so honest drafts render exactly as before).
  user_attested?: {
    skills?: string[];
    new_bullets?: { role_ref?: string; text?: string }[];
    bullet_rewrites?: { role_ref?: string; after?: string }[];
    note?: string;
  };
  // Legacy (pre-alignment-loop drafts) — still read so old rows render.
  caught_before_enforce?: { violations: string[]; fabricated: string[] };
  final_ok?: boolean;
} | null;
export type TailoredContent = {
  contact?: { name?: string; email?: string; phone?: string; location?: string; links?: string[] };
  summary?: string;
  skills?: string[];
  experience?: {
    title?: string;
    company?: string;
    /** Where the role was based. Locked like the employer, and printed opposite it. */
    location?: string;
    dates?: string;
    bullets?: string[];
  }[];
  education?: { text?: string }[];
  certifications?: string[];
  additional_sections?: { heading?: string; items?: string[] }[];
  skill_evidence?: CvSkillEvidence[];
  change_notes?: CvChangeNote[];
} | null;
export type CvDraft = {
  id: string;
  job_id: string;
  status: "draft_ready" | "approved" | "rejected";
  before_score: number | null;
  after_score: number | null;
  pdf_ref: string | null;
  docx_ref: string | null;
  base_resume_version?: string | null;
  critic_notes_json: CvCriticNotes;
  tailored_content_json: TailoredContent;
  created_at: string | null;
};
export type CvReportRow = {
  draft_id: string;
  job_id: string;
  job_title: string | null;
  company: string | null;
  before_score: number | null;
  after_score: number | null;
  delta: number | null;
  status: string;
};
export type CvReport = {
  report: CvReportRow[];
  improved: number;
  unchanged: number;
  worse: number;
  total: number;
};

// --- Apply agent (stage 1): pre-configured application facts ---
// Every value an apply agent may submit on your behalf traces to this profile or to the
// answer bank below — never to a model's inference.
export type ApplicationProfile = {
  id?: string | null;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  address_line1: string | null;
  city: string | null;
  region: string | null;
  postcode: string | null;
  country: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
  notice_period_days: number | null;
  earliest_start_date: string | null; // ISO-8601 date
  salary_expectation: number | null;
  salary_currency: string | null;
  willing_to_relocate: boolean | null;
  work_mode_preference: string | null;
  // Keyed by jurisdiction ("IE", "UK", …) because authorisation differs per country
  // while the question wording barely does. Locked: never similarity-matched.
  eligibility_json: Record<string, Record<string, unknown>>;
  demographics_json: Record<string, unknown>;
  seeded_from_resume_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type AnswerKind = "text" | "boolean" | "select" | "number" | "date";

// A screening question you've already answered, reused on later forms. `locked` is set
// server-side from the question's content — sensitive questions resolve by exact match
// only, so a near-miss escalates to you instead of reusing a wrong answer.
export type AnswerBankEntry = {
  id: string;
  question_text: string;
  question_norm: string;
  answer_value: string;
  answer_kind: AnswerKind;
  scope: string; // "global" | "employer:<slug>"
  locked: boolean;
  times_used: number;
  last_used_at: string | null;
  confirmed_at: string | null;
  created_at: string | null;
};

export type SeedProfileResult = {
  seeded: boolean;
  reason?: string;
  fields?: string[];
  profile?: ApplicationProfile;
};
