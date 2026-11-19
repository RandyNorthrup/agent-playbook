#!/usr/bin/env python3
"""Fetch LinkedIn job details through the public guest API (no login).

Prints one summary line per job:
  job_id | title | company | location | employment type | seniority | applicants [| remote-in-description]
and writes the full descriptions as JSON to --out (default: details.json in the current folder).
A job that failed is reported on stderr ("ERR job_id: reason"). Exit 1 when every job failed; the output
file is then left as it was.
"""
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["network", "files"]
URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG JOB_ID [JOB_ID ...] [--out details.json]")
    ap.add_argument("jobs", nargs="+", metavar="JOB_ID")
    ap.add_argument("--out", default="details.json", metavar="FILE", help="where the JSON goes (default details.json)")
    return ap


def parse_job(page, jid):
    """The job's fields from a guest jobPosting page. Raises ValueError when the page holds no job
    (a login wall, a rate-limit page, a removed job)."""
    def field(rx):
        x = re.search(rx, page, re.S)
        return html.unescape(re.sub(r"\s+", " ", x.group(1)).strip()) if x else ""

    crit = dict(re.findall(r'description__job-criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', page, re.S))
    desc = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", page, re.S)
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc.group(1)))).strip() if desc else ""
    job = dict(id=jid, title=field(r'topcard__title">\s*(.*?)\s*<'), co=field(r"topcard__org-name-link[^>]*>\s*(.*?)\s*<"),
               loc=field(r'topcard__flavor topcard__flavor--bullet">\s*(.*?)\s*<'),
               applicants=field(r"num-applicants__caption[^>]*>\s*(.*?)\s*<"),
               criteria={html.unescape(k): html.unescape(v) for k, v in crit.items()}, description=text)
    if not job["title"] and not job["description"]:
        raise ValueError("no job in the reply (removed, rate-limited or a login page)")
    return job


def summary(job):
    remote = " | remote-in-description" if "remote" in job["description"].lower() else ""
    return " | ".join([job["id"], job["title"], job["co"], job["loc"], job["criteria"].get("Employment type", ""),
                       job["criteria"].get("Seniority level", ""), job["applicants"] + remote])


def main(argv=None):
    a = build_parser().parse_args(argv)
    for jid in a.jobs:
        if not jid.isdigit():
            common.die("a job id is all digits, got %r" % jid, common.EXIT_USAGE)
    common.safe_stdio()
    out, tally = [], common.Tally()
    for n, jid in enumerate(a.jobs):
        if n:
            time.sleep(0.7)
        try:
            job = parse_job(common.fetch_text(URL + jid), jid)
        except (common.FetchError, ValueError) as e:
            tally.bad(jid, e)
            continue
        tally.good()
        out.append(job)
        print(summary(job))
    if out:
        try:
            common.write_atomic(a.out, json.dumps(out, ensure_ascii=False, indent=1) + "\n")
        except OSError as e:
            common.die("can't write %s: %s" % (a.out, e.strerror or e))
    return tally.exit_code("job")


if __name__ == "__main__":
    sys.exit(main())
