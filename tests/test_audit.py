"""audit.py from the command line, and the log model, against a copy of the fixture playbook."""
import datetime as dt
import json
import os
import subprocess
import sys
import unittest

from pbtest import PLAYBOOK, PlaybookCase
from lib import auditlog


class TestAuditCli(PlaybookCase):
    def log_bytes(self):
        return (self.root / "audit" / "posts.jsonl").read_bytes()

    def rows(self):  # records end at LF only: splitlines() would also cut at U+2028 inside a post
        return [json.loads(line) for line in self.log_bytes().decode("utf-8").split("\n") if line.strip()]

    def add(self, *extra, **kw):
        args = ["add", "--platform", kw.get("platform", "X"), "--account", kw.get("account", "@sam"), "--action", kw.get("action", "reply")]
        return self.audit(*(args + list(extra)))

    def test_add_requires_fields(self):
        before = self.log_bytes()
        for args, expect in ((("add",), "--platform is required"),
                             (("add", "--platform", "X"), "--action is required"),
                             (("add", "--platform", "X", "--action", "post"), "nothing to log"),
                             (("add", "--platform", "X", "--action", "post", "--text", "   "), "nothing to log"),
                             (("add", "--platform", "  ", "--action", "post", "--text", "x"), "--platform is required")):
            r = self.audit(*args)
            self.assertRan(r, 1)
            self.assertIn(expect, r.stderr)
        self.assertEqual(self.log_bytes(), before)

    def test_add_logs_an_item_in_lf(self):
        text = self.tmp / "post.txt"
        text.write_bytes("line one\r\nline two é — ✓\r\n".encode("utf-8"))
        r = self.add("--text-file", text, "--url", "https://x.com/sam/status/3", "--project", "posting", "--media", "a.png", "--media", "b.png",
                     "--place", "Makers group", "--promo", action="post")
        self.assertRan(r)
        item = self.rows()[-1]
        self.assertEqual(r.stdout.strip(), item["id"])
        self.assertRegex(item["id"], r"^\d{4}-\d\d-\d\d-x-[0-9a-f]{8}$")
        self.assertEqual(item["text"], "line one\nline two é — ✓")
        self.assertEqual((item["media"], item["status"], item["place"], item["promo"], item["at_precision"]), (["a.png", "b.png"], "live", "Makers group", True, "exact"))
        self.assertNotIn(b"\r", self.log_bytes())
        self.assertRan(self.audit("add", "--platform", "X", "--action", "post", "--text-file", self.tmp / "nope.txt"), 1)

    def test_log_is_append_only(self):
        before = self.log_bytes()
        self.assertRan(self.add("--text", "one more"))
        self.assertRan(self.audit("update", "2026-01-03-x-aaaaaaa2", "--verified", "seen"))
        after = self.log_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after[len(before):].count(b"\n"), 2)

    def test_duplicate_add_refused(self):
        r = self.add("--text", "same words", "--url", "https://x.com/sam/status/10")
        self.assertRan(r)
        n = len(self.rows())
        r = self.add("--text", "other words", "--url", "https://x.com/sam/status/10")  # the same link
        self.assertRan(r, 1)
        self.assertIn("already logged", r.stderr)
        r = self.add("--text", "Same   WORDS")  # the same text within 24 hours
        self.assertRan(r, 1)
        self.assertIn("already logged", r.stderr)
        self.assertEqual(len(self.rows()), n)
        self.assertRan(self.add("--text", "same words", "--force", account="@other"))  # another account is another post (not in the registry: --force)
        self.assertRan(self.add("--text", "same words", action="post"))  # so is another kind of action
        self.assertRan(self.add("--text", "same words", "--force"))
        old = (dt.datetime.now().astimezone() - dt.timedelta(days=3)).isoformat(timespec="seconds")
        self.assertRan(self.add("--text", "same words", "--at", old))  # the same text days apart is fine
        self.assertEqual(len(self.rows()), n + 4)
        r = self.add("--text", "First post about the project.", "--url", "https://x.com/sam/status/1", action="post")  # already in the fixture log
        self.assertRan(r, 1)

    def test_dates_validated(self):
        for bad in ("not a date", "2026-13-01", "11-08", "2026-02-30T10:00", "yesterday"):
            r = self.add("--text", "x " + bad, "--at", bad)
            self.assertRan(r, 1)
            self.assertIn("date", r.stderr)
        for good in ("2026-03-01", "2026-03-01T10:00", "2026-03-01T10:00:00", "2026-03-01T10:00:00-08:00", "2026-03-01T10:00:00Z"):
            self.assertRan(self.add("--text", "x " + good, "--at", good))
        r = self.add("--text", "sched", "--status", "scheduled", "--scheduled-for", "soon")
        self.assertRan(r, 1)
        self.assertRan(self.audit("list", "--since", "garbage"), 1)
        r = self.audit("stats")
        self.assertRan(r)
        self.assertRegex(r.stdout, r"items, 2026-01-02 to \d{4}-\d\d-\d\d")

    def test_status_vocabulary_and_reason(self):
        r = self.add("--text", "s1", "--status", "posted")
        self.assertRan(r, 1)
        self.assertIn("is not a status", r.stderr)
        for status in ("deleted", "removed", "Deleted ", " REMOVED"):
            r = self.audit("update", "2026-01-03-x-aaaaaaa2", "--status", status)
            self.assertRan(r, 1)
            self.assertIn("reason is required", r.stderr)
        r = self.audit("update", "2026-01-03-x-aaaaaaa2", "--status", "Deleted ", "--reason", "  Wrong   link ")
        self.assertRan(r)
        last = self.rows()[-1]
        self.assertEqual((last["status"], last["reason"]), ("deleted", "Wrong link"))
        r = self.audit("show", "2026-01-03-x-aaaaaaa2")
        self.assertIn("reason: Wrong link", r.stdout)
        self.assertIn("2026-01-03-x-aaaaaaa2", self.audit("list", "--status", "DELETED").stdout)
        self.assertRan(self.add("--text", "gone", action="delete"), 1)
        self.assertRan(self.add("--text", "gone", "--status", "removed"), 1)
        self.assertRan(self.add("--text", "gone", "--reason", "the owner asked", action="delete"))

    def test_update_requires_a_field_and_a_known_item(self):
        n = len(self.rows())
        r = self.audit("update", "2026-01-03-x-aaaaaaa2")
        self.assertRan(r, 1)
        self.assertIn("nothing to update", r.stderr)
        self.assertRan(self.audit("update", "2026-01-03-x-aaaaaaa2", "--note", "  "), 1)
        self.assertRan(self.audit("update", "nosuch", "--status", "live"), 1)
        self.assertEqual(len(self.rows()), n)

    def test_import_is_all_or_nothing(self):
        before = self.log_bytes()
        good = {"platform": "X", "account": "@sam", "action": "post", "text": "imported one", "at": "2026-01-05"}
        bad = self.tmp / "bad.jsonl"
        bad.write_text(json.dumps(good) + "\n\nnot json\n" + json.dumps({"platform": "X", "action": "post", "text": "t", "at": "nope"}) + "\n", encoding="utf-8")
        r = self.audit("import", bad)
        self.assertRan(r, 1)
        self.assertIn("line 3", r.stderr)
        self.assertIn("line 4", r.stderr)
        self.assertIn("nothing imported", r.stderr)
        self.assertEqual(self.log_bytes(), before)
        ok = self.tmp / "ok.jsonl"
        ok.write_text(json.dumps(good) + "\n" + json.dumps(good) + "\n", encoding="utf-8")
        r = self.audit("import", ok)
        self.assertRan(r)
        self.assertIn("imported 1 item(s)", r.stdout)
        self.assertIn("imported 0 item(s)", self.audit("import", ok).stdout)

    def test_read_tolerates_bad_lines(self):
        with open(str(self.root / "audit" / "posts.jsonl"), "ab") as fh:
            fh.write(b'not json\n{"kind": "item", "platform": "X"}\n[1]\n{"kind": "update", "of": "ghost", "status": "live"}\n{"kind": "mystery"}\n'
                     b'{"kind": "item", "id": "2026-01-02-x-aaaaaaa1", "platform": "X"}\n')
        for cmd in (("stats",), ("list",), ("search", "project"), ("show", "aaaaaaa1"), ("today",), ("recent", "--platform", "X")):
            r = self.audit(*cmd)
            self.assertRan(r)
            self.assertIn("warning: line 5 is not JSON", r.stderr)
            self.assertIn("is an item with no id", r.stderr)
        self.assertIn("3 items", self.audit("stats").stdout)
        items, problems = auditlog.read(self.root / "audit" / "posts.jsonl")
        self.assertEqual(len(items), 3)
        self.assertEqual(len(problems), 6)
        self.assertEqual(auditlog.read(self.root / "audit" / "missing.jsonl"), ([], []))

    def test_a_post_with_unicode_line_separators_stays_one_item(self):
        text = "first second third\x85fourth"  # JSON leaves these unescaped; they are not line ends
        r = self.add("--text", text, action="post")
        self.assertRan(r)
        item_id = r.stdout.strip()
        items, problems = auditlog.read(self.root / "audit" / "posts.jsonl")
        self.assertEqual(problems, [])
        self.assertEqual([i["text"] for i in items if i["id"] == item_id], [text])
        r = self.audit("show", item_id)
        self.assertRan(r)
        self.assertNotIn("warning", r.stderr)
        r = self.add("--text", text, action="post")  # the same words again are a duplicate, not a new item
        self.assertRan(r, 1)
        self.assertIn("already logged as %s" % item_id, r.stderr)

    def test_odd_updates_never_break_reading(self):
        with open(str(self.root / "audit" / "posts.jsonl"), "ab") as fh:
            fh.write(b'{"kind": "update", "of": ["x"], "status": "live"}\n'
                     b'{"kind": "update", "of": {"a": 1}}\n'
                     b'{"kind": ["item"]}\n'
                     b'{"kind": "update", "of": "2026-01-02-x-aaaaaaa1", "id": "hijacked", "history": 5, "_line": -1, '
                     b'"platform": 7, "status": "deleted", "reason": ["why"]}\n'
                     b'{"kind": "update", "of": "2026-01-02-x-aaaaaaa1", "verified": "seen again"}\n')
        items, problems = auditlog.read(self.root / "audit" / "posts.jsonl")
        self.assertEqual(len(problems), 3)
        hit = [i for i in items if i["id"] == "2026-01-02-x-aaaaaaa1"]
        self.assertEqual(len(hit), 1)  # an update cannot rename an item or replace its timeline
        self.assertEqual(len(hit[0]["history"]), 3)
        self.assertEqual((hit[0]["platform"], hit[0]["status"], hit[0]["verified"], hit[0]["_line"]), ("7", "deleted", "seen again", 1))
        for cmd in (("stats",), ("list",), ("today",), ("recent", "--platform", "X"), ("show", "aaaaaaa1"), ("search", "seen")):
            self.assertRan(self.audit(*cmd))

    def test_each_warning_is_printed_once(self):
        with open(str(self.root / "audit" / "posts.jsonl"), "ab") as fh:
            fh.write(b"not json\n")
        for cmd in (("today",), ("show", "aaaaaaa1")):
            r = self.audit(*cmd)
            self.assertRan(r)
            self.assertEqual(r.stderr.count("is not JSON"), 1, cmd)

    def test_list_show_search(self):
        r = self.audit("list", "--platform", "x", "-n", "1")
        self.assertRan(r)
        self.assertIn("2026-01-03-x-aaaaaaa2", r.stdout)
        self.assertNotIn("aaaaaaa1", r.stdout)
        self.assertIn("2 item(s)", r.stdout)
        r = self.audit("show", "2026-01-02-x-aaaaaaa1")
        self.assertRan(r)
        self.assertIn("verified: checked live", r.stdout)
        self.assertIn("First post about the project.", r.stdout)
        self.assertRan(self.audit("show", "aaaaaaa3"))
        self.assertRan(self.audit("show", "nosuch"), 1)
        self.assertRan(self.audit("show", "a"), 1)
        self.assertIn("Makers group", self.audit("show", "aaaaaaa3").stdout)
        self.assertIn("aaaaaaa2", self.audit("search", "that", "helps").stdout)
        self.assertRan(self.audit(), 2)
        self.assertRan(self.audit("bogus"), 2)

    def test_recent(self):
        r = self.audit("recent", "--platform", "X", "--account", "@sam")
        self.assertRan(r)
        lines = [line for line in r.stdout.splitlines() if line[:4].isdigit()]
        self.assertEqual(len(lines), 2)
        self.assertIn("aaaaaaa2", lines[0])  # newest first
        self.assertRegex(lines[0], r"reply\s+posting\s+promo \?\s+live")
        self.assertRan(self.add("--text", "launch day", "--promo", "--place", "Makers group", "--project", "posting", action="post", platform="Facebook group", account="Sam"))
        self.assertRan(self.add("--text", "a tip", "--no-promo", "--place", "Other group", action="post", platform="Facebook group", account="Sam"))
        r = self.audit("recent", "--platform", "facebook group", "--account", "sam", "--place", "makers group")
        self.assertRan(r)
        self.assertIn("launch day", r.stdout)
        self.assertIn("Sharing the release", r.stdout)  # an older line with no place field matches on its target
        self.assertNotIn("a tip", r.stdout)
        self.assertRegex(r.stdout, r"post\s+posting\s+promo\s+live")
        r = self.audit("recent", "--platform", "Facebook group", "-n", "1")
        self.assertIn("a tip", r.stdout)
        self.assertIn("not promo", r.stdout)
        self.assertIn("1 item(s)", r.stdout)
        self.assertRan(self.audit("recent"), 2)

    def test_today(self):
        r = self.audit("today")
        self.assertRan(r)
        self.assertIn("0 item(s) today", r.stdout)
        self.assertRan(self.add("--text", "reply one"))
        self.assertRan(self.add("--text", "reply two"))
        self.assertRan(self.add("--text", "a post", action="post"))
        self.assertRan(self.add("--text", "elsewhere", platform="Bluesky", account="sam.bsky.social"))
        r = self.audit("today")
        self.assertRan(r)
        self.assertRegex(r.stdout, r"X\s+@sam\s+3\s+post 1, reply 2")
        self.assertRegex(r.stdout, r"Bluesky\s+sam\.bsky\.social\s+1\s+reply 1")
        self.assertIn("4 item(s) today", r.stdout)
        r = self.audit("today", "--platform", "bluesky")
        self.assertIn("1 item(s) today", r.stdout)
        self.assertNotIn("@sam ", r.stdout)

    def test_platform_spelling_follows_the_log(self):
        self.assertRan(self.add("--text", "first"))
        self.assertRan(self.add("--text", "second", platform=" x "))
        self.assertEqual([r["platform"] for r in self.rows() if r.get("kind") == "item"][-2:], ["X", "X"])
        self.assertRan(self.add("--text", "new place", platform="Mastodon", account="@sam@example.social"))
        self.assertEqual(self.rows()[-1]["platform"], "Mastodon")
        with open(str(self.root / "audit" / "posts.jsonl"), "ab") as fh:  # a line written before add normalized names
            fh.write(b'{"kind": "item", "id": "2026-01-02-x-aaaaaaa2", "platform": "x", "action": "reply", "text": "old"}\n')
        items = auditlog.read(self.root / "audit" / "posts.jsonl")[0]
        self.assertEqual(set(i["platform"] for i in items if i["platform"].lower() == "x"), {"X"})
        self.assertNotRegex(self.audit("stats").stdout, r"\d+ x\b")

    def test_account_must_be_in_registry(self):
        r = self.add("--text", "who is this", account="Somebody Else")
        self.assertRan(r, 1)
        self.assertIn("isn't in the accounts registry", r.stderr)
        self.assertIn("@sam", r.stderr)
        self.assertRan(self.add("--text", "by label", account="Main account"))  # a label counts as well as a handle
        self.assertRan(self.add("--text", "forced", "--force", account="Somebody Else"))

    def test_pace_flag_on_quick_replies(self):
        self.assertRan(self.add("--text", "first quick one", "--at", "2026-01-05T10:00:00"))
        r = self.add("--text", "second quick one", "--at", "2026-01-05T10:00:10")
        self.assertRan(r)
        self.assertIn("pace flag", r.stderr)
        flagged = [x for x in self.rows() if x.get("text") == "second quick one"]
        self.assertIn("minimum is 30 s", flagged[0]["pace_flag"])
        r = self.add("--text", "third, patient", "--at", "2026-01-05T10:01:00")
        self.assertNotIn("pace flag", r.stderr)

    def test_gap_ready_then_wait(self):
        r = self.audit("gap", "--platform", "X", "--account", "@sam", "--action", "reply")
        self.assertRan(r)
        self.assertIn("READY", r.stdout)
        self.assertRan(self.add("--text", "just now"))
        r = self.audit("gap", "--platform", "X", "--account", "@sam", "--action", "reply")
        self.assertRan(r, 1)
        self.assertIn("WAIT", r.stdout)
        self.assertRan(self.audit("gap", "--platform", "X", "--account", "Nobody", "--action", "reply"), 1)

    def test_parallel_adds_all_land(self):
        procs = [subprocess.Popen([sys.executable, str(PLAYBOOK / "audit.py"), "--root", str(self.root), "add", "--platform", "X", "--account", "@p%d" % i, "--force",
                                   "--action", "reply", "--text", "parallel %d " % i + "x" * 6000], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(8)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
        rows = self.rows()
        self.assertEqual(len(rows), 4 + 8)
        self.assertEqual(len(set(r["id"] for r in rows if r["kind"] == "item")), 3 + 8)


class TestAuditModel(unittest.TestCase):
    def test_parse_when(self):
        t, exact = auditlog.parse_when("2026-03-01T10:00:00-08:00")
        self.assertTrue(exact)
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=-8))
        self.assertEqual(auditlog.parse_when("2026-03-01T18:00:00Z")[0], t)
        self.assertEqual(auditlog.parse_when("2026-03-01T10:00:00.5-0800")[0], t + dt.timedelta(milliseconds=500))
        self.assertFalse(auditlog.parse_when("2026-03-01")[1])
        for bad in ("", "x", None, 5, "2026-3-1", "2026-03-01T25:00"):
            with self.assertRaises(auditlog.AuditError):
                auditlog.parse_when(bad)

    def test_local_times_use_the_offset_of_their_own_date(self):
        """A time with no offset, and the local day of an item, follow daylight saving: a summer time keeps its
        summer offset when read in winter. Runs with TZ=PST8PDT (honoured on Windows, macOS and Linux)."""
        code = ("import sys, time, datetime as dt; sys.path.insert(0, sys.argv[1]); from lib import auditlog\n"
                "if not time.localtime(time.mktime((2026, 7, 1, 12, 0, 0, 0, 0, -1))).tm_isdst: print('no-dst'); sys.exit(0)\n"
                "m = dt.timedelta(minutes=1)\n"
                "print(auditlog.parse_when('2026-07-01T12:00')[0].utcoffset() // m)\n"
                "print(auditlog.parse_when('2026-01-15T12:00')[0].utcoffset() // m)\n"
                "print(auditlog.parse_when('2026-07-01')[0].utcoffset() // m)\n"
                "print(auditlog.local_day({'at': '2026-11-01T00:30:00-07:00'}))\n"
                "print(auditlog.local_day({'at': '2026-03-08T01:30:00-08:00'}))\n"
                "print(auditlog.local_day({'at': '1900-01-01T10:00:00Z'}) is not None)\n")
        # POSIX systems get the rules spelled out: without the legacy zone file (Ubuntu 24.04 moved it to tzdata-legacy)
        # the C library falls back to the 1987 US rules, under which summer time ended on the last Sunday of October
        tz = "PST8PDT" if os.name == "nt" else "PST8PDT,M3.2.0,M11.1.0"
        r = subprocess.run([sys.executable, "-c", code, str(PLAYBOOK)], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ=tz))
        self.assertEqual(r.returncode, 0, r.stderr)
        if r.stdout.strip() == "no-dst":
            self.skipTest("this platform ignores TZ")
        self.assertEqual(r.stdout.split(), ["-420", "-480", "-420", "2026-11-01", "2026-03-08", "True"])

    def test_needs_reason_ignores_case_and_spaces(self):
        for s in ("deleted", " Deleted ", "REMOVED", "removed\t"):
            self.assertTrue(auditlog.needs_reason(s))
        self.assertTrue(auditlog.needs_reason(None, " Delete "))
        self.assertFalse(auditlog.needs_reason("live", "post"))


if __name__ == "__main__":
    unittest.main()
