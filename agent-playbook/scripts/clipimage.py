#!/usr/bin/env python3
"""Put an image on the system clipboard silently (paste it with Ctrl+V into a site or a program).

This is the SYSTEM clipboard, which the owner and every agent share. For a paste into a page use
chrome-control's browser_clipboard with the file's path (or browser_upload); use this only for a
program outside the browser.
Windows: Pillow + the Win32 API when Pillow is installed; otherwise a hidden PowerShell
(no console window). macOS: osascript. Linux: wl-copy or xclip with the image bytes.
Prints "ok image <path>"; exit 1 when the file is missing or the clipboard did not take it.
"""
import io
import os
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []  # Pillow is used on Windows when present, not required
EFFECTS = ["clipboard"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG IMAGE")
    ap.add_argument("image", metavar="IMAGE", help="a PNG or JPEG file")
    return ap


TOOL_TIMEOUT = 30  # seconds; a clipboard tool that takes longer is stuck


def windows(path):
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "[System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile('%s'))"
              % os.path.abspath(path).replace("'", "''"))
        try:
            r = common.run_silent(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps], timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0
    try:
        with Image.open(path) as im:
            img = flat_rgb(im)
    except Exception as e:  # Pillow raises several types for a file that isn't an image
        common.die("not an image Pillow can read: %s (%s)" % (path, e))
    buf = io.BytesIO()
    img.save(buf, "BMP")
    return common.set_clipboard_data(common.CF_DIB, buf.getvalue()[14:])  # strip BITMAPFILEHEADER -> CF_DIB


def flat_rgb(im):
    """The picture as RGB for a CF_DIB (which has no transparency): transparent parts on white, as the
    image shows in a viewer, not whatever colour the transparent pixels happen to hold (usually black)."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("RGB")
    return im.convert("RGB")


def applescript_text(text):
    """A string as an AppleScript string literal."""
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def run_tool(cmd, data=None):
    """True when a clipboard tool took the image. Its output is not captured: wl-copy and xclip fork a server
    that keeps the inherited stdout and stderr open until another program takes the clipboard."""
    try:
        return subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=TOOL_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def main(argv=None):
    a = build_parser().parse_args(argv)
    path = a.image
    if not os.path.isfile(path):
        common.die("no such file: %s" % path)
    system = platform.system()
    try:
        if system == "Windows":
            ok = windows(path)
        elif system == "Darwin":
            cls = "PNGf" if path.lower().endswith(".png") else "JPEG"
            script = "set the clipboard to (read (POSIX file %s) as «class %s»)" % (applescript_text(os.path.abspath(path)), cls)
            ok = run_tool(["osascript", "-e", script])
        else:
            with open(path, "rb") as fh:
                data = fh.read()
            mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
            cmd = ["wl-copy", "--type", mime] if shutil.which("wl-copy") else ["xclip", "-selection", "clipboard", "-t", mime]
            ok = run_tool(cmd, data)
    except OSError as e:
        common.die("clipboard not available: %s (Linux: install wl-clipboard or xclip)" % e)
    if not ok:
        common.die("FAILED image %s: the clipboard did not take it" % path)
    print("ok image " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
