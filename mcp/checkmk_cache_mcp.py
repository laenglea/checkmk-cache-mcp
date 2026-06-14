#!/usr/bin/env python3
"""
CheckMK Cache MCP Server
========================
Reads current agent-output files from a flat cache directory
(one file per host, named by hostname) and exposes token-efficient tools.

Configuration (env vars):
  CHECKMK_CACHE_DIR    Default: /cachehistory/prodhubli
  CHECKMK_HISTORY_DIR  Default: "" (empty = historical tools disabled)
  CHECKMK_FILE_TZ      Default: Europe/Vaduz (timezone of archive filenames)
  MCP_HOST             Default: 0.0.0.0
  MCP_PORT             Default: 8765
  MCP_API_KEY          Default: "" (no auth)
"""
from __future__ import annotations
import fnmatch
import hmac
import json
import os
import re
import subprocess
import tarfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

BASE_DIR     = Path(os.environ.get("CHECKMK_CACHE_DIR", "/cachehistory/prodhubli"))
_history_env = os.environ.get("CHECKMK_HISTORY_DIR", "")
HISTORY_DIR  = Path(_history_env) if _history_env else None
FILE_TZ      = ZoneInfo(os.environ.get("CHECKMK_FILE_TZ", "Europe/Vaduz"))
MCP_HOST     = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT     = int(os.environ.get("MCP_PORT", "8765"))
API_KEY      = os.environ.get("MCP_API_KEY", "")

_ZSTD_AVAILABLE = bool(subprocess.run(["which", "zstd"], capture_output=True).returncode == 0)
_archive_errors: list[str] = []

SECTION_ALIASES: dict[str, list[str]] = {
    # Linux
    "ps":       ["ps_lnx"],
    "mem":      ["mem"],
    "cpu":      ["cpu"],
    "disk":     ["df_v2", "diskstat"],
    "df":       ["df_v2"],
    "net":      ["lnx_if", "winperf_if"],
    "tcp":      ["tcp_conn_stats"],
    "services": ["systemd_units", "services"],
    "uptime":   ["uptime"],
    "mounts":   ["mounts"],
    "local":    ["local"],
    "kernel":   ["kernel"],
    # Windows-spezifisch
    "wincpu":   ["wmi_cpuload", "winperf_processor"],
    "windisk":  ["winperf_phydisk", "winperf_logicaldisk"],
    "winmem":   ["mem"],
    "winos":    ["wmi_os"],
    "winps":    ["ps"],
    "winnet":   ["winperf_if"],
    "winif":    ["winperf_if"],
}

# Session TTL in seconds (cleanup on each new initialize)
_SESSION_TTL = 3600


# ── File helpers ──────────────────────────────────────────────────────────────

def _read_host(host: str) -> Optional[str]:
    p = BASE_DIR / host
    return p.read_text(errors="replace") if p.is_file() else None


def _cache_age(host: str) -> str:
    p = BASE_DIR / host
    if not p.is_file():
        return "?"
    age = int(datetime.now().timestamp() - p.stat().st_mtime)
    if age < 3600:
        return f"{age // 60}m{age % 60:02d}s ago"
    return f"{age // 3600}h{(age % 3600) // 60}m ago"


# ── Historical archive helpers ──────────────────────────────────────────────

def _parse_file_ts(filename: str) -> Optional[datetime]:
    m = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.", filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=FILE_TZ)
    except ValueError:
        return None


def _host_of_file(filename: str) -> Optional[str]:
    m = re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.(.+)$", filename)
    return m.group(1) if m else None


def _parse_at(at: str) -> Optional[datetime]:
    at_clean = at.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d_%H-%M-%S", "%Y-%m-%d_%H"):
        try:
            return datetime.strptime(at_clean, fmt).replace(tzinfo=FILE_TZ)
        except ValueError:
            continue
    return None


def _buckets_for_range(start: datetime, end: datetime) -> list[tuple[str, Path, bool]]:
    assert HISTORY_DIR is not None
    buckets = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        slot = cursor.strftime("%Y-%m-%d_%H")
        dir_path = HISTORY_DIR / slot
        arc_path = None
        for ext in (".tar.zst", ".tar.gz", ".tar.bz2", ".tar.xz"):
            p = HISTORY_DIR / f"{slot}{ext}"
            if p.is_file():
                arc_path = p
                break
        if dir_path.is_dir():
            buckets.append((slot, dir_path, False))
        elif arc_path:
            buckets.append((slot, arc_path, True))
        cursor += timedelta(hours=1)
    return buckets


def _read_files_from_dir(bucket: Path, host: str,
                         start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    results = []
    for fp in sorted(bucket.iterdir()):
        if not fp.is_file() or _host_of_file(fp.name) != host:
            continue
        ts = _parse_file_ts(fp.name)
        if ts and start <= ts <= end:
            try:
                results.append((ts, fp.read_text(errors="replace")))
            except OSError:
                pass
    return results


def _read_files_from_archive(archive: Path, host: str,
                              start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    results = []
    proc = None
    try:
        if archive.name.endswith(".tar.zst"):
            if not _ZSTD_AVAILABLE:
                raise RuntimeError("zstd binary not found — install zstd to read .tar.zst archives")
            proc = subprocess.Popen(
                ["zstd", "-d", "-c", str(archive)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            tf = tarfile.open(fileobj=proc.stdout, mode="r|")
        else:
            tf = tarfile.open(archive, "r:*")
        with tf:
            for member in tf:
                if not member.isfile():
                    continue
                fname = Path(member.name).name
                if _host_of_file(fname) != host:
                    continue
                ts = _parse_file_ts(fname)
                if not ts or not (start <= ts <= end):
                    continue
                f = tf.extractfile(member)
                if f:
                    results.append((ts, f.read().decode(errors="replace")))
        if proc:
            proc.wait()
    except Exception as e:
        _archive_errors.append(f"{archive.name}: {e}")
    return results


def _get_snapshots(host: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    snaps: list[tuple[datetime, str]] = []
    for _, path, compressed in _buckets_for_range(start, end):
        if compressed:
            snaps.extend(_read_files_from_archive(path, host, start, end))
        else:
            snaps.extend(_read_files_from_dir(path, host, start, end))
    snaps.sort(key=lambda x: x[0])
    return snaps


def _nearest_snap(host: str, at: datetime,
                  window_minutes: int = 10) -> Optional[tuple[datetime, str]]:
    half = timedelta(minutes=window_minutes / 2)
    snaps = _get_snapshots(host, at - half, at + half)
    if not snaps:
        return None
    return min(snaps, key=lambda x: abs((x[0] - at).total_seconds()))


# ── Section parsing ─────────────────────────────────────────────────────────

def parse_section(content: str, section_name: str) -> Optional[str]:
    pattern = re.compile(
        r"^<<<" + re.escape(section_name) + r"(?::[^>]*)?>>\>\n(.*?)(?=^<<<|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    return m.group(1).strip() if m else None


def detect_os(content: str) -> str:
    """Erkennt OS anhand von AgentOS-Header oder vorhandenen Sections."""
    m = re.search(r"^AgentOS:\s*(\S+)", content, re.MULTILINE)
    if m:
        return m.group(1).lower()
    if parse_section(content, "wmi_cpuload") or parse_section(content, "winperf_processor"):
        return "windows"
    if parse_section(content, "ps_lnx"):
        return "linux"
    chk = parse_section(content, "check_mk") or ""
    if "windows" in chk.lower():
        return "windows"
    return "linux"


# ── Linux process parsing ───────────────────────────────────────────────────

def parse_processes_linux(ps_text: str, filter_user: str = None,
                          filter_cmd: str = None,
                          filter_regex: re.Pattern = None) -> list[dict]:
    """Parse ps_lnx section. Columns: CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND"""
    procs = []
    in_procs = False
    for line in ps_text.splitlines():
        if line.startswith("[processes]"):
            in_procs = True
            continue
        if not in_procs or line.startswith("[header]"):
            continue
        if line.startswith("["):
            break
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        _, user, vsz, rss, cpu_time, elapsed, pid, cmd = parts
        if filter_user and not fnmatch.fnmatch(user.lower(), filter_user.lower() if "*" in filter_user else f"*{filter_user.lower()}*"):
            continue
        if filter_cmd and not fnmatch.fnmatch(cmd.lower(), filter_cmd.lower() if "*" in filter_cmd else f"*{filter_cmd.lower()}*"):
            continue
        if filter_regex and not filter_regex.search(cmd):
            continue
        procs.append({
            "pid":      pid,
            "user":     user,
            "vsz_kb":   int(vsz) if vsz.isdigit() else 0,
            "rss_kb":   int(rss) if rss.isdigit() else 0,
            "cpu_time": cpu_time,
            "elapsed":  elapsed,
            "cmd":      cmd,
        })
    return procs


# ── Windows process parsing ─────────────────────────────────────────────────

_WIN_PS_RE = re.compile(
    r"\("
    r"([^,]*)"       # 1  user
    r",(\d+)"        # 2  virtual_size   KiB
    r",(\d+)"        # 3  resident_size  KiB  (Working Set)
    r",([\d.]+)"     # 4  %cpu
    r",(\d+)"        # 5  processID
    r",(\d+)"        # 6  pagefile_usage KiB
    r",(\d+)"        # 7  usermodetime   100ns → /10_000_000 = s
    r",(\d+)"        # 8  kernelmodetime 100ns
    r",(\d+)"        # 9  openHandles
    r",(\d+)"        # 10 threadCount
    r"(?:,(\d+))?"   # 11 processUptime  s (optional, neuere Agenten)
    r"\)"
)


def parse_processes_windows(ps_text: str,
                             filter_cmd: str = None,
                             filter_user: str = None,
                             filter_regex: re.Pattern = None) -> list[dict]:
    procs = []
    for line in ps_text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        meta, name = parts[0], parts[1].strip()
        if filter_cmd and not fnmatch.fnmatch(name.lower(), filter_cmd.lower() if "*" in filter_cmd else f"*{filter_cmd.lower()}*"):
            continue
        if filter_regex and not filter_regex.search(name):
            continue
        m = _WIN_PS_RE.match(meta)
        if not m:
            continue
        user = m.group(1)
        if filter_user and not fnmatch.fnmatch(user.lower(), filter_user.lower() if "*" in filter_user else f"*{filter_user.lower()}*"):
            continue
        procs.append({
            "name":         name,
            "user":         user,
            "virtual_kb":   int(m.group(2)),
            "resident_kb":  int(m.group(3)),
            "cpu_pct":      float(m.group(4)),
            "pid":          m.group(5),
            "pagefile_kb":  int(m.group(6)),
            "usermode_s":   int(m.group(7)) / 10_000_000,
            "kernelmode_s": int(m.group(8)) / 10_000_000,
            "handles":      int(m.group(9)),
            "threads":      int(m.group(10)),
            "uptime_s":     int(m.group(11)) if m.group(11) else None,
        })
    return procs


def aggregate_windows_processes(procs: list[dict]) -> list[dict]:
    """Mehrere Instanzen gleichen Namens zusammenfassen (wie CheckMK)."""
    agg: dict[str, dict] = {}
    for p in procs:
        key = p["name"].lower()
        if key not in agg:
            agg[key] = {**p, "_count": 1}
        else:
            a = agg[key]
            a["virtual_kb"]   += p["virtual_kb"]
            a["resident_kb"]  += p["resident_kb"]
            a["cpu_pct"]      += p["cpu_pct"]
            a["pagefile_kb"]  += p["pagefile_kb"]
            a["usermode_s"]   += p["usermode_s"]
            a["kernelmode_s"] += p["kernelmode_s"]
            a["handles"]      += p["handles"]
            a["threads"]      += p["threads"]
            a["_count"]       += 1
            a["pid"]           = f"({a['_count']} PIDs)"
    return list(agg.values())


# ── Format helpers ──────────────────────────────────────────────────────────

_SORT_KEY_LNX = {"rss": "rss_kb", "vsz": "vsz_kb", "cpu_time": "cpu_time",
                 "elapsed": "elapsed", "cmd": "cmd"}

_SORT_KEY_WIN = {"rss": "resident_kb", "vsz": "virtual_kb", "cpu": "cpu_pct",
                 "pagefile": "pagefile_kb", "name": "name",
                 "handles": "handles", "threads": "threads"}


def _fmt_uptime(seconds_f: float) -> str:
    s = int(seconds_f)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _fmt_uptime_short(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?"
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _fmt_kb(kb: int) -> str:
    if kb > 1_048_576:
        return f"{kb / 1_048_576:.1f} GB"
    if kb > 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def _format_procs_linux(procs: list[dict], limit: int) -> str:
    hdr = f"{'PID':>7} {'USER':<12} {'RSS_KB':>8} {'VSZ_KB':>8} {'CPU':>9} {'ELAPSED':>12}  CMD"
    lines = [hdr, "-" * 74]
    for p in procs[:limit]:
        lines.append(
            f"{p['pid']:>7} {p['user']:<12} {p['rss_kb']:>8} {p['vsz_kb']:>8} "
            f"{p['cpu_time']:>9} {p['elapsed']:>12}  {p['cmd'][:55]}"
        )
    if len(procs) > limit:
        lines.append(f"  … {len(procs) - limit} more (increase limit to see all)")
    return "\n".join(lines)


def _format_procs_windows(procs: list[dict], limit: int,
                           aggregate: bool = False) -> str:
    if aggregate:
        procs = aggregate_windows_processes(procs)
    hdr = (f"{'PID':>10} {'USER':<28} {'RSS_MB':>7} {'VIRT_MB':>8} "
           f"{'PF_MB':>6} {'CPU%':>5} {'U_cpu_s':>9} {'K_cpu_s':>9} "
           f"{'HDL':>5} {'THR':>4} {'UPTIME':>9}  NAME")
    lines = [hdr, "-" * len(hdr)]
    for p in procs[:limit]:
        cnt = p.get("_count", 1)
        cnt_s = f" x{cnt}" if cnt > 1 else ""
        lines.append(
            f"{p['pid']:>10} {p['user']:<28} "
            f"{p['resident_kb'] / 1024:>7.1f} {p['virtual_kb'] / 1024:>8.1f} "
            f"{p['pagefile_kb'] / 1024:>6.1f} {p['cpu_pct']:>5.1f} "
            f"{p['usermode_s']:>9.1f} {p['kernelmode_s']:>9.1f} "
            f"{p['handles']:>5} {p['threads']:>4} "
            f"{_fmt_uptime_short(p['uptime_s']):>9}  {p['name'][:45]}{cnt_s}"
        )
    if len(procs) > limit:
        lines.append(f"  … {len(procs) - limit} more (increase limit to see all)")
    return "\n".join(lines)


# ── Overview helpers ───────────────────────────────────────────────────────

def _overview_agent(content: str) -> list[str]:
    sec = parse_section(content, "check_mk") or ""
    d: dict[str, str] = {}
    for line in sec.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            d[k] = v.strip()
    return [f"  {k:<12} {d[k]}"
            for k in ("Hostname", "AgentOS", "OSName", "OSVersion", "Version")
            if k in d]


def _overview_uptime(content: str, os_type: str) -> list[str]:
    sec = parse_section(content, "uptime") or ""
    parts = sec.split()
    if parts:
        try:
            return [f"  {'Uptime':<12} {_fmt_uptime(float(parts[0]))}"]
        except ValueError:
            pass
    return []


def _cpu_from_wmi_cpuload(content: str) -> list[str]:
    """Liest CPU-Info aus wmi_cpuload Section."""
    sec = parse_section(content, "wmi_cpuload") or ""
    if not sec:
        return []
    queue_len: Optional[int] = None
    logical_cpus: Optional[int] = None
    physical_cpus: Optional[int] = None
    section_name = ""
    headers: list[str] = []
    for line in sec.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1]
            headers = []
            continue
        if "|" in line:
            parts = line.split("|")
            if not headers:
                headers = [h.strip() for h in parts]
                continue
            row = dict(zip(headers, [p.strip() for p in parts]))
            if section_name == "system_perf":
                try:
                    queue_len = int(row.get("ProcessorQueueLength", "x"))
                except ValueError:
                    pass
            elif section_name == "computer_system":
                try:
                    logical_cpus = int(row.get("NumberOfLogicalProcessors", "x"))
                except ValueError:
                    pass
                try:
                    physical_cpus = int(row.get("NumberOfProcessors", "x"))
                except ValueError:
                    pass
    if logical_cpus is not None:
        cpu_info = f"{logical_cpus} logical"
        if physical_cpus is not None:
            cpu_info += f" / {physical_cpus} physical"
        queue_info = f"  queue={queue_len}" if queue_len is not None else ""
        return [f"  {'CPU':<12} {cpu_info}{queue_info}"]
    return []


def _overview_cpu(content: str, os_type: str) -> list[str]:
    if os_type == "windows":
        # Primär: wmi_cpuload (liefert Kernanzahl + Queue-Length)
        lines = _cpu_from_wmi_cpuload(content)
        if lines:
            return lines
        # Fallback: winperf_processor (Kernanzahl aus Zeilenanzahl schätzen)
        wp = parse_section(content, "winperf_processor") or ""
        if wp:
            # Datenzeilen (beginnen mit Zahl) ≈ Anzahl Kerne + 1 Summierzeile
            data_lines = [l for l in wp.strip().splitlines()
                          if l and l[0].isdigit()]
            # Letzte Zeile ist oft "_Total" → abziehen
            core_count = max(len(data_lines) - 1, 1)
            return [f"  {'CPU':<12} ~{core_count} cores (via winperf_processor, kein WMI)"]
        return [f"  {'CPU':<12} (wmi_cpuload und winperf_processor nicht verfügbar)"]
    else:
        sec = parse_section(content, "cpu") or ""
        parts = sec.split()
        if len(parts) >= 3:
            return [f"  {'Load':<12} {parts[0]} / {parts[1]} / {parts[2]}  (1/5/15 min)"]
        return []


def _overview_mem(content: str, os_type: str) -> list[str]:
    sec = parse_section(content, "mem") or ""
    m: dict[str, int] = {}
    for line in sec.splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                m[p[0].rstrip(":")] = int(p[1])
            except ValueError:
                pass
    if not m:
        return []
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable") or m.get("MemFree", 0)
    used  = total - avail
    pct   = f"{100 * used / total:.0f}%" if total else "?"
    mem_note = " (MemFree)" if "MemAvailable" not in m and os_type == "windows" else ""
    lines = [f"  {'RAM':<12} {_fmt_kb(used)} / {_fmt_kb(total)} used ({pct}){mem_note}"]
    swap_total = m.get("SwapTotal", 0)
    swap_free  = m.get("SwapFree", 0)
    # Windows-Agent liefert manchmal absurd große SwapFree-Werte (Bug) → ignorieren
    if swap_total and swap_free <= swap_total:
        swap_used = swap_total - swap_free
        lines.append(f"  {'Swap':<12} {_fmt_kb(swap_used)} / {_fmt_kb(swap_total)}"
                     f" used ({100 * swap_used / swap_total:.0f}%)")
    return lines


def _parse_winperf_logicaldisk(sec: str) -> list[tuple[str, str]]:
    """
    Extrahiert Laufwerksbelegung aus winperf_logicaldisk.
    Sucht Zeilen die mit einem Laufwerksbuchstaben (z.B. 'C:') beginnen
    und einen Prozentwert (0-100) enthalten.
    """
    results = []
    for line in sec.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^[A-Z]:$", parts[0]):
            for p in parts[1:]:
                try:
                    val = int(p)
                    if 0 <= val <= 100:
                        results.append((parts[0], f"{val}%"))
                        break
                except ValueError:
                    continue
    return sorted(results, key=lambda x: int(x[1].rstrip("%")), reverse=True)


def _overview_df(content: str, os_type: str) -> list[str]:
    # df_v2 funktioniert auf Linux und manchmal auch auf Windows-Agenten
    sec = parse_section(content, "df_v2") or parse_section(content, "df") or ""
    mounts: list[tuple[int, str, str]] = []
    for line in sec.splitlines():
        if line.startswith("["):
            break
        parts = line.split()
        if len(parts) >= 6:
            pct_s = parts[-2].rstrip("%")
            try:
                mounts.append((int(pct_s), parts[-1], parts[-2]))
            except ValueError:
                pass
    if mounts:
        mounts.sort(reverse=True)
        return [f"  {'Disk':<12} {mount:<30} {pct_s:>4}%"
                for _, mount, pct_s in mounts[:5]]

    if os_type == "windows":
        # Fallback 1: winperf_logicaldisk (neuere Windows-Agenten)
        ld = parse_section(content, "winperf_logicaldisk") or ""
        if ld:
            ld_mounts = _parse_winperf_logicaldisk(ld)
            if ld_mounts:
                return [f"  {'Disk':<12} {drive:<30} {pct:>4}"
                        for drive, pct in ld_mounts[:5]]
        # Fallback 2: winperf_phydisk vorhanden, aber kein Belegungswert aus Einzelsnapshot
        if parse_section(content, "winperf_phydisk"):
            return [f"  {'Disk':<12} (nur winperf_phydisk – Belegung% nicht aus Einzelsnapshot berechenbar)"]
        return [f"  {'Disk':<12} (keine df/df_v2/winperf_logicaldisk Section verfügbar)"]
    return []


# ── Tool implementations ────────────────────────────────────────────────────

def tool_list_hosts(pattern: str = None) -> str:
    if not BASE_DIR.exists():
        return f"Cache directory not found: {BASE_DIR}"
    hosts = sorted(p.name for p in BASE_DIR.iterdir() if p.is_file())
    if pattern:
        hosts = [h for h in hosts if fnmatch.fnmatch(h.lower(), pattern.lower())]
    if not hosts:
        return (f"No hosts matching '{pattern}' in {BASE_DIR}"
                if pattern else f"No host files found in {BASE_DIR}")
    label = f"Hosts in {BASE_DIR} ({len(hosts)}" + (f", filter='{pattern}'" if pattern else "") + "):"
    lines = [label]
    for h in hosts:
        lines.append(f"  {h:<40} {_cache_age(h)}")
    return "\n".join(lines)


def tool_get_overview(host: str) -> str:
    content = _read_host(host)
    if not content:
        return f"Host '{host}' not found in {BASE_DIR}"
    os_type = detect_os(content)
    lines = [f"=== {host}  (cache: {_cache_age(host)}, OS: {os_type}) ==="]
    lines += _overview_agent(content)
    lines += _overview_uptime(content, os_type)
    lines += _overview_cpu(content, os_type)
    lines += _overview_mem(content, os_type)
    lines += _overview_df(content, os_type)
    return "\n".join(lines)


def tool_get_processes(host: str, sort_by: str = "rss", limit: int = 15,
                       filter_cmd: str = None, filter_user: str = None,
                       filter_regex: str = None, aggregate: bool = False) -> str:
    content = _read_host(host)
    if not content:
        return f"Host '{host}' not found in {BASE_DIR}"

    compiled = None
    if filter_regex:
        try:
            compiled = re.compile(filter_regex)
        except re.error as e:
            return f"Invalid filter_regex: {e}"

    os_type = detect_os(content)

    if os_type == "windows":
        ps_raw = parse_section(content, "ps") or ""
        if not ps_raw:
            return f"No 'ps' section found for Windows host '{host}'"
        procs = parse_processes_windows(ps_raw, filter_cmd, filter_user, compiled)
        if not procs:
            return f"No matching processes on '{host}'"
        sort_key = _SORT_KEY_WIN.get(sort_by, "resident_kb")
        reverse  = sort_key != "name"
        procs.sort(key=lambda p: p[sort_key], reverse=reverse)
        header = (f"Host: {host}  OS: windows  total={len(procs)}"
                  f"  sort={sort_by}  showing={min(limit, len(procs))}"
                  f"{'  [aggregated]' if aggregate else ''}")
        return header + "\n" + _format_procs_windows(procs, limit, aggregate)
    else:
        ps_raw = parse_section(content, "ps_lnx") or ""
        if not ps_raw:
            return f"No ps_lnx section found for '{host}'"
        procs = parse_processes_linux(ps_raw, filter_user, filter_cmd, compiled)
        if not procs:
            return f"No matching processes on '{host}'"
        sort_key = _SORT_KEY_LNX.get(sort_by, "rss_kb")
        procs.sort(key=lambda p: p[sort_key], reverse=sort_key not in ("cmd",))
        header = (f"Host: {host}  OS: linux  total={len(procs)}"
                  f"  sort={sort_by}  showing={min(limit, len(procs))}"
                  + ("  [note: aggregate ignored on Linux]" if aggregate else ""))
        return header + "\n" + _format_procs_linux(procs, limit)


def tool_list_sections(host: str) -> str:
    content = _read_host(host)
    if not content:
        return f"Host '{host}' not found in {BASE_DIR}"
    sections = re.findall(r"^<<<([^>]+)>>>", content, re.MULTILINE)
    if not sections:
        return f"No sections found for host '{host}'"
    lines = [f"Sections for {host} ({len(sections)}):"]
    for s in sections:
        lines.append(f"  {s}")
    return "\n".join(lines)


def tool_get_section(host: str, section: str) -> str:
    content = _read_host(host)
    if not content:
        return f"Host '{host}' not found in {BASE_DIR}"
    names = SECTION_ALIASES.get(section, [section])
    parts = []
    for name in names:
        raw = parse_section(content, name)
        if raw:
            parts.append(f"<<<{name}>>>\n{raw}")
    if not parts:
        return f"Section '{section}' not found for host '{host}'"
    return "\n\n".join(parts)


# ── History range helpers ─────────────────────────────────────────────────

def _extract_mem(content: str) -> Optional[tuple[int, int]]:
    """Returns (used_kb, total_kb) or None."""
    sec = parse_section(content, "mem") or ""
    m: dict[str, int] = {}
    for line in sec.splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                m[p[0].rstrip(":")] = int(p[1])
            except ValueError:
                pass
    total = m.get("MemTotal", 0)
    if not total:
        return None
    avail = m.get("MemAvailable") or m.get("MemFree", 0)
    return total - avail, total


def _extract_cpu(content: str, os_type: str) -> Optional[str]:
    """Returns a short CPU load string or None."""
    if os_type == "windows":
        sec = parse_section(content, "wmi_cpuload") or ""
        for line in sec.splitlines():
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[0].isdigit():
                    try:
                        return f"queue={int(parts[0])}"
                    except ValueError:
                        pass
        return None
    else:
        sec = parse_section(content, "cpu") or ""
        parts = sec.split()
        if len(parts) >= 3:
            return f"{parts[0]} / {parts[1]} / {parts[2]}"
        return None


def _pick_samples(snaps: list[tuple[datetime, str]],
                  start: datetime, end: datetime,
                  n: int) -> list[tuple[datetime, str]]:
    """Pick n evenly distributed snapshots across [start, end]."""
    if not snaps or n <= 0:
        return []
    if len(snaps) <= n:
        return snaps
    span = (end - start).total_seconds()
    selected = []
    for i in range(n):
        target = start + timedelta(seconds=span * (i + 0.5) / n)
        closest = min(snaps, key=lambda x: abs((x[0] - target).total_seconds()))
        if not selected or selected[-1][0] != closest[0]:
            selected.append(closest)
    return selected


def tool_get_history_range(host: str, metric: str, frm: str, to: str,
                            samples: int = 10) -> str:
    if not HISTORY_DIR:
        return "CHECKMK_HISTORY_DIR not configured."
    if metric not in ("mem", "cpu"):
        return "metric must be 'mem' or 'cpu'."
    start = _parse_at(frm)
    end   = _parse_at(to)
    if not start:
        return f"Timestamp 'from' not recognised: '{frm}'"
    if not end:
        return f"Timestamp 'to' not recognised: '{to}'"
    if end <= start:
        return "'to' must be after 'from'."
    samples = max(2, min(samples, 50))

    snaps = _get_snapshots(host, start, end)
    if not snaps:
        return f"No snapshots found for '{host}' between {frm} and {to}."

    chosen = _pick_samples(snaps, start, end, samples)
    os_type = detect_os(chosen[0][1])

    header = (f"=== {host} — {metric} — "
              f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} "
              f"({len(chosen)} samples, OS: {os_type}) ===")
    lines = [header]

    if metric == "mem":
        values = []
        for ts, content in chosen:
            r = _extract_mem(content)
            if r:
                values.append((ts, r[0], r[1]))
        if not values:
            return f"No mem data found for '{host}' in the given range."
        peak_ts = max(values, key=lambda x: x[1])[0]
        for ts, used, total in values:
            pct = 100 * used / total if total else 0
            marker = "  ← peak" if ts == peak_ts else ""
            lines.append(f"  {ts.strftime('%H:%M:%S')}   "
                         f"{_fmt_kb(used):>10} / {_fmt_kb(total):>10}  ({pct:4.0f}%){marker}")

    else:  # cpu
        values = []
        for ts, content in chosen:
            r = _extract_cpu(content, os_type)
            if r:
                values.append((ts, r))
        if not values:
            return f"No cpu data found for '{host}' in the given range."
        label = "load 1/5/15 min" if os_type != "windows" else "processor queue"
        lines.append(f"  {'time':<10}  {label}")
        for ts, val in values:
            lines.append(f"  {ts.strftime('%H:%M:%S')}   {val}")

    return "\n".join(lines)


# ── Historical tool functions ─────────────────────────────────────────────

def tool_get_history_overview(host: str, at: str, window_minutes: int = 10) -> str:
    if not HISTORY_DIR:
        return "CHECKMK_HISTORY_DIR not configured."
    center = _parse_at(at)
    if not center:
        return f"Timestamp '{at}' not recognised. Example: '2026-05-16T13:30:00'"
    snap = _nearest_snap(host, center, window_minutes)
    if not snap:
        return f"No snapshot found for '{host}' around {at} (±{window_minutes // 2}min)"
    ts, content = snap
    os_type = detect_os(content)
    lines = [f"=== {host}  (snapshot: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}, OS: {os_type}) ==="]
    lines += _overview_agent(content)
    lines += _overview_uptime(content, os_type)
    lines += _overview_cpu(content, os_type)
    lines += _overview_mem(content, os_type)
    lines += _overview_df(content, os_type)
    return "\n".join(lines)


def tool_get_history_processes(host: str, at: str, sort_by: str = "rss",
                                limit: int = 15, filter_cmd: str = None,
                                filter_user: str = None, filter_regex: str = None,
                                window_minutes: int = 10,
                                aggregate: bool = False) -> str:
    if not HISTORY_DIR:
        return "CHECKMK_HISTORY_DIR not configured."
    compiled = None
    if filter_regex:
        try:
            compiled = re.compile(filter_regex)
        except re.error as e:
            return f"Invalid filter_regex: {e}"
    center = _parse_at(at)
    if not center:
        return f"Timestamp '{at}' not recognised."
    snap = _nearest_snap(host, center, window_minutes)
    if not snap:
        return f"No snapshot found for '{host}' around {at}"
    ts, content = snap
    os_type = detect_os(content)

    if os_type == "windows":
        ps_raw = parse_section(content, "ps") or ""
        if not ps_raw:
            return f"No 'ps' section in snapshot for '{host}' at {ts}"
        procs = parse_processes_windows(ps_raw, filter_cmd, filter_user, compiled)
        if not procs:
            return "No matching processes"
        sort_key = _SORT_KEY_WIN.get(sort_by, "resident_kb")
        procs.sort(key=lambda p: p[sort_key], reverse=sort_key != "name")
        header = (f"Host: {host}  OS: windows  snapshot: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                  f"  total={len(procs)}  sort={sort_by}  showing={min(limit, len(procs))}"
                  f"{'  [aggregated]' if aggregate else ''}")
        return header + "\n" + _format_procs_windows(procs, limit, aggregate)
    else:
        ps_raw = parse_section(content, "ps_lnx") or ""
        if not ps_raw:
            return f"No ps_lnx section in snapshot for '{host}' at {ts}"
        procs = parse_processes_linux(ps_raw, filter_user, filter_cmd, compiled)
        if not procs:
            return "No matching processes"
        sort_key = _SORT_KEY_LNX.get(sort_by, "rss_kb")
        procs.sort(key=lambda p: p[sort_key], reverse=sort_key not in ("cmd",))
        header = (f"Host: {host}  OS: linux  snapshot: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                  f"  total={len(procs)}  sort={sort_by}  showing={min(limit, len(procs))}"
                  + ("  [note: aggregate ignored on Linux]" if aggregate else ""))
        return header + "\n" + _format_procs_linux(procs, limit)


def tool_get_history_section(host: str, at: str, section: str,
                              window_minutes: int = 10) -> str:
    if not HISTORY_DIR:
        return "CHECKMK_HISTORY_DIR not configured."
    center = _parse_at(at)
    if not center:
        return f"Timestamp '{at}' not recognised."
    snap = _nearest_snap(host, center, window_minutes)
    if not snap:
        return f"No snapshot found for '{host}' around {at}"
    ts, content = snap
    names = SECTION_ALIASES.get(section, [section])
    parts = []
    for name in names:
        raw = parse_section(content, name)
        if raw:
            parts.append(f"<<<{name}>>>\n{raw}")
    if not parts:
        return f"Section '{section}' not found in snapshot for '{host}' at {ts}"
    header = f"# snapshot: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    return header + "\n\n".join(parts)


# ── Tool definitions (MCP schema) ───────────────────────────────────────────

_HISTORY_TOOLS = [
    {
        "name": "get_history_overview",
        "description": (
            "Overview for a host at a specific point in time (reads from archive). "
            "Same format as get_overview. at: ISO-8601 e.g. '2026-05-16T13:30:00'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":           {"type": "string"},
                "at":             {"type": "string", "description": "ISO-8601 timestamp"},
                "window_minutes": {"type": "integer", "default": 10},
            },
            "required": ["host", "at"],
        },
    },
    {
        "name": "get_history_processes",
        "description": (
            "Process list at a specific point in time (reads from archive). "
            "Automatically detects Windows or Linux. "
            "Windows sort_by: rss (resident/working-set), vsz (virtual), cpu, pagefile, name, handles, threads. "
            "Linux sort_by: rss, vsz, cpu_time, elapsed, cmd. "
            "filter_cmd OR filter_regex (not both — AND logic). "
            "aggregate=true (Windows only, ignored on Linux): groups multiple instances of the same process name. "
            "window_minutes: search radius around 'at' to find the nearest snapshot (default 10)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":           {"type": "string"},
                "at":             {"type": "string"},
                "sort_by":        {"type": "string", "default": "rss"},
                "limit":          {"type": "integer", "default": 15, "minimum": 1, "maximum": 500},
                "filter_cmd":     {"type": "string"},
                "filter_regex":   {"type": "string"},
                "filter_user":    {"type": "string"},
                "aggregate":      {"type": "boolean", "default": False,
                                   "description": "Windows only: group by process name"},
                "window_minutes": {"type": "integer", "default": 10},
            },
            "required": ["host", "at"],
        },
    },
    {
        "name": "get_history_section",
        "description": (
            "Raw CheckMK section at a specific point in time (reads from archive). "
            "Same section aliases as get_section."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":           {"type": "string"},
                "at":             {"type": "string"},
                "section":        {"type": "string"},
                "window_minutes": {"type": "integer", "default": 10},
            },
            "required": ["host", "at", "section"],
        },
    },
    {
        "name": "get_history_range",
        "description": (
            "Time series of RAM or CPU over a time range — use this for trend analysis, NOT for process lists. "
            "metric: 'mem' (RAM used/total/%) or 'cpu' (load averages on Linux, processor queue on Windows). "
            "from/to: ISO-8601 range, e.g. '2026-06-14T14:00' / '2026-06-14T15:00'. "
            "samples: evenly distributed data points (default 10, max 50). "
            "Does NOT support sort_by, limit, filter_cmd, filter_user, aggregate, or window_minutes — use get_history_processes for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":    {"type": "string"},
                "metric":  {"type": "string", "enum": ["mem", "cpu"]},
                "from":    {"type": "string", "description": "Start timestamp (ISO-8601)"},
                "to":      {"type": "string", "description": "End timestamp (ISO-8601)"},
                "samples": {"type": "integer", "default": 10, "minimum": 2, "maximum": 50},
            },
            "required": ["host", "metric", "from", "to"],
        },
    },
]

TOOLS = [
    {
        "name": "list_hosts",
        "description": (
            "List available hosts in the cache directory with cache file age. "
            "Supports glob pattern filtering, e.g. pattern='web*' or pattern='*prod*'. "
            "Omit pattern to list all hosts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Optional glob pattern (case-insensitive), e.g. 'web*', '*db*'"},
            },
            "required": [],
        },
    },
    {
        "name": "list_sections",
        "description": (
            "List all CheckMK sections present in the cached agent output for a host. "
            "Only call this if you are unsure which sections exist — skip it when the section name is already known."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"host": {"type": "string"}},
            "required": ["host"],
        },
    },
    {
        "name": "get_overview",
        "description": (
            "Compact single-call overview: hostname, OS, uptime, CPU load, "
            "RAM/swap usage, and top-5 disk mounts. Works for both Linux and Windows hosts. "
            "Use this first before fetching detailed data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"host": {"type": "string", "description": "Hostname as listed by list_hosts"}},
            "required": ["host"],
        },
    },
    {
        "name": "get_processes",
        "description": (
            "Process list, sorted and limited server-side. Automatically detects Windows or Linux. "
            "Windows sort_by: rss (resident/working-set, default), vsz (virtual), cpu, pagefile, name, handles, threads. "
            "Linux sort_by: rss (default), vsz, cpu_time, elapsed, cmd. "
            "filter_cmd: case-insensitive substring or glob (e.g. 'java', 'java*', '*agent*'). "
            "filter_regex: Python regex on command/process name — use either filter_cmd OR filter_regex, not both (AND logic). "
            "filter_user: case-insensitive glob on username (e.g. 'oracle', 'svc_*', '*'). "
            "aggregate=true (Windows only, ignored on Linux): groups multiple instances of the same process name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":         {"type": "string"},
                "sort_by":      {"type": "string", "default": "rss"},
                "limit":        {"type": "integer", "default": 15, "minimum": 1, "maximum": 500},
                "filter_cmd":   {"type": "string"},
                "filter_regex": {"type": "string"},
                "filter_user":  {"type": "string"},
                "aggregate":    {"type": "boolean", "default": False,
                                 "description": "Windows only: group by process name"},
            },
            "required": ["host"],
        },
    },
    {
        "name": "get_section",
        "description": (
            "Raw CheckMK section content for detailed inspection. "
            "Linux aliases: ps | mem | cpu | disk | df | net | tcp | services | uptime | mounts | local | kernel. "
            "Windows aliases: winps | winmem | wincpu | windisk | winnet | winif | winos. "
            "Or use any direct section name like 'df_v2', 'winperf_phydisk', 'winperf_logicaldisk', "
            "'wmi_cpuload', 'winperf_if', 'winperf_processor'. "
            "Note: df/df_v2 is typically absent on Windows hosts; use windisk for disk data instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host":    {"type": "string"},
                "section": {"type": "string"},
            },
            "required": ["host", "section"],
        },
    },
]

if HISTORY_DIR:
    TOOLS += _HISTORY_TOOLS


def dispatch(name: str, args: dict) -> str:
    if name == "list_hosts":
        return tool_list_hosts(args.get("pattern"))
    if name == "list_sections":
        return tool_list_sections(args["host"])
    if name == "get_overview":
        return tool_get_overview(args["host"])
    if name == "get_processes":
        return tool_get_processes(
            args["host"],
            args.get("sort_by", "rss"),
            args.get("limit", 15),
            args.get("filter_cmd"),
            args.get("filter_user"),
            args.get("filter_regex"),
            args.get("aggregate", False),
        )
    if name == "get_section":
        return tool_get_section(args["host"], args["section"])
    if name == "get_history_overview":
        at = args.get("at") or args.get("from")
        if not at:
            return "Error: missing required argument 'at' (ISO-8601 timestamp)"
        return tool_get_history_overview(args["host"], at, args.get("window_minutes", 10))
    if name == "get_history_processes":
        at = args.get("at") or args.get("from")
        if not at:
            return "Error: missing required argument 'at' (ISO-8601 timestamp)"
        return tool_get_history_processes(
            args["host"], at,
            args.get("sort_by", "rss"),
            args.get("limit", 15),
            args.get("filter_cmd"),
            args.get("filter_user"),
            args.get("filter_regex"),
            args.get("window_minutes", 10),
            args.get("aggregate", False),
        )
    if name == "get_history_section":
        at = args.get("at") or args.get("from")
        if not at:
            return "Error: missing required argument 'at' (ISO-8601 timestamp)"
        section = args.get("section")
        if not section:
            return "Error: missing required argument 'section'"
        return tool_get_history_section(args["host"], at, section, args.get("window_minutes", 10))
    if name == "get_history_range":
        frm = args.get("from") or args.get("at")
        to  = args.get("to")
        metric = args.get("metric")
        if not frm or not to:
            return "Error: missing required arguments 'from' and 'to' (ISO-8601 timestamps)"
        if not metric:
            return "Error: missing required argument 'metric' ('mem' or 'cpu')"
        return tool_get_history_range(args["host"], metric, frm, to, args.get("samples", 10))
    return f"Unknown tool: {name}"


# ── HTTP server ───────────────────────────────────────────────────────────

try:
    import uvicorn
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import StreamingResponse
except ImportError:
    raise SystemExit("Missing packages: pip install fastapi uvicorn")

app = FastAPI(title="CheckMK Cache MCP", version="2.1.0", redirect_slashes=False)

# Sessions: id → created timestamp (float). Cleaned up on each new initialize.
_sessions: dict[str, float] = {}


def _cleanup_sessions() -> None:
    """Entfernt abgelaufene Sessions (TTL: _SESSION_TTL Sekunden)."""
    now = datetime.now().timestamp()
    expired = [sid for sid, ts in _sessions.items() if now - ts > _SESSION_TTL]
    for sid in expired:
        del _sessions[sid]


def _cors() -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, Mcp-Protocol-Version",
    }


def _sse(data: dict):
    yield "data: " + json.dumps(data) + "\n\n"


def _check_auth(request: Request) -> bool:
    if not API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return hmac.compare_digest(auth[7:], API_KEY)
    return hmac.compare_digest(request.headers.get("x-api-key", ""), API_KEY)


@app.api_route("/mcp", methods=["OPTIONS", "GET", "POST"])
@app.api_route("/mcp/", methods=["OPTIONS", "GET", "POST"])
async def mcp_endpoint(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=200, headers=_cors())
    if not _check_auth(request):
        payload = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "Unauthorized"}}
        return StreamingResponse(_sse(payload), media_type="text/event-stream",
                                 status_code=401,
                                 headers={**_cors(), "WWW-Authenticate": "Bearer"})
    if request.method == "GET":
        return StreamingResponse(_sse({}), media_type="text/event-stream", headers=_cors())
    try:
        body = await request.json()
    except Exception:
        payload = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}}
        return StreamingResponse(_sse(payload), media_type="text/event-stream",
                                 status_code=400, headers=_cors())

    rpc_id = body.get("id")
    method  = body.get("method", "")
    params  = body.get("params", {})

    if rpc_id is None and method.startswith("notifications/"):
        return StreamingResponse(_sse({}), media_type="text/event-stream", headers=_cors())

    session_id = None
    if method == "initialize":
        _cleanup_sessions()
        session_id = str(uuid.uuid4())
        _sessions[session_id] = datetime.now().timestamp()
        result = {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "checkmk-cache", "version": "2.1.0"},
            "capabilities": {"tools": {}},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            text = dispatch(tool_name, arguments)
        except Exception as e:
            text = f"Error: {e}"
        result = {"content": [{"type": "text", "text": text}],
                  "isError": isinstance(text, str) and re.match(r"^(Error|Unknown tool):", text) is not None}
    else:
        result = {"code": -32601, "message": f"Unknown method: {method}"}

    if isinstance(result, dict) and "code" in result and "message" in result:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "error": result}
    else:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    headers = _cors()
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return StreamingResponse(_sse(payload), media_type="text/event-stream", headers=headers)


@app.get("/health")
async def health():
    hosts = sorted(p.name for p in BASE_DIR.iterdir() if p.is_file()) if BASE_DIR.exists() else []
    recent_errors = _archive_errors[-10:] if _archive_errors else []
    return {
        "status": "degraded" if recent_errors else "ok",
        "cache_dir": str(BASE_DIR),
        "history_dir": str(HISTORY_DIR) if HISTORY_DIR else None,
        "zstd_available": _ZSTD_AVAILABLE,
        "host_count": len(hosts),
        "hosts": hosts,
        "recent_archive_errors": recent_errors,
    }


if __name__ == "__main__":
    n_hist = len(_HISTORY_TOOLS) if HISTORY_DIR else 0
    print("CheckMK Cache MCP  v2.1 (Windows+Linux)")
    print(f"  Cache:    {BASE_DIR}")
    print(f"  History:  {HISTORY_DIR or '(disabled — set CHECKMK_HISTORY_DIR to enable)'}")
    n_current = len(TOOLS) - (n_hist if HISTORY_DIR else 0)
    print(f"  Tools:    {len(TOOLS)} ({n_current} current{f' + {n_hist} historical' if HISTORY_DIR else ''})")
    print(f"  Endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")
    print(f"  Health:   http://{MCP_HOST}:{MCP_PORT}/health")
    print(f"  API-Key:  {'(set)' if API_KEY else '(none — set MCP_API_KEY to enable auth)'}")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
