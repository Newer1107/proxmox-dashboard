#!/usr/bin/env python3
"""
TCET Centre of Excellence — Proxmox 1 Node Dashboard
Tailored for: i7-14700 · SK Hynix 1TB NVMe · LVM-thin · PVE 9.2.4
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from datetime import datetime
from typing import Optional

import psutil
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sh(cmd: list[str], timeout: int = 6) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def sh_json(cmd: list[str], timeout: int = 6):
    raw = sh(cmd, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def human(b: float, pad: int = 7) -> str:
    for u in ("B ", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:6.1f} {u}"
        b /= 1024
    return f"{b:6.1f} PB"

def spark(values: deque, width: int = 24) -> str:
    BLOCKS = " ▁▂▃▄▅▆▇█"
    if not values:
        return "─" * width
    vals = list(values)[-width:]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    chars = [BLOCKS[int((v - lo) / span * 8)] for v in vals]
    return " " * (width - len(chars)) + "".join(chars)

def pct_bar(pct: float, width: int = 22) -> tuple[str, str]:
    """Returns (bar_markup, color)"""
    if pct < 60:   color = "#22c55e"   # green
    elif pct < 80: color = "#f59e0b"   # amber
    else:          color = "#ef4444"   # red
    filled = int(pct / 100 * width)
    empty  = width - filled
    bar = f"[{color}]{'█' * filled}[/{color}][#2a2a3a]{'▓' * empty}[/#2a2a3a]"
    return bar, color

def pct_style(pct: float) -> str:
    if pct < 60:   return "#22c55e"
    elif pct < 80: return "#f59e0b"
    return "#ef4444"

DIVIDER = "[#2a3a5a]" + "─" * 46 + "[/#2a3a5a]"
THIN    = "[#1e2a3a]" + "─" * 46 + "[/#1e2a3a]"


# ══════════════════════════════════════════════════════════════════════════════
#  DATA COLLECTOR — tailored to this exact machine
# ══════════════════════════════════════════════════════════════════════════════

class SysData:
    HIST = 60

    def __init__(self):
        # CPU
        self.cpu_pct:      float = 0.0
        self.cpu_cores:    list[float] = []
        self.cpu_freq:     float = 0.0
        self.cpu_temp:     Optional[float] = None
        self.load:         tuple = (0.0, 0.0, 0.0)
        self.cpu_hist:     deque = deque(maxlen=self.HIST)

        # Memory
        self.mem_pct:      float = 0.0
        self.mem_used:     int   = 0
        self.mem_total:    int   = 0
        self.mem_avail:    int   = 0
        self.swap_pct:     float = 0.0
        self.swap_used:    int   = 0
        self.swap_total:   int   = 0
        self.mem_hist:     deque = deque(maxlen=self.HIST)

        # Network  
        self.net_up:       float = 0.0
        self.net_dn:       float = 0.0
        self.net_tx_total: int   = 0
        self.net_rx_total: int   = 0
        self.net_up_hist:  deque = deque(maxlen=self.HIST)
        self.net_dn_hist:  deque = deque(maxlen=self.HIST)
        self._p_sent:      int   = 0
        self._p_recv:      int   = 0
        self._p_time:      float = time.time()

        # Storage
        self.root_pct:     float = 0.0
        self.root_used:    int   = 0
        self.root_total:   int   = 0
        self.lvm_pct:      float = 0.0   # data% from lvs
        self.lvm_used_gb:  float = 0.0
        self.lvm_total_gb: float = 0.0

        # VMs
        self.vms:          list[dict] = []
        self.snapshots:    list[dict] = []

        # System
        self.uptime:       float = 0.0
        self.hostname:     str   = sh(["hostname"]) or "proxmox"
        self.kernel:       str   = sh(["uname", "-r"])
        self.pve_ver:      str   = ""
        self.node_ip:      str   = ""
        self.ts_ip:        str   = ""

        # One-time
        self.pve_ver = "PVE 9.2.4"
        v = sh(["pveversion"])
        if v:
            m = re.search(r'pve-manager[:/\s]+(\S+)', v)
            if m:
                self.pve_ver = f"PVE {m.group(1)}"

    # ── fast (1s) ────────────────────────────────────────────────────────────

    def tick_fast(self):
        self._cpu()
        self._mem()
        self._net()
        self._storage_fast()
        self.uptime = time.time() - psutil.boot_time()

    def _cpu(self):
        self.cpu_pct   = psutil.cpu_percent(interval=None)
        self.cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
        f = psutil.cpu_freq()
        self.cpu_freq  = f.current if f else 0.0
        self.load      = psutil.getloadavg()
        self.cpu_hist.append(self.cpu_pct)
        # temperature — try coretemp first, then acpitz fallback
        try:
            temps = psutil.sensors_temperatures()
            for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "iwlwifi"):
                if key in temps and temps[key]:
                    self.cpu_temp = temps[key][0].current
                    break
            else:
                # try /sys/class/thermal
                raw = sh(["cat", "/sys/class/thermal/thermal_zone0/temp"])
                if raw.isdigit():
                    self.cpu_temp = int(raw) / 1000.0
        except Exception:
            self.cpu_temp = None

    def _mem(self):
        m = psutil.virtual_memory()
        self.mem_pct   = m.percent
        self.mem_used  = m.used
        self.mem_total = m.total
        self.mem_avail = m.available
        s = psutil.swap_memory()
        self.swap_pct  = s.percent
        self.swap_used = s.used
        self.swap_total = s.total
        self.mem_hist.append(self.mem_pct)

    def _net(self):
        now = time.time()
        c   = psutil.net_io_counters()
        dt  = max(now - self._p_time, 0.1)
        self.net_up = (c.bytes_sent - self._p_sent) / dt
        self.net_dn = (c.bytes_recv - self._p_recv) / dt
        self._p_sent = c.bytes_sent
        self._p_recv = c.bytes_recv
        self._p_time = now
        self.net_tx_total = c.bytes_sent
        self.net_rx_total = c.bytes_recv
        # normalise to 125 MB/s (1Gbps) for sparkline scale
        self.net_up_hist.append(min(self.net_up / 125e6 * 100, 100))
        self.net_dn_hist.append(min(self.net_dn / 125e6 * 100, 100))

    def _storage_fast(self):
        try:
            u = psutil.disk_usage("/")
            self.root_pct   = u.percent
            self.root_used  = u.used
            self.root_total = u.total
        except Exception:
            pass

    # ── slow (5s) ────────────────────────────────────────────────────────────

    def tick_slow(self):
        self._lvm()
        self._vms()
        self._snapshots()
        self._net_ips()

    def _lvm(self):
        # Parse lvs for thinpool data%
        out = sh(["lvs", "--noheadings", "-o", "lv_name,data_percent,lv_size",
                  "--units", "g", "pve/data"])
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "data":
                try:
                    self.lvm_pct      = float(parts[1])
                    self.lvm_total_gb = float(parts[2].rstrip("g"))
                    self.lvm_used_gb  = self.lvm_pct / 100 * self.lvm_total_gb
                except Exception:
                    pass

    def _vms(self):
        data = sh_json(["pvesh", "get", "/nodes/localhost/qemu",
                         "--output-format", "json"])
        if data is None:
            # fallback: qm list
            raw = sh(["qm", "list"])
            vms = []
            for line in raw.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    vms.append({"vmid": int(parts[0]),
                                "name": parts[1],
                                "status": parts[2],
                                "maxmem": 0, "cpus": "?"})
            self.vms = sorted(vms, key=lambda x: x["vmid"])
        else:
            self.vms = sorted(data, key=lambda x: x.get("vmid", 0))

    def _snapshots(self):
        snaps = []
        for vm in self.vms:
            vmid = vm.get("vmid")
            if not vmid:
                continue
            raw = sh(["qm", "listsnapshot", str(vmid)])
            for line in raw.splitlines():
                if "current" in line.lower() or not line.strip():
                    continue
                parts = line.split()
                if parts:
                    snaps.append({
                        "vmid": vmid,
                        "name": parts[0].lstrip("└─ ") if parts else "?",
                        "vm_name": vm.get("name", str(vmid)),
                    })
        self.snapshots = snaps

    def _net_ips(self):
        # refresh IPs from ip addr
        out = sh(["ip", "-o", "addr", "show", "vmbr0"])
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        if m:
            self.node_ip = m.group(1)
        out2 = sh(["ip", "-o", "addr", "show", "tailscale0"])
        m2 = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out2)
        if m2:
            self.ts_ip = m2.group(1)

    # ── helpers ──────────────────────────────────────────────────────────────

    def uptime_str(self) -> str:
        s = int(self.uptime)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if d:
            return f"{d}d {h:02d}h {m:02d}m"
        return f"{h:02d}h {m:02d}m {s:02d}s"

    def running_vms(self) -> int:
        return sum(1 for v in self.vms if v.get("status") == "running")


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class HeaderWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        now  = datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
        up   = self.d.uptime_str()
        host = self.d.hostname.upper()
        ver  = self.d.pve_ver
        return (
            f"[bold #f59e0b] ◈  {host}[/bold #f59e0b]"
            f"[#3a4a6a]  │  [/#3a4a6a][#7090b0]{ver}[/#7090b0]"
            f"[#3a4a6a]  │  [/#3a4a6a][#607090]UP {up}[/#607090]"
            f"[#3a4a6a]  │  [/#3a4a6a][bold white]{now}[/bold white]"
        )


class CpuWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        sp   = spark(d.cpu_hist, 24)
        bar, col = pct_bar(d.cpu_pct, 22)
        col  = pct_style(d.cpu_pct)
        temp = f"[#ef4444]{d.cpu_temp:.0f}°C[/#ef4444]" if d.cpu_temp else "[#607090]──°C[/#607090]"
        freq = f"{d.cpu_freq/1000:.2f}GHz" if d.cpu_freq else "──GHz"
        ld   = d.load

        lines = [
            f"[bold #f59e0b]▸ CPU[/bold #f59e0b]  [#607090]i7-14700  20c/28t[/#607090]",
            THIN,
            f"  {bar} [{col}]{d.cpu_pct:5.1f}%[/{col}]",
            f"  [#607090]spark[/#607090] [#22c55e]{sp}[/#22c55e]",
            f"  [#607090]freq[/#607090]  [white]{freq}[/white]   [#607090]temp[/#607090] {temp}",
            f"  [#607090]load[/#607090]  [white]{ld[0]:.2f}[/white][#3a4a6a] · [/#3a4a6a][white]{ld[1]:.2f}[/white][#3a4a6a] · [/#3a4a6a][white]{ld[2]:.2f}[/white]  [#607090]1·5·15m[/#607090]",
        ]

        # Per-core grid — 28 logical CPUs in 4 rows of 7
        cores = (d.cpu_cores or [])[:28]
        MINI = " ▂▄▅▆▇█"
        rows_txt = []
        row_size = 7
        for r in range(0, len(cores), row_size):
            chunk = cores[r:r+row_size]
            row = "  "
            for c in chunk:
                col2 = pct_style(c)
                row += f"[{col2}]{MINI[min(int(c/100*6),6)]}[/{col2}]"
            rows_txt.append(row)

        lines += rows_txt
        return "\n".join(lines)


class MemWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        sp        = spark(d.mem_hist, 24)
        bar_r, _  = pct_bar(d.mem_pct,  22)
        bar_s, _  = pct_bar(d.swap_pct, 22)
        col_r     = pct_style(d.mem_pct)
        col_s     = pct_style(d.swap_pct)
        used_h    = human(d.mem_used).strip()
        total_h   = human(d.mem_total).strip()
        avail_h   = human(d.mem_avail).strip()
        su_h      = human(d.swap_used).strip()
        st_h      = human(d.swap_total).strip()

        return "\n".join([
            f"[bold #f59e0b]▸ MEMORY[/bold #f59e0b]  [#607090]32 GB DDR5[/#607090]",
            THIN,
            f"  [#607090]ram [/#607090]{bar_r} [{col_r}]{d.mem_pct:5.1f}%[/{col_r}]",
            f"  [#607090]     [/#607090][white]{used_h}[/white][#3a4a6a] used · [/#3a4a6a][white]{avail_h}[/white][#3a4a6a] free · [/#3a4a6a][#607090]{total_h} total[/#607090]",
            f"  [#607090]swap[/#607090]{bar_s} [{col_s}]{d.swap_pct:5.1f}%[/{col_s}]",
            f"  [#607090]     [/#607090][white]{su_h}[/white][#3a4a6a] used · [/#3a4a6a][#607090]{st_h} total[/#607090]",
            f"  [#607090]hist[/#607090] [#22c55e]{sp}[/#22c55e]",
        ])


class VmWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d     = self.d
        total = len(d.vms)
        run   = d.running_vms()
        stop  = total - run

        lines = [
            f"[bold #f59e0b]▸ VIRTUAL MACHINES[/bold #f59e0b]  "
            f"[#22c55e]{run} running[/#22c55e][#3a4a6a] · [/#3a4a6a][#607090]{stop} stopped[/#607090]",
            THIN,
        ]

        VM_NAMES = {
            100: "Main-VM",
            101: "coding-platform",
            102: "staging-vm",
            104: "104",
            106: "db-backup",
        }

        for vm in d.vms[:8]:
            vmid   = vm.get("vmid", "?")
            name   = (vm.get("name") or VM_NAMES.get(vmid) or f"vm-{vmid}")[:18]
            status = vm.get("status", "?")
            if status == "running":
                icon  = "[#22c55e]▶[/#22c55e]"
                scol  = "#22c55e"
            else:
                icon  = "[#ef4444]■[/#ef4444]"
                scol  = "#607090"

            maxmem = vm.get("maxmem", 0)
            mem_s  = human(maxmem).strip() if maxmem else "    ─"
            cpus   = vm.get("cpus", "─")

            lines.append(
                f"  {icon} [bold white]{vmid:>3}[/bold white]"
                f"  [{scol}]{name:<18}[/{scol}]"
                f"  [#607090]cpu[/#607090][white]{cpus}[/white]"
                f"  [#607090]mem[/#607090][white]{mem_s}[/white]"
            )

        return "\n".join(lines)


class NetworkWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d      = self.d
        sp_up  = spark(d.net_up_hist, 20)
        sp_dn  = spark(d.net_dn_hist, 20)
        up_s   = human(d.net_up).strip()
        dn_s   = human(d.net_dn).strip()
        tx_s   = human(d.net_tx_total).strip()
        rx_s   = human(d.net_rx_total).strip()

        return "\n".join([
            f"[bold #f59e0b]▸ NETWORK[/bold #f59e0b]",
            THIN,
            f"  [bold #22c55e]▲[/bold #22c55e] [white]{up_s:>12}/s[/white]  [#22c55e]{sp_up}[/#22c55e]",
            f"  [bold #38bdf8]▼[/bold #38bdf8] [white]{dn_s:>12}/s[/white]  [#38bdf8]{sp_dn}[/#38bdf8]",
            f"  [#607090]TX[/#607090] [white]{tx_s:>12}[/white]  [#607090]total sent[/#607090]",
            f"  [#607090]RX[/#607090] [white]{rx_s:>12}[/white]  [#607090]total recv[/#607090]",
            THIN,
            f"  [#607090]vmbr0     [/#607090][white]{d.node_ip:<18}[/white][#607090]1 Gb[/#607090]",
            f"  [#607090]tailscale [/#607090][#a855f7]{d.ts_ip:<18}[/#a855f7][#607090]VPN[/#607090]",
        ])


class StorageWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        bar_r, _ = pct_bar(d.root_pct, 20)
        bar_l, _ = pct_bar(d.lvm_pct, 20)
        cr = pct_style(d.root_pct)
        cl = pct_style(d.lvm_pct)
        r_used  = human(d.root_used).strip()
        r_total = human(d.root_total).strip()
        l_used  = f"{d.lvm_used_gb:.1f}GB" if d.lvm_used_gb else "─"
        l_total = f"{d.lvm_total_gb:.1f}GB" if d.lvm_total_gb else "─"

        return "\n".join([
            f"[bold #f59e0b]▸ STORAGE[/bold #f59e0b]  [#607090]SK Hynix NVMe 1TB[/#607090]",
            THIN,
            f"  [#607090]root  [/#607090]{bar_r} [{cr}]{d.root_pct:5.1f}%[/{cr}]",
            f"  [#607090]ext4  [/#607090][white]{r_used}[/white][#3a4a6a] / [/#3a4a6a][#607090]{r_total}[/#607090]",
            THIN,
            f"  [#607090]data  [/#607090]{bar_l} [{cl}]{d.lvm_pct:5.1f}%[/{cl}]",
            f"  [#607090]thin  [/#607090][white]{l_used}[/white][#3a4a6a] / [/#3a4a6a][#607090]{l_total}[/#607090]",
            f"  [#607090]pool  [/#607090][white]pve/data[/white][#3a4a6a] · [/#3a4a6a][#607090]LVM-thin[/#607090]",
        ])


class SnapshotWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold #f59e0b]▸ SNAPSHOTS[/bold #f59e0b]  [#607090]{len(d.snapshots)} total[/#607090]",
            THIN,
        ]
        if not d.snapshots:
            lines.append("  [#607090]no snapshots found[/#607090]")
        else:
            for sn in d.snapshots[:6]:
                lines.append(
                    f"  [#a855f7]◉[/#a855f7]  [white]VM {sn['vmid']}[/white]"
                    f"  [#607090]{sn['vm_name'][:12]:<12}[/#607090]"
                    f"  [#f59e0b]{sn['name'][:18]}[/#f59e0b]"
                )
        return "\n".join(lines)


class CentreWidget(Static):
    """Identity centrepiece — TCET COE branding with warning."""

    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d
        self._tick = 0

    def render(self) -> str:
        self._tick += 1
        # pulse the outer glow char between two shades
        amber_hi = "#f59e0b"
        amber_lo = "#92600a"
        glow     = amber_hi if self._tick % 2 == 0 else amber_lo

        d   = self.d
        run = d.running_vms()
        tot = len(d.vms)

        # live status line inside the seal
        if run == tot and tot > 0:
            status_col = "#22c55e"
            status_txt = f"ALL {tot} VMs OPERATIONAL"
        elif run == 0:
            status_col = "#ef4444"
            status_txt = "ALL VMs OFFLINE"
        else:
            status_col = "#f59e0b"
            status_txt = f"{run}/{tot} VMs RUNNING"

        ip = d.node_ip

        L = "[" + glow + "]"
        R = "[/" + glow + "]"
        # box width = 44 inner chars
        W = 44

        def centre(text: str, width: int = W) -> str:
            # strip Rich markup for length calc
            plain = re.sub(r'\[[^\]]+\]', '', text)
            pad   = max(0, width - len(plain))
            lp    = pad // 2
            rp    = pad - lp
            return " " * lp + text + " " * rp

        border_h  = "═" * W
        border_top    = f"{L}╔{border_h}╗{R}"
        border_bot    = f"{L}╚{border_h}╝{R}"
        side          = lambda inner: f"{L}║{R}{inner}{L}║{R}"

        blank  = side(" " * W)

        # row contents (44 wide)
        r_label = centre(f"[bold #607090]T C E T[/bold #607090]")
        r_sub1  = centre(f"[#3a5a8a]Centre of Excellence[/#3a5a8a]")
        r_rule  = centre(f"[{amber_lo}]{'─' * 30}[/{amber_lo}]")
        r_title = centre(f"[bold white]P R O X M O X   1[/bold white]")
        r_node  = centre(f"[#607090]node · {ip}[/#607090]")
        r_blank2 = centre("")
        r_status = centre(f"[bold {status_col}]{status_txt}[/bold {status_col}]")

        # Warning — small, tracked, understated
        r_warn_a = centre(f"[dim #8a6a20]{'·' * 34}[/dim #8a6a20]")
        r_warn_b = centre(f"[dim #a07828]⚠  AUTHORISED ACCESS ONLY  ⚠[/dim #a07828]")
        r_warn_c = centre(f"[dim #607050]DO NOT POWER OFF OR MODIFY[/dim #607050]")
        r_warn_d = centre(f"[dim #607050]WITHOUT APPROVAL FROM COE ADMIN[/dim #607050]")
        r_warn_e = centre(f"[dim #8a6a20]{'·' * 34}[/dim #8a6a20]")

        rows = [
            border_top,
            blank,
            side(r_label),
            side(r_sub1),
            side(r_rule),
            side(r_title),
            side(r_node),
            side(r_blank2),
            side(r_status),
            blank,
            side(r_warn_a),
            side(r_warn_b),
            side(r_warn_c),
            side(r_warn_d),
            side(r_warn_e),
            blank,
            border_bot,
        ]

        return "\n".join(rows)


class FooterWidget(Static):
    def __init__(self, d: SysData, **kw):
        super().__init__(**kw)
        self.d = d
        self._t = 0

    def render(self) -> str:
        self._t += 1
        d   = self.d
        run = d.running_vms()
        tot = len(d.vms)
        k   = d.kernel[:32] if d.kernel else "─"
        return (
            f"[#607090] kernel[/#607090] [white]{k}[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [#607090]vms[/#607090] [#22c55e]{run}[/#22c55e][#3a4a6a]/[/#3a4a6a][white]{tot}[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [#607090]root[/#607090] [white]{d.root_pct:.0f}%[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [#607090]lvm-thin[/#607090] [white]{d.lvm_pct:.1f}%[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [#607090]mem[/#607090] [white]{d.mem_pct:.0f}%[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [#607090]cpu[/#607090] [white]{d.cpu_pct:.0f}%[/white]"
            f"  [#3a4a6a]│[/#3a4a6a]  [dim #607090]tick #{self._t}  ·  1s refresh[/dim #607090]"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════════

class TCETDashboard(App):
    CSS = """
    Screen {
        background: #0a0e1a;
        color: #c0cce0;
        layout: vertical;
    }

    #header {
        height: 1;
        background: #0d1220;
        border-bottom: tall #1e2a3a;
        padding: 0 2;
        content-align: left middle;
    }

    #footer {
        height: 1;
        background: #0d1220;
        border-top: tall #1e2a3a;
        padding: 0 2;
        content-align: left middle;
    }

    #body {
        height: 1fr;
        layout: horizontal;
    }

    /* ── Left column ── */
    #left {
        width: 26;
        layout: vertical;
        border-right: tall #1e2a3a;
    }

    #cpu-pane {
        height: 1fr;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #mem-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #vm-pane {
        height: 1fr;
        padding: 1 1;
    }

    /* ── Centre column ── */
    #centre {
        width: 1fr;
        layout: vertical;
        align: center middle;
        content-align: center middle;
        padding: 0 2;
    }

    #centre-widget {
        width: auto;
        height: auto;
        content-align: center middle;
        align: center middle;
    }

    /* ── Right column ── */
    #right {
        width: 28;
        layout: vertical;
        border-left: tall #1e2a3a;
    }

    #net-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #storage-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #snap-pane {
        height: 1fr;
        padding: 1 1;
    }
    """

    TITLE = "TCET COE · Proxmox 1"
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.d = SysData()
        self._slow = 0
        # prime data
        self.d.tick_fast()
        self.d.tick_slow()

    def compose(self) -> ComposeResult:
        yield HeaderWidget(self.d, id="header")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield CpuWidget(self.d, id="cpu-pane")
                yield MemWidget(self.d, id="mem-pane")
                yield VmWidget(self.d, id="vm-pane")
            with Vertical(id="centre"):
                yield CentreWidget(self.d, id="centre-widget")
            with Vertical(id="right"):
                yield NetworkWidget(self.d, id="net-pane")
                yield StorageWidget(self.d, id="storage-pane")
                yield SnapshotWidget(self.d, id="snap-pane")
        yield FooterWidget(self.d, id="footer")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.d.tick_fast()
        self._slow += 1
        if self._slow % 5 == 0:
            self.d.tick_slow()
        for wid in ("header", "cpu-pane", "mem-pane", "vm-pane",
                    "centre-widget", "net-pane", "storage-pane",
                    "snap-pane", "footer"):
            try:
                self.query_one(f"#{wid}").refresh()
            except Exception:
                pass


def _claim_tty() -> bool:
    """Fork + setsid to make /dev/tty1 the controlling terminal.

    systemd services with StandardInput=tty/StandardOutput=tty do NOT
    set the controlling terminal, so /dev/tty fails with ENXIO.
    Textual's LinuxDriver opens /dev/tty and silently breaks without one.
    """
    try:
        pid = os.fork()
    except OSError:
        return False

    if pid > 0:
        def _forward(signum, frame):
            try:
                os.kill(pid, signum)
            except OSError:
                pass

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _forward)

        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os._exit(0)

    os.setsid()

    for dev in ("/dev/tty1", "/dev/tty"):
        try:
            fd = os.open(dev, os.O_RDWR)
        except OSError:
            continue
        os.dup2(fd, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        if fd > 2:
            os.close(fd)
        return True

    return False


def main():
    _claim_tty()
    app = TCETDashboard()
    app.run()


if __name__ == "__main__":
    main()
