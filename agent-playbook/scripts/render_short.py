#!/usr/bin/env python3
"""Run an explainer video pipeline's shorts.py with each window's audio edges moved to the real word onsets.

  render_short.py [--shorts-dir DIR] <video_dir> [shorts.py args...]

shorts.py pads a fixed time around the words.json timings. Those word starts can run 0.1-0.4 s late on
skit lines, and the fixed 0.6 s tail can catch the onset of the next word. This wrapper runs shorts.py
unchanged except for the window edges:
  start: walk back from the first word's aligned start to the last silent frame (under -55 dB) after the
         previous word, and start 40 ms before the real onset (never later than shorts.py's own start).
         With no silence in between (one continuous phrase), start at the quietest frame in that gap.
  end:   shorts.py's own end, pulled in to 40 ms before the next word's real onset if it would reach it.
Everything after <video_dir> goes to shorts.py as is (--from, --to, --hook, --style, --plan, ...).

The folder that holds shorts.py is --shorts-dir (give it before <video_dir>), else $AGENT_PLAYBOOK_SHORTS_DIR.
shorts.py is not part of this skill: it must define window(words, A, B, pre, post) returning (start, end,
the words in the window) and main(), and read <video_dir>/words.json and narration.mp3.
Needs numpy, ffmpeg on PATH, and whatever shorts.py itself needs (OpenCV, Pillow).
Exit 1 when shorts.py, narration.mp3 or ffmpeg is missing; exit 3 without numpy.
"""
import argparse
import os
import shutil
import site
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = ["numpy", "ffmpeg", "a shorts.py with window() and main(), and what it needs (OpenCV, Pillow)"]
EFFECTS = ["files"]
SR, HOP, SIL, GUARD = 16000, 0.01, -55.0, 0.04
np = None  # loaded in main(), so --help works without it


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [--shorts-dir DIR] VIDEO_DIR [SHORTS_ARGS ...]")
    ap.add_argument("--shorts-dir", metavar="DIR", help="the folder that holds shorts.py (default: $AGENT_PLAYBOOK_SHORTS_DIR)")
    ap.add_argument("video_dir", metavar="VIDEO_DIR")
    ap.add_argument("shorts_args", nargs=argparse.REMAINDER, metavar="SHORTS_ARGS", help="passed to shorts.py unchanged")
    return ap


def load_db(video_dir):
    """The narration's loudness in dB per 10 ms frame."""
    src = Path(video_dir) / "narration.mp3"
    if not src.is_file():
        common.die("no narration.mp3 in %s" % video_dir)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        common.die("ffmpeg not found on PATH")
    try:
        r = common.run_silent([ffmpeg, "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"])
    except OSError as e:
        common.die("can't start ffmpeg (%s): %s" % (ffmpeg, e.strerror or e))
    if r.returncode != 0 or not r.stdout:
        common.die("ffmpeg could not decode %s: %s" % (src, r.stderr.decode("utf-8", "replace").strip()[-300:]))
    x = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768
    n = int(SR * HOP)
    m = len(x) // n
    rms = np.sqrt(np.mean(x[:m * n].reshape(m, n) ** 2, axis=1))
    return 20 * np.log10(rms + 1e-9)


def frame(t):
    return int(round(t / HOP))


def refine(words, i0, i1, a, b, db):
    """New (start, end) for the window words[i0..i1] that shorts.py would cut at (a, b), from the
    narration's per-frame loudness `db`."""
    s, e = words[i0]["start"], words[i1]["end"]
    # ---- start
    pe = words[i0 - 1]["end"] if i0 > 0 else 0.0
    k = min(frame(s), len(db))  # words.json can run past the end of the decoded audio
    lo = min(frame(max(pe, s - 1.0)), k)
    while k > lo and db[k - 1] > SIL:
        k -= 1
    if k > lo:                                   # found silence: frame k is the real onset
        q = k
        while q > lo and db[q - 1] <= SIL:       # q: where that silence began
            q -= 1
        new_a = max(min(a, k * HOP - GUARD), q * HOP)
    else:                                        # continuous speech: cut in the quietest frame of the gap
        seg = db[frame(pe):frame(s) + 1]
        new_a = (frame(pe) + int(np.argmin(seg))) * HOP if len(seg) else a
    new_a = max(new_a, 0.0)
    # ---- end
    new_b = b
    if i1 + 1 < len(words):
        ns = words[i1 + 1]["start"]
        j, hi = frame(e), min(frame(max(b, ns) + 0.05), len(db))
        while j < hi and db[j] > SIL:            # the last word's own decay
            j += 1
        quiet = j
        while j < hi and db[j] <= SIL:           # silence until the next word starts
            j += 1
        if j < hi and j * HOP - GUARD < b:
            new_b = max(j * HOP - GUARD, quiet * HOP + 0.02, e)
        elif quiet >= hi:                        # no silence before the next word: cut at the quietest frame
            seg = db[frame(e):frame(ns) + 1]
            if len(seg):
                new_b = min(b, (frame(e) + int(np.argmin(seg))) * HOP)
    return new_a, new_b


def main(argv=None):
    global np
    a = build_parser().parse_args(argv)
    shorts_dir = a.shorts_dir or os.environ.get("AGENT_PLAYBOOK_SHORTS_DIR")
    if not shorts_dir:
        common.die("where is shorts.py? pass --shorts-dir DIR (before the video folder) or set AGENT_PLAYBOOK_SHORTS_DIR",
                   common.EXIT_USAGE)
    shorts_dir = Path(shorts_dir).resolve()
    if not (shorts_dir / "shorts.py").is_file():
        common.die("no shorts.py in %s" % shorts_dir)
    if not Path(a.video_dir).is_dir():
        common.die("no such video folder: %s" % a.video_dir)
    np = common.require("numpy")
    db = load_db(a.video_dir)
    sys.path.insert(0, str(shorts_dir))
    try:
        sys.path.append(site.getusersitepackages())  # python -I drops the user site; OpenCV often lives there
    except AttributeError:
        pass
    try:
        import shorts
    except ImportError as e:
        common.die("can't import shorts.py from %s: %s" % (shorts_dir, e), common.EXIT_NEEDS)
    original = shorts.window

    def window(words, A, B, pre, post):
        wa, wb, ws = original(words, A, B, pre, post)
        i0 = next(i for i, w in enumerate(words) if w is ws[0])
        i1 = next(i for i, w in enumerate(words) if w is ws[-1])
        new_a, new_b = refine(words, i0, i1, wa, wb, db)
        print(f"  edges '{ws[0]['w']}..{ws[-1]['w']}': start {wa:.3f}->{new_a:.3f}  end {wb:.3f}->{new_b:.3f}")
        return new_a, new_b, ws

    shorts.window = window
    sys.argv = [str(shorts_dir / "shorts.py"), a.video_dir] + a.shorts_args
    return shorts.main() or 0


if __name__ == "__main__":
    sys.exit(main())
