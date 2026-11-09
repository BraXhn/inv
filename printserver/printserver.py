#!/usr/bin/env python3
"""BraXhn Verkstad – printserver för Brother QL-etikettskrivare på en Raspberry Pi.

Tar emot PNG-bilder över HTTP och skriver ut dem med brother_ql (paketet brother_ql_next).
Bara standardbiblioteket här; brother_ql och Pillow installeras av install.sh i en venv.

  GET  /health            status (ingen nyckel krävs, inga hemligheter)
  POST /print             skriv ut, kropp = PNG. Rubrik: Authorization: Bearer <nyckel>
                          frågor: copies=1..10, label=62, cut=1, red=0
  POST /test              skriv ut en testetikett

Inställningar i /etc/verkstad-print.env (se install.sh).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"
TOKEN = os.environ.get("PRINT_TOKEN", "")
MODEL = os.environ.get("PRINTER_MODEL", "QL-800")
PRINTER = os.environ.get("PRINTER", "file:///dev/usb/lp0")
BACKEND = os.environ.get("PRINTER_BACKEND", "linux_kernel")
LABEL = os.environ.get("LABEL", "62")
PORT = int(os.environ.get("PORT", "8631"))
BROTHER_QL = os.environ.get("BROTHER_QL", shutil.which("brother_ql") or "/opt/verkstad-print/venv/bin/brother_ql")
MAX_BYTES = 6 * 1024 * 1024
LABELS = {"62", "62red", "62x29", "62x100", "29", "29x90", "38", "50", "54", "102", "17x54", "17x87", "23x23",
          "29x42", "39x90", "39x48", "52x29", "102x51", "102x152", "12", "18", "d12", "d24", "d58"}

_lock = threading.Lock()       # en utskrift i taget – skrivaren klarar inte parallella jobb
_stats = {"printed": 0, "last_ok": None, "last_error": None, "started": datetime.now().isoformat(timespec="seconds")}


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def printer_present():
    if BACKEND == "linux_kernel":
        dev = PRINTER.replace("file://", "")
        return os.path.exists(dev), dev
    return True, PRINTER  # nätverk/pyusb: kan inte kontrolleras utan att skriva


def run_print(png_path, label, copies=1, cut=True, red=False):
    ok, dev = printer_present()
    if not ok:
        raise RuntimeError(f"Skrivaren hittas inte ({dev}). Kontrollera att den är på och ansluten med USB.")
    cmd = [BROTHER_QL, "-b", BACKEND, "-m", MODEL, "-p", PRINTER, "print", "-l", label]
    if not cut:
        cmd.append("--no-cut")
    if red:
        cmd.append("--red")
    cmd += [png_path] * copies
    with _lock:
        log(f"Skriver ut {copies} st på {label} ({MODEL})")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Skrivaren svarade inte inom 90 sekunder. Kontrollera att etikettrullen sitter rätt.")
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        hint = out.strip().splitlines()[-1] if out.strip() else f"kod {res.returncode}"
        if "label" in out.lower() and "size" in out.lower():
            hint = f"Fel etikettstorlek i skrivaren. Byt rulle eller ändra LABEL ({label}). ({hint})"
        raise RuntimeError(f"Utskriften misslyckades: {hint}")
    return out


def make_test_png(path):
    from PIL import Image, ImageDraw, ImageFont  # finns i venv via brother_ql
    img = Image.new("RGB", (696, 300), "white")
    d = ImageDraw.Draw(img)

    def font(size):
        for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
            if os.path.exists(f):
                return ImageFont.truetype(f, size)
        return ImageFont.load_default()

    d.rectangle([6, 6, 689, 293], outline="black", width=4)
    d.text((30, 30), "Testetikett", fill="black", font=font(64))
    d.text((30, 120), "BraXhn Verkstad printserver", fill="black", font=font(34))
    d.text((30, 170), f"{MODEL} · {LABEL} mm · {datetime.now():%Y-%m-%d %H:%M}", fill="black", font=font(28))
    d.text((30, 225), "Fungerar!", fill="black", font=font(40))
    img.save(path)


class Handler(BaseHTTPRequestHandler):
    server_version = f"VerkstadPrint/{VERSION}"

    def log_message(self, fmt, *args):  # tystare logg: bara fel och utskrifter
        pass

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        sent = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("X-Print-Token", "")
        import hmac
        return bool(sent) and hmac.compare_digest(sent, TOKEN)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/health"):
            ok, dev = printer_present()
            return self._json(200, {"ok": True, "version": VERSION, "printer_connected": ok, "model": MODEL,
                                    "label": LABEL, "backend": BACKEND, "printed": _stats["printed"],
                                    "last_ok": _stats["last_ok"], "last_error": _stats["last_error"],
                                    "started": _stats["started"]})
        self._json(404, {"ok": False, "error": "Finns inte"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ("/print", "/test"):
            return self._json(404, {"ok": False, "error": "Finns inte"})
        if not self._authorized():
            time.sleep(0.5)  # bromsa gissningar
            return self._json(401, {"ok": False, "error": "Fel nyckel. Kopiera PRINT_TOKEN från /etc/verkstad-print.env på Pi:n."})
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        label = qs.get("label") or LABEL
        if label not in LABELS:
            return self._json(400, {"ok": False, "error": f"Okänd etikettstorlek: {label}"})
        try:
            copies = max(1, min(10, int(qs.get("copies", "1"))))
        except ValueError:
            copies = 1
        length = int(self.headers.get("Content-Length") or 0)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        try:
            if url.path == "/test":
                make_test_png(tmp.name)
            else:
                if length <= 0 or length > MAX_BYTES:
                    return self._json(400, {"ok": False, "error": "Skicka en PNG-bild (max 6 MB)."})
                data = self.rfile.read(length)
                if not data.startswith(b"\x89PNG"):
                    return self._json(400, {"ok": False, "error": "Bara PNG-bilder kan skrivas ut."})
                with open(tmp.name, "wb") as f:
                    f.write(data)
            run_print(tmp.name, label, copies, cut=qs.get("cut", "1") != "0", red=qs.get("red") == "1")
            _stats["printed"] += copies
            _stats["last_ok"] = datetime.now().isoformat(timespec="seconds")
            self._json(200, {"ok": True, "printed": copies})
        except Exception as exc:  # noqa: BLE001 – felet ska tillbaka till Verkstad som text
            _stats["last_error"] = f"{datetime.now():%H:%M} {exc}"
            log(f"FEL: {exc}")
            self._json(500, {"ok": False, "error": str(exc)})
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def main():
    if not TOKEN:
        log("PRINT_TOKEN saknas i /etc/verkstad-print.env – utskrifter nekas tills den är satt.")
    log(f"Verkstad printserver {VERSION} på port {PORT} – {MODEL}, {BACKEND} {PRINTER}, etikett {LABEL}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
