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
  dedupe_hash: string | null;
  status: string;
  created_at: string | null;
  qualification: Qualification;
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
  type: string; // "scout.completed", "qualify.progress", "pipeline.started", …
  stage: string; // scout | qualify | pipeline
  kind: string; // started | progress | completed | failed
  pipeline_id: string | null;
  counts: RunCounts | null;
  ts: string;
  error?: string;
};
