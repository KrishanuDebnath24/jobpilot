"use client";

import { useState } from "react";
import { login, register, ApiError } from "@/lib/api";

type Mode = "signin" | "register";

export default function LoginGate({ onAuthed }: { onAuthed: () => void }) {
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("test@jobpilot.dev");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isRegister = mode === "register";

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setConfirm("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (isRegister) {
      if (password.length < 8) return setError("Password must be at least 8 characters.");
      if (password !== confirm) return setError("Passwords don’t match.");
    }
    setBusy(true);
    try {
      if (isRegister) await register(email, password);
      else await login(email, password);
      onAuthed();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : isRegister
            ? "Could not create account"
            : "Login failed",
      );
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
          <p className="text-sm text-neutral-400">
            {isRegister ? "Create your cockpit." : "Sign in to your cockpit."}
          </p>
        </div>

        {/* Sign in / Create account switch */}
        <div className="flex rounded-lg border border-neutral-800 p-0.5 text-sm">
          {(["signin", "register"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => switchMode(m)}
              className={`flex-1 rounded-md px-3 py-1.5 transition ${
                mode === m ? "bg-neutral-800 text-neutral-100" : "text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {m === "signin" ? "Sign in" : "Create account"}
            </button>
          ))}
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
            autoComplete={isRegister ? "new-password" : "current-password"}
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
          {isRegister && (
            <span className="text-xs text-neutral-600">At least 8 characters.</span>
          )}
        </label>
        {isRegister && (
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-400">Confirm password</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
            />
          </label>
        )}

        {error && <p className="text-sm text-rose-400">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-neutral-100 px-3 py-2 text-sm font-medium text-neutral-900 transition hover:bg-white disabled:opacity-50"
        >
          {busy
            ? isRegister
              ? "Creating account…"
              : "Signing in…"
            : isRegister
              ? "Create account"
              : "Sign in"}
        </button>

        <p className="text-center text-xs text-neutral-500">
          {isRegister ? (
            <>
              Already have an account?{" "}
              <button type="button" onClick={() => switchMode("signin")} className="text-neutral-300 underline underline-offset-2 hover:text-neutral-100">
                Sign in
              </button>
            </>
          ) : (
            <>
              New here?{" "}
              <button type="button" onClick={() => switchMode("register")} className="text-neutral-300 underline underline-offset-2 hover:text-neutral-100">
                Create an account
              </button>
            </>
          )}
        </p>
      </form>
    </main>
  );
}
