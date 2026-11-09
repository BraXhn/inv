"""Ekonomi: kundfakturor och kvitton (utgående) samt leverantörsfakturor och kvitton (inkommande).

Regler som koden följer:
- Utgående dokument får ett unikt löpnummer och låses när de skapas (raderna och kund-/säljaruppgifterna
  sparas som kopia). Fel rättas med en kreditfaktura, aldrig genom att ändra.
- Moms visas bara om verksamheten är momsregistrerad (Inställningar → Företag och fakturor).
- Underlagen ska sparas i 7 år (bokföringslagen) och undantas därför från GDPR-gallringen.
- Det här är inte ett bokföringsprogram: exportera till redovisningskonsult/Fortnox via CSV och zip.
"""
import csv
import io
import json
import os
import secrets
import uuid
import zipfile
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, redirect, render_template, request, send_file, url_for

from app import (DATA_DIR, app, base_url, clean, connect, fmt_dt, fmt_money, get_db, log, login_required,
                 mail_customer, mail_enabled, now, now_s, q, settings, setting_int, to_float, to_int, wo_totals, x)

ATTACH_DIR = os.path.join(DATA_DIR, "bilagor")
PAYMENT_METHODS = [("swish", "Swish"), ("kort", "Kort"), ("kontant", "Kontant"), ("bankgiro", "Bankgiro"),
                   ("overforing", "Banköverföring"), ("annat", "Annat")]
PAYMENT_LABEL = dict(PAYMENT_METHODS)
EXPENSE_CATEGORIES = ["Reservdelar", "Förbrukningsmaterial", "Verktyg", "Kemikalier", "Frakt", "Lokal och el",
                      "Programvara", "Övrigt"]
KIND_LABEL = {"faktura": "Faktura", "kvitto": "Kvitto", "kredit": "Kreditfaktura"}
# Filtyper som får laddas upp, känns igen på innehållet (inte bara filnamnet)
MAGIC = [(b"%PDF", "pdf", "application/pdf"), (b"\x89PNG", "png", "image/png"),
         (b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"RIFF", "webp", "image/webp")]

FINANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY, number INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL,
    work_order_id INTEGER, customer_id INTEGER,
    cust_name TEXT, cust_address TEXT, cust_email TEXT, cust_phone TEXT, cust_org TEXT,
    issue_date TEXT NOT NULL, due_date TEXT, paid_at TEXT, payment_method TEXT,
    status TEXT NOT NULL, credit_of INTEGER, net_total REAL NOT NULL, vat_total REAL NOT NULL, total REAL NOT NULL,
    vat_registered INTEGER NOT NULL, seller_json TEXT NOT NULL, note TEXT, token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL, qty REAL NOT NULL, unit_price REAL NOT NULL, vat_rate REAL NOT NULL,
    net REAL NOT NULL, vat REAL NOT NULL, total REAL NOT NULL);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, supplier TEXT NOT NULL, ref TEXT, date TEXT NOT NULL,
    due_date TEXT, total REAL NOT NULL, vat REAL NOT NULL DEFAULT 0, category TEXT, status TEXT NOT NULL,
    paid_at TEXT, payment_method TEXT, work_order_id INTEGER, note TEXT,
    attachment TEXT, attachment_name TEXT, attachment_mime TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_invoices_wo ON invoices(work_order_id);
CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(date);
"""


def init_finance():
    os.makedirs(ATTACH_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(FINANCE_SCHEMA)
    conn.commit()
    conn.close()


init_finance()


# ---------------------------------------------------------------------------
# Beräkningar
# ---------------------------------------------------------------------------
def r2(v):
    return round(float(v) + 0.0, 2)


def vat_rate():
    s = settings()
    return max(0.0, to_float(s.get("vat_rate"), 25)) / 100 if s.get("vat_registered") == "1" else 0.0


def line_amounts(qty, unit_price, rate, incl):
    """Belopp för en rad. incl = priserna i systemet är inklusive moms (konsumentpriser)."""
    gross_or_net = qty * unit_price
    if rate <= 0:
        return r2(gross_or_net), 0.0, r2(gross_or_net)
    if incl:
        total = r2(gross_or_net)
        vat = r2(total * rate / (1 + rate))
        return r2(total - vat), vat, total
    net = r2(gross_or_net)
    vat = r2(net * rate)
    return net, vat, r2(net + vat)


def seller_snapshot():
    s = settings()
    return {k: s.get(k, "") for k in ("company_name", "business_name", "org_number", "vat_registered", "vat_number",
                                       "f_skatt", "address", "contact_phone", "contact_email", "bankgiro", "plusgiro",
                                       "swish_number", "invoice_note")}


def next_number(db):
    row = db.execute("SELECT MAX(number) FROM invoices").fetchone()[0]
    return max(row + 1 if row else 0, setting_int("invoice_start", 1001))


def wo_invoice_lines(wo):
    lines = [(l["description"], l["qty"], l["unit_price"]) for l in
             q("SELECT * FROM wo_lines WHERE work_order_id=? ORDER BY id", (wo["id"],))]
    hours = (wo["labor_minutes"] or 0) / 60
    if hours > 0 and (wo["labor_rate"] or 0) > 0:
        lines.append(("Arbete, timmar", round(hours, 2), wo["labor_rate"]))
    return lines


def create_invoice(kind, lines, customer, work_order_id=None, payment_method=None, note="", credit_of=None,
                   issue=None):
    """Skapar och låser ett utgående dokument. lines = [(beskrivning, antal, à-pris)]."""
    s = settings()
    rate = vat_rate()
    incl = s.get("prices_include_vat", "1") == "1"
    issue = issue or now().date()
    computed = []
    for desc, qty, price in lines:
        net, vat, total = line_amounts(to_float(qty), to_float(price), rate, incl)
        computed.append((desc, to_float(qty), to_float(price), rate * 100, net, vat, total))
    net_t, vat_t, tot_t = (r2(sum(c[i] for c in computed)) for i in (4, 5, 6))
    paid = kind == "kvitto"
    due = None if kind != "faktura" else (issue + timedelta(days=max(0, setting_int("payment_terms_days", 30)))).isoformat()
    db = get_db()
    for _ in range(5):
        try:
            with db:
                number = next_number(db)
                iid = db.execute(
                    "INSERT INTO invoices(number, kind, work_order_id, customer_id, cust_name, cust_address, cust_email, "
                    "cust_phone, cust_org, issue_date, due_date, paid_at, payment_method, status, credit_of, net_total, "
                    "vat_total, total, vat_registered, seller_json, note, token, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (number, kind, work_order_id, customer.get("id"), customer.get("name"), customer.get("address"),
                     customer.get("email"), customer.get("phone"), customer.get("org_number"), issue.isoformat(), due,
                     now_s() if paid else None, payment_method if paid else None,
                     "betald" if paid else ("krediterad" if kind == "kredit" else "obetald"), credit_of,
                     net_t, vat_t, tot_t, 1 if rate > 0 else 0, json.dumps(seller_snapshot(), ensure_ascii=False),
                     note, secrets.token_urlsafe(24), now_s())).lastrowid
                db.executemany("INSERT INTO invoice_lines(invoice_id, description, qty, unit_price, vat_rate, net, vat, total) "
                               "VALUES (?,?,?,?,?,?,?,?)", [(iid, *c) for c in computed])
            return iid
        except Exception as exc:  # noqa: BLE001 – krock på löpnummer: försök igen
            if "UNIQUE" not in str(exc):
                raise
    raise RuntimeError("Kunde inte ta fram ett fakturanummer")


def load_invoice(iid=None, token=None):
    row = q("SELECT * FROM invoices WHERE " + ("id=?" if iid else "token=?"), (iid or token,), one=True)
    if not row:
        abort(404)
    lines = q("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (row["id"],))
    seller = json.loads(row["seller_json"] or "{}")
    # Momsöversikt per sats (krav på faktura för momsregistrerade)
    vat_summary = {}
    for l in lines:
        if l["vat"]:
            v = vat_summary.setdefault(l["vat_rate"], [0.0, 0.0])
            v[0] += l["net"]
            v[1] += l["vat"]
    overdue = row["status"] == "obetald" and row["due_date"] and row["due_date"] < now().date().isoformat()
    return row, lines, seller, sorted(vat_summary.items()), overdue


def invoice_url(row):
    return f"{base_url()}{url_for('public_invoice', token=row['token'])}"


def sniff_upload(data):
    for magic, ext, mime in MAGIC:
        if data.startswith(magic) and (ext != "webp" or data[8:12] == b"WEBP"):
            return ext, mime
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "heic", "image/heic"
    return None, None


def save_upload(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None, None, None
    data = file_storage.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        return None, None, None, "Filen är för stor (max 15 MB)."
    ext, mime = sniff_upload(data)
    if not ext:
        return None, None, None, "Bara PDF eller bild (JPG, PNG, HEIC, WebP) kan bifogas."
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(ATTACH_DIR, name), "wb") as f:
        f.write(data)
    return name, clean(os.path.basename(file_storage.filename), 200), mime, None


app.jinja_env.globals.update(PAYMENT_METHODS=PAYMENT_METHODS, PAYMENT_LABEL=PAYMENT_LABEL, KIND_LABEL=KIND_LABEL)


# ---------------------------------------------------------------------------
# Utgående: fakturor och kvitton
# ---------------------------------------------------------------------------
@app.route("/admin/ao/<int:wid>/faktura", methods=["POST"])
@login_required
def wo_create_invoice(wid):
    wo = q("SELECT w.*, c.name customer_name, c.email customer_email, c.phone customer_phone, c.address customer_address, "
           "c.org_number customer_org FROM work_orders w LEFT JOIN customers c ON c.id=w.customer_id WHERE w.id=?",
           (wid,), one=True) or abort(404)
    kind = request.form.get("kind", "faktura")
    if kind not in ("faktura", "kvitto"):
        abort(400)
    active = q("SELECT number FROM invoices WHERE work_order_id=? AND kind IN ('faktura','kvitto') AND status<>'krediterad'",
               (wid,), one=True)
    if active and not request.form.get("again"):
        flash(f"Ärendet har redan nr {active['number']}. Kreditera den först om något ska ändras.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#ekonomi")
    lines = wo_invoice_lines(wo)
    if not lines:
        flash("Ärendet har inga rader eller arbetstid att fakturera.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#ekonomi")
    method = request.form.get("payment_method") or None
    if kind == "kvitto" and method not in PAYMENT_LABEL:
        flash("Välj hur kunden betalade.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#ekonomi")
    address = clean(request.form.get("address"), 300) or (wo["customer_address"] or "")
    org = clean(request.form.get("org_number"), 30) or (wo["customer_org"] or "")
    if kind == "faktura" and not address:
        flash("En faktura behöver kundens adress.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#ekonomi")
    if wo["customer_id"] and (address != (wo["customer_address"] or "") or org != (wo["customer_org"] or "")):
        x("UPDATE customers SET address=?, org_number=? WHERE id=?", (address, org, wo["customer_id"]))
    customer = {"id": wo["customer_id"], "name": wo["customer_name"], "address": address, "email": wo["customer_email"],
                "phone": wo["customer_phone"], "org_number": org}
    iid = create_invoice(kind, lines, customer, work_order_id=wid, payment_method=method,
                         note=f"Ärende {wo['number']}: {wo['item']}")
    row = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    if request.form.get("send") and wo["customer_email"]:
        send_invoice_mail(row)
    flash(f"{KIND_LABEL[kind]} nr {row['number']} är skapad" + (" och skickad." if request.form.get("send") and wo["customer_email"] and mail_enabled() else "."))
    return redirect(url_for("invoice_admin", iid=iid))


def send_invoice_mail(row):
    if not row["cust_email"]:
        return False
    s = settings()
    kind = KIND_LABEL[row["kind"]]
    details = [("Nummer", str(row["number"])), ("Datum", row["issue_date"]), ("Att betala" if row["status"] == "obetald" else "Belopp", fmt_money(row["total"]))]
    paras = [f"Hej {row['cust_name']}!"]
    if row["kind"] == "faktura":
        details.append(("Förfallodag", row["due_date"]))
        pay = ", ".join(v for v in (f"Bankgiro {s['bankgiro']}" if s.get("bankgiro") else "",
                                    f"Swish {s['swish_number']}" if s.get("swish_number") else "") if v)
        paras.append(f"Här kommer din faktura. Betala senast {row['due_date']}" + (f" till {pay}" if pay else "")
                     + f" och ange nummer {row['number']} som referens.")
    elif row["kind"] == "kvitto":
        paras.append("Tack för att du valde oss! Här är ditt kvitto.")
    else:
        paras.append("Här är en kreditfaktura som ersätter en tidigare faktura.")
    mail_customer(row["cust_email"], f"{kind} {row['number']} från {s['business_name']}", f"ekonomi_{row['kind']}",
                  heading=f"{kind} {row['number']}", paragraphs=paras, details=details,
                  button=("Visa och skriv ut", invoice_url(row)))
    return True


@app.route("/admin/faktura/<int:iid>")
@login_required
def invoice_admin(iid):
    row, lines, seller, vat_summary, overdue = load_invoice(iid=iid)
    credits = q("SELECT * FROM invoices WHERE credit_of=?", (iid,))
    original = q("SELECT * FROM invoices WHERE id=?", (row["credit_of"],), one=True) if row["credit_of"] else None
    return render_template("admin/invoice.html", inv=row, lines=lines, seller=seller, vat_summary=vat_summary,
                           overdue=overdue, credits=credits, original=original, public_url=invoice_url(row),
                           mail=mail_enabled())


@app.route("/f/<token>")
def public_invoice(token):
    row, lines, seller, vat_summary, overdue = load_invoice(token=token)
    return render_template("public/invoice.html", inv=row, lines=lines, seller=seller, vat_summary=vat_summary,
                           overdue=overdue)


@app.route("/admin/faktura/<int:iid>/betald", methods=["POST"])
@login_required
def invoice_paid(iid):
    row = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True) or abort(404)
    if row["status"] != "obetald":
        abort(400)
    method = request.form.get("payment_method")
    if method not in PAYMENT_LABEL:
        method = "bankgiro"
    try:
        paid = date.fromisoformat(request.form.get("paid_date", "")[:10]).isoformat() + " 12:00:00"
    except ValueError:
        paid = now_s()
    x("UPDATE invoices SET status='betald', paid_at=?, payment_method=? WHERE id=?", (paid, method, iid))
    flash(f"Nr {row['number']} är markerad som betald.")
    return redirect(request.referrer or url_for("invoice_admin", iid=iid))


@app.route("/admin/faktura/<int:iid>/kreditera", methods=["POST"])
@login_required
def invoice_credit(iid):
    row, lines, *_ = load_invoice(iid=iid)
    if row["kind"] == "kredit" or row["status"] == "krediterad":
        abort(400)
    customer = {"id": row["customer_id"], "name": row["cust_name"], "address": row["cust_address"],
                "email": row["cust_email"], "phone": row["cust_phone"], "org_number": row["cust_org"]}
    # Kreditera exakt det som fakturerades: negativa antal, samma à-priser
    cid = create_invoice("kredit", [(l["description"], -l["qty"], l["unit_price"]) for l in lines], customer,
                         work_order_id=row["work_order_id"], credit_of=iid,
                         note=f"Kreditering av {KIND_LABEL[row['kind']].lower()} nr {row['number']}. "
                              + clean(request.form.get("reason"), 300))
    x("UPDATE invoices SET status='krediterad' WHERE id=?", (iid,))
    flash(f"Kreditfaktura skapad. Nr {row['number']} är krediterad.")
    return redirect(url_for("invoice_admin", iid=cid))


@app.route("/admin/faktura/<int:iid>/skicka", methods=["POST"])
@login_required
def invoice_send(iid):
    row = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True) or abort(404)
    if not mail_enabled():
        flash("Ställ in e-post först (Inställningar → E-post).", "error")
    elif send_invoice_mail(row):
        flash(f"Nr {row['number']} är skickad till {row['cust_email']}.")
    else:
        flash("Kunden saknar e-postadress.", "error")
    return redirect(url_for("invoice_admin", iid=iid))


@app.route("/admin/fakturor")
@login_required
def invoices_list():
    st = request.args.get("status", "")
    sql, args = "SELECT * FROM invoices WHERE 1=1", []
    if st == "obetald":
        sql += " AND status='obetald'"
    elif st == "forfallen":
        sql += " AND status='obetald' AND due_date < ?"
        args.append(now().date().isoformat())
    elif st in ("betald", "krediterad"):
        sql += " AND status=?"
        args.append(st)
    term = clean(request.args.get("q"), 80)
    if term:
        sql += " AND (cust_name LIKE ? OR CAST(number AS TEXT)=? OR note LIKE ?)"
        args += [f"%{term}%", term, f"%{term}%"]
    rows = q(sql + " ORDER BY number DESC LIMIT 500", args)
    return render_template("admin/invoices.html", rows=rows, st=st, term=term, today=now().date().isoformat())


# ---------------------------------------------------------------------------
# Inkommande: leverantörsfakturor och kvitton
# ---------------------------------------------------------------------------
def _expense_form_values(f):
    kind = f.get("kind") if f.get("kind") in ("faktura", "kvitto") else "kvitto"
    try:
        d = date.fromisoformat(f.get("date", "")[:10]).isoformat()
    except ValueError:
        d = now().date().isoformat()
    try:
        due = date.fromisoformat(f.get("due_date", "")[:10]).isoformat() if f.get("due_date") else None
    except ValueError:
        due = None
    return dict(kind=kind, supplier=clean(f.get("supplier"), 120), ref=clean(f.get("ref"), 60), date=d, due_date=due,
                total=r2(to_float(f.get("total"))), vat=r2(to_float(f.get("vat"))), category=clean(f.get("category"), 60),
                work_order_id=to_int(f.get("work_order_id")) or None, note=clean(f.get("note"), 1000))


@app.route("/admin/utgifter")
@login_required
def expenses_list():
    st = request.args.get("status", "")
    month = request.args.get("m", "")
    sql, args = ("SELECT e.*, w.number wo_number FROM expenses e LEFT JOIN work_orders w ON w.id=e.work_order_id "
                 "WHERE 1=1"), []
    if st == "obetald":
        sql += " AND e.status='obetald'"
    if len(month) == 7:
        sql += " AND substr(e.date,1,7)=?"
        args.append(month)
    rows = q(sql + " ORDER BY e.date DESC, e.id DESC LIMIT 500", args)
    return render_template("admin/expenses.html", rows=rows, st=st, month=month, today=now().date().isoformat())


@app.route("/admin/utgifter/ny", methods=["GET", "POST"])
@app.route("/admin/utgifter/<int:eid>", methods=["GET", "POST"])
@login_required
def expense_form(eid=None):
    row = q("SELECT * FROM expenses WHERE id=?", (eid,), one=True) if eid else None
    if eid and not row:
        abort(404)
    if request.method == "POST":
        if request.form.get("action") == "betald" and row:
            x("UPDATE expenses SET status='betald', paid_at=?, payment_method=? WHERE id=?",
              (now_s(), request.form.get("payment_method") or "bankgiro", eid))
            flash("Markerad som betald.")
            return redirect(request.referrer or url_for("expenses_list"))
        if request.form.get("action") == "radera" and row:
            if row["attachment"]:
                try:
                    os.remove(os.path.join(ATTACH_DIR, row["attachment"]))
                except OSError:
                    pass
            x("DELETE FROM expenses WHERE id=?", (eid,))
            flash("Utgiften är borttagen.")
            return redirect(url_for("expenses_list"))
        v = _expense_form_values(request.form)
        if not v["supplier"] or v["total"] <= 0:
            flash("Ange leverantör och belopp.", "error")
            return redirect(request.url)
        if v["vat"] > v["total"]:
            flash("Momsen kan inte vara större än totalbeloppet.", "error")
            return redirect(request.url)
        att, att_name, att_mime, err = save_upload(request.files.get("attachment"))
        if err:
            flash(err, "error")
            return redirect(request.url)
        status = "betald" if (v["kind"] == "kvitto" or request.form.get("paid")) else "obetald"
        db = get_db()
        with db:
            if row:
                db.execute("UPDATE expenses SET kind=?, supplier=?, ref=?, date=?, due_date=?, total=?, vat=?, category=?, "
                           "work_order_id=?, note=? WHERE id=?",
                           (v["kind"], v["supplier"], v["ref"], v["date"], v["due_date"], v["total"], v["vat"],
                            v["category"], v["work_order_id"], v["note"], eid))
                if att:
                    if row["attachment"]:
                        try:
                            os.remove(os.path.join(ATTACH_DIR, row["attachment"]))
                        except OSError:
                            pass
                    db.execute("UPDATE expenses SET attachment=?, attachment_name=?, attachment_mime=? WHERE id=?",
                               (att, att_name, att_mime, eid))
                new_id = eid
            else:
                new_id = db.execute(
                    "INSERT INTO expenses(kind, supplier, ref, date, due_date, total, vat, category, status, paid_at, "
                    "payment_method, work_order_id, note, attachment, attachment_name, attachment_mime, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v["kind"], v["supplier"], v["ref"], v["date"], v["due_date"], v["total"], v["vat"], v["category"],
                     status, now_s() if status == "betald" else None,
                     request.form.get("payment_method") if status == "betald" else None, v["work_order_id"], v["note"],
                     att, att_name, att_mime, now_s())).lastrowid
                # Inleverans: lägg in köpta delar i lagret direkt
                pid, qty = to_int(request.form.get("part_id")), to_float(request.form.get("part_qty"))
                if pid and qty > 0:
                    db.execute("UPDATE parts SET qty = qty + ? WHERE id=?", (qty, pid))
                    db.execute("INSERT INTO stock_moves(part_id, delta, reason, created_at) VALUES (?,?,?,?)",
                               (pid, qty, f"Inleverans {v['supplier']} {v['ref']}".strip(), now_s()))
                    if request.form.get("update_cost"):
                        db.execute("UPDATE parts SET unit_cost=? WHERE id=?", (r2(v["total"] / qty), pid))
        flash("Utgiften är sparad." + (" Lagret är uppdaterat." if not row and to_int(request.form.get("part_id")) else ""))
        return redirect(url_for("expenses_list") if not request.form.get("next") else url_for("expense_form"))
    wos = q("SELECT id, number, item FROM work_orders ORDER BY id DESC LIMIT 200")
    parts = q("SELECT id, name, sku, qty, unit FROM parts WHERE active=1 ORDER BY name")
    suppliers = [r["supplier"] for r in q("SELECT DISTINCT supplier FROM expenses ORDER BY supplier LIMIT 200")]
    return render_template("admin/expense_form.html", e=row, wos=wos, parts=parts, categories=EXPENSE_CATEGORIES,
                           today=now().date().isoformat(), suppliers=suppliers)


@app.route("/admin/utgifter/<int:eid>/bilaga")
@login_required
def expense_attachment(eid):
    row = q("SELECT * FROM expenses WHERE id=?", (eid,), one=True) or abort(404)
    if not row["attachment"]:
        abort(404)
    path = os.path.join(ATTACH_DIR, os.path.basename(row["attachment"]))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=row["attachment_mime"] or "application/octet-stream",
                     as_attachment=request.args.get("ladda") == "1",
                     download_name=row["attachment_name"] or os.path.basename(path))


# ---------------------------------------------------------------------------
# Översikt och export
# ---------------------------------------------------------------------------
def _month_bounds(m):
    try:
        start = datetime.strptime(m + "-01", "%Y-%m-%d").date()
    except (ValueError, TypeError):
        start = now().date().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


@app.route("/admin/ekonomi")
@login_required
def finance_overview():
    start, end = _month_bounds(request.args.get("m", ""))
    a, b = start.isoformat(), end.isoformat()
    today = now().date().isoformat()
    inv = q("SELECT * FROM invoices WHERE issue_date >= ? AND issue_date < ?", (a, b))
    billed = r2(sum(i["total"] for i in inv))
    billed_net = r2(sum(i["net_total"] for i in inv))
    out_vat = r2(sum(i["vat_total"] for i in inv))
    received = r2(sum(i["total"] for i in q("SELECT total FROM invoices WHERE status='betald' AND paid_at >= ? AND paid_at < ?",
                                             (a, b))))
    unpaid = q("SELECT * FROM invoices WHERE status='obetald' ORDER BY due_date")
    overdue = [i for i in unpaid if i["due_date"] and i["due_date"] < today]
    exp = q("SELECT * FROM expenses WHERE date >= ? AND date < ?", (a, b))
    spent = r2(sum(e["total"] for e in exp))
    in_vat = r2(sum(e["vat"] for e in exp))
    payables = q("SELECT * FROM expenses WHERE status='obetald' ORDER BY COALESCE(due_date, date)")
    by_cat = {}
    for e in exp:
        by_cat[e["category"] or "Övrigt"] = r2(by_cat.get(e["category"] or "Övrigt", 0) + e["total"])
    prev = (start - timedelta(days=1)).strftime("%Y-%m")
    nxt = end.strftime("%Y-%m")
    months = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
              "november", "december"]
    return render_template("admin/finance.html", start=start, label=f"{months[start.month - 1]} {start.year}",
                           billed=billed, billed_net=billed_net, out_vat=out_vat, received=received, unpaid=unpaid,
                           overdue=overdue, spent=spent, in_vat=in_vat, payables=payables, by_cat=sorted(by_cat.items()),
                           result=r2(billed_net - (spent - in_vat)), prev=prev, nxt=nxt, m=start.strftime("%Y-%m"),
                           recent=q("SELECT * FROM invoices ORDER BY id DESC LIMIT 8"),
                           recent_exp=q("SELECT * FROM expenses ORDER BY date DESC, id DESC LIMIT 8"),
                           unpaid_total=r2(sum(i["total"] for i in unpaid)),
                           payables_total=r2(sum(e["total"] for e in payables)), today=today,
                           vat_on=settings().get("vat_registered") == "1")


def _period():
    try:
        a = date.fromisoformat(request.args.get("fran", "")[:10])
    except ValueError:
        a = now().date().replace(month=1, day=1)
    try:
        b = date.fromisoformat(request.args.get("till", "")[:10])
    except ValueError:
        b = now().date()
    return a.isoformat(), b.isoformat()


@app.route("/admin/ekonomi/export.csv")
@login_required
def finance_export():
    a, b = _period()
    typ = request.args.get("typ", "fakturor")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    num = lambda v: f"{v:.2f}".replace(".", ",")  # noqa: E731 – svenska decimaler för Excel
    if typ == "utgifter":
        w.writerow(["Datum", "Typ", "Leverantör", "Referens", "Kategori", "Belopp inkl moms", "Moms", "Status",
                    "Betald", "Betalsätt", "Förfallodag", "Ärende", "Bilaga", "Anteckning"])
        for e in q("SELECT e.*, w.number wo FROM expenses e LEFT JOIN work_orders w ON w.id=e.work_order_id "
                   "WHERE e.date BETWEEN ? AND ? ORDER BY e.date", (a, b)):
            w.writerow([e["date"], "Leverantörsfaktura" if e["kind"] == "faktura" else "Kvitto", e["supplier"],
                        e["ref"] or "", e["category"] or "", num(e["total"]), num(e["vat"]), e["status"],
                        (e["paid_at"] or "")[:10], PAYMENT_LABEL.get(e["payment_method"], ""), e["due_date"] or "",
                        e["wo"] or "", e["attachment_name"] or "", e["note"] or ""])
    else:
        w.writerow(["Nummer", "Typ", "Datum", "Förfallodag", "Kund", "Org/personnr", "Netto", "Moms", "Totalt",
                    "Status", "Betald", "Betalsätt", "Krediterar nr", "Ärende"])
        for i in q("SELECT i.*, c.number credit_number FROM invoices i LEFT JOIN invoices c ON c.id=i.credit_of "
                   "WHERE i.issue_date BETWEEN ? AND ? ORDER BY i.number", (a, b)):
            w.writerow([i["number"], KIND_LABEL[i["kind"]], i["issue_date"], i["due_date"] or "", i["cust_name"] or "",
                        i["cust_org"] or "", num(i["net_total"]), num(i["vat_total"]), num(i["total"]), i["status"],
                        (i["paid_at"] or "")[:10], PAYMENT_LABEL.get(i["payment_method"], ""),
                        i["credit_number"] or "", i["note"] or ""])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={typ}-{a}-{b}.csv"})


@app.route("/admin/ekonomi/bilagor.zip")
@login_required
def finance_attachments_zip():
    a, b = _period()
    rows = q("SELECT * FROM expenses WHERE attachment IS NOT NULL AND date BETWEEN ? AND ? ORDER BY date", (a, b))
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for e in rows:
            path = os.path.join(ATTACH_DIR, os.path.basename(e["attachment"]))
            if os.path.isfile(path):
                safe = "".join(ch for ch in e["supplier"] if ch.isalnum() or ch in " -_")[:40].strip() or "leverantor"
                z.write(path, f"{e['date']}_{safe}_{e['id']}.{e['attachment'].rsplit('.', 1)[-1]}")
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True, download_name=f"bilagor-{a}-{b}.zip")


log.info("Ekonomi laddad (bilagor i %s)", ATTACH_DIR)
