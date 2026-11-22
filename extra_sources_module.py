from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from freehire_module import _experience_fields

try:
    from jobspy import scrape_jobs as _jobspy_scrape_jobs
except ImportError as exc:
    _jobspy_scrape_jobs = None
    _jobspy_import_error = str(exc)
else:
    _jobspy_import_error = ""

try:
    from ats_scrapers import search as _ats_scrapers_search
except ImportError as exc:
    _ats_scrapers_search = None
    _ats_scrapers_import_error = str(exc)
else:
    _ats_scrapers_import_error = ""

DB_PATH = "ats_finder.db"
REMOTEOK_API_URL = "https://remoteok.com/api"
ARBEITNOW_API_URL = "https://www.arbeitnow.com/api/job-board-api"
JOBSPY_SITES = {"naukri", "linkedin", "indeed", "glassdoor"}
router = APIRouter(prefix="/api/extra", tags=["extra_sources"])
sync_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "fetched_jobs": 0,
    "saved_companies": 0,
    "error": None,
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "<na>", "none"} else text


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if _text(value):
            return value
    return None


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", _text(value)).strip("-._").lower()
    return (text or fallback)[:80]


def _records(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict(orient="records")
        except TypeError:
            value = value.to_dict()
    if isinstance(value, dict):
        for key in ("jobs", "data", "results", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
        else:
            value = [value]
    if not isinstance(value, list):
        raise ValueError("Source returned data that could not be converted to job records")
    normalized_rows = []
    for row in value:
        if not isinstance(row, dict):
            continue
        normalized_rows.append({
            re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()).strip("_"): item
            for key, item in row.items()
        })
    return normalized_rows


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not math.isnan(value)
    return _text(value).lower() in {"1", "true", "yes", "remote"}


def _location_text(value: Any) -> str:
    if isinstance(value, dict):
        components = [
            value.get(key)
            for key in ("city", "region", "state", "country", "name", "display_name")
        ]
        return ", ".join(_text(part) for part in components if _text(part))
    if isinstance(value, list):
        return ", ".join(_text(part) for part in value if _text(part))
    return _text(value)


def _format_location(value: Any, remote: bool) -> str:
    location = _location_text(value) or ("Worldwide" if remote else "Unspecified")
    if remote and "(remote)" not in location.lower():
        location = f"{location} (Remote)"
    return location


def _timestamp(value: Any, unix: bool = False) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif unix or isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return _text(value)
    else:
        iso_value = _text(value)
        try:
            parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        except ValueError:
            return iso_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _make_job(
    *,
    ats: str,
    identity: str,
    company_name: str,
    title: str,
    location: Any,
    url: str,
    updated_at: Any,
    description: Any,
    remote: bool,
    board_url: str = "",
    company_token: str = "",
    timestamp_is_unix: bool = False,
) -> dict[str, Any]:
    company_name = _text(company_name) or "Unknown Company"
    title = _text(title) or "Untitled role"
    description_text = _text(description)
    token = _slug(company_token or company_name, "unknown-company")
    exp_min, exp_max, exp_label = _experience_fields({
        "title": title,
        "description": description_text,
    })
    apply_url = _text(url)
    return {
        "id": identity,
        "ats": ats,
        "company_token": token,
        "company_name": company_name,
        "title": title,
        "location": _format_location(location, remote),
        "absolute_url": apply_url,
        "updated_at": _timestamp(updated_at, unix=timestamp_is_unix),
        "description": description_text,
        "exp_min": exp_min,
        "exp_max": exp_max,
        "exp_label": exp_label,
        "board_url": _text(board_url) or apply_url or f"https://{ats}.com",
    }


def _hash_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _normalize_jobspy(raw: dict[str, Any], selected_sites: list[str]) -> dict[str, Any]:
    raw_site = _text(_first(raw, ("site", "site_name", "source", "platform"))).lower()
    site = re.sub(r"[^a-z0-9_-]+", "_", raw_site) if raw_site else selected_sites[0]
    company_name = _text(_first(raw, ("company", "company_name", "employer"))) or "Unknown Company"
    title = _text(_first(raw, ("title", "job_title", "position"))) or "Untitled role"
    apply_url = _text(_first(raw, ("job_url", "url", "absolute_url", "apply_url", "job_url_direct")))
    remote = _truthy(_first(raw, ("is_remote", "remote")))
    location = _first(raw, ("location", "job_location", "candidate_required_location"))
    remote = remote or "remote" in _location_text(location).lower()
    identity_seed = apply_url or f"{company_name.casefold()}|{title.casefold()}"
    return _make_job(
        ats=site,
        identity=f"{site}_{_hash_identity(identity_seed)}",
        company_name=company_name,
        title=title,
        location=location,
        url=apply_url,
        updated_at=_first(raw, ("date_posted", "posted_at", "updated_at", "created_at")),
        description=_first(raw, ("description", "job_description", "summary")),
        remote=remote,
        board_url=_text(_first(raw, ("company_url", "company_website"))) or apply_url,
        company_token=_text(_first(raw, ("company_slug", "company_id"))),
    )


def _normalize_ats_scrapers(raw: dict[str, Any]) -> dict[str, Any]:
    raw_ats = _text(_first(raw, ("ats", "ats_name", "ats_source", "site", "source", "platform", "board"))).lower()
    ats = re.sub(r"[^a-z0-9_-]+", "_", raw_ats) if raw_ats else "ats_scrapers"
    company_name = _text(_first(raw, ("company", "company_name", "employer", "organization")))
    title = _text(_first(raw, ("title", "job_title", "position", "name")))
    url = _text(_first(raw, ("url", "job_url", "absolute_url", "link", "apply_url")))
    location = _first(raw, ("location", "job_location", "candidate_required_location"))
    remote = _truthy(_first(raw, ("remote", "is_remote", "remote_only")))
    remote = remote or "remote" in _location_text(location).lower()
    description = _first(raw, ("description", "job_description", "summary"))
    identity_value = _text(_first(raw, ("id", "job_id"))) or url or f"{company_name}|{title}"
    return _make_job(
        ats=ats,
        identity=f"{ats}_{_hash_identity(identity_value)}",
        company_name=company_name,
        title=title,
        location=location,
        url=url,
        updated_at=_first(raw, ("date_posted", "posted_at", "updated_at", "created_at", "date")),
        description=description,
        remote=remote,
        board_url=_text(_first(raw, ("company_url", "company_website"))) or url,
        company_token=_text(_first(raw, ("company_slug", "company_id"))),
    )


async def _fetch_jobspy(
    sites: list[str],
    q: str,
    location: str,
    hours_old: int,
    remote_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if _jobspy_scrape_jobs is None:
        detail = f": {_jobspy_import_error}" if _jobspy_import_error else ""
        raise RuntimeError(f"python-jobspy is unavailable; install the jobspy package{detail}")
    country_indeed = "India" if "india" in location.lower() else "USA"
    result = await asyncio.to_thread(
        _jobspy_scrape_jobs,
        site_name=sites,
        search_term=q,
        location=location,
        country_indeed=country_indeed,
        results_wanted=limit,
        hours_old=hours_old,
        is_remote=remote_only,
    )
    jobs = []
    seen: set[tuple[str, str]] = set()
    for raw in _records(result):
        job = _normalize_jobspy(raw, sites)
        key = (re.sub(r"\s+", " ", job["company_name"]).casefold(), re.sub(r"\s+", " ", job["title"]).casefold())
        if key in seen:
            continue
        seen.add(key)
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


async def _fetch_ats_scrapers(
    q: str,
    remote_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if _ats_scrapers_search is None:
        detail = f": {_ats_scrapers_import_error}" if _ats_scrapers_import_error else ""
        raise RuntimeError(f"ats-scrapers is unavailable; install the ats-scrapers package{detail}")
    result = await asyncio.to_thread(
        _ats_scrapers_search,
        query=q,
        remote=remote_only if remote_only else None,
        limit=limit,
    )
    return [_normalize_ats_scrapers(raw) for raw in _records(result)[:limit]]


def _matches_query(values: tuple[Any, ...], query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    return needle in " ".join(_text(value) for value in values).casefold()


def _normalize_remoteok(raw: dict[str, Any]) -> dict[str, Any]:
    title = _text(_first(raw, ("position", "title")))
    company_name = _text(_first(raw, ("company", "company_name")))
    identity = _text(raw.get("id")) or _hash_identity(f"{company_name}|{title}|{raw.get('url', '')}")
    url = _text(_first(raw, ("apply_url", "url")))
    return _make_job(
        ats="remoteok",
        identity=f"remoteok_{identity}",
        company_name=company_name,
        title=title,
        location=raw.get("location"),
        url=url,
        updated_at=raw.get("date"),
        description=raw.get("description"),
        remote=True,
        board_url=_text(raw.get("company_url")) or url,
        company_token=_text(raw.get("company_slug")),
    )


async def _fetch_remoteok(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    response = await client.get(REMOTEOK_API_URL)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("RemoteOK API response was not a job list")
    jobs = []
    for raw in payload[1:]:
        if not isinstance(raw, dict):
            continue
        if not _matches_query((raw.get("position"), raw.get("company"), raw.get("tags")), q):
            continue
        jobs.append(_normalize_remoteok(raw))
        if len(jobs) >= limit:
            break
    return jobs


def _normalize_arbeitnow(raw: dict[str, Any]) -> dict[str, Any]:
    company_name = _text(_first(raw, ("company_name", "company")))
    title = _text(_first(raw, ("title", "position")))
    slug = _text(raw.get("slug")) or _hash_identity(f"{company_name}|{title}|{raw.get('url', '')}")
    remote = _truthy(raw.get("remote"))
    url = _text(_first(raw, ("url", "apply_url", "job_url")))
    return _make_job(
        ats="arbeitnow",
        identity=f"arbeitnow_{slug}",
        company_name=company_name,
        title=title,
        location=raw.get("location"),
        url=url,
        updated_at=raw.get("created_at"),
        description=raw.get("description"),
        remote=remote,
        board_url=_text(raw.get("company_url")) or url,
        company_token=_text(_first(raw, ("company_slug", "company_name"))),
        timestamp_is_unix=True,
    )


async def _fetch_arbeitnow(
    client: httpx.AsyncClient,
    q: str,
    remote_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    jobs = []
    seen: set[str] = set()
    for page in range(1, 6):
        response = await client.get(ARBEITNOW_API_URL, params={"page": page})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Arbeitnow API response did not contain a jobs list")
        if not rows:
            break
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if not _matches_query((raw.get("title"), raw.get("company_name"), raw.get("tags")), q):
                continue
            if remote_only and not _truthy(raw.get("remote")):
                continue
            job = _normalize_arbeitnow(raw)
            if job["id"] in seen:
                continue
            seen.add(job["id"])
            jobs.append(job)
            if len(jobs) >= limit:
                break
        if len(jobs) >= limit:
            break
    return jobs


def _save_jobs(jobs: list[dict[str, Any]]) -> int:
    companies: dict[tuple[str, str], dict[str, str]] = {}
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for job in jobs:
            key = (job["ats"], job["company_token"])
            company = companies.setdefault(key, {
                "name": job["company_name"],
                "board_url": job["board_url"],
            })
            if not company["board_url"] and job["board_url"]:
                company["board_url"] = job["board_url"]
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
                    job["title"], job["location"], job["absolute_url"], job["updated_at"] or None,
                    job["description"], job["exp_min"], job["exp_max"], job["exp_label"],
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
                """
                UPDATE companies
                SET job_count = (
                    SELECT COUNT(*) FROM jobs WHERE ats = ? AND company_token = ?
                )
                WHERE ats = ? AND token = ?
                """,
                (ats, token, ats, token),
            )
    return len(companies)


async def _run_sync(
    source: str,
    sites: str,
    q: str,
    location: str,
    hours_old: int,
    remote_only: bool,
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
        selected_sites = [site.strip().lower() for site in sites.split(",") if site.strip()]
        async with httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": "ATSFinderExtraSources/1.0"},
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            if source == "jobspy":
                sync_state["stage"] = "fetching JobSpy"
                jobs = await _fetch_jobspy(selected_sites, q, location, hours_old, remote_only, limit)
                sync_state["fetched_jobs"] = len(jobs)
            elif source == "ats_scrapers":
                sync_state["stage"] = "fetching ATS-Scrapers"
                jobs = await _fetch_ats_scrapers(q, remote_only, limit)
                sync_state["fetched_jobs"] = len(jobs)
            else:
                if source in {"remoteok", "all_json"}:
                    sync_state["stage"] = "fetching RemoteOK"
                    jobs.extend(await _fetch_remoteok(client, q, limit))
                    sync_state["fetched_jobs"] = len(jobs)
                if source in {"arbeitnow", "all_json"}:
                    sync_state["stage"] = "fetching Arbeitnow"
                    jobs.extend(await _fetch_arbeitnow(client, q, remote_only, limit))
                    sync_state["fetched_jobs"] = len(jobs)
        sync_state["fetched_jobs"] = len(jobs)
        sync_state["stage"] = "saving"
        sync_state["saved_companies"] = _save_jobs(jobs)
        sync_state["stage"] = "complete"
    except Exception as exc:
        sync_state["stage"] = "failed"
        sync_state["error"] = str(exc)
    finally:
        sync_state["running"] = False


@router.post("/sync")
async def start_sync(
    background_tasks: BackgroundTasks,
    source: str = Query(
        "jobspy",
        pattern="^(jobspy|ats_scrapers|remoteok|arbeitnow|all_json)$",
    ),
    sites: str = Query("naukri,linkedin,indeed"),
    q: str = Query("AI Engineer"),
    location: str = Query("India"),
    hours_old: int = Query(72, ge=1, le=720),
    remote_only: bool = Query(False),
    limit: int = Query(50, ge=10, le=300),
) -> dict[str, Any]:
    selected_sites = [site.strip().lower() for site in sites.split(",") if site.strip()]
    if source == "jobspy":
        if not selected_sites:
            raise HTTPException(status_code=422, detail="At least one JobSpy site is required")
        unsupported = sorted(set(selected_sites) - JOBSPY_SITES)
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported JobSpy site(s): {', '.join(unsupported)}",
            )
    if sync_state["running"]:
        return {"status": "already_running", "state": dict(sync_state)}
    sync_state.update({
        "running": True,
        "stage": "queued",
        "fetched_jobs": 0,
        "saved_companies": 0,
        "error": None,
    })
    background_tasks.add_task(
        _run_sync,
        source,
        ",".join(selected_sites),
        q,
        location,
        hours_old,
        remote_only,
        limit,
    )
    return {"status": "started"}


@router.get("/status")
def get_sync_status() -> dict[str, Any]:
    return dict(sync_state)
