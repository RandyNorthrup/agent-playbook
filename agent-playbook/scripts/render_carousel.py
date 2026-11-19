#!/usr/bin/env python3
"""Render an HTML page to PNG slides with headless Chrome (silent, cross-platform).

Each slide is the page at index.html#<n> (see templates/carousel.html). A one-image page (a banner) is
--count 1 with its own --width and --height.

  render_carousel.py --html work/ig/tips/index.html --name tips --count 8 --out ~/Pictures/Instagram
  ... --only 2 3        re-render selected slides
  ... --chrome PATH     Chrome/Chromium binary if not auto-detected (or set AGENT_PLAYBOOK_CHROME)
Uses a throwaway private profile and render folder (both deleted afterwards), so it never touches the owner's
Chrome, leaves nothing beside the HTML, and two renders can run at once. Each PNG lands in --out as NAME-N.png.
Prints "ok NAME-N" or "FAILED NAME-N" per slide. Exit 1 when Chrome or the HTML is missing or any slide
failed to render.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

PNG_HEAD = b"\x89PNG\r\n\x1a\n"
PNG_TAIL = b"IEND\xaeB`\x82"  # the last chunk: a file without it was cut off

NEEDS = ["Google Chrome or Chromium"]
EFFECTS = ["files"]


def build_parser():
    ap = common.make_parser(
        __doc__, __file__,
        "PROG --html FILE --name NAME --count N --out DIR [--only N [N ...]] [--width 1080] [--height 1350] "
        "[--timeout 45] [--tries 3] [--chrome PATH]")
    ap.add_argument("--html", required=True, metavar="FILE")
    ap.add_argument("--name", required=True, help="slides are saved as NAME-1.png, NAME-2.png, ...")
    ap.add_argument("--count", type=int, required=True, metavar="N", help="number of slides")
    ap.add_argument("--out", required=True, metavar="DIR", help="folder the PNGs are copied to")
    ap.add_argument("--only", type=int, nargs="+", metavar="N", help="render only these slides")
    ap.add_argument("--chrome", metavar="PATH")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1350)
    ap.add_argument("--timeout", type=int, default=45, help="seconds before a hung render is killed (default 45)")
    ap.add_argument("--tries", type=int, default=3, help="attempts per slide (default 3)")
    return ap


def complete_png(path):
    """True when the file is a whole PNG: the signature at the start and the IEND chunk at the end. A Chrome
    that was killed mid-write leaves an empty or cut-off file."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(len(PNG_HEAD))
            fh.seek(max(0, size - 12))
            tail = fh.read()
    except OSError:
        return False
    return head == PNG_HEAD and PNG_TAIL in tail


def shoot(chrome, url, png, profile, width, height, timeout):
    """One headless screenshot. True when a whole PNG appeared."""
    if os.path.exists(png):
        os.remove(png)
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
           f"--user-data-dir={profile}", f"--window-size={width},{height}", "--virtual-time-budget=6000",
           f"--screenshot={png}", url]
    try:
        p = common.popen_tree(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        common.die("can't start Chrome (%s): %s" % (chrome, e.strerror or e))
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # a hung render (heavy WebGL etc.), or a Chrome that never exits after the shot
        common.kill_tree(p)
    for _ in range(8):
        if complete_png(png):
            return True
        time.sleep(0.25)
    return False


def remove_profile(profile):
    """Delete the throwaway profile; Chrome's last helper processes can hold a file in it for a moment."""
    for _ in range(10):
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile):
            return
        time.sleep(0.5)


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.count < 1 or a.tries < 1 or a.timeout < 1 or a.width < 1 or a.height < 1:
        common.die("--count, --tries, --timeout, --width and --height must be 1 or more", common.EXIT_USAGE)
    slides = a.only or list(range(1, a.count + 1))
    if any(i < 1 for i in slides):
        common.die("slide numbers start at 1", common.EXIT_USAGE)
    html = os.path.abspath(os.path.expanduser(a.html))
    if not os.path.isfile(html):
        common.die("no such HTML file: %s" % html)
    chrome = common.find_chrome(a.chrome)
    if not chrome:
        common.die("Chrome not found%s; pass --chrome PATH or set AGENT_PLAYBOOK_CHROME"
                   % (" at %s" % a.chrome if a.chrome else ""))
    out = os.path.abspath(os.path.expanduser(a.out))
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        common.die("can't create %s: %s" % (out, e.strerror or e))
    url = Path(html).as_uri()  # percent-encoded: a space, # or % in a folder name can't cut the URL short
    profile = tempfile.mkdtemp(prefix="agent-playbook-chrome-")
    shots = tempfile.mkdtemp(prefix="agent-playbook-shots-")  # this run's own: two runs never share a file
    failed = []
    try:
        for i in slides:
            png = os.path.join(shots, f"{a.name}-{i}.png")
            target = os.path.join(out, f"{a.name}-{i}.png")
            if any(shoot(chrome, f"{url}#{i}", png, profile, a.width, a.height, a.timeout) for _ in range(a.tries)):
                try:
                    shutil.copy2(png, target)
                except OSError as e:
                    common.die("can't copy %s to %s: %s" % (png, out, e.strerror or e))
                print(f"ok {a.name}-{i}")
            else:
                failed.append(i)
                print(f"FAILED {a.name}-{i}")
            sys.stdout.flush()
    finally:
        remove_profile(profile)
        shutil.rmtree(shots, ignore_errors=True)
    if failed:
        common.die("%d of %d slides failed to render: %s" % (len(failed), len(slides), ", ".join(map(str, failed))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
