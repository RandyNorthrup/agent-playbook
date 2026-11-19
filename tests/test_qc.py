"""QC: the filters, qc.py check and its receipts, the audit.py send gate, and the recipes pb.py checks."""
import contextlib
import datetime as dt
import difflib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import unittest

from pbtest import PLAYBOOK, PlaybookCase
from lib import qcengine


class TestQcEngine(unittest.TestCase):
    def test_hash_ignores_whitespace_and_line_ends(self):
        self.assertEqual(qcengine.text_hash("one  two\r\nthree\n"), qcengine.text_hash("one two three"))
        self.assertNotEqual(qcengine.text_hash("one two"), qcengine.text_hash("one three"))

    def test_norm_url_keeps_youtube_video(self):
        a = qcengine.norm_url("https://www.youtube.com/watch?v=RW6u1bGc36c&lc=Ugw")
        self.assertEqual(a, qcengine.norm_url("https://youtu.be/RW6u1bGc36c"))
        self.assertNotEqual(a, qcengine.norm_url("https://www.youtube.com/watch?v=6IoraEc2kfM"))
        self.assertEqual(qcengine.norm_url("https://x.com/a/status/1?s=20"), "x.com/a/status/1")

    def test_author_from_links(self):
        self.assertEqual(qcengine.author_of("https://x.com/Dev_1/status/123?s=20"), "dev_1")
        self.assertEqual(qcengine.author_of("https://bsky.app/profile/a.bsky.social/post/3k"), "a.bsky.social")
        self.assertEqual(qcengine.author_of("https://www.facebook.com/groups/x/posts/1"), "")

    def test_verdict(self):
        self.assertEqual(qcengine.verdict([]), "pass")
        self.assertEqual(qcengine.verdict([{"severity": "warn"}]), "warn")
        self.assertEqual(qcengine.verdict([{"severity": "ask"}]), "block")
        self.assertEqual(qcengine.verdict([{"severity": "ask", "answered": "README"}]), "pass")
        self.assertEqual(qcengine.verdict([{"severity": "block", "waived": "they asked"}]), "pass")
        self.assertEqual(qcengine.verdict([{"severity": "block"}, {"severity": "warn"}]), "block")

    def test_validate_wants_examples_and_known_types(self):
        bad = [{"id": "a", "type": "nope", "severity": "block", "message": "m", "src": "s", "examples": {"bad": [{}], "good": [{}]}},
               {"id": "b", "type": "pattern", "severity": "warn", "message": "m", "src": "s", "patterns": ["("], "examples": {"bad": [{}]}},
               {"id": "b", "type": "pattern", "severity": "loud", "message": "", "examples": {"bad": [{}], "good": [{}]}}]
        problems = " | ".join(qcengine.validate(bad, {}))
        for want in ("unknown type", "bad pattern", "bad and one good example", "duplicate id", "severity must be", "needs a message and a src"):
            self.assertIn(want, problems)

    def test_near_duplicate_reports_exactly_what_a_full_comparison_would(self):
        """The duplicate check skips texts whose quick upper bound is below the threshold; the findings must be the
        same as comparing every text in full (the old way), on random drafts and histories."""
        f = {"id": "near-duplicate", "type": "duplicate", "severity": "warn", "message": "close to", "warn_ratio": 0.8,
             "block_ratio": 0.92, "days": 14, "min_len": 40}
        words = "notes markdown folder grep git backup sync plugin release editor search tags link export".split()
        rnd = random.Random(7)
        now = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)

        def reference(d, hist):
            mine = " ".join(qcengine.URL_RE.sub(" ", d.text).lower().split())
            for it in hist:  # newest first, as Context.sent orders them
                other = " ".join(qcengine.URL_RE.sub(" ", it["text"]).lower().split())
                if len(other) < 40:
                    continue
                r = difflib.SequenceMatcher(None, mine, other).ratio()
                same = it["platform"].lower() == d.platform.lower() and it.get("place", "").lower() == d.place.lower()
                if same and r >= 0.92:
                    return [("block", it["id"])]
                if r >= 0.8:
                    return [("warn", it["id"])]
            return []

        hits = 0
        for trial in range(60):
            base = " ".join(rnd.choice(words) for _ in range(rnd.randint(8, 30)))
            hist = []
            for i in range(30):
                t = base.split()
                for _ in range(rnd.choice([0, 1, 2, 5, 15])):  # some near copies, some far
                    t[rnd.randrange(len(t))] = rnd.choice(words)
                at = (now - dt.timedelta(hours=i + 1)).isoformat()
                hist.append({"id": "h%d" % i, "status": "live", "account": "@sam", "platform": rnd.choice(["X", "Bluesky"]),
                             "place": "", "at": at, "text": " ".join(t) if rnd.random() < 0.7 else
                             " ".join(rnd.choice(words) for _ in range(20))})
            d = qcengine.Draft(base, "X", "@sam", "post", at=now.isoformat())
            got = [(s, m.split(" the same as ")[1].split(" ")[0]) for s, m in
                   [(r if isinstance(r, tuple) else ("warn", r)) for r in qcengine.t_duplicate(f, d, qcengine.Context(hist, {}))]]
            want = reference(d, hist) if len(" ".join(base.split())) >= 40 else []
            self.assertEqual(got, want, base)
            hits += bool(want)
        self.assertGreater(hits, 10)  # the comparison was exercised, not skipped

    def test_real_filters_pass_their_own_examples(self):
        r = subprocess.run([sys.executable, str(PLAYBOOK / "qc.py"), "--root", str(PLAYBOOK), "selftest"],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("all pass", r.stdout)


class QcCase(PlaybookCase):
    """The fixture playbook with the real QC filters and lists (the fixture's only account is X @sam)."""

    def setUp(self):
        super().setUp()
        shutil.copytree(str(PLAYBOOK / "qc"), str(self.root / "qc"))
        self.n = 0

    def qc(self, *args):
        return self._run("qc.py", args)

    def draft(self, text):
        self.n += 1
        p = self.tmp / ("draft%d.txt" % self.n)
        p.write_text(text, encoding="utf-8")
        return p

    def check(self, path, *extra, **kw):
        args = ["check", "--platform", kw.get("platform", "X"), "--account", kw.get("account", "@sam"),
                "--action", kw.get("action", "post"), "--text-file", path]
        return self.qc(*(args + list(extra)))

    def add(self, path, *extra, **kw):
        args = ["add", "--platform", kw.get("platform", "X"), "--account", kw.get("account", "@sam"),
                "--action", kw.get("action", "post"), "--text-file", path]
        return self.audit(*(args + list(extra)))

    def last_item(self):
        rows = [json.loads(line) for line in (self.root / "audit" / "posts.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()]
        return [r for r in rows if r.get("kind") == "item"][-1]


class TestQcGate(QcCase):
    def test_check_then_add_records_the_receipt(self):
        d = self.draft("Hot take: the best agent prompt is a good commit history.")
        r = self.check(d)
        self.assertRan(r)
        self.assertIn("PASS receipt", r.stdout)
        receipt = r.stdout.split("receipt ")[1].split(":")[0]
        self.assertRan(self.add(d))
        self.assertEqual(self.last_item()["qc"], receipt)

    def test_whitespace_edits_still_match(self):
        self.assertRan(self.check(self.draft("Small diffs  beat big ones.\r\n")))
        self.assertRan(self.add(self.draft("Small diffs beat big ones.")))

    def test_add_refuses_text_qc_never_saw(self):
        d = self.draft("A post nobody checked.")
        r = self.add(d)
        self.assertRan(r, 1)
        self.assertIn("no QC receipt", r.stderr)
        r = self.add(d, "--override", "owner: posted by hand, logging it")
        self.assertRan(r)
        self.assertIn("QC override recorded", r.stderr)
        self.assertEqual(self.last_item()["qc_override"], "owner: posted by hand, logging it")

    def test_receipt_is_per_account(self):
        d = self.draft("Checked for one account only.")
        self.assertRan(self.check(d))
        r = self.add(d, "--force", account="@other")
        self.assertRan(r, 1)
        self.assertIn("no QC receipt", r.stderr)

    def test_blocked_text_is_refused(self):
        d = self.draft("Globex just launched cloud notes for everyone.")
        r = self.check(d)
        self.assertRan(r, 1)
        self.assertIn("BLOCK  brand-launch", r.stdout)
        r = self.add(d)
        self.assertRan(r, 1)
        self.assertIn("QC blocked this text", r.stderr)

    def test_a_damaged_receipt_clears_nothing(self):
        d = self.draft("Checked, then the receipt got damaged.")
        self.assertRan(self.check(d))
        log = self.root / "audit" / "qc.jsonl"
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        for damage in ({"verdict": None}, {"verdict": "ok"}, {"id": ""}):
            log.write_text(json.dumps(dict(rows[-1], **damage)) + "\n", encoding="utf-8")
            r = self.add(d)
            self.assertRan(r, 1)
            self.assertIn("receipt for this text can't be read", r.stderr)
        log.write_text(json.dumps(rows[-1]) + "\n", encoding="utf-8")
        self.assertRan(self.add(d))

    def test_asks_block_until_answered(self):
        d = self.draft("A new study on how teams review AI code.")
        r = self.check(d, "--recipe", "news-share")
        self.assertRan(r, 1)
        self.assertIn("ASK    source-read", r.stdout)
        r = self.check(d, "--recipe", "news-share", "--ok", "source-read=https://example.org/study, read in full",
                       "--ok", "news-screen=a finding, no product, no competitor")
        self.assertRan(r)
        self.assertIn("answered: https://example.org/study", r.stdout)
        self.assertRan(self.check(d, "--ok", "nosuch=x"), 2)
        self.assertRan(self.check(d, "--ok", "source-read"), 2)

    def test_reply_needs_target_and_author(self):
        d = self.draft("Good point, thanks.")
        self.assertRan(self.check(d, action="reply"), 2)
        self.assertRan(self.check(d, "--target", "https://x.com/dev/status/1", action="reply"))
        r = self.check(d, "--target", "https://www.facebook.com/groups/g/posts/1", action="reply")
        self.assertRan(r, 2)
        self.assertIn("--target-author", r.stderr)

    def test_ignore_list_and_double_reply(self):
        d = self.draft("Fair point, thanks for reading.")
        r = self.check(d, "--target", "https://www.facebook.com/groups/g/posts/1", "--target-author", "Pat Example", action="reply")
        self.assertRan(r, 1)
        self.assertIn("ignore-list", r.stdout)
        t = "https://x.com/dev/status/77"
        self.assertRan(self.check(d, "--target", t, action="reply"))
        self.assertRan(self.add(d, "--target", t, action="reply"))
        r = self.check(self.draft("A second answer to the same post."), "--target", t + "?s=20", action="reply")
        self.assertRan(r, 1)
        self.assertIn("already-replied", r.stdout)

    def test_unsent_items_need_no_receipt(self):
        self.assertRan(self.audit("add", "--platform", "X", "--account", "@sam", "--action", "post", "--text", "never sent",
                                  "--status", "skipped", "--notes", "owner said no"))

    def test_receipts_and_filters_listing(self):
        self.assertRan(self.check(self.draft("Listing test.")))
        r = self.qc("receipts")
        self.assertRan(r)
        self.assertIn("@sam", r.stdout)
        r = self.qc("filters", "--platform", "X", "--action", "reply")
        self.assertRan(r)
        self.assertIn("already-replied", r.stdout)
        self.assertNotIn("new-post-rule", r.stdout)
        self.assertRan(self.qc("explain", "ignore-list"))
        self.assertRan(self.qc("explain", "nosuch"), 2)

    def test_report_survives_a_console_that_cannot_print_the_draft(self):
        """Windows pipes default to a code page with no emoji. The report must still print (in UTF-8), with the
        right exit code and a receipt, instead of crashing mid-report."""
        text = "Notes \U0001F600 guaranteed to keep your plain markdown files where you put them"
        lists = self.root / "qc" / "lists.json"  # a warning that quotes the draft, whatever the playbook's own lists say
        data = json.loads(lists.read_text(encoding="utf-8"))
        data.setdefault("never_say", {}).setdefault("_all", []).append({"pattern": "guaranteed", "why": "test", "severity": "warn"})
        lists.write_text(json.dumps(data), encoding="utf-8")
        r = subprocess.run([sys.executable, str(PLAYBOOK / "qc.py"), "--root", str(self.root), "check", "--platform", "X",
                            "--account", "@sam", "--action", "post", "--text", text], capture_output=True, timeout=120,
                           env=dict(os.environ, PYTHONIOENCODING="cp1252"))
        out, err = r.stdout.decode("utf-8"), r.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", err)
        self.assertEqual(r.returncode, 0, out + err)
        self.assertIn("\U0001F600", out)  # the never-say warning quotes the draft
        self.assertIn("receipt qc-", out)
        self.assertTrue((self.root / "audit" / "qc.jsonl").exists())

    def test_receipts_in_the_same_second_get_their_own_ids(self):
        """Checking the same words twice in one second (the second time with --ok) gives two receipts."""
        import qc
        from lib import auditlog
        p = self.draft("The same words, checked twice in the same second, about notes in folders.")
        real = auditlog.now
        auditlog.now = lambda: "2026-11-10T12:00:00+00:00"
        try:
            for extra in ([], ["--ok", "product-news=checked the release notes"]):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    qc.main(["--root", str(self.root), "check", "--platform", "X", "--account", "@sam", "--action", "post",
                             "--text-file", str(p)] + extra)
        finally:
            auditlog.now = real
        ids = [json.loads(line)["id"] for line in (self.root / "audit" / "qc.jsonl").read_text(encoding="utf-8").split("\n") if line]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        for rid in ids:
            self.assertRegex(rid, r"^qc-20261110T120000-[0-9a-f]{6}$")

    def test_retired_and_skipped_accounts_send_nothing(self):
        acct = json.loads((self.root / "store" / "accounts" / "x-sam.json").read_text(encoding="utf-8"))
        acct.update(handle="@old_sam", label="Old account", status="retired")
        (self.root / "store" / "accounts" / "x-old.json").write_text(json.dumps(acct), encoding="utf-8")
        p = self.draft("A post from an account that was retired last year, about notes in folders.")
        r = self.check(p, account="@old_sam")
        self.assertRan(r, 1)
        self.assertIn("is marked retired in the accounts registry", r.stdout)
        r = self.add(p, "--override", "testing", account="@old_sam")
        self.assertRan(r, 1)
        self.assertIn("is marked retired", r.stderr)
        self.assertRan(self.add(p, "--override", "testing", "--force", account="@old_sam"))
        self.assertRan(self.add(p, "--status", "skipped", "--notes", "not sent", account="Old account"))  # nothing went out
        r = self.audit("gap", "--platform", "X", "--account", "@old_sam", "--action", "reply")
        self.assertRan(r, 1)
        self.assertIn("is marked retired", r.stderr)
        acct.update(handle="@old_sam", label="Old account again", status="active")  # one live record for the name is enough
        (self.root / "store" / "accounts" / "x-old2.json").write_text(json.dumps(acct), encoding="utf-8")
        self.assertRan(self.audit("gap", "--platform", "X", "--account", "@old_sam", "--action", "reply"))

    def test_receipt_matches_words_with_a_unicode_line_separator(self):
        p = self.draft("One line and the next one, both in the words of this post about folders.")
        self.assertRan(self.check(p))
        self.assertRan(self.add(p))
        self.assertTrue(self.last_item().get("qc", "").startswith("qc-"))
        with open(str(self.root / "audit" / "qc.jsonl"), "ab") as fh:
            fh.write(b'not json\n[1]\n{"kind": "qc"}\n{"at": 5, "findings": "x"}\n')
        r = self.qc("receipts")
        self.assertRan(r)
        self.assertIn("@sam", r.stdout)

    def test_malformed_filters_are_named_not_a_crash(self):
        """filters.json and lists.json are edited by hand: a wrong shape is a message naming the filter or list
        entry, from qc.py check (exit 2) and pb.py check (exit 1), never a traceback mid-check."""
        fpath, lpath = self.root / "qc" / "filters.json", self.root / "qc" / "lists.json"
        filters, lists = fpath.read_text(encoding="utf-8"), lpath.read_text(encoding="utf-8")

        def caps(d):
            return [x for x in d["filters"] if x["type"] == "caps"][0]["caps"]
        cases = [(fpath, lambda d: d["filters"].append("oops"), "filter #"),
                 (fpath, lambda d: d["filters"][0].__setitem__("applies", ["X"]), "applies must be an object"),
                 (fpath, lambda d: d["filters"][0].__setitem__("examples", ["bad"]), "bad and one good example"),
                 (fpath, lambda d: caps(d).append({"max": 1}), "needs a platform and a max"),
                 (fpath, lambda d: d["filters"][0].__setitem__("patterns", "x("), "patterns must be a list"),
                 (lpath, lambda d: d["never_say"]["_all"].append({"pattern": "(", "why": "x"}), "never_say._all"),
                 (lpath, lambda d: d["never_say"]["_all"].append({"pattern": "x"}), "needs a \"pattern\" and a \"why\""),
                 (lpath, lambda d: d["ad_copy"].append({"pattern": "[unclosed"}), "lists.json ad_copy"),
                 (lpath, lambda d: d["projects"].__setitem__("app", {"names": ["(x"]}), "projects.app.names"),
                 (lpath, lambda d: d.__setitem__("ignore", "Pat Example"), "lists.json ignore must be a list"),
                 (lpath, lambda d: d["ignore"].append(5), "must be text or {")]  # whatever the list's length
        for path, mutate, want in cases:
            data = json.loads(filters if path == fpath else lists)
            mutate(data)
            path.write_text(json.dumps(data), encoding="utf-8")
            r = self.check(self.draft("A plain draft about notes and folders, nothing more."))
            self.assertRan(r, 2)
            self.assertIn(want, r.stderr)
            r = self.pb("check")
            self.assertRan(r, 1)
            self.assertIn(want, r.stdout)
            fpath.write_text(filters, encoding="utf-8")
            lpath.write_text(lists, encoding="utf-8")
        self.assertRan(self.check(self.draft("A plain draft about notes and folders, nothing more.")))


class TestRecipesInCheck(QcCase):
    def write_recipes(self, recipes):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "recipes.json").write_text(json.dumps({"recipes": recipes}), encoding="utf-8")

    def test_recipe_refs_are_checked(self):
        self.write_recipes({"job": {"title": "A job", "when": "now", "flows": ["posting", "nosuch"],
                                    "steps": [{"id": "a", "text": "Do it", "flow": "posting"},
                                              {"id": "a", "text": "Again", "part": "nopart"}]}})
        r = self.pb("check")
        self.assertRan(r, 1)
        for want in ("recipe-ref", "no flow 'nosuch'", "duplicate step id", "no part 'nopart'"):
            self.assertIn(want, r.stdout)

    def test_good_recipe_and_catalog(self):
        self.write_recipes({"job": {"title": "A job", "when": "when asked", "actions": ["post"], "flows": ["posting", "welcome"],
                                    "steps": [{"id": "a", "text": "Do it", "flow": "posting"}]}})
        self.assertRan(self.pb("check", "--strict"))
        r = self.pb("catalog")
        self.assertRan(r)
        self.assertIn("job", r.stdout)
        r = self.pb("catalog", "job")
        self.assertRan(r)
        self.assertIn("flows to read:        posting, welcome", r.stdout)
        self.assertIn("ad-copy", r.stdout)
        self.assertRan(self.pb("catalog", "nosuch"), 1)
        self.assertIn("Where a new piece of knowledge goes", self.pb("catalog", "--pieces").stdout)

    def test_flow_no_recipe_warns(self):
        self.write_recipes({"job": {"title": "A job", "when": "when asked", "flows": ["posting"],
                                    "steps": [{"id": "a", "text": "Do it", "flow": "posting"}]}})
        r = self.pb("check", "-w")
        if "welcome" in r.stdout:
            self.assertIn("flow-no-recipe", r.stdout)
        # named like every other finding: the file's path from the playbook folder
        self.assertIn("store/flows/welcome.json: (document): warning [flow-no-recipe]", r.stdout)

    def test_malformed_recipes_are_problems_not_a_crash(self):
        self.write_recipes({"text": "not a recipe", "flat": {"title": "T", "when": "w", "steps": "do it"},
                            "odd": {"title": "T", "when": "w", "flows": [5], "steps": [{"id": "a", "text": "x", "flow": "posting"}]},
                            "mixed": {"title": "T", "when": "w", "steps": ["a step", {"id": "b", "text": "y", "flow": ["posting"]}]},
                            "refs": {"title": "T", "when": "w", "steps": [{"id": ["a"], "text": "x", "part": 5}]}})
        r = self.pb("check")
        self.assertRan(r, 1)
        for want in ("recipes.text: error [recipe-field]", "recipes.flat.steps: error [recipe-field] must be a list",
                     "recipes.odd.flows: error [recipe-field] must be a list of flow ids", "recipes.mixed.steps[0]: error [recipe-step]",
                     "no part 5"):
            self.assertIn(want, r.stdout)
        self.assertRan(self.pb("catalog"))
        for flow in ("[1, 2]", '{"nodes": "x"}'):  # a flow that breaks the schema is a schema error, not a crash
            (self.root / "store" / "flows" / "junk.json").write_text(flow, encoding="utf-8")
            r = self.pb("check")
            self.assertRan(r, 1)
            self.assertIn("store/flows/junk.json", r.stdout)

    def test_broken_filter_fails_check(self):
        f = self.root / "qc" / "filters.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data["filters"][0]["examples"]["good"] = data["filters"][0]["examples"]["bad"]
        f.write_text(json.dumps(data), encoding="utf-8")
        r = self.pb("check")
        self.assertRan(r, 1)
        self.assertIn("qc-selftest", r.stdout)


if __name__ == "__main__":
    unittest.main()
