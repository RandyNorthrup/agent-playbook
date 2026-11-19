#!/usr/bin/env python3
"""Setup check for agent-playbook: Python, Chrome, the clipboard and the public APIs. Run once per machine.

Checks Python, Chrome, a silent system-clipboard round-trip (the owner's clipboard is put back; optional,
since browser pastes go through the browser tool's own clipboard), and that the public APIs are
reachable (Bluesky, X syndication, LinkedIn guest). The gh CLI is optional.
Nothing here posts, logs in or changes settings, and no console windows are opened.
Prints READY (exit 0) or the required items to fix (exit 1).
"""
import os
import platform
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["clipboard", "network"]
NOTE = """
Browser control is checked by the agent, not this script:
  Proven: chrome-control MCP driving the owner's real, logged-in Chrome.
  Other tools must be able to drive the real logged-in profile (headed), read page text, give element
  refs, click by ref and by coordinates, press keys, take screenshots, upload to a file input,
  and paste from the system clipboard. The flows that use the browser are listed by
  python playbook/pb.py list.
"""


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [--no-clipboard] [--no-network]")
    ap.add_argument("--no-clipboard", action="store_true", help="skip the clipboard round-trip")
    ap.add_argument("--no-network", action="store_true", help="skip the API reachability checks")
    return ap


def reach(url):
    try:
        common.fetch(url, 15)
        return True, ""
    except common.FetchError as e:
        return False, str(e)


def clipboard_round_trip():
    import clip
    probe = "agent-playbook clipboard probe ✓ “quotes”"
    before = clip.get_clip()
    ok = clip.copy_verified(probe)
    if before:
        clip.set_clip(before)  # restore whatever the owner had copied
    return ok


def main(argv=None):
    a = build_parser().parse_args(argv)
    common.safe_stdio()
    results = []

    def check(name, ok, detail="", required=True):
        results.append((name, ok, required))
        mark = "OK  " if ok else ("FAIL" if required else "warn")
        print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")

    def skip(name):
        print(f"[skip] {name}")

    check("Python 3.8+", sys.version_info >= (3, 8), platform.python_version())
    chrome = common.find_chrome()
    check("Google Chrome (owner's browser + carousel rendering)", bool(chrome), chrome or "not found")

    name = "Clipboard round-trip (UTF-8, silent)"
    if a.no_clipboard:
        skip(name)
    else:
        try:
            check(name, clipboard_round_trip(), "optional; only clip.py and clipimage.py use it", required=False)
        except Exception as e:  # any failure here is the answer to the check
            check(name, False, f"optional; {e} (Linux: install wl-clipboard or xclip)", required=False)

    apis = [("Bluesky public API", "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=bsky.app", "", True),
            ("X syndication (thread context)", "https://cdn.syndication.twimg.com/tweet-result?id=20&token=a",
             "optional; falls back to the browser", False),
            ("LinkedIn guest jobs API",
             "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=developer",
             "optional; only li_search.py and li_detail.py use it", False)]
    for name, url, detail, required in apis:
        if a.no_network:
            skip(name)
            continue
        ok, why = reach(url)
        check(name, ok, detail if ok or not why else (detail + "; " if detail else "") + why, required)
    check("gh CLI (read-only README lookups)", bool(shutil.which("gh")), "optional", required=False)

    print(NOTE)
    failed = [n for n, ok, req in results if req and not ok]
    print("READY" if not failed else "Fix required items: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
