#!/usr/bin/env python3
"""Recent original posts from X accounts, read from the public syndication timeline (no login).

  xsynd.py <max_age_hours> handle1 handle2 ...
Output: handle id | age fav replies followers | text      (replies and reposts left out)
The endpoint is unofficial and can change; if it errors, fall back to reading
the account's profile page in the browser.
An account that failed is reported on stderr ("ERR handle: reason"). Exit 1 when every account failed.
"""
import datetime
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]
URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/"


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG MAX_AGE_HOURS HANDLE [HANDLE ...]")
    ap.add_argument("hours", type=float, metavar="MAX_AGE_HOURS")
    ap.add_argument("handles", nargs="+", metavar="HANDLE")
    return ap


def timeline_entries(page):
    """The timeline entries embedded in a syndication profile page. Raises ValueError when the page has
    none (rate limit, format change)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("no timeline data in the page (rate limit or a format change)")
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("the page's timeline data has an unexpected shape (a format change)")
    if not isinstance(entries, list):
        raise ValueError("the page's timeline data has an unexpected shape (a format change)")
    return entries


def rows_from_entries(entries, name, hours, now):
    """(favorites, handle, id, age_hours, replies, followers, text) for the account's own recent posts."""
    rows = []
    for e in entries:
        t = common.as_dict(common.as_dict(common.as_dict(e).get("content")).get("tweet"))
        if not t or t.get("in_reply_to_status_id_str") or t.get("retweeted_status"):
            continue
        user = common.as_dict(t.get("user"))
        if not common.same_handle(common.as_text(user.get("screen_name")), name):
            continue
        try:
            ts = datetime.datetime.strptime(common.as_text(t.get("created_at")), "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            continue
        age = (now - ts).total_seconds() / 3600
        if age > hours:
            continue
        text = (common.as_text(t.get("full_text")) or common.as_text(t.get("text"))).replace("\n", " ")
        rows.append((common.as_count(t.get("favorite_count")), name, common.as_text(t.get("id_str")) or "?", age,
                     common.as_count(t.get("reply_count")), common.as_count(user.get("followers_count")), text[:220]))
    return rows


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.hours <= 0:
        common.die("MAX_AGE_HOURS must be above 0", common.EXIT_USAGE)
    common.safe_stdio()
    now, rows, tally = common.now_utc(), [], common.Tally()
    for name in a.handles:
        name = name.strip().lstrip("@")
        if not name:
            continue
        try:
            entries = timeline_entries(common.fetch_text(URL + urllib.parse.quote(name, safe="")))
        except (common.FetchError, ValueError) as e:  # network, rate limit or format change
            tally.bad(name, e)
            continue
        tally.good()
        rows += rows_from_entries(entries, name, a.hours, now)
    rows.sort(reverse=True)
    for fav, name, i, age, rc, fol, text in rows:
        print(f"{name} {i} | {age:.1f}h fav{fav} r{rc} f{fol} | {text}")
    return tally.exit_code("account")


if __name__ == "__main__":
    sys.exit(main())
