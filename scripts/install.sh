#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# TCET Centre of Excellence — Proxmox 1 Dashboard Installer
# Run as root on your Proxmox node: bash scripts/install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[ ok ]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
die()     { echo -e "${RED}[fail]${RESET}  $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="/opt/proxmox-dashboard"
BIN="/usr/local/bin/proxmox-dashboard"

echo ""
echo -e "${BOLD}${CYAN}  TCET Centre of Excellence · Proxmox 1 Dashboard${RESET}"
echo -e "${DIM}  ─────────────────────────────────────────────────${RESET}"
echo ""

[[ $EUID -ne 0 ]] && die "Must be run as root."

# ── 1. Dependencies ──────────────────────────────────────────────────────────
info "Installing Python dependencies..."
apt-get update -qq 2>/dev/null
apt-get install -y -qq python3 python3-pip python3-psutil lm-sensors 2>/dev/null || true
pip3 install --break-system-packages -q "textual>=0.47.0" "psutil>=5.9" 2>/dev/null
ok "Dependencies ready."

# Optional: probe sensors (non-fatal)
sensors-detect --auto >/dev/null 2>&1 || true

# ── 2. Copy files ────────────────────────────────────────────────────────────
info "Installing dashboard to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}/src"
cp "${ROOT_DIR}/src/dashboard.py" "${INSTALL_DIR}/src/dashboard.py"
chmod 644 "${INSTALL_DIR}/src/dashboard.py"

cat > "${BIN}" << 'WRAPPER'
#!/usr/bin/env bash
export TERM="${TERM:-linux}"
export PYTHONUNBUFFERED=1
printf '\033[?25l'
clear
exec /usr/bin/python3 /opt/proxmox-dashboard/src/dashboard.py "$@"
WRAPPER
chmod +x "${BIN}"
ok "Dashboard installed → ${BIN}"

# ── 3. Systemd units ─────────────────────────────────────────────────────────
info "Installing systemd units..."

cat > /etc/systemd/system/proxmox-dashboard.service << 'UNIT'
[Unit]
Description=TCET COE Proxmox 1 Node Dashboard
After=network.target pve-cluster.service pvedaemon.service
Wants=network.target

[Service]
Type=simple
User=root
Group=root
StandardInput=tty
StandardOutput=tty
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVTDisallocate=yes
Environment=TERM=linux
Environment=HOME=/root
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/local/bin/proxmox-dashboard
Restart=always
RestartSec=3s
StartLimitIntervalSec=60s
StartLimitBurst=5
ProtectKernelTunables=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
UNIT

# Suppress getty on tty1
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/override.conf << 'OVERRIDE'
[Unit]
Conflicts=getty@tty1.service
[Service]
ExecStart=
ExecStart=-/bin/true
Restart=no
OVERRIDE

ok "Systemd units installed."

# ── 4. Enable & start ────────────────────────────────────────────────────────
info "Enabling and starting dashboard..."
systemctl daemon-reload
systemctl stop getty@tty1.service 2>/dev/null || true
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable proxmox-dashboard.service
systemctl restart proxmox-dashboard.service
sleep 2

if systemctl is-active --quiet proxmox-dashboard.service; then
    ok "proxmox-dashboard.service is RUNNING."
else
    warn "Service may still be starting. Check: journalctl -u proxmox-dashboard -n 30"
fi

# ── 5. Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  Installation complete.${RESET}"
echo ""
echo -e "  ${CYAN}Monitor${RESET}     tty1 — dashboard is live on the physical screen"
echo -e "  ${CYAN}Shell${RESET}       Ctrl+Alt+F2  →  tty2 (normal login)"
echo -e "  ${CYAN}Status${RESET}      systemctl status proxmox-dashboard"
echo -e "  ${CYAN}Logs${RESET}        journalctl -u proxmox-dashboard -f"
echo -e "  ${CYAN}Restart${RESET}     systemctl restart proxmox-dashboard"
echo -e "  ${CYAN}Uninstall${RESET}   bash ${ROOT_DIR}/scripts/uninstall.sh"
echo ""
