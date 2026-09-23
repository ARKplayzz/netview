"""NetView — P45 estate inventory.

A read-mostly view of every node in the lab: what it is, what it is called in
DNS, what addresses it holds, which layer it sits in, whether it is running, and
whether any of that still matches the manifest.  Plus one active tool: pick two
nodes and measure the path between them.

Standalone by design.  It shares the data files and the `cml` skill's API/console
helpers with the rest of the repo and nothing else; the older dashboard under
`dashboard/` is not imported, extended, or depended on.

Run:
    py -3.11 netview/app.py
    http://127.0.0.1:5057
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / ".claude" / "skills" / "cml" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import cml_api          # noqa: E402
import cmlsource        # noqa: E402
import features         # noqa: E402
import model            # noqa: E402
import pathtrace        # noqa: E402
import probes           # noqa: E402

app = Flask(__name__)

PORT = 5057
TOPOLOGY_TTL_S = 5       # how long a live topology pull is reused; short, so a
                         # change made in CML shows on the page's next refresh
STATE_TTL_S = 4          # how long node states are reused

_cache: dict = {"topology": None, "topology_at": 0, "topology_source": None,
                "states": None, "states_at": 0, "error": None}
_cache_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _fetch_topology(force: bool = False) -> tuple[dict, str, str | None]:
    """Live topology if the lab is reachable, else the cached snapshot.

    The cached file under data/ is what render_configs builds from, and it is
    only as fresh as the last `refresh_topology.py --write`.  Serving it without
    saying so would let this dashboard describe an estate that no longer exists —
    the snapshot in this repo was a day behind the lab and six nodes short of it.
    So the source is always reported alongside the data.
    """
    with _cache_lock:
        if not force and _cache["topology"] and _now() - _cache["topology_at"] < TOPOLOGY_TTL_S:
            return _cache["topology"], _cache["topology_source"], _cache["error"]

    topo, source, error = None, None, None
    try:
        cml = cml_api.connect()
        topo = cml.topology()
        source = "live"
    except Exception as e:                     # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        try:
            topo = model.load_topology()
            source = "snapshot"
        except Exception as e2:                # noqa: BLE001
            error = f"{error}; snapshot unreadable: {e2}"
            topo = {"nodes": [], "links": []}
            source = "none"

    with _cache_lock:
        _cache.update({"topology": topo, "topology_at": _now(),
                       "topology_source": source, "error": error})
    return topo, source, error


def _fetch_states(force: bool = False) -> tuple[dict[str, str], str | None]:
    """label -> state, from the live lab.  Empty dict if the lab is unreachable."""
    with _cache_lock:
        if not force and _cache["states"] is not None and _now() - _cache["states_at"] < STATE_TTL_S:
            return _cache["states"], None
    try:
        cml = cml_api.connect()
        full = cml.nodes_full()
        states = {n["label"].rstrip(): n.get("state") for n in full.values()}
        err = None
    except Exception as e:                     # noqa: BLE001
        states, err = {}, f"{type(e).__name__}: {e}"
    with _cache_lock:
        _cache.update({"states": states, "states_at": _now()})
    return states, err


def _snapshot_age() -> dict:
    p = model.TOPOLOGY
    if not p.exists():
        return {"exists": False}
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return {"exists": True, "mtime": mtime.isoformat(),
            "age_hours": round((datetime.now(timezone.utc) - mtime).total_seconds() / 3600, 1)}


def _live_primary(n: dict) -> str | None:
    """The best live address to reach this node on.

    Preference order:
      1. the interface carrying the manifest's PRIMARY address specifically —
         not just any manifest-known address. A router's Loopback0 is also a
         manifest address (the OSPF router-id), and it is very often listed
         BEFORE the management SVI in `show ip interface brief` output. Matching
         "any manifest IP" picked Loopback0 for every core/dist/edge router here
         and reported it as the live address, which then diffed against the
         mgmt-IP primary and looked like drift on 19 healthy nodes — a false
         positive from the harvest's own comparison, not a finding about the lab.
      2. any other address the manifest also knows about, so a node addressed
         only by its service IP (no separate mgmt_ip) still matches
      3. the first up interface with any address, for an undeclared node
    """
    live = n.get("live") or {}
    addrs = [a for a in (live.get("addresses") or []) if a.get("proto") == "up"]
    if not addrs:
        return None
    primary = n.get("primary_ip")
    for a in addrs:
        if a["ip"] == primary:
            return a["ip"]
    manifest_ips = {a["value"] for a in (n.get("addresses") or [])}
    for a in addrs:
        if a["ip"] in manifest_ips:
            return a["ip"]
    return addrs[0]["ip"]


def _inventory(force: bool = False) -> dict:
    topo, source, topo_error = _fetch_topology(force)
    states, state_error = _fetch_states(force)
    inv = model.build_inventory(topology=topo)

    # CML is the source of truth for what each node is and holds; the
    # manifest values survive only as `declared`, for comparison.
    cml_reads: dict = {}
    lab_at = None
    if source == "live":
        try:
            cml = cml_api.connect()
            lab = cmlsource.fetch(cml, force)
            configs, cfg_err = cmlsource.node_configs(cml, topo.get("nodes", []), force)
            cml_reads = dict(lab["reads"], topology=None, configs=cfg_err)
            lab_at = lab["at"]
            cmlsource.apply(inv, topo, lab, configs, model.ROLE_LAYER, model.LAYER_TITLE,
                            model.ROLE_TITLE)
        except Exception as e:                 # noqa: BLE001
            cml_reads = {"topology": None, "detail": f"{type(e).__name__}: {e}"}
    else:
        cml_reads = {"topology": topo_error or "unreachable"}

    try:
        issues = features.issues_by_node([n["label"] for n in inv["nodes"]])
    except Exception:                          # noqa: BLE001
        issues = {}

    harvested = features.harvest_nodes()

    up = 0
    for n in inv["nodes"]:
        n["state"] = states.get(n["label"])
        n["up"] = n["state"] in ("STARTED", "BOOTED")
        n["issues"] = issues.get(n["label"], [])
        # Live addresses read off the device outrank the manifest everywhere
        # they exist — the manifest says what SHOULD be, the device says what
        # IS.  Each record keeps its read time so staleness is visible.
        n["live"] = harvested.get(n["label"])
        n["live_ip"] = _live_primary(n)
        if n["up"]:
            up += 1

    inv["meta"] = {
        "topology_source": source,
        "topology_error": topo_error,
        "state_error": state_error,
        "snapshot": _snapshot_age(),
        "generated": datetime.now(timezone.utc).isoformat(),
        "harvest": features.harvest_status(include_nodes=False),
        "counts": {
            "total": len(inv["nodes"]),
            "up": up,
            "down": len(inv["nodes"]) - up,
            "undeclared": sum(1 for n in inv["nodes"] if n["config_status"] == "undeclared"),
            "absent": len(inv["ghosts"]),
        },
        "refresh_seconds": STATE_TTL_S,
        "cml_reads": cml_reads,
        "lab_read_at": lab_at,
        "manifest": model.MANIFEST.exists(),
    }
    return inv


def _address_owners(nodes: list[dict]) -> dict[str, str]:
    """ip -> node label, from every address the inventory knows (CML, config,
    device reads, manifest).  Used to name traceroute hops."""
    out: dict[str, str] = {}
    for n in nodes:
        for a in (n.get("live") or {}).get("addresses") or []:
            out.setdefault(a["ip"], n["label"])
        for a in n.get("addresses") or []:
            if a.get("kind") and "virtual" in str(a.get("kind")):
                continue          # an HSRP VIP belongs to whichever peer is active
            out.setdefault(a["value"], n["label"])
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/inventory")
def api_inventory():
    return jsonify(_inventory(force=request.args.get("fresh") == "1"))


@app.get("/api/state")
def api_state():
    """Cheap poll: just the live states, for the auto-refresh tick."""
    states, err = _fetch_states()
    up = sum(1 for s in states.values() if s in ("STARTED", "BOOTED"))
    return jsonify({
        "states": states,
        "error": err,
        "counts": {"total": len(states), "up": up, "down": len(states) - up},
        "at": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/api/probe")
def api_probe():
    body = request.get_json(force=True) or {}
    src_label = (body.get("source") or "").strip()
    dst_label = (body.get("target") or "").strip()
    methods = [m for m in (body.get("methods") or ["ip"]) if m in ("ip", "dns")]
    count = max(1, min(int(body.get("count", 5)), 50))

    inv = _inventory()
    by_label = {n["label"]: n for n in inv["nodes"]}
    source, target = by_label.get(src_label), by_label.get(dst_label)

    if not source or not target:
        return jsonify({"error": "unknown source or target node"}), 404
    if not source["can_source_probe"]:
        return jsonify({"error": f"{src_label} has no console this tool can drive "
                                 f"({source['node_definition']}), so it cannot be "
                                 f"a probe source. It can still be a target."}), 400
    if not source["up"]:
        return jsonify({"error": f"{src_label} is {source['state'] or 'not running'} "
                                 f"— a probe needs a booted source."}), 409

    # Target preference: what the user picked > the address the DEVICE holds
    # right now > the manifest's declaration.  A DHCP desktop has no manifest
    # address at all, and a renumbered device's manifest address is a stale
    # target — the live read covers both.
    target_ip = (body.get("target_ip") or target.get("live_ip")
                 or target.get("primary_ip"))
    if "ip" in methods and not target_ip:
        return jsonify({"error": f"{dst_label} has no live-read address and none "
                                 f"in the manifest — run an address harvest, or "
                                 f"give an explicit target_ip."}), 400

    vantage = None
    if "dns" in methods:
        vantage = probes.pick_dns_vantage(source, inv["nodes"], source.get("site"))

    job_id = probes.submit({
        "source": source, "target": target, "methods": methods,
        "count": count, "target_ip": target_ip, "vantage": vantage,
    })
    job = probes.get(job_id)
    return jsonify({
        "job_id": job_id,
        "eta_seconds": job["eta_seconds"],
        "vantage": vantage["label"] if vantage else None,
        "target_ip": target_ip,
    })


@app.get("/api/probe/<job_id>")
def api_probe_get(job_id: str):
    job = probes.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    # The spec holds two whole node dicts; strip them so the poll stays small.
    out = {k: v for k, v in job.items() if k != "spec"}
    out["source"] = job["spec"]["source"]["label"]
    out["target"] = job["spec"]["target"]["label"]
    return jsonify(out)


@app.get("/api/path")
def api_path():
    """Predicted paths only (no console): every shortest path over running nodes."""
    inv = _inventory()
    return jsonify(pathtrace.candidates(inv["nodes"], request.args.get("source", ""),
                                        request.args.get("target", "")))


@app.post("/api/trace")
def api_trace():
    body = request.get_json(force=True) or {}
    inv = _inventory(force=True)       # trace against the lab as it is right now
    by_label = {n["label"]: n for n in inv["nodes"]}
    source, target = by_label.get(body.get("source", "")), by_label.get(body.get("target", ""))
    if not source or not target:
        return jsonify({"error": "unknown source or target node"}), 404
    if not source["can_source_probe"]:
        return jsonify({"error": f"{source['label']} has no console this tool can drive, "
                                 f"so it cannot send the test traffic."}), 400
    if not source["up"]:
        return jsonify({"error": f"{source['label']} is {source['state'] or 'not running'}."}), 409
    target_ip = body.get("target_ip") or target.get("live_ip") or target.get("primary_ip")
    if not target_ip:
        return jsonify({"error": f"No address known for {target['label']}."}), 400
    count = max(20, min(int(body.get("count", 100)), 500))
    job_id = pathtrace.submit({"source": source, "target": target, "target_ip": target_ip,
                               "count": count, "nodes": inv["nodes"],
                               "owners": _address_owners(inv["nodes"])})
    job = pathtrace.get(job_id)
    return jsonify({"job_id": job_id, "eta_seconds": job["eta_seconds"], "target_ip": target_ip})


@app.get("/api/trace/<job_id>")
def api_trace_get(job_id: str):
    job = pathtrace.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({k: v for k, v in job.items() if k != "spec"})


@app.get("/api/matrix")
def api_matrix():
    """The curated baseline path list plus the state of any run."""
    st = features.matrix_status()
    st["probes"] = features.matrix_definition()
    return jsonify(st)


@app.post("/api/matrix/run")
def api_matrix_run():
    body = request.get_json(silent=True) or {}
    count = max(1, min(int(body.get("count", 4)), 20))
    r = features.matrix_start(count)
    return (jsonify(r), 409) if r.get("error") else jsonify(r)


@app.get("/api/dhcp/<path:label>")
def api_dhcp(label: str):
    inv = _inventory()
    node = next((n for n in inv["nodes"] if n["label"] == label), None)
    if not node:
        return jsonify({"error": "unknown node"}), 404
    try:
        return jsonify(features.read_lease(node))
    except Exception as e:                     # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/simstats")
def api_simstats():
    """Live per-link packet/drop counters — 0.04s for the whole lab, no console.

    The client diffs two samples to show packets-per-second per link, which is
    the 'prefer interface counters' instrument at estate scale.
    """
    try:
        cml = cml_api.connect()
        s = cml.simulation_stats()
        return jsonify({"links": s.get("links", {}),
                        "at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:                     # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/api/console-log/<path:label>")
def api_console_log(label: str):
    """Console scrollback with NO attach — for containers, the service log
    itself (dnsmasq queries, freeradius rejections, nginx access log)."""
    line = int(request.args.get("line", 0))
    lines = max(1, min(int(request.args.get("lines", 60)), 400))
    try:
        cml = cml_api.connect()
        node = next((n for n in cml.nodes_full().values()
                     if n["label"].rstrip() == label), None)
        if not node:
            return jsonify({"error": "unknown node"}), 404
        log = cml._request(
            "GET", f"/api/v0/labs/{cml.lab_id}/nodes/{node['id']}"
                   f"/consoles/{line}/log?lines={lines}")
        return jsonify({"label": label, "line": line, "log": log})
    except Exception as e:                     # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/api/harvest")
def api_harvest():
    return jsonify(features.harvest_status())


@app.post("/api/harvest/run")
def api_harvest_run():
    inv = _inventory()
    r = features.harvest_start(inv["nodes"])
    return (jsonify(r), 409) if r.get("error") else jsonify(r)


@app.get("/api/dnsaudit")
def api_dnsaudit():
    return jsonify(features.dns_audit_status())


@app.post("/api/dnsaudit/run")
def api_dnsaudit_run():
    inv = _inventory()
    r = features.dns_audit_start(inv["nodes"])
    return (jsonify(r), 409) if r.get("error") else jsonify(r)


@app.get("/api/issues")
def api_issues():
    sections = features.known_issues()
    return jsonify([{k: v for k, v in s.items() if k != "body"} for s in sections])


@app.get("/api/export.csv")
def api_export_csv():
    inv = _inventory()
    issues = {n["label"]: n.get("issues", []) for n in inv["nodes"]}
    csv_text = features.inventory_csv(inv["nodes"], issues)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(csv_text, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="p45-inventory-{stamp}.csv"'})


@app.get("/api/probes")
def api_probes():
    out = []
    for job in probes.recent():
        out.append({
            "id": job["id"],
            "status": job["status"],
            "source": job["spec"]["source"]["label"],
            "target": job["spec"]["target"]["label"],
            "methods": job["spec"]["methods"],
            "created": job["created"],
            "verdict": (job.get("result") or {}).get("verdict"),
        })
    return jsonify(out)


if __name__ == "__main__":
    print(f"NetView on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
