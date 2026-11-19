#!/usr/bin/env python3
"""List replies on the owner's recent Bluesky threads that the owner has not answered.

Output: time(UTC) handle rkey | text
Open a reply at https://bsky.app/profile/<handle>/post/<rkey>.
The owner handle matches without case or a leading @.
Tip (Windows): set PYTHONIOENCODING=utf-8 so emoji print as themselves (otherwise they print as "?").
Exit 1 when the owner's feed can't be read or every thread request failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG OWNER_HANDLE [THREADS_TO_SCAN]")
    ap.add_argument("owner", metavar="OWNER_HANDLE")
    ap.add_argument("threads", nargs="?", type=int, default=70, metavar="THREADS_TO_SCAN",
                    help="how many of the owner's recent posts to check (default 70)")
    return ap


def open_replies(node, owner, out=None):
    """{(time, handle, rkey, text)} for every reply under `node` by someone else that the owner has not
    answered directly."""
    out = set() if out is None else out
    for r in common.as_list(common.as_dict(node).get("replies")):
        r = common.as_dict(r)
        p = common.as_dict(r.get("post"))
        if not p:
            continue  # a deleted or blocked reply
        handle = author(p)
        if handle and not common.same_handle(handle, owner):
            answered = any(common.same_handle(author(common.as_dict(common.as_dict(rr).get("post"))), owner)
                           for rr in common.as_list(r.get("replies")))
            if not answered:
                out.add((common.as_text(p.get("indexedAt"))[:16], handle, common.as_text(p.get("uri")).split("/")[-1],
                         common.as_text(common.as_dict(p.get("record")).get("text"))[:220].replace("\n", " ")))
        open_replies(r, owner, out)
    return out


def author(post):
    return common.as_text(common.as_dict(post.get("author")).get("handle"))


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.threads < 1:
        common.die("THREADS_TO_SCAN must be 1 or more", common.EXIT_USAGE)
    common.safe_stdio()
    owner = common.norm_handle(a.owner)
    try:
        feed = common.as_dict(common.bsky("app.bsky.feed.getAuthorFeed", actor=owner, limit=100)).get("feed")
        if not isinstance(feed, list):
            raise ValueError("no feed list in the reply (the API format may have changed)")
    except (common.FetchError, ValueError) as e:
        common.die("can't read %s's feed: %s" % (owner, e))
    feed = [common.as_dict(it) for it in feed]
    uris = [common.as_dict(it.get("post")).get("uri") for it in feed if not it.get("reason")]
    uris = [u for u in uris if isinstance(u, str) and u]
    out, tally = set(), common.Tally()
    for uri in uris[:a.threads]:
        try:
            thread = common.as_dict(common.bsky("app.bsky.feed.getPostThread", depth=6, uri=uri)).get("thread")
            if not isinstance(thread, dict):
                raise ValueError("no thread in the reply (the API format may have changed)")
        except (common.FetchError, ValueError) as e:
            tally.bad(uri, e)
            continue
        tally.good()
        open_replies(thread, owner, out)
    for when, handle, rkey, text in sorted(out, reverse=True)[:40]:
        print(when, handle, rkey, "|", text)
    return tally.exit_code("thread request")


if __name__ == "__main__":
    sys.exit(main())
