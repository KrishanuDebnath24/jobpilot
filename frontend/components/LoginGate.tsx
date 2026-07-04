"use client";

import { useState } from "react";
import { login, ApiError } from "@/lib/api";

export default function LoginGate({ onAuthed }: { onAuthed: () => void }) {
  const [email, setEmail] = useState("test@jobpilot.dev");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      onAuthed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-5 rounded-2xl border border-neutral-800 bg-neutral-900/60 p-8 shadow-xl"
      >
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">JobPilot</h1>
          <p className="text-sm text-neutral-400">Sign in to your cockpit.</p>
        </div>
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-400">Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
        </label>
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-400">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
        </label>
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-neutral-100 px-3 py-2 text-sm font-medium text-neutral-900 transition hover:bg-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
