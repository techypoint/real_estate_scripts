#!/usr/bin/env node
/**
 * Polls the `projects` collection for newly-listed projects and triggers
 * agentic_ai_workflow's "Real Estate Content · New Listing Announcement"
 * pipeline (Pipeline A — see SOCIAL_CONTENT_PIPELINE.md there) once per
 * project found. Meant to run from system crontab, e.g. every 30 minutes:
 *
 *   0,30 * * * * cd /path/to/real_estate_scripts && /usr/bin/node crons/listing_content.mjs >> logs/listing_content.log 2>&1
 *
 * "New" here means `createdAt` (when the project document was first
 * scraped/imported — see lib/db.mjs / import_rera.mjs) newer than the
 * checkpoint, AND `listed: true` (the actual public-visibility gate — see
 * ProjectRepository.buildFilter in real_estate_backend). The checkpoint is
 * a single doc in a new `cron_state` collection, advanced after each
 * project is successfully handed to the pipeline (not batched), so a
 * mid-run crash never loses progress or double-triggers.
 *
 * KNOWN LIMITATION, by design of using createdAt: a project scraped weeks
 * ago whose `listed` flag only flips to true today will NOT be caught —
 * its createdAt is old, so it's already behind any checkpoint set after it
 * was scraped. This only catches projects that are new AND already listed
 * at scrape/import time. If projects commonly get listed well after being
 * scraped, this needs an `updatedAt`-based check instead (with the
 * tradeoff that any edit, not just a listing/publish, would look "new").
 *
 * First run ever (no checkpoint doc): initializes the checkpoint to the
 * newest `createdAt` currently in the collection and triggers nothing —
 * deliberately, so turning this on for the first time doesn't generate
 * content for the ~1,000+ projects that already exist. Only projects
 * created after that point are ever picked up.
 *
 * Usage:
 *   node crons/listing_content.mjs
 *   node crons/listing_content.mjs --dry-run   # find candidates, print them, write nothing, trigger nothing
 */
import { connect, close, loadEnv, COL } from "../lib/db.mjs";

const DASHBOARD_URL = process.env.DASHBOARD_URL ?? "http://127.0.0.1:4000";
const FRONTEND_URL = process.env.FRONTEND_URL ?? "http://localhost:3000";
const PIPELINE_NAME = "Real Estate Content · New Listing Announcement";
const CHECKPOINT_ID = "listing_content_cron";
const MAX_PER_RUN = 5; // bounds cost per tick if a big import just landed; the rest catch up next run

// Mirrors real_estate_frontend/src/lib/slug.ts's projectSlug() exactly —
// kept in sync by hand, not imported (separate repo/runtime). Only used for
// the link handed to the Content Writer as reference copy; if the two ever
// drift, the frontend's own project page redirects a slightly-wrong slug to
// its canonical one anyway (see project/[slug]/page.tsx's permanentRedirect).
function slugify(input) {
  return input
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
function projectSlug(name, registrationNo) {
  const namePart = name ? slugify(name) : "";
  return namePart ? `${namePart}-${registrationNo}` : registrationNo;
}

function buildTriggerInput(project) {
  const slug = projectSlug(project.project_name, project.registration_no);
  const lines = [
    `Project: ${project.project_name || project.registration_no}`,
    `Registration No: ${project.registration_no}`,
    `Promoter: ${project.promoter_name || "(not on file)"}`,
    `District: ${project.district || "(not on file)"}`,
    `Project Type: ${project.project_type || "(not on file)"}`,
    project.project_category ? `Category: ${project.project_category}` : null,
    project.declared_completion_date ? `Declared Completion: ${project.declared_completion_date}` : null,
    `Listing URL: ${FRONTEND_URL}/project/${slug}`,
  ].filter(Boolean);
  return lines.join("\n");
}

async function findPipelineId(dashboardUrl) {
  const res = await fetch(`${dashboardUrl}/api/pipelines`);
  if (!res.ok) throw new Error(`GET /api/pipelines -> ${res.status}`);
  const pipelines = await res.json();
  const match = pipelines.find((p) => p.name === PIPELINE_NAME);
  if (!match) {
    throw new Error(
      `No pipeline named "${PIPELINE_NAME}" found on ${dashboardUrl}. Run scripts/seed-content-pipeline.mjs in agentic_ai_workflow first.`,
    );
  }
  return match.id;
}

async function triggerRun(dashboardUrl, pipelineId, triggerInput) {
  const res = await fetch(`${dashboardUrl}/api/pipelines/${pipelineId}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ triggerInput }),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`POST /api/pipelines/${pipelineId}/runs -> ${res.status}: ${body.slice(0, 500)}`);
  return JSON.parse(body);
}

async function main() {
  loadEnv();
  const dryRun = process.argv.includes("--dry-run");
  const { db } = await connect();

  try {
    const checkpoints = db.collection("cron_state");
    const state = await checkpoints.findOne({ _id: CHECKPOINT_ID });

    if (!state) {
      const [newest] = await db
        .collection(COL.projects)
        .find({}, { projection: { createdAt: 1 } })
        .sort({ createdAt: -1 })
        .limit(1)
        .toArray();
      const initCheckpoint = newest?.createdAt ?? new Date();
      console.log(
        `[${new Date().toISOString()}] no checkpoint yet — initializing to ${initCheckpoint.toISOString()} and triggering nothing this run`,
      );
      if (!dryRun) {
        await checkpoints.insertOne({ _id: CHECKPOINT_ID, lastCreatedAt: initCheckpoint, updatedAt: new Date() });
      }
      return;
    }

    const candidates = await db
      .collection(COL.projects)
      .find(
        { listed: true, createdAt: { $gt: state.lastCreatedAt } },
        { projection: { registration_no: 1, project_name: 1, promoter_name: 1, district: 1, project_type: 1, project_category: 1, declared_completion_date: 1, createdAt: 1 } },
      )
      .sort({ createdAt: 1 })
      .limit(MAX_PER_RUN)
      .toArray();

    console.log(`[${new Date().toISOString()}] checkpoint ${state.lastCreatedAt.toISOString()} — ${candidates.length} new listed project(s) found`);

    if (candidates.length === 0) return;

    const pipelineId = dryRun ? null : await findPipelineId(DASHBOARD_URL);

    for (const project of candidates) {
      const triggerInput = buildTriggerInput(project);
      console.log(`--- ${project.registration_no} · ${project.project_name} (createdAt ${project.createdAt.toISOString()}) ---`);
      console.log(triggerInput);

      if (dryRun) continue;

      const run = await triggerRun(DASHBOARD_URL, pipelineId, triggerInput);
      console.log(`started run ${run.id}`);

      await checkpoints.updateOne(
        { _id: CHECKPOINT_ID },
        { $set: { lastCreatedAt: project.createdAt, updatedAt: new Date() } },
      );
    }
  } finally {
    await close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
