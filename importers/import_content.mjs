#!/usr/bin/env node
/**
 * Import curated project content (the brochure layer) into `projectcontents`.
 *
 *   npm run import:content                     # every JSON in data/content/
 *   npm run import:content -- UPRERAPRJ125561  # one project
 *   npm run import:content -- --force          # import despite validation failures
 *
 * Each file is one project, named <REGISTRATION_NO>.json, upserted by
 * registration_no — re-running is idempotent and the file on disk stays the
 * source of truth. This is the whole publishing workflow: drop in a JSON file,
 * run this, and the project's page sections appear. No frontend change.
 *
 * Validation against schema/project-schema.json runs FIRST and blocks the
 * import on failure. A project with a missing area_basis or a transposed
 * SQ.M./SQ.FT. column should never reach the database.
 */
import fs from "node:fs/promises";
import fssync from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { connect, close, COL, ROOT } from "../lib/db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.join(ROOT, "data/content");
const VALIDATOR = path.resolve(__dirname, "../validate_content.mjs");

function validate(regs) {
  try {
    execFileSync(process.execPath, [VALIDATOR, ...regs], { stdio: "inherit" });
    return true;
  } catch {
    return false;
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const force = argv.includes("--force");
  const only = argv.filter((a) => !a.startsWith("-"));

  if (!fssync.existsSync(CONTENT_DIR)) {
    console.error(`No content directory at ${CONTENT_DIR}`);
    process.exit(1);
  }

  let files = (await fs.readdir(CONTENT_DIR)).filter((f) => f.endsWith(".json"));
  if (only.length) files = files.filter((f) => only.includes(path.basename(f, ".json")));

  if (!files.length) {
    console.log("Nothing to import.");
    return;
  }

  const regs = files.map((f) => path.basename(f, ".json"));
  const valid = validate(regs);
  if (!valid && !force) {
    console.error("\nValidation failed — nothing imported. Fix the content, or re-run with --force.");
    process.exit(1);
  }
  if (!valid) console.warn("\n--force: importing despite validation failures.\n");

  const { db } = await connect();
  const content = db.collection(COL.content);
  await content.createIndex({ registration_no: 1 }, { unique: true });
  await content.createIndex({ published: 1 });
  await content.createIndex({ slug: 1 });

  let ok = 0;
  for (const file of files) {
    let doc;
    try {
      doc = JSON.parse(await fs.readFile(path.join(CONTENT_DIR, file), "utf8"));
    } catch (err) {
      console.error(`  x ${file} — invalid JSON: ${err.message}`);
      continue;
    }

    const reg = doc.registration_no || path.basename(file, ".json");
    doc.registration_no = reg;

    // Content is meaningless without a RERA record to hang off — the detail
    // page is keyed on Project, so an orphan content doc would never render.
    const project = await db.collection(COL.projects).findOne({ registration_no: reg }, { projection: { has_detail: 1 } });
    if (!project) {
      console.error(`  x ${reg} — no matching project; run \`npm run import:rera\` first`);
      continue;
    }

    doc.updatedAt = new Date();
    await content.updateOne({ registration_no: reg }, { $set: doc, $setOnInsert: { createdAt: new Date() } }, { upsert: true });

    // `listed` gates the public listing (see ProjectRepository.buildFilter in
    // real_estate_backend) — publishing content is the other way (besides a
    // captured RERA detail page) a project earns a spot in it. Recomputed
    // rather than force-set true, so un-publishing a project with no captured
    // detail correctly drops it back out of the listing.
    await db.collection(COL.projects).updateOne(
      { registration_no: reg },
      { $set: { listed: Boolean(project.has_detail) || Boolean(doc.published) } }
    );

    const parts = [
      doc.unit_types?.length && `${doc.unit_types.length} unit types`,
      doc.pricing && "pricing",
      doc.amenities?.length && "amenities",
      doc.custom_sections?.length && `${doc.custom_sections.length} custom`,
    ].filter(Boolean);

    console.log(`  ok ${reg} — ${parts.join(", ") || "no sections"}${doc.published ? "" : "  (unpublished)"}`);
    ok++;
  }

  console.log(`\n${ok}/${files.length} imported.`);
  await close();
}

main().catch(async (err) => {
  console.error(err);
  await close();
  process.exit(1);
});
