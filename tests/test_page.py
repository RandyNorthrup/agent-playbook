"""The canvas page: the build is reproducible, its script parses, its lists match the Python schema, and its
saving logic behaves (run under Node with a stand-in DOM; skipped when Node is not installed)."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from pbtest import PLAYBOOK, ROOT
from lib import ids, schema
from test_server import ServerCase

def find_node():
    """Node 18 or newer (the harness uses its built-in fetch), or None."""
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        return exe if int(v.lstrip("v").split(".")[0]) >= 18 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


NODE = find_node()
SRC = PLAYBOOK / "app" / "playbook.src.html"
PAGE = PLAYBOOK / "playbook.html"


def page_script(html):
    return html[html.index("<script>") + 8:html.rindex("</script>")]


def js_list(src, name):
    """The strings in `const NAME = [...]` (or `new Set([...])`) from the page source."""
    m = re.search(r"const %s = (?:new Set\()?\[(.*?)\]" % re.escape(name), src, re.S)
    if not m:
        raise AssertionError("the page no longer defines %s" % name)
    return re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1))


class TestBuild(unittest.TestCase):
    def test_build_is_byte_reproducible(self):
        sys.path.insert(0, str(PLAYBOOK / "app"))
        try:
            import build_page
        finally:
            sys.path.remove(str(PLAYBOOK / "app"))
        built = build_page.build()
        self.assertEqual(built, build_page.build())
        self.assertNotIn(b"\r", built)
        self.assertTrue(built.endswith(b"</body></html>\n"))
        # the tracked page is what the source builds (a Windows checkout may hold it with CRLF)
        self.assertEqual(PAGE.read_bytes().replace(b"\r\n", b"\n"), built, "playbook.html is stale: run python agent-playbook/playbook/app/build_page.py")
        r = subprocess.run([sys.executable, str(PLAYBOOK / "app" / "build_page.py"), "--check"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_help_does_not_build(self):
        before = PAGE.stat().st_mtime_ns
        script = str(PLAYBOOK / "app" / "build_page.py")
        for flag in ("--help", "-h"):
            r = subprocess.run([sys.executable, script, flag], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("--check", r.stdout)
            self.assertNotIn("wrote", r.stdout)
        r = subprocess.run([sys.executable, script, "--chek"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown argument --chek", r.stderr)
        self.assertEqual(PAGE.stat().st_mtime_ns, before, "a help or a bad argument must not rewrite playbook.html")

    def test_placeholders(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("__ICONS__", html)
        self.assertEqual(html.count("__PB_TOKEN__"), 1)  # filled in by the server on every request

    @unittest.skipUnless(NODE, "node 18+ is not installed")
    def test_page_script_parses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            js = os.path.join(d, "page.js")
            with open(js, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(page_script(PAGE.read_text(encoding="utf-8")))
            r = subprocess.run([NODE, "--check", js], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)


class TestPageMatchesSchema(unittest.TestCase):
    def setUp(self):
        self.src = SRC.read_text(encoding="utf-8")

    def test_page_enums_match_python(self):
        self.assertEqual(js_list(self.src, "KIND_ORDER"), list(schema.KINDS))
        self.assertEqual(js_list(self.src, "GROUPS"), list(schema.GROUPS))
        self.assertEqual(js_list(self.src, "FLOW_ICONS"), list(schema.FLOW_ICONS))
        self.assertEqual(js_list(self.src, "PLATFORMS"), list(schema.PLATFORMS))
        self.assertEqual(js_list(self.src, "STATUSES"), list(schema.STATUSES))
        self.assertEqual(js_list(self.src, "FOLDER_KINDS"), list(schema.FOLDER_KINDS))
        self.assertEqual(js_list(self.src, "COLS"), list(ids.COLS))
        self.assertEqual(js_list(self.src, "SIDE_KINDS"), list(schema.SIDE_KINDS))
        self.assertEqual(js_list(self.src, "PART_FIELDS"), list(schema.FROM_PART))
        self.assertEqual(sorted(js_list(self.src, "RESERVED")), sorted(ids.RESERVED))

    def test_page_icons_exist(self):
        icons = json.loads((PLAYBOOK / "app" / "icons.json").read_text(encoding="utf-8"))
        for name in schema.FLOW_ICONS:
            self.assertIn(name, icons)

    def test_page_limits_match_schema(self):
        self.assertIn('id="nf_title" value="${esc(n.title)}" maxlength="%d"' % schema.MAX["title"], self.src)
        self.assertIn('maxlength="%d"' % schema.MAX["sub"], self.src)
        self.assertIn('id="nf_body" rows="9" maxlength="%d"' % schema.MAX["body"], self.src)
        self.assertIn('id="ef_label" value="${esc(e.label || \'\')}" maxlength="%d"' % schema.MAX["label"], self.src)
        self.assertIn("GRID = %d" % schema.MAX["grid"], self.src)

    def test_dead_code_is_gone(self):
        for gone in ("scheduleRender", "db:null", "rqCols", "S.canWrite !== null", "updatedAt", "groupOptions"):
            self.assertNotIn(gone, self.src)


@unittest.skipUnless(NODE, "node 18+ is not installed")
class TestPageLogic(ServerCase):
    """The page's own save code, run under Node against the test server (see page_harness.js)."""

    def test_saving_logic(self):
        import socket
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()  # nothing listens here: this is "the server is down"
        r = subprocess.run([NODE, str(ROOT / "tests" / "page_harness.js"), str(PAGE), "http://127.0.0.1:%d" % self.port, str(dead_port)],
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        o = json.loads(r.stdout)

        self.assertEqual(o["boot"], {"mode": "live", "flows": ["posting", "welcome"], "revs": ["posting", "welcome"], "status": "saved", "canWrite": True})

        # an edit is saved; the stale token is refreshed once and the save repeated
        e = o["edit"]
        self.assertTrue(e["settled"])
        self.assertEqual(e["calls"], ["PUT /api/flows/welcome", "GET /api/session", "PUT /api/flows/welcome"])
        self.assertEqual((e["onDisk"], e["status"], e["revMatches"]), ("edited in the page", "saved", True))
        self.assertEqual(e["originKept"], 'Sam, 01-02: "always read the rules first".')  # a field the page does not edit survives
        self.assertTrue(e["noEdgeIds"])

        # a stale save does not overwrite the outside change; the page takes the newer version and says so
        s = o["stale"]
        self.assertEqual(o["outsidePut"], 200)
        self.assertEqual((s["onDisk"], s["inPage"]), ("changed outside", "changed outside"))
        self.assertTrue(s["revMatches"])
        self.assertIn("was changed outside this window", s["toast"])
        self.assertEqual((s["undoLeft"], s["unsaved"], s["dirty"]), (0, [], []))

        self.assertEqual(o["poll"], {"inPage": "posting changed outside", "stampMatches": True})

        # a refused save is tried once, reported, and not repeated
        f = o["refused"]
        self.assertEqual((f["puts"], f["unsaved"], f["dirty"], f["status"]), (1, ["welcome"], [], "error"))
        self.assertIn("Not saved", f["toast"])
        self.assertIn("is not one of", f["toast"])
        self.assertEqual(f["afterFix"], {"status": "saved", "unsaved": []})

        # with the server down the change is kept and retried with a growing wait (about 1 s, 2 s, 4 s ...), with one toast
        g = o["outage"]
        self.assertGreaterEqual(g["tries"], 1)
        self.assertLessEqual(g["tries"], 4)  # without backoff it would be a dozen or more in four seconds
        self.assertEqual(g["fails"], g["tries"])
        self.assertTrue(g["stillDirty"])
        self.assertEqual((g["status"], g["toasts"]), ("error", 1))
        self.assertTrue(g["settled"])
        self.assertEqual((g["onDisk"], g["statusAfter"]), ("saved after the outage", "saved"))

        # a node that references a part stores only the reference
        p = o["parts"]
        self.assertEqual(p["stored"], ["id", "part", "x", "y"])
        self.assertEqual((p["shown"], p["kind"], p["uses"]), ("Read the field back", "check", 1))
        self.assertEqual(p["added"]["keys"], ["editedAt", "id", "part", "x", "y"])
        self.assertTrue(p["added"]["idOk"])
        self.assertEqual(p["added"]["cleaned"], ["editedAt", "id", "part", "x", "y"])
        self.assertTrue(p["savedRef"])
        self.assertEqual(p["onDiskRef"]["part"], "part-read-back")
        self.assertNotIn("title", p["onDiskRef"])
        d = p["onDiskDetached"]
        self.assertNotIn("part", d)
        self.assertEqual((d["kind"], d["title"], d["checks"]), ("check", "Read the field back", ["Text matches the draft"]))
        self.assertEqual(p["status"], "saved")

        a = o["account"]
        self.assertEqual((a["saved"], a["onDisk"]), (True, "kept by the page"))
        self.assertEqual((a["staleSave"], a["afterStale"]), (False, "changed outside"))

        # the board, the details panel, the edit form and the pages render, and show provenance and part references
        v = o["render"]
        for key in ("board", "plain", "form", "partBoard", "part"):
            self.assertTrue(all(v[key]), "%s: %r" % (key, v[key]))
        self.assertTrue(v["partHasNoEditButton"])
        self.assertIsNone(v["editOnPart"]["editing"])
        self.assertIn("comes from a part", v["editOnPart"]["toast"])
        self.assertEqual(v["detached"], {"part": False, "title": "Read the field back", "inspectorHasEdit": True})
        self.assertEqual(v["detachedOnDisk"], "Read the field back")
        for name in ("home", "accounts", "folders", "parts"):
            self.assertGreater(o["pages"][name], 100, name)
        self.assertTrue(all(o["pages"]["partsText"]) and all(o["pages"]["accountsText"]))
        self.assertEqual(o["status"], "saved")

        # the forms open; a goto asks for its target before the node exists; the account and folder forms keep
        # fields they do not show (origin on the account, updatedAt on the folder)
        f = o["forms"]
        for key in ("flow", "newFlow", "gotoPicker", "accountPicker", "part", "account", "folder"):
            self.assertTrue(all(f[key]), "%s: %r" % (key, f[key]))
        self.assertEqual(f["gotoNotAddedYet"], 6)
        self.assertEqual(f["accountSaved"], {"label": "Edited in the form", "status": "paused", "origin": "kept by the page", "notes": ["One note"], "hasId": False})
        self.assertEqual(f["folderSaved"], {"path": "C:\\fixture\\scripts2", "note": "Edited note", "updatedAt": "2026-01-01T00:00:00Z", "hasId": False})
        self.assertEqual(f["status"], "saved")

        # a form whose save fails (server down) stays open with what was typed, the banner says the server is gone,
        # and the next try saves it and closes the form
        g = o["offlineForm"]
        self.assertTrue(g["open"])
        self.assertEqual(g["inPage"], "Edited in the form")  # the stored account is unchanged until the save lands
        self.assertIn("then try again", g["toast"])  # the toast text is captured HTML-escaped
        self.assertTrue(g["banner"])
        self.assertEqual(g["afterRetry"], {"open": False, "onDisk": "Typed while the server was down", "banner": False})

        # changed elsewhere while the form was open: nothing typed is lost, nothing of theirs is overwritten silently
        c = o["conflictForm"]
        self.assertTrue(c["open"])
        self.assertEqual((c["inPage"], c["onDisk"]), ("Changed by someone else", "Changed by someone else"))
        self.assertIn("still in the form", c["toast"])
        self.assertEqual(c["afterSaveAgain"], {"open": False, "onDisk": "Typed during a conflict", "origin": "kept from their version"})

        self.assertEqual(o["pending"], {"whileDirty": True, "afterSave": False})  # what the unload warning asks
        self.assertTrue(all(o["kindOf"]) and len(o["kindOf"]) > 2, o["kindOf"])

        # fit: whole when readable; a very wide flow opens readable at its left edge, F again shows all, then back
        t = o["fit"]
        self.assertEqual((t["small"], t["smallZ"]), ("all", 1.05))
        self.assertEqual((t["wide"], t["wideZ"], t["wideX"]), ("read", 0.75, 60))
        self.assertEqual(t["again"], "all")
        self.assertLess(t["againZ"], 0.55)
        self.assertEqual(t["thirdZ"], 0.75)

        # deleting an account asks why; the step that showed it becomes an ordinary step that keeps its text
        a = o["acctDel"]
        self.assertTrue(all(a["asked"]), a["asked"])
        self.assertTrue(a["gone"])
        self.assertEqual(a["node"], {"kind": "step", "title": "@sam", "account": False})
        self.assertIn("Account deleted", a["toast"])
        self.assertEqual(a["status"], "saved")
        status, payload, _ = self.get("/api/changes")
        self.assertEqual(status, 200)
        gone = [c for c in payload["changes"] if c.get("col") == "accounts" and c.get("id") == "x-sam" and c.get("action") == "delete"]
        self.assertEqual([c.get("reason") for c in gone], ["Replaced by a new account"])
        moved = [c for c in payload["changes"] if c.get("col") == "flows" and c.get("id") == "posting" and "@sam was deleted" in (c.get("reason") or "")]
        self.assertTrue(moved, "the flow change that turned the account step into a plain step says why")


if __name__ == "__main__":
    unittest.main()
