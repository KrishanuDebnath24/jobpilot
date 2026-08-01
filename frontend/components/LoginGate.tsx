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

  const inputClass =
    "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 shadow-card outline-none transition placeholder:text-slate-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100";

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-white px-6">
      {/* A quiet wash of colour behind the card — the page is white, not blank. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(60rem_40rem_at_50%_-10%,theme(colors.indigo.50),transparent)]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.35] [background-image:linear-gradient(theme(colors.slate.100)_1px,transparent_1px),linear-gradient(90deg,theme(colors.slate.100)_1px,transparent_1px)] [background-size:44px_44px] [mask-image:radial-gradient(40rem_28rem_at_50%_40%,black,transparent)]"
      />

      <form
        onSubmit={submit}
        className="screen-enter relative w-full max-w-sm space-y-5 rounded-2xl border border-slate-200 bg-white p-8 shadow-xl shadow-slate-900/5"
      >
        <div className="space-y-2">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-indigo-600 text-sm font-bold text-white shadow-sm">
            JP
          </span>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">JobPilot</h1>
          <p className="text-sm text-slate-500">
            {isRegister ? "Create your cockpit." : "Sign in to your cockpit."}
          </p>
        </div>

        {/* Sign in / Create account switch */}
        <div className="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-sm">
          {(["signin", "register"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => switchMode(m)}
              className={`flex-1 rounded-md px-3 py-1.5 font-medium transition-colors ${
                mode === m
                  ? "bg-white text-slate-900 shadow-card"
                  : "text-slate-500 hover:text-slate-900"
              }`}
            >
              {m === "signin" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-slate-600">Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            className={inputClass}
          />
        </label>
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-slate-600">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={isRegister ? "new-password" : "current-password"}
            className={inputClass}
          />
          {isRegister && <span className="text-xs text-slate-400">At least 8 characters.</span>}
        </label>
        {isRegister && (
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-slate-600">Confirm password</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              className={inputClass}
            />
          </label>
        )}

        {error && (
          <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-3 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 active:scale-[0.99] disabled:opacity-50"
        >
          {busy && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
          )}
          {busy
            ? isRegister
              ? "Creating account…"
              : "Signing in…"
            : isRegister
              ? "Create account"
              : "Sign in"}
        </button>

        <p className="text-center text-xs text-slate-500">
          {isRegister ? (
            <>
              Already have an account?{" "}
              <button
                type="button"
                onClick={() => switchMode("signin")}
                className="font-medium text-indigo-600 underline underline-offset-2 hover:text-indigo-700"
              >
                Sign in
              </button>
            </>
          ) : (
            <>
              New here?{" "}
              <button
                type="button"
                onClick={() => switchMode("register")}
                className="font-medium text-indigo-600 underline underline-offset-2 hover:text-indigo-700"
              >
                Create an account
              </button>
            </>
          )}
        </p>
      </form>
    </main>
  );
}
