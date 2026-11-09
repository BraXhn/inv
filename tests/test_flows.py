"""End-to-end-tester med Flasks testklient. Kör: python -m pytest tests/ eller python tests/test_flows.py"""
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=TMP, ADMIN_PASSWORD="hemligt123", BACKUP_ENABLED="0", TRUST_PROXY="1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import app as verkstad  # noqa: E402

A = verkstad.app
A.config["TESTING"] = True


def csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert m, f"Ingen CSRF-token på {path}"
    return m.group(1)


def login(c):
    tok = csrf(c, "/login")
    r = c.post("/login", data={"_csrf": tok, "username": "admin", "password": "hemligt123", "next": "/admin"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin"), r.status_code
    return c


def set_open_all_days(c):
    tok = csrf(c, "/admin/installningar")
    data = {"_csrf": tok, "business_name": "Testverkstan", "categories": "Cykel,Elektronik", "booking_enabled": "1",
            "open_days": ["1", "2", "3", "4", "5", "6", "7"], "open_from": "08:00", "open_to": "22:00",
            "slot_minutes": "60", "slot_capacity": "1", "days_ahead": "14", "min_notice_hours": "0",
            "labor_rate": "500", "retention_months": "24", "public_url": "https://verkstad.example.se/"}
    assert c.post("/admin/installningar", data=data).status_code == 302


def test_everything():
    pub = A.test_client()
    adm = login(A.test_client())
    set_open_all_days(adm)

    # Säkerhet: admin kräver inloggning, POST utan CSRF nekas, headers finns
    assert pub.get("/admin").status_code == 302
    assert pub.post("/boka", data={"name": "x"}).status_code == 400
    r = pub.get("/boka")
    assert "Content-Security-Policy" in r.headers and r.headers["X-Frame-Options"] == "DENY"
    assert pub.get("/healthz").json["ok"]

    # Publik bokning
    html = pub.get("/boka").get_data(as_text=True)
    slots = re.findall(r'name="slot" id="[^"]+" value="([^"]+)"', html)
    assert slots, "Inga lediga tider genererades"
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    form = {"_csrf": tok, "name": "Anna Andersson", "email": "Anna@Example.se", "phone": "0701234567",
            "category": "Cykel", "item": "Scott Spark", "description": "Bromsarna tar dåligt", "slot": slots[0],
            "consent": "1", "website": ""}
    r = pub.post("/boka", data=form)
    assert r.status_code == 302 and "/s/" in r.headers["Location"], r.get_data(as_text=True)[:500]
    status_path = r.headers["Location"].split("?")[0]
    assert "Bokning" in pub.get(status_path).get_data(as_text=True)

    # Samma tid ska inte kunna dubbelbokas (kapacitet 1)
    tok = csrf(pub, "/boka")
    r = pub.post("/boka", data={**form, "_csrf": tok, "email": "bo@example.se"})
    assert r.status_code == 200 and "inte längre ledig" in r.get_data(as_text=True)

    # Honeypot
    tok = csrf(pub, "/boka")
    r = pub.post("/boka", data={**form, "_csrf": tok, "slot": slots[1], "website": "spam"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/boka")

    # Validering
    tok = csrf(pub, "/boka")
    r = pub.post("/boka", data={"_csrf": tok, "slot": slots[1]})
    body = r.get_data(as_text=True)
    assert "Ange ditt namn" in body and "godkänna" in body

    # Admin ser bokningen och bekräftar
    html = adm.get("/admin/bokningar").get_data(as_text=True)
    assert "Scott Spark" in html
    bid = re.search(r'/admin/bokningar/(\d+)/status', html).group(1)
    tok = csrf(adm, "/admin/bokningar")
    assert adm.post(f"/admin/bokningar/{bid}/status", data={"_csrf": tok, "action": "bekrafta"}).status_code == 302
    assert "Bekräftad" in pub.get(status_path).get_data(as_text=True)

    # Kalenderflöde
    cal_token = verkstad.connect().execute("SELECT value FROM settings WHERE key='calendar_token'").fetchone()[0]
    ics = pub.get(f"/kalender/{cal_token}.ics").get_data(as_text=True)
    assert "BEGIN:VEVENT" in ics and "Scott Spark" in ics
    assert pub.get("/kalender/fel.ics").status_code == 404

    # Konvertera till arbetsorder
    r = adm.post(f"/admin/bokningar/{bid}/arbetsorder", data={"_csrf": tok})
    assert r.status_code == 302
    wid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    html = adm.get(f"/admin/ao/{wid}").get_data(as_text=True)
    assert "AO-" in html and "Ramnummer noterat" in html  # cykelchecklista
    assert "verkstad.example.se/t/" in html  # tagg-URL använder publik adress

    # Kundens statuslänk visar nu arbetsordern
    assert "Inlämnad" in pub.get(status_path).get_data(as_text=True)

    # Plats + del + lageravdrag
    tok = csrf(adm, "/admin/platser")
    r = adm.post("/admin/platser", data={"_csrf": tok, "name": "Hylla B · Låda 3", "description": "Bromsar"})
    lid = int(r.headers["Location"].split("/")[-1])
    tok = csrf(adm, "/admin/lager/ny")
    r = adm.post("/admin/lager/ny", data={"_csrf": tok, "name": "Bromsbelägg Shimano B01S", "sku": "B01S",
                                          "category": "Bromsar", "location_id": lid, "qty": "4", "min_qty": "2",
                                          "unit": "par", "unit_cost": "60", "unit_price": "129"})
    pid = int(r.headers["Location"].split("/")[-1])
    tok = csrf(adm, f"/admin/ao/{wid}")
    adm.post(f"/admin/ao/{wid}/rad", data={"_csrf": tok, "part_id": pid, "qty": "2", "unit_price": ""})
    qty = verkstad.connect().execute("SELECT qty FROM parts WHERE id=?", (pid,)).fetchone()[0]
    assert qty == 2, qty
    adm.post(f"/admin/ao/{wid}/rad", data={"_csrf": tok, "description": "Bromsvätska (köpt)", "qty": "1",
                                           "unit_price": "89"})
    # Låg-saldo syns på översikten och inköpslistan
    assert "Bromsbelägg" in adm.get("/admin").get_data(as_text=True)
    assert "Bromsbelägg" in adm.get("/admin/inkop").get_data(as_text=True)

    # Ta bort lagerrad → återförs
    line_id = verkstad.connect().execute("SELECT id FROM wo_lines WHERE part_id=?", (pid,)).fetchone()[0]
    adm.post(f"/admin/ao/{wid}/rad/{line_id}/ta-bort", data={"_csrf": tok})
    assert verkstad.connect().execute("SELECT qty FROM parts WHERE id=?", (pid,)).fetchone()[0] == 4
    adm.post(f"/admin/ao/{wid}/rad", data={"_csrf": tok, "part_id": pid, "qty": "1", "unit_price": "150"})

    # Spara arbetstid + offert, skicka offert, kund godkänner
    adm.post(f"/admin/ao/{wid}", data={"_csrf": tok, "category": "Cykel", "item": "Scott Spark",
                                       "labor_minutes": "90", "labor_rate": "500", "price_quote": "1100",
                                       "diagnosis": "Byte av belägg och luftning", "checklist": ["Ramnummer noterat"],
                                       "location_id": lid})
    adm.post(f"/admin/ao/{wid}/status", data={"_csrf": tok, "status": "offert", "message": ""})
    page = pub.get(status_path).get_data(as_text=True)
    assert "Godkänn offerten" in page and "1 100 kr" in page
    ptok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    wo_token = verkstad.connect().execute("SELECT token FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    assert pub.post(f"/s/{wo_token}/godkann", data={"_csrf": ptok}).status_code == 302
    assert verkstad.connect().execute("SELECT status FROM work_orders WHERE id=?", (wid,)).fetchone()[0] == "pagar"

    # Summa: 150 + 89 + 1,5 h * 500 = 989
    page = pub.get(f"/s/{wo_token}").get_data(as_text=True)
    assert "989 kr" in page, re.findall(r"Totalt.*?kr", page, re.S)[:1]
    # Interna anteckningar läcker inte
    adm.post(f"/admin/ao/{wid}/handelse", data={"_csrf": tok, "text": "HEMLIG intern notering"})
    assert "HEMLIG" not in pub.get(f"/s/{wo_token}").get_data(as_text=True)

    # Taggar: kräver inloggning, leder rätt
    tag = verkstad.connect().execute("SELECT tag_code FROM parts WHERE id=?", (pid,)).fetchone()[0]
    assert pub.get(f"/t/{tag}").status_code == 302 and "/login" in pub.get(f"/t/{tag}").headers["Location"]
    assert adm.get(f"/t/{tag}").headers["Location"].endswith(f"/admin/lager/{pid}")

    # Utskrifter, etiketter, CSV, alla listsidor renderar
    for path in [f"/admin/ao/{wid}/kvitto", f"/admin/ao/{wid}/kvitto?typ=faktura", "/admin/etiketter",
                 "/admin/etiketter?typ=delar", "/admin/etiketter?typ=ao", "/admin/lager", "/admin/lager?lagt=1",
                 "/admin/kunder", "/admin/ao", "/admin/ao?status=alla", "/admin/ao?q=Scott", f"/admin/platser/{lid}",
                 f"/admin/lager/{pid}", "/admin/ao/ny", "/admin/installningar", "/integritet",
                 "/admin/bokningar?visa=alla"]:
        r = adm.get(path)
        assert r.status_code == 200, (path, r.status_code)
    assert "Bromsbelägg" in adm.get("/admin/lager.csv").get_data(as_text=True)
    try:  # QR-koder verifieras där segno finns (Docker/CI)
        import segno  # noqa: F401
        assert "<svg" in adm.get(f"/admin/lager/{pid}").get_data(as_text=True), "QR-kod saknas"
    except ImportError:
        print("segno saknas lokalt – QR-test hoppas över")

    # Klar + utlämnad → intäkt på översikten
    adm.post(f"/admin/ao/{wid}/status", data={"_csrf": tok, "status": "klar"})
    adm.post(f"/admin/ao/{wid}/status", data={"_csrf": tok, "status": "utlamnad"})
    assert "989 kr" in adm.get("/admin").get_data(as_text=True)

    # Inventering (sätt exakt saldo)
    adm.post(f"/admin/lager/{pid}/justera", data={"_csrf": tok, "mode": "set", "amount": "10"})
    assert verkstad.connect().execute("SELECT qty FROM parts WHERE id=?", (pid,)).fetchone()[0] == 10

    # GDPR: export + anonymisering
    cid = verkstad.connect().execute("SELECT customer_id FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    assert "anna@example.se" in adm.get(f"/admin/kunder/{cid}/export").get_data(as_text=True)
    adm.post(f"/admin/kunder/{cid}/anonymisera", data={"_csrf": tok})
    row = verkstad.connect().execute("SELECT name, email FROM customers WHERE id=?", (cid,)).fetchone()
    assert row[0] == "Anonymiserad kund" and row[1] is None

    # Backup
    adm.post("/admin/backup", data={"_csrf": tok})
    assert os.listdir(os.path.join(TMP, "backups"))

    # Manuell arbetsorder med ny kund, elektronikchecklista
    tok = csrf(adm, "/admin/ao/ny")
    r = adm.post("/admin/ao/ny", data={"_csrf": tok, "new_name": "Bertil", "new_phone": "070", "category": "Elektronik",
                                       "item": "Marantz PM6006", "problem": "Brum i vänster kanal"})
    wid2 = int(r.headers["Location"].split("/")[-1])
    html = adm.get(f"/admin/ao/{wid2}").get_data(as_text=True)
    assert "Serienummer noterat" in html and "AO-" in html

    # Inloggningsbegränsning
    c = A.test_client()
    for _ in range(9):
        t = csrf(c, "/login")
        r = c.post("/login", data={"_csrf": t, "username": "admin", "password": "fel"})
    assert "För många" in r.get_data(as_text=True)

    # Öppen redirect blockeras
    c2 = A.test_client()
    t = csrf(c2, "/login")
    r = c2.post("/login", data={"_csrf": t, "username": "admin", "password": "hemligt123",
                                "next": "//evil.example.com"}, environ_base={"REMOTE_ADDR": "10.9.9.9"})
    assert r.headers["Location"].endswith("/admin"), r.headers["Location"]
    print("ALLA TESTER OK")


if __name__ == "__main__":
    test_everything()
