export type DimensionScores = {
  skills?: number;
  experience?: number;
  title_keyword?: number;
  location?: number;
};

export type ReasonsJson = {
  dimension_scores?: DimensionScores;
  weighted_score?: number;
  semantic_fit?: number | null;
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
  source: string | null;
  source_type: string | null;
  connector: string | null;
  // Addendum 2.2: full | snippet.
  description_completeness?: string | null;
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
  status: "proposed" | "accepted" | "dismissed";
  created_at: string | null;
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

// Phase 6.5: tailored CVs.
export type CvChangeNote = { field: string; why?: string; grounded_in?: string };
export type CvCriticIssue = { category: string; detail: string };
export type CvCriticNotes = {
  rounds?: { verdict?: string; issues?: CvCriticIssue[] }[];
  caught_before_enforce?: { violations: string[]; fabricated: string[] };
  final_ok?: boolean;
  change_notes?: CvChangeNote[];
} | null;
export type TailoredContent = {
  contact?: { name?: string; email?: string; phone?: string; location?: string; links?: string[] };
  summary?: string;
  skills?: string[];
  experience?: { title?: string; company?: string; dates?: string; bullets?: string[] }[];
  education?: { text?: string }[];
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
