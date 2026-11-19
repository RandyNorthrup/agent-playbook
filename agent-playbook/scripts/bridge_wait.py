#!/usr/bin/env python3
"""Wait for a fresh chrome-control native host after a bridge drop (Windows; silent).

The Chrome extension respawns `chrome_control_mcp.exe` with a command line that contains
"chrome-extension://". When a new one appears, call browser_tabs again.

  bridge_wait.py [max_minutes=5]   prints "host-up [pids]" when a new host has attached
  bridge_wait.py --list            show native hosts (Chrome-spawned vs session servers)

Only for chrome-control; other browser tools have their own reconnect behavior.
chrome-control 1.8 and newer resume a dropped relay's tabs by themselves and 1.9 and newer say in the
reply what is wrong (browser_extension_status shows the versions), so retry the call first; this is for
looking at the host processes when that fails.
Exit 1 on timeout, when the process list can't be read, or on a system other than Windows.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]
PS = ("Get-CimInstance Win32_Process -Filter \"Name='chrome_control_mcp.exe'\" | "
      "Select-Object ProcessId,@{n='Created';e={$_.CreationDate.ToUniversalTime().ToString('o')}},"
      "@{n='Chrome';e={$_.CommandLine -like '*chrome-extension://*'}} | ConvertTo-Json -Compress")


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [MAX_MINUTES] | --list")
    ap.add_argument("max_minutes", nargs="?", type=float, default=5.0, help="how long to wait (default 5)")
    ap.add_argument("--list", action="store_true", help="list the native hosts and exit")
    return ap


def parse_hosts(out):
    """The process list PowerShell printed: [] for nothing, always a list."""
    out = out.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def hosts():
    try:
        r = common.run_silent(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS], text=True, timeout=60)
        return parse_hosts(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        common.die("can't read the process list: %s" % e)


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.max_minutes <= 0:
        common.die("max_minutes must be above 0", common.EXIT_USAGE)
    common.require_windows("it watches for chrome_control_mcp.exe, the chrome-control native host")
    if a.list:
        for h in hosts():
            print(h["ProcessId"], h["Created"], "chrome-spawned" if h["Chrome"] else "session-server")
        return 0
    start = {h["ProcessId"] for h in hosts() if h["Chrome"]}
    deadline = time.time() + a.max_minutes * 60
    while time.time() < deadline:
        now = {h["ProcessId"] for h in hosts() if h["Chrome"]}
        new = now - start
        if new:
            time.sleep(15)  # let the extension attach before calling browser_tabs
            print("host-up", sorted(new))
            return 0
        if not now:
            start = set()  # all hosts gone; the next one to appear is fresh
        time.sleep(3)
    common.die("timeout: no new host; ask the owner to reload the extension or close a stuck tab")


if __name__ == "__main__":
    sys.exit(main())
