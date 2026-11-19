#!/usr/bin/env python3
"""Say how much of the weekly Codex limit is used, and refuse a bulk image run when it is too high.

  quota.py                   the last reading; exit 1 when 80 percent or more is used (no bulk generation)
  quota.py --max-used 95     the limit for a single image
  quota.py --json            the reading as JSON: used_percent, window_minutes, resets_at, read_at, file

Every Codex run writes its rate limits into its session file. This reads the newest reading under
<codex home>/sessions (lines of type event_msg whose payload is a token_count:
payload.rate_limits.primary.used_percent), walking back through the session files until one has a reading.
The codex home is --codex-home, else $CODEX_HOME, else ~/.codex.

Exit 1, with the reason on stderr, when:
  the reading is at or above --max-used
  no reading is newer than --max-age-hours, or the limit has reset since the last one (run any codex
  command once, then retry)
  there is no sessions folder
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "scripts")]
import common  # noqa: E402

NEEDS = []
EFFECTS = ["none"]
BULK_MAX_USED = 80.0     # a bulk run (more than BULK_OVER images) is refused at this percent used
SINGLE_MAX_USED = 95.0   # single images are allowed up to this
BULK_OVER = 5
MAX_AGE_HOURS = 24.0
WEEK_MINUTES = 7 * 24 * 60  # a rate-limit window this long or longer is the weekly limit


class QuotaError(Exception):
    """No usable reading, or too much of the limit is used; str() is the one-line reason."""


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [--max-used 80] [--max-age-hours 24] [--codex-home DIR] [--json]")
    ap.add_argument("--max-used", type=float, default=BULK_MAX_USED, metavar="PERCENT",
                    help="exit 1 at or above this percent used (default 80)")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS, metavar="HOURS",
                    help="a reading older than this does not count (default 24)")
    ap.add_argument("--codex-home", metavar="DIR", help="the Codex folder (default: $CODEX_HOME, else ~/.codex)")
    ap.add_argument("--json", action="store_true", help="print the reading as JSON")
    return ap


def codex_home(explicit=None):
    return explicit or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


BLOCK = 1 << 16


def lines_backwards(fh, block=BLOCK):
    """The lines of a binary file, last first, without their line ends. Reads from the end in blocks, so the
    last reading of a session file hundreds of megabytes long costs one or two reads, not the whole file."""
    fh.seek(0, os.SEEK_END)
    pos = fh.tell()
    later = []  # the pieces of the line being put together, the piece nearest the end first
    while pos > 0:
        step = min(block, pos)
        pos -= step
        fh.seek(pos)
        chunk = fh.read(step)
        end = len(chunk)
        cut = chunk.rfind(b"\n", 0, end)
        while cut != -1:
            line = chunk[cut + 1:end]
            if later:
                line += b"".join(reversed(later))
                later = []
            yield line.rstrip(b"\r")
            end = cut
            cut = chunk.rfind(b"\n", 0, end)
        later.append(chunk[:end])
    if later:
        yield b"".join(reversed(later)).rstrip(b"\r")


def parse_reading(raw):
    """(primary, timestamp) from one session-file line that is a rate-limit reading, else None."""
    if b'"rate_limits"' not in raw:
        return None
    try:
        line = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    payload = line.get("payload") if isinstance(line, dict) else None
    if not isinstance(payload, dict) or line.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = [w for w in (limits.get("primary"), limits.get("secondary"))
               if isinstance(w, dict) and _number(w.get("used_percent"))]
    if not windows:
        return None
    # the weekly window is the one this tool reports: primary today, but a Codex that also reports a short window
    # (5 hours) may put the weekly one in secondary, so pick it by its length when the reading says
    weekly = [w for w in windows if _number(w.get("window_minutes")) and w["window_minutes"] >= WEEK_MINUTES]
    return (weekly[0] if weekly else windows[0]), line.get("timestamp")


def last_reading(path):
    """The last rate-limit reading in one session file, or None. Lines that are not JSON are skipped."""
    try:
        with open(path, "rb") as fh:
            for raw in lines_backwards(fh):
                found = parse_reading(raw)
                if found:
                    return found
    except OSError:
        return None
    return None


def session_files(sessions, newer_than):
    """(mtime, path) of every session file changed at or after `newer_than`, newest first. os.scandir: on
    Windows the listing carries each file's time, so thousands of session files cost no extra stat calls."""
    out, folders = [], [sessions]
    while folders:
        try:
            entries = list(os.scandir(folders.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders.append(entry.path)
                elif entry.name.endswith(".jsonl"):
                    mtime = entry.stat().st_mtime
                    if mtime >= newer_than:
                        out.append((mtime, entry.path))
            except OSError:
                continue
    return sorted(out, reverse=True)


def reading(home=None, max_age_hours=MAX_AGE_HOURS, now=None):
    """The newest quota reading as a dict (used_percent, window_minutes, resets_at, read_at, file), with the
    two times as aware datetimes (resets_at may be None). Raises QuotaError when there is none to trust."""
    home = codex_home(home)
    sessions = os.path.join(home, "sessions")
    now = now or common.now_utc()
    if not os.path.isdir(sessions):
        raise QuotaError("no Codex sessions folder at %s" % sessions)
    oldest = now - datetime.timedelta(hours=max_age_hours)
    for mtime, path in session_files(sessions, oldest.timestamp()):
        found = last_reading(path)
        if not found:
            continue
        primary, stamp = found
        try:
            read_at = common.parse_time(stamp)
        except ValueError:
            read_at = datetime.datetime.fromtimestamp(mtime, common.UTC)
        if read_at < oldest:
            continue
        resets = primary.get("resets_at")
        try:
            if _number(resets):
                resets_at = datetime.datetime.fromtimestamp(resets, common.UTC)
            elif _number(primary.get("resets_in_seconds")):  # older Codex: seconds from the reading
                resets_at = read_at + datetime.timedelta(seconds=primary["resets_in_seconds"])
            else:
                resets_at = None
        except (ValueError, OverflowError, OSError):
            resets_at = None  # not a time in seconds
        if resets_at and resets_at <= now:
            raise QuotaError("the limit has reset since the last quota reading (%s) under %s: run any codex command once, "
                             "then retry" % (local(resets_at), sessions))
        return {"used_percent": float(primary["used_percent"]), "window_minutes": primary.get("window_minutes"),
                "resets_at": resets_at, "read_at": read_at, "file": path}
    raise QuotaError("no quota reading newer than %g h under %s: run any codex command once, then retry" % (max_age_hours, sessions))


def check(max_used, home=None, max_age_hours=MAX_AGE_HOURS, bulk=True, now=None):
    """The reading, when less than max_used percent of the limit is used. Raises QuotaError otherwise."""
    got = reading(home, max_age_hours, now)
    if got["used_percent"] >= max_used:
        raise QuotaError(over(got["used_percent"], max_used, bulk))
    return got


def over(used, max_used, bulk=True):
    if bulk:
        return ("%.1f%% of the weekly Codex limit is used (limit for a bulk run: %g%%): generate nothing in bulk; "
                "mark unfixed scenes pending-codex" % (used, max_used))
    return "%.1f%% of the weekly Codex limit is used (limit for a single image: %g%%): generate nothing until it resets" % (used, max_used)


def local(when):
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def sentence(got, now=None):
    minutes = max(0, int(((now or common.now_utc()) - got["read_at"]).total_seconds() // 60))
    age = "%d min ago" % minutes if minutes < 90 else "%.1f h ago" % (minutes / 60.0)
    resets = ", resets %s" % local(got["resets_at"]) if got["resets_at"] else ""
    return "codex weekly limit: %.1f%% used%s, read %s" % (got["used_percent"], resets, age)


def as_json(got):
    out = dict(got)
    for key in ("resets_at", "read_at"):
        out[key] = out[key].isoformat(timespec="seconds") if out[key] else None
    return out


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not 0 < a.max_used <= 100:
        ap.error("--max-used is a percent from 1 to 100")
    if a.max_age_hours <= 0:
        ap.error("--max-age-hours must be more than 0")
    common.safe_stdio()
    try:
        got = reading(a.codex_home, a.max_age_hours)
    except QuotaError as e:
        common.die(str(e))
    print(json.dumps(as_json(got), ensure_ascii=False) if a.json else sentence(got))
    sys.stdout.flush()
    if got["used_percent"] >= a.max_used:
        common.die(over(got["used_percent"], a.max_used, bulk=a.max_used < SINGLE_MAX_USED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
