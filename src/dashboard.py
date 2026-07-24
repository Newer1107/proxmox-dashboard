#!/usr/bin/env python3
"""
Proxmox Node Dashboard — Premium Appliance Operations View (v2)
Bordered, titled cards · dynamic-width graphs · real 0-100 scaled sparklines.
"""

from __future__ import annotations

import json
import os
import re
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

OK      = "#34d399"
WARN    = "#fbbf24"
CRIT    = "#f87171"
INFO    = "#38bdf8"
PURPLE  = "#a78bfa"
TEAL    = "#2dd4bf"
ORANGE  = "#fb923c"
GOLD    = "#f5b942"
DIM     = "#6b7a99"
DIM2    = "#39476b"
WHITE   = "#dbe3f3"

BG        = "#090c15"
PANEL_BG  = "#0e1524"
CARD_BG   = "#111a2c"
BORDER    = "#25324d"

BLOCKS = " ▁▂▃▄▅▆▇█"

ORG_NAME = "T C E T"
ORG_SUB  = "Centre of Excellence"


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
    return f"[{col}]{'█' * filled}[/{col}][{DIM2}]{'─' * empty}[/{DIM2}]"


def spark_wide(vals: list[float], width: int, colour: Optional[str] = None) -> str:
    """Sparkline resampled to *width* chars, fixed 0-100 scale (all stored
    histories are already percentages) so flat/idle data still reads as a
    real baseline instead of collapsing into blank space."""
    width = max(1, width)
    if not vals:
        return f"[{DIM2}]{'▁' * width}[/{DIM2}]"
    n = len(vals)
    chars = []
    for i in range(width):
        idx = i / (width - 1) * (n - 1) if width > 1 else 0
        left = int(idx)
        right = min(left + 1, n - 1)
        frac = idx - left
        v = vals[left] * (1 - frac) + vals[right] * frac
        v = max(0.0, min(100.0, v))
        level = int(v / 100 * 8)
        if v > 0.5 and level == 0:
            level = 1
        chars.append(BLOCKS[level])
    col = colour or pct_color(vals[-1])
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
        self.severity = severity
        self.message = message
        self.time = datetime.now().strftime("%H:%M")


class Event:
    def __init__(self, message: str, kind: str = "info"):
        self.message = message
        self.kind = kind
        self.time = datetime.now().strftime("%H:%M")


# ══════════════════════════════════════════════════════════════════════════════
#  NODE DATA COLLECTOR
# ══════════════════════════════════════════════════════════════════════════════

class NodeData:
    """Aggregated node data — fast (1 s) and slow (5 s) ticks."""

    def __init__(self):
        self.cpu_pct: float = 0.0
        self.cpu_cores: list[float] = []
        self.cpu_freq: float = 0.0
        self.cpu_model: str = ""
        self.cpu_temp: Optional[float] = None
        self.load: tuple = (0.0, 0.0, 0.0)
        self.cpu_hist = History()

        self.mem_pct: float = 0.0
        self.mem_used: int = 0
        self.mem_total: int = 0
        self.mem_avail: int = 0
        self.swap_pct: float = 0.0
        self.swap_used: int = 0
        self.swap_total: int = 0
        self.mem_hist = History()

        self.net_up: float = 0.0
        self.net_dn: float = 0.0
        self.net_tx_total: int = 0
        self.net_rx_total: int = 0
        self.net_up_hist = History()
        self.net_dn_hist = History()
        self._p_sent = 0
        self._p_recv = 0
        self._p_time = time.time()

        self.disk_r_hist = History()
        self.disk_w_hist = History()
        self._p_disk_r = 0
        self._p_disk_w = 0
        self._p_disk_time = time.time()
        self._disk_ceil = 500e6

        self.root_pct: float = 0.0
        self.root_used: int = 0
        self.root_total: int = 0
        self.lvm_pct: float = 0.0
        self.lvm_used_gb: float = 0.0
        self.lvm_total_gb: float = 0.0

        self.zfs_pools: list[dict] = []
        self.zfs_arc_size: int = 0
        self.zfs_arc_max: int = 0

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

        self.uptime: float = 0.0
        self.hostname: str = sh(["hostname"]) or "proxmox"
        self.kernel: str = sh(["uname", "-r"])
        self.pve_ver: str = ""
        self.node_ip: str = ""
        self.ts_ip: str = ""

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

        cinfo = sh(["cat", "/proc/cpuinfo"])
        m = re.search(r'model name\s*:\s*(.+)', cinfo)
        if m:
            self.cpu_model = re.sub(r'\s+', ' ', m.group(1)).strip()

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
            self.events.append(Event(f"VMs: {self._last_vm_run} → {self.vms_running} ({d})", "info"))
        self._last_vm_run = self.vms_running
        if self._last_lxc_run and self.lxc_running != self._last_lxc_run:
            d = "up" if self.lxc_running > self._last_lxc_run else "down"
            self.events.append(Event(f"LXCs: {self._last_lxc_run} → {self.lxc_running} ({d})", "info"))
        self._last_lxc_run = self.lxc_running

    def uptime_str(self) -> str:
        s = int(self.uptime)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{d}d {h:02d}h {m:02d}m" if d else f"{h:02d}h {m:02d}m {s:02d}s"

    def status_dot_text(self) -> tuple[str, str]:
        return {"ok": (OK, "HEALTHY"),
                "warn": (WARN, "WARNING"),
                "crit": (CRIT, "CRITICAL")}[self.overall_status]


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class HeaderWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        now = datetime.now().strftime("%a %d %b %Y  •  %H:%M:%S")
        s_col, s_txt = d.status_dot_text()
        return (
            f"[bold {GOLD}]◈ {d.hostname.upper()}[/bold {GOLD}]"
            f"  [{DIM2}]│[/{DIM2}]  [{INFO}]{d.pve_ver or 'PVE'}[/{INFO}]"
            f"  [{DIM2}]│[/{DIM2}]  [{DIM}]uptime[/{DIM}] [{WHITE}]{d.uptime_str()}[/{WHITE}]"
            f"  [{DIM2}]│[/{DIM2}]  [{s_col}]●[/{s_col}] [bold {s_col}]{s_txt}[/bold {s_col}]"
            f"[{DIM2}]  │  [/{DIM2}][{DIM}]{now}[/{DIM}]"
        )


class CpuCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "CPU"

    def render(self) -> str:
        d = self.d
        self.border_subtitle = (d.cpu_model[:22] + "...") if len(d.cpu_model) > 22 else d.cpu_model
        temp = f"[{CRIT}]{d.cpu_temp:.0f}°C[/{CRIT}]" if d.cpu_temp else f"[{DIM}]--°C[/{DIM}]"
        freq = f"{d.cpu_freq/1000:.2f} GHz" if d.cpu_freq else "-- GHz"
        ld = d.load
        bar = pct_bar(d.cpu_pct, 20)
        sp = spark_wide(d.cpu_hist.get(), 22)
        return "\n".join([
            f"{bar}  [{pct_color(d.cpu_pct)}]{d.cpu_pct:>5.1f}%[/]",
            f"[{DIM}]temp[/{DIM}]  {temp}    [{DIM}]freq[/{DIM}]  [{WHITE}]{freq}[/{WHITE}]",
            f"[{DIM}]load[/{DIM}]  [{WHITE}]{ld[0]:.2f}[/{WHITE}] [{DIM2}]·[/{DIM2}] [{WHITE}]{ld[1]:.2f}[/{WHITE}] [{DIM2}]·[/{DIM2}] [{WHITE}]{ld[2]:.2f}[/{WHITE}]",
            f"[{DIM}]1m[/{DIM}]    {sp}",
        ])


class MemCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "MEMORY"

    def render(self) -> str:
        d = self.d
        self.border_subtitle = human(d.mem_total, 0)
        bar = pct_bar(d.mem_pct, 20)
        sbar = pct_bar(d.swap_pct, 20)
        used = human(d.mem_used)
        total = human(d.mem_total)
        sp = spark_wide(d.mem_hist.get(), 22)
        return "\n".join([
            f"{bar}  [{pct_color(d.mem_pct)}]{d.mem_pct:>5.1f}%[/]",
            f"[{DIM}]used[/{DIM}]  [{WHITE}]{used}[/{WHITE}] [{DIM}]of[/{DIM}] [{WHITE}]{total}[/{WHITE}]",
            f"[{DIM}]swap[/{DIM}]  {sbar}  [{pct_color(d.swap_pct)}]{d.swap_pct:>5.1f}%[/]",
            f"[{DIM}]1m[/{DIM}]    {sp}",
        ])


class VmCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "VIRTUALISATION"

    def render(self) -> str:
        d = self.d
        cpu_sp = spark_wide(d.vm_cpu_hist.get(), 22, colour=PURPLE)
        mem_sp = spark_wide(d.vm_mem_hist.get(), 22, colour=PURPLE)
        run_col = OK if d.vms_running > 0 else DIM
        lxc_col = OK if d.lxc_running > 0 else DIM
        return "\n".join([
            f"[{run_col}]▶ VMs [/{run_col}] [bold {run_col}]{d.vms_running}[/bold {run_col}][{DIM}] run[/{DIM}]"
            f"  [{DIM2}]/[/{DIM2}]  [{DIM}]{d.vms_stopped} stop  {d.vms_total} tot[/{DIM}]",
            f"[{lxc_col}]▶ LXCs[/{lxc_col}] [bold {lxc_col}]{d.lxc_running}[/bold {lxc_col}][{DIM}] run[/{DIM}]"
            f"  [{DIM2}]/[/{DIM2}]  [{DIM}]{d.lxc_stopped} stop  {d.lxc_total} tot[/{DIM}]",
            f"[{DIM}]vCPU[/{DIM}]  [{WHITE}]{d.vm_total_vcpus:>4}[/{WHITE}] [{DIM}]alloc[/{DIM}]"
            f"   [{DIM}]RAM[/{DIM}] [{WHITE}]{human_int(d.vm_total_maxmem)}[/{WHITE}]",
            f"[{DIM}]cpu[/{DIM}]   [{pct_color(d.vm_cpu_pct)}]{d.vm_cpu_pct:>5.1f}%[/]  {cpu_sp}",
            f"[{DIM}]mem[/{DIM}]   [{pct_color(d.vm_mem_pct)}]{d.vm_mem_pct:>5.1f}%[/]  {mem_sp}",
        ])


class HeroPanel(Static):
    """Centred appliance status seal — visual identity of the dashboard."""

    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "PROXMOX · 1"

    def render(self) -> str:
        d = self.d
        s_col, s_txt = d.status_dot_text()
        ip = d.node_ip or d.ts_ip or "─"
        now_s = datetime.now().strftime("%H:%M:%S")

        def centre(text: str, width: int) -> str:
            plain = re.sub(r'\[[^\]]+\]', '', text)
            pad = max(0, width - len(plain))
            lp = pad // 2
            return " " * lp + text + " " * (pad - lp)

        W = 40
        rows = [
            "",
            centre(f"[{DIM}]{ORG_NAME}[/{DIM}]", W),
            centre(f"[{DIM2}]{ORG_SUB}[/{DIM2}]", W),
            centre(f"[{DIM2}]{'─' * 26}[/{DIM2}]", W),
            "",
            centre(f"[{s_col}]●●●[/{s_col}]", W),
            centre(f"[bold {s_col}]{s_txt}[/bold {s_col}]", W),
            "",
            centre(f"[{DIM}]node[/{DIM}]  [bold {WHITE}]{ip}[/bold {WHITE}]", W),
            centre(f"[{DIM}]up[/{DIM}]    [{WHITE}]{d.uptime_str()}[/{WHITE}]", W),
            centre(f"[{DIM}]{now_s}[/{DIM}]", W),
            "",
            centre(
                f"[{DIM2}]VMs[/{DIM2}] [{OK}]{'●' * min(d.vms_running,8)}{'○' * min(d.vms_stopped,4)}[/{OK}] [{DIM}]{d.vms_running}/{d.vms_total}[/{DIM}]"
                f"   [{DIM2}]LXC[/{DIM2}] [{OK}]{'●' * min(d.lxc_running,8)}{'○' * min(d.lxc_stopped,4)}[/{OK}] [{DIM}]{d.lxc_running}/{d.lxc_total}[/{DIM}]",
                W,
            ),
        ]
        return "\n".join(rows)


class NetCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "NETWORK"

    def render(self) -> str:
        d = self.d
        self.border_subtitle = d.node_ip or ""
        up_s = human(d.net_up)
        dn_s = human(d.net_dn)
        tx_s = human_int(d.net_tx_total)
        rx_s = human_int(d.net_rx_total)
        up_sp = spark_wide(d.net_up_hist.get(), 22, colour=OK)
        dn_sp = spark_wide(d.net_dn_hist.get(), 22, colour=INFO)
        return "\n".join([
            f"[{OK}]▲ up  [/{OK}] [{WHITE}]{up_s:>10}/s[/{WHITE}]",
            f"       {up_sp}",
            f"[{INFO}]▼ down[/{INFO}] [{WHITE}]{dn_s:>10}/s[/{WHITE}]",
            f"       {dn_sp}",
            f"[{DIM}]tx[/{DIM}] [{WHITE}]{tx_s:>9}[/{WHITE}]   [{DIM}]rx[/{DIM}] [{WHITE}]{rx_s:>9}[/{WHITE}]",
        ])


class StorageCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "STORAGE"

    def render(self) -> str:
        d = self.d
        lines = [
            f"[{DIM}]root[/{DIM}]  {pct_bar(d.root_pct, 20)}  [{pct_color(d.root_pct)}]{d.root_pct:>4.1f}%[/]",
            f"[{DIM}]ext4[/{DIM}]  [{WHITE}]{human(d.root_used)}[/{WHITE}] [{DIM2}]/[/{DIM2}] [{DIM}]{human(d.root_total)}[/{DIM}]",
        ]
        if d.lvm_pct:
            lines += [
                f"[{DIM}]thin[/{DIM}]  {pct_bar(d.lvm_pct, 20)}  [{pct_color(d.lvm_pct)}]{d.lvm_pct:>4.1f}%[/]",
                f"[{DIM}]data[/{DIM}]  [{WHITE}]{d.lvm_used_gb:.1f}G[/{WHITE}] [{DIM2}]/[/{DIM2}] [{DIM}]{d.lvm_total_gb:.0f}G[/{DIM}]",
            ]
        for pool in d.zfs_pools[:2]:
            hcol = OK if pool["health"] == "ONLINE" else CRIT
            lines.append(
                f"[{hcol}]◈ {pool['name']:<8}[/{hcol}]"
                f"[{hcol}]{pool['health']:<7}[/{hcol}]"
                f"[{WHITE}]{pool['capacity']:>3}%[/{WHITE}]"
            )
        if d.zfs_arc_max:
            arc_pct = d.zfs_arc_size / d.zfs_arc_max * 100
            lines.append(
                f"[{DIM}]arc[/{DIM}]   {pct_bar(arc_pct, 14)}  [{pct_color(arc_pct)}]{arc_pct:>3.0f}%[/]"
                f" [{DIM}]{human_int(d.zfs_arc_size)}[/{DIM}]"
            )
        return "\n".join(lines)


class AlertsCard(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "ALERTS"

    def render(self) -> str:
        d = self.d
        lines = []
        for a in d.alerts[:5]:
            if a.severity == "ok":
                lines.append(f"[{OK}]✓ {a.message:<24}[/{OK}]")
            elif a.severity == "warn":
                lines.append(f"[{WARN}]⚡ {a.message:<24}[/{WARN}]")
            else:
                lines.append(f"[{CRIT}]● {a.message:<24}[/{CRIT}]")
        if d.events:
            lines.append(f"[{DIM2}]{'─' * 22}[/{DIM2}]")
            cols = {"ok": OK, "warn": WARN, "info": INFO, "crit": CRIT}
            for ev in list(d.events)[-3:]:
                c = cols.get(ev.kind, DIM)
                lines.append(f"[{DIM}]{ev.time}[/{DIM}] [{c}]{ev.message[:20]}[/{c}]")
        return "\n".join(lines)


class GraphArea(Static):
    """Four full-width animated graphs: CPU, MEM, NET, DISK."""

    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def on_mount(self) -> None:
        self.border_title = "PERFORMANCE HISTORY"
        self.border_subtitle = "last 120s"

    def render(self) -> str:
        try:
            return self._render_graphs()
        except Exception as exc:
            return f"[{DIM}]graph render error: {exc} — retrying next tick[/{DIM}]"

    def _row(self, label: str, colour: str, val: float, hist: History,
              w: int, extra: str = "") -> list[str]:
        vals = hist.get()
        mn = f"{min(vals):.1f}" if vals else "─"
        mx = f"{max(vals):.1f}" if vals else "─"
        sp = spark_wide(vals, w, colour=colour)
        head = (
            f"[bold {colour}]{label:<10}[/bold {colour}]"
            f"[{pct_color(val)}]{val:>5.1f}%[/]"
            f"  [{DIM}]min[/{DIM}] [{WHITE}]{mn:>5}[/{WHITE}]"
            f"  [{DIM}]max[/{DIM}] [{WHITE}]{mx:>5}[/{WHITE}]"
            f"  {extra}"
        )
        return [head, f"  {sp}"]

    def _render_graphs(self) -> str:
        d = self.d
        w = max(20, self.size.width - 4) if self.size.width else 100

        lines: list[str] = []

        extra = f"[{DIM}]load[/{DIM}] [{WHITE}]{d.load[0]:.2f}[/{WHITE}]"
        lines += self._row("CPU", GOLD, d.cpu_pct, d.cpu_hist, w, extra)
        lines.append("")

        extra = f"[{DIM}]swap[/{DIM}] [{pct_color(d.swap_pct)}]{d.swap_pct:.1f}%[/]"
        lines += self._row("MEMORY", INFO, d.mem_pct, d.mem_hist, w, extra)
        lines.append("")

        up_val, dn_val = d.net_up_hist.last(), d.net_dn_hist.last()
        extra_net = (
            f"[{OK}]▲[/{OK}] [{WHITE}]{human(d.net_up)}/s[/{WHITE}]"
            f"  [{INFO}]▼[/{INFO}] [{WHITE}]{human(d.net_dn)}/s[/{WHITE}]"
            f"  [{DIM}]tx[/{DIM}] [{WHITE}]{human_int(d.net_tx_total)}[/{WHITE}]"
            f"  [{DIM}]rx[/{DIM}] [{WHITE}]{human_int(d.net_rx_total)}[/{WHITE}]"
        )
        lines.append(f"[bold {TEAL}]NETWORK   [/bold {TEAL}]{extra_net}")
        lines.append(f"  {spark_wide(d.net_up_hist.get(), w, colour=OK)}")
        lines.append(f"  {spark_wide(d.net_dn_hist.get(), w, colour=INFO)}")
        lines.append("")

        r_val, w_val = d.disk_r_hist.last(), d.disk_w_hist.last()
        r_actual = r_val / 100 * d._disk_ceil
        w_actual = w_val / 100 * d._disk_ceil
        extra_disk = (
            f"[{OK}]R[/{OK}] [{WHITE}]{human(r_actual)}/s[/{WHITE}]"
            f"  [{INFO}]W[/{INFO}] [{WHITE}]{human(w_actual)}/s[/{WHITE}]"
        )
        lines.append(f"[bold {ORANGE}]DISK I/O  [/bold {ORANGE}]{extra_disk}")
        lines.append(f"  {spark_wide(d.disk_r_hist.get(), w, colour=OK)}")
        lines.append(f"  {spark_wide(d.disk_w_hist.get(), w, colour=INFO)}")

        return "\n".join(lines)


class FooterWidget(Static):
    def __init__(self, d: NodeData, **kw):
        super().__init__(**kw)
        self.d = d

    def render(self) -> str:
        d = self.d
        k = d.kernel[:28] if d.kernel else "─"
        sep = f"  [{DIM2}]│[/{DIM2}]  "
        return (
            f"[{DIM}]kernel[/{DIM}] [{WHITE}]{k}[/{WHITE}]"
            f"{sep}[{DIM}]cpu[/{DIM}] [{pct_color(d.cpu_pct)}]{d.cpu_pct:.0f}%[/]"
            f"{sep}[{DIM}]mem[/{DIM}] [{pct_color(d.mem_pct)}]{d.mem_pct:.0f}%[/]"
            f"{sep}[{DIM}]vm[/{DIM}] [{OK}]{d.vms_running}[/{OK}][{DIM2}]/[/{DIM2}][{WHITE}]{d.vms_total}[/{WHITE}]"
            f"{sep}[{DIM}]ct[/{DIM}] [{OK}]{d.lxc_running}[/{OK}][{DIM2}]/[/{DIM2}][{WHITE}]{d.lxc_total}[/{WHITE}]"
            f"{sep}[{DIM}]root[/{DIM}] [{pct_color(d.root_pct)}]{d.root_pct:.0f}%[/]"
            f"{sep}[{DIM}]swap[/{DIM}] [{pct_color(d.swap_pct)}]{d.swap_pct:.0f}%[/]"
            f"{sep}[{DIM}]tick[/{DIM}] [{WHITE}]{d.tick_n}[/{WHITE}]"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════════

class OperationsDashboard(App):
    CSS = f"""
    Screen {{
        background: {BG};
        color: {WHITE};
        layout: vertical;
    }}

    #header {{
        height: 1;
        background: {PANEL_BG};
        border-bottom: solid {BORDER};
        padding: 0 2;
        content-align: left middle;
    }}

    #footer {{
        height: 1;
        background: {PANEL_BG};
        border-top: solid {BORDER};
        padding: 0 2;
        content-align: left middle;
    }}

    #body {{
        height: 1fr;
        layout: horizontal;
        background: {BG};
    }}

    .side-col {{
        width: 36;
        layout: vertical;
        padding: 1 1;
    }}

    #centre-area {{
        width: 1fr;
        layout: vertical;
        align: center middle;
        content-align: center middle;
        padding: 1 2;
    }}

    #hero {{
        width: 46;
        height: auto;
        border: heavy {GOLD};
        background: {CARD_BG};
        border-title-color: {GOLD};
        border-title-style: bold;
        border-title-align: center;
        border-subtitle-color: {DIM};
        padding: 1 2;
        content-align: center top;
    }}

    .card {{
        height: auto;
        background: {CARD_BG};
        border: round {BORDER};
        border-title-style: bold;
        border-subtitle-color: {DIM};
        padding: 1 2;
        margin-bottom: 1;
    }}

    .card-cpu     {{ border: round {GOLD};   border-title-color: {GOLD}; }}
    .card-mem     {{ border: round {INFO};   border-title-color: {INFO}; }}
    .card-vm      {{ border: round {PURPLE}; border-title-color: {PURPLE}; }}
    .card-net     {{ border: round {TEAL};   border-title-color: {TEAL}; }}
    .card-storage {{ border: round {ORANGE}; border-title-color: {ORANGE}; }}
    .card-alerts  {{ border: round {BORDER}; border-title-color: {WHITE}; }}

    #graphs {{
        height: 1fr;
        min-height: 16;
        margin: 0 2 1 2;
        background: {CARD_BG};
        border: round {BORDER};
        border-title-color: {WHITE};
        border-title-style: bold;
        border-subtitle-color: {DIM};
        padding: 1 2;
    }}
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
            with Vertical(id="left", classes="side-col"):
                yield CpuCard(self.d, classes="card card-cpu", id="cpu-card")
                yield MemCard(self.d, classes="card card-mem", id="mem-card")
                yield VmCard(self.d, classes="card card-vm", id="vm-card")
            with Vertical(id="centre-area"):
                yield HeroPanel(self.d, id="hero")
            with Vertical(id="right", classes="side-col"):
                yield NetCard(self.d, classes="card card-net", id="net-card")
                yield StorageCard(self.d, classes="card card-storage", id="storage-card")
                yield AlertsCard(self.d, classes="card card-alerts", id="alerts-card")
        yield GraphArea(self.d, id="graphs")
        yield FooterWidget(self.d, id="footer")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.d.tick_fast()
        self._slow += 1
        if self._slow % 5 == 0:
            self.d.tick_slow()
        for wid in ("header", "cpu-card", "mem-card", "vm-card",
                    "hero", "net-card", "storage-card",
                    "alerts-card", "graphs", "footer"):
            try:
                self.query_one(f"#{wid}").refresh()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

CRASH_LOG = "/tmp/proxmox-dashboard-crash.log"


def main():
    """Make Textual render to tty1 by redirecting stderr (its fallback path)."""
    import builtins as _B
    _real_open = _B.open

    def _tty_redirect(file, mode="r", *args, **kw):
        if isinstance(file, str) and file == "/dev/tty":
            file = "/dev/tty1"
        return _real_open(file, mode, *args, **kw)

    _B.open = _tty_redirect

    try:
        os.dup2(1, 2)
    except OSError:
        pass

    app = OperationsDashboard()
    try:
        app.run()
    except Exception:
        import traceback
        with open(CRASH_LOG, "a") as f:
            f.write(f"=== crash at {datetime.now()} ===\n")
            traceback.print_exc(file=f)
        raise


if __name__ == "__main__":
    main()
