"""Versions and the change journal.

history/<col>/<id>/<stamp>.json   the content a save replaced, one file per change of content
history/changes.jsonl             who changed what, when and why (one JSON object per line, append-only)

A version's name is local time to the microsecond plus the first 8 characters of its revision, so two saves
in the same second can never share a file. A version is skipped only when the newest one already holds the
same content. The functions here are called by Store with its lock held.
"""
import datetime as dt
import os

from . import ids, schema, textio

KEEP_VERSIONS = 300


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp_name(rev):
    tail = "".join(c for c in str(rev or "") if c in "0123456789abcdef")[-8:].rjust(8, "0")
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + tail


def _folder(store, col, doc_id):
    return store.history / col / doc_id


def versions(store, col, doc_id):
    """Version names, newest first. Empty for an unknown collection or id."""
    if col not in ids.COLS or not ids.valid_id(doc_id):
        return []
    d = _folder(store, col, doc_id)
    try:
        names = os.listdir(str(d))
    except OSError:
        return []
    return sorted((n[:-5] for n in names if n.endswith(".json") and ids.valid_version(n[:-5])), reverse=True)


def keep_version(store, col, doc_id, doc, raw, rev):
    """Keep the content that is about to be replaced. Returns the version name, or None when the newest
    version already holds exactly this content."""
    have = versions(store, col, doc_id)
    tail = stamp_name(rev)[-8:]
    if have and have[0].endswith("-" + tail) and len(have[0]) > 15:
        try:
            if doc is not None and textio.loads(textio.read_bytes(_folder(store, col, doc_id) / (have[0] + ".json"))) == doc:
                return None
        except (OSError, ValueError):
            pass
    name = stamp_name(rev)
    data = raw
    if doc is not None:
        try:
            data = schema.canonical_bytes(col, doc)
        except ValueError:
            data = raw
    textio.atomic_write(_folder(store, col, doc_id) / (name + ".json"), data)
    for old in have[KEEP_VERSIONS - 1:]:
        try:
            os.unlink(str(_folder(store, col, doc_id) / (old + ".json")))
        except OSError:
            pass
    return name


def read_version(store, col, doc_id, version):
    """The document saved as `version`. Raises LookupError when there is no such version, ValueError when unreadable."""
    if col not in ids.COLS or not ids.valid_id(doc_id) or not ids.valid_version(version):
        raise LookupError("no such version")
    try:
        raw = textio.read_bytes(_folder(store, col, doc_id) / (version + ".json"))
    except FileNotFoundError:
        raise LookupError("no such version")
    return textio.loads(raw)


def journal(store, col, doc_id, action, by, title, reason="", removed=None, rev=None, prev=None):
    rec = {"at": now_iso(), "col": col, "id": doc_id, "action": action, "by": by, "title": title}
    if rev:
        rec["rev"] = rev
    if prev:
        rec["prev"] = prev
    if removed:
        rec["removed"] = list(removed)
    if reason:
        rec["reason"] = reason
    textio.append_lines(store.history / "changes.jsonl", [textio.dumps(rec)])


def read_changes(store, limit=400):
    """The latest changes, newest first. Lines that are not JSON objects are skipped."""
    try:
        text = textio.read_bytes(store.history / "changes.jsonl").decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    rows = []
    for line in textio.split_lines(text)[-max(int(limit), 0):] if limit else []:
        try:
            r = textio.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("col"), str) and isinstance(r.get("id"), str):
            r.setdefault("at", "")
            r.setdefault("action", "edit")
            r.setdefault("by", "")
            rows.append(r)
    return rows[::-1]
