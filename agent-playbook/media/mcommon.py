#!/usr/bin/env python3
"""Shared helpers for the agent-playbook media tools: ffmpeg, fonts, image measures and the format numbers.

Not a command. Standard library only at import time: Pillow is loaded inside the functions that draw or
measure, through common.require, so every tool's --help works on a machine without it.

Exit codes every media tool uses (the same as scripts/):
  0  worked
  1  failed (one line on stderr, prefixed with the script name); this includes a missing external program
     (ffmpeg, codex, Chrome), with how to point at it
  2  usage error (argparse)
  3  a Python module is missing ("needs PIL: pip install Pillow")

ffmpeg and ffprobe are $AGENT_PLAYBOOK_FFMPEG and $AGENT_PLAYBOOK_FFPROBE, else the ones on PATH.
A font is a path, a bare file name looked up in the font folders, or the word "builtin".
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
for _folder in (SCRIPTS, HERE):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)
import common  # noqa: E402
from common import die, make_parser, read_text, require, run_silent, safe_stdio, warn, write_atomic  # noqa: E402,F401

# ---------------------------------------------------------------- the format numbers

FRAME_9x16 = (1080, 1920)
FPS = 30
SAFE = {"top": 280, "right": 950, "bottom": 1600}  # on a 1080x1920 frame: text stays inside these
CROP_3x4 = (240, 1680)      # the rows of a 9:16 frame a 3:4 feed crop shows
CROP_SQUARE = (420, 1500)   # the rows a square crop shows
THUMB = (1280, 720)
THUMB_MAX_BYTES = 2 * 1024 * 1024 - 50000
UPLOAD_MAX_BYTES = 2 * 1024 ** 3
FEED = ((360, 202), (168, 94))  # the sizes a thumbnail is shown at in feeds
YT_BANNER = {"size": (2560, 1440), "desktop": (2560, 423), "safe": (1546, 423)}
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
TAIL = ["-movflags", "+faststart", "-map_metadata", "-1"]

PROBE_TIMEOUT = 120       # seconds for one ffprobe, or one frame out of ffmpeg: longer is a hung process

FLAT_STDDEV = 2.0         # luma standard deviation under this: one flat colour
BLACK_LEVEL = 3           # a tile whose brightest pixel is under this is black
ALPHA_MIN_SHARE = 0.005   # at least this share of pixels fully transparent: a real cut-out


def pillow():
    """The PIL package, or exit 3 with the install line."""
    return require("PIL", "Pillow")


# ---------------------------------------------------------------- ffmpeg

def _program(name, env):
    given = os.environ.get(env)
    if given:
        found = given if os.path.isfile(given) else shutil.which(given)
        if not found:
            die("%s not found at %s ($%s)" % (name, given, env))  # never fall back to another build silently
        return found
    found = shutil.which(name)
    if not found:
        die("%s not found on PATH; install it or set %s" % (name, env))
    return found


def ffmpeg():
    return _program("ffmpeg", "AGENT_PLAYBOOK_FFMPEG")


def ffprobe():
    return _program("ffprobe", "AGENT_PLAYBOOK_FFPROBE")


def run_ff(cmd, what, **kw):
    """Run an ffmpeg or ffprobe command silently. A non-zero exit is exit 1 with `what` and the end of its
    stderr; so is passing `timeout` (seconds) and running past it, which ends the process and anything it
    started. Returns the finished process (stdout and stderr as bytes). Its stdin is empty unless `input` is
    given, so ffmpeg never reads keys from the console."""
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    if kw.get("input") is None:
        kw.pop("input", None)
        kw.setdefault("stdin", subprocess.DEVNULL)
    try:
        r = common.run_tree(cmd, **kw)
    except subprocess.TimeoutExpired:
        die("%s: %s did not finish in %g s" % (what, os.path.basename(cmd[0]), kw["timeout"]))
    except OSError as e:
        die("%s: can't start %s: %s" % (what, cmd[0], e.strerror or e))
    if r.returncode:
        err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
        die("%s: %s" % (what, err[-1500:].strip() or "exit %d" % r.returncode))
    return r


def probe(path):
    """ffprobe's format and streams for a media file, as a dict."""
    r = run_ff([ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
               "ffprobe %s" % path, timeout=PROBE_TIMEOUT)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except ValueError:
        die("ffprobe %s: the reply is not JSON" % path)


def _rotated(stream):
    """True when the stream is shown turned a quarter (a phone video): ffmpeg then decodes it with the sides swapped."""
    turn = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            turn = side["rotation"]
    if not turn:
        turn = (stream.get("tags") or {}).get("rotate", 0)
    try:
        return int(round(abs(float(turn)))) % 180 == 90
    except (TypeError, ValueError):
        return False


def video_frame(path, t=0.0):
    """One frame of a video at `t` seconds, as an RGB PIL image."""
    pillow()
    from PIL import Image
    video = [s for s in probe(path).get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        die("no video stream in %s" % path)
    w, h = int(video[0]["width"]), int(video[0]["height"])
    if _rotated(video[0]):
        w, h = h, w
    r = run_ff([ffmpeg(), "-v", "error", "-ss", "%.3f" % t, "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"], "frame at %.3f s of %s" % (t, path), timeout=PROBE_TIMEOUT)
    if len(r.stdout) < w * h * 3:
        die("no frame at %.3f s in %s" % (t, path))
    return Image.frombytes("RGB", (w, h), r.stdout[:w * h * 3])


def faststart(path):
    """True when 'moov' comes before 'mdat' in the file's first 64 KiB: the video can start before it has
    all arrived."""
    with open(path, "rb") as fh:
        head = fh.read(1 << 16)
    m, d = head.find(b"moov"), head.find(b"mdat")
    return m != -1 and (d == -1 or m < d)


# ---------------------------------------------------------------- fonts

def font_dirs(font_dir=None):
    """The folders a bare font file name is looked up in, in order."""
    dirs = [font_dir, os.environ.get("AGENT_PLAYBOOK_FONT_DIR")]
    if os.environ.get("WINDIR"):
        dirs.append(os.path.join(os.environ["WINDIR"], "Fonts"))
    dirs += ["/Library/Fonts", "/usr/share/fonts"]
    return [d for d in dirs if d]


def find_font(file, font_dir=None):
    """The path of a font file: a path as given, or a bare name found in font_dirs() (sub-folders included,
    first hit). None when it isn't there."""
    if os.path.isfile(file):
        return file
    if os.path.dirname(file):
        return None
    for folder in font_dirs(font_dir):
        for root, _dirs, names in os.walk(folder):
            for name in names:
                if name.lower() == file.lower():
                    return os.path.join(root, name)
    return None


def font(file, size, variation=None, font_dir=None):
    """A PIL font. `file` is a path, a bare file name looked up in the font folders, or "builtin" (Pillow's
    own scalable font). A font that isn't found is exit 1 naming it and the folders: never a silent substitute."""
    pillow()
    from PIL import ImageFont
    if file == "builtin":
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            die("needs Pillow 10.1 or newer for the builtin font: pip install -U Pillow", common.EXIT_NEEDS)
    path = find_font(file, font_dir)
    if not path:
        die("font not found: %s (looked in: %s)" % (file, ", ".join(font_dirs(font_dir)) or "no folder"))
    try:
        f = ImageFont.truetype(path, size)
        if variation:
            f.set_variation_by_name(variation)
    except (OSError, ValueError) as e:
        die("can't use the font %s%s: %s" % (path, " (%s)" % variation if variation else "", e))
    return f


# ---------------------------------------------------------------- image measures

def is_flat(img):
    """True when the picture is one flat colour: the standard deviation of its luma is under 2.0."""
    from PIL import ImageStat
    return ImageStat.Stat(img.convert("L")).stddev[0] < FLAT_STDDEV


def black_blocks(img, block=64):
    """How many block x block tiles (on a grid from the top-left corner; part tiles at the right and bottom
    edges are not counted) have no pixel brighter than 2: what a failed upscale leaves behind."""
    grey = img.convert("L")
    w, h = grey.size
    count = 0
    for y in range(0, h - block + 1, block):
        for x in range(0, w - block + 1, block):
            if grey.crop((x, y, x + block, y + block)).getextrema()[1] < BLACK_LEVEL:
                count += 1
    return count


def _runs(values):
    """Lengths of the runs of equal values in a sequence."""
    out, last, n = [], None, 0
    for v in values:
        if n and v == last:
            n += 1
        else:
            if n:
                out.append(n)
            last, n = v, 1
    if n:
        out.append(n)
    return out


def _even(runs):
    """True for three or more runs whose inner ones all have the same length, give or take 2 pixels."""
    if len(runs) < 3:
        return False
    inner = sorted(runs[1:-1])
    middle = inner[len(inner) // 2]
    return all(abs(r - middle) <= 2 for r in inner) and runs[0] <= middle + 2 and runs[-1] <= middle + 2


def _checker_patch(patch):
    """True when a patch holds only two grey levels, alternating in a regular grid."""
    from PIL import ImageChops
    r, g, b = patch.convert("RGB").split()
    if max(ImageChops.difference(r, g).getextrema()[1], ImageChops.difference(g, b).getextrema()[1]) > 16:
        return False  # coloured, so not the grey squares
    grey = patch.convert("L")
    lo, hi = grey.getextrema()
    if hi - lo < 10:
        return False  # one level only
    w, h = grey.size
    mid = (lo + hi) / 2.0
    pixels = grey.tobytes()  # one byte per pixel, row by row
    hist = grey.histogram()  # counted in C, not pixel by pixel in Python
    near = sum(hist[v] for v in range(256) if v - lo <= 12 or hi - v <= 12)
    if near < 0.9 * len(pixels):
        return False  # a gradient or a picture, not two levels
    rows = [[v > mid for v in pixels[y * w:(y + 1) * w]] for y in (h // 4, h // 2, 3 * h // 4)]
    cols = [[pixels[y * w + x] > mid for y in range(h)] for x in (w // 4, w // 2, 3 * w // 4)]
    return any(_even(_runs(line)) for line in rows) and any(_even(_runs(line)) for line in cols)


def painted_checkerboard(img):
    """True when all four corners of the picture show the grey checkerboard that stands for "transparent" in
    an editor: a generator painted it instead of leaving real transparency."""
    w, h = img.size
    side = max(16, min(160, min(w, h) // 5))
    if w < side or h < side:
        return False
    boxes = [(0, 0), (w - side, 0), (0, h - side), (w - side, h - side)]
    return all(_checker_patch(img.crop((x, y, x + side, y + side))) for x, y in boxes)


def alpha_problem(img):
    """Why the picture is not a real cut-out, or "" when it is: it has an alpha channel and at least 0.5% of
    its pixels are fully transparent."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    share = 0.0
    if has_alpha:
        # an RGBA, LA or PA picture has its alpha band already: no full-size RGBA copy just to read it
        alpha = img.getchannel("A") if "A" in img.getbands() else img.convert("RGBA").getchannel("A")
        hist = alpha.histogram()
        share = hist[0] / float(sum(hist) or 1)
        if share >= ALPHA_MIN_SHARE:
            return ""
    if painted_checkerboard(img.convert("RGB")):
        return "opaque image with a painted checkerboard"
    if not has_alpha:
        return "no alpha channel"
    return "alpha channel but only %.2f%% of pixels are transparent" % (100 * share)


def real_alpha(img):
    return not alpha_problem(img)


# ---------------------------------------------------------------- files, ids, reports

def write_json(path, obj):
    """Write JSON the way every media tool does: indent 1, UTF-8 as is, LF, a trailing newline, in one rename."""
    write_atomic(str(path), json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_ids(text):
    """'3,17,40' as [3, 17, 40]: whole numbers from 1, in the order given, each once. ValueError otherwise."""
    ids = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part.isdigit() or int(part) < 1:
            raise ValueError("not a list of ids like 3,17,40: %r" % (text,))
        if int(part) not in ids:
            ids.append(int(part))
    return ids


class Report(object):
    """Collects one line per check and prints them: 'ok   dims 1280x720  A.jpg' or 'FAIL no-exif  B.jpg: GPS
    tags present'. finish() returns the exit code."""

    def __init__(self):
        self.lines = []  # (passed, check, subject, detail)

    def add(self, passed, check, subject, detail=""):
        self.lines.append((bool(passed), check, subject, detail))
        return bool(passed)

    def ok(self, check, subject, detail=""):
        return self.add(True, check, subject, detail)

    def fail(self, check, subject, detail=""):
        return self.add(False, check, subject, detail)

    def failed(self):
        return [line for line in self.lines if not line[0]]

    def as_list(self):
        return [{"file": subject, "check": check, "ok": passed, "detail": detail}
                for passed, check, subject, detail in self.lines]

    def finish(self):
        """Print every line, then the count of failures on stderr. Returns 0, or 1 when a check failed."""
        safe_stdio()
        for passed, check, subject, detail in self.lines:
            print("%s %s  %s%s" % ("ok  " if passed else "FAIL", check, subject, ": " + detail if detail else ""))
        sys.stdout.flush()
        bad = len(self.failed())
        if bad:
            warn("%d of %d checks failed" % (bad, len(self.lines)))
            return common.EXIT_FAIL
        return 0
