"""pb.py from the command line, against a copy of the fixture playbook."""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from pbtest import PLAYBOOK, ROOT, PlaybookCase, good_flow
import pb
from lib import store as storelib


class TestPbRead(PlaybookCase):
    def test_list_is_the_default_and_groups_flows(self):
        for args in ((), ("list",)):
            r = self.pb(*args)
            self.assertRan(r)
            self.assertIn("Start here", r.stdout)
            self.assertRegex(r.stdout, r"welcome\s+Welcome\s+6 nodes")
            self.assertRegex(r.stdout, r"posting\s+Posting\s+4 nodes")
            self.assertLess(r.stdout.index("welcome"), r.stdout.index("posting"))

    def test_show_is_compact_and_complete(self):
        r = self.pb("show", "welcome")
        self.assertRan(r)
        out = r.stdout
        self.assertRegex(out.splitlines()[0], r"^# Welcome \[Start here\] id=welcome rev=[0-9a-f]{16}$")
        self.assertIn("python pb.py show <id>", out)
        self.assertIn("[START] Begin a run | Read this first", out)
        self.assertIn("\n  + [RULE] Say only what is true\n", out)  # the attached rule hangs under its step
        self.assertIn("  (post) [GOTO] Posting | When there is something new -> posting\n", out)  # a goto is one line
        self.assertIn("  folders: Helper scripts -> C:\\fixture\\scripts | links: Example https://example.com/rules\n", out)
        self.assertIn("  [ ] Rules read\n", out)
        for hidden in ("session:fixture", "always read the rules first", "origin", "src"):
            self.assertNotIn(hidden, out)  # provenance is not printed
        for line in out.splitlines():
            indent = len(line) - len(line.lstrip(" "))
            self.assertLessEqual(indent, 6, line)

    def test_show_names_where_a_joining_path_goes_on(self):
        """A connection into a step that is already printed (a loop, or two branches meeting) is a line of its
        own, with or without a label: an unlabelled one used to print nothing, so the path looked like a dead end."""
        doc = self.doc("flows", "welcome")
        doc["edges"].append({"from": "welcome-done", "to": "welcome-rules", "type": "flow"})
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("w.json", doc)))
        out = self.pb("show", "welcome").stdout
        title = [n for n in doc["nodes"] if n["id"] == "welcome-rules"][0]["title"]
        self.assertIn("-> continue at: %s (above)\n" % title, out)
        doc["edges"][-1]["label"] = "again"
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("w.json", doc)))
        self.assertIn("(again) -> continue at: %s (above)\n" % title, self.pb("show", "welcome").stdout)

    def test_show_resolves_part_references(self):
        r = self.pb("show", "posting")
        self.assertRan(r)
        self.assertIn("[QC] Read the field back | Before pressing Post", r.stdout)
        self.assertIn("Read what is in the box before posting it.", r.stdout)
        self.assertIn("[ ] Text matches the draft", r.stdout)
        self.assertIn("account: X @sam (active)", r.stdout)

    def test_show_json(self):
        r = self.pb("show", "posting", "--json")
        self.assertRan(r)
        out = json.loads(r.stdout)
        self.assertEqual((out["col"], out["id"]), ("flows", "posting"))
        self.assertEqual(out["doc"], self.doc("flows", "posting"))
        self.assertEqual(out["rev"], self.pb("rev", "flows", "posting").stdout.strip())
        stored = [n for n in out["doc"]["nodes"] if n["id"] == "posting-readback"][0]
        shown = [n for n in out["resolved"]["nodes"] if n["id"] == "posting-readback"][0]
        self.assertNotIn("title", stored)
        self.assertEqual(shown["title"], "Read the field back")
        self.assertNotIn("resolved", json.loads(self.pb("show", "welcome", "--json").stdout))

    def test_show_unknown_flow(self):
        r = self.pb("show", "nosuch")
        self.assertRan(r, 1)
        self.assertIn("no flow 'nosuch'", r.stderr)
        self.assertRan(self.pb("show", "welc"))  # a unique part of the id or name is enough

    def test_search_accounts_folders_parts_edited_changes_export(self):
        r = self.pb("search", "field", "back")
        self.assertRan(r)
        self.assertIn("posting", r.stdout)  # found through the part reference
        self.assertIn("part-read-back", r.stdout)
        r = self.pb("accounts", "x")
        self.assertRan(r)
        self.assertIn("@sam", r.stdout)
        self.assertEqual(self.pb("accounts", "nomatch").stdout, "")
        r = self.pb("folders")
        self.assertRan(r)
        self.assertIn("used by: welcome", r.stdout)
        r = self.pb("parts")
        self.assertRan(r)
        self.assertIn("used by: posting", r.stdout)
        self.assertEqual(self.pb("edited").stdout, "")
        doc = self.doc("flows", "welcome")
        doc["nodes"][1]["editedAt"] = "2026-03-04T05:06:07.000Z"
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("w.json", doc)))
        self.assertIn("Read the rules", self.pb("edited").stdout)
        self.assertIn("Read the rules", self.pb("edited", "2026-03-01").stdout)
        self.assertEqual(self.pb("edited", "2026-04-01").stdout, "")
        self.assertRan(self.pb("edited", "garbage"), 2)
        r = self.pb("changes")
        self.assertRan(r)
        self.assertIn("flows/welcome", r.stdout)
        self.assertRan(self.pb("changes", "abc"), 2)
        r = self.pb("export")
        self.assertRan(r)
        exported = json.loads((self.root / "store.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(exported), ["accounts", "flows", "folders", "parts"])
        self.assertEqual(exported["flows"]["posting"], self.doc("flows", "posting"))
        self.assertNotIn(b"\r", (self.root / "store.json").read_bytes())

    def test_corrupt_file_is_named_and_nothing_crashes(self):
        (self.root / "store" / "flows" / "broken.json").write_bytes(b'{"name": "broken", "nodes": [')
        for args in (("list",), ("show", "welcome"), ("search", "rules"), ("folders",), ("parts",), ("accounts",), ("edited",), ("export",)):
            r = self.pb(*args)
            self.assertRan(r)
            self.assertIn("store/flows/broken.json", r.stderr)
        r = self.pb("check")
        self.assertRan(r, 1)
        self.assertIn("store/flows/broken.json: (document): error [unreadable]", r.stdout)

    def test_show_survives_a_document_that_breaks_the_schema(self):
        (self.root / "store" / "flows" / "odd.json").write_bytes(
            b'{"name": "Odd", "nodes": [{"kind": "start", "title": "no id"}, {"id": "b", "kind": "step", "title": "B", "x": null, "y": 0}, 7], "edges": [{"from": "b"}, 3]}')
        r = self.pb("show", "odd")
        self.assertRan(r)
        self.assertIn("[STEP] B", r.stdout)
        self.assertIn("breaks the schema", r.stderr)
        self.assertRan(self.pb("list"))
        self.assertRan(self.pb("search", "b"))


class TestPbCheck(PlaybookCase):
    def test_clean_store(self):
        r = self.pb("check")
        self.assertRan(r)
        self.assertRegex(r.stdout, r"2 flows, 10 nodes, 1 accounts, 1 folders, 1 parts: 0 problem\(s\), 0 warning\(s\)")
        self.assertRan(self.pb("check", "--strict"))

    def test_errors_exit_1_with_file_path_message(self):
        doc = self.doc("flows", "welcome")
        doc["nodes"][1]["kind"] = "weird"
        doc["extra"] = 1
        (self.root / "store" / "flows" / "welcome.json").write_text(json.dumps(doc), encoding="utf-8")
        r = self.pb("check")
        self.assertRan(r, 1)
        self.assertIn("store/flows/welcome.json: nodes/welcome-rules/kind: error [schema] 'weird' is not one of", r.stdout)
        self.assertIn("store/flows/welcome.json: (document): error [schema] unknown key 'extra'", r.stdout)
        self.assertRegex(r.stdout, r"2 problem\(s\) \(schema 2\)")

    def test_warnings_pass_unless_strict(self):
        doc = self.doc("flows", "welcome")
        doc["nodes"][1]["body"] = 'Sam, 11-08: "do it this way".'
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("w.json", doc)))
        r = self.pb("check")
        self.assertRan(r)
        self.assertRegex(r.stdout, r"0 problem\(s\), 3 warning\(s\) \(attribution 1, date 1, owner-name 1\)")
        self.assertNotIn("[attribution]", r.stdout)
        r = self.pb("check", "-w")
        self.assertRan(r)
        self.assertIn("store/flows/welcome.json: nodes/welcome-rules: warning [attribution]", r.stdout)
        r = self.pb("check", "--strict")
        self.assertRan(r, 1)
        self.assertIn("warning [date]", r.stdout)
        r = self.pb("check", "-w", "--rule", "date")
        self.assertNotIn("[attribution]", r.stdout)
        self.assertIn("[date]", r.stdout)

    def test_garbage_documents_are_problems(self):
        """The old check printed '0 problem(s)' with an empty flow, a NaN flow and a garbage account in the store."""
        (self.root / "store" / "flows" / "zz-empty.json").write_bytes(b"{}")
        (self.root / "store" / "flows" / "zz-nan.json").write_bytes(b'{"a": NaN, "b": Infinity}')
        (self.root / "store" / "accounts" / "zz-acct.json").write_bytes(b'{"anything": {"goes": true}}')
        r = self.pb("check")
        self.assertRan(r, 1)
        for name in ("store/flows/zz-empty.json", "store/flows/zz-nan.json", "store/accounts/zz-acct.json"):
            self.assertIn(name, r.stdout)
        self.assertNotIn("0 problem(s)", r.stdout)


class TestPbWrite(PlaybookCase):
    def test_put_validates_versions_and_reports_the_revision(self):
        r = self.pb("put", "flows", "sample", self.write_json("s.json", good_flow()))
        self.assertRan(r)
        self.assertRegex(r.stdout, r"^saved flows/sample rev [0-9a-f]{16}\n$")
        r2 = self.pb("put", "flows", "sample", self.write_json("s.json", good_flow()))
        self.assertRan(r2)
        self.assertIn("unchanged", r2.stdout)
        bad = good_flow()
        bad["nodes"][0]["kind"] = "weird"
        bad["mystery"] = 1
        r = self.pb("put", "flows", "sample", self.write_json("bad.json", bad))
        self.assertRan(r, 1)
        self.assertIn("breaks the schema", r.stderr)
        self.assertIn("store/flows/sample.json: nodes/s-start/kind: 'weird' is not one of", r.stderr)
        self.assertIn("unknown key 'mystery'", r.stderr)
        self.assertEqual(self.doc("flows", "sample"), good_flow())

    def test_put_bad_input_files(self):
        nan = self.tmp / "nan.json"
        nan.write_text('{"a": NaN}', encoding="utf-8")
        notjson = self.tmp / "bad.json"
        notjson.write_text("{bad", encoding="utf-8")
        for path, expect in ((nan, "NaN"), (notjson, "not valid JSON"), (self.tmp / "missing.json", "cannot read"), (self.write_json("arr.json", [1, 2]), "must be an object")):
            r = self.pb("put", "flows", "sample", path)
            self.assertRan(r, 1)
            self.assertIn(expect, r.stderr)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_put_refuses_bad_ids_and_cannot_leave_the_store(self):
        f = self.write_json("s.json", good_flow())
        for bad in ("../../zz-escape", "a b", "CON", "UPPER", "a:b", "..", "history"):
            r = self.pb("put", "flows", bad, f)
            self.assertRan(r, 1)
        self.assertFalse((self.root / "zz-escape.json").exists())
        self.assertEqual(sorted(os.listdir(str(self.root / "store" / "flows"))), ["posting.json", "welcome.json"])
        self.assertRan(self.pb("delete", "flows", "../../zz-escape", "--reason", "x"), 1)

    def test_usage_errors_exit_2(self):
        f = self.write_json("s.json", good_flow())
        for args in (("bogus",), ("put", "flow", "sample", f), ("put", "flows", "sample"), ("put", "flows", "sample", f, "extra"),
                     ("delete", "flow", "welcome", "--reason", "x"), ("versions", "flows"), ("show",), ("search",), ("restore", "flows", "welcome"),
                     ("put", "flows", "sample", f, "--reason"), ("check", "--nope")):
            r = self.pb(*args)
            self.assertRan(r, 2)
            self.assertIn("usage:", r.stderr)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())
        self.assertRan(self.pb("--help"))
        self.assertRan(self.pb("put", "--help"))

    def test_reason_forms_and_delete(self):
        r = self.pb("delete", "parts", "part-read-back")
        self.assertRan(r, 1)
        self.assertIn("reason is required", r.stderr)
        r = self.pb("delete", "parts", "part-read-back", "--reason", "   ")
        self.assertRan(r, 1)
        self.assertTrue((self.root / "store" / "parts" / "part-read-back.json").exists())
        r = self.pb("delete", "parts", "part-read-back", "--reason=No longer used")  # the = form used to do nothing and exit 0
        self.assertRan(r)
        self.assertFalse((self.root / "store" / "parts" / "part-read-back.json").exists())
        self.assertEqual(self.changes()[-1]["reason"], "No longer used")
        r = self.pb("delete", "parts", "part-read-back", "--reason", "again")
        self.assertRan(r, 1)
        self.assertIn("no parts/part-read-back", r.stderr)
        r = self.pb("delete", "folders", "helper-scripts", "--reason", "two words")
        self.assertRan(r)

    def test_a_busy_store_is_a_plain_refusal(self):
        """Another process holding the store lock past the timeout: exit 1 with one line, never a traceback."""
        hold = ("import sys, time; sys.path.insert(0, sys.argv[1]); from lib import textio\n"
                "lock = textio.FileLock(sys.argv[2]); lock.__enter__(); print('held', flush=True); time.sleep(120)\n")
        p = subprocess.Popen([sys.executable, "-c", hold, str(PLAYBOOK), str(self.store.lock_path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            out, err = io.StringIO(), io.StringIO()
            old = storelib.Store.lock_timeout
            storelib.Store.lock_timeout = 0.3
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    pb.main(["--root", str(self.root), "put", "flows", "sample", self.write_json("s.json", good_flow())])
            finally:
                storelib.Store.lock_timeout = old
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("busy", err.getvalue())
        finally:
            p.kill()
            p.communicate(timeout=60)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_put_needs_a_reason_to_drop_steps(self):
        doc = self.doc("flows", "welcome")
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "welcome-done"]
        doc["edges"] = [e for e in doc["edges"] if e["to"] != "welcome-done"]
        f = self.write_json("cut.json", doc)
        r = self.pb("put", "flows", "welcome", f)
        self.assertRan(r, 1)
        self.assertIn("It removes: Run finished", r.stderr)
        self.assertRan(self.pb("put", "flows", "welcome", f, "--reason=merged into another step"))
        self.assertEqual(self.changes()[-1]["removed"], ["Run finished"])

    def test_base_revision_refuses_a_stale_put(self):
        rev = self.pb("rev", "flows", "welcome").stdout.strip()
        mine = self.doc("flows", "welcome")
        mine["summary"] = "mine"
        theirs = self.doc("flows", "welcome")
        theirs["summary"] = "theirs"
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("t.json", theirs), "--base", rev))
        r = self.pb("put", "flows", "welcome", self.write_json("m.json", mine), "--base", rev)
        self.assertRan(r, 1)
        self.assertIn("changed since revision " + rev, r.stderr)
        self.assertEqual(self.doc("flows", "welcome")["summary"], "theirs")
        self.assertRan(self.pb("put", "flows", "welcome", self.write_json("m.json", mine), "--base=" + self.pb("rev", "flows", "welcome").stdout.strip()))
        r = self.pb("delete", "flows", "welcome", "--reason", "x", "--base", rev)
        self.assertRan(r, 1)
        self.assertRan(self.pb("rev", "flows", "nosuch"), 1)

    def test_versions_and_restore(self):
        original = self.doc("flows", "welcome")
        for i in range(3):
            doc = self.doc("flows", "welcome")
            doc["summary"] = "edit %d" % i
            self.assertRan(self.pb("put", "flows", "welcome", self.write_json("w.json", doc)))
        names = self.pb("versions", "flows", "welcome").stdout.split()
        self.assertEqual(len(names), 3)
        self.assertEqual(names, sorted(names, reverse=True))
        r = self.pb("restore", "flows", "welcome", names[-1])
        self.assertRan(r)
        self.assertEqual(self.doc("flows", "welcome"), original)
        self.assertEqual(len(self.pb("versions", "flows", "welcome").stdout.split()), 4)
        self.assertRan(self.pb("restore", "flows", "welcome", "19990101-000000"), 1)
        self.assertEqual(self.pb("versions", "flows", "nosuch").stdout, "")
        self.assertRan(self.pb("delete", "flows", "posting", "--reason", "test"))
        v = self.pb("versions", "flows", "posting").stdout.split()[0]
        self.assertRan(self.pb("restore", "flows", "posting", v))
        self.assertTrue((self.root / "store" / "flows" / "posting.json").exists())

    def test_show_json_round_trips_through_put(self):
        """The documented edit loop: show --json, change the doc, put the same file back. Its rev is the base."""
        shown = json.loads(self.pb("show", "posting", "--json").stdout)
        self.assertIn("resolved", shown)
        shown["doc"]["summary"] = "Edited from the show output."
        f = self.write_json("shown.json", shown)
        r = self.pb("put", "flows", "posting", f)
        self.assertRan(r)
        self.assertEqual(self.doc("flows", "posting")["summary"], "Edited from the show output.")
        self.assertNotIn("col", self.doc("flows", "posting"))
        shown["doc"]["summary"] = "A second edit from the same, now stale, output."
        r = self.pb("put", "flows", "posting", self.write_json("shown.json", shown))
        self.assertRan(r, 1)  # its rev is no longer what is stored: refused, not merged over
        self.assertIn("changed since revision %s" % shown["rev"], r.stderr)
        r = self.pb("put", "flows", "posting", self.write_json("shown.json", shown), "--base", self.pb("rev", "flows", "posting").stdout.strip())
        self.assertRan(r)  # an explicit --base wins
        r = self.pb("put", "flows", "posting-copy", self.write_json("shown.json", shown))
        self.assertRan(r)  # saved under another id: a copy, the shown rev does not apply
        self.assertRan(self.pb("put", "accounts", "x-sam", self.write_json("shown.json", shown)), 1)
        self.assertIn("output of show --json for flows/posting", self.pb("put", "accounts", "x-sam", self.write_json("shown.json", shown)).stderr)

    def test_delete_names_what_still_points_at_it(self):
        r = self.pb("delete", "parts", "part-read-back", "--reason", "merged into the flow")
        self.assertRan(r)
        self.assertIn("store/flows/posting.json: nodes/posting-readback follows the part 'part-read-back', which is now deleted", r.stderr)
        self.assertIn("1 reference(s) left", r.stderr)
        r = self.pb("delete", "accounts", "x-sam", "--reason", "account closed")
        self.assertRan(r)
        self.assertIn("nodes/posting-account names the account 'x-sam'", r.stderr)
        r = self.pb("delete", "flows", "posting", "--reason", "merged into welcome")
        self.assertRan(r)
        self.assertIn("store/flows/welcome.json: nodes/", r.stderr)
        self.assertIn("opens the flow 'posting'", r.stderr)
        self.assertNotIn("store/accounts/x-sam.json", r.stderr)  # deleted above, so nothing left to name
        r = self.pb("delete", "folders", "helper-scripts", "--reason", "moved")
        self.assertRan(r)
        self.assertIn("store/flows/welcome.json: nodes/", r.stderr)
        self.assertIn("uses the folder 'helper-scripts'", r.stderr)
        r = self.pb("delete", "flows", "welcome", "--reason", "gone too")
        self.assertRan(r)
        self.assertNotIn("reference(s) left", r.stderr)  # nothing pointed at it

    def test_actor_comes_from_the_environment(self):
        env = dict(os.environ)
        env.pop("AGENT_PLAYBOOK_ACTOR", None)
        doc = self.doc("flows", "welcome")
        for actor, want in ((None, "agent"), ("  release   bot ", "release bot")):
            if actor is not None:
                env["AGENT_PLAYBOOK_ACTOR"] = actor
            doc["summary"] = "by %s" % want
            r = subprocess.run([sys.executable, str(PLAYBOOK / "pb.py"), "--root", str(self.root), "put", "flows", "welcome",
                                self.write_json("w.json", doc)], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.changes()[-1]["by"], want)
        with open(str(self.root / "history" / "changes.jsonl"), "ab") as fh:  # entries from before keep reading fine
            fh.write(b'{"at": "2026-01-01T00:00:00Z", "col": "flows", "id": "welcome", "action": "edit", "by": "claude", "title": "Welcome"}\n')
        self.assertIn("claude", self.pb("changes").stdout)

    def test_folder_paths_expand_home_and_read_relative_from_the_skill_folder(self):
        folder = self.doc("folders", "helper-scripts")
        for path, missing in (("playbook/audit/posts.jsonl", False), ("playbook/no/such", True), ("~", False),
                              ("~/no-such-folder-for-the-playbook-test", True), ("\\\\server\\share\\x", False)):
            folder["path"] = path
            (self.root / "store" / "folders" / "helper-scripts.json").write_text(json.dumps(folder), encoding="utf-8")
            r = self.pb("check", "--paths")
            self.assertRan(r)
            self.assertEqual("folder not on this machine: %s" % path in r.stdout, missing, path)

    def test_put_warns_about_dangling_references(self):
        doc = good_flow()
        doc["nodes"][1]["folders"] = ["no-such-folder"]
        r = self.pb("put", "flows", "sample", self.write_json("s.json", doc))
        self.assertRan(r)
        self.assertIn("uses the folder 'no-such-folder'", r.stderr)

    def test_normalize(self):
        doc = self.doc("flows", "welcome")
        old = dict(reversed(list(doc.items())))
        old["edges"] = [dict(e, id="e%d" % i) for i, e in enumerate(doc["edges"])]
        del old["edges"][0]["type"]
        (self.root / "store" / "flows" / "welcome.json").write_bytes(json.dumps(old, indent=1).replace("\n", "\r\n").encode("utf-8"))
        r = self.pb("normalize", "--check")
        self.assertRan(r, 1)
        self.assertIn("not canonical: store/flows/welcome.json", r.stdout)
        r = self.pb("normalize")
        self.assertRan(r)
        self.assertIn("1 document(s) rewritten", r.stdout)
        self.assertEqual(self.doc("flows", "welcome"), doc)
        self.assertNotIn(b"\r", self.raw("flows", "welcome"))
        self.assertRan(self.pb("normalize", "--check"))
        self.assertRan(self.pb("check"))


class TestEngineSource(unittest.TestCase):
    FILES = ["pb.py", "audit.py", "qc.py", "server.py", "launch.pyw", "install_shortcut.py", os.path.join("app", "build_page.py")] + \
            [os.path.join("lib", n) for n in sorted(os.listdir(str(PLAYBOOK / "lib"))) if n.endswith(".py")]

    def test_python_38_syntax(self):
        for name in self.FILES:
            src = (PLAYBOOK / name).read_text(encoding="utf-8")
            ast.parse(src, filename=name, feature_version=(3, 8))
        for path in sorted((ROOT / "tests").glob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 8))

    def test_engine_text_does_not_name_the_demo(self):
        """The engine is data-free: the demo app and its accounts live in the store, never in the code."""
        for name in self.FILES:
            src = (PLAYBOOK / name).read_text(encoding="utf-8")
            for word in ("Acme Notes", "acmenotes", "acme-notes"):
                self.assertNotIn(word, src, name)

    def test_engine_files_use_lf(self):
        for name in self.FILES + [os.path.join("app", "playbook.src.html")]:
            raw = (PLAYBOOK / name).read_bytes()
            if b"\r\n" in raw:
                # a Windows checkout may convert text files; the committed bytes are what matters, so only LF-only
                # or CRLF-only is acceptable here (never a mix)
                self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), name)


if __name__ == "__main__":
    unittest.main()
