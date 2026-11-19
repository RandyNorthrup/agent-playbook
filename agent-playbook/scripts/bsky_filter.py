#!/usr/bin/env python3
"""Filter Bluesky candidates: drop threads the owner already replied to and reply-gated threads.

Pipe in lines that start with "handle rkey ..." (the output of bsky_feed.py or bsky_authors.py):
  python bsky_authors.py a.bsky.social b.dev | python bsky_filter.py <owner-handle>
Output: f<author_followers> <original line>
A candidate that could not be checked is reported on stderr ("ERR handle rkey: reason") and left out.
Exit 1 when the owner handle can't be resolved or every candidate failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG OWNER_HANDLE < candidates")
    ap.add_argument("owner", metavar="OWNER_HANDLE")
    return ap


def candidate(line):
    """(handle, rkey) from a "handle rkey ..." line, or None for anything else."""
    parts = line.split(" ", 2)
    if len(parts) < 3 or not parts[0] or not parts[1]:
        return None
    return common.norm_handle(parts[0]), parts[1]


def replied(node, did):
    """True when the account `did` has a reply anywhere under this thread node."""
    for r in common.as_list(common.as_dict(node).get("replies")):
        if common.as_dict(common.as_dict(common.as_dict(r).get("post")).get("author")).get("did") == did or replied(r, did):
            return True
    return False


def gated(thread, threadgate=None):
    """True when replies to the post are limited. getPostThread puts the threadgate beside the thread (and in
    the post view); a threadgate whose record has no "allow" list only hides replies, and anyone may still
    reply. One with an "allow" list (even one that might let the owner in) counts as limited."""
    thread = common.as_dict(thread)
    for gate in (threadgate, thread.get("threadgate"), common.as_dict(thread.get("post")).get("threadgate")):
        if gate:
            record = common.as_dict(gate).get("record")
            if not isinstance(record, dict) or "allow" in record:
                return True
    return False


def keep(thread, owner_did, threadgate=None):
    """True for a thread worth replying to: a post that is there (not deleted or blocked), open to replies and
    not answered by the owner yet. `threadgate` is the threadgate from beside the thread in the reply."""
    thread = common.as_dict(thread)
    if not thread.get("post") or thread.get("notFound") or thread.get("blocked"):
        return False
    return not gated(thread, threadgate) and not replied(thread, owner_did)


def did_of(reply):
    """The did in a resolveHandle reply. Raises ValueError when there is none (a changed reply format)."""
    did = common.as_dict(reply).get("did")
    if not isinstance(did, str) or not did:
        raise ValueError("no did in the reply (the API format may have changed)")
    return did


def main(argv=None):
    a = build_parser().parse_args(argv)
    common.safe_stdio()
    try:
        owner_did = did_of(common.bsky("com.atproto.identity.resolveHandle", handle=common.norm_handle(a.owner)))
    except (common.FetchError, ValueError) as e:
        common.die("can't resolve the owner handle %s: %s" % (a.owner, e))
    seen, tally = set(), common.Tally()
    for line in sys.stdin:
        c = candidate(line)
        if not c or c in seen:
            continue
        seen.add(c)
        handle, rkey = c
        try:
            did = did_of(common.bsky("com.atproto.identity.resolveHandle", handle=handle))
            reply = common.as_dict(common.bsky("app.bsky.feed.getPostThread", depth=3, parentHeight=0,
                                               uri="at://%s/app.bsky.feed.post/%s" % (did, rkey)))
            if not isinstance(reply.get("thread"), dict):
                raise ValueError("no thread in the reply (the API format may have changed)")
        except (common.FetchError, ValueError) as e:
            tally.bad("%s %s" % (handle, rkey), e)
            continue
        tally.good()
        if not keep(reply["thread"], owner_did, reply.get("threadgate")):
            continue
        try:
            followers = common.as_count(common.as_dict(common.bsky("app.bsky.actor.getProfile", actor=did)).get("followersCount"))
        except common.FetchError:
            followers = 0
        sys.stdout.write("f%s %s" % (followers, line if line.endswith("\n") else line + "\n"))
    return tally.exit_code("candidate")


if __name__ == "__main__":
    sys.exit(main())
