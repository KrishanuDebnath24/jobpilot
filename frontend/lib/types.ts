export type DimensionScores = {
  skills?: number;
  experience?: number;
  roles_match?: number;
};

// Phase 8b: per-user LLM cost report (estimates from logged run usage).
export type CostReport = {
  total_cost_usd: number;
  total_naive_cost_usd: number;
  total_savings_usd: number;
  savings_pct: number;
  tokens: { input_tokens?: number; output_tokens?: number; cache_read_tokens?: number; cache_write_tokens?: number };
  by_agent: Record<string, { cost_usd: number; runs: number; input_tokens: number; output_tokens: number }>;
  by_day: Record<string, number>;
  batch_discount_active: boolean;
  cache_discount_active: boolean;
  daily_threshold_usd: number;
  alerts: { date: string; cost_usd: number }[];
  over_threshold: boolean;
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
export type GateConfig = {
  gates: Record<string, boolean>;
  defaults?: Record<string, boolean>;
  keys?: string[];
};
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
  experience?: { title?: string; company?: string; dates?: string; bullets?: string[] }[];
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
