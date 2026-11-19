"""Split one flow into smaller single-job flows that can be run alone or chained (pb.py split).

A plan names the new flows and the nodes each one takes; every other node stays in the original flow, which
becomes the hub. Notes attached to a node (rules, tools, gotchas) move with it. A connection that ends up
crossing from one flow to another becomes a goto node, so nothing is lost and every path still leads on:
the goto's title is the node the path continued at. Each new flow gets a start node leading to its entry.

Plan (JSON):
  {"flows": {
     "<new-id>": {"name": "Facebook: post in a group", "summary": "...", "entry": "<node id>", "entry_label": "...",
                  "also": {"<node id>": "label"},   (optional: more ways in, each a branch from the start node)
                  "start": {"title": "...", "sub": "...", "body": "..."}, "nodes": ["<node id>", ...]},
     ...},
   "hub": {"summary": "the original flow's new summary (optional)"}}
"""
import copy

from . import ids

GAP_X, GAP_Y = 320, 200


class PlanError(ValueError):
    pass


def check_plan(plan, used_ids):
    """PlanError, naming the flow and the field, unless the plan has the shape split() reads and every new flow
    id follows the id rule and is free."""
    if not isinstance(plan, dict) or not isinstance(plan.get("flows"), dict) or not plan["flows"]:
        raise PlanError("the plan needs a \"flows\" object (lib/split.py describes the plan)")
    if not isinstance(plan.get("hub", {}), dict):
        raise PlanError("\"hub\" must be an object")
    for fid, spec in plan["flows"].items():
        why = ids.id_problem(fid, "flows")
        if why:
            raise PlanError("flow id %r: %s" % (fid, why))
        if fid in used_ids:
            raise PlanError("a flow %r already exists" % fid)
        if not isinstance(spec, dict):
            raise PlanError("%s: must be an object with a name, an entry and its nodes" % fid)
        if not isinstance(spec.get("name"), str) or not spec["name"].strip():
            raise PlanError("%s: needs a name" % fid)
        nodes = spec.get("nodes", [])
        if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
            raise PlanError("%s: nodes must be a list of node ids" % fid)
        if not isinstance(spec.get("entry"), str):
            raise PlanError("%s: its entry %r must be one of its nodes" % (fid, spec.get("entry")))
        for key in ("start", "also"):
            if key in spec and not isinstance(spec[key], dict):
                raise PlanError("%s: %s must be an object" % (fid, key))
        for key in ("summary", "entry_label"):
            if key in spec and not isinstance(spec[key], str):
                raise PlanError("%s: %s must be text" % (fid, key))


def _attached(flow):
    """{node id: [ids of notes attached to it]} (attach edges run note -> node)."""
    out = {}
    for e in flow.get("edges", []):
        if e.get("type") == "attach":
            out.setdefault(e["to"], []).append(e["from"])
    return out


def assign(flow, plan):
    """{node id: flow id it ends up in}; notes follow the node they are attached to."""
    ids = [n["id"] for n in flow["nodes"]]
    home = dict((i, None) for i in ids)
    for fid, spec in plan["flows"].items():
        for nid in spec.get("nodes", []):
            if nid not in home:
                raise PlanError("%s: no node %r in the flow" % (fid, nid))
            if home[nid]:
                raise PlanError("node %r is in two plans (%s, %s)" % (nid, home[nid], fid))
            home[nid] = fid
        if spec.get("entry") not in spec.get("nodes", []):
            raise PlanError("%s: its entry %r must be one of its nodes" % (fid, spec.get("entry")))
    notes = _attached(flow)
    changed = True
    while changed:  # a note attached to a moved node moves too (notes can hang off notes)
        changed = False
        for target, ns in notes.items():
            for n in ns:
                if home.get(target) and home.get(n) is None:
                    home[n] = home[target]
                    changed = True
    return home


def _title(flow, nid):
    for n in flow["nodes"]:
        if n["id"] == nid:
            return n.get("title") or nid
    return nid


def _resolved_title(flow, nid, parts):
    for n in flow["nodes"]:
        if n["id"] == nid:
            if n.get("title"):
                return n["title"]
            if n.get("part") and n["part"] in parts:
                return parts[n["part"]].get("title") or nid
    return nid


def split(flow_id, flow, plan, parts, used_ids):
    """{flow id: new flow document} for the hub (flow_id) and every new flow in the plan."""
    check_plan(plan, used_ids)
    home = assign(flow, plan)
    where = dict((nid, h or flow_id) for nid, h in home.items())
    names = dict((fid, spec["name"]) for fid, spec in plan["flows"].items())
    names[flow_id] = flow.get("name", flow_id)
    out = {}
    order = flow.get("order", 50)
    for k, fid in enumerate([flow_id] + list(plan["flows"])):
        spec = plan["flows"].get(fid, {})
        doc = dict((key, copy.deepcopy(v)) for key, v in flow.items() if key not in ("nodes", "edges", "editedAt", "origin", "copiedFrom"))
        if fid != flow_id:
            doc.update({"name": spec["name"], "summary": spec.get("summary", ""), "order": order + (k * 0.1 if isinstance(order, float) else 0)})
        elif plan.get("hub", {}).get("summary"):
            doc["summary"] = plan["hub"]["summary"]
        doc["nodes"] = [copy.deepcopy(n) for n in flow["nodes"] if where[n["id"]] == fid]
        doc["edges"] = []
        out[fid] = doc
    gotos, copied = {}, set()
    for e in flow.get("edges", []):
        a, b = where.get(e["from"]), where.get(e["to"])
        if a is None or b is None:
            continue
        if a == b:
            out[a]["edges"].append(copy.deepcopy(e))
            continue
        if e.get("type") == "attach":
            raise PlanError("note %s is attached across flows (%s -> %s)" % (e["from"], a, b))
        target = next(x for x in flow["nodes"] if x["id"] == e["to"])
        if target.get("kind") in ("output", "goto"):  # an ending (a result or a goto): each flow gets its own copy
            if not any(x["id"] == e["to"] for x in out[a]["nodes"]):
                src = next(x for x in out[a]["nodes"] if x["id"] == e["from"])
                out[a]["nodes"].append(dict(copy.deepcopy(target), x=src.get("x", 0) + GAP_X, y=src.get("y", 0)))
                copied.add((b, e["to"]))
            out[a]["edges"].append(copy.deepcopy(e))
            continue
        key = (a, b, e["to"])
        if key not in gotos:
            gid = "%s-to-%s" % (e["from"], b)
            n = 2
            while any(x["id"] == gid for x in out[a]["nodes"]):
                gid = "%s-to-%s-%d" % (e["from"], b, n)
                n += 1
            src = next(x for x in out[a]["nodes"] if x["id"] == e["from"])
            target_title = _resolved_title(flow, e["to"], parts)
            entry = plan["flows"].get(b, {}).get("entry")
            sub = "From its start" if e["to"] == entry else "At: %s" % target_title
            out[a]["nodes"].append({"id": gid, "kind": "goto", "title": names[b], "sub": sub[:120], "goto": b,
                                    "src": ["split of flow %s" % flow_id],
                                    "x": src.get("x", 0) + GAP_X, "y": src.get("y", 0) - GAP_Y // 2})
            gotos[key] = gid
        ne = {"from": e["from"], "to": gotos[key], "type": "flow"}
        if e.get("label"):
            ne["label"] = e["label"]
        out[a]["edges"].append(ne)
    for fid, nid in copied:  # an ending copied out that nothing in its own flow leads to any more
        doc = out[fid]
        if not any(e["to"] == nid for e in doc["edges"]):
            doc["nodes"] = [n for n in doc["nodes"] if n["id"] != nid]
            doc["edges"] = [e for e in doc["edges"] if e["from"] != nid]
    for fid, spec in plan["flows"].items():
        doc = out[fid]
        entry = next(n for n in doc["nodes"] if n["id"] == spec["entry"])
        dx, dy = GAP_X - entry.get("x", 0), -entry.get("y", 0)
        for n in doc["nodes"]:
            n["x"], n["y"] = n.get("x", 0) + dx, n.get("y", 0) + dy
        st = spec.get("start", {})
        sid = "%s-start" % fid
        node = {"id": sid, "kind": "start", "title": st.get("title") or spec["name"], "x": 0, "y": 0,
                "src": ["split of flow %s" % flow_id]}
        for k in ("sub", "body"):
            if st.get(k):
                node[k] = st[k]
        doc["nodes"].insert(0, node)
        first = {"from": sid, "to": spec["entry"], "type": "flow"}
        if spec.get("entry_label"):
            first["label"] = spec["entry_label"]
        doc["edges"].insert(0, first)
        for nid, label in (spec.get("also") or {}).items():  # more ways in: the start node branches to each
            if nid not in [n["id"] for n in doc["nodes"]]:
                raise PlanError("%s: also-entry %r is not one of its nodes" % (fid, nid))
            doc["edges"].insert(1, {"from": sid, "to": nid, "type": "flow", "label": label})
    return out
