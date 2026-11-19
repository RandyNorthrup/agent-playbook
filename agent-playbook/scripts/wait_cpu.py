#!/usr/bin/env python3
"""Block until CPU load stays under a threshold for 3 checks in a row (silent, no child processes).

  wait_cpu.py [threshold_percent=75] [max_minutes=30] [interval_seconds=10]
Run it in the background; resume heavy work (renders, bulk generation) when it prints "cpu-ok".
Browser calls no longer need it: chrome-control 1.7 and newer keep a slow command alive on a loaded machine.
Windows reads GetSystemTimes, Linux /proc/stat, macOS and other Unix the 1-minute load average.
Exit 1 on timeout, or on a system that offers none of those.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [THRESHOLD_PERCENT] [MAX_MINUTES] [INTERVAL_SECONDS]")
    ap.add_argument("threshold", nargs="?", type=float, default=75.0, metavar="THRESHOLD_PERCENT")
    ap.add_argument("max_minutes", nargs="?", type=float, default=30.0, metavar="MAX_MINUTES")
    ap.add_argument("interval", nargs="?", type=float, default=10.0, metavar="INTERVAL_SECONDS")
    return ap


def _windows_sampler():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32")

    def times():
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))

        def ticks(t):
            return (t.dwHighDateTime << 32) | t.dwLowDateTime
        return ticks(idle), ticks(kern), ticks(user)  # kernel time includes idle time

    def sample(interval):
        i1, k1, u1 = times()
        time.sleep(interval)
        i2, k2, u2 = times()
        total = (k2 - k1) + (u2 - u1)
        return 100.0 * (1 - (i2 - i1) / total) if total else 0.0
    return sample


def _linux_sampler():
    def read():
        with open("/proc/stat") as fh:
            vals = [int(x) for x in fh.readline().split()[1:]]
        return vals[3] + vals[4], sum(vals)  # idle + iowait, total

    def sample(interval):
        i1, t1 = read()
        time.sleep(interval)
        i2, t2 = read()
        return 100.0 * (1 - (i2 - i1) / (t2 - t1)) if t2 > t1 else 0.0
    return sample


def _loadavg_sampler():
    def sample(interval):
        time.sleep(interval)
        # A load average counts waiting processes too, so it can pass 100% of the cores: cap it just under.
        return min(99.9, 100.0 * os.getloadavg()[0] / (os.cpu_count() or 1))
    return sample


def pick_sampler():
    """A function(interval) -> load percent for this system, or None when it offers no way to read load."""
    if common.WINDOWS:
        return _windows_sampler()
    if os.path.exists("/proc/stat"):
        return _linux_sampler()
    if hasattr(os, "getloadavg"):
        return _loadavg_sampler()
    return None


def wait(sample, threshold, max_minutes, interval):
    """(ok, last_load): ok once the load was under the threshold 3 times in a row before the deadline."""
    deadline = time.time() + max_minutes * 60
    streak, load = 0, 100.0
    while streak < 3:
        load = sample(interval)
        streak = streak + 1 if load < threshold else 0
        if streak < 3 and time.time() > deadline:
            return False, load
    return True, load


def main(argv=None):
    a = build_parser().parse_args(argv)
    if not 0 < a.threshold <= 100 or a.max_minutes <= 0 or a.interval <= 0:
        common.die("threshold is 1-100, max_minutes and interval_seconds above 0", common.EXIT_USAGE)
    sample = pick_sampler()
    if sample is None:
        common.die("no CPU load reading on this system (needs Windows, /proc/stat or os.getloadavg)")
    ok, load = wait(sample, a.threshold, a.max_minutes, a.interval)
    if not ok:
        common.die(f"timeout load={load:.0f}")
    print(f"cpu-ok {time.strftime('%H:%M:%S')} load={load:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
