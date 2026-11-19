"""The playbook's schema: what a flow, node, edge, account, folder and part may contain.

Strict on purpose. A key that is not listed here is an error, so a typo cannot hide as data and two tools cannot
drift apart. `validate()` never raises: it returns findings as (file, path, message) tuples, and an empty list
means the document is valid.

The canvas page carries the same lists (kinds, groups, icons, platforms, statuses, folder kinds, reserved
words); tests/test_page.py fails when the two disagree.
"""
import copy
import datetime as dt
import hashlib
import re

from . import ids, textio

KINDS = ("start", "step", "decision", "check", "rule", "tool", "gotcha", "output", "goto", "account")
SIDE_KINDS = ("rule", "tool", "gotcha")  # notes that hang off a step rather than sit on the main path
GROUPS = ("Start here", "Rules & QC", "Platforms", "Video", "Art & thumbnails", "Projects & promo", "Jobs & inbox")
FLOW_ICONS = ("map", "loop", "shield", "check", "browser", "spam", "x", "bluesky", "threads", "linkedin", "facebook",
              "instagram", "reddit", "article", "rocket", "reply", "video", "youtube", "tiktok", "music", "camera", "mic",
              "palette", "image", "thumb", "layers", "banner", "megaphone", "box", "briefcase", "mask", "puzzle", "chat",
              "glasses", "window", "gamepad", "shirt", "list", "clock", "folder", "library", "sparkle")
PLATFORMS = ("X", "Bluesky", "Threads", "LinkedIn", "Facebook", "Facebook group", "Instagram", "TikTok", "YouTube",
             "Reddit", "DEV", "Medium", "Hashnode", "GitHub", "Hacker News", "Product Hunt", "Contra", "Upwork", "Gmail",
             "other")
STATUSES = ("active", "replies-only", "paused", "retired", "skip")
FOLDER_KINDS = ("music", "images", "video", "scripts", "docs", "playbook", "other")
EDGE_TYPES = ("flow", "attach")
GOTO_OVERVIEW = "map"  # a goto may point at the overview page instead of a flow

MAX = {"name": 60, "summary": 300, "origin": 4000, "title": 80, "sub": 120, "body": 1200, "check": 200, "checks": 12,
       "folders": 8, "links": 6, "link_label": 80, "url": 500, "src": 12, "src_item": 300, "label": 24, "nodes": 60,
       "edges": 150, "order": 999, "coord": 20000, "grid": 10, "handle": 60, "owner": 40, "purpose": 300, "pacing": 200,
       "login": 200, "projects": 10, "notes": 20, "note": 300, "folder_label": 60, "path": 260, "folder_note": 200}

# the order keys are written in; also the complete list of keys each object may have
KEYS = {
    "flow": ("name", "group", "icon", "summary", "order", "origin", "copiedFrom", "editedAt", "updatedAt", "nodes", "edges"),
    "node": ("id", "kind", "part", "title", "sub", "body", "checks", "folders", "links", "goto", "account", "src", "origin",
             "x", "y", "editedAt"),
    "edge": ("id", "from", "to", "type", "label"),
    "link": ("label", "url"),
    "account": ("platform", "handle", "label", "owner", "status", "flow", "purpose", "pacing", "login", "url", "projects",
                "notes", "src", "origin", "editedAt", "updatedAt"),
    "folder": ("label", "path", "kind", "note", "editedAt", "updatedAt"),
    "part": ("kind", "title", "sub", "body", "checks", "folders", "links", "goto", "account", "src", "origin", "editedAt",
             "updatedAt"),
}
# a node that references a part takes these from the part, so it may not carry its own
FROM_PART = ("kind", "title", "sub", "body", "checks", "goto", "account")
_EMPTY_LISTS = ("checks", "folders", "links", "src")
_EMPTY_TEXT = ("sub", "body")

TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d)(?:\.\d{1,6})?)?(?:Z|[+-](\d\d):(\d\d))$")
URL_RE = re.compile(r"^https?://\S+$")


def real_stamp(value):
    """Whether `value` is a timestamp like 2026-11-09T14:02:00Z that names a real moment (no month 13, no 25:00)."""
    m = TS_RE.match(value) if isinstance(value, str) else None
    if not m:
        return False
    y, mo, d, hh, mm, ss, oh, om = [int(g) if g else 0 for g in m.groups()]
    try:
        dt.date(y, mo, d)
    except ValueError:
        return False
    return hh < 24 and mm < 60 and ss < 60 and oh < 24 and om < 60


def _tname(v):
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "a list"
    if isinstance(v, dict):
        return "an object"
    return "null" if v is None else type(v).__name__


def _join(path, key):
    return "%s/%s" % (path, key) if path else str(key)


class _Check(object):
    def __init__(self, file):
        self.file = file
        self.out = []

    def add(self, path, message):
        self.out.append((self.file, path or "(document)", message))

    def keys(self, obj, kind, path):
        allowed = KEYS[kind]
        for k in obj:
            if k not in allowed:
                self.add(path, "unknown key %r" % (k,))

    def text(self, obj, key, path, hi, lo=0, required=False, line=False, trim=False, clean=False):
        if key not in obj:
            if required:
                self.add(path, "missing %r" % key)
            return None
        v, p = obj[key], _join(path, key)
        if not isinstance(v, str):
            self.add(p, "must be text, not %s" % _tname(v))
            return None
        if len(v) < lo:
            self.add(p, "is empty" if lo == 1 else "is shorter than %d characters" % lo)
        if len(v) > hi:
            self.add(p, "is %d characters; the limit is %d" % (len(v), hi))
        if line and ("\n" in v or "\r" in v):
            self.add(p, "must be a single line")
        if trim and v != v.strip():
            self.add(p, "starts or ends with a space")
        if clean and ("\r" in v or "\t" in v):
            self.add(p, "contains a tab or a carriage return")
        bad = [ch for ch in v if (ord(ch) < 32 and ch not in "\n\r\t") or ord(ch) == 127]
        if bad:  # a shell turning "\b" or "\a" in a path into a control character
            self.add(p, "contains a control character (%r); a backslash sequence was probably read as an escape" % bad[0])
        return v

    def enum(self, obj, key, path, allowed, required=False):
        if key not in obj:
            if required:
                self.add(path, "missing %r" % key)
            return None
        v = obj[key]
        if not isinstance(v, str) or v not in allowed:
            self.add(_join(path, key), "%r is not one of: %s" % (v, ", ".join(allowed)))
            return None
        return v

    def integer(self, obj, key, path, lo, hi, required=False, step=1):
        if key not in obj:
            if required:
                self.add(path, "missing %r" % key)
            return None
        v, p = obj[key], _join(path, key)
        if isinstance(v, bool) or not isinstance(v, int):
            self.add(p, "must be a whole number, not %s" % _tname(v))
            return None
        if v < lo or v > hi:
            self.add(p, "%d is outside %d to %d" % (v, lo, hi))
        elif step > 1 and v % step:
            self.add(p, "%d is not a multiple of %d" % (v, step))
        return v

    def stamp(self, obj, key, path):
        if key in obj and not real_stamp(obj[key]):
            self.add(_join(path, key), "must be a timestamp like 2026-11-09T14:02:00Z")

    def ref(self, obj, key, path, col=None, required=False, also=()):
        if key not in obj:
            if required:
                self.add(path, "missing %r" % key)
            return None
        v = obj[key]
        if isinstance(v, str) and v in also:
            return v
        why = ids.id_problem(v, col)
        if why:
            self.add(_join(path, key), why)
            return None
        return v

    def items(self, obj, key, path, hi, required=False, lo=0):
        """The list under `key` (None when absent or not a list), with its length checked."""
        if key not in obj:
            if required:
                self.add(path, "missing %r" % key)
            return None
        v, p = obj[key], _join(path, key)
        if not isinstance(v, list):
            self.add(p, "must be a list, not %s" % _tname(v))
            return None
        if len(v) > hi:
            self.add(p, "has %d items; the limit is %d" % (len(v), hi))
        if len(v) < lo:
            self.add(p, "is empty" if lo == 1 else "has fewer than %d items" % lo)
        return v

    def texts(self, obj, key, path, hi, item_hi, line=True):
        v = self.items(obj, key, path, hi)
        for i, s in enumerate(v or []):
            p = "%s[%d]" % (_join(path, key), i)
            if not isinstance(s, str):
                self.add(p, "must be text, not %s" % _tname(s))
            elif not s.strip():
                self.add(p, "is empty")
            elif len(s) > item_hi:
                self.add(p, "is %d characters; the limit is %d" % (len(s), item_hi))
            elif line and ("\n" in s or "\r" in s):
                self.add(p, "must be a single line")

    def id_list(self, obj, key, path, hi, col=None):
        v = self.items(obj, key, path, hi)
        seen = set()
        for i, s in enumerate(v or []):
            p = "%s[%d]" % (_join(path, key), i)
            why = ids.id_problem(s, col)
            if why:
                self.add(p, why if isinstance(s, str) else "must be an id (text), not %s" % _tname(s))
            elif s in seen:
                self.add(p, "%r is listed twice" % s)
            else:
                seen.add(s)

    def links(self, obj, path):
        v = self.items(obj, "links", path, MAX["links"])
        for i, l in enumerate(v or []):
            p = "%s/links[%d]" % (path, i) if path else "links[%d]" % i
            if not isinstance(l, dict):
                self.add(p, "must be an object with a label and a url, not %s" % _tname(l))
                continue
            self.keys(l, "link", p)
            self.text(l, "label", p, MAX["link_label"], lo=1, required=True, line=True)
            u = self.text(l, "url", p, MAX["url"], required=True, line=True)
            if u is not None and not URL_RE.match(u):
                self.add(p + "/url", "%r is not an http(s) link" % u)


def _content(c, obj, path):
    """The fields a node and a part share: kind, text, checklist, references."""
    kind = c.enum(obj, "kind", path, KINDS, required=True)
    c.text(obj, "title", path, MAX["title"], lo=1, required=True, line=True, trim=True)
    c.text(obj, "sub", path, MAX["sub"], line=True)
    c.text(obj, "body", path, MAX["body"], clean=True)
    c.texts(obj, "checks", path, MAX["checks"], MAX["check"])
    if kind == "goto":
        c.ref(obj, "goto", path, "flows", required=True, also=(GOTO_OVERVIEW,))
    elif "goto" in obj:
        c.add(_join(path, "goto"), "only a goto node has a target flow")
    if kind == "account":
        c.ref(obj, "account", path, "accounts", required=True)
    elif "account" in obj:
        c.add(_join(path, "account"), "only an account node names an account")


def _extras(c, obj, path):
    c.id_list(obj, "folders", path, MAX["folders"], "folders")
    c.links(obj, path)
    c.texts(obj, "src", path, MAX["src"], MAX["src_item"])
    c.text(obj, "origin", path, MAX["origin"])
    c.stamp(obj, "editedAt", path)


def _node(c, n, path):
    c.keys(n, "node", path)
    if "part" in n:
        c.ref(n, "part", path, "parts")
        for k in FROM_PART:
            if k in n:
                c.add(_join(path, k), "comes from the part %r; remove it here, or detach the part" % (n.get("part"),))
    else:
        _content(c, n, path)
    _extras(c, n, path)
    c.integer(n, "x", path, -MAX["coord"], MAX["coord"], required=True, step=MAX["grid"])
    c.integer(n, "y", path, -MAX["coord"], MAX["coord"], required=True, step=MAX["grid"])


def _flow(c, d):
    c.keys(d, "flow", "")
    c.text(d, "name", "", MAX["name"], lo=1, required=True, line=True, trim=True)
    c.enum(d, "group", "", GROUPS, required=True)
    c.enum(d, "icon", "", FLOW_ICONS, required=True)
    c.text(d, "summary", "", MAX["summary"], required=True, line=True)
    c.integer(d, "order", "", 0, MAX["order"], required=True)
    c.text(d, "origin", "", MAX["origin"])
    c.ref(d, "copiedFrom", "", None)
    c.stamp(d, "editedAt", "")
    c.stamp(d, "updatedAt", "")
    node_ids = set()
    for i, n in enumerate(c.items(d, "nodes", "", MAX["nodes"], required=True, lo=1) or []):
        if not isinstance(n, dict):
            c.add("nodes[%d]" % i, "must be an object, not %s" % _tname(n))
            continue
        nid = n.get("id")
        why = ids.id_problem(nid) if "id" in n else "missing 'id'"
        if why:
            c.add("nodes[%d]" % i, why)
            path = "nodes[%d]" % i
        else:
            path = "nodes/%s" % nid
            if nid in node_ids:
                c.add(path, "the id %r is used by two nodes" % nid)
            node_ids.add(nid)
        _node(c, n, path)
    pairs = set()
    for i, e in enumerate(c.items(d, "edges", "", MAX["edges"], required=True) or []):
        if not isinstance(e, dict):
            c.add("edges[%d]" % i, "must be an object, not %s" % _tname(e))
            continue
        a, b = e.get("from"), e.get("to")
        path = "edges/%s>%s" % (a, b) if isinstance(a, str) and isinstance(b, str) else "edges[%d]" % i
        c.keys(e, "edge", path)
        ok = True
        for k, v in (("from", a), ("to", b)):
            if k not in e:
                c.add(path, "missing %r" % k)
                ok = False
            elif not isinstance(v, str) or v not in node_ids:
                c.add(path, "%r points at %r, which is not a node in this flow" % (k, v))
                ok = False
        if ok:
            if a == b:
                c.add(path, "connects a node to itself")
            if (a, b) in pairs:
                c.add(path, "the same connection is listed twice")
            pairs.add((a, b))
            if "id" in e and e["id"] != "%s>%s" % (a, b):
                c.add(path + "/id", "must be %r (or be left out)" % ("%s>%s" % (a, b)))
        c.enum(e, "type", path, EDGE_TYPES, required=True)
        c.text(e, "label", path, MAX["label"], line=True)


def _account(c, d):
    c.keys(d, "account", "")
    c.enum(d, "platform", "", PLATFORMS, required=True)
    c.enum(d, "status", "", STATUSES, required=True)
    h = c.text(d, "handle", "", MAX["handle"], line=True)
    l = c.text(d, "label", "", MAX["handle"], line=True)
    if not (h or "").strip() and not (l or "").strip():
        c.add("", "needs a handle or a label")
    c.text(d, "owner", "", MAX["owner"], line=True)
    c.text(d, "purpose", "", MAX["purpose"])
    c.text(d, "pacing", "", MAX["pacing"])
    c.text(d, "login", "", MAX["login"])
    if d.get("flow") != "":
        c.ref(d, "flow", "", "flows")
    u = c.text(d, "url", "", MAX["url"], line=True)
    if u and not URL_RE.match(u):
        c.add("url", "%r is not an http(s) link" % u)
    c.id_list(d, "projects", "", MAX["projects"], "flows")
    c.texts(d, "notes", "", MAX["notes"], MAX["note"])
    c.texts(d, "src", "", MAX["src"], MAX["src_item"])
    c.text(d, "origin", "", MAX["origin"])
    c.stamp(d, "editedAt", "")
    c.stamp(d, "updatedAt", "")


def _folder(c, d):
    c.keys(d, "folder", "")
    c.text(d, "label", "", MAX["folder_label"], lo=1, required=True, line=True, trim=True)
    c.text(d, "path", "", MAX["path"], lo=1, required=True, line=True, trim=True)
    c.enum(d, "kind", "", FOLDER_KINDS, required=True)
    c.text(d, "note", "", MAX["folder_note"])
    c.stamp(d, "editedAt", "")
    c.stamp(d, "updatedAt", "")


def _part(c, d):
    c.keys(d, "part", "")
    _content(c, d, "")
    _extras(c, d, "")
    c.stamp(d, "updatedAt", "")


_BY_COL = {"flows": _flow, "accounts": _account, "folders": _folder, "parts": _part}


def file_of(col, doc_id):
    return "store/%s/%s.json" % (col, doc_id)


def validate(col, doc_id, doc, file=None):
    """Every way `doc` breaks the schema, as (file, path, message). An empty list means it is valid. Never raises."""
    c = _Check(file or file_of(col, doc_id))
    try:
        if col not in _BY_COL:
            c.add("", "%r is not a collection (%s)" % (col, ", ".join(ids.COLS)))
            return c.out
        why = ids.id_problem(doc_id, col)
        if why:
            c.add("(file name)", why)
        if not isinstance(doc, dict):
            c.add("", "must be an object, not %s" % _tname(doc))
            return c.out
        _BY_COL[col](c, doc)
    except Exception as e:  # a validator must never take the caller down
        c.add("", "could not be checked (%s: %s)" % (type(e).__name__, e))
    return c.out


# ---------------------------------------------------------------- canonical form

def _ordered(obj, kind):
    order = KEYS[kind]
    out = {}
    for k in order:
        if k in obj:
            out[k] = obj[k]
    for k in sorted(k for k in obj if k not in order):
        out[k] = obj[k]
    return out


def _tidy(obj):
    for k in _EMPTY_LISTS:
        if obj.get(k) == []:
            del obj[k]
    for k in _EMPTY_TEXT:
        if obj.get(k) == "":
            del obj[k]
    if isinstance(obj.get("links"), list):
        obj["links"] = [_ordered(l, "link") if isinstance(l, dict) else l for l in obj["links"]]


def canonical(col, doc):
    """The one stored shape of a document: keys in a fixed order, and nothing that says nothing
    (an empty checklist, an empty summary line, an empty edge label, an edge id that only repeats from>to)."""
    if not isinstance(doc, dict):
        return doc
    d = copy.deepcopy(doc)
    if col == "flows":
        nodes = []
        for n in d.get("nodes") if isinstance(d.get("nodes"), list) else []:
            if isinstance(n, dict):
                _tidy(n)
                n = _ordered(n, "node")
            nodes.append(n)
        edges = []
        for e in d.get("edges") if isinstance(d.get("edges"), list) else []:
            if isinstance(e, dict):
                if e.get("id") == "%s>%s" % (e.get("from"), e.get("to")):
                    del e["id"]
                if e.get("label") == "":
                    del e["label"]
                e = _ordered(e, "edge")
            edges.append(e)
        if isinstance(d.get("nodes"), list):
            d["nodes"] = nodes
        if isinstance(d.get("edges"), list):
            d["edges"] = edges
        return _ordered(d, "flow")
    if col == "parts":
        _tidy(d)
        return _ordered(d, "part")
    if col == "accounts":
        return _ordered(d, "account")
    if col == "folders":
        return _ordered(d, "folder")
    return d


def canonical_bytes(col, doc):
    """The exact bytes a document is stored as: UTF-8, LF, one-space indent, a final newline. ValueError on NaN."""
    return (textio.dumps(canonical(col, doc), indent=1) + "\n").encode("utf-8")


def revision(col, doc):
    """A short fingerprint of a document's content. Two saves with the same content have the same revision."""
    return hashlib.sha256(canonical_bytes(col, doc)).hexdigest()[:16]


def upgrade(col, doc):
    """Bring a document written before the schema existed up to it without changing what it says:
    a connection with no type is a main-path connection, and edge ids are derived, not stored."""
    d = copy.deepcopy(doc)
    if col == "flows" and isinstance(d, dict) and isinstance(d.get("edges"), list):
        for e in d["edges"]:
            if isinstance(e, dict):
                e.setdefault("type", "flow")
                e.pop("id", None)
    return d


# ---------------------------------------------------------------- parts by reference

def resolve_node(node, parts):
    """A node as it reads. One that references a part takes the part's kind, title, summary, details, checklist
    (and goto or account); its own folders and links are added to the part's. Returns (node, missing_part_id)."""
    if not isinstance(node, dict) or "part" not in node:
        return node, None
    pid = node.get("part")
    part = parts.get(pid) if isinstance(pid, str) else None
    if not isinstance(part, dict):
        out = dict(node)
        out.setdefault("kind", "step")
        out.setdefault("title", "(missing part %s)" % (pid,))
        return out, pid
    out = dict(node)
    for k in FROM_PART:
        if k in part:
            out[k] = copy.deepcopy(part[k])
    for k in ("folders", "links"):
        merged = []
        for item in list(part.get(k) or []) + list(node.get(k) or []):
            if item not in merged:
                merged.append(item)
        if merged:
            out[k] = merged
    return out, None


def resolve_flow(flow, parts):
    """A copy of the flow with every part reference filled in (for reading; never store this)."""
    if not isinstance(flow, dict):
        return flow
    out = dict(flow)
    out["nodes"] = [resolve_node(n, parts)[0] for n in flow.get("nodes") or [] if isinstance(n, dict)]
    return out
