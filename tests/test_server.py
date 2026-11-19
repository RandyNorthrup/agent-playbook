"""The local server over HTTP, in this process on a free port, serving a copy of the fixture playbook."""
import copy
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse

from pbtest import PLAYBOOK, PlaybookCase, good_flow
import server
from lib import config


class ServerCase(PlaybookCase):
    TIMEOUT = server.TIMEOUT

    def setUp(self):
        PlaybookCase.setUp(self)
        self.opened = []
        self.srv = server.make_server(self.root, 0, page_dir=PLAYBOOK, log_path=self.tmp / "server.log", opener=self.fake_open,
                                      timeout=self.TIMEOUT)
        self.port = self.srv.server_address[1]
        self.token = self.srv.app.token
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)
        self.srv.app.close()

    def fake_open(self, path):
        self.opened.append(path)
        return os.path.exists(path)

    def req(self, method, path, body=None, headers=None, token=True, host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = dict(headers or {})
        if token:
            h.setdefault("X-Playbook-Token", self.token)
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host or "127.0.0.1:%d" % self.port)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None and "Content-Length" not in h:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            text = r.read()
            try:
                payload = json.loads(text.decode("utf-8"))
            except ValueError:
                payload = text
            return r.status, payload, dict(r.getheaders())
        finally:
            c.close()

    def get(self, path, **kw):
        return self.req("GET", path, token=False, **kw)

    def rev(self, col, doc_id):
        return self.get("/api/all")[1]["revs"][col][doc_id]

    def put(self, col, doc_id, doc, rev=None, reason=None, **kw):
        h = {"If-Match": '"%s"' % rev} if rev else {"If-None-Match": "*"}
        if reason:
            h["X-Change-Reason"] = urllib.parse.quote(reason)
        return self.req("PUT", "/api/%s/%s" % (col, doc_id), doc, h, **kw)

    def log_text(self):
        return (self.tmp / "server.log").read_text(encoding="utf-8")


class TestReads(ServerCase):
    def test_page_ping_and_headers(self):
        s, body, h = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn(self.token.encode(), body)
        self.assertNotIn(b"__PB_TOKEN__", body)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Cache-Control"], "no-store")
        s, body, _ = self.get("/api/ping")
        self.assertEqual((s, body["ok"], body["app"]), (200, True, config.APP))
        self.assertRegex(body["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.get("/favicon.ico")[0], 200)

    def test_all_carries_revisions_and_problems(self):
        (self.root / "store" / "flows" / "broken.json").write_bytes(b"{")
        (self.root / "store" / "flows" / "nan.json").write_bytes(b'{"a": NaN}')
        (self.root / "store" / "parts" / "part-odd.json").write_bytes(b'{"kind": "weird"}')
        s, body, _ = self.get("/api/all")
        self.assertEqual(s, 200)
        self.assertEqual(sorted(body["data"]["flows"]), ["posting", "welcome"])
        self.assertRegex(body["revs"]["flows"]["welcome"], r"^[0-9a-f]{16}$")
        self.assertEqual(sorted(e["file"] for e in body["errors"]), ["store/flows/broken.json", "store/flows/nan.json"])
        self.assertIn("part-odd", body["invalid"]["parts"])
        self.assertEqual(body["stamp"], self.get("/api/stamp")[1]["stamp"])
        raw = self.req("GET", "/api/all", token=False)[1]
        json.loads(json.dumps(raw), parse_constant=lambda c: self.fail("NaN in the answer"))

    def test_audit_changes_versions(self):
        s, body, _ = self.get("/api/audit")
        self.assertEqual(s, 200)
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(body["items"][0]["verified"], "checked live")
        self.assertFalse(any(k.startswith("_") for k in body["items"][0]))
        self.assertEqual(body["audit"], self.get("/api/stamp")[1]["audit"])
        self.assertEqual(self.get("/api/changes")[1], {"changes": []})
        self.assertEqual(self.get("/api/versions/flows/welcome")[1], {"versions": []})
        doc = self.doc("flows", "welcome")
        doc["summary"] = "v2"
        self.assertEqual(self.put("flows", "welcome", doc, self.rev("flows", "welcome"))[0], 200)
        v = self.get("/api/versions/flows/welcome")[1]["versions"]
        self.assertEqual(len(v), 1)
        s, body, _ = self.get("/api/versions/flows/welcome/" + v[0])
        self.assertEqual(body["doc"]["summary"], "Where a run starts: read the rules, then post.")
        self.assertEqual(self.get("/api/versions/flows/welcome/19990101-000000")[0], 404)
        self.assertEqual(self.get("/api/versions/flows/welcome/2026")[0], 404)
        self.assertEqual(self.get("/api/versions/bogus/x")[0], 404)
        self.assertEqual(self.get("/api/changes")[1]["changes"][0]["id"], "welcome")

    def test_unknown_paths_are_json_404(self):
        for path in ("/api/nope", "/playbook.html", "/store.json", "/api", "/api/flows/welcome"):
            s, body, h = self.get(path)
            self.assertEqual(s, 404, path)
            self.assertEqual(body["error"], "not found")
            self.assertIn("application/json", h["Content-Type"])


class TestGuards(ServerCase):
    def test_host_allow_list(self):
        for host in ("evil.example", "127.0.0.1", "127.0.0.1:1", "[::1]:%d" % self.port, "LOCALHOST:%d" % self.port):
            s, body, _ = self.get("/api/ping", host=host)
            self.assertEqual((s, body), (403, {"error": "bad host"}), host)
        self.assertEqual(self.get("/api/ping", host="localhost:%d" % self.port)[0], 200)
        self.assertEqual(self.put("flows", "sample", good_flow(), host="evil.example")[0], 403)

    def test_token_required_for_every_write(self):
        doc, rev = good_flow(), self.rev("flows", "welcome")
        for token in (False, "wrong", "caf\u00e9"):
            h = {"X-Playbook-Token": token} if token else {}
            self.assertEqual(self.req("PUT", "/api/flows/sample", doc, dict(h, **{"If-None-Match": "*"}), token=False)[0], 403)
            self.assertEqual(self.req("DELETE", "/api/flows/welcome", None, dict(h, **{"If-Match": rev, "X-Change-Reason": "x"}), token=False)[0], 403)
            self.assertEqual(self.req("POST", "/api/open", {"path": str(self.root)}, h, token=False)[0], 403)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())
        self.assertTrue((self.root / "store" / "flows" / "welcome.json").exists())
        self.assertEqual(self.opened, [])

    def test_no_cors_no_options_and_cross_site_refused(self):
        s, _, h = self.req("OPTIONS", "/api/flows/x", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "PUT"}, token=False)
        self.assertEqual(s, 501)
        self.assertFalse(any(k.lower().startswith("access-control") for k in h))
        self.assertFalse(any(k.lower().startswith("access-control") for k in self.get("/api/all")[2]))
        self.assertEqual(self.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        s, body, _ = self.get("/api/session", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((s, body["token"]), (200, self.token))


class TestWrites(ServerCase):
    def test_create_edit_and_revisions(self):
        s, body, _ = self.put("flows", "sample", good_flow())
        self.assertEqual(s, 200)
        rev = body["rev"]
        self.assertEqual(rev, self.rev("flows", "sample"))
        self.assertEqual(self.doc("flows", "sample"), good_flow())
        self.assertNotIn(b"\r", self.raw("flows", "sample"))
        doc = good_flow()
        doc["summary"] = "edited é"
        doc["origin"] = "kept"
        doc["nodes"][1]["origin"] = "kept too"
        s, body, _ = self.put("flows", "sample", doc, rev)
        self.assertEqual(s, 200)
        self.assertNotEqual(body["rev"], rev)
        self.assertEqual(self.doc("flows", "sample"), doc)
        self.assertEqual(self.changes()[-1]["by"], "canvas")

    def test_precondition_is_required(self):
        s, body, _ = self.req("PUT", "/api/flows/sample", good_flow())
        self.assertEqual(s, 428)
        self.assertEqual(self.req("DELETE", "/api/flows/welcome", headers={"X-Change-Reason": "x"})[0], 428)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_stale_write_gets_412_with_the_current_document(self):
        rev = self.rev("flows", "welcome")
        theirs = self.doc("flows", "welcome")
        theirs["summary"] = "changed by the agent"
        r = subprocess.run([sys.executable, str(PLAYBOOK / "pb.py"), "--root", str(self.root), "put", "flows", "welcome", self.write_json("t.json", theirs)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        mine = self.doc("flows", "welcome")
        mine["summary"] = "stale canvas copy"
        s, body, _ = self.put("flows", "welcome", mine, rev)
        self.assertEqual(s, 412)
        self.assertEqual(body["error"], "stale")
        self.assertEqual(body["current"]["summary"], "changed by the agent")
        self.assertEqual(body["rev"], self.rev("flows", "welcome"))
        self.assertEqual(self.doc("flows", "welcome")["summary"], "changed by the agent")
        s, body, _ = self.put("flows", "welcome", mine, body["rev"])
        self.assertEqual(s, 200)
        self.assertEqual(self.put("flows", "welcome", good_flow())[0], 412)  # "new" against a document that exists
        self.assertEqual(self.put("flows", "ghost", good_flow(), "0123456789abcdef")[0], 412)  # an edit of one that is gone
        s, body, _ = self.req("DELETE", "/api/flows/posting", headers={"If-Match": '"0000000000000000"', "X-Change-Reason": "x"})
        self.assertEqual(s, 412)
        self.assertTrue((self.root / "store" / "flows" / "posting.json").exists())

    def test_reason_rules(self):
        doc, rev = self.doc("flows", "welcome"), self.rev("flows", "welcome")
        cut = copy.deepcopy(doc)
        cut["nodes"] = [n for n in cut["nodes"] if n["id"] != "welcome-done"]
        cut["edges"] = [e for e in cut["edges"] if e["to"] != "welcome-done"]
        s, body, _ = self.put("flows", "welcome", cut, rev)
        self.assertEqual((s, body["error"], body["removed"]), (409, "reason required", ["Run finished"]))
        self.assertEqual(self.put("flows", "welcome", cut, rev, reason="   ")[0], 409)
        s, body, _ = self.put("flows", "welcome", cut, rev, reason="tést — why")
        self.assertEqual(s, 200)
        self.assertEqual(self.changes()[-1]["reason"], "tést — why")
        self.assertEqual(len(self.versions("flows", "welcome")), 1)

    def test_delete(self):
        rev = self.rev("parts", "part-read-back")
        s, body, _ = self.req("DELETE", "/api/parts/part-read-back", headers={"If-Match": rev})
        self.assertEqual((s, body["error"]), (409, "reason required"))
        s, _, _ = self.req("DELETE", "/api/parts/part-read-back", headers={"If-Match": rev, "X-Change-Reason": "audit%20cleanup"})
        self.assertEqual(s, 200)
        self.assertFalse((self.root / "store" / "parts" / "part-read-back.json").exists())
        self.assertEqual(len(os.listdir(str(self.root / "store" / ".trash"))), 1)
        self.assertEqual(self.changes()[-1]["reason"], "audit cleanup")
        s, body, _ = self.req("DELETE", "/api/parts/part-read-back", headers={"If-Match": rev, "X-Change-Reason": "again"})
        self.assertEqual((s, body["error"]), (404, "not found"))
        self.assertEqual(len(self.get("/api/versions/parts/part-read-back")[1]["versions"]), 1)

    def test_invalid_documents_get_400_with_findings(self):
        bad = good_flow()
        bad["nodes"][0]["kind"] = "weird"
        s, body, _ = self.put("flows", "sample", bad)
        self.assertEqual((s, body["error"]), (400, "invalid"))
        self.assertEqual(body["findings"][0]["path"], "nodes/s-start/kind")
        for doc in ({}, {"nodes": "abc"}, {"nodes": [1, 2]}, {"anything": {"goes": True}}):
            s, body, _ = self.put("flows", "sample", doc)
            self.assertEqual((s, body["error"]), (400, "invalid"), doc)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_bad_bodies_return_400(self):
        h = {"If-None-Match": "*"}
        cases = [b"{nope", b"[1]", b"", b'{"a": NaN, "b": Infinity}', b'{"a":"\xff\xfe"}', b'{"a":' + b"[" * 100000 + b"]" * 100000 + b"}", b'{"a": 1, "a": 2}']
        for raw in cases:
            s, body, hd = self.req("PUT", "/api/flows/sample", raw=raw, headers=h)
            self.assertEqual(s, 400, raw[:30])
            self.assertIn("error", body)
        self.assertEqual(self.req("PUT", "/api/flows/sample", raw=b"{}", headers=dict(h, **{"Content-Length": "abc"}))[0], 400)
        self.assertEqual(self.req("PUT", "/api/flows/sample", raw=b"", headers=dict(h, **{"Content-Length": "-1"}))[0], 400)
        s, body, _ = self.req("PUT", "/api/flows/sample", raw=b'{"a":"' + b"x" * 2000100 + b'"}', headers=h)
        self.assertEqual(s, 413)
        self.assertEqual(self.get("/api/ping")[0], 200)
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())

    def test_bad_ids(self):
        part = {"kind": "step", "title": "t"}
        for bad in ("..", ".", "a:b", "CON", "NUL", "UPPER", ".hidden", "a..b", "trail.", "zz~+@x", "part-x.json", "a%20b", "a%2Fb", "..%2F..%2Fx", "x" * 121, "no-prefix"):
            s, body, _ = self.put("parts", bad, part)
            self.assertIn(s, (400, 404), bad)
            self.assertIn("error", body)
        self.assertEqual(sorted(os.listdir(str(self.root / "store" / "parts"))), ["part-read-back.json"])
        self.assertFalse((self.root / "history").exists() and any(n.endswith(".json") for n in os.listdir(str(self.root / "history"))))
        self.assertEqual(self.get("/api/versions/parts/..")[0], 404)

    def test_unhandled_error_returns_500_json_and_is_logged(self):
        def boom(*a, **k):
            raise RuntimeError("kaboom")
        self.srv.app.store.write = boom
        s, body, _ = self.put("flows", "sample", good_flow())
        self.assertEqual(s, 500)
        self.assertEqual(body["error"], "server error")
        self.assertIn("kaboom", body["detail"])
        log = self.log_text()
        self.assertIn("PUT /api/flows/sample failed", log)
        self.assertIn("RuntimeError: kaboom", log)
        self.assertEqual(self.get("/api/ping")[0], 200)


class TestRawRequests(ServerCase):
    """Requests the standard library answers by itself, and clients that stall: always JSON, never a stuck thread."""
    TIMEOUT = 0.5

    def raw(self, data, wait=5):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        try:
            s.sendall(data)
            chunks = []
            while True:
                try:
                    c = s.recv(65536)
                except socket.timeout:
                    break
                if not c:
                    break
                chunks.append(c)
            return b"".join(chunks)
        finally:
            s.close()

    def parse(self, reply):
        if not reply.startswith(b"HTTP/"):  # the standard library answers a request line with no usable version
            return None, "", reply          # HTTP/0.9 style: the body alone
        head, _, body = reply.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), head.decode("latin-1"), body

    def test_errors_from_the_standard_library_are_json(self):
        host = ("Host: 127.0.0.1:%d\r\n" % self.port).encode("ascii")
        for data, code in ((b"PATCH /api/flows/x HTTP/1.1\r\n" + host + b"\r\n", 501),
                           (b"HEAD /api/ping HTTP/1.1\r\n" + host + b"\r\n", 501),
                           (b"GARBAGE\r\n\r\n", 400),
                           (b"GET / HTTP/1.1 extra\r\n" + host + b"\r\n", 400),
                           (b"GET / HTTP/9.9\r\n" + host + b"\r\n", 505)):
            status, head, body = self.parse(self.raw(data))
            if code == 501:
                self.assertEqual(status, code, data[:30])
                self.assertIn("nosniff", head)
            if status is not None:
                self.assertEqual(status, code, data[:30])
            if data.startswith(b"HEAD"):
                self.assertEqual(body, b"")  # a HEAD answer has no body
                continue
            if status is not None:
                self.assertIn("application/json", head)
            self.assertIn("error", json.loads(body.decode("utf-8")), data[:30])
        self.assertEqual(self.get("/api/ping")[0], 200)
        self.assertTrue(self.get("/api/ping")[2]["Server"].startswith("AgentPlaybook/"))

    def test_a_stalled_client_is_cut_off_and_others_are_served(self):
        host = ("Host: 127.0.0.1:%d\r\n" % self.port).encode("ascii")
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(idle.close)
        idle.sendall(b"GET /api/ping HTTP/1.1\r\n")  # headers never finish
        self.assertEqual(self.get("/api/ping")[0], 200)  # meanwhile everyone else is served
        self.assertEqual(idle.recv(100), b"")  # and the stalled one is closed after the timeout
        body = (b"PUT /api/flows/sample HTTP/1.1\r\n" + host + b"If-None-Match: *\r\nX-Playbook-Token: " +
                self.token.encode("ascii") + b"\r\nContent-Length: 100\r\n\r\n{\"name\"")  # a body that stops short
        status, head, payload = self.parse(self.raw(body))
        self.assertEqual(status, 408)
        self.assertEqual(json.loads(payload.decode("utf-8"))["error"], "request timed out")
        self.assertFalse((self.root / "store" / "flows" / "sample.json").exists())
        self.assertIn("stalled", self.log_text())
        self.assertNotIn("Traceback", self.log_text())


class TestOpen(ServerCase):
    def post(self, body, raw=None):
        return self.req("POST", "/api/open", body, raw=raw)

    def test_open_rejects_empty_and_network_paths(self):
        for body in ({}, {"path": ""}, {"path": "   "}, {"path": 5}, {"path": "\\\\server\\share"},
                     {"path": "//server/share"}, [1], "text"):
            s, payload, _ = self.post(body)
            self.assertEqual((s, payload["error"]), (400, "bad path"), body)
        self.assertEqual(self.post(None, raw=b"x")[0], 400)
        self.assertEqual(self.opened, [])

    def test_open_reads_folder_paths_like_pb_check(self):
        """~ is the home folder, and a relative path is taken from the skill folder (the parent of playbook/),
        the same as pb.py check reads a folder record; it still has to exist."""
        skill = os.path.normpath(str(self.root.parent))
        self.assertEqual(self.post({"path": "playbook/audit/posts.jsonl"})[0], 200)
        self.assertEqual(self.post({"path": "~"})[0], 200)
        self.assertEqual(self.post({"path": "playbook/no/such"})[0], 404)
        self.assertEqual(self.post({"path": "relative\\folder"})[0], 404)
        def real(p):  # the server opens the real path: no 8.3 short names (Windows), no /var -> /private/var link (macOS)
            return os.path.normcase(os.path.realpath(p))
        self.assertEqual([real(p) for p in self.opened[:3]],
                         [real(os.path.join(skill, "playbook", "audit", "posts.jsonl")), real(os.path.expanduser("~")),
                          real(os.path.join(skill, "playbook", "no", "such"))])

    def test_open_missing_and_existing(self):
        missing = str(self.root / "no" / "such" / "place")
        self.assertEqual(self.post({"path": missing})[0], 404)
        self.assertEqual(self.post({"path": str(self.root)})[0], 200)
        self.assertEqual(self.opened, [missing, str(self.root)])
        self.assertEqual(self.req("POST", "/api/other", {})[0], 404)

    def test_open_command_per_platform(self):
        folder, file = str(self.root), str(self.root / "audit" / "posts.jsonl")
        self.assertEqual(server.open_command(folder, "win32", "nt"), ["explorer", folder])
        self.assertEqual(server.open_command(file, "win32", "nt"), ["explorer", "/select,", file])
        self.assertEqual(server.open_command(folder, "darwin", "posix"), ["open", folder])
        self.assertEqual(server.open_command(file, "darwin", "posix"), ["open", "-R", file])
        self.assertEqual(server.open_command(folder, "linux", "posix"), ["xdg-open", folder])
        self.assertEqual(server.open_command(file, "linux", "posix"), ["xdg-open", os.path.dirname(file)])
        self.assertFalse(server.open_in_explorer(str(self.root / "missing")))
        bundle = self.root / "Calculator.app"  # an app bundle is a folder; `open` would launch it, so it is revealed
        bundle.mkdir()
        self.assertEqual(server.open_command(str(bundle), "darwin", "posix"), ["open", "-R", str(bundle)])
        self.assertEqual(server.open_command(str(bundle) + "/", "darwin", "posix"), ["open", "-R", str(bundle) + "/"])


class TestStartup(PlaybookCase):
    def test_port_setting(self):
        self.assertEqual(config.port(None) if not os.environ.get(config.PORT_ENV) else config.DEFAULT_PORT, config.DEFAULT_PORT)
        self.assertEqual(config.port("47399"), 47399)
        for bad in ("abc", "80", "70000", "-1"):
            with self.assertRaises(ValueError):
                config.port(bad)
        old = os.environ.get(config.PORT_ENV)
        os.environ[config.PORT_ENV] = "47444"
        try:
            self.assertEqual(config.port(), 47444)
            self.assertEqual(config.port("47555"), 47555)
        finally:
            if old is None:
                del os.environ[config.PORT_ENV]
            else:
                os.environ[config.PORT_ENV] = old

    def test_bind_error_is_a_non_zero_exit(self):
        srv = server.make_server(self.root, 0, page_dir=PLAYBOOK, log_path=None)
        try:
            port = srv.server_address[1]
            if port < 1024:
                self.skipTest("no usable port")
            r = subprocess.run([sys.executable, str(PLAYBOOK / "server.py"), "--root", str(self.root), "--port", str(port)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("cannot listen", r.stderr)
            self.assertIn("cannot listen", (self.root / "server.log").read_text(encoding="utf-8"))
        finally:
            srv.server_close()
            srv.app.close()
        r = subprocess.run([sys.executable, str(PLAYBOOK / "server.py"), "--port", "abc"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)

    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port < 1024:
            self.skipTest("no usable port")
        return port

    def launch(self, *args):
        return subprocess.run([sys.executable, str(PLAYBOOK / "launch.pyw"), "--no-browser"] + list(args),
                              capture_output=True, text=True, timeout=60)

    def test_help_does_nothing_else(self):
        """--help prints the usage and exits 0: the launcher starts no server, the installer makes no shortcut."""
        port = self.free_port()
        r = subprocess.run([sys.executable, str(PLAYBOOK / "launch.pyw"), "--help", "--port", str(port), "--root", str(self.root)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-browser", r.stdout)
        self.assertFalse((self.root / "server.log").exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        r = subprocess.run([sys.executable, str(PLAYBOOK / "install_shortcut.py"), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--remove", r.stdout)
        self.assertNotIn("created", r.stdout)
        r = subprocess.run([sys.executable, str(PLAYBOOK / "install_shortcut.py"), "--remvoe"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)  # a mistyped flag is refused, not taken as "create"
        self.assertNotIn("created", r.stdout)

    def test_launcher_says_when_another_program_has_the_port(self):
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(5)
        self.addCleanup(other.close)
        port = other.getsockname()[1]
        t = time.monotonic()
        r = self.launch("--port", str(port), "--root", str(self.root))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("in use by another program", r.stderr)
        self.assertLess(time.monotonic() - t, 30)
        self.assertFalse((self.root / "server.log").exists())  # no server was started to fail

    def test_launcher_stops_waiting_when_the_server_cannot_start(self):
        t = time.monotonic()
        r = self.launch("--port", str(self.free_port()), "--root", str(self.tmp / "no" / "such" / "playbook"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("stopped at once", r.stderr)
        self.assertLess(time.monotonic() - t, 7)  # well before the start timeout

    def test_launcher_starts_the_server_and_it_stops_cleanly(self):
        port = self.free_port()
        r = self.launch("--port", str(port), "--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.root / "server.log"
        pid = int([line for line in log.read_text(encoding="utf-8").splitlines() if "listening on" in line][-1].rsplit("pid ", 1)[1])

        def kill():
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=60)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.addCleanup(kill)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/api/ping")
        self.assertEqual(json.loads(c.getresponse().read().decode("utf-8"))["app"], config.APP)
        c.close()
        self.assertEqual(self.launch("--port", str(port), "--root", str(self.root)).returncode, 0)  # already up: reused
        if os.name == "nt":
            return  # Windows has no SIGTERM to send a detached process
        os.kill(pid, signal.SIGTERM)  # POSIX: a clean stop that says so in the log
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "stopped" not in log.read_text(encoding="utf-8"):
            time.sleep(0.1)
        self.assertIn("stopped", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
