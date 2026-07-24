# Proxmox Node Dashboard

A full-screen TUI dashboard that automatically displays on the physical monitor
(tty1) of a Proxmox VE node — no login required, no desktop environment needed.

```
╔══════════════════════════════════════════════════════════════════╗
║  󰨠  PROXMOX01   Proxmox VE 8.1   up 14d 02h   Friday 14:23:05  ║
╠═══════════════════════════╦══════════════════════════════════════╣
║ CPU           87.2%       ║ DISKS                                ║
║ ▁▂▄▅▆▇▇▇▇█   1 min load  ║  /          ██░░░░░░  23%  120GB     ║
║ MEMORY        52.1%       ║  /var/lib   ████░░░░  51%  1.8TB     ║
║ Swap           8.3%       ║ VMs (6)                              ║
║ NETWORK                   ║  ▶ 100  web01   running              ║
║ ▲  12.4 MB/s              ║  ▶ 101  db01    running              ║
║ ▼  88.1 MB/s              ║  ■ 102  backup  stopped              ║
║ ZFS POOLS                 ║ LXC (4)                              ║
║  rpool  ONLINE  2.7T used ║  ▶ 200  nginx   running              ║
╚═══════════════════════════╩══════════════════════════════════════╝
```

## Requirements

- Proxmox VE 7.x or 8.x
- Python 3.9+
- `pip3 install textual psutil`

## Installation

```bash
# On your Proxmox node, as root:
git clone https://github.com/you/proxmox-dashboard.git
cd proxmox-dashboard
bash scripts/install.sh
```

The installer will:
1. Install Python dependencies (`textual`, `psutil`)
2. Copy the dashboard to `/opt/proxmox-dashboard/`
3. Install a `systemd` service that starts on boot
4. Suppress the tty1 getty (login prompt) so the dashboard takes over
5. Start the dashboard immediately

## Usage After Install

| Action | Command |
|--------|---------|
| Check service status | `systemctl status proxmox-dashboard` |
| View logs | `journalctl -u proxmox-dashboard -f` |
| Restart dashboard | `systemctl restart proxmox-dashboard` |
| Get a shell | `Ctrl+Alt+F2` (tty2 — normal login) |
| Return to dashboard | `Ctrl+Alt+F1` (tty1) |
| Uninstall | `bash scripts/uninstall.sh` |

## How It Works

### tty1 Takeover

Normally `getty@tty1.service` owns tty1 and shows the login prompt.
The installer drops a systemd override at:

```
/etc/systemd/system/getty@tty1.service.d/override.conf
```

This tells systemd that `getty@tty1` and `proxmox-dashboard` conflict,
and replaces getty's `ExecStart` with `/bin/true` (a no-op). Our service
then claims tty1 exclusively using:

```ini
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
```

No autologin, no PAM, no shell — just our Python process writing directly
to the virtual console.

### Other TTYs

`tty2`–`tty6` are completely untouched. `Ctrl+Alt+F2` through `F6` give
you normal login prompts. SSH is also unaffected.

### Data Sources

| Widget | Source |
|--------|--------|
| CPU, Memory, Network, Disk | `psutil` (Python) |
| CPU temperature | `psutil.sensors_temperatures()` |
| ZFS pools | `zpool list` |
| VM status | `pvesh get /nodes/localhost/qemu` → `qm list` fallback |
| LXC status | `pvesh get /nodes/localhost/lxc` → `pct list` fallback |
| Proxmox version | `pveversion --verbose` |

### Refresh Intervals

- **1 second**: CPU, memory, network speeds, uptime, clock
- **5 seconds**: Disks, ZFS pools, VM list, LXC list

This keeps CPU overhead minimal while keeping the dashboard current.

## Extending the Dashboard

Each widget is a self-contained `Static` subclass in `src/dashboard.py`.

To add a new widget:

```python
class BackupWidget(Static):
    def __init__(self, data: SystemData, **kwargs):
        super().__init__(**kwargs)
        self.data = data

    def render(self) -> str:
        # query backup status via pvesh or vzdump logs
        return "[bold]╔══ BACKUPS ══╗[/bold]\n  ..."
```

Then add it to the `compose()` method in `ProxmoxDashboard`:

```python
yield BackupWidget(self.data, id="backups")
```

And add `"backups"` to the `refresh_data()` widget list.

### Ideas for additional widgets

- **Backup status** — parse `/var/log/vzdump/` or `pvesh get /nodes/localhost/tasks`
- **Replication** — `pvesh get /nodes/localhost/replication`
- **Cluster health** — `pvecm status`
- **SMART disk health** — `smartctl -H /dev/sdX`
- **Ceph status** — `ceph status`
- **Recent syslog** — tail `/var/log/syslog`

## Troubleshooting

**Dashboard doesn't appear on tty1 after boot:**
```bash
systemctl status proxmox-dashboard
journalctl -u proxmox-dashboard -n 50
```

**Black screen / terminal garbage:**
The dashboard needs `TERM=linux` on the virtual console.
The wrapper script `/usr/local/bin/proxmox-dashboard` sets this.
Check: `echo $TERM` from tty1.

**pvesh permission errors:**
The service runs as root, which should have full pvesh access.
If you see 403 errors, check `/etc/pve/user.cfg`.

**Textual version compatibility:**
If you see import errors, update textual:
```bash
pip3 install --break-system-packages --upgrade "textual>=0.47.0"
```
