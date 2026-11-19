"""The one id rule for the playbook. The server, the command-line tool and the linter all use it.

An id is lower-case words joined by single hyphens: `x`, `promo-post`, `part-read-back`. It is also the file
name (`<id>.json`), so nothing that could leave the collection folder or confuse a file system is allowed.
"""
import re

COLS = ("flows", "accounts", "folders", "parts")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_ID = 64
# words the canvas uses as page routes: a flow with one of these ids could never be opened
RESERVED = ("home", "accounts", "folders", "parts", "history", "settings", "sidebar", "customize", "upgrade",
            "downloads", "map")
# names Windows treats as devices whatever the extension
DEVICES = frozenset(["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] + ["lpt%d" % i for i in range(1, 10)])
VERSION_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{6}-[0-9a-f]{8})?$")


def id_problem(doc_id, col=None):
    """Why `doc_id` is not a valid id, or None when it is fine. Never raises."""
    if not isinstance(doc_id, str):
        return "an id must be text"
    if not doc_id:
        return "the id is empty"
    if len(doc_id) > MAX_ID:
        return "the id is %d characters; the limit is %d" % (len(doc_id), MAX_ID)
    if not ID_RE.match(doc_id):
        return "the id %r must be lower-case letters and digits joined by single hyphens" % doc_id
    if doc_id in DEVICES:
        return "the id %r is a reserved device name on Windows" % doc_id
    if col == "flows" and doc_id in RESERVED:
        return "the id %r is a page name on the canvas" % doc_id
    if col == "parts" and not doc_id.startswith("part-"):
        return "a part id starts with 'part-'"
    return None


def valid_id(doc_id, col=None):
    return id_problem(doc_id, col) is None


def valid_version(name):
    return isinstance(name, str) and bool(VERSION_RE.match(name))
