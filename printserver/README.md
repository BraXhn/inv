# Printserver för Brother QL (Raspberry Pi)

Gör en Raspberry Pi till printserver för en Brother QL-etikettskrivare. Verkstad skickar färdiga etiketter som bilder (objektlapp, kundens mottagningssedel och etiketter för lagerplatser och delar) och de skrivs ut direkt, utan utskriftsdialog.

Servern använder [brother_ql_next](https://pypi.org/project/brother-ql-next/), den underhållna fortsättningen på brother_ql. Den pratar direkt med skrivaren, så ingen drivrutin från Brother behövs. Det är bra, eftersom Brother inte har några drivrutiner för Pi.

## Det här behövs
- Raspberry Pi med Raspberry Pi OS (Bookworm eller senare). Pi Zero 2 W, 3, 4 eller 5 fungerar alla.
- Brother QL: 500, 550, 560, 570, 580N, 600, 650TD, 700, 710W, 720NW, 800, 810W, 820NWB, 1050, 1060N, 1100 eller 1115NWB.
- **62 mm löpande rulle (DK-22205)** rekommenderas. Då får både objektlappen och kundens sedel plats. 62 × 29 mm stansade etiketter (DK-11209) fungerar för objekt- och lageretiketter.

## Installera (ett kommando)
Anslut skrivaren med USB och kör på Pi:n:
```bash
curl -fsSL https://raw.githubusercontent.com/BraXhn/inv/main/printserver/install.sh | sudo bash
```
Skriptet frågar efter modell, rulle och anslutning (USB eller IP-adress). Det installerar allt och startar tjänsten. Till sist visar det **adress och nyckel**. Klistra in dem i Verkstad under **Inställningar → Etikettskrivare** och klicka **Skriv ut testetikett**.

Utan frågor: `curl -fsSL …/install.sh | sudo PRINTER_MODEL=QL-820NWB LABEL=62 PRINTER=usb bash`

## Viktigt: Editor Lite
QL-700, QL-800 och QL-810W har en knapp som heter **Editor Lite**. Lampan bredvid den ska vara **släckt**. Om den lyser visar sig skrivaren som ett USB-minne istället för som en skrivare, och då fungerar ingen utskrift. Håll in knappen tills lampan slocknar.

## Felsökning
| Problem | Åtgärd |
|---|---|
| "Skrivaren hittas inte (/dev/usb/lp0)" | Kontrollera att skrivaren är på, att USB-kabeln sitter och att Editor Lite är av. Kör `ls /dev/usb/`. |
| "Fel nyckel" | Kopiera `PRINT_TOKEN` från `/etc/verkstad-print.env` till Verkstad. |
| Fel storlek eller ingenting skrivs ut | `LABEL` i `/etc/verkstad-print.env` måste stämma med rullen i skrivaren. Kör sedan `sudo systemctl restart verkstad-print`. |
| Se loggen | `journalctl -u verkstad-print -f` |
| Status | `curl http://<pi>:8631/health` |

## Nätverksskrivare (QL-720NW, 810W, 820NWB …)
Ange skrivarens IP-adress när skriptet frågar om anslutning, så används `tcp://IP:9100`. Observera att skrivaren då inte kan rapportera fel som slut på etiketter, eftersom den inte svarar på statusfrågor över nätverket.

## Säkerhet
- Utskrift kräver nyckeln (`Authorization: Bearer …`). `/health` visar bara status, inga hemligheter.
- Servern ska bara nås från hemmanätet. Exponera den inte via Cloudflare-tunneln. Verkstad på Unraid anropar den direkt på LAN.
- Tjänsten körs som en egen användare utan inloggning och har bara åtkomst till skrivaren (gruppen `lp`).

## Avinstallera
```bash
sudo systemctl disable --now verkstad-print
sudo rm -rf /opt/verkstad-print /etc/systemd/system/verkstad-print.service /etc/verkstad-print.env /etc/udev/rules.d/60-brother-ql.rules
```
