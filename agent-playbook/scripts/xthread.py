#!/usr/bin/env python3
"""Show an X post's author, text and what it replies to, from the public syndication endpoint (no login).

  xthread.py <status_id> [status_id ...]
Output per post: "== id @author: text", then "   in reply to @parent: text" when it is a reply.
A post that failed is reported on stderr ("ERR id: reason"). Exit 1 when every post failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]
URL = "https://cdn.syndication.twimg.com/tweet-result?id=%s&token=a"


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG STATUS_ID [STATUS_ID ...]")
    ap.add_argument("ids", nargs="+", metavar="STATUS_ID")
    return ap


def describe(status_id, d):
    """The lines to print for one tweet-result reply. Raises ValueError when it holds no post
    (deleted, protected or withheld posts come back empty or as a tombstone)."""
    if not isinstance(d, dict) or "text" not in d:
        kind = d.get("__typename", "") if isinstance(d, dict) else ""
        raise ValueError("no post in the reply%s (deleted, protected or withheld)" % (" (%s)" % kind if kind else ""))
    def who(post):
        return common.as_text(common.as_dict(post.get("user")).get("screen_name")) or "?"

    lines = ["== %s @%s: %s" % (status_id, who(d), common.as_text(d.get("text")))]
    parent = common.as_dict(d.get("parent"))
    if parent:
        lines.append("   in reply to @%s: %s" % (who(parent), common.as_text(parent.get("text"))))
    elif d.get("in_reply_to_screen_name"):
        lines.append("   in reply to @%s (%s)" % (d["in_reply_to_screen_name"], d.get("in_reply_to_status_id_str")))
    return lines


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    for i in a.ids:
        if not i.isdigit():
            ap.error("a status id is all digits, got %r" % i)
    common.safe_stdio()
    tally = common.Tally()
    for i in a.ids:
        try:
            lines = describe(i, common.fetch_json(URL % i))
        except (common.FetchError, ValueError) as e:
            tally.bad(i, e)
            continue
        tally.good()
        print("\n".join(lines))
    return tally.exit_code("post")


if __name__ == "__main__":
    sys.exit(main())
