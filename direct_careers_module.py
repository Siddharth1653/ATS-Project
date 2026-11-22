from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from freehire_module import _experience_fields

DB_PATH = "ats_finder.db"
DEFAULT_SOURCES = "yc,hn,dotai"
CAREER_PATHS = ("/careers", "/jobs", "/join-us")
CAREER_LINK_TERMS = ("career", "jobs", "hiring", "join-us")
SUPPORTED_ATS_DOMAINS = (
    "greenhouse.io", "ashbyhq.com", "lever.co", "smartrecruiters.com",
    "workable.com", "recruitee.com", "keka.com", "zohorecruit.in",
    "kula.ai", "darwinbox.in", "darwinbox.com", "pinpointhq.com",
    "bamboohr.com", "breezy.hr", "teamtailor.com",
)
AGGREGATOR_DOMAINS = (
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.com",
    "freehire.me", "remoteok.com", "arbeitnow.com", "himalayas.app",
    "remotive.com",
)
LEGAL_SUFFIXES = {
    "inc", "llc", "ltd", "pvt", "private", "limited", "corp",
    "corporation", "technologies", "labs",
}
CLEARBIT_URL = "https://autocomplete.clearbit.com/v1/companies/suggest"
SOCIAL_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "tiktok.com", "threads.net", "reddit.com",
    "news.ycombinator.com", "ycombinator.com", "github.com",
)
_COMPANY_TERMS = (
    "ai", "genai", "gen ai", "generative ai", "llm", "llms",
    "large language model", "machine learning", "ml", "nlp",
    "agent", "agentic", "data",
)
_JOB_TERMS = (
    "ai", "genai", "gen ai", "generative ai", "agentic", "agent",
    "llm", "llms", "large language model", "machine learning", "ml",
    "nlp", "natural language", "forward deployed", "fde",
)
_AI_TEXT_PATTERN = re.compile(
    r"\b(?:ai|gen\s?ai|genai|generative\s+ai|llms?|large\s+language\s+models?|"
    r"machine\s+learning|ml|nlp|natural\s+language|agentic|agents?|"
    r"forward\s+deployed|fde)\b",
    re.I,
)
_RAW_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.I)

router = APIRouter(prefix="/api/direct", tags=["direct_careers"])
sync_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "domains_found": 0,
    "pages_found": 0,
    "jobs_saved": 0,
    "auto_enabled": False,
    "interval_hours": 12,
    "last_run": None,
    "error": None,
}
_auto_task: asyncio.Task | None = None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.text_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a":
            self._anchor_href = attrs_dict.get("href", "")
            self._anchor_text = []
        elif tag.lower() == "script" and attrs_dict.get("type", "").lower().startswith("application/ld+json"):
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_href is not None:
            self.anchors.append((self._anchor_href, " ".join(self._anchor_text).strip()))
            self._anchor_href = None
            self._anchor_text = []
        elif tag.lower() == "script" and self._script_parts is not None:
            self.jsonld.append("".join(self._script_parts))
            self._script_parts = None


class _CommentTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _host_is_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _is_filtered_domain(host: str) -> bool:
    return any(
        _host_is_domain(host, domain)
        for domain in (*SUPPORTED_ATS_DOMAINS, *SOCIAL_DOMAINS)
    )


def _safe_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not _is_filtered_domain(host)
    return address.is_global


def _career_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    url = raw if re.match(r"^https?://", raw, re.I) else f"https://{raw.lstrip('/')}"
    return url if _safe_url(url) else None


def _company_domain(value: Any) -> str | None:
    url = _career_url(value)
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    labels = host.split(".")
    while len(labels) > 2 and labels[0] in {"www", "careers", "jobs"}:
        labels.pop(0)
    domain = ".".join(labels)
    if not domain or _is_filtered_domain(domain):
        return None
    return domain


def _has_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text, re.I) for term in terms)


def _matching_job(title: str, description: str = "") -> bool:
    return bool(_AI_TEXT_PATTERN.search(f"{title} {description}"))


def _company_record(name: Any, website: Any, career_url: Any = None) -> tuple[str, str, str | None] | None:
    domain = _company_domain(website or career_url)
    if not domain:
        return None
    known_career_url = _career_url(career_url)
    company_name = str(name or "").strip() or domain
    return company_name, domain, known_career_url


def _dedupe_companies(
    records: list[tuple[str, str, str | None]],
) -> list[tuple[str, str, str | None]]:
    unique: dict[str, tuple[str, str, str | None]] = {}
    for name, domain, career_url in records:
        key = domain.lower()
        previous = unique.get(key)
        if previous is None or (career_url and not previous[2]):
            unique[key] = (name, key, career_url or (previous[2] if previous else None))
    return list(unique.values())


async def _fetch_yc(client: httpx.AsyncClient, limit: int) -> list[tuple[str, str, str | None]]:
    response = await client.get("https://yc-oss.github.io/api/companies/hiring.json")
    response.raise_for_status()
    payload = response.json()
    rows = payload if isinstance(payload, list) else payload.get("companies", []) if isinstance(payload, dict) else []
    records = []
    for company in rows:
        if not isinstance(company, dict):
            continue
        tags = company.get("tags") or []
        industries = company.get("industries") or []
        text = " ".join(
            str(value)
            for value in (
                tags if isinstance(tags, list) else [tags]
            ) + (
                industries if isinstance(industries, list) else [industries]
            ) + [
                company.get("one_liner") or "",
                company.get("long_description") or "",
            ]
        )
        if not _has_term(text, _COMPANY_TERMS):
            continue
        record = _company_record(
            company.get("name") or company.get("company_name"),
            company.get("website") or company.get("url"),
            company.get("career_url") or company.get("careers_url"),
        )
        if record:
            records.append(record)
            if len(records) >= limit:
                break
    return _dedupe_companies(records)


def _plain_comment(value: Any) -> str:
    parser = _CommentTextParser()
    parser.feed(str(value or ""))
    return " ".join(" ".join(parser.parts).split())


def _comment_company_name(text: str, domain: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text.strip())
    first_line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", first_line)
    name = re.split(r"\s+(?:\||–|—)\s+|\s+-\s+", first_line, maxsplit=1)[0].strip(" :|,-")
    if not name or len(name) > 100:
        name = domain.split(".")[0].replace("-", " ").title()
    return name or domain


async def _fetch_hn(client: httpx.AsyncClient, limit: int) -> list[tuple[str, str, str | None]]:
    response = await client.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={
            "query": "Ask HN Who is hiring",
            "tags": "story",
            "hitsPerPage": 2,
        },
    )
    response.raise_for_status()
    stories = response.json().get("hits") or []
    if not stories:
        return []
    story_id = stories[0].get("objectID")
    if not story_id:
        return []

    response = await client.get(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": f"comment,story_{story_id}", "hitsPerPage": 200},
    )
    response.raise_for_status()
    records = []
    for comment in response.json().get("hits") or []:
        text = _plain_comment(comment.get("comment_text"))
        if not _has_term(text, _JOB_TERMS):
            continue
        raw_text = str(comment.get("comment_text") or "")
        urls = [
            match.group(0).rstrip(".,;:!?)]}")
            for match in _RAW_URL_PATTERN.finditer(raw_text)
        ]
        for url in dict.fromkeys(urls):
            record = _company_record(None, url, url if _has_term(url.lower(), CAREER_LINK_TERMS) else None)
            if record:
                _, domain, career_url = record
                records.append((_comment_company_name(text, domain), domain, career_url))
                if len(records) >= limit:
                    return _dedupe_companies(records)
    return _dedupe_companies(records)


def _cdx_originals(response_text: str) -> list[str]:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, list):
        rows = payload
        if rows and rows[0] in (["original"], "original"):
            rows = rows[1:]
        return [
            str(row[0] if isinstance(row, list) and row else row).strip()
            for row in rows
            if isinstance(row, (str, list)) and row
        ]
    originals = []
    for line in response_text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = line
        if isinstance(row, list):
            row = row[0] if row else ""
        if isinstance(row, str) and row.strip().lower() != "original":
            originals.append(row.strip())
    return originals


async def _fetch_dotai(client: httpx.AsyncClient, limit: int) -> list[tuple[str, str, str | None]]:
    records = []
    for pattern in ("*.ai/careers*", "*.ai/jobs*"):
        response = await client.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url": pattern,
                "output": "json",
                "fl": "original",
                "collapse": "urlkey",
                "limit": limit,
            },
        )
        response.raise_for_status()
        for candidate in _cdx_originals(response.text):
            if not candidate:
                continue
            url = candidate if re.match(r"^https?://", candidate, re.I) else f"https://{candidate}"
            record = _company_record(None, url, url)
            if record and record[1].endswith(".ai"):
                records.append(record)
                if len(records) >= limit:
                    return _dedupe_companies(records)
    return _dedupe_companies(records)


def _existing_domains() -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return {
            str(row[0]).lower()
            for row in conn.execute("SELECT LOWER(token) FROM companies WHERE ats = 'direct'")
        }


def _supported_ats_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(_host_is_domain(host, domain) for domain in SUPPORTED_ATS_DOMAINS)


def _is_aggregator_domain(host: str) -> bool:
    return any(
        _host_is_domain(host, domain)
        for domain in (*SUPPORTED_ATS_DOMAINS, *SOCIAL_DOMAINS, *AGGREGATOR_DOMAINS)
    )


def _company_domain_from_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    url = raw if re.match(r"^https?://", raw, re.I) else f"https://{raw}"
    try:
        if not _safe_url(url):
            return None
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return None if _is_aggregator_domain(host) else host


def _company_words(value: Any) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    words = [word for word in words if word not in {"the", "company", "group"}]
    if len(words) > 1:
        words = [word for word in words if word != "ai"]
    return words


def _strong_company_name_overlap(company_name: str, candidate_name: str) -> bool:
    company_words = set(_company_words(company_name))
    candidate_words = set(_company_words(candidate_name))
    if not company_words or not candidate_words:
        return False
    shared = company_words & candidate_words
    return bool(shared) and len(shared) / min(len(company_words), len(candidate_words)) >= 0.6


def _page_title(page_text: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", page_text, re.I | re.S)
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    return " ".join(html.unescape(title).split())


async def _resolve_company_domain(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    board_url: str,
) -> str | None:
    domain = _company_domain_from_url(board_url)
    if domain:
        return domain

    company_name = name.strip() or token.strip()
    if company_name:
        try:
            response = await client.get(
                CLEARBIT_URL,
                params={"query": company_name},
                timeout=6.0,
            )
            response.raise_for_status()
            suggestions = response.json()
            if isinstance(suggestions, list):
                for suggestion in suggestions:
                    if not isinstance(suggestion, dict):
                        continue
                    if not _strong_company_name_overlap(
                        company_name,
                        str(suggestion.get("name") or ""),
                    ):
                        continue
                    domain = _company_domain_from_url(suggestion.get("domain"))
                    if domain:
                        return domain
        except (httpx.HTTPError, ValueError):
            pass

    fallback_name = name.strip()
    words = _company_words(fallback_name)
    if not words:
        fallback_name = token.strip()
        if "." in fallback_name:
            fallback_name = fallback_name.split(".", 1)[0]
        words = _company_words(fallback_name)
    if not words:
        return None
    slug = re.sub(r"[^a-z0-9]", "", "".join(words))[:63]
    main_word = words[0]
    if not slug or not main_word:
        return None
    for suffix in (".com", ".ai", ".io", ".in", ".co"):
        candidate = f"{slug}{suffix}"
        try:
            response = await client.get(
                f"https://{candidate}",
                timeout=5.0,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            continue
        title = _page_title(response.text) if response.status_code == 200 else ""
        if response.status_code == 200 and re.search(
            rf"\b{re.escape(main_word)}\b",
            title,
            re.I,
        ):
            return candidate
    return None


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str] | None:
    current_url = url
    for _ in range(6):
        if not _safe_url(current_url) or _supported_ats_url(current_url):
            return None
        try:
            response = await client.get(
                current_url,
                timeout=20.0,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            redirect = response.headers.get("location")
            if not redirect:
                return None
            current_url = urljoin(current_url, redirect)
            continue
        if response.status_code != 200:
            return None
        final_url = str(response.url or current_url)
        if not _safe_url(final_url) or _supported_ats_url(final_url):
            return None
        return response, final_url
    return None


async def _find_career_page_impl(
    client: httpx.AsyncClient,
    domain: str,
    known_url: str | None,
    homepage_fallback: bool = False,
) -> tuple[str, str, bool] | None:
    if known_url:
        page = await _fetch_page(client, known_url)
        if page:
            return page[1], page[0].text, True

    homepage = await _fetch_page(client, f"https://{domain}")
    if not homepage:
        homepage = await _fetch_page(client, f"http://{domain}")
    if homepage:
        parser = _PageParser()
        parser.feed(homepage[0].text)
        candidates = []
        for href, _ in parser.anchors:
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            if any(term in href.lower() for term in CAREER_LINK_TERMS):
                candidate = urljoin(homepage[1], href)
                if candidate not in candidates:
                    candidates.append(candidate)
        for candidate in candidates:
            page = await _fetch_page(client, candidate)
            if page:
                return page[1], page[0].text, True

    probes = [f"https://{domain}{path}" for path in CAREER_PATHS]
    probes.extend((f"https://careers.{domain}", f"https://jobs.{domain}"))
    for candidate in probes:
        page = await _fetch_page(client, candidate)
        if page:
            return page[1], page[0].text, True
    if homepage_fallback and homepage:
        homepage_path = (urlparse(homepage[1]).path or "").lower()
        is_career_page = any(term in homepage_path for term in CAREER_LINK_TERMS)
        return homepage[1], homepage[0].text, is_career_page
    return None


async def _find_career_page(
    client: httpx.AsyncClient,
    domain: str,
    known_url: str | None,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str] | None:
    async with semaphore:
        result = await _find_career_page_impl(client, domain, known_url)
    return result[:2] if result else None


def _jsonld_objects(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_objects(item)
    elif isinstance(value, dict):
        raw_type = value.get("@type") or []
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(item).rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower() == "jobposting" for item in types):
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _jsonld_objects(child)


def _location_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [_location_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    address = value.get("address")
    if isinstance(address, dict):
        parts = []
        for key in ("streetAddress", "addressLocality", "addressRegion", "addressCountry"):
            item = address.get(key)
            if isinstance(item, dict):
                item = item.get("name") or item.get("addressCountry")
            if item:
                parts.append(str(item).strip())
        if parts:
            return ", ".join(parts)
    return str(value.get("name") or "")


def _direct_job(
    domain: str,
    title: str,
    description: str,
    location: str,
    url: str,
    updated_at: Any,
) -> dict[str, Any]:
    digest = hashlib.sha256(f"{url}|{title}".encode("utf-8")).hexdigest()[:16]
    exp_min, exp_max, exp_label = _experience_fields({
        "title": title,
        "description": description,
    })
    return {
        "id": f"direct_{domain}_{digest}",
        "title": title,
        "location": location or "Unknown",
        "absolute_url": url,
        "updated_at": str(updated_at or ""),
        "description": description,
        "exp_min": exp_min,
        "exp_max": exp_max,
        "exp_label": exp_label,
    }


def _extract_direct_jobs(domain: str, career_url: str, page_text: str) -> list[dict[str, Any]]:
    parser = _PageParser()
    parser.feed(page_text)
    jobs = []
    seen: set[str] = set()
    has_job_posting = False

    def add_job(title: Any, description: Any, location: str, url: Any, updated_at: Any) -> None:
        title_text = str(title or "").strip()
        description_text = str(description or "")
        if not title_text or not _matching_job(title_text, description_text):
            return
        job_url = urljoin(career_url, str(url or career_url))
        if not _safe_url(job_url) or _supported_ats_url(job_url):
            return
        job = _direct_job(
            domain,
            title_text,
            description_text,
            location,
            job_url,
            updated_at,
        )
        if job["id"] not in seen:
            seen.add(job["id"])
            jobs.append(job)

    for script in parser.jsonld:
        try:
            payload = json.loads(script)
        except json.JSONDecodeError:
            continue
        for posting in _jsonld_objects(payload):
            has_job_posting = True
            title = posting.get("title") or posting.get("name")
            description = posting.get("description") or ""
            location = _location_text(posting.get("jobLocation"))
            remote_value = posting.get("jobLocationType") or ""
            remote_types = remote_value if isinstance(remote_value, list) else [remote_value]
            remote = any("telecommute" in str(item).lower() for item in remote_types)
            if remote and not location.lower().endswith("(remote)"):
                location = f"{location} (Remote)" if location else "Remote"
            add_job(
                title,
                description,
                location,
                posting.get("url") or career_url,
                posting.get("datePosted"),
            )

    if not has_job_posting:
        for href, anchor_text in parser.anchors:
            title = " ".join(anchor_text.split())
            normalized = title.lower().strip(" .:-")
            if not title or normalized in {"all jobs", "careers", "jobs", "view all jobs", "view careers"}:
                continue
            if not _matching_job(title):
                continue
            add_job(title, "", "Unknown", href, "")
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
        if "career_url" not in columns:
            try:
                conn.execute("ALTER TABLE companies ADD COLUMN career_url TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                if "career_url" not in {
                    row[1] for row in conn.execute("PRAGMA table_info(companies)")
                }:
                    raise
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


def _save_company_and_jobs(
    name: str,
    domain: str,
    career_url: str,
    jobs: list[dict[str, Any]],
) -> None:
    _init_database()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO companies (ats, token, name, board_url, job_count, updated_at, career_url)
            VALUES ('direct', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ats, token) DO UPDATE SET
                name = excluded.name,
                board_url = excluded.board_url,
                job_count = excluded.job_count,
                updated_at = excluded.updated_at,
                career_url = excluded.career_url
            """,
            (domain, name, career_url, len(jobs), now, career_url),
        )
        conn.execute(
            "DELETE FROM jobs WHERE ats = 'direct' AND company_token = ?",
            (domain,),
        )
        if jobs:
            conn.executemany(
                """
                INSERT INTO jobs (
                    id, ats, company_token, company_name, title, location,
                    absolute_url, updated_at, description, exp_min, exp_max, exp_label
                )
                VALUES (?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
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
                WHERE jobs.ats = 'direct'
                """,
                [
                    (
                        job["id"], domain, name, job["title"], job["location"],
                        job["absolute_url"], job["updated_at"] or None,
                        job["description"], job["exp_min"], job["exp_max"],
                        job["exp_label"],
                    )
                    for job in jobs
                ],
            )


async def run_direct_sync(
    sources: str = DEFAULT_SOURCES,
    limit: int = 150,
    skip_existing: bool = True,
) -> None:
    if sync_state["running"] and sync_state["stage"] != "queued":
        return
    sync_state.update({
        "running": True,
        "stage": "discovering",
        "domains_found": 0,
        "pages_found": 0,
        "jobs_saved": 0,
        "error": None,
    })
    errors = []
    try:
        _init_database()
        selected_sources = [source.strip().lower() for source in sources.split(",") if source.strip()]
        records: list[tuple[str, str, str | None]] = []
        async with httpx.AsyncClient(
            headers={"Accept": "application/json,text/html", "User-Agent": "DirectCareerFinder/1.0"},
            follow_redirects=True,
            timeout=25.0,
        ) as client:
            for source in selected_sources:
                try:
                    if source == "yc":
                        records.extend(await _fetch_yc(client, limit))
                    elif source == "hn":
                        records.extend(await _fetch_hn(client, limit))
                    elif source == "dotai":
                        records.extend(await _fetch_dotai(client, limit))
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    errors.append(f"{source}: {exc}")

            companies = _dedupe_companies(records)
            sync_state["domains_found"] = len(companies)
            if skip_existing:
                existing = _existing_domains()
                companies = [company for company in companies if company[1].lower() not in existing]
            companies = companies[:limit]

            semaphore = asyncio.Semaphore(10)

            async def inspect_company(
                company: tuple[str, str, str | None],
            ) -> tuple[str, str, str, list[dict[str, Any]]] | None:
                name, domain, known_url = company
                found = await _find_career_page(client, domain, known_url, semaphore)
                if not found:
                    return None
                career_url, page_text = found
                jobs = _extract_direct_jobs(domain, career_url, page_text)
                return name, domain, career_url, jobs

            results = await asyncio.gather(
                *(inspect_company(company) for company in companies),
                return_exceptions=True,
            )
            sync_state["stage"] = "saving"
            for result in results:
                if isinstance(result, BaseException):
                    if not isinstance(result, asyncio.CancelledError):
                        errors.append(str(result))
                    continue
                if not result:
                    continue
                name, domain, career_url, jobs = result
                _save_company_and_jobs(name, domain, career_url, jobs)
                sync_state["pages_found"] += 1
                sync_state["jobs_saved"] += len(jobs)

        sync_state["stage"] = "complete_with_errors" if errors else "complete"
        sync_state["error"] = "; ".join(errors) if errors else None
    except asyncio.CancelledError:
        sync_state["stage"] = "cancelled"
        raise
    except Exception as exc:
        sync_state["stage"] = "failed"
        sync_state["error"] = str(exc)
    finally:
        sync_state["running"] = False
        sync_state["last_run"] = datetime.now(timezone.utc).isoformat()


def _existing_companies(source_ats: str, limit: int, only_missing: bool) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    if source_ats.lower() != "all":
        clauses.append("LOWER(ats) = LOWER(?)")
        params.append(source_ats)
    if only_missing:
        clauses.append("COALESCE(career_url, '') = ''")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ats, token, name, board_url FROM companies"
            f"{where} ORDER BY name COLLATE NOCASE LIMIT ?",
            [*params, limit],
        ).fetchall()


def _save_enrichment_result(
    company: sqlite3.Row,
    domain: str,
    career_url: str,
    jobs: list[dict[str, Any]],
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with sqlite3.connect(DB_PATH) as conn:
        if career_url:
            conn.execute(
                "UPDATE companies SET career_url = ? WHERE ats = ? AND token = ?",
                (career_url, company["ats"], company["token"]),
            )
        for job in jobs:
            duplicate = conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE LOWER(company_name) = LOWER(?) AND LOWER(title) = LOWER(?)
                LIMIT 1
                """,
                (company["name"], job["title"]),
            ).fetchone()
            if duplicate:
                continue
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    id, ats, company_token, company_name, title, location,
                    absolute_url, updated_at, description, exp_min, exp_max, exp_label
                )
                VALUES (?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    job["id"], domain, company["name"], job["title"], job["location"],
                    job["absolute_url"], job["updated_at"] or None, job["description"],
                    job["exp_min"], job["exp_max"], job["exp_label"],
                ),
            )
            inserted += cursor.rowcount

        if inserted:
            conn.execute(
                """
                INSERT INTO companies (
                    ats, token, name, board_url, job_count, updated_at, career_url
                )
                VALUES ('direct', ?, ?, ?, 0, ?, ?)
                ON CONFLICT(ats, token) DO UPDATE SET
                    name = excluded.name,
                    board_url = excluded.board_url,
                    updated_at = excluded.updated_at,
                    career_url = excluded.career_url
                """,
                (domain, company["name"], career_url, now, career_url),
            )
            conn.execute(
                """
                UPDATE companies
                SET job_count = (
                    SELECT COUNT(*) FROM jobs WHERE ats = 'direct' AND company_token = ?
                )
                WHERE ats = 'direct' AND token = ?
                """,
                (domain, domain),
            )
    return inserted


async def _run_db_enrichment(
    source_ats: str,
    limit: int,
    only_missing: bool,
    scrape_jobs: bool,
) -> None:
    if sync_state["running"] and sync_state["stage"] != "queued":
        return
    sync_state.update({
        "running": True,
        "stage": "loading companies",
        "domains_found": 0,
        "pages_found": 0,
        "jobs_saved": 0,
        "error": None,
    })
    errors = []
    try:
        _init_database()
        companies = _existing_companies(source_ats, limit, only_missing)
        sync_state["stage"] = "resolving domains and career pages"
        semaphore = asyncio.Semaphore(8)
        async with httpx.AsyncClient(
            headers={"Accept": "application/json,text/html", "User-Agent": "DirectCareerFinder/1.0"},
            follow_redirects=True,
            timeout=25.0,
        ) as client:
            async def enrich_company(
                company: sqlite3.Row,
            ) -> tuple[sqlite3.Row, str, str, list[dict[str, Any]]] | None:
                async with semaphore:
                    domain = await _resolve_company_domain(
                        client,
                        str(company["name"] or ""),
                        str(company["token"] or ""),
                        str(company["board_url"] or ""),
                    )
                    if not domain:
                        return None
                    sync_state["domains_found"] += 1
                    result = await _find_career_page_impl(
                        client,
                        domain,
                        None,
                        homepage_fallback=True,
                    )
                    if not result:
                        return company, domain, "", []
                    career_url, page_text, is_career_page = result
                    sync_state["pages_found"] += 1
                    jobs = (
                        _extract_direct_jobs(domain, career_url, page_text)
                        if scrape_jobs and is_career_page
                        else []
                    )
                    return company, domain, career_url, jobs

            results = await asyncio.gather(
                *(enrich_company(company) for company in companies),
                return_exceptions=True,
            )
        sync_state["stage"] = "saving career pages and jobs"
        for result in results:
            if isinstance(result, BaseException):
                if not isinstance(result, asyncio.CancelledError):
                    errors.append(str(result))
                continue
            if not result:
                continue
            company, domain, career_url, jobs = result
            saved = _save_enrichment_result(company, domain, career_url, jobs)
            sync_state["jobs_saved"] += saved
        sync_state["stage"] = "complete_with_errors" if errors else "complete"
        sync_state["error"] = "; ".join(errors) if errors else None
    except asyncio.CancelledError:
        sync_state["stage"] = "cancelled"
        raise
    except Exception as exc:
        sync_state["stage"] = "failed"
        sync_state["error"] = str(exc)
    finally:
        sync_state["running"] = False
        sync_state["last_run"] = datetime.now(timezone.utc).isoformat()


async def _auto_discovery_loop() -> None:
    while sync_state["auto_enabled"]:
        if not sync_state["running"]:
            await run_direct_sync(DEFAULT_SOURCES, 150, True)
        await asyncio.sleep(sync_state["interval_hours"] * 3600)


async def _stop_auto_task() -> None:
    global _auto_task
    task = _auto_task
    _auto_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@router.post("/sync")
async def start_direct_sync(
    background_tasks: BackgroundTasks,
    sources: str = Query(DEFAULT_SOURCES),
    limit: int = Query(150, ge=20, le=1000),
    skip_existing: bool = Query(True),
) -> dict[str, Any]:
    selected = {source.strip().lower() for source in sources.split(",") if source.strip()}
    if not selected or selected - {"yc", "hn", "dotai"}:
        raise HTTPException(status_code=422, detail="sources must contain yc, hn, and/or dotai")
    if sync_state["running"]:
        return {"status": "already_running", "state": dict(sync_state)}
    sync_state.update({
        "running": True,
        "stage": "queued",
        "domains_found": 0,
        "pages_found": 0,
        "jobs_saved": 0,
        "error": None,
    })
    background_tasks.add_task(run_direct_sync, sources, limit, skip_existing)
    return {"status": "started"}


@router.post("/enrich-db")
async def start_db_enrichment(
    background_tasks: BackgroundTasks,
    source_ats: str = Query("all"),
    limit: int = Query(100, ge=10, le=1000),
    only_missing: bool = Query(True),
    scrape_jobs: bool = Query(True),
) -> dict[str, Any]:
    if sync_state["running"]:
        return {"status": "already_running", "state": dict(sync_state)}
    sync_state.update({
        "running": True,
        "stage": "queued",
        "domains_found": 0,
        "pages_found": 0,
        "jobs_saved": 0,
        "error": None,
    })
    background_tasks.add_task(
        _run_db_enrichment,
        source_ats.strip() or "all",
        limit,
        only_missing,
        scrape_jobs,
    )
    return {"status": "started"}


@router.get("/status")
def get_direct_status() -> dict[str, Any]:
    return dict(sync_state)


@router.post("/auto")
async def set_auto_discovery(
    enabled: bool,
    interval_hours: int = Query(12, ge=1, le=168),
) -> dict[str, Any]:
    global _auto_task
    sync_state["interval_hours"] = interval_hours
    if enabled:
        sync_state["auto_enabled"] = True
        if _auto_task is None or _auto_task.done():
            _auto_task = asyncio.create_task(_auto_discovery_loop())
    else:
        sync_state["auto_enabled"] = False
        await _stop_auto_task()
    return dict(sync_state)


