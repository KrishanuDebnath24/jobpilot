import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "JobPilot — Dashboard",
  description: "Agentic job-hunting cockpit — Qualifier dashboard.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
