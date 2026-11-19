"""Offline tests for the agent-playbook helper scripts (no network, no browser, no clipboard).

Run: python -m unittest discover -s tests -v

Script subprocesses run with AGENT_PLAYBOOK_OFFLINE=1 (every request fails at once) and with the guard in
tests/fixtures/scripts/guard on PYTHONPATH, which records and blocks any network call. Parser tests feed
the scripts' own parse functions with the synthetic samples in tests/fixtures/scripts. The system clipboard
is only touched when AGENT_PLAYBOOK_TEST_CLIPBOARD=1.
"""
import ast
import contextlib
import datetime as dt
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error

ROOT =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "agent-playbook", "scripts")
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "scripts")
GUARD = os.path.join(FIXTURES, "guard")
NOW = dt.datetime(2026, 11, 9, 18, 0, tzinfo=dt.timezone.utc)  # the fixtures' "now"

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import common  # noqa: E402


def script_names():
    return sorted(n for n in os.listdir(SCRIPTS) if n.endswith(".py") and n != "common.py")


def run(script, *args, env=None, cwd=None, stdin=None, marker=None):
    e = dict(os.environ, PYTHONIOENCODING="utf-8", AGENT_PLAYBOOK_OFFLINE="1", PYTHONPATH=GUARD)
    for name in ("AGENT_PLAYBOOK_BANK", "AGENT_PLAYBOOK_GROUPS", "AGENT_PLAYBOOK_CHROME", "CHROME_PATH", "CHROME_BIN", "CODEX_BIN", "AGENT_PLAYBOOK_SHORTS_DIR"):
        e.pop(name, None)
    if marker:
        e["AGENT_PLAYBOOK_TEST_NET_MARKER"] = marker
    e.update(env or {})
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], input=stdin,
                          capture_output=True, text=True, encoding="utf-8", env=e, cwd=cwd, timeout=120)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def call(module, *argv, fetch=None, stdin=""):
    """Run a script's main() in this process. Returns (exit code, stdout, stderr). `fetch` replaces
    common.fetch (url -> bytes, or raise common.FetchError), so no request leaves the machine."""
    def refuse(url, timeout=20, headers=None):
        raise common.FetchError("blocked in tests: " + url)

    out, err = io.StringIO(), io.StringIO()
    saved = common.fetch, sys.argv, sys.stdin
    common.fetch, sys.argv, sys.stdin = fetch or refuse, [module.__name__ + ".py"], io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(list(argv))
            except SystemExit as e:
                code = e.code
    finally:
        common.fetch, sys.argv, sys.stdin = saved
    return code or 0, out.getvalue(), err.getvalue()


def serve(routes):
    """A fake common.fetch: the first (substring, fixture name or bytes or Exception) whose substring is in
    the URL answers. An unknown URL fails the request."""
    def fetch(url, timeout=20, headers=None):
        for part, reply in routes:
            if part in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply if isinstance(reply, bytes) else fixture(reply).encode("utf-8")
        raise common.FetchError("no fixture for " + url)
    return fetch


def executable(folder, name, python_script=None, exit_code=0):
    """A tiny program the scripts can launch in place of a real binary. With python_script it runs that
    script with this Python and the same arguments; otherwise it just exits with exit_code."""
    if os.name == "nt":
        path = os.path.join(folder, name + ".cmd")
        body = '@"%s" "%s" %%*\r\n' % (sys.executable, python_script) if python_script else "@exit /b %d\r\n" % exit_code
    else:
        path = os.path.join(folder, name)
        body = ('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, python_script) if python_script
                else "#!/bin/sh\nexit %d\n" % exit_code)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


# A program that starts a grandchild, writes the grandchild's pid to $TREE_PID_FILE, then hangs: what a codex
# under node, Chrome with its renderers, or an end-card script that runs Chrome looks like when it is stuck.
SPAWN_AND_HANG = r'''
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.environ["TREE_PID_FILE"], "w") as fh:
    fh.write(str(child.pid))
time.sleep(90)
'''


SPAWN_THEN_EXIT = r'''
import os, subprocess, sys, time
# the grandchild inherits stdout, so it holds the pipe the caller reads after this process has gone
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"], stdin=subprocess.DEVNULL)
with open(os.environ["TREE_PID_FILE"], "w") as fh:
    fh.write(str(child.pid))
time.sleep(2.5)
'''


def alive(pid):
    """True while the process runs (a zombie counts as ended)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return r.returncode == 0 and not r.stdout.strip().startswith("Z")


def ended(pid, seconds=15.0):
    """True once the process has ended, within `seconds`. A process still running then is killed, so a failed
    test never leaves one behind."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    return False


def read_pid(path, seconds=30.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("the stand-in never wrote its grandchild's pid to " + path)


class TestEveryScript(unittest.TestCase):
    def test_all_scripts_compile(self):
        import py_compile
        for name in os.listdir(SCRIPTS):
            if name.endswith(".py"):
                py_compile.compile(os.path.join(SCRIPTS, name), doraise=True)

    @unittest.skipUnless(sys.version_info >= (3, 8), "ast feature_version needs 3.8")
    def test_all_scripts_parse_as_python_3_8(self):
        for name in os.listdir(SCRIPTS):
            if name.endswith(".py"):
                with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
                    try:
                        ast.parse(fh.read(), name, feature_version=(3, 8))
                    except SyntaxError as e:
                        self.fail("%s is not Python 3.8 syntax: %s" % (name, e))

    def test_every_script_has_help(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            for name in script_names():
                with self.subTest(script=name):
                    r = run(name, "--help", cwd=d, marker=marker)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertTrue(r.stdout.startswith("usage: " + name), r.stdout[:200])
                    self.assertEqual(r.stderr, "")
                    self.assertFalse(os.path.exists(marker), "%s --help made a network call" % name)
                    self.assertEqual(os.listdir(d), [], "%s --help wrote files" % name)

    def test_help_works_in_isolated_mode(self):
        # python -I leaves the script's folder off sys.path; each script puts it back to import common
        for name in script_names():
            with self.subTest(script=name):
                r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, name), "--help"],
                                   capture_output=True, text=True, encoding="utf-8", timeout=120,
                                   env=dict(os.environ, AGENT_PLAYBOOK_OFFLINE="1"))
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_usage_errors_exit_2(self):
        cases = {
            "bank.py": [["nonsense"], ["x"], ["text"]],
            "bridge_wait.py": [["soon"], ["--bogus"]],
            "bsky_authors.py": [[], ["a.example", "--hours", "soon"], ["a.example", "--limit", "0"], ["a.example", "--hours", "0"]],
            "bsky_feed.py": [[], ["not-a-feed-uri"], ["at://did:plc:x/app.bsky.feed.generator/y", "--hours", "-1"]],
            "bsky_filter.py": [[]],
            "bsky_open.py": [[], ["owner.example", "many"], ["owner.example", "0"]],
            "captions.py": [[], ["cues.json", "out.ass", "wide", "1920"]],
            "clip.py": [[], ["--text"], ["file.txt", "--text", "both"]],
            "clipimage.py": [[]],
            "codex_img.py": [[], ["only a prompt"], ["p", "out.png", "--timeout", "soon"], ["  ", "out.png"]],
            "doctor.py": [["--bogus"]],
            "fb_snap.py": [[], ["snap.txt", "everything"]],
            "li_detail.py": [[], ["not-a-number"]],
            "li_search.py": [[], ["  "]],
            "loop_qc.py": [[], ["frames", "--fps"], ["frames", "--bg", "blue"], ["frames", "--fps", "0"]],
            "make_index.py": [["--bogus"]],
            "mklibanner.py": [[]],
            "pace.py": [["groups.tsv", "--mark"], ["a.tsv", "--groups", "b.tsv"], ["--groups"]],
            "pr_review.py": [[], ["prep"], ["prep", "repo", "abc"], ["prep", "repo", "0"], ["run", "c", "b"],
                             ["run", "c", "b", "o", "--timeout", "0"], ["bogus"]],
            "render_carousel.py": [[], ["--html", "x.html", "--name", "n", "--count", "two", "--out", "o"],
                                   ["--html", "x.html", "--name", "n", "--count", "0", "--out", "o"]],
            "render_short.py": [[], ["some-video-dir"]],
            "run.py": [[], ["start"], ["done", "start"], ["skip", "start"], ["abandon"], ["list", "-n", "many"]],
            "wait_cpu.py": [["high"], ["0"], ["75", "30", "0"]],
            "xintent.py": [[], ["123"], ["abc", "text"], ["0", "  "]],
            "xsynd.py": [[], ["12"], ["soon", "handle"], ["0", "handle"]],
            "xthread.py": [[], ["not-a-number"]],
        }
        self.assertEqual(sorted(cases), script_names(), "every script needs a usage-error case")
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            for name, arg_lists in sorted(cases.items()):
                for args in arg_lists:
                    with self.subTest(script=name, args=args):
                        r = run(name, *args, cwd=d, marker=marker)
                        self.assertEqual(r.returncode, 2, "%s %s\n%s%s" % (name, args, r.stdout, r.stderr))
                        self.assertNotIn("Traceback", r.stderr)
                        self.assertTrue(r.stderr.strip(), "a usage error says why")
            self.assertFalse(os.path.exists(marker), "a usage error made a network call")

    def test_missing_input_files_exit_1_without_traceback(self):
        cases = [("bank.py", ["--bank", "nope.tsv", "check"]), ("bank.py", ["--bank", "nope.tsv", "x", "key"]),
                 ("bank.py", ["check"], {"AGENT_PLAYBOOK_BANK": "nope.tsv"}), ("pace.py", ["nope.tsv"]),
                 ("pace.py", ["--groups", "nope.tsv"]), ("pace.py", [], {"AGENT_PLAYBOOK_GROUPS": "nope.tsv"}),
                 ("pace.py", ["nope.tsv", "--mark", "g"]),
                 ("pr_review.py", ["prep", "nope", "5"]), ("pr_review.py", ["run", "nope", "nope.md", "out.txt"]),
                 ("pr_review.py", ["done", "nope", "5"]),
                 ("fb_snap.py", ["nope.txt"]), ("clip.py", ["nope.txt"]), ("clipimage.py", ["nope.png"]),
                 ("captions.py", ["nope.json", "out.ass", "1080", "1920"]),
                 ("mklibanner.py", ["nope.html"]),
                 ("codex_img.py", ["a prompt", "out.png", "--ref", "nope.png"]),
                 ("render_carousel.py", ["--html", "nope.html", "--name", "n", "--count", "1", "--out", "out"]),
                 ("render_short.py", ["--shorts-dir", "nope", "video"])]
        with tempfile.TemporaryDirectory() as d:
            for name, args, *env in cases:
                with self.subTest(script=name, args=args):
                    r = run(name, *args, cwd=d, env=env[0] if env else None)
                    self.assertEqual(r.returncode, 1, "%s %s\n%s%s" % (name, args, r.stdout, r.stderr))
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertIn(name + ":", r.stderr)
            self.assertEqual(os.listdir(d), [], "a failed run left files behind")

    def test_network_scripts_exit_1_when_every_request_fails(self):
        cases = [("bsky_authors.py", ["a.example", "b.example"], "all 2 requests failed"),
                 ("bsky_feed.py", ["at://did:plc:x/app.bsky.feed.generator/y"], "all 1 request failed"),
                 ("bsky_filter.py", ["owner.example"], "can't resolve the owner handle"),
                 ("bsky_open.py", ["owner.example"], "can't read owner.example's feed"),
                 ("li_search.py", ["web developer"], "search failed"),
                 ("li_detail.py", ["4000000001", "4000000002"], "all 2 jobs failed"),
                 ("xsynd.py", ["12", "ExampleDev"], "all 1 account failed"),
                 ("xthread.py", ["20"], "all 1 post failed")]
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            for name, args, message in cases:
                with self.subTest(script=name):
                    r = run(name, *args, cwd=d, stdin="", marker=marker)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(message, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertEqual(r.stdout, "")
            self.assertFalse(os.path.exists(marker), "AGENT_PLAYBOOK_OFFLINE=1 still let a request out")
            self.assertEqual(os.listdir(d), [], "li_detail.py wrote details.json though every job failed")

    def test_no_user_or_scratchpad_paths_in_scripts(self):
        bad = re.compile(r"[A-Za-z]:[\\/]+Users|/Users/[A-Za-z]|/home/[a-z]|scratchpad|AppData[\\/]+Local[\\/]+Temp", re.I)
        for folder in (SCRIPTS, os.path.join(ROOT, "agent-playbook", "templates")):
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if not os.path.isfile(path) or not name.endswith((".py", ".html", ".json")):
                    continue
                with open(path, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        self.assertIsNone(bad.search(line), "%s:%d holds a machine path: %s" % (name, n, line.strip()[:120]))

    def test_index_is_current(self):
        r = run("make_index.py", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("make_index.py", "--dir", os.path.join(ROOT, "agent-playbook", "media"), "--check")  # the media tools' own index
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(SCRIPTS, "INDEX.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual([s["name"] for s in index["scripts"]], script_names())
        for s in index["scripts"]:
            self.assertTrue(s["purpose"] and s["usage"].startswith(s["name"]), s)
            self.assertTrue(set(s["side_effects"]) <= {"none", "clipboard", "network", "files", "browser"}, s)


class TestCommon(unittest.TestCase):
    def test_parse_time_reads_what_python_3_8_fromisoformat_cannot(self):
        utc = dt.timezone.utc
        cases = {"2026-11-09T10:00:00.000Z": dt.datetime(2026, 11, 9, 10, tzinfo=utc),
                 "2026-11-09T10:00:00Z": dt.datetime(2026, 11, 9, 10, tzinfo=utc),
                 "2026-11-09T10:00:00.1234567Z": dt.datetime(2026, 11, 9, 10, 0, 0, 123456, tzinfo=utc),
                 "2026-11-09T10:00:00.12+00:00": dt.datetime(2026, 11, 9, 10, 0, 0, 120000, tzinfo=utc),
                 "2026-11-09T10:00": dt.datetime(2026, 11, 9, 10, tzinfo=utc),  # no zone: read as UTC
                 "2026-11-09 02:00:00-0800": dt.datetime(2026, 11, 9, 10, tzinfo=utc),
                 "2026-11-09T12:00+02:00": dt.datetime(2026, 11, 9, 10, tzinfo=utc)}
        for text, want in cases.items():
            got = common.parse_time(text)
            self.assertIsNotNone(got.tzinfo, text)
            self.assertEqual(got, want, text)
        for text in ("", "yesterday", "2026-13-40T99:00", None):
            with self.assertRaises(ValueError):
                common.parse_time(text)

    def test_x_weighted_follows_x_rules(self):
        cases = [("hi there https://example.com/x", 32),            # 9 + a link is always 23
                 ("https://example.com/" + "a" * 200, 23),
                 ("plain ascii", 11), ("caf\u00e9 na\u00efve", 10),  # Latin letters with accents weigh 1
                 ("cafe\u0301", 4),                                  # counted after NFC normalisation
                 ("\u201cquoted\u201d \u2014 it\u2019s", 15),        # curly quotes and dashes weigh 1
                 ("\u65e5\u672c\u8a9e", 6),                          # CJK weighs 2
                 ("\ud55c\uae00", 4), ("\u1ebf", 2),                 # Hangul; U+1100-U+1FFF weighs 2
                 ("\u2026", 2), ("\u2192", 2), ("\u2713", 2),        # ellipsis, arrow, check mark weigh 2
                 ("\U0001F44D", 2), ("\U0001F44D\U0001F3FD", 2),     # an emoji is 2, with a skin tone too
                 ("\U0001F468\u200D\U0001F469\u200D\U0001F467", 2),  # a joined family is one emoji
                 ("\U0001F1FA\U0001F1F8", 2), ("1\ufe0f\u20e3", 2),  # a flag, a keycap
                 ("\u2764\ufe0f ok", 5), ("a" * 280, 280), ("\u65e5" * 141, 282)]
        for text, want in cases:
            self.assertEqual(common.x_weighted(text), want, ascii(text))

    def test_tsv_keeps_comments_and_pads_rows(self):
        text = "# head\tx\n\na\tb\n  # indented comment\nc\n"
        self.assertEqual(common.tsv_lines(text), [("other", "# head\tx"), ("other", ""), ("row", ["a", "b"]),
                                                  ("other", "  # indented comment"), ("row", ["c"])])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.tsv")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write("# c\r\na\tb\r\nc\r\n")
            self.assertEqual(common.read_tsv(path, 3), [["a", "b", ""], ["c", "", ""]])

    def test_handles_compare_without_case_or_at(self):
        self.assertTrue(common.same_handle("@Example.Dev", "example.dev "))
        self.assertFalse(common.same_handle("example.dev", "example.devs"))
        self.assertFalse(common.same_handle(None, "example.dev"))

    def test_fetch_refuses_when_offline(self):
        old = os.environ.get("AGENT_PLAYBOOK_OFFLINE")
        os.environ["AGENT_PLAYBOOK_OFFLINE"] = "1"
        try:
            with self.assertRaises(common.FetchError) as ctx:
                common.fetch("https://example.invalid/x")
            self.assertIn("offline", str(ctx.exception))
            with self.assertRaises(common.FetchError):
                common.bsky("app.bsky.actor.getProfile", actor="bsky.app")
        finally:
            if old is None:
                del os.environ["AGENT_PLAYBOOK_OFFLINE"]
            else:
                os.environ["AGENT_PLAYBOOK_OFFLINE"] = old

    def test_find_chrome_takes_the_given_path_or_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            fake = executable(d, "fakechrome")
            self.assertEqual(common.find_chrome(fake), fake)
            self.assertIsNone(common.find_chrome(os.path.join(d, "missing-chrome")))
            saved = {k: os.environ.pop(k, None) for k in common.CHROME_ENV}
            try:
                os.environ["AGENT_PLAYBOOK_CHROME"] = fake
                self.assertEqual(common.find_chrome(), fake)
                os.environ["AGENT_PLAYBOOK_CHROME"] = os.path.join(d, "missing-chrome")
                self.assertIsNone(common.find_chrome(), "a wrong $AGENT_PLAYBOOK_CHROME must not fall back silently")
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None)
                    if v is not None:
                        os.environ[k] = v

    def test_write_atomic_replaces_or_leaves_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.txt")
            common.write_atomic(path, "one\n")
            common.write_atomic(path, "two\n")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "two\n")
            real = common.os.replace
            common.os.replace = lambda a, b: (_ for _ in ()).throw(OSError("disk full"))
            try:
                with self.assertRaises(OSError):
                    common.write_atomic(path, "three\n")
            finally:
                common.os.replace = real
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "two\n")
            self.assertEqual(os.listdir(d), ["f.txt"])

    def test_reading_a_reply_that_changed_shape(self):
        self.assertEqual((common.as_dict(None), common.as_dict([1]), common.as_dict({"a": 1})), ({}, {}, {"a": 1}))
        self.assertEqual((common.as_list(None), common.as_list({"a": 1}), common.as_list([2])), ([], [], [2]))
        self.assertEqual((common.as_text(None), common.as_text(5), common.as_text("x")), ("", "", "x"))
        self.assertEqual([common.as_count(v) for v in (None, "7", True, 7, 7.9, float("nan"), float("inf"))], [0, 0, 0, 7, 7, 0, 0])


class FakeReply(io.BytesIO):
    """What urlopen returns: a body that works as a context manager."""


class TestFetchRetry(unittest.TestCase):
    """fetch() against a stand-in urlopen: nothing leaves the machine and nobody really waits."""

    def setUp(self):
        self.calls, self.waits = [], []
        saved = common.urllib.request.urlopen, common.time.sleep, os.environ.pop("AGENT_PLAYBOOK_OFFLINE", None)
        common.time.sleep = self.waits.append
        common._trouble[0] = 0

        def restore():
            common.urllib.request.urlopen, common.time.sleep = saved[0], saved[1]
            common._trouble[0] = 0
            if saved[2] is not None:
                os.environ["AGENT_PLAYBOOK_OFFLINE"] = saved[2]
        self.addCleanup(restore)

    def replies(self, *answers):
        """urlopen answers each call with the next item: bytes (the body) or an exception to raise."""
        answers = list(answers)

        def urlopen(request, timeout=None):
            self.calls.append(request.full_url)
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return FakeReply(answer)
        common.urllib.request.urlopen = urlopen

    @staticmethod
    def http(code, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return urllib.error.HTTPError("https://api.example/x", code, "Status %d" % code, headers, None)

    def test_a_server_error_or_a_rate_limit_is_tried_again(self):
        self.replies(self.http(503), b"ok")
        self.assertEqual(common.fetch("https://api.example/x"), b"ok")
        self.assertEqual((len(self.calls), self.waits), (2, [1.0]))
        self.waits.clear()
        self.replies(self.http(429, "5"), b"ok")  # the server's Retry-After wins over the shorter own wait
        self.assertEqual(common.fetch("https://api.example/x"), b"ok")
        self.assertEqual(self.waits, [5.0])

    def test_timeouts_and_dropped_connections_are_tried_three_times_in_all(self):
        import socket
        self.replies(urllib.error.URLError(socket.timeout("timed out")), ConnectionResetError("reset"),
                     urllib.error.URLError(socket.timeout("timed out")))
        with self.assertRaises(common.FetchError) as ctx:
            common.fetch("https://api.example/x")
        self.assertEqual((len(self.calls), self.waits), (3, [1.0, 3.0]))
        self.assertIn("timed out: https://api.example/x", str(ctx.exception))

    def test_what_another_try_cannot_fix_fails_at_once(self):
        import socket
        for error, message in ((self.http(404), "HTTP 404 Status 404: https://api.example/x"),
                               (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "getaddrinfo failed"),
                               (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "refused"),
                               (ValueError("unknown url type: 'nope'"), "unknown url type")):
            with self.subTest(error=repr(error)):
                self.calls.clear()
                self.replies(error)
                with self.assertRaises(common.FetchError) as ctx:
                    common.fetch("https://api.example/x")
                self.assertIn(message, str(ctx.exception))
                self.assertEqual((len(self.calls), self.waits), (1, []))

    def test_a_long_retry_after_is_reported_not_waited_out(self):
        self.replies(self.http(429, "3600"))
        with self.assertRaises(common.FetchError) as ctx:
            common.fetch("https://api.example/x")
        self.assertEqual((len(self.calls), self.waits), (1, []))
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertIn("asks to wait 3600 s", str(ctx.exception))

    def test_a_service_that_keeps_failing_is_tried_once_per_request(self):
        down = [self.http(503)] * (3 * common.TROUBLE_LIMIT)
        self.replies(*down + [self.http(503), b"back", self.http(503), b"ok"])
        for _ in range(common.TROUBLE_LIMIT):
            with self.assertRaises(common.FetchError):
                common.fetch("https://api.example/x")
        self.assertEqual(len(self.calls), 3 * common.TROUBLE_LIMIT)
        self.calls.clear()
        with self.assertRaises(common.FetchError):
            common.fetch("https://api.example/x")
        self.assertEqual(len(self.calls), 1, "after %d failed requests in a row a request gets one try" % common.TROUBLE_LIMIT)
        self.assertEqual(common.fetch("https://api.example/x"), b"back")
        self.calls.clear()
        self.assertEqual(common.fetch("https://api.example/x"), b"ok")  # a success: back to three tries
        self.assertEqual(len(self.calls), 2)

    def test_the_user_agent_is_generic(self):
        seen = []

        def urlopen(request, timeout=None):
            seen.append(request.get_header("User-agent"))
            return FakeReply(b"{}")
        common.urllib.request.urlopen = urlopen
        common.fetch_json("https://api.example/x")
        self.assertEqual(seen, ["Mozilla/5.0"])


class TestProcessTree(unittest.TestCase):
    def test_a_timeout_ends_the_child_and_what_it_started(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "hang.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(SPAWN_AND_HANG)
            pid_file = os.path.join(d, "pid.txt")
            # through a launcher script, as codex runs through node and the tests' stand-ins through a .cmd
            launcher = executable(d, "launcher", python_script=script)
            t0 = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                common.run_tree([launcher], timeout=3, env=dict(os.environ, TREE_PID_FILE=pid_file),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertLess(time.time() - t0, 30, "run_tree waited on pipes the grandchild held")
            self.assertTrue(ended(read_pid(pid_file)), "the grandchild outlived the timeout")

    def test_what_outlives_its_parent_is_ended_at_the_timeout(self):
        """The child exits (node crashing under codex) while what it started runs on, holding the pipes."""
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "spawn.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(SPAWN_THEN_EXIT)
            pid_file = os.path.join(d, "pid.txt")
            t0 = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                common.run_tree([sys.executable, script], timeout=6, env=dict(os.environ, TREE_PID_FILE=pid_file),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertLess(time.time() - t0, 30)
            self.assertTrue(ended(read_pid(pid_file)), "the orphaned grandchild outlived the timeout")

    def test_run_tree_returns_like_subprocess_run(self):
        r = common.run_tree([sys.executable, "-c", "import sys; print(sys.stdin.read().upper()); sys.exit(3)"],
                            input="hi", stdout=subprocess.PIPE, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (3, "HI"))


class TestPace(unittest.TestCase):
    def write(self, rows):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# slug\tname\tmode\tmin_hours\tlast_post_utc\n")
            for r in rows:
                fh.write("\t".join(r) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def ago(self, hours):
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat(timespec="minutes")

    def state(self, out, slug):
        return next(l for l in out.splitlines() if " %s " % slug in l).split()[0]

    def test_approval_group_waits_six_hours(self):
        p = self.write([["appr", "Approval", "approval", "", self.ago(2)]])
        out = run("pace.py", p).stdout
        self.assertIn("wait", out)

    def test_approval_group_ready_after_window(self):
        p = self.write([["appr", "Approval", "approval", "", self.ago(7)]])
        self.assertIn("READY", run("pace.py", p).stdout)

    def test_approval_groups_pace_independently(self):
        # Approval is per group: a fresh post in a1 must not hold back a2 or a3.
        p = self.write([["a1", "A1", "approval", "", self.ago(1)],
                        ["a2", "A2", "approval", "", ""],
                        ["a3", "A3", "approval", "", self.ago(7)]])
        lines = run("pace.py", p).stdout.splitlines()
        self.assertTrue(next(l for l in lines if " a1 " in l).startswith("wait"))
        for slug in ("a2", "a3"):
            line = next(l for l in lines if " %s " % slug in l)
            self.assertTrue(line.startswith("READY"), line)
            self.assertNotIn("another approval group", line)

    def test_own_group_short_cooldown_and_skip(self):
        p = self.write([["mine", "Mine", "own", "", self.ago(1.5)],
                        ["stuck", "Stuck", "skip", "", ""]])
        lines = run("pace.py", p).stdout.splitlines()
        self.assertTrue(next(l for l in lines if " mine " in l).startswith("READY"))
        self.assertTrue(next(l for l in lines if " stuck " in l).startswith("skip"))

    def test_each_mode_has_its_own_clock(self):
        # own 1 h, open and strict 4 h, approval 6 h; min_hours overrides the mode's default
        p = self.write([["own-early", "", "own", "", self.ago(0.5)], ["own-late", "", "own", "", self.ago(1.2)],
                        ["open-early", "", "open", "", self.ago(3.5)], ["open-late", "", "open", "", self.ago(4.5)],
                        ["strict-early", "", "strict", "", self.ago(3.5)], ["blank-mode", "", "", "", self.ago(4.5)],
                        ["appr-early", "", "approval", "", self.ago(5.5)], ["appr-late", "", "approval", "", self.ago(6.5)],
                        ["custom", "", "open", "12", self.ago(8)], ["never-posted", "", "approval", "", ""]])
        r = run("pace.py", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        want = {"own-early": "wait", "own-late": "READY", "open-early": "wait", "open-late": "READY",
                "strict-early": "wait", "blank-mode": "READY", "appr-early": "wait", "appr-late": "READY",
                "custom": "wait", "never-posted": "READY"}
        self.assertEqual({slug: self.state(r.stdout, slug) for slug in want}, want)

    def test_timestamp_without_a_zone_is_utc(self):
        naive = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        zulu = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = self.write([["naive", "N", "open", "", naive], ["zulu", "Z", "approval", "", zulu]])
        r = run("pace.py", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state(r.stdout, "naive"), "wait")
        self.assertIn("2.0h since last", r.stdout)
        self.assertEqual(self.state(r.stdout, "zulu"), "READY")

    def test_bad_rows_wait_and_fail_loudly(self):
        p = self.write([["good", "G", "open", "", ""], ["badtime", "B", "open", "", "last tuesday"],
                        ["badmode", "M", "aproval", "", ""], ["badhours", "H", "open", "six", ""]])
        r = run("pace.py", p)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("3 rows", r.stderr)
        self.assertEqual(self.state(r.stdout, "good"), "READY")
        for slug in ("badtime", "badmode", "badhours"):
            self.assertEqual(self.state(r.stdout, slug), "wait")

    def test_mark_records_time(self):
        p = self.write([["g", "G", "open", "", ""]])
        self.assertIn("marked g", run("pace.py", p, "--mark", "g").stdout)
        self.assertIn("wait", run("pace.py", p).stdout)

    def test_mark_keeps_comments_and_other_rows_and_leaves_no_temp_file(self):
        p = self.write([["a", "A", "approval", "", "2026-01-01T00:00+00:00"], ["b", "B", "own"]])
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\n# a trailing note\n")
        r = run("pace.py", p, "--mark", "b")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "# slug\tname\tmode\tmin_hours\tlast_post_utc")
        self.assertEqual(lines[1], "a\tA\tapproval\t\t2026-01-01T00:00+00:00")
        self.assertRegex(lines[2], r"^b\tB\town\t\t\d{4}-\d\d-\d\dT\d\d:\d\d\+00:00$")
        self.assertEqual(lines[3:], ["", "# a trailing note"])
        folder, name = os.path.split(p)
        self.assertEqual([n for n in os.listdir(folder) if n.startswith(name) and n != name], [])

    def test_mark_is_atomic(self):
        import pace
        p = self.write([["g", "G", "open", "", ""]])
        with open(p, encoding="utf-8") as fh:
            before = fh.read()
        real = common.os.replace
        common.os.replace = lambda a, b: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        try:
            code, out, err = call(pace, p, "--mark", "g")
        finally:
            common.os.replace = real
        self.assertEqual(code, 1)
        self.assertIn("can't write", err)
        self.assertNotIn("marked", out)
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        folder, name = os.path.split(p)
        self.assertEqual([n for n in os.listdir(folder) if n.startswith(name) and n != name], [])

    def test_groups_file_from_flag_environment_or_default(self):
        p = self.write([["g", "G", "own", "", ""]])
        self.assertEqual(self.state(run("pace.py", "--groups", p).stdout, "g"), "READY")
        self.assertEqual(self.state(run("pace.py", env={"AGENT_PLAYBOOK_GROUPS": p}).stdout, "g"), "READY")
        other = self.write([["h", "H", "skip", "", ""]])
        out = run("pace.py", other, env={"AGENT_PLAYBOOK_GROUPS": p}).stdout  # the file named on the command line wins
        self.assertEqual(self.state(out, "h"), "skip")
        self.assertIn("marked g", run("pace.py", "--mark", "g", env={"AGENT_PLAYBOOK_GROUPS": p}).stdout)
        self.assertEqual(self.state(run("pace.py", "--groups", p).stdout, "g"), "wait")
        default = common.state_file("groups.tsv", None, "AGENT_PLAYBOOK_GROUPS")
        self.assertEqual(default, os.path.join(ROOT, "agent-playbook", "playbook", "state", "groups.tsv"))
        self.assertTrue(os.path.isfile(default), "the skill ships the groups file (it may hold only its header)")

    def test_mark_needs_a_slug_and_a_known_one(self):
        p = self.write([["g", "G", "open", "", ""]])
        with open(p, encoding="utf-8") as fh:
            before = fh.read()
        r = run("pace.py", p, "--mark")  # used to fall through and list the groups, recording nothing
        self.assertEqual(r.returncode, 2)
        r = run("pace.py", p, "--mark", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("slug not found", r.stderr)
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)


class TestBank(unittest.TestCase):
    def bank(self, d, name="bank.tsv"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("# comment\nshort\tHello world\thttps://example.com\nlong\t" + "x" * 290 + "\t\n"
                     "quotes\t" + "\u201cso\u201d \u2014 " * 40 + "\t\n")
        return os.path.join(d, name)

    def test_check_and_urls(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"AGENT_PLAYBOOK_BANK": self.bank(d)}
            out = run("bank.py", "check", env=env).stdout
            self.assertRegex(out, r"long\s+x290.*X!")
            self.assertNotIn("X!", [l for l in out.splitlines() if l.startswith("short")][0])
            # 40 x 7 characters: 280 by X's rules (curly quotes and dashes weigh 1); the old rule said 400
            self.assertRegex(out, r"quotes\s+x280 b280\s*$")
            url = run("bank.py", "b", "short", env=env).stdout.strip()
            self.assertTrue(url.startswith("https://bsky.app/intent/compose?text=Hello%20world"))
            self.assertIn("example.com", url)
            self.assertNotIn("example.com", run("bank.py", "x", "short", env=env).stdout)
            self.assertEqual(run("bank.py", env=env).stdout, out, "no command means check")

    def test_text_and_plus(self):
        with tempfile.TemporaryDirectory() as d:
            bank = self.bank(d)
            self.assertEqual(run("bank.py", "--bank", bank, "text", "short").stdout, "Hello world\n")
            self.assertEqual(run("bank.py", "text", "short", "+", "--bank", bank).stdout, "Hello world\n\nhttps://example.com\n")

    def test_bank_from_flag_or_environment(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.bank(d, "other.tsv")
            self.assertEqual(run("bank.py", "--bank", path, "text", "short").stdout, "Hello world\n")
            self.assertEqual(run("bank.py", "text", "short", env={"AGENT_PLAYBOOK_BANK": path}).stdout, "Hello world\n")
            other = os.path.join(d, "flag-wins.tsv")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write("short\tFrom the flag\t\n")
            r = run("bank.py", "--bank", other, "text", "short", env={"AGENT_PLAYBOOK_BANK": path})
            self.assertEqual(r.stdout, "From the flag\n", "--bank wins over the environment")

    def test_default_bank_is_in_the_playbook_state_folder(self):
        default = common.state_file("bank.tsv", None, "AGENT_PLAYBOOK_BANK")
        self.assertEqual(default, os.path.join(ROOT, "agent-playbook", "playbook", "state", "bank.tsv"))
        self.assertTrue(os.path.isfile(default), "the skill ships the bank file (it may hold only its header)")
        r = run("bank.py", "text", "no-such-key-in-any-bank")
        self.assertEqual(r.returncode, 1)
        self.assertIn(os.path.join("playbook", "state", "bank.tsv"), r.stderr)

    def test_unknown_key_is_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            bank = self.bank(d)
            for cmd in ("x", "b", "text"):
                r = run("bank.py", cmd, "nope", "--bank", bank)
                self.assertEqual(r.returncode, 1)
                self.assertIn("no key 'nope'", r.stderr)
                self.assertIn("short", r.stderr)  # it lists the keys that exist
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(r.stdout, "")

    def test_importing_does_not_read_the_bank(self):
        import bank  # used to open bank.tsv at import and fail without one
        self.assertTrue(callable(bank.main))


class TestXIntent(unittest.TestCase):
    def test_reply_and_new(self):
        r = run("xintent.py", "123", "hi there https://example.com/x")
        self.assertIn("in_reply_to=123", r.stdout)
        self.assertIn("weighted length 32/280", r.stderr)  # 9 chars + 23 for the link
        self.assertTrue(run("xintent.py", "0", "hi").stdout.startswith("https://x.com/intent/post?text=hi"))

    def test_over_length_still_prints_the_url_but_exits_1(self):
        r = run("xintent.py", "0", "\u65e5" * 141)
        self.assertEqual(r.returncode, 1)
        self.assertIn("weighted length 282/280 OVER by 2", r.stderr)
        self.assertTrue(r.stdout.startswith("https://x.com/intent/post?text="))
        r = run("xintent.py", "0", "\u201cso\u201d \u2014 " * 40)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("weighted length 280/280", r.stderr)


class TestClip(unittest.TestCase):
    def test_bad_line_numbers_are_usage_errors(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("one\ntwo\n")
            for n in ("0", "3", "-1", "two", "1.5"):
                r = run("clip.py", path, n)
                self.assertEqual(r.returncode, 2, "line %s: %s" % (n, r.stderr))
                self.assertIn("LINE must be a number from 1 to 2", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_pick_text(self):
        import clip
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("one\ntwo\n")
            ap = clip.build_parser()
            self.assertEqual(clip.pick_text(ap.parse_args([path]), ap), "one\ntwo\n")
            self.assertEqual(clip.pick_text(ap.parse_args([path, "2"]), ap), "two")
            self.assertEqual(clip.pick_text(ap.parse_args(["--text", "literal"]), ap), "literal")
            self.assertEqual(clip.pick_text(ap.parse_args(["--text", ""]), ap), "")

    @unittest.skipIf(os.name == "nt", "wl-copy and xclip are for Linux (the Windows clipboard is Win32 calls)")
    def test_a_copy_tool_that_forks_a_server_does_not_hang(self):
        # wl-copy and xclip fork a server that keeps serving the clipboard and inherits stdout and stderr.
        # Waiting for those pipes to close waited until another program took the clipboard: forever, often.
        import clip
        import clipimage
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "fake_copy.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("import os, subprocess, sys\n"
                         "data = sys.stdin.buffer.read()\n"
                         "server = subprocess.Popen(['sleep', '60'])  # keeps this process's stdout and stderr\n"
                         "open(os.environ['SERVER_PID'], 'a').write('%d\\n' % server.pid)\n"
                         "open(os.environ['CLIP_FILE'], 'wb').write(data)\n")
            fake = executable(d, "wl-copy", python_script=script)
            pids, copied = os.path.join(d, "pids.txt"), os.path.join(d, "clip.bin")
            saved = dict(os.environ)
            os.environ.update(SERVER_PID=pids, CLIP_FILE=copied)
            try:
                t0 = time.time()
                clip._tool([fake], "agent-playbook ✓".encode("utf-8"))
                self.assertTrue(clipimage.run_tool([fake], b"\x89PNG image bytes"))
                self.assertLess(time.time() - t0, 20, "a copy tool's forked server held the run open")
                with open(copied, "rb") as fh:
                    self.assertEqual(fh.read(), b"\x89PNG image bytes")
            finally:
                os.environ.clear()
                os.environ.update(saved)
                with open(pids, encoding="utf-8") as fh:
                    for pid in fh.read().split():
                        ended(int(pid), 0)

    def test_a_clipboard_tool_that_hangs_is_an_error_not_a_wait(self):
        import clip
        real = clip.TOOL_TIMEOUT
        clip.TOOL_TIMEOUT = 1
        try:
            with self.assertRaises(OSError) as ctx:
                clip._tool([sys.executable, "-c", "import time; time.sleep(30)"], read=True)
            self.assertIn("did not finish in 1 s", str(ctx.exception))
            with self.assertRaises(OSError) as ctx:
                clip._tool([os.path.join(tempfile.gettempdir(), "no-such-clipboard-tool")])
            self.assertIn("is not installed", str(ctx.exception))
        finally:
            clip.TOOL_TIMEOUT = real

    @unittest.skipUnless(os.environ.get("AGENT_PLAYBOOK_TEST_CLIPBOARD") == "1", "set AGENT_PLAYBOOK_TEST_CLIPBOARD=1 to touch the clipboard")
    def test_round_trip_on_the_real_clipboard(self):
        import clip
        before = clip.get_clip()
        try:
            r = run("clip.py", "--text", "agent-playbook test \u2713")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("ok=True :: agent-playbook test"))
            self.assertEqual(clip.get_clip().rstrip(), "agent-playbook test \u2713")
        finally:
            clip.set_clip(before)


class TestFbSnap(unittest.TestCase):
    SNAP = "\n".join([
        '- article "Comment by Ada Lovelace 2 hours ago" [ref=e10]',
        '  - button "Reply" [ref=e11]',
        '  - button "View 2 replies" [ref=e12]',
        '- article "Reply by Alan Turing to Ada Lovelace\'s comment 1 hour ago" [ref=e20]',
        '  - button "Reply" [ref=e21]',
    ])

    def save(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.remove, fh.name)
        return fh.name

    def test_maps_reply_refs(self):
        out = run("fb_snap.py", self.save(self.SNAP)).stdout
        self.assertIn("REPLY e11 Comment by Ada Lovelace", out)
        self.assertIn("VIEW  e12 View 2 replies", out)
        self.assertIn("REPLY e21 Reply by Alan Turing", out)

    def test_modes_and_the_json_wrapped_snapshot(self):
        path = self.save(json.dumps([{"type": "text", "text": self.SNAP}]))
        self.assertEqual(run("fb_snap.py", path, "view").stdout, "VIEW  e12 View 2 replies\n")
        self.assertEqual(len(run("fb_snap.py", path, "reply").stdout.splitlines()), 2)

    def test_a_snapshot_with_no_refs_says_so(self):
        r = run("fb_snap.py", self.save("- heading \"Nothing here\" [ref=e1]\n"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("no Reply or View refs", r.stderr)


class TestLinkedInParsers(unittest.TestCase):
    def test_search_cards(self):
        import li_search
        seen = set()
        rows = li_search.parse_cards(fixture("li_search_page.html"), seen)
        self.assertEqual(rows, [
            ("4000000001", "Web Developer (React & Node)", "Example Labs", "United States", "2026-11-08", "$50.00/hr - $65.00/hr"),
            ("4000000002", "Contract Front-End Engineer", "Sample & Co", "Remote", "2026-11-05", "")])
        self.assertEqual(li_search.parse_cards(fixture("li_search_page.html"), seen), [], "ids already seen are left out")
        self.assertEqual(li_search.parse_cards(fixture("li_wall.html")), [])

    def test_search_url_defaults_and_extras(self):
        import li_search
        url = li_search.search_url("web developer", "", 25)
        for part in ("keywords=web+developer", "f_WT=2", "f_AL=true", "f_TPR=r604800", "start=25", "geoId=103644278"):
            self.assertIn(part, url)
        url = li_search.search_url("x", "f_JT=C&f_TPR=r86400", 0)
        self.assertTrue(url.endswith("&f_JT=C&f_TPR=r86400"))
        self.assertNotIn("r604800", url)

    def test_search_main_prints_rows_and_stops_at_an_empty_page(self):
        import li_search
        asked = []

        def fetch(url, timeout=20, headers=None):
            asked.append(url)
            return (fixture("li_search_page.html") if "start=0" in url else "<html></html>").encode("utf-8")
        real = li_search.time.sleep
        li_search.time.sleep = lambda s: None
        try:
            code, out, err = call(li_search, "web developer", "f_JT=C", fetch=fetch)
        finally:
            li_search.time.sleep = real
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "4000000001 | Web Developer (React & Node) | Example Labs | United States | 2026-11-08 | $50.00/hr - $65.00/hr")
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(len(asked), 2)

    def test_search_main_fails_loudly_on_a_login_wall(self):
        # a login page is not "no jobs found": exit 1, so an agent doesn't report an empty market
        import li_search
        real = li_search.time.sleep
        li_search.time.sleep = lambda s: None
        try:
            code, out, err = call(li_search, "web developer", fetch=serve([("search?", "li_wall.html")]))
            self.assertEqual((code, out), (1, ""))
            self.assertIn("search failed: LinkedIn answered with a login page", err)

            def later_wall(url, timeout=20, headers=None):
                return fixture("li_search_page.html" if "start=0" in url else "li_wall.html").encode("utf-8")
            code, out, err = call(li_search, "web developer", fetch=later_wall)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(out.splitlines()), 2)
            self.assertIn("stopped after 2 jobs: LinkedIn answered with a login page", err)
            code, out, err = call(li_search, "web developer", fetch=serve([("search?", b"")]))
            self.assertEqual((code, out), (0, ""))  # an empty reply is LinkedIn's "no more results"
            self.assertIn("no jobs found", err)
        finally:
            li_search.time.sleep = real
        self.assertTrue(li_search.login_wall(fixture("li_wall.html")))
        self.assertFalse(li_search.login_wall(fixture("li_search_page.html")))

    def test_job_details(self):
        import li_detail
        job = li_detail.parse_job(fixture("li_job.html"), "4000000001")
        self.assertEqual(job["title"], "Web Developer (React & Node)")
        self.assertEqual((job["co"], job["loc"], job["applicants"]), ("Example Labs", "United States", "Over 200 applicants"))
        self.assertEqual(job["criteria"], {"Seniority level": "Mid-Senior level", "Employment type": "Contract"})
        self.assertEqual(job["description"], "We're hiring a fully remote web developer. React & Node 3+ years")
        self.assertEqual(li_detail.summary(job), "4000000001 | Web Developer (React & Node) | Example Labs | United States | "
                         "Contract | Mid-Senior level | Over 200 applicants | remote-in-description")
        with self.assertRaises(ValueError):
            li_detail.parse_job(fixture("li_wall.html"), "4000000009")  # a login wall is not an empty job

    def test_detail_main_writes_out_and_reports_the_job_that_failed(self):
        import li_detail
        fetch = serve([("jobPosting/4000000001", "li_job.html"), ("jobPosting/4000000009", "li_wall.html")])
        real = li_detail.time.sleep
        li_detail.time.sleep = lambda s: None
        try:
            with tempfile.TemporaryDirectory() as d:
                out_file = os.path.join(d, "jobs.json")
                code, out, err = call(li_detail, "4000000001", "4000000009", "4000000404", "--out", out_file, fetch=fetch)
                self.assertEqual(code, 0, err)
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn("ERR 4000000009: no job in the reply", err)
                self.assertIn("ERR 4000000404:", err)
                with open(out_file, encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.assertEqual([j["id"] for j in saved], ["4000000001"])
                with open(out_file, "w", encoding="utf-8") as fh:
                    fh.write("keep me")
                code, out, err = call(li_detail, "4000000009", "--out", out_file, fetch=fetch)
                self.assertEqual(code, 1)
                self.assertIn("all 1 job failed", err)
                with open(out_file, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "keep me", "a run where every job failed must not overwrite the file")
        finally:
            li_detail.time.sleep = real


class TestXParsers(unittest.TestCase):
    def test_timeline_rows(self):
        import xsynd
        entries = xsynd.timeline_entries(fixture("x_timeline.html"))
        self.assertEqual(len(entries), 6)
        rows = xsynd.rows_from_entries(entries, "exampledev", 12, NOW)  # the handle matches without case
        self.assertEqual([r[2] for r in rows], ["1900000000000000001"], "replies, reposts, other accounts and old posts are left out")
        fav, name, i, age, replies, followers, text = rows[0]
        self.assertEqual((fav, replies, followers, round(age, 1)), (120, 9, 12000, 6.0))
        self.assertEqual(text, "Shipping the new build today. Notes in the thread.")
        self.assertEqual(len(xsynd.rows_from_entries(entries, "ExampleDev", 100, NOW)), 2)
        for page in ("<html>rate limited</html>", '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'):
            with self.assertRaises(ValueError):
                xsynd.timeline_entries(page)

    def test_timeline_main_one_account_fails(self):
        import xsynd
        fetch = serve([("screen-name/ExampleDev", "x_timeline.html"), ("screen-name/Limited", b"<html>rate limited</html>")])
        code, out, err = call(xsynd, "1000000", "@ExampleDev", "Limited", "Gone", fetch=fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertTrue(out.startswith("ExampleDev 1900000000000000005 | "), out)  # most favourites first
        self.assertIn("ERR Limited: no timeline data", err)
        self.assertIn("ERR Gone:", err)
        code, out, err = call(xsynd, "12", "Limited", fetch=fetch)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("all 1 account failed", err)

    def test_timeline_entries_with_null_fields_do_not_crash_the_sort(self):
        import xsynd
        entries = [None, {"content": None}, {"content": {"tweet": None}},
                   {"content": {"tweet": {"id_str": "1", "created_at": "Mon Nov 09 12:00:00 +0000 2026", "full_text": None,
                                          "favorite_count": None, "reply_count": None, "user": {"screen_name": "ExampleDev"}}}},
                   {"content": {"tweet": {"id_str": "2", "created_at": "Mon Nov 09 13:00:00 +0000 2026", "text": "hi",
                                          "favorite_count": 5, "user": None}}},
                   {"content": {"tweet": {"id_str": "3", "created_at": None, "user": {"screen_name": "ExampleDev"}}}},
                   {"content": {"tweet": {"id_str": "4", "created_at": "Mon Nov 09 14:00:00 +0000 2026", "text": "two",
                                          "favorite_count": 2, "user": {"screen_name": "exampledev", "followers_count": None}}}}]
        rows = sorted(xsynd.rows_from_entries(entries, "ExampleDev", 12, NOW), reverse=True)
        self.assertEqual([(r[0], r[2], r[4], r[5], r[6]) for r in rows], [(2, "4", 0, 0, "two"), (0, "1", 0, 0, "")])
        with self.assertRaises(ValueError):
            xsynd.timeline_entries('<script id="__NEXT_DATA__" type="application/json">'
                                   '{"props": {"pageProps": {"timeline": {"entries": {"a": 1}}}}}</script>')

    def test_thread(self):
        import xthread
        self.assertEqual(xthread.describe("1", json.loads(fixture("x_tweet.json"))),
                         ["== 1 @ExampleDev: Shipping the new build today."])
        self.assertEqual(xthread.describe("10", json.loads(fixture("x_tweet_reply.json"))),
                         ["== 10 @CuriousUser: @ExampleDev does it run on Linux?",
                          "   in reply to @ExampleDev: Shipping the new build today."])
        reply = json.loads(fixture("x_tweet_reply.json"))
        del reply["parent"]
        self.assertEqual(xthread.describe("10", reply)[1], "   in reply to @ExampleDev (1900000000000000001)")
        for empty in (json.loads(fixture("x_tombstone.json")), {}, []):
            with self.assertRaises(ValueError):
                xthread.describe("5", empty)

    def test_thread_main_reports_a_deleted_post(self):
        import xthread
        fetch = serve([("id=1900000000000000001&", "x_tweet.json"), ("id=5&", "x_tombstone.json")])
        code, out, err = call(xthread, "1900000000000000001", "5", fetch=fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "== 1900000000000000001 @ExampleDev: Shipping the new build today.\n")
        self.assertIn("ERR 5: no post in the reply (TweetTombstone)", err)
        code, out, err = call(xthread, "5", fetch=fetch)
        self.assertEqual(code, 1)


class TestBlueskyParsers(unittest.TestCase):
    def test_author_feed(self):
        import bsky_authors
        data = json.loads(fixture("bsky_author_feed.json"))
        rows = bsky_authors.rows_from_feed(data, "example.dev", NOW)
        # the feed spells the handle "Example.Dev" on one post: it still matches; the repost and the old post don't count
        self.assertEqual([(r[2], round(r[3], 1), r[4], r[5]) for r in rows], [("3aaa111", 8.0, 4, 10)])
        self.assertEqual(rows[0][0], 10 + 3 * 4)
        self.assertEqual(rows[0][6], "Fresh post with two lines")
        self.assertEqual([r[2] for r in bsky_authors.rows_from_feed(data, "EXAMPLE.DEV", NOW, 16)], ["3aaa111", "3aaa222"])
        self.assertEqual(len(bsky_authors.rows_from_feed(data, "example.dev", NOW, 100)), 3)
        self.assertEqual(bsky_authors.rows_from_feed(data, "someone.else", NOW), [])

    def test_authors_main(self):
        import bsky_authors
        fetch = serve([("actor=example.dev", "bsky_author_feed.json")])
        code, out, err = call(bsky_authors, "@Example.Dev", "missing.example", "--hours", "1000000", fetch=fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertTrue(out.startswith("example.dev 3aaa333 | "), out)  # 500 likes ranks first
        self.assertIn("ERR missing.example:", err)

    def test_custom_feed(self):
        import bsky_feed
        rows = bsky_feed.rows_from_feed(json.loads(fixture("bsky_custom_feed.json")), NOW)
        self.assertEqual({(r[2], r[3]) for r in rows}, {("maker.example", "3ccc111")}, "replies and old posts are left out")
        self.assertEqual(rows[0][0], 7 + 3 * 2)
        code, out, err = call(bsky_feed, "at://did:plc:f/app.bsky.feed.generator/one", "at://did:plc:f/app.bsky.feed.generator/two",
                              "--hours", "1000000", fetch=serve([("getFeed", "bsky_custom_feed.json")]))
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, "a post listed by two feeds prints once")
        self.assertTrue(lines[0].startswith("old.example 3ccc333 | "), out)  # 300 likes ranks first
        self.assertTrue(lines[1].startswith("maker.example 3ccc111 | "), out)

    def test_filter_rules(self):
        import bsky_filter
        owner = json.loads(fixture("bsky_resolve.json"))["did"]
        self.assertFalse(bsky_filter.keep(json.loads(fixture("bsky_thread.json"))["thread"], owner), "the owner already replied")
        self.assertTrue(bsky_filter.keep(json.loads(fixture("bsky_thread_unanswered.json"))["thread"], owner))
        self.assertFalse(bsky_filter.keep(json.loads(fixture("bsky_thread_gated.json"))["thread"], owner), "replies are gated")
        self.assertEqual(bsky_filter.candidate("Maker.Example 3ccc111 | 2h r2 l7 | text\n"), ("maker.example", "3ccc111"))
        self.assertIsNone(bsky_filter.candidate("ERR something\n"))

    def test_filter_finds_the_threadgate_where_the_api_puts_it(self):
        # getPostThread returns {thread, threadgate}: the gate sits beside the thread, and in the post view.
        import bsky_filter
        owner = "did:plc:owner123"
        reply = json.loads(fixture("bsky_thread_gated.json"))
        beside = json.loads(fixture("bsky_thread_gated.json"))
        del beside["thread"]["post"]["threadgate"]
        self.assertTrue(bsky_filter.keep(beside["thread"], owner), "without the gate beside it the thread looks open")
        self.assertFalse(bsky_filter.keep(beside["thread"], owner, beside["threadgate"]))
        self.assertFalse(bsky_filter.keep(reply["thread"], owner, reply["threadgate"]))
        followers_only = json.loads(fixture("bsky_thread_gated.json"))
        followers_only["threadgate"]["record"]["allow"] = [{"$type": "app.bsky.feed.threadgate#followerRule"}]
        del followers_only["thread"]["post"]["threadgate"]
        self.assertFalse(bsky_filter.keep(followers_only["thread"], owner, followers_only["threadgate"]))
        # a threadgate with no "allow" only hides replies: anyone may still reply
        hides = json.loads(fixture("bsky_thread_gated.json"))
        for gate in (hides["threadgate"], hides["thread"]["post"]["threadgate"]):
            del gate["record"]["allow"]
            gate["record"]["hiddenReplies"] = ["at://did:plc:x/app.bsky.feed.post/3hid"]
        self.assertTrue(bsky_filter.keep(hides["thread"], owner, hides["threadgate"]))

    def test_filter_drops_a_deleted_or_blocked_post(self):
        import bsky_filter
        owner = "did:plc:owner123"
        for thread in ({"$type": "app.bsky.feed.defs#notFoundPost", "uri": "at://x", "notFound": True},
                       {"$type": "app.bsky.feed.defs#blockedPost", "uri": "at://x", "blocked": True, "author": {"did": "did:plc:x"}},
                       {}, None, []):
            with self.subTest(thread=thread):
                self.assertFalse(bsky_filter.keep(thread, owner))

    def test_a_reply_that_changed_shape_is_reported_not_a_traceback(self):
        import bsky_authors
        import bsky_feed
        import bsky_open
        odd = {"feed": [None, {"post": None}, {"post": {"author": None, "record": None, "indexedAt": None}},
                        {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/3odd", "author": {"handle": "example.dev"},
                                  "record": {"text": None}, "replyCount": None, "likeCount": "many",
                                  "indexedAt": "2026-11-09T17:00:00.000Z"}}]}
        self.assertEqual(bsky_authors.rows_from_feed(odd, "example.dev", NOW), [(0, "example.dev", "3odd", 1.0, 0, 0, "")])
        self.assertEqual([r[3] for r in bsky_feed.rows_from_feed(odd, NOW)], ["3odd"])
        for changed in ({"posts": []}, [], None, {"feed": "nope"}):
            with self.assertRaises(ValueError):
                bsky_authors.rows_from_feed(changed, "example.dev", NOW)
            with self.assertRaises(ValueError):
                bsky_feed.rows_from_feed(changed, NOW)
        code, out, err = call(bsky_authors, "example.dev", fetch=serve([("getAuthorFeed", b'{"items": []}')]))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("ERR example.dev: no feed list in the reply (the API format may have changed)", err)
        self.assertNotIn("Traceback", err)
        thread = {"post": {}, "replies": [None, {"post": None}, {"post": {"author": None, "record": None}, "replies": None},
                                          {"post": {"author": {"handle": "asker.example"}, "uri": "at://x/y/3q",
                                                    "indexedAt": None, "record": {"text": None}}}]}
        self.assertEqual(bsky_open.open_replies(thread, "owner.example"), {("", "asker.example", "3q", "")})
        code, out, err = call(bsky_open, "owner.example", fetch=serve([("getAuthorFeed", b'{"feed": {"not": "a list"}}')]))
        self.assertEqual(code, 1)
        self.assertIn("no feed list in the reply", err)
        fetch = serve([("resolveHandle", b'{"handle": "owner.example"}')])
        code, out, err = call(__import__("bsky_filter"), "owner.example", fetch=fetch, stdin="maker.example 3ccc111 | x\n")
        self.assertEqual(code, 1)
        self.assertIn("can't resolve the owner handle owner.example: no did in the reply", err)

    def test_filter_main(self):
        import bsky_filter
        beside = json.loads(fixture("bsky_thread_gated.json"))
        del beside["thread"]["post"]["threadgate"]  # the gate only beside the thread, at the top of the reply
        fetch = serve([("resolveHandle?handle=owner.example", "bsky_resolve.json"),
                       ("resolveHandle?handle=maker.example", b'{"did": "did:plc:maker"}'),
                       ("resolveHandle?handle=gated.example", b'{"did": "did:plc:gated"}'),
                       ("resolveHandle?handle=beside.example", b'{"did": "did:plc:beside"}'),
                       ("resolveHandle?handle=asked.example", b'{"did": "did:plc:asked"}'),
                       ("did%3Aplc%3Amaker%2Fapp.bsky.feed.post%2F3ccc111", "bsky_thread_unanswered.json"),
                       ("did%3Aplc%3Agated", "bsky_thread_gated.json"),
                       ("did%3Aplc%3Abeside", json.dumps(beside).encode("utf-8")),
                       ("did%3Aplc%3Aasked", "bsky_thread.json"),
                       ("getProfile", "bsky_profile.json")])
        lines = ("Maker.Example 3ccc111 | 2h r2 l7 | Top-level post\n"
                 "maker.example 3ccc111 | 2h r2 l7 | the same post again\n"
                 "gated.example 3gat001 | 1h r0 l0 | Replies limited\n"
                 "beside.example 3gat001 | 1h r0 l0 | Replies limited, the gate beside the thread\n"
                 "asked.example 3own001 | 3h r3 l1 | already answered\n"
                 "gone.example 3xyz | 1h r0 l0 | can't be checked\n")
        code, out, err = call(bsky_filter, "Owner.Example", fetch=fetch, stdin=lines)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "f321 Maker.Example 3ccc111 | 2h r2 l7 | Top-level post\n")
        self.assertIn("ERR gone.example 3xyz:", err)  # dropped, but no longer silently
        code, out, err = call(bsky_filter, "owner.example", fetch=fetch, stdin="gone.example 3xyz | x\n")
        self.assertEqual(code, 1)
        self.assertIn("all 1 candidate failed", err)

    def test_open_replies(self):
        import bsky_open
        thread = json.loads(fixture("bsky_thread.json"))["thread"]
        got = bsky_open.open_replies(thread, "owner.example")  # the thread spells it "Owner.Example" once
        self.assertEqual(sorted(got), [("2026-11-09T06:50", "asker.example", "3ask002", "Nice, and on Arm?"),
                                       ("2026-11-09T07:00", "second.example", "3sec001", "Following this. Looks useful")])

    def test_open_main(self):
        import bsky_open
        feed = json.dumps({"feed": [{"post": {"uri": "at://did:plc:owner123/app.bsky.feed.post/3own001"}},
                                    {"post": {"uri": "at://did:plc:owner123/app.bsky.feed.post/3gone"}},
                                    {"post": {"uri": "at://did:plc:friend/app.bsky.feed.post/3rep"}, "reason": {}}]}).encode()
        fetch = serve([("getAuthorFeed", feed), ("3own001", "bsky_thread.json")])
        code, out, err = call(bsky_open, "@Owner.Example", "5", fetch=fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["2026-11-09T07:00 second.example 3sec001 | Following this. Looks useful",
                                            "2026-11-09T06:50 asker.example 3ask002 | Nice, and on Arm?"])
        self.assertIn("3gone", err)


class TestRenderCarousel(unittest.TestCase):
    def page(self, d):
        path = os.path.join(d, "index.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><title>t</title><p>slide</p>")
        return path

    def test_exit_1_when_slides_fail(self):
        with tempfile.TemporaryDirectory() as d:
            chrome = executable(d, "fakechrome", exit_code=1)  # starts, renders nothing
            out = os.path.join(d, "out")
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "2", "--out", out,
                    "--chrome", chrome, "--tries", "1")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(r.stdout.splitlines(), ["FAILED t-1", "FAILED t-2"])
            self.assertIn("2 of 2 slides failed to render: 1, 2", r.stderr)
            self.assertEqual(os.listdir(out), [])

    # A stand-in Chrome: writes the file --screenshot names (the PNG signature, the URL it was given, the IEND
    # chunk), records its arguments, then does what $FAKE_CHROME says: exit, hang, or stop half-way.
    SHOT = ("import os, sys, time\n"
            "png = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--screenshot=')][0]\n"
            "open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'argv.txt'), 'a', encoding='utf-8')"
            ".write('\\n'.join(sys.argv[1:]) + '\\n\\n')\n"
            "mode = os.environ.get('FAKE_CHROME', 'ok')\n"
            "body = b'\\x89PNG\\r\\n\\x1a\\n' + sys.argv[-1].encode()\n"
            "open(png, 'wb').write(body if mode == 'cut' else body + b'\\x00\\x00\\x00\\x00IEND\\xaeB`\\x82')\n"
            "if mode == 'hang':\n"
            "    time.sleep(90)\n")

    def chrome(self, d):
        shot = os.path.join(d, "shot.py")
        with open(shot, "w", encoding="utf-8") as fh:
            fh.write(self.SHOT)
        return executable(d, "fakechrome", python_script=shot)

    def test_exit_0_and_copies_when_slides_render(self):
        with tempfile.TemporaryDirectory() as d:
            chrome = self.chrome(d)
            out = os.path.join(d, "out")
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "3", "--only", "1", "3",
                    "--out", out, "--chrome", chrome, "--width", "1640", "--height", "624")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.splitlines(), ["ok t-1", "ok t-3"])
            self.assertEqual(sorted(os.listdir(out)), ["t-1.png", "t-3.png"])
            with open(os.path.join(out, "t-3.png"), "rb") as fh:
                self.assertIn(b"index.html#3", fh.read())

    def test_the_profile_is_thrown_away_and_the_url_is_encoded(self):
        with tempfile.TemporaryDirectory() as d:
            chrome = self.chrome(d)
            folder = os.path.join(d, "post #3 50%")  # a # or % in a folder name used to cut the file URL short
            os.makedirs(folder)
            r = run("render_carousel.py", "--html", self.page(folder), "--name", "t", "--count", "1", "--out", folder,
                    "--chrome", chrome)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(sorted(os.listdir(folder)), ["index.html", "t-1.png"], "no profile left beside the page")
            with open(os.path.join(d, "argv.txt"), encoding="utf-8") as fh:
                argv = fh.read().split("\n")
            self.assertTrue(argv[argv.index("") - 1].endswith("/post%20%233%2050%25/index.html#1"), argv)
            profile = [x.split("=", 1)[1] for x in argv if x.startswith("--user-data-dir=")][0]
            self.assertNotEqual(os.path.dirname(os.path.abspath(profile)), os.path.abspath(folder))
            self.assertFalse(os.path.exists(profile), "the throwaway profile is deleted afterwards")

    def test_nothing_is_left_beside_the_page(self):
        # each run renders into its own folder: two renders of one page into different --out folders can't swap
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "1", "--out", out,
                    "--chrome", self.chrome(d))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(os.listdir(out), ["t-1.png"])
            self.assertNotIn("t-1.png", os.listdir(d))

    def test_a_cut_off_png_is_a_failed_slide(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "1", "--out", out,
                    "--chrome", self.chrome(d), "--tries", "2", env={"FAKE_CHROME": "cut"})
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(r.stdout.splitlines(), ["FAILED t-1"])
            self.assertEqual(os.listdir(out), [])

    def test_a_chrome_that_never_exits_is_stopped_and_its_shot_kept(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            t0 = time.time()
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "1", "--out", out,
                    "--chrome", self.chrome(d), "--timeout", "3", env={"FAKE_CHROME": "hang"})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.splitlines(), ["ok t-1"])
            self.assertLess(time.time() - t0, 60)

    def test_chrome_given_but_missing_is_an_error_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "1", "--out", d,
                    "--chrome", os.path.join(d, "no-such-chrome"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("Chrome not found at", r.stderr)
            r = run("render_carousel.py", "--html", self.page(d), "--name", "t", "--count", "1", "--out", d,
                    env={"AGENT_PLAYBOOK_CHROME": os.path.join(d, "no-such-chrome")})
            self.assertEqual(r.returncode, 1)
            self.assertIn("Chrome not found", r.stderr)


class TestDoctor(unittest.TestCase):
    def test_runs_with_clipboard_and_network_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            r = run("doctor.py", "--no-clipboard", "--no-network", marker=marker)
            self.assertIn(r.returncode, (0, 1), r.stderr)  # 1 only when this machine has no Chrome
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("[OK  ] Python 3.8+", r.stdout)
            self.assertIn("[skip] Clipboard round-trip", r.stdout)
            self.assertIn("[skip] Bluesky public API", r.stdout)
            self.assertFalse(os.path.exists(marker))


@unittest.skipUnless(sys.platform == "win32", "Win32 global-memory helpers")
class TestClipboardWin32(unittest.TestCase):
    def test_global_block_round_trip(self):
        # Handles are 64-bit on x64 Windows; passing one without declared prototypes
        # raised "int too long to convert". No clipboard access here.
        _, k32 = common.win32_clipboard()
        h = common.global_copy(k32, b"\x00" * 4096)
        self.assertTrue(h)
        self.assertFalse(k32.GlobalFree(h))  # NULL means the block was freed


PLAYBOOK = os.path.join(ROOT, "agent-playbook", "playbook")


class TestPlaybook(unittest.TestCase):
    def pb(self, *args):
        e = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, os.path.join(PLAYBOOK, "pb.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", env=e, timeout=60)

    def test_playbook_scripts_compile(self):
        import py_compile
        for name in ("pb.py", "server.py", "audit.py", "launch.pyw", "install_shortcut.py", os.path.join("app", "build_page.py")):
            py_compile.compile(os.path.join(PLAYBOOK, name), doraise=True)

    def test_store_is_consistent(self):
        r = self.pb("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 problem(s)", r.stdout)

    def test_show_follows_gotos(self):
        r = self.pb("show", "release")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[START]", r.stdout)
        self.assertIn("python pb.py show", r.stdout)

    def test_page_has_no_unfilled_placeholders(self):
        with open(os.path.join(PLAYBOOK, "playbook.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("__ICONS__", html)
        self.assertIn("__PB_TOKEN__", html)  # filled in by server.py per run

    def test_audit_log_parses(self):
        e = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, os.path.join(PLAYBOOK, "audit.py"), "stats"], capture_output=True, text=True, encoding="utf-8", env=e, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("warning", r.stderr)


class TestDeleteReasons(unittest.TestCase):
    """Every deletion, in the playbook or the audit log, needs a reason. Runs on a throwaway copy of the playbook."""

    def setUp(self):
        import importlib.util
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.pb = os.path.join(self.tmp, "playbook")
        shutil.copytree(PLAYBOOK, self.pb, ignore=shutil.ignore_patterns("history", ".trash", "__pycache__", "store.json"))
        spec = importlib.util.spec_from_file_location("pb_server_copy", os.path.join(self.pb, "server.py"))
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *args):
        e = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8", env=e, cwd=self.pb, timeout=60)

    def flow(self, fid):
        import json
        with open(os.path.join(self.pb, "store", "flows", fid + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_dropping_steps_needs_a_reason(self):
        srv = self.server
        f = self.flow("audit")
        gone = f["nodes"][0]["id"]  # the store now refuses a flow whose connections point at a missing step
        cut = dict(f, nodes=f["nodes"][1:], edges=[e for e in f["edges"] if gone not in (e["from"], e["to"])])
        with self.assertRaises(srv.ReasonRequired) as ctx:
            srv.write_doc("flows", "audit", cut)
        self.assertEqual(ctx.exception.removed, [f["nodes"][0]["title"]])
        srv.write_doc("flows", "audit", dict(f, summary=f.get("summary", "") + " "))  # no steps dropped: no reason needed
        srv.write_doc("flows", "audit", cut, reason="  Duplicate   step ")
        last = srv.read_changes(1)[0]
        self.assertEqual(last["reason"], "Duplicate step")
        self.assertEqual(last["removed"], [f["nodes"][0]["title"]])

    def test_deleting_a_connection_needs_a_reason(self):
        srv = self.server
        f = self.flow("audit")
        e0 = f["edges"][0]
        flipped = dict(e0, id=e0["to"] + ">" + e0["from"], **{"from": e0["to"], "to": e0["from"]})
        srv.write_doc("flows", "audit", dict(f, edges=[flipped] + f["edges"][1:]))  # reversing keeps the link
        srv.write_doc("flows", "audit", f)
        with self.assertRaises(srv.ReasonRequired) as ctx:
            srv.write_doc("flows", "audit", dict(f, edges=f["edges"][1:]))
        self.assertEqual(len(ctx.exception.removed), 1)
        self.assertTrue(ctx.exception.removed[0].startswith("connection "))
        srv.write_doc("flows", "audit", dict(f, edges=f["edges"][1:]), reason="Wrong order")
        self.assertEqual(srv.read_changes(1)[0]["reason"], "Wrong order")

    def test_deleting_a_document_needs_a_reason(self):
        srv = self.server
        part = sorted(os.listdir(os.path.join(self.pb, "store", "parts")))[0][:-5]
        with self.assertRaises(srv.ReasonRequired):
            srv.delete_doc("parts", part)
        self.assertTrue(os.path.exists(os.path.join(self.pb, "store", "parts", part + ".json")))
        srv.delete_doc("parts", part, reason="No longer used")
        self.assertFalse(os.path.exists(os.path.join(self.pb, "store", "parts", part + ".json")))
        last = srv.read_changes(1)[0]
        self.assertEqual((last["action"], last["reason"]), ("delete", "No longer used"))

    def test_pb_delete_refuses_without_reason(self):
        folder = sorted(os.listdir(os.path.join(self.pb, "store", "folders")))[0][:-5]
        r = self.run_tool("pb.py", "delete", "folders", folder)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reason is required", r.stderr)
        r = self.run_tool("pb.py", "delete", "folders", folder, "--reason", "test")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_audit_delete_refuses_without_reason(self):
        r = self.run_tool("audit.py", "add", "--platform", "X", "--account", "@acmenotes_demo", "--action", "post", "--text", "hello", "--at", "2026-11-09T10:00:00", "--override", "test setup: not a real send")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = r.stdout.strip()
        for status in ("deleted", "removed"):
            r = self.run_tool("audit.py", "update", item, "--status", status)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("reason is required", r.stderr)
        r = self.run_tool("audit.py", "update", item, "--status", "deleted", "--reason", "Wrong link")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_tool("audit.py", "show", item)
        self.assertIn("reason: Wrong link", r.stdout)
        r = self.run_tool("audit.py", "add", "--platform", "X", "--action", "delete", "--text", "gone")
        self.assertNotEqual(r.returncode, 0)



class TestRunChecklist(unittest.TestCase):
    """run.py on an isolated copy of the scripts and the recipes."""

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(SCRIPTS, os.path.join(self.tmp, "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(self.tmp, "playbook", "state"))
        shutil.copy(os.path.join(ROOT, "tests", "fixtures", "scripts", "recipes.json"), os.path.join(self.tmp, "playbook", "state"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, *args):
        e = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, os.path.join(self.tmp, "scripts", "run.py")] + list(args),
                              capture_output=True, text=True, encoding="utf-8", env=e, timeout=60)

    def test_nothing_skipped_silently(self):
        self.assertEqual(self.run_py("check").returncode, 0)  # no run open: nothing to hold the agent to
        self.assertEqual(self.run_py("start", "--kind", "post").returncode, 0)
        self.assertNotEqual(self.run_py("start", "--kind", "reply").returncode, 0)  # one open run at a time
        r = self.run_py("check")
        self.assertEqual(r.returncode, 2)
        self.assertIn("todo", r.stderr)
        self.assertNotEqual(self.run_py("finish").returncode, 0)
        self.assertNotEqual(self.run_py("skip", "facts", "--reason", "   ").returncode, 0)
        self.assertNotEqual(self.run_py("done", "nosuch", "--note", "x").returncode, 0)
        for step in ("start", "facts", "qc", "gap", "live", "logged"):
            self.assertEqual(self.run_py("done", step, "--note", "checked").returncode, 0)
        self.assertEqual(self.run_py("skip", "report", "--reason", "owner: said skip it").returncode, 0)
        r = self.run_py("finish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("report: owner: said skip it (owner's call)", r.stdout)
        self.assertEqual(self.run_py("check").returncode, 0)

    def test_agent_skip_is_flagged_for_the_owner(self):
        self.run_py("start", "--kind", "post")
        for step in ("start", "facts", "qc", "gap", "live", "logged"):
            self.run_py("done", step, "--note", "ok")
        self.run_py("skip", "report", "--reason", "ran out of time")
        self.assertIn("agent's call: tell the owner", self.run_py("finish").stdout)

    def test_abandon_needs_a_reason(self):
        self.run_py("start", "--kind", "reply")
        self.assertNotEqual(self.run_py("abandon").returncode, 0)
        r = self.run_py("abandon", "--reason", "owner stopped the run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_py("check").returncode, 0)

    def test_recipe_steps_name_their_flow(self):
        r = self.run_py("recipes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("news-share", r.stdout)
        r = self.run_py("start", "--recipe", "news-share")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(flow qc)", r.stdout)
        self.assertIn("(part part-verify-live)", r.stdout)
        self.run_py("abandon", "--reason", "test")

    def test_chained_recipes(self):
        r = self.run_py("start", "--recipe", "news-share,group-post")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("news-share:start", r.stdout)
        self.assertIn("group-post:mark", r.stdout)
        self.assertEqual(self.run_py("done", "group-post:mark", "--note", "pace.py --mark example-group").returncode, 0)
        self.assertNotEqual(self.run_py("start", "--recipe", "nope").returncode, 0)

    def test_unknown_recipe(self):
        r = self.run_py("start", "--recipe", "news-share,nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown recipe", r.stderr)

    def test_owner_next_run_carries_over(self):
        self.run_py("start", "--recipe", "post")
        for step in ("start", "facts", "qc", "gap", "live", "logged"):
            self.run_py("done", step, "--note", "ok")
        self.run_py("skip", "report", "--reason", "owner: next run, out of time today")
        self.assertEqual(self.run_py("finish").returncode, 0)
        r = self.run_py("start", "--recipe", "post")  # the next run of the same recipe gets it
        self.assertIn("Left for this run by the last one", r.stdout)
        self.assertIn("- report (post run", r.stdout)

    def test_a_damaged_run_file_is_skipped_not_a_traceback(self):
        runs = os.path.join(self.tmp, "playbook", "state", "runs")
        os.makedirs(runs)
        for name, text in (("20260101-000000-post.json", '{"kind": "post", "state": "op'),
                           ("20260102-000000-post.json", '["not", "a", "run"]'),
                           ("20260103-000000-post.json", '{"kind": "post", "state": "finished", "steps": [{"id": "a", "status": "done"}]}'),
                           ("20260104-000000-post.json", '{"kind": "post", "state": "open", "steps": [{"id": 1}]}')):
            with open(os.path.join(runs, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        # the newest file may be the open run: the Stop hook's check and start fail closed and say how to close it
        for args in (("check",), ("start", "--recipe", "post")):
            r = self.run_py(*args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("20260104-000000-post.json can't be read and may be the open run", r.stderr)
        os.replace(os.path.join(runs, "20260104-000000-post.json"), os.path.join(self.tmp, "moved-out.json"))
        r = self.run_py("check")  # damaged files older than a readable run can't be open: skipped with a warning
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("skipped 20260101-000000-post.json", r.stderr)
        r = self.run_py("start", "--recipe", "post")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_py("check").returncode, 2)
        r = self.run_py("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 2)  # the finished run and the new one

    def test_finish_lists_flags_logged_in_another_utc_offset(self):
        # the run's start and the audit stamps are compared as times, not as text
        self.assertEqual(self.run_py("start", "--recipe", "lesson").returncode, 0)
        later = dt.datetime.now(dt.timezone(dt.timedelta(hours=-12))) + dt.timedelta(seconds=5)
        os.makedirs(os.path.join(self.tmp, "playbook", "audit"))
        with open(os.path.join(self.tmp, "playbook", "audit", "posts.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "item", "id": "a1", "platform": "X", "account": "@demo",
                                 "logged_at": later.isoformat(timespec="seconds"), "pace_flag": "2 min after the last"}) + "\n")
            fh.write(json.dumps({"kind": "item", "id": "a2", "platform": "Bluesky", "account": "@demo",
                                 "logged_at": later.isoformat(timespec="seconds"), "qc_warn": "one"}) + "\n")
            fh.write("not json\n")
        r = self.run_py("status")
        for line in r.stdout.splitlines():
            if line.startswith("[ ] "):
                self.run_py("skip", line.split()[2], "--reason", "test")
        r = self.run_py("finish")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("- a1 X @demo: 2 min after the last", r.stdout)
        self.assertIn("- a2 Bluesky @demo: one", r.stdout)

    def test_carry_over_waits_for_its_recipe(self):
        self.run_py("start", "--recipe", "post")
        for step in ("start", "facts", "qc", "gap", "live", "logged"):
            self.run_py("done", step, "--note", "ok")
        self.run_py("skip", "report", "--reason", "owner: next run")
        self.run_py("finish")
        r = self.run_py("start", "--recipe", "lesson")  # another recipe: the leftover waits
        self.assertNotIn("Left for this run", r.stdout)
        self.run_py("abandon", "--reason", "test")
        r = self.run_py("start", "--recipe", "post")
        self.assertIn("- report (post run", r.stdout)


if __name__ == "__main__":
    unittest.main()
