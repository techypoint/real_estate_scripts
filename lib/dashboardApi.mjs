/**
 * Shared fetch wrapper for agentic_ai_workflow's dashboard API. Every route
 * there (its src/proxy.ts) now requires either a browser session cookie or
 * this bearer token — crons have no session, so this is the only way in.
 * DASHBOARD_API_TOKEN here must match agentic_ai_workflow's CRON_API_TOKEN.
 */
import { loadEnv } from "./db.mjs";

export function dashboardFetch(path, options = {}) {
  loadEnv();
  const base = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4000";
  const token = process.env.DASHBOARD_API_TOKEN;
  if (!token) throw new Error("DASHBOARD_API_TOKEN not set. Add it to .env at the repo root.");

  return fetch(new URL(path, base), {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${token}` },
  });
}
