#!/usr/bin/env bash
# BraXhn Verkstad – installera printservern för Brother QL på Raspberry Pi OS (Bookworm/Trixie).
#   curl -fsSL https://raw.githubusercontent.com/BraXhn/inv/main/printserver/install.sh | sudo bash
# Välj modell utan frågor:  ... | sudo PRINTER_MODEL=QL-820NWB bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/BraXhn/inv/main/printserver"
DIR=/opt/verkstad-print
ENV_FILE=/etc/verkstad-print.env
SERVICE=verkstad-print
USERNAME=verkstad-print

if [[ $EUID -ne 0 ]]; then echo "Kör med sudo: sudo bash install.sh"; exit 1; fi

ask() {  # ask "Fråga" standardvärde  – fungerar även när skriptet körs via curl | bash
  local q="$1" def="$2" ans=""
  if [[ -r /dev/tty ]]; then read -r -p "$q [$def]: " ans </dev/tty || true; fi
  echo "${ans:-$def}"
}

echo "== BraXhn Verkstad printserver =="
MODEL="${PRINTER_MODEL:-}"
if [[ -z "$MODEL" ]]; then
  echo "Modeller: QL-500 QL-550 QL-560 QL-570 QL-580N QL-600 QL-650TD QL-700 QL-710W QL-720NW"
  echo "          QL-800 QL-810W QL-820NWB QL-1050 QL-1060N QL-1100 QL-1100NWB QL-1115NWB"
  MODEL=$(ask "Vilken skrivare har du?" "QL-800")
fi
LABEL="${LABEL:-$(ask "Etikettrulle (62 = 62 mm löpande, 62x29, 29x90 …)" "62")}"
CONN="${PRINTER:-$(ask "Anslutning: usb eller IP-adress för nätverksskrivare" "usb")}"
if [[ "$CONN" == "usb" ]]; then BACKEND=linux_kernel; PRINTER="file:///dev/usb/lp0"
elif [[ "$CONN" == tcp://* || "$CONN" == file://* ]]; then PRINTER="$CONN"; BACKEND=$([[ "$CONN" == tcp://* ]] && echo network || echo linux_kernel)
else BACKEND=network; PRINTER="tcp://$CONN:9100"; fi

echo "-> Installerar paket"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip fonts-dejavu-core libusb-1.0-0 curl >/dev/null

echo "-> Skapar användare och mapp"
id -u "$USERNAME" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --groups lp "$USERNAME"
usermod -aG lp "$USERNAME"
mkdir -p "$DIR"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [[ -f "$SRC_DIR/printserver.py" ]]; then cp "$SRC_DIR/printserver.py" "$DIR/printserver.py"
else curl -fsSL "$REPO_RAW/printserver.py" -o "$DIR/printserver.py"; fi

echo "-> Installerar brother_ql_next i $DIR/venv"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" uninstall -y -q brother_ql >/dev/null 2>&1 || true   # gamla paketet krockar
"$DIR/venv/bin/pip" install -q "brother_ql_next>=0.12,<1" "pillow>=10"

echo "-> USB-behörighet för Brother (udev)"
cat > /etc/udev/rules.d/60-brother-ql.rules <<'RULES'
# Brother (04f9): skrivaren som /dev/usb/lp* och rå USB, läs/skriv för gruppen lp
SUBSYSTEM=="usbmisc", KERNEL=="lp*", ATTRS{idVendor}=="04f9", GROUP="lp", MODE="0660"
SUBSYSTEM=="usb", ATTR{idVendor}=="04f9", GROUP="lp", MODE="0660"
RULES
udevadm control --reload-rules && udevadm trigger || true

if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  cat > "$ENV_FILE" <<CONF
# BraXhn Verkstad printserver. Starta om efter ändring: sudo systemctl restart $SERVICE
PRINT_TOKEN=$TOKEN
PRINTER_MODEL=$MODEL
PRINTER_BACKEND=$BACKEND
PRINTER=$PRINTER
LABEL=$LABEL
PORT=8631
BROTHER_QL=$DIR/venv/bin/brother_ql
CONF
else
  echo "   $ENV_FILE finns redan – behåller nyckeln. Uppdaterar modell, etikett och anslutning."
  sed -i "s|^PRINTER_MODEL=.*|PRINTER_MODEL=$MODEL|; s|^PRINTER_BACKEND=.*|PRINTER_BACKEND=$BACKEND|; s|^PRINTER=.*|PRINTER=$PRINTER|; s|^LABEL=.*|LABEL=$LABEL|" "$ENV_FILE"
fi
chown root:"$USERNAME" "$ENV_FILE"; chmod 640 "$ENV_FILE"

echo "-> Tjänst ($SERVICE)"
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=BraXhn Verkstad printserver (Brother QL)
After=network-online.target
Wants=network-online.target

[Service]
User=$USERNAME
Group=lp
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/venv/bin/python $DIR/printserver.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $SERVICE
systemctl restart $SERVICE
sleep 2

IP=$(hostname -I | awk '{print $1}')
TOKEN=$(grep '^PRINT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
echo
echo "== Klart =="
curl -fsS "http://127.0.0.1:8631/health" && echo || echo "Tjänsten svarar inte än – se: journalctl -u $SERVICE -f"
echo
echo "Fyll i under Verkstad → Inställningar → Etikettskrivare:"
echo "  Adress: http://$IP:8631"
echo "  Nyckel: $TOKEN"
echo
echo "Tips: QL-700/800/810W har knappen Editor Lite – lampan ska vara SLÄCKT, annars syns skrivaren som ett USB-minne."
