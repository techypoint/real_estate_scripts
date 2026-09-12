/**
 * Mongo access for scripts — raw driver, no ODM.
 *
 * The authoritative read model is the Java backend. Defining Mongoose schemas
 * here as well would mean two definitions of every document that could silently
 * disagree. Scripts write plain objects; `schema/project-schema.json` is what
 * validates their shape (see validate_content.mjs).
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MongoClient } from "mongodb";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// This repo's own root — schema/ and data/ live directly inside it (moved
// here from the old real_estate_website monorepo when the three areas split
// into separate repos).
export const ROOT = path.resolve(__dirname, "..");

let envLoaded = false;

/** Minimal .env reader — avoids a dotenv dependency. Loads every key, not just Mongo's. */
export function loadEnv() {
  if (envLoaded) return;
  envLoaded = true;
  const file = path.join(ROOT, ".env");
  if (fs.existsSync(file)) {
    for (const line of fs.readFileSync(file, "utf8").split("\n")) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
    }
  }
}

let client;

export async function connect() {
  loadEnv();
  if (!process.env.MONGODB_URI) {
    throw new Error("MONGODB_URI not set. Copy .env.example to .env at the repo root and fill it in.");
  }
  client = new MongoClient(process.env.MONGODB_URI);
  await client.connect();
  const db = client.db();
  return { db, client };
}

export async function close() {
  await client?.close();
}

export const COL = {
  projects: "projects",
  content: "projectcontents",
  assets: "projectassets",
};
