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
    assert "Beställning" in pub.get(status_path).get_data(as_text=True)

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
    wid = int(re.search(r"/admin/ao/(\d+)", r.headers["Location"]).group(1))
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
    assert "Godkänn och starta" in page and "1 100 kr" in page
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
    wid2 = int(re.search(r"/admin/ao/(\d+)", r.headers["Location"]).group(1))
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



def test_v2():
    """Tjänster, autobekräftelse, e-post, påminnelser, timer, tavla, sök och API."""
    import time as _t
    verkstad._rl_hits.clear()  # nollställ inloggningsspärren från förra testet
    sent = []
    verkstad._deliver = lambda cfg, msg: sent.append(msg)  # fångar e-post istället för SMTP
    adm = login(A.test_client())
    pub = A.test_client()
    set_open_all_days(adm)
    tok = csrf(adm, "/admin/installningar")
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "epost", "smtp_host": "smtp.test",
                                           "smtp_port": "587", "smtp_user": "u", "smtp_password": "p",
                                           "smtp_from": "verkstad@test.se", "notify_email": "agare@test.se"})
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "bokning", "booking_enabled": "1",
                                           "auto_confirm": "1", "reminder_enabled": "1",
                                           "open_days": ["1", "2", "3", "4", "5", "6", "7"], "open_from": "08:00",
                                           "open_to": "22:00", "slot_minutes": "60", "slot_capacity": "1",
                                           "days_ahead": "14", "min_notice_hours": "0"})
    db = verkstad.connect()
    assert db.execute("SELECT value FROM settings WHERE key='smtp_user'").fetchone()[0] == "u"
    assert db.execute("SELECT value FROM settings WHERE key='booking_enabled'").fetchone()[0] == "1"
    # Tomt lösenordsfält ska behålla sparat lösenord
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "epost", "smtp_host": "smtp.test",
                                           "smtp_password": ""})
    assert verkstad.connect().execute("SELECT value FROM settings WHERE key='smtp_password'").fetchone()[0] == "p"

    # Tjänst med fast pris
    adm.post("/admin/tjanster", data={"_csrf": tok, "name": "Punktering fast", "category": "Cykel",
                                      "price_from": "199", "price_fixed": "1", "active": "1", "sort": "1"})
    svc_id = verkstad.connect().execute("SELECT id FROM services WHERE name='Punktering fast'").fetchone()[0]
    html = pub.get("/boka").get_data(as_text=True)
    assert "Punktering fast" in html and "199 kr" in html
    slots = re.findall(r'name="slot" id="[^"]+" value="([^"]+)"', html)
    ptok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = pub.post("/boka", data={"_csrf": ptok, "name": "Cecilia Carlsson", "email": "cc@test.se", "category": "Cykel",
                                "service_id": str(svc_id), "item": "Crescent", "description": "", "slot": slots[-1],
                                "consent": "1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    status_path = r.headers["Location"].split("?")[0]
    page = pub.get(r.headers["Location"]).get_data(as_text=True)
    assert "Tack för din beställning" in page and "Punktering fast" in page
    assert pub.get(status_path + "/kalender.ics").get_data(as_text=True).startswith("BEGIN:VCALENDAR")
    _t.sleep(0.5)
    subjects = [m["Subject"] for m in sent]
    assert any("Beställning bekräftad" in x for x in subjects), subjects
    conf = next(m for m in sent if "Beställning bekräftad" in m["Subject"])
    assert any(part.get_filename() == "bokning.ics" for part in conf.iter_attachments())
    assert conf.get_body(("html",)) is not None
    assert any(m["To"] == "agare@test.se" for m in sent)  # notis till ägaren
    assert verkstad.connect().execute("SELECT COUNT(*) FROM mail_log WHERE status='skickat'").fetchone()[0] >= 2

    # Ta emot → arbetsorder med fast pris som rad
    bid = verkstad.connect().execute("SELECT id FROM bookings WHERE item='Crescent'").fetchone()[0]
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "verkstad", "print_on_intake": "1"})
    r = adm.post(f"/admin/bokningar/{bid}/arbetsorder", data={"_csrf": tok})
    assert "/mottagning?auto=1" in r.headers["Location"], r.headers["Location"]
    wid = int(re.search(r"/admin/ao/(\d+)", r.headers["Location"]).group(1))
    line = verkstad.connect().execute("SELECT description, unit_price FROM wo_lines WHERE work_order_id=?", (wid,)).fetchone()
    assert line[0] == "Punktering fast" and line[1] == 199

    # Mottagningssedel + objektlapp, hämtkod och mejl till kunden
    code = verkstad.connect().execute("SELECT pickup_code FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    assert code and len(code) == 4 and code.isdigit()
    page = adm.get(f"/admin/ao/{wid}/mottagning?auto=1").get_data(as_text=True)
    assert "Mottagningssedel" in page and code in page and "data-autoprint" in page and "Klipp här" in page
    assert "Crescent" in page and "print-a4.css" in page
    lab = adm.get(f"/admin/ao/{wid}/mottagning?format=etikett").get_data(as_text=True)
    assert "print-label.css" in lab and "Mottagningssedel</div>" not in lab and "o-number" in lab
    kund = adm.get(f"/admin/ao/{wid}/mottagning?format=kund").get_data(as_text=True)
    assert "objtag" not in kund
    _t.sleep(0.3)
    mott = [m for m in sent if "Vi har tagit emot" in m["Subject"]]
    assert mott and code in mott[-1].get_body(("plain",)).get_content()

    # Ny del direkt från arbetsordern: köp 4, använd 1 -> 3 i lager, rad på ärendet
    r = adm.post(f"/admin/ao/{wid}/ny-del", data={"_csrf": tok, "name": "Slang 28x1.5", "sku": "SL28", "qty": "1",
                                                 "stock": "4", "unit_price": "129", "unit_cost": "45"})
    assert r.status_code == 302
    c = verkstad.connect()
    part = c.execute("SELECT id, qty, tag_code FROM parts WHERE sku='SL28'").fetchone()
    assert part[1] == 3 and part[2]
    assert tuple(c.execute("SELECT qty, unit_price FROM wo_lines WHERE part_id=? AND work_order_id=?", (part[0], wid)).fetchone()) == (1, 129)
    assert sum(r[0] for r in c.execute("SELECT delta FROM stock_moves WHERE part_id=?", (part[0],))) == 3
    adm.post(f"/admin/ao/{wid}/ny-del", data={"_csrf": tok, "name": "Dubblett", "sku": "SL28", "qty": "1", "unit_price": "1"})
    assert verkstad.connect().execute("SELECT COUNT(*) FROM parts WHERE sku='SL28'").fetchone()[0] == 1
    # Utan lagersaldo: lager = använt -> 0 kvar
    adm.post(f"/admin/ao/{wid}/ny-del", data={"_csrf": tok, "name": "Ventilkåpa", "qty": "2", "unit_price": "10"})
    assert verkstad.connect().execute("SELECT qty FROM parts WHERE name='Ventilkåpa'").fetchone()[0] == 0
    # Ta bort raden -> delen återförs till lagret
    lid = verkstad.connect().execute("SELECT id FROM wo_lines WHERE description='Ventilkåpa'").fetchone()[0]
    adm.post(f"/admin/ao/{wid}/rad/{lid}/ta-bort", data={"_csrf": tok})
    assert verkstad.connect().execute("SELECT qty FROM parts WHERE name='Ventilkåpa'").fetchone()[0] == 2
    # Fast pris-raden + Slang ska finnas kvar; ta bort Slang igen så summan i senare test stämmer
    lid = verkstad.connect().execute("SELECT id FROM wo_lines WHERE description='Slang 28x1.5'").fetchone()[0]
    adm.post(f"/admin/ao/{wid}/rad/{lid}/ta-bort", data={"_csrf": tok})

    # Timer start/stopp
    adm.post(f"/admin/ao/{wid}/timer", data={"_csrf": tok})
    c = verkstad.connect()
    assert c.execute("SELECT timer_started_at FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    c.execute("UPDATE work_orders SET timer_started_at=datetime('now','localtime','-25 minutes') WHERE id=?", (wid,))
    c.commit()
    assert "Timer" in adm.get("/admin").get_data(as_text=True)
    adm.post(f"/admin/ao/{wid}/timer", data={"_csrf": tok})
    mins = verkstad.connect().execute("SELECT labor_minutes FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    assert 25 <= mins <= 27, mins

    # Tavla + flytt därifrån tillbaka till tavlan, med e-post vid klart
    assert "Crescent" in adm.get("/admin/tavla").get_data(as_text=True)
    sent.clear()
    r = adm.post(f"/admin/ao/{wid}/status", data={"_csrf": tok, "status": "klar", "back": "tavla", "notify": "1"})
    assert r.headers["Location"].endswith("/admin/tavla")
    _t.sleep(0.4)
    assert any("Klar för hämtning" in m["Subject"] for m in sent), [m["Subject"] for m in sent]

    # Sök: exakt nummer går direkt, fritext listar
    number = verkstad.connect().execute("SELECT number FROM work_orders WHERE id=?", (wid,)).fetchone()[0]
    assert adm.get(f"/admin/sok?q={number}").headers["Location"].endswith(f"/admin/ao/{wid}")
    assert adm.get("/admin/sok?q=Crescent").status_code in (200, 302)

    # Påminnelse: bekräftad bokning inom 26 h, skapad för mer än 2 h sedan
    c = verkstad.connect()
    soon = (verkstad.now() + verkstad.timedelta(hours=20)).strftime("%Y-%m-%dT%H:00")
    cid = c.execute("SELECT id FROM customers WHERE email='cc@test.se'").fetchone()[0]
    c.execute("INSERT INTO bookings(customer_id, category, item, slot_start, status, token, created_at) "
              "VALUES (?, 'Cykel', 'Påminnelsecykel', ?, 'bekraftad', 'tok-rem', datetime('now','localtime','-1 day'))",
              (cid, soon))
    c.commit()
    sent.clear()
    with A.test_request_context(base_url="https://verkstad.example.se"):
        assert verkstad.send_due_reminders() == 1
        assert verkstad.send_due_reminders() == 0  # skickas bara en gång
    _t.sleep(0.4)
    assert any("Påminnelse" in m["Subject"] for m in sent)

    # Startsidan och ärendeuppslag med nummer + hämtkod
    home = pub.get("/").get_data(as_text=True)
    assert "Tjänster och priser" in home and "Punktering fast" in home and "Boka service" in home
    assert "klart senast" in home
    tid = re.search(r'/boka\?tid=([0-9T:%A-F-]+)"', home)
    assert tid, "Förvald tid saknas"
    num, code = verkstad.connect().execute("SELECT number, pickup_code FROM work_orders WHERE id=?", (wid,)).fetchone()
    lt = csrf(pub, "/arende")
    r = pub.post("/arende", data={"_csrf": lt, "number": num.lower().replace("-", " ", 1), "code": code})
    assert r.status_code == 302 and "/s/" in r.headers["Location"], r.status_code
    r = pub.post("/arende", data={"_csrf": lt, "number": num, "code": "0000" if code != "0000" else "1111"})
    assert "Vi hittade inget ärende" in r.get_data(as_text=True)

    # Testmejl via inställningar
    sent.clear()
    r = adm.post("/admin/epost/test", data={"_csrf": tok, "to": "x@test.se"})
    assert len(sent) == 1 and "Testmejl" in sent[0]["Subject"]
    assert "Testmejl" in adm.get("/admin/epost").get_data(as_text=True)

    # API
    key = verkstad.connect().execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]
    assert pub.get("/api/v1/summary").status_code == 401
    assert pub.get("/api/v1/summary", headers={"Authorization": "Bearer fel"}).status_code == 401
    h = {"Authorization": f"Bearer {key}"}
    js = pub.get("/api/v1/summary", headers=h).json
    assert js["work_orders"]["open"] >= 1 and "bookings" in js and js["business"] == "Testverkstan"
    bk = pub.get("/api/v1/bookings?days=14", headers={"X-API-Key": key}).json["bookings"]
    assert bk and all("@" not in (b["customer"] or "") for b in bk)  # ingen e-post i API:t
    assert any(b["customer"] == "Cecilia C." for b in bk)
    wos = pub.get("/api/v1/work-orders", headers=h).json["work_orders"]
    assert any(w["number"] == number for w in wos)
    assert pub.get("/api/v1/work-orders?status=xyz", headers=h).status_code == 400
    assert "parts" in pub.get("/api/v1/low-stock", headers=h).json
    # CORS
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "api", "api_cors_origin": "http://hub.local"})
    r = pub.get("/api/v1/summary", headers=h)
    assert r.headers.get("Access-Control-Allow-Origin") == "http://hub.local"
    assert pub.open("/api/v1/summary", method="OPTIONS").status_code in (200, 204)

    # Nya sidor renderar
    for path in ["/admin/tavla", "/admin/tjanster", "/admin/epost", "/admin/installningar", "/manifest.webmanifest",
                 "/theme.css", "/admin/ao/ny"]:
        assert adm.get(path).status_code == 200, path
    print("V2-TESTER OK")


def test_labels():
    """Etikettskrivare: förhandsvisning, utskrift vid mottagning, platser/delar, test, fel från printservern."""
    verkstad._rl_hits.clear()
    jobs = []
    kit = verkstad.labelkit
    kit.send = lambda url, token, png, label="62", copies=1: (jobs.append((url, token, png[:4], label, copies)) or (True, "Utskriven"))
    kit.test = lambda url, token: (True, "Testetikett utskriven")
    kit.health = lambda url, timeout=2: {"ok": True, "printer_connected": True, "model": "QL-800", "label": "62", "printed": 3}
    adm = login(A.test_client())
    tok = csrf(adm, "/admin/installningar")
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "etikett", "label_printer_url": "http://pi:8631",
                                           "label_printer_token": "hemlig", "label_size": "62", "label_on_intake": "1",
                                           "label_customer_on_intake": "1"})
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "verkstad"})  # kryssruta ej ikryssad = av
    page = adm.get("/admin/installningar").get_data(as_text=True)
    assert "Ansluten" in page and "QL-800" in page, page[:200]
    assert 'value="hemlig"' not in page
    # Tomt nyckelfält behåller nyckeln
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "etikett", "label_printer_url": "http://pi:8631",
                                           "label_printer_token": "", "label_size": "62", "label_on_intake": "1",
                                           "label_customer_on_intake": "1"})
    assert verkstad.connect().execute("SELECT value FROM settings WHERE key='label_printer_token'").fetchone()[0] == "hemlig"

    tok = csrf(adm, "/admin/ao/ny")
    r = adm.post("/admin/ao/ny", data={"_csrf": tok, "new_name": "Dora Dahl", "new_phone": "0731112233",
                                       "category": "Cykel", "item": "Bianchi Sprint", "problem": "Service"})
    assert re.search(r"/admin/ao/\d+$", r.headers["Location"]), r.headers["Location"]  # A4 av -> direkt till ärendet
    wid = int(r.headers["Location"].rsplit("/", 1)[1])
    assert [j[3] for j in jobs] == ["62", "62"] and all(j[2] == b"\x89PNG" and j[1] == "hemlig" for j in jobs)
    for kind, oid in (("ao", wid), ("kund", wid)):
        r = adm.get(f"/admin/etikett/{kind}/{oid}.png")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == b"\x89PNG"
    c = verkstad.connect()
    lid = c.execute("SELECT id FROM locations LIMIT 1").fetchone()[0]
    pid = c.execute("SELECT id FROM parts LIMIT 1").fetchone()[0]
    jobs.clear()
    adm.post(f"/admin/etikett/plats/{lid}", data={"_csrf": tok, "copies": "3"}, headers={"Referer": "/admin/platser"})
    adm.post(f"/admin/etikett/del/{pid}", data={"_csrf": tok})
    assert [j[4] for j in jobs] == [3, 1]
    assert "Skriv ut etikett" in adm.get(f"/admin/lager/{pid}").get_data(as_text=True)
    assert "Objektlapp" in adm.get(f"/admin/ao/{wid}").get_data(as_text=True)
    jobs.clear()
    adm.post("/admin/etikett/platser", data={"_csrf": tok})
    assert len(jobs) == c.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    r = adm.post("/admin/etikett/test", data={"_csrf": tok}, follow_redirects=True)
    assert "Testetikett utskriven" in r.get_data(as_text=True)
    # Fel från printservern visas för användaren
    kit.send = lambda *a, **k: (False, "Skrivaren hittas inte (/dev/usb/lp0).")
    r = adm.post(f"/admin/etikett/ao/{wid}", data={"_csrf": tok}, headers={"Referer": f"/admin/ao/{wid}"}, follow_redirects=True)
    assert "Skrivaren hittas inte" in r.get_data(as_text=True)
    # 62x29: kundsedel skrivs inte ut automatiskt
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "etikett", "label_printer_url": "http://pi:8631",
                                           "label_size": "62x29", "label_on_intake": "1", "label_customer_on_intake": "1"})
    jobs.clear()
    kit.send = lambda url, token, png, label="62", copies=1: (jobs.append(label) or (True, "ok"))
    tok = csrf(adm, "/admin/ao/ny")
    adm.post("/admin/ao/ny", data={"_csrf": tok, "customer_id": "1", "category": "Cykel", "item": "Liten etikett"})
    assert jobs == ["62x29"]
    print("ETIKETT-TESTER OK")


def test_orders():
    """Beställning: inlämning på tid eller enligt överenskommelse, klardatum efter kö, expresstillägg."""
    verkstad._rl_hits.clear()
    from datetime import date as _d
    adm = login(A.test_client())
    pub = A.test_client()
    tok = csrf(adm, "/admin/installningar")
    adm.post("/admin/installningar", data={"_csrf": tok, "section": "bokning", "booking_enabled": "1",
                                           "auto_confirm": "1", "allow_by_agreement": "1",
                                           "open_days": ["1", "2", "3", "4", "5", "6", "7"], "open_from": "08:00",
                                           "open_to": "22:00", "slot_minutes": "60", "slot_capacity": "1",
                                           "days_ahead": "14", "min_notice_hours": "0", "capacity_hours_week": "7",
                                           "default_est_minutes": "60", "express_fee_per_day": "150",
                                           "min_lead_days": "1", "ready_days_window": "21"})
    # 7 h/vecka över 7 dagar = 60 min/dag
    js = pub.get("/klar.json").json
    assert js["per_day_minutes"] == 60
    std = _d.fromisoformat(js["standard"])
    opts = js["options"]
    first = _d.fromisoformat(opts[0]["date"])
    assert first == verkstad.now().date() + verkstad.timedelta(days=1)  # kortaste ledtid 1 dag
    assert opts[0]["fee"] == (std - first).days * 150  # alla dagar är arbetsdagar här
    assert next(o for o in opts if o["date"] == js["standard"])["fee"] == 0
    assert all(o["fee"] == 0 for o in opts if o["date"] >= js["standard"])
    # Längre jobb -> senare standarddatum
    long_id = verkstad.connect().execute("SELECT id FROM services WHERE est_minutes >= 120 LIMIT 1").fetchone()[0]
    js_long = pub.get(f"/klar.json?service_id={long_id}").json
    assert js_long["standard"] > js["standard"]

    # Beställning enligt överenskommelse med expressdatum: första försöket utan godkänt pris nekas
    express_day = opts[0]["date"]
    fee = opts[0]["fee"]
    assert fee > 0
    t = csrf(pub, "/boka")
    data = {"_csrf": t, "name": "Erik Ek", "email": "erik@test.se", "phone": "0701111111", "category": "Cykel",
            "item": "Monark", "description": "Rostig kedja", "dropoff_mode": "overens", "ready_date": express_day,
            "fee_ack": "0", "consent": "1"}
    r = pub.post("/boka", data=data)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Expresstillägget" in body and f'value="{fee}' in body
    t = csrf(pub, "/boka")
    r = pub.post("/boka", data={**data, "_csrf": t, "fee_ack": str(fee)})
    assert r.status_code == 302, r.get_data(as_text=True)[:600]
    b = verkstad.connect().execute("SELECT * FROM bookings WHERE item='Monark'").fetchone()
    assert b["slot_start"] == "" and b["dropoff_mode"] == "overens" and b["status"] == "ny"
    assert b["desired_ready"] == express_day and b["express_fee"] == fee and b["est_minutes"] == 60
    page = pub.get(r.headers["Location"]).get_data(as_text=True)
    assert "Enligt överenskommelse" in page and "Expresstillägg" in page and "Vi kontaktar dig" in page
    # Kön växer: standarddatum flyttas fram
    assert pub.get("/klar.json").json["standard"] >= js["standard"]

    # Admin bestämmer inlämningstid -> bekräftad
    when = (verkstad.now() + verkstad.timedelta(days=1)).strftime("%Y-%m-%dT10:00")
    adm.post(f"/admin/bokningar/{b['id']}/inlamning", data={"_csrf": tok, "when": when}, headers={"Referer": "/admin/bokningar"})
    b2 = verkstad.connect().execute("SELECT status, slot_start, dropoff_mode FROM bookings WHERE id=?", (b["id"],)).fetchone()
    assert tuple(b2) == ("bekraftad", when, "tid")
    assert "Erik Ek" in adm.get("/admin/bokningar").get_data(as_text=True)

    # Ta emot -> utlovat datum och expressrad på ärendet
    r = adm.post(f"/admin/bokningar/{b['id']}/arbetsorder", data={"_csrf": tok})
    wid = int(re.search(r"/admin/ao/(\d+)", r.headers["Location"]).group(1))
    w = verkstad.connect().execute("SELECT due_date, est_minutes FROM work_orders WHERE id=?", (wid,)).fetchone()
    assert w["due_date"] == express_day and w["est_minutes"] == 60
    line = verkstad.connect().execute("SELECT description, unit_price FROM wo_lines WHERE work_order_id=?", (wid,)).fetchone()
    assert line["description"].startswith("Expresstillägg") and line["unit_price"] == fee

    # Inlämning på tid utan expressdatum: standarddatum, ingen avgift, autobekräftad
    html = pub.get("/boka").get_data(as_text=True)
    slots = re.findall(r'name="slot" id="[^"]+" value="([^"]+)"', html)
    t = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = pub.post("/boka", data={"_csrf": t, "name": "Frida F", "email": "frida@test.se", "category": "Elektronik",
                                "item": "Radio", "description": "Brus", "dropoff_mode": "tid", "slot": slots[-1],
                                "consent": "1"})
    assert r.status_code == 302
    b3 = verkstad.connect().execute("SELECT * FROM bookings WHERE item='Radio'").fetchone()
    assert b3["status"] == "bekraftad" and b3["express_fee"] == 0 and b3["desired_ready"] == b3["standard_ready"]
    assert b3["desired_ready"] > b3["slot_start"][:10]
    # Startsidan visar klardatum, tjänsteadmin visar arbetstid
    assert "klart senast" in pub.get("/").get_data(as_text=True)
    assert 'name="est_minutes"' in adm.get("/admin/tjanster").get_data(as_text=True)
    print("BESTÄLLNINGS-TESTER OK")


if __name__ == "__main__":
    test_everything()
    test_v2()
    test_labels()
    test_orders()
    print("ALLA TESTER OK")
