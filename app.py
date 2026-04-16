import logging
import os
import re
import sqlite3
from datetime import date
from functools import wraps
from pathlib import Path

import requests
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from services.gemini_service import analyze_resume as gemini_analyze_resume
from services.gemini_service import (
    analyze_keywords as gemini_analyze_keywords,
    tailor_resume_structured as gemini_tailor_structured,
)
from services.resume_extractor import extract_structured
from services.resume_exporter import (
    export_docx, export_pdf,                       # legacy text-based
    export_pdf_from_json, export_docx_from_json,   # canonical JSON-based
)
from services.resume_parser import extract_text as parse_resume_text, extract_hyperlinks
from services import job_search_service

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("app")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

DATABASE = os.path.join(app.root_path, "database.db")
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "docx"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            resume_path   TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            company_name TEXT    NOT NULL,
            job_role     TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'Applied',
            date_applied TEXT    NOT NULL DEFAULT '',
            apply_link   TEXT    NOT NULL DEFAULT '',
            date_posted  TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

    """)
    # Migrate existing installations: add columns if missing
    existing_cols = [row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()]
    if "apply_link" not in existing_cols:
        db.execute("ALTER TABLE jobs ADD COLUMN apply_link TEXT NOT NULL DEFAULT ''")
    if "job_description" not in existing_cols:
        db.execute("ALTER TABLE jobs ADD COLUMN job_description TEXT NOT NULL DEFAULT ''")
    if "date_posted" not in existing_cols:
        db.execute("ALTER TABLE jobs ADD COLUMN date_posted TEXT NOT NULL DEFAULT ''")
    db.commit()


# ---------------------------------------------------------------------------
# Structured resume helpers
# ---------------------------------------------------------------------------

def _structured_to_plaintext(s: dict) -> str:
    """Render a structured resume dict to a plain-text string for export."""
    parts: list[str] = []
    h = s.get("header", {})
    if h.get("name"):
        parts.append(h["name"])
    contact = " | ".join(filter(None, [
        h.get("email"), h.get("phone"), h.get("linkedin"), h.get("portfolio")
    ]))
    if contact:
        parts.append(contact)
    parts.append("")

    if s.get("professional_summary"):
        parts += ["Professional Summary", s["professional_summary"], ""]

    if s.get("areas_of_expertise"):
        parts.append("Areas of Expertise")
        for item in s["areas_of_expertise"]:
            parts.append(f"• {item}")
        parts.append("")

    if s.get("experience"):
        parts.append("Professional Experience")
        for job in s["experience"]:
            # Support both new schema (role/company) and legacy (title_line)
            role    = job.get("role")    or job.get("title_line") or ""
            company = job.get("company") or ""
            parts.append(f"{role} \u2014 {company}" if company else role)
            # Meta: location | start_date – end_date
            meta_parts = []
            loc = job.get("location", "") or job.get("location_period", "") or ""
            if loc:
                meta_parts.append(loc)
            start = job.get("start_date", "") or ""
            end   = job.get("end_date",   "") or ""
            if start:
                meta_parts.append(start + (f" \u2013 {end}" if end else ""))
            if meta_parts:
                parts.append(" | ".join(meta_parts))
            for b in (job.get("highlights") or job.get("bullets") or []):
                parts.append(f"\u2022 {b}")
            parts.append("")

    if s.get("achievements_awards"):
        parts.append("Achievements & Awards")
        for item in s["achievements_awards"]:
            parts.append(f"• {item}")
        parts.append("")

    if s.get("certifications"):
        parts.append("Certifications")
        for item in s["certifications"]:
            parts.append(f"• {item}")
        parts.append("")

    if s.get("education"):
        parts.append("Education")
        for ed in s["education"]:
            parts.append(ed.get("degree", ""))
            if ed.get("institution"):
                parts.append(ed["institution"])
            if ed.get("period"):
                parts.append(ed["period"])
            parts.append("")

    return "\n".join(parts)



def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("signup.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("signup.html")

        db = get_db()
        if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            flash("Email already registered.", "danger")
            return render_template("signup.html")
        if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            flash("Username already taken.", "danger")
            return render_template("signup.html")

        db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, generate_password_hash(password)),
        )
        db.commit()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes – Resume
# ---------------------------------------------------------------------------

@app.route("/upload-resume", methods=["POST"])
@login_required
def upload_resume():
    file = request.files.get("resume")
    if not file or file.filename == "":
        flash("No file selected.", "warning")
        return redirect(url_for("dashboard"))

    if not allowed_file(file.filename):
        flash("Only PDF and DOCX files are allowed.", "danger")
        return redirect(url_for("dashboard"))

    filename = secure_filename(f"user_{session['user_id']}_{file.filename}")
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)

    db = get_db()
    db.execute(
        "UPDATE users SET resume_path = ? WHERE id = ?",
        (f"uploads/{filename}", session["user_id"]),
    )
    db.commit()
    flash("Resume uploaded successfully.", "success")
    return redirect(url_for("resume_page"))


# ---------------------------------------------------------------------------
# Routes – Resume Page
# ---------------------------------------------------------------------------

@app.route("/resume")
@login_required
def resume_page():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return render_template("resume.html", user=user)


# ---------------------------------------------------------------------------
# Routes – Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    status_filter = request.args.get("status", "")
    if status_filter and status_filter in ("Saved", "Applied", "Interview", "Rejected", "Offer"):
        jobs = db.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND status = ? ORDER BY date_applied DESC",
            (session["user_id"], status_filter),
        ).fetchall()
    else:
        jobs = db.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY date_applied DESC",
            (session["user_id"],),
        ).fetchall()

    stats = {
        "total": len(jobs) if not status_filter else db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ?", (session["user_id"],)).fetchone()[0],
        "saved": db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'Saved'",
            (session["user_id"],)).fetchone()[0],
        "applied": db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'Applied'",
            (session["user_id"],)).fetchone()[0],
        "interview": db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'Interview'",
            (session["user_id"],)).fetchone()[0],
        "rejected": db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'Rejected'",
            (session["user_id"],)).fetchone()[0],
        "offer": db.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'Offer'",
            (session["user_id"],)).fetchone()[0],
    }

    return render_template(
        "dashboard.html",
        user=user,
        jobs=jobs,
        stats=stats,
        status_filter=status_filter,
        today=date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# Routes – CRUD
# ---------------------------------------------------------------------------

@app.route("/jobs/add", methods=["GET", "POST"])
@login_required
def add_job():
    if request.method == "POST":
        company = request.form["company_name"].strip()
        role = request.form["job_role"].strip()
        status = request.form["status"]
        date_applied = request.form.get("date_applied", "").strip()
        apply_link   = request.form.get("apply_link", "").strip()
        job_desc     = request.form.get("job_description", "").strip()[:5000]
        date_posted  = request.form.get("date_posted", "").strip()[:30]

        if not company or not role:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Company and role are required."}), 400
            flash("Company and role are required.", "danger")
            return render_template("add_job.html", today=date.today().isoformat())

        if status not in ("Saved", "Applied", "Interview", "Rejected", "Offer"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Invalid status."}), 400
            flash("Invalid status.", "danger")
            return render_template("add_job.html", today=date.today().isoformat())

        # Only store date_applied when status is Applied
        if status != "Applied":
            date_applied = ""

        if apply_link and not apply_link.startswith(("http://", "https://")):
            apply_link = ""

        db = get_db()

        # Block if same company already has an Applied entry for this user
        existing = db.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND status = 'Applied'"
            " AND LOWER(company_name) = LOWER(?)",
            (session["user_id"], company),
        ).fetchone()
        if existing:
            msg = f"A job application for {company} already exists in the system."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": msg}), 409
            flash(msg, "danger")
            return render_template("add_job.html", today=date.today().isoformat())

        db.execute(
            "INSERT INTO jobs (user_id, company_name, job_role, status, date_applied, apply_link, job_description, date_posted)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (session["user_id"], company, role, status, date_applied, apply_link, job_desc, date_posted),
        )
        db.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True}), 201
        flash("Job application added.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_job.html", today=date.today().isoformat())


@app.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def edit_job(job_id):
    db = get_db()
    job = db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, session["user_id"]),
    ).fetchone()

    if job is None:
        flash("Job not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        company = request.form["company_name"].strip()
        role = request.form["job_role"].strip()
        status = request.form["status"]
        date_applied = request.form.get("date_applied", "").strip()

        if not company or not role:
            flash("Company and role are required.", "danger")
            return render_template("edit_job.html", job=job)

        if status not in ("Saved", "Applied", "Interview", "Rejected", "Offer"):
            flash("Invalid status.", "danger")
            return render_template("edit_job.html", job=job)

        # Only store date_applied when status is Applied
        if status != "Applied":
            date_applied = ""

        db.execute(
            "UPDATE jobs SET company_name=?, job_role=?, status=?, date_applied=? WHERE id=? AND user_id=?",
            (company, role, status, date_applied, job_id, session["user_id"]),
        )
        db.commit()
        flash("Job updated.", "success")
        return redirect(url_for("dashboard"))

    return render_template("edit_job.html", job=job)


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    db = get_db()
    db.execute(
        "DELETE FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, session["user_id"]),
    )
    db.commit()
    flash("Job deleted.", "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes – Resume Analysis
# ---------------------------------------------------------------------------

@app.route("/analyze-resume", methods=["POST"])
@login_required
def analyze_resume():
    db = get_db()
    user = db.execute(
        "SELECT resume_path FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()

    if not user or not user["resume_path"]:
        return jsonify({"error": "No resume uploaded. Please upload a resume first."}), 400

    filepath = os.path.join(app.root_path, "static", user["resume_path"])
    if not os.path.isfile(filepath):
        return jsonify({"error": "Uploaded resume file not found on server."}), 404

    try:
        resume_text = parse_resume_text(filepath)
    except (ValueError, RuntimeError) as exc:
        logger.warning("Resume parse failed for user %s: %s", session["user_id"], exc)
        return jsonify({"error": f"Could not read resume: {exc}"}), 422

    if not resume_text.strip():
        logger.warning("Empty resume text for user %s at %s", session["user_id"], filepath)
        return jsonify({"error": "Resume appears to be empty or unreadable."}), 422

    logger.info("Starting resume analysis for user %s", session["user_id"])
    try:
        result = gemini_analyze_resume(resume_text)
    except ValueError as exc:
        # Missing API key
        logger.error("API key error: %s", exc)
        return jsonify({"error": str(exc)}), 503
    except RuntimeError as exc:
        logger.error("Gemini runtime error: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.exception("Unexpected error during analysis: %s", exc)
        return jsonify({"error": "An unexpected error occurred. Check logs for details."}), 500

    logger.info("Analysis succeeded for user %s — score %d", session["user_id"], result["ats_score"])
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Routes – Resume Tailor
# ---------------------------------------------------------------------------

@app.route("/resume-tailor")
@login_required
def resume_tailor():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return render_template("resume_tailor.html", user=user)


@app.route("/generate-tailored-resume", methods=["POST"])
@login_required
def generate_tailored_resume():
    job_description = (request.json or {}).get("job_description", "").strip()
    if not job_description:
        return jsonify({"error": "Job description is required."}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if not user or not user["resume_path"]:
        return jsonify({"error": "No resume uploaded. Please upload a resume first."}), 400

    filepath = os.path.join(app.root_path, "static", user["resume_path"])
    if not os.path.isfile(filepath):
        return jsonify({"error": "Resume file not found on server."}), 404

    try:
        resume_text = parse_resume_text(filepath)
    except (ValueError, RuntimeError) as exc:
        logger.warning("Resume parse failed (tailor) user %s: %s", session["user_id"], exc)
        return jsonify({"error": f"Could not read resume: {exc}"}), 422

    if not resume_text.strip():
        return jsonify({"error": "Resume appears to be empty or unreadable."}), 422

    # ── Step 1: structured extraction (no Gemini) ─────────────────────────
    structured = extract_structured(resume_text)
    logger.info("Structured extract done for user %s: header=%s",
                session["user_id"], structured["header"])

    # ── Step 1b: overlay real hyperlinks from PDF/DOCX annotations ────────
    # Plain-text extraction only yields labels like "LinkedIn" / "Portfolio".
    # Annotation-based extraction recovers the actual embedded URLs.
    pdf_links = extract_hyperlinks(filepath)
    for _lk in ("linkedin", "portfolio"):
        if pdf_links.get(_lk):
            old_val = structured["header"].get(_lk, "")
            structured["header"][_lk] = pdf_links[_lk]
            if old_val != pdf_links[_lk]:
                logger.info(
                    "Hyperlink overlay user %s: %s %r → %r",
                    session["user_id"], _lk, old_val, pdf_links[_lk],
                )

    # Build a keyword-context string from summary + expertise for the gap call
    resume_context = "\n".join([
        structured.get("professional_summary", "")[:600],
        " ".join(structured.get("areas_of_expertise", []))[:600],
    ]).strip()[:2000]

    logger.info("Starting tailor pipeline for user %s", session["user_id"])
    try:
        # ── Step 2: keyword gap analysis (low-token call) ─────────────────
        kw_result = gemini_analyze_keywords(resume_context, job_description)
        all_keywords = (
            kw_result.get("missing_keywords", [])
            + kw_result.get("skill_gaps", [])
        )
        logger.info("Tailor step 1 done: %d keywords to add", len(all_keywords))

        # ── Step 3: structured improvement (JSON in/out) ──────────────────
        tailored_structured = gemini_tailor_structured(
            structured, all_keywords, job_description
        )
        logger.info("Tailor step 2 done: structured improvement complete")

        # ── Step 3b: header immutability validation ────────────────────────
        # Belt-and-suspenders: compare every header field against the original
        # extraction and force-restore any that have drifted.
        _IMMUTABLE_HEADER_FIELDS = ("name", "title", "location", "email", "phone", "linkedin", "portfolio")
        orig_header = structured["header"]
        tail_header = tailored_structured.setdefault("header", {})
        for _field in _IMMUTABLE_HEADER_FIELDS:
            orig_val = orig_header.get(_field, "")
            tail_val = tail_header.get(_field, "")
            if tail_val != orig_val:
                logger.warning(
                    "Header field %r mismatch — original=%r  optimized=%r — restoring original",
                    _field, orig_val, tail_val,
                )
                tail_header[_field] = orig_val
        logger.info(
            "Header validation passed — email=%r phone=%r",
            tail_header.get("email"), tail_header.get("phone"),
        )

    except ValueError as exc:
        logger.error("API key error (tailor pipeline): %s", exc)
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Tailor pipeline error for user %s: %s", session["user_id"], exc)
        return jsonify({"error": "AI generation failed. Check logs for details."}), 500

    # Optimised output is returned to the client only — never persisted.
    logger.info("Tailor pipeline complete for user %s (stateless — not persisted)", session["user_id"])
    return jsonify({
        "structured": tailored_structured,
        "keywords_added": all_keywords[:8],
    }), 200


@app.route("/download/pdf", methods=["POST"])
@login_required
def download_pdf():
    """Generate and stream a PDF from the structured JSON sent in the request body.

    The optimised resume is never stored in the DB; the client always sends the
    current structured JSON it holds in memory.
    """
    data = request.get_json() or {}
    resume_json = data.get("structured")
    if not resume_json or not isinstance(resume_json, dict):
        return jsonify({"error": "No structured resume data provided."}), 400
    try:
        pdf_bytes = export_pdf_from_json(resume_json)
        logger.info("PDF export (stateless) for user %s", session["user_id"])
    except Exception as exc:
        logger.exception("PDF export error: %s", exc)
        return jsonify({"error": "PDF export failed."}), 500
    from flask import Response
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=optimized_resume.pdf"},
    )


@app.route("/download/docx", methods=["POST"])
@login_required
def download_docx():
    """Generate and stream a DOCX from the structured JSON sent in the request body.

    The optimised resume is never stored in the DB; the client always sends the
    current structured JSON it holds in memory.
    """
    data = request.get_json() or {}
    resume_json = data.get("structured")
    if not resume_json or not isinstance(resume_json, dict):
        return jsonify({"error": "No structured resume data provided."}), 400
    try:
        docx_bytes = export_docx_from_json(resume_json)
        logger.info("DOCX export (stateless) for user %s", session["user_id"])
    except Exception as exc:
        logger.exception("DOCX export error: %s", exc)
        return jsonify({"error": "DOCX export failed."}), 500
    from flask import Response
    return Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=optimized_resume.docx"},
    )


# ---------------------------------------------------------------------------
# Routes – Job Search
# ---------------------------------------------------------------------------

@app.route("/job-search")
@login_required
def job_search():
    return render_template("job_search.html")


@app.route("/api/search-jobs", methods=["POST"])
@login_required
def api_search_jobs():
    body = request.get_json() or {}
    query       = body.get("query", "").strip()
    location    = body.get("location", "").strip()
    date_posted = body.get("date_posted", "any")

    if not query:
        return jsonify({"error": "Job role / keywords are required.", "jobs": []}), 400

    if date_posted not in ("any", "24h", "3days", "week", "month"):
        date_posted = "any"

    result = job_search_service.search_jobs(
        query=query,
        location=location,
        date_posted=date_posted,
    )
    status_code = 200 if not result["error"] else 502
    return jsonify(result), status_code


@app.route("/jobs/save-from-search", methods=["POST"])
@login_required
def save_job_from_search():
    """Save a job card from the search page directly to the user's dashboard."""
    body = request.get_json() or {}
    company     = (body.get("company")     or "").strip()
    role        = (body.get("role")        or "").strip()
    apply_link  = (body.get("apply_link")  or "").strip()
    job_desc    = (body.get("description") or "").strip()[:5000]
    date_posted = (body.get("posted_at")   or "").strip()[:30]

    if not company or not role:
        return jsonify({"error": "Company and role are required."}), 400

    # Basic URL validation — must start with http if provided
    if apply_link and not apply_link.startswith(("http://", "https://")):
        apply_link = ""

    db = get_db()

    # Block save if same company already has an Applied entry for this user
    existing = db.execute(
        "SELECT id FROM jobs WHERE user_id = ? AND status = 'Applied'"
        " AND LOWER(company_name) = LOWER(?)",
        (session["user_id"], company),
    ).fetchone()
    if existing:
        return jsonify({"error": f"A job application for {company} already exists in the system."}), 409

    db.execute(
        "INSERT INTO jobs (user_id, company_name, job_role, status, date_applied, apply_link, job_description, date_posted)"
        " VALUES (?, ?, ?, 'Saved', '', ?, ?, ?)",
        (session["user_id"], company, role, apply_link, job_desc, date_posted),
    )
    db.commit()
    logger.info("Saved job from search: user=%s company=%r role=%r",
                session["user_id"], company, role)
    return jsonify({"ok": True}), 201


_VALID_STATUSES = {"Saved", "Applied", "Interview", "Offer", "Rejected"}


@app.route("/update-job-status", methods=["POST"])
@login_required
def update_job_status():
    body = request.get_json() or {}
    job_id = body.get("job_id")
    new_status = (body.get("status") or "").strip()

    if not job_id or new_status not in _VALID_STATUSES:
        return jsonify({"error": "Invalid job_id or status."}), 400

    db = get_db()
    if new_status == "Applied":
        result = db.execute(
            "UPDATE jobs SET status = ?, date_applied = date('now') WHERE id = ? AND user_id = ?",
            (new_status, job_id, session["user_id"]),
        )
    else:
        result = db.execute(
            "UPDATE jobs SET status = ?, date_applied = '' WHERE id = ? AND user_id = ?",
            (new_status, job_id, session["user_id"]),
        )
    db.commit()
    if result.rowcount == 0:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# Routes – Job Description Fetch
# ---------------------------------------------------------------------------

_JD_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Tags whose full subtree is stripped before text extraction
_JD_NOISE_TAGS = {
    "script", "style", "noscript", "header", "footer",
    "nav", "aside", "form", "button", "iframe",
}

# CSS-class/id substrings that hint at a "junk" container
_JD_NOISE_PATTERNS = re.compile(
    r"(cookie|banner|modal|popup|nav|header|footer|sidebar|advertisement|promo)",
    re.I,
)


def _extract_text_from_html(html: str) -> str:
    """Return clean visible text from a job posting HTML page."""
    from bs4 import BeautifulSoup  # lazy import — only needed here

    soup = BeautifulSoup(html, "html.parser")

    # Remove noise subtrees
    for tag in soup.find_all(_JD_NOISE_TAGS):
        tag.decompose()

    # Remove elements whose class/id hints at clutter
    for tag in soup.find_all(True):
        cls   = " ".join(tag.get("class", []))
        tagid = tag.get("id", "")
        if _JD_NOISE_PATTERNS.search(cls) or _JD_NOISE_PATTERNS.search(tagid):
            tag.decompose()

    text = soup.get_text(separator="\n")
    # Collapse blank lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


@app.route("/fetch-job-description", methods=["POST"])
@login_required
def fetch_job_description():
    """Fetch and extract job description text from the job's apply_link.

    Accepts either:
      { "job_id": 123 }  — looks up apply_link from the user's jobs table
      { "url": "https://..." }  — uses provided URL directly

    Returns { "job_description": "<text>" } or { "error": "..." }.
    """
    body   = request.get_json() or {}
    job_id = body.get("job_id")
    url    = (body.get("url") or "").strip()

    if job_id:
        db  = get_db()
        row = db.execute(
            "SELECT apply_link, job_description FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, session["user_id"]),
        ).fetchone()
        if row is None:
            return jsonify({"error": "Job not found."}), 404
        # Return stored description immediately — no scraping needed
        stored_jd = (row["job_description"] or "").strip()
        if stored_jd:
            return jsonify({"job_description": stored_jd}), 200
        url = row["apply_link"] or ""

    if not url:
        return jsonify({"error": "No apply link available for this job."}), 400

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL."}), 400

    try:
        resp = requests.get(url, headers=_JD_FETCH_HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out fetching job page."}), 504
    except requests.exceptions.RequestException as exc:
        logger.warning("JD fetch failed for url=%r: %s", url, exc)
        return jsonify({"error": "Could not fetch job page. The site may block automated access."}), 502

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return jsonify({"error": "Job page did not return HTML content."}), 415

    jd_text = _extract_text_from_html(resp.text)
    if len(jd_text) < 100:
        return jsonify({"error": "Could not extract job description — the site may require JavaScript or login."}), 422

    # Truncate to 8 000 chars to avoid overwhelming the AI
    return jsonify({"job_description": jd_text[:8000]}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)
