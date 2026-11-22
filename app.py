from __future__ import annotations

import asyncio
import html
import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from direct_careers_module import router as direct_router
from extra_sources_module import router as extra_router
from freehire_module import router as freehire_router
from job_boards_module import router as job_boards_router
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

app = FastAPI(title="Multi-ATS Company & Job Finder (Global + India)")
app.include_router(freehire_router)
app.include_router(extra_router)
app.include_router(job_boards_router)
app.include_router(direct_router)
DB_PATH = "ats_finder.db"

IGNORED_SLUGS = {
    "embed", "users", "sessions", "assets", "robots.txt", "sitemap.xml",
    "favicon.ico", "api", "jobs", "applications", "auth", "login", "static",
    "privacy", "terms", "postings", "apply", "search", "www", "app", "help",
    "blog", "docs", "status", "cdn", "j", "signup", "careers", "support",
    "csdemo", "demo", "test", "sandbox",
}
TOKEN_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,80}$")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

ATS_CONFIG = {
    # Global
    "greenhouse": {
        "patterns": ("boards.greenhouse.io/*", "job-boards.greenhouse.io/*"),
        "exclude": (".*/jobs/.*", ".*/embed/.*"),
    },
    "ashby": {
        "patterns": ("jobs.ashbyhq.com/*",),
        "exclude": (".*/[0-9a-f]{8}-[0-9a-f]{4}-.*", ".*/application.*"),
    },
    "lever": {
        "patterns": ("jobs.lever.co/*",),
        "exclude": (".*/[0-9a-f]{8}-[0-9a-f]{4}-.*", ".*/apply.*"),
    },
    "smartrecruiters": {
        "patterns": ("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"),
        "exclude": (".*/[0-9]{6,}-.*",),
    },
    "workable": {
        "patterns": ("apply.workable.com/*",),
        "exclude": (".*/j/.*", ".*/apply.*"),
    },
    "recruitee": {
        "patterns": ("*.recruitee.com",),
        "exclude": (),
    },
    "pinpoint": {
        "patterns": ("*.pinpointhq.com",),
        "exclude": (),
    },
    "bamboohr": {
        "patterns": ("*.bamboohr.com",),
        "exclude": (),
    },
    "breezy": {
        "patterns": ("*.breezy.hr",),
        "exclude": (),
    },
    "teamtailor": {
        "patterns": ("*.teamtailor.com",),
        "exclude": (),
    },
    # India-focused
    "keka": {
        "patterns": ("*.keka.com",),
        "exclude": (),
    },
    "zohorecruit": {
        "patterns": ("*.zohorecruit.in",),
        "exclude": (),
    },
    "kula": {
        "patterns": ("careers.kula.ai/*",),
        "exclude": (".*/[0-9]+-.*", ".*/jobs/.*"),
    },
    "darwinbox": {
        "patterns": ("*.darwinbox.in", "*.darwinbox.com"),
        "exclude": (),
    },
}

INDIAN_ATS = ("keka", "zohorecruit", "kula", "darwinbox")

crawl_state = {
    "running": False,
    "stage": "idle",
    "tokens_found": 0,
    "verified_checked": 0,
    "active_companies": 0,
    "error": None,
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with get_db() as conn:
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
            CREATE INDEX IF NOT EXISTS idx_companies_ats ON companies(ats);
            CREATE INDEX IF NOT EXISTS idx_jobs_ats_company ON jobs(ats, company_token);
            CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);
            CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
            """
        )
        company_columns = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
        if "career_url" not in company_columns:
            try:
                conn.execute("ALTER TABLE companies ADD COLUMN career_url TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                if "career_url" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(companies)")
                }:
                    raise
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        migrations = {
            "description": "ALTER TABLE jobs ADD COLUMN description TEXT NOT NULL DEFAULT ''",
            "exp_min": "ALTER TABLE jobs ADD COLUMN exp_min INTEGER",
            "exp_max": "ALTER TABLE jobs ADD COLUMN exp_max INTEGER",
            "exp_label": "ALTER TABLE jobs ADD COLUMN exp_label TEXT NOT NULL DEFAULT 'Unspecified'",
        }
        for column, statement in migrations.items():
            if column not in job_columns:
                conn.execute(statement)


init_db()


def extract_token(ats: str, raw_url: str) -> str | None:
    try:
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower().split(":")[0]
        parts = [p for p in parsed.path.split("/") if p]
        candidate = None

        if ats == "greenhouse" and host.endswith("greenhouse.io") and parts:
            candidate = (parse_qs(parsed.query).get("for") or [None])[0] if parts[0].lower() == "embed" else parts[0]
        elif ats == "ashby" and host == "jobs.ashbyhq.com" and parts:
            candidate = parts[0]
        elif ats == "lever" and host == "jobs.lever.co" and parts:
            candidate = parts[0]
        elif ats == "smartrecruiters" and host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
            candidate = parts[0]
        elif ats == "workable" and host == "apply.workable.com" and parts:
            candidate = parts[0]
        elif ats == "kula" and host == "careers.kula.ai" and parts:
            candidate = parts[0]
        elif ats in ("recruitee", "keka", "zohorecruit", "darwinbox", "pinpoint", "bamboohr", "breezy", "teamtailor"):
            suffixes = {
                "recruitee": (".recruitee.com",),
                "keka": (".keka.com",),
                "zohorecruit": (".zohorecruit.in",),
                "darwinbox": (".darwinbox.in", ".darwinbox.com"),
                "pinpoint": (".pinpointhq.com",),
                "bamboohr": (".bamboohr.com",),
                "breezy": (".breezy.hr",),
                "teamtailor": (".teamtailor.com",),
            }[ats]
            for suffix in suffixes:
                if host.endswith(suffix):
                    sub = host[: -len(suffix)]
                    if sub and "." not in sub:
                        candidate = sub
                    break

        if not candidate:
            return None
        candidate = candidate.strip()
        if ats not in ("ashby", "smartrecruiters"):
            candidate = candidate.lower()
        if candidate.lower() in IGNORED_SLUGS or not TOKEN_RE.match(candidate):
            return None
        return candidate
    except Exception:
        return None


def _expand_prefixes(spec: str) -> list[str]:
    prefixes: list[str] = []
    seen: set[str] = set()
    for item in spec.split(","):
        part = item.strip().lower()
        if re.fullmatch(r"[a-z0-9]", part):
            expanded = [part]
        elif re.fullmatch(r"[a-z0-9]-[a-z0-9]", part):
            start, end = part[0], part[2]
            if start.isalpha() != end.isalpha() or start > end:
                raise ValueError(f"Invalid prefix range: {item}")
            expanded = [chr(code) for code in range(ord(start), ord(end) + 1)]
        else:
            raise ValueError(f"Invalid prefix specification: {item}")
        for prefix in expanded:
            if prefix not in seen:
                prefixes.append(prefix)
                seen.add(prefix)
    if not prefixes:
        raise ValueError("At least one prefix is required")
    return prefixes


def _prefix_pattern(pattern: str, prefix: str) -> str:
    if pattern.startswith("*."):
        return f"{prefix}*.{pattern[2:]}"
    if "/" in pattern and pattern.endswith("*"):
        return f"{pattern[:-1]}{prefix}*"
    return pattern


async def _get_cdx_response(
    client: httpx.AsyncClient, url: str, params: list[tuple[str, str]]
) -> httpx.Response:
    retry_statuses = {429, 502, 503, 504}
    for attempt in range(3):
        response = await client.get(url, params=params, timeout=60.0)
        if response.status_code not in retry_statuses or attempt == 2:
            return response
        await asyncio.sleep(3.0)
    return response


async def fetch_cdx(
    client: httpx.AsyncClient,
    ats: str,
    limit: int,
    source: str,
    cdx_api: str | None,
    prefix: str | None = None,
    deep: bool = False,
) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    seen: set[str] = set()
    cfg = ATS_CONFIG[ats]
    call_number = 0

    for pattern in cfg["patterns"]:
        query_patterns = [pattern] if prefix is None else [_prefix_pattern(pattern, prefix)]
        if prefix is not None and ats in ("ashby", "smartrecruiters"):
            upper_pattern = _prefix_pattern(pattern, prefix.upper())
            if upper_pattern not in query_patterns:
                query_patterns.append(upper_pattern)

        for query_pattern in query_patterns:
            if source in ("commoncrawl", "both") and cdx_api:
                params = [("url", query_pattern), ("output", "json"), ("fl", "url"), ("filter", "status:200"), ("limit", str(limit))]
                params += [("filter", f"!url:{ex}") for ex in cfg["exclude"]]
                try:
                    if deep and call_number:
                        await asyncio.sleep(1.0)
                    call_number += 1
                    resp = await _get_cdx_response(client, cdx_api, params)
                    if resp.status_code == 200:
                        for line in resp.text.splitlines():
                            if line.strip():
                                try:
                                    tok = extract_token(ats, json.loads(line).get("url", ""))
                                    if tok:
                                        key = tok if ats in ("ashby", "smartrecruiters") else tok.lower()
                                        if key not in seen:
                                            seen.add(key)
                                            found.add((ats, tok))
                                except json.JSONDecodeError:
                                    pass
                except httpx.HTTPError:
                    pass

            if source in ("wayback", "both"):
                params = [("url", query_pattern), ("output", "json"), ("fl", "original"), ("collapse", "urlkey"), ("filter", "statuscode:200"), ("limit", str(limit))]
                params += [("filter", f"!original:{ex}") for ex in cfg["exclude"]]
                try:
                    if deep and call_number:
                        await asyncio.sleep(1.0)
                    call_number += 1
                    resp = await _get_cdx_response(client, "https://web.archive.org/cdx/search/cdx", params)
                    if resp.status_code == 200:
                        for row in resp.json()[1:]:
                            if row:
                                tok = extract_token(ats, row[0])
                                if tok:
                                    key = tok if ats in ("ashby", "smartrecruiters") else tok.lower()
                                    if key not in seen:
                                        seen.add(key)
                                        found.add((ats, tok))
                except (httpx.HTTPError, ValueError):
                    pass
    return found


def _save_company_and_jobs(ats: str, token: str, name: str, board_url: str, jobs: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO companies (ats, token, name, board_url, job_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ats, token) DO UPDATE SET
                name=excluded.name, board_url=excluded.board_url,
                job_count=excluded.job_count, updated_at=excluded.updated_at
            """,
            (ats, token, name, board_url, len(jobs), now),
        )
        conn.execute("DELETE FROM jobs WHERE ats = ? AND company_token = ?", (ats, token))
        if jobs:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jobs
                (id, ats, company_token, company_name, title, location, absolute_url, updated_at, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"{ats}_{token}_{j['id']}", ats, token, name, j["title"],
                        j["location"], j["url"], j["updated_at"], j.get("desc", ""),
                    )
                    for j in jobs
                ],
            )


def _make_job(
    jid: str | int | None,
    title: str | None,
    location: str,
    url: str,
    updated_at: str | None = "",
    desc: str | None = "",
) -> dict[str, str]:
    return {
        "id": str(jid or ""),
        "title": str(title or "").strip(),
        "location": str(location or "Unknown").strip(),
        "url": str(url or ""),
        "updated_at": str(updated_at or ""),
        "desc": str(desc or ""),
    }


def _fmt_loc(parts: list[str | None], remote: bool = False, default: str = "Unknown") -> str:
    loc = ", ".join(p.strip() for p in parts if p and str(p).strip())
    if remote:
        loc = f"{loc} (Remote)" if loc else "Remote"
    return loc or default


def _html_title(text: str, fallback: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not m:
        return fallback
    cleaned = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    cleaned = re.sub(r"\s*[-|]\s*(Careers|Jobs|Openings|Keka|Zoho Recruit|Kula|Darwinbox).*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^Jobs at\s+", "", cleaned, flags=re.I).strip()
    return cleaned or fallback


async def verify_board(client: httpx.AsyncClient, sem: asyncio.Semaphore, ats: str, token: str) -> bool:
    async with sem:
        try:
            pretty = token.replace("-", " ").replace("_", " ").title()
            jobs: list[dict] = []

            if ats == "greenhouse":
                base = f"https://boards-api.greenhouse.io/v1/boards/{token}"
                r1, r2 = await asyncio.gather(client.get(base, timeout=15.0), client.get(f"{base}/jobs", timeout=15.0))
                crawl_state["verified_checked"] += 1
                if r1.status_code != 200 or r2.status_code != 200:
                    return False
                name = r1.json().get("name") or pretty
                board = f"https://boards.greenhouse.io/{token}"
                jobs = [
                    {
                        "id": str(j.get("id", i)),
                        "title": (j.get("title") or "").strip(),
                        "location": ((j.get("location") or {}).get("name") or "Unknown").strip(),
                        "url": j.get("absolute_url") or board,
                        "updated_at": j.get("updated_at") or "",
                    }
                    for i, j in enumerate(r2.json().get("jobs") or [])
                ]

            elif ats == "ashby":
                r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or r.json().get("jobs") is None:
                    return False
                d = r.json()
                name = d.get("organizationName") or pretty
                board = f"https://jobs.ashbyhq.com/{token}"
                jobs = [
                    {
                        "id": str(j.get("id", i)),
                        "title": (j.get("title") or "").strip(),
                        "location": (j.get("location") or "Unknown").strip(),
                        "url": j.get("jobUrl") or board,
                        "updated_at": j.get("publishedAt") or "",
                    }
                    for i, j in enumerate(d.get("jobs") or [])
                ]

            elif ats == "lever":
                r = await client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"}, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or not isinstance(r.json(), list):
                    return False
                name, board = pretty, f"https://jobs.lever.co/{token}"
                for i, j in enumerate(r.json()):
                    ms = j.get("createdAt")
                    ts = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if isinstance(ms, (int, float)) else ""
                    jobs.append({
                        "id": str(j.get("id", i)),
                        "title": (j.get("text") or "").strip(),
                        "location": ((j.get("categories") or {}).get("location") or "Unknown").strip(),
                        "url": j.get("hostedUrl") or board,
                        "updated_at": ts,
                    })

            elif ats == "smartrecruiters":
                r = await client.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings", params={"limit": 100}, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or not isinstance(r.json().get("content"), list):
                    return False
                raw = r.json()["content"]
                name = ((raw[0].get("company") or {}).get("name") if raw else None) or token
                board = f"https://jobs.smartrecruiters.com/{token}"
                for i, j in enumerate(raw):
                    lo = j.get("location") or {}
                    jid = str(j.get("id", i))
                    jobs.append({
                        "id": jid,
                        "title": (j.get("name") or "").strip(),
                        "location": lo.get("fullLocation") or _fmt_loc([lo.get("city"), lo.get("region"), lo.get("country")], bool(lo.get("remote"))),
                        "url": f"{board}/{jid}",
                        "updated_at": j.get("releasedDate") or "",
                    })

            elif ats == "workable":
                await asyncio.sleep(1.5)
                r = await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{token}", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or not isinstance(r.json().get("jobs"), list):
                    return False
                d = r.json()
                name, board = d.get("name") or pretty, f"https://apply.workable.com/{token}"
                seen_codes: set[str] = set()
                for i, j in enumerate(d["jobs"]):
                    code = str(j.get("shortcode") or i)
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    jobs.append({
                        "id": code,
                        "title": (j.get("title") or "").strip(),
                        "location": _fmt_loc([j.get("city"), j.get("state"), j.get("country")], bool(j.get("telecommuting"))),
                        "url": j.get("url") or f"{board}/j/{code}/",
                        "updated_at": j.get("published_on") or "",
                    })

            elif ats == "recruitee":
                r = await client.get(f"https://{token}.recruitee.com/api/offers/", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or not isinstance(r.json().get("offers"), list):
                    return False
                raw = r.json()["offers"]
                name = (raw[0].get("company_name") if raw else None) or pretty
                board = f"https://{token}.recruitee.com"
                jobs = [
                    {
                        "id": str(j.get("id", i)),
                        "title": (j.get("title") or "").strip(),
                        "location": (j.get("location") or "Unknown").strip(),
                        "url": j.get("careers_url") or board,
                        "updated_at": j.get("created_at") or "",
                    }
                    for i, j in enumerate(raw)
                ]

            elif ats == "pinpoint":
                board = f"https://{token}.pinpointhq.com"
                r = await client.get(f"{board}/postings.json", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200:
                    return False
                data = r.json()
                raw = data.get("data") if isinstance(data, dict) else data
                if not isinstance(raw, list):
                    return False
                name = (data.get("company_name") if isinstance(data, dict) else None) or pretty
                for i, j in enumerate(raw):
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("id") or i)
                    lo = j.get("location") or {}
                    location_name = lo.get("name") if isinstance(lo, dict) else lo
                    workplace_type = j.get("workplace_type")
                    location = _fmt_loc(
                        [
                            str(location_name) if location_name else None,
                            str(workplace_type) if workplace_type else None,
                        ],
                        default="Unknown",
                    )
                    job_url = j.get("url") or j.get("absolute_url") or f"{board}/postings/{jid}"
                    jobs.append(_make_job(
                        jid,
                        j.get("title"),
                        location,
                        urljoin(f"{board}/", str(job_url)),
                        j.get("updated_at"),
                        j.get("description"),
                    ))

            elif ats == "bamboohr":
                board = f"https://{token}.bamboohr.com"
                r = await client.get(f"{board}/careers/list", timeout=15.0)
                crawl_state["verified_checked"] += 1
                final_url = urlparse(str(r.url))
                if (
                    r.status_code != 200
                    or final_url.hostname in ("bamboohr.com", "www.bamboohr.com")
                    or final_url.path.lower().endswith("/login.php")
                ):
                    return False
                data = r.json()
                raw = data.get("result") if isinstance(data, dict) else None
                if not isinstance(raw, list):
                    return False
                name = (data.get("companyName") if isinstance(data, dict) else None) or pretty
                board = f"{board}/careers"
                for i, j in enumerate(raw):
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("id") or i)
                    loc = j.get("location") or {}
                    if not isinstance(loc, dict):
                        loc = {}
                    jobs.append(_make_job(
                        jid,
                        j.get("jobOpeningName"),
                        _fmt_loc(
                            [
                                str(loc.get("city")) if loc.get("city") else None,
                                str(loc.get("state")) if loc.get("state") else None,
                                str(loc.get("country")) if loc.get("country") else None,
                            ],
                            remote=bool(j.get("isRemote")),
                        ),
                        f"{board}/{jid}",
                        j.get("updated_at") or j.get("dateLastModified"),
                        j.get("description"),
                    ))

            elif ats == "breezy":
                board = f"https://{token}.breezy.hr"
                r = await client.get(f"{board}/json", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or not isinstance(r.json(), list):
                    return False
                raw = r.json()
                name, board = pretty, f"https://{token}.breezy.hr"
                for i, j in enumerate(raw):
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("id") or i)
                    loc = j.get("location") or {}
                    if not isinstance(loc, dict):
                        loc = {}
                    location_name = loc.get("name")
                    if loc.get("is_remote"):
                        location = f"{location_name} (Remote)" if location_name else "Remote"
                    else:
                        location = str(location_name or "Unknown")
                    jobs.append(_make_job(
                        jid,
                        j.get("name"),
                        location,
                        str(j.get("url") or f"{board}/jobs/{jid}"),
                        j.get("published_date"),
                        j.get("description"),
                    ))

            elif ats == "teamtailor":
                board = f"https://{token}.teamtailor.com"
                r = await client.get(f"{board}/jobs.rss", timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200:
                    return False
                root = ET.fromstring(r.text)

                def xml_text(node: ET.Element, tag_name: str) -> str:
                    for child in node.iter():
                        local_name = child.tag.rsplit("}", 1)[-1].split(":")[-1].lower()
                        if local_name == tag_name.lower():
                            return " ".join(
                                part.strip() for part in child.itertext() if part.strip()
                            )
                    return ""

                name = xml_text(root, "title") or pretty
                for i, item in enumerate(
                    node for node in root.iter()
                    if node.tag.rsplit("}", 1)[-1].split(":")[-1].lower() == "item"
                ):
                    guid = xml_text(item, "guid")
                    link = xml_text(item, "link")
                    item_url = link or guid or board
                    location = xml_text(item, "location") or "Remote / Europe"
                    jobs.append(_make_job(
                        guid or link or str(i),
                        xml_text(item, "title"),
                        location,
                        urljoin(f"{board}/", item_url),
                        xml_text(item, "pubDate"),
                        xml_text(item, "description"),
                    ))

            elif ats == "keka":
                board = f"https://{token}.keka.com/careers"
                r = await client.get(board, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or len(r.text) < 200 or "page not found" in r.text.lower():
                    return False
                name = _html_title(r.text, pretty)
                m_uuid = UUID_RE.search(r.text)
                if m_uuid:
                    jr = await client.get(
                        f"https://{token}.keka.com/careers/api/embedjobs/default/active/{m_uuid.group(0)}",
                        timeout=15.0,
                    )
                    if jr.status_code == 200 and isinstance(jr.json(), list):
                        for i, j in enumerate(jr.json()):
                            jid = str(j.get("id") or j.get("jobId") or i)
                            locs = j.get("jobLocations") or []
                            loc_str = ", ".join(
                                _fmt_loc([l.get("city"), l.get("state"), l.get("countryName") or l.get("name")], default="")
                                for l in locs if isinstance(l, dict)
                            ) or (j.get("location") or "India")
                            jobs.append({
                                "id": jid,
                                "title": (j.get("title") or j.get("jobTitle") or "").strip(),
                                "location": loc_str,
                                "url": f"{board}/jobdetails/{jid}",
                                "updated_at": j.get("publishedDate") or j.get("createdOn") or "",
                            })

            elif ats == "zohorecruit":
                board = f"https://{token}.zohorecruit.in/jobs/Careers"
                r = await client.get(board, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or "page not found" in r.text.lower():
                    return False
                name = _html_title(r.text, pretty)
                jobs_input = re.search(
                    r"<input\b(?=[^>]*\s+id\s*=\s*(['\"])jobs\1)[^>]*>",
                    r.text,
                    re.I,
                )
                if not jobs_input:
                    return False
                value_match = re.search(
                    r"(?:^|\s)value\s*=\s*(['\"])(.*?)\1",
                    jobs_input.group(0),
                    re.I | re.S,
                )
                if not value_match:
                    return False
                raw_jobs = json.loads(html.unescape(value_match.group(2)))
                if not isinstance(raw_jobs, list):
                    return False
                for j in raw_jobs:
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("id") or j.get("Job_Opening_ID") or "")
                    title = str(
                        j.get("Posting_Title")
                        or j.get("Job_Opening_Name")
                        or j.get("title")
                        or ""
                    ).strip()
                    exp_text = str(j.get("Work_Experience") or j.get("experience") or "")
                    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    jobs.append({
                        "id": jid,
                        "title": title,
                        "location": _fmt_loc(
                            [
                                j.get("City") or j.get("city"),
                                j.get("State") or j.get("state"),
                                j.get("Country") or j.get("country"),
                            ],
                            remote=bool(j.get("Remote_Job") or j.get("is_remote")),
                            default="India",
                        ),
                        "url": f"{board}/{jid}/{slug}",
                        "updated_at": j.get("Date_Opened") or j.get("updated_at") or "",
                        "desc": exp_text,
                    })

            elif ats == "kula":
                board = f"https://careers.kula.ai/{token}"
                r = await client.get(board, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or "not found" in r.text.lower()[:800]:
                    return False
                name = _html_title(r.text, pretty)
                seen_ids: set[str] = set()
                pat = rf'href="(/{re.escape(token)}/(\d+)-[^"]+)"[^>]*>([^<]{{3,140}})<'
                for m in re.finditer(pat, r.text):
                    href, jid, title = m.groups()
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    jobs.append({
                        "id": jid,
                        "title": html.unescape(title).strip(),
                        "location": "India / Remote",
                        "url": f"https://careers.kula.ai{href}",
                        "updated_at": "",
                    })

            elif ats == "darwinbox":
                board = f"https://{token}.darwinbox.in/ms/candidate/careers"
                r = await client.get(board, timeout=15.0)
                crawl_state["verified_checked"] += 1
                if r.status_code != 200 or len(r.text) < 200 or "404" in r.text[:500].lower():
                    return False
                name = _html_title(r.text, pretty)

            else:
                return False

            _save_company_and_jobs(ats, token, name, board, jobs)
            crawl_state["active_companies"] += 1
            return True
        except Exception:
            crawl_state["verified_checked"] += 1
            return False


async def run_pipeline(ats_filter: str, source: str, limit: int, prefixes: str = "a-z", deep: bool = False) -> None:
    expanded_prefixes = _expand_prefixes(prefixes)
    crawl_state.update({"running": True, "stage": "starting", "tokens_found": 0, "verified_checked": 0, "active_companies": 0, "error": None})
    if ats_filter == "all":
        selected = list(ATS_CONFIG.keys())
    elif ats_filter == "india":
        selected = list(INDIAN_ATS)
    else:
        selected = [ats_filter]

    try:
        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 MultiATSFinder/3.0"}, follow_redirects=True) as client:
            cdx_api = None
            if source in ("commoncrawl", "both"):
                coll = await client.get("https://index.commoncrawl.org/collinfo.json", timeout=20.0)
                coll.raise_for_status()
                cdx_api = coll.json()[0]["cdx-api"]

            candidates: set[tuple[str, str]] = set()
            prefix_calls = 0
            for ats in selected:
                if deep:
                    for index, prefix in enumerate(expanded_prefixes, start=1):
                        crawl_state["stage"] = f"discovering {ats} [{prefix}*] ({index}/{len(expanded_prefixes)})"
                        if prefix_calls:
                            await asyncio.sleep(1.0)
                        candidates |= await fetch_cdx(client, ats, limit, source, cdx_api, prefix, deep=True)
                        prefix_calls += 1
                else:
                    crawl_state["stage"] = f"discovering {ats}"
                    candidates |= await fetch_cdx(client, ats, limit, source, cdx_api)

            deduped = {(a, t.lower()): (a, t) for a, t in candidates}
            pairs = sorted(deduped.values())
            crawl_state["tokens_found"] = len(pairs)
            crawl_state["stage"] = "verifying boards"

            default_sem, slow_sem = asyncio.Semaphore(12), asyncio.Semaphore(2)
            await asyncio.gather(*(verify_board(client, slow_sem if a == "workable" else default_sem, a, t) for a, t in pairs))

        crawl_state["stage"] = "done"
    except Exception as exc:
        crawl_state["stage"] = "error"
        crawl_state["error"] = str(exc)
    finally:
        crawl_state["running"] = False


@app.get("/")
def serve_ui():
    return FileResponse("index.html")


@app.post("/api/discover")
async def start_discovery(
    background_tasks: BackgroundTasks,
    ats: str = Query("india", pattern="^(all|india|greenhouse|ashby|lever|smartrecruiters|workable|recruitee|pinpoint|bamboohr|breezy|teamtailor|keka|zohorecruit|kula|darwinbox)$"),
    source: str = Query("both", pattern="^(commoncrawl|wayback|both)$"),
    limit: int = Query(3000, ge=100, le=20000),
    prefixes: str = Query("a-z", min_length=1, max_length=100),
    deep: bool = Query(False),
):
    if crawl_state["running"]:
        return {"status": "already_running", "state": crawl_state}
    try:
        _expand_prefixes(prefixes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(run_pipeline, ats, source, limit, prefixes, deep)
    return {"status": "started"}


@app.get("/api/status")
def get_status():
    with get_db() as conn:
        c = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        j = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_ats = {r["ats"]: r["cnt"] for r in conn.execute("SELECT ats, COUNT(*) AS cnt FROM companies GROUP BY ats").fetchall()}
    return {**crawl_state, "db_companies": c, "db_jobs": j, "by_ats": by_ats}


def _ats_clause(ats: str, col: str = "ats") -> tuple[str, list]:
    if ats == "all":
        return "", []
    if ats == "india":
        return f" AND {col} IN (?,?,?,?)", list(INDIAN_ATS)
    return f" AND {col} = ?", [ats]


@app.get("/api/companies")
def list_companies(ats: str = "all", q: str = "", min_jobs: int = 0, limit: int = Query(300, ge=1, le=3000)):
    clause, extra = _ats_clause(ats)
    sql = f"SELECT ats, token, name, board_url, career_url, job_count, updated_at FROM companies WHERE (name LIKE ? OR token LIKE ?) AND job_count >= ? {clause} ORDER BY job_count DESC, name ASC LIMIT ?"
    with get_db() as conn:
        rows = conn.execute(sql, [f"%{q}%", f"%{q}%", min_jobs, *extra, limit]).fetchall()
    return [dict(r) for r in rows]


ROLE_PRESETS = {
    "genai_agentic": (
        "genai", "gen ai", "generative ai", "agentic", "ai agent", "agent engineer",
        " llm ", " llms ", "large language model", " rag ", "prompt engineer",
        "applied ai", "conversational ai", "voice ai", "multimodal", "foundation model",
        "langchain", "langgraph", " mcp ", "copilot", "ai engineer", "ai developer",
        "ai platform", "ai systems", "ai architect",
    ),
    "ml_nlp": (
        "machine learning", " ml ", " mle ", " ai ml ", " ml ai ",
        " nlp ", "natural language", "deep learning", "computer vision",
        "artificial intelligence", "applied scientist", "research scientist",
        "research engineer", "mlops", "ml ops", "speech", " asr ", " tts ",
        "recommendation", "reinforcement learning",
    ),
    "fde": (
        "forward deployed", " fde ", "deployment engineer", "deployed engineer",
        "solutions engineer", "solution engineer", "solutions architect",
        "field engineer", "customer engineer", "implementation engineer",
        "technical solutions", "ai solutions", "delivery engineer",
    ),
    "data": ("data scientist", "data analyst", "data engineer", "analytics"),
    "intern": (" intern ", "internship", "trainee", "apprentice", "fresher", "new grad", "graduate"),
}

NON_ENG_EXCLUDE = (
    " sales ", "account executive", "business development", " bdr ", " sdr ",
    "recruiter", "recruiting", "talent acquisition", "marketing", "copywriter",
    "content writer", "annotator", "labeler", "data entry", "legal", "counsel",
    "customer support", "hr ", "human resources", "payroll",
)

REMOTE_INDIA_TERMS = (
    "india", "bengaluru", "bangalore", "pune", "hyderabad", "mumbai",
    "delhi", "gurugram", "noida", "chennai", "ist",
)
REMOTE_US_TERMS = (
    "united states", "usa", "us", "dc", "nyc", "california", "new york",
    "texas", "americas", "north america", "canada",
)
REMOTE_EU_TERMS = (
    "uk", "united kingdom", "london", "europe", "emea", "poland", "spain",
    "germany", "france", "ireland", "netherlands",
)
REMOTE_GLOBAL_TERMS = ("global", "anywhere", "worldwide")
REMOTE_GLOBAL_EXCLUDES = tuple(
    dict.fromkeys((*REMOTE_US_TERMS, *REMOTE_EU_TERMS, *REMOTE_INDIA_TERMS))
)
REMOTE_FLEXIBLE_TERMS = ("flexible", "hybrid", "or")

EXP_PATTERNS: dict[str, re.Pattern] = {
    "0-1": re.compile(r"\b0\s*[-–]\s*1\b|\b0\b.*\byear|\bfresher\b|\bentry.?level\b|\bintern\b", re.I),
    "1-2": re.compile(r"\b1\s*[-–]\s*2\b|\b1\s*[-–]\s*3\b|\b1\b.*\byear", re.I),
    "2-3": re.compile(r"\b2\s*[-–]\s*3\b|\b2\s*[-–]\s*4\b|\b2\b.*\byear|\b3\b.*\byear", re.I),
    "3+":  re.compile(r"\b[3-9]\s*[-–]\s*\d+\b|\b[3-9]\+?\s*year|\b1[0-9]\b.*\byear|\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b", re.I),
}


def _job_filters_sql(
    role: str,
    location: str = "",
    remote_type: str = "all",
    exclude_loc: str = "",
    p: str = "",
) -> tuple[str, list[str]]:
    normalized_title = (
        f"LOWER(' ' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
        f"{p}title, '/', ' '), '-', ' '), '(', ' '), ')', ' '), ',', ' ') || ' ')"
    )
    norm_loc = (
        f"(' ' || LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
        f"{p}location, '/', ' '), '-', ' '), '(', ' '), ')', ' '), ',', ' ')) || ' ')"
    )
    clauses: list[str] = []
    params: list[str] = []

    if role != "all":
        keywords = ROLE_PRESETS.get(role)
        if role == "target_all":
            keywords = ROLE_PRESETS["genai_agentic"] + ROLE_PRESETS["ml_nlp"] + ROLE_PRESETS["fde"]
        if not keywords:
            clauses.append("0")
        else:
            role_clauses = [f"{normalized_title} LIKE ?" for _ in keywords]
            role_params = [f"%{keyword.lower()}%" for keyword in keywords]
            role_sql = f"({' OR '.join(role_clauses)}"
            if role in ("target_all", "ml_nlp"):
                role_sql += f" OR ({normalized_title} LIKE ? AND {normalized_title} LIKE ?)"
                role_params.extend(("% ai %", "% software engineer %"))
            role_sql += ")"
            clauses.append(role_sql)
            params.extend(role_params)

            if role in ("target_all", "genai_agentic", "ml_nlp", "fde"):
                exclude_clauses = [f"{normalized_title} LIKE ?" for _ in NON_ENG_EXCLUDE]
                clauses.append(f"NOT ({' OR '.join(exclude_clauses)})")
                params.extend(f"%{keyword.lower()}%" for keyword in NON_ENG_EXCLUDE)

    if location.strip():
        if location.strip().lower() == "india":
            clauses.append(f"{norm_loc} LIKE ?")
            params.append("% india %")
        else:
            clauses.append(f"LOWER({p}location) LIKE ?")
            params.append(f"%{location.strip().lower()}%")

    remote_expr = f"{norm_loc} LIKE '% remote %'"

    def matching_terms(terms: tuple[str, ...]) -> tuple[str, list[str]]:
        return (
            "(" + " OR ".join(f"{norm_loc} LIKE ?" for _ in terms) + ")",
            [f"% {term} %" for term in terms],
        )

    if remote_type == "remote_any":
        clauses.append(remote_expr)
    elif remote_type == "remote_india":
        terms_sql, terms_params = matching_terms(REMOTE_INDIA_TERMS)
        clauses.extend((remote_expr, terms_sql))
        params.extend(terms_params)
    elif remote_type == "remote_global":
        global_sql, global_params = matching_terms(REMOTE_GLOBAL_TERMS)
        exclude_sql, exclude_params = matching_terms(REMOTE_GLOBAL_EXCLUDES)
        clauses.extend((remote_expr, f"({norm_loc} = ' remote ' OR {global_sql})", f"NOT {exclude_sql}"))
        params.extend(global_params)
        params.extend(exclude_params)
    elif remote_type == "remote_us":
        terms_sql, terms_params = matching_terms(REMOTE_US_TERMS)
        clauses.extend((remote_expr, terms_sql))
        params.extend(terms_params)
    elif remote_type == "remote_eu":
        terms_sql, terms_params = matching_terms(REMOTE_EU_TERMS)
        clauses.extend((remote_expr, terms_sql))
        params.extend(terms_params)
    elif remote_type == "remote_flexible":
        terms_sql, terms_params = matching_terms(REMOTE_FLEXIBLE_TERMS)
        clauses.extend((remote_expr, terms_sql))
        params.extend(terms_params)
    elif remote_type.startswith("custom:"):
        custom_location = remote_type[len("custom:"):].strip().lower()
        if custom_location:
            clauses.append(f"LOWER({p}location) LIKE ?")
            params.append(f"%{custom_location}%")
        else:
            clauses.append("0")
    elif remote_type != "all":
        clauses.append("0")

    excluded_locations = []
    for term in exclude_loc.split(","):
        normalized_term = " ".join(re.sub(r"[/\-(),]+", " ", term.lower()).split())
        if normalized_term:
            excluded_locations.append(normalized_term)
    if excluded_locations:
        clauses.append(
            "NOT (" + " OR ".join(f"{norm_loc} LIKE ?" for _ in excluded_locations) + ")"
        )
        params.extend(f"% {term} %" for term in excluded_locations)

    return (" AND " + " AND ".join(clauses), params) if clauses else ("", [])


def _remote_location_bucket(location: str) -> str | None:
    normalized = " " + " ".join(
        re.sub(r"[/\-(),]+", " ", location.lower()).split()
    ) + " "
    if " remote " not in normalized:
        return None

    def has_term(terms: tuple[str, ...]) -> bool:
        return any(f" {term} " in normalized for term in terms)

    if has_term(REMOTE_FLEXIBLE_TERMS):
        return "flexible"
    if has_term(REMOTE_INDIA_TERMS):
        return "india"
    if has_term(REMOTE_US_TERMS):
        return "us"
    if has_term(REMOTE_EU_TERMS):
        return "eu"
    if (
        normalized == " remote "
        or has_term(REMOTE_GLOBAL_TERMS)
    ) and not has_term(REMOTE_GLOBAL_EXCLUDES):
        return "global"
    return None


def _matches_exp(title: str, exp: str, desc: str = "") -> bool:
    if exp == "all":
        return True
    pat = EXP_PATTERNS.get(exp)
    return bool(pat and pat.search(f"{title} {desc}"))


def _exp_label(title: str, desc: str = "") -> str:
    for label, pat in EXP_PATTERNS.items():
        if pat.search(f"{title} {desc}"):
            return label
    return ""


@app.get("/api/jobs")
def search_jobs(
    ats: str = "all",
    q: str = "",
    location: str = "",
    role: str = "all",
    exp: str = "all",
    limit: int = Query(300, ge=1, le=2000),
    remote_type: str = "all",
    exclude_loc: str = "",
):
    clause, extra = _ats_clause(ats)
    job_filter_sql, job_filter_params = _job_filters_sql(
        role, location, remote_type, exclude_loc
    )
    sql = f"SELECT ats, company_name, title, location, absolute_url, updated_at, description FROM jobs WHERE title LIKE ? {clause}{job_filter_sql} ORDER BY updated_at DESC LIMIT ?"
    with get_db() as conn:
        rows = conn.execute(
            sql,
            [f"%{q}%", *extra, *job_filter_params, limit],
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        description = d.pop("description", "")
        if not _matches_exp(d["title"], exp, description):
            continue
        d["exp_label"] = _exp_label(d["title"], description) or "Unspecified"
        results.append(d)
    return results


@app.get("/api/remote-types")
def list_remote_types() -> list[dict[str, str]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT location, COUNT(*) AS cnt FROM jobs "
            "WHERE LOWER(location) LIKE '%remote%' "
            "GROUP BY location ORDER BY cnt DESC"
        ).fetchall()
    return [
        {
            "value": f"custom:{row['location']}",
            "label": f"Remote: {row['location']} ({row['cnt']})",
        }
        for row in rows
        if _remote_location_bucket(row["location"]) is None
    ][:30]


def _style_sheet(ws, headers: list[str], widths: list[int]) -> None:
    fill, font = PatternFill("solid", fgColor="1F2937"), Font(bold=True, color="FFFFFF", size=11)
    ws.append(headers)
    for idx, w in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill, cell.font, cell.alignment = fill, font, Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"


@app.get("/api/export/companies.xlsx")
def export_xlsx(
    ats: str = "all",
    q: str = "",
    min_jobs: int = 0,
    include_jobs: bool = True,
    remote_type: str = "all",
    exclude_loc: str = "",
):
    c_clause, c_extra = _ats_clause(ats, "ats")
    j_clause, j_extra = _ats_clause(ats, "j.ats")
    remote_filter_sql, remote_filter_params = _job_filters_sql(
        "all", remote_type=remote_type, exclude_loc=exclude_loc, p="j."
    )
    with get_db() as conn:
        companies = conn.execute(
            f"SELECT ats, name, token, job_count, board_url, career_url, updated_at FROM companies WHERE (name LIKE ? OR token LIKE ?) AND job_count >= ? {c_clause} ORDER BY job_count DESC, name ASC",
            [f"%{q}%", f"%{q}%", min_jobs, *c_extra],
        ).fetchall()
        jobs = conn.execute(
            f"SELECT j.ats, j.company_name, j.title, j.location, j.absolute_url, j.updated_at FROM jobs j JOIN companies c ON j.ats = c.ats AND j.company_token = c.token WHERE (c.name LIKE ? OR c.token LIKE ?) AND c.job_count >= ? {j_clause}{remote_filter_sql} ORDER BY j.company_name ASC, j.updated_at DESC",
            [f"%{q}%", f"%{q}%", min_jobs, *j_extra, *remote_filter_params],
        ).fetchall() if include_jobs else []

    wb = Workbook()
    zebra, link = PatternFill("solid", fgColor="F9FAFB"), Font(color="2563EB", underline="single")

    ws1 = wb.active
    ws1.title = "Companies"
    _style_sheet(ws1, ["ATS", "Company Name", "Board Token", "Open Jobs", "Careers Page", "Direct Careers", "Updated (UTC)"], [15, 30, 24, 12, 46, 46, 26])
    for i, c in enumerate(companies, start=2):
        ws1.append([c["ats"].title(), c["name"], c["token"], c["job_count"], c["board_url"], c["career_url"] or "", c["updated_at"]])
        ws1.cell(row=i, column=5).hyperlink = c["board_url"]
        ws1.cell(row=i, column=5).font = link
        if c["career_url"]:
            ws1.cell(row=i, column=6).hyperlink = c["career_url"]
            ws1.cell(row=i, column=6).font = link
        if i % 2 == 0:
            for col in range(1, 8):
                ws1.cell(row=i, column=col).fill = zebra
    ws1.auto_filter.ref = ws1.dimensions

    if include_jobs:
        ws2 = wb.create_sheet("Live Jobs")
        _style_sheet(ws2, ["ATS", "Company", "Role Title", "Location", "Apply Link", "Updated At"], [15, 28, 42, 28, 48, 26])
        for i, j in enumerate(jobs, start=2):
            ws2.append([j["ats"].title(), j["company_name"], j["title"], j["location"], j["absolute_url"], j["updated_at"]])
            ws2.cell(row=i, column=5).hyperlink = j["absolute_url"]
            ws2.cell(row=i, column=5).font = link
            if i % 2 == 0:
                for col in range(1, 7):
                    ws2.cell(row=i, column=col).fill = zebra
        ws2.auto_filter.ref = ws2.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"ats_companies_{ats}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)