#!/usr/bin/env node
/**
 * Drains agentic_ai_workflow's topic queue one topic at a time, triggering
 * its "Real Estate Content · Draft From Queue" pipeline (Pipeline B-2 —
 * see ORCHESTRATION_IMPROVEMENTS.md item 7 there) once per claimed topic.
 * Meant to run from system crontab, e.g. every 30 minutes:
 *
 *   0,30 * * * * cd /path/to/real_estate_scripts && /usr/bin/node crons/draft_from_queue.mjs >> logs/draft_from_queue.log 2>&1
 *
 * Unlike listing_content.mjs, this doesn't touch Mongo directly — the topic
 * queue is orchestration work-state that lives in agentic_ai_workflow's own
 * database, not a real-estate data collection this repo otherwise owns, so
 * it's reached purely over HTTP (same as daily_topic_digest.mjs already
 * does for triggering a pipeline). Claiming (POST /api/topic-queue/claim)
 * is atomic on the server side — see claimOldestPendingTopic there — so
 * running this concurrently with itself can't double-process a topic.
 *
 * Deliberately claims and triggers at most ONE topic per tick, not a batch
 * like listing_content.mjs's MAX_PER_RUN=5 — each triggered run is a real,
 * fairly expensive multi-agent pipeline (opus-5 drafting + image +
 * compliance + publish), unlike that script's cheaper per-item cost, so
 * starting a burst of them per tick isn't the right default here.
 *
 * Usage:
 *   node crons/draft_from_queue.mjs
 *   node crons/draft_from_queue.mjs --dry-run   # show the oldest pending topic, claim/trigger nothing
 */
const DASHBOARD_URL = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4000";
const PIPELINE_NAME = "Real Estate Content · Draft From Queue";
const dryRun = process.argv.includes("--dry-run");

function buildTriggerInput(topic) {
  return [
    `Title: ${topic.title}`,
    `Content type: ${topic.contentType}`,
    `Angle: ${topic.angle}`,
    `Target audience: ${topic.targetAudience}`,
    `Why now: ${topic.whyNow}`,
  ].join("\n");
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

async function claimOldestPendingTopic() {
  const res = await fetch(`${DASHBOARD_URL}/api/topic-queue/claim`, { method: "POST" });
  if (!res.ok) throw new Error(`POST /api/topic-queue/claim -> ${res.status}`);
  const { claimed } = await res.json();
  return claimed;
}

async function peekOldestPendingTopic() {
  const res = await fetch(`${DASHBOARD_URL}/api/topic-queue?status=pending`);
  if (!res.ok) throw new Error(`GET /api/topic-queue?status=pending -> ${res.status}`);
  const pending = await res.json();
  return pending[0] ?? null; // already sorted oldest-first
}

async function triggerRun(pipelineId, triggerInput) {
  const res = await fetch(`${DASHBOARD_URL}/api/pipelines/${pipelineId}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ triggerInput }),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`POST /api/pipelines/${pipelineId}/runs -> ${res.status}: ${body.slice(0, 500)}`);
  return JSON.parse(body);
}

async function recordRunId(topicId, runId) {
  // Best-effort — see setTopicQueueRunId's own comment: this is bookkeeping
  // for later visibility, not required for the claim's own correctness.
  try {
    const res = await fetch(`${DASHBOARD_URL}/api/topic-queue/${topicId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId }),
    });
    if (!res.ok) console.warn(`PATCH /api/topic-queue/${topicId} -> ${res.status} (non-fatal)`);
  } catch (err) {
    console.warn(`PATCH /api/topic-queue/${topicId} failed (non-fatal): ${err.message}`);
  }
}

async function main() {
  if (dryRun) {
    const topic = await peekOldestPendingTopic();
    if (!topic) {
      console.log(`[${new Date().toISOString()}] queue empty — nothing to claim`);
      return;
    }
    console.log(`[${new Date().toISOString()}] would claim: ${topic.title} (id ${topic.id})`);
    console.log(buildTriggerInput(topic));
    return;
  }

  const topic = await claimOldestPendingTopic();
  if (!topic) {
    console.log(`[${new Date().toISOString()}] queue empty — nothing claimed`);
    return;
  }

  console.log(`[${new Date().toISOString()}] claimed: ${topic.title} (id ${topic.id})`);
  const triggerInput = buildTriggerInput(topic);
  console.log(triggerInput);

  const pipelineId = await findPipelineId();
  const run = await triggerRun(pipelineId, triggerInput);
  console.log(`started run ${run.id}`);

  await recordRunId(topic.id, run.id);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
