"""Shared MongoDB + R2 helpers for the UP-RERA pipeline.

Used by scrape_uprera.py (list upsert), process_details.py (the per-project
streaming fetch+import), and import_rera.py (the older offline/file-based
batch import). Keeping this in one place means those three scripts can't
drift out of sync on how a record is shaped or how a document is resolved.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
COL_PROJECTS = "projects"


# ---------------------------------------------------------------------------
# .env — same minimal reader as lib/db.mjs: KEY=VALUE lines, first one wins,
# existing process env vars are never overridden.
# ---------------------------------------------------------------------------
def load_env() -> dict:
    import os
    import re

    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip().strip("'\"")
    return os.environ


def required_env(env: dict, name: str) -> str:
    value = env.get(name)
    if not value:
        raise SystemExit(f"{name} not set. Copy .env.example to .env at the repo root and fill it in.")
    return value


# ---------------------------------------------------------------------------
# Mongo
# ---------------------------------------------------------------------------
def connect_mongo(env: dict):
    from pymongo import MongoClient

    uri = required_env(env, "MONGODB_URI")
    client = MongoClient(uri)
    db = client.get_default_database()
    return client, db


def ensure_indexes(db) -> None:
    col = db[COL_PROJECTS]
    col.create_index("registration_no", unique=True)
    col.create_index("district")
    col.create_index("project_type")
    col.create_index([("has_detail", -1), ("project_name", 1)])
    col.create_index("listed")
    col.create_index(
        [("project_name", "text"), ("promoter_name", "text"), ("registration_no", "text")]
    )


# ---------------------------------------------------------------------------
# R2 (S3-compatible)
# ---------------------------------------------------------------------------
def r2_client(env: dict):
    import boto3

    account_id = required_env(env, "R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=required_env(env, "ACCESS_KEY"),
        aws_secret_access_key=required_env(env, "SECRET_ACCESS_KEY"),
    )


def r2_object_exists(s3, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise


def r2_put_object(s3, bucket: str, key: str, data: bytes, content_type: str, filename: str) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        ContentDisposition=f'inline; filename="{filename}"',
    )


# ---------------------------------------------------------------------------
# The bare district list -> Mongo
# ---------------------------------------------------------------------------
def upsert_list_rows(db, rows: list[dict]) -> int:
    """Upsert bare list rows (sno/project_name/promoter_name/district/project_type)
    by registration_no. `has_detail`/`listed` are set to False only on insert
    (see mapDetailRecord-equivalent below for when they flip to True), so this
    never downgrades a project that already has full detail."""
    from pymongo import UpdateOne

    rows = [r for r in rows if r.get("registration_no")]
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    ops = [
        UpdateOne(
            {"registration_no": r["registration_no"]},
            {
                # `listed` gates the public listing (see ProjectRepository.buildFilter
                # in real_estate_backend) — a bare list-scrape row has no detail page
                # worth sending a visitor to yet.
                "$setOnInsert": {"has_detail": False, "listed": False, "createdAt": now},
                "$set": {
                    "sno": r.get("sno"),
                    "project_name": r.get("project_name"),
                    "promoter_name": r.get("promoter_name"),
                    "district": r.get("district"),
                    "project_type": r.get("project_type"),
                    "updatedAt": now,
                },
            },
            upsert=True,
        )
        for r in rows
    ]

    CHUNK = 500
    for i in range(0, len(ops), CHUNK):
        db[COL_PROJECTS].bulk_write(ops[i : i + CHUNK], ordered=False)
    return len(ops)


# ---------------------------------------------------------------------------
# One project's documents -> R2
# ---------------------------------------------------------------------------
def resolve_document(s3, bucket: str, reg_no: str, doc: dict, files_dir: Path, dry_run: bool) -> dict:
    """Upload one document to R2 (or reuse it if already there), preferring an
    already-downloaded local file under files_dir/<reg_no>/<file_name>, falling
    back to the document's public blob_url."""
    file_name = doc.get("file_name")
    if not file_name:
        return {**doc, "source": "missing"}

    key = f"documents/{reg_no}/{file_name}"

    if not dry_run and r2_object_exists(s3, bucket, key):
        return {
            "document_name": doc.get("document_name"),
            "file_name": file_name,
            "upload_type": doc.get("upload_type"),
            "uploaded_date": doc.get("uploaded_date"),
            "blob_url": doc.get("blob_url"),
            "url": key,
            "source": "existing",
        }

    local_path = files_dir / reg_no / file_name
    buffer: bytes | None = None
    source = "missing"
    content_type = "application/pdf"

    if local_path.exists():
        buffer = local_path.read_bytes()
        source = "local"
    elif doc.get("blob_url"):
        try:
            resp = requests.get(doc["blob_url"], timeout=60)
            if resp.ok:
                buffer = resp.content
                content_type = resp.headers.get("content-type", content_type)
                source = "blob"
            else:
                print(f"  FAIL {reg_no}/{file_name} HTTP {resp.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  FAIL {reg_no}/{file_name} {e}", file=sys.stderr)

    url = None
    size = None
    if buffer is not None:
        if dry_run:
            print(f"  [dry-run] would upload {key} ({len(buffer)} bytes, {source})")
        else:
            r2_put_object(s3, bucket, key, buffer, content_type, file_name)
            print(f"  ok   {key} ({len(buffer)} bytes, {source})")
        url = key
        size = len(buffer)

    return {
        "document_name": doc.get("document_name"),
        "file_name": file_name,
        "upload_type": doc.get("upload_type"),
        "uploaded_date": doc.get("uploaded_date"),
        "blob_url": doc.get("blob_url"),
        "url": url,
        "content_type": content_type,
        "size": size,
        "source": source,
    }


# ---------------------------------------------------------------------------
# One project's parsed pages -> the flat document stored in Mongo
# ---------------------------------------------------------------------------
def map_detail_record(rec: dict) -> dict:
    """Flatten a {registration_no, district, summary, viewprojects} record
    (fetch_details.py's / process_details.py's shape) into the flat document
    importers/import_rera.mjs's mapDetailRecord() used to write."""
    s = rec.get("summary") or {}
    v = rec.get("viewprojects") or {}

    if s.get("registration_no") and v.get("registration_no") and s["registration_no"] != v["registration_no"]:
        print(
            f"  WARN {rec.get('registration_no')}: summary/viewprojects registration_no mismatch "
            f"({s['registration_no']!r} vs {v['registration_no']!r})",
            file=sys.stderr,
        )

    return {
        "registration_no": rec.get("registration_no") or s.get("registration_no") or v.get("registration_no"),
        "project_name": s.get("project_name") or v.get("project_name"),
        "promoter_name": s.get("promoter_name") or v.get("promoter_name"),
        "district": rec.get("district") or s.get("district"),
        "project_type": s.get("project_type") or v.get("project_type"),
        "project_category": v.get("project_category"),
        "state": s.get("state"),
        "tehsil": s.get("tehsil"),
        "project_address": s.get("project_address"),
        "form_id": s.get("form_id") or v.get("form_id"),
        "registration_date": s.get("registration_date") or v.get("registration_date"),
        "declared_completion_date": s.get("declared_completion_date"),
        "proposed_start_date": s.get("proposed_start_date"),
        "project_duration_months": v.get("project_duration_months"),
        "proposed_period_months": s.get("proposed_period_months"),
        "registration_fee": v.get("registration_fee"),
        "coordinator_number": s.get("coordinator_number"),
        "coordinator_number_contactbox": s.get("coordinator_number_contactbox"),
        "project_complaints": s.get("project_complaints"),
        "helpline_number": s.get("helpline_number"),
        "promoter": {
            "id": v.get("promoter_id"),
            "id_raw": v.get("promoter_id_raw"),
            "name": s.get("promoter_name") or v.get("promoter_name"),
            "applicant_type": s.get("promoter_applicant_type"),
            "mobile": s.get("promoter_mobile"),
            "mobile_contactbox": s.get("promoter_mobile_contactbox"),
            "email": s.get("promoter_email"),
            "address": s.get("promoter_address"),
            "chairman_address": s.get("promoter_chairman_address"),
            "total_projects": s.get("promoter_total_projects"),
            "total_complaints": s.get("promoter_total_complaints"),
        },
        "co_promoters": s.get("co_promoters") or [],
        "detail": {
            "agents": v.get("agents") or [],
            "permits": v.get("permits") or [],
            "development_works": v.get("development_works") or [],
            "account_collection": v.get("account_collection") or [],
            "account_separate": v.get("account_separate") or [],
            "account_transaction": v.get("account_transaction") or [],
            "land_details": v.get("land_details") or [],
            "land_documents": v.get("land_documents") or [],
            "khasra": v.get("khasra") or [],
            "plan_details": v.get("plan_details") or [],
            "registry_agreements": v.get("registry_agreements") or [],
        },
        "has_detail": True,
        "listed": True,
    }
