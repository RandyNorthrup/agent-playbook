#!/usr/bin/env python3
"""Make the LinkedIn-size (1584x396) banner page from the 1500x500 X/Reddit night-sky banner page.

  mklibanner.py banner.html [banner_li.html]
Reads the 1500x500 banner's HTML, swaps its sizes and positions for the 1584x396 layout (one-line
headline, moon and waves moved, Google Fonts instead of local font files) and writes the result
(default: banner_li.html beside the source). Render it with:
  render_carousel.py --html banner_li.html --name banner-li --count 1 --width 1584 --height 396 --out DIR
The finished 1584x396 page and the 1640x624 Facebook cover are kept in templates/banner_li.html and
templates/banner_fb.html; this script is only needed when the 1500x500 source changes.
Exit 1 when the source can't be read or an expected piece of it is missing (each one is listed), so a
changed source never produces a half-converted banner.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["files"]
FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Oxanium:wght@700&family=SUSE+Mono:wght@400&display=block">')
LOCAL_FONTS = [
    '  @font-face { font-family: Oxanium; font-weight: 700 800; src: url(oxanium.woff2) format("woff2"); }\n',
    '  @font-face { font-family: "SUSE Mono"; font-weight: 400; src: url(suse-mono.woff2) format("woff2"); }\n',
]
SWAPS = [
    ('<meta charset="utf-8">', '<meta charset="utf-8">\n' + FONTS),
    ("width: 1500px; height: 500px;", "width: 1584px; height: 396px;"),
    (".moon { position: absolute; top: 92px; left: 1318px; }", ".moon { position: absolute; top: 78px; left: 1452px; }"),
    (".text { position: absolute; left: 0; right: 0; top: 118px; text-align: center; }",
     ".text { position: absolute; left: 360px; right: 300px; top: 88px; text-align: center; }"),
    ("font-size: 72px; line-height: 1;", "font-size: 54px; line-height: 1;"),
    ('margin-top: 22px; font-family: "SUSE Mono", monospace; font-size: 23px;',
     'margin-top: 18px; font-family: "SUSE Mono", monospace; font-size: 20px;'),
    ('margin-top: 20px; font-family: "SUSE Mono", monospace; font-size: 19px;',
     'margin-top: 14px; font-family: "SUSE Mono", monospace; font-size: 17px;'),
    ("left:-150px;top:436px", "left:-110px;top:330px"),
    ("left:760px;top:401px", "left:1010px;top:330px"),
    ("left:310px;top:486px", "left:380px;top:372px"),
    (".waves { position: absolute; left: 0; right: 0; bottom: 0; height: 70px; }",
     ".waves { position: absolute; left: 0; bottom: 0; width: 1584px; height: 54px; }"),
    ("left: r() * 1500 + 'px', top: r() * 360 + 'px'", "left: r() * 1584 + 'px', top: r() * 290 + 'px'"),
]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG BANNER_HTML [OUT_HTML]")
    ap.add_argument("source", metavar="BANNER_HTML", help="the 1500x500 banner page")
    ap.add_argument("out", nargs="?", metavar="OUT_HTML", help="default: banner_li.html beside the source")
    return ap


# The two-line headline, whatever it says, becomes one line: "<h1>A<br>B</h1>" -> "<h1>A · B</h1>".
HEADLINE = re.compile(r"<h1>([^<]*?)\s*<br\s*/?>\s*([^<]*?)</h1>")
HEADLINE_PIECE = "<h1>FIRST LINE<br>SECOND LINE</h1>"


def convert(src):
    """(the LinkedIn-size page, [expected pieces the source did not contain])."""
    missing = [old for old, _ in SWAPS if old not in src]
    if not HEADLINE.search(src):
        missing.append(HEADLINE_PIECE)
    for font in LOCAL_FONTS:
        src = src.replace(font, "")
    for old, new in SWAPS:
        src = src.replace(old, new)
    src = HEADLINE.sub(lambda m: "<h1>%s · %s</h1>" % (m.group(1), m.group(2)), src, count=1)
    return src, missing


def main(argv=None):
    a = build_parser().parse_args(argv)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.source)), "banner_li.html")
    page, missing = convert(common.read_text(a.source))
    if missing:
        common.safe_stdio()
        common.die("%s is not the 1500x500 banner this converts; %d expected piece%s missing:\n  %s"
                   % (a.source, len(missing), "" if len(missing) == 1 else "s", "\n  ".join(missing)))
    try:
        common.write_atomic(out, page)
    except OSError as e:
        common.die("can't write %s: %s" % (out, e.strerror or e))
    print("ok", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
