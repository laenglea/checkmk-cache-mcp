#!/usr/bin/env python3
"""
MCP Server Integration Test
============================
Tests all tools against a running checkmk-cache-mcp server.

Usage:
    python3 test_mcp.py [--url http://localhost:8765] [--api-key KEY] [--history-at 2026-06-14T10:00]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta

WINDOWS_HOST = "windowsbox"
LINUX_HOST   = "linuxserver1"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
WARN = "\033[33mWARN\033[0m"


def call(url: str, tool: str, args: dict, api_key: str = "") -> tuple[bool, str]:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
    except urllib.error.URLError as e:
        return False, f"Connection error: {e}"

    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                result = data.get("result", {})
                content = result.get("content", [{}])
                text = content[0].get("text", "") if content else ""
                is_error = result.get("isError", False)
                return not is_error, text
            except json.JSONDecodeError:
                pass
    return False, f"Unparseable response: {raw[:200]}"


def check(label: str, ok: bool, text: str, expect: str = None, warn_if: str = None):
    if not ok:
        print(f"  [{FAIL}] {label}")
        print(f"         → {text[:120]}")
        return False
    if expect and expect.lower() not in text.lower():
        print(f"  [{FAIL}] {label}  (expected '{expect}' in output)")
        print(f"         → {text[:120]}")
        return False
    if warn_if and warn_if.lower() in text.lower():
        print(f"  [{WARN}] {label}  (unexpected content: '{warn_if}')")
        print(f"         → {text[:120]}")
        return True
    print(f"  [{PASS}] {label}")
    return True


def run(url: str, api_key: str, history_at: str):
    results = []

    def t(label, tool, args, expect=None, warn_if=None):
        ok, text = call(url, tool, args, api_key)
        results.append(check(label, ok, text, expect, warn_if))

    # ── Health check ────────────────────────────────────────────────────────
    print("\n── Health ─────────────────────────────────────────────────────")
    try:
        req = urllib.request.Request(
            url.replace("/mcp", "/health"),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read())
        status = health.get("status", "?")
        zstd   = health.get("zstd_available", "?")
        n_hosts = health.get("host_count", 0)
        errors  = health.get("recent_archive_errors", [])
        color = "\033[32m" if status == "ok" else "\033[33m"
        print(f"  [{color}{status}\033[0m] status={status}  zstd={zstd}  hosts={n_hosts}")
        if errors:
            print(f"  [{WARN}] archive errors: {errors[:3]}")
    except Exception as e:
        print(f"  [{FAIL}] /health unreachable: {e}")
        results.append(False)

    # ── list_hosts ──────────────────────────────────────────────────────────
    print("\n── list_hosts ─────────────────────────────────────────────────")
    t("list_hosts (all)",          "list_hosts", {}, expect=WINDOWS_HOST)
    t("list_hosts pattern match",  "list_hosts", {"pattern": "windows*"}, expect=WINDOWS_HOST)
    t("list_hosts pattern no-hit", "list_hosts", {"pattern": "zzz_nomatch_zzz"}, warn_if=WINDOWS_HOST)

    # ── list_sections ───────────────────────────────────────────────────────
    print("\n── list_sections ──────────────────────────────────────────────")
    t("list_sections windows", "list_sections", {"host": WINDOWS_HOST}, expect="wmi_cpuload")
    t("list_sections linux",   "list_sections", {"host": LINUX_HOST},   expect="ps_lnx")
    t("list_sections unknown", "list_sections", {"host": "no_such_host"}, expect="not found")

    # ── get_overview ────────────────────────────────────────────────────────
    print("\n── get_overview ───────────────────────────────────────────────")
    t("get_overview windows", "get_overview", {"host": WINDOWS_HOST}, expect="windows")
    t("get_overview linux",   "get_overview", {"host": LINUX_HOST},   expect="linux")
    t("get_overview unknown", "get_overview", {"host": "no_such_host"}, expect="not found")

    # ── get_processes ───────────────────────────────────────────────────────
    print("\n── get_processes ──────────────────────────────────────────────")
    t("get_processes windows default",     "get_processes", {"host": WINDOWS_HOST})
    t("get_processes windows sort cpu",    "get_processes", {"host": WINDOWS_HOST, "sort_by": "cpu", "limit": 5})
    t("get_processes windows filter_user *","get_processes", {"host": WINDOWS_HOST, "filter_user": "*"})
    t("get_processes windows aggregate",   "get_processes", {"host": WINDOWS_HOST, "aggregate": True, "limit": 5})
    t("get_processes windows filter_cmd",  "get_processes", {"host": WINDOWS_HOST, "filter_cmd": "svchost"})
    t("get_processes linux default",       "get_processes", {"host": LINUX_HOST})
    t("get_processes linux sort vsz",      "get_processes", {"host": LINUX_HOST, "sort_by": "vsz", "limit": 5})
    t("get_processes linux filter_user *", "get_processes", {"host": LINUX_HOST, "filter_user": "*"})
    t("get_processes linux aggregate warn","get_processes", {"host": LINUX_HOST, "aggregate": True},
      expect="aggregate ignored")

    # ── get_section ─────────────────────────────────────────────────────────
    print("\n── get_section ────────────────────────────────────────────────")
    t("get_section linux mem",     "get_section", {"host": LINUX_HOST,   "section": "mem"},  expect="MemTotal")
    t("get_section linux cpu",     "get_section", {"host": LINUX_HOST,   "section": "cpu"})
    t("get_section linux uptime",  "get_section", {"host": LINUX_HOST,   "section": "uptime"})
    t("get_section windows wincpu","get_section", {"host": WINDOWS_HOST, "section": "wincpu"})
    t("get_section windows winmem","get_section", {"host": WINDOWS_HOST, "section": "winmem"}, expect="MemTotal")
    t("get_section unknown alias", "get_section", {"host": LINUX_HOST,   "section": "zzz_no_section"}, expect="not found")

    # ── historical tools ────────────────────────────────────────────────────
    if not history_at:
        print(f"\n── historical tools ────────────────────────────────────────── [{SKIP}] --history-at not set")
    else:
        frm = history_at
        try:
            dt  = datetime.strptime(history_at.rstrip("Z"), "%Y-%m-%dT%H:%M")
            to  = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            to = history_at

        print(f"\n── get_history_overview  (at={frm}) ───────────────────────")
        t("history_overview windows", "get_history_overview", {"host": WINDOWS_HOST, "at": frm})
        t("history_overview linux",   "get_history_overview", {"host": LINUX_HOST,   "at": frm})

        print(f"\n── get_history_processes  (at={frm}) ──────────────────────")
        t("history_processes windows",          "get_history_processes", {"host": WINDOWS_HOST, "at": frm, "limit": 5})
        t("history_processes windows aggregate","get_history_processes", {"host": WINDOWS_HOST, "at": frm, "aggregate": True, "limit": 5})
        t("history_processes linux",            "get_history_processes", {"host": LINUX_HOST,   "at": frm, "limit": 5})

        print(f"\n── get_history_section  (at={frm}) ────────────────────────")
        t("history_section linux mem",    "get_history_section", {"host": LINUX_HOST,   "at": frm, "section": "mem"})
        t("history_section windows wincpu","get_history_section", {"host": WINDOWS_HOST, "at": frm, "section": "wincpu"})

        print(f"\n── get_history_range  ({frm} → {to}) ──────────────────────")
        t("history_range linux mem",    "get_history_range", {"host": LINUX_HOST,   "metric": "mem", "from": frm, "to": to, "samples": 5})
        t("history_range linux cpu",    "get_history_range", {"host": LINUX_HOST,   "metric": "cpu", "from": frm, "to": to, "samples": 5})
        t("history_range windows mem",  "get_history_range", {"host": WINDOWS_HOST, "metric": "mem", "from": frm, "to": to, "samples": 5})
        t("history_range wrong metric", "get_history_range", {"host": LINUX_HOST,   "metric": "disk","from": frm, "to": to}, expect="must be")
        t("history_range missing from", "get_history_range", {"host": LINUX_HOST,   "metric": "mem", "to": to},             expect="Error:")

    # ── Summary ─────────────────────────────────────────────────────────────
    passed = sum(results)
    total  = len(results)
    failed = total - passed
    color  = "\033[32m" if failed == 0 else "\033[31m"
    print(f"\n{'─'*60}")
    print(f"  {color}{passed}/{total} passed{'\033[0m'}"
          + (f"  \033[31m{failed} failed\033[0m" if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",        default="http://localhost:8765/mcp")
    parser.add_argument("--api-key",    default="")
    parser.add_argument("--history-at", default="",
                        help="ISO-8601 timestamp for historical tests, e.g. 2026-06-14T10:00")
    args = parser.parse_args()
    sys.exit(run(args.url, args.api_key, args.history_at))
