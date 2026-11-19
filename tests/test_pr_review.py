"""pr_review.py against a real throwaway git repository: what the exports hold and when cleanup refuses.

The GitHub lookup is replaced with the PR's facts; git runs for real on a bare origin in a temp folder.
"""
import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "agent-playbook", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import pr_review  # noqa: E402

GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    r = subprocess.run(["git"] + GIT_ID + list(args), cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), r.stderr))
    return r.stdout.strip()


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


@unittest.skipUnless(shutil.which("git"), "needs git")
class TestPrReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", origin)
        git(self.tmp, "clone", "--quiet", origin, self.repo)
        git(self.repo, "checkout", "--quiet", "-b", "main")
        write(self.repo, "src/app.cs", "class App {}\n")
        write(self.repo, ".gitignore", ".claude/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "base")
        git(self.repo, "push", "--quiet", "origin", "main")
        git(self.repo, "checkout", "--quiet", "-b", "feat")
        write(self.repo, "src/app.cs", "class App { int Score; }\n")
        write(self.repo, "tests/AppTests.cs", "class AppTests {}\n")
        write(self.repo, "tools/build.ps1", "Write-Output build\n")
        write(self.repo, "src/packages.lock.json", "{\"lock\": 1}\n")
        write(self.repo, "docs/design/PLAN.md", "# Plan\n")
        write(self.repo, "docs/delivery/reports/run.md", "evidence\n")
        with open(os.path.join(self.repo, "art.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "feature")
        git(self.repo, "push", "--quiet", "origin", "feat")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--quiet", "main")
        self.info = {"number": 7, "title": "Add a score", "body": "Adds the score field.", "url": "",
                     "headRefName": "feat", "headRefOid": self.head, "baseRefName": "main"}
        self._orig = pr_review.pr_info
        pr_review.pr_info = lambda repo, pr: self.info

    def tearDown(self):
        pr_review.pr_info = self._orig
        subprocess.run(["git", "-C", self.repo, "worktree", "prune"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prep(self, **kw):
        args = argparse.Namespace(repo=self.repo, pr=7, base=None, brief=None, force=False)
        for k, v in kw.items():
            setattr(args, k, v)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pr_review.prep(args), 0)
        return out.getvalue()

    def read(self, name):
        with open(os.path.join(pr_review.worktree_path(self.repo, 7), ".review", name), encoding="utf-8") as fh:
            return fh.read()

    def test_exports_split_the_diff_and_leave_out_locks_images_and_evidence(self):
        self.prep()
        wt = pr_review.worktree_path(self.repo, 7)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), self.head)
        self.assertIn("int Score", self.read("src.diff"))
        self.assertNotIn("lock", self.read("src.diff"))
        self.assertIn("AppTests", self.read("tests.diff"))
        tools = self.read("tools.diff")
        self.assertIn("build.ps1", tools)
        self.assertNotIn("art.png", tools)
        self.assertNotIn("app.cs", tools)
        docs = self.read("docs.diff")
        self.assertIn("PLAN.md", docs)
        self.assertNotIn("evidence", docs)
        self.assertIn("src/packages.lock.json", self.read("name-status.txt"))
        brief = self.read("brief.md")
        self.assertIn("PR #7, Add a score", brief)
        self.assertIn("Adds the score field.", brief)
        self.assertIn("REQUEST CHANGES", brief)

    def test_an_existing_brief_is_kept_unless_forced(self):
        self.prep()
        path = os.path.join(pr_review.worktree_path(self.repo, 7), ".review", "brief.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("my notes")
        self.prep()
        self.assertEqual(self.read("brief.md"), "my notes")
        self.prep(force=True)
        self.assertIn("Review brief", self.read("brief.md"))

    def test_base_limits_a_round_two_review_to_the_new_commits(self):
        self.prep()
        git(self.repo, "checkout", "--quiet", "feat")
        write(self.repo, "src/app.cs", "class App { int Score; int Best; }\n")
        git(self.repo, "commit", "--quiet", "-am", "fix")
        git(self.repo, "push", "--quiet", "origin", "feat")
        first, self.info["headRefOid"] = self.head, git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--quiet", "main")
        self.prep(base=first)
        src = self.read("src.diff")
        self.assertIn("+class App { int Score; int Best; }", src)
        self.assertIn("-class App { int Score; }", src)
        self.assertEqual(self.read("tests.diff"), "")

    def test_done_refuses_a_worktree_with_changes_then_removes_a_clean_one(self):
        self.prep()
        wt = pr_review.worktree_path(self.repo, 7)
        write(wt, "src/app.cs", "edited\n")
        args = argparse.Namespace(repo=self.repo, pr=7)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            pr_review.done(args)
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("holds changes", err.getvalue())
        self.assertTrue(os.path.isdir(wt))
        git(wt, "checkout", "--quiet", "--", "src/app.cs")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pr_review.done(args), 0)
        self.assertFalse(os.path.exists(wt))

    def test_the_reviewer_command_is_read_only(self):
        argv = pr_review.command("grok", "C:/co", "C:/brief.md", pr_review.MODEL)
        joined = " ".join(argv)
        for flag in ("--permission-mode plan", "--disallowed-tools run_terminal_cmd", "--disable-web-search",
                     "--no-subagents", "--reasoning-effort xhigh", "-m grok-4.7"):
            self.assertIn(flag, joined)
        for unsafe in ("bypassPermissions", "--always-approve", "--allow"):
            self.assertNotIn(unsafe, joined)


class TestNothingWaitsForever(unittest.TestCase):
    def test_git_and_gh_run_without_prompts_and_with_a_time_limit(self):
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        real = pr_review.common.run_silent
        pr_review.common.run_silent = fake
        try:
            self.assertEqual(pr_review.git(".", "status"), "true\n")
        finally:
            pr_review.common.run_silent = real
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")  # a fetch that needs a password fails, never waits
        self.assertEqual(seen["timeout"], pr_review.GIT_TIMEOUT)
        self.assertIs(seen["stdin"], subprocess.DEVNULL)

        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        pr_review.common.run_silent = hangs
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                pr_review.git(".", "fetch", "--quiet", "origin")
        finally:
            pr_review.common.run_silent = real
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("git fetch --quiet origin did not finish in %d s" % pr_review.GIT_TIMEOUT, err.getvalue())

    def test_a_reviewer_past_its_time_is_ended_with_what_it_started(self):
        import time
        from test_scripts import SPAWN_AND_HANG, ended, executable, read_pid
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "grok.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(SPAWN_AND_HANG)
        brief = os.path.join(d, "brief.md")
        with open(brief, "w", encoding="utf-8") as fh:
            fh.write("# brief\n")
        pid_file = os.path.join(d, "pid.txt")
        env = dict(os.environ, GROK_BIN=executable(d, "grok", python_script=script), TREE_PID_FILE=pid_file)
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "pr_review.py"), "run", d, brief, os.path.join(d, "out.txt"),
                            "--timeout", "0.05"], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("the reviewer ran past 0.05 minutes (stopped)", r.stderr)
        self.assertLess(time.time() - t0, 60)
        self.assertTrue(ended(read_pid(pid_file)), "what the reviewer started outlived the time limit")
        self.assertFalse(os.path.exists(os.path.join(d, "out.txt")))


if __name__ == "__main__":
    unittest.main()
