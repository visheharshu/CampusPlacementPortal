import os
import re
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pypdf import PdfReader

ROOT = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(ROOT, "placement_portal.db")
UPLOAD_DIR = os.path.join(ROOT, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "development-key-change-before-deployment"
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt"}

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db_conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student','company')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            skills TEXT NOT NULL,
            eligibility TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(company_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            resume_filename TEXT NOT NULL,
            resume_score INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Applied',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, job_id),
            FOREIGN KEY(student_id) REFERENCES users(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            interview_date TEXT NOT NULL,
            interview_mode TEXT NOT NULL,
            notes TEXT DEFAULT '',
            FOREIGN KEY(application_id) REFERENCES applications(id)
        );
        """)

def login_required(role=None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                flash("This page is not available for your account type.", "warning")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorate

def is_allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS



def calculate_resume_score(saved_filename, required_skills):
    """Calculate a simple resume keyword-match score."""
    full_path = os.path.join(UPLOAD_DIR, saved_filename)
    ext = saved_filename.rsplit(".", 1)[-1].lower()
    resume_text = ""

    try:
        if ext == "txt":
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                resume_text = f.read().lower()

        elif ext == "pdf":
            reader = PdfReader(full_path)
            resume_text = " ".join(
                page.extract_text() or "" for page in reader.pages
            ).lower()

        else:
            return 0

    except Exception as e:
        print("Resume reading error:", e)
        return 0

    skills = [
        s.strip().lower()
        for s in re.split(r"[,;\n]+", required_skills or "")
        if s.strip()
    ]

    if not skills or not resume_text.strip():
        return 0

    found = sum(1 for skill in skills if skill in resume_text)
    score = round((found / len(skills)) * 100)

    print("Required skills:", skills)
    print("Matched skills:", found)
    print("Resume score:", score)

    return score

@app.context_processor
def inject_globals():
    return {"current_year": datetime.now().year}

@app.route("/")
def home():
    with db_conn() as db:
        latest_jobs = db.execute("""SELECT j.*, u.name AS company_name FROM jobs j
            JOIN users u ON u.id=j.company_id ORDER BY j.id DESC LIMIT 3""").fetchall()
        stats = {
            "jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "students": db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
            "companies": db.execute("SELECT COUNT(*) FROM users WHERE role='company'").fetchone()[0]
        }
    return render_template("home.html", jobs=latest_jobs, stats=stats)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        if not name or not email or len(password) < 6 or role not in {"student", "company"}:
            flash("Enter all details. Password must contain at least 6 characters.", "error")
            return render_template("register.html")
        try:
            with db_conn() as db:
                db.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                           (name, email, generate_password_hash(password), role))
            flash("Account created successfully. Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("This email is already registered. Please use another email.", "error")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with db_conn() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.update(user_id=user["id"], name=user["name"], role=user["role"])
            flash("Welcome, " + user["name"] + "!", "success")
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("home"))

@app.route("/jobs")
def jobs():
    with db_conn() as db:
        items = db.execute("""SELECT j.*, u.name AS company_name FROM jobs j
            JOIN users u ON u.id=j.company_id ORDER BY j.id DESC""").fetchall()
    return render_template("jobs.html", jobs=items)

@app.route("/jobs/new", methods=["GET", "POST"])
@login_required("company")
def new_job():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        skills = request.form.get("skills", "").strip()
        eligibility = request.form.get("eligibility", "").strip()
        if not title or not description or not skills:
            flash("Job title, description and skills are required.", "error")
            return render_template("new_job.html")
        with db_conn() as db:
            db.execute("INSERT INTO jobs(company_id,title,description,skills,eligibility) VALUES(?,?,?,?,?)",
                       (session["user_id"], title, description, skills, eligibility))
        flash("Job posted successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("new_job.html")

@app.route("/apply/<int:job_id>", methods=["GET", "POST"])
@login_required("student")
def apply(job_id):
    with db_conn() as db:
        job = db.execute("""SELECT j.*, u.name AS company_name FROM jobs j
            JOIN users u ON u.id=j.company_id WHERE j.id=?""", (job_id,)).fetchone()
        if not job:
            flash("Job not found.", "error")
            return redirect(url_for("jobs"))
        if request.method == "POST":
            file = request.files.get("resume")
            if not file or not file.filename:
                flash("Choose a resume file to continue.", "error")
                return render_template("apply.html", job=job)
            if not is_allowed(file.filename):
                flash("Upload PDF, DOC, DOCX or TXT only.", "error")
                return render_template("apply.html", job=job)
            safe_name = secure_filename(file.filename)
            filename = f"{session['user_id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            path = os.path.join(UPLOAD_DIR, filename)
            file.save(path)
            score = calculate_resume_score(filename, job["skills"])
            try:
                with db_conn() as db2:
                    db2.execute("INSERT INTO applications(student_id,job_id,resume_filename,resume_score) VALUES(?,?,?,?)",
                                (session["user_id"], job_id, filename, score))
                flash(f"Application submitted. Resume keyword match score: {score}/100.", "success")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                try: os.remove(path)
                except OSError: pass
                flash("You have already applied for this job.", "warning")
                return redirect(url_for("dashboard"))
    return render_template("apply.html", job=job)

@app.route("/dashboard")
@login_required()

def dashboard():
    with db_conn() as db:

        # Student Dashboard
        if session["role"] == "student":

            student_id = session["user_id"]

            applications = db.execute("""
                SELECT a.*, j.title, c.name AS company_name
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                JOIN users c ON c.id = j.company_id
                WHERE a.student_id = ?
                ORDER BY a.id DESC
            """, (student_id,)).fetchall()

            interviews = db.execute("""
                SELECT i.*, j.title, c.name AS company_name
                FROM interviews i
                JOIN applications a ON a.id = i.application_id
                JOIN jobs j ON j.id = a.job_id
                JOIN users c ON c.id = j.company_id
                WHERE a.student_id = ?
                ORDER BY i.interview_date
            """, (student_id,)).fetchall()

            # Student Dashboard Statistics
            total_applications = len(applications)

            shortlisted = sum(
                1 for a in applications
                if (a["status"] or "").lower() == "shortlisted"
            )

            average_score = round(
                sum(
                    (a["resume_score"] or 0)
                    for a in applications
                ) / total_applications
            ) if total_applications else 0

            total_interviews = len(interviews)

            return render_template(
                "student_dashboard.html",
                applications=applications,
                interviews=interviews,
                total_applications=total_applications,
                shortlisted=shortlisted,
                average_score=average_score,
                total_interviews=total_interviews
            )

        # Company Dashboard
        company_id = session["user_id"]

        jobs = db.execute("""
            SELECT *
            FROM jobs
            WHERE company_id = ?
            ORDER BY id DESC
        """, (company_id,)).fetchall()

        applicants = db.execute("""
            SELECT a.*, j.title,
                   s.name AS student_name,
                   s.email AS student_email
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            JOIN users s ON s.id = a.student_id
            WHERE j.company_id = ?
            ORDER BY a.id DESC
        """, (company_id,)).fetchall()

        students = db.execute("""
            SELECT id, name, email, created_at
            FROM users
            WHERE role = 'student'
            ORDER BY id DESC
        """).fetchall()

        total_jobs = len(jobs)
        total_applications = len(applicants)

        shortlisted = sum(
            1 for a in applicants
            if (a["status"] or "").lower() == "shortlisted"
        )

        average_score = round(
            sum((a["resume_score"] or 0) for a in applicants)
            / total_applications
        ) if total_applications else 0

        return render_template(
            "company_dashboard.html",
            jobs=jobs,
            applicants=applicants,
            students=students,
            total_jobs=total_jobs,
            total_applications=total_applications,
            shortlisted=shortlisted,
            average_score=average_score
        )
        
@app.route("/applications/<int:application_id>/status", methods=["POST"])
@login_required("company")
def update_status(application_id):
    status = request.form.get("status", "Applied")
    if status not in {"Applied", "Shortlisted", "Rejected"}:
        status = "Applied"
    with db_conn() as db:
        owned = db.execute("""SELECT a.id FROM applications a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=? AND j.company_id=?""", (application_id, session["user_id"])).fetchone()
        if owned:
            db.execute("UPDATE applications SET status=? WHERE id=?", (status, application_id))
            flash("Application status updated.", "success")
        else:
            flash("Application not found.", "error")
    return redirect(url_for("dashboard"))

@app.route("/applications/<int:application_id>/interview", methods=["POST"])
@login_required("company")
def schedule_interview(application_id):
    date = request.form.get("interview_date", "").strip()
    mode = request.form.get("interview_mode", "Online")
    notes = request.form.get("notes", "").strip()
    try:
        datetime.fromisoformat(date)
    except ValueError:
        flash("Select a valid interview date and time.", "error")
        return redirect(url_for("dashboard"))
    if mode not in {"Online", "In-person"}:
        mode = "Online"
    with db_conn() as db:
        owned = db.execute("""SELECT a.id FROM applications a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=? AND j.company_id=?""", (application_id, session["user_id"])).fetchone()
        if not owned:
            flash("Application not found.", "error")
        else:
            db.execute("INSERT INTO interviews(application_id,interview_date,interview_mode,notes) VALUES(?,?,?,?)",
                       (application_id, date, mode, notes))
            db.execute("UPDATE applications SET status='Shortlisted' WHERE id=?", (application_id,))
            flash("Interview scheduled successfully.", "success")
    return redirect(url_for("dashboard"))

@app.route("/recalculate-scores")
@login_required("company")
def recalculate_scores():
    with db_conn() as db:
        applications = db.execute("""
            SELECT a.id, a.resume_filename, j.skills
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE j.company_id = ?
        """, (session["user_id"],)).fetchall()

        for application in applications:
            score = calculate_resume_score(
                application["resume_filename"],
                application["skills"]
            )
            db.execute(
                "UPDATE applications SET resume_score = ? WHERE id = ?",
                (score, application["id"])
            )

    flash("Resume scores recalculated successfully.", "success")
    return redirect(url_for("dashboard"))

@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. Maximum upload size is 5 MB.", "error")
    return redirect(request.referrer or url_for("jobs"))

@app.route("/health")
def health():
    return {"status": "ok", "project": "Campus Placement Portal with AI Shortlisting"}

init_db()

if __name__ == "__main__":
    app.run(debug=True)
