import csv
import io
import os
import secrets
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///sparkclaw_perks.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
db = SQLAlchemy(app)

STATUS_LABELS = {
    "submitted": "Submitted",
    "portfolio_verified": "Portfolio verified",
    "needs_review": "Needs review",
    "approved": "Approved",
    "rejected": "Rejected",
    "partner_emailed": "Partner emailed",
    "code_received": "Code received",
    "fulfilled": "Fulfilled",
}


class PortfolioCompany(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    website = db.Column(db.String(255), nullable=True)
    allowed_domains = db.Column(db.Text, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def domain_list(self):
        return [d.strip().lower() for d in (self.allowed_domains or "").split(",") if d.strip()]


class PerkRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    requester_name = db.Column(db.String(255), nullable=False)
    requester_email = db.Column(db.String(255), nullable=False, index=True)
    company_name = db.Column(db.String(255), nullable=False, index=True)
    company_website = db.Column(db.String(255), nullable=True)
    perk_type = db.Column(db.String(255), nullable=False, default="Supabase credits")
    use_case = db.Column(db.Text, nullable=False)
    expected_monthly_spend = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    portfolio_verified = db.Column(db.Boolean, default=False, nullable=False)
    verification_reason = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), nullable=False, default="submitted", index=True)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    partner_notified_at = db.Column(db.DateTime, nullable=True)
    partner_notified_email = db.Column(db.String(255), nullable=True)
    code_received_at = db.Column(db.DateTime, nullable=True)
    code_delivered_at = db.Column(db.DateTime, nullable=True)
    last_email_status = db.Column(db.String(50), nullable=True)
    last_email_response = db.Column(db.Text, nullable=True)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("perk_request.id"), nullable=True)
    actor = db.Column(db.String(255), nullable=False)
    action = db.Column(db.String(255), nullable=False)
    meta = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


with app.app_context():
    db.create_all()


@app.context_processor
def inject_globals():
    return {
        "STATUS_LABELS": STATUS_LABELS,
        "APP_NAME": os.getenv("APP_NAME", "Spark Claw Perks Console"),
    }


def now_utc():
    return datetime.utcnow()


def clean_domain(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[1]
    if not value.startswith("http"):
        value = f"https://{value}"
    parsed = urlparse(value)
    host = parsed.netloc.lower().replace("www.", "")
    return host


def add_audit(request_id: int | None, actor: str, action: str, meta: str = ""):
    row = AuditLog(request_id=request_id, actor=actor, action=action, meta=meta)
    db.session.add(row)
    db.session.commit()


def get_portfolio_match(email: str, company_name: str):
    email_domain = clean_domain(email)
    candidates = PortfolioCompany.query.filter_by(active=True).all()
    for company in candidates:
        if company.name.strip().lower() == company_name.strip().lower():
            if email_domain in company.domain_list():
                return company, f"Matched company name and email domain ({email_domain})"
            return company, "Matched company name but email domain needs admin review"
        if email_domain and email_domain in company.domain_list():
            return company, f"Matched email domain ({email_domain})"
    return None, "No portfolio match found"


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Please sign in as an admin.", "warning")
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)

    return wrapper


def send_resend_email(subject: str, html: str, text: str, to_email: str):
    api_key = os.getenv("RESEND_API_KEY")
    email_from = os.getenv("EMAIL_FROM")
    if not api_key or not email_from:
        return False, "Missing RESEND_API_KEY or EMAIL_FROM"

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": email_from,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=20,
    )
    if response.ok:
        return True, response.text
    return False, response.text


def notify_supabase_partner(perk_request: PerkRequest):
    to_email = os.getenv("SUPABASE_PARTNER_EMAIL") or os.getenv("PARTNER_NOTIFICATION_EMAIL")
    if not to_email:
        return False, "Missing SUPABASE_PARTNER_EMAIL"

    subject = f"[Spark Claw] Supabase perk request #{perk_request.id} - {perk_request.company_name}"
    text = f"""
A new Supabase perk request has been submitted through the Spark Claw Perks Console.

Request ID: {perk_request.id}
Requester: {perk_request.requester_name}
Requester Email: {perk_request.requester_email}
Company: {perk_request.company_name}
Company Website: {perk_request.company_website or '-'}
Perk Type: {perk_request.perk_type}
Portfolio Verified: {'Yes' if perk_request.portfolio_verified else 'Needs review'}
Verification Reason: {perk_request.verification_reason or '-'}
Expected Monthly Spend: {perk_request.expected_monthly_spend or '-'}
Use Case:
{perk_request.use_case}

Internal Notes:
{perk_request.notes or '-'}
""".strip()
    html = f"""
    <h2>Spark Claw Supabase Perk Request</h2>
    <p><strong>Request ID:</strong> {perk_request.id}</p>
    <p><strong>Requester:</strong> {perk_request.requester_name} ({perk_request.requester_email})</p>
    <p><strong>Company:</strong> {perk_request.company_name}</p>
    <p><strong>Company Website:</strong> {perk_request.company_website or '-'}</p>
    <p><strong>Perk Type:</strong> {perk_request.perk_type}</p>
    <p><strong>Portfolio Verified:</strong> {'Yes' if perk_request.portfolio_verified else 'Needs review'}</p>
    <p><strong>Verification Reason:</strong> {perk_request.verification_reason or '-'}</p>
    <p><strong>Expected Monthly Spend:</strong> {perk_request.expected_monthly_spend or '-'}</p>
    <p><strong>Use Case:</strong></p>
    <pre>{perk_request.use_case}</pre>
    <p><strong>Internal Notes:</strong></p>
    <pre>{perk_request.notes or '-'}</pre>
    """
    success, response_text = send_resend_email(subject, html, text, to_email)
    perk_request.last_email_status = "sent" if success else "failed"
    perk_request.last_email_response = response_text[:2000]
    if success:
        perk_request.partner_notified_at = now_utc()
        perk_request.partner_notified_email = to_email
        if perk_request.status not in {"code_received", "fulfilled"}:
            perk_request.status = "partner_emailed"
    db.session.commit()
    add_audit(perk_request.id, "system", "supabase_email_sent" if success else "supabase_email_failed", response_text[:300])
    return success, response_text


@app.route("/", methods=["GET"])
def index():
    recent_requests = PerkRequest.query.order_by(PerkRequest.requested_at.desc()).limit(5).all()
    company_count = PortfolioCompany.query.filter_by(active=True).count()
    return render_template("index.html", recent_requests=recent_requests, company_count=company_count)


@app.route("/request", methods=["POST"])
def submit_request():
    form = request.form
    requester_name = form.get("requester_name", "").strip()
    requester_email = form.get("requester_email", "").strip().lower()
    company_name = form.get("company_name", "").strip()
    company_website = form.get("company_website", "").strip()
    perk_type = form.get("perk_type", "Supabase credits").strip()
    use_case = form.get("use_case", "").strip()
    expected_monthly_spend = form.get("expected_monthly_spend", "").strip()
    notes = form.get("notes", "").strip()

    required_fields = [requester_name, requester_email, company_name, use_case]
    if not all(required_fields):
        flash("Please fill in all required fields.", "danger")
        return redirect(url_for("index"))

    company_match, reason = get_portfolio_match(requester_email, company_name)
    verified = bool(company_match and "domain" in reason.lower())
    status = "portfolio_verified" if verified else "needs_review"

    new_request = PerkRequest(
        requester_name=requester_name,
        requester_email=requester_email,
        company_name=company_name,
        company_website=company_website,
        perk_type=perk_type,
        use_case=use_case,
        expected_monthly_spend=expected_monthly_spend,
        notes=notes,
        portfolio_verified=verified,
        verification_reason=reason,
        status=status,
    )
    db.session.add(new_request)
    db.session.commit()
    add_audit(new_request.id, requester_email, "request_submitted", f"status={status}")

    auto_notify = os.getenv("AUTO_NOTIFY_ON_VERIFIED_REQUEST", "true").lower() == "true"
    if verified and perk_type.lower().startswith("supabase") and auto_notify:
        success, _ = notify_supabase_partner(new_request)
        if success:
            flash(f"Request #{new_request.id} submitted and Supabase was notified.", "success")
        else:
            flash(f"Request #{new_request.id} submitted, but partner email is not configured yet.", "warning")
    else:
        flash(f"Request #{new_request.id} submitted successfully.", "success")
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if email == os.getenv("ADMIN_EMAIL", "admin@sparkclaw.co.kr").lower() and password == os.getenv("ADMIN_PASSWORD", "change-me"):
            session["is_admin"] = True
            session["admin_email"] = email
            flash("Signed in successfully.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin credentials.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET"])
@admin_required
def admin_dashboard():
    selected_status = request.args.get("status", "all")
    search = request.args.get("search", "").strip().lower()

    query = PerkRequest.query.order_by(PerkRequest.requested_at.desc())
    if selected_status != "all":
        query = query.filter_by(status=selected_status)
    rows = query.all()
    if search:
        rows = [
            row
            for row in rows
            if search in row.requester_email.lower()
            or search in row.company_name.lower()
            or search in row.requester_name.lower()
        ]

    stats = {
        "total": PerkRequest.query.count(),
        "verified": PerkRequest.query.filter_by(portfolio_verified=True).count(),
        "needs_review": PerkRequest.query.filter_by(status="needs_review").count(),
        "partner_emailed": PerkRequest.query.filter(PerkRequest.partner_notified_at.isnot(None)).count(),
        "fulfilled": PerkRequest.query.filter_by(status="fulfilled").count(),
    }
    companies = PortfolioCompany.query.order_by(PortfolioCompany.created_at.desc()).all()
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(15).all()
    return render_template(
        "admin_dashboard.html",
        requests=rows,
        stats=stats,
        selected_status=selected_status,
        search=search,
        companies=companies,
        recent_logs=recent_logs,
    )


@app.route("/admin/portfolio", methods=["POST"])
@admin_required
def add_portfolio_company():
    name = request.form.get("name", "").strip()
    website = request.form.get("website", "").strip()
    allowed_domains = request.form.get("allowed_domains", "").strip().lower()
    notes = request.form.get("notes", "").strip()

    if not name or not allowed_domains:
        flash("Company name and at least one domain are required.", "danger")
        return redirect(url_for("admin_dashboard"))

    existing = PortfolioCompany.query.filter(db.func.lower(PortfolioCompany.name) == name.lower()).first()
    if existing:
        existing.website = website
        existing.allowed_domains = allowed_domains
        existing.notes = notes
        existing.active = True
        db.session.commit()
        add_audit(None, session.get("admin_email", "admin"), "portfolio_company_updated", name)
        flash(f"Updated portfolio company: {name}", "success")
    else:
        company = PortfolioCompany(name=name, website=website, allowed_domains=allowed_domains, notes=notes)
        db.session.add(company)
        db.session.commit()
        add_audit(None, session.get("admin_email", "admin"), "portfolio_company_added", name)
        flash(f"Added portfolio company: {name}", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/request/<int:request_id>")
@admin_required
def request_detail(request_id: int):
    row = PerkRequest.query.get_or_404(request_id)
    logs = AuditLog.query.filter_by(request_id=request_id).order_by(AuditLog.created_at.desc()).all()
    return render_template("request_detail.html", row=row, logs=logs)


@app.route("/admin/request/<int:request_id>/action", methods=["POST"])
@admin_required
def update_request(request_id: int):
    row = PerkRequest.query.get_or_404(request_id)
    action = request.form.get("action", "")
    actor = session.get("admin_email", "admin")

    if action == "approve":
        row.status = "approved"
        row.reviewed_at = now_utc()
        row.portfolio_verified = True
        row.verification_reason = row.verification_reason or "Approved manually by admin"
        db.session.commit()
        add_audit(row.id, actor, "request_approved")
        flash(f"Request #{row.id} approved.", "success")
    elif action == "reject":
        row.status = "rejected"
        row.reviewed_at = now_utc()
        db.session.commit()
        add_audit(row.id, actor, "request_rejected")
        flash(f"Request #{row.id} rejected.", "warning")
    elif action == "send_email":
        success, _ = notify_supabase_partner(row)
        flash(
            f"Partner email {'sent' if success else 'failed'} for request #{row.id}.",
            "success" if success else "danger",
        )
    elif action == "code_received":
        row.code_received_at = now_utc()
        row.status = "code_received"
        db.session.commit()
        add_audit(row.id, actor, "code_received")
        flash(f"Marked request #{row.id} as code received.", "success")
    elif action == "fulfilled":
        row.code_delivered_at = now_utc()
        row.status = "fulfilled"
        db.session.commit()
        add_audit(row.id, actor, "request_fulfilled")
        flash(f"Marked request #{row.id} as fulfilled.", "success")
    else:
        flash("Unknown action.", "danger")

    return redirect(url_for("request_detail", request_id=request_id))


@app.route("/admin/export.csv")
@admin_required
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "request_id",
        "requester_name",
        "requester_email",
        "company_name",
        "perk_type",
        "status",
        "portfolio_verified",
        "requested_at",
        "partner_notified_at",
        "code_received_at",
        "code_delivered_at",
    ])
    for row in PerkRequest.query.order_by(PerkRequest.requested_at.desc()).all():
        writer.writerow([
            row.id,
            row.requester_name,
            row.requester_email,
            row.company_name,
            row.perk_type,
            row.status,
            row.portfolio_verified,
            row.requested_at.isoformat() if row.requested_at else "",
            row.partner_notified_at.isoformat() if row.partner_notified_at else "",
            row.code_received_at.isoformat() if row.code_received_at else "",
            row.code_delivered_at.isoformat() if row.code_delivered_at else "",
        ])
    memory = io.BytesIO(output.getvalue().encode("utf-8"))
    memory.seek(0)
    return send_file(memory, mimetype="text/csv", as_attachment=True, download_name="sparkclaw_perk_requests.csv")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
