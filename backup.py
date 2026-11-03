#!/usr/bin/env python3
"""Back up Pipedrive CRM data into a single timestamped ZIP archive.

Archive layout (pipedrive_backup_YYYY-MM-DD_HHMMSS.zip):
    manifest.json                  run metadata and per-endpoint record counts
    json/{endpoint}.json           complete raw API records (full-fidelity restore)
    json/fields/{entity}Fields.json  custom-field definitions (hash key -> name/options)
    csv/{deals,persons,organizations}.csv  flattened tables for Pipedrive's importer

Environment:
    PIPEDRIVE_API_TOKEN   required; API token of the backing-up user
    PIPEDRIVE_BASE_URL    optional; defaults to https://api.pipedrive.com/v1
    BACKUP_OUTPUT_DIR     optional; directory for the ZIP (default: current dir)

Exit codes: 0 on success, 1 on any unrecoverable error.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_VERSION = "1.0.0"
API_TOKEN_ENV = "PIPEDRIVE_API_TOKEN"
DEFAULT_BASE_URL = "https://api.pipedrive.com/v1"

PAGE_LIMIT = 500  # Pipedrive's maximum page size for v1 list endpoints
REQUEST_TIMEOUT = (10, 90)  # (connect, read) seconds
RETRY_STATUSES = (429, 500, 502, 503, 504)

ENDPOINTS = ("deals", "persons", "organizations", "notes", "pipelines", "stages")

# Extra list parameters per endpoint. Sorting by id keeps offset pagination
# stable-ish while records are being created/edited during the backup.
ENDPOINT_PARAMS: dict[str, dict[str, Any]] = {
    "deals": {"status": "all_not_deleted", "sort": "id ASC"},
    "persons": {"sort": "id ASC"},
    "organizations": {"sort": "id ASC"},
    "notes": {"sort": "id ASC"},
}

# Entities exported as importer-ready CSV, mapped to their field-definition endpoint.
CSV_ENTITIES = {
    "deals": "dealFields",
    "persons": "personFields",
    "organizations": "organizationFields",
}

CUSTOM_KEY_RE = re.compile(r"[0-9a-f]{40}")
CUSTOM_PREFIX_RE = re.compile(r"^([0-9a-f]{40})(.*)$")

log = logging.getLogger("pipedrive_backup")


class BackupError(Exception):
    """Unrecoverable failure; the run must exit non-zero."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_session(token: str) -> requests.Session:
    retry = Retry(
        total=8,
        connect=5,
        read=5,
        status=8,
        backoff_factor=2,  # 0, 4, 8, 16, ... seconds (capped by backoff_max)
        backoff_max=120,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,  # honoured for 429 and 503
        raise_on_status=False,  # hand back the final response so we can report it
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    # Header auth keeps the token out of URLs, and therefore out of logs and tracebacks.
    session.headers.update(
        {
            "x-api-token": token,
            "Accept": "application/json",
            "User-Agent": f"pipedrive-backup/{SCRIPT_VERSION}",
        }
    )
    return session


def api_get(session: requests.Session, base_url: str, path: str, params: dict[str, Any]) -> dict:
    try:
        resp = session.get(f"{base_url}/{path}", params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise BackupError(f"GET /{path} failed after retries: {type(exc).__name__}: {exc}") from exc

    if resp.status_code == 401:
        raise BackupError(f"GET /{path}: HTTP 401 - {API_TOKEN_ENV} is invalid, expired or revoked")
    if resp.status_code == 403:
        raise BackupError(f"GET /{path}: HTTP 403 - token lacks permission for this resource")
    if not resp.ok:
        raise BackupError(f"GET /{path}: HTTP {resp.status_code} after retries: {resp.text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise BackupError(f"GET /{path}: response is not valid JSON: {resp.text[:300]}") from exc
    if not payload.get("success", False):
        raise BackupError(f"GET /{path}: API returned success=false: {payload.get('error')}")
    return payload


def fetch_all(
    session: requests.Session, base_url: str, path: str, extra_params: dict[str, Any] | None = None
) -> tuple[list[dict], int]:
    """Walk start/limit pagination until more_items_in_collection is false."""
    records: list[dict] = []
    start = 0
    pages = 0
    while True:
        params = {"start": start, "limit": PAGE_LIMIT, **(extra_params or {})}
        payload = api_get(session, base_url, path, params)
        pages += 1

        # Empty collections come back as "data": null, not [].
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise BackupError(f"/{path}: expected a list in 'data', got {type(data).__name__}")
        records.extend(data)

        pagination = (payload.get("additional_data") or {}).get("pagination") or {}
        log.debug("endpoint=%s page=%d start=%d got=%d", path, pages, start, len(data))
        # Some endpoints (pipelines, stages) may omit pagination entirely: one page = everything.
        if not pagination.get("more_items_in_collection"):
            return records, pages

        next_start = pagination.get("next_start")
        if not isinstance(next_start, int) or next_start <= start:
            raise BackupError(
                f"/{path}: API reports more items but next_start={next_start!r} "
                f"does not advance past start={start}; aborting to avoid an incomplete or endless loop"
            )
        start = next_start


def dedupe_by_id(records: list[dict], endpoint: str) -> list[dict]:
    """Offset pagination can repeat a record if rows shift mid-run; keep the latest copy."""
    if not records or any("id" not in r for r in records):
        return records
    by_id: dict[Any, dict] = {}
    for rec in records:
        by_id[rec["id"]] = rec
    dropped = len(records) - len(by_id)
    if dropped:
        log.warning("endpoint=%s duplicates_removed=%d (data changed during pagination)", endpoint, dropped)
    return list(by_id.values())


# --------------------------------------------------------------------------- #
# Flattening for the Pipedrive spreadsheet importer
# --------------------------------------------------------------------------- #
def build_field_lookup(fields: list[dict]) -> dict[str, dict[str, Any]]:
    """Map 40-char custom-field hash keys to their display name, type and option labels."""
    lookup: dict[str, dict[str, Any]] = {}
    for field in fields:
        key = field.get("key") or ""
        if not CUSTOM_KEY_RE.fullmatch(key):
            continue
        options = {str(o["id"]): o.get("label") for o in field.get("options") or [] if "id" in o}
        lookup[key] = {"name": field.get("name") or key, "type": field.get("field_type"), "options": options}
    return lookup


def prepare_record(record: dict) -> dict:
    """Collapse Pipedrive's reference objects and contact lists into importer-friendly scalars."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, dict) and "value" in value:
            # person_id/org_id/user_id/picture_id and person/org/user custom fields:
            # {"value": 123, "name": "...", ...} -> the ID itself, plus the name for humans.
            out[key] = value.get("value")
            if value.get("name") is not None:
                out[f"{key}.name"] = value["name"]
        elif isinstance(value, list):
            if not value:
                out[key] = None
            elif all(isinstance(item, dict) and "value" in item for item in value):
                # email/phone/im: [{"label": "work", "value": "...", "primary": true}, ...]
                primary = next((item for item in value if item.get("primary")), value[0])
                out[key] = primary.get("value")
                if len(value) > 1:
                    out[f"{key}.all"] = "; ".join(str(item["value"]) for item in value if item.get("value"))
            else:
                out[key] = json.dumps(value, ensure_ascii=False)
        else:
            out[key] = value  # scalars, and plain dicts left for json_normalize
    return out


def _option_labels(value: Any, options: dict[str, str]) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return value
    ids = [part.strip() for part in str(value).split(",")]
    return ", ".join(str(options.get(i, i)) for i in ids)


def build_csv_frame(records: list[dict], field_lookup: dict[str, dict[str, Any]]) -> pd.DataFrame:
    df = pd.json_normalize([prepare_record(r) for r in records], sep=".")

    # Anything still nested (lists inside sub-objects) becomes a JSON string cell.
    for col in df.columns[df.dtypes == object]:
        df[col] = df[col].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)

    # Single/multi-option custom fields store option IDs; the importer expects labels.
    for key, meta in field_lookup.items():
        if key in df.columns and meta["options"] and meta["type"] in ("enum", "set"):
            df[key] = df[key].map(lambda v, opts=meta["options"]: _option_labels(v, opts))

    # Rename hash columns (incl. composite sub-columns like <hash>_currency) to field names.
    taken = set(df.columns)
    renames: dict[str, str] = {}
    for col in df.columns:
        match = CUSTOM_PREFIX_RE.match(col)
        if not match or match.group(1) not in field_lookup:
            continue
        new_name = field_lookup[match.group(1)]["name"] + match.group(2)
        if new_name in taken:
            new_name = f"{new_name} [{match.group(1)[:8]}]"
        taken.add(new_name)
        renames[col] = new_name
    df = df.rename(columns=renames)

    if "id" in df.columns:
        df = df[["id"] + [c for c in df.columns if c != "id"]]
    return df


# --------------------------------------------------------------------------- #
# Archive writing (streamed straight into the ZIP; no temp files)
# --------------------------------------------------------------------------- #
def write_json(zf: zipfile.ZipFile, arcname: str, obj: Any) -> zipfile.ZipInfo:
    with io.TextIOWrapper(zf.open(arcname, "w", force_zip64=True), encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return zf.getinfo(arcname)


def write_csv(zf: zipfile.ZipFile, arcname: str, df: pd.DataFrame) -> zipfile.ZipInfo:
    # utf-8-sig so Excel/Sheets render non-ASCII names correctly when users open the file.
    with io.TextIOWrapper(zf.open(arcname, "w", force_zip64=True), encoding="utf-8-sig", newline="") as fh:
        if not df.empty:
            df.to_csv(fh, index=False, lineterminator="\n")
    return zf.getinfo(arcname)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"


def backup_endpoint(
    zf: zipfile.ZipFile, session: requests.Session, base_url: str, endpoint: str
) -> dict[str, Any]:
    started = time.monotonic()
    records, pages = fetch_all(session, base_url, endpoint, ENDPOINT_PARAMS.get(endpoint))
    records = dedupe_by_id(records, endpoint)
    fetch_secs = time.monotonic() - started

    json_info = write_json(zf, f"json/{endpoint}.json", records)
    summary: dict[str, Any] = {
        "records": len(records),
        "pages": pages,
        "json_bytes": json_info.file_size,
    }

    csv_note = ""
    if endpoint in CSV_ENTITIES:
        fields_endpoint = CSV_ENTITIES[endpoint]
        fields, _ = fetch_all(session, base_url, fields_endpoint)
        write_json(zf, f"json/fields/{fields_endpoint}.json", fields)
        df = build_csv_frame(records, build_field_lookup(fields))
        csv_info = write_csv(zf, f"csv/{endpoint}.csv", df)
        summary.update(csv_bytes=csv_info.file_size, csv_columns=len(df.columns))
        csv_note = f" csv={human_size(csv_info.file_size)} csv_columns={len(df.columns)}"

    log.info(
        "endpoint=%s records=%d pages=%d fetch_time=%.1fs total_time=%.1fs json=%s (zipped %s)%s",
        endpoint,
        len(records),
        pages,
        fetch_secs,
        time.monotonic() - started,
        human_size(json_info.file_size),
        human_size(json_info.compress_size),
        csv_note,
    )
    return summary


def run() -> Path:
    token = os.environ.get(API_TOKEN_ENV, "").strip()
    if not token:
        raise BackupError(
            f"Environment variable {API_TOKEN_ENV} is not set or empty. "
            "In GitHub Actions, add it as a repository secret and pass it via `env:`."
        )
    base_url = os.environ.get("PIPEDRIVE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    output_dir = Path(os.environ.get("BACKUP_OUTPUT_DIR", ".")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    run_started = datetime.now(timezone.utc)
    final_path = output_dir / f"pipedrive_backup_{run_started:%Y-%m-%d_%H%M%S}.zip"
    partial_path = final_path.with_name(final_path.name + ".partial")
    log.info("backup_start base_url=%s output=%s", base_url, final_path)

    session = build_session(token)
    t0 = time.monotonic()
    try:
        with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            results = {ep: backup_endpoint(zf, session, base_url, ep) for ep in ENDPOINTS}
            write_json(
                zf,
                "manifest.json",
                {
                    "script_version": SCRIPT_VERSION,
                    "started_at_utc": run_started.isoformat(),
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "base_url": base_url,
                    "endpoints": results,
                },
            )
        # Only a fully written archive ever gets the final name.
        partial_path.replace(final_path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise
    finally:
        session.close()

    total_records = sum(r["records"] for r in results.values())
    log.info(
        "backup_complete file=%s size=%s total_records=%d elapsed=%.1fs",
        final_path.name,
        human_size(final_path.stat().st_size),
        total_records,
        time.monotonic() - t0,
    )

    # Expose the path to later workflow steps (e.g. actions/upload-artifact).
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write(f"backup_path={final_path}\n")
    return final_path


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime
    try:
        run()
    except BackupError as exc:
        log.error("backup_failed reason=%s", exc)
        return 1
    except Exception:
        log.exception("backup_failed reason=unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
