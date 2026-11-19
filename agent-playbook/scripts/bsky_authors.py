#!/usr/bin/env python3
"""Recent original posts from chosen Bluesky accounts, ranked by likes + 3 x replies.

Posts under 14 hours old (--hours), reposts and replies left out. Handles match without case or a leading @.
Output: handle rkey | age replies likes | text
Exit 1 when every request failed; a handle that failed is reported on stderr as "ERR handle: reason".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG HANDLE [HANDLE ...] [--hours 14] [--limit 40]")
    ap.add_argument("handles", nargs="+", metavar="HANDLE")
    ap.add_argument("--hours", type=float, default=14.0, help="oldest post to list, in hours (default 14)")
    ap.add_argument("--limit", type=int, default=40, help="most rows to print (default 40)")
    return ap


def rows_from_feed(data, handle, now, max_hours=14.0):
    """(score, handle, rkey, age_hours, replies, likes, text) for the account's own recent posts. Raises
    ValueError when the reply holds no feed list (the API format changed)."""
    feed = common.as_dict(data).get("feed")
    if not isinstance(feed, list):
        raise ValueError("no feed list in the reply (the API format may have changed)")
    rows = []
    for it in feed:
        it = common.as_dict(it)
        if it.get("reason"):  # a repost
            continue
        p = common.as_dict(it.get("post"))
        if not common.same_handle(common.as_text(common.as_dict(p.get("author")).get("handle")), handle):
            continue
        try:
            age = common.hours_since(common.as_text(p.get("indexedAt")), now)
        except ValueError:
            continue
        if age > max_hours:
            continue
        rc, lc = common.as_count(p.get("replyCount")), common.as_count(p.get("likeCount"))
        rows.append((lc + 3 * rc, common.norm_handle(handle), common.as_text(p.get("uri")).split("/")[-1], age, rc, lc,
                     common.as_text(common.as_dict(p.get("record")).get("text"))[:150].replace("\n", " ")))
    return rows


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.hours <= 0 or a.limit < 1:
        ap.error("--hours must be above 0 and --limit 1 or more")
    common.safe_stdio()
    now, rows, tally = common.now_utc(), [], common.Tally()
    for handle in a.handles:
        handle = common.norm_handle(handle)
        try:
            data = common.bsky("app.bsky.feed.getAuthorFeed", 15, actor=handle, limit=15, filter="posts_no_replies")
            got = rows_from_feed(data, handle, now, a.hours)
        except (common.FetchError, ValueError) as e:
            tally.bad(handle, e)
            continue
        tally.good()
        rows += got
    rows.sort(reverse=True)
    for _, handle, rkey, age, rc, lc, text in rows[:a.limit]:
        print(f"{handle} {rkey} | {age:.0f}h r{rc} l{lc} | {text}")
    return tally.exit_code()


if __name__ == "__main__":
    sys.exit(main())
