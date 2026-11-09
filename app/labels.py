"""Etiketter som PNG för Brother QL (300 dpi). 62 mm löpande = 696 px bred, 62x29 stansad = 696 x 271 px.

Ritas med Pillow och skickas till printservern på Raspberry Pi:n (printserver/printserver.py).
"""
import io
import json
import os
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

WIDTH = 696
SIZES = {"62": None, "62x29": 271, "62x100": 1109}   # None = löpande, höjden blir efter innehållet
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"]


def _font(size, bold=True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # äldre Pillow
        return ImageFont.load_default()


def _qr(data, px):
    """QR-kod som svartvit bild, px bred. Utan segno (bara lokalt) blir det en tom ruta."""
    try:
        import segno
        m = segno.make(data, error="m").matrix
        n = len(m)
        border = 2
        img = Image.new("1", (n + 2 * border, n + 2 * border), 1)
        pix = img.load()
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    pix[x + border, y + border] = 0
        return img.resize((px, px), Image.NEAREST)
    except Exception:  # noqa: BLE001
        img = Image.new("1", (px, px), 1)
        ImageDraw.Draw(img).rectangle([0, 0, px - 1, px - 1], outline=0, width=4)
        return img


def _fit(draw, text, font, max_w):
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _font_fit(draw, text, max_w, start, minimum=20):
    """Största fetstil (från start nedåt) där texten får plats – numret kapas aldrig."""
    size = start
    while size > minimum and draw.textlength(str(text), font=_font(size)) > max_w:
        size -= 2
    return _font(size)


def _wrap(draw, text, font, max_w, max_lines=3):
    words, lines, cur = str(text or "").split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and " ".join(lines) != " ".join(words):
        lines[-1] = _fit(draw, lines[-1] + " …", font, max_w)
    return lines


def _png(img):
    buf = io.BytesIO()
    img.convert("1", dither=Image.NONE).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _canvas(size, h_default):
    h = SIZES.get(size) or h_default
    img = Image.new("L", (WIDTH, h), 255)
    return img, ImageDraw.Draw(img), h


def object_label(wo, obj_url, size="62"):
    """Lappen på objektet: ärendenummer, objekt, kund och QR till ärendet (NFC/QR-spårbarhet)."""
    compact = SIZES.get(size) == 271
    img, d, h = _canvas(size, 400)
    if compact:  # 62x29: QR till vänster i full höjd, text till höger
        qr_px = h - 24
        img.paste(_qr(obj_url, qr_px), (8, 12))
        x, w, y = qr_px + 24, WIDTH - qr_px - 36, 14
        num = _font_fit(d, wo["number"], w, 50)
        d.text((x, y), wo["number"], font=num, fill=0)
        y += num.size + 14
        f_item = _font(32)
        d.text((x, y), _fit(d, wo["item"], f_item, w), font=f_item, fill=0)
        y += 44
        f_small = _font(24, bold=False)
        phone = (wo["customer_phone"] or "")[-4:]
        for line in (f"{wo['customer_name'] or ''}" + (f", tel …{phone}" if phone else ""),
                     f"Inlämnad {str(wo['created_at'])[:10]}"):
            d.text((x, y), _fit(d, line, f_small, w), font=f_small, fill=0)
            y += 32
        return _png(img)
    x, y = 22, 18
    num = _font_fit(d, wo["number"], WIDTH - 44, 84)
    d.text((x, y), wo["number"], font=num, fill=0)
    y += num.size + 18
    d.line([(x, y), (WIDTH - 22, y)], fill=0, width=3)
    y += 16
    qr_px = h - y - 14
    img.paste(_qr(obj_url, qr_px), (WIDTH - qr_px - 14, y))
    w = WIDTH - qr_px - 60
    f_item = _font(38)
    for line in _wrap(d, wo["item"], f_item, w, 2):
        d.text((x, y), line, font=f_item, fill=0)
        y += 46
    y += 4
    f_small = _font(26, bold=False)
    phone = (wo["customer_phone"] or "")[-4:]
    meta = [wo["customer_name"] or "", f"tel …{phone}" if phone else "",
            f"{wo['category'] or ''}, {str(wo['created_at'])[:10]}"]
    if wo["serial"]:
        meta.append(f"Ser.nr {wo['serial']}")
    if wo["accessories"]:
        meta.append(f"Med: {wo['accessories']}")
    for line in [m for m in meta if m]:
        if y + 30 > h - 6:
            break
        d.text((x, y), _fit(d, line, f_small, w), font=f_small, fill=0)
        y += 34
    return _png(img)


def customer_ticket(wo, status_url, s):
    """Mottagningssedel för kunden på 62 mm löpande rulle."""
    img = Image.new("L", (WIDTH, 2400), 255)
    d = ImageDraw.Draw(img)
    x, w, y = 24, WIDTH - 48, 20
    d.text((x, y), _fit(d, s.get("business_name", ""), _font(40), w), font=_font(40), fill=0)
    y += 52
    contact = ", ".join(v for v in (s.get("address"), s.get("contact_phone")) if v)
    if contact:
        d.text((x, y), _fit(d, contact, _font(24, False), w), font=_font(24, False), fill=0)
        y += 34
    d.line([(x, y + 6), (x + w, y + 6)], fill=0, width=3)
    y += 22
    d.text((x, y), "Mottagningssedel", font=_font(30), fill=0)
    y += 44
    num = _font_fit(d, wo["number"], w, 80)
    d.text((x, y), wo["number"], font=num, fill=0)
    y += num.size + 22
    # Hämtkod i ram till vänster, QR till kundens statussida till höger
    code_f = _font(64)
    cw = d.textlength(wo["pickup_code"] or "", font=code_f) + 50
    d.rounded_rectangle([x, y, x + max(cw, 260), y + 118], radius=14, outline=0, width=4)
    d.text((x + 22, y + 8), "Hämtkod", font=_font(22, False), fill=0)
    d.text((x + 22, y + 34), wo["pickup_code"] or "", font=code_f, fill=0)
    qr_px = 230
    img.paste(_qr(status_url, qr_px), (WIDTH - qr_px - 14, y - 6))
    d.text((WIDTH - qr_px - 14, y + qr_px - 2), "Följ ärendet", font=_font(20, False), fill=0)
    y += qr_px + 34
    f_l, f_v = _font(22, False), _font(26)
    rows = [("Kund", wo["customer_name"]), ("Objekt", f"{wo['category']}: {wo['item']}"),
            ("Ser.nr", wo["serial"] or "–"), ("Medlämnat", wo["accessories"] or "Inget"),
            ("Inlämnad", str(wo["created_at"])[:16])]
    for label, val in rows:
        d.text((x, y), label, font=f_l, fill=0)
        for line in _wrap(d, val, f_v, w - 170, 2):
            d.text((x + 170, y - 2), line, font=f_v, fill=0)
            y += 34
        y += 6
    if wo["problem"]:
        d.text((x, y), "Uppdrag", font=f_l, fill=0)
        for line in _wrap(d, wo["problem"], _font(24, False), w - 170, 4):
            d.text((x + 170, y), line, font=_font(24, False), fill=0)
            y += 32
        y += 6
    y += 10
    d.line([(x, y), (x + w, y)], fill=0, width=2)
    y += 16
    for line in _wrap(d, f"Ta med sedeln eller visa hämtkoden {wo['pickup_code']} när du hämtar. "
                         "Skanna QR-koden för att följa ärendet.", _font(26), w, 4):
        d.text((x, y), line, font=_font(26), fill=0)
        y += 34
    y += 8
    for line in _wrap(d, s.get("intake_terms", ""), _font(20, False), w, 8):
        d.text((x, y), line, font=_font(20, False), fill=0)
        y += 26
    return _png(img.crop((0, 0, WIDTH, y + 24)))


def tag_label(title, subtitle, code, url, size="62"):
    """Etikett för lagerplats eller del: namn, beskrivning och QR (samma länk som NFC-taggen)."""
    compact = SIZES.get(size) == 271
    img, d, h = _canvas(size, 300)
    qr_px = h - 30 if compact else 270
    img.paste(_qr(url, qr_px), (WIDTH - qr_px - 10, (h - qr_px) // 2))
    x, w, y = 22, WIDTH - qr_px - 50, 20
    f_t = _font(52 if compact else 58)
    for line in _wrap(d, title, f_t, w, 2):
        d.text((x, y), line, font=f_t, fill=0)
        y += 62 if compact else 70
    if subtitle:
        f_s = _font(26, False)
        for line in _wrap(d, subtitle, f_s, w, 2 if compact else 3):
            if y + 30 > h - 40:
                break
            d.text((x, y), line, font=f_s, fill=0)
            y += 34
    d.text((x, h - 38), code, font=_font(22, False), fill=0)
    return _png(img)


# ---------------------------------------------------------------------------
# Printservern
# ---------------------------------------------------------------------------
def _request(url, token, path, data=None, timeout=12):
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST" if data is not None else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "image/png")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read())
        except ValueError:
            return False, {"error": f"Printservern svarade {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, {"error": f"Når inte printservern ({getattr(exc, 'reason', exc)}). Är Pi:n på och adressen rätt?"}


def send(url, token, png, label="62", copies=1):
    if not url:
        return False, "Ingen etikettskrivare är inställd (Inställningar → Etikettskrivare)."
    ok, js = _request(url, token, f"/print?label={label}&copies={int(copies)}", png)
    return (True, "Utskriven") if ok and js.get("ok") else (False, js.get("error") or "Utskriften misslyckades")


def test(url, token):
    ok, js = _request(url, token, "/test", b"")
    return (True, "Testetikett utskriven") if ok and js.get("ok") else (False, js.get("error") or "Misslyckades")


def health(url, timeout=2):
    if not url:
        return None
    ok, js = _request(url, "", "/health", timeout=timeout)
    return js if ok else {"ok": False, "error": js.get("error")}
