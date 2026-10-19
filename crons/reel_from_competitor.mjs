#!/usr/bin/env node
/**
 * Starts agentic_ai_workflow's "Real Estate Content · Reel From Competitor"
 * pipeline: a Scout shortlists up to three competitor reels whose topic can be
 * remade from official sources, a human picks one, and the reel is researched,
 * fact-checked, written, rendered and (after a second approval) posted. See
 * video_code/channels/acreinfotech/formats/inspired/SKILL.md. Meant to run from
 * system crontab once a day, e.g. 08:30:
 *
 *   30 8 * * * cd /home/deepak/projects/real_estate_scripts && /usr/bin/node crons/reel_from_competitor.mjs >> logs/reel_from_competitor.log 2>&1
 *
 * Once a day is also the limit worth keeping: the Scout's competitor lookup uses
 * a large share of Instagram's hourly request allowance.
 *
 * Skips the tick when an earlier run of this pipeline is still open (waiting at
 * a gate or working), so unanswered shortlists don't pile up. On a day when
 * nothing qualifies the Scout says so and the run is cancelled at "Pick a reel".
 *
 * Usage:
 *   node crons/reel_from_competitor.mjs --dry-run       # show what it would do, change nothing
 *
 * Env (read from .env at the repo root, or the shell):
 *   REEL_LANGUAGE             "Hindi/Hinglish" (default) or "English"
 *   DASHBOARD_URL, DASHBOARD_API_TOKEN   same as the other crons
 */
import { dashboardFetch } from "../lib/dashboardApi.mjs";
import { loadEnv } from "../lib/db.mjs";

loadEnv();
const PIPELINE_NAME = "Real Estate Content · Reel From Competitor";
const REEL_LANGUAGE = process.env.REEL_LANGUAGE ?? "Hindi/Hinglish";
const FINISHED = new Set(["succeeded", "failed", "rejected", "cancelled"]);

const dryRun = process.argv.includes("--dry-run");
const stamp = () => new Date().toISOString();

async function dashboardJson(path, options) {
  const res = await dashboardFetch(path, options);
  const body = await res.text();
  if (!res.ok) throw new Error(`${options?.method ?? "GET"} ${path} -> ${res.status}: ${body.slice(0, 500)}`);
  return JSON.parse(body);
}

async function main() {
  const pipeline = (await dashboardJson("/api/pipelines")).find((p) => p.name === PIPELINE_NAME);
  if (!pipeline) {
    throw new Error(`No pipeline named "${PIPELINE_NAME}". Run scripts/seed-content-pipeline.mjs in agentic_ai_workflow first.`);
  }

  const open = (await dashboardJson("/api/runs")).filter((r) => r.pipelineId === pipeline.id && !FINISHED.has(r.status));
  if (open.length > 0) {
    console.log(`[${stamp()}] run ${open[0].id} is still ${open[0].status} — not starting another`);
    return;
  }

  const triggerInput = `Language: ${REEL_LANGUAGE}`;
  if (dryRun) {
    console.log(`[dry-run] would start "${PIPELINE_NAME}" with input: ${triggerInput}`);
    return;
  }
  const run = await dashboardJson(`/api/pipelines/${pipeline.id}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ triggerInput }),
  });
  console.log(`[${stamp()}] started run ${run.id} — it waits for a pick at "Pick a reel"`);
}

main().catch((err) => {
  console.error(`[${stamp()}]`, err);
  process.exit(1);
});
