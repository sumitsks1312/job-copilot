"""Gemini API service for resume analysis."""
import json
import logging
import os
import re
from pathlib import Path

from google import genai

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("gemini_service")
if not logger.handlers:
    _handler = logging.FileHandler(_LOG_DIR / "gemini.log")
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "resources" / "config.env"


def _load_api_key() -> str:
    """Return GEMINI_API_KEY from resources/config.env, falling back to env var."""
    if _CONFIG_FILE.is_file():
        with open(_CONFIG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "GEMINI_API_KEY":
                    val = value.strip()
                    if val and val != "your-gemini-api-key-here":
                        return val
    # Fall back to environment variable
    return os.environ.get("GEMINI_API_KEY", "").strip()

_PROMPT_TEMPLATE = """You are an expert ATS (Applicant Tracking System) and resume coach.

Analyze the following resume text and return a JSON object with exactly these keys:
  - "ats_score":          integer from 0 to 100 representing ATS compatibility
  - "missing_keywords":   list of strings — important keywords/skills absent from the resume
  - "suggestions":        list of strings — concrete, actionable improvements

Rules:
- Respond ONLY with valid JSON. No markdown, no extra text.
- Base the score on formatting clarity, keyword density, quantified achievements, and section completeness.

Resume:
{resume_text}
"""


def analyze_resume(resume_text: str) -> dict:
    """Send resume text to Gemini and return parsed analysis dict.

    Returns a dict with keys: ats_score (int), missing_keywords (list), suggestions (list).
    Raises ValueError when the API key is not configured.
    Raises RuntimeError when Gemini returns an unparseable response.
    """
    api_key = _load_api_key()
    if not api_key:
        logger.error("GEMINI_API_KEY not found in resources/config.env or environment.")
        raise ValueError(
            "GEMINI_API_KEY is not set. Add it to resources/config.env."
        )

    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash-lite"

    # Truncate to ~6 000 chars to stay within token budget while preserving content
    truncated_text = resume_text[:6000]
    prompt = _PROMPT_TEMPLATE.format(resume_text=truncated_text)

    logger.info("Sending resume to Gemini (model=%s, chars=%d)", model, len(truncated_text))
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
    except Exception as exc:
        logger.exception("Gemini API call failed: %s", exc)
        raise

    raw = response.text.strip()
    logger.debug("Raw Gemini response (first 300 chars): %s", raw[:300])

    # Strip markdown code fences that Gemini sometimes wraps JSON in
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Gemini JSON: %s", raw[:200])
        raise RuntimeError(f"Gemini returned non-JSON response: {raw[:200]}") from exc

    # Normalise types to guard against Gemini quirks
    result["ats_score"] = int(result.get("ats_score", 0))
    result["missing_keywords"] = list(result.get("missing_keywords", []))
    result["suggestions"] = list(result.get("suggestions", []))

    logger.info("Analysis complete — ATS score: %d", result["ats_score"])
    return result


# ---------------------------------------------------------------------------
# Resume tailoring — structured JSON pipeline
# ---------------------------------------------------------------------------

_MODEL = "gemini-2.5-flash-lite"


def _call_gemini(prompt: str, label: str) -> str:
    """Shared helper: call Gemini and return stripped text. Raises on failure."""
    api_key = _load_api_key()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set. Add it to resources/config.env.")
    client = genai.Client(api_key=api_key)
    logger.info("%s: calling Gemini (model=%s)", label, _MODEL)
    try:
        response = client.models.generate_content(model=_MODEL, contents=prompt)
    except Exception as exc:
        logger.exception("%s: Gemini API call failed: %s", label, exc)
        raise
    raw = response.text.strip()
    raw = re.sub(r"^```(?:\w+)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw.rstrip())
    logger.info("%s: %d chars returned", label, len(raw))
    return raw


# ── Step 1: keyword gap analysis (low-token, context-only call) ────────────

_ANALYZE_KEYWORDS_PROMPT = """You are an ATS specialist comparing a resume against a job description.
Return a JSON object with exactly these keys:
  - "missing_keywords":  list of strings — important keywords/skills from the JD absent in the resume
  - "skill_gaps":        list of strings — domain or soft skills the candidate may be lacking

Rules:
- Respond ONLY with valid JSON. No markdown, no extra text.
- Keep each list concise (max 15 items each).

--- RESUME CONTEXT ---
{resume_context}

--- JOB DESCRIPTION ---
{job_description}
"""


def analyze_keywords(resume_context: str, job_description: str) -> dict:
    """Extract missing keywords and skill gaps (Step 1 of the pipeline).

    Returns dict: {missing_keywords: list, skill_gaps: list}.
    """
    prompt = _ANALYZE_KEYWORDS_PROMPT.format(
        resume_context=resume_context[:2000],
        job_description=job_description[:2500],
    )
    raw = _call_gemini(prompt, "analyze_keywords")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("analyze_keywords JSON parse error: %s", raw[:200])
        raise RuntimeError(f"Could not parse keyword analysis response: {raw[:200]}") from exc
    result["missing_keywords"] = list(result.get("missing_keywords", []))
    result["skill_gaps"] = list(result.get("skill_gaps", []))
    logger.info("analyze_keywords: %d missing keywords", len(result["missing_keywords"]))
    return result


# ── Step 2: structured experience + skills improvement   ──────────────────

_TAILOR_STRUCTURED_PROMPT = """You are a professional resume writer and ATS optimisation specialist.

You will receive JSON containing resume sections to improve — the header (name/email/phone/links)
is intentionally excluded and will never be modified.

YOUR ONLY TASK:
  A. Improve each bullet point in the "highlights" array of each experience entry —
     stronger action verbs, impact language, quantified results where numbers already exist.
     DO NOT change role, company, location, start_date, or end_date fields.
     DO NOT invent new roles, companies, or dates.
  B. Improve the "areas_of_expertise" list — reword and add missing keywords ONLY where plausible.
  C. Improve the "professional_summary" — make it more targeted to the job.

ABSOLUTE RULES:
  - DO NOT add, remove, or rename JSON keys.
  - DO NOT produce markdown; output only plain text inside JSON string values.
  - Output ONLY valid JSON — no extra text, no markdown fences.
  - Experience schema per entry: {{role, company, location, start_date, end_date, highlights}}

INPUT JSON (header omitted — do not add it back):
{structured_json}

MISSING KEYWORDS TO INCORPORATE (only where genuinely applicable):
{keywords}

JOB DESCRIPTION CONTEXT:
{job_description}
"""


def tailor_resume_structured(structured: dict, missing_keywords: list, job_description: str) -> dict:
    """Improve experience, skills, and summary sections via Gemini.

    Takes and returns a structured resume dict.
    The header, certifications, education, and achievements are passed through unchanged.
    Raises ValueError when API key is missing.
    Raises RuntimeError on unparseable response.
    """
    import copy

    # Header is NEVER sent to Gemini — it is immutable and always taken from
    # the locally-extracted structured dict.
    payload = {
        "professional_summary": structured.get("professional_summary", ""),
        "areas_of_expertise": structured.get("areas_of_expertise", []),
        "experience": structured.get("experience", []),
        "achievements_awards": structured.get("achievements_awards", []),
        "certifications": structured.get("certifications", []),
        "education": structured.get("education", []),
    }

    keywords_str = ", ".join(missing_keywords[:15]) if missing_keywords else "(none)"
    prompt = _TAILOR_STRUCTURED_PROMPT.format(
        structured_json=json.dumps(payload, ensure_ascii=False),
        keywords=keywords_str,
        job_description=job_description[:2000],
    )

    raw = _call_gemini(prompt, "tailor_structured")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("tailor_structured JSON parse error: %s", raw[:400])
        raise RuntimeError(f"Gemini returned non-JSON for structured tailor: {raw[:400]}") from exc

    # Remove any accidental markdown from text fields FIRST — before injecting
    # immutable sections so that email/phone/links are never touched by this regex.
    # (The _{1,2} pattern would corrupt underscored emails like first_last@domain.com)
    def _strip_md(v):
        if isinstance(v, str):
            return re.sub(r"\*{1,2}|_{1,2}|`", "", v)
        if isinstance(v, list):
            return [_strip_md(i) for i in v]
        if isinstance(v, dict):
            return {k: _strip_md(val) for k, val in v.items()}
        return v

    result = _strip_md(result)

    # Always inject immutable original fields AFTER strip_md — these are never
    # sent to Gemini and must never be rewritten or processed by any regex.
    orig_header = structured["header"]
    result["header"]             = orig_header
    result["certifications"]     = structured.get("certifications", [])
    result["education"]          = structured.get("education", [])
    result["achievements_awards"] = structured.get("achievements_awards", [])

    logger.info(
        "tailor_structured: header restored — name=%r email=%r phone=%r",
        orig_header.get("name"), orig_header.get("email"), orig_header.get("phone"),
    )
    logger.info("tailor_structured: complete")
    return result

