#!/usr/bin/env node
/**
 * Daily trigger for agentic_ai_workflow's "Real Estate Content · Daily
 * Topic Digest" pipeline (Pipeline B — see SOCIAL_CONTENT_PIPELINE.md
 * there). Meant to run from system crontab, once a day:
 *
 *   0 6 * * * cd /path/to/real_estate_scripts && /usr/bin/node crons/daily_topic_digest.mjs >> logs/daily_topic_digest.log 2>&1
 *
 * Looks the pipeline up by name (not a hardcoded id) so re-seeding it in
 * agentic_ai_workflow (same name, new id) never breaks this script. Every
 * run just starts the pipeline with today's date — the Topic Scout agent
 * does the actual research; this script only supplies the one thing that
 * changes daily.
 *
 * Usage:
 *   node crons/daily_topic_digest.mjs
 *   node crons/daily_topic_digest.mjs --dry-run   # resolve the pipeline, print the request, POST nothing
 */
import { loadEnv } from "../lib/db.mjs";

loadEnv();

const DASHBOARD_URL = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4000";
const PIPELINE_NAME = "Real Estate Content · Daily Topic Digest";
const dryRun = process.argv.includes("--dry-run");

function todayISTDate() {
  // Reporting date for the digest, not a timezone-precise cutoff — this
  // only labels the run, it's not used for any query filtering.
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }); // "YYYY-MM-DD"
}

function buildTriggerInput() {
  return `Daily topic run for ${todayISTDate()}. Market: Uttar Pradesh RERA-registered projects (state-wide, no single-city bias). Propose 4-6 topics per your usual format.`;
}

async function findPipelineId() {
  const res = await fetch(`${DASHBOARD_URL}/api/pipelines`);
  if (!res.ok) throw new Error(`GET /api/pipelines -> ${res.status}`);
  const pipelines = await res.json();
  const match = pipelines.find((p) => p.name === PIPELINE_NAME);
  if (!match) {
    throw new Error(
      `No pipeline named "${PIPELINE_NAME}" found on ${DASHBOARD_URL}. Run scripts/seed-content-pipeline.mjs in agentic_ai_workflow first.`,
    );
  }
  return match.id;
}

async function main() {
  const triggerInput = buildTriggerInput();
  const pipelineId = await findPipelineId();

  console.log(`[${new Date().toISOString()}] pipeline "${PIPELINE_NAME}" (${pipelineId})`);
  console.log(`triggerInput: ${triggerInput}`);

  if (dryRun) {
    console.log("--dry-run: not starting a run.");
    return;
  }

  const res = await fetch(`${DASHBOARD_URL}/api/pipelines/${pipelineId}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ triggerInput }),
  });
  const body = await res.text();
  if (!res.ok) {
    throw new Error(`POST /api/pipelines/${pipelineId}/runs -> ${res.status}: ${body.slice(0, 500)}`);
  }
  const run = JSON.parse(body);
  console.log(`started run ${run.id}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
