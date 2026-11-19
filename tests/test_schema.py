"""The schema, the id rule and the canonical form. Run: python -m unittest discover -s tests -v"""
import copy
import json
import random
import unittest

from pbtest import FIXTURE, good_flow, validate
from lib import ids, schema, textio


def fixture_docs():
    for col in ids.COLS:
        for path in sorted((FIXTURE / "store" / col).glob("*.json")):
            yield col, path.stem, path


class TestIds(unittest.TestCase):
    def test_id_rules_shared(self):
        for good in ("x", "promo-post", "a1-b2", "part-read-back"):
            self.assertIsNone(ids.id_problem(good), good)
        for bad in ("", "..", ".", "a:b", "CON", "con", "nul", "com1", "UPPER", "a b", "a/b", "a\\b", "../x", ".hidden", "trail.",
                    "a..b", "a--b", "-a", "a-", "a_b", "x" * 65, "zz.json", None, 5, ["x"]):
            self.assertTrue(ids.id_problem(bad), repr(bad))

    def test_reserved_words_and_part_prefix(self):
        self.assertTrue(ids.id_problem("history", "flows"))
        self.assertIsNone(ids.id_problem("history", "folders"))
        self.assertTrue(ids.id_problem("read-back", "parts"))
        self.assertIsNone(ids.id_problem("part-read-back", "parts"))

    def test_version_names(self):
        self.assertTrue(ids.valid_version("20261109-201131"))
        self.assertTrue(ids.valid_version("20261109-201131-123456-0a1b2c3d"))
        for bad in ("2026", "../x", "20261109-201131-x", "", None):
            self.assertFalse(ids.valid_version(bad))


class TestSchema(unittest.TestCase):
    def test_valid_fixture_store_passes(self):
        n = 0
        for col, doc_id, path in fixture_docs():
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(validate(col, doc_id, doc), [], path)
            n += 1
        self.assertGreaterEqual(n, 5)

    def test_good_flow_is_valid(self):
        self.assertEqual(validate("flows", "sample", good_flow()), [])
        for stamp in ("2026-11-09T14:02:00Z", "2026-11-09T14:02Z", "2028-02-29T23:59:59.123456-08:00", "2026-11-09T00:00:00+05:30"):
            self.assertEqual(validate("flows", "sample", dict(good_flow(), editedAt=stamp)), [], stamp)

    def check(self, change, expect, col="flows", doc_id="sample", base=None):
        doc = copy.deepcopy(base if base is not None else good_flow())
        change(doc)
        found = validate(col, doc_id, doc)
        self.assertTrue(any(expect in m for m in found), "expected %r in %r" % (expect, found))

    def test_each_flow_rule_rejects(self):
        cases = [
            (lambda d: d.update(extra=1), "unknown key 'extra'"),
            (lambda d: d.pop("name"), "missing 'name'"),
            (lambda d: d.update(name=""), "name: is empty"),
            (lambda d: d.update(name=" x "), "starts or ends with a space"),
            (lambda d: d.update(name="x" * 61), "the limit is 60"),
            (lambda d: d.update(group="Nope"), "is not one of"),
            (lambda d: d.update(icon="unicorn"), "is not one of"),
            (lambda d: d.update(summary="a\nb"), "must be a single line"),
            (lambda d: d.update(order="3"), "must be a whole number"),
            (lambda d: d.update(order=True), "must be a whole number"),
            (lambda d: d.update(order=1000), "is outside 0 to 999"),
            (lambda d: d.update(nodes=[]), "nodes: is empty"),
            (lambda d: d.update(nodes="abc"), "must be a list"),
            (lambda d: d.update(nodes=[1, 2]), "must be an object"),
            (lambda d: d.update(edges=None), "must be a list"),
            (lambda d: d.update(editedAt="yesterday"), "must be a timestamp"),
            (lambda d: d.update(editedAt="2026-13-01T10:00:00Z"), "must be a timestamp"),  # the right shape, no such moment
            (lambda d: d.update(editedAt="2026-02-30T10:00:00Z"), "must be a timestamp"),
            (lambda d: d.update(editedAt="2026-11-09T24:00:00Z"), "must be a timestamp"),
            (lambda d: d.update(editedAt="2026-11-09T14:60Z"), "must be a timestamp"),
            (lambda d: d.update(editedAt="2026-11-09T14:02:00+25:00"), "must be a timestamp"),
            (lambda d: d.update(origin="x" * 4001), "the limit is 4000"),
            (lambda d: d.update(copiedFrom="Bad Id"), "lower-case"),
        ]
        for change, expect in cases:
            self.check(change, expect)

    def test_each_node_rule_rejects(self):
        def node(i=1):
            return lambda fn: (lambda d: fn(d["nodes"][i]))
        n1 = node(1)
        cases = [
            (n1(lambda n: n.pop("id")), "missing 'id'"),
            (n1(lambda n: n.update(id="Bad Id")), "lower-case"),
            (n1(lambda n: n.update(id="s-start")), "used by two nodes"),
            (n1(lambda n: n.update(kind="weird")), "is not one of"),
            (n1(lambda n: n.pop("title")), "missing 'title'"),
            (n1(lambda n: n.update(title="x" * 81)), "the limit is 80"),
            (n1(lambda n: n.update(sub="x" * 121)), "the limit is 120"),
            (n1(lambda n: n.update(body="x" * 1201)), "the limit is 1200"),
            (n1(lambda n: n.update(body="a\tb")), "contains a tab"),
            (n1(lambda n: n.update(checks=["ok", ""])), "checks[1]: is empty"),
            (n1(lambda n: n.update(checks=["x"] * 13)), "the limit is 12"),
            (n1(lambda n: n.update(folders=["a", "a"])), "listed twice"),
            (n1(lambda n: n.update(folders=[{"path": "C:\\x"}])), "must be an id"),
            (n1(lambda n: n.update(links=[{"label": "x", "url": "vscode://x"}])), "is not an http(s) link"),
            (n1(lambda n: n.update(links=[{"label": "x", "url": "https://x.io", "extra": 1}])), "unknown key 'extra'"),
            (n1(lambda n: n.update(src=[5])), "must be text"),
            (n1(lambda n: n.update(x=15)), "is not a multiple of 10"),
            (n1(lambda n: n.update(x=None)), "must be a whole number"),
            (n1(lambda n: n.update(y=30000)), "is outside"),
            (n1(lambda n: n.pop("x")), "missing 'x'"),
            (n1(lambda n: n.update(goto="posting")), "only a goto node"),
            (n1(lambda n: n.update(account="x-sam")), "only an account node"),
            (n1(lambda n: n.update(kind="goto")), "missing 'goto'"),
            (n1(lambda n: n.update(kind="account")), "missing 'account'"),
            (n1(lambda n: n.update(mystery=1)), "unknown key 'mystery'"),
            (n1(lambda n: n.update(body="python music\bedcheck.py")), "contains a control character"),
            (n1(lambda n: n.update(title="Ring \a the bell")), "contains a control character"),
            (n1(lambda n: n.update(part="part-x")), "comes from the part"),
            (n1(lambda n: (n.clear(), n.update(id="s-step", part="not-a-part", x=0, y=0))), "a part id starts with 'part-'"),
        ]
        for change, expect in cases:
            self.check(change, expect)

    def test_node_with_part_reference_is_valid(self):
        d = good_flow()
        d["nodes"][1] = {"id": "s-step", "part": "part-read-back", "x": 320, "y": 0, "origin": "from a part", "folders": ["helper-scripts"]}
        self.assertEqual(validate("flows", "sample", d), [])

    def test_each_edge_rule_rejects(self):
        def edge(fn):
            return lambda d: fn(d["edges"][0])
        cases = [
            (edge(lambda e: e.pop("type")), "missing 'type'"),
            (edge(lambda e: e.update(type="dotted")), "is not one of"),
            (edge(lambda e: e.update(to="ghost")), "is not a node in this flow"),
            (edge(lambda e: e.pop("from")), "missing 'from'"),
            (edge(lambda e: e.update(to="s-start")), "connects a node to itself"),
            (edge(lambda e: e.update(label="x" * 25)), "the limit is 24"),
            (edge(lambda e: e.update(id="e1")), "must be 's-start>s-step'"),
            (edge(lambda e: e.update(weight=2)), "unknown key 'weight'"),
            (lambda d: d["edges"].append(dict(d["edges"][0])), "listed twice"),
            (lambda d: d["edges"].append("x"), "must be an object"),
        ]
        for change, expect in cases:
            self.check(change, expect)

    def test_account_folder_part_rules_reject(self):
        acct = json.loads((FIXTURE / "store" / "accounts" / "x-sam.json").read_text(encoding="utf-8"))
        folder = json.loads((FIXTURE / "store" / "folders" / "helper-scripts.json").read_text(encoding="utf-8"))
        part = json.loads((FIXTURE / "store" / "parts" / "part-read-back.json").read_text(encoding="utf-8"))
        for change, expect in [
            (lambda d: d.update(platform="MySpace"), "is not one of"),
            (lambda d: d.update(status="Active"), "is not one of"),
            (lambda d: d.update(handle="", label=""), "needs a handle or a label"),
            (lambda d: d.update(url="ftp://x"), "is not an http(s) link"),
            (lambda d: d.update(flow="No Such"), "lower-case"),
            (lambda d: d.update(projects=["ok", 5]), "must be an id"),
            (lambda d: d.update(notes="one"), "must be a list"),
            (lambda d: d.update(password="hunter2"), "unknown key 'password'"),
        ]:
            self.check(change, expect, "accounts", "x-sam", acct)
        for change, expect in [
            (lambda d: d.pop("path"), "missing 'path'"),
            (lambda d: d.update(kind="misc"), "is not one of"),
            (lambda d: d.update(label=""), "label: is empty"),
            (lambda d: d.update(machine="pc1"), "unknown key 'machine'"),
        ]:
            self.check(change, expect, "folders", "helper-scripts", folder)
        for change, expect in [
            (lambda d: d.update(id="x"), "unknown key 'id'"),
            (lambda d: d.update(x=0), "unknown key 'x'"),
            (lambda d: d.pop("kind"), "missing 'kind'"),
        ]:
            self.check(change, expect, "parts", "part-read-back", part)
        self.assertTrue(validate("parts", "read-back", part))  # the id rule applies to the file name too

    def test_empty_flow_field_is_allowed_on_accounts(self):
        acct = json.loads((FIXTURE / "store" / "accounts" / "x-sam.json").read_text(encoding="utf-8"))
        acct["flow"] = ""
        self.assertEqual(validate("accounts", "x-sam", acct), [])

    def test_validator_never_raises(self):
        junk = [None, 1, 1.5, True, "text", [], [1, [2, [3]]], {}, {"nodes": "abc"}, {"nodes": [1, None, {"id": []}], "edges": [[], {"from": {}, "to": 1}]},
                {"nodes": [{"id": "a", "x": "1", "y": [], "links": [1, {"url": 5}], "folders": 7, "checks": {"a": 1}}], "edges": {}},
                {1: 2}, {"name": {"deep": {"deeper": []}}}]
        rnd = random.Random(7)
        keys = sum((list(v) for v in schema.KEYS.values()), [])
        for _ in range(300):
            junk.append(dict((rnd.choice(keys), rnd.choice(junk[:12])) for _ in range(rnd.randint(0, 8))))
        for col in list(ids.COLS) + ["bogus", None]:
            for doc in junk:
                for doc_id in ("ok-id", "", None, "..", 5):
                    found = schema.validate(col, doc_id, doc)  # must not raise
                    self.assertIsInstance(found, list)
                    for f in found:
                        self.assertEqual(len(f), 3)
        self.assertTrue(schema.validate("flows", "x", None))
        self.assertTrue(schema.validate("bogus", "x", {}))


class TestCanonical(unittest.TestCase):
    def test_nan_and_duplicate_keys_rejected_on_read(self):
        for bad in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1, "a": 2}', "{nope", "", b"\xff\xfe"):
            with self.assertRaises(ValueError):
                textio.loads(bad)
        with self.assertRaises(ValueError):
            textio.loads("[" * 100000 + "]" * 100000)
        self.assertEqual(textio.loads(b'\xef\xbb\xbf{"a": "\xc3\xa9"}'), {"a": "\u00e9"})

    def test_nan_rejected_on_write(self):
        d = good_flow()
        d["order"] = float("nan")
        with self.assertRaises(ValueError):
            schema.canonical_bytes("flows", d)

    def test_canonical_bytes(self):
        d = good_flow()
        data = schema.canonical_bytes("flows", d)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"}\n") and not data.endswith(b"\n\n"))
        self.assertEqual(data, schema.canonical_bytes("flows", json.loads(data.decode("utf-8"))))
        # the same content in another key order, with the things that say nothing, is the same bytes
        shuffled = {"edges": [dict(reversed(list(e.items())), id="%s>%s" % (e["from"], e["to"]), label="") for e in d["edges"]],
                    "nodes": [dict(reversed(list(n.items())), sub="", checks=[], folders=[], links=[], src=[]) for n in d["nodes"]]}
        for k in reversed(list(d)):
            shuffled.setdefault(k, d[k])
        self.assertEqual(schema.canonical_bytes("flows", shuffled), data)
        self.assertEqual(schema.revision("flows", shuffled), schema.revision("flows", d))
        self.assertEqual(list(json.loads(data.decode("utf-8"))), [k for k in schema.KEYS["flow"] if k in d])
        self.assertIn("\u2192".encode("utf-8"), schema.canonical_bytes("folders", {"label": "a \u2192 b", "path": "p", "kind": "other"}))

    def test_fixture_files_are_canonical(self):
        for col, doc_id, path in fixture_docs():
            raw = path.read_bytes().replace(b"\r\n", b"\n")  # git may check a text file out with CRLF on Windows
            self.assertEqual(raw, schema.canonical_bytes(col, json.loads(raw.decode("utf-8"))), path)

    def test_roundtrip_preserves_origin(self):
        d = good_flow()
        d["origin"] = "why this flow exists"
        d["nodes"][1]["origin"] = "Sam, 01-02: said so"
        back = json.loads(schema.canonical_bytes("flows", d).decode("utf-8"))
        self.assertEqual(back["origin"], "why this flow exists")
        self.assertEqual(back["nodes"][1]["origin"], "Sam, 01-02: said so")
        self.assertEqual(back, d)

    def test_upgrade_adds_type_and_drops_edge_ids(self):
        old = good_flow()
        old["edges"] = [{"id": "sample-e1", "from": "s-start", "to": "s-step"}, {"from": "s-step", "to": "s-end", "type": "attach"}]
        self.assertTrue(validate("flows", "sample", old))
        new = schema.upgrade("flows", old)
        self.assertEqual(validate("flows", "sample", new), [])
        self.assertEqual(new["edges"], [{"from": "s-start", "to": "s-step", "type": "flow"}, {"from": "s-step", "to": "s-end", "type": "attach"}])
        self.assertIn("id", old["edges"][0])  # the input is not touched

    def test_resolve_part_reference(self):
        parts = {"part-read-back": {"kind": "check", "title": "Read it back", "body": "Read.", "checks": ["a"], "folders": ["f1"]}}
        node = {"id": "n", "part": "part-read-back", "x": 0, "y": 0, "folders": ["f2", "f1"], "origin": "o"}
        r, missing = schema.resolve_node(node, parts)
        self.assertIsNone(missing)
        self.assertEqual((r["kind"], r["title"], r["body"], r["checks"], r["folders"], r["origin"]), ("check", "Read it back", "Read.", ["a"], ["f1", "f2"], "o"))
        self.assertNotIn("title", node)
        r, missing = schema.resolve_node({"id": "n", "part": "part-gone", "x": 0, "y": 0}, parts)
        self.assertEqual(missing, "part-gone")
        self.assertIn("missing part", r["title"])


if __name__ == "__main__":
    unittest.main()
