#!/usr/bin/env python3
"""Generate one image with Codex behind a quota check, and accept it only when it is the image asked for.

  image_gen.py "PROMPT" OUT.png [--ref IMAGE ...] [--aspect 16:9] [--alpha]

Runs the Codex image run of scripts/codex_img.py, then checks the result before it becomes OUT.png: it opens
as an image, its shorter side is at least --min-side, its shape is within 5 percent of --aspect, it is not
one flat colour, and with --alpha it has real transparency (not a painted checkerboard). A result that
fails is kept as OUT.rejected-N.png and the run is tried again, up to --tries times; after a try that made
no image at all the tool waits --wait seconds first. An image already at OUT.png stays until an accepted
one replaces it.

Writes OUT.png, OUT.txt (the prompt) and OUT.gen.json (prompt hash, the references and their hashes, tries,
seconds, the quota reading, and why each failed try was rejected).
Before the first try the weekly Codex limit is read (media/quota.py): at or above --max-used percent, or
with no reading to trust, nothing is generated. --no-quota skips the check.
--dry-run prints the prompt as it is sent, the references and the codex command, and starts nothing.
The prompt is ASCII only. The codex binary is --codex, else $CODEX_BIN, else codex on PATH.
Exit 1: the quota is too high or unreadable, codex is missing, a reference is missing, or no try was accepted.
Exit 3: Pillow is missing.
"""
import hashlib
import os
import sys
import time
import unicodedata
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "scripts")]
import common  # noqa: E402
import codex_img  # noqa: E402
import mcommon  # noqa: E402
import quota  # noqa: E402

NEEDS = ["Codex CLI (signed in)", "Pillow"]
EFFECTS = ["network", "files"]
ASPECTS = {"16:9": 16 / 9.0, "3:2": 3 / 2.0, "1:1": 1.0, "9:16": 9 / 16.0, "any": None}
ASPECT_SLACK = 0.05
MIN_SIDE = 800
TRIES = 3
TIMEOUT = 900
WAIT = 10.0


def build_parser():
    ap = common.make_parser(__doc__, __file__, 'PROG "PROMPT" OUT.png [--ref IMAGE ...] [--aspect 16:9|3:2|1:1|9:16|any] '
                            "[--min-side 800] [--alpha] [--tries 3] [--timeout 900] [--wait 10] [--max-used 95] [--no-quota] "
                            "[--dry-run] [--codex PATH]")
    ap.add_argument("prompt", metavar="PROMPT")
    ap.add_argument("out", metavar="OUT.png")
    ap.add_argument("--ref", action="append", default=[], metavar="IMAGE", help="a reference image; repeatable")
    ap.add_argument("--aspect", choices=sorted(ASPECTS), default="any",
                    help="the shape wanted; a result more than 5 percent off is rejected (default any)")
    ap.add_argument("--min-side", type=int, default=MIN_SIDE, metavar="PIXELS", help="the shorter side at least this (default 800)")
    ap.add_argument("--alpha", action="store_true", help="require real transparency")
    ap.add_argument("--tries", type=int, default=TRIES, metavar="N", help="runs before giving up (default 3)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT, metavar="SECONDS", help="give up on one run after this long (default 900)")
    ap.add_argument("--wait", type=float, default=WAIT, metavar="SECONDS", help="pause after a run that made no image (default 10)")
    ap.add_argument("--max-used", type=float, default=quota.SINGLE_MAX_USED, metavar="PERCENT",
                    help="generate nothing at or above this percent of the weekly limit (default 95)")
    ap.add_argument("--no-quota", action="store_true", help="skip the quota check")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent; start nothing")
    ap.add_argument("--codex", metavar="PATH", help="the codex binary (default: $CODEX_BIN, else codex on PATH)")
    return ap


def non_ascii(text):
    """Where the first character outside ASCII is, as 'U+2019 RIGHT SINGLE QUOTATION MARK at position 12',
    or "" when there is none."""
    for i, ch in enumerate(text, 1):
        if ord(ch) > 127:
            return "U+%04X %s at position %d" % (ord(ch), unicodedata.name(ch, "(unnamed)"), i)
    return ""


def problem(path, aspect=None, min_side=MIN_SIDE, alpha=False):
    """Why the image at `path` is not the one asked for, or "" when it is. `aspect` is width / height."""
    from PIL import Image
    try:
        with Image.open(str(path)) as im:
            im.load()
            w, h = im.size
            if min(w, h) < min_side:
                return "%dx%d, shorter side under %d" % (w, h, min_side)
            if aspect and abs(w / float(h) - aspect) > ASPECT_SLACK * aspect:
                return "aspect %.2f, wanted %.2f" % (w / float(h), aspect)
            if mcommon.is_flat(im):
                return "one flat colour"
            return mcommon.alpha_problem(im) if alpha else ""
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        return "not a readable image (%s)" % (str(e).splitlines() or [type(e).__name__])[0]


def _free_name(out):
    n = 1
    while out.with_name("%s.rejected-%d.png" % (out.stem, n)).exists():
        n += 1
    return out.with_name("%s.rejected-%d.png" % (out.stem, n))


def make(prompt, out, refs=(), cmd=None, aspect=None, min_side=MIN_SIDE, alpha=False, tries=TRIES, timeout=TIMEOUT,
         wait=WAIT, sleep=time.sleep, log=None):
    """Try up to `tries` Codex runs until one gives an image that passes problem(); only then is `out`
    replaced and its .txt written. Returns {accepted, tries, seconds, rejected: [reason per failed try],
    size}. `wait` is the pause after a run that made no image: seconds, or a function of the try number.
    Never exits and never raises for a failed run, so it is safe to call from worker threads."""
    out = Path(out)
    part = out.with_name(out.stem + ".part.png")
    record = {"accepted": False, "tries": 0, "seconds": 0.0, "rejected": [], "size": None}
    t0 = time.time()
    for n in range(1, tries + 1):
        record["tries"] = n
        try:
            if part.exists():
                part.unlink()
            codex_img.generate(prompt, part, refs, timeout, cmd)
        except (codex_img.CodexError, OSError) as e:
            record["rejected"].append("try %d: %s" % (n, e))
            if n < tries:
                pause = wait(n) if callable(wait) else wait
                if log and pause:
                    log("%s: %s; waiting %g s" % (out.name, record["rejected"][-1], pause))
                sleep(pause)
            continue
        why = problem(part, aspect, min_side, alpha)
        try:
            if why:
                kept = _free_name(out)
                os.replace(str(part), str(kept))
                record["rejected"].append("try %d: %s" % (n, why))
                if log:
                    log("%s: %s; kept as %s" % (out.name, record["rejected"][-1], kept.name))
                continue
            from PIL import Image
            with Image.open(str(part)) as im:
                record["size"] = list(im.size)
            os.replace(str(part), str(out))
            out.with_suffix(".txt").write_text(prompt + "\n", encoding="utf-8")
        except OSError as e:
            record["rejected"].append("try %d: can't write beside %s: %s" % (n, out, e.strerror or e))
            continue
        record["accepted"] = True
        break
    record["seconds"] = round(time.time() - t0, 1)
    return record


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.prompt.strip():
        ap.error("PROMPT is empty")
    bad = non_ascii(a.prompt)
    if bad:
        ap.error("PROMPT is ASCII only: it has %s" % bad)
    out = Path(a.out)
    if out.suffix.lower() != ".png":
        ap.error("OUT must end in .png")
    for name, least in (("tries", 1), ("timeout", 1), ("min_side", 1), ("wait", 0)):
        if getattr(a, name) < least:
            ap.error("--%s must be %d or more" % (name.replace("_", "-"), least))
    if not 0 < a.max_used <= 100:
        ap.error("--max-used is a percent from 1 to 100")
    common.safe_stdio()
    refs = [Path(r).resolve() for r in a.ref]
    for r in refs:
        if not r.is_file():
            common.die("no such reference image: %s" % r)
    if a.dry_run:
        print("prompt:", codex_img.WRAP % a.prompt)
        for r in refs:
            print("reference:", r)
        print("command:", " ".join((codex_img.find_codex(a.codex) or ["codex"]) + codex_img.exec_args(refs)),
              "(the prompt goes in on standard input)")
        return 0
    mcommon.pillow()
    cmd = codex_img.codex_cmd(a.codex)
    used = None
    if not a.no_quota:
        try:
            used = quota.check(a.max_used, bulk=False)["used_percent"]
        except quota.QuotaError as e:
            common.die(str(e))
    def say(text):
        print(text)
        sys.stdout.flush()

    record = make(a.prompt, out, refs, cmd, ASPECTS[a.aspect], a.min_side, a.alpha, a.tries, a.timeout, a.wait, log=say)
    sidecar = {"prompt_sha256": hashlib.sha256(a.prompt.encode("utf-8")).hexdigest(),
               "refs": [{"file": str(r), "sha256": mcommon.sha256(str(r))} for r in refs],
               "tries": record["tries"], "seconds": record["seconds"], "quota_used_percent": used,
               "rejected": record["rejected"], "accepted": record["accepted"], "size": record["size"]}
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        mcommon.write_json(out.with_suffix(".gen.json"), sidecar)
    except OSError as e:
        common.die("can't write %s: %s" % (out.with_suffix(".gen.json"), e.strerror or e))
    if not record["accepted"]:
        common.die("no accepted image after %d tr%s: %s" % (record["tries"], "y" if record["tries"] == 1 else "ies",
                                                             record["rejected"][-1]))
    print("saved %s %dx%d in %.0f s, try %d" % (out, record["size"][0], record["size"][1], record["seconds"], record["tries"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
