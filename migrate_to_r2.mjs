#!/usr/bin/env node
/**
 * One-off migration: local disk images + content JSON -> R2.
 *
 * Run once when cutting over to Cloudflare R2 (see CLAUDE.md / docs for the
 * design). Idempotent — safe to re-run; putObject skips keys that already
 * exist, and content JSON rewrites are no-ops once already migrated.
 *
 * What it does NOT do: delete data/media/. Verify the CDN serves everything
 * correctly first, then remove that yourself once you're confident — this
 * script only ever adds.
 *
 * Usage:
 *   node migrate_to_r2.mjs            # images (data/media) + content JSON
 */
import fs from "node:fs/promises";
import path from "node:path";
import { ROOT } from "./lib/db.mjs";
import { putObject } from "./lib/r2.mjs";

const MEDIA_DIR = path.join(ROOT, "data/media");
const CONTENT_DIR = path.join(ROOT, "data/content");

function contentTypeFor(file) {
  if (file.endsWith(".webp")) return "image/webp";
  if (file.endsWith(".png")) return "image/png";
  if (file.endsWith(".jpg") || file.endsWith(".jpeg")) return "image/jpeg";
  return "application/octet-stream";
}

async function walkFiles(dir) {
  const out = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walkFiles(full)));
    else out.push(full);
  }
  return out;
}

async function migrateImages() {
  const files = await walkFiles(MEDIA_DIR).catch(() => []);
  console.log(`Images: ${files.length} file(s) under data/media/`);

  let uploaded = 0;
  for (const file of files) {
    const relPath = path.relative(MEDIA_DIR, file).split(path.sep).join("/");
    const key = `media/${relPath}`;
    const { uploaded: didUpload } = await putObject(key, await fs.readFile(file), {
      contentType: contentTypeFor(file),
    });
    if (didUpload) {
      uploaded++;
      console.log(`  ok   ${key}`);
    }
  }
  console.log(`Images done. ${uploaded} uploaded, ${files.length - uploaded} already present.\n`);
}

/** Recursively rewrite "/media/..." url values to bare "media/..." keys. */
function rewriteUrls(node) {
  if (Array.isArray(node)) {
    node.forEach(rewriteUrls);
  } else if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) {
      if (k === "url" && typeof v === "string" && v.startsWith("/media/")) {
        node[k] = v.slice(1); // "/media/REG/x.webp" -> "media/REG/x.webp"
      } else {
        rewriteUrls(v);
      }
    }
  }
}

async function migrateContentJson() {
  const files = (await fs.readdir(CONTENT_DIR).catch(() => [])).filter((f) => f.endsWith(".json"));
  console.log(`Content JSON: ${files.length} file(s) under data/content/`);

  let changed = 0;
  for (const file of files) {
    const full = path.join(CONTENT_DIR, file);
    const raw = await fs.readFile(full, "utf-8");
    const doc = JSON.parse(raw);
    rewriteUrls(doc);
    const next = JSON.stringify(doc, null, 2) + "\n";
    if (next !== raw) {
      await fs.writeFile(full, next);
      changed++;
      console.log(`  rewrote ${file}`);
    }
  }
  console.log(`Content JSON done. ${changed} file(s) rewritten.`);
  if (changed) console.log(`Run "npm run import:content" to push the rewritten URLs into Mongo.\n`);
  else console.log();
}

async function main() {
  await migrateImages();
  await migrateContentJson();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
