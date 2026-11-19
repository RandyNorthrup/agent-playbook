"""QC filters: the machine checks every draft passes before it is sent, and the receipts audit.py add asks for.

A filter is data in qc/filters.json: an id, a type (one of the checks in TYPES below), a severity, where it
applies, its parameters, the message, the incident it came from, and examples: drafts it must catch (bad) and
drafts it must pass (good). `qc.py selftest` runs every example, so a filter that stops catching what it was
written for fails the test suite and `pb.py check`.

Severities:
  block   the draft can't go out; audit.py add refuses its text unless --override "why"
  warn    the draft can go out; the warning is on the receipt and in the run report
  ask     a judgment a script can't make; the agent answers it with --ok <id>="evidence". Unanswered = block.
A block filter marked "waivable" also takes --ok <id>="why" (for example: a link the thread asked for).

Lists the filters read (ignore list, competitors, never-say per project...) are in qc/lists.json.
"""
import datetime as dt
import difflib
import hashlib
import re

from . import auditlog, textio

SEVERITIES = ("block", "warn", "ask")
REPLY_ACTIONS = ("reply", "comment")
POST_ACTIONS = ("post", "article", "video", "reel", "short", "carousel", "quote")
SENT = ("live", "pending", "scheduled")
URL_RE = re.compile(r"https?://\S+", re.I)
HASHTAG_RE = re.compile(r"(?<![\w&])#[^\W\d_][\w]*", re.U)
TLD_RE = re.compile(r"(?<![\w./:@-])([\w-]+\.(md|ai|io|sh|dev|app|com|net|org))\b(?![\w/.-])", re.I)
AUTHOR_RE = (re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/", re.I),
             re.compile(r"bsky\.app/profile/([^/\s]+)/post/", re.I),
             re.compile(r"threads\.(?:net|com)/@([^/\s]+)", re.I),
             re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/(?:p|reel)/", re.I))


class QCError(ValueError):
    """The filters file or a draft is wrong: a plain message for the command line."""


def kind_of(action):
    a = (action or "").lower()
    return "reply" if a in REPLY_ACTIONS else "post" if a in POST_ACTIONS else None


def norm_text(text):
    """The text as audit.py stores it: LF line ends, no trailing newline."""
    return (text or "").replace("\r\n", "\n").rstrip("\n")


def text_hash(text):
    """What a receipt and an audit item are matched on: the words, whitespace collapsed."""
    return hashlib.sha256(" ".join(norm_text(text).split()).encode("utf-8")).hexdigest()[:24]


def norm_url(url):
    raw = (url or "").strip()
    # YouTube watch links carry the video in ?v=: keep it, or every video looks like the same page
    m = re.search(r"youtube\.com/watch\?(?:.*&)?v=([\w-]{11})", raw, re.I) or re.search(r"youtu\.be/([\w-]{11})", raw, re.I)
    if m:
        return "youtube.com/watch?v=" + m.group(1)
    u = raw.lower().split("#")[0].split("?")[0].rstrip("/")
    return re.sub(r"^https?://(www\.|m\.|mobile\.|old\.)?", "", u)


def author_of(url):
    for r in AUTHOR_RE:
        m = r.search(url or "")
        if m:
            return m.group(1).lower()
    return ""


def load(base):
    """(filters, lists) from <base>/qc. Raises QCError when the files are missing or malformed."""
    out = []
    for name in ("filters.json", "lists.json"):
        try:
            out.append(textio.loads(textio.read_bytes(base / "qc" / name)))
        except OSError:
            raise QCError("%s is missing" % (base / "qc" / name))
        except ValueError as e:
            raise QCError("%s is not valid JSON: %s" % (base / "qc" / name, e))
    if not isinstance(out[1], dict):
        raise QCError("qc/lists.json must be an object of named lists")
    filters, lists = out[0].get("filters") if isinstance(out[0], dict) else None, out[1]
    if not isinstance(filters, list):
        raise QCError("qc/filters.json needs a \"filters\" list")
    problems = validate(filters, lists)
    if problems:
        raise QCError("qc/filters.json: " + "; ".join(problems))
    return filters, lists


def _regex_problem(p):
    if not isinstance(p, str):
        return "bad pattern %r (not text)" % (p,)
    try:
        re.compile(p)
    except re.error as e:
        return "bad pattern %r (%s)" % (p, e)
    return None


def _list_problems(lists, as_patterns):
    """qc/lists.json entries the checks read: each pattern compiles, each never-say entry has a pattern and a why.
    as_patterns: the lists a pattern filter matches as regular expressions (others, like ignore, hold names)."""
    out = []
    for name, v in sorted(lists.items()):
        if name == "about":  # the file's own description
            continue
        if name == "never_say":
            if not isinstance(v, dict):
                out.append("lists.json never_say must be an object of project: [entries]")
                continue
            for pid, entries in sorted(v.items()):
                for i, e in enumerate(entries if isinstance(entries, list) else [None]):
                    where = "lists.json never_say.%s[%d]" % (pid, i)
                    if not isinstance(e, dict) or not isinstance(e.get("why"), str):
                        out.append("%s needs a \"pattern\" and a \"why\"" % where)
                        continue
                    why = _regex_problem(e.get("pattern"))
                    if why:
                        out.append("%s: %s" % (where, why))
                    if "severity" in e and e["severity"] not in SEVERITIES:
                        out.append("%s severity must be one of %s" % (where, ", ".join(SEVERITIES)))
        elif name == "projects":
            if not isinstance(v, dict):
                out.append("lists.json projects must be an object of project: {\"names\": [patterns]}")
                continue
            for pid, spec in sorted(v.items()):
                names = spec.get("names", []) if isinstance(spec, dict) else None
                for i, p in enumerate(names if isinstance(names, list) else [None]):
                    why = _regex_problem(p)
                    if why:
                        out.append("lists.json projects.%s.names[%d]: %s" % (pid, i, why))
        elif name in as_patterns:
            if isinstance(v, dict):
                v = list(v.keys())
            if not isinstance(v, list):
                out.append("lists.json %s must be a list of patterns" % name)
                continue
            for i, x in enumerate(v):
                p = x.get("pattern") if isinstance(x, dict) else x
                why = _regex_problem(p)
                if why:
                    out.append("lists.json %s[%d]: %s" % (name, i, why))
        elif isinstance(v, list):
            for i, x in enumerate(v):  # ignore and the like: names, as text or {"pattern": ...}
                if not (isinstance(x, str) or (isinstance(x, dict) and isinstance(x.get("pattern", ""), str))):
                    out.append("lists.json %s[%d] must be text or {\"pattern\": text}" % (name, i))
        elif not isinstance(v, dict):
            out.append("lists.json %s must be a list" % name)
    return out


def validate(filters, lists):
    """Problems with the filters and lists, each naming the filter (or list entry) and the field. Everything a
    check reads is checked here, so a typo in the files is a plain message, not a crash in the middle of a check."""
    problems, seen, as_patterns = [], set(), set()
    for i, f in enumerate(filters):
        if not isinstance(f, dict):
            problems.append("filter #%d must be an object" % (i + 1))
            continue
        if f.get("type") == "pattern":
            as_patterns.update(n for n in (f.get("list"), f.get("and_list")) if isinstance(n, str))
        fid = f.get("id") or "#%d" % (i + 1)
        if not isinstance(fid, str):
            problems.append("filter #%d: the id must be text" % (i + 1))
            continue
        if fid in seen:
            problems.append("%s: duplicate id" % fid)
        seen.add(fid)
        if f.get("type") not in TYPES:
            problems.append("%s: unknown type %r (types: %s)" % (fid, f.get("type"), ", ".join(sorted(TYPES))))
        if f.get("severity") not in SEVERITIES:
            problems.append("%s: severity must be one of %s" % (fid, ", ".join(SEVERITIES)))
        sbk = f.get("severity_by_kind")
        if sbk is not None and (not isinstance(sbk, dict) or any(v not in SEVERITIES for v in sbk.values())):
            problems.append("%s: severity_by_kind maps reply/post to one of %s" % (fid, ", ".join(SEVERITIES)))
        for key in ("list", "and_list"):
            name = f.get(key)
            if name and (not isinstance(name, str) or name not in lists):
                problems.append("%s: %s %r is not in qc/lists.json" % (fid, key, name))
        pats = f.get("patterns", [])
        if not isinstance(pats, list):
            problems.append("%s: patterns must be a list" % fid)
            pats = []
        for p in pats:
            why = _regex_problem(p)
            if why:
                problems.append("%s: %s" % (fid, why))
        a = f.get("applies", {})
        if a is not None and not isinstance(a, dict):
            problems.append("%s: applies must be an object" % fid)
        elif a:
            for key in ("platforms", "kinds", "actions", "recipes"):
                if key in a and not (isinstance(a[key], list) and all(isinstance(x, str) for x in a[key])):
                    problems.append("%s: applies.%s must be a list of names" % (fid, key))
        if not isinstance(f.get("message"), str) or not f.get("message") or not f.get("src"):
            problems.append("%s: needs a message and a src (the incident or rule it came from)" % fid)
        ex = f.get("examples") or {}
        if not isinstance(ex, dict) or not ex.get("bad") or not ex.get("good"):
            problems.append("%s: needs at least one bad and one good example" % fid)
        else:
            for label in ("bad", "good"):
                v = ex.get(label)
                if not isinstance(v, list) or not all(isinstance(e, dict) and isinstance(e.get("text", ""), str) for e in v):
                    problems.append("%s: examples.%s must be a list of drafts (objects with a text)" % (fid, label))
        if f.get("type") == "caps":
            for j, cap in enumerate(f.get("caps") if isinstance(f.get("caps"), list) else [None]):
                if not (isinstance(cap, dict) and isinstance(cap.get("platform"), str) and isinstance(cap.get("max"), int)):
                    problems.append("%s: caps[%d] needs a platform and a max" % (fid, j))
    return problems + _list_problems(lists, as_patterns)


class Draft(object):
    """One thing about to be sent, and what QC knows about where it goes."""

    def __init__(self, text, platform, account, action, place="", project="", promo=None, recipe="", target="",
                 target_author="", media=None, ok=None, at=None):
        self.text = norm_text(text)
        self.platform = (platform or "").strip()
        self.account = (account or "").strip()
        self.action = (action or "").strip().lower()
        self.place = (place or "").strip()
        self.project = (project or "").strip()
        self.promo = promo
        self.recipe = (recipe or "").strip()
        self.target = (target or "").strip()
        self.target_author = (target_author or "").strip() or author_of(target)
        self.media = list(media or [])
        self.ok = dict(ok or {})
        self.at = at or auditlog.now()
        self.kind = kind_of(self.action)

    @classmethod
    def from_example(cls, ex):
        known = ("text", "platform", "account", "action", "place", "project", "promo", "recipe", "target",
                 "target_author", "media", "ok", "at")
        return cls(**dict((k, v) for k, v in ex.items() if k in known))


class Context(object):
    """Everything a check reads besides the draft: the log, the lists, and hooks into audit.py."""

    def __init__(self, history, lists, account_problem=None, pace_wait=None, x_length=None):
        self.history = history
        self.lists = lists
        self.account_problem = account_problem or (lambda d: None)
        self.pace_wait = pace_wait or (lambda d: None)
        self.x_length = x_length or len
        self.apps = lists.get("projects", {})

    def projects_in(self, d):
        """The owner's apps the draft is about: --project, plus any app named in the text."""
        found = []
        if d.project in self.apps:
            found.append(d.project)
        for pid, spec in sorted(self.apps.items()):
            if pid not in found and any(re.search(p, d.text, re.I) for p in spec.get("names", [])):
                found.append(pid)
        return found

    def is_promo(self, d):
        return d.promo if d.promo is not None else bool(self.projects_in(d))

    def item_promo(self, it):
        return it.get("promo") if it.get("promo") is not None else it.get("project") in self.apps

    def sent(self, d, same_account=True, kind=None, days=None, same_day=False):
        """Logged items that went out (or are scheduled), newest first. A scheduled item counts at its scheduled
        time. days: only items within that many days of the draft's time, either side; same_day: only items on
        the draft's local calendar day."""
        mid = when(d.at)
        out = []
        for it in self.history:
            if it.get("status") not in SENT:
                continue
            if same_account and (it.get("account") or "").lower() != d.account.lower():
                continue
            if kind and kind_of(it.get("action")) != kind:
                continue
            t = item_time(it)
            if (days or same_day) and (t is None or mid is None):
                continue
            if days and abs((t - mid).total_seconds()) > days * 86400:
                continue
            if same_day and auditlog.to_local(t).date() != auditlog.to_local(mid).date():
                continue
            out.append((t, it))
        out.sort(key=lambda p: p[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        return [it for _, it in out]


def when(value):
    return auditlog.when_or_none(value) if value else None


def item_time(it):
    if it.get("status") == "scheduled" and when(it.get("scheduled_for")):
        return when(it["scheduled_for"])
    return when(it.get("at"))


def _same_platform(it, d):
    return (it.get("platform") or "").lower() == d.platform.lower()


def _snip(m, text):
    a, b = max(0, m.start() - 20), min(len(text), m.end() + 20)
    return "…" + text[a:b].replace("\n", " ") + "…"


def _list(ctx, name):
    v = ctx.lists.get(name, [])
    return [x if isinstance(x, str) else x.get("pattern", "") for x in v] if isinstance(v, list) else list(v.keys())


# ---- the checks: each returns a list of findings, a finding being a message or (severity, message) ----

def t_pattern(f, d, ctx):
    """Any of `patterns`, or of the named `list`, in the text. With `and_list`, only when that list matches too."""
    pats = list(f.get("patterns", [])) + (_list(ctx, f["list"]) if f.get("list") else [])
    if f.get("unless_own_project") and ctx.projects_in(d):
        return []
    if f.get("and_list") and not any(re.search(p, d.text, re.I) for p in _list(ctx, f["and_list"])):
        return []
    out = []
    for p in pats:
        m = re.search(p, d.text, re.I)
        if m:
            out.append("%s (%s)" % (f["message"], _snip(m, d.text)))
    return out[:1]


def t_never_say(f, d, ctx):
    """The never-say list of every app the draft is about, plus the list for everything ("_all")."""
    ns = ctx.lists.get("never_say", {})
    out = []
    for pid in ["_all"] + ctx.projects_in(d):
        for entry in ns.get(pid, []):
            m = re.search(entry["pattern"], d.text, re.I)
            if m:
                where = "everywhere" if pid == "_all" else "for %s" % pid
                out.append((entry.get("severity", f["severity"]),
                            "never say %s: %s (%s)" % (where, entry["why"], _snip(m, d.text))))
    return out


def t_links(f, d, ctx):
    n = len(URL_RE.findall(d.text))
    return ["%d link(s); the most is %d. %s" % (n, f.get("max", 0), f["message"])] if n > f.get("max", 0) else []


def t_hashtags(f, d, ctx):
    table = f.get("max", {})
    most = table.get(d.platform.lower(), table.get("default", 2))
    n = len(HASHTAG_RE.findall(d.text))
    return ["%d hashtags; %s takes at most %d. %s" % (n, d.platform, most, f["message"])] if n > most else []


def t_length(f, d, ctx):
    table = f.get("max", {})
    most = table.get(d.platform.lower())
    if not most:
        return []
    n = ctx.x_length(d.text) if d.platform.lower() == "x" else len(d.text)
    return ["%d characters; %s takes %d. %s" % (n, d.platform, most, f["message"])] if n > most else []


def t_tld(f, d, ctx):
    bare = URL_RE.sub(" ", d.text)
    hits = [m.group(1) for m in TLD_RE.finditer(bare)]
    keep = set(x.lower() for x in f.get("allow", []))
    hits = [h for h in hits if h.lower() not in keep]
    return ["%s: %s" % (f["message"], ", ".join(hits))] if hits else []


def t_ignore(f, d, ctx):
    names = [n.lower().lstrip("@") for n in _list(ctx, f.get("list", "ignore"))]
    who = [d.target_author.lower().lstrip("@")] + [m.lower() for m in re.findall(r"@([\w.]+)", d.text)]
    hit = [w for w in who if w and w in names]
    return ["%s: %s" % (f["message"], hit[0])] if hit else []


def t_already_replied(f, d, ctx):
    if not d.target:
        return []
    want = norm_url(d.target)
    for it in ctx.sent(d, kind="reply"):
        if _same_platform(it, d) and norm_url(it.get("target")) == want:
            return ["%s: %s (%s)" % (f["message"], it["id"], (it.get("at") or "")[:16])]
    return []


def t_author_cap(f, d, ctx):
    if not d.target_author:
        return []
    who = d.target_author.lower().lstrip("@")
    me = d.account.lower().lstrip("@")
    if who == me or who.split(".")[0] == me.split(".")[0]:  # replies on the owner's own threads
        return []
    n = sum(1 for it in ctx.sent(d, kind="reply", days=1)
            if _same_platform(it, d) and author_of(it.get("target")) == who)
    return ["%s: %d replies to %s in the last day" % (f["message"], n, who)] if n >= f.get("max", 2) else []


def t_account(f, d, ctx):
    p = ctx.account_problem(d)
    return [p] if p else []


def t_pacing(f, d, ctx):
    w = ctx.pace_wait(d)
    return ["%s: %s" % (f["message"], w)] if w else []


def t_caps(f, d, ctx):
    out = []
    for cap in f.get("caps", []):
        if cap["platform"].lower() != d.platform.lower() or cap.get("kind", "post") != d.kind:
            continue
        if cap.get("promo") and not ctx.is_promo(d):
            continue
        items = [it for it in ctx.sent(d, kind=d.kind, same_day=True) if _same_platform(it, d)
                 and (not cap.get("promo") or ctx.item_promo(it))]
        what = "%s %s%s" % (d.platform, "promo " if cap.get("promo") else "", d.kind)
        if len(items) >= cap["max"]:
            out.append("%s: %d %ss already that day; the cap is %d" % (f["message"], len(items), what, cap["max"]))
        hours, mid = cap.get("min_hours"), when(d.at)
        near = [abs((item_time(it) - mid).total_seconds()) for it in items if item_time(it) and mid]
        if hours and near and min(near) < hours * 3600:
            out.append("%s: another %s is %.1f h away; the minimum is %g h" % (f["message"], what, min(near) / 3600, hours))
    return out


def t_new_post_rule(f, d, ctx):
    """A promo post for an app is allowed when that app was never posted in this place, or when none of the
    account's last N posts on the platform (the filter's "last", 4 by default) promoted any of the owner's apps.
    The rule itself lives in the playbook (the demo has it in flow pacing, node "Promo again in the same place")."""
    if d.kind != "post" or not ctx.is_promo(d):
        return []
    apps = ctx.projects_in(d) or ([d.project] if d.project else [])
    posts = [it for it in ctx.sent(d, kind="post") if _same_platform(it, d)]
    for app in apps:
        here = [it for it in posts if it.get("project") == app and auditlog.matches_place(it, d.place or None)] \
            if d.place else [it for it in posts if it.get("project") == app]
        if not here:
            return []
    last = posts[:f.get("last", 4)]
    promos = [it["id"] for it in last if ctx.item_promo(it)]
    if promos:
        return ["%s: %s already posted here, and %d of the account's last %d %s posts promoted an app (%s)"
                % (f["message"], "/".join(apps) or "this app", len(promos), len(last), d.platform, ", ".join(promos))]
    return []


def t_duplicate(f, d, ctx):
    def words(t):  # links left out: a link-only reply under the owner's own post is not a duplicate post
        return " ".join(URL_RE.sub(" ", t or "").lower().split())
    mine = words(d.text)
    if len(mine) < f.get("min_len", 40):
        return []
    out = []
    warn, block = f.get("warn_ratio", 0.8), f.get("block_ratio", 0.92)
    for it in ctx.sent(d, days=f.get("days", 14)):
        other = words(it.get("text"))
        if len(other) < f.get("min_len", 40):
            continue
        same_place = _same_platform(it, d) and (it.get("place") or "").lower() == d.place.lower()
        floor = min(warn, block) if same_place else warn
        sm = difflib.SequenceMatcher(None, mine, other)
        # real_quick_ratio and quick_ratio are upper bounds of ratio: a text that cannot reach the lowest ratio
        # that would be reported is skipped without the slow full comparison (same findings, a fraction of the time)
        if sm.real_quick_ratio() < floor or sm.quick_ratio() < floor:
            continue
        r = sm.ratio()
        if same_place and r >= block:
            out.append(("block", "%s: %d%% the same as %s in the same place" % (f["message"], r * 100, it["id"])))
        elif r >= warn:
            out.append("%s: %d%% the same as %s (%s)" % (f["message"], r * 100, it["id"], it.get("platform", "")))
        if out:
            break
    return out


def _ends_question(text):
    t = re.sub(r"[\s\W_]+$", lambda m: "?" if "?" in m.group(0) else "", text.strip())
    return t.endswith("?")


def t_question_ratio(f, d, ctx):
    if d.kind != "post" or not _ends_question(d.text):
        return []
    last = [it for it in ctx.sent(d, kind="post") if _same_platform(it, d)
            and (it.get("place") or "").lower() == d.place.lower()][:f.get("last", 4)]
    q = sum(1 for it in last if _ends_question(it.get("text") or ""))
    return ["%s: %d of the last %d posts here ended on a question too" % (f["message"], q, len(last))] if q >= f.get("max", 3) else []


def t_ask(f, d, ctx):
    return [f["message"]]


TYPES = {"pattern": t_pattern, "never-say": t_never_say, "links": t_links, "hashtags": t_hashtags, "length": t_length,
         "tld": t_tld, "ignore": t_ignore, "already-replied": t_already_replied, "author-cap": t_author_cap,
         "account": t_account, "pacing": t_pacing, "caps": t_caps, "new-post-rule": t_new_post_rule,
         "duplicate": t_duplicate, "question-ratio": t_question_ratio, "ask": t_ask}


def applies(f, d, ctx):
    a = f.get("applies") or {}
    if a.get("platforms") and d.platform.lower() not in [p.lower() for p in a["platforms"]]:
        return False
    if a.get("kinds") and d.kind not in a["kinds"]:
        return False
    if a.get("actions") and d.action not in a["actions"]:
        return False
    if a.get("recipes") and d.recipe not in a["recipes"]:
        return False
    if "promo" in a and bool(ctx.is_promo(d)) != bool(a["promo"]):
        return False
    if a.get("media") and not d.media:
        return False
    return True


def run(filters, d, ctx, only=None):
    """[{id, severity, message, waived?}] for every filter that applies and finds something."""
    out = []
    for f in filters:
        if only and f["id"] != only:
            continue
        if not applies(f, d, ctx):
            continue
        sev = (f.get("severity_by_kind") or {}).get(d.kind, f["severity"])
        for found in TYPES[f["type"]](f, d, ctx):
            s, msg = found if isinstance(found, tuple) else (sev, found)
            row = {"id": f["id"], "severity": s, "message": msg}
            answer = d.ok.get(f["id"])
            if answer and (s == "ask" or (s == "block" and f.get("waivable"))):
                row["answered" if s == "ask" else "waived"] = " ".join(answer.split())[:400]
            out.append(row)
    return out


def verdict(findings):
    if any((r["severity"] == "block" and "waived" not in r) or (r["severity"] == "ask" and "answered" not in r)
           for r in findings):
        return "block"
    return "warn" if any(r["severity"] == "warn" for r in findings) else "pass"


def selftest(filters, lists, ctx_for):
    """Failures: a bad example its filter doesn't catch, or a good example it flags. ctx_for(example) builds the
    Context (the example's own "history" stands in for the audit log)."""
    fails = []
    for f in filters:
        for label, want in (("bad", True), ("good", False)):
            for i, ex in enumerate((f.get("examples") or {}).get(label, [])):
                d = Draft.from_example(ex)
                rows = run([f], d, ctx_for(ex))
                hit = verdict(rows) != "pass"
                if hit != want:
                    fails.append("%s %s example %d: %s" % (f["id"], label, i + 1,
                                                          "not caught" if want else "flagged: " + "; ".join(r["message"] for r in rows)))
    return fails
