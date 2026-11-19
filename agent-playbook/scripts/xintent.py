#!/usr/bin/env python3
"""Build an X intent URL for a new post or a reply, and report the post's weighted length.

  xintent.py <status_id|0> "text"   (0 = new post, otherwise a reply to that post)
Prints the URL on stdout and "weighted length N/280" on stderr. The count follows X's own rules
(common.x_weighted): a link counts 23, CJK and most symbols 2, one emoji 2, curly quotes and dashes 1.
Exit 1 when the text is over 280 (the URL is still printed); exit 2 on a missing or malformed argument.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, 'PROG STATUS_ID|0 "TEXT"')
    ap.add_argument("status_id", metavar="STATUS_ID", help="the post to reply to, or 0 for a new post")
    ap.add_argument("text", metavar="TEXT")
    return ap


def intent_url(status_id, text):
    base = "https://x.com/intent/post?text=" if status_id == "0" else "https://x.com/intent/post?in_reply_to=%s&text=" % status_id
    return base + urllib.parse.quote(text, safe="")


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.status_id.isdigit():
        ap.error("STATUS_ID is the post's number, or 0 for a new post; got %r" % a.status_id)
    if not a.text.strip():
        ap.error("TEXT is empty")
    weighted = common.x_weighted(a.text)
    over = weighted - common.X_LIMIT
    print("weighted length %d/%d%s" % (weighted, common.X_LIMIT, " OVER by %d" % over if over > 0 else ""), file=sys.stderr)
    print(intent_url(a.status_id, a.text))
    return 1 if over > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
