#!/usr/bin/env python3
"""Map Facebook comment authors to their Reply and "View N replies" refs from a saved snapshot file.

When a browser snapshot is too big, the harness saves it to a file. Parse that file
instead of reading it into context.
With chrome-control 1.12 or newer, try browser_find first (regex "^Reply$", role button): it returns
only the matching elements with their refs, so there is no big snapshot to save.

  fb_snap.py <snapshot-file> [all|reply|view]
Output:
  REPLY e140 Reply by Jordan Price to Sean Wendland's comment 3 hours ago
  VIEW  e241 View 2 replies
Refs go stale after the page re-renders; re-snapshot if a click says "Unknown element ref".
Exit 1 when the file can't be read. A snapshot with no matching refs prints a note on stderr and exits 0.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]
ARTICLE = re.compile(r'article "((?:Comment|Reply) by [^"]+)" \[ref=(e\d+)\]')
VIEW = re.compile(r'button "((?:View|See) [^"]*repl[^"]*|View more comments|View previous comments)" \[ref=(e\d+)\]')
REPLY = re.compile(r'button "Reply" \[ref=(e\d+)\]')


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG SNAPSHOT_FILE [all|reply|view]")
    ap.add_argument("snapshot", metavar="SNAPSHOT_FILE")
    ap.add_argument("mode", nargs="?", default="all", choices=["all", "reply", "view"])
    return ap


def snapshot_text(raw):
    """Some harnesses save [{"type":"text","text":"..."}]; others the text itself."""
    try:
        return json.loads(raw)[0]["text"]
    except (ValueError, LookupError, TypeError):
        return raw


def scan(text, mode="all"):
    """[("VIEW" | "REPLY", ref, label)] in page order."""
    out, current = [], None
    for line in text.splitlines():
        m = ARTICLE.search(line)
        if m:
            current = m.group(1)
        if mode in ("all", "view"):
            v = VIEW.search(line)
            if v:
                out.append(("VIEW", v.group(2), v.group(1)))
        if mode in ("all", "reply"):
            r = REPLY.search(line)
            if r and current:
                out.append(("REPLY", r.group(1), current))
                current = None
    return out


def main(argv=None):
    a = build_parser().parse_args(argv)
    common.safe_stdio()
    found = scan(snapshot_text(common.read_text(a.snapshot)), a.mode)
    for kind, ref, label in found:
        print("%-5s %s %s" % (kind, ref, label))
    if not found:
        common.warn("no Reply or View refs in %s (is it a Facebook comments snapshot?)" % a.snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
