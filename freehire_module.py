from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Query
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

DB_PATH = "ats_finder.db"
DEFAULT_API_URL = "https://freehire.me"
ATS_NAME = "freehire"
PAGE_SIZE = 100

EXPERIENCE_PATTERNS = (
    ("0-1", re.compile(r"\b0\s*[-–]\s*1\b|\b0\b.*\byear|\bfresher\b|\bentry.?level\b|\bintern\b", re.I)),
    ("1-2", re.compile(r"\b1\s*[-–]\s*2\b|\b1\s*[-–]\s*3\b|\b1\b.*\byear", re.I)),
    ("2-3", re.compile(r"\b2\s*[-–]\s*3\b|\b2\s*[-–]\s*4\b|\b2\b.*\byear|\b3\b.*\byear", re.I)),
    ("3+", re.compile(r"\b[3-9]\s*[-–]\s*\d+\b|\b[3-9]\+?\s*year|\b1[0-9]\b.*\byear|\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b", re.I)),
)
EXPERIENCE_BOUNDS: dict[str, tuple[int, int | None]] = {
    "0-1": (0, 1),
    "1-2": (1, 2),
    "2-3": (2, 3),
    "3+": (3, None),
}

router = APIRouter(prefix="/api/freehire", tags=["freehire"])
sync_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "fetched_jobs": 0,
    "saved_companies": 0,
    "error": None,
}


def _api_urls() -> tuple[str, str]:
    configured = os.getenv("FREEHIRE_API_URL", DEFAULT_API_URL).strip().rstrip("/") or DEFAULT_API_URL
    search_suffix = "/api/v1/jobs/search"
    jobs_suffix = "/api/v1/jobs"
    if configured.endswith(search_suffix):
        return configured, configured[: -len("/search")]
    if configured.endswith(jobs_suffix):
        return configured[: -len("jobs")] + "jobs/search", configured
    return configured + search_suffix, configured + jobs_suffix


def _extract_jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("data", "jobs", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                try:
                    return _extract_jobs(value)
                except ValueError:
                    continue
        if rows is None:
            raise ValueError("Freehire API response did not contain a jobs list")
    else:
        raise ValueError("Freehire API returned an unsupported response")
    return [row for row in rows if isinstance(row, dict)]


def _remote_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "remote"}


def _is_remote(job: dict[str, Any], remote_only: bool) -> bool:
    if remote_only:
        return True
    for key in ("is_remote", "remote", "remote_only"):
        if key in job and _remote_value(job[key]):
            return True
    for key in ("workplace_type", "workplace", "location_type"):
        if "remote" in str(job.get(key) or "").lower():
            return True
    if "remote" in str(job.get("location") or "").lower():
        return True
    return False


def _format_location(job: dict[str, Any], remote: bool) -> str:
    location = job.get("location")
    if isinstance(location, dict):
        components = [location.get(key) for key in ("city", "region", "state", "country", "name")]
        location_text = ", ".join(str(part).strip() for part in components if part)
    elif isinstance(location, list):
        location_text = ", ".join(str(part).strip() for part in location if part)
    else:
        location_text = str(location or "").strip()

    if not location_text:
        cities = job.get("cities")
        location_text = ", ".join(str(city).strip() for city in cities if city) if isinstance(cities, list) else ""
    if not location_text:
        location_text = "Worldwide" if remote else "Unspecified"
    if remote and not location_text.lower().endswith("(remote)"):
        location_text += " (Remote)"
    return location_text


def _experience_fields(job: dict[str, Any]) -> tuple[int | None, int | None, str]:
    text_parts = [
        str(job.get("experience") or ""),
        str(job.get("experience_level") or ""),
        str(job.get("title") or ""),
        re.sub(r"<[^>]*>", " ", str(job.get("description") or "")),
    ]
    text = html.unescape(" ".join(text_parts))
    label = next((name for name, pattern in EXPERIENCE_PATTERNS if pattern.search(text)), "")

    explicit_min = job.get("experience_min", job.get("exp_min"))
    explicit_max = job.get("experience_max", job.get("exp_max"))
    try:
        exp_min = int(float(explicit_min)) if explicit_min is not None else None
    except (TypeError, ValueError):
        exp_min = None
    try:
        exp_max = int(float(explicit_max)) if explicit_max is not None else None
    except (TypeError, ValueError):
        exp_max = None

    if not label and (exp_min is not None or exp_max is not None):
        if exp_max is not None and exp_max <= 1:
            label = "0-1"
        elif exp_min is not None and exp_min >= 3:
            label = "3+"
        elif exp_min is not None and exp_min >= 2:
            label = "2-3"
        elif exp_min is not None or exp_max is not None:
            label = "1-2"

    if label:
        default_min, default_max = EXPERIENCE_BOUNDS[label]
        exp_min = default_min if exp_min is None else exp_min
        exp_max = default_max if exp_max is None else exp_max
    return exp_min, exp_max, f"{label} yrs" if label else "Unspecified"


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
    if not text:
        text = fallback
    return text[:80]


def _normalize_job(job: dict[str, Any], remote_only: bool) -> dict[str, Any]:
    company_name = str(job.get("company") or job.get("company_name") or "Unknown Company").strip()
    company_token = _slug(job.get("company_slug") or company_name, "unknown-company").lower()
    apply_url = str(job.get("url") or job.get("apply_url") or job.get("absolute_url") or "").strip()
    identity = job.get("public_slug") or job.get("slug") or job.get("id") or job.get("external_id")
    if not identity:
        seed = f"{company_token}:{job.get('title', '')}:{apply_url}"
        identity = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    job_slug = _slug(identity, "job")
    is_remote = _is_remote(job, remote_only)
    exp_min, exp_max, exp_label = _experience_fields(job)
    return {
        "id": f"freehire_{job_slug}",
        "ats": ATS_NAME,
        "company_token": company_token,
        "company_name": company_name,
        "title": str(job.get("title") or "Untitled role").strip(),
        "location": _format_location(job, is_remote),
        "absolute_url": apply_url,
        "updated_at": str(job.get("updated_at") or job.get("posted_at") or job.get("created_at") or ""),
        "board_url": str(job.get("company_url") or job.get("careers_url") or apply_url or "https://freehire.me"),
        "exp_min": exp_min,
        "exp_max": exp_max,
        "exp_label": exp_label,
    }


async def _fetch_jobs(q: str, country: str, remote_only: bool, limit: int) -> list[dict[str, Any]]:
    search_url, fallback_url = _api_urls()
    headers = {"Accept": "application/json", "User-Agent": "FreehireSync/1.0"}
    token = os.getenv("FREEHIRE_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    jobs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        selected_url = ""
        first_page: list[dict[str, Any]] = []
        for candidate_url in (search_url, fallback_url):
            try:
                page_limit = min(PAGE_SIZE, limit)
                params = {
                    "country": country,
                    "remote_only": str(remote_only).lower(),
                    "limit": page_limit,
                    "offset": 0,
                }
                if q:
                    params["q"] = q
                response = await client.get(candidate_url, params=params)
                response.raise_for_status()
                first_page = _extract_jobs(response.json())
                selected_url = candidate_url
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        else:
            raise RuntimeError(f"Freehire API endpoints failed: {last_error}") from last_error

        offset = 0
        while offset < limit:
            page_limit = min(PAGE_SIZE, limit - offset)
            params = {
                "country": country,
                "remote_only": str(remote_only).lower(),
                "limit": page_limit,
                "offset": offset,
            }
            if q:
                params["q"] = q
            if offset == 0:
                page = first_page[:page_limit]
            else:
                response = await client.get(selected_url, params=params)
                response.raise_for_status()
                page = _extract_jobs(response.json())[:page_limit]
            sync_state["fetched_jobs"] += len(page)
            jobs.extend(_normalize_job(row, remote_only) for row in page)
            offset += len(page)
            if len(page) < page_limit:
                break
    return jobs[:limit]


def _init_database() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                ats TEXT NOT NULL,
                token TEXT NOT NULL,
                name TEXT NOT NULL,
                board_url TEXT NOT NULL,
                job_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ats, token)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                ats TEXT NOT NULL,
                company_token TEXT NOT NULL,
                company_name TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT NOT NULL,
                absolute_url TEXT NOT NULL,
                updated_at TEXT
            );
            """
        )


def _save_jobs(jobs: list[dict[str, Any]]) -> int:
    _init_database()
    companies: dict[str, dict[str, str]] = {}
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for job in jobs:
            token = job["company_token"]
            companies[token] = {
                "name": job["company_name"],
                "board_url": job["board_url"],
            }
            conn.execute(
                """
                INSERT INTO jobs (id, ats, company_token, company_name, title, location, absolute_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ats = excluded.ats,
                    company_token = excluded.company_token,
                    company_name = excluded.company_name,
                    title = excluded.title,
                    location = excluded.location,
                    absolute_url = excluded.absolute_url,
                    updated_at = excluded.updated_at
                WHERE jobs.ats = 'freehire'
                """,
                (
                    job["id"], ATS_NAME, token, job["company_name"], job["title"],
                    job["location"], job["absolute_url"], job["updated_at"] or None,
                ),
            )

        for token, company in companies.items():
            conn.execute(
                """
                INSERT INTO companies (ats, token, name, board_url, job_count, updated_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(ats, token) DO UPDATE SET
                    name = excluded.name,
                    board_url = excluded.board_url,
                    updated_at = excluded.updated_at
                """,
                (ATS_NAME, token, company["name"], company["board_url"], now),
            )
            conn.execute(
                "UPDATE companies SET job_count = (SELECT COUNT(*) FROM jobs WHERE ats = ? AND company_token = ?) WHERE ats = ? AND token = ?",
                (ATS_NAME, token, ATS_NAME, token),
            )
    return len(companies)


async def _run_sync(q: str, country: str, remote_only: bool, limit: int) -> list[dict[str, Any]]:
    sync_state.update({
        "running": True,
        "stage": "fetching",
        "fetched_jobs": 0,
        "saved_companies": 0,
        "error": None,
    })
    try:
        jobs = await _fetch_jobs(q, country, remote_only, limit)
        sync_state["stage"] = "saving"
        sync_state["saved_companies"] = _save_jobs(jobs)
        sync_state["stage"] = "complete"
        return jobs
    except Exception as exc:
        sync_state["stage"] = "failed"
        sync_state["error"] = str(exc)
        raise
    finally:
        sync_state["running"] = False


@router.post("/sync")
async def start_sync(
    background_tasks: BackgroundTasks,
    q: str = Query(""),
    country: str = Query("IN", min_length=2, max_length=3),
    remote_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    if sync_state["running"]:
        return {"status": "already_running", "state": dict(sync_state)}
    sync_state.update({
        "running": True,
        "stage": "queued",
        "fetched_jobs": 0,
        "saved_companies": 0,
        "error": None,
    })
    background_tasks.add_task(_run_sync, q, country, remote_only, limit)
    return {"status": "started"}


@router.get("/status")
def get_sync_status():
    return dict(sync_state)


def _write_xlsx(path: str, jobs: list[dict[str, Any]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Freehire Jobs"
    headers = ["ATS", "Company", "Role Title", "Experience", "Location", "Apply Link", "Updated At"]
    widths = [15, 30, 42, 18, 30, 55, 26]
    worksheet.append(headers)
    for column, width in enumerate(widths, start=1):
        cell = worksheet.cell(row=1, column=column)
        cell.fill = PatternFill("solid", fgColor="1F2937")
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.alignment = Alignment(vertical="center")
        worksheet.column_dimensions[get_column_letter(column)].width = width
    worksheet.row_dimensions[1].height = 24
    worksheet.freeze_panes = "A2"

    link_font = Font(color="2563EB", underline="single")
    zebra = PatternFill("solid", fgColor="F9FAFB")
    for row_number, job in enumerate(jobs, start=2):
        worksheet.append([
            "Freehire",
            job["company_name"],
            job["title"],
            job["exp_label"],
            job["location"],
            job["absolute_url"],
            job["updated_at"],
        ])
        if job["absolute_url"]:
            worksheet.cell(row=row_number, column=6).hyperlink = job["absolute_url"]
            worksheet.cell(row=row_number, column=6).font = link_font
        if row_number % 2 == 0:
            for column in range(1, len(headers) + 1):
                worksheet.cell(row=row_number, column=column).fill = zebra
    worksheet.auto_filter.ref = worksheet.dimensions
    workbook.save(Path(path))


def _parse_limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= 500:
        raise argparse.ArgumentTypeError("limit must be between 1 and 500")
    return limit


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Freehire jobs into ats_finder.db")
    parser.add_argument("--q", default="", help="Optional job keyword")
    parser.add_argument("--country", default="IN", help="Country code")
    parser.add_argument("--remote", action="store_true", help="Fetch remote-only jobs")
    parser.add_argument("--limit", type=_parse_limit, default=100, help="Maximum jobs to fetch (1-500)")
    parser.add_argument("--xlsx", help="Optional path for a formatted Excel export")
    args = parser.parse_args()

    jobs = asyncio.run(_run_sync(args.q, args.country, args.remote, args.limit))
    if args.xlsx:
        _write_xlsx(args.xlsx, jobs)
    print(f"Fetched {len(jobs)} Freehire jobs; saved {sync_state['saved_companies']} companies.")


if __name__ == "__main__":
    main()
