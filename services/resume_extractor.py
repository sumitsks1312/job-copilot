"""Structured extraction of resume fields into a normalised JSON dict.

This module extracts contact info, sections, and structured content from
plain-text resume output WITHOUT relying on Gemini.  The goal is to
guarantee that emails, phone numbers, and URLs are never mangled by AI.
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_structured(text: str) -> dict:
    """Return a structured dict from plain resume text.

    Keys guaranteed to always be present (may be empty string / empty list):
        header:
            name, title, location, email, phone, linkedin, portfolio
        professional_summary    str
        areas_of_expertise      list[str]   — bullet items
        experience              list[dict]  — {role, company, location, start_date, end_date, highlights}
        achievements_awards     list[str]
        certifications          list[str]
        education               list[dict]  — {degree, institution, period}
    """
    lines = [ln.rstrip() for ln in text.splitlines()]

    # ── 1. Contact header (first ~10 non-blank lines) ──────────────────────
    header = _extract_header(lines)

    # ── 2. Split into labelled sections ────────────────────────────────────
    raw_sections = _split_raw_sections(lines)

    # ── 3. Parse each section ──────────────────────────────────────────────
    summary   = _parse_summary(raw_sections)
    expertise = _parse_expertise(raw_sections)
    experience = _parse_experience(raw_sections)
    achievements = _parse_list_section(raw_sections, {"achievements & awards", "achievements", "awards"})
    certs     = _parse_list_section(raw_sections, {"certifications", "certificates", "licenses"})
    education = _parse_education(raw_sections)

    return {
        "header": header,
        "professional_summary": summary,
        "areas_of_expertise": expertise,
        "experience": experience,
        "achievements_awards": achievements,
        "certifications": certs,
        "education": education,
    }


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------

# Allow optional spaces anywhere in the address — PDF parsers commonly inject
# spaces mid-token, e.g. "sumit. sks1989@ icloud .com".  Spaces are collapsed
# after matching (see header extraction below).
_EMAIL_RE   = re.compile(
    r"[a-zA-Z0-9._%+\-]+(?:\s[a-zA-Z0-9._%+\-]+)*"  # local part (spaces allowed)
    r"\s*@\s*"                                          # @
    r"[a-zA-Z0-9.\-]+(?:\s*\.\s*[a-zA-Z0-9.\-]+)*"  # domain segments
    r"\s*\.\s*[a-zA-Z]{2,}"                            # TLD
)
_PHONE_RE   = re.compile(r"[\+\d][\d\s\-().]{6,}\d")
_LINKEDIN_RE = re.compile(r"(https?://[^\s]*linkedin\.com[^\s]*|linkedin\.com/in/[^\s|,]+)", re.I)
_PORTFOLIO_RE = re.compile(
    r"(https?://(?!.*linkedin)[^\s]+\.[a-zA-Z]{2,}[^\s]*|portfolio[^\s|,]*)", re.I
)
# Title: a line that looks like a professional designation (between name and contact line)
_TITLE_RE = re.compile(
    r"^[A-Z][a-zA-Z\s\(\)/\|&\-,]{8,100}$"
)
# Location segment in a contact line: "City, State" or "City, Country" style
_LOCATION_RE = re.compile(
    r"^[A-Z][a-z]+(?:[\s,]+(?:[A-Z][a-z]+|[A-Z]{2,3}))+$"
)


def _extract_header(lines: list[str]) -> dict:
    header: dict = {
        "name":      "",
        "title":     "",  # professional designation, if a dedicated line exists
        "location":  "",  # city/country from contact line
        "email":     "",
        "phone":     "",
        "linkedin":  "",
        "portfolio": "",
    }

    # ── Pass 1: find name (first short alphabetic line before any contact data) ──
    name_line_idx: int = -1
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if _EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped):
            break
        if re.match(r"^(https?|www\.)", stripped, re.I):
            break
        if re.match(r"^[A-Z][a-zA-Z\s\-\'\.]{2,50}$", stripped):
            header["name"] = stripped
            name_line_idx = i
            break

    # ── Pass 2: check the single line immediately after name for a title ──────
    # A title is a non-contact, non-section-heading line that looks like
    # a professional designation: e.g. "Senior Site Reliability Engineer"
    if name_line_idx >= 0:
        idx = name_line_idx + 1
        while idx < len(lines):
            candidate = lines[idx].strip()
            idx += 1
            if not candidate:
                continue
            # Stop as soon as we hit contact data
            if _EMAIL_RE.search(candidate) or _PHONE_RE.search(candidate):
                break
            if re.match(r"^(https?|www\.)", candidate, re.I):
                break
            # Title: mostly letters + title punctuation, no 4-digit years, not all-caps section heading
            if (_TITLE_RE.match(candidate)
                    and not re.search(r"\b\d{4}\b", candidate)
                    and not candidate.isupper()):
                header["title"] = candidate
            break  # inspect exactly one candidate line

    # ── Pass 3: scan first 15 non-blank lines for all contact fields ────────
    def _is_contact_line(s: str) -> bool:
        return bool(
            _EMAIL_RE.search(s) or _PHONE_RE.search(s)
            or re.search(r"\blinkedin\b|\bportfolio\b", s, re.I)
        )

    scanned = 0
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        scanned += 1
        if scanned > 15:
            break

        if not header["email"]:
            m = _EMAIL_RE.search(stripped)
            if m:
                # Collapse ALL spaces (PDF-extraction artefact: "user@ domain .com")
                header["email"] = re.sub(r"\s+", "", m.group(0))

        if not header["phone"]:
            m = _PHONE_RE.search(stripped)
            if m:
                header["phone"] = m.group(0).strip()

        if not header["linkedin"]:
            m = _LINKEDIN_RE.search(stripped)
            if m:
                header["linkedin"] = m.group(0).strip().rstrip(",|")
            elif re.search(r"\bLinkedIn\b", stripped):
                header["linkedin"] = "LinkedIn"

        if not header["portfolio"]:
            m = _PORTFOLIO_RE.search(stripped)
            if m and "linkedin" not in m.group(0).lower():
                val = m.group(0).strip().rstrip(",|")
                if val.lower() != "linkedin":
                    header["portfolio"] = val

        # Location: look for a "City, State" / "City Country" segment in the
        # contact line that doesn’t match any other field pattern.
        if not header["location"] and _is_contact_line(stripped):
            for segment in re.split(r"\s*\|\s*", stripped):
                seg = segment.strip()
                if not seg:
                    continue
                if _EMAIL_RE.search(seg) or _PHONE_RE.search(seg):
                    continue
                if re.search(r"\blinkedin\b|\bportfolio\b", seg, re.I):
                    continue
                if re.match(r"^(https?|www\.)", seg, re.I):
                    continue
                if _LOCATION_RE.match(seg):
                    header["location"] = seg
                    break

    return header


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

# Section heading → canonical key
_SECTION_LABELS: dict[str, str] = {
    "professional summary": "professional_summary",
    "summary": "professional_summary",
    "career summary": "professional_summary",
    "objective": "professional_summary",
    "profile": "professional_summary",
    "areas of expertise": "areas_of_expertise",
    "key skills": "areas_of_expertise",
    "core competencies": "areas_of_expertise",
    "skills": "areas_of_expertise",
    "technical skills": "areas_of_expertise",
    "professional experience": "experience",
    "experience": "experience",
    "work experience": "experience",
    "employment history": "experience",
    "career history": "experience",
    "achievements & awards": "achievements_awards",
    "achievements and awards": "achievements_awards",
    "achievements": "achievements_awards",
    "awards": "achievements_awards",
    "certifications": "certifications",
    "certificates": "certifications",
    "licenses & certifications": "certifications",
    "education": "education",
    "academic background": "education",
}


def _normalise_heading(line: str) -> str | None:
    """Return canonical section key if line is a known section heading, else None."""
    stripped = line.strip()
    # Remove trailing colon and decorators
    cleaned = re.sub(r"[:\-=_\*•]+$", "", stripped).strip()
    lower = cleaned.lower()

    if lower in _SECTION_LABELS:
        return _SECTION_LABELS[lower]

    # Exact all-caps match
    if cleaned.isupper() and 1 <= len(cleaned.split()) <= 5:
        if lower in _SECTION_LABELS:
            return _SECTION_LABELS[lower]

    return None


def _split_raw_sections(lines: list[str]) -> dict[str, list[str]]:
    """Return {canonical_key: [lines]} mapping, with 'preamble' for lines before any heading."""
    result: dict[str, list[str]] = {"preamble": []}
    current = "preamble"

    for ln in lines:
        canonical = _normalise_heading(ln)
        if canonical:
            current = canonical
            if current not in result:
                result[current] = []
        else:
            if current not in result:
                result[current] = []
            result[current].append(ln)

    return result


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_summary(sections: dict[str, list[str]]) -> str:
    raw = sections.get("professional_summary", [])
    lines = [ln for ln in raw if ln.strip()]
    return "\n".join(lines)


def _parse_expertise(sections: dict[str, list[str]]) -> list[str]:
    raw = sections.get("areas_of_expertise", [])
    items: list[str] = []
    for ln in raw:
        stripped = ln.strip()
        if not stripped:
            continue
        # Remove leading bullet characters
        stripped = re.sub(r"^[•\-\*\u2022]\s*", "", stripped)
        if stripped:
            items.append(stripped)
    return items


_DATE_RANGE_RE = re.compile(
    r"(\d{1,2}/\d{4}|\d{4})\s*[–—\-]\s*(\d{1,2}/\d{4}|\d{4}|[Pp]resent|[Cc]urrent|[Oo]ngoing)"
)


def _split_role_company(title_line: str) -> tuple[str, str]:
    """Split 'Role — Company' or 'Role | Company' into (role, company)."""
    for sep in (" — ", " – ", " - ", " | "):
        if sep in title_line:
            parts = title_line.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return title_line.strip(), ""


def _split_location_dates(period_str: str) -> tuple[str, str, str]:
    """Extract (location, start_date, end_date) from a period string.

    Handles patterns like:
      "San Francisco, USA | 06/2023 – 03/2026"
      "New York | 2019 – 2022"
      "2020 - Present"
    """
    if not period_str:
        return "", "", ""
    location = period_str
    start_date = ""
    end_date = ""

    m = _DATE_RANGE_RE.search(period_str)
    if m:
        start_date = m.group(1)
        end_date = m.group(2)
        location = period_str[:m.start()].strip(" |,–—-")
    else:
        years = re.findall(r"\b((?:19|20)\d{2})\b", period_str)
        if years:
            start_date = years[0]
            end_date = years[1] if len(years) > 1 else ""
            location = re.sub(r"\b(19|20)\d{2}\b", "", period_str).strip(" |,–—-")

    return location.strip(), start_date, end_date


def _is_job_header(line: str) -> bool:
    """Heuristic: line looks like 'Role — Company' (em-dash separator)."""
    stripped = line.strip()
    if not stripped:
        return False
    # ONLY em-dash (U+2014) separates role — company.
    # En-dash (U+2013) is used in date ranges — do NOT match those.
    has_em_dash = "\u2014" in stripped
    is_bullet = stripped.startswith(("•", "-", "*", "\u2022"))
    return has_em_dash and not is_bullet


def _parse_experience(sections: dict[str, list[str]]) -> list[dict]:
    """Parse experience section into a list of normalised job dicts.

    Output schema per job:
        role          str   — job title
        company       str   — employer / client
        location      str   — city/country
        start_date    str   — e.g. "06/2023" or "2023"
        end_date      str   — e.g. "03/2026" or "Present"
        highlights    list  — bullet points
    """
    raw = sections.get("experience", [])
    jobs: list[dict] = []
    current: dict | None = None

    for ln in raw:
        stripped = ln.strip()
        if not stripped:
            continue

        is_bullet = bool(re.match(r"^[•\-\*\u2022]\s", stripped))

        # New job block
        if _is_job_header(stripped) and not is_bullet:
            if current is not None:
                jobs.append(current)
            role, company = _split_role_company(stripped)
            current = {
                "role": role,
                "company": company,
                "location": "",
                "start_date": "",
                "end_date": "",
                "highlights": [],
            }
        elif current is not None:
            if is_bullet:
                bullet_text = re.sub(r"^[•\-\*\u2022]\s*", "", stripped)
                current["highlights"].append(bullet_text)
            elif (
                not current["location"]
                and not current["start_date"]
                and re.search(r"\b(19|20)\d{2}\b|[A-Z][a-z]+,\s*[A-Z]", stripped)
            ):
                # Location/date line immediately after the title
                loc, s_date, e_date = _split_location_dates(stripped)
                current["location"] = loc
                current["start_date"] = s_date
                current["end_date"] = e_date
            else:
                # Continuation of company name or orphan text
                if not current["highlights"]:
                    if current["company"]:
                        current["company"] += " " + stripped
                    else:
                        current["role"] += " " + stripped
                else:
                    current["highlights"].append(stripped)

    if current is not None:
        jobs.append(current)

    return jobs


def _parse_list_section(sections: dict[str, list[str]], keys: set[str]) -> list[str]:
    raw: list[str] = []
    for k in keys:
        if k in sections:
            raw = sections[k]
            break
    items: list[str] = []
    for ln in raw:
        stripped = re.sub(r"^[•\-\*\u2022]\s*", "", ln.strip())
        if stripped:
            items.append(stripped)
    return items


def _parse_education(sections: dict[str, list[str]]) -> list[dict]:
    raw = sections.get("education", [])
    entries: list[dict] = []
    current: dict | None = None

    for ln in raw:
        stripped = ln.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^[•\-\*\u2022]\s*", "", stripped)
        if re.search(r"\b(19|20)\d{2}\b", stripped) and current and not current["period"]:
            current["period"] = stripped
        elif current and current["degree"] and not current["institution"]:
            current["institution"] = stripped
        else:
            if current:
                entries.append(current)
            current = {"degree": stripped, "institution": "", "period": ""}

    if current:
        entries.append(current)
    return entries
