"""Read and change the Agent Playbook from the command line (for agents). The canvas and this tool share
the same files in playbook/store/, so whatever the owner changed on the canvas is what this prints.

  python pb.py list                     every flow, grouped, with node counts (* = has the owner's edits)
  python pb.py show <flow> [--json]     the flow as an outline: steps, decisions, rules, checks, folders
  python pb.py search <text>            nodes, accounts and folders that mention the text
  python pb.py accounts [platform]      the accounts registry
  python pb.py folders                  the shared folders and which flows use them
  python pb.py parts                    the parts library
  python pb.py edited [YYYY-MM-DD]      what the owner changed on the canvas (optionally since a date)
  python pb.py catalog [recipe]         the recipes (one per kind of job) and every piece a recipe uses
  python pb.py catalog --pieces         what each kind of piece is for, and where a new lesson goes
  python pb.py check [--strict] [-w]    schema, lint, recipes and QC filters (qc.py selftest); errors exit 1;
                                        --strict fails on warnings too; -w lists them
  python pb.py rev <col> <id>           the revision of a document (pass it to put --base)
  python pb.py put <col> <id> <file> [--reason "why"] [--base REV]
                                        save a flow/account/folder/part from a JSON file; the old version is kept.
                                        The file is checked against the schema first. --reason is required when the
                                        new version drops steps or connections. --base REV refuses the save when the
                                        stored document is no longer the revision you started from. The file may be
                                        the output of show --json, edited: its "doc" is saved, and its "rev" is the
                                        base unless --base says otherwise.
  python pb.py split <flow> <plan.json> --reason "why" [--dry-run]
                                        split a flow into single-job flows: the plan names each new flow and its
                                        nodes; attached notes follow; crossing paths become gotos; the original
                                        stays as the hub. Checked with the full lint before anything is saved.
  python pb.py delete <col> <id> --reason "why" [--base REV]
                                        move a document to store/.trash (a reason is always required); names every
                                        flow, part or account that still points at it
  python pb.py versions <col> <id>      list saved versions of a document
  python pb.py restore <col> <id> <version> [--reason "why"]
                                        put a saved version back (the current one is kept in history first)
  python pb.py changes [n]              the latest changes to the playbook (who, what, when, why)
  python pb.py export                   write store.json next to playbook.html, for a view-only published copy
  python pb.py normalize [--check]      rewrite every document in canonical form (same content, fixed bytes)

<col> is flows, accounts, folders or parts. Every deletion needs a real reason: what was wrong, or what the owner
asked for. It goes into the change history, with the actor $AGENT_PLAYBOOK_ACTOR (default "agent").
Exit codes: 0 done, 1 failed (the reason is on stderr), 2 the command line was wrong.
"""
import argparse
import datetime as dt
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from lib import catalog, history, ids, lint, schema, textio  # noqa: E402
from lib import store as storelib  # noqa: E402

KIND = {"start": "START", "step": "STEP", "decision": "DECIDE", "check": "QC", "rule": "RULE", "tool": "TOOL",
        "gotcha": "GOTCHA", "output": "RESULT", "goto": "GOTO", "account": "ACCOUNT"}
ACTOR_ENV = "AGENT_PLAYBOOK_ACTOR"


def actor():
    """Who the change history says made a change from this command line: $AGENT_PLAYBOOK_ACTOR, else "agent"."""
    name = " ".join(os.environ.get(ACTOR_ENV, "").split())[:40]
    return name or "agent"


BY = actor()


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def load(store):
    """Everything readable. A file that cannot be read is named on stderr and left out; nothing crashes."""
    data, revs, errors = store.load_all()
    for file, path, msg in errors:
        print("warning: %s: %s: %s" % (file, path, msg), file=sys.stderr)
    return data, revs


def text(v):
    return v if isinstance(v, str) else ""


def items(v):
    return v if isinstance(v, list) else []


def flows_sorted(flows):
    def key(kv):
        f = kv[1] if isinstance(kv[1], dict) else {}
        g = f.get("group")
        gi = schema.GROUPS.index(g) if g in schema.GROUPS else 99
        order = f.get("order") if isinstance(f.get("order"), int) else 999
        return (gi, order, text(f.get("name")), kv[0])
    return sorted(flows.items(), key=key)


def flow_nodes(f, parts):
    """The flow's nodes as they read (part references filled in). Entries that are not usable nodes are skipped."""
    out = []
    for n in items(f.get("nodes")) if isinstance(f, dict) else []:
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            out.append(schema.resolve_node(n, parts)[0])
    return out


# ---------------------------------------------------------------- show

def node_lines(n, indent, tag, ctx):
    """One node, compact: a head line, its details, its checklist, then one line for folders, links and account."""
    kind = n.get("kind")
    head = "%s%s[%s] %s" % (indent, tag, KIND.get(kind, "STEP"), text(n.get("title")))
    if n.get("sub"):
        head += " | " + text(n["sub"])
    if kind == "goto":
        head += " -> " + text(n.get("goto"))
    if n.get("editedAt"):
        head += " (owner-edited)"
    out = [head]
    pad = indent + "  "
    if kind != "goto":  # a goto is a pointer: its target flow holds the instructions
        for line in text(n.get("body")).splitlines():
            if line.strip():
                out.append(pad + line.strip())
        for c in items(n.get("checks")):
            out.append(pad + "[ ] " + text(c).strip())
    tail = []
    refs = []
    for ref in items(n.get("folders")):
        fo = ctx["folders"].get(ref) if isinstance(ref, str) else None
        if not isinstance(fo, dict):
            refs.append("%s (not in the library)" % (ref,))
        elif ref in ctx["shown"]:
            refs.append("%s (above)" % text(fo.get("label")))
        else:
            ctx["shown"].add(ref)
            refs.append("%s -> %s" % (text(fo.get("label")), text(fo.get("path"))))
    if refs:
        tail.append("folders: " + "; ".join(refs))
    links = ["%s %s" % (text(l.get("label")), text(l.get("url"))) for l in items(n.get("links")) if isinstance(l, dict)]
    if links:
        tail.append("links: " + "; ".join(links))
    if kind == "account" and n.get("account"):
        a = ctx["accounts"].get(n["account"]) or {}
        tail.append("account: %s %s (%s)" % (text(a.get("platform")), text(a.get("handle")) or text(a.get("label")), text(a.get("status"))))
    if tail:
        out.append(pad + " | ".join(tail))
    return out


def show_lines(fid, f, rev, data):
    nodes = {}
    for n in flow_nodes(f, data["parts"]):
        nodes.setdefault(n["id"], n)
    nxt, attached = {}, {}
    for e in items(f.get("edges")):
        if not isinstance(e, dict) or e.get("from") not in nodes or e.get("to") not in nodes:
            continue
        a, b = nodes[e["from"]], nodes[e["to"]]
        if e.get("type") == "attach":
            # hang rules, tools and gotchas under the step they belong to
            host, extra = (e["to"], e["from"]) if a.get("kind") in schema.SIDE_KINDS else (e["from"], e["to"])
            attached.setdefault(host, []).append(extra)
        else:
            nxt.setdefault(e["from"], []).append((e["to"], text(e.get("label"))))
    out = ["# %s [%s] id=%s rev=%s" % (text(f.get("name")) or fid, text(f.get("group")), fid, rev)]
    if text(f.get("summary")).strip():
        out.append(text(f["summary"]).strip())
    if any(n.get("kind") == "goto" for n in nodes.values()):
        out.append("A GOTO line ends in the id of the flow it opens: python pb.py show <id>")
    out.append("")
    ctx = {"folders": data["folders"], "accounts": data["accounts"], "shown": set()}
    seen = set()

    def ypos(nid):
        y = nodes[nid].get("y")
        return y if isinstance(y, (int, float)) and not isinstance(y, bool) else 0

    def walk(nid, depth, label):
        indent = "  " * depth
        if nid in seen:
            # a path that joins a step printed earlier (a loop back, or two branches meeting): name where it goes on
            out.append("%s%s-> continue at: %s (above)" % (indent, "(%s) " % label if label else "", text(nodes[nid].get("title"))))
            return
        seen.add(nid)
        out.extend(node_lines(nodes[nid], indent, "(%s) " % label if label else "", ctx))
        for a in attached.get(nid, []):
            if a not in seen:
                seen.add(a)
                out.extend(node_lines(nodes[a], indent + "  ", "+ ", ctx))
        kids = sorted(nxt.get(nid, []), key=lambda t: ypos(t[0]))
        for to, lab in kids:
            walk(to, depth + (1 if len(kids) > 1 else 0), lab)

    starts = [nid for nid, n in nodes.items() if n.get("kind") == "start"] or list(nodes)[:1]
    for s in starts:
        walk(s, 0, "")
    rest = [nid for nid in nodes if nid not in seen]
    if rest:
        out.append("")
        out.append("## Also in this flow")
        for nid in rest:
            if nid not in seen:
                seen.add(nid)
                out.extend(node_lines(nodes[nid], "", "", ctx))
    return out


def pick_flow(flows, name):
    if name in flows:
        return name
    hits = [k for k, v in flows.items() if name.lower() in (k + " " + text(v.get("name") if isinstance(v, dict) else "")).lower()]
    if len(hits) == 1:
        return hits[0]
    fail("no flow '%s'. Try: python pb.py list" % name + (" (did you mean %s?)" % ", ".join(hits) if hits else ""))


def cmd_show(store, args):
    data, revs = load(store)
    fid = pick_flow(data["flows"], args.flow)
    f, rev = data["flows"][fid], revs["flows"][fid]
    if not isinstance(f, dict):
        fail("flows/%s is not an object; run: python pb.py check" % fid)
    if args.json:
        out = {"col": "flows", "id": fid, "rev": rev, "doc": f}
        if any(isinstance(n, dict) and "part" in n for n in items(f.get("nodes"))):
            out["resolved"] = schema.resolve_flow(f, data["parts"])
        print(textio.dumps(out, indent=1))
        return
    if schema.validate("flows", fid, f):
        print("warning: flows/%s breaks the schema; run: python pb.py check" % fid, file=sys.stderr)
    for line in show_lines(fid, f, rev, data):
        print(line)


# ---------------------------------------------------------------- other reads

def cmd_list(store, args):
    data, _ = load(store)
    group = object()
    for fid, f in flows_sorted(data["flows"]):
        if not isinstance(f, dict):
            continue
        if f.get("group") != group:
            group = f.get("group")
            print("\n%s" % (text(group) or "(no group)"))
        nodes = [n for n in items(f.get("nodes")) if isinstance(n, dict)]
        star = "*" if f.get("editedAt") or any(n.get("editedAt") for n in nodes) else " "
        print(" %s %-18s %-32s %3d nodes  %s" % (star, fid, text(f.get("name")), len(nodes), text(f.get("summary"))[:70]))


def cmd_search(store, args):
    q = " ".join(args.text).lower()
    data, _ = load(store)
    for fid, f in flows_sorted(data["flows"]):
        for n in flow_nodes(f, data["parts"]):
            blob = " ".join([text(n.get("title")), text(n.get("sub")), text(n.get("body"))] + [text(c) for c in items(n.get("checks"))]).lower()
            if q in blob:
                print("%-18s [%s] %s | %s" % (fid, KIND.get(n.get("kind"), "STEP"), text(n.get("title")), text(n.get("sub"))))
    for aid, ac in sorted(data["accounts"].items()):
        if isinstance(ac, dict) and q in textio.dumps(ac).lower():
            print("account            %s: %s (%s)" % (text(ac.get("platform")), text(ac.get("handle")) or text(ac.get("label")), text(ac.get("status"))))
    for fo_id, fo in sorted(data["folders"].items()):
        if isinstance(fo, dict) and q in (text(fo.get("label")) + " " + text(fo.get("path"))).lower():
            print("folder             %s -> %s" % (text(fo.get("label")), text(fo.get("path"))))
    for pid, p in sorted(data["parts"].items()):
        if isinstance(p, dict) and q in " ".join([text(p.get("title")), text(p.get("sub")), text(p.get("body"))]).lower():
            print("part               [%s] %s (%s)" % (KIND.get(p.get("kind"), "STEP"), text(p.get("title")), pid))


def cmd_accounts(store, args):
    plat = " ".join(args.platform).lower()
    data, _ = load(store)
    rows = [(aid, a) for aid, a in data["accounts"].items() if isinstance(a, dict)]
    for aid, ac in sorted(rows, key=lambda kv: (text(kv[1].get("platform")), kv[0])):
        if plat and plat not in text(ac.get("platform")).lower():
            continue
        print("%-15s %-34s %-12s flow=%s" % (text(ac.get("platform")), text(ac.get("handle")) or text(ac.get("label")), text(ac.get("status")), text(ac.get("flow"))))
        for line in text(ac.get("purpose")).splitlines():
            if line.strip():
                print("  " + line.strip())
        if ac.get("pacing"):
            print("  pacing: %s" % text(ac["pacing"]))
        for note in items(ac.get("notes")):
            print("  - %s" % text(note))


def cmd_folders(store, args):
    data, _ = load(store)
    use = {}
    for fid, f in data["flows"].items():
        for n in flow_nodes(f, data["parts"]):
            for r in items(n.get("folders")):
                if isinstance(r, str):
                    use.setdefault(r, set()).add(fid)
    rows = [(i, fo) for i, fo in data["folders"].items() if isinstance(fo, dict)]
    for fo_id, fo in sorted(rows, key=lambda kv: (text(kv[1].get("kind")), text(kv[1].get("label")), kv[0])):
        print("%-9s %-36s %s   used by: %s" % (text(fo.get("kind")), text(fo.get("label")), text(fo.get("path")), ", ".join(sorted(use.get(fo_id, []))) or "-"))


def cmd_parts(store, args):
    data, _ = load(store)
    use = {}
    for fid, f in data["flows"].items():
        for n in items(f.get("nodes")) if isinstance(f, dict) else []:
            if isinstance(n, dict) and isinstance(n.get("part"), str):
                use.setdefault(n["part"], set()).add(fid)
    for pid, p in sorted(data["parts"].items()):
        if isinstance(p, dict):
            print("[%s] %s | %s  (%s)  used by: %s" % (KIND.get(p.get("kind"), "STEP"), text(p.get("title")), text(p.get("sub")), pid, ", ".join(sorted(use.get(pid, []))) or "-"))


def since_utc(value):
    """A local YYYY-MM-DD as the UTC instant that day starts, in the form the canvas stamps edits with."""
    if not value:
        return ""
    try:
        day = dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        fail("'%s' is not a date: use YYYY-MM-DD" % value, 2)
    return day.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def cmd_edited(store, args):
    since = since_utc(args.since)
    data, _ = load(store)
    for fid, f in flows_sorted(data["flows"]):
        if not isinstance(f, dict):
            continue
        if text(f.get("editedAt")) and text(f.get("editedAt")) >= since:
            print("%-18s flow settings changed %s" % (fid, f["editedAt"][:16]))
        for n in items(f.get("nodes")):
            if isinstance(n, dict) and text(n.get("editedAt")) and n["editedAt"] >= since:
                r = schema.resolve_node(n, data["parts"])[0]
                print("%-18s %s  [%s] %s" % (fid, n["editedAt"][:16], KIND.get(r.get("kind"), "STEP"), text(r.get("title"))))
    for col in ("accounts", "folders", "parts"):
        for did, d in sorted(data[col].items()):
            if isinstance(d, dict) and text(d.get("editedAt")) and d["editedAt"] >= since:
                print("%-18s %s  %s" % (col, d["editedAt"][:16], text(d.get("handle")) or text(d.get("label")) or text(d.get("title")) or did))


# ---------------------------------------------------------------- check

def qc_findings(root):
    """[(level, rule, file, path, message)] for qc/filters.json and qc/lists.json: valid, and every filter catches
    its bad examples and passes its good ones (qc.py selftest). Nothing when the playbook has no qc folder."""
    if not (root / "qc" / "filters.json").exists():
        return []
    import qc as qcmod
    from lib import qcengine
    try:
        filters, lists = qcengine.load(root)
    except qcengine.QCError as e:
        return [("error", "qc-filters", "qc/filters.json", "", str(e))]
    fails = qcengine.selftest(filters, lists, lambda ex: qcmod.make_context(root, ex.get("history", []), lists, ex.get("registry", {})))
    return [("error", "qc-selftest", "qc/filters.json", f.split(" ")[0], f) for f in fails]


def cmd_check(store, args):
    rep = lint.run(store, strict=args.strict, check_paths=True)
    data, _ = store.load_all()[:2]
    for row in catalog.check(store.root, data.get("flows", {}), data.get("parts", {})) + qc_findings(store.root):
        rep.add(*row)
    listed = [lint.ERROR] + ([lint.WARN] if args.warnings or args.strict else [])
    for f in rep.findings:
        if f["level"] in listed and (not args.rule or f["rule"] in args.rule):
            print("%s: %s: %s [%s] %s" % (f["file"], f["path"], f["level"], f["rule"], f["message"]))
    if args.paths:
        for p in rep.missing_paths:
            print("note: folder not on this machine: %s" % p)
    errors, warns = rep.counts(lint.ERROR), rep.counts(lint.WARN)

    def by_rule(c):
        return " (" + ", ".join("%s %d" % kv for kv in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) + ")" if c else ""

    s = rep.stats
    line = "%d flows, %d nodes, %d accounts, %d folders, %d parts: %d problem(s)%s, %d warning(s)%s" % (
        s["flows"], s["nodes"], s["accounts"], s["folders"], s["parts"], sum(errors.values()), by_rule(errors),
        sum(warns.values()), by_rule(warns))
    line += ", %d folder(s) not on this machine" % len(rep.missing_paths)
    print(line)
    if warns and not (args.warnings or args.strict):
        print("(python pb.py check -w lists every warning; --strict makes them fail)")
    sys.exit(1 if rep.failed(args.strict) else 0)


# ---------------------------------------------------------------- catalog

GUIDE = """Pieces (python pb.py catalog --pieces explains each):
  recipe      one kind of job: its flows and its run checklist (state/recipes.json)
  flow        one job's steps, decisions, rules and gotchas (the canvas)
  part        steps several flows share (pb.py parts)
  filter      one QC check a script makes on every draft (qc/filters.json; qc.py)
  list        names and phrases the filters read (qc/lists.json)
  check node  a QC judgment a script can't make, with the evidence that answers it
  rule        a standing rule; gotcha: a failure seen before; tool: a command
  script      a helper the steps call (scripts/INDEX.json, media/INDEX.json)"""

PIECE_HELP = """Where a new piece of knowledge goes (flow playbook, Where a lesson goes, says the same):
  a mistake a script can catch          -> a filter in qc/filters.json, with the bad text that slipped through as its
                                           bad example and a good one; or an entry in qc/lists.json (a name, a phrase)
  a judgment a script can't make        -> a check node (QC) in the flow, saying what evidence answers it; if every
                                           send of a kind needs it, an "ask" filter too
  a standing rule for every job         -> flow ground-rules (and a filter if it can be checked)
  how one platform or tool behaves      -> a gotcha or step in that platform's flow
  a fact about one of the owner's apps  -> that app's flow; never-say items also go in qc/lists.json never_say
  the same steps in two or more flows   -> a part (pb.py parts), referenced from each flow
  a new kind of job                     -> a recipe in state/recipes.json, each step naming its flow or part
  a flow that does several jobs         -> split it: pb.py split <flow> <plan.json> --reason "why" (the plan
                                           format is in lib/split.py); then point recipe steps at the new flows
  a step in a job that keeps getting skipped -> a step in that recipe's checklist
Then: python playbook/pb.py check --strict (it runs qc.py selftest too)."""


def cmd_catalog(store, args):
    data, _ = load(store)
    recipes, problem = catalog.load(store.root)
    if problem:
        fail(problem)
    recipes = dict((k, v) for k, v in recipes.items() if isinstance(v, dict))  # pb.py check names the others
    if args.pieces:
        print(GUIDE + "\n\n" + PIECE_HELP)
        return
    filters, lists, applies = None, None, None
    if (store.root / "qc" / "filters.json").exists():
        from lib import qcengine
        try:
            filters, lists = qcengine.load(store.root)
        except qcengine.QCError as e:
            print("warning: %s" % e, file=sys.stderr)
        if filters is not None:
            ctx = qcengine.Context([], lists)

            def applies(f, action):
                a = f.get("applies") or {}
                if a.get("recipes") and args.recipe not in a["recipes"]:
                    return False
                return qcengine.applies(dict(f, applies=dict((k, v) for k, v in a.items() if k in ("kinds", "actions"))),
                                        qcengine.Draft("x", "", "", action), ctx)
    if not args.recipe:
        print(GUIDE + "\n\nRecipes (python pb.py catalog <recipe> for its pieces; python scripts/run.py start --recipe <id>):")
        group = None
        for rid, r in sorted(recipes.items(), key=lambda kv: (kv[1].get("group", ""), kv[0])):
            if r.get("group") != group:
                group = r.get("group")
                print("\n  %s" % (group or "other"))
            print("    %-14s %s\n    %-14s when: %s" % (rid, r.get("title", ""), "", r.get("when", "")))
        return
    if args.recipe not in recipes:
        fail("no recipe %r: %s" % (args.recipe, ", ".join(sorted(recipes))))
    r = recipes[args.recipe]
    p = catalog.pieces(r, data["flows"], data["parts"], filters, lists, applies)
    print("%s: %s\nwhen: %s\nstart: python scripts/run.py start --recipe %s\n" % (args.recipe, r.get("title", ""), r.get("when", ""), args.recipe))
    print("checklist:")
    for s in r.get("steps") if isinstance(r.get("steps"), list) else []:
        if isinstance(s, dict):
            print("  %-16s %s  [%s]" % (s.get("id", "?"), s.get("text", ""), ("flow %s" % s["flow"]) if s.get("flow") else ("part %s" % s.get("part", "?"))))
    print("\nflows to read:        " + ", ".join(p["flows"]))
    print("reached by gotos:     " + (", ".join(p["reached"]) or "-"))
    print("parts:                " + (", ".join(p["parts"]) or "-"))
    print("scripts named:        " + (", ".join(p["scripts"]) or "-"))
    if filters is not None:
        print("QC filters (sends: %s): %s" % ("/".join(str(a) for a in r.get("actions") or []) or "none", ", ".join(p["filters"]) or "-"))
        only = [f["id"] for f in filters if args.recipe in (f.get("applies") or {}).get("recipes", [])]
        if only:
            print("QC filters only this recipe gets: " + ", ".join(only))


# ---------------------------------------------------------------- writes

def read_doc_file(path):
    try:
        raw = textio.read_bytes(path)
    except OSError as e:
        fail("cannot read %s: %s" % (path, e.strerror or e))
    try:
        return textio.loads(raw)
    except ValueError as e:
        fail("%s: %s" % (path, e))


SHOW_KEYS = ("col", "id", "rev", "doc", "resolved")


def unwrap_shown(doc, col, doc_id, path):
    """(document, revision or None). The output of `show --json` is {"col", "id", "rev", "doc"[, "resolved"]}; a
    stored document can never have those keys, so a file shaped like that is the wrapper. Its revision counts
    only when it is the same document."""
    if not (isinstance(doc, dict) and "doc" in doc and "col" in doc and set(doc) <= set(SHOW_KEYS)):
        return doc, None
    if doc["col"] != col:
        fail("%s is the output of show --json for %s/%s, not %s" % (path, doc["col"], doc.get("id"), col))
    rev = doc.get("rev") if doc.get("id") == doc_id and isinstance(doc.get("rev"), str) else None
    return doc["doc"], rev


def explain(store, e, cmd):
    """Turn a store refusal into one plain message and exit 1."""
    if isinstance(e, storelib.Invalid):
        print("not saved: the document breaks the schema", file=sys.stderr)
        for file, path, msg in e.findings:
            print("%s: %s: %s" % (file, path, msg), file=sys.stderr)
        sys.exit(1)
    if isinstance(e, storelib.ReasonRequired):
        what = " It removes: %s." % ", ".join(e.removed) if cmd != "delete" else ""
        fail('A reason is required.%s Run it again with --reason "why".' % what)
    if isinstance(e, storelib.Stale):
        fail("not saved: %s" % e)
    fail(str(e))


def after_save(store, col, doc_id):
    for msg in lint.doc_refs(store, col, doc_id):
        print("warning: %s/%s: %s (python pb.py check)" % (col, doc_id, msg), file=sys.stderr)


def cmd_put(store, args):
    try:
        store.path(args.col, args.id)
        # the revision on disk when this command started: the save is refused if it changes before the write
        try:
            _, seen = store.read(args.col, args.id)
        except storelib.NotFound:
            seen = storelib.NEW
        except storelib.Unreadable:
            seen = None  # replacing a file that cannot be read is allowed; its bytes are kept in history
        doc, shown_rev = unwrap_shown(read_doc_file(args.file), args.col, args.id, args.file)
        base = args.base or shown_rev or seen
        r = store.write(args.col, args.id, doc, by=BY, reason=args.reason or "", base=base)
    except (storelib.StoreError, storelib.ReasonRequired) as e:
        explain(store, e, "put")
    print("%s %s/%s rev %s" % ("saved" if r["changed"] else "unchanged", args.col, args.id, r["rev"]))
    after_save(store, args.col, args.id)


def cmd_split(store, args):
    """Split a flow by a plan (lib/split.py): check the result with the full lint in a scratch copy of the store,
    then save the hub and every new flow, each with the reason."""
    import shutil
    import tempfile
    from lib import split as splitlib
    data, revs = load(store)
    fid = pick_flow(data["flows"], args.flow)
    plan = read_doc_file(args.plan)
    if not isinstance(plan, dict) or not isinstance(plan.get("flows"), dict) or not plan["flows"]:
        fail("the plan needs a \"flows\" object (python pb.py split --help, or lib/split.py)")
    if schema.validate("flows", fid, data["flows"][fid]):
        fail("flows/%s breaks the schema; fix it before splitting it (python pb.py check)" % fid)
    start = [n["id"] for n in data["flows"][fid]["nodes"] if n.get("kind") == "start"]
    try:
        splitlib.check_plan(plan, set(data["flows"]))
        for spec in plan["flows"].values():
            if set(start) & set(spec.get("nodes", [])):
                raise splitlib.PlanError("the start node stays in the hub")
        docs = splitlib.split(fid, data["flows"][fid], plan, data["parts"], set(data["flows"]))
    except splitlib.PlanError as e:
        fail("plan: %s" % e)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pbsplit-"))
    try:
        shutil.copytree(str(store.root / "store"), str(tmp / "store"), ignore=shutil.ignore_patterns(".trash"))
        for other in ("state", "qc"):
            if (store.root / other).is_dir():
                shutil.copytree(str(store.root / other), str(tmp / other), ignore=shutil.ignore_patterns("runs"))
        for new_id, doc in docs.items():
            (tmp / "store" / "flows" / (new_id + ".json")).write_text(textio.dumps(doc, indent=1) + "\n", encoding="utf-8")
        rep = lint.run(storelib.Store(tmp), strict=True)
        bad = [f for f in rep.findings if f["level"] == lint.ERROR or (f["file"].split("/")[-1][:-5] in docs and f["rule"] != "flow-no-recipe")]
    finally:
        shutil.rmtree(str(tmp), True)
    for f in bad:
        print("%s: %s: %s [%s] %s" % (f["file"], f["path"], f["level"], f["rule"], f["message"]))
    if args.dry_run or bad:
        for new_id, doc in docs.items():
            print("%s: %d nodes (%s)" % (new_id, len(doc["nodes"]), doc.get("name")))
        if bad:
            fail("not saved: the split would leave the problems above")
        return
    reason = args.reason or ""
    if not reason:
        fail('A reason is required: pb.py split <flow> <plan> --reason "why"')
    # All or nothing: the new flows are saved first and the hub last, against the revision the split was worked
    # out from. If the hub changed meanwhile (the canvas saved it), the new flows are taken back out, so the
    # playbook never holds the moved steps twice.
    # Each new flow is listed with the revision this split writes before the write starts: a write can land on disk
    # and then fail (journaling it), and the take-back must still find it. The take-back deletes a flow only at that
    # revision, so an edit made to it meanwhile (or a flow of that name that was already there) is never removed.
    created, said = [], []
    try:
        now_doc, now_rev = store.read("flows", fid)
        if now_rev != revs["flows"][fid]:
            raise storelib.Stale("flows", fid, revs["flows"][fid], now_doc, now_rev)
        for new_id, doc in docs.items():
            if new_id != fid:
                created.append((new_id, schema.revision("flows", doc)))
                r = store.write("flows", new_id, doc, by=BY, reason=reason, base=storelib.NEW)
                said.append("saved flows/%s rev %s (%d nodes)" % (new_id, r["rev"], len(doc["nodes"])))
        r = store.write("flows", fid, docs[fid], by=BY, reason=reason, base=revs["flows"][fid])
        said.append("saved flows/%s rev %s (%d nodes, the hub)" % (fid, r["rev"], len(docs[fid]["nodes"])))
    except (storelib.StoreError, storelib.ReasonRequired, OSError) as e:
        undone = []
        for new_id, rev in reversed(created):
            try:
                store.delete("flows", new_id, by=BY, reason="split of %s not finished, taken back: %s" % (fid, e), base=rev)
                undone.append(new_id)
            except storelib.NotFound:
                pass  # never written
            except storelib.Stale:
                print("left flows/%s in place: it isn't the version this split wrote" % new_id, file=sys.stderr)
            except (storelib.StoreError, storelib.ReasonRequired, OSError) as e2:
                print("could not take back flows/%s: %s" % (new_id, e2), file=sys.stderr)
        if undone:
            print("not split: took back %s (kept in store/.trash)" % ", ".join(reversed(undone)), file=sys.stderr)
        if isinstance(e, OSError):
            raise  # main() turns it into one plain line
        explain(store, e, "put")
    for line in said:
        print(line)


def cmd_delete(store, args):
    try:
        store.path(args.col, args.id)
        data = store.load_all()[0]
        store.delete(args.col, args.id, by=BY, reason=args.reason or "", base=args.base or None)
    except storelib.NotFound:
        fail("no %s/%s" % (args.col, args.id))
    except (storelib.StoreError, storelib.ReasonRequired) as e:
        explain(store, e, "delete")
    print("deleted %s/%s (kept in store/.trash and its last version in history)" % (args.col, args.id))
    # the canvas detaches what pointed at a deleted folder, part or account; from here, say what still does
    refs = lint.references(data, args.col, args.id)
    for file, path, what in refs:
        print("warning: %s: %s %s, which is now deleted" % (file, path, what), file=sys.stderr)
    if refs:
        print("warning: %d reference(s) left pointing at %s/%s: edit them on the canvas or with pb.py put "
              "(python pb.py check lists them)" % (len(refs), args.col, args.id), file=sys.stderr)


def cmd_rev(store, args):
    try:
        print(store.read(args.col, args.id)[1])
    except storelib.StoreError as e:
        fail(str(e))


def cmd_versions(store, args):
    try:
        store.path(args.col, args.id)
    except storelib.BadId as e:
        fail(str(e))
    for v in history.versions(store, args.col, args.id):
        print(v)


def cmd_restore(store, args):
    try:
        r = store.restore(args.col, args.id, args.version, by=BY + " (restore)", reason=args.reason or "")
    except LookupError:
        fail("no such version; list them with: python pb.py versions %s %s" % (args.col, args.id))
    except ValueError as e:
        if isinstance(e, storelib.ReasonRequired):
            explain(store, e, "restore")
        fail("that version cannot be read: %s" % e)
    except storelib.StoreError as e:
        explain(store, e, "restore")
    print("restored %s/%s to %s (rev %s)" % (args.col, args.id, args.version, r["rev"]))
    after_save(store, args.col, args.id)


def cmd_changes(store, args):
    for c in history.read_changes(store, args.n):
        print("%s  %-16s %-7s %s/%s  %s" % (text(c.get("at"))[:19], text(c.get("by")), text(c.get("action")), c["col"], c["id"], text(c.get("title"))))
        removed = [text(x) for x in items(c.get("removed"))]
        if removed:
            print("%21sremoved: %s" % ("", ", ".join(removed)))
        if c.get("reason") or removed or c.get("action") == "delete":
            print("%21swhy: %s" % ("", text(c.get("reason")) or "(no reason recorded)"))


def cmd_export(store, args):
    data, _ = load(store)
    out = store.root / "store.json"
    textio.write_text(out, textio.dumps(data) + "\n")
    print("wrote", out)


def cmd_normalize(store, args):
    """Rewrite every document as canonical bytes. Content stays the same, apart from what the schema derives:
    a connection with no type becomes a main-path connection, stored edge ids go, empty lists and empty optional
    text go. Unreadable files are reported and left alone."""
    changed, bad = [], []
    with store.lock():
        for col in ids.COLS:
            for doc_id, path in store.files(col):
                try:
                    raw = textio.read_bytes(path)
                    doc = textio.loads(raw)
                except (OSError, ValueError) as e:
                    bad.append("%s: %s" % (schema.file_of(col, doc_id), e))
                    continue
                new = schema.upgrade(col, doc)
                try:
                    data = schema.canonical_bytes(col, new)
                except ValueError as e:
                    bad.append("%s: %s" % (schema.file_of(col, doc_id), e))
                    continue
                if data != raw:
                    changed.append(schema.file_of(col, doc_id))
                    if not args.check:
                        textio.atomic_write(path, data)
        if changed and not args.check:
            history.journal(store, "store", "all", "normalize", BY, "%d documents rewritten in canonical form" % len(changed))
        swept = 0 if args.check else store.sweep_temp()
    if swept:
        print("removed %d temp file(s) an interrupted write left behind" % swept)
    for b in bad:
        print("unreadable: " + b, file=sys.stderr)
    if args.check:
        for c in changed:
            print("not canonical: " + c)
        print("%d document(s) not in canonical form" % len(changed))
        sys.exit(1 if changed or bad else 0)
    print("%d document(s) rewritten in canonical form" % len(changed))
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- command line

def build_parser():
    p = argparse.ArgumentParser(prog="pb.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help=argparse.SUPPRESS)  # another playbook folder (the tests use a fixture)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    def doc_args(sp):
        sp.add_argument("col", choices=ids.COLS, metavar="col", help="flows, accounts, folders or parts")
        sp.add_argument("id")

    add("list", cmd_list, help="every flow, grouped")
    sp = add("show", cmd_show, help="one flow as an outline")
    sp.add_argument("flow")
    sp.add_argument("--json", action="store_true", help="the stored document and its revision, as JSON")
    sp = add("search", cmd_search, help="find text")
    sp.add_argument("text", nargs="+")
    sp = add("accounts", cmd_accounts, help="the accounts registry")
    sp.add_argument("platform", nargs="*")
    add("folders", cmd_folders, help="the shared folders")
    add("parts", cmd_parts, help="the parts library")
    sp = add("edited", cmd_edited, help="what the owner changed on the canvas")
    sp.add_argument("since", nargs="?", default="", metavar="YYYY-MM-DD")
    sp = add("catalog", cmd_catalog, help="recipes and the pieces each one uses")
    sp.add_argument("recipe", nargs="?")
    sp.add_argument("--pieces", action="store_true", help="what each kind of piece is for, and where a new lesson goes")
    sp = add("check", cmd_check, help="schema and lint")
    sp.add_argument("--strict", action="store_true", help="warnings fail too")
    sp.add_argument("-w", "--warnings", action="store_true", help="list every warning")
    sp.add_argument("--rule", action="append", help="only list this rule (repeatable)")
    sp.add_argument("--paths", action="store_true", help="list folders that are not on this machine")
    sp = add("rev", cmd_rev, help="a document's revision")
    doc_args(sp)
    sp = add("put", cmd_put, help="save a document from a JSON file")
    doc_args(sp)
    sp.add_argument("file")
    sp.add_argument("--reason")
    sp.add_argument("--base", metavar="REV")
    sp = add("split", cmd_split, help="split a flow into single-job flows by a plan (lib/split.py)")
    sp.add_argument("flow")
    sp.add_argument("plan", help="JSON: {\"flows\": {\"<new-id>\": {\"name\", \"summary\", \"entry\", \"start\", \"nodes\"}}}")
    sp.add_argument("--reason")
    sp.add_argument("--dry-run", action="store_true", help="check the result and list the flows; save nothing")
    sp = add("delete", cmd_delete, help="move a document to the trash")
    doc_args(sp)
    sp.add_argument("--reason")
    sp.add_argument("--base", metavar="REV")
    sp = add("versions", cmd_versions, help="saved versions of a document")
    doc_args(sp)
    sp = add("restore", cmd_restore, help="put a saved version back")
    doc_args(sp)
    sp.add_argument("version")
    sp.add_argument("--reason")
    sp = add("changes", cmd_changes, help="the latest changes")
    sp.add_argument("n", nargs="?", type=int, default=40)
    add("export", cmd_export, help="write store.json for a view-only copy")
    sp = add("normalize", cmd_normalize, help="rewrite every document in canonical form")
    sp.add_argument("--check", action="store_true", help="only report; exit 1 if anything would change")
    return p


def main(argv=None):
    textio.safe_stdio()
    args = build_parser().parse_args(argv)
    store = storelib.Store(args.root or HERE)
    try:
        if not getattr(args, "fn", None):
            return cmd_list(store, args)
        return args.fn(store, args)
    except textio.LockTimeout:
        fail("not saved: the playbook is busy (another process has held its lock for %g s); try again"
             % store.lock_timeout)
    except OSError as e:  # a full disk, a read-only folder: say which file, never a traceback
        fail("failed: %s: %s" % (getattr(e, "filename", None) or store.root, e.strerror or e))


if __name__ == "__main__":
    main()
