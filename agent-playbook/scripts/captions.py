#!/usr/bin/env python3
"""Write an ASS subtitle file for captions burned into a vertical or landscape video with ffmpeg.

  captions.py cues.json out.ass WIDTH HEIGHT
cues.json: [{"start": 0.0, "end": 3.5, "text": "line one\\nline two", "style": "Cap"}, ...]
  start, end   seconds; end after start
  text         the caption; \\n breaks the line
  style        Cap (default: 64 px Segoe UI Semibold on a dark box), Big (92 px Segoe UI Black) or
               Tag (48 px, teal, a little lower)
Sizes are for a 1080-wide frame and scale with WIDTH. Captions sit in the upper-middle band, clear of the
TikTok/Shorts interface at the bottom and right. Burn in with: ffmpeg -i in.mp4 -vf ass=out.ass ...
Exit 1 when cues.json can't be read or a cue is malformed (the message names the cue).
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["files"]
STYLES = ("Cap", "Big", "Tag")


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG CUES_JSON OUT_ASS WIDTH HEIGHT")
    ap.add_argument("cues", metavar="CUES_JSON")
    ap.add_argument("out", metavar="OUT_ASS")
    ap.add_argument("width", type=int, metavar="WIDTH")
    ap.add_argument("height", type=int, metavar="HEIGHT")
    return ap


def stamp(t):
    """Seconds as an ASS time, H:MM:SS.cc. Rounded to whole centiseconds first, so 59.996 s is 0:01:00.00,
    never 0:00:60.00 (which players read wrong)."""
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    return "%d:%02d:%02d.%02d" % (h, m, cs // 100, cs % 100)


def check(cues):
    """Raise ValueError naming the first malformed cue."""
    if not isinstance(cues, list):
        raise ValueError("the file must hold a list of cues")
    for i, c in enumerate(cues, 1):
        if not isinstance(c, dict):
            raise ValueError("cue %d is not an object" % i)
        for key in ("start", "end"):
            if isinstance(c.get(key), bool) or not isinstance(c.get(key), (int, float)) or (
                    isinstance(c[key], float) and not math.isfinite(c[key])):  # json reads NaN and Infinity
                raise ValueError("cue %d: %s must be a number of seconds" % (i, key))
        if c["end"] <= c["start"]:
            raise ValueError("cue %d: end (%s) is not after start (%s)" % (i, c["end"], c["start"]))
        if not isinstance(c.get("text"), str) or not c["text"].strip():
            raise ValueError("cue %d: text is missing" % i)
        if c.get("style", "Cap") not in STYLES:
            raise ValueError("cue %d: style %r is not one of %s" % (i, c.get("style"), ", ".join(STYLES)))


def build_ass(cues, width, height):
    """The .ass file's text for these cues at this frame size."""
    s = width / 1080.0  # the sizes below are for a 1080-wide frame
    # BorderStyle 3 = opaque box behind the text; BackColour is the box (AABBGGRR, AA 00 = opaque)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Segoe UI Semibold,{int(64*s)},&H00FFFFFF,&H00FFFFFF,&H2A0E0B0B,&H2A0E0B0B,1,0,0,0,100,100,0,0,3,{int(18*s)},0,8,{int(60*s)},{int(120*s)},{int(height*0.17)},1
Style: Big,Segoe UI Black,{int(92*s)},&H00FFFFFF,&H00FFFFFF,&H1A0E0B0B,&H1A0E0B0B,1,0,0,0,100,100,0,0,3,{int(22*s)},0,8,{int(80*s)},{int(140*s)},{int(height*0.15)},1
Style: Tag,Segoe UI Semibold,{int(48*s)},&H00D9C73E,&H00D9C73E,&H330E0B0B,&H330E0B0B,1,0,0,0,100,100,0,0,3,{int(14*s)},0,8,{int(60*s)},{int(120*s)},{int(height*0.15 + 150*s)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = ["Dialogue: 0,%s,%s,%s,,0,0,0,,{\\fad(180,180)}%s"
             % (stamp(c["start"]), stamp(c["end"]), c.get("style", "Cap"), c["text"].replace("\r", "").replace("\n", "\\N"))
             for c in cues]
    return head + "\n".join(lines) + "\n"


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.width < 1 or a.height < 1:
        ap.error("WIDTH and HEIGHT are the frame size in pixels")
    try:
        cues = json.loads(common.read_text(a.cues, "utf-8-sig"))
        check(cues)
    except ValueError as e:
        common.die("%s: %s" % (a.cues, e))
    try:
        with open(a.out, "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write(build_ass(cues, a.width, a.height))
    except OSError as e:
        common.die("can't write %s: %s" % (a.out, e.strerror or e))
    print("wrote", a.out, len(cues), "cues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
