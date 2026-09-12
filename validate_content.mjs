#!/usr/bin/env node
/**
 * Validate project content files against schema/project-schema.json.
 *
 *   node real_estate_scripts/validate_content.mjs                 # all content files
 *   node real_estate_scripts/validate_content.mjs UPRERAPRJ125561 # one project
 *   node real_estate_scripts/validate_content.mjs --checklist     # what to extract, per section
 *
 * Answers the question the master schema exists to answer: for THIS project,
 * what have we captured and what is still missing? The extraction method
 * differs per builder; the checklist does not.
 *
 * Deliberately dependency-free — it reads the x-importance / enum / pattern /
 * min / max keywords directly. Swap in ajv if full JSON Schema conformance is
 * ever needed; this covers what the workflow actually uses.
 *
 * Exit code 1 if any required field is missing, so it can gate a publish.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// This file lives at the repo root, same level as schema/ and data/.
const ROOT = __dirname;
const SCHEMA_PATH = path.join(ROOT, "schema/project-schema.json");
const CONTENT_DIR = path.join(ROOT, "data/content");

const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, "utf8"));

const C = { red: "\x1b[31m", yellow: "\x1b[33m", green: "\x1b[32m", dim: "\x1b[2m", bold: "\x1b[1m", off: "\x1b[0m" };

function deref(node) {
  if (node?.$ref?.startsWith("#/$defs/")) return schema.$defs[node.$ref.slice(8)];
  return node;
}

/**
 * Walk the schema and collect every field that carries an x-importance marker.
 * Array items are expanded per element of the actual document, so a missing
 * area table on unit_types[3] is reported against that specific unit.
 */
function collect(node, value, prefix, out) {
  node = deref(node);
  if (!node) return;

  if (node.type === "object" || node.properties) {
    for (const [key, rawChild] of Object.entries(node.properties || {})) {
      const child = deref(rawChild);
      const childPath = prefix ? `${prefix}.${key}` : key;
      const childValue = value == null ? undefined : value[key];
      const importance = rawChild["x-importance"] || child?.["x-importance"];

      if (importance) {
        out.push({ path: childPath, importance, node: child, raw: rawChild, value: childValue, present: isPresent(childValue) });
      }
      // Always descend into object subtrees, even absent ones — otherwise a
      // wholly missing parent (`scale`) silently hides its required children
      // (`scale.land_area`) from the report.
      if (child?.properties || isPresent(childValue)) {
        collect(child, childValue, childPath, out);
      }
    }
    return;
  }

  if (node.type === "array" && node.items) {
    if (!Array.isArray(value)) return;
    value.forEach((el, i) => collect(node.items, el, `${prefix}[${i}]`, out));
  }
}

function isPresent(v) {
  if (v === undefined || v === null || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return true;
}

// Light value checks — the ones that catch real transcription mistakes.
function checkValue(field) {
  const { node, value, path: p } = field;
  const errs = [];
  if (!isPresent(value)) return errs;

  if (node.enum && !node.enum.includes(value)) errs.push(`${p}: "${value}" not in [${node.enum.join(", ")}]`);
  if (node.pattern && typeof value === "string" && !new RegExp(node.pattern).test(value)) errs.push(`${p}: "${value}" fails /${node.pattern}/`);
  if (typeof value === "number") {
    if (node.minimum !== undefined && value < node.minimum) errs.push(`${p}: ${value} < minimum ${node.minimum}`);
    if (node.maximum !== undefined && value > node.maximum) errs.push(`${p}: ${value} > maximum ${node.maximum}`);
  }
  return errs;
}

// Cross-field rules that no per-field schema keyword can express.
function semanticChecks(doc) {
  const errs = [];
  (doc.unit_types || []).forEach((u, i) => {
    const a = u.areas || {};
    const tag = `unit_types[${i}] (${u.key || "?"})`;
    const [sup, bu, carp, balc] = [a.super?.sqft, a.built_up?.sqft, a.carpet?.sqft, a.balcony?.sqft];
    if (sup && bu && bu > sup) errs.push(`${tag}: built_up (${bu}) > super (${sup})`);
    if (bu && carp && carp > bu) errs.push(`${tag}: carpet (${carp}) > built_up (${bu})`);
    // The transposed SQ.M./SQ.FT. column mistake, which is common in print.
    if (a.balcony?.sqm && a.super?.sqm && a.balcony.sqm > a.super.sqm)
      errs.push(`${tag}: balcony sq.m. (${a.balcony.sqm}) exceeds super sq.m. (${a.super.sqm}) — columns likely transposed in the source`);
    if (balc && sup && balc > sup) errs.push(`${tag}: balcony (${balc}) > super (${sup})`);

    if (u.price?.indicative_total && u.price.bsp_per_sqft && sup) {
      const expected = u.price.bsp_per_sqft * sup;
      if (Math.abs(expected - u.price.indicative_total) > 1)
        errs.push(`${tag}: indicative_total ${u.price.indicative_total} != bsp × super (${expected})`);
    }
  });

  const keys = (doc.unit_types || []).map((u) => u.key);
  const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
  if (dupes.length) errs.push(`duplicate unit_types[].key: ${[...new Set(dupes)].join(", ")}`);

  // Every source_ref must resolve to a declared source.
  const sourceIds = new Set((doc.sources || []).map((s) => s.id));
  (doc.unit_types || []).forEach((u, i) => {
    const sid = u.source_ref?.source_id;
    if (sid && !sourceIds.has(sid)) errs.push(`unit_types[${i}]: source_ref.source_id "${sid}" not declared in sources[]`);
  });

  if (doc.pricing?.bsp && !doc.pricing?.area_basis) errs.push(`pricing: bsp present without area_basis — a rate with no basis is unusable`);

  return errs;
}

function printChecklist() {
  const fields = [];
  collect(schema, {}, "", fields);
  const bySection = {};
  const walk = (node, prefix) => {
    node = deref(node);
    for (const [key, rawChild] of Object.entries(node.properties || {})) {
      const child = deref(rawChild);
      const p = prefix ? `${prefix}.${key}` : key;
      const imp = rawChild["x-importance"] || child?.["x-importance"];
      const sec = rawChild["x-section"] || child?.["x-section"] || prefix.split(".")[0] || "—";
      if (imp) (bySection[sec] ||= []).push({ p, imp, src: rawChild["x-sources"] || child?.["x-sources"] || [] });
      if (child?.properties) walk(child, p);
      if (child?.type === "array" && child.items?.properties) walk(child.items, `${p}[]`);
    }
  };
  walk(schema, "");

  console.log(`\n${C.bold}Extraction checklist — schema v${schema["x-schema-version"]}${C.off}`);
  for (const [sec, list] of Object.entries(bySection)) {
    console.log(`\n${C.bold}${sec}${C.off}`);
    for (const f of list.sort((a, b) => a.imp.localeCompare(b.imp))) {
      const mark = f.imp === "required" ? `${C.red}●${C.off}` : f.imp === "recommended" ? `${C.yellow}●${C.off}` : `${C.dim}○${C.off}`;
      console.log(`  ${mark} ${f.p.padEnd(46)} ${C.dim}${f.src.join(", ")}${C.off}`);
    }
  }
  console.log(`\n${C.red}●${C.off} required   ${C.yellow}●${C.off} recommended   ${C.dim}○${C.off} optional\n`);
}

function validateFile(file) {
  const doc = JSON.parse(fs.readFileSync(file, "utf8"));
  const reg = doc.registration_no || path.basename(file, ".json");

  const fields = [];
  collect(schema, doc, "", fields);

  const missingRequired = fields.filter((f) => f.importance === "required" && !f.present);
  const missingRecommended = fields.filter((f) => f.importance === "recommended" && !f.present);
  const valueErrors = fields.flatMap(checkValue);
  const semantic = semanticChecks(doc);

  const scored = fields.filter((f) => f.importance !== "optional");
  const pct = scored.length ? Math.round((scored.filter((f) => f.present).length / scored.length) * 100) : 0;

  const ok = missingRequired.length === 0 && valueErrors.length === 0 && semantic.length === 0;
  console.log(`\n${C.bold}${reg}${C.off}  ${ok ? C.green + "PASS" : C.red + "FAIL"}${C.off}  ${C.dim}completeness ${pct}% (${scored.filter((f) => f.present).length}/${scored.length} required+recommended)${C.off}`);

  for (const f of missingRequired) console.log(`  ${C.red}missing (required)${C.off}    ${f.path}`);
  for (const f of missingRecommended) console.log(`  ${C.yellow}missing (recommended)${C.off} ${f.path}`);
  for (const e of valueErrors) console.log(`  ${C.red}invalid${C.off}               ${e}`);
  for (const e of semantic) console.log(`  ${C.red}inconsistent${C.off}          ${e}`);

  return ok;
}

const args = process.argv.slice(2);
if (args.includes("--checklist")) {
  printChecklist();
  process.exit(0);
}

const only = args.filter((a) => !a.startsWith("--"));
let files = fs.existsSync(CONTENT_DIR) ? fs.readdirSync(CONTENT_DIR).filter((f) => f.endsWith(".json")) : [];
if (only.length) files = files.filter((f) => only.includes(path.basename(f, ".json")));

if (!files.length) {
  console.log(`No content files found in ${CONTENT_DIR}`);
  process.exit(0);
}

const results = files.map((f) => validateFile(path.join(CONTENT_DIR, f)));
console.log(`\n${results.filter(Boolean).length}/${results.length} passing.\n`);
process.exit(results.every(Boolean) ? 0 : 1);
