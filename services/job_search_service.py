"""Job search service — proxies JSearch (RapidAPI) for LinkedIn/Indeed/Glassdoor jobs.

API docs: https://rapidapi.com/letscrape-6bfWPX/api/jsearch
Free tier: 500 requests / month.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from resources.constants import (
    CACHE_TTL_SECONDS,
    JSEARCH_NUM_PAGES,
    JSEARCH_TIMEOUT_SECONDS,
)

logger = logging.getLogger("job_search_service")

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "resources" / "config.env"

_JSEARCH_HOST = "jsearch.p.rapidapi.com"
_JSEARCH_URL  = f"https://{_JSEARCH_HOST}/search"

# Map UI date-filter values → JSearch date_posted param
_DATE_MAP: dict[str, str] = {
    "any":    "all",
    "24h":    "today",
    "3days":  "3days",
    "week":   "week",
    "month":  "month",
}

# ---------------------------------------------------------------------------
# File-based cache — survives Flask debug reloader process restarts
# ---------------------------------------------------------------------------

_CACHE_TTL  = CACHE_TTL_SECONDS
_CACHE_DIR  = Path(__file__).resolve().parent.parent / ".cache"
_CACHE_FILE = _CACHE_DIR / "job_search_cache.json"


def _load_cache() -> dict:
    """Read the on-disk cache file; return empty dict on any error."""
    try:
        with open(_CACHE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    """Atomically write the cache dict to disk."""
    _CACHE_DIR.mkdir(exist_ok=True)
    tmp = _CACHE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        tmp.replace(_CACHE_FILE)
    except OSError as exc:
        logger.warning("Could not write job search cache: %s", exc)
        tmp.unlink(missing_ok=True)


def _make_cache_key(query: str, location: str, date_posted: str) -> str:
    return f"{query.strip().lower()}_{location.strip().lower()}_{date_posted.strip()}"


def _cache_get(key: str) -> list[dict] | None:
    """Return cached jobs if present and not expired, else None."""
    data = _load_cache()
    entry = data.get(key)
    if entry is None:
        return None
    if time.time() - entry["timestamp"] > _CACHE_TTL:
        # Remove expired entry and persist
        del data[key]
        _save_cache(data)
        logger.debug("Cache expired for key=%r", key)
        return None
    logger.info("Cache hit for key=%r (%d jobs)", key, len(entry["data"]))
    return entry["data"]


def _cache_set(key: str, jobs: list[dict]) -> None:
    data = _load_cache()
    data[key] = {"timestamp": time.time(), "data": jobs}
    _save_cache(data)
    logger.info("Cache stored for key=%r (%d jobs)", key, len(jobs))


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicate jobs by (title, company, location) tuple."""
    seen: set[tuple] = set()
    unique: list[dict] = []
    for job in jobs:
        fingerprint = (
            job.get("title", "").lower().strip(),
            job.get("company", "").lower().strip(),
            job.get("location", "").lower().strip(),
        )
        if fingerprint not in seen:
            unique.append(job)
            seen.add(fingerprint)
    dupes = len(jobs) - len(unique)
    if dupes:
        logger.debug("Deduplication removed %d duplicate job(s)", dupes)
    return unique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_api_key() -> str:
    """Return JSEARCH_API_KEY from resources/config.env, falling back to env var."""
    if _CONFIG_FILE.is_file():
        with open(_CONFIG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "JSEARCH_API_KEY":
                    val = value.strip()
                    if val and val != "your-jsearch-rapidapi-key-here":
                        return val
    return os.environ.get("JSEARCH_API_KEY", "").strip()


def _build_smart_query(raw: str, location: str) -> str:
    """Convert user input to a JSearch query string.

    Comma / slash / pipe / semicolon delimiters → OR semantics for both
    role and location fields:
      "devops, sre"          → "(devops) OR (sre)"
      "noida, gurgaon"       → "(noida) OR (gurgaon)"
      "devops, sre" + "noida, gurgaon"
        → "(devops) OR (sre) (noida) OR (gurgaon)"
    """
    def _or_join(text: str) -> str:
        parts = [p.strip() for p in re.split(r'[,/|;]+', text) if p.strip()]
        if len(parts) > 1:
            return " OR ".join(f"({p})" for p in parts)
        return text.strip()

    joined = _or_join(raw)

    if location:
        joined = f"{joined} {_or_join(location)}"
    return joined


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_jobs(
    query: str,
    location: str = "",
    date_posted: str = "any",
    page: int = 1,
    num_pages: int = JSEARCH_NUM_PAGES,
) -> dict:
    """Search jobs via JSearch API with caching and deduplication.

    Returns a dict with keys:
      - ``jobs``: list of normalised, deduplicated job dicts
      - ``error``: non-empty string on failure
      - ``cached``: True if result came from cache
    """
    api_key = _load_api_key()
    if not api_key:
        return {
            "jobs": [],
            "error": (
                "JSearch API key is not configured. "
                "Add JSEARCH_API_KEY to resources/config.env. "
                "Get a free key at https://rapidapi.com/letscrape-6bfWPX/api/jsearch"
            ),
            "cached": False,
        }

    # ── Cache lookup ─────────────────────────────────────────────────────
    cache_key = _make_cache_key(query, location, date_posted)
    cached_jobs = _cache_get(cache_key)
    if cached_jobs is not None:
        return {"jobs": cached_jobs, "error": "", "cached": True}

    # ── Fetch from API ───────────────────────────────────────────────────
    full_query = _build_smart_query(query, location)

    import random
    rand_page = random.randint(1, 10)

    params: dict = {
        "query":       full_query,
        "num_pages":   str(num_pages),
        "date_posted": _DATE_MAP.get(date_posted, "all"),
        "sort_by":     "RELEVANCE",
    }
    headers = {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": _JSEARCH_HOST,
    }

    try:
        resp = requests.get(_JSEARCH_URL, params=params, headers=headers, timeout=JSEARCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("JSearch API timeout for query=%r", full_query)
        return {"jobs": [], "error": "Job search timed out. Please try again.", "cached": False}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if status in (401, 403):
            return {"jobs": [], "error": "Invalid JSearch API key. Check resources/config.env.", "cached": False}
        if status == 429:
            return {"jobs": [], "error": "JSearch API rate limit reached. Try again later.", "cached": False}
        logger.warning("JSearch HTTP %s for query=%r", status, full_query)
        return {"jobs": [], "error": f"Job search API returned error {status}.", "cached": False}
    except requests.exceptions.RequestException as exc:
        logger.warning("JSearch network error: %s", exc)
        return {"jobs": [], "error": "Network error reaching job search API.", "cached": False}

    # ── Normalise + deduplicate + cache ──────────────────────────────────
    raw_jobs: list[dict] = data.get("data") or []
    jobs = _deduplicate([_normalise(j) for j in raw_jobs])
    logger.info("JSearch returned %d jobs (after dedup) for query=%r", len(jobs), full_query)

    _cache_set(cache_key, jobs)
    return {"jobs": jobs, "error": "", "cached": False}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(raw: dict) -> dict:
    """Extract the fields we care about from a raw JSearch job object."""
    posted_str = raw.get("job_posted_at_human_readable") or raw.get("job_posted_at_datetime_utc") or ""
    if posted_str and "T" in posted_str:
        posted_str = posted_str[:10]

    return {
        "id":          raw.get("job_id", ""),
        "title":       raw.get("job_title", "").strip(),
        "company":     raw.get("employer_name", "").strip(),
        "location":    _build_location(raw),
        "apply_link":  raw.get("job_apply_link", "").strip(),
        "posted_at":   posted_str,
        "employment_type": (raw.get("job_employment_type") or "").replace("_", " ").title(),
        "logo":        raw.get("employer_logo", ""),
        "description_snippet": (raw.get("job_description") or "")[:200].strip(),
        "description_full":    (raw.get("job_description") or "")[:5000].strip(),
    }


def _build_location(raw: dict) -> str:
    parts = []
    city    = (raw.get("job_city")    or "").strip()
    state   = (raw.get("job_state")   or "").strip()
    country = (raw.get("job_country") or "").strip()
    if city:
        parts.append(city)
    if state and state != city:
        parts.append(state)
    if country:
        parts.append(country)
    loc = ", ".join(parts)
    if raw.get("job_is_remote"):
        return f"Remote — {loc}" if loc else "Remote"
    return loc or "Location not specified"


def _build_location(raw: dict) -> str:
    parts = []
    city    = (raw.get("job_city")    or "").strip()
    state   = (raw.get("job_state")   or "").strip()
    country = (raw.get("job_country") or "").strip()
    if city:
        parts.append(city)
    if state and state != city:
        parts.append(state)
    if country:
        parts.append(country)
    loc = ", ".join(parts)
    if raw.get("job_is_remote"):
        return f"Remote — {loc}" if loc else "Remote"
    return loc or "Location not specified"
