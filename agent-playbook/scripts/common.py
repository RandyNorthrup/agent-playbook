#!/usr/bin/env python3
"""Shared helpers for the agent-playbook scripts. Standard library only; importing it does nothing.

Exit codes every script uses:
  0  worked
  1  failed (the message is on stderr): every request failed, a slide did not render, an input file is missing
  2  usage error (argparse's own code): a missing or malformed argument
  3  an optional module is missing ("needs X: pip install X")

Set AGENT_PLAYBOOK_OFFLINE=1 to make every request made through fetch() fail at once, so a script can be
exercised with no network (the tests do this).
"""
import argparse
import datetime
import http.client
import importlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

EXIT_FAIL, EXIT_USAGE, EXIT_NEEDS = 1, 2, 3
# The owner's working files (post bank, Facebook groups) live beside the playbook store.
STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playbook", "state")
WINDOWS = platform.system() == "Windows"
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW: a child process never flashes a console
UA = "Mozilla/5.0"
UTC = datetime.timezone.utc


# ---------------------------------------------------------------- command line

def prog():
    return os.path.basename(sys.argv[0]) or "agent-playbook"


def make_parser(doc, script, usage):
    """An argparse parser for a script: the docstring's first line is the description (INDEX.json reads it),
    the rest is the epilog. `usage` is written out by hand, so it reads the same on every Python version."""
    lines = doc.strip().splitlines()
    name = os.path.basename(script)
    return argparse.ArgumentParser(
        prog=name, usage=usage.replace("%", "%%").replace("PROG", name), description=lines[0].replace("%", "%%"),
        epilog="\n".join(lines[1:]).strip().replace("%", "%%") or None,
        formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)


def warn(msg):
    print("%s: %s" % (prog(), msg), file=sys.stderr)


def die(msg, code=EXIT_FAIL):
    """Print the message on stderr and exit non-zero."""
    warn(msg)
    sys.exit(code)


def safe_stdio():
    """Never raise on a character the console can't show (emoji on a cp1252 console): print '?' instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def require(module, pip=None):
    """Import an optional module, or exit 3 with the install line."""
    try:
        return importlib.import_module(module)
    except ImportError:
        die("needs %s: pip install %s" % (module, pip or module), EXIT_NEEDS)


def require_windows(what):
    if not WINDOWS:
        die("Windows only: %s (this is %s)" % (what, platform.system() or "an unknown system"))


def state_file(name, flag=None, env=None):
    """The path of an owner state file: the command-line value, else the environment variable, else
    <skill>/playbook/state/<name>."""
    return flag or (os.environ.get(env) if env else None) or os.path.join(STATE_DIR, name)


def read_text(path, encoding="utf-8"):
    """A file's text, or exit 1 with a plain message (no traceback) when it can't be read."""
    try:
        with open(path, encoding=encoding) as fh:
            return fh.read()
    except OSError as e:
        die("can't read %s: %s" % (path, e.strerror or e))
    except UnicodeDecodeError:
        die("%s is not %s text" % (path, encoding))


def write_atomic(path, text):
    """Write a text file so a crash can't leave it half-written: temp file beside it, then one rename."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- TSV

def tsv_lines(text):
    """(kind, value) per line of tab-separated text: ("row", [columns]) or ("other", the raw line) for blank
    and # comment lines, so a file can be rewritten with its comments kept."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(("other", line))
        else:
            out.append(("row", line.split("\t")))
    return out


def read_tsv(path, width=0):
    """The data rows of a tab-separated file, each padded with "" to at least `width` columns."""
    return [cols + [""] * (width - len(cols)) for kind, cols in tsv_lines(read_text(path)) if kind == "row"]


# ---------------------------------------------------------------- time

_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d+))?)?)?\s*(Z|z|[+-]\d\d(?::?\d\d)?)?$")


def parse_time(text):
    """An ISO 8601 timestamp as an aware datetime. Accepts a trailing Z and any number of fraction digits
    (datetime.fromisoformat on Python 3.8-3.10 takes neither); a timestamp with no zone is read as UTC."""
    m = _ISO.match((text or "").strip())
    if not m:
        raise ValueError("not an ISO timestamp: %r" % (text,))
    y, mo, d, h, mi, s, frac, zone = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    tz = UTC
    if zone and zone not in "Zz":
        digits = zone[1:].replace(":", "")
        delta = datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:] or 0))
        tz = datetime.timezone(-delta if zone[0] == "-" else delta)
    return datetime.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), micro, tz)


def now_utc():
    return datetime.datetime.now(UTC)


def hours_since(text, now=None):
    return ((now or now_utc()) - parse_time(text)).total_seconds() / 3600


# ---------------------------------------------------------------- requests

class FetchError(Exception):
    """A request failed; str() is a one-line reason that names the URL."""


def offline():
    return os.environ.get("AGENT_PLAYBOOK_OFFLINE", "") not in ("", "0")


# A request that hits a rate limit, a server error, a timeout or a dropped connection is tried again after
# RETRY_WAIT seconds (or the server's Retry-After, up to MAX_RETRY_AFTER). After TROUBLE_LIMIT requests in a
# row that failed even so, the service counts as down and later requests are tried once, so a script that
# makes 70 requests fails in seconds instead of minutes.
RETRY_STATUS = (429, 500, 502, 503, 504)
RETRY_WAIT = (1.0, 3.0)
MAX_RETRY_AFTER = 15.0
TROUBLE_LIMIT = 3
_trouble = [0]


def _transient(e):
    """True for a failure worth another try: a rate limit or server error, a timeout, a dropped connection.
    Never for a DNS failure, a refused connection or a client error: those don't fix themselves."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in RETRY_STATUS
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    return isinstance(e, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError,
                          http.client.RemoteDisconnected, http.client.IncompleteRead))


def _retry_after(e):
    """The seconds an HTTP error's Retry-After header asks for, or None (absent, or an HTTP date)."""
    try:
        return max(0.0, float(e.headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _reason(e, url):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %s %s: %s" % (e.code, e.reason, url)
    if isinstance(e, urllib.error.URLError):
        return "%s: %s" % (e.reason, url)
    return "%s: %s" % (str(e) or type(e).__name__, url)


def fetch(url, timeout=20, headers=None):
    """GET a URL and return the body bytes. Any failure (offline switch, DNS, timeout, HTTP status) raises
    FetchError with a one-line reason. A rate limit, a server error, a timeout or a dropped connection is tried
    up to twice more, with a short wait (see RETRY_WAIT)."""
    if offline():
        raise FetchError("offline (AGENT_PLAYBOOK_OFFLINE is set): " + url)
    h = {"User-Agent": UA}
    h.update(headers or {})
    tries = 1 if _trouble[0] >= TROUBLE_LIMIT else len(RETRY_WAIT) + 1
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                body = r.read()
            _trouble[0] = 0
            return body
        except (OSError, http.client.HTTPException, ValueError) as e:  # URLError and HTTPError are OSErrors
            wait = RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)]
            asked = None
            if isinstance(e, urllib.error.HTTPError):
                asked = _retry_after(e)
                try:
                    e.close()  # an HTTP error holds the response open: let the connection go
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
            if asked is not None:
                wait = max(wait, asked)
            if attempt + 1 < tries and _transient(e) and wait <= MAX_RETRY_AFTER:
                time.sleep(wait)
                continue
            if _transient(e):
                _trouble[0] += 1
            reason = _reason(e, url)
            if asked is not None and asked > MAX_RETRY_AFTER:
                reason += " (the server asks to wait %d s before the next request)" % asked
            raise FetchError(reason)


def fetch_text(url, timeout=20, headers=None):
    return fetch(url, timeout, headers).decode("utf-8", "replace")


def fetch_json(url, timeout=20, headers=None):
    body = fetch(url, timeout, headers)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise FetchError("the reply is not JSON: " + url)


BSKY_API = "https://public.api.bsky.app/xrpc/"


def bsky(method, timeout=20, **params):
    """Call a public Bluesky XRPC method (no login) and return its JSON."""
    return fetch_json(BSKY_API + method + "?" + urllib.parse.urlencode(params), timeout)


def as_dict(value):
    """`value` when it is a JSON object, else {}: replies change shape, and a field can be null or missing."""
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def as_text(value):
    return value if isinstance(value, str) else ""


def as_count(value):
    """A JSON number as an int; 0 for a count that is missing, null or not a number (NaN and Infinity too)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value)
    except (ValueError, OverflowError):
        return 0


def norm_handle(handle):
    """Handles compare without case and without a leading @."""
    return (handle or "").strip().lstrip("@").lower()


def same_handle(a, b):
    return norm_handle(a) == norm_handle(b)


class Tally:
    """Counts requests that worked and failed, so a script can exit 1 when every one failed."""

    def __init__(self):
        self.ok = 0
        self.failed = 0

    def good(self):
        self.ok += 1

    def bad(self, what, error):
        self.failed += 1
        print("ERR %s: %s" % (what, error), file=sys.stderr)

    def exit_code(self, noun="request"):
        if self.failed and not self.ok:
            warn("all %d %s%s failed" % (self.failed, noun, "" if self.failed == 1 else "s"))
            return EXIT_FAIL
        return 0


# ---------------------------------------------------------------- X length

X_LIMIT = 280
X_URL_LENGTH = 23
URL_RE = re.compile(r"https?://\S+")
# twitter-text configuration v3: code points in these ranges weigh 1, every other code point weighs 2,
# a link weighs 23 whatever its length, and one emoji (a whole sequence) weighs 2.
_X_LIGHT = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
_ZWJ, _VS16, _KEYCAP = 0x200D, 0xFE0F, 0x20E3


def _light(cp):
    return any(lo <= cp <= hi for lo, hi in _X_LIGHT)


def _emoji_tail(cp):
    """Code points that extend the emoji before them: variation selectors, skin tones, the keycap, tags."""
    return cp in (0xFE0E, _VS16, _KEYCAP) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def x_weighted(text):
    """The length X counts for a post (limit 280), by X's published twitter-text rules.

    Known gap: X also counts a bare domain ("example.com/page") as a 23-character link; here only
    http:// and https:// links are, so a bare domain is counted at its full length (never under-counted
    unless it is shorter than 23 characters).
    """
    text = URL_RE.sub("x" * X_URL_LENGTH, unicodedata.normalize("NFC", text))
    cps = [ord(c) for c in text]
    n, i, total = len(cps), 0, 0
    while i < n:
        cp = cps[i]
        if _light(cp):
            keycap = chr(cp) in "0123456789#*" and (
                cps[i + 1:i + 3] == [_VS16, _KEYCAP] or cps[i + 1:i + 2] == [_KEYCAP])
            if keycap or cps[i + 1:i + 2] == [_VS16]:  # a plain character shown as an emoji: one emoji
                total += 2
                i += 1
                while i < n and _emoji_tail(cps[i]):
                    i += 1
            else:
                total += 1
                i += 1
            continue
        total += 2
        i += 1
        if 0x1F1E6 <= cp <= 0x1F1FF and i < n and 0x1F1E6 <= cps[i] <= 0x1F1FF:  # a flag is two letters
            i += 1
        while i < n:
            if _emoji_tail(cps[i]):
                i += 1
            elif cps[i] == _ZWJ and i + 1 < n and not _light(cps[i + 1]):  # joined emoji (family, flags)
                i += 2
            else:
                break
    return total


# ---------------------------------------------------------------- Chrome

CHROME_ENV = ("AGENT_PLAYBOOK_CHROME", "CHROME_PATH", "CHROME_BIN")
CHROME_NAMES = ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def chrome_candidates():
    paths = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            paths.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    paths += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
              "/usr/bin/chromium-browser", "/snap/bin/chromium"]
    return paths


def _chrome_from_registry():
    if not WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key) as k:
                path = winreg.QueryValue(k, None)
        except OSError:
            continue
        if path and os.path.exists(path):
            return path
    return None


def find_chrome(explicit=None):
    """Path of a Chrome or Chromium binary, or None. Order: the path given, $AGENT_PLAYBOOK_CHROME (then
    $CHROME_PATH, $CHROME_BIN), the usual install folders, the Windows App Paths registry key, PATH.
    A path that was given but doesn't exist returns None rather than falling back to another browser."""
    if explicit:
        explicit = os.path.expanduser(explicit)
        return explicit if os.path.exists(explicit) else shutil.which(explicit)
    for var in CHROME_ENV:
        value = os.environ.get(var)
        if value:
            return value if os.path.exists(value) else shutil.which(value)
    for path in chrome_candidates():
        if os.path.exists(path):
            return path
    found = _chrome_from_registry()
    if found:
        return found
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- silent child processes

def run_silent(cmd, **kw):
    """subprocess.run with captured output and, on Windows, no console window."""
    kw.setdefault("capture_output", True)
    if WINDOWS:
        kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kw)


def popen_tree(cmd, **kw):
    """subprocess.Popen for a child that may start children of its own (codex through node, Chrome, grok), to
    be ended with kill_tree(); on Windows with no console window. The child stays in this process group, so
    Ctrl+C in a terminal still reaches it."""
    if WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW
    proc = subprocess.Popen(cmd, **kw)
    proc._agent_job = _job_for(proc) if WINDOWS else None
    proc._agent_seen = []
    return proc


def _kernel32():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def _job_for(proc):
    """Windows: put the child in a job object, so kill_tree() ends everything it started even after the child
    itself has exited (taskkill /T finds the tree only through a live child). None when that isn't possible."""
    try:
        k32 = _kernel32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            k32.CloseHandle(job)
            return None
        return job
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def _end_job(proc, terminate=True):
    """Close the child's job object; with `terminate`, first end every process still in it."""
    job, proc._agent_job = getattr(proc, "_agent_job", None), None
    if job:
        try:
            k32 = _kernel32()
            if terminate:
                k32.TerminateJobObject(job, 1)
            k32.CloseHandle(job)
        except (OSError, AttributeError, ValueError, TypeError):
            pass


def _parents():
    """{pid: parent pid} for every process on a Unix system: /proc on Linux, else ps. {} when neither works."""
    found = {}
    if os.path.isdir("/proc"):
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open("/proc/%s/stat" % name, "rb") as fh:
                        stat = fh.read()
                    found[int(name)] = int(stat[stat.rindex(b")") + 2:].split()[1])  # the name may hold spaces
                except (OSError, ValueError, IndexError):
                    continue
        if found:
            return found
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            found[int(parts[0])] = int(parts[1])
    return found


def descendants(pid):
    """The pids of every process `pid` started, and theirs in turn (Unix)."""
    children = {}
    for child, parent in _parents().items():
        children.setdefault(parent, []).append(child)
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.append(child)
                todo.append(child)
    return found


def kill_tree(proc, wait=10):
    """End a child started by popen_tree() and every process it started; never raises. subprocess's own kill
    ends only the child, so a hung codex under node, or Chrome's renderers, would live on."""
    if proc.poll() is None:  # the tree is found through the child, so only while it runs
        if WINDOWS:
            try:
                run_silent(["taskkill", "/T", "/F", "/PID", str(proc.pid)], timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            below = descendants(proc.pid)
            for pid in [proc.pid] + below:  # the child first, so it can't start another
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    elif not WINDOWS:
        # the child exited but what it started may live on (holding its pipes): those run_tree() saw while the
        # child ran. Pids aren't reused within seconds, so a pid still alive is the same process.
        for pid in getattr(proc, "_agent_seen", None) or []:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if WINDOWS:
        _end_job(proc)  # everything still in the child's job, the child's exit notwithstanding
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        pass


def run_tree(cmd, timeout=None, input=None, **kw):
    """subprocess.run for a child that may start children of its own: on a timeout (TimeoutExpired is raised)
    or Ctrl+C the whole tree is ended, not just the child."""
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    deadline = None if timeout is None else time.monotonic() + timeout
    with popen_tree(cmd, **kw) as proc:
        try:
            first = True
            while True:  # in slices, so the tree can be noted while the child runs (POSIX: see kill_tree)
                left = None if deadline is None else deadline - time.monotonic()
                try:
                    out, err = proc.communicate(input if first else None, timeout=1.0 if left is None else max(0.0, min(1.0, left)))
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if not WINDOWS and proc.poll() is None:
                        proc._agent_seen = descendants(proc.pid)
                    if deadline is not None and time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(cmd, timeout)
        except BaseException:
            kill_tree(proc)
            raise
    if WINDOWS:
        _end_job(proc, terminate=False)  # finished normally: only the handle goes
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------------------------------------------------------------- Win32 clipboard

CF_DIB, CF_UNICODETEXT = 8, 13
GMEM_MOVEABLE = 0x0002


def win32_clipboard():
    """(user32, kernel32) with the clipboard and global-memory prototypes declared.

    Handles are 64-bit on x64 Windows. Without argtypes ctypes passes them as a C int and raises
    "int too long to convert" as soon as a handle needs more than 32 bits.
    """
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    return u32, k32


def global_copy(k32, data):
    """Copy bytes into a moveable global memory block and return its handle."""
    import ctypes
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("GlobalAlloc failed")
    p = k32.GlobalLock(h)
    if not p:
        k32.GlobalFree(h)
        raise OSError("GlobalLock failed")
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    return h


def open_clipboard(u32, tries=20, delay=0.05):
    """Open the clipboard, retrying briefly: another app may hold it for a moment."""
    import time
    for _ in range(tries):
        if u32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def set_clipboard_data(fmt, data):
    """Replace the Windows clipboard with one block of `fmt` data. True when the clipboard took it."""
    u32, k32 = win32_clipboard()
    h = global_copy(k32, data)
    if not open_clipboard(u32):
        k32.GlobalFree(h)
        return False
    try:
        u32.EmptyClipboard()
        ok = bool(u32.SetClipboardData(fmt, h))  # on success the clipboard owns h
    finally:
        u32.CloseClipboard()
    if not ok:
        k32.GlobalFree(h)
    return ok
