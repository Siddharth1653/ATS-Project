from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Query

from freehire_module import _experience_fields

DB_PATH = "ats_finder.db"
HIMALAYAS_API_URL = "https://himalayas.app/jobs/api/search"
REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"

router = APIRouter(prefix="/api/boards", tags=["job_boards"])
sync_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "fetched_jobs": 0,
    "saved_companies": 0,
    "error": None,
}


def _slug(value: Any, fallback: str) -> str:
    text = "".join(
        char.lower() if char.isalnum() or char in "._-" else "-"
        for char in str(value or "").strip()
    ).strip("-._")
    return (text or fallback)[:80]


def _experience_values(title: str, description: str) -> tuple[int | None, int | None, str]:
    return _experience_fields({"title": title, "description": description})


def _timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def _location_restrictions(value: Any) -> str:
    if isinstance(value, str):
        countries = [value.strip()] if value.strip() else []
    elif isinstance(value, list):
        countries = [
            str(item.get("name") or item.get("code") or "").strip()
            if isinstance(item, dict) else str(item).strip()
            for item in value
        ]
        countries = [country for country in countries if country]
    elif isinstance(value, dict):
        raw_countries = value.get("countries") or value.get("locations") or []
        return _location_restrictions(raw_countries)
    else:
        countries = []
    return ", ".join(countries) if countries else "Worldwide (Remote)"


def _normalize_himalayas_job(raw: dict[str, Any]) -> dict[str, Any]:
    company_name = str(raw.get("companyName") or "Unknown Company").strip()
    company_token = _slug(raw.get("companySlug") or company_name, "unknown-company")
    title = str(raw.get("title") or "Untitled role").strip()
    description = str(raw.get("description") or "")
    identity = str(raw.get("guid") or raw.get("slug") or raw.get("id") or title).strip()
    job_id = f"himalayas_{identity or 'job'}"
    exp_min, exp_max, exp_label = _experience_values(title, description)
    company_url = str(
        raw.get("companyUrl")
        or raw.get("company_url")
        or f"https://himalayas.app/companies/{company_token}"
    )
    return {
        "id": job_id,
        "ats": "himalayas",
        "company_token": company_token,
        "company_name": company_name,
        "title": title,
        "location": _location_restrictions(raw.get("locationRestrictions")),
        "absolute_url": str(raw.get("applicationLink") or ""),
        "updated_at": _timestamp(raw.get("pubDate")),
        "description": description,
        "exp_min": exp_min,
        "exp_max": exp_max,
        "exp_label": exp_label,
        "board_url": company_url,
    }


def _normalize_remotive_job(raw: dict[str, Any]) -> dict[str, Any]:
    company_name = str(raw.get("company_name") or "Unknown Company").strip()
    company_token = _slug(raw.get("company_name") or company_name, "unknown-company")
    title = str(raw.get("title") or "Untitled role").strip()
    description = str(raw.get("description") or "")
    location = str(raw.get("candidate_required_location") or "Worldwide").strip()
    if not location.lower().endswith("(remote)"):
        location = f"{location} (Remote)"
    exp_min, exp_max, exp_label = _experience_values(title, description)
    company_url = str(
        raw.get("company_url")
        or f"https://remotive.com/remote-jobs/company/{company_token}"
    )
    return {
        "id": f"remotive_{raw.get('id')}",
        "ats": "remotive",
        "company_token": company_token,
        "company_name": company_name,
        "title": title,
        "location": location,
        "absolute_url": str(raw.get("url") or ""),
        "updated_at": str(raw.get("publication_date") or ""),
        "description": description,
        "exp_min": exp_min,
        "exp_max": exp_max,
        "exp_label": exp_label,
        "board_url": company_url,
    }


def _extract_himalayas_jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("jobs", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            raise ValueError("Himalayas API response did not contain a jobs list")
    else:
        raise ValueError("Himalayas API returned an unsupported response")
    return [row for row in rows if isinstance(row, dict)]


async def _fetch_himalayas(
    client: httpx.AsyncClient,
    q: str,
    country: str,
    worldwide: bool,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 1
    while len(jobs) < limit:
        params: dict[str, str] = {"q": q, "page": str(page)}
        if country.strip():
            params["country"] = country.strip()
        if worldwide:
            params["worldwide"] = "true"
        response = await client.get(HIMALAYAS_API_URL, params=params)
        response.raise_for_status()
        raw_jobs = _extract_himalayas_jobs(response.json())
        if not raw_jobs:
            break
        page_count = 0
        for raw in raw_jobs:
            job = _normalize_himalayas_job(raw)
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            jobs.append(job)
            page_count += 1
            if len(jobs) >= limit:
                break
        sync_state["fetched_jobs"] += page_count
        if page_count == 0:
            break
        page += 1
    return jobs


async def _fetch_remotive(
    client: httpx.AsyncClient,
    q: str,
    limit: int,
) -> list[dict[str, Any]]:
    response = await client.get(
        REMOTIVE_API_URL,
        params={"search": q, "limit": limit},
    )
    response.raise_for_status()
    payload = response.json()
    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw_jobs, list):
        raise ValueError("Remotive API response did not contain a jobs list")
    jobs = [
        _normalize_remotive_job(raw)
        for raw in raw_jobs[:limit]
        if isinstance(raw, dict)
    ]
    sync_state["fetched_jobs"] += len(jobs)
    return jobs


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
                updated_at TEXT,
                description TEXT NOT NULL DEFAULT '',
                exp_min INTEGER,
                exp_max INTEGER,
                exp_label TEXT NOT NULL DEFAULT 'Unspecified'
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        migrations = {
            "description": "ALTER TABLE jobs ADD COLUMN description TEXT NOT NULL DEFAULT ''",
            "exp_min": "ALTER TABLE jobs ADD COLUMN exp_min INTEGER",
            "exp_max": "ALTER TABLE jobs ADD COLUMN exp_max INTEGER",
            "exp_label": "ALTER TABLE jobs ADD COLUMN exp_label TEXT NOT NULL DEFAULT 'Unspecified'",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)


def _save_jobs(jobs: list[dict[str, Any]]) -> int:
    _init_database()
    companies: dict[tuple[str, str], dict[str, str]] = {}
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for job in jobs:
            key = (job["ats"], job["company_token"])
            companies[key] = {
                "name": job["company_name"],
                "board_url": job["board_url"],
            }
            conn.execute(
                """
                INSERT INTO jobs (
                    id, ats, company_token, company_name, title, location,
                    absolute_url, updated_at, description, exp_min, exp_max, exp_label
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ats = excluded.ats,
                    company_token = excluded.company_token,
                    company_name = excluded.company_name,
                    title = excluded.title,
                    location = excluded.location,
                    absolute_url = excluded.absolute_url,
                    updated_at = excluded.updated_at,
                    description = excluded.description,
                    exp_min = excluded.exp_min,
                    exp_max = excluded.exp_max,
                    exp_label = excluded.exp_label
                WHERE jobs.ats = excluded.ats
                """,
                (
                    job["id"], job["ats"], job["company_token"], job["company_name"],
                    job["title"], job["location"], job["absolute_url"],
                    job["updated_at"] or None, job["description"], job["exp_min"],
                    job["exp_max"], job["exp_label"],
                ),
            )

        for (ats, token), company in companies.items():
            conn.execute(
                """
                INSERT INTO companies (ats, token, name, board_url, job_count, updated_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(ats, token) DO UPDATE SET
                    name = excluded.name,
                    board_url = excluded.board_url,
                    updated_at = excluded.updated_at
                """,
                (ats, token, company["name"], company["board_url"], now),
            )
            conn.execute(
                "UPDATE companies SET job_count = "
                "(SELECT COUNT(*) FROM jobs WHERE ats = ? AND company_token = ?) "
                "WHERE ats = ? AND token = ?",
                (ats, token, ats, token),
            )
    return len(companies)


async def _run_sync(
    source: str,
    q: str,
    country: str,
    worldwide: bool,
    limit: int,
) -> None:
    sync_state.update({
        "running": True,
        "stage": "fetching",
        "fetched_jobs": 0,
        "saved_companies": 0,
        "error": None,
    })
    try:
        jobs: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": "ATSJobBoardsSync/1.0"},
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            if source in ("both", "himalayas"):
                sync_state["stage"] = "fetching Himalayas"
                jobs.extend(await _fetch_himalayas(client, q, country, worldwide, limit))
            if source in ("both", "remotive"):
                sync_state["stage"] = "fetching Remotive"
                jobs.extend(await _fetch_remotive(client, q, limit))

        sync_state["stage"] = "saving"
        sync_state["saved_companies"] = _save_jobs(jobs)
        sync_state["stage"] = "complete"
    except Exception as exc:
        sync_state["stage"] = "failed"
        sync_state["error"] = str(exc)
        raise
    finally:
        sync_state["running"] = False


@router.post("/sync")
async def start_sync(
    background_tasks: BackgroundTasks,
    source: str = Query("both", pattern="^(both|himalayas|remotive)$"),
    q: str = Query(""),
    country: str = Query("IN"),
    worldwide: bool = Query(True),
    limit: int = Query(100, ge=10, le=300),
) -> dict[str, Any]:
    if sync_state["running"]:
        return {"status": "already_running", "state": dict(sync_state)}
    sync_state.update({
        "running": True,
        "stage": "queued",
        "fetched_jobs": 0,
        "saved_companies": 0,
        "error": None,
    })
    background_tasks.add_task(_run_sync, source, q, country, worldwide, limit)
    return {"status": "started"}


@router.get("/status")
def get_sync_status() -> dict[str, Any]:
    return dict(sync_state)
