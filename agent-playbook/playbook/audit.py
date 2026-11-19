"""Audit log of everything posted, scheduled, edited or deleted on the owner's behalf.

One append-only file, playbook/audit/posts.jsonl. Lines are never rewritten:
  {"kind": "item", "id": ..., ...}     one posted thing (post, reply, comment, article, video...)
  {"kind": "update", "of": <id>, ...}  a later change to it (verified, deleted, removed, edited...)
The canvas History tab and `audit.py show` merge them into each item's current state and timeline.

  python audit.py add --platform X --account @handle --action reply --project myapp \\
      --url https://x.com/... --target https://x.com/... --text-file reply.txt [--media a.png] [--status live]
      [--place "r/somesub"] [--promo | --no-promo] [--at 2026-11-08T14:02] [--force]
      needs --platform, --action and the words (--text or --text-file), or at least --title or --url.
      Refused as a duplicate when the same platform, account and action already has this link, or this text
      within 24 hours; --force logs it anyway.
  python audit.py update <id> --status live --verified "checked live 11-08 14:02" [--note "..."]
  python audit.py update <id> --status deleted --reason "why it was taken down"
      deleted or removed always needs --reason (so does add with --action delete or one of those statuses)
  python audit.py recent --platform X --account @handle [--place PLACE] [-n 4]
      the last N things posted there, newest first: when, kind, project, promo or not, status, the first words
  python audit.py today [--platform X]
      counts for the local day per platform, account and kind (reply, post, comment...), and today's pace flags
  python audit.py gap --platform X --account @handle --action reply [--place PLACE]
      before a send: READY, or WAIT and how long (exit 1). Replies 30 s apart, posts 10 min, Facebook groups by
      their groups.tsv gap. add records a pace_flag on any send that came sooner. add and gap refuse an account
      the registry (store/accounts) doesn't know for that platform, and a send from an account it marks retired or
      skip; add --force logs it anyway.
  QC gate (where playbook/qc/filters.json exists): add refuses a reply, comment, post, quote, article, video, reel,
      short or carousel that went out or is scheduled unless playbook/qc.py check passed the same words for the same
      platform and account in the last 72 hours. Replies, comments, posts and quotes must carry their words
      (--text-file); the others are checked whenever they do. The receipt id is recorded on the item.
      --override "why" logs it anyway; run.py finish lists it.
  python audit.py list [--platform X] [--project myapp] [--since 2026-11-01] [--status live] [-n 50]
  python audit.py show <id>            the item, its full text and every update
  python audit.py search <text>
  python audit.py stats                counts per platform, project, action and status
  python audit.py import <file.jsonl>  append normalized items, skipping ones already logged; all or nothing
Statuses: live, scheduled, pending, skipped, failed, deleted, removed, unknown.
Text: --text "..." or --text-file path (the full text exactly as posted). Never put secrets in the log.
Exit codes: 0 done, 1 refused or failed (the reason is on stderr), 2 the command line was wrong."""
import argparse
import datetime as dt
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from lib import auditlog, qcengine, textio  # noqa: E402
from lib.auditlog import AuditError, GONE  # noqa: E402,F401

LOG = HERE / "audit" / "posts.jsonl"


def paths(root=None):
    """(log file, lock file) for a playbook folder."""
    base = pathlib.Path(root).resolve() if root else HERE
    return base / "audit" / "posts.jsonl", base / "history" / ".audit.lock"


def read(log=None):
    """The log merged into items with their update history (kept for callers that import this module)."""
    return auditlog.read(log or LOG)[0]


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def load(log):
    items, problems = auditlog.read(log)
    for p in problems:
        print("warning: %s" % p, file=sys.stderr)
    return items


SENT = ("live", "pending")
REPLY_ACTIONS = ("reply", "comment")
POST_ACTIONS = ("post", "article", "video", "reel", "short", "carousel", "quote")
REPLY_GAP_S = 30          # flow anti-spam: 30 to 90 seconds between replies
POST_GAP_S = 600          # flow anti-spam: originals minutes apart
GROUP_HOURS = {"approval": 6.0, "open": 4.0, "own": 1.0, "strict": 4.0}  # same defaults as scripts/pace.py


def base_dir(root=None):
    return pathlib.Path(root).resolve() if root else HERE


STOPPED = ("retired", "skip")  # account statuses nothing goes out from


def _accounts(base):
    """(platform lower case, [names as written], status) for each readable record in store/accounts."""
    out = []
    for f in sorted((base / "store" / "accounts").glob("*.json")):
        try:
            a = textio.loads(textio.read_bytes(f))
        except (OSError, ValueError):
            continue
        if not isinstance(a, dict):
            continue
        names = [" ".join(str(a.get(k) or "").split()) for k in ("handle", "label")]
        out.append((" ".join(str(a.get("platform") or "").split()).lower(), [n for n in names if n], str(a.get("status") or "")))
    return out


def registry(base):
    """{platform (lower case): {name (lower case): name as written}} from store/accounts: handles and labels.
    Facebook group posts go out as a Facebook identity, so that platform also accepts the Facebook names."""
    out = {}
    for platform, names, _ in _accounts(base):
        known = out.setdefault(platform, {})
        for v in names:
            known[v.lower()] = v
    if "facebook" in out:
        out.setdefault("facebook group", {}).update(out["facebook"])
    return out


def account_statuses(base):
    """{platform (lower case): {name (lower case): set of the statuses its records have}} from store/accounts."""
    out = {}
    for platform, names, status in _accounts(base):
        for v in names:
            out.setdefault(platform, {}).setdefault(v.lower(), set()).add(status)
    if "facebook" in out:
        group = out.setdefault("facebook group", {})
        for k, v in out["facebook"].items():
            group.setdefault(k, set()).update(v)
    return out


def status_problem(rec, statuses):
    """A message when every registry record for this account marks it retired or skip; None otherwise."""
    acct = " ".join(str(rec.get("account") or "").split())
    have = statuses.get(str(rec.get("platform") or "").lower(), {}).get(acct.lower())
    if not have or not have <= set(STOPPED):
        return None
    return ("--account %r is marked %s in the accounts registry for %s: nothing goes out from it (python playbook/pb.py "
            "accounts %s); --force logs it anyway." % (acct, "/".join(sorted(have)), rec["platform"], rec["platform"]))


def account_problem(rec, reg):
    """A message when the account isn't in the registry for that platform; None when it is, or when the platform
    has no registry entries to check against."""
    names = reg.get(rec["platform"].lower())
    if not names:
        return None
    acct = " ".join(str(rec.get("account") or "").split())
    if acct.lower() in names:
        return None
    return ("--account %r isn't in the accounts registry for %s. Use one of: %s (python playbook/pb.py accounts %s); "
            "--force logs it anyway." % (acct, rec["platform"], ", ".join(sorted(set(names.values()))), rec["platform"]))


def kind_of(action):
    a = (action or "").lower()
    return "reply" if a in REPLY_ACTIONS else "post" if a in POST_ACTIONS else None


def group_hours(base, place):
    """Minimum hours between posts in a Facebook group, from playbook/state/groups.tsv (slug, name, mode, min_hours)."""
    path = base / "state" / "groups.tsv"
    try:
        lines = textio.read_bytes(path).decode("utf-8-sig", "replace").splitlines()
    except OSError:
        return None
    for line in lines:
        cols = line.split("\t")
        if line.startswith("#") or len(cols) < 3 or cols[0].strip().lower() != place.strip().lower():
            continue
        try:
            return float(cols[3]) if len(cols) > 3 and cols[3].strip() else GROUP_HOURS.get(cols[2].strip())
        except ValueError:
            return GROUP_HOURS.get(cols[2].strip())
    return None


def min_gap(rec, base):
    """(seconds, what) the pacing rules ask between this send and the previous one of its kind, or (None, None)."""
    kind = kind_of(rec.get("action"))
    if kind == "reply":
        return REPLY_GAP_S, "reply"
    if kind == "post":
        if rec["platform"].lower() == "facebook group" and rec.get("place"):
            hours = group_hours(base, rec["place"])
            if hours:
                return hours * 3600, "post in %s" % rec["place"]
        return POST_GAP_S, "post"
    return None, None


def last_sent(items, rec, kind):
    best = None
    for i in items:
        if i.get("status") not in SENT or i.get("at_precision") != "exact" or kind_of(i.get("action")) != kind:
            continue
        if i.get("platform", "").lower() != rec["platform"].lower() or i.get("account", "").lower() != str(rec.get("account") or "").lower():
            continue
        if kind == "post" and rec["platform"].lower() == "facebook group" and (i.get("place") or "").lower() != (rec.get("place") or "").lower():
            continue
        t = auditlog.when_or_none(i.get("at"))
        if t is not None and (best is None or t > best[0]):
            best = (t, i)
    return best


def pace_problem(items, rec, base):
    """A message when this send came sooner after the previous one than the pacing rules allow, else None."""
    if rec.get("status") not in SENT or rec.get("at_precision") != "exact":
        return None
    gap, what = min_gap(rec, base)
    if not gap:
        return None
    prev = last_sent(items, rec, kind_of(rec["action"]))
    t = auditlog.when_or_none(rec["at"])
    if prev is None or t is None:
        return None
    secs = (t - prev[0]).total_seconds()
    if 0 <= secs < gap:
        return "sent %s after the previous %s (%s); the minimum is %s" % (span(secs), what, prev[1]["id"], span(gap))
    return None


def span(secs):
    secs = int(secs)
    return "%d s" % secs if secs < 120 else "%d min" % (secs // 60) if secs < 7200 else "%.1f h" % (secs / 3600.0)


def open_run(base):
    for f in sorted((base / "state" / "runs").glob("*.json"), reverse=True):
        try:
            run = textio.loads(textio.read_bytes(f))
        except (OSError, ValueError):
            continue
        if isinstance(run, dict) and run.get("state") == "open":
            return f
    return None


def fmt(it, full=False):
    t = (it.get("text") or it.get("title") or "").replace("\n", " ")
    head = "%-16s %-12s %-8s %-9s %-10s %s" % (it.get("at", "")[:16], it.get("platform", ""), it.get("action", ""),
                                               it.get("status", ""), it.get("project", ""), it["id"])
    why = "\n    why %s: %s" % (it.get("status"), it.get("reason") or "(no reason recorded)") if it.get("status") in GONE else ""
    if not full:
        return head + "\n    " + (t[:120] + ("…" if len(t) > 120 else "")) + ("\n    %s" % it["url"] if it.get("url") else "") + why
    out = [head]
    for k in ("account", "place", "promo", "url", "target", "title", "scheduled_for", "verified", "reason", "media", "source", "text_source", "notes",
              "qc", "qc_warn", "qc_override", "pace_flag"):
        if it.get(k) not in (None, "", []):
            out.append("  %s: %s" % (k, it[k]))
    out.append("  text:")
    out += ["    " + line for line in (it.get("text") or "(not recorded)").splitlines()]
    for h in it.get("history", []):
        ch = dict((k, v) for k, v in h.items() if k not in ("kind", "of", "at"))
        out.append("  update %s: %s" % (str(h.get("at", ""))[:16], textio.dumps(ch)))
    return "\n".join(out)


QC_TEXT_ACTIONS = ("reply", "comment", "post", "quote")  # these need their exact words, so QC can be matched
QC_WINDOW_H = 72


def qc_receipt(base, rec):
    """The latest QC receipt (playbook/qc.py check) for this text, platform and account within QC_WINDOW_H, or None."""
    path = base / "audit" / "qc.jsonl"
    try:
        text = textio.read_bytes(path).decode("utf-8", "replace")
    except OSError:
        return None
    h, best = qcengine.text_hash(rec["text"]), None
    oldest = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=QC_WINDOW_H)
    for line in textio.split_lines(text):
        if h not in line:  # the hash is hex, written as is: a line without it is not this text's receipt
            continue
        try:
            r = textio.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("hash") != h or str(r.get("platform") or "").lower() != rec["platform"].lower():
            continue
        if str(r.get("account") or "").lower() != str(rec.get("account") or "").lower():
            continue
        t = auditlog.when_or_none(r.get("at") or "")
        if t is not None and t >= oldest:
            best = r
    return best


def qc_gate(base, rec, override):
    """None when this send may be logged; else why not. Only where QC is set up (playbook/qc/filters.json), and only
    for replies and posts that went out or are scheduled. Records the receipt (or the override) on the item."""
    if not (base / "qc" / "filters.json").exists() or rec.get("status") not in SENT + ("scheduled",) or kind_of(rec.get("action")) is None:
        return None
    why = " ".join(str(override or "").split())[:400]
    if why:
        rec["qc_override"] = why
        return None
    if not rec.get("text"):
        if rec["action"] in QC_TEXT_ACTIONS:
            return ("a %s needs its exact words (--text-file) so its QC receipt can be matched; or --override \"why\" "
                    "(the run report lists it)" % rec["action"])
        return None
    r = qc_receipt(base, rec)
    hint = ("python playbook/qc.py check --platform %r --account %r --action %s --text-file <the same file> ..."
            % (rec["platform"], rec.get("account") or "", rec["action"]))
    if r is None:
        return ("no QC receipt for this text on %s %s in the last %d hours. Check it first: %s. Or --override \"why\" "
                "(the run report lists it)." % (rec["platform"], rec.get("account") or "", QC_WINDOW_H, hint))
    findings = [f for f in r.get("findings") or [] if isinstance(f, dict)] if isinstance(r.get("findings"), list) else []
    if r.get("verdict") not in ("pass", "warn", "block") or not r.get("id"):
        # only qc.py writes receipts; one without a verdict it gives or an id was damaged or hand-made: it clears nothing
        return ("the QC receipt for this text can't be read (no verdict or id). Check it again: %s. Or --override "
                "\"why\" (the run report lists it)." % hint)
    if r.get("verdict") == "block":
        what = "; ".join("%s: %s" % (f.get("id"), str(f.get("message", ""))[:80]) for f in findings
                         if (f.get("severity") == "block" and "waived" not in f) or (f.get("severity") == "ask" and "answered" not in f))
        return "QC blocked this text (%s): %s. Fix it and check again, or --override \"why\"." % (r.get("id"), what)
    rec["qc"] = r.get("id")
    warns = [f.get("id") for f in findings if f.get("severity") == "warn"]
    if warns:
        rec["qc_warn"] = warns
    return None


def cmd_add(args, log, lock):
    kw = dict((k, getattr(args, k, None)) for k in auditlog.FIELDS)
    if args.text_file:
        try:
            kw["text"] = textio.read_bytes(args.text_file).decode("utf-8-sig").replace("\r\n", "\n").rstrip("\n")
        except OSError as e:
            fail("cannot read %s: %s" % (args.text_file, e.strerror or e))
        except UnicodeDecodeError:
            fail("%s is not UTF-8 text" % args.text_file)
    kw["media"] = args.media or []
    kw["place"] = args.place
    kw["promo"] = args.promo
    base = base_dir(args.root)
    with textio.FileLock(lock):
        items = auditlog.read(log)[0]
        kw["platform"] = auditlog.canonical_platform(kw["platform"], auditlog.platform_spellings(items))
        rec = auditlog.build_item(kw)
        dup = auditlog.find_duplicate(items, rec)
        if dup is not None and not (args.force and dup["id"] != rec["id"]):
            fail("already logged as %s (%s %s, %s). Not logged again; use --force if this really is another one."
                 % (dup["id"], dup.get("platform", ""), dup.get("action", ""), dup.get("at", "")[:16]))
        problem = account_problem(rec, registry(base))
        if not problem and rec.get("status") in SENT + ("scheduled",):
            problem = status_problem(rec, account_statuses(base))
        if problem and not args.force:
            fail(problem)
        flag = pace_problem(items, rec, base)
        if flag:
            rec["pace_flag"] = flag
        gate = qc_gate(base, rec, args.override)
        if gate:
            fail(gate)
        auditlog.append(log, [rec], None)
    print(rec["id"])
    if flag:
        print("pace flag: %s. It is recorded on the item; explain it in the report. Logging a batch afterwards? "
              "Pass --at with each send time." % flag, file=sys.stderr)
    if rec.get("qc_override"):
        print("QC override recorded: %s. run.py finish lists it for the report." % rec["qc_override"], file=sys.stderr)
    if rec.get("status") in SENT + ("scheduled",) and open_run(base) is None:
        print("note: no run checklist is open: python scripts/run.py start --recipe <id> (run.py recipes)", file=sys.stderr)


def cmd_gap(args, log, lock):
    """READY, or WAIT with how long, before a send: the same pacing rules add flags afterwards."""
    base = base_dir(args.root)
    items = load(log)
    rec = {"platform": auditlog.canonical_platform(args.platform, auditlog.platform_spellings(items)), "account": args.account,
           "action": args.action.lower(), "place": args.place or "", "status": "live", "at_precision": "exact", "at": auditlog.now()}
    problem = account_problem(rec, registry(base)) or status_problem(rec, account_statuses(base))
    if problem:
        fail(problem)
    gap, what = min_gap(rec, base)
    if not gap:
        print("READY (no pacing rule for %r)" % args.action)
        return
    prev = last_sent(items, rec, kind_of(rec["action"]))
    if prev is None:
        print("READY (nothing earlier of this kind logged for %s %s)" % (rec["platform"], args.account))
        return
    waited = (auditlog.when_or_none(rec["at"]) - prev[0]).total_seconds()
    if waited >= gap:
        print("READY (%s since the last %s, %s)" % (span(waited), what, prev[1]["id"]))
        return
    print("WAIT %s more: the last %s (%s) was %s ago; the minimum is %s" % (span(gap - waited), what, prev[1]["id"], span(waited), span(gap)))
    sys.exit(1)


def cmd_update(args, log, lock):
    items = load(log)
    hit = [i for i in items if i["id"] == args.id]
    if not hit:
        fail("no item %s" % args.id)
    rec = auditlog.build_update(args.id, dict((k, getattr(args, k, None)) for k in auditlog.UPDATE_FIELDS))
    auditlog.append(log, [rec], lock)
    print("updated", args.id)


def cmd_list(args, log, lock):
    since = auditlog.parse_when(args.since)[0] if args.since else None
    status = auditlog.clean_status(args.status) if args.status else None
    rows = []
    for i in load(log):
        if any(getattr(args, k) and str(i.get(k, "")).lower() != getattr(args, k).strip().lower() for k in ("platform", "project", "action")):
            continue
        if status and i.get("status") != status:
            continue
        if since:
            t = auditlog.when_or_none(i.get("at"))
            if t is None or t < since:
                continue
        rows.append(i)
    for it in sorted(rows, key=auditlog.sort_key)[-args.n:] if args.n > 0 else []:
        print(fmt(it))
    print("\n%d item(s)" % len(rows))


def cmd_show(args, log, lock):
    if not args.id.strip():
        fail("give the item's id (or the end of it)")
    items = load(log)
    hit = [i for i in items if i["id"] == args.id] or [i for i in items if i["id"].endswith(args.id)]
    if not hit:
        fail("not found")
    if len(hit) > 1:
        fail("%d items end in %s: %s" % (len(hit), args.id, ", ".join(i["id"] for i in hit[:5])))
    print(fmt(hit[0], full=True))


def cmd_search(args, log, lock):
    t = " ".join(args.text).lower()
    for it in load(log):
        if t in textio.dumps(auditlog.public(it)).lower():
            print(fmt(it))


def cmd_stats(args, log, lock):
    rows = load(log)
    days = sorted(i["at"][:10] for i in rows if auditlog.when_or_none(i.get("at")))
    print("%d items" % len(rows) + (", %s to %s" % (days[0], days[-1]) if days else ""))
    for k in ("platform", "project", "action", "status"):
        c = {}
        for i in rows:
            c[i.get(k) or "?"] = c.get(i.get(k) or "?", 0) + 1
        print("\n%s: " % k + ", ".join("%d %s" % (n, v) for v, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))))
    gone = [i for i in rows if i.get("status") in GONE]
    if gone:
        print("\ndeleted or removed: %d, %d with no reason recorded (logged before reasons were required)"
              % (len(gone), sum(1 for i in gone if not i.get("reason"))))


def cmd_import(args, log, lock):
    try:
        text = textio.read_bytes(args.file).decode("utf-8-sig")
    except OSError as e:
        fail("cannot read %s: %s" % (args.file, e.strerror or e))
    except UnicodeDecodeError:
        fail("%s is not UTF-8 text" % args.file)
    recs, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            src = textio.loads(line)
            if not isinstance(src, dict):
                raise ValueError("not an object")
            kw = dict((k, src.get(k)) for k in auditlog.FIELDS + auditlog.OPTIONAL)
            rec = auditlog.build_item(kw)
            rec["imported"] = True
            recs.append(rec)
        except ValueError as e:
            errors.append("line %d: %s" % (n, e))
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        fail("nothing imported: fix the %d line(s) above and run it again" % len(errors))
    with textio.FileLock(lock):
        have = set(i["id"] for i in auditlog.read(log)[0])
        new = []
        for rec in recs:
            if rec["id"] not in have:
                have.add(rec["id"])
                new.append(rec)
        auditlog.append(log, new, None)
    print("imported %d item(s)" % len(new))


def cmd_recent(args, log, lock):
    plat, acct = args.platform.strip().lower(), (args.account or "").strip().lower()
    rows = [i for i in load(log)
            if i.get("platform", "").lower() == plat
            and (not acct or i.get("account", "").lower() == acct)
            and auditlog.matches_place(i, args.place)]
    rows = sorted(rows, key=auditlog.sort_key, reverse=True)[:max(args.n, 0)]
    for it in rows:
        promo = {True: "promo", False: "not promo"}.get(it.get("promo"), "promo ?")
        words = " ".join((it.get("text") or it.get("title") or "").split())
        where = it.get("place") or it.get("target") or ""
        print("%-16s %-8s %-10s %-9s %-9s %s" % (it.get("at", "")[:16], it.get("action", ""), it.get("project", "") or "-", promo,
                                                 it.get("status", ""), it["id"]))
        if where:
            print("    in: %s" % where)
        print("    %s" % (words[:140] + ("…" if len(words) > 140 else "") if words else "(text not recorded)"))
    print("%d item(s)" % len(rows))


def cmd_today(args, log, lock):
    today = dt.datetime.now().astimezone().date()
    plat = (args.platform or "").strip().lower()
    counts, flags = {}, []
    for i in load(log):  # one read: the counts and the pace flags come from the same pass
        if auditlog.local_day(i) != today or (plat and i.get("platform", "").lower() != plat):
            continue
        if i.get("pace_flag"):
            flags.append(i)
        key = (i.get("platform") or "?", i.get("account") or "?")
        counts.setdefault(key, {})
        kind = i.get("action") or "?"
        counts[key][kind] = counts[key].get(kind, 0) + 1
    print("%s (local day)" % today.isoformat())
    total = 0
    for (p, a), kinds in sorted(counts.items()):
        n = sum(kinds.values())
        total += n
        print("%-15s %-28s %3d  %s" % (p, a, n, ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    print("%d item(s) today" % total)
    if flags:
        print("\npace flags today (explain each in the report):")
        for i in flags:
            print("  %s  %s" % (i["id"], i["pace_flag"]))


def build_parser():
    p = argparse.ArgumentParser(prog="audit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help=argparse.SUPPRESS)  # another playbook folder (the tests use a fixture)
    sub = p.add_subparsers(dest="cmd", metavar="command")
    a = sub.add_parser("add")
    for k in auditlog.FIELDS:
        if k not in ("text", "media"):
            a.add_argument("--" + k.replace("_", "-"), dest=k)
    a.add_argument("--text")
    a.add_argument("--text-file")
    a.add_argument("--media", action="append")
    a.add_argument("--place")
    a.add_argument("--promo", dest="promo", action="store_true", default=None)
    a.add_argument("--no-promo", dest="promo", action="store_false")
    a.add_argument("--force", action="store_true")
    a.add_argument("--override", help="log a reply or post without a passing QC receipt; say why (it goes in the report)")
    a.set_defaults(fn=cmd_add)
    u = sub.add_parser("update")
    u.add_argument("id")
    for k in auditlog.UPDATE_FIELDS:
        u.add_argument("--" + k.replace("_", "-"), dest=k)
    u.set_defaults(fn=cmd_update)
    l = sub.add_parser("list")
    for k in ("platform", "project", "status", "action", "since"):
        l.add_argument("--" + k)
    l.add_argument("-n", type=int, default=50)
    l.set_defaults(fn=cmd_list)
    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    q = sub.add_parser("search")
    q.add_argument("text", nargs="+")
    q.set_defaults(fn=cmd_search)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    im = sub.add_parser("import")
    im.add_argument("file")
    im.set_defaults(fn=cmd_import)
    r = sub.add_parser("recent")
    r.add_argument("--platform", required=True)
    r.add_argument("--account")
    r.add_argument("--place")
    r.add_argument("-n", type=int, default=4)
    r.set_defaults(fn=cmd_recent)
    t = sub.add_parser("today")
    t.add_argument("--platform")
    t.set_defaults(fn=cmd_today)
    g = sub.add_parser("gap")
    g.add_argument("--platform", required=True)
    g.add_argument("--account", required=True)
    g.add_argument("--action", required=True)
    g.add_argument("--place")
    g.set_defaults(fn=cmd_gap)
    return p


def main(argv=None):
    textio.safe_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_usage(sys.stderr)
        sys.exit(2)
    log, lock = paths(args.root)
    try:
        args.fn(args, log, lock)
    except AuditError as e:
        fail(str(e))
    except textio.LockTimeout:
        fail("the audit log is busy (another process holds its lock); try again")
    except OSError as e:  # a full disk, a read-only folder: say which file, never a traceback
        fail("cannot use %s: %s" % (getattr(e, "filename", None) or log, e.strerror or e))


if __name__ == "__main__":
    main()
