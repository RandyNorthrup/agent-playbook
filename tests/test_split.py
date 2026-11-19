"""pb.py split: a flow becomes a hub plus single-job flows; notes follow their node, crossing paths become gotos,
results and gotos are copied, and nothing is saved unless the whole playbook still passes the lint."""
import contextlib
import io
import json

from pbtest import PlaybookCase
import pb
from lib import store as storelib


def big_flow():
    return {"name": "Big", "group": "Start here", "icon": "map", "summary": "A flow with two jobs.", "order": 5,
            "nodes": [{"id": "big-start", "kind": "start", "title": "Start here", "x": 0, "y": 0},
                      {"id": "big-a", "kind": "step", "title": "First job", "body": "Do the first job.", "x": 320, "y": 0},
                      {"id": "big-b", "kind": "step", "title": "Second job", "body": "Do the second job.", "x": 640, "y": 0},
                      {"id": "big-note", "kind": "rule", "title": "A rule for the second job", "body": "Mind this.", "x": 640, "y": 200},
                      {"id": "big-c", "kind": "step", "title": "Finish the second job", "body": "Wrap it up.", "x": 960, "y": 0},
                      {"id": "big-out", "kind": "output", "title": "Done", "x": 1280, "y": 0}],
            "edges": [{"from": "big-start", "to": "big-a", "type": "flow"},
                      {"from": "big-a", "to": "big-b", "type": "flow", "label": "then"},
                      {"from": "big-note", "to": "big-b", "type": "attach"},
                      {"from": "big-b", "to": "big-c", "type": "flow"},
                      {"from": "big-c", "to": "big-out", "type": "flow", "label": "done"},
                      {"from": "big-c", "to": "big-a", "type": "flow", "label": "again"}]}


PLAN = {"hub": {"summary": "The hub."},
        "flows": {"big-two": {"name": "Big: the second job", "summary": "Only the second job.", "entry": "big-b",
                              "start": {"title": "The second job", "sub": "On its own"}, "nodes": ["big-b", "big-c"]}}}


class TestSplit(PlaybookCase):
    def setUp(self):
        super().setUp()
        self.assertRan(self.pb("put", "flows", "big", self.write_json("big.json", big_flow())))

    def split(self, plan, *extra):
        return self.pb("split", "big", self.write_json("plan.json", plan), *extra)

    def test_split_makes_a_hub_and_a_job_flow(self):
        r = self.split(PLAN, "--dry-run")
        self.assertRan(r)
        self.assertIn("big-two: ", r.stdout)
        self.assertEqual(self.doc("flows", "big"), big_flow())  # a dry run saves nothing
        self.assertRan(self.split(PLAN), 1)  # a real split needs a reason
        r = self.split(PLAN, "--reason", "owner asked for single-job flows")
        self.assertRan(r)
        hub, two = self.doc("flows", "big"), self.doc("flows", "big-two")
        self.assertEqual(sorted(n["id"] for n in hub["nodes"]), ["big-a", "big-a-to-big-two", "big-start"])  # the result moved with its only path
        self.assertNotIn("big-b", [n["id"] for n in hub["nodes"]])
        self.assertEqual(hub["summary"], "The hub.")
        ids = [n["id"] for n in two["nodes"]]
        self.assertEqual(ids[0], "big-two-start")
        for nid in ("big-b", "big-c", "big-note", "big-out"):  # the note follows its node; the result is copied
            self.assertIn(nid, ids)
        goto = [n for n in two["nodes"] if n.get("kind") == "goto"][0]
        self.assertEqual((goto["goto"], goto["sub"]), ("big", "At: First job"))
        self.assertIn({"from": "big-c", "to": goto["id"], "type": "flow", "label": "again"}, two["edges"])
        self.assertIn({"from": "big-note", "to": "big-b", "type": "attach"}, two["edges"])
        self.assertEqual(two["group"], "Start here")
        self.assertEqual([c["reason"] for c in self.changes()[-2:]], ["owner asked for single-job flows"] * 2)
        self.assertRan(self.pb("check"))

    def test_bad_plans_are_refused(self):
        before = self.doc("flows", "big")
        for plan, want in (({"flows": {"big-two": dict(PLAN["flows"]["big-two"], nodes=["big-b", "nosuch"])}}, "no node 'nosuch'"),
                           ({"flows": {"big-two": dict(PLAN["flows"]["big-two"], entry="big-a")}}, "entry"),
                           ({"flows": {"big-two": dict(PLAN["flows"]["big-two"], nodes=["big-b", "big-start"])}}, "start node stays"),
                           ({"flows": {"Big Two": PLAN["flows"]["big-two"]}}, "lower-case"),
                           ({"flows": {"posting": PLAN["flows"]["big-two"]}}, "already exists"),
                           ({"flows": {"big-two": PLAN["flows"]["big-two"], "big-three": dict(PLAN["flows"]["big-two"], name="x")}}, "two plans"),
                           ({"nope": 1}, "needs a \"flows\" object")):
            r = self.split(plan, "--reason", "test")
            self.assertRan(r, 1)
            self.assertIn(want, r.stderr)
        self.assertEqual(self.doc("flows", "big"), before)

    def test_plans_of_the_wrong_shape_are_named_not_a_crash(self):
        spec = PLAN["flows"]["big-two"]
        for plan, want in (({"flows": {"big-two": dict((k, v) for k, v in spec.items() if k != "name")}}, "big-two: needs a name"),
                           ({"flows": {"big-two": "just text"}}, "must be an object"),
                           ({"flows": {"big-two": dict(spec, nodes="big-b")}}, "nodes must be a list"),
                           ({"flows": {"big-two": dict(spec, nodes=[["big-b"]])}}, "nodes must be a list"),
                           ({"flows": {"big-two": dict(spec, start="x")}}, "start must be an object"),
                           ({"flows": {"big--two": spec}}, "lower-case"),
                           ({"flows": {"home": spec}}, "page name"),
                           ({"flows": {"con": spec}}, "device name"),
                           ({"flows": {"big-two": spec}, "hub": "x"}, "\"hub\" must be an object")):
            r = self.split(plan, "--reason", "test")
            self.assertRan(r, 1)
            self.assertIn(want, r.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "store" / "flows").iterdir()), ["big.json", "posting.json", "welcome.json"])

    def test_a_hub_changed_meanwhile_leaves_no_half_split(self):
        """The canvas saves the hub while the split is being written: the new flows are taken back out, so the
        moved steps are never in the playbook twice, and the canvas edit is kept."""
        real = storelib.Store.write

        def write(store, col, doc_id, doc, **kw):
            r = real(store, col, doc_id, doc, **kw)
            if doc_id == "big-two":  # just after the new flow lands, the owner edits the hub on the canvas
                hub, rev = store.read("flows", "big")
                real(store, "flows", "big", dict(hub, summary="edited on the canvas"), by="canvas", base=rev)
            return r
        storelib.Store.write = write
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                pb.main(["--root", str(self.root), "split", "big", self.write_json("plan.json", PLAN), "--reason", "test"])
        finally:
            storelib.Store.write = real
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("changed since revision", err.getvalue())
        self.assertIn("took back big-two", err.getvalue())
        self.assertFalse((self.root / "store" / "flows" / "big-two.json").exists())
        self.assertEqual(self.doc("flows", "big")["summary"], "edited on the canvas")
        self.assertEqual(len(self.doc("flows", "big")["nodes"]), 6)
        self.assertEqual(self.changes()[-1]["action"], "delete")
        self.assertIn("not finished", self.changes()[-1]["reason"])
        self.assertRan(self.pb("check"))

    def run_split_with(self, write):
        real = storelib.Store.write
        storelib.Store.write = write
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                pb.main(["--root", str(self.root), "split", "big", self.write_json("plan.json", PLAN), "--reason", "test"])
        finally:
            storelib.Store.write = real
        return cm.exception.code, err.getvalue()

    def test_a_new_flow_that_landed_but_failed_to_journal_is_taken_back(self):
        """The write lands on disk and journaling it fails: the take-back still finds and removes the new flow."""
        real = storelib.Store.write
        from lib import history

        def write(store, col, doc_id, doc, **kw):
            if doc_id != "big-two":
                return real(store, col, doc_id, doc, **kw)
            journal = history.journal
            history.journal = lambda *a, **k: (_ for _ in ()).throw(storelib.Unreadable("journal: disk full"))
            try:
                return real(store, col, doc_id, doc, **kw)
            finally:
                history.journal = journal
        code, err = self.run_split_with(write)
        self.assertEqual(code, 1)
        self.assertIn("took back big-two", err)
        self.assertFalse((self.root / "store" / "flows" / "big-two.json").exists())
        self.assertEqual(len(self.doc("flows", "big")["nodes"]), 6)

    def test_the_take_back_never_removes_an_edit_made_meanwhile(self):
        """The owner edits the new flow on the canvas before the hub save fails: that flow is left in place."""
        real = storelib.Store.write

        def write(store, col, doc_id, doc, **kw):
            r = real(store, col, doc_id, doc, **kw)
            if doc_id == "big-two":
                new, rev = store.read("flows", "big-two")
                real(store, "flows", "big-two", dict(new, summary="edited on the canvas"), by="canvas", base=rev)
                hub, hub_rev = store.read("flows", "big")
                real(store, "flows", "big", dict(hub, summary="hub edited too"), by="canvas", base=hub_rev)
            return r
        code, err = self.run_split_with(write)
        self.assertEqual(code, 1)
        self.assertIn("left flows/big-two in place", err)
        self.assertEqual(self.doc("flows", "big-two")["summary"], "edited on the canvas")

    def test_a_hub_changed_before_saving_saves_nothing(self):
        from lib import split as splitlib
        real = splitlib.split

        def split(fid, flow, plan, parts, used):
            docs = real(fid, flow, plan, parts, used)
            hub, rev = self.store.read("flows", "big")  # the canvas saves the hub while the split is checked
            self.store.write("flows", "big", dict(hub, summary="edited on the canvas"), by="canvas", base=rev)
            return docs
        splitlib.split = split
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                pb.main(["--root", str(self.root), "split", "big", self.write_json("plan.json", PLAN), "--reason", "test"])
        finally:
            splitlib.split = real
        self.assertIn("flows/big changed since revision", err.getvalue())
        self.assertNotIn("took back", err.getvalue())  # nothing had been written
        self.assertFalse((self.root / "store" / "flows" / "big-two.json").exists())
        self.assertEqual(self.doc("flows", "big")["summary"], "edited on the canvas")

    def test_unreachable_result_is_not_saved(self):
        plan = {"flows": {"big-two": dict(PLAN["flows"]["big-two"], entry="big-c", nodes=["big-b", "big-c"])}}
        r = self.split(plan, "--reason", "test")
        self.assertRan(r, 1)
        self.assertIn("unreachable", r.stdout)
        plan["flows"]["big-two"]["also"] = {"big-b": "from b"}
        self.assertRan(self.split(plan, "--reason", "test"))
        self.assertIn({"from": "big-two-start", "to": "big-b", "type": "flow", "label": "from b"}, self.doc("flows", "big-two")["edges"])
