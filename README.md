# Verkstad

Arbetsordrar, bokningar, kunder och lager för en cykel- och elektronikverkstad. Allt körs i en enda container på Unraid, och kunderna når den via Cloudflare Tunnel.

## Vad den gör

**För kunden (publikt)**
- **Bokning** av inlämningstid med lediga tider, öppettider, kapacitet per tid och stängda datum.
- **Statussida** med hemlig länk. Kunden följer bokningen och sedan reparationen: förlopp, händelser och kostnad hittills.
- **Offertgodkännande med ett klick.** Ingen telefon-ping-pong.
- **Avbokning** själv via samma länk.

**För dig (admin)**
- **Arbetsordrar** med statusflöde, felbeskrivning, diagnos, interna anteckningar som aldrig syns för kunden, arbetstid och timpris, utlovat datum och förvaringsplats.
- **Inlämningschecklistor** per kategori. Cykel: ramnummer, skador, lås. Elektronik: serienummer, laddare, dataförlust.
- **Delar på ärendet** dras automatiskt från lagret och återförs om raden tas bort. Fria rader finns för sådant som köps in till ett enskilt ärende.
- **Lager** med lagerplatser, beställningspunkt, inköpslista per leverantör, lagervärde, inventering, historik och CSV-export.
- **NFC och QR** för lagerplatser, delar och inlämnade objekt. Skanna en låda så ser du innehållet, och du kan dra ett uttag med −1 direkt.
- **Etikettark** att skriva ut för platser, delar och öppna arbetsordrar.
- **Inlämningskvitto och specifikation** för utskrift, med QR-kod till kundens statussida.
- **Kalenderprenumeration** (.ics), så att bokningarna syns i telefonens kalender.
- **Översikt** över status, kommande bokningar, saldon som behöver fyllas på, försenade och bortglömda ärenden samt månadens intäkt.
- **GDPR**: export av kunddata, anonymisering och automatisk gallring av inaktiva kunder.
- **Daglig backup** till `/data/backups` som kan laddas ner från gränssnittet.
- **E-post** till kunden och dig (valfritt, via SMTP) vid bokning, bekräftelse, offert och när reparationen är klar.

## Installation på Unraid

### Alternativ A: GitHub + Unraid-mall (rekommenderas, ger enkla uppdateringar)
1. Koden ligger i `BraXhn/inv`.
2. GitHub Actions kör testerna och publicerar en image till `ghcr.io/braxhn/inv:latest`.
   - Kontrollera under *Packages → inv → Package settings* att paketet är publikt, annars måste Unraid logga in med `docker login ghcr.io` och en PAT med `read:packages`.
3. Kopiera `unraid/verkstad.xml` till `/boot/config/plugins/dockerMan/templates-user/my-verkstad.xml` (redan ifylld för `BraXhn`).
4. I Unraid: **Docker → Add Container → Template: verkstad**. Fyll i lösenord och publik adress och klicka Apply.

### Alternativ B: bygg lokalt med Compose Manager
1. Kopiera mappen till t.ex. `/mnt/user/appdata/verkstad-src`.
2. Redigera `docker-compose.yml`: lösenord, `PUBLIC_URL` och eventuellt SMTP.
3. Kör `docker compose up -d --build` i mappen, eller lägg till stacken i Compose Manager-pluginet.

Öppna sedan `http://<unraid-ip>:8080/admin` och logga in.

**Glömt att sätta lösenord?** Då genereras ett, och det finns i `/mnt/user/appdata/verkstad/admin_password`.

## Cloudflare Tunnel och Access (viktigt)

Bokningen ska vara öppen för alla, men admin ska ha två lås: appens eget lösenord och Cloudflare Access framför.

1. **Tunnel:** I Zero Trust, gå till *Networks → Tunnels* och lägg till en publik hostname, t.ex. `verkstad.dindoman.se`. Service: `http://<unraid-ip>:8080`, eller `http://verkstad:8080` om cloudflared ligger i samma Docker-nätverk.
2. **Access:** Gå till *Access → Applications → Add → Self-hosted* och skapa en applikation med dessa sökvägar:
   - `verkstad.dindoman.se/admin*`
   - `verkstad.dindoman.se/login*`
   - `verkstad.dindoman.se/t/*`

   Som policy väljer du *Allow* för din egen e-post, med engångskod. Skydda **inte** `/boka`, `/s/*`, `/static/*` och `/kalender/*`. Kunder och telefonens kalenderapp måste nå dem utan inloggning. Statuslänkar och kalenderlänk skyddas istället av långa slumpade nycklar.
3. **Rekommenderat:** Lägg en regel för rate limiting under *Security → WAF* på `/boka` med metoden POST, till exempel 10 förfrågningar per minut per IP. Appen begränsar också själv till 5 bokningar per timme och IP.
4. Ställ in **Publik adress** (`PUBLIC_URL`) på din domän **innan** du skriver NFC-taggar. Taggarna innehåller hela adressen. Om miljövariabeln är satt vinner den över värdet under Inställningar.

## NFC-taggar
1. Skapa platserna (t.ex. "Hylla B · Låda 3") under **Platser**.
2. Öppna platsen och kopiera länken i rutan "NFC / QR".
3. I mobilappen **NFC Tools** väljer du *Skriv → Lägg till post → URL* och klistrar in länken. Skriv sedan till taggen.
4. Använd taggar märkta "on-metal" på metallhyllor.

Etikettarken under **Platser → Etiketter** ger en QR-kod med samma länk. Då fungerar även telefoner utan NFC.

## Säkerhet i appen
- CSRF-skydd på alla formulär, strikt CSP (inga externa skript eller inline-skript), `X-Frame-Options: DENY` och `no-store` på känsliga sidor.
- Begränsning av inloggning (8 försök per 15 min och IP), honeypot och rate limiting på bokningen.
- Rätt klient-IP via `CF-Connecting-IP`. Säker cookie automatiskt över https.
- Containern kör som `99:100` (nobody:users) och inte som root.
- Interna anteckningar och interna logghändelser visas aldrig på kundsidor. Det finns test som verifierar det.

## Data och backup
- Allt ligger i `/data` (appdata). Databasen är SQLite i WAL-läge.
- Varje dygn tas en backup till `/data/backups`. De senaste 14 sparas, vilket kan ändras med `BACKUP_KEEP`.
- **Återställning:** stoppa containern, kopiera en backupfil till `/data/verkstad.db`, ta bort `verkstad.db-wal` och `verkstad.db-shm`, och starta igen.

## Miljövariabler
| Variabel | Standard | Beskrivning |
|---|---|---|
| `ADMIN_USER` | `admin` | Användarnamn |
| `ADMIN_PASSWORD` | genereras | Lösenord |
| `PUBLIC_URL` | – | Publik adress via tunneln |
| `SECRET_KEY` | genereras i `/data` | Nyckel för sessioner |
| `TZ` | `Europe/Stockholm` | Tidszon |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | – | E-post (valfritt) |
| `NOTIFY_EMAIL` | kontakt-e-post | Vart dina notiser skickas |
| `BACKUP_ENABLED` / `BACKUP_KEEP` | `1` / `14` | Backup |
| `TRUST_PROXY` | `1` | Lita på proxyheaders från cloudflared |
| `PUID` / `PGID` | `99` / `100` | Användare i containern |

## Utveckling
```bash
pip install -r requirements.txt
python tests/test_flows.py          # end-to-end-test av alla flöden
DATA_DIR=./data ADMIN_PASSWORD=dev python app/app.py
```
