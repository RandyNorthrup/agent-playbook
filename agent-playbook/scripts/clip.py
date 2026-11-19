#!/usr/bin/env python3
"""Copy text to the system clipboard silently and verify it arrived (no console windows).

  clip.py FILE        copy the whole file (multi-line post)
  clip.py FILE N      copy line N (1-based)
  clip.py --text "s"  copy a literal string

This is the SYSTEM clipboard, which the owner and every agent share. For a paste into a page use
chrome-control's browser_clipboard (each agent has its own clipboard there); use this only for a
program outside the browser.
Windows: Win32 clipboard API via ctypes (no PowerShell, nothing pops up).
macOS: pbcopy/pbpaste. Linux: wl-copy/wl-paste or xclip.
Prints "ok=True :: <text>" when the clipboard verifiably holds the text; exit 1 when it doesn't,
or when the file can't be read.
"""
import os
import platform
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["clipboard"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, 'PROG FILE [LINE] | --text "STRING"')
    ap.add_argument("file", nargs="?", metavar="FILE")
    ap.add_argument("line", nargs="?", metavar="LINE", help="copy only this line of FILE (1-based)")
    ap.add_argument("--text", metavar="STRING", help="copy this string instead of a file")
    return ap


TOOL_TIMEOUT = 10  # seconds; a clipboard tool that takes longer is stuck (no display, a dead X server)


def _tool(cmd, data=None, read=False):
    """Run a clipboard tool. A copy tool's output is never captured: wl-copy and xclip fork a server that keeps
    serving the clipboard and inherits stdout and stderr, so waiting for those pipes to close would wait until
    another program takes the clipboard."""
    out = subprocess.PIPE if read else subprocess.DEVNULL
    try:
        return subprocess.run(cmd, input=data, stdout=out, stderr=subprocess.DEVNULL, timeout=TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise OSError("%s did not finish in %d s (is there a display?)" % (cmd[0], TOOL_TIMEOUT))
    except OSError:
        raise OSError("%s is not installed (Linux: install wl-clipboard or xclip)" % cmd[0])


def set_clip(text):
    """Put text on the system clipboard."""
    if common.WINDOWS:
        common.set_clipboard_data(common.CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")
    elif platform.system() == "Darwin":
        _tool(["pbcopy"], text.encode("utf-8"))
    elif shutil.which("wl-copy"):
        _tool(["wl-copy"], text.encode("utf-8"))
    else:
        _tool(["xclip", "-selection", "clipboard"], text.encode("utf-8"))


def get_clip():
    """The text on the system clipboard ("" when it holds none)."""
    if common.WINDOWS:
        import ctypes
        u32, k32 = common.win32_clipboard()
        if not common.open_clipboard(u32):
            return ""
        try:
            h = u32.GetClipboardData(common.CF_UNICODETEXT)
            if not h:
                return ""
            p = k32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    if platform.system() == "Darwin":
        r = _tool(["pbpaste"], read=True)
    elif shutil.which("wl-paste"):
        r = _tool(["wl-paste", "--no-newline"], read=True)
    else:
        r = _tool(["xclip", "-selection", "clipboard", "-o"], read=True)
    return (r.stdout or b"").decode("utf-8", "replace")


def copy_verified(text):
    """Copy, read back, retry a few times. True when the clipboard holds the text."""
    for _ in range(10):
        set_clip(text)
        time.sleep(0.1)
        if get_clip().replace("\r", "").rstrip() == text.replace("\r", "").rstrip():
            return True
        time.sleep(0.2)
    return False


def pick_text(a, ap):
    """The text the arguments ask for (usage errors exit 2, an unreadable file exits 1)."""
    if a.text is not None:
        if a.file is not None:
            ap.error("give FILE or --text, not both")
        return a.text
    if a.file is None:
        ap.error("give FILE or --text")
    text = common.read_text(a.file)
    if a.line is None:
        return text
    lines = text.splitlines()
    if not a.line.isdigit() or not 1 <= int(a.line) <= len(lines):
        ap.error("LINE must be a number from 1 to %d (%s has %d lines), got %r" % (len(lines), a.file, len(lines), a.line))
    return lines[int(a.line) - 1]


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    text = pick_text(a, ap).rstrip()
    try:
        ok = copy_verified(text)
    except OSError as e:
        common.die("clipboard not available: %s" % e)
    # ASCII-safe echo so Windows consoles never choke on emoji
    print(f"ok={ok} :: {text[:120]}".encode("ascii", "replace").decode())
    if not ok:
        common.die("the clipboard does not hold the text (another program may be holding it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
