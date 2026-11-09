"""BraXhn Verkstad – arbetsordrar, bokningar och lager för en hobbyverkstad (cykel + elektronik).

En enda container: Flask + SQLite. Publik del (bokning, status) och admin-del (allt annat).
"""
import csv
import io
import json
import logging
import os
import secrets
import smtplib
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from flask.sessions import SecureCookieSessionInterface
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

VERSION = "1.0.0"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "verkstad.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verkstad")

# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------
WO_STATUSES = [
    ("inlamnad", "Inlämnad"),
    ("felsokning", "Felsökning"),
    ("offert", "Väntar på ditt godkännande"),
    ("vantar_delar", "Väntar på delar"),
    ("pagar", "Pågår"),
    ("klar", "Klar för hämtning"),
    ("utlamnad", "Utlämnad"),
    ("avbruten", "Avbruten"),
]
WO_STATUS_LABEL = dict(WO_STATUSES)
WO_OPEN = ("inlamnad", "felsokning", "offert", "vantar_delar", "pagar", "klar")

BOOKING_STATUSES = {"ny": "Ny", "bekraftad": "Bekräftad", "avbojd": "Avböjd",
                    "konverterad": "Inlämnad", "avbokad": "Avbokad"}

# Inlämningschecklistor per kategori – det som skiljer en cykel från en förstärkare.
INTAKE_CHECKLISTS = {
    "Cykel": ["Ramnummer noterat", "Repor/skador fotograferade", "Lås/nyckel medlämnat",
              "Lampor/dator borttagna eller noterade", "Däcktryck kontrollerat"],
    "Elektronik": ["Serienummer noterat", "Laddare/kablar medlämnade", "Synliga skador noterade",
                   "Lösenord/data – kund informerad om risk", "Strömförsörjning testad vid inlämning"],
}

SV_DAYS = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]
SV_MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
TAG_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

DEFAULT_SETTINGS = {
    "business_name": "BraXhn Verkstad",
    "contact_email": "",
    "contact_phone": "",
    "address": "",
    "public_url": os.environ.get("PUBLIC_URL", "").rstrip("/"),
    "labor_rate": "400",
    "categories": "Cykel,Elektronik",
    "booking_enabled": "1",
    "booking_intro": "Boka en tid för att lämna in din cykel eller elektronik för service eller reparation.",
    "open_days": "1,2,3,4,5",
    "open_from": "17:00",
    "open_to": "20:00",
    "slot_minutes": "30",
    "slot_capacity": "1",
    "days_ahead": "21",
    "min_notice_hours": "12",
    "blocked_dates": "",
    "retention_months": "24",
    "calendar_token": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, phone TEXT, notes TEXT,
    created_at TEXT NOT NULL, last_activity TEXT, anonymized INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT, tag_code TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY, sku TEXT UNIQUE, name TEXT NOT NULL, category TEXT,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    qty REAL NOT NULL DEFAULT 0, min_qty REAL NOT NULL DEFAULT 0, unit TEXT DEFAULT 'st',
    unit_cost REAL DEFAULT 0, unit_price REAL DEFAULT 0, supplier TEXT, supplier_url TEXT,
    notes TEXT, tag_code TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
    customer_id INTEGER REFERENCES customers(id), booking_id INTEGER,
    category TEXT, item TEXT, serial TEXT, accessories TEXT, problem TEXT, diagnosis TEXT,
    internal_notes TEXT, checklist TEXT, status TEXT NOT NULL DEFAULT 'inlamnad',
    labor_minutes INTEGER NOT NULL DEFAULT 0, labor_rate REAL, price_quote REAL,
    quote_approved_at TEXT, due_date TEXT,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    tag_code TEXT UNIQUE, token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS wo_events (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL, text TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wo_lines (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    part_id INTEGER REFERENCES parts(id) ON DELETE SET NULL, description TEXT NOT NULL,
    qty REAL NOT NULL, unit_price REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    delta REAL NOT NULL, reason TEXT, work_order_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
    category TEXT, item TEXT, description TEXT, slot_start TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ny', token TEXT NOT NULL UNIQUE, work_order_id INTEGER,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_bookings_slot ON bookings(slot_start);
CREATE INDEX IF NOT EXISTS ix_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS ix_parts_name ON parts(name);
"""

# ---------------------------------------------------------------------------
# App-setup
# ---------------------------------------------------------------------------
os.makedirs(DATA_DIR, exist_ok=True)


def _persistent_secret(filename, env_name, nbytes=32):
    val = os.environ.get(env_name)
    if val:
        return val
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    val = secrets.token_urlsafe(nbytes)
    with open(path, "w") as f:
        f.write(val)
    os.chmod(path, 0o600)
    return val


class DynamicSecureSession(SecureCookieSessionInterface):
    """Secure-flagga på cookien när anslutningen är https (bakom Cloudflare), inte på LAN-http."""

    def get_cookie_secure(self, app):
        try:
            return request.is_secure
        except RuntimeError:
            return False


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_persistent_secret("secret_key", "SECRET_KEY"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=1024 * 1024,
)
app.session_interface = DynamicSecureSession()
if os.environ.get("TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_admin_pw = os.environ.get("ADMIN_PASSWORD")
if not _admin_pw:
    _admin_pw = _persistent_secret("admin_password", "ADMIN_PASSWORD_UNUSED", 12)
    log.warning("ADMIN_PASSWORD saknas – genererat lösenord finns i %s/admin_password", DATA_DIR)
ADMIN_HASH = generate_password_hash(_admin_pw)
del _admin_pw


# ---------------------------------------------------------------------------
# Databas
# ---------------------------------------------------------------------------
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def init_db():
    conn = connect()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if os.environ.get("PUBLIC_URL"):  # miljövariabeln vinner, så att Unraid-mallen styr
        conn.execute("UPDATE settings SET value=? WHERE key='public_url'", (os.environ["PUBLIC_URL"].rstrip("/"),))
    if not conn.execute("SELECT value FROM settings WHERE key='calendar_token'").fetchone()[0]:
        conn.execute("UPDATE settings SET value=? WHERE key='calendar_token'", (secrets.token_urlsafe(24),))
    conn.commit()
    conn.close()


def now():
    return datetime.now().replace(microsecond=0)


def now_s():
    return now().isoformat(sep=" ")


def settings():
    if "settings" not in g:
        g.settings = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}
    return g.settings


def setting_int(key, default):
    try:
        return int(settings().get(key) or default)
    except ValueError:
        return default


def categories():
    return [c.strip() for c in settings()["categories"].split(",") if c.strip()]


def new_tag():
    while True:
        code = "".join(secrets.choice(TAG_ALPHABET) for _ in range(8))
        if not tag_lookup(code):
            return code


def tag_lookup(code):
    for table, kind in (("locations", "plats"), ("parts", "del"), ("work_orders", "ao")):
        row = q(f"SELECT id FROM {table} WHERE tag_code=?", (code,), one=True)
        if row:
            return kind, row["id"]
    return None


def base_url():
    return settings().get("public_url") or request.url_root.rstrip("/")


def tag_url(code):
    return f"{base_url()}/t/{code}"


def next_wo_number(db):
    year = now().year
    row = db.execute("SELECT number FROM work_orders WHERE number LIKE ? ORDER BY id DESC LIMIT 1",
                     (f"AO-{year}-%",)).fetchone()
    n = int(row["number"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"AO-{year}-{n:04d}"


def touch_customer(db, customer_id):
    if customer_id:
        db.execute("UPDATE customers SET last_activity=? WHERE id=?", (now_s(), customer_id))


def to_float(val, default=0.0):
    try:
        return float(str(val).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return default


def to_int(val, default=0):
    try:
        return int(float(str(val).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def clean(val, maxlen=2000):
    return (val or "").strip()[:maxlen]


# ---------------------------------------------------------------------------
# Säkerhet: CSRF, rate limit, headers, inloggning
# ---------------------------------------------------------------------------
_rl_hits = {}
_rl_lock = threading.Lock()


def client_ip():
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "okänd"


def rate_limited(key, limit, window, record=True):
    t = time.time()
    with _rl_lock:
        hits = [h for h in _rl_hits.get(key, []) if t - h < window]
        blocked = len(hits) >= limit
        if record and not blocked:
            hits.append(t)
        _rl_hits[key] = hits
        if len(_rl_hits) > 5000:  # skydd mot minnesläcka
            for k in list(_rl_hits)[:1000]:
                _rl_hits.pop(k, None)
        return blocked


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        expected = session.get("_csrf")
        sent = request.form.get("_csrf", "")
        if not expected or not secrets.compare_digest(expected, sent):
            abort(400, "Formuläret har gått ut. Ladda om sidan och försök igen.")


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith(("/admin", "/s/", "/t/", "/login")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# E-post (valfritt, via SMTP-miljövariabler)
# ---------------------------------------------------------------------------
def mail_enabled():
    return bool(os.environ.get("SMTP_HOST"))


def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host or not to:
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", user)

    def run():
        try:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                srv = smtplib.SMTP(host, port, timeout=20)
                srv.starttls()
            if user:
                srv.login(user, password)
            srv.send_message(msg)
            srv.quit()
        except Exception:  # noqa: BLE001 – e-post får aldrig fälla en request
            log.exception("Kunde inte skicka e-post till %s", to)

    threading.Thread(target=run, daemon=True).start()


def notify_admin(subject, body):
    send_mail(os.environ.get("NOTIFY_EMAIL") or settings().get("contact_email"), subject, body)


# ---------------------------------------------------------------------------
# Mallhjälpare
# ---------------------------------------------------------------------------
def fmt_dt(val, with_time=True):
    if not val:
        return ""
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val.replace(" ", "T"))
        except ValueError:
            return val
    if isinstance(val, datetime):
        s = f"{SV_DAYS[val.weekday()]} {val.day} {SV_MONTHS[val.month - 1]}"
        if val.year != now().year:
            s += f" {val.year}"
        return f"{s} {val:%H:%M}" if with_time else s
    if isinstance(val, date):
        return f"{SV_DAYS[val.weekday()]} {val.day} {SV_MONTHS[val.month - 1]}"
    return str(val)


def fmt_money(val):
    v = to_float(val)
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    return f"{s} kr"


def fmt_qty(val):
    v = to_float(val)
    return str(int(v)) if v == int(v) else f"{v:g}".replace(".", ",")


def qr_svg(data):
    """QR-kod som inline-SVG (segno). Faller tillbaka till ingenting om biblioteket saknas."""
    try:
        import segno
        buf = io.BytesIO()
        segno.make(data, error="m").save(buf, kind="svg", scale=4, border=2, xmldecl=False,
                                         svgclass="qr", dark="#000", light="#fff")
        return Markup(buf.getvalue().decode())
    except Exception:  # noqa: BLE001
        return Markup("")


app.jinja_env.filters.update(dt=fmt_dt, money=fmt_money, qty=fmt_qty,
                             d=lambda v: fmt_dt(v, with_time=False))
app.jinja_env.globals.update(csrf_token=csrf_token, WO_STATUSES=WO_STATUSES,
                             WO_STATUS_LABEL=WO_STATUS_LABEL, BOOKING_STATUSES=BOOKING_STATUSES,
                             qr_svg=qr_svg, tag_url=tag_url, VERSION=VERSION)


@app.context_processor
def inject_globals():
    ctx = {"S": settings(), "is_admin": bool(session.get("admin"))}
    if session.get("admin"):
        ctx["nav_new_bookings"] = q("SELECT COUNT(*) c FROM bookings WHERE status='ny'", one=True)["c"]
    return ctx


# ---------------------------------------------------------------------------
# Bokningslogik
# ---------------------------------------------------------------------------
def _parse_hhmm(val, default):
    try:
        h, m = val.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


def available_slots():
    s = settings()
    open_days = {int(d) for d in s["open_days"].split(",") if d.strip().isdigit()}
    blocked = {d.strip() for d in s["blocked_dates"].split(",") if d.strip()}
    step = max(10, setting_int("slot_minutes", 30))
    capacity = max(1, setting_int("slot_capacity", 1))
    days_ahead = min(120, max(1, setting_int("days_ahead", 21)))
    earliest = now() + timedelta(hours=setting_int("min_notice_hours", 12))
    fh, fm = _parse_hhmm(s["open_from"], (17, 0))
    th, tm = _parse_hhmm(s["open_to"], (20, 0))

    taken = {}
    for r in q("SELECT slot_start, COUNT(*) c FROM bookings WHERE status IN ('ny','bekraftad') "
               "AND slot_start >= ? GROUP BY slot_start", (now().strftime("%Y-%m-%dT%H:%M"),)):
        taken[r["slot_start"]] = r["c"]

    result = []
    today = now().date()
    for i in range(days_ahead + 1):
        d = today + timedelta(days=i)
        if d.isoweekday() not in open_days or d.isoformat() in blocked:
            continue
        t = datetime(d.year, d.month, d.day, fh, fm)
        end = datetime(d.year, d.month, d.day, th, tm)
        slots = []
        while t + timedelta(minutes=step) <= end:
            key = t.strftime("%Y-%m-%dT%H:%M")
            if t >= earliest and taken.get(key, 0) < capacity:
                slots.append(key)
            t += timedelta(minutes=step)
        if slots:
            result.append((d, slots))
    return result


# ---------------------------------------------------------------------------
# Publika sidor
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("book"))


@app.route("/healthz")
def healthz():
    q("SELECT 1")
    return {"ok": True, "version": VERSION}


@app.route("/integritet")
def privacy():
    return render_template("public/privacy.html")


@app.route("/boka", methods=["GET", "POST"])
def book():
    s = settings()
    if s.get("booking_enabled") != "1":
        return render_template("public/closed.html")
    slots = available_slots()
    valid_slots = {k for _, day in slots for k in day}
    form = request.form if request.method == "POST" else {}
    errors = []

    if request.method == "POST":
        if form.get("website"):  # honeypot – botar fyller i allt
            return redirect(url_for("book"))
        if rate_limited(f"book:{client_ip()}", 5, 3600):
            errors.append("För många bokningar från din anslutning. Försök igen senare.")
        name, email, phone = clean(form.get("name"), 120), clean(form.get("email"), 200).lower(), clean(form.get("phone"), 40)
        category, item = clean(form.get("category"), 40), clean(form.get("item"), 200)
        description, slot = clean(form.get("description"), 3000), form.get("slot", "")
        if not name:
            errors.append("Ange ditt namn.")
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            errors.append("Ange en giltig e-postadress.")
        if category not in categories():
            errors.append("Välj vad du vill lämna in.")
        if not item:
            errors.append("Beskriv vad det är (t.ex. märke och modell).")
        if not description:
            errors.append("Beskriv felet eller vad du vill ha gjort.")
        if slot not in valid_slots:
            errors.append("Den valda tiden är inte längre ledig. Välj en annan tid.")
        if not form.get("consent"):
            errors.append("Du behöver godkänna att vi sparar dina uppgifter för att hantera bokningen.")

        if not errors:
            db = get_db()
            with db:
                # Atomisk kontroll av kapacitet inne i transaktionen
                cnt = db.execute("SELECT COUNT(*) FROM bookings WHERE slot_start=? AND status IN ('ny','bekraftad')",
                                 (slot,)).fetchone()[0]
                if cnt >= max(1, setting_int("slot_capacity", 1)):
                    errors.append("Tiden blev precis bokad av någon annan. Välj en annan tid.")
                else:
                    cust = db.execute("SELECT id FROM customers WHERE lower(email)=? AND anonymized=0 "
                                      "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
                    if cust:
                        cid = cust["id"]
                        if phone:
                            db.execute("UPDATE customers SET phone=COALESCE(NULLIF(phone,''), ?) WHERE id=?", (phone, cid))
                    else:
                        cid = db.execute("INSERT INTO customers(name, email, phone, created_at, last_activity) "
                                         "VALUES (?,?,?,?,?)", (name, email, phone, now_s(), now_s())).lastrowid
                    touch_customer(db, cid)
                    token = secrets.token_urlsafe(24)
                    db.execute("INSERT INTO bookings(customer_id, category, item, description, slot_start, token, created_at) "
                               "VALUES (?,?,?,?,?,?,?)", (cid, category, item, description, slot, token, now_s()))
            if not errors:
                status_link = f"{base_url()}{url_for('status', token=token)}"
                send_mail(email, f"Bokningsförfrågan mottagen – {s['business_name']}",
                          f"Hej {name}!\n\nVi har tagit emot din bokningsförfrågan för {fmt_dt(slot)}.\n"
                          f"Gäller: {category} – {item}\n\nDu får besked när tiden är bekräftad. "
                          f"Följ din bokning här:\n{status_link}\n\nHälsningar\n{s['business_name']}")
                notify_admin(f"Ny bokning {fmt_dt(slot)}: {category} – {item}",
                             f"{name} ({email}, {phone})\n\n{description}\n\n{base_url()}/admin/bokningar")
                return redirect(url_for("status", token=token, ny=1))

    return render_template("public/book.html", slots=slots, form=form, errors=errors, categories=categories())


def _status_context(token):
    wo = q("SELECT w.*, c.name customer_name FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
           "WHERE w.token=?", (token,), one=True)
    booking = None
    if not wo:
        booking = q("SELECT * FROM bookings WHERE token=?", (token,), one=True)
        if booking and booking["work_order_id"]:
            wo = q("SELECT w.*, c.name customer_name FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
                   "WHERE w.id=?", (booking["work_order_id"],), one=True)
    if not wo and not booking:
        abort(404)
    return wo, booking


@app.route("/s/<token>")
def status(token):
    if rate_limited(f"status:{client_ip()}", 120, 600):
        abort(429)
    wo, booking = _status_context(token)
    events = lines = []
    totals = None
    if wo:
        events = q("SELECT * FROM wo_events WHERE work_order_id=? AND public=1 ORDER BY id DESC", (wo["id"],))
        lines = q("SELECT * FROM wo_lines WHERE work_order_id=? ORDER BY id", (wo["id"],))
        totals = wo_totals(wo, lines)
    return render_template("public/status.html", wo=wo, booking=booking, events=events, lines=lines,
                           totals=totals, token=token, is_new=request.args.get("ny"))


@app.route("/s/<token>/godkann", methods=["POST"])
def approve_quote(token):
    wo, _ = _status_context(token)
    if not wo or wo["status"] != "offert":
        abort(400)
    db = get_db()
    with db:
        db.execute("UPDATE work_orders SET status='pagar', quote_approved_at=?, updated_at=? WHERE id=?",
                   (now_s(), now_s(), wo["id"]))
        db.execute("INSERT INTO wo_events(work_order_id, created_at, text, public) VALUES (?,?,?,1)",
                   (wo["id"], now_s(), f"Kunden godkände offerten ({fmt_money(wo['price_quote'])}). Arbetet påbörjas."))
    notify_admin(f"{wo['number']}: offert godkänd", f"{wo['item']} – {fmt_money(wo['price_quote'])}")
    flash("Tack! Offerten är godkänd och vi sätter igång.")
    return redirect(url_for("status", token=token))


@app.route("/s/<token>/avboka", methods=["POST"])
def cancel_booking(token):
    booking = q("SELECT * FROM bookings WHERE token=?", (token,), one=True)
    if not booking or booking["status"] not in ("ny", "bekraftad"):
        abort(400)
    x("UPDATE bookings SET status='avbokad' WHERE id=?", (booking["id"],))
    notify_admin(f"Avbokning {fmt_dt(booking['slot_start'])}", f"{booking['category']} – {booking['item']}")
    flash("Din bokning är avbokad.")
    return redirect(url_for("status", token=token))


@app.route("/kalender/<token>.ics")
def calendar_feed(token):
    if not secrets.compare_digest(token, settings()["calendar_token"]):
        abort(404)
    step = setting_int("slot_minutes", 30)
    rows = q("SELECT b.*, c.name, c.phone FROM bookings b LEFT JOIN customers c ON c.id=b.customer_id "
             "WHERE b.status IN ('ny','bekraftad') AND b.slot_start >= ?",
             ((now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),))

    def esc(v):
        return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//braxhn-verkstad//SV", "X-WR-CALNAME:BraXhn Verkstad – bokningar"]
    for b in rows:
        start = datetime.fromisoformat(b["slot_start"])
        end = start + timedelta(minutes=step)
        prefix = "" if b["status"] == "bekraftad" else "[OBEKRÄFTAD] "
        out += ["BEGIN:VEVENT", f"UID:booking-{b['id']}@verkstad",
                f"DTSTAMP:{now():%Y%m%dT%H%M%S}",
                f"DTSTART:{start:%Y%m%dT%H%M%S}",  # flytande lokal tid (giltigt enligt RFC 5545)
                f"DTEND:{end:%Y%m%dT%H%M%S}",
                f"SUMMARY:{esc(prefix + (b['category'] or '') + ': ' + (b['item'] or ''))}",
                f"DESCRIPTION:{esc((b['name'] or '') + ' ' + (b['phone'] or '') + chr(10) + (b['description'] or ''))}",
                "END:VEVENT"]
    out.append("END:VCALENDAR")
    return Response("\r\n".join(out) + "\r\n", mimetype="text/calendar")


# ---------------------------------------------------------------------------
# Inloggning och taggar
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    nxt = request.values.get("next", "")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("dashboard")
    if request.method == "POST":
        key = f"login:{client_ip()}"
        if rate_limited(key, 8, 900, record=False):
            error = "För många misslyckade försök. Vänta 15 minuter."
        elif (secrets.compare_digest(request.form.get("username", ""), ADMIN_USER)
              and check_password_hash(ADMIN_HASH, request.form.get("password", ""))):
            session.clear()
            session["admin"] = True
            session.permanent = True
            csrf_token()
            return redirect(nxt)
        else:
            rate_limited(key, 8, 900)
            log.warning("Misslyckad inloggning från %s", client_ip())
            error = "Fel användarnamn eller lösenord."
    return render_template("public/login.html", error=error, next=nxt)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/t/<code>")
@login_required
def tag(code):
    hit = tag_lookup(code.lower())
    if not hit:
        abort(404)
    kind, oid = hit
    return redirect({"plats": url_for("location_detail", lid=oid), "del": url_for("part_detail", pid=oid),
                     "ao": url_for("wo_detail", wid=oid)}[kind])


# ---------------------------------------------------------------------------
# Admin: översikt
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def dashboard():
    counts = {r["status"]: r["c"] for r in q("SELECT status, COUNT(*) c FROM work_orders GROUP BY status")}
    today = now().strftime("%Y-%m-%d")
    upcoming = q("SELECT b.*, c.name FROM bookings b LEFT JOIN customers c ON c.id=b.customer_id "
                 "WHERE b.status IN ('ny','bekraftad') AND b.slot_start >= ? ORDER BY b.slot_start LIMIT 10",
                 (today,))
    low = q("SELECT p.*, l.name loc FROM parts p LEFT JOIN locations l ON l.id=p.location_id "
            "WHERE p.active=1 AND p.min_qty > 0 AND p.qty <= p.min_qty ORDER BY p.name")
    stale = q(f"SELECT w.*, c.name customer_name FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
              f"WHERE w.status IN ({','.join('?' * len(WO_OPEN))}) AND w.updated_at < ? ORDER BY w.updated_at",
              (*WO_OPEN, (now() - timedelta(days=7)).isoformat(sep=" ")))
    overdue = q(f"SELECT w.*, c.name customer_name FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
                f"WHERE w.status IN ({','.join('?' * len(WO_OPEN))}) AND w.due_date IS NOT NULL AND w.due_date <> '' "
                f"AND w.due_date < ? ORDER BY w.due_date", (*WO_OPEN, today))
    month_start = now().strftime("%Y-%m-01")
    closed_month = q("SELECT * FROM work_orders WHERE status='utlamnad' AND closed_at >= ?", (month_start,))
    revenue = sum(wo_totals(w, q("SELECT * FROM wo_lines WHERE work_order_id=?", (w["id"],)))["total"]
                  for w in closed_month)
    return render_template("admin/dashboard.html", counts=counts, upcoming=upcoming, low=low, stale=stale,
                           overdue=overdue, revenue=revenue, closed_count=len(closed_month))


# ---------------------------------------------------------------------------
# Admin: bokningar
# ---------------------------------------------------------------------------
@app.route("/admin/bokningar")
@login_required
def bookings_list():
    show = request.args.get("visa", "aktuella")
    if show == "alla":
        rows = q("SELECT b.*, c.name, c.email, c.phone FROM bookings b LEFT JOIN customers c ON c.id=b.customer_id "
                 "ORDER BY b.slot_start DESC LIMIT 300")
    else:
        rows = q("SELECT b.*, c.name, c.email, c.phone FROM bookings b LEFT JOIN customers c ON c.id=b.customer_id "
                 "WHERE b.status IN ('ny','bekraftad') ORDER BY b.slot_start")
    return render_template("admin/bookings.html", rows=rows, show=show,
                           cal_url=f"{base_url()}/kalender/{settings()['calendar_token']}.ics")


@app.route("/admin/bokningar/<int:bid>/status", methods=["POST"])
@login_required
def booking_status(bid):
    b = q("SELECT b.*, c.name, c.email FROM bookings b LEFT JOIN customers c ON c.id=b.customer_id WHERE b.id=?",
          (bid,), one=True) or abort(404)
    action = request.form.get("action")
    s = settings()
    link = f"{base_url()}{url_for('status', token=b['token'])}"
    if action == "bekrafta":
        x("UPDATE bookings SET status='bekraftad' WHERE id=?", (bid,))
        send_mail(b["email"], f"Bokning bekräftad {fmt_dt(b['slot_start'])} – {s['business_name']}",
                  f"Hej {b['name']}!\n\nDin tid {fmt_dt(b['slot_start'])} är bekräftad.\n"
                  f"{('Adress: ' + s['address'] + chr(10)) if s.get('address') else ''}"
                  f"\nDin bokning: {link}\n\nVälkommen!\n{s['business_name']}")
        flash("Bokningen är bekräftad." + (" Kunden har fått e-post." if mail_enabled() else ""))
    elif action == "avboj":
        reason = clean(request.form.get("reason"), 500)
        x("UPDATE bookings SET status='avbojd' WHERE id=?", (bid,))
        send_mail(b["email"], f"Angående din bokning – {s['business_name']}",
                  f"Hej {b['name']}!\n\nTyvärr kan vi inte ta emot din bokning {fmt_dt(b['slot_start'])}.\n"
                  f"{reason}\n\nDu är välkommen att boka en annan tid: {base_url()}/boka\n\n{s['business_name']}")
        flash("Bokningen är avböjd.")
    elif action == "avboka":
        x("UPDATE bookings SET status='avbokad' WHERE id=?", (bid,))
        flash("Bokningen är avbokad.")
    return redirect(request.referrer or url_for("bookings_list"))


@app.route("/admin/bokningar/<int:bid>/arbetsorder", methods=["POST"])
@login_required
def booking_to_wo(bid):
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True) or abort(404)
    if b["work_order_id"]:
        return redirect(url_for("wo_detail", wid=b["work_order_id"]))
    wid = create_work_order(customer_id=b["customer_id"], category=b["category"], item=b["item"],
                            problem=b["description"], booking_id=bid)
    x("UPDATE bookings SET status='konverterad', work_order_id=? WHERE id=?", (wid, bid))
    flash("Arbetsorder skapad från bokningen. Kunden ser nu status via samma länk.")
    return redirect(url_for("wo_detail", wid=wid))


# ---------------------------------------------------------------------------
# Admin: arbetsordrar
# ---------------------------------------------------------------------------
def create_work_order(customer_id, category, item, problem, booking_id=None, serial="", accessories="",
                      due_date=""):
    db = get_db()
    for _ in range(5):
        try:
            with db:
                number = next_wo_number(db)
                wid = db.execute(
                    "INSERT INTO work_orders(number, customer_id, booking_id, category, item, serial, accessories, "
                    "problem, status, labor_rate, tag_code, token, due_date, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'inlamnad', ?,?,?,?,?,?)",
                    (number, customer_id, booking_id, category, item, serial, accessories, problem,
                     to_float(settings()["labor_rate"]), new_tag(), secrets.token_urlsafe(24),
                     due_date or None, now_s(), now_s())).lastrowid
                db.execute("INSERT INTO wo_events(work_order_id, created_at, text, public) VALUES (?,?,?,1)",
                           (wid, now_s(), "Inlämnad och registrerad."))
                touch_customer(db, customer_id)
            return wid
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Kunde inte skapa arbetsordernummer")


def wo_totals(wo, lines):
    rate = to_float(wo["labor_rate"])
    labor = round((wo["labor_minutes"] or 0) / 60 * rate, 2)
    parts = round(sum(to_float(l["qty"]) * to_float(l["unit_price"]) for l in lines), 2)
    return {"labor": labor, "parts": parts, "total": round(labor + parts, 2), "hours": (wo["labor_minutes"] or 0) / 60}


@app.route("/admin/ao")
@login_required
def wo_list():
    status_filter = request.args.get("status", "oppna")
    search = clean(request.args.get("q"), 100)
    sql = ("SELECT w.*, c.name customer_name, l.name loc FROM work_orders w "
           "LEFT JOIN customers c ON c.id=w.customer_id LEFT JOIN locations l ON l.id=w.location_id WHERE 1=1")
    args = []
    if status_filter == "oppna":
        sql += f" AND w.status IN ({','.join('?' * len(WO_OPEN))})"
        args += WO_OPEN
    elif status_filter in WO_STATUS_LABEL:
        sql += " AND w.status=?"
        args.append(status_filter)
    if search:
        sql += " AND (w.number LIKE ? OR w.item LIKE ? OR w.serial LIKE ? OR c.name LIKE ? OR w.problem LIKE ?)"
        args += [f"%{search}%"] * 5
    sql += " ORDER BY CASE w.status WHEN 'klar' THEN 0 WHEN 'offert' THEN 1 ELSE 2 END, w.updated_at DESC LIMIT 300"
    return render_template("admin/wo_list.html", rows=q(sql, args), status_filter=status_filter, search=search)


@app.route("/admin/ao/ny", methods=["GET", "POST"])
@login_required
def wo_new():
    if request.method == "POST":
        f = request.form
        cid = to_int(f.get("customer_id"))
        if not cid and clean(f.get("new_name")):
            cid = x("INSERT INTO customers(name, email, phone, created_at, last_activity) VALUES (?,?,?,?,?)",
                    (clean(f.get("new_name"), 120), clean(f.get("new_email"), 200).lower(),
                     clean(f.get("new_phone"), 40), now_s(), now_s()))
        if not cid or not clean(f.get("item")):
            flash("Välj eller skapa en kund och ange vad som lämnas in.", "error")
            return redirect(url_for("wo_new"))
        wid = create_work_order(cid, clean(f.get("category"), 40), clean(f.get("item"), 200),
                                clean(f.get("problem"), 5000), serial=clean(f.get("serial"), 100),
                                accessories=clean(f.get("accessories"), 500), due_date=clean(f.get("due_date"), 10))
        return redirect(url_for("wo_detail", wid=wid))
    customers = q("SELECT id, name, phone FROM customers WHERE anonymized=0 ORDER BY name")
    return render_template("admin/wo_new.html", customers=customers, categories=categories(),
                           preselect=to_int(request.args.get("kund")))


def _load_wo(wid):
    return q("SELECT w.*, c.name customer_name, c.email customer_email, c.phone customer_phone, l.name loc "
             "FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
             "LEFT JOIN locations l ON l.id=w.location_id WHERE w.id=?", (wid,), one=True) or abort(404)


@app.route("/admin/ao/<int:wid>", methods=["GET", "POST"])
@login_required
def wo_detail(wid):
    wo = _load_wo(wid)
    if request.method == "POST":
        f = request.form
        checklist = json.dumps(f.getlist("checklist"), ensure_ascii=False)
        x("UPDATE work_orders SET category=?, item=?, serial=?, accessories=?, problem=?, diagnosis=?, "
          "internal_notes=?, labor_minutes=?, labor_rate=?, price_quote=?, due_date=?, location_id=?, checklist=?, "
          "updated_at=? WHERE id=?",
          (clean(f.get("category"), 40), clean(f.get("item"), 200), clean(f.get("serial"), 100),
           clean(f.get("accessories"), 500), clean(f.get("problem"), 5000), clean(f.get("diagnosis"), 5000),
           clean(f.get("internal_notes"), 5000), max(0, to_int(f.get("labor_minutes"))),
           to_float(f.get("labor_rate"), to_float(settings()["labor_rate"])),
           to_float(f.get("price_quote")) if clean(f.get("price_quote")) else None,
           clean(f.get("due_date"), 10) or None, to_int(f.get("location_id")) or None, checklist, now_s(), wid))
        flash("Sparat.")
        return redirect(url_for("wo_detail", wid=wid))
    lines = q("SELECT wl.*, p.sku, p.unit FROM wo_lines wl LEFT JOIN parts p ON p.id=wl.part_id "
              "WHERE wl.work_order_id=? ORDER BY wl.id", (wid,))
    events = q("SELECT * FROM wo_events WHERE work_order_id=? ORDER BY id DESC", (wid,))
    parts = q("SELECT p.id, p.name, p.sku, p.qty, p.unit, p.unit_price, l.name loc FROM parts p "
              "LEFT JOIN locations l ON l.id=p.location_id WHERE p.active=1 ORDER BY p.name")
    try:
        checked = set(json.loads(wo["checklist"] or "[]"))
    except ValueError:
        checked = set()
    return render_template("admin/wo_detail.html", wo=wo, lines=lines, events=events, parts=parts,
                           totals=wo_totals(wo, lines), locations=q("SELECT * FROM locations ORDER BY name"),
                           categories=categories(), checklist=INTAKE_CHECKLISTS.get(wo["category"], []),
                           checked=checked, mail=mail_enabled(),
                           status_url=f"{base_url()}{url_for('status', token=wo['token'])}")


@app.route("/admin/ao/<int:wid>/status", methods=["POST"])
@login_required
def wo_set_status(wid):
    wo = _load_wo(wid)
    new = request.form.get("status")
    if new not in WO_STATUS_LABEL:
        abort(400)
    if new == "offert" and wo["price_quote"] is None:
        flash("Ange ett offertbelopp och spara innan du skickar offerten.", "error")
        return redirect(url_for("wo_detail", wid=wid))
    msg = clean(request.form.get("message"), 2000)
    db = get_db()
    with db:
        db.execute("UPDATE work_orders SET status=?, updated_at=?, closed_at=? WHERE id=?",
                   (new, now_s(), now_s() if new in ("utlamnad", "avbruten") else None, wid))
        text = f"Status: {WO_STATUS_LABEL[new]}." + (f" {msg}" if msg else "")
        if new == "offert":
            text += f" Offert: {fmt_money(wo['price_quote'])}."
        db.execute("INSERT INTO wo_events(work_order_id, created_at, text, public) VALUES (?,?,?,1)",
                   (wid, now_s(), text))
        touch_customer(db, wo["customer_id"])
    if request.form.get("notify") and wo["customer_email"]:
        s = settings()
        link = f"{base_url()}{url_for('status', token=wo['token'])}"
        extra = {"offert": f"Vi har en offert på {fmt_money(wo['price_quote'])}. Godkänn den via länken nedan.",
                 "klar": "Din reparation är klar och kan hämtas."}.get(new, "")
        send_mail(wo["customer_email"], f"{wo['number']}: {WO_STATUS_LABEL[new]} – {s['business_name']}",
                  f"Hej {wo['customer_name']}!\n\n{wo['item']}: {WO_STATUS_LABEL[new]}.\n{extra}\n{msg}\n\n"
                  f"Följ ärendet: {link}\n\n{s['business_name']}")
        flash("Status uppdaterad och kunden meddelad.")
    else:
        flash("Status uppdaterad.")
    return redirect(url_for("wo_detail", wid=wid))


@app.route("/admin/ao/<int:wid>/handelse", methods=["POST"])
@login_required
def wo_add_event(wid):
    _load_wo(wid)
    text = clean(request.form.get("text"), 3000)
    if text:
        x("INSERT INTO wo_events(work_order_id, created_at, text, public) VALUES (?,?,?,?)",
          (wid, now_s(), text, 1 if request.form.get("public") else 0))
        x("UPDATE work_orders SET updated_at=? WHERE id=?", (now_s(), wid))
    return redirect(url_for("wo_detail", wid=wid) + "#logg")


@app.route("/admin/ao/<int:wid>/rad", methods=["POST"])
@login_required
def wo_add_line(wid):
    wo = _load_wo(wid)
    f = request.form
    qty = to_float(f.get("qty"), 1)
    if qty <= 0:
        flash("Antal måste vara större än noll.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#rader")
    pid = to_int(f.get("part_id"))
    db = get_db()
    with db:
        if pid:
            p = db.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone() or abort(404)
            price = to_float(f.get("unit_price"), p["unit_price"]) if clean(f.get("unit_price")) else p["unit_price"]
            db.execute("INSERT INTO wo_lines(work_order_id, part_id, description, qty, unit_price, created_at) "
                       "VALUES (?,?,?,?,?,?)", (wid, pid, p["name"], qty, price, now_s()))
            db.execute("UPDATE parts SET qty = qty - ? WHERE id=?", (qty, pid))
            db.execute("INSERT INTO stock_moves(part_id, delta, reason, work_order_id, created_at) VALUES (?,?,?,?,?)",
                       (pid, -qty, f"Används i {wo['number']}", wid, now_s()))
            remaining = p["qty"] - qty
            if remaining < 0:
                flash(f"Obs: lagersaldot för {p['name']} är nu negativt ({fmt_qty(remaining)}). Kontrollera lagret.", "error")
            elif p["min_qty"] and remaining <= p["min_qty"]:
                flash(f"{p['name']} är nu under beställningspunkten ({fmt_qty(remaining)} kvar).", "error")
        else:
            desc = clean(f.get("description"), 300)
            if not desc:
                flash("Välj en del från lagret eller skriv en beskrivning.", "error")
                return redirect(url_for("wo_detail", wid=wid) + "#rader")
            db.execute("INSERT INTO wo_lines(work_order_id, part_id, description, qty, unit_price, created_at) "
                       "VALUES (?,?,?,?,?,?)", (wid, None, desc, qty, to_float(f.get("unit_price")), now_s()))
        db.execute("UPDATE work_orders SET updated_at=? WHERE id=?", (now_s(), wid))
    return redirect(url_for("wo_detail", wid=wid) + "#rader")


@app.route("/admin/ao/<int:wid>/rad/<int:lid>/ta-bort", methods=["POST"])
@login_required
def wo_remove_line(wid, lid):
    wo = _load_wo(wid)
    line = q("SELECT * FROM wo_lines WHERE id=? AND work_order_id=?", (lid, wid), one=True) or abort(404)
    db = get_db()
    with db:
        db.execute("DELETE FROM wo_lines WHERE id=?", (lid,))
        if line["part_id"]:
            db.execute("UPDATE parts SET qty = qty + ? WHERE id=?", (line["qty"], line["part_id"]))
            db.execute("INSERT INTO stock_moves(part_id, delta, reason, work_order_id, created_at) VALUES (?,?,?,?,?)",
                       (line["part_id"], line["qty"], f"Återförd från {wo['number']}", wid, now_s()))
    flash("Raden är borttagen" + (" och delen återförd till lagret." if line["part_id"] else "."))
    return redirect(url_for("wo_detail", wid=wid) + "#rader")


@app.route("/admin/ao/<int:wid>/kvitto")
@login_required
def wo_receipt(wid):
    wo = _load_wo(wid)
    lines = q("SELECT * FROM wo_lines WHERE work_order_id=? ORDER BY id", (wid,))
    return render_template("admin/wo_receipt.html", wo=wo, lines=lines, totals=wo_totals(wo, lines),
                           kind=request.args.get("typ", "inlamning"),
                           status_url=f"{base_url()}{url_for('status', token=wo['token'])}")


# ---------------------------------------------------------------------------
# Admin: kunder (inkl. GDPR)
# ---------------------------------------------------------------------------
@app.route("/admin/kunder")
@login_required
def customers_list():
    search = clean(request.args.get("q"), 100)
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM work_orders w WHERE w.customer_id=c.id) wo_count "
           "FROM customers c WHERE c.anonymized=0")
    args = []
    if search:
        sql += " AND (c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?)"
        args = [f"%{search}%"] * 3
    return render_template("admin/customers.html", rows=q(sql + " ORDER BY c.name LIMIT 500", args), search=search)


@app.route("/admin/kunder/<int:cid>", methods=["GET", "POST"])
@login_required
def customer_detail(cid):
    c = q("SELECT * FROM customers WHERE id=?", (cid,), one=True) or abort(404)
    if request.method == "POST":
        name = clean(request.form.get("name"), 120)
        if not name:
            flash("Namn krävs.", "error")
        else:
            x("UPDATE customers SET name=?, email=?, phone=?, notes=? WHERE id=?",
              (name, clean(request.form.get("email"), 200).lower(), clean(request.form.get("phone"), 40),
               clean(request.form.get("notes"), 3000), cid))
            flash("Kunden är uppdaterad.")
        return redirect(url_for("customer_detail", cid=cid))
    wos = q("SELECT * FROM work_orders WHERE customer_id=? ORDER BY id DESC", (cid,))
    bookings = q("SELECT * FROM bookings WHERE customer_id=? ORDER BY slot_start DESC", (cid,))
    return render_template("admin/customer_detail.html", c=c, wos=wos, bookings=bookings)


@app.route("/admin/kunder/<int:cid>/export")
@login_required
def customer_export(cid):
    c = q("SELECT * FROM customers WHERE id=?", (cid,), one=True) or abort(404)
    data = {"kund": dict(c),
            "bokningar": [dict(r) for r in q("SELECT category, item, description, slot_start, status, created_at "
                                              "FROM bookings WHERE customer_id=?", (cid,))],
            "arbetsordrar": []}
    for w in q("SELECT * FROM work_orders WHERE customer_id=?", (cid,)):
        d = {k: w[k] for k in ("number", "category", "item", "serial", "problem", "diagnosis", "status",
                               "created_at", "closed_at")}
        d["handelser"] = [dict(e) for e in q("SELECT created_at, text FROM wo_events WHERE work_order_id=? AND public=1",
                                             (w["id"],))]
        data["arbetsordrar"].append(d)
    return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=kund-{cid}.json"})


def anonymize_customer(db, cid):
    db.execute("UPDATE customers SET name='Anonymiserad kund', email=NULL, phone=NULL, notes=NULL, anonymized=1 "
               "WHERE id=?", (cid,))
    db.execute("UPDATE bookings SET description='(raderad)' WHERE customer_id=?", (cid,))
    db.execute("UPDATE work_orders SET serial=NULL WHERE customer_id=?", (cid,))


@app.route("/admin/kunder/<int:cid>/anonymisera", methods=["POST"])
@login_required
def customer_anonymize(cid):
    open_wo = q(f"SELECT COUNT(*) c FROM work_orders WHERE customer_id=? AND status IN ({','.join('?' * len(WO_OPEN))})",
                (cid, *WO_OPEN), one=True)["c"]
    if open_wo:
        flash("Kunden har pågående arbetsordrar. Avsluta dem först.", "error")
        return redirect(url_for("customer_detail", cid=cid))
    db = get_db()
    with db:
        anonymize_customer(db, cid)
    flash("Kunden är anonymiserad. Arbetsordrarna finns kvar för statistik, utan personuppgifter.")
    return redirect(url_for("customers_list"))


@app.route("/admin/gallra", methods=["POST"])
@login_required
def purge_old():
    months = max(1, setting_int("retention_months", 24))
    cutoff = (now() - timedelta(days=30 * months)).isoformat(sep=" ")
    db = get_db()
    rows = db.execute(f"SELECT id FROM customers WHERE anonymized=0 AND COALESCE(last_activity, created_at) < ? "
                      f"AND id NOT IN (SELECT customer_id FROM work_orders WHERE customer_id IS NOT NULL AND status IN "
                      f"({','.join('?' * len(WO_OPEN))}))", (cutoff, *WO_OPEN)).fetchall()
    with db:
        for r in rows:
            anonymize_customer(db, r["id"])
    flash(f"{len(rows)} kund(er) utan aktivitet på {months} månader har anonymiserats.")
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# Admin: lager och platser
# ---------------------------------------------------------------------------
@app.route("/admin/lager")
@login_required
def parts_list():
    search = clean(request.args.get("q"), 100)
    cat = clean(request.args.get("kategori"), 60)
    only_low = request.args.get("lagt") == "1"
    sql = ("SELECT p.*, l.name loc FROM parts p LEFT JOIN locations l ON l.id=p.location_id WHERE p.active=1")
    args = []
    if search:
        sql += " AND (p.name LIKE ? OR p.sku LIKE ? OR p.supplier LIKE ? OR p.notes LIKE ?)"
        args += [f"%{search}%"] * 4
    if cat:
        sql += " AND p.category=?"
        args.append(cat)
    if only_low:
        sql += " AND p.min_qty > 0 AND p.qty <= p.min_qty"
    rows = q(sql + " ORDER BY p.category, p.name LIMIT 1000", args)
    cats = [r["category"] for r in q("SELECT DISTINCT category FROM parts WHERE category IS NOT NULL AND category<>'' "
                                     "AND active=1 ORDER BY category")]
    value = q("SELECT COALESCE(SUM(qty*unit_cost),0) v FROM parts WHERE active=1 AND qty>0", one=True)["v"]
    return render_template("admin/parts.html", rows=rows, search=search, cat=cat, cats=cats, only_low=only_low,
                           value=value)


def _part_form_values(f):
    return (clean(f.get("sku"), 60) or None, clean(f.get("name"), 200), clean(f.get("category"), 60),
            to_int(f.get("location_id")) or None, to_float(f.get("min_qty")), clean(f.get("unit"), 10) or "st",
            to_float(f.get("unit_cost")), to_float(f.get("unit_price")), clean(f.get("supplier"), 120),
            clean(f.get("supplier_url"), 500), clean(f.get("notes"), 2000))


@app.route("/admin/lager/ny", methods=["GET", "POST"])
@login_required
def part_new():
    if request.method == "POST":
        vals = _part_form_values(request.form)
        if not vals[1]:
            flash("Namn krävs.", "error")
        else:
            try:
                db = get_db()
                with db:
                    pid = db.execute("INSERT INTO parts(sku, name, category, location_id, min_qty, unit, unit_cost, "
                                     "unit_price, supplier, supplier_url, notes, qty, tag_code, created_at) "
                                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (*vals, to_float(request.form.get("qty")), new_tag(), now_s())).lastrowid
                    if to_float(request.form.get("qty")):
                        db.execute("INSERT INTO stock_moves(part_id, delta, reason, created_at) VALUES (?,?,?,?)",
                                   (pid, to_float(request.form.get("qty")), "Ingående saldo", now_s()))
                flash("Delen är tillagd.")
                return redirect(url_for("part_detail", pid=pid) if request.form.get("next") != "ny"
                                else url_for("part_new", plats=vals[3] or "", kategori=vals[2]))
            except sqlite3.IntegrityError:
                flash("Artikelnumret används redan.", "error")
    return render_template("admin/part_form.html", p=None, locations=q("SELECT * FROM locations ORDER BY name"),
                           pre_loc=to_int(request.args.get("plats")), pre_cat=request.args.get("kategori", ""),
                           cats=[r["category"] for r in q("SELECT DISTINCT category FROM parts WHERE category<>'' ORDER BY 1")])


@app.route("/admin/lager/<int:pid>", methods=["GET", "POST"])
@login_required
def part_detail(pid):
    p = q("SELECT p.*, l.name loc FROM parts p LEFT JOIN locations l ON l.id=p.location_id WHERE p.id=?",
          (pid,), one=True) or abort(404)
    if request.method == "POST":
        vals = _part_form_values(request.form)
        if not vals[1]:
            flash("Namn krävs.", "error")
        else:
            try:
                x("UPDATE parts SET sku=?, name=?, category=?, location_id=?, min_qty=?, unit=?, unit_cost=?, "
                  "unit_price=?, supplier=?, supplier_url=?, notes=? WHERE id=?", (*vals, pid))
                flash("Sparat.")
            except sqlite3.IntegrityError:
                flash("Artikelnumret används redan.", "error")
        return redirect(url_for("part_detail", pid=pid))
    moves = q("SELECT m.*, w.number FROM stock_moves m LEFT JOIN work_orders w ON w.id=m.work_order_id "
              "WHERE m.part_id=? ORDER BY m.id DESC LIMIT 50", (pid,))
    return render_template("admin/part_detail.html", p=p, moves=moves,
                           locations=q("SELECT * FROM locations ORDER BY name"),
                           cats=[r["category"] for r in q("SELECT DISTINCT category FROM parts WHERE category<>'' ORDER BY 1")])


@app.route("/admin/lager/<int:pid>/justera", methods=["POST"])
@login_required
def part_adjust(pid):
    q("SELECT id FROM parts WHERE id=?", (pid,), one=True) or abort(404)
    mode = request.form.get("mode", "delta")
    amount = to_float(request.form.get("amount"))
    reason = clean(request.form.get("reason"), 200)
    db = get_db()
    with db:
        current = db.execute("SELECT qty FROM parts WHERE id=?", (pid,)).fetchone()["qty"]
        delta = amount - current if mode == "set" else amount
        if delta:
            db.execute("UPDATE parts SET qty = qty + ? WHERE id=?", (delta, pid))
            db.execute("INSERT INTO stock_moves(part_id, delta, reason, created_at) VALUES (?,?,?,?)",
                       (pid, delta, reason or ("Inventering" if mode == "set" else "Manuell justering"), now_s()))
    flash("Lagersaldot är uppdaterat.")
    return redirect(request.referrer or url_for("part_detail", pid=pid))


@app.route("/admin/lager/<int:pid>/arkivera", methods=["POST"])
@login_required
def part_archive(pid):
    x("UPDATE parts SET active=0 WHERE id=?", (pid,))
    flash("Delen är arkiverad (historiken finns kvar).")
    return redirect(url_for("parts_list"))


@app.route("/admin/lager.csv")
@login_required
def parts_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Artikelnr", "Namn", "Kategori", "Plats", "Saldo", "Enhet", "Beställningspunkt", "Inköpspris",
                "Pris", "Leverantör", "Länk"])
    for p in q("SELECT p.*, l.name loc FROM parts p LEFT JOIN locations l ON l.id=p.location_id WHERE p.active=1 "
               "ORDER BY p.category, p.name"):
        w.writerow([p["sku"] or "", p["name"], p["category"] or "", p["loc"] or "", fmt_qty(p["qty"]), p["unit"],
                    fmt_qty(p["min_qty"]), str(p["unit_cost"]).replace(".", ","), str(p["unit_price"]).replace(".", ","),
                    p["supplier"] or "", p["supplier_url"] or ""])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lager.csv"})


@app.route("/admin/inkop")
@login_required
def shopping_list():
    rows = q("SELECT p.*, l.name loc FROM parts p LEFT JOIN locations l ON l.id=p.location_id "
             "WHERE p.active=1 AND p.min_qty > 0 AND p.qty <= p.min_qty ORDER BY p.supplier, p.name")
    return render_template("admin/shopping.html", rows=rows)


@app.route("/admin/platser", methods=["GET", "POST"])
@login_required
def locations_list():
    if request.method == "POST":
        name = clean(request.form.get("name"), 100)
        if name:
            try:
                lid = x("INSERT INTO locations(name, description, tag_code) VALUES (?,?,?)",
                        (name, clean(request.form.get("description"), 500), new_tag()))
                flash(f"Platsen {name} är skapad.")
                return redirect(url_for("location_detail", lid=lid))
            except sqlite3.IntegrityError:
                flash("Det finns redan en plats med det namnet.", "error")
        return redirect(url_for("locations_list"))
    rows = q("SELECT l.*, (SELECT COUNT(*) FROM parts p WHERE p.location_id=l.id AND p.active=1) n, "
             "(SELECT COUNT(*) FROM work_orders w WHERE w.location_id=l.id AND w.status IN "
             f"({','.join('?' * len(WO_OPEN))})) nwo FROM locations l ORDER BY l.name", WO_OPEN)
    return render_template("admin/locations.html", rows=rows)


@app.route("/admin/platser/<int:lid>", methods=["GET", "POST"])
@login_required
def location_detail(lid):
    loc = q("SELECT * FROM locations WHERE id=?", (lid,), one=True) or abort(404)
    if request.method == "POST":
        if request.form.get("action") == "radera":
            x("DELETE FROM locations WHERE id=?", (lid,))
            flash("Platsen är borttagen. Delarna ligger nu utan plats.")
            return redirect(url_for("locations_list"))
        try:
            x("UPDATE locations SET name=?, description=? WHERE id=?",
              (clean(request.form.get("name"), 100) or loc["name"], clean(request.form.get("description"), 500), lid))
            flash("Sparat.")
        except sqlite3.IntegrityError:
            flash("Det finns redan en plats med det namnet.", "error")
        return redirect(url_for("location_detail", lid=lid))
    parts = q("SELECT * FROM parts WHERE location_id=? AND active=1 ORDER BY name", (lid,))
    wos = q(f"SELECT w.*, c.name customer_name FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id "
            f"WHERE w.location_id=? AND w.status IN ({','.join('?' * len(WO_OPEN))})", (lid, *WO_OPEN))
    return render_template("admin/location_detail.html", loc=loc, parts=parts, wos=wos)


@app.route("/admin/etiketter")
@login_required
def labels():
    kind = request.args.get("typ", "platser")
    if kind == "delar":
        items = [(p["name"], p["sku"] or "", p["tag_code"]) for p in
                 q("SELECT * FROM parts WHERE active=1 ORDER BY category, name")]
    elif kind == "ao":
        items = [(w["number"], w["item"] or "", w["tag_code"]) for w in
                 q(f"SELECT * FROM work_orders WHERE status IN ({','.join('?' * len(WO_OPEN))}) ORDER BY id", WO_OPEN)]
    else:
        items = [(l["name"], l["description"] or "", l["tag_code"]) for l in q("SELECT * FROM locations ORDER BY name")]
    return render_template("admin/labels.html", items=items, kind=kind)


# ---------------------------------------------------------------------------
# Admin: inställningar och backup
# ---------------------------------------------------------------------------
@app.route("/admin/installningar", methods=["GET", "POST"])
@login_required
def settings_page():
    editable = [k for k in DEFAULT_SETTINGS if k != "calendar_token"]
    if request.method == "POST":
        db = get_db()
        with db:
            for k in editable:
                if k == "booking_enabled":
                    val = "1" if request.form.get(k) else "0"
                elif k == "open_days":
                    val = ",".join(d for d in request.form.getlist(k) if d.isdigit())
                else:
                    val = clean(request.form.get(k), 3000)
                    if k == "public_url":
                        val = val.rstrip("/")
                db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, val))
            if request.form.get("new_calendar_token"):
                db.execute("UPDATE settings SET value=? WHERE key='calendar_token'", (secrets.token_urlsafe(24),))
        flash("Inställningarna är sparade.")
        return redirect(url_for("settings_page"))
    backups = sorted(os.listdir(BACKUP_DIR), reverse=True)[:10] if os.path.isdir(BACKUP_DIR) else []
    return render_template("admin/settings.html", mail=mail_enabled(), backups=backups,
                           days=list(enumerate(SV_DAYS, start=1)))


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"verkstad-{now():%Y%m%d-%H%M%S}.db")
    src = connect()
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    keep = int(os.environ.get("BACKUP_KEEP", "14"))
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for old in files[:-keep]:
        os.remove(os.path.join(BACKUP_DIR, old))
    return target


@app.route("/admin/backup", methods=["POST"])
@login_required
def backup_now():
    path = make_backup()
    flash(f"Backup skapad: {os.path.basename(path)}")
    return redirect(url_for("settings_page"))


@app.route("/admin/backup/<name>")
@login_required
def backup_download(name):
    if "/" in name or not name.endswith(".db") or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        abort(404)
    with open(os.path.join(BACKUP_DIR, name), "rb") as f:
        data = f.read()
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def _backup_loop():
    while True:
        try:
            files = sorted(os.listdir(BACKUP_DIR)) if os.path.isdir(BACKUP_DIR) else []
            latest = files[-1] if files else ""
            if not latest.startswith(f"verkstad-{now():%Y%m%d}"):
                log.info("Daglig backup: %s", make_backup())
        except Exception:  # noqa: BLE001
            log.exception("Backup misslyckades")
        time.sleep(3600)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(429)
def http_error(err):
    return render_template("public/error.html", err=err), err.code


# ---------------------------------------------------------------------------
init_db()
if os.environ.get("BACKUP_ENABLED", "1") == "1" and not app.config.get("TESTING"):
    threading.Thread(target=_backup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
