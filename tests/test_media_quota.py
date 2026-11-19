"""Offline tests for media/quota.py against a Codex home built here: no Codex run, no network.

The session files are in the shape Codex writes (one JSON object per line; a token_count event carries
rate_limits.primary). Their modified times are set with os.utime.
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediatest import quota_at, run, session_file, stamp, token_count  # noqa: E402

import quota  # noqa: E402  (mediatest put agent-playbook/media on sys.path)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


class TestQuota(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "codex-home")

    def quota(self, *args, **env):
        return run("quota.py", "--codex-home", self.home, *args, env=env or None)

    def test_reads_the_newest_file(self):
        quota_at(self.home, 5.0, minutes_ago=12, name="a.jsonl")
        quota_at(self.home, 91.0, minutes_ago=300, name="b.jsonl")
        r = self.quota()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"^codex weekly limit: 5\.0% used, resets \d{4}-\d\d-\d\d \d\d:\d\d, read 1[23] min ago\n$")
        self.assertEqual(r.stderr, "")

    def test_a_newest_file_with_no_reading_falls_back_to_the_older_one(self):
        meta = json.dumps({"timestamp": stamp(now()), "type": "session_meta", "payload": {"id": "s"}})
        no_limits = json.dumps({"timestamp": stamp(now()), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}})
        session_file(self.home, "a.jsonl", [meta, no_limits], minutes_ago=1)
        quota_at(self.home, 91.0, minutes_ago=120, name="b.jsonl")
        r = self.quota()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("91.0% of the weekly Codex limit is used (limit for a bulk run: 80%): generate nothing in bulk; "
                      "mark unfixed scenes pending-codex", r.stderr)
        self.assertIn("91.0% used", r.stdout)
        self.assertIn("read 2.0 h ago", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_max_used_moves_the_limit(self):
        quota_at(self.home, 91.0)
        self.assertEqual(self.quota().returncode, 1)
        r = self.quota("--max-used", "95")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.quota("--max-used", "91")  # at the limit is over it
        self.assertEqual(r.returncode, 1)
        self.assertIn("limit for a bulk run: 91%", r.stderr)
        quota_at(self.home, 96.0, name="newer.jsonl", minutes_ago=1)
        r = self.quota("--max-used", "95")
        self.assertEqual(r.returncode, 1)
        self.assertIn("96.0% of the weekly Codex limit is used (limit for a single image: 95%)", r.stderr)

    def test_a_reading_older_than_max_age_does_not_count(self):
        quota_at(self.home, 5.0, minutes_ago=25 * 60)
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stderr, r"no quota reading newer than 24 h under .*sessions: run any codex command once, then retry")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.quota("--max-age-hours", "48").returncode, 0)
        r = self.quota("--max-age-hours", "0.5")
        self.assertIn("newer than 0.5 h", r.stderr)

    def test_an_old_reading_in_a_file_touched_since_does_not_count(self):
        # the file's own time is new (the session went on), but its last reading is two days old
        old = now() - datetime.timedelta(hours=50)
        session_file(self.home, "a.jsonl", [token_count(5.0, old, resets_in_hours=100)], minutes_ago=1)
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no quota reading newer than 24 h", r.stderr)

    def test_a_reading_from_before_the_reset_does_not_count(self):
        when = now() - datetime.timedelta(hours=3)
        session_file(self.home, "a.jsonl", [token_count(97.0, when, resets_in_hours=1)], minutes_ago=180)
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertIn("the limit has reset since the last quota reading", r.stderr)
        self.assertIn("run any codex command once, then retry", r.stderr)
        self.assertNotIn("97.0%", r.stderr, "a finished week's number must not be reported as this week's")

    def test_no_sessions_folder(self):
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Codex sessions folder at " + os.path.join(self.home, "sessions"), r.stderr)
        os.makedirs(os.path.join(self.home, "sessions"))
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no quota reading newer than 24 h", r.stderr)

    def test_codex_home_from_the_environment(self):
        quota_at(self.home, 5.0)
        r = run("quota.py", env={"CODEX_HOME": self.home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("5.0% used", r.stdout)

    def test_broken_lines_are_skipped(self):
        when = now() - datetime.timedelta(minutes=3)
        good = token_count(42.5, when)
        lines = ['{"timestamp": "2026-11-09T10:00:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_perc',
                 good,
                 "not json at all but it says \"rate_limits\"",
                 json.dumps(["rate_limits"]),
                 json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": "lots"}}}}),
                 json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": True}}}}),
                 json.dumps({"type": "response_item", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 99.0}}}}),
                 good[:len(good) // 2]]
        path = session_file(self.home, "a.jsonl", lines, minutes_ago=3)
        with open(path, "ab") as fh:
            fh.write(b'\xff\xfe"rate_limits" in bytes that are not UTF-8\n')
        t = datetime.datetime.now().timestamp() - 180
        os.utime(path, (t, t))
        r = self.quota()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("42.5% used", r.stdout)

    def test_the_last_reading_in_a_file_wins(self):
        when = now() - datetime.timedelta(minutes=30)
        session_file(self.home, "a.jsonl", [token_count(10.0, when), token_count(20.0, when + datetime.timedelta(minutes=10)),
                                            token_count(30.0, when + datetime.timedelta(minutes=20))], minutes_ago=10)
        self.assertIn("30.0% used", self.quota().stdout)

    def test_missing_secondary_and_credits_do_not_matter(self):
        when = now() - datetime.timedelta(minutes=2)
        bare = json.dumps({"timestamp": stamp(when), "type": "event_msg",
                           "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 7}}}})
        session_file(self.home, "a.jsonl", [bare], minutes_ago=2)
        r = self.quota("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual((got["used_percent"], got["window_minutes"], got["resets_at"]), (7.0, None, None))
        self.assertEqual(self.quota().stdout.split(", read")[0], "codex weekly limit: 7.0% used")
        session_file(self.home, "b.jsonl", [token_count(8.0, when, secondary={"used_percent": 99.0}, credits=None)], minutes_ago=1)
        self.assertIn("8.0% used", self.quota().stdout)

    def test_the_weekly_window_is_picked_by_its_length(self):
        when = now() - datetime.timedelta(minutes=2)
        five_hours = {"used_percent": 90.0, "window_minutes": 300, "resets_at": int(when.timestamp() + 3600)}
        weekly = {"used_percent": 12.0, "window_minutes": 10080, "resets_at": int(when.timestamp() + 86400)}
        session_file(self.home, "a.jsonl", [token_count(0.0, when, primary=five_hours, secondary=weekly)], minutes_ago=2)
        got = json.loads(self.quota("--json").stdout)
        self.assertEqual((got["used_percent"], got["window_minutes"]), (12.0, 10080))

    def test_a_line_with_no_timestamp_is_dated_by_its_file(self):
        bare = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 7}}}})
        session_file(self.home, "a.jsonl", [bare], minutes_ago=30)
        self.assertRegex(self.quota().stdout, r"read (29|30|31) min ago")

    def test_json_prints_one_object_with_the_five_keys(self):
        path = quota_at(self.home, 5.0, minutes_ago=12)
        r = self.quota("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        got = json.loads(r.stdout)
        self.assertEqual(sorted(got), ["file", "read_at", "resets_at", "used_percent", "window_minutes"])
        self.assertEqual((got["used_percent"], got["window_minutes"], got["file"]), (5.0, 10080, path))
        for key in ("read_at", "resets_at"):
            self.assertRegex(got[key], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        quota_at(self.home, 85.0, minutes_ago=1, name="newer.jsonl")
        r = self.quota("--json")  # over the limit: the numbers are still printed, and the exit says no
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["used_percent"], 85.0)

    def test_lines_backwards_gives_every_line_last_first(self):
        import io
        import random
        rng = random.Random(5)
        pieces = [b"", b"x", b"\r", b"a" * 70, b"\xff\xfe", b"b" * 300, b'{"rate_limits": 1}']
        for case in range(300):
            data = b"\n".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
            if rng.random() < 0.5:
                data += b"\n"
            block = rng.choice([1, 2, 3, 7, 64, 1 << 16])
            want = [line.rstrip(b"\r") for line in data.split(b"\n")][::-1] if data else []
            got = list(quota.lines_backwards(io.BytesIO(data), block))
            self.assertEqual(got, want, (case, data, block))

    def test_the_last_reading_of_a_long_file_is_read_from_its_end(self):
        import io

        class Counted(io.BytesIO):
            taken = 0

            def read(self, n=-1):
                got = super().read(n)
                Counted.taken += len(got)
                return got

        when = now() - datetime.timedelta(minutes=3)
        filler = json.dumps({"type": "response_item", "payload": {"type": "message", "content": "y" * 200}}).encode()
        body = b"\n".join([token_count(10.0, when).encode()] + [filler] * 40000 + [token_count(55.0, when).encode()]
                          + [filler] * 20) + b"\n"
        fh = Counted(body)
        reading = next(quota.parse_reading(raw) for raw in quota.lines_backwards(fh) if quota.parse_reading(raw))
        self.assertEqual(reading[0]["used_percent"], 55.0)
        self.assertLess(Counted.taken, 2 * quota.BLOCK, "only the tail of a %d MB file is read" % (len(body) >> 20))
        path = session_file(self.home, "long.jsonl", [], minutes_ago=3)
        with open(path, "wb") as fh:
            fh.write(body)
        self.assertEqual(quota.last_reading(path)[0]["used_percent"], 55.0)

    def test_an_older_codex_that_says_resets_in_seconds(self):
        when = now() - datetime.timedelta(minutes=10)
        older = {"used_percent": 33.0, "window_minutes": 10080, "resets_in_seconds": 7200}
        line = json.dumps({"timestamp": stamp(when), "type": "event_msg",
                           "payload": {"type": "token_count", "rate_limits": {"primary": older}}})
        session_file(self.home, "a.jsonl", [line], minutes_ago=10)
        r = self.quota("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["used_percent"], 33.0)
        self.assertEqual(got["resets_at"], (when.replace(microsecond=0) + datetime.timedelta(seconds=7200)).isoformat())
        older["resets_in_seconds"] = 60  # reset a minute after the reading, nine minutes ago
        session_file(self.home, "a.jsonl", [json.dumps({"timestamp": stamp(when), "type": "event_msg",
                                                        "payload": {"type": "token_count", "rate_limits": {"primary": older}}})], minutes_ago=10)
        r = self.quota()
        self.assertEqual(r.returncode, 1)
        self.assertIn("the limit has reset since the last quota reading", r.stderr)

    def test_check_is_the_gate_the_other_tools_call(self):
        quota_at(self.home, 85.0)
        with self.assertRaises(quota.QuotaError) as ctx:
            quota.check(80, self.home)
        self.assertIn("85.0% of the weekly Codex limit is used (limit for a bulk run: 80%)", str(ctx.exception))
        self.assertEqual(quota.check(95, self.home, bulk=False)["used_percent"], 85.0)
        with self.assertRaises(quota.QuotaError) as ctx:
            quota.check(80, os.path.join(self.tmp.name, "nowhere"))
        self.assertIn("no Codex sessions folder", str(ctx.exception))
        self.assertEqual((quota.BULK_MAX_USED, quota.SINGLE_MAX_USED, quota.BULK_OVER), (80.0, 95.0, 5))


if __name__ == "__main__":
    unittest.main()
