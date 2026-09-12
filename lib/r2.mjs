/**
 * Cloudflare R2 access — S3-compatible, so the AWS SDK talks to it directly.
 *
 * Object keys are the only thing that ends up in Mongo / data/content/*.json
 * (see schema/project-schema.json media_ref). The bucket, endpoint, and CDN
 * domain live only here and in real_estate_backend's app.cdn-base-url —
 * switching storage providers later means changing those two places, not
 * touching stored content.
 */
import { S3Client, PutObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";
import { loadEnv } from "./db.mjs";

let client;

function env(name) {
  loadEnv();
  const value = process.env[name];
  if (!value) throw new Error(`${name} not set. Add it to .env at the repo root.`);
  return value;
}

export function r2Client() {
  if (client) return client;
  const accountId = env("R2_ACCOUNT_ID");
  client = new S3Client({
    region: "auto",
    endpoint: `https://${accountId}.r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId: env("ACCESS_KEY"),
      secretAccessKey: env("SECRET_ACCESS_KEY"),
    },
  });
  return client;
}

export function r2Bucket() {
  return env("R2_BUCKET");
}

/** True if `key` already exists in the bucket — makes uploads idempotent. */
export async function objectExists(key) {
  try {
    await r2Client().send(new HeadObjectCommand({ Bucket: r2Bucket(), Key: key }));
    return true;
  } catch (e) {
    if (e.$metadata?.httpStatusCode === 404 || e.name === "NotFound") return false;
    throw e;
  }
}

/**
 * Uploads a buffer to `key`, skipping if it already exists (re-running an
 * ingest is idempotent — same behaviour as the old GridFS upload path).
 * Returns { key, uploaded: boolean }.
 */
export async function putObject(key, buffer, { contentType, contentDisposition } = {}) {
  if (await objectExists(key)) return { key, uploaded: false };

  await r2Client().send(
    new PutObjectCommand({
      Bucket: r2Bucket(),
      Key: key,
      Body: buffer,
      ContentType: contentType,
      ContentDisposition: contentDisposition,
    })
  );
  return { key, uploaded: true };
}
