#!/usr/bin/env node
/**
 * Import UP-RERA data into the `projects` collection.
 *
 * 1. Upserts the bare district project list (name/promoter/type only).
 * 2. Merges captured full-detail records, enriching the matching project by
 *    registration_no.
 * 3. Uploads each referenced document PDF to R2 under documents/<reg>/<file> —
 *    preferring an already-downloaded local file, falling back to the public
 *    blob_url. Idempotent: re-running skips keys that already exist in R2.
 *    Each document's stored `url` is the bare R2 key; the Java API composes
 *    the absolute CDN URL at read time (see CdnUrlResolver).
 *
 * Usage:
 *   npm run import:rera
 *   npm run import:rera -- --details ~/Downloads/uprera_details.json \
 *                          --files   ~/Downloads/UPRERA_files
 *
 * This collection is machine-owned and disposable — it can be rebuilt from
 * source at any time. Curated brochure content lives in `projectcontents` and
 * is never touched here. See CLAUDE.md, "The two data layers".
 */
import fs from "node:fs/promises";
import fssync from "node:fs";
import path from "node:path";
import { connect, close, COL, ROOT } from "../lib/db.mjs";
import { objectExists, putObject } from "../lib/r2.mjs";

function parseArgs() {
  const args = process.argv.slice(2);
  const out = {
    list: path.join(ROOT, "data/gautam_buddha_nagar_projects.json"),
    details: path.join(process.env.HOME, "Downloads/uprera_details.json"),
    files: path.join(process.env.HOME, "Downloads/UPRERA_files"),
  };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--list") out.list = args[++i];
    else if (args[i] === "--details") out.details = args[++i];
    else if (args[i] === "--files") out.files = args[++i];
  }
  return out;
}

const readJson = async (p) => JSON.parse(await fs.readFile(p, "utf-8"));

async function importList(db, listPath) {
  if (!fssync.existsSync(listPath)) {
    console.log(`(skip) list file not found: ${listPath}`);
    return;
  }
  const rows = await readJson(listPath);
  console.log(`List: ${rows.length} projects from ${path.basename(listPath)}`);

  const ops = rows
    .filter((r) => r.registration_no)
    .map((r) => ({
      updateOne: {
        filter: { registration_no: r.registration_no },
        update: {
          // `listed` gates the public listing (see ProjectRepository.buildFilter
          // in real_estate_backend) — a bare list-scrape row has no detail page
          // worth sending a visitor to yet. importDetails() below flips it true;
          // import_content.mjs flips it true independently when curated content
          // publishes. $setOnInsert only, so a re-run of the list scrape never
          // resets a project that has since gained detail or content.
          $setOnInsert: { has_detail: false, listed: false, createdAt: new Date() },
          $set: {
            sno: r.sno,
            project_name: r.project_name,
            promoter_name: r.promoter_name,
            district: r.district,
            project_type: r.project_type,
            updatedAt: new Date(),
          },
        },
        upsert: true,
      },
    }));

  const CHUNK = 500;
  for (let i = 0; i < ops.length; i += CHUNK) {
    await db.collection(COL.projects).bulkWrite(ops.slice(i, i + CHUNK), { ordered: false });
  }
  console.log(`List import done (${ops.length} upserts).`);
}

async function resolveDocument(regNo, doc, filesDir) {
  const fileName = doc.file_name;
  if (!fileName) return { ...doc, source: "missing" };

  const key = `documents/${regNo}/${fileName}`;

  if (await objectExists(key)) {
    return {
      document_name: doc.document_name,
      file_name: fileName,
      upload_type: doc.upload_type,
      uploaded_date: doc.uploaded_date,
      blob_url: doc.blob_url,
      url: key,
      source: "existing",
    };
  }

  const localPath = path.join(filesDir, regNo, fileName);
  let buffer = null;
  let source = "missing";
  let contentType = "application/pdf";

  if (fssync.existsSync(localPath)) {
    buffer = await fs.readFile(localPath);
    source = "local";
  } else if (doc.blob_url) {
    try {
      const res = await fetch(doc.blob_url);
      if (res.ok) {
        buffer = Buffer.from(await res.arrayBuffer());
        contentType = res.headers.get("content-type") || contentType;
        source = "blob";
      } else {
        console.warn(`  FAIL ${regNo}/${fileName} HTTP ${res.status}`);
      }
    } catch (e) {
      console.warn(`  FAIL ${regNo}/${fileName} ${e.message}`);
    }
  }

  let url, size;
  if (buffer) {
    await putObject(key, buffer, { contentType, contentDisposition: `inline; filename="${fileName}"` });
    url = key;
    size = buffer.length;
    console.log(`  ok   ${key} (${size} bytes, ${source})`);
  }

  return { document_name: doc.document_name, file_name: fileName, upload_type: doc.upload_type, uploaded_date: doc.uploaded_date, blob_url: doc.blob_url, url, content_type: contentType, size, source };
}

function mapDetailRecord(rec) {
  const d = rec.detail || {};
  return {
    registration_no: rec.registration_no,
    project_name: rec.project_name,
    promoter_name: rec.promoter_name,
    district: rec.district,
    project_type: rec.project_type,
    project_category: d.project_category,
    state: rec.state,
    tehsil: rec.tehsil,
    project_address: rec.project_address,
    source_url: rec.source_url || d.source_url,
    form_id: rec.form_id,
    registration_date: rec.registration_date,
    declared_completion_date: rec.declared_completion_date,
    proposed_start_date: rec.proposed_start_date,
    project_duration_months: d.project_duration_months,
    proposed_period_months: rec.proposed_period_months,
    registration_fee: d.registration_fee,
    coordinator_number: rec.coordinator_number,
    project_complaints: rec.project_complaints,
    helpline_number: rec.helpline_number,
    promoter: {
      id: d.promoter_id,
      id_raw: d.promoter_id_raw,
      name: rec.promoter_name,
      applicant_type: rec.promoter_applicant_type,
      mobile: rec.promoter_mobile,
      email: rec.promoter_email,
      address: rec.promoter_address,
      chairman_address: rec.promoter_chairman_address,
      total_projects: rec.promoter_total_projects,
      total_complaints: rec.promoter_total_complaints,
    },
    co_promoters: rec.co_promoters || [],
    detail: {
      agents: d.agents || [],
      permits: d.permits || [],
      development_works: d.development_works || [],
      account_collection: d.account_collection || [],
      account_separate: d.account_separate || [],
      account_transaction: d.account_transaction || [],
      land_details: d.land_details || [],
      land_documents: d.land_documents || [],
      khasra: d.khasra || [],
      plan_details: d.plan_details || [],
      registry_agreements: d.registry_agreements || [],
    },
    has_detail: true,
    listed: true,
    captured_at: rec.captured_at ? new Date(rec.captured_at) : undefined,
    updatedAt: new Date(),
  };
}

async function importDetails(db, detailsPath, filesDir) {
  if (!fssync.existsSync(detailsPath)) {
    console.log(`(skip) details file not found: ${detailsPath}`);
    return;
  }
  const records = await readJson(detailsPath);
  console.log(`Details: ${records.length} captured record(s) from ${path.basename(detailsPath)}`);

  for (const rec of records) {
    if (!rec.registration_no) continue;
    const rawDocs = rec.detail?.documents || [];
    const documents = [];
    for (const doc of rawDocs) documents.push(await resolveDocument(rec.registration_no, doc, filesDir));

    const mapped = mapDetailRecord(rec);
    mapped.documents = documents;

    await db.collection(COL.projects).updateOne(
      { registration_no: rec.registration_no },
      { $set: mapped, $setOnInsert: { createdAt: new Date() } },
      { upsert: true }
    );
    console.log(`  merged detail: ${rec.registration_no} — ${rec.project_name} (${documents.length} documents)`);
  }
}

async function ensureIndexes(db) {
  const c = db.collection(COL.projects);
  await c.createIndex({ registration_no: 1 }, { unique: true });
  await c.createIndex({ district: 1 });
  await c.createIndex({ project_type: 1 });
  await c.createIndex({ has_detail: -1, project_name: 1 });
  await c.createIndex({ listed: 1 });
  await c.createIndex({ project_name: "text", promoter_name: "text", registration_no: "text" });
}

async function main() {
  const { list, details, files } = parseArgs();
  const { db } = await connect();
  console.log(`Connected to ${db.databaseName}`);

  await ensureIndexes(db);
  await importList(db, list);
  await importDetails(db, details, files);

  const col = db.collection(COL.projects);
  const [total, withDetail, listed] = await Promise.all([
    col.countDocuments(),
    col.countDocuments({ has_detail: true }),
    col.countDocuments({ listed: true }),
  ]);
  console.log(`\nDone. projects: ${total} total, ${withDetail} with full detail, ${listed} listed.`);

  await close();
}

main().catch(async (err) => {
  console.error(err);
  await close();
  process.exit(1);
});
