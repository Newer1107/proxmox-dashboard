#!/usr/bin/env python3
"""
Proxmox Node Dashboard — NOC-style operations view
Aggregated metrics, history graphs, alerts, activity feed.
No individual VM/container listings.
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
from textual.widgets import Static


# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OK   = "#22c55e"
WARN = "#f59e0b"
CRIT = "#ef4444"
INFO = "#38bdf8"
DIM  = "#607090"
DIM2 = "#3a4a6a"
AMBER_HI = "#f59e0b"
AMBER_LO = "#92600a"
BG       = "#0a0e1a"
BG2      = "#0d1220"
BORDER   = "#1e2a3a"
GLYPH    = "#2a3a5a"

DIVIDER = f"[{DIM2}]{'─' * 46}[/{DIM2}]"
THINLN  = f"[{GLYPH}]{'─' * 46}[/{GLYPH}]"

BLOCKS = " ▁▂▃▄▅▆▇█"
MINI   = " ▂▄▅▆▇█"


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
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def human(b: float) -> str:
    for u in ("B ", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:>5.1f} {u}"
        b /= 1024
    return f"{b:>5.1f} PB"


def human_int(b: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{int(b)} {u}"
        b /= 1024
    return f"{int(b)} PB"


def pct_bar(pct: float, width: int = 12, style: bool = True) -> str:
    filled = max(0, min(int(pct / 100 * width), width))
    empty = width - filled
    color = "#22c55e" if pct < 60 else "#f59e0b" if pct < 80 else "#ef4444"
    bar = "█" * filled + "░" * empty
    return f"[{color}]{bar}[/{color}]" if style else bar


def pct_color(pct: float) -> str:
    return OK if pct < 60 else WARN if pct < 80 else CRIT


def spark(values: list[float], width: int = 20) -> str:
    if not values:
        return "─" * width
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    return "".join(BLOCKS[min(int((v - lo) / span * 8), 8)] for v in vals)


def spark_color(values: list[float], width: int = 20, color: str = OK) -> str:
    raw = spark(values, width)
    return f"[{color}]{raw}[/{color}]"


def compact_cores(cores: list[float], per_row: int = 8) -> list[str]:
    rows = []
    for i in range(0, len(cores), per_row):
        chunk = cores[i : i + per_row]
        line = "  "
        for c in chunk:
            col = pct_color(c)
            idx = min(int(c / 100 * 5), 5)
            line += f"[{col}]{MINI[idx]}[/{col}]"
        rows.append(line)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  HISTORY RING BUFFER
# ══════════════════════════════════════════════════════════════════════════════

class History:
    """Fixed-length time-series buffer with render helpers."""

    def __init__(self, maxlen: int = 60):
        self._data: deque[float] = deque(maxlen=maxlen)

    def add(self, v: float) -> None:
        self._data.append(v)

    def get(self) -> list[float]:
        return list(self._data)

    def last(self, default: float = 0.0) -> float:
        return self._data[-1] if self._data else default

    def spark(self, width: int = 20) -> str:
        if not self._data:
            return f"[{DIM}]{'─' * width}[/{DIM}]"
        vals = list(self._data)[-width:]
        lo, hi = min(vals), max(vals)
        span = hi - lo or 1
        raw = "".join(BLOCKS[min(int((v - lo) / span * 8), 8)] for v in vals)
        col = OK if self.last() < 60 else WARN if self.last() < 80 else CRIT
        return f"[{col}]{raw}[/{col}]"


# ══════════════════════════════════════════════════════════════════════════════
#  ALERT & EVENT TRACKING
# ══════════════════════════════════════════════════════════════════════════════

class Alert:
    severity: str  # "ok" | "warn" | "crit"
    message: str
    time: str

    def __init__(self, severity: str, message: str):
        self.severity = severity
        self.message = message
        self.time = datetime.now().strftime("%H:%M")


class Event:
    message: str
    time: str
    kind: str  # "info" | "warn" | "ok"

    def __init__(self, message: str, kind: str = "info"):
        self.message = message
        self.kind = kind
        self.time = datetime.now().strftime("%H:%M")


# ══════════════════════════════════════════════════════════════════════════════
#  DATA COLLECTOR
# ══════════════════════════════════════════════════════════════════════════════

class NodeData:
    """Aggregated node data collector — fast + slow ticks."""

    HIST_LEN = 60

    def __init__(self):
        # ── CPU ──
        self.cpu_pct: float = 0.0
        self.cpu_cores: list[float] = []
        self.cpu_freq: float = 0.0
        self.cpu_temp: Optional[float] = None
        self.load: tuple = (0.0, 0.0, 0.0)
        self.cpu_hist = History(self.HIST_LEN)

        # ── Memory ──
        self.mem_pct: float = 0.0
        self.mem_used: int = 0
        self.mem_total: int = 0
        self.mem_avail: int = 0
        self.mem_cached: int = 0
        self.mem_buffers: int = 0
        self.swap_pct: float = 0.0
        self.swap_used: int = 0
        self.swap_total: int = 0
        self.mem_hist = History(self.HIST_LEN)
        self.swap_hist = History(self.HIST_LEN)

        # ── Network ──
        self.net_up: float = 0.0
        self.net_dn: float = 0.0
        self.net_tx_total: int = 0
        self.net_rx_total: int = 0
        self.net_up_hist = History(self.HIST_LEN)
        self.net_dn_hist = History(self.HIST_LEN)
        self.net_errs: int = 0
        self._p_sent: int = 0
        self._p_recv: int = 0
        self._p_errin: int = 0
        self._p_errout: int = 0
        self._p_time: float = time.time()

        # ── Storage ──
        self.root_pct: float = 0.0
        self.root_used: int = 0
        self.root_total: int = 0
        self.lvm_pct: float = 0.0
        self.lvm_used_gb: float = 0.0
        self.lvm_total_gb: float = 0.0
        self.disk_r_hist = History(self.HIST_LEN)
        self.disk_w_hist = History(self.HIST_LEN)
        self._p_disk_r: int = 0
        self._p_disk_w: int = 0
        self._p_disk_time: float = time.time()

        # ── ZFS ──
        self.zfs_pools: list[dict] = []
        self.zfs_arc_max: int = 0
        self.zfs_arc_used: int = 0

        # ── Virtualization (aggregated) ──
        self.vms_running: int = 0
        self.vms_stopped: int = 0
        self.vms_total: int = 0
        self.vm_total_vcpus: int = 0
        self.vm_total_maxmem: int = 0
        self.vm_total_mem: int = 0
        self.vm_cpu_pct: float = 0.0
        self.vm_mem_pct: float = 0.0
        self.vm_disk_r: int = 0
        self.vm_disk_w: int = 0
        self.vm_net_in: int = 0
        self.vm_net_out: int = 0
        self.vm_cpu_hist = History(self.HIST_LEN)
        self.vm_mem_hist = History(self.HIST_LEN)

        # ── LXCs (aggregated) ──
        self.lxc_running: int = 0
        self.lxc_stopped: int = 0
        self.lxc_total: int = 0
        self.lxc_cpu_pct: float = 0.0
        self.lxc_mem_pct: float = 0.0

        # ── System ──
        self.uptime: float = 0.0
        self.hostname: str = sh(["hostname"]) or "proxmox"
        self.kernel: str = sh(["uname", "-r"])
        self.pve_ver: str = ""
        self.node_ip: str = ""
        self.ts_ip: str = ""

        # ── Alerts & Events ──
        self.alerts: list[Alert] = []
        self.events: deque[Event] = deque(maxlen=20)
        self._last_vm_count: int = 0
        self._last_lxc_count: int = 0

        # ── Status ──
        self.overall_status: str = "ok"  # ok | warn | crit
        self.tick_n: int = 0

        # One-time probes
        v = sh(["pveversion"])
        if v:
            m = re.search(r'pve-manager[:/\s]+(\S+)', v)
            if m:
                self.pve_ver = f"PVE {m.group(1)}"
        self._net_ips()

    # ── Fast tick (1s) ─────────────────────────────────────────────────

    def tick_fast(self):
        self.tick_n += 1
        self._cpu()
        self._mem()
        self._net()
        self._disk_io()
        self.uptime = time.time() - psutil.boot_time()

    def _cpu(self):
        self.cpu_pct = psutil.cpu_percent(interval=None)
        self.cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
        f = psutil.cpu_freq()
        self.cpu_freq = f.current if f else 0.0
        self.load = psutil.getloadavg()
        self.cpu_hist.add(self.cpu_pct)
        try:
            temps = psutil.sensors_temperatures()
            for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "iwlwifi"):
                if key in temps and temps[key]:
                    self.cpu_temp = temps[key][0].current
                    break
            else:
                raw = sh(["cat", "/sys/class/thermal/thermal_zone0/temp"])
                if raw.isdigit():
                    self.cpu_temp = int(raw) / 1000.0
        except Exception:
            self.cpu_temp = None

    def _mem(self):
        m = psutil.virtual_memory()
        self.mem_pct = m.percent
        self.mem_used = m.used
        self.mem_total = m.total
        self.mem_avail = m.available
        self.mem_cached = m.cached if hasattr(m, 'cached') else 0
        self.mem_buffers = m.buffers if hasattr(m, 'buffers') else 0
        s = psutil.swap_memory()
        self.swap_pct = s.percent
        self.swap_used = s.used
        self.swap_total = s.total
        self.mem_hist.add(self.mem_pct)
        self.swap_hist.add(self.swap_pct)

    def _net(self):
        now = time.time()
        c = psutil.net_io_counters()
        dt = max(now - self._p_time, 0.1)
        self.net_up = (c.bytes_sent - self._p_sent) / dt
        self.net_dn = (c.bytes_recv - self._p_recv) / dt
        self.net_errs = (c.errin - self._p_errin) + (c.errout - self._p_errout)
        self._p_sent = c.bytes_sent
        self._p_recv = c.bytes_recv
        self._p_errin = c.errin
        self._p_errout = c.errout
        self._p_time = now
        self.net_tx_total = c.bytes_sent
        self.net_rx_total = c.bytes_recv
        # normalise to 125 MB/s (1 Gbps) for sparkline scale
        self.net_up_hist.add(min(self.net_up / 125e6 * 100, 100))
        self.net_dn_hist.add(min(self.net_dn / 125e6 * 100, 100))

    def _disk_io(self):
        try:
            now = time.time()
            c = psutil.disk_io_counters()
            if c:
                dt = max(now - self._p_disk_time, 0.1)
                r = (c.read_bytes - self._p_disk_r) / dt
                w = (c.write_bytes - self._p_disk_w) / dt
                # Normalise to 500 MB/s for sparkline scale
                self.disk_r_hist.add(min(r / 500e6 * 100, 100))
                self.disk_w_hist.add(min(w / 500e6 * 100, 100))
                self._p_disk_r = c.read_bytes
                self._p_disk_w = c.write_bytes
                self._p_disk_time = now
        except Exception:
            pass

    # ── Slow tick (5s) ─────────────────────────────────────────────────

    def tick_slow(self):
        self._storage()
        self._zfs()
        self._vms_aggregate()
        self._lxcs_aggregate()
        self._net_ips()
        self._update_alerts()
        self._check_events()

    def _storage(self):
        try:
            u = psutil.disk_usage("/")
            self.root_pct = u.percent
            self.root_used = u.used
            self.root_total = u.total
        except Exception:
            pass
        # LVM thin pool usage
        out = sh(["lvs", "--noheadings", "-o", "lv_name,data_percent,lv_size",
                  "--units", "g", "pve/data"])
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "data":
                try:
                    self.lvm_pct = float(parts[1])
                    self.lvm_total_gb = float(parts[2].rstrip("g"))
                    self.lvm_used_gb = self.lvm_pct / 100 * self.lvm_total_gb
                except Exception:
                    pass

    def _zfs(self):
        # ZFS pool list
        raw = sh(["zpool", "list", "-H", "-o", "name,health,capacity,allocated,size"])
        pools = []
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                pools.append({
                    "name": parts[0],
                    "health": parts[1],
                    "capacity": parts[2],
                    "used": parts[3],
                    "total": parts[4],
                })
        self.zfs_pools = pools
        # ARC stats
        arc = sh(["cat", "/proc/spl/kstat/zfs/arcstats"])
        for line in arc.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                if "size" == parts[0]:
                    self.zfs_arc_used = int(parts[2])
                elif "c_max" == parts[0]:
                    self.zfs_arc_max = int(parts[2])

    def _vms_aggregate(self):
        data = sh_json(["pvesh", "get", "/nodes/localhost/qemu",
                         "--output-format", "json"])
        if data is None:
            raw = sh(["qm", "list"])
            # fallback parsing
            vms_data = []
            for line in raw.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    vms_data.append({"vmid": parts[0], "name": parts[1],
                                     "status": parts[2]})
            data = vms_data

        if not data:
            return

        running = [v for v in data if v.get("status") == "running"]
        self.vms_running = len(running)
        self.vms_stopped = len(data) - self.vms_running
        self.vms_total = len(data)

        if running:
            self.vm_total_vcpus = sum(v.get("cpus", 0) or 0 for v in running)
            self.vm_total_maxmem = sum(v.get("maxmem", 0) or 0 for v in running)
            self.vm_total_mem = sum(v.get("mem", 0) or 0 for v in running)
            avg_cpu = sum(v.get("cpu", 0) or 0 for v in running) / len(running)
            self.vm_cpu_pct = avg_cpu * 100
            if self.vm_total_maxmem > 0:
                self.vm_mem_pct = self.vm_total_mem / self.vm_total_maxmem * 100
            self.vm_cpu_hist.add(self.vm_cpu_pct)
            self.vm_mem_hist.add(self.vm_mem_pct)
            # Aggregate I/O (newer PVE versions provide these)
            self.vm_disk_r = sum(v.get("diskread", 0) or 0 for v in running)
            self.vm_disk_w = sum(v.get("diskwrite", 0) or 0 for v in running)
            self.vm_net_in = sum(v.get("netin", 0) or 0 for v in running)
            self.vm_net_out = sum(v.get("netout", 0) or 0 for v in running)
        else:
            self.vm_total_vcpus = 0
            self.vm_total_maxmem = 0
            self.vm_total_mem = 0
            self.vm_cpu_pct = 0
            self.vm_mem_pct = 0
            self.vm_disk_r = 0
            self.vm_disk_w = 0
            self.vm_net_in = 0
            self.vm_net_out = 0

    def _lxcs_aggregate(self):
        data = sh_json(["pvesh", "get", "/nodes/localhost/lxc",
                         "--output-format", "json"])
        if data is None:
            raw = sh(["pct", "list"])
            lxc_data = []
            for line in raw.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    lxc_data.append({"vmid": parts[0], "name": parts[1],
                                     "status": parts[2]})
            data = lxc_data

        if not data:
            return

        running = [c for c in data if c.get("status") == "running"]
        self.lxc_running = len(running)
        self.lxc_stopped = len(data) - self.lxc_running
        self.lxc_total = len(data)

        if running:
            total_cpu = sum(c.get("cpu", 0) or 0 for c in running)
            self.lxc_cpu_pct = total_cpu / len(running) * 100
            total_mem = sum(c.get("mem", 0) or 0 for c in running)
            total_maxmem = sum(c.get("maxmem", 0) or 0 for c in running)
            self.lxc_mem_pct = (total_mem / total_maxmem * 100) if total_maxmem else 0

    def _net_ips(self):
        out = sh(["ip", "-o", "addr", "show", "vmbr0"])
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        if m:
            self.node_ip = m.group(1)
        out2 = sh(["ip", "-o", "addr", "show", "tailscale0"])
        m2 = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out2)
        if m2:
            self.ts_ip = m2.group(1)

    # ── Alerts ────────────────────────────────────────────────────────

    def _update_alerts(self):
        alerts = []
        if self.cpu_pct > 85:
            alerts.append(Alert("crit", f"CPU {self.cpu_pct:.0f}%"))
        elif self.cpu_pct > 70:
            alerts.append(Alert("warn", f"CPU {self.cpu_pct:.0f}%"))

        if self.mem_pct > 85:
            alerts.append(Alert("crit", f"Memory {self.mem_pct:.0f}%"))
        elif self.mem_pct > 75:
            alerts.append(Alert("warn", f"Memory {self.mem_pct:.0f}%"))

        if self.swap_pct > 50:
            alerts.append(Alert("warn", f"Swap {self.swap_pct:.0f}%"))

        if self.root_pct > 85:
            alerts.append(Alert("crit", f"Root disk {self.root_pct:.0f}%"))
        elif self.root_pct > 75:
            alerts.append(Alert("warn", f"Root disk {self.root_pct:.0f}%"))

        if self.lvm_pct > 85:
            alerts.append(Alert("crit", f"LVM-thin {self.lvm_pct:.0f}%"))
        elif self.lvm_pct > 75:
            alerts.append(Alert("warn", f"LVM-thin {self.lvm_pct:.0f}%"))

        if self.cpu_temp and self.cpu_temp > 80:
            alerts.append(Alert("warn", f"Temp {self.cpu_temp:.0f}°C"))

        for pool in self.zfs_pools:
            if pool.get("health") != "ONLINE":
                alerts.append(Alert("crit", f"ZFS {pool['name']}: {pool['health']}"))

        self.alerts = alerts[:8]  # max 8 alerts shown
        if not self.alerts:
            self.alerts = [Alert("ok", "All systems healthy")]

        # Overall status
        sevs = {a.severity for a in self.alerts}
        self.overall_status = "crit" if "crit" in sevs else "warn" if "warn" in sevs else "ok"

    # ── Events ────────────────────────────────────────────────────────

    def _check_events(self):
        # Track VM count changes
        if self._last_vm_count > 0 and self.vms_running != self._last_vm_count:
            verb = "up" if self.vms_running > self._last_vm_count else "down"
            self.events.append(Event(
                f"VM count changed: {self._last_vm_count} → {self.vms_running} ({verb})",
                "info"))
        self._last_vm_count = self.vms_running

        # Track LXC count changes
        if self._last_lxc_count > 0 and self.lxc_running != self._last_lxc_count:
            verb = "up" if self.lxc_running > self._last_lxc_count else "down"
            self.events.append(Event(
                f"LXC count changed: {self._last_lxc_count} → {self.lxc_running} ({verb})",
                "info"))
        self._last_lxc_count = self.lxc_running

        # Check for new alerts
        for a in self.alerts:
            if a.severity != "ok":
                self.events.append(Event(a.message, a.severity))

    # ── Helpers ───────────────────────────────────────────────────────

    def uptime_str(self) -> str:
        s = int(self.uptime)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if d:
            return f"{d}d {h:02d}h {m:02d}m"
        return f"{h:02d}h {m:02d}m {s:02d}s"

    def overall_status_dot(self) -> str:
        col = {"ok": OK, "warn": WARN, "crit": CRIT}[self.overall_status]
        return f"[{col}]●[/{col}]"

    def overall_status_text(self) -> str:
        return {"ok": "HEALTHY", "warn": "WARNING", "crit": "CRITICAL"}[self.overall_status]


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class HeaderWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        now = datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
        return (
            f"[bold {AMBER_HI}] ◈  {self.d.hostname.upper()}[/bold {AMBER_HI}]"
            f"[{DIM2}]  │  [/{DIM2}][{INFO}]{self.d.pve_ver}[/{INFO}]"
            f"[{DIM2}]  │  [/{DIM2}][{DIM}]UP {self.d.uptime_str()}[/{DIM}]"
            f"[{DIM2}]  │  [/{DIM2}][bold white]{now}[/bold white]"
            f"  {self.d.overall_status_dot()} [{pct_color({'ok': 10, 'warn': 50, 'crit': 90}[self.d.overall_status])}]{self.d.overall_status_text()}[/]"
        )


class CpuPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        bar = pct_bar(d.cpu_pct, 14)
        col = pct_color(d.cpu_pct)
        sp = d.cpu_hist.spark(18)
        temp = f"[{CRIT}]{d.cpu_temp:.0f}°C[/{CRIT}]" if d.cpu_temp else f"[{DIM}]──°C[/{DIM}]"
        freq = f"{d.cpu_freq/1000:.2f}GHz" if d.cpu_freq else "──GHz"
        ld = d.load
        return "\n".join([
            f"[bold {AMBER_HI}]▸ CPU[/bold {AMBER_HI}]  [{DIM}]i7-14700  20c/28t[/{DIM}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  {bar} [{col}]{d.cpu_pct:>5.1f}%[/{col}]",
            f"  [{DIM}]freq[/{DIM}]  [white]{freq}[/white]   [{DIM}]temp[/{DIM}] {temp}",
            f"  [{DIM}]load[/{DIM}]  {ld[0]:.2f}[{DIM2}]·[/{DIM2}]{ld[1]:.2f}[{DIM2}]·[/{DIM2}]{ld[2]:.2f}",
            f"  [{DIM}]hist[/{DIM}]  {sp}",
        ])


class MemPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        bar = pct_bar(d.mem_pct, 14)
        col = pct_color(d.mem_pct)
        sp = d.mem_hist.spark(18)
        sbar = pct_bar(d.swap_pct, 14)
        scol = pct_color(d.swap_pct)
        used = human(d.mem_used)
        total = human(d.mem_total)
        return "\n".join([
            f"[bold {AMBER_HI}]▸ MEMORY[/bold {AMBER_HI}]  [{DIM}]32 GB DDR5[/{DIM}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  {bar} [{col}]{d.mem_pct:>5.1f}%[/{col}]",
            f"  [{DIM}]used[/{DIM}] [white]{used}[/white]  [{DIM}]of[/{DIM}] [white]{total}[/white]",
            f"  [{DIM}]swap[/{DIM}] {sbar} [{scol}]{d.swap_pct:>5.1f}%[/{scol}]",
            f"  [{DIM}]hist[/{DIM}]  {sp}",
        ])


class PerCorePanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        cores = (self.d.cpu_cores or [])[:28]
        if not cores:
            return f"[{DIM}]per-core n/a[/{DIM}]"
        rows = compact_cores(cores, 8)
        ncols = min(len(cores), 28)
        return "\n".join([
            f"[bold {AMBER_HI}]▸ PER-CORE[/bold {AMBER_HI}]  [{DIM}]{ncols} threads[/{DIM}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            *rows,
        ])


class CenterGraphs(Static):
    """Large central sparkline area — CPU, Mem, Net, Disk."""

    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def _labeled_spark(self, label: str, hist: History, unit: str,
                        width: int, color: str = OK) -> list[str]:
        val = hist.last()
        raw = hist.spark(width - 2)
        col = pct_color(val) if isinstance(val, (int, float)) and val <= 100 else color
        bar = pct_bar(val, 10) if val <= 100 else f"[white]{human(val)}[/white]"
        return [
            f"[bold {AMBER_HI}]{label:<6}[/bold {AMBER_HI}]"
            f"[{col}]{bar:>12}[/{col}]  "
            f"[white]{val if isinstance(val, (int, float)) else 0:>6.1f}[/white]"
            f"[{DIM}] {unit}[/{DIM}]",
            f"  {raw}",
        ]

    def render(self) -> str:
        d = self.d
        # Calculate dynamic width from terminal
        # Textual doesn't expose width easily in render(), so use fixed
        w = 100
        lines = []
        lines += self._labeled_spark("CPU", d.cpu_hist, "%", w)
        lines.append("")
        lines += self._labeled_spark("MEM", d.mem_hist, "%", w)
        lines.append("")

        # Network: dual sparkline
        up_val = d.net_up_hist.last()
        dn_val = d.net_dn_hist.last()
        up_s = human(d.net_up)
        dn_s = human(d.net_dn)
        lines.append(
            f"[bold {AMBER_HI}]NETW  [/bold {AMBER_HI}]"
            f"  [bold {OK}]▲[/bold {OK}] [white]{up_s:>9}/s[/white]"
            f"  [bold {INFO}]▼[/bold {INFO}] [white]{dn_s:>9}/s[/white]"
        )
        lines.append(f"  [{OK}]{d.net_up_hist.spark(w)}[/{OK}]")
        lines.append(f"  [{INFO}]{d.net_dn_hist.spark(w)}[/{INFO}]")
        lines.append("")

        # Disk: dual sparkline
        dr_val = d.disk_r_hist.last()
        dw_val = d.disk_w_hist.last()
        lines.append(
            f"[bold {AMBER_HI}]DISK  [/bold {AMBER_HI}]"
            f"  [{OK}]R[/{OK}] [white]{human(d.disk_r_hist.last() * 5e6) if d.disk_r_hist.last() else '  0.0 B '}/s[/white]"
            f"  [{INFO}]W[/{INFO}] [white]{human(d.disk_w_hist.last() * 5e6) if d.disk_w_hist.last() else '  0.0 B '}/s[/white]"
        )
        lines.append(f"  [{OK}]{d.disk_r_hist.spark(w)}[/{OK}]")
        lines.append(f"  [{INFO}]{d.disk_w_hist.spark(w)}[/{INFO}]")

        return "\n".join(lines)


class VmSummaryPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        vm_cpu_sp = d.vm_cpu_hist.spark(14)
        vm_mem_sp = d.vm_mem_hist.spark(14)
        vm_run_col = OK if d.vms_running > 0 else DIM
        lxc_run_col = OK if d.lxc_running > 0 else DIM
        return "\n".join([
            f"[bold {AMBER_HI}]▸ VIRTUALIZATION[/bold {AMBER_HI}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  [{vm_run_col}]▶[/{vm_run_col}] [white]VMs[/white]    [{vm_run_col}]{d.vms_running} running[/{vm_run_col}]"
            f"[{DIM2}] / [/{DIM2}][{DIM}]{d.vms_stopped} stopped[/{DIM}]  [{DIM}]{d.vms_total} total[/{DIM}]",
            f"  [{lxc_run_col}]▶[/{lxc_run_col}] [white]LXCs[/white]   [{lxc_run_col}]{d.lxc_running} running[/{lxc_run_col}]"
            f"[{DIM2}] / [/{DIM2}][{DIM}]{d.lxc_stopped} stopped[/{DIM}]  [{DIM}]{d.lxc_total} total[/{DIM}]",
            f"",
            f"  [{DIM}]vCPUs[/{DIM}]    [white]{d.vm_total_vcpus:>4}[/white]  [{DIM}]allocated[/{DIM}]",
            f"  [{DIM}]RAM[/{DIM}]     [white]{human_int(d.vm_total_maxmem):>9}[/white]  [{DIM}]allocated[/{DIM}]",
            f"  [{DIM}]CPU[/{DIM}]     [{pct_color(d.vm_cpu_pct)}]{d.vm_cpu_pct:>5.1f}%[/]  {vm_cpu_sp}",
            f"  [{DIM}]MEM[/{DIM}]     [{pct_color(d.vm_mem_pct)}]{d.vm_mem_pct:>5.1f}%[/]  {vm_mem_sp}",
        ])


class StoragePanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold {AMBER_HI}]▸ STORAGE[/bold {AMBER_HI}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  [{DIM}]root[/{DIM}]  {pct_bar(d.root_pct, 12)} [{pct_color(d.root_pct)}]{d.root_pct:.1f}%[/]",
            f"  [{DIM}]ext4[/{DIM}]  [white]{human(d.root_used)}[/] [{DIM2}]/[/{DIM2}] [{DIM}]{human(d.root_total)}[/{DIM}]",
        ]
        if d.lvm_pct:
            lines += [
                f"  [{DIM}]data[/{DIM}]  {pct_bar(d.lvm_pct, 12)} [{pct_color(d.lvm_pct)}]{d.lvm_pct:.1f}%[/]",
                f"  [{DIM}]thin[/{DIM}]  [white]{d.lvm_used_gb:.1f}G[/] [{DIM2}]/[/{DIM2}] [{DIM}]{d.lvm_total_gb:.0f}G[/{DIM}]",
            ]
        # ZFS pools
        if d.zfs_pools:
            lines.append("")
            for pool in d.zfs_pools[:2]:
                hcol = OK if pool["health"] == "ONLINE" else CRIT
                lines.append(
                    f"  [{hcol}]◈[/{hcol}] [{DIM}]{pool['name']:<10}[/{DIM}]"
                    f"[{hcol}]{pool['health']:<8}[/{hcol}]"
                    f"[white]{pool['capacity']:>3}%[/white]"
                )
            if d.zfs_arc_max:
                arc_pct = d.zfs_arc_used / d.zfs_arc_max * 100 if d.zfs_arc_max else 0
                lines.append(
                    f"  [{DIM}]ARC[/{DIM}]   {pct_bar(arc_pct, 8)} [{pct_color(arc_pct)}]{arc_pct:.0f}%[/]"
                    f"  [{DIM}]{human_int(d.zfs_arc_used)}[/{DIM}]"
                )
        return "\n".join(lines)


class NetworkPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        up_s = human(d.net_up)
        dn_s = human(d.net_dn)
        tx_s = human_int(d.net_tx_total)
        rx_s = human_int(d.net_rx_total)
        err_col = CRIT if d.net_errs > 0 else DIM
        net_up_sp = d.net_up_hist.spark(14)
        net_dn_sp = d.net_dn_hist.spark(14)
        return "\n".join([
            f"[bold {AMBER_HI}]▸ NETWORK[/bold {AMBER_HI}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  [bold {OK}]▲[/bold {OK}] [white]{up_s:>9}/s[/white]  [{OK}]{net_up_sp}[/{OK}]",
            f"  [bold {INFO}]▼[/bold {INFO}] [white]{dn_s:>9}/s[/white]  [{INFO}]{net_dn_sp}[/{INFO}]",
            f"  [{DIM}]TX[/{DIM}] [white]{tx_s:>10}[/white]  [{DIM}]total[/{DIM}]",
            f"  [{DIM}]RX[/{DIM}] [white]{rx_s:>10}[/white]  [{DIM}]total[/{DIM}]",
            f"  [{DIM}]err[/{DIM}] [{err_col}]{d.net_errs:>9}[/{err_col}]  [{DIM}]pkts[/{DIM}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
            f"  [{DIM}]vmbr0    [/{DIM}][white]{d.node_ip:<14}[/white][{DIM}]1G[/{DIM}]",
            f"  [{DIM}]tscale   [/{DIM}][#a855f7]{d.ts_ip:<14}[/#a855f7][{DIM}]VPN[/{DIM}]" if d.ts_ip else "",
        ])


class AlertsPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold {AMBER_HI}]▸ ALERTS[/bold {AMBER_HI}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
        ]
        for a in d.alerts[:6]:
            if a.severity == "ok":
                icon = "[#22c55e]✓[/#22c55e]"
                col = OK
            elif a.severity == "warn":
                icon = "[#f59e0b]⚡[/#f59e0b]"
                col = WARN
            else:
                icon = "[#ef4444]●[/#ef4444]"
                col = CRIT
            lines.append(f"  {icon} [{col}]{a.message:<22}[/{col}]")
        return "\n".join(lines)


class ActivityPanel(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold {AMBER_HI}]▸ RECENT[/bold {AMBER_HI}]",
            f"[{GLYPH}]{'─' * 24}[/{GLYPH}]",
        ]
        evts = list(d.events)
        if not evts:
            lines.append(f"  [{DIM}]no recent events[/{DIM}]")
        else:
            for ev in evts[-10:]:
                col = {"ok": OK, "warn": WARN, "info": INFO, "crit": CRIT}.get(ev.kind, DIM)
                lines.append(
                    f"  [{DIM}]{ev.time}[/{DIM}] [{col}]{ev.message[:24]:<24}[/{col}]"
                )
        return "\n".join(lines)


class FooterWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        k = d.kernel[:28] if d.kernel else "─"
        return (
            f"[{DIM}] kernel[/{DIM}] [white]{k:<28}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]cpu[/{DIM}] [{pct_color(d.cpu_pct)}]{d.cpu_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]mem[/{DIM}] [{pct_color(d.mem_pct)}]{d.mem_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]vms[/{DIM}] [{OK}]{d.vms_running}[/{OK}][{DIM2}]/[/{DIM2}][white]{d.vms_total}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]lxc[/{DIM}] [{OK}]{d.lxc_running}[/{OK}][{DIM2}]/[/{DIM2}][white]{d.lxc_total}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]root[/{DIM}] [{pct_color(d.root_pct)}]{d.root_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]swap[/{DIM}] [{pct_color(d.swap_pct)}]{d.swap_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]tick #[/{DIM}][white]{d.tick_n}[/white]"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════════

class OperationsDashboard(App):
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

    /* ── Left column: system metrics ── */
    #left {
        width: 28;
        layout: vertical;
        border-right: tall #1e2a3a;
    }

    #cpu-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #mem-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #percore-pane {
        height: 1fr;
        padding: 1 1;
    }

    /* ── Centre column: history graphs ── */
    #centre {
        width: 1fr;
        layout: vertical;
        padding: 1 2;
    }

    #graphs-pane {
        height: 1fr;
        content-align: center middle;
    }

    /* ── Right column: VM summary, storage, alerts, activity ── */
    #right {
        width: 28;
        layout: vertical;
        border-left: tall #1e2a3a;
    }

    #vm-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #storage-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #net-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #alerts-pane {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }

    #activity-pane {
        height: 1fr;
        padding: 1 1;
    }
    """

    TITLE = "TCET COE · Proxmox 1 NOC Dashboard"
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.d = NodeData()
        self._slow = 0
        self.d.tick_fast()
        self.d.tick_slow()

    def compose(self) -> ComposeResult:
        yield HeaderWidget(self.d, id="header")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield CpuPanel(self.d, id="cpu-pane")
                yield MemPanel(self.d, id="mem-pane")
                yield PerCorePanel(self.d, id="percore-pane")
            with Vertical(id="centre"):
                yield CenterGraphs(self.d, id="graphs-pane")
            with Vertical(id="right"):
                yield VmSummaryPanel(self.d, id="vm-pane")
                yield StoragePanel(self.d, id="storage-pane")
                yield NetworkPanel(self.d, id="net-pane")
                yield AlertsPanel(self.d, id="alerts-pane")
                yield ActivityPanel(self.d, id="activity-pane")
        yield FooterWidget(self.d, id="footer")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.d.tick_fast()
        self._slow += 1
        if self._slow % 5 == 0:
            self.d.tick_slow()
        for wid in ("header", "cpu-pane", "mem-pane", "percore-pane",
                     "graphs-pane", "vm-pane", "storage-pane", "net-pane",
                     "alerts-pane", "activity-pane", "footer"):
            try:
                self.query_one(f"#{wid}").refresh()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  TTY CLAIM + ENTRY
# ══════════════════════════════════════════════════════════════════════════════

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
    OperationsDashboard().run()


if __name__ == "__main__":
    main()
