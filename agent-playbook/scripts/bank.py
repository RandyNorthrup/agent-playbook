#!/usr/bin/env python3
"""Post bank: check lengths and print compose links for the posts in bank.tsv.

bank.tsv lines: key<TAB>text<TAB>optional link   (# lines are comments)
The bank is --bank FILE, else $AGENT_PLAYBOOK_BANK, else playbook/state/bank.tsv in the skill folder.

  bank.py check          flag posts too long for X (280 weighted) or Bluesky (300)
  bank.py x <key>        X intent URL (link left out; put links in a reply)
  bank.py b <key>        Bluesky compose intent URL (link appended)
  bank.py text <key> [+] raw text; add any 3rd argument to append the link

Exit 1 when the bank can't be read or the key isn't in it.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]
BSKY_LIMIT = 300


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [--bank FILE] [check | x KEY | b KEY | text KEY [+]]")
    ap.add_argument("command", nargs="?", default="check", choices=["check", "x", "b", "text"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("plus", nargs="?", help="with text: any value appends the link")
    ap.add_argument("--bank", metavar="FILE", help="the bank file (default: $AGENT_PLAYBOOK_BANK, else playbook/state/bank.tsv)")
    return ap


def load(path):
    rows = {}
    for cols in common.read_tsv(path, 3):
        rows[cols[0]] = (cols[1], cols[2])
    return rows


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    common.safe_stdio()
    if a.command != "check" and not a.key:
        ap.error("%s needs a key" % a.command)
    path = common.state_file("bank.tsv", a.bank, "AGENT_PLAYBOOK_BANK")
    rows = load(path)
    if a.command == "check":
        for k, (t, link) in rows.items():
            x = common.x_weighted(t)
            b = len(t) + (len(link) + 2 if link else 0)
            flag = ("X! " if x > common.X_LIMIT else "") + ("B!" if b > BSKY_LIMIT else "")
            print(f"{k:14} x{x:3} b{b:3} {flag}")
        return 0
    if a.key not in rows:
        common.die("no key %r in %s (keys: %s)" % (a.key, path, ", ".join(rows) or "none"))
    t, link = rows[a.key]
    if a.command == "x":
        print("https://x.com/intent/post?text=" + urllib.parse.quote(t, safe=""))
    elif a.command == "b":
        print("https://bsky.app/intent/compose?text=" + urllib.parse.quote(t + ("\n\n" + link if link else ""), safe=""))
    else:
        print(t + ("\n\n" + link if link and a.plus else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
