"""Checks across the whole playbook: the schema, then the rules a single document cannot see.

Errors (the playbook is wrong; `pb.py check` exits 1):
  unreadable      a file is not JSON, or its name is not a valid id
  schema          a document breaks the schema
  dangling-ref    a goto, folder, account or part reference points at something that does not exist
  start           a flow has no start node, or more than one
  unreachable     a node cannot be reached from the start
  goto-outgoing   a goto node has a main-path connection leaving it (a goto ends the path)

Warnings (the playbook is harder to follow than it should be; `--strict` makes them fail too):
  attribution     text says who said it ("<owner>, 11-08: ...") instead of what to do
  date            text carries a calendar date (2026-11-08, or 11-08 with a two-digit month; "8-12 fps" is a range)
  owner-name      text names the owner by first name instead of saying "the owner"
                  (these three read node, part, account and folder text; handles, urls and paths are not text)
  part-copy       a node repeats a part's title instead of referencing the part
  unused-folder   no node uses the folder
  unused-part     no node references the part
  flow-unlinked   no goto points at the flow (flows in the first group are entry points and exempt)
  attach-main     an "attached note" connection joins two main-path nodes
  flow-to-note    a main-path connection touches a rule, tool or gotcha
  two-way         two nodes are connected in both directions
  restates-title  a node's details only repeat its title or summary line
  placeholder     a node still has its placeholder title
  goto-body       a goto node has details; `pb.py show` prints a goto as one line, so the details are never read
  duplicate-name  two flows share a name
  email           an account record contains an email address

There is no list of accepted problems: the playbook is kept at zero findings, and CI runs the check with --strict.
"""
import collections
import os
import re

from . import schema, textio

ERROR, WARN = "error", "warning"
ERROR_RULES = ("unreadable", "schema", "dangling-ref", "start", "unreachable", "goto-outgoing")
WARN_RULES = ("attribution", "date", "owner-name", "part-copy", "unused-folder", "unused-part", "flow-unlinked", "attach-main",
              "flow-to-note", "two-way", "restates-title", "placeholder", "goto-body", "duplicate-name", "email")

# A full date, or MM-DD with a two-digit month. A number range is not a date: "8-12 fps" has a one-digit first
# number, and "10-15 minutes" is followed by what it counts ("11-07 post" is still a date: a range has no 07).
_UNITS = (r"fps|px|pt|s|sec|seconds?|min|minutes?|h|hours?|days?|weeks?|months?|years?|words?|characters?|chars?"
          r"|posts?|replies|comments?|frames?|scenes?|slides?|items?|lines?|steps?|times|x|k|kb|mb|gb|db|lufs|hz|khz")
DATE_RE = re.compile(r"(?<![\d-])(?:20\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
                     r"|(?:0[1-9]|1[0-2])-(?:0[1-9]|(?:[12]\d|3[01])(?!\s?(?:%%|(?:%s)\b))))(?![\d-])" % _UNITS, re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.(?:com|net|org|io|dev|app|co|me|ai)\b")
PLACEHOLDER_RE = re.compile(r"^(?:Untitled|Start|New (?:start|step|decision|qc check|rule|tool|gotcha|result|go to flow|account))$")
_WORD_RE = re.compile(r"[a-z0-9]+")


def owner_names(accounts):
    """First names the playbook itself gives its owners: single-word `owner` values on accounts."""
    names = set()
    for a in accounts.values():
        o = a.get("owner") if isinstance(a, dict) else None
        if isinstance(o, str) and re.match(r"^[A-Z][a-z]+$", o.strip()):
            names.add(o.strip())
    return sorted(names)


def attribution_re(names):
    """Who-said-it-when: '<Name>, 11-08', '<Name> on 11-08', '(<Name>', '<Name>: "...', or a bare '(11-08)'."""
    parts = [r"\(\d{1,2}-\d{2}\)"]
    if names:
        n = "(?:%s)" % "|".join(re.escape(x) for x in names)
        parts += [n + r", \d", n + r"(?: \w+)? on \d", r"\(" + n + r"\b", n + r': ["“]', n + r" \w+ed on \d"]
    return re.compile("|".join(parts))


def owner_name_re(names):
    """An owner's first name on its own: not inside a handle (@SamSmith), a path (Users\\Sam\\) or a longer word,
    and not a full name ("signer Sam Smith", a server called 'Sam Smith Live'), which is a literal to keep."""
    if not names:
        return None
    return re.compile(r"(?<![\\/@\w.-])(?:%s)(?:'s)?(?![\\/\w]|\.\w| [A-Z][a-z])" % "|".join(re.escape(x) for x in names))


def _words(s):
    return _WORD_RE.findall((s or "").lower())


class Report(object):
    def __init__(self):
        self.findings = []  # dicts: level, rule, file, path, message
        self.stats = collections.OrderedDict()
        self.missing_paths = []

    def add(self, level, rule, file, path, message):
        self.findings.append({"level": level, "rule": rule, "file": file, "path": path, "message": message})

    def of(self, level):
        return [f for f in self.findings if f["level"] == level]

    def counts(self, level):
        c = collections.OrderedDict()
        for f in self.of(level):
            c[f["rule"]] = c.get(f["rule"], 0) + 1
        return c

    def failed(self, strict=False):
        if strict:
            return bool(self.findings)
        return bool(self.of(ERROR))


def run(store, strict=False, check_paths=False):
    """Lint the whole store. Never raises on bad content."""
    rep = Report()
    data, _revs, errors = store.load_all()
    flows, accounts, folders, parts = data["flows"], data["accounts"], data["folders"], data["parts"]
    for file, path, msg in errors:
        rep.add(ERROR, "unreadable", file, path, msg)
    bad = set()
    for col in ("flows", "accounts", "folders", "parts"):
        for doc_id, doc in data[col].items():
            found = schema.validate(col, doc_id, doc)
            for file, path, msg in found:
                rep.add(ERROR, "schema", file, path, msg)
            if found:
                bad.add((col, doc_id))

    owners = owner_names(dict((k, v) for k, v in accounts.items() if ("accounts", k) not in bad))
    said, named = attribution_re(owners), owner_name_re(owners)

    def text_rules(file, path, text):
        """The rules every piece of instruction text follows, whichever kind of document holds it."""
        m = said.search(text)
        if m:
            rep.add(WARN, "attribution", file, path, "says who said it (%r); keep the instruction, move the rest to origin" % m.group(0))
        m = DATE_RE.search(text)
        if m:
            rep.add(WARN, "date", file, path, "carries a date (%r); move it to origin unless the date is the instruction" % m.group(0))
        m = named.search(text) if named else None
        if m:
            rep.add(WARN, "owner-name", file, path, "names the owner (%r); write \"the owner\"" % m.group(0))

    part_title = dict((p.get("title"), pid) for pid, p in parts.items() if ("parts", pid) not in bad)
    used_folders, used_parts, goto_targets = set(), set(), set()

    for fid, f in flows.items():
        if ("flows", fid) in bad:
            continue
        file = schema.file_of("flows", fid)
        nodes, kinds = {}, {}
        for raw in f["nodes"]:
            n, missing = schema.resolve_node(raw, parts)
            nid = raw["id"]
            path = "nodes/%s" % nid
            nodes[nid] = n
            kinds[nid] = n.get("kind")
            if "part" in raw:
                if missing or ("parts", raw["part"]) in bad:
                    rep.add(ERROR, "dangling-ref", file, path, "references the part %r, which does not exist" % raw["part"])
                else:
                    used_parts.add(raw["part"])
            for r in raw.get("folders") or []:
                used_folders.add(r)
                if r not in folders:
                    rep.add(ERROR, "dangling-ref", file, path, "uses the folder %r, which is not in the library" % r)
            if n.get("kind") == "goto":
                t = n.get("goto")
                if t != schema.GOTO_OVERVIEW:
                    goto_targets.add(t)
                    if t not in flows:
                        rep.add(ERROR, "dangling-ref", file, path, "opens the flow %r, which does not exist" % t)
                    elif t == fid:
                        rep.add(ERROR, "dangling-ref", file, path, "opens its own flow")
            if n.get("kind") == "account" and n.get("account") not in accounts:
                rep.add(ERROR, "dangling-ref", file, path, "names the account %r, which does not exist" % n.get("account"))
            # text rules look at what the node itself stores; a referenced part is checked once, as a part
            if "part" not in raw:
                text_rules(file, path, "\n".join([raw.get("title") or "", raw.get("sub") or "", raw.get("body") or ""] + list(raw.get("checks") or [])))
                if raw.get("title") in part_title:
                    rep.add(WARN, "part-copy", file, path, "repeats the part %r; reference it with \"part\" instead" % part_title[raw["title"]])
                if PLACEHOLDER_RE.match(raw.get("title") or "") and raw.get("kind") != "start":
                    rep.add(WARN, "placeholder", file, path, "still has the placeholder title %r" % raw.get("title"))
                if raw.get("kind") == "goto" and (raw.get("body") or raw.get("checks")):
                    rep.add(WARN, "goto-body", file, path, "a goto is shown as one line; move its details into the flow it opens, or make it a step")
                body = _words(raw.get("body"))
                if body and (body == _words(raw.get("title")) or body == _words(raw.get("sub"))):
                    rep.add(WARN, "restates-title", file, path, "the details only repeat the title or summary line")

        starts = [nid for nid, k in kinds.items() if k == "start"]
        if len(starts) != 1:
            rep.add(ERROR, "start", file, "nodes", "has %d start nodes; a flow has exactly one" % len(starts))
        out = collections.defaultdict(list)
        attach = collections.defaultdict(set)
        pairs = set()
        for e in f["edges"]:
            a, b, t = e["from"], e["to"], e["type"]
            path = "edges/%s>%s" % (a, b)
            side = kinds.get(a) in schema.SIDE_KINDS or kinds.get(b) in schema.SIDE_KINDS
            if t == "flow":
                out[a].append(b)
                if kinds.get(a) == "goto":
                    rep.add(ERROR, "goto-outgoing", file, path, "leaves a goto node; a goto ends the path (make it a step, or attach the note)")
                if side:
                    rep.add(WARN, "flow-to-note", file, path, "is a main-path connection but touches a rule, tool or gotcha; make it an attached note")
            else:
                attach[a].add(b)
                attach[b].add(a)
                if not side:
                    rep.add(WARN, "attach-main", file, path, "is an attached-note connection between two main-path nodes")
            if (b, a) in pairs:
                rep.add(WARN, "two-way", file, path, "these two nodes are connected in both directions")
            pairs.add((a, b))
        if len(starts) == 1:
            seen, todo = set(starts), list(starts)
            while todo:
                x = todo.pop()
                for y in list(out[x]) + list(attach[x]):
                    if y not in seen:
                        seen.add(y)
                        todo.append(y)
            for nid in nodes:
                if nid not in seen:
                    rep.add(ERROR, "unreachable", file, "nodes/%s" % nid, "cannot be reached from the start node")

    names = collections.defaultdict(list)
    for fid, f in flows.items():
        if ("flows", fid) in bad:
            continue
        names[f["name"]].append(fid)
        if fid not in goto_targets and f["group"] != schema.GROUPS[0]:
            rep.add(WARN, "flow-unlinked", schema.file_of("flows", fid), "(document)", "no goto in any flow opens this one")
    for name, fl in names.items():
        for fid in fl[1:]:
            rep.add(WARN, "duplicate-name", schema.file_of("flows", fid), "name", "%r is also the name of %s" % (name, fl[0]))

    for aid, a in accounts.items():
        if ("accounts", aid) in bad:
            continue
        file = schema.file_of("accounts", aid)
        if a.get("flow") and a["flow"] not in flows:
            rep.add(ERROR, "dangling-ref", file, "flow", "names the flow %r, which does not exist" % a["flow"])
        for p in a.get("projects") or []:
            if p not in flows:
                rep.add(ERROR, "dangling-ref", file, "projects", "names the flow %r, which does not exist" % p)
        m = EMAIL_RE.search(textio.dumps(a))
        if m:
            rep.add(WARN, "email", file, "(document)", "contains an email address (%s); accounts never hold emails or secrets" % m.group(0))
        text_rules(file, "(text)", "\n".join([a.get("label") or "", a.get("purpose") or "", a.get("pacing") or ""] + list(a.get("notes") or [])))

    for pid, p in parts.items():
        if ("parts", pid) in bad:
            continue
        file = schema.file_of("parts", pid)
        text_rules(file, "(text)", "\n".join([p.get("title") or "", p.get("sub") or "", p.get("body") or ""] + list(p.get("checks") or [])))
        for r in p.get("folders") or []:
            used_folders.add(r)
            if r not in folders:
                rep.add(ERROR, "dangling-ref", file, "folders", "uses the folder %r, which is not in the library" % r)
        if p.get("kind") == "goto" and p.get("goto") != schema.GOTO_OVERVIEW and p.get("goto") not in flows:
            rep.add(ERROR, "dangling-ref", file, "goto", "opens the flow %r, which does not exist" % p.get("goto"))
        if p.get("kind") == "account" and p.get("account") not in accounts:
            rep.add(ERROR, "dangling-ref", file, "account", "names the account %r, which does not exist" % p.get("account"))
        if pid not in used_parts:
            rep.add(WARN, "unused-part", file, "(document)", "no node references this part")

    for fo_id, fo in folders.items():
        if ("folders", fo_id) in bad:
            continue
        if fo_id not in used_folders:
            rep.add(WARN, "unused-folder", schema.file_of("folders", fo_id), "(document)", "no node uses this folder")
        text_rules(schema.file_of("folders", fo_id), "(text)", "\n".join([fo.get("label") or "", fo.get("note") or ""]))
        if check_paths:
            local = folder_path(fo.get("path", ""), store.root.parent)
            if local is not None and not os.path.exists(local):
                rep.missing_paths.append(fo.get("path", ""))

    rep.stats["flows"] = len(flows)
    rep.stats["nodes"] = sum(len(f["nodes"]) for f in flows.values() if isinstance(f, dict) and isinstance(f.get("nodes"), list))
    rep.stats["accounts"] = len(accounts)
    rep.stats["folders"] = len(folders)
    rep.stats["parts"] = len(parts)
    return rep


def references(data, col, doc_id):
    """Everything that points at one document, as (file, path, what): the nodes and parts that use a folder, follow
    a part, name an account or open a flow, and the accounts that name a flow. `data` is Store.load_all()[0]."""
    out = []

    def nodes_of(f):
        nodes = f.get("nodes") if isinstance(f, dict) else None
        return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []

    def points(obj):
        if not isinstance(obj, dict):
            return None
        if col == "folders" and isinstance(obj.get("folders"), list) and doc_id in obj["folders"]:
            return "uses the folder %r" % doc_id
        if col == "parts" and obj.get("part") == doc_id:
            return "follows the part %r" % doc_id
        if col == "accounts" and obj.get("kind") == "account" and obj.get("account") == doc_id and "part" not in obj:
            return "names the account %r" % doc_id
        if col == "flows" and obj.get("kind") == "goto" and obj.get("goto") == doc_id and "part" not in obj:
            return "opens the flow %r" % doc_id
        return None

    for fid, f in sorted(data.get("flows", {}).items()):
        for n in nodes_of(f):
            what = points(n)
            if what and not (col == "flows" and fid == doc_id):
                out.append((schema.file_of("flows", fid), "nodes/%s" % n.get("id"), what))
    for pid, p in sorted(data.get("parts", {}).items()):
        what = points(p)
        if what and not (col == "parts" and pid == doc_id):
            out.append((schema.file_of("parts", pid), "(document)", what))
    if col == "flows":
        for aid, a in sorted(data.get("accounts", {}).items()):
            if isinstance(a, dict) and (a.get("flow") == doc_id or doc_id in (a.get("projects") if isinstance(a.get("projects"), list) else [])):
                out.append((schema.file_of("accounts", aid), "flow" if a.get("flow") == doc_id else "projects", "names the flow %r" % doc_id))
    return out


def folder_path(path, skill_dir):
    """A folder record's path as this machine sees it: ~ expanded, and a relative path (playbook/state/bank.tsv)
    taken from the skill folder, the parent of playbook/. None for a network path, which is never probed (a
    missing server can stall a check for many seconds)."""
    if not isinstance(path, str) or not path.strip():
        return None
    p = os.path.expanduser(path.strip())
    if p.startswith("\\\\") or p.startswith("//"):
        return None
    if not (os.path.isabs(p) or (len(p) > 2 and p[1] == ":" and p[2] in "\\/")):
        p = os.path.join(str(skill_dir), p)
    return os.path.normpath(p)


def doc_refs(store, col, doc_id):
    """Dangling references in one document (after a save), as messages. Never raises."""
    try:
        rep = run(store, strict=True)
    except Exception:
        return []
    file = schema.file_of(col, doc_id)
    return ["%s: %s" % (f["path"], f["message"]) for f in rep.findings
            if f["file"] == file and f["rule"] in ("dangling-ref", "start", "unreachable", "goto-outgoing")]
