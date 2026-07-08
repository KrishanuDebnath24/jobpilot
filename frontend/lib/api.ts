import type {
  ApplyPreview,
  ChatMessage,
  ChatTurn,
  CvDraft,
  CvReport,
  GateConfig,
  Job,
  PendingGate,
  PipelineEvent,
  PipelineExecution,
  PipelineRun,
  QueryTurn,
  ResumeVersion,
  Suggestion,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "jobpilot_token";

export function getToken(): string | null {
  return typeof window !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;
}
export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export async function login(email: string, password: string): Promise<string> {
  const body = await request<{ access_token: string }>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  setToken(body.access_token);
  return body.access_token;
}

export const getMe = () => request<{ id: string; email: string }>("/auth/me");
export const getDashboardJobs = () =>
  request<{ jobs: Job[] }>("/dashboard/jobs").then((r) => r.jobs);
export const getQualifyRuns = () =>
  request<{ runs: PipelineRun[] }>("/qualify/runs").then((r) => r.runs);
export const getScoutRuns = () =>
  request<{ runs: PipelineRun[] }>("/scout/runs").then((r) => r.runs);
export const runScout = () =>
  request<{ task_id: string }>("/scout/run", { method: "POST" });
export const runQualify = (requalifyAll = false) =>
  request<{ task_id: string }>(`/qualify/run?requalify_all=${requalifyAll}`, {
    method: "POST",
  });

// --- Phase 4: pipeline orchestrator ---
export type RunPipelineResult = {
  pipeline_id?: string;
  status?: string;
  rejected?: boolean;
  reason?: string;
  active_pipeline_id?: string | null;
};

export const runPipeline = () =>
  request<RunPipelineResult>("/pipeline/run", { method: "POST" });
export const runStage = (stage: "scout" | "qualify") =>
  request<RunPipelineResult>(`/pipeline/stage/${stage}`, { method: "POST" });
export const getPipelineStatus = () =>
  request<{ pipeline: PipelineExecution; active: boolean; pending_gate: PendingGate }>(
    "/pipeline/status",
  );
export const listPipelineRuns = () =>
  request<{ runs: PipelineExecution[] }>("/pipeline/runs").then((r) => r.runs);

// --- Phase 5: Analyzer + Enhancer ---
export type Scope = { job_ids?: string[] | null; all_qualified?: boolean };

export const runAnalyze = (scope: Scope) =>
  request<{ task_id: string }>("/analyze/run", {
    method: "POST",
    body: JSON.stringify(scope),
  });
export const runEnhance = (scope: Scope) =>
  request<{ task_id: string }>("/enhance/run", {
    method: "POST",
    body: JSON.stringify(scope),
  });
export const getAnalyzeRuns = () =>
  request<{ runs: PipelineRun[] }>("/analyze/runs").then((r) => r.runs);
export const getEnhanceRuns = () =>
  request<{ runs: PipelineRun[] }>("/enhance/runs").then((r) => r.runs);
export const getSuggestions = () =>
  request<{ suggestions: Suggestion[] }>("/suggestions").then((r) => r.suggestions);
export const setSuggestionStatus = (id: string, status: "accepted" | "dismissed" | "proposed") =>
  request<{ suggestion: unknown }>(`/suggestions/${id}/status?status=${status}`, {
    method: "POST",
  });

// --- Phase 6: Concierge chat + gates + apply-to-resume ---
export const createChatSession = () =>
  request<{ id: string }>("/chat/sessions", { method: "POST" });
export const listChatSessions = () =>
  request<{ sessions: { id: string; title: string | null }[] }>("/chat/sessions").then(
    (r) => r.sessions,
  );
export const getChatMessages = (id: string) =>
  request<{ messages: ChatMessage[] }>(`/chat/sessions/${id}/messages`).then((r) => r.messages);
export const postChatMessage = (id: string, text: string) =>
  request<ChatTurn>(`/chat/sessions/${id}/message`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });

export const getGates = () => request<GateConfig>("/gates");
export const updateGate = (gate_key: string, enabled: boolean) =>
  request<GateConfig>("/gates", {
    method: "POST",
    body: JSON.stringify({ gate_key, enabled }),
  });
export const approveGate = (gate_id: string) =>
  request<{ status: string }>("/pipeline/gate/approve", {
    method: "POST",
    body: JSON.stringify({ gate_id }),
  });
export const skipGate = (gate_id: string) =>
  request<{ status: string }>("/pipeline/gate/skip", {
    method: "POST",
    body: JSON.stringify({ gate_id }),
  });

export const prepareApply = (confirmed_skills?: string[]) =>
  request<ApplyPreview>("/resume/apply/prepare", {
    method: "POST",
    body: JSON.stringify({ confirmed_skills: confirmed_skills ?? null }),
  });
export const applyResume = (confirmed_skills?: string[], label?: string) =>
  request<{ applied: boolean; resume?: { id: string } }>("/resume/apply", {
    method: "POST",
    body: JSON.stringify({ confirmed_skills: confirmed_skills ?? null, label: label ?? null }),
  });
export const getResumeVersions = () =>
  request<{ resumes: ResumeVersion[] }>("/resume/versions").then((r) => r.resumes);

/** Download a resume version as text (fetch with the auth header, then save). */
export async function downloadResumeVersion(id: string): Promise<void> {
  const res = await fetch(`${API_URL}/resume/versions/${id}/download`, {
    headers: { Authorization: `Bearer ${getToken() ?? ""}` },
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, `download failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `resume-${id.slice(0, 8)}.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

// --- Phase 6.5: Tailored CVs ---
export const tailorCvs = (job_ids: string[]) =>
  request<{ task_id: string; job_count: number }>("/cv/tailor", {
    method: "POST",
    body: JSON.stringify({ job_ids }),
  });
export const getCvDrafts = (status?: string) =>
  request<{ drafts: CvDraft[] }>(`/cv/drafts${status ? `?status=${status}` : ""}`).then(
    (r) => r.drafts,
  );
export const approveCvDrafts = (draft_ids?: string[], all = false) =>
  request<{ updated: number }>("/cv/approve", {
    method: "POST",
    body: JSON.stringify({ draft_ids: draft_ids ?? null, all }),
  });
export const rejectCvDrafts = (draft_ids?: string[], all = false) =>
  request<{ updated: number }>("/cv/reject", {
    method: "POST",
    body: JSON.stringify({ draft_ids: draft_ids ?? null, all }),
  });
export const exportCvs = (draft_ids?: string[]) =>
  request<{ task_id: string }>("/cv/export", {
    method: "POST",
    body: JSON.stringify({ draft_ids: draft_ids ?? null }),
  });
export const requalifyCvs = (draft_ids?: string[]) =>
  request<{ task_id: string }>("/cv/requalify", {
    method: "POST",
    body: JSON.stringify({ draft_ids: draft_ids ?? null }),
  });
export const getCvReport = () => request<CvReport>("/cv/report");

/** Download a rendered tailored CV (pdf|docx), sending the auth header. */
export async function downloadCv(draftId: string, fmt: "pdf" | "docx"): Promise<void> {
  const res = await fetch(`${API_URL}/cv/drafts/${draftId}/download/${fmt}`, {
    headers: { Authorization: `Bearer ${getToken() ?? ""}` },
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, `download failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `cv-${draftId.slice(0, 8)}.${fmt}`;
  a.click();
  URL.revokeObjectURL(url);
}

// --- Phase 7: Query agent (read-only Q&A over data + work status) ---
export const createQuerySession = () =>
  request<{ id: string }>("/query/session", { method: "POST" });
export const getQueryMessages = (id: string) =>
  request<{ messages: ChatMessage[] }>(`/query/session/${id}/messages`).then((r) => r.messages);
export const askQuery = (question: string, session_id?: string | null) =>
  request<QueryTurn>("/query/ask", {
    method: "POST",
    body: JSON.stringify({ question, session_id: session_id ?? null }),
  });

/** Subscribe to the live pipeline event stream (SSE over fetch, so we can send the
 *  auth header). Auto-reconnects until the returned stop() is called. */
export function streamPipeline(
  onEvent: (ev: PipelineEvent) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  let stopped = false;
  const controller = new AbortController();

  (async () => {
    while (!stopped) {
      try {
        const res = await fetch(`${API_URL}/pipeline/stream`, {
          headers: { Authorization: `Bearer ${getToken() ?? ""}`, Accept: "text/event-stream" },
          signal: controller.signal,
          cache: "no-store",
        });
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        onStatus?.(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const frames = buf.split("\n\n");
          buf = frames.pop() ?? "";
          for (const frame of frames) {
            const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
            if (!dataLine) continue; // heartbeat/comment
            try {
              onEvent(JSON.parse(dataLine.slice(5).trim()) as PipelineEvent);
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      } catch {
        if (stopped) break;
      }
      onStatus?.(false);
      if (!stopped) await new Promise((r) => setTimeout(r, 2000)); // reconnect backoff
    }
  })();

  return () => {
    stopped = true;
    controller.abort();
  };
}
