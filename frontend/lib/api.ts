import type { Job, PipelineRun } from "./types";

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
