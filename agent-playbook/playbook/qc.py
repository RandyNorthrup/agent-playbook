"""QC every draft before it is sent: the filters in qc/filters.json, a verdict, and a receipt audit.py add looks for.

  python playbook/qc.py check --platform X --account @handle --action reply --text-file draft.txt \\
      --target https://x.com/... [--target-author NAME] [--place PLACE] [--project id] [--promo | --no-promo] \\
      [--recipe news-share] [--media a.png] [--scheduled-for 2026-11-12T10:15] [--ok facts-read="README, this session"]
      BLOCK  can't go out: fix the draft and check again (or audit.py add --override "why", which the report lists)
      ASK    a judgment only you can make: answer with --ok <id>="evidence" and check again
      WARN   can go out; it is on the receipt and in the run report
      Exit 0 with a receipt id when nothing blocks, 1 when something does, 2 when the command line is wrong.
      A reply needs --target (the link it answers) and the author: from the link, or --target-author.
      --recipe defaults to the open run's recipe (scripts/run.py).
  python playbook/qc.py filters [--platform X --action reply --recipe news-share]   which filters apply, and why
  python playbook/qc.py explain <filter-id>    one filter in full: what it checks, where it came from, its examples
  python playbook/qc.py selftest               every filter against its own examples (the test suite runs this too)
  python playbook/qc.py receipts [-n 20]       the latest receipts
Add a filter or a list entry: qc/filters.json and qc/lists.json (how: the "about" line at the top of each)."""
import argparse
import json
import pathlib
import sys
import uuid

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import audit  # noqa: E402
from lib import auditlog, qcengine, textio  # noqa: E402
from lib.qcengine import QCError  # noqa: E402

try:
    sys.path.insert(0, str(HERE.parent / "scripts"))
    from common import x_weighted  # noqa: E402
except ImportError:  # the playbook copied without scripts/: plain length
    x_weighted = len
finally:
    sys.path.pop(0)


def receipts_path(base):
    return base / "audit" / "qc.jsonl", base / "history" / ".qc.lock"


def make_context(base, history, lists, registry=None):
    """registry: {platform: [names]} standing in for store/accounts (a filter example carries its own, so the
    selftest gives the same answer on every machine)."""
    if registry is None:
        reg, statuses = audit.registry(base), audit.account_statuses(base)
    else:
        reg, statuses = dict((p.lower(), dict((n.lower(), n) for n in names)) for p, names in registry.items()), {}

    def account_problem(d):  # unknown for the platform, or marked retired or skip: the same rule as audit.py add
        rec = {"platform": d.platform, "account": d.account}
        return audit.account_problem(rec, reg) or audit.status_problem(rec, statuses)

    def pace_wait(d):
        rec = {"platform": d.platform, "account": d.account, "action": d.action, "place": d.place,
               "status": "live", "at_precision": "exact", "at": d.at}
        gap, what = audit.min_gap(rec, base)
        kind = audit.kind_of(d.action)
        prev = audit.last_sent(history, rec, kind) if gap and kind else None
        t = auditlog.when_or_none(d.at)
        if not prev or t is None:
            return None
        waited = (t - prev[0]).total_seconds()
        if 0 <= waited < gap:
            return "the last %s (%s) was %s before; the minimum is %s" % (what, prev[1]["id"], audit.span(waited), audit.span(gap))
        return None

    return qcengine.Context(history, lists, account_problem, pace_wait, x_weighted)


def open_recipe(base):
    f = audit.open_run(base)
    if f is None:
        return ""
    try:
        run = textio.loads(textio.read_bytes(f))
    except (OSError, ValueError):
        return ""
    recipe = (run.get("recipe") or run.get("kind") or "") if isinstance(run, dict) else ""
    return recipe if isinstance(recipe, str) else ""


def parse_ok(pairs):
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise QCError("--ok takes <filter-id>=\"evidence\", got %r" % p)
        out[k.strip()] = v.strip()
    return out


def cmd_check(a, base):
    filters, lists = qcengine.load(base)
    if bool(a.text) == bool(a.text_file):
        raise QCError("give the draft with --text-file (or --text), once")
    text = a.text
    if a.text_file:
        try:
            text = textio.read_bytes(a.text_file).decode("utf-8-sig")
        except OSError as e:
            raise QCError("cannot read %s: %s" % (a.text_file, e.strerror or e))
        except UnicodeDecodeError:
            raise QCError("%s is not UTF-8 text" % a.text_file)
    if not qcengine.norm_text(text).strip():
        raise QCError("the draft is empty")
    items = audit.load(audit.paths(a.root)[0])
    platform = auditlog.canonical_platform(a.platform, auditlog.platform_spellings(items))
    at = auditlog.now()
    if a.scheduled_for:
        try:
            at = auditlog.parse_when(a.scheduled_for)[0].isoformat(timespec="seconds")
        except auditlog.AuditError as e:
            raise QCError(str(e))
    ok = parse_ok(a.ok)
    known = set(f["id"] for f in filters)
    for k in ok:
        if k not in known:
            raise QCError("--ok %s: no filter has that id (python playbook/qc.py filters)" % k)
    d = qcengine.Draft(text, platform, a.account, a.action, a.place, a.project, a.promo, a.recipe or open_recipe(base),
                       a.target, a.target_author, a.media, ok, at)
    if d.kind == "reply" and not (d.target and d.target_author):
        raise QCError("a reply needs --target (the link it answers) and its author: the link shows it on X, Bluesky, "
                      "Threads and Instagram; elsewhere add --target-author \"Name\"")
    if d.kind is None:
        print("note: %r isn't a reply or post action; only the text checks apply" % d.action, file=sys.stderr)
    ctx = make_context(base, items, lists)
    rows = qcengine.run(filters, d, ctx)
    verdict = qcengine.verdict(rows)
    order = {"block": 0, "ask": 1, "warn": 2}
    for r in sorted(rows, key=lambda r: order[r["severity"]]):
        tag = r["severity"].upper()
        if "answered" in r:
            tag, extra = "OK", "  answered: %s" % r["answered"]
        elif "waived" in r:
            tag, extra = "OK", "  waived: %s" % r["waived"]
        else:
            extra = "  (answer: --ok %s=\"...\")" % r["id"] if r["severity"] == "ask" else ""
        print("%-6s %-20s %s%s" % (tag, r["id"], r["message"], extra))
    # random, not derived from the words and the second: checking the same words twice in one second (again with
    # --ok) must give two receipts, each with its own verdict
    rid = "qc-%s-%s" % (at[:19].replace(":", "").replace("-", ""), uuid.uuid4().hex[:6])
    receipt = {"kind": "qc", "id": rid, "at": auditlog.now(), "hash": qcengine.text_hash(d.text), "platform": d.platform,
               "account": d.account, "action": d.action, "place": d.place, "project": d.project, "recipe": d.recipe,
               "target": d.target, "verdict": verdict, "findings": rows, "words": d.text[:80]}
    log, lock = receipts_path(base)
    textio.append_lines(log, [textio.dumps(receipt)], lock)
    projects = ctx.projects_in(d)
    if projects and not d.project:
        print("note: it names %s; pass --project so its never-say list and the promo rules apply by name" % ", ".join(projects), file=sys.stderr)
    if verdict == "block":
        print("BLOCKED (%s): fix the draft and check again; audit.py add refuses this text unless --override \"why\"" % rid)
        return 1
    print("%s receipt %s: send it, then audit.py add with the same text" % ("PASS" if verdict == "pass" else "PASS with warnings", rid))
    return 0


def describe(f):
    a = f.get("applies") or {}
    where = ", ".join("%s=%s" % (k, "/".join(v) if isinstance(v, list) else v) for k, v in sorted(a.items())) or "everything"
    return "%-20s %-6s %-15s %-40s %s" % (f["id"], f["severity"], f["type"], where[:40], f["message"][:90])


def cmd_filters(a, base):
    filters, lists = qcengine.load(base)
    if a.platform or a.action or a.recipe:
        d = qcengine.Draft("x", a.platform or "", "", a.action or "", recipe=a.recipe or "", promo=a.promo)
        ctx = qcengine.Context([], lists)
        filters = [f for f in filters if qcengine.applies(f, d, ctx)]
    for f in filters:
        print(describe(f))
    return 0


def cmd_explain(a, base):
    filters, _ = qcengine.load(base)
    for f in filters:
        if f["id"] == a.id:
            print(json.dumps(f, ensure_ascii=False, indent=1))
            return 0
    raise QCError("no filter %r: python playbook/qc.py filters" % a.id)


def cmd_selftest(a, base):
    filters, lists = qcengine.load(base)
    fails = qcengine.selftest(filters, lists, lambda ex: make_context(base, ex.get("history", []), lists, ex.get("registry", {})))
    for f in fails:
        print("FAIL " + f)
    n = sum(len((f.get("examples") or {}).get(k, [])) for f in filters for k in ("bad", "good"))
    print("%d filters, %d examples: %s" % (len(filters), n, "all pass" if not fails else "%d failed" % len(fails)))
    return 1 if fails else 0


def cmd_receipts(a, base):
    log, _ = receipts_path(base)
    try:
        lines = textio.split_lines(textio.read_bytes(log).decode("utf-8", "replace"))
    except OSError:
        lines = []
    for line in lines[-a.n:] if a.n > 0 else []:
        try:
            r = textio.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        found = [f for f in r.get("findings") or [] if isinstance(f, dict)] if isinstance(r.get("findings"), list) else []
        print("%s %-6s %-14s %-24s %s  %s" % (str(r.get("at", ""))[:16], r.get("verdict", "?"), r.get("platform", ""),
                                             str(r.get("account", ""))[:24], r.get("id", "?"),
                                             ", ".join("%s:%s" % (f.get("severity"), f.get("id")) for f in found) or "-"))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="qc.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--platform", required=True)
    c.add_argument("--account", required=True)
    c.add_argument("--action", required=True)
    c.add_argument("--text")
    c.add_argument("--text-file")
    c.add_argument("--place", default="")
    c.add_argument("--project", default="")
    c.add_argument("--promo", dest="promo", action="store_true", default=None)
    c.add_argument("--no-promo", dest="promo", action="store_false")
    c.add_argument("--recipe", default="")
    c.add_argument("--target", default="")
    c.add_argument("--target-author", default="")
    c.add_argument("--media", action="append")
    c.add_argument("--scheduled-for")
    c.add_argument("--ok", action="append", metavar="ID=EVIDENCE")
    f = sub.add_parser("filters")
    f.add_argument("--platform")
    f.add_argument("--action")
    f.add_argument("--recipe")
    f.add_argument("--promo", action="store_true", default=None)
    e = sub.add_parser("explain")
    e.add_argument("id")
    sub.add_parser("selftest")
    r = sub.add_parser("receipts")
    r.add_argument("-n", type=int, default=20)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    base = audit.base_dir(a.root)
    try:
        return {"check": cmd_check, "filters": cmd_filters, "explain": cmd_explain, "selftest": cmd_selftest,
                "receipts": cmd_receipts}[a.cmd](a, base)
    except QCError as e:
        print(str(e), file=sys.stderr)
        return 2
    except textio.LockTimeout:
        print("the QC receipts are busy (another process holds their lock); try again", file=sys.stderr)
        return 1
    except OSError as e:  # a full disk, a read-only folder: say which file, never a traceback
        print("cannot use %s: %s" % (getattr(e, "filename", None) or receipts_path(base)[0], e.strerror or e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    textio.safe_stdio()  # a draft with an emoji must not crash the report on a Windows code page
    sys.exit(main())
