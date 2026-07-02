import { NextResponse } from "next/server";

// Frontend's own liveness endpoint (used by the container healthcheck / CI).
export async function GET() {
  return NextResponse.json({ status: "ok", service: "jobpilot-frontend" });
}
