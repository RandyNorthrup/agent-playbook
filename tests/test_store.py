"""Store writes, revisions, history and the trash, on a throwaway copy of the fixture playbook."""
import copy
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from pbtest import PLAYBOOK, PlaybookCase, good_flow
from lib import history, schema, textio
from lib import store as storelib


class TestStoreWrites(PlaybookCase):
    def test_write_creates_canonical_file_and_journal(self):
        r = self.store.write("flows", "sample", good_flow(), by="tester", base=storelib.NEW)
        self.assertTrue(r["changed"] and r["created"])
        raw = self.raw("flows", "sample")
        self.assertEqual(raw, schema.canonical_bytes("flows", good_flow()))
        self.assertNotIn(b"\r", raw)
        self.assertEqual(self.store.read("flows", "sample")[1], r["rev"])
        last = self.changes()[-1]
        self.assertEqual((last["col"], last["id"], last["action"], last["by"], last["rev"]), ("flows", "sample", "create", "tester", r["rev"]))
        self.assertRegex(last["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertNotIn(b"\r", (self.root / "history" / "changes.jsonl").read_bytes())

    def test_same_content_is_not_a_change(self):
        doc, rev = self.store.read("flows", "welcome")
        before = len(self.changes())
        r = self.store.write("flows", "welcome", doc, base=rev)
        self.assertFalse(r["changed"])
        self.assertEqual(r["rev"], rev)
        self.assertEqual(len(self.changes()), before)
        self.assertEqual(self.versions("flows", "welcome"), [])

    def test_invalid_document_is_refused_and_nothing_changes(self):
        before = self.raw("flows", "welcome")
        bad = self.doc("flows", "welcome")
        bad["nodes"][0]["kind"] = "weird"
        with self.assertRaises(storelib.Invalid) as ctx:
            self.store.write("flows", "welcome", bad)
        self.assertIn("is not one of", str(ctx.exception))
        self.assertEqual(self.raw("flows", "welcome"), before)
        for junk in ({}, [], None, {"nodes": "abc"}, {"a": float("nan")}):
            with self.assertRaises(storelib.Invalid):
                self.store.write("flows", "zz-junk", junk)
        self.assertFalse(self.store.exists("flows", "zz-junk"))

    def test_bad_ids_and_paths_are_refused(self):
        for bad in ("..", "../../escape", "a:b", "CON", "a b", "UPPER", ".hidden", "x" * 65, ""):
            with self.assertRaises(storelib.BadId, msg=bad):
                self.store.write("parts", bad, {"kind": "step", "title": "t"})
            with self.assertRaises(storelib.BadId, msg=bad):
                self.store.delete("parts", bad, reason="x")
        with self.assertRaises(storelib.BadId):
            self.store.write("bogus", "x", {})
        self.assertFalse((self.root / "escape.json").exists())
        self.assertEqual(sorted(os.listdir(str(self.root / "store" / "parts"))), ["part-read-back.json"])

    def test_path_stays_inside_collection(self):
        p = self.store.path("flows", "welcome")
        self.assertEqual(p.parent, (self.root / "store" / "flows").resolve())

    def test_stale_write_is_refused(self):
        doc, rev = self.store.read("flows", "welcome")
        mine = copy.deepcopy(doc)
        mine["summary"] = "mine"
        theirs = copy.deepcopy(doc)
        theirs["summary"] = "theirs"
        r = self.store.write("flows", "welcome", theirs, by="other", base=rev)
        with self.assertRaises(storelib.Stale) as ctx:
            self.store.write("flows", "welcome", mine, by="me", base=rev)
        self.assertEqual(ctx.exception.rev, r["rev"])
        self.assertEqual(ctx.exception.current["summary"], "theirs")
        self.assertEqual(self.doc("flows", "welcome")["summary"], "theirs")
        self.store.write("flows", "welcome", mine, by="me", base=r["rev"])
        self.assertEqual(self.doc("flows", "welcome")["summary"], "mine")

    def test_new_base_refuses_an_existing_document(self):
        with self.assertRaises(storelib.Stale):
            self.store.write("flows", "welcome", good_flow(), base=storelib.NEW)
        with self.assertRaises(storelib.Stale):
            self.store.write("flows", "never-was", good_flow(), base="0123456789abcdef")

    def test_reason_required_for_dropped_steps_and_connections(self):
        doc, rev = self.store.read("flows", "welcome")
        cut = copy.deepcopy(doc)
        cut["nodes"] = [n for n in cut["nodes"] if n["id"] != "welcome-done"]
        cut["edges"] = [e for e in cut["edges"] if e["to"] != "welcome-done"]
        with self.assertRaises(storelib.ReasonRequired) as ctx:
            self.store.write("flows", "welcome", cut, base=rev)
        self.assertEqual(ctx.exception.removed, ["Run finished"])
        no_edge = copy.deepcopy(doc)
        no_edge["edges"] = no_edge["edges"][1:]
        with self.assertRaises(storelib.ReasonRequired) as ctx:
            self.store.write("flows", "welcome", no_edge, base=rev)
        self.assertTrue(ctx.exception.removed[0].startswith("connection "))
        flipped = copy.deepcopy(doc)
        e = flipped["edges"][2]
        e["from"], e["to"] = e["to"], e["from"]
        self.store.write("flows", "welcome", flipped, base=rev)  # reversing keeps the link: no reason needed
        r = self.store.write("flows", "welcome", cut, reason="  not   needed ")
        self.assertEqual(r["removed"], ["Run finished"])
        self.assertEqual(self.changes()[-1]["reason"], "not needed")

    def test_delete_moves_to_trash_and_keeps_a_version(self):
        with self.assertRaises(storelib.ReasonRequired):
            self.store.delete("parts", "part-read-back")
        with self.assertRaises(storelib.ReasonRequired):
            self.store.delete("parts", "part-read-back", reason="   ")
        self.assertTrue(self.store.exists("parts", "part-read-back"))
        self.store.delete("parts", "part-read-back", by="tester", reason="No longer used")
        self.assertFalse(self.store.exists("parts", "part-read-back"))
        trash = os.listdir(str(self.root / "store" / ".trash"))
        self.assertEqual(len(trash), 1)
        self.assertTrue(trash[0].startswith("parts--part-read-back--"))
        self.assertEqual(len(self.versions("parts", "part-read-back")), 1)
        last = self.changes()[-1]
        self.assertEqual((last["action"], last["reason"]), ("delete", "No longer used"))
        with self.assertRaises(storelib.NotFound):
            self.store.delete("parts", "part-read-back", reason="again")

    def test_restore_brings_back_a_deleted_document(self):
        doc, _ = self.store.read("parts", "part-read-back")
        self.store.delete("parts", "part-read-back", reason="oops")
        v = history.versions(self.store, "parts", "part-read-back")[0]
        self.store.restore("parts", "part-read-back", v)
        self.assertEqual(self.store.read("parts", "part-read-back")[0], doc)
        with self.assertRaises(LookupError):
            self.store.restore("parts", "part-read-back", "19990101-000000")
        with self.assertRaises(LookupError):
            self.store.restore("parts", "part-read-back", "../../x")

    def test_write_is_atomic(self):
        before = self.raw("flows", "welcome")
        doc = self.doc("flows", "welcome")
        doc["summary"] = "changed"
        real = os.replace

        def boom(src, dst):
            raise OSError("disk full")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.store.write("flows", "welcome", doc)
        finally:
            os.replace = real
        self.assertEqual(self.raw("flows", "welcome"), before)
        self.assertEqual([n for n in os.listdir(str(self.root / "store" / "flows")) if n.endswith(".tmp")], [])
        self.store.write("flows", "welcome", doc)  # the lock was released
        self.assertEqual(self.doc("flows", "welcome")["summary"], "changed")

    def test_unreadable_file_is_reported_not_fatal(self):
        (self.root / "store" / "flows" / "broken.json").write_bytes(b'{"name": "broken", "nodes": [')
        (self.root / "store" / "flows" / "nan.json").write_bytes(b'{"order": NaN}')
        (self.root / "store" / "flows" / "Bad Name.json").write_bytes(b"{}")
        data, revs, errors = self.store.load_all()
        self.assertEqual(sorted(data["flows"]), ["posting", "welcome"])
        self.assertEqual(sorted(e[0] for e in errors), ["store/flows/Bad Name.json", "store/flows/broken.json", "store/flows/nan.json"])
        with self.assertRaises(storelib.Unreadable):
            self.store.read("flows", "broken")
        r = self.store.write("flows", "broken", good_flow(), by="fixer")  # replacing it on purpose works, and keeps the bytes
        self.assertTrue(r["changed"])
        v = self.versions("flows", "broken")
        self.assertEqual(len(v), 1)
        self.assertEqual((self.root / "history" / "flows" / "broken" / (v[0] + ".json")).read_bytes(), b'{"name": "broken", "nodes": [')

    def test_stamp_changes_with_any_write_or_delete(self):
        s0 = self.store.stamp()
        self.assertEqual(s0, self.store.stamp())
        self.store.write("flows", "sample", good_flow())
        s1 = self.store.stamp()
        self.assertNotEqual(s0, s1)
        self.store.delete("flows", "sample", reason="x")
        self.assertNotEqual(s1, self.store.stamp())


class TestHistory(PlaybookCase):
    def edit(self, n, by="tester"):
        doc, rev = self.store.read("flows", "welcome")
        doc["summary"] = "edit %s" % n
        return self.store.write("flows", "welcome", doc, by=by, base=rev)

    def test_same_second_saves_keep_every_version(self):
        for i in range(6):
            self.edit(i)
        names = self.versions("flows", "welcome")
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)
        seen = []
        for v in sorted(names):
            seen.append(history.read_version(self.store, "flows", "welcome", v)["summary"])
        self.assertEqual(seen, ["Where a run starts: read the rules, then post."] + ["edit %d" % i for i in range(5)])
        for v in names:
            self.assertRegex(v, r"^\d{8}-\d{6}-\d{6}-[0-9a-f]{8}$")

    def test_version_kept_when_the_actor_changes(self):
        self.edit(1, by="canvas")
        self.edit(2, by="claude")
        self.edit(3, by="canvas")
        self.assertEqual(len(self.versions("flows", "welcome")), 3)
        self.assertEqual([c["by"] for c in self.changes()[-3:]], ["canvas", "claude", "canvas"])

    def test_identical_content_is_not_kept_twice(self):
        doc, rev = self.store.read("flows", "welcome")
        name = history.keep_version(self.store, "flows", "welcome", doc, self.raw("flows", "welcome"), rev)
        self.assertIsNotNone(name)
        self.assertIsNone(history.keep_version(self.store, "flows", "welcome", doc, self.raw("flows", "welcome"), rev))
        self.assertEqual(len(self.versions("flows", "welcome")), 1)

    def test_versions_list_and_changes_are_tolerant(self):
        self.assertEqual(history.versions(self.store, "flows", "../x"), [])
        self.assertEqual(history.versions(self.store, "bogus", "x"), [])
        self.edit(1)
        with open(str(self.root / "history" / "changes.jsonl"), "ab") as fh:
            fh.write(b"not json\n[1,2]\n")
        rows = history.read_changes(self.store, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "welcome")
        self.edit(2)
        self.assertEqual([r["action"] for r in history.read_changes(self.store, 10)], ["edit", "edit"])

    def test_changes_keep_a_title_with_a_unicode_line_separator(self):
        doc = good_flow()
        doc["name"] = "Two lines and\x85more"  # legal in a name; JSON keeps these characters unescaped
        self.store.write("flows", "sample", doc, reason="a reason on two lines")
        rows = history.read_changes(self.store, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Two lines and\x85more")


class TestConcurrency(PlaybookCase):
    def test_concurrent_writers_do_not_corrupt(self):
        """Threads in this process and a second process hammer one flow: every save either lands whole or is
        refused as stale; the file is always valid and no update is silently lost."""
        results = {"ok": 0, "stale": 0, "other": []}
        guard = threading.Lock()

        def worker(k):
            for i in range(25):
                try:
                    doc, rev = self.store.read("flows", "welcome")
                    doc["summary"] = "w%d-%d" % (k, i)
                    self.store.write("flows", "welcome", doc, by="t%d" % k, base=rev)
                    with guard:
                        results["ok"] += 1
                except storelib.Stale:
                    with guard:
                        results["stale"] += 1
                except Exception as e:  # anything else is a bug
                    with guard:
                        results["other"].append(repr(e))

        code = ("import sys; sys.path.insert(0, sys.argv[1]); from lib import store\n"
                "s = store.Store(sys.argv[2]); ok = stale = 0\n"
                "for i in range(25):\n"
                "    try:\n"
                "        d, rev = s.read('flows', 'welcome'); d['summary'] = 'p-%d' % i; s.write('flows', 'welcome', d, by='proc', base=rev); ok += 1\n"
                "    except store.Stale: stale += 1\n"
                "print(ok, stale)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(PLAYBOOK), str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, err)
        p_ok, p_stale = [int(x) for x in out.split()]
        self.assertEqual(results["other"], [])
        self.assertEqual(results["ok"] + results["stale"], 100)
        self.assertEqual(p_ok + p_stale, 25)
        total_ok = results["ok"] + p_ok
        self.assertGreater(total_ok, 0)
        json.loads(self.raw("flows", "welcome").decode("utf-8"))
        self.assertEqual(self.raw("flows", "welcome"), schema.canonical_bytes("flows", self.doc("flows", "welcome")))
        edits = [c for c in self.changes() if c["id"] == "welcome"]
        self.assertEqual(len(edits), total_ok)  # one journal line per save that landed
        self.assertEqual(len(self.versions("flows", "welcome")), total_ok)  # and one kept version each
        self.assertEqual([n for n in os.listdir(str(self.root / "store" / "flows")) if n.endswith(".tmp")], [])
        for line in (self.root / "history" / "changes.jsonl").read_text(encoding="utf-8").splitlines():
            json.loads(line)

    def test_lock_is_released_after_an_error(self):
        with self.assertRaises(ZeroDivisionError):
            with self.store.lock():
                1 / 0
        with self.store.lock():
            pass

    def test_append_lines_from_many_threads(self):
        log = self.tmp / "log.jsonl"
        lock = self.tmp / "log.lock"

        def worker(k):
            for i in range(40):
                textio.append_lines(log, [json.dumps({"k": k, "i": i, "pad": "x" * 6000})], lock)
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        raw = log.read_bytes()
        self.assertNotIn(b"\r", raw)
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        self.assertEqual(len(rows), 200)
        self.assertEqual(sorted((r["k"], r["i"]) for r in rows), sorted((k, i) for k in range(5) for i in range(40)))


class TestLocks(PlaybookCase):
    """The store lock against another process: a live holder makes others wait (then time out); a holder that
    crashes never leaves a stale lock, on Windows (msvcrt) or POSIX (flock)."""
    HOLD = ("import sys, time; sys.path.insert(0, sys.argv[1]); from lib import textio\n"
            "lock = textio.FileLock(sys.argv[2]); lock.__enter__(); print('held', flush=True); time.sleep(120)\n")

    def hold(self):
        p = subprocess.Popen([sys.executable, "-c", self.HOLD, str(PLAYBOOK), str(self.store.lock_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def end():
            p.kill()
            p.communicate(timeout=60)
        self.addCleanup(end)
        self.assertEqual(p.stdout.readline().strip(), "held", p.stderr.read() if p.poll() is not None else "")
        return p

    def test_a_live_holder_makes_writers_time_out(self):
        self.hold()
        t = time.monotonic()
        with self.assertRaises(textio.LockTimeout):
            with textio.FileLock(self.store.lock_path, timeout=0.5):
                pass
        self.assertLess(time.monotonic() - t, 5)
        self.store.lock_timeout = 0.3
        with self.assertRaises(textio.LockTimeout):
            self.store.write("flows", "sample", good_flow())
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_a_crashed_holder_leaves_no_stale_lock(self):
        p = self.hold()
        p.kill()
        p.communicate(timeout=60)
        self.assertTrue(self.store.lock_path.exists())  # the file stays behind; only the OS lock counts
        t = time.monotonic()
        with textio.FileLock(self.store.lock_path, timeout=5):
            pass
        self.assertLess(time.monotonic() - t, 3)
        self.assertTrue(self.store.write("flows", "sample", good_flow())["created"])


class TestTextio(PlaybookCase):
    def test_json_lines_end_only_at_line_feeds(self):
        text = 'a b\n{"x": " \x85\x0c"}\r\nlast'
        self.assertEqual(textio.split_lines(text), ["a b", '{"x": " \x85\x0c"}', "last"])
        self.assertEqual(textio.split_lines(""), [])
        self.assertEqual(textio.split_lines("one\n"), ["one"])
        self.assertEqual(textio.split_lines("one\n\ntwo\n"), ["one", "", "two"])

    def test_nesting_limit_counts_only_real_brackets(self):
        with self.assertRaises(ValueError) as cm:
            textio.loads("[" * 65 + "]" * 65)
        self.assertIn("nested too deeply", str(cm.exception))
        self.assertEqual(len(textio.loads("[" * 64 + "]" * 64)), 1)  # the limit itself is allowed
        inside = "[" * 500 + '\\"' + "{" * 500  # brackets inside a string do not nest, escaped quote and all
        doc = textio.loads('{"a": "%s", "b": %s}' % (inside, "[" * 60 + "]" * 60))
        self.assertEqual(doc["a"], "[" * 500 + '"' + "{" * 500)
        self.assertEqual(textio.loads(b'\xef\xbb\xbf{"bom": 1}'), {"bom": 1})
        self.assertEqual(textio.loads('﻿{"bom": 1}'), {"bom": 1})
        t = time.monotonic()  # a string that never closes keeps the scan linear
        with self.assertRaises(ValueError):
            textio.loads("[" * 65 + '"\\"' * 300000 + "\\")
        self.assertLess(time.monotonic() - t, 5)

    def test_temp_files_a_killed_writer_left_are_swept(self):
        flows = self.root / "store" / "flows"
        old, fresh = flows / ".welcome.json.123.0123abcd.tmp", flows / ".welcome.json.456.89abcdef.tmp"
        other = flows / ".notes.tmp"
        hist = self.root / "history" / "flows" / "welcome"
        hist.mkdir(parents=True)
        h_old = hist / ".20260101-000000-000000-00000000.json.9.00000000.tmp"
        for p in (old, fresh, other, h_old):
            p.write_bytes(b"{")
        long_ago = time.time() - 7200
        for p in (old, other, h_old):
            os.utime(str(p), (long_ago, long_ago))
        self.assertEqual(self.store.sweep_temp(), 2)
        self.assertFalse(old.exists())
        self.assertFalse(h_old.exists())
        self.assertTrue(fresh.exists())  # a write may still be in progress
        self.assertTrue(other.exists())  # not one of ours
        self.assertEqual(sorted(self.store.load_all()[0]["flows"]), ["posting", "welcome"])
        r = self.pb("normalize")
        self.assertRan(r)
        self.assertNotIn("temp file", r.stdout)


if __name__ == "__main__":
    unittest.main()
