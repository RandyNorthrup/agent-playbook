"""The playbook's documents on disk: one JSON file per flow, account, folder and part under store/.

Every write goes through `Store.write()` or `Store.delete()`:
  - the id is checked and the path is proven to stay inside its collection folder
  - the document is checked against the schema and refused if it breaks it
  - the caller's base revision is compared with what is on disk, so a stale save is refused, not merged over
  - the version being replaced is kept in history/, and the change is journalled
  - the file is written in canonical bytes, to a private temp file, and swapped in whole
all under one lock shared by the server and the command-line tool.
"""
import hashlib
import os
import pathlib

from . import history, ids, schema, textio

COLS = ids.COLS
NEW = "new"  # as a base revision: "I believe this document does not exist yet"


class StoreError(Exception):
    """Something the caller should be told in plain words."""


class BadId(StoreError):
    pass


class NotFound(StoreError):
    pass


class Unreadable(StoreError):
    pass


class Invalid(StoreError):
    """The document breaks the schema. `findings` is the list of (file, path, message)."""

    def __init__(self, findings):
        self.findings = list(findings)
        first = "; ".join("%s: %s" % (p, m) for _, p, m in self.findings[:3])
        more = " (and %d more)" % (len(self.findings) - 3) if len(self.findings) > 3 else ""
        StoreError.__init__(self, "the document is not valid: " + first + more)


class Stale(StoreError):
    """The document changed since the caller read it. `current` is what is stored now (None when it is gone)."""

    def __init__(self, col, doc_id, base, current, rev):
        self.current, self.rev, self.base = current, rev, base
        if current is None:
            msg = "%s/%s no longer exists (it was deleted after you read it)" % (col, doc_id)
        elif base == NEW:
            msg = "%s/%s already exists (revision %s)" % (col, doc_id, rev)
        else:
            msg = "%s/%s changed since revision %s; it is now %s. Read it again and redo the change" % (col, doc_id, base, rev)
        StoreError.__init__(self, msg)


class ReasonRequired(ValueError):
    """Deleting anything (a whole document, or steps or connections from a flow) needs a reason."""

    def __init__(self, removed=()):
        self.removed = list(removed)
        ValueError.__init__(self, "a reason is required to delete" + (": " + ", ".join(self.removed) if self.removed else ""))


def clean_reason(reason):
    return " ".join(str(reason or "").split())[:500]


def title_of(doc):
    d = doc if isinstance(doc, dict) else {}
    for k in ("name", "title", "handle", "label"):
        if isinstance(d.get(k), str) and d[k]:
            return d[k]
    return ""


def removed_items(old, new):
    """Steps and connections that are in the saved flow but missing from the new version.
    A connection counts as removed when both its steps remain but nothing links them any more
    (reversing one keeps the link; connections that go with a deleted step are covered by that step)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    old_nodes = [n for n in old.get("nodes") or [] if isinstance(n, dict)] if isinstance(old.get("nodes"), list) else []
    title = {}
    for n in old_nodes:
        nid = n.get("id")
        if isinstance(nid, str):
            t = n.get("title") or (("part " + n["part"]) if isinstance(n.get("part"), str) else "") or nid
            title[nid] = t if isinstance(t, str) else nid
    new_nodes = new.get("nodes") if isinstance(new.get("nodes"), list) else []
    keep = set(n.get("id") for n in new_nodes if isinstance(n, dict) and isinstance(n.get("id"), str))
    gone = [title[n["id"]] for n in old_nodes if isinstance(n.get("id"), str) and n["id"] not in keep]
    new_edges = new.get("edges") if isinstance(new.get("edges"), list) else []
    linked = set()
    for e in new_edges:
        if isinstance(e, dict) and isinstance(e.get("from"), str) and isinstance(e.get("to"), str):
            linked.add(frozenset((e["from"], e["to"])))
    for e in old.get("edges") if isinstance(old.get("edges"), list) else []:
        if not isinstance(e, dict):
            continue
        a, b = e.get("from"), e.get("to")
        if isinstance(a, str) and isinstance(b, str) and a in title and b in title and a in keep and b in keep \
                and frozenset((a, b)) not in linked:
            gone.append("connection %s → %s" % (title[a], title[b]))
    return gone


class Store(object):
    """The documents under `<root>/store`, with their history in `<root>/history`. `root` is the playbook folder."""

    lock_timeout = 20.0  # seconds to wait for another writer before giving up with textio.LockTimeout

    def __init__(self, root):
        self.root = pathlib.Path(str(root)).resolve()
        self.dir = self.root / "store"
        self.trash = self.dir / ".trash"
        self.history = self.root / "history"
        self.lock_path = self.history / ".lock"

    def lock(self):
        return textio.FileLock(self.lock_path, self.lock_timeout)

    def sweep_temp(self, older_than=3600.0):
        """Remove the temp files a killed writer left behind in the store and its history. Returns how many."""
        folders = [self.dir / col for col in COLS]
        for col in COLS:
            try:
                folders += [self.history / col / n for n in os.listdir(str(self.history / col))]
            except OSError:
                pass
        return sum(len(textio.sweep_temp(f, older_than)) for f in folders)

    # ---------------------------------------------------------------- paths and reads

    def path(self, col, doc_id):
        """Where a document lives. Raises BadId unless the id is valid and the path stays inside its collection."""
        if col not in COLS:
            raise BadId("%r is not a collection (%s)" % (col, ", ".join(COLS)))
        why = ids.id_problem(doc_id, col)
        if why:
            raise BadId(why)
        folder = (self.dir / col).resolve()
        p = (folder / (doc_id + ".json")).resolve()
        if p.parent != folder:
            raise BadId("the id %r leaves the %s folder" % (doc_id, col))
        return p

    def files(self, col):
        """(id, path) for every document file in a collection, sorted. Hidden and temp files are skipped."""
        d = self.dir / col
        try:
            names = sorted(os.listdir(str(d)))
        except OSError:
            return []
        return [(n[:-5], d / n) for n in names if n.endswith(".json") and not n.startswith(".")]

    def _parse(self, col, doc_id, raw):
        try:
            doc = textio.loads(raw)
        except ValueError as e:
            raise Unreadable("%s: %s" % (schema.file_of(col, doc_id), e))
        return doc

    def _current(self, col, doc_id, path):
        """(doc, revision, raw bytes) of what is on disk, or (None, None, None). An unreadable file has doc None
        and a revision made from its bytes, so it can still be replaced on purpose."""
        try:
            raw = textio.read_bytes(path)
        except FileNotFoundError:
            return None, None, None
        try:
            doc = textio.loads(raw)
            return doc, schema.revision(col, doc), raw
        except ValueError:
            return None, "raw-" + hashlib.sha256(raw).hexdigest()[:12], raw

    def read(self, col, doc_id):
        """(document, revision). NotFound when missing, Unreadable when it is not valid JSON."""
        path = self.path(col, doc_id)
        try:
            raw = textio.read_bytes(path)
        except FileNotFoundError:
            raise NotFound("no %s/%s" % (col, doc_id))
        doc = self._parse(col, doc_id, raw)
        return doc, schema.revision(col, doc)

    def exists(self, col, doc_id):
        try:
            return self.path(col, doc_id).is_file()
        except BadId:
            return False

    def load_all(self):
        """Everything readable, plus what is not: (data, revisions, errors).
        data[col][id] is the document, revisions[col][id] its revision, errors a list of (file, path, message)
        for files that could not be read or whose name is not a valid id. Never raises on a bad file."""
        data, revs, errors = {}, {}, []
        for col in COLS:
            data[col], revs[col] = {}, {}
            for doc_id, path in self.files(col):
                file = schema.file_of(col, doc_id)
                why = ids.id_problem(doc_id, col)
                if why:
                    errors.append((file, "(file name)", why))
                    continue
                try:
                    doc = textio.loads(textio.read_bytes(path))
                    rev = schema.revision(col, doc)
                except FileNotFoundError:
                    continue  # deleted while we were listing
                except (ValueError, OSError) as e:
                    errors.append((file, "(document)", "cannot be read: %s" % e))
                    continue
                data[col][doc_id], revs[col][doc_id] = doc, rev
        return data, revs, errors

    def load(self, col):
        return self.load_all()[0][col]

    def stamp(self):
        """Changes whenever any stored file is added, removed or rewritten, so a reader knows to look again."""
        h = hashlib.sha1()
        for col in COLS:
            for doc_id, path in self.files(col):
                try:
                    st = os.stat(str(path))
                except OSError:
                    continue
                h.update(("%s/%s:%d:%d;" % (col, doc_id, st.st_size, st.st_mtime_ns)).encode("utf-8"))
        return h.hexdigest()[:16]

    # ---------------------------------------------------------------- writes

    def _check_base(self, col, doc_id, base, cur_doc, cur_rev):
        if base is None:
            return
        if base == NEW:
            if cur_rev is not None:
                raise Stale(col, doc_id, base, cur_doc, cur_rev)
        elif base != cur_rev:
            raise Stale(col, doc_id, base, cur_doc, cur_rev)

    def write(self, col, doc_id, doc, by="canvas", reason="", base=None):
        """Save a document. Returns {"rev", "changed", "created", "removed"}.

        base: the revision the caller started from (or NEW); a mismatch raises Stale and nothing is written.
        Raises Invalid when the document breaks the schema, ReasonRequired when it drops steps or connections
        from a flow and no reason is given, BadId for a bad collection or id."""
        path = self.path(col, doc_id)
        reason = clean_reason(reason)
        findings = schema.validate(col, doc_id, doc)
        if findings:
            raise Invalid(findings)
        data = schema.canonical_bytes(col, doc)
        rev = hashlib.sha256(data).hexdigest()[:16]
        with self.lock():
            cur_doc, cur_rev, cur_raw = self._current(col, doc_id, path)
            self._check_base(col, doc_id, base, cur_doc, cur_rev)
            gone = removed_items(cur_doc, doc) if col == "flows" else []
            if gone and not reason:
                raise ReasonRequired(gone)
            if cur_raw == data:
                return {"rev": rev, "changed": False, "created": False, "removed": []}
            if cur_raw is not None:
                history.keep_version(self, col, doc_id, cur_doc, cur_raw, cur_rev)
            textio.atomic_write(path, data)
            history.journal(self, col, doc_id, "edit" if cur_raw is not None else "create", by, title_of(doc),
                            reason, gone, rev=rev, prev=cur_rev)  # any reason given is kept: the history says why
        return {"rev": rev, "changed": True, "created": cur_raw is None, "removed": gone}

    def delete(self, col, doc_id, by="canvas", reason="", base=None):
        """Move a document to store/.trash. A reason is always required. NotFound when it does not exist."""
        path = self.path(col, doc_id)
        reason = clean_reason(reason)
        if not reason:
            raise ReasonRequired([doc_id])
        with self.lock():
            cur_doc, cur_rev, cur_raw = self._current(col, doc_id, path)
            if cur_raw is None:
                raise NotFound("no %s/%s" % (col, doc_id))
            self._check_base(col, doc_id, base, cur_doc, cur_rev)
            history.keep_version(self, col, doc_id, cur_doc, cur_raw, cur_rev)
            self.trash.mkdir(parents=True, exist_ok=True)
            dest = self.trash / ("%s--%s--%s.json" % (col, doc_id, history.stamp_name(cur_rev)))
            textio._retry(lambda: os.replace(str(path), str(dest)))
            history.journal(self, col, doc_id, "delete", by, title_of(cur_doc), reason, prev=cur_rev)
        return {"rev": None, "changed": True}

    def restore(self, col, doc_id, version, by="agent (restore)", reason="", base=None):
        """Put a saved version back (the current one is kept in history first)."""
        self.path(col, doc_id)
        doc = history.read_version(self, col, doc_id, version)
        doc = schema.upgrade(col, doc)
        return self.write(col, doc_id, doc, by=by, reason=reason or "restored the version from %s" % version, base=base)
