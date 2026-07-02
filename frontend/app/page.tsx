import HealthStatus from "@/components/HealthStatus";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">JobPilot</h1>
        <p className="text-neutral-400">
          Agentic job-hunting cockpit —{" "}
          <span className="text-neutral-200">Phase 0 scaffold</span>. This page is
          wired end-to-end to the backend health check.
        </p>
      </header>

      <HealthStatus />

      <footer className="text-sm text-neutral-500">
        Next up (pending approval): Phase 1 — data model, <code>db-mcp</code>,
        resume parsing, and GDPR erasure.
      </footer>
    </main>
  );
}
