"""Agent Playbook canvas: local server. Serves the canvas page and keeps every flow, account, folder and part
as one JSON file under store/, so edits land on disk where agents can read them.

  python server.py [--port N]     default 47321, or $AGENT_PLAYBOOK_PORT

Binds to 127.0.0.1 only. Every request must name this server in its Host header; every write and every
"open folder" needs the per-run token the page is served with. Every save carries the revision it started
from (If-Match), so a stale save is refused with 412 and the current document instead of overwriting it.
Every error is answered as JSON and written to server.log. launch.pyw starts this without a console window.
"""
import argparse
import logging
import logging.handlers
import os
import pathlib
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import lib  # noqa: E402
from lib import auditlog, config, history, ids, lint, schema, textio  # noqa: E402
from lib import store as storelib  # noqa: E402
from lib.store import ReasonRequired, clean_reason, removed_items, title_of  # noqa: E402,F401

COLS = ids.COLS
MAX_BODY = 2000000
TIMEOUT = 20.0  # seconds a client may stall mid-request before its connection is closed
NO_WINDOW = 0x08000000
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                    ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", CSP))

# the playbook next to this file: pb.py and the tests call these directly
_store = storelib.Store(HERE)
STORE = _store.dir
TRASH = _store.trash
HISTORY = _store.history
AUDIT = HERE / "audit" / "posts.jsonl"


def write_doc(col, doc_id, data, by="canvas", reason="", base=None):
    """Save a document. If it drops steps or connections from a flow, `reason` is required (ReasonRequired otherwise)."""
    return _store.write(col, doc_id, data, by=by, reason=reason, base=base)


def delete_doc(col, doc_id, by="canvas", reason="", base=None):
    """Move a document to store/.trash. `reason` is required (ReasonRequired otherwise). Missing documents are ignored."""
    try:
        return _store.delete(col, doc_id, by=by, reason=reason, base=base)
    except storelib.NotFound:
        return None


def read_changes(limit=400):
    return history.read_changes(_store, limit)


def versions(col, doc_id):
    return history.versions(_store, col, doc_id)


def read_all():
    return _store.load_all()[0]


def stamp():
    return _store.stamp()


# ---------------------------------------------------------------- opening folders

def path_problem(path):
    """Why a path may not be opened, or None: nothing empty, and nothing on a network share."""
    if not isinstance(path, str) or not path.strip():
        return "no path given"
    if lint.folder_path(path, HERE.parent) is None:
        return "network paths are not opened"
    return None


def open_command(path, platform=None, name=None):
    """The command that shows a folder, or selects a file, in the system file manager. It never runs the file."""
    platform = platform or sys.platform
    name = name or os.name
    is_dir = os.path.isdir(path)
    if name == "nt":
        return ["explorer", path] if is_dir else ["explorer", "/select,", path]
    if platform == "darwin":
        # a folder with an extension may be a bundle (an .app is a folder): `open` would launch it, so reveal it
        bundle = bool(os.path.splitext(os.path.basename(path.rstrip("/")))[1])
        return ["open", path] if is_dir and not bundle else ["open", "-R", path]
    return ["xdg-open", path if is_dir else os.path.dirname(path) or "/"]


def open_in_explorer(path):
    """Show a folder (or select a file) in the file manager. False when it does not exist or cannot be shown."""
    if not os.path.exists(path):
        return False
    kw = {"creationflags": NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        subprocess.Popen(open_command(path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- the app

class HttpError(Exception):
    def __init__(self, code, error, **extra):
        Exception.__init__(self, error)
        self.code = code
        self.payload = dict(extra, error=error)


def make_logger(path):
    log = logging.getLogger("agent-playbook-server-%s" % path)
    log.propagate = False
    if path and not log.handlers:
        try:
            h = logging.handlers.RotatingFileHandler(str(path), maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(h)
        except OSError:
            pass
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.setLevel(logging.INFO)
    return log


class App(object):
    """One playbook being served: its store, its token, where its page and log live."""

    def __init__(self, root=None, page_dir=None, token=None, log_path="default", opener=None):
        self.root = pathlib.Path(str(root or HERE)).resolve()
        self.store = _store if self.root == HERE else storelib.Store(self.root)
        self.page_dir = pathlib.Path(str(page_dir or HERE))
        self.token = token or secrets.token_urlsafe(24)
        self.audit_log = self.root / "audit" / "posts.jsonl"
        self.hosts = set()
        self.opener = opener or open_in_explorer
        self.log = make_logger(self.root / "server.log" if log_path == "default" else log_path)

    def close(self):
        for h in list(self.log.handlers):
            self.log.removeHandler(h)
            h.close()

    def audit_stamp(self):
        try:
            st = os.stat(str(self.audit_log))
            return "%d-%d" % (st.st_size, st.st_mtime_ns)
        except OSError:
            return "none"


def _quoted(value):
    v = (value or "").strip()
    if v.startswith("W/"):
        v = v[2:]
    return v.strip('"')


class Handler(BaseHTTPRequestHandler):
    app = None  # set by make_server
    server_version = "AgentPlaybook/" + lib.VERSION

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- plumbing

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = textio.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """What the standard library answers by itself (a malformed request line, an unknown method, headers too
        long) is JSON too, like every other answer, and closes the connection."""
        short = self.responses.get(code, ("error",))[0]
        self.close_connection = True
        self.send_response(code, short)
        self.send_header("Connection", "close")
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if code < 200 or code in (204, 304):
            self.end_headers()
            return
        body = textio.dumps({"error": message or short}).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":  # a HEAD answer carries the headers only
            self.wfile.write(body)

    def _dispatch(self, method):
        """Every request goes through here, so every failure is a JSON answer and a line in server.log."""
        try:
            self._guard(write=method != "GET")
            parts = [urllib.parse.unquote(x) for x in self.path.split("?")[0].split("/") if x]
            code, body, kw = getattr(self, "_" + method.lower())(parts)
            self._send(code, body, **kw)
        except HttpError as e:
            if e.code >= 500:
                self.app.log.error("%s %s -> %d %s", method, self.path, e.code, e.payload.get("error"))
            self._answer_error(e.code, e.payload)
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except socket.timeout:
            # a client that stopped sending mid-request: answer if it still listens, and free the thread
            self.app.log.info("%s %s: the client stalled for %g s; connection closed", method, self.path, self.timeout)
            self._body_read = True
            self.close_connection = True
            try:
                self._send(408, {"error": "request timed out"})
            except Exception:
                pass
        except Exception as e:  # never leave the page without an answer
            self.app.log.error("%s %s failed\n%s", method, self.path, traceback.format_exc())
            self._answer_error(500, {"error": "server error", "detail": "%s: %s" % (type(e).__name__, e)})

    def _answer_error(self, code, payload):
        try:
            self._drain()
            self._send(code, payload)
        except Exception:
            self.close_connection = True

    def _drain(self):
        """Read a request body nobody asked for, so the client gets the answer instead of a reset connection."""
        if getattr(self, "_body_read", False) or self.command == "GET":
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        left = min(max(n, 0), 8 * MAX_BODY)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)

    def _guard(self, write=False):
        if self.headers.get("Host") not in self.app.hosts:
            raise HttpError(403, "bad host")
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none") and self.path.startswith("/api"):
            raise HttpError(403, "cross-site requests are refused")
        given = (self.headers.get("X-Playbook-Token") or "").encode("utf-8", "replace")
        if write and not secrets.compare_digest(given, self.app.token.encode("utf-8")):
            raise HttpError(403, "bad token")

    def _body(self):
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "Content-Length is required")
        try:
            n = int(raw)
        except ValueError:
            n = -1
        if n < 0:
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            raise HttpError(413, "too large: the limit is %d bytes" % MAX_BODY)
        self._body_read = True
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise HttpError(400, "the request body was cut short")
        try:
            return textio.loads(data or b"{}")
        except ValueError as e:
            raise HttpError(400, "the body is %s" % e)

    def _reason(self):
        return urllib.parse.unquote(self.headers.get("X-Change-Reason") or "")

    def _base(self):
        """The revision the caller started from: If-Match "<rev>", or If-None-Match * for a new document."""
        match, none = self.headers.get("If-Match"), self.headers.get("If-None-Match")
        if match:
            return _quoted(match)
        if none and none.strip() == "*":
            return storelib.NEW
        raise HttpError(428, "send If-Match with the revision you started from, or If-None-Match: * for a new document")

    def _doc_target(self, parts):
        if len(parts) == 3 and parts[0] == "api" and parts[1] in COLS:
            why = ids.id_problem(parts[2], parts[1])
            if why:
                raise HttpError(400, "bad id", message=why)
            return parts[1], parts[2]
        raise HttpError(404, "not found")

    def _store_call(self, fn):
        try:
            return fn()
        except storelib.Invalid as e:
            raise HttpError(400, "invalid", message=str(e), findings=[{"file": f, "path": p, "message": m} for f, p, m in e.findings])
        except storelib.ReasonRequired as e:
            raise HttpError(409, "reason required", removed=e.removed)
        except storelib.Stale as e:
            raise HttpError(412, "stale", message=str(e), current=e.current, rev=e.rev)
        except storelib.NotFound as e:
            raise HttpError(404, "not found", message=str(e))
        except storelib.BadId as e:
            raise HttpError(400, "bad id", message=str(e))
        except textio.LockTimeout:
            raise HttpError(503, "busy", message="the store is locked by another process; try again")
        except ValueError as e:
            raise HttpError(400, "invalid", message=str(e))

    # ---------------------------------------------------------------- routes

    def _get(self, p):
        app = self.app
        if not p:
            try:
                html = textio.read_bytes(app.page_dir / "playbook.html").decode("utf-8")
            except OSError:
                raise HttpError(500, "playbook.html is missing: run python app/build_page.py")
            return 200, html.replace("__PB_TOKEN__", app.token), {"ctype": "text/html; charset=utf-8"}
        if p == ["favicon.ico"]:
            try:
                return 200, textio.read_bytes(app.page_dir / "playbook.ico"), {"ctype": "image/x-icon"}
            except OSError:
                raise HttpError(404, "not found")
        if p == ["api", "ping"]:
            return 200, {"ok": True, "app": config.APP, "version": lib.VERSION}, {}
        if p == ["api", "session"]:
            # the page asks again after a server restart; only this origin can read the answer
            return 200, {"token": app.token, "version": lib.VERSION}, {}
        if p == ["api", "all"]:
            data, revs, errors = app.store.load_all()
            invalid = {}
            for col in COLS:
                for doc_id, doc in data[col].items():
                    found = schema.validate(col, doc_id, doc)
                    if found:
                        invalid.setdefault(col, {})[doc_id] = ["%s: %s" % (pth, msg) for _, pth, msg in found[:5]]
            return 200, {"data": data, "revs": revs, "stamp": app.store.stamp(), "audit": app.audit_stamp(), "invalid": invalid,
                         "errors": [{"file": f, "path": pth, "message": m} for f, pth, m in errors], "version": lib.VERSION}, {}
        if p == ["api", "stamp"]:
            return 200, {"stamp": app.store.stamp(), "audit": app.audit_stamp()}, {}
        if p == ["api", "audit"]:
            items, problems = auditlog.read(app.audit_log)
            return 200, {"items": [auditlog.public(i) for i in items], "problems": problems, "audit": app.audit_stamp()}, {}
        if p == ["api", "changes"]:
            return 200, {"changes": history.read_changes(app.store)}, {}
        if len(p) in (4, 5) and p[:2] == ["api", "versions"] and p[2] in COLS and ids.valid_id(p[3], p[2]):
            if len(p) == 4:
                return 200, {"versions": history.versions(app.store, p[2], p[3])}, {}
            try:
                return 200, {"doc": schema.upgrade(p[2], history.read_version(app.store, p[2], p[3], p[4]))}, {}
            except LookupError:
                raise HttpError(404, "not found", message="no such version")
            except ValueError as e:
                raise HttpError(500, "that version cannot be read", message=str(e))
        raise HttpError(404, "not found")

    def _put(self, p):
        col, doc_id = self._doc_target(p)
        base = self._base()
        data = self._body()
        if not isinstance(data, dict):
            raise HttpError(400, "invalid", message="expected an object")
        r = self._store_call(lambda: self.app.store.write(col, doc_id, data, by="canvas", reason=self._reason(), base=base))
        return 200, {"ok": True, "rev": r["rev"], "changed": r["changed"], "stamp": self.app.store.stamp()}, {}

    def _delete(self, p):
        col, doc_id = self._doc_target(p)
        base = self._base()
        self._store_call(lambda: self.app.store.delete(col, doc_id, by="canvas", reason=self._reason(), base=base))
        return 200, {"ok": True, "stamp": self.app.store.stamp()}, {}

    def _post(self, p):
        if p != ["api", "open"]:
            raise HttpError(404, "not found")
        data = self._body()
        path = data.get("path") if isinstance(data, dict) else None
        why = path_problem(path)
        if why:
            raise HttpError(400, "bad path", message=why)
        # the same reading of a folder path as pb.py check: ~ expanded, a relative path taken from the skill folder
        if not self.app.opener(lint.folder_path(path, self.app.root.parent)):
            raise HttpError(404, "not found", message="that path does not exist on this machine")
        return 200, {"ok": True}, {}

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_POST(self):
        self._dispatch("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR would let a second server share the port; elsewhere it only allows a quick restart
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # nobody else may bind the port while the canvas has it, not even a program asking to share it
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        # HTTPServer.server_bind also looks the host name up (a reverse DNS query that can stall startup for
        # seconds on some networks); the address is always 127.0.0.1, so skip it
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        pass  # request errors are answered and logged by the dispatcher; a dropped socket is not news


def make_server(root=None, port=None, page_dir=None, token=None, log_path="default", opener=None, timeout=TIMEOUT):
    """A server for the playbook in `root`, bound to 127.0.0.1. Port 0 picks a free one (the tests do this).
    `timeout`: seconds a client may stall mid-request. Raises OSError when the port is taken."""
    app = App(root, page_dir, token, log_path, opener)
    handler = type("BoundHandler", (Handler,), {"app": app, "timeout": timeout})
    srv = Server(("127.0.0.1", config.port(port) if port != 0 else 0), handler)
    actual = srv.server_address[1]
    app.hosts = {"127.0.0.1:%d" % actual, "localhost:%d" % actual}
    srv.app = app
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="port to listen on (default %d, or $%s)" % (config.DEFAULT_PORT, config.PORT_ENV))
    ap.add_argument("--root", help=argparse.SUPPRESS)  # another playbook folder (the tests use a fixture)
    args = ap.parse_args(argv)
    try:
        port = config.port(args.port)
    except ValueError as e:
        ap.error(str(e))
    try:
        srv = make_server(args.root, port)
    except OSError as e:
        msg = "cannot listen on 127.0.0.1:%d (%s). Is the playbook already open, or is another program using the port?" % (port, e.strerror or e)
        make_logger(pathlib.Path(str(args.root or HERE)) / "server.log").error(msg)
        if sys.stderr:
            print(msg, file=sys.stderr)
        sys.exit(1)
    try:
        srv.app.store.dir.mkdir(exist_ok=True)
    except OSError as e:
        msg = "cannot use the playbook at %s (%s)" % (srv.app.root, e.strerror or e)
        srv.app.log.error(msg)
        if sys.stderr:
            print(msg, file=sys.stderr)
        srv.server_close()
        sys.exit(1)
    swept = srv.app.store.sweep_temp()
    if swept:
        srv.app.log.info("removed %d temp file(s) an interrupted write left behind", swept)

    def stop(signum, frame):  # shut down between requests: a save in progress finishes first
        threading.Thread(target=srv.shutdown, daemon=True).start()
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (OSError, ValueError):
                pass
    srv.app.log.info("listening on 127.0.0.1:%d, version %s, pid %d", port, lib.VERSION, os.getpid())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        srv.app.log.info("stopped")


if __name__ == "__main__":
    main()
