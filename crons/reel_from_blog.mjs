#!/usr/bin/env node
/**
 * Makes one Instagram Reel from the newest published blog that has no reel
 * yet, by starting agentic_ai_workflow's "Real Estate Content · Reel From
 * Blog" pipeline (see video_code/ and SOCIAL_CONTENT_PIPELINE.md). Meant to
 * run from system crontab, e.g. every 2 hours:
 *
 *   0 0,2,4,6,8,10,12,14,16,18,20,22 * * * cd /home/deepak/projects/real_estate_scripts && /usr/bin/node crons/reel_from_blog.mjs >> logs/reel_from_blog.log 2>&1
 *
 * One blog per tick, same discipline as draft_from_queue.mjs: each run is a
 * multi-step pipeline with a render and a human approval, so starting a burst
 * per tick isn't the default.
 *
 * Steps:
 *   1. GET  real_estate_backend /api/blogs/needs-reel   (published, no reel yet)
 *   2. take the first one (the backend returns newest first)
 *   3. POST /api/blogs/:slug/reel/claim                  (atomic — a racing run gets 409)
 *   4. start the pipeline with the blog text as its input
 *   5. if step 4 fails, PATCH the blog's reel to FAILED so it isn't stuck IN_PROGRESS
 *
 * Usage:
 *   node crons/reel_from_blog.mjs --dry-run              # show the blog it would pick, change nothing
 *
 * Env (read from .env at the repo root, or the shell):
 *   REAL_ESTATE_BACKEND_URL   default http://127.0.0.1:8888
 *   REEL_BLOG_BASE_URL        public blog base, used for the blog link. default https://acreinfotech.com/blog
 *   DASHBOARD_URL, DASHBOARD_API_TOKEN   same as the other crons
 */
import { dashboardFetch } from "../lib/dashboardApi.mjs";
import { loadEnv } from "../lib/db.mjs";

loadEnv();
const BACKEND_URL = process.env.REAL_ESTATE_BACKEND_URL ?? "http://127.0.0.1:8888";
const BLOG_BASE_URL = (process.env.REEL_BLOG_BASE_URL ?? "https://acreinfotech.com/blog").replace(/\/$/, "");
const PIPELINE_NAME = "Real Estate Content · Reel From Blog";

const dryRun = process.argv.includes("--dry-run");

const stamp = () => new Date().toISOString();

async function backend(path, options = {}) {
  return fetch(new URL(path, BACKEND_URL), {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
}

async function findNeedsReel() {
  const res = await backend("/api/blogs/needs-reel?limit=20");
  if (!res.ok) throw new Error(`GET /api/blogs/needs-reel -> ${res.status}`);
  const page = await res.json();
  return page.items; // newest first, already filtered to "no reel yet" by the backend
}

function buildTriggerInput(blog) {
  return [
    `Title: ${blog.title}`,
    `Slug: ${blog.slug}`,
    `Blog URL: ${BLOG_BASE_URL}/${blog.slug}`,
    `SEO description: ${blog.seoDescription ?? ""}`,
    "",
    "Blog body (Markdown):",
    blog.body,
  ].join("\n");
}

async function findPipelineId() {
  const res = await dashboardFetch("/api/pipelines");
  if (!res.ok) throw new Error(`GET /api/pipelines -> ${res.status}`);
  const pipelines = await res.json();
  const match = pipelines.find((p) => p.name === PIPELINE_NAME);
  if (!match) {
    throw new Error(`No pipeline named "${PIPELINE_NAME}". Run scripts/seed-content-pipeline.mjs in agentic_ai_workflow first.`);
  }
  return match.id;
}

async function triggerRun(pipelineId, triggerInput) {
  const res = await dashboardFetch(`/api/pipelines/${pipelineId}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ triggerInput }),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`POST /api/pipelines/${pipelineId}/runs -> ${res.status}: ${body.slice(0, 500)}`);
  return JSON.parse(body);
}

async function markReelFailed(slug) {
  const res = await backend(`/api/blogs/${slug}/reel`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "FAILED" }),
  });
  if (!res.ok) console.warn(`[${stamp()}] PATCH reel FAILED for ${slug} -> ${res.status} (non-fatal; blog is still IN_PROGRESS)`);
}

async function main() {
  const candidates = await findNeedsReel();
  if (candidates.length === 0) {
    console.log(`[${stamp()}] no published blog without a reel — nothing to do`);
    return;
  }

  const blog = candidates[0];
  console.log(`[${stamp()}] next: "${blog.title}" (${blog.slug}), published ${blog.publishedAt}`);

  if (dryRun) {
    console.log(`[dry-run] would claim ${blog.slug} and start "${PIPELINE_NAME}". ${candidates.length - 1} other blog(s) without a reel.`);
    return;
  }

  const claim = await backend(`/api/blogs/${blog.slug}/reel/claim`, { method: "POST" });
  if (claim.status === 409) {
    console.log(`[${stamp()}] ${blog.slug} was claimed by another run — skipping`);
    return;
  }
  if (!claim.ok) throw new Error(`POST /api/blogs/${blog.slug}/reel/claim -> ${claim.status}`);
  console.log(`[${stamp()}] claimed ${blog.slug}`);

  try {
    const pipelineId = await findPipelineId();
    const run = await triggerRun(pipelineId, buildTriggerInput(blog));
    console.log(`[${stamp()}] started run ${run.id} — it waits for approval at "Approve reel"`);
  } catch (err) {
    await markReelFailed(blog.slug);
    throw err;
  }
}

main().catch((err) => {
  console.error(`[${stamp()}]`, err);
  process.exit(1);
});
