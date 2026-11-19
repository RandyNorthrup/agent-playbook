#!/usr/bin/env python3
"""Fresh top-level posts from Bluesky custom feeds, ranked by likes + 3 x replies.

A feed is its at:// URI: at://did:plc:<id>/app.bsky.feed.generator/<name>. Posts under 30 hours old
(--hours); replies left out; a post in several feeds is listed once.
Output: handle rkey | age replies likes | text
Exit 1 when every request failed; a feed that failed is reported on stderr as "ERR feed: reason".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG FEED_URI [FEED_URI ...] [--hours 30]")
    ap.add_argument("feeds", nargs="+", metavar="FEED_URI")
    ap.add_argument("--hours", type=float, default=30.0, help="oldest post to list, in hours (default 30)")
    return ap


def rows_from_feed(data, now, max_hours=30.0):
    """(score, age_hours, handle, rkey, replies, likes, text) for the feed's recent top-level posts. Raises
    ValueError when the reply holds no feed list (the API format changed)."""
    feed = common.as_dict(data).get("feed")
    if not isinstance(feed, list):
        raise ValueError("no feed list in the reply (the API format may have changed)")
    rows = []
    for it in feed:
        p = common.as_dict(common.as_dict(it).get("post"))
        record = common.as_dict(p.get("record"))
        if record.get("reply"):
            continue
        try:
            age = common.hours_since(common.as_text(p.get("indexedAt")), now)
        except ValueError:
            continue
        if age > max_hours:
            continue
        rc, lc = common.as_count(p.get("replyCount")), common.as_count(p.get("likeCount"))
        rows.append((lc + 3 * rc, age, common.as_text(common.as_dict(p.get("author")).get("handle")) or "?",
                     common.as_text(p.get("uri")).split("/")[-1], rc, lc, common.as_text(record.get("text"))[:160].replace("\n", " ")))
    return rows


def main(argv=None):
    a = build_parser().parse_args(argv)
    for feed in a.feeds:
        if not feed.startswith("at://"):
            common.die("not a feed URI (at://did:plc:.../app.bsky.feed.generator/name): %s" % feed, common.EXIT_USAGE)
    if a.hours <= 0:
        common.die("--hours must be above 0", common.EXIT_USAGE)
    common.safe_stdio()
    now, rows, tally = common.now_utc(), [], common.Tally()
    for feed in a.feeds:
        try:
            got = rows_from_feed(common.bsky("app.bsky.feed.getFeed", 20, feed=feed, limit=40), now, a.hours)
        except (common.FetchError, ValueError) as e:
            tally.bad(feed, e)
            continue
        tally.good()
        rows += got
    seen = set()
    for _, age, handle, rkey, rc, lc, text in sorted(rows, reverse=True):
        if (handle, rkey) in seen:
            continue
        seen.add((handle, rkey))
        print(f"{handle} {rkey} | {age:.0f}h r{rc} l{lc} | {text}")
    return tally.exit_code()


if __name__ == "__main__":
    sys.exit(main())
