#!/usr/bin/env python3
"""Search LinkedIn jobs through the public guest API (no login).

Defaults: remote (f_WT=2), Easy Apply (f_AL=true), posted in the last 7 days (f_TPR=r604800).
Extra params are appended and override nothing already set, e.g.:
  "f_JT=C&sortBy=R"          contract only, relevance sort (respects keywords)
  "f_TPR=r86400"             last 24 hours (use instead of the default window)
Location: set LI_LOCATION (default "United States") and LI_GEOID (default 103644278).
Output: job_id | title | company | location | posted | salary
Then: li_detail.py <job_id> ...
Exit 1 when the first request fails or LinkedIn answers with a login page instead of results. A later page
that fails ends the list with a note on stderr.
"""
import html
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network"]
URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"


def build_parser():
    ap = common.make_parser(__doc__, __file__, 'PROG "KEYWORDS" ["EXTRA=QUERY&PARAMS"]')
    ap.add_argument("keywords", metavar="KEYWORDS")
    ap.add_argument("extra", nargs="?", default="", metavar="EXTRA", help="more query params, as a=b&c=d")
    return ap


def search_url(keywords, extra, start):
    base = {"keywords": keywords, "location": os.environ.get("LI_LOCATION", "United States"),
            "geoId": os.environ.get("LI_GEOID", "103644278"), "f_WT": "2", "f_AL": "true"}
    if "f_TPR=" not in extra:
        base["f_TPR"] = "r604800"
    base["start"] = start
    return URL + urllib.parse.urlencode(base) + ("&" + extra if extra else "")


def parse_cards(page, seen=None):
    """[(job_id, title, company, location, posted, salary)] for the job cards on a results page, leaving
    out ids already in `seen` (which is updated)."""
    seen = set() if seen is None else seen
    rows = []
    for card in page.split("<li"):
        m = re.search(r"jobPosting:(\d+)", card)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))

        def field(rx):
            x = re.search(rx, card, re.S)
            return html.unescape(re.sub(r"\s+", " ", x.group(1)).strip()) if x else ""
        rows.append((m.group(1), field(r'base-search-card__title">\s*(.*?)\s*<'),
                     field(r'base-search-card__subtitle">.*?>\s*(.*?)\s*<'),
                     field(r'job-search-card__location">\s*(.*?)\s*<'), field(r'datetime="(.*?)"'),
                     field(r'job-search-card__salary-info">\s*(.*?)\s*<')))
    return rows


WALL = re.compile(r"authwall|/uas/login|<title>[^<]*(?:Sign Up|Log In|Sign In)[^<]*LinkedIn", re.I)


def login_wall(page):
    """True when LinkedIn answered with a sign-up or log-in page instead of results (rate-limited or blocked)."""
    return "jobPosting:" not in page and bool(WALL.search(page))


def main(argv=None):
    a = build_parser().parse_args(argv)
    if not a.keywords.strip():
        common.die("KEYWORDS is empty", common.EXIT_USAGE)
    extra = a.extra.strip().lstrip("&?")
    common.safe_stdio()
    seen, total = set(), 0
    for start in (0, 25, 50):
        if start:
            time.sleep(1)
        try:
            page = common.fetch_text(search_url(a.keywords, extra, start))
        except common.FetchError as e:
            if not start:
                common.die("search failed: %s" % e)
            common.warn("stopped after %d jobs: %s" % (total, e))
            break
        if login_wall(page):
            if not start:
                common.die("search failed: LinkedIn answered with a login page instead of results (rate-limited or "
                           "blocked); try again later")
            common.warn("stopped after %d jobs: LinkedIn answered with a login page" % total)
            break
        rows = parse_cards(page, seen)
        for row in rows:
            print(" | ".join(row))
        total += len(rows)
        if not rows:
            break
    if not total:
        common.warn("no jobs found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
