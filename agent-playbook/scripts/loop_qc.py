#!/usr/bin/env python3
"""Measure whether an animation loops cleanly and plays at a sane speed (UI effects, sprite loops, overlays).

  loop_qc.py ANIMATION [--fps 30] [--out QC_DIR] [--bg 1e2128] [--allow-boil]

ANIMATION is a folder of frames (PNG/WebP, sorted by name), or an animated GIF / WebP / APNG. Give --fps
for a frame folder (an animated file's own frame durations are used otherwise). The frames must be one full
cycle: the frame after the last is the first again.

It reports, and fails (exit 1) on:
  step    the median mean change per pixel between neighbouring frames (0-255): the motion
  jump    the biggest step / the median step. A flicker, a pop or a frame from a different drawing shows
          up here. Limit 2.5.
  seam    the step from the last frame back to the first / the median step. A clean loop's seam is an
          ordinary step. Limit 1.5.
  boil    how much of each step is NOT explained by things moving a pixel or two (0 = smooth motion, 1 =
          every frame drawn afresh). Generated frame sheets boil. Limit 0.5; pass --allow-boil only for an
          effect meant to flicker in place, and then look at it.
  pop     the 99.9th percentile of one step's per-pixel change (0-255), reported for looking at.
  cycle   the loop's length in seconds. Calm ambient loops run 2 to 10 s; under 1.5 s is flagged as too
          fast for anything meant to sit on screen.

and writes QC_DIR/seam.png (the last three frames over the first three, 2x) and strip.png (12 frames
across the cycle) to look at. QC_DIR defaults to <ANIMATION>_loopqc beside the animation. Numbers first,
eyes second: look at both sheets and play the animation at real speed before showing anyone.

Why: generated frame sheets redraw every frame from scratch, so effects jump and the loop never closes.
Looping motion has to be built so it loops: a still or a tileable strip moved by code, offset modulo the
tile, noise sampled on a circle.

Exit codes: 0 the loop passes, 1 it fails a limit, 2 the input can't be measured (missing, fewer than 3
frames, frames of different sizes, a bad option), 3 numpy or Pillow is missing.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = ["numpy", "Pillow"]
EFFECTS = ["files"]
LIMITS = {"jump": 2.5, "seam": 1.5, "boil": 0.5}
FLOOR = 0.05  # mean change per pixel below which two frames count as the same
MIN_CYCLE = 1.5
np = Image = ImageDraw = ImageSequence = None  # loaded in main(), so --help works without them


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG ANIMATION [--fps 30] [--out QC_DIR] [--bg 1e2128] [--allow-boil]")
    ap.add_argument("animation", metavar="ANIMATION", help="a folder of frames, or a GIF / WebP / APNG")
    ap.add_argument("--fps", type=float, help="frame rate (needed for a frame folder; default 30)")
    ap.add_argument("--out", metavar="QC_DIR", help="where seam.png and strip.png go")
    ap.add_argument("--bg", default="1e2128", metavar="RRGGBB", help="colour transparent frames are laid over")
    ap.add_argument("--allow-boil", action="store_true", help="the effect is meant to flicker in place")
    return ap


def load_modules():
    global np, Image, ImageDraw, ImageSequence
    np = common.require("numpy")
    common.require("PIL", "Pillow")
    from PIL import Image, ImageDraw, ImageSequence


def load(path, fps):
    """(frames as RGBA arrays, cycle length in seconds)."""
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".png", ".webp"))
        frames = [np.asarray(Image.open(p).convert("RGBA")) for p in files]
        return frames, len(frames) / (fps or 30.0)
    im = Image.open(path)
    frames, total = [], 0.0
    for f in ImageSequence.Iterator(im):
        frames.append(np.asarray(f.convert("RGBA")))
        total += (f.info.get("duration") or (1000 / (fps or 30.0))) / 1000
    if fps:
        total = len(frames) / fps
    return frames, total


def change(a, b):
    """The per-pixel change from frame a to frame b: the biggest of the three channel differences (0-255).
    np.maximum over the channels, not .max(-1): a reduction over a 3-wide last axis is several times slower."""
    d = np.abs(b - a)
    return np.maximum(np.maximum(d[..., 0], d[..., 1]), d[..., 2])


def mean(d):
    return float(d.mean(dtype=np.float64))  # float32 frames, float64 sums: no rounding drift on big frames


def residual(a, b, reach=2):
    """How much of the change from a to b is NOT explained by things moving a pixel or two: 0 for smooth
    motion, near 1 when the next frame is drawn afresh (effects jumping to new places)."""
    raw = mean(change(a, b))
    if raw < FLOOR:
        return 0.0
    h, w = a.shape[:2]
    best = raw
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            sa = a[max(0, -dy): h - max(0, dy), max(0, -dx): w - max(0, dx)]
            sb = b[max(0, dy): h - max(0, -dy), max(0, dx): w - max(0, -dx)]
            best = min(best, mean(change(sa, sb)))
    return best / raw


def flatten(f, bg):
    """The RGBA frame laid over the background colour, as float32 RGB (half the memory of float64: a hundred
    1080x1920 frames are 2.5 GB instead of 5)."""
    a = f[..., 3:4].astype(np.float32) / 255
    return f[..., :3].astype(np.float32) * a + np.array(bg, np.float32) * (1 - a)


def measure(rgb, cycle, allow_boil=False):
    """(metrics, failed limits) for flattened frames that make one full cycle."""
    steps, pops = [], []
    for i in range(len(rgb)):
        d = change(rgb[i], rgb[(i + 1) % len(rgb)])
        steps.append(mean(d))  # mean change per pixel, 0-255
        pops.append(float(np.percentile(d, 99.9)))
    inner = steps[:-1]  # the last entry is the seam
    med = max(float(np.median(inner)), FLOOR)  # a near-still animation mustn't blow the ratios up
    boil = float(np.median([residual(rgb[i], rgb[(i + 1) % len(rgb)]) for i in range(0, len(rgb), max(1, len(rgb) // 60))]))
    m = {"step": med, "jump": max(inner) / med, "seam": steps[-1] / med, "boil": boil, "pop": max(pops)}
    if allow_boil:  # an effect meant to flicker in place (lightning): say so, and look at it
        m["boil"] = 0.0
    bad = [k for k, v in LIMITS.items() if m[k] > v]
    if cycle < MIN_CYCLE:
        bad.append("too fast")
    return m, bad


def write_sheets(rgb, bg, out):
    out.mkdir(parents=True, exist_ok=True)
    h, w = rgb[0].shape[:2]
    seam = rgb[-3:] + rgb[:3]
    sheet = Image.new("RGB", (w * 2 * 3, h * 2 * 2), bg)
    for i, f in enumerate(seam):
        sheet.paste(Image.fromarray(f.round().astype(np.uint8)).resize((w * 2, h * 2), Image.NEAREST), ((i % 3) * w * 2, (i // 3) * h * 2))
    ImageDraw.Draw(sheet).text((6, 6), "last 3 frames (top) -> first 3 frames (bottom)", fill=(230, 230, 230))
    sheet.save(out / "seam.png")
    picks = [rgb[round(i * (len(rgb) - 1) / 11)] for i in range(12)]
    strip = Image.new("RGB", (w * 6, h * 2), bg)
    for i, f in enumerate(picks):
        strip.paste(Image.fromarray(f.round().astype(np.uint8)), ((i % 6) * w, (i // 6) * h))
    strip.save(out / "strip.png")


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    bg_hex = a.bg.lstrip("#")
    if len(bg_hex) != 6 or any(c not in "0123456789abcdefABCDEF" for c in bg_hex):
        ap.error("--bg is six hex digits (1e2128), got %r" % a.bg)
    if a.fps is not None and a.fps <= 0:
        ap.error("--fps must be above 0")
    path = Path(a.animation)
    if not path.exists():
        common.die("no such file or folder: %s" % path, common.EXIT_USAGE)
    load_modules()
    bg = tuple(int(bg_hex[i: i + 2], 16) for i in (0, 2, 4))
    out = Path(a.out) if a.out else path.with_name(path.stem + "_loopqc")
    try:
        frames, cycle = load(path, a.fps)
    except Exception as e:  # Pillow raises several types for a file that isn't an image
        common.die("can't read %s as an animation: %s" % (path, e), common.EXIT_USAGE)
    if len(frames) < 3:
        common.die("need at least 3 frames, found %d in %s" % (len(frames), path), common.EXIT_USAGE)
    if len({f.shape for f in frames}) != 1:
        common.die("the frames are not all the same size: %s" % sorted({f.shape[1::-1] for f in frames}), common.EXIT_USAGE)
    rgb = [flatten(f, bg) for f in frames]
    m, bad = measure(rgb, cycle, a.allow_boil)
    common.safe_stdio()
    print(
        f"{path.name}: {len(frames)} frames, cycle {cycle:.2f}s | step {m['step']:.2f}/255 | jump x{m['jump']:.1f} | seam x{m['seam']:.1f}"
        f" | boil {m['boil']:.2f} | pop {m['pop']:.0f} | {'FAIL ' + ', '.join(bad) if bad else 'ok'}"
    )
    try:
        write_sheets(rgb, bg, out)
    except OSError as e:
        common.die("can't write the QC sheets to %s: %s" % (out, e.strerror or e), common.EXIT_USAGE)
    if bad:
        common.warn("FAIL: %s (limits: jump %.1f, seam %.1f, boil %.1f, cycle %.1f s or longer); sheets in %s"
                    % (", ".join(bad), LIMITS["jump"], LIMITS["seam"], LIMITS["boil"], MIN_CYCLE, out))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
