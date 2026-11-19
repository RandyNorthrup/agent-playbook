"""Recipes: the map from a kind of job to the pieces that do it, and the checks that keep the map true.

state/recipes.json holds the recipes (scripts/run.py opens a checklist from one; pb.py catalog prints them).
Everything else in a recipe's piece list is derived, so it can't go stale: the flows its gotos reach, the parts
those flows use, the scripts their nodes name, and the QC filters (qc/filters.json) that apply to its sends.
"""
import re

from . import textio

RECIPES = ("state", "recipes.json")
SCRIPT_RE = re.compile(r"\b((?:scripts|media|playbook)/[\w-]+\.py|\b(?:pb|audit|qc)\.py)\b")
PIECES = ("recipe", "flow", "part", "filter", "list", "check node", "rule", "gotcha", "script")


def load(root):
    """(recipes dict, problem or None)."""
    path = root.joinpath(*RECIPES)
    try:
        data = textio.loads(textio.read_bytes(path))
    except OSError:
        return {}, None
    except ValueError as e:
        return {}, "%s is not valid JSON: %s" % (path, e)
    recipes = data.get("recipes") if isinstance(data, dict) else None
    if not isinstance(recipes, dict):
        return {}, "%s needs a \"recipes\" object" % path
    return recipes, None


def _list(v):
    return v if isinstance(v, list) else []


def _nodes(flow):
    """A flow's nodes that are objects; nothing for a document that breaks the schema (pb.py check names it)."""
    nodes = flow.get("nodes") if isinstance(flow, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def flow_gotos(flow):
    return [n["goto"] for n in _nodes(flow) if n.get("kind") == "goto" and isinstance(n.get("goto"), str) and n["goto"]]


def reach(flows, start):
    """Flow ids reachable from `start` through goto nodes, in visit order."""
    seen, todo = [], list(start)
    while todo:
        fid = todo.pop(0)
        if fid in seen or fid not in flows:
            continue
        seen.append(fid)
        todo.extend(flow_gotos(flows[fid]))
    return seen


def node_text(n, parts):
    p = parts.get(n["part"]) if isinstance(n.get("part"), str) else None
    p = p if isinstance(p, dict) else {}
    checks = n.get("checks") or p.get("checks") or []
    bits = [n.get("title"), n.get("sub"), n.get("body"), p.get("title"), p.get("body")]
    bits += [c if isinstance(c, str) else c.get("text", "") if isinstance(c, dict) else ""
             for c in (checks if isinstance(checks, list) else [])]
    return " ".join(str(b or "") for b in bits)


def pieces(recipe, flows, parts, filters=None, lists=None, qc_applies=None):
    """Everything a recipe uses: its own flows and steps, the flows they reach, parts, scripts, QC filters."""
    steps = [s for s in _list(recipe.get("steps")) if isinstance(s, dict)]
    own = [f for f in _list(recipe.get("flows")) if isinstance(f, str)]
    named = [f for f in dict.fromkeys(own + [s["flow"] for s in steps if isinstance(s.get("flow"), str)]) if f in flows]
    linked = []  # one goto away: flows a step may send the agent to (gotos chain on to nearly everything after that)
    for fid in named:
        for g in flow_gotos(flows[fid]):
            if g in flows and g not in named and g not in linked:
                linked.append(g)
    used_parts, scripts = [], []
    for fid in named:
        for n in _nodes(flows[fid]):
            if isinstance(n.get("part"), str) and n["part"] not in used_parts:
                used_parts.append(n["part"])
            for m in SCRIPT_RE.findall(node_text(n, parts)):
                name = m.split("/")[-1]
                if name not in scripts:
                    scripts.append(name)
    for s in steps:
        if isinstance(s.get("part"), str) and s["part"] not in used_parts:
            used_parts.append(s["part"])
    qc = []
    if filters is not None and qc_applies is not None:
        for action in _list(recipe.get("actions")):
            for f in filters:
                if f["id"] not in qc and qc_applies(f, action):
                    qc.append(f["id"])
    return {"flows": named, "reached": linked, "parts": used_parts,
            "scripts": sorted(scripts), "filters": qc}


def check(root, flows, parts):
    """[(level, rule, file, path, message)]: recipes that point at nothing, and flows no recipe reaches."""
    out = []
    recipes, problem = load(root)
    rel = "/".join(RECIPES)
    if problem:
        return [("error", "recipe-file", rel, "", problem)]
    if not recipes:
        return out
    usable = {}
    for rid, r in sorted(recipes.items()):
        path = "recipes." + rid
        if not re.match(r"^[a-z][a-z0-9-]*$", rid):
            out.append(("error", "recipe-id", rel, path, "recipe ids are lower-case words joined by hyphens"))
        if not isinstance(r, dict):
            out.append(("error", "recipe-field", rel, path, "a recipe is an object with a title, when and steps"))
            continue
        for k in ("title", "when", "steps"):
            if not r.get(k):
                out.append(("error", "recipe-field", rel, path, "needs %r" % k))
        shape = [k for k in ("flows", "steps", "actions") if k in r and not isinstance(r[k], list)]
        shape += ["flows"] if isinstance(r.get("flows"), list) and not all(isinstance(f, str) for f in r["flows"]) else []
        for k in shape:
            out.append(("error", "recipe-field", rel, path + "." + k, "must be a list" + (" of flow ids" if k == "flows" else "")))
        if shape:
            continue
        bad_steps = [i for i, s in enumerate(r.get("steps", [])) if not isinstance(s, dict)]
        for i in bad_steps:
            out.append(("error", "recipe-step", rel, "%s.steps[%d]" % (path, i), "a step is an object with an id and text"))
        if bad_steps:
            continue
        usable[rid] = r
        for fid in r.get("flows", []):
            if fid not in flows:
                out.append(("error", "recipe-ref", rel, path + ".flows", "no flow %r" % fid))
        seen = set()
        for i, s in enumerate(r.get("steps", [])):
            sp = "%s.steps[%d]" % (path, i)
            if not s.get("id") or not s.get("text"):
                out.append(("error", "recipe-step", rel, sp, "a step needs an id and text"))
            sid = s.get("id") if isinstance(s.get("id"), str) else repr(s.get("id"))
            if sid in seen:
                out.append(("error", "recipe-step", rel, sp, "duplicate step id %r" % s.get("id")))
            seen.add(sid)
            for k, known in (("flow", flows), ("part", parts)):
                v = s.get(k)
                if v and (not isinstance(v, str) or v not in known):
                    out.append(("error", "recipe-ref", rel, sp, "no %s %r" % (k, v)))
            if not s.get("flow") and not s.get("part"):
                out.append(("warning", "recipe-step-ref", rel, sp, "a step names the flow or part that does it"))
    covered = set()
    for r in usable.values():
        covered.update(reach(flows, list(r.get("flows", [])) + [s["flow"] for s in r.get("steps", []) if isinstance(s.get("flow"), str)]))
    for fid in sorted(flows):
        if fid not in covered:
            out.append(("warning", "flow-no-recipe", "store/flows/%s.json" % fid, "(document)", "no recipe reaches this flow: add it to a recipe's flows, or a goto to it"))
    return out
