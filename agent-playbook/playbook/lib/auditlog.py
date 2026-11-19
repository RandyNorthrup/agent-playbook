"""The audit log's model: audit/posts.jsonl, one JSON object per line, append-only.

  {"kind": "item", "id": ..., ...}     one posted thing (post, reply, comment, article, video...)
  {"kind": "update", "of": <id>, ...}  a later change to it (verified, deleted, removed, edited...)

Lines are never rewritten. Reading merges updates into each item's current state and keeps the timeline.
audit.py is the command line; the server reads through here too.
"""
import datetime as dt
import hashlib
import re

from . import textio

FIELDS = ["at", "at_precision", "platform", "account", "action", "project", "url", "target", "title", "text",
          "text_source", "media", "status", "scheduled_for", "verified", "source", "notes", "reason"]
OPTIONAL = ["place", "promo"]  # written only when given: where it went (group, subreddit, channel) and whether it promotes
UPDATE_FIELDS = ["status", "reason", "verified", "url", "note", "text", "scheduled_for"]
STATUSES = ("live", "scheduled", "pending", "skipped", "failed", "deleted", "removed", "unknown")
GONE = ("deleted", "removed")  # statuses that need a reason
DUPLICATE_WINDOW = dt.timedelta(hours=24)

_WHEN_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,6}))?)?(Z|[+-]\d\d:?\d\d)?)?$")


class AuditError(ValueError):
    """A plain message for the person or agent at the command line."""


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def local_tz():
    """The local offset right now. Only a fallback: a date in the other half of the year has another offset, so
    convert with to_local() and local_time() instead."""
    return dt.datetime.now().astimezone().tzinfo


def to_local(t):
    """An aware datetime in the machine's local time, with the offset that date had (summer or winter time)."""
    try:
        return t.astimezone()
    except (OverflowError, OSError, ValueError):  # Windows cannot convert dates before 1970 or after 3000
        return t.astimezone(local_tz())


def local_time(naive):
    """A wall-clock time on this machine as an aware datetime, with the offset in force on that date."""
    try:
        return naive.astimezone()
    except (OverflowError, OSError, ValueError):
        return naive.replace(tzinfo=local_tz())


def parse_when(value):
    """(aware datetime, is_exact) for 'YYYY-MM-DD' or an ISO date-time; a date means that day at 00:00 local,
    a time with no offset is local (with that date's own offset across a daylight-saving change). Raises
    AuditError for anything else."""
    m = _WHEN_RE.match(value.strip()) if isinstance(value, str) else None
    if not m:
        raise AuditError("%r is not a date: use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][+HH:MM]" % (value,))
    y, mo, d, hh, mm, ss, frac, off = m.groups()
    try:
        if hh is None:
            return local_time(dt.datetime(int(y), int(mo), int(d))), False
        micro = int((frac or "0").ljust(6, "0"))
        if off in (None, ""):
            return local_time(dt.datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0), micro)), True
        if off == "Z":
            tz = dt.timezone.utc
        else:
            digits = off[1:].replace(":", "")
            delta = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            tz = dt.timezone(-delta if off[0] == "-" else delta)
        return dt.datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0), micro, tzinfo=tz), True
    except ValueError as e:
        raise AuditError("%r is not a real date (%s)" % (value, e))


def when_or_none(value):
    try:
        return parse_when(value)[0]
    except AuditError:
        return None


def clean_reason(reason):
    return " ".join(str(reason or "").split())[:500]


def clean_status(status):
    """The status in its stored form (lower case, no stray spaces). Raises AuditError when it is not a known one."""
    s = " ".join(str(status or "").split()).lower()
    if s not in STATUSES:
        raise AuditError("%r is not a status: use one of %s" % (status, ", ".join(STATUSES)))
    return s


def needs_reason(status=None, action=None):
    return " ".join(str(status or "").split()).lower() in GONE or " ".join(str(action or "").split()).lower() == "delete"


def make_id(rec):
    basis = "|".join(str(rec.get(k, "")) for k in ("at", "platform", "account", "action", "url", "target", "text", "source", "notes"))
    plat = re.sub(r"[^a-z0-9]", "", (rec.get("platform") or "x").lower()) or "x"
    return "%s-%s-%s" % ((rec.get("at") or "undated")[:10], plat, hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8])


TEXT_KEYS = ("at", "platform", "account", "action", "project", "status", "url", "target", "text", "title")
# what an update never overwrites: its own bookkeeping, and the keys that make an item an item
NOT_MERGED = ("kind", "of", "at", "note", "by", "reason", "id", "history")


def read(log):
    """(items, problems). Items are in log order, each with its updates merged in and listed under "history".
    A line that cannot be used is skipped and described in problems; reading never fails on bad content."""
    items, order, problems = {}, [], []
    try:
        text = textio.read_bytes(log).decode("utf-8", "replace")
    except FileNotFoundError:
        return [], []
    for i, line in enumerate(textio.split_lines(text), 1):
        if not line.strip():
            continue
        try:
            r = textio.loads(line)
        except ValueError:
            problems.append("line %d is not JSON" % i)
            continue
        if not isinstance(r, dict):
            problems.append("line %d is not an object" % i)
            continue
        if r.get("kind") == "item":
            if not isinstance(r.get("id"), str) or not r["id"]:
                problems.append("line %d is an item with no id" % i)
                continue
            if r["id"] in items:
                problems.append("line %d repeats the id %s (the first one is kept)" % (i, r["id"]))
                continue
            r = dict(r, history=[])
            r["_line"] = i
            items[r["id"]] = r
            order.append(r["id"])
        elif r.get("kind") == "update":
            of = r.get("of")
            it = items.get(of) if isinstance(of, str) else None
            if it is None:
                problems.append("line %d updates %r, which is not in the log" % (i, of))
                continue
            it["history"].append(r)
            for k, v in r.items():
                if k not in NOT_MERGED and not k.startswith("_") and v not in (None, ""):
                    it[k] = v
            if r.get("status"):
                reason = r.get("reason", "")
                it["reason"] = reason if isinstance(reason, str) else str(reason)  # why it has its current status
        else:
            problems.append("line %d has an unknown kind %r" % (i, r.get("kind")))
    result = [items[i] for i in order]
    for it in result:  # text fields are text, whatever a line or a later update wrote
        for k in TEXT_KEYS:
            if not isinstance(it.get(k), str):
                it[k] = "" if it.get(k) is None else str(it[k])
        if not isinstance(it.get("media"), list):
            it["media"] = []
    spellings = platform_spellings(result)
    for it in result:  # one spelling per platform, so a stray "reddit" counts with "Reddit"
        it["platform"] = canonical_platform(it["platform"], spellings)
    return result, problems


def platform_spellings(items):
    """{lower-case platform: the spelling most items use}: the log's own spelling of each platform."""
    counts = {}
    for it in items:
        name = " ".join(str(it.get("platform") or "").split())
        if name:
            c = counts.setdefault(name.lower(), {})
            c[name] = c.get(name, 0) + 1
    return dict((k, sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]) for k, c in counts.items())


def canonical_platform(name, spellings):
    """The log's spelling of a platform name typed in any case or spacing; a new platform keeps its own."""
    name = " ".join(str(name or "").split())
    return spellings.get(name.lower(), name)


def public(item):
    """An item without the reader's private keys (for JSON output)."""
    return dict((k, v) for k, v in item.items() if not k.startswith("_"))


def sort_key(item):
    t = when_or_none(item.get("at") or "")
    return (t.timestamp() if t else 0.0, item.get("_line", 0))


def build_item(kw):
    """A new item record from field values. Validates; raises AuditError with what to fix."""
    rec = {"kind": "item", "logged_at": now()}
    for k in FIELDS:
        v = kw.get(k)
        rec[k] = v if v is not None else ([] if k == "media" else "")
    for k in ("platform", "account", "action", "project", "url", "target", "title"):
        rec[k] = " ".join(str(rec[k]).split())
    if not rec["platform"]:
        raise AuditError("--platform is required (X, Bluesky, Facebook group, ...)")
    if not rec["action"]:
        raise AuditError("--action is required (post, reply, comment, schedule, edit, delete, ...)")
    rec["action"] = rec["action"].lower()
    if not (str(rec["text"]).strip() or rec["title"] or rec["url"]):
        raise AuditError("nothing to log: give --text or --text-file (the words as posted), or at least --title or --url")
    if not isinstance(rec["media"], list):
        rec["media"] = [rec["media"]]
    rec["at"] = str(rec["at"]).strip() or now()
    _, exact = parse_when(rec["at"])
    rec["at_precision"] = rec["at_precision"] or ("exact" if exact else "day")
    if rec["scheduled_for"]:
        parse_when(rec["scheduled_for"])
    rec["status"] = clean_status(rec["status"] or "live")
    rec["reason"] = clean_reason(rec["reason"])
    if needs_reason(rec["status"], rec["action"]) and not rec["reason"]:
        raise AuditError('a reason is required to log a deletion or removal: add --reason "why"')
    place = " ".join(str(kw.get("place") or "").split())
    if place:
        rec["place"] = place
    if kw.get("promo") is not None:
        rec["promo"] = bool(kw["promo"])
    rec["id"] = kw.get("id") or make_id(rec)
    return rec


def find_duplicate(items, rec):
    """The already-logged item that `rec` repeats, or None: the same platform, account and action with the same
    link, or with the same text within 24 hours."""
    def norm(s):
        return " ".join(str(s or "").split()).lower()

    who = (norm(rec.get("platform")), norm(rec.get("account")), norm(rec.get("action")))
    url, text = norm(rec.get("url")), norm(rec.get("text"))
    at = when_or_none(rec.get("at") or "")
    for it in items:
        if it.get("id") == rec.get("id"):
            return it
        if (norm(it.get("platform")), norm(it.get("account")), norm(it.get("action"))) != who:
            continue
        if url and norm(it.get("url")) == url:
            return it
        if text and norm(it.get("text")) == text:
            other = when_or_none(it.get("at") or "")
            if at is None or other is None or abs(at - other) <= DUPLICATE_WINDOW:
                return it
    return None


def build_update(item_id, kw):
    """An update record. Needs at least one field; a deleted or removed status needs a reason."""
    rec = {"kind": "update", "of": item_id, "at": now()}
    for k in UPDATE_FIELDS:
        v = kw.get(k)
        if v is not None and str(v).strip():
            rec[k] = v
    if "status" in rec:
        rec["status"] = clean_status(rec["status"])
    if "reason" in rec:
        rec["reason"] = clean_reason(rec["reason"])
    if "scheduled_for" in rec:
        parse_when(rec["scheduled_for"])
    if len(rec) == 3:
        raise AuditError("nothing to update: give at least one of " + ", ".join("--" + k.replace("_", "-") for k in UPDATE_FIELDS))
    if needs_reason(rec.get("status")) and not rec.get("reason"):
        raise AuditError('a reason is required to mark an item %s: add --reason "why"' % rec["status"])
    return rec


def append(log, records, lock_path):
    """Append records in one locked write, so parallel agents cannot interleave or lose a line."""
    textio.append_lines(log, [textio.dumps(r) for r in records], lock_path)


def local_day(item):
    """The local calendar day an item went out, with the offset of that day (not of today)."""
    t = when_or_none(item.get("at") or "")
    return to_local(t).date() if t else None


def matches_place(item, place):
    """Whether an item went to `place`: its recorded place, or (for older lines) text in its target, link or account."""
    p = " ".join(str(place or "").split()).lower()
    if not p:
        return True
    if isinstance(item.get("place"), str) and item["place"]:
        return item["place"].lower() == p
    return any(p in str(item.get(k) or "").lower() for k in ("target", "url", "account"))
