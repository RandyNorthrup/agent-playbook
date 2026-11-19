#!/usr/bin/env python3
"""Check a video's scene list and generate its scene images with Codex.

Two commands, each with its own help (scenes.py lint --help, scenes.py gen --help):
  scenes.py lint VIDEO_DIR [--channel NAME]     check scenes.json before any image is spent on it
  scenes.py gen VIDEO_DIR --channel NAME        write scenes/NNN.png for every scene that has none

A channel is the name of a file in media/channels/ (its style prompt, character, end card and scene count).
gen exits 1 when any wanted scene has no fresh image at the end, and refuses a bulk run when too much of
the weekly Codex limit is used.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "scripts")]
import common  # noqa: E402
import codex_img  # noqa: E402
import image_gen  # noqa: E402
import mcommon  # noqa: E402
import quota  # noqa: E402

NEEDS = ["Codex CLI (signed in) and Pillow (gen only)"]
EFFECTS = ["network", "files"]
CHANNELS = os.path.join(HERE, "channels")
MOVES = ("push_in", "pull_out", "pan_left", "pan_right", "drift_up", "still")
KEYS = {"id", "text", "prompt", "move", "label", "character", "end_card", "start", "end"}
LABEL_WORDS = 4
LABEL_SHARE = (0.15, 0.35)
HANDS = "Every hand has exactly five fingers"
WORKERS = 2
PACE_TRIES = 6
PACE_BACKOFF = 600
PACE_MAX_WAIT = 900
END_CARD_TIMEOUT = 180

USAGE = ("PROG lint VIDEO_DIR [--channel NAME] [--count MIN:MAX] [--strict] [--channels-dir DIR]\n"
         "       PROG gen VIDEO_DIR --channel NAME [--branch NAME] [--only 3,17] [--workers 2] [--tries 3] [--timeout 900] "
         "[--pace] [--backoff 600] [--max-used 80] [--no-quota] [--explainer-dir DIR] [--channels-dir DIR] [--dry-run]")

LINT_HELP = """Reads VIDEO_DIR/scenes.json, and VIDEO_DIR/words.json when it exists. Prints one line per finding
('scene 17: E5 same move as scene 16 (pan_left)') and exits 1 when there is an error; --strict fails on
warnings too. Standard library only.

Errors:
  E1  scenes.json is a JSON list of objects
  E2  id: whole numbers 1..N in order, no gaps, no repeats
  E3  text: a non-empty string (not needed on a scene with end_card: true)
  E4  prompt: a non-empty string, ASCII only, never the phrase "same as scene" (restate the props in full)
  E5  move: one of push_in, pull_out, pan_left, pan_right, drift_up, still; never the move of the scene before
  E6  label, when present: an ASCII string of at most 4 words
  E7  start and end, when set: numbers, end after start, each start equal to the end before it, the first
      start 0 (a list whose times are all 0 is not timed yet and is not checked)
  E8  with words.json: the scene texts, word for word, are the narration's words in order
  E9  an end_card scene is the last scene, and with --channel the channel has an end card
Warnings:
  W1  the share of labelled scenes is outside 15% to 35% (about 1 in 4)
  W2  the number of scenes is outside --count (default with --channel: the channel's own range)
  W3  a key other than id, text, prompt, move, label, character, end_card, start, end or prompt_qc*
"""

GEN_HELP = """Writes VIDEO_DIR/scenes/NNN.png and NNN.txt (the prompt) for every scene that has no image, with the
channel's character sheet attached, through media/image_gen.py: a run that made no image, or a flat or
undersized one, is tried again, and an image already there stays until a new one replaces it.
--only regenerates the listed scenes whether or not they have an image.
A scene with end_card: true is drawn by the channel's own end-card script, never by the image model.
Each run adds a record to VIDEO_DIR/qc/gen.json: channel, branch, the hashes of the channel file and the
character sheet, and per scene its tries, status, seconds and error.

The character sheet is <explainer-dir>/<channel>/branding/character-sheet.png, or
character-sheet-<branch>.png when that exists. The explainer folder is --explainer-dir, else
$AGENT_PLAYBOOK_EXPLAINER_DIR, else the nearest folder above VIDEO_DIR that holds <channel>/branding.
Before a run of more than 5 scenes the weekly Codex limit is read (media/quota.py): at or above --max-used
percent nothing is generated. A smaller run is allowed up to 95 percent. --no-quota skips the check.
--pace generates one scene at a time and, after a run that made no image, waits --backoff seconds (600),
doubling to at most 900, for up to 6 tries per scene. Without --pace the wait is 10 seconds.
--dry-run prints every prompt and the end-card command, and starts nothing.

Exit 1: a scene failed or an end card did not render ('3 of 60 scenes failed: 7, 19, 44'), the quota is too
high or unreadable, codex or the character sheet is missing, or scenes.json can't be used.
Exit 3: Pillow is missing.
"""


def build_parser():
    ap = common.make_parser(__doc__, __file__, USAGE)
    name = os.path.basename(__file__)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    lines = USAGE.replace("PROG", name).replace("%", "%%").split("\n       ")

    def command(word, usage, text, summary):
        return sub.add_parser(word, prog="%s %s" % (name, word), usage=usage, help=summary, description=summary,
                              epilog=text.replace("%", "%%"), formatter_class=ap.formatter_class, allow_abbrev=False)

    lint_p = command("lint", lines[0], LINT_HELP, "Check a video's scenes.json before any image is spent on it.")
    lint_p.add_argument("video_dir", metavar="VIDEO_DIR")
    lint_p.add_argument("--channel", metavar="NAME", help="the channel: its scene count and whether it has an end card")
    lint_p.add_argument("--count", metavar="MIN:MAX", help="the number of scenes expected")
    lint_p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    lint_p.add_argument("--channels-dir", default=CHANNELS, metavar="DIR", help="the channel files (default: media/channels)")

    gen_p = command("gen", lines[1], GEN_HELP, "Generate the scene images a video still needs.")
    gen_p.add_argument("video_dir", metavar="VIDEO_DIR")
    gen_p.add_argument("--channel", required=True, metavar="NAME", help="the name of a file in media/channels")
    gen_p.add_argument("--branch", metavar="NAME", help="the channel's colour branch (channels with branches, e.g. blueprint)")
    gen_p.add_argument("--only", metavar="IDS", help="regenerate these scenes, as 3,17,40")
    gen_p.add_argument("--workers", type=int, default=WORKERS, metavar="N", help="scenes generated at once (default 2)")
    gen_p.add_argument("--tries", type=int, metavar="N", help="runs per scene before giving up (default 3; 6 with --pace)")
    gen_p.add_argument("--timeout", type=int, default=image_gen.TIMEOUT, metavar="SECONDS", help="give up on one run after this long (default 900)")
    gen_p.add_argument("--pace", action="store_true", help="one scene at a time, backing off after a failed run")
    gen_p.add_argument("--backoff", type=float, metavar="SECONDS",
                       help="the wait after a run that made no image (default 10; with --pace 600, doubling to at most 900)")
    gen_p.add_argument("--max-used", type=float, default=quota.BULK_MAX_USED, metavar="PERCENT",
                       help="refuse a run of more than 5 scenes at or above this percent of the weekly limit (default 80)")
    gen_p.add_argument("--no-quota", action="store_true", help="skip the quota check")
    gen_p.add_argument("--explainer-dir", metavar="DIR", help="the explainer-channel folder (default: $AGENT_PLAYBOOK_EXPLAINER_DIR, else found above VIDEO_DIR)")
    gen_p.add_argument("--channels-dir", default=CHANNELS, metavar="DIR", help="the channel files (default: media/channels)")
    gen_p.add_argument("--dry-run", action="store_true", help="print what would be sent; start nothing")
    ap.commands = {"lint": lint_p, "gen": gen_p}
    return ap


# ---------------------------------------------------------------- channels

def channel_names(channels_dir):
    try:
        return sorted(n[:-5] for n in os.listdir(channels_dir) if n.endswith(".json"))
    except OSError:
        return []


def load_channel(name, channels_dir, error):
    """The channel file as a dict. An unknown name is a usage error that lists the channels; a file that
    isn't a channel is exit 1."""
    names = channel_names(channels_dir)
    if name not in names:
        error("unknown --channel %s (choose from: %s)" % (name, ", ".join(names) or "no channel files in %s" % channels_dir))
    path = os.path.join(channels_dir, name + ".json")
    try:
        doc = json.loads(common.read_text(path))
    except ValueError as e:
        common.die("%s is not JSON: %s" % (path, e))

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    wrong = []
    if not isinstance(doc, dict) or not text(doc.get("style")) or not text(doc.get("character")):
        common.die("%s needs the strings 'style' and 'character'" % path)
    if "hands" in doc and not text(doc["hands"]):
        wrong.append("'hands' is a string")
    if any(image_gen.non_ascii(doc[k]) for k in ("style", "character", "hands") if isinstance(doc.get(k), str)):
        wrong.append("the prompt text is ASCII only")
    branches = doc.get("branches")
    if branches is not None and not (isinstance(branches, dict) and branches and all(text(k) and text(v) for k, v in branches.items())
                                     and doc.get("default_branch") in branches):
        wrong.append("'branches' maps names to colours and 'default_branch' is one of them")
    card = doc.get("end_card")
    if card is not None and not (isinstance(card, dict) and text(card.get("script")) and os.path.basename(card["script"]) == card["script"]
                                 and isinstance(card.get("args", []), list) and all(isinstance(x, str) for x in card.get("args", []))):
        wrong.append("'end_card' is {script: a file name, args: [strings]}")
    count = doc.get("scene_count")
    if count is not None and not (isinstance(count, list) and len(count) == 2 and all(type(n) is int for n in count) and count[0] <= count[1]):
        wrong.append("'scene_count' is [min, max]")
    if wrong:
        common.die("%s: %s" % (path, "; ".join(wrong)))
    return doc


def build_prompt(scene, channel, branch=None):
    """The prompt sent for one scene: the channel's style, the scene, the character or the no-character
    sentence, the label or the no-text sentence, and the fixed closing sentences, in that order."""
    style, char = channel["style"], channel["character"]
    if channel.get("branches"):
        colour = channel["branches"][branch or channel["default_branch"]]
        style, char = style.replace("{BRANCH}", colour), char.replace("{BRANCH}", colour)
    parts = ["Landscape 16:9 (1536x1024) scene for an animated explainer video. %s" % style,
             "Scene: %s" % scene["prompt"]]
    if scene.get("character"):
        parts.append(char)
    else:
        parts.append("Use the attached sheet only as a reference for the pen style; the main character is NOT in this scene "
                     "unless the scene text says so.")
    if scene.get("label"):
        parts.append("Include this hand-lettered text, spelled exactly: \"%s\". No other text or words anywhere." % scene["label"])
    else:
        parts.append("No text, letters or words anywhere in the image.")
    parts.append("Keep the composition simple and readable, main subject centered with some empty paper around the edges. "
                 "No logos, brand names or real people's likenesses.")
    parts.append("Everything is drawn the right way up and facing the way the scene describes (screens and pages face "
                 "whoever is reading them). %s; no extra or merged limbs." % channel.get("hands", HANDS))
    return " ".join(parts)


# ---------------------------------------------------------------- lint

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def norm(token):
    return re.sub(r"[^\w%$]", "", token.lower())


def tokens(text):
    return [t for t in (norm(x) for x in text.split()) if t]


def scene_name(scene, position):
    return "scene %d" % scene["id"] if isinstance(scene, dict) and _whole(scene.get("id")) else "scene at position %d" % position


def lint(scenes, words=None, count=None, channel=None, channel_name=None):
    """The findings for a scene list, as (rule, where, message), in rule order per scene. Rules starting
    with E are errors, W warnings. `words` is the parsed words.json, or None when there is none."""
    out = []
    if not isinstance(scenes, list):
        return [("E1", "scenes.json", "not a JSON list of objects")]
    for pos, s in enumerate(scenes, 1):
        if not isinstance(s, dict):
            out.append(("E1", "scene at position %d" % pos, "not an object"))
    if out:
        return out
    last_move = last_name = None
    for pos, s in enumerate(scenes, 1):
        here = scene_name(s, pos)
        if not _whole(s.get("id")) or s["id"] != pos:
            out.append(("E2", "scene at position %d" % pos, "id is %s, expected %d" % (json.dumps(s.get("id")), pos)))
        text = s.get("text")
        if not s.get("end_card") and not (isinstance(text, str) and text.strip()):
            out.append(("E3", here, "text is missing or empty"))
        prompt = s.get("prompt")
        if not (isinstance(prompt, str) and prompt.strip()):
            out.append(("E4", here, "prompt is missing or empty"))
        else:
            bad = image_gen.non_ascii(prompt)
            if bad:
                out.append(("E4", here, "prompt is ASCII only: it has %s" % bad))
            if "same as scene" in prompt.lower():
                out.append(("E4", here, "prompt says \"same as scene\": restate the props in full"))
        move = s.get("move")
        if move not in MOVES:
            out.append(("E5", here, "move is %s (one of %s)" % (json.dumps(move), ", ".join(MOVES))))
        elif move == last_move:
            out.append(("E5", here, "same move as %s (%s)" % (last_name, move)))
        last_move, last_name = (move if move in MOVES else None), here
        if "label" in s:
            label = s["label"]
            if not (isinstance(label, str) and label.strip()):
                out.append(("E6", here, "label is not a string with words in it (leave the key out for no label)"))
            elif len(label.split()) > LABEL_WORDS:
                out.append(("E6", here, "label has %d words, at most %d: %s" % (len(label.split()), LABEL_WORDS, json.dumps(label))))
            elif image_gen.non_ascii(label):
                out.append(("E6", here, "label is ASCII only: it has %s" % image_gen.non_ascii(label)))
        if s.get("end_card"):
            if pos != len(scenes):
                out.append(("E9", here, "an end card must be the last scene"))
            elif channel is not None and not channel.get("end_card"):
                out.append(("E9", here, "is an end card, but channel %s has none" % channel_name))
        extra = sorted(k for k in s if k not in KEYS and not k.startswith("prompt_qc"))
        if extra:
            out.append(("W3", here, "unknown key%s: %s" % ("" if len(extra) == 1 else "s", ", ".join(extra))))
    out += _lint_times(scenes)
    if words is not None:
        out += _lint_words(scenes, words)
    if scenes:
        labelled = sum(1 for s in scenes if s.get("label"))
        share = labelled / float(len(scenes))
        if not LABEL_SHARE[0] <= share <= LABEL_SHARE[1]:
            out.append(("W1", "scenes.json", "%d of %d scenes have a label (%.0f%%); about 1 in 4 should (15%% to 35%%)"
                        % (labelled, len(scenes), 100 * share)))
    if count and not count[0] <= len(scenes) <= count[1]:
        out.append(("W2", "scenes.json", "%d scenes, expected %d to %d" % (len(scenes), count[0], count[1])))
    return out


def _lint_times(scenes):
    timed = [s for s in scenes if "start" in s or "end" in s]
    if not timed or all(_number(s.get("start")) and _number(s.get("end")) and s["start"] == 0 and s["end"] == 0 for s in timed):
        return []  # no times, or the planner's zeros: not timed yet
    out, before, last_name = [], None, None
    for pos, s in enumerate(scenes, 1):
        here, start, end, earlier = scene_name(s, pos), s.get("start"), s.get("end"), last_name
        last_name = here
        if not _number(start) or not _number(end):
            out.append(("E7", here, "start and end are not both numbers, though the list is timed"))
            before = None
            continue
        if end <= start:
            out.append(("E7", here, "end %s is not after start %s" % (end, start)))
        if pos == 1 and start != 0:
            out.append(("E7", here, "the first scene starts at %s, not 0" % start))
        elif before is not None and start != before:
            out.append(("E7", here, "starts at %s, but %s ends at %s" % (start, earlier, before)))
        before = end
    return out


def _lint_words(scenes, words):
    if not isinstance(words, list) or not all(isinstance(w, dict) and isinstance(w.get("w"), str) for w in words):
        return [("E8", "words.json", "not a list of {w, start, end} objects")]
    said = [w["w"] for w in words if norm(w["w"])]
    spoken = [norm(w) for w in said]
    planned = [(t, scene_name(s, pos)) for pos, s in enumerate(scenes, 1) if isinstance(s.get("text"), str) for t in tokens(s["text"])]
    for i, (x, (y, here)) in enumerate(zip(spoken, planned)):
        if x != y:
            return [("E8", here, "the scene texts leave the narration at word %d: narration \"%s\", scene text \"%s\" (narration: ... %s ...)"
                     % (i + 1, x, y, " ".join(said[max(0, i - 5):i + 6])))]
    if len(spoken) > len(planned):
        return [("E8", "scenes.json", "the narration has %d more word%s than the scene texts, from: %s"
                 % (len(spoken) - len(planned), "" if len(spoken) - len(planned) == 1 else "s", " ".join(said[len(planned):len(planned) + 5])))]
    if len(planned) > len(spoken):
        extra = planned[len(spoken):]
        return [("E8", extra[0][1], "the scene texts have %d more word%s than the narration, from: %s"
                 % (len(extra), "" if len(extra) == 1 else "s", " ".join(t for t, _here in extra[:5])))]
    return []


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def read_json(path):
    """(parsed, "") or (None, why it is not JSON). A file that can't be read is exit 1."""
    try:
        return json.loads(common.read_text(path)), ""
    except ValueError as e:
        return None, str(e)


def cmd_lint(a, error):
    count = None
    if a.count:
        m = re.match(r"^(\d+):(\d+)$", a.count)
        if not m or int(m.group(1)) > int(m.group(2)):
            error("--count is MIN:MAX, like 55:65")
        count = (int(m.group(1)), int(m.group(2)))
    channel = load_channel(a.channel, a.channels_dir, error) if a.channel else None
    if channel and not count and channel.get("scene_count"):
        count = tuple(channel["scene_count"])
    scenes, why = read_json(os.path.join(a.video_dir, "scenes.json"))
    if why:
        findings = [("E1", "scenes.json", "not JSON: %s" % why)]
    else:
        words, words_path = None, os.path.join(a.video_dir, "words.json")
        if os.path.exists(words_path):
            words, why = read_json(words_path)
            if why:
                words = "not JSON"  # reported as E8 by the shape check
        findings = lint(scenes, words, count, channel, a.channel)
    for rule, where, message in findings:
        print("%s: %s %s" % (where, rule, message))
    sys.stdout.flush()
    errors = sum(1 for f in findings if f[0].startswith("E"))
    warnings = len(findings) - errors
    summary = "%s, %s" % (plural(errors, "error"), plural(warnings, "warning"))
    if errors or (a.strict and warnings):
        common.die(summary)
    print("ok: %s, %s" % (plural(len(scenes), "scene"), summary))
    return 0


# ---------------------------------------------------------------- gen

def load_scenes(video_dir):
    """scenes.json as a list of objects with usable ids, or exit 1 (gen needs no more than that; lint checks the rest)."""
    path = os.path.join(video_dir, "scenes.json")
    scenes, why = read_json(path)
    if why:
        common.die("%s is not JSON: %s" % (path, why))
    if not isinstance(scenes, list) or not all(isinstance(s, dict) for s in scenes):
        common.die("%s is not a JSON list of objects" % path)
    seen = set()
    for pos, s in enumerate(scenes, 1):
        if not _whole(s.get("id")) or s["id"] < 1 or s["id"] in seen:
            common.die("%s: the scene at position %d has no id of its own (id %s); run scenes.py lint" % (path, pos, json.dumps(s.get("id"))))
        seen.add(s["id"])
    return scenes


def image_path(video_dir, scene_id):
    return os.path.join(video_dir, "scenes", "%03d.png" % scene_id)


def find_explainer(video_dir, channel_name):
    """The nearest folder above the video folder that holds <channel>/branding, or None."""
    folder = os.path.abspath(video_dir)
    while os.path.dirname(folder) != folder:
        folder = os.path.dirname(folder)
        if os.path.isdir(os.path.join(folder, channel_name, "branding")):
            return folder
    return None


def end_card_command(channel, channel_name, explainer, target):
    card = channel.get("end_card")
    if not card:
        return None
    return [sys.executable, os.path.join(explainer, channel_name, card["script"]), target] + list(card.get("args", []))


def render_end_card(channel, channel_name, explainer, card_path):
    """Draw the end card with the channel's script. Returns "" when the PNG is in place, else why not."""
    part = card_path[:-4] + ".part.png"
    cmd = end_card_command(channel, channel_name, explainer, part)
    if not cmd:
        return "channel %s has no end card" % channel_name
    if not os.path.isfile(cmd[1]):
        return "no end card script: %s" % cmd[1]
    try:
        if os.path.exists(part):
            os.remove(part)
        # run_tree: an end-card script that renders through Chrome is ended with its browser on a timeout
        r = common.run_tree(cmd, timeout=END_CARD_TIMEOUT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return "the end card script did not finish in %d s" % END_CARD_TIMEOUT
    except OSError as e:
        return "can't run the end card script: %s" % (e.strerror or e)
    if not os.path.isfile(part) or not os.path.getsize(part):
        said = ((r.stderr or b"") + (r.stdout or b"")).decode("utf-8", "replace").strip().splitlines()
        return "the end card script wrote no PNG%s" % (" (%s)" % said[-1][:200] if said else "")
    try:
        os.replace(part, card_path)
    except OSError as e:
        return "can't write %s: %s" % (card_path, e.strerror or e)
    return ""


def cmd_gen(a, error):
    channel = load_channel(a.channel, a.channels_dir, error)
    branches = channel.get("branches") or {}
    if a.branch and a.branch not in branches:
        error("channel %s has %s" % (a.channel, "the branches %s" % ", ".join(sorted(branches)) if branches else "no branches"))
    branch = a.branch or channel.get("default_branch")
    only = None
    if a.only is not None:
        try:
            only = mcommon.parse_ids(a.only)
        except ValueError:
            error("--only is a list of scene ids, like 3,17,40")
    tries = a.tries if a.tries is not None else (PACE_TRIES if a.pace else image_gen.TRIES)
    backoff = a.backoff if a.backoff is not None else (PACE_BACKOFF if a.pace else image_gen.WAIT)
    for name, value, least in (("workers", a.workers, 1), ("tries", tries, 1), ("timeout", a.timeout, 1), ("backoff", backoff, 0)):
        if value < least:
            error("--%s must be %d or more" % (name, least))
    if not 0 < a.max_used <= 100:
        error("--max-used is a percent from 1 to 100")
    if not os.path.isdir(a.video_dir):
        common.die("no such video folder: %s" % a.video_dir)
    explainer = a.explainer_dir or os.environ.get("AGENT_PLAYBOOK_EXPLAINER_DIR") or find_explainer(a.video_dir, a.channel)
    if not explainer:
        error("no folder above %s holds %s: name the explainer-channel folder with --explainer-dir DIR or $AGENT_PLAYBOOK_EXPLAINER_DIR"
              % (a.video_dir, os.path.join(a.channel, "branding")))
    scenes = load_scenes(a.video_dir)
    by_id = dict((s["id"], s) for s in scenes)
    if only:
        missing = [i for i in only if i not in by_id]
        if missing:
            common.die("no scene %s in %s" % (", ".join(map(str, missing)), os.path.join(a.video_dir, "scenes.json")))
    todo = [s for s in scenes if (s["id"] in only if only else not os.path.exists(image_path(a.video_dir, s["id"])))]
    cards = [s for s in todo if s.get("end_card")]
    drawn = [s for s in todo if not s.get("end_card")]

    ref = os.path.join(explainer, a.channel, "branding", "character-sheet.png")
    by_branch = os.path.join(explainer, a.channel, "branding", "character-sheet-%s.png" % branch)
    if branch and os.path.isfile(by_branch):
        ref = by_branch
    prompts = {}
    for s in drawn:
        if not (isinstance(s.get("prompt"), str) and s["prompt"].strip()):
            common.die("scene %d has no prompt; run scenes.py lint" % s["id"])
        for field in ("prompt", "label"):
            bad = image_gen.non_ascii(s[field]) if isinstance(s.get(field), str) else ""
            if bad:
                common.die("scene %d: the %s is ASCII only: it has %s; run scenes.py lint" % (s["id"], field, bad))
        prompts[s["id"]] = build_prompt(s, channel, branch)
    if drawn and not os.path.isfile(ref):
        common.die("no character sheet: %s" % ref)

    print("%d of %s to generate%s" % (len(todo), plural(len(scenes), "scene"),
                                      "" if not drawn else ", one at a time" if a.pace else ", %d at a time" % a.workers))
    if a.dry_run:
        for s in todo:
            if s.get("end_card"):
                cmd = end_card_command(channel, a.channel, explainer, image_path(a.video_dir, s["id"]))
                print("scene %03d: end card: %s" % (s["id"], " ".join(cmd) if cmd else "channel %s has no end card" % a.channel))
            else:
                print("scene %03d: %s" % (s["id"], prompts[s["id"]]))
        if drawn:
            print("reference:", ref)
        return 0
    if not todo:
        return 0

    used, cmd = None, None
    if drawn:
        mcommon.pillow()
        cmd = codex_img.codex_cmd()
        if not a.no_quota:
            bulk = len(drawn) > quota.BULK_OVER
            try:
                used = quota.check(a.max_used if bulk else max(a.max_used, quota.SINGLE_MAX_USED), bulk=bulk)["used_percent"]
            except quota.QuotaError as e:
                common.die(str(e))
    try:
        os.makedirs(os.path.join(a.video_dir, "scenes"), exist_ok=True)
        os.makedirs(os.path.join(a.video_dir, "qc"), exist_ok=True)
    except OSError as e:
        common.die("can't make the scenes and qc folders in %s: %s" % (a.video_dir, e.strerror or e))

    lock = threading.Lock()
    results = {}
    done = [0]

    def say(text):
        with lock:
            print(text)
            sys.stdout.flush()

    def finish(scene_id, record):
        with lock:
            results[scene_id] = record
            done[0] += 1
            n = done[0]
        say("[%d/%d] scene %03d %s" % (n, len(todo), scene_id, "ok" if record["status"] == "ok" else "FAILED: %s" % record["error"]))

    def one(s):
        try:
            wait = (lambda n: min(PACE_MAX_WAIT, backoff * 2 ** (n - 1))) if a.pace else backoff
            got = image_gen.make(prompts[s["id"]], image_path(a.video_dir, s["id"]), [ref], cmd, tries=tries, timeout=a.timeout,
                                 wait=wait, log=say)
            record = {"id": s["id"], "tries": got["tries"], "status": "ok" if got["accepted"] else "failed",
                      "seconds": got["seconds"], "error": "; ".join(got["rejected"]) or None}
        except Exception as e:  # a worker must always report: a scene that vanishes from the list would read as done
            record = {"id": s["id"], "tries": 0, "status": "failed", "seconds": 0.0, "error": "%s: %s" % (type(e).__name__, e)}
        finish(s["id"], record)

    run = {"started": common.now_utc().isoformat(timespec="seconds"), "channel": a.channel, "branch": branch,
           "channel_sha256": mcommon.sha256(os.path.join(a.channels_dir, a.channel + ".json")),
           "reference": ref if drawn else None, "reference_sha256": mcommon.sha256(ref) if drawn else None,
           "only": only, "pace": bool(a.pace), "quota_used_percent": used, "scenes": []}
    log_path = os.path.join(a.video_dir, "qc", "gen.json")
    try:
        for s in cards:
            t0 = datetime.datetime.now()
            why = render_end_card(channel, a.channel, explainer, image_path(a.video_dir, s["id"]))
            finish(s["id"], {"id": s["id"], "end_card": True, "tries": 1, "status": "failed" if why else "ok",
                             "seconds": round((datetime.datetime.now() - t0).total_seconds(), 1), "error": why or None})
        if drawn:
            with ThreadPoolExecutor(1 if a.pace else a.workers) as pool:
                list(pool.map(one, drawn))
    finally:
        for s in todo:  # a scene with no result (the run was stopped) is recorded as not done
            run["scenes"].append(results.get(s["id"]) or {"id": s["id"], "tries": 0, "status": "failed", "seconds": 0.0, "error": "the run stopped before this scene"})
        try:
            runs = []
            if os.path.exists(log_path):
                runs, why = read_json(log_path)
                if why or not isinstance(runs, list):
                    os.replace(log_path, log_path[:-5] + ".unreadable.json")
                    runs = []
            mcommon.write_json(log_path, runs + [run])
        except OSError as e:
            common.warn("can't write %s: %s" % (log_path, e.strerror or e))
    failed = sorted(r["id"] for r in run["scenes"] if r["status"] != "ok")
    if failed:
        common.die("%d of %s failed: %s (%s has the reasons)" % (len(failed), plural(len(todo), "scene"), ", ".join(map(str, failed)),
                                                                 os.path.join("qc", "gen.json")))
    print("%s ok" % plural(len(todo), "scene"))
    return 0


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    common.safe_stdio()  # scene text and narration hold characters a cp1252 console can't show
    try:
        return {"lint": cmd_lint, "gen": cmd_gen}[a.command](a, ap.commands[a.command].error)
    except KeyboardInterrupt:
        common.die("stopped")


if __name__ == "__main__":
    sys.exit(main())
