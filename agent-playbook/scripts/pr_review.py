#!/usr/bin/env python3
"""Prepare, run and clean up an independent read-only PR review (the Grok CLI by default).

  pr_review.py prep REPO PR [--base REF] [--brief FILE] [--force]
      Fetch the PR, check its head out in a detached review worktree (REPO/.claude/worktrees/review-prN),
      export the diffs into that checkout's .review folder, and write a brief skeleton (default
      .review/brief.md; an existing brief is kept unless --force). --base REF diffs from REF instead of the
      merge base with the PR's base branch: a round-2 review of the fix commits only.
  pr_review.py cmd CHECKOUT BRIEF [--model M]     print the exact reviewer command line
  pr_review.py run CHECKOUT BRIEF OUT [--model M] [--timeout MIN]
      run the reviewer at below-normal priority; its plain output is saved to OUT
  pr_review.py done REPO PR                       remove the review worktree (refused if it holds changes)

Exports in .review: name-status.txt (every changed path), src.diff, tests.diff, tools.diff (everything outside
src/, tests/ and docs/) and docs.diff (without docs/delivery/reports), all without lock files, images, fonts
or audio; pr.md holds the title, body, head and base.
The reviewer is read-only by construction: --permission-mode plan, no shell tool, no web search, no
subagents, so it reviews by reading files. It can't run git headless, which is why the diffs are exported.
GROK_BIN overrides the executable; the default is grok on PATH, then ~/.grok/bin/grok(.exe).
Needs git and the GitHub CLI (gh, signed in) for prep. Exit 0 ok, 1 failure, 2 usage error.
"""
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = ["git", "gh", "grok"]
EFFECTS = ["files", "network"]

MODEL = "grok-4.7"
EFFORT = "xhigh"
SKIP = ("**/packages.lock.json", "**/package-lock.json", "**/*.lock", "**/*.png", "**/*.jpg", "**/*.jpeg",
        "**/*.gif", "**/*.webp", "**/*.ico", "**/*.psd", "**/*.ttf", "**/*.otf", "**/*.woff", "**/*.woff2",
        "**/*.wav", "**/*.ogg", "**/*.mp3", "**/*.flac", "**/*.mp4", "**/*.zip")
GROUPS = (("src.diff", ["src/"]),
          ("tests.diff", ["tests/"]),
          ("tools.diff", [".", ":(exclude)src/", ":(exclude)tests/", ":(exclude)docs/"]),
          ("docs.diff", ["docs/", ":(exclude)docs/delivery/reports/"]))
BELOW_NORMAL = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
GIT_TIMEOUT = 600  # seconds for one git or gh call (a fetch of a big repository included)
# Never wait on a prompt nobody can answer: no terminal credential prompt, no credential-manager window.
QUIET_GIT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"}


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG {prep REPO PR | cmd CHECKOUT BRIEF | run CHECKOUT BRIEF OUT | done REPO PR} [options]")
    sub = ap.add_subparsers(dest="action", metavar="ACTION")
    sub.required = True
    p = sub.add_parser("prep", help="check out and export a PR for review")
    p.add_argument("repo", metavar="REPO")
    p.add_argument("pr", metavar="PR", type=int)
    p.add_argument("--base", metavar="REF", help="diff from REF (round 2: the last reviewed head)")
    p.add_argument("--brief", metavar="FILE", help="where to write the brief skeleton")
    p.add_argument("--force", action="store_true", help="overwrite an existing brief")
    for name in ("cmd", "run"):
        p = sub.add_parser(name, help="print the reviewer command" if name == "cmd" else "run the reviewer")
        p.add_argument("checkout", metavar="CHECKOUT")
        p.add_argument("brief", metavar="BRIEF")
        if name == "run":
            p.add_argument("out", metavar="OUT")
            p.add_argument("--timeout", metavar="MIN", type=float, default=90.0)
        p.add_argument("--model", metavar="M", default=MODEL)
    p = sub.add_parser("done", help="remove the review worktree")
    p.add_argument("repo", metavar="REPO")
    p.add_argument("pr", metavar="PR", type=int)
    return ap


def quiet_run(cmd, what, **kw):
    """Run git or gh with no prompts and a time limit; exit 1 naming `what` when it can't start or hangs."""
    try:
        return common.run_silent(cmd, env=dict(os.environ, **QUIET_GIT), stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, **kw)
    except subprocess.TimeoutExpired:
        common.die("%s did not finish in %d s" % (what, GIT_TIMEOUT))
    except OSError as e:
        common.die("can't run %s (%s): %s" % (what, cmd[0], e.strerror or e))


def git(repo, *args, check=True):
    r = quiet_run(["git", "-C", repo] + list(args), "git %s" % " ".join(args[:3]), text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        common.die("git %s failed: %s" % (" ".join(args[:3]), (r.stderr or r.stdout).strip()[:400]))
    return r.stdout


def is_repo(path):
    if not os.path.isdir(path):
        return False
    r = quiet_run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], "git rev-parse", text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def worktree_path(repo, pr):
    return os.path.join(os.path.abspath(repo), ".claude", "worktrees", "review-pr%d" % pr)


def pathspec(paths):
    return paths + [":(exclude,glob)%s" % g for g in SKIP]


def pr_info(repo, pr):
    exe = shutil.which("gh")
    if not exe:
        common.die("the GitHub CLI (gh) is not on PATH")
    fields = "number,title,body,url,headRefName,headRefOid,baseRefName"
    r = quiet_run([exe, "pr", "view", str(pr), "--json", fields], "gh pr view", cwd=repo, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        common.die("gh pr view %d failed: %s" % (pr, (r.stderr or r.stdout).strip()[:400]))
    try:
        return json.loads(r.stdout)
    except ValueError:
        common.die("gh pr view %d did not return JSON" % pr)


def brief_text(info, base, files):
    body = (info.get("body") or "").strip() or "(no description)"
    return "\n".join([
        "# Review brief: PR #%d, %s" % (info["number"], info["title"]),
        "",
        "Review this pull request read-only. You cannot run commands: read files instead. The checkout is the",
        "PR's head (%s); read any file in full for context." % info["headRefOid"],
        "",
        "## Material",
        "",
        "- `.review/name-status.txt`: every changed path (%d) against %s." % (files, base),
        "- `.review/src.diff`, `.review/tests.diff`, `.review/tools.diff`, `.review/docs.diff`: the changes,",
        "  without lock files, images, fonts or audio. Delivery evidence under docs/delivery/reports is left out.",
        "- `.review/pr.md`: the title and description.",
        "",
        "## PR description",
        "",
        body,
        "",
        "## Focus",
        "",
        "Focus on src/, tests/ and tools/: correctness bugs, regressions, missed edge and failure cases,",
        "threading and async misuse, resource leaks, state that can go stale, input handling, and tests that",
        "don't prove what they claim. Not style nits, not formatting, not the delivery evidence.",
        "",
        "## Already known or out of scope",
        "",
        "- FILL IN: earlier-round findings and how each was fixed; deliberate deferrals with their milestone.",
        "",
        "## Output",
        "",
        "First line: the verdict, one of APPROVE, APPROVE WITH NITS, REQUEST CHANGES.",
        "Then each finding: severity (blocker, major, minor, nit), `path:line`, what goes wrong (the inputs or",
        "state, and the wrong result), and the fix you suggest. Most severe first. No praise. If you find",
        "nothing, say so.",
        "",
    ])


def prep(a):
    repo = os.path.abspath(a.repo)
    if not is_repo(repo):
        common.die("no git repository at %s" % a.repo)
    info = pr_info(repo, a.pr)
    head, base_branch = info["headRefOid"], info["baseRefName"]
    git(repo, "fetch", "--quiet", "origin", info["headRefName"], base_branch)
    if git(repo, "cat-file", "-t", head, check=False).strip() != "commit":
        common.die("the PR head %s is not in the fetched history" % head)
    wt = worktree_path(repo, a.pr)
    if os.path.isdir(wt):
        git(wt, "checkout", "--quiet", "--detach", head)
    else:
        git(repo, "worktree", "add", "--quiet", "--detach", wt, head)
    base = a.base or git(repo, "merge-base", "origin/" + base_branch, head).strip()
    out = os.path.join(wt, ".review")
    os.makedirs(out, exist_ok=True)
    names = git(wt, "diff", "--no-color", "--name-status", "-M", base, head)
    common.write_atomic(os.path.join(out, "name-status.txt"), names)
    sizes = []
    for name, paths in GROUPS:
        text = git(wt, "diff", "--no-color", "--no-ext-diff", "-M", base, head, "--", *pathspec(paths))
        common.write_atomic(os.path.join(out, name), text)
        sizes.append("%s %d lines" % (name, text.count("\n")))
    common.write_atomic(os.path.join(out, "pr.md"), "# PR #%d: %s\n\n%s\n\nhead %s\nbase %s (%s)\n%s\n" % (
        info["number"], info["title"], (info.get("body") or "").strip(), head, base, base_branch, info.get("url", "")))
    brief = os.path.abspath(a.brief) if a.brief else os.path.join(out, "brief.md")
    files = len([line for line in names.splitlines() if line.strip()])
    if os.path.exists(brief) and not a.force:
        kept = "kept (use --force to rewrite)"
    else:
        common.write_atomic(brief, brief_text(info, base, files))
        kept = "written"
    print("checkout %s" % wt)
    print("head %s  base %s  %d files" % (head, base, files))
    print("exports %s" % ", ".join(sizes))
    print("brief %s %s: fill in the known and out-of-scope section" % (brief, kept))
    print("next: pr_review.py run %s %s <OUT>" % (wt, brief))
    return 0


def grok_exe():
    env = os.environ.get("GROK_BIN")
    if env:
        return env
    found = shutil.which("grok")
    if found:
        return found
    home = os.path.join(os.path.expanduser("~"), ".grok", "bin", "grok.exe" if common.WINDOWS else "grok")
    return home if os.path.exists(home) else None


def command(exe, checkout, brief, model):
    return [exe, "-m", model, "--reasoning-effort", EFFORT, "--permission-mode", "plan",
            "--disallowed-tools", "run_terminal_cmd", "--disable-web-search", "--no-subagents",
            "--cwd", checkout, "--output-format", "plain", "--prompt-file", brief]


def check_inputs(a):
    if not os.path.isdir(a.checkout):
        common.die("no checkout at %s" % a.checkout)
    if not os.path.isfile(a.brief):
        common.die("no brief at %s" % a.brief)
    exe = grok_exe()
    if not exe:
        common.die("grok not found: set GROK_BIN or put grok on PATH")
    return exe


def quote(arg):
    return '"%s"' % arg if re.search(r"\s", arg) else arg


def cmd(a):
    exe = check_inputs(a)
    print(" ".join(quote(x) for x in command(exe, os.path.abspath(a.checkout), os.path.abspath(a.brief), a.model)))
    return 0


def run(a):
    exe = check_inputs(a)
    argv = command(exe, os.path.abspath(a.checkout), os.path.abspath(a.brief), a.model)
    try:
        # run_tree: past the time limit the reviewer and anything it started are ended, not just the first process
        r = common.run_tree(argv, timeout=a.timeout * 60, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=BELOW_NORMAL if common.WINDOWS else 0)
    except subprocess.TimeoutExpired:
        common.die("the reviewer ran past %g minutes (stopped)" % a.timeout)
    except OSError as e:
        common.die("can't start the reviewer: %s" % e)
    if r.returncode != 0 or not r.stdout.strip():
        common.die("the reviewer failed (exit %d): %s" % (r.returncode, (r.stderr or "no output").strip()[:400]))
    try:
        common.write_atomic(a.out, r.stdout)
    except OSError as e:
        common.die("can't write %s: %s" % (a.out, e.strerror or e))
    first = next((line.strip() for line in r.stdout.splitlines() if line.strip()), "")
    print("saved %s: %s" % (a.out, first[:120]))
    return 0


def done(a):
    repo = os.path.abspath(a.repo)
    if not is_repo(repo):
        common.die("no git repository at %s" % a.repo)
    wt = worktree_path(repo, a.pr)
    if not os.path.isdir(wt):
        print("no review worktree for PR %d" % a.pr)
        return 0
    status = [line for line in git(wt, "status", "--porcelain", "--untracked-files=normal").splitlines()
              if line.strip() and line[3:].rstrip("/") != ".review"]
    if status:
        common.die("the review worktree holds changes, left in place: %s" % "; ".join(status[:5]))
    shutil.rmtree(os.path.join(wt, ".review"), ignore_errors=True)
    git(repo, "worktree", "remove", wt)
    print("removed %s" % wt)
    return 0


def main(argv=None):
    common.safe_stdio()
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.action == "run" and a.timeout <= 0:
        ap.error("--timeout must be above 0 minutes")
    if a.action in ("prep", "done") and a.pr <= 0:
        ap.error("PR is the pull request's number")
    return {"prep": prep, "cmd": cmd, "run": run, "done": done}[a.action](a)


if __name__ == "__main__":
    sys.exit(main())
