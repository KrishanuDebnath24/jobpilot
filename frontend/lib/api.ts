import type { Job, PipelineEvent, PipelineExecution, PipelineRun } from "./types";

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
  request<{ pipeline: PipelineExecution; active: boolean }>("/pipeline/status");
export const listPipelineRuns = () =>
  request<{ runs: PipelineExecution[] }>("/pipeline/runs").then((r) => r.runs);

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
