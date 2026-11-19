"""The lint rules, each provoked on a copy of the fixture playbook."""
import json
import unittest

from pbtest import PlaybookCase
from lib import lint, textio


class TestLint(PlaybookCase):
    def put(self, col, doc_id, doc):
        textio.atomic_write(self.root / "store" / col / (doc_id + ".json"), (json.dumps(doc) + "\n").encode("utf-8"))

    def edit(self, col, doc_id, fn):
        doc = self.doc(col, doc_id)
        fn(doc)
        self.put(col, doc_id, doc)

    def rules(self, level, strict=False):
        rep = lint.run(self.store, strict=strict)
        return sorted(set(f["rule"] for f in rep.findings if f["level"] == level)), rep

    def node(self, doc, nid):
        return [n for n in doc["nodes"] if n["id"] == nid][0]

    def test_fixture_is_clean(self):
        rep = lint.run(self.store)
        self.assertEqual(rep.findings, [])
        self.assertEqual(dict(rep.stats), {"flows": 2, "nodes": 10, "accounts": 1, "folders": 1, "parts": 1})
        self.assertFalse(rep.failed(strict=True))

    def expect(self, level, rule, where=None):
        rep = lint.run(self.store, strict=True)
        hits = [f for f in rep.findings if f["rule"] == rule]
        self.assertTrue(hits, "no %s finding in %r" % (rule, rep.findings))
        self.assertEqual(hits[0]["level"], level)
        if where:
            self.assertIn(where, hits[0]["path"])
        self.assertEqual(rep.failed(), level == lint.ERROR or any(f["level"] == lint.ERROR for f in rep.findings))
        return hits

    def test_error_unreadable_names_the_file(self):
        (self.root / "store" / "flows" / "broken.json").write_bytes(b"{")
        hit = self.expect(lint.ERROR, "unreadable")[0]
        self.assertEqual(hit["file"], "store/flows/broken.json")

    def test_error_schema(self):
        self.edit("flows", "welcome", lambda d: d.update(extra=1))
        self.expect(lint.ERROR, "schema")

    def test_error_dangling_refs(self):
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-post").update(goto="nowhere"))
        self.expect(lint.ERROR, "dangling-ref", "welcome-post")
        self.edit("flows", "welcome", lambda d: (self.node(d, "welcome-post").update(goto="posting"), self.node(d, "welcome-rules").update(folders=["ghost"])))
        self.expect(lint.ERROR, "dangling-ref", "welcome-rules")
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-rules").update(folders=["helper-scripts"]))
        self.edit("flows", "posting", lambda d: self.node(d, "posting-account").update(account="nobody"))
        self.expect(lint.ERROR, "dangling-ref", "posting-account")
        self.edit("flows", "posting", lambda d: (self.node(d, "posting-account").update(account="x-sam"), self.node(d, "posting-readback").update(part="part-gone")))
        self.expect(lint.ERROR, "dangling-ref", "posting-readback")
        self.edit("flows", "posting", lambda d: self.node(d, "posting-readback").update(part="part-read-back"))
        self.edit("accounts", "x-sam", lambda d: d.update(flow="gone"))
        self.expect(lint.ERROR, "dangling-ref", "flow")

    def test_error_start_count(self):
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-done").update(kind="start"))
        self.expect(lint.ERROR, "start")
        self.edit("flows", "welcome", lambda d: (self.node(d, "welcome-done").update(kind="step"), self.node(d, "welcome-start").update(kind="step")))
        self.expect(lint.ERROR, "start")

    def test_error_unreachable(self):
        self.edit("flows", "welcome", lambda d: d["nodes"].append({"id": "welcome-island", "kind": "step", "title": "Nobody gets here", "body": "x", "x": 0, "y": 400}))
        self.expect(lint.ERROR, "unreachable", "welcome-island")

    def test_attached_note_counts_as_reached(self):
        self.edit("flows", "welcome", lambda d: (d["nodes"].append({"id": "welcome-tip", "kind": "gotcha", "title": "Watch out", "body": "x", "x": 0, "y": 400}),
                                                 d["edges"].append({"from": "welcome-choose", "to": "welcome-tip", "type": "attach"})))
        self.assertEqual(lint.run(self.store, strict=True).findings, [])

    def test_error_goto_with_outgoing_connection(self):
        self.edit("flows", "welcome", lambda d: d["edges"].append({"from": "welcome-post", "to": "welcome-done", "type": "flow"}))
        self.expect(lint.ERROR, "goto-outgoing", "welcome-post>welcome-done")

    def test_warnings(self):
        cases = [
            ("attribution", lambda d: self.node(d, "welcome-rules").update(body='Sam, 11-08: "QC everything".')),
            ("attribution", lambda d: self.node(d, "welcome-rules").update(body="Do it this way (11-08).")),
            ("date", lambda d: self.node(d, "welcome-rules").update(body="Changed on 2026-11-08.")),
            ("part-copy", lambda d: self.node(d, "welcome-rules").update(title="Read the field back")),
            ("attach-main", lambda d: d["edges"].append({"from": "welcome-start", "to": "welcome-done", "type": "attach"})),
            ("flow-to-note", lambda d: [e.update({"from": "welcome-rules", "to": "welcome-honest", "type": "flow"}) for e in d["edges"] if e["from"] == "welcome-honest"]),
            ("two-way", lambda d: d["edges"].append({"from": "welcome-rules", "to": "welcome-start", "type": "flow"})),
            ("restates-title", lambda d: self.node(d, "welcome-rules").update(body="Read the rules!")),
            ("placeholder", lambda d: self.node(d, "welcome-rules").update(title="New step")),
            ("goto-body", lambda d: self.node(d, "welcome-post").update(body="Some details nobody will read.")),
        ]
        for rule, change in cases:
            self.setUp()
            self.edit("flows", "welcome", change)
            hits = self.expect(lint.WARN, rule)
            self.assertEqual(hits[0]["file"], "store/flows/welcome.json", rule)
            rep = lint.run(self.store)
            self.assertFalse(rep.failed(), rule)  # a warning does not fail the check
            self.assertTrue(rep.failed(strict=True), rule)  # unless --strict

    def test_dates_that_are_not_dates_pass(self):
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-rules").update(body="Wait 30-90 seconds. Use 1080-1350 slides. Call 555-0100."))
        self.assertEqual(lint.run(self.store, strict=True).findings, [])
        # number ranges: a one-digit first number, or a count of something
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-rules").update(
            body="Play it at 8-12 fps. Loops run 2-10 s. Wait 10-15 minutes, then 10-12 posts, 10-20% of them links. 12-24 Hz hum."))
        self.assertEqual(lint.run(self.store, strict=True).findings, [])

    def test_short_dates_are_still_dates(self):
        for body in ("Posted 10-21.", "Renamed 10-12; wait.", "The 11-07 post went in.", "From 10-05/06.", "Since 01-31"):
            self.setUp()
            self.edit("flows", "welcome", lambda d, body=body: self.node(d, "welcome-rules").update(body=body))
            self.expect(lint.WARN, "date")

    def test_owner_name(self):
        """The fixture's owner is Sam (the `owner` of its account)."""
        for body in ("Ask Sam first.", "Sam's own group.", "Never go live for Sam"):
            self.setUp()
            self.edit("flows", "welcome", lambda d, body=body: self.node(d, "welcome-rules").update(body=body))
            hits = self.expect(lint.WARN, "owner-name")
            self.assertIn("the owner", hits[0]["message"])
        self.setUp()  # a handle, a path, a domain and a longer word are not the owner's name
        self.edit("flows", "welcome", lambda d: self.node(d, "welcome-rules").update(
            body="Post as @SamSmith or @Sam. Files: C:\\Users\\Sam\\docs and /home/Sam/x. Site Sam.example. Samuel and Samwise are other people. The signer is Sam Smith."))
        self.assertEqual(lint.run(self.store, strict=True).findings, [])

    def test_text_rules_cover_accounts_folders_and_parts(self):
        cases = [
            ("accounts", "x-sam", "date", lambda d: d.update(notes=["Premium since 10-12: use the tools."])),
            ("accounts", "x-sam", "owner-name", lambda d: d.update(purpose="Sam's main account.")),
            ("accounts", "x-sam", "owner-name", lambda d: d.update(label="Sam's X")),
            ("accounts", "x-sam", "attribution", lambda d: d.update(notes=["Keep it short (Sam)."])),
        ]
        for col, doc_id, rule, change in cases:
            self.setUp()
            self.edit(col, doc_id, change)
            hits = self.expect(lint.WARN, rule)
            self.assertEqual(hits[0]["file"], "store/%s/%s.json" % (col, doc_id), rule)
        self.setUp()
        folder = sorted(p.stem for p in (self.root / "store" / "folders").glob("*.json"))[0]
        self.edit("folders", folder, lambda d: d.update(note="Drafted 10-24; not posted."))
        self.assertEqual(self.expect(lint.WARN, "date")[0]["file"], "store/folders/%s.json" % folder)
        self.setUp()
        part = sorted(p.stem for p in (self.root / "store" / "parts").glob("*.json"))[0]
        self.edit("parts", part, lambda d: d.update(body="Sam wants the field read back."))
        self.assertEqual(self.expect(lint.WARN, "owner-name")[0]["file"], "store/parts/%s.json" % part)
        self.setUp()  # the owner field itself, a handle and a url are data, not text
        self.edit("accounts", "x-sam", lambda d: d.update(owner="Sam", handle="@SamSmith", url="https://x.example/Sam"))
        self.assertEqual(lint.run(self.store, strict=True).findings, [])

    def test_warn_unused_folder_part_and_unlinked_flow(self):
        self.put("folders", "spare", {"label": "Spare", "path": "C:\\spare", "kind": "other"})
        self.expect(lint.WARN, "unused-folder")
        self.put("parts", "part-spare", {"kind": "rule", "title": "Spare rule", "body": "x"})
        self.expect(lint.WARN, "unused-part")
        self.edit("flows", "welcome", lambda d: (d["nodes"].remove(self.node(d, "welcome-post")), d["edges"].remove([e for e in d["edges"] if e["to"] == "welcome-post"][0])))
        hits = self.expect(lint.WARN, "flow-unlinked")
        self.assertEqual(hits[0]["file"], "store/flows/posting.json")

    def test_warn_duplicate_name_and_email(self):
        self.edit("flows", "posting", lambda d: d.update(name="Welcome"))
        self.expect(lint.WARN, "duplicate-name")
        self.edit("accounts", "x-sam", lambda d: d.update(login="sam@example.com signs in"))
        self.expect(lint.WARN, "email")

    def test_no_file_can_excuse_an_error(self):
        """There is no baseline: an error fails the check whatever sits next to the store."""
        self.edit("flows", "welcome", lambda d: d["edges"].append({"from": "welcome-post", "to": "welcome-done", "type": "flow"}))
        f = lint.run(self.store).of(lint.ERROR)[0]
        (self.root / "lint-baseline.json").write_text(json.dumps({"known": [{"rule": f["rule"], "file": f["file"], "path": f["path"]}]}), encoding="utf-8")
        self.assertTrue(lint.run(self.store).failed())

    def test_lint_never_raises_on_garbage(self):
        for name, raw in (("a", b"[]"), ("b", b'{"nodes": "abc", "edges": 5}'), ("c", b'{"nodes": [{"id": "x"}], "edges": [{"from": 1}]}'), ("d", b"null")):
            (self.root / "store" / "flows" / (name + ".json")).write_bytes(raw)
        (self.root / "store" / "accounts" / "junk.json").write_bytes(b'{"owner": 5, "projects": "x"}')
        (self.root / "store" / "parts" / "part-junk.json").write_bytes(b'{"folders": 3}')
        rep = lint.run(self.store)
        self.assertTrue(rep.failed())
        self.assertGreaterEqual(len(rep.of(lint.ERROR)), 6)


if __name__ == "__main__":
    unittest.main()
