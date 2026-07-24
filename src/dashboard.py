#!/usr/bin/env python3
"""
Proxmox Node Dashboard — Premium Appliance Operations View
Centered status panel, symmetric cards, large live graphs.
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
#  DESIGN TOKENS
# ══════════════════════════════════════════════════════════════════════════════

OK    = "#22c55e"
WARN  = "#f59e0b"
CRIT  = "#ef4444"
INFO  = "#38bdf8"
PURPLE= "#a855f7"
DIM   = "#607090"
DIM2  = "#3a4a6a"
GOLD  = "#f59e0b"
GLYPH = "#2a3a5a"
BG    = "#0a0e1a"
BG2   = "#0d1220"
BDR   = "#1e2a3a"
WHITE = "#c0cce0"

BLOCKS = " ▁▂▃▄▅▆▇█"


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


def human(b: float, dec: int = 1) -> str:
    for u in ("B ", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:>.{dec}f} {u}"
        b /= 1024
    return f"{b:>.{dec}f} PB"


def human_int(b: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{int(b)} {u}"
        b /= 1024
    return f"{int(b)} PB"


def pct_color(pct: float) -> str:
    return OK if pct < 60 else WARN if pct < 80 else CRIT


def pct_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(int(pct / 100 * width), width))
    empty = width - filled
    col = pct_color(pct)
    return f"[{col}]{'█' * filled}{'░' * empty}[/{col}]"


def spark_wide(vals: list[float], width: int) -> str:
    """Resampled sparkline that fills exactly *width* chars."""
    if not vals:
        return f"[{DIM}]{'─' * width}[/{DIM}]"
    n = len(vals)
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    chars = []
    for i in range(width):
        idx = i / (width - 1) * (n - 1) if width > 1 else 0
        left = int(idx)
        right = min(left + 1, n - 1)
        frac = idx - left
        v = vals[left] * (1 - frac) + vals[right] * frac
        chars.append(BLOCKS[min(int((v - lo) / span * 8), 8)])
    # Color based on latest value
    col = pct_color(vals[-1])
    return f"[{col}]{''.join(chars)}[/{col}]"



# ══════════════════════════════════════════════════════════════════════════════
#  HISTORY
# ══════════════════════════════════════════════════════════════════════════════

class History:
    """Fixed-length ring buffer (default 120 s @ 1 Hz)."""

    def __init__(self, maxlen: int = 120):
        self._d: deque[float] = deque(maxlen=maxlen)

    def add(self, v: float) -> None:
        self._d.append(v)

    def get(self) -> list[float]:
        return list(self._d)

    def last(self, default: float = 0.0) -> float:
        return self._d[-1] if self._d else default


# ══════════════════════════════════════════════════════════════════════════════
#  ALERTS / EVENTS
# ══════════════════════════════════════════════════════════════════════════════

class Alert:
    def __init__(self, severity: str, message: str):
        self.severity = severity  # ok | warn | crit
        self.message = message
        self.time = datetime.now().strftime("%H:%M")


class Event:
    def __init__(self, message: str, kind: str = "info"):
        self.message = message
        self.kind = kind  # ok | warn | info | crit
        self.time = datetime.now().strftime("%H:%M")


# ══════════════════════════════════════════════════════════════════════════════
#  NODE DATA COLLECTOR
# ══════════════════════════════════════════════════════════════════════════════

class NodeData:
    """Aggregated node data — fast (1 s) and slow (5 s) ticks."""

    def __init__(self):
        # CPU
        self.cpu_pct: float = 0.0
        self.cpu_cores: list[float] = []
        self.cpu_freq: float = 0.0
        self.cpu_temp: Optional[float] = None
        self.load: tuple = (0.0, 0.0, 0.0)
        self.cpu_hist = History()

        # Memory
        self.mem_pct: float = 0.0
        self.mem_used: int = 0
        self.mem_total: int = 0
        self.mem_avail: int = 0
        self.swap_pct: float = 0.0
        self.swap_used: int = 0
        self.swap_total: int = 0
        self.mem_hist = History()

        # Network
        self.net_up: float = 0.0
        self.net_dn: float = 0.0
        self.net_tx_total: int = 0
        self.net_rx_total: int = 0
        self.net_up_hist = History()
        self.net_dn_hist = History()
        self._p_sent = 0
        self._p_recv = 0
        self._p_time = time.time()

        # Disk I/O
        self.disk_r_hist = History()
        self.disk_w_hist = History()
        self._p_disk_r = 0
        self._p_disk_w = 0
        self._p_disk_time = time.time()
        # Normalisation ceiling
        self._disk_ceil = 500e6

        # Storage
        self.root_pct: float = 0.0
        self.root_used: int = 0
        self.root_total: int = 0
        self.lvm_pct: float = 0.0
        self.lvm_used_gb: float = 0.0
        self.lvm_total_gb: float = 0.0

        # ZFS
        self.zfs_pools: list[dict] = []
        self.zfs_arc_size: int = 0
        self.zfs_arc_max: int = 0

        # Virtualisation (aggregated, no individual names)
        self.vms_running: int = 0
        self.vms_stopped: int = 0
        self.vms_total: int = 0
        self.lxc_running: int = 0
        self.lxc_stopped: int = 0
        self.lxc_total: int = 0
        self.vm_total_vcpus: int = 0
        self.vm_total_maxmem: int = 0
        self.vm_total_mem: int = 0
        self.vm_cpu_pct: float = 0.0
        self.vm_mem_pct: float = 0.0
        self.vm_cpu_hist = History()
        self.vm_mem_hist = History()

        # System
        self.uptime: float = 0.0
        self.hostname: str = sh(["hostname"]) or "proxmox"
        self.kernel: str = sh(["uname", "-r"])
        self.pve_ver: str = ""
        self.node_ip: str = ""
        self.ts_ip: str = ""

        # Alerts / events
        self.alerts: list[Alert] = []
        self.events: deque[Event] = deque(maxlen=24)
        self.overall_status: str = "ok"
        self.tick_n: int = 0
        self._last_vm_run = 0
        self._last_lxc_run = 0

        v = sh(["pveversion"])
        if v:
            m = re.search(r'pve-manager[:/\s]+(\S+)', v)
            if m:
                self.pve_ver = f"PVE {m.group(1)}"

    # ── FAST TICK (1 s) ─────────────────────────────────────────────────────

    def tick_fast(self):
        self.tick_n += 1
        self._cpu()
        self._mem()
        self._net()
        self._disk()
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
            for k in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "iwlwifi"):
                if k in temps and temps[k]:
                    self.cpu_temp = temps[k][0].current
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
        s = psutil.swap_memory()
        self.swap_pct = s.percent
        self.swap_used = s.used
        self.swap_total = s.total
        self.mem_hist.add(self.mem_pct)

    def _net(self):
        now = time.time()
        c = psutil.net_io_counters()
        dt = max(now - self._p_time, 0.1)
        self.net_up = (c.bytes_sent - self._p_sent) / dt
        self.net_dn = (c.bytes_recv - self._p_recv) / dt
        self._p_sent = c.bytes_sent
        self._p_recv = c.bytes_recv
        self._p_time = now
        self.net_tx_total = c.bytes_sent
        self.net_rx_total = c.bytes_recv
        self.net_up_hist.add(min(self.net_up / 125e6 * 100, 100))
        self.net_dn_hist.add(min(self.net_dn / 125e6 * 100, 100))

    def _disk(self):
        try:
            now = time.time()
            c = psutil.disk_io_counters()
            if c:
                dt = max(now - self._p_disk_time, 0.1)
                r = (c.read_bytes - self._p_disk_r) / dt
                w = (c.write_bytes - self._p_disk_w) / dt
                self.disk_r_hist.add(min(r / self._disk_ceil * 100, 100))
                self.disk_w_hist.add(min(w / self._disk_ceil * 100, 100))
                self._p_disk_r = c.read_bytes
                self._p_disk_w = c.write_bytes
                self._p_disk_time = now
        except Exception:
            pass

    # ── SLOW TICK (5 s) ────────────────────────────────────────────────────

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
        raw = sh(["zpool", "list", "-H", "-o",
                  "name,health,capacity,allocated,size"])
        pools = []
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                pools.append({"name": parts[0], "health": parts[1],
                              "capacity": parts[2], "used": parts[3],
                              "total": parts[4]})
        self.zfs_pools = pools
        arc = sh(["cat", "/proc/spl/kstat/zfs/arcstats"])
        for line in arc.splitlines():
            ps = line.split()
            if len(ps) >= 3:
                if ps[0] == "size":
                    self.zfs_arc_size = int(ps[2])
                elif ps[0] == "c_max":
                    self.zfs_arc_max = int(ps[2])

    def _vms_aggregate(self):
        data = sh_json(["pvesh", "get", "/nodes/localhost/qemu",
                         "--output-format", "json"])
        if data is None:
            raw = sh(["qm", "list"])
            data = []
            for line in raw.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    data.append({"vmid": parts[0], "status": parts[2]})
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
            avg = sum(v.get("cpu", 0) or 0 for v in running) / len(running)
            self.vm_cpu_pct = avg * 100
            if self.vm_total_maxmem:
                self.vm_mem_pct = self.vm_total_mem / self.vm_total_maxmem * 100
            self.vm_cpu_hist.add(self.vm_cpu_pct)
            self.vm_mem_hist.add(self.vm_mem_pct)

    def _lxcs_aggregate(self):
        data = sh_json(["pvesh", "get", "/nodes/localhost/lxc",
                         "--output-format", "json"])
        if data is None:
            raw = sh(["pct", "list"])
            data = []
            for line in raw.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    data.append({"vmid": parts[0], "status": parts[2]})
        if not data:
            return
        running = [c for c in data if c.get("status") == "running"]
        self.lxc_running = len(running)
        self.lxc_stopped = len(data) - self.lxc_running
        self.lxc_total = len(data)

    def _net_ips(self):
        out = sh(["ip", "-o", "addr", "show", "vmbr0"])
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        if m:
            self.node_ip = m.group(1)
        out2 = sh(["ip", "-o", "addr", "show", "tailscale0"])
        m2 = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out2)
        if m2:
            self.ts_ip = m2.group(1)

    # ── ALERTS ──────────────────────────────────────────────────────────────

    def _update_alerts(self):
        a = []
        if self.cpu_pct > 85:
            a.append(Alert("crit", f"CPU {self.cpu_pct:.0f}%"))
        elif self.cpu_pct > 70:
            a.append(Alert("warn", f"CPU {self.cpu_pct:.0f}%"))
        if self.mem_pct > 85:
            a.append(Alert("crit", f"Memory {self.mem_pct:.0f}%"))
        elif self.mem_pct > 75:
            a.append(Alert("warn", f"Memory {self.mem_pct:.0f}%"))
        if self.swap_pct > 50:
            a.append(Alert("warn", f"Swap {self.swap_pct:.0f}%"))
        if self.root_pct > 85:
            a.append(Alert("crit", f"Root disk {self.root_pct:.0f}%"))
        elif self.root_pct > 75:
            a.append(Alert("warn", f"Root disk {self.root_pct:.0f}%"))
        if self.lvm_pct > 85:
            a.append(Alert("crit", f"LVM-thin {self.lvm_pct:.0f}%"))
        elif self.lvm_pct > 75:
            a.append(Alert("warn", f"LVM-thin {self.lvm_pct:.0f}%"))
        if self.cpu_temp and self.cpu_temp > 80:
            a.append(Alert("warn", f"Temp {self.cpu_temp:.0f}°C"))
        for pool in self.zfs_pools:
            if pool.get("health") != "ONLINE":
                a.append(Alert("crit", f"ZFS {pool['name']}: {pool['health']}"))
        self.alerts = a[:6]
        if not self.alerts:
            self.alerts = [Alert("ok", "All systems healthy")]
        sevs = {x.severity for x in self.alerts}
        self.overall_status = "crit" if "crit" in sevs \
                             else "warn" if "warn" in sevs else "ok"

    def _check_events(self):
        if self._last_vm_run and self.vms_running != self._last_vm_run:
            d = "up" if self.vms_running > self._last_vm_run else "down"
            self.events.append(Event(
                f"VMs: {self._last_vm_run} → {self.vms_running} ({d})", "info"))
        self._last_vm_run = self.vms_running
        if self._last_lxc_run and self.lxc_running != self._last_lxc_run:
            d = "up" if self.lxc_running > self._last_lxc_run else "down"
            self.events.append(Event(
                f"LXCs: {self._last_lxc_run} → {self.lxc_running} ({d})", "info"))
        self._last_lxc_run = self.lxc_running

    # ── HELPERS ─────────────────────────────────────────────────────────────

    def uptime_str(self) -> str:
        s = int(self.uptime)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{d}d {h:02d}h {m:02d}m" if d else f"{h:02d}h {m:02d}m {s:02d}s"

    def status_dot_text(self) -> tuple[str, str]:
        return {"ok": ("#22c55e", "HEALTHY"),
                "warn": ("#f59e0b", "WARNING"),
                "crit": ("#ef4444", "CRITICAL")}[self.overall_status]


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS — symmetric cards around a centered status panel
# ══════════════════════════════════════════════════════════════════════════════

# ── Header ───────────────────────────────────────────────────────────────────

class HeaderWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        now = datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
        s_col, s_txt = d.status_dot_text()
        return (
            f"[bold {GOLD}]  ◈  {d.hostname.upper()}[/bold {GOLD}]"
            f"[{DIM2}]  │  [/{DIM2}][{INFO}]{d.pve_ver}[/{INFO}]"
            f"[{DIM2}]  │  [/{DIM2}][{DIM}]UP {d.uptime_str()}[/{DIM}]"
            f"  [{s_col}]●[/{s_col}] [{s_col}]{s_txt}[/{s_col}]"
            f"[{DIM2}]  │  [/{DIM2}][{WHITE}]{now}[/{WHITE}]"
        )


# ── Left column: CPU / Memory / Virtualisation ───────────────────────────────

class CpuCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        temp = f"[{CRIT}]{d.cpu_temp:.0f}°C[/{CRIT}]" if d.cpu_temp else f"[{DIM}]--°C[/{DIM}]"
        freq = f"{d.cpu_freq/1000:.2f} GHz" if d.cpu_freq else "-- GHz"
        ld = d.load
        bar = pct_bar(d.cpu_pct, 18)
        sp = spark_wide(d.cpu_hist.get(), 20)
        return "\n".join([
            f"[bold {GOLD}]CPU[/bold {GOLD}]  [{DIM}]i7-14700  20c/28t[/{DIM}]",
            f"{bar}  [{pct_color(d.cpu_pct)}]{d.cpu_pct:>5.1f}%[/]",
            f"[{DIM}]temp[/{DIM}]  {temp}     [{DIM}]freq[/{DIM}]  [white]{freq}[/white]",
            f"[{DIM}]load[/{DIM}]  [white]{ld[0]:.2f}[/white]  [{DIM2}]·[/{DIM2}]  [white]{ld[1]:.2f}[/white]  [{DIM2}]·[/{DIM2}]  [white]{ld[2]:.2f}[/white]",
            f"[{DIM}]hist[/{DIM}]  {sp}",
        ])


class MemCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        bar = pct_bar(d.mem_pct, 18)
        sbar = pct_bar(d.swap_pct, 18)
        used = human(d.mem_used)
        total = human(d.mem_total)
        sp = spark_wide(d.mem_hist.get(), 20)
        return "\n".join([
            f"[bold {GOLD}]MEMORY[/bold {GOLD}]  [{DIM}]32 GB DDR5[/{DIM}]",
            f"{bar}  [{pct_color(d.mem_pct)}]{d.mem_pct:>5.1f}%[/]",
            f"[{DIM}]used[/{DIM}]  [white]{used}[/white]  [{DIM}]of[/{DIM}]  [white]{total}[/white]",
            f"[{DIM}]swap[/{DIM}] {sbar}  [{pct_color(d.swap_pct)}]{d.swap_pct:>5.1f}%[/]",
            f"[{DIM}]hist[/{DIM}]  {sp}",
        ])


class VmCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        cpu_sp = spark_wide(d.vm_cpu_hist.get(), 20)
        mem_sp = spark_wide(d.vm_mem_hist.get(), 20)
        run_col = OK if d.vms_running > 0 else DIM
        lxc_col = OK if d.lxc_running > 0 else DIM
        return "\n".join([
            f"[bold {GOLD}]VIRTUALISATION[/bold {GOLD}]",
            f"[{run_col}]▶[/{run_col}] [white]VMs[/white]   [{run_col}]{d.vms_running} run[/{run_col}]"
            f"  [{DIM2}]/[/{DIM2}]  [{DIM}]{d.vms_stopped} stop[/{DIM}]  [{DIM}]{d.vms_total} total[/{DIM}]",
            f"[{lxc_col}]▶[/{lxc_col}] [white]LXCs[/white]  [{lxc_col}]{d.lxc_running} run[/{lxc_col}]"
            f"  [{DIM2}]/[/{DIM2}]  [{DIM}]{d.lxc_stopped} stop[/{DIM}]  [{DIM}]{d.lxc_total} total[/{DIM}]",
            f"",
            f"[{DIM}]vCPUs[/{DIM}]   [white]{d.vm_total_vcpus:>4}[/white]  [{DIM}]allocated[/{DIM}]",
            f"[{DIM}]RAM[/{DIM}]    [white]{human_int(d.vm_total_maxmem):>9}[/white]  [{DIM}]allocated[/{DIM}]",
            f"[{DIM}]CPU[/{DIM}]   [{pct_color(d.vm_cpu_pct)}]{d.vm_cpu_pct:>5.1f}%[/]  {cpu_sp}",
            f"[{DIM}]MEM[/{DIM}]   [{pct_color(d.vm_mem_pct)}]{d.vm_mem_pct:>5.1f}%[/]  {mem_sp}",
        ])


# ── Centre: premium appliance status panel ───────────────────────────────────

class CentrePanel(Static):
    """The big centred status seal — visual identity of the dashboard."""

    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d
        self._tick = 0

    def render(self) -> str:
        self._tick += 1
        d = self.d
        s_col, s_txt = d.status_dot_text()

        # inner box width
        W = 44

        def centre(text: str, width: int = W) -> str:
            plain = re.sub(r'\[[^\]]+\]', '', text)
            pad = max(0, width - len(plain))
            lp = pad // 2
            rp = pad - lp
            return " " * lp + text + " " * rp

        H = "═"
        V = f"[{GOLD}]║[/{GOLD}]"
        L = f"[{GOLD}]"
        R = f"[/{GOLD}]"

        top = f"  {L}╔{H * W}╗{R}"
        bot = f"  {L}╚{H * W}╝{R}"

        now_s = datetime.now().strftime("%H:%M:%S")
        ip = d.node_ip or d.ts_ip or "─"

        rows = [
            top,
            f"{V}{centre(f'[{DIM}]T C E T[/{DIM}]')}{V}",
            f"{V}{centre(f'[{DIM2}]Centre of Excellence[/{DIM2}]')}{V}",
            f"{V}{centre(f'[{DIM2}]' + ('─' * 28) + f'[/{DIM2}]')}{V}",
            f"{V}{centre(f'[bold white]P R O X M O X   1[/bold white]')}{V}",
            f"{V}{centre(f'[{DIM}]Node · {ip}[/{DIM}]')}{V}",
            f"{V}{' ' * W}{V}",
            f"{V}{centre(f'[{s_col}]●[/{s_col}]  [bold {s_col}]{s_txt}[/bold {s_col}]')}{V}",
            f"{V}{' ' * W}{V}",
            f"{V}{centre(f'[{DIM}]Up {d.uptime_str()}[/{DIM}]')}{V}",
            f"{V}{centre(f'[{DIM}]{now_s}[/{DIM}]')}{V}",
            bot,
        ]

        # Running VMs / LXCs summary line below the box
        vm_line = (
            f"  [{DIM2}]VMs[/{DIM2}]  [{OK}]{'●' * d.vms_running}{'○' * d.vms_stopped}[/{OK}]"
            f"   [{DIM}]{d.vms_running}/{d.vms_total}[/{DIM}]"
            f"   [{DIM2}]LXCs[/{DIM2}]  [{OK}]{'●' * d.lxc_running}{'○' * d.lxc_stopped}[/{OK}]"
            f"   [{DIM}]{d.lxc_running}/{d.lxc_total}[/{DIM}]"
        )
        rows.append(vm_line)

        return "\n".join(rows)


# ── Right column: Network / Storage / Alerts ─────────────────────────────────

class NetCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        up_s = human(d.net_up)
        dn_s = human(d.net_dn)
        tx_s = human_int(d.net_tx_total)
        rx_s = human_int(d.net_rx_total)
        up_sp = spark_wide(d.net_up_hist.get(), 20)
        dn_sp = spark_wide(d.net_dn_hist.get(), 20)
        lines = [
            f"[bold {GOLD}]NETWORK[/bold {GOLD}]",
            f"  [{OK}]▲[/{OK}] [white]{up_s:>9}/s[/white]",
            f"  [{OK}]{up_sp}[/{OK}]",
            f"  [{INFO}]▼[/{INFO}] [white]{dn_s:>9}/s[/white]",
            f"  [{INFO}]{dn_sp}[/{INFO}]",
            f"  [{DIM}]TX[/{DIM}] [white]{tx_s:>10}[/white]  [{DIM}]RX[/{DIM}] [white]{rx_s:>10}[/white]",
        ]
        if d.node_ip:
            lines.append(
                f"  [{DIM}]vmbr0[/{DIM}]  [white]{d.node_ip:<14}[/white]"
            )
        return "\n".join(lines)


class StorageCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold {GOLD}]STORAGE[/bold {GOLD}]",
            f"  [{DIM}]root[/{DIM}]  {pct_bar(d.root_pct, 18)}  [{pct_color(d.root_pct)}]{d.root_pct:.1f}%[/]",
            f"  [{DIM}]ext4[/{DIM}]  [white]{human(d.root_used)}[/]  [{DIM2}]/[/{DIM2}]  [{DIM}]{human(d.root_total)}[/{DIM}]",
        ]
        if d.lvm_pct:
            lines += [
                f"  [{DIM}]data[/{DIM}]  {pct_bar(d.lvm_pct, 18)}  [{pct_color(d.lvm_pct)}]{d.lvm_pct:.1f}%[/]",
                f"  [{DIM}]thin[/{DIM}]  [white]{d.lvm_used_gb:.1f}G[/]  [{DIM2}]/[/{DIM2}]  [{DIM}]{d.lvm_total_gb:.0f}G[/{DIM}]",
            ]
        for pool in d.zfs_pools[:2]:
            hcol = OK if pool["health"] == "ONLINE" else CRIT
            lines.append(
                f"  [{hcol}]◈[/{hcol}] [{DIM}]{pool['name']:<8}[/{DIM}]"
                f"[{hcol}]{pool['health']:<6}[/{hcol}]"
                f"[white]{pool['capacity']:>3}%[/white]"
            )
        if d.zfs_arc_max:
            arc_pct = d.zfs_arc_size / d.zfs_arc_max * 100
            lines.append(
                f"  [{DIM}]ARC[/{DIM}]  {pct_bar(arc_pct, 12)}  [{pct_color(arc_pct)}]{arc_pct:.0f}%[/]"
                f"  [{DIM}]{human_int(d.zfs_arc_size)}[/{DIM}]"
            )
        return "\n".join(lines)


class AlertsCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        lines = [
            f"[bold {GOLD}]ALERTS[/bold {GOLD}]",
        ]
        for a in d.alerts[:5]:
            if a.severity == "ok":
                lines.append(f"  [{OK}]✓[/{OK}]  [{OK}]{a.message:<24}[/{OK}]")
            elif a.severity == "warn":
                lines.append(f"  [{WARN}]⚡[/{WARN}]  [{WARN}]{a.message:<24}[/{WARN}]")
            else:
                lines.append(f"  [{CRIT}]●[/{CRIT}]  [{CRIT}]{a.message:<24}[/{CRIT}]")
        if d.events:
            lines.append("")
            lines.append(f"[{DIM2}]── recent ──[/{DIM2}]")
            for ev in list(d.events)[-3:]:
                cols = {"ok": OK, "warn": WARN, "info": INFO, "crit": CRIT}
                c = cols.get(ev.kind, DIM)
                lines.append(f"  [{DIM}]{ev.time}[/{DIM}]  [{c}]{ev.message[:20]:<20}[/{c}]")
        return "\n".join(lines)


# ── Lower half: 4 large full-width history graphs ────────────────────────────

class GraphArea(Static):
    """Four full-width animated graphs: CPU, MEM, NET, DISK."""

    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def _graph_block(self, title: str, colour: str,
                     hist: History, unit: str,
                     w: int, extra: str = "",
                     hist2: Optional[History] = None,
                     label2: str = "") -> list[str]:
        val = hist.last()
        sp = spark_wide(hist.get(), w)
        col = pct_color(val) if unit == "%" else colour
        val_s = f"[{col}]{val:>5.1f}{unit}[/{col}]"
        vals = hist.get()
        mn = f"{min(vals):.1f}" if vals else "─"
        mx = f"{max(vals):.1f}" if vals else "─"
        lines = [
            f"  [{colour}]━━━[/{colour}] [bold {colour}]{title}[/bold]  {val_s}"
            f"[{DIM}]     min[/{DIM}] [white]{mn}[/white][{DIM}]  max[/{DIM}] [white]{mx}[/white]"
            f"  {extra}",
            f"  {sp}",
        ]
        if hist2:
            v2 = hist2.last()
            col2 = pct_color(v2) if unit == "%" else colour
            v2_s = f"[{col2}]{v2:>5.1f}{unit}[/{col2}]"
            sp2 = spark_wide(hist2.get(), w)
            vals2 = hist2.get()
            mn2 = f"{min(vals2):.1f}" if vals2 else "─"
            mx2 = f"{max(vals2):.1f}" if vals2 else "─"
            lines += [
                f"  [{label2}]  {v2_s}"
                f"[{DIM}]     min[/{DIM}] [white]{mn2}[/white][{DIM}]  max[/{DIM}] [white]{mx2}[/white]",
                f"  {sp2}",
            ]
        return lines

    def render(self) -> str:
        try:
            return self._render_graphs()
        except Exception as exc:
            return (
                f"  [{DIM}]graph render error: {exc}[/{DIM}]\n"
                f"  [{DIM}]will retry on next tick[/{DIM}]"
            )

    def _render_graphs(self) -> str:
        d = self.d
        w = 234

        lines = []

        # CPU
        extra = f"[{DIM}]load[/{DIM}] [{WHITE}]{d.load[0]:.2f}[/{WHITE}]"
        lines += self._graph_block("CPU UTILIZATION", GOLD, d.cpu_hist, "%", w, extra)
        lines.append("")

        # Memory
        extra = f"[{DIM}]swap[/{DIM}] [{pct_color(d.swap_pct)}]{d.swap_pct:.1f}%[/]"
        lines += self._graph_block("MEMORY USAGE", INFO, d.mem_hist, "%", w, extra)
        lines.append("")

        # Network (dual sparkline)
        up_val = d.net_up_hist.last()
        dn_val = d.net_dn_hist.last()
        up_s = human(d.net_up)
        dn_s = human(d.net_dn)
        extra_net = (
            f"[{OK}]▲[/{OK}] [{WHITE}]{up_s}[/{WHITE}]/s"
            f"   [{INFO}]▼[/{INFO}] [{WHITE}]{dn_s}[/{WHITE}]/s"
            f"   [{DIM}]TX[/{DIM}] [{WHITE}]{human_int(d.net_tx_total)}[/{WHITE}]"
            f"   [{DIM}]RX[/{DIM}] [{WHITE}]{human_int(d.net_rx_total)}[/{WHITE}]"
        )
        lines.append(
            f"  [{INFO}]━━━[/{INFO}] [bold {INFO}]NETWORK THROUGHPUT[/bold {INFO}]  {extra_net}"
        )
        lines.append(f"  [{OK}]{spark_wide(d.net_up_hist.get(), w)}[/{OK}]")
        lines.append(
            f"  [{DIM}]▲  up[/{DIM}]  [{pct_color(up_val)}]{up_val:>5.1f}%[/]"
            f"[{DIM}]  min[/{DIM}] [white]{min(d.net_up_hist.get()):.1f}[/white]"
            f"[{DIM}]  max[/{DIM}] [white]{max(d.net_up_hist.get()):.1f}[/white]"
        )
        lines.append(f"  [{INFO}]{spark_wide(d.net_dn_hist.get(), w)}[/{INFO}]")
        lines.append(
            f"  [{DIM}]▼  dn[/{DIM}]  [{pct_color(dn_val)}]{dn_val:>5.1f}%[/]"
            f"[{DIM}]  min[/{DIM}] [white]{min(d.net_dn_hist.get()):.1f}[/white]"
            f"[{DIM}]  max[/{DIM}] [white]{max(d.net_dn_hist.get()):.1f}[/white]"
        )
        lines.append("")

        # Disk I/O (dual sparkline)
        r_val = d.disk_r_hist.last()
        w_val = d.disk_w_hist.last()
        r_actual = r_val / 100 * d._disk_ceil
        w_actual = w_val / 100 * d._disk_ceil
        extra_disk = (
            f"[{OK}]R[/{OK}] [{WHITE}]{human(r_actual)}[/{WHITE}]/s"
            f"   [{INFO}]W[/{INFO}] [{WHITE}]{human(w_actual)}[/{WHITE}]/s"
        )
        lines.append(
            f"  [{OK}]━━━[/{OK}] [bold {OK}]DISK THROUGHPUT[/bold {OK}]  {extra_disk}"
        )
        lines.append(f"  [{OK}]{spark_wide(d.disk_r_hist.get(), w)}[/{OK}]")
        lines.append(
            f"  [{DIM}]R  read[/{DIM}]  [{pct_color(r_val)}]{r_val:>5.1f}%[/]"
            f"[{DIM}]  min[/{DIM}] [white]{min(d.disk_r_hist.get()):.1f}[/white]"
            f"[{DIM}]  max[/{DIM}] [white]{max(d.disk_r_hist.get()):.1f}[/white]"
        )
        lines.append(f"  [{INFO}]{spark_wide(d.disk_w_hist.get(), w)}[/{INFO}]")
        lines.append(
            f"  [{DIM}]W  write[/{DIM}] [{pct_color(w_val)}]{w_val:>5.1f}%[/]"
            f"[{DIM}]  min[/{DIM}] [white]{min(d.disk_w_hist.get()):.1f}[/white]"
            f"[{DIM}]  max[/{DIM}] [white]{max(d.disk_w_hist.get()):.1f}[/white]"
        )

        return "\n".join(lines)


# ── Footer ───────────────────────────────────────────────────────────────────

class FooterWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        k = d.kernel[:30] if d.kernel else "─"
        return (
            f"[{DIM}]  kernel[/{DIM}] [white]{k:<30}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]cpu[/{DIM}] [{pct_color(d.cpu_pct)}]{d.cpu_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]mem[/{DIM}] [{pct_color(d.mem_pct)}]{d.mem_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]v[/{DIM}] [{OK}]{d.vms_running}[/{OK}][{DIM2}]/[/{DIM2}][white]{d.vms_total}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]c[/{DIM}] [{OK}]{d.lxc_running}[/{OK}][{DIM2}]/[/{DIM2}][white]{d.lxc_total}[/white]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]root[/{DIM}] [{pct_color(d.root_pct)}]{d.root_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]swap[/{DIM}] [{pct_color(d.swap_pct)}]{d.swap_pct:.0f}%[/]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]tick[/{DIM}] [white]{d.tick_n}[/white]"
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

    /* ── Main body: symmetric 3-column ── */
    #body {
        height: 1fr;
        layout: horizontal;
    }

    .side-col {
        width: 26;
        layout: vertical;
    }

    #left {
        border-right: tall #1e2a3a;
        padding: 1 0 1 1;
    }

    #right {
        border-left: tall #1e2a3a;
        padding: 1 1 1 0;
    }

    /* ── Centre: appliance panel ── */
    #centre-area {
        width: 1fr;
        layout: vertical;
        align: center middle;
        content-align: center middle;
    }

    #centre-panel {
        width: auto;
        height: auto;
        content-align: center middle;
        align: center middle;
    }

    /* ── Card sections ── */
    .card {
        height: auto;
        padding: 1 1;
        border-bottom: tall #1e2a3a;
    }



    /* ── Lower graphs ── */
    #graphs {
        height: 1fr;
        padding: 1 0;
        border-top: tall #1e2a3a;
        background: #0c1020;
    }

    #graphs-widget {
        height: 1fr;
    }
    """

    TITLE = "Proxmox NOC Dashboard"
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
            # Left column: cards stacked vertically
            with Vertical(id="left", classes="side-col"):
                yield CpuCard(self.d, classes="card", id="cpu-card")
                yield MemCard(self.d, classes="card", id="mem-card")
                yield VmCard(self.d, classes="card", id="vm-card")
            # Centre: the appliance seal
            with Vertical(id="centre-area"):
                yield CentrePanel(self.d, id="centre-panel")
            # Right column
            with Vertical(id="right", classes="side-col"):
                yield NetCard(self.d, classes="card", id="net-card")
                yield StorageCard(self.d, classes="card", id="storage-card")
                yield AlertsCard(self.d, classes="card", id="alerts-card")
        # Lower half: full-width graphs
        with Vertical(id="graphs"):
            yield GraphArea(self.d, id="graphs-widget")
        yield FooterWidget(self.d, id="footer")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.d.tick_fast()
        self._slow += 1
        if self._slow % 5 == 0:
            self.d.tick_slow()
        for wid in ("header", "cpu-card", "mem-card", "vm-card",
                     "centre-panel", "net-card", "storage-card",
                     "alerts-card", "graphs-widget", "footer"):
            try:
                self.query_one(f"#{wid}").refresh()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  TTY CLAIM + ENTRY
# ══════════════════════════════════════════════════════════════════════════════

CRASH_LOG = "/tmp/proxmox-dashboard-crash.log"


def _claim_tty() -> bool:
    """Fork + setsid to make /dev/tty1 the controlling terminal.

    Redirects stdin + stdout to tty1 but leaves stderr on journal
    so crash traces are visible in journalctl.
    """
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid > 0:
        def _fw(signum, frame):
            try:
                os.kill(pid, signum)
            except OSError:
                pass
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _fw)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os._exit(0)
    try:
        os.setsid()
    except OSError:
        pass
    for dev in ("/dev/tty1", "/dev/tty"):
        try:
            fd = os.open(dev, os.O_RDWR)
        except OSError:
            continue
        os.dup2(fd, 0)
        os.dup2(fd, 1)
        # stderr stays on journal so crash traces are logged
        if fd > 2:
            os.close(fd)
        return True
    return False


def main():
    _claim_tty()
    try:
        OperationsDashboard().run()
    except Exception:
        import traceback
        with open(CRASH_LOG, "a") as f:
            f.write(f"=== crash at {datetime.now()} ===\n")
            traceback.print_exc(file=f)
        raise


if __name__ == "__main__":
    main()
