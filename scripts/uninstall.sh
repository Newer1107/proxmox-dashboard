#!/usr/bin/env bash
# Removes the Proxmox dashboard and restores tty1 login.

set -euo pipefail
[[ $EUID -ne 0 ]] && echo "Run as root." && exit 1

echo "Stopping and disabling service..."
systemctl stop proxmox-dashboard.service   2>/dev/null || true
systemctl disable proxmox-dashboard.service 2>/dev/null || true

echo "Removing files..."
rm -f /etc/systemd/system/proxmox-dashboard.service
rm -f /etc/systemd/system/getty@tty1.service.d/override.conf
rmdir --ignore-fail-on-non-empty /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true
rm -f /usr/local/bin/proxmox-dashboard
rm -rf /opt/proxmox-dashboard

echo "Restoring getty on tty1..."
systemctl daemon-reload
systemctl enable getty@tty1.service
systemctl start getty@tty1.service

echo "Done. tty1 will show the normal login prompt again."
