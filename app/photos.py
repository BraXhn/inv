"""Foton på arbetsordrar: skick vid inlämning, före och efter.

Varje bild skalas ner och sparas om som JPEG. Det tar bort EXIF-data, alltså även GPS-position och
telefonmodell, och vrider bilden rätt. Kunden ser bara bilder som är markerade som synliga, och bara via
sin egen hemliga statuslänk.
"""
import io
import os
import uuid
import warnings

from flask import abort, flash, redirect, request, send_file, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from app import PHOTO_DIR, app, clean, client_ip, connect, log, login_required, now_s, q, rate_limited, x

STAGES = {"inlamning": "Vid inlämning", "fore": "Före", "efter": "Efter", "ovrigt": "Övrigt"}
MAX_SIDE, THUMB_SIDE = 1600, 360
MAX_FILE = 25 * 1024 * 1024
MAX_PER_UPLOAD = 12
Image.MAX_IMAGE_PIXELS = 60_000_000  # skydd mot "dekompressionsbomber"; en 48 MP-mobilbild ryms

PHOTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS wo_photos (
    id INTEGER PRIMARY KEY,
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    created_at TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'ovrigt',
    caption TEXT,
    public INTEGER NOT NULL DEFAULT 1,
    file TEXT NOT NULL,
    thumb TEXT NOT NULL,
    width INTEGER, height INTEGER
);
CREATE INDEX IF NOT EXISTS ix_wo_photos_wo ON wo_photos(work_order_id);
"""


def init_photos():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(PHOTO_SCHEMA)
    conn.commit()
    conn.close()


init_photos()


def process_image(data):
    """Returnerar (jpeg, miniatyr, bredd, höjd) eller kastar ValueError med ett begripligt fel."""
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        raise ValueError("HEIC-bilder kan inte läsas. På iPhone: Inställningar → Kamera → Format → Mest kompatibel.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Bara JPG, PNG eller WebP.")
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as exc:
        raise ValueError("Filen är ingen bild som går att läsa.") from exc
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    big = io.BytesIO()
    img.save(big, "JPEG", quality=84, optimize=True, progressive=True)  # inga EXIF-data följer med
    w, h = img.size
    small_img = img.copy()
    small_img.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
    small = io.BytesIO()
    small_img.save(small, "JPEG", quality=78, optimize=True)
    return big.getvalue(), small.getvalue(), w, h


def _write(data):
    name = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join(PHOTO_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)  # aldrig en halvskriven bild på disk
    return name


def delete_files(names):
    for n in names:
        if not n:
            continue
        try:
            os.remove(os.path.join(PHOTO_DIR, os.path.basename(n)))
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Kunde inte ta bort foto %s", n)


def photos_for(wid, public_only=False):
    return q("SELECT * FROM wo_photos WHERE work_order_id=?" + (" AND public=1" if public_only else "")
             + " ORDER BY CASE stage WHEN 'inlamning' THEN 0 WHEN 'fore' THEN 1 WHEN 'efter' THEN 2 ELSE 3 END, id",
             (wid,))


@app.route("/admin/ao/<int:wid>/foto", methods=["POST"])
@login_required
def wo_photo_upload(wid):
    q("SELECT id FROM work_orders WHERE id=?", (wid,), one=True) or abort(404)
    stage = request.form.get("stage") if request.form.get("stage") in STAGES else "ovrigt"
    public = 1 if request.form.get("public") else 0
    caption = clean(request.form.get("caption"), 200)
    files = [f for f in request.files.getlist("photos") if f and f.filename][:MAX_PER_UPLOAD]
    if not files:
        flash("Välj eller ta en bild först.", "error")
        return redirect(url_for("wo_detail", wid=wid) + "#foton")
    saved, errors, written = 0, [], []
    for f in files:
        data = f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            errors.append(f"{f.filename}: för stor (max 25 MB).")
            continue
        try:
            big, small, w, h = process_image(data)
        except ValueError as exc:
            errors.append(f"{f.filename}: {exc}")
            continue
        names = (_write(big), _write(small))
        written.extend(names)
        try:
            x("INSERT INTO wo_photos(work_order_id, created_at, stage, caption, public, file, thumb, width, height) "
              "VALUES (?,?,?,?,?,?,?,?,?)", (wid, now_s(), stage, caption or None, public, names[0], names[1], w, h))
            saved += 1
        except Exception:  # noqa: BLE001 – städa filerna om raden inte gick att spara
            delete_files(names)
            raise
    if saved:
        flash(f"{saved} bild{'er' if saved > 1 else ''} sparad{'e' if saved > 1 else ''}"
              + (" och synlig för kunden." if public else ", bara synlig för dig."))
    for e in errors:
        flash(e, "error")
    return redirect(url_for("wo_detail", wid=wid) + "#foton")


def _send(row, thumb):
    name = row["thumb"] if thumb else row["file"]
    path = os.path.join(PHOTO_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype="image/jpeg", max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/admin/foto/<int:pid>")
@login_required
def photo_admin(pid):
    row = q("SELECT * FROM wo_photos WHERE id=?", (pid,), one=True) or abort(404)
    return _send(row, request.args.get("liten") == "1")


@app.route("/admin/foto/<int:pid>/synlig", methods=["POST"])
@login_required
def photo_toggle(pid):
    row = q("SELECT * FROM wo_photos WHERE id=?", (pid,), one=True) or abort(404)
    x("UPDATE wo_photos SET public=? WHERE id=?", (0 if row["public"] else 1, pid))
    flash("Bilden visas nu för kunden." if not row["public"] else "Bilden är nu bara synlig för dig.")
    return redirect(url_for("wo_detail", wid=row["work_order_id"]) + "#foton")


@app.route("/admin/foto/<int:pid>/ta-bort", methods=["POST"])
@login_required
def photo_delete(pid):
    row = q("SELECT * FROM wo_photos WHERE id=?", (pid,), one=True) or abort(404)
    x("DELETE FROM wo_photos WHERE id=?", (pid,))
    delete_files((row["file"], row["thumb"]))
    flash("Bilden är borttagen.")
    return redirect(url_for("wo_detail", wid=row["work_order_id"]) + "#foton")


@app.route("/s/<token>/foto/<int:pid>")
def photo_public(token, pid):
    """Kundens bild: kräver ärendets hemliga länk och att bilden är markerad som synlig."""
    if rate_limited(f"photo:{client_ip()}", 300, 600):
        abort(429)
    row = q("SELECT p.* FROM wo_photos p JOIN work_orders w ON w.id=p.work_order_id "
            "WHERE p.id=? AND p.public=1 AND w.token=?", (pid, token), one=True)
    if not row:
        b = q("SELECT work_order_id FROM bookings WHERE token=? AND work_order_id IS NOT NULL", (token,), one=True)
        if b:
            row = q("SELECT * FROM wo_photos WHERE id=? AND public=1 AND work_order_id=?",
                    (pid, b["work_order_id"]), one=True)
    if not row:
        abort(404)
    return _send(row, request.args.get("liten") == "1")


app.jinja_env.globals.update(photos_for=photos_for, PHOTO_STAGES=STAGES)
