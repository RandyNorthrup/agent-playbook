#!/usr/bin/env python3
"""Generate one image with the Codex CLI's built-in image generation (the owner's ChatGPT sign-in), no browser.

  codex_img.py "<prompt>" <out.png> [--timeout 600] [--ref image.png ...]

Runs `codex exec` in a throwaway work folder with the prompt on stdin, then copies the PNG that this run
produced under <CODEX_HOME or ~/.codex>/generated_images/<thread id>/ to <out.png> (byte for byte) and
writes <out>.txt with the prompt beside it. Safe to run in the background alongside browser work.
--ref attaches a reference image (a character sheet); repeat it for several.
Each run draws on the ChatGPT plan's weekly Codex limit: check the quota before a bulk run.
The codex binary is --codex, else $CODEX_BIN, else `codex` on PATH.
Exit 1 when codex is missing, a reference is missing, the run timed out or it produced no image.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = ["Codex CLI (signed in)"]
EFFECTS = ["network", "files"]
WRAP = ("Use your image generation tool to create exactly one image. %s"
        " Do not write any code or files yourself; just generate the image and reply 'done'.")


def build_parser():
    ap = common.make_parser(__doc__, __file__, 'PROG "PROMPT" OUT.png [--timeout 600] [--ref IMAGE ...] [--codex PATH]')
    ap.add_argument("prompt", metavar="PROMPT")
    ap.add_argument("out", metavar="OUT.png")
    ap.add_argument("--timeout", type=int, default=600, metavar="SECONDS", help="give up after this long (default 600)")
    ap.add_argument("--ref", action="append", default=[], metavar="IMAGE", help="a reference image; repeatable")
    ap.add_argument("--codex", metavar="PATH", help="the codex binary (default: $CODEX_BIN, else codex on PATH)")
    return ap


class CodexError(Exception):
    """One run made no image; str() is the one-line reason."""


def find_codex(explicit=None):
    """The command that starts codex, or None when it isn't installed. An npm install's .cmd shim is
    bypassed (node runs codex.js itself) so stdin and arguments pass through cleanly."""
    exe = explicit or os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not exe or not (os.path.exists(exe) or shutil.which(exe)):
        return None
    js = Path(exe).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if js.exists():
        return [shutil.which("node") or "node", str(js)]
    return [exe]


def codex_cmd(explicit=None):
    """find_codex(), or exit 1 saying where it was looked for."""
    cmd = find_codex(explicit)
    if not cmd:
        exe = explicit or os.environ.get("CODEX_BIN")
        common.die("codex not found%s; install the Codex CLI or pass --codex PATH" % (" at %s" % exe if exe else " on PATH"))
    return cmd


def exec_args(refs=()):
    """The arguments of one image run; the prompt goes in on stdin."""
    return ["exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"] + [x for r in refs for x in ("-i", str(r))] + ["-"]


def thread_id(events):
    """The thread id from codex's --json event lines, or None."""
    for line in events.splitlines():
        if '"thread.started"' in line:
            try:
                return json.loads(line).get("thread_id")
            except ValueError:
                return None
    return None


def newest_png(folder, since):
    """The newest PNG under `folder` written at or after `since`, or None."""
    if not folder or not folder.exists():
        return None
    pngs = [p for p in folder.rglob("*.png") if p.stat().st_mtime >= since - 1]
    return max(pngs, key=lambda p: p.stat().st_mtime) if pngs else None


def generate(prompt, out, refs=(), timeout=600, cmd=None):
    """Run codex once and copy the PNG this run produced to `out` (byte for byte). Returns (source, seconds);
    raises CodexError when the run timed out, could not start or made no image. Safe to call from threads."""
    cmd = cmd or codex_cmd()
    out = Path(out)
    gen_root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "generated_images"
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="codeximg_"))
    try:
        with open(work / "events.jsonl", "w", encoding="utf-8") as fo, open(work / "err.txt", "w", encoding="utf-8") as fe:
            # utf-8: the cp1252 default can't send CJK prompt text. run_tree: on a timeout the codex binary that
            # node started is ended too, not just node, so a hung run can't go on spending the quota.
            common.run_tree(cmd + exec_args(refs), timeout=timeout, input=WRAP % prompt, text=True, encoding="utf-8",
                            stdout=fo, stderr=fe, cwd=str(work))
    except subprocess.TimeoutExpired:
        raise CodexError("codex did not finish in %d s (stopped); see %s" % (timeout, work))
    except OSError as e:
        raise CodexError("can't start codex (%s): %s" % (cmd[0], e.strerror or e))
    # Take the image from THIS run's thread folder, never "the newest PNG anywhere": with several runs in
    # parallel that grabs another run's image. No thread folder = fail, and the caller retries.
    thread = thread_id((work / "events.jsonl").read_text(encoding="utf-8", errors="replace"))
    newest = newest_png(gen_root / thread if thread else None, t0)
    if not newest:
        raise CodexError("no image produced for this run's thread (%s); see %s" % (thread, work))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(newest, out)
    except OSError as e:
        raise CodexError("can't write %s: %s" % (out, e.strerror or e))
    shutil.rmtree(work, ignore_errors=True)  # kept only when the run failed, for the "see" in the message
    return newest, time.time() - t0


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.prompt.strip():
        ap.error("PROMPT is empty")
    if a.timeout < 1:
        ap.error("--timeout must be 1 or more")
    out = Path(a.out)
    refs = [Path(r).resolve() for r in a.ref]
    for r in refs:
        if not r.is_file():
            common.die("no such reference image: %s" % r)
    cmd = codex_cmd(a.codex)
    try:
        newest, seconds = generate(a.prompt, out, refs, a.timeout, cmd)
        out.with_suffix(".txt").write_text(a.prompt + "\n", encoding="utf-8")
    except CodexError as e:
        common.die(str(e))
    except OSError as e:
        common.die("can't write %s: %s" % (out, e.strerror or e))
    common.safe_stdio()
    print("saved", out, out.stat().st_size // 1024, "KB from", newest, f"in {seconds:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
