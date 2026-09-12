#!/usr/bin/env node
/**
 * One-off migration: local disk images + Mongo GridFS documents -> R2.
 *
 * Run once when cutting over to Cloudflare R2 (see CLAUDE.md / docs for the
 * design). Idempotent — safe to re-run; putObject skips keys that already
 * exist, and content JSON rewrites are no-ops once already migrated.
 *
 * What it does NOT do: delete data/media/ or the GridFS `documents` bucket.
 * Verify the CDN serves everything correctly first, then remove those
 * yourself once you're confident — this script only ever adds.
 *
 * Usage:
 *   node migrate_to_r2.mjs            # images (data/media) + content JSON
 *   node migrate_to_r2.mjs --docs     # also: GridFS documents -> R2,
 *                                     # updates projects.documents[] in Mongo
 */
import fs from "node:fs/promises";
import path from "node:path";
import { GridFSBucket } from "mongodb";
import { connect, close, COL, ROOT } from "./lib/db.mjs";
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

async function migrateDocuments(db) {
  const bucket = new GridFSBucket(db, { bucketName: "documents" });
  const files = await bucket.find({}).toArray();
  console.log(`Documents: ${files.length} file(s) in GridFS`);

  const keyByGridfsId = new Map();
  for (const file of files) {
    const key = `documents/${file.filename}`;
    const chunks = [];
    await new Promise((resolve, reject) => {
      bucket
        .openDownloadStream(file._id)
        .on("data", (c) => chunks.push(c))
        .on("error", reject)
        .on("end", resolve);
    });
    const { uploaded } = await putObject(key, Buffer.concat(chunks), {
      contentType: file.metadata?.contentType || "application/pdf",
      contentDisposition: `inline; filename="${path.basename(file.filename)}"`,
    });
    keyByGridfsId.set(String(file._id), key);
    console.log(`  ${uploaded ? "ok  " : "skip"} ${key}`);
  }

  const projects = db.collection(COL.projects);
  const cursor = projects.find({ "documents.gridfs_id": { $exists: true } });
  let updated = 0;
  for await (const proj of cursor) {
    const documents = (proj.documents || []).map((d) => {
      if (!d.gridfs_id) return d;
      const key = keyByGridfsId.get(String(d.gridfs_id));
      const { gridfs_id, content_type, ...rest } = d;
      return key ? { ...rest, url: key } : d;
    });
    await projects.updateOne({ _id: proj._id }, { $set: { documents } });
    updated++;
  }
  console.log(`Documents done. ${files.length} uploaded/verified, ${updated} project(s) updated.\n`);
}

async function main() {
  const withDocs = process.argv.includes("--docs");

  await migrateImages();
  await migrateContentJson();

  if (withDocs) {
    const { db } = await connect();
    await migrateDocuments(db);
  }

  await close();
}

main().catch(async (err) => {
  console.error(err);
  await close();
  process.exit(1);
});
