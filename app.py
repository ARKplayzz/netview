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

import os
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

# Named DNS/anycast failover identities layered on top of CML by the DNS and
# routing config, not something CML's own API has any concept of - a lab
# topology pull has no way to discover "fileserver.p45.nz" on its own, so
# this is hand-maintained rather than derived. Selectable as a trace TARGET
# only: what answers depends on which real backend currently holds the name
# or address, which is exactly the thing a trace to one is meant to reveal.
VIRTUAL_TARGETS = [
    {"label": "fileserver.p45.nz", "fqdn": "fileserver.p45.nz", "kind": "dns-failover",
     "note": "DNS-based failover: normally FileServer-HQ-1, fails over to FileServer-DR-1 "
             "on a failed SMB/445 health check (probed from DC-DR-1 every 1 min, "
             "3-consecutive-check hysteresis both ways). The record itself flips, TTL 45s - "
             "tracing this resolves it fresh from the source's own site resolver, the same way "
             "a real client would, so the answer reflects whichever way it's currently pointed."},
    {"label": "(company)website.p45.nz", "ip": "10.50.100.20", "kind": "anycast-vip",
     "note": "Network-layer (anycast) failover: always the same address, 10.50.100.20 - "
             "whichever site currently advertises the best route to it is who answers. "
             "No DNS lookup involved; the failover already happened by the time a ping reaches it."},
]

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

    harvested = features.harvest_nodes()

    up = 0
    for n in inv["nodes"]:
        n["state"] = states.get(n["label"])
        n["up"] = n["state"] in ("STARTED", "BOOTED")
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
    inv["virtual_targets"] = VIRTUAL_TARGETS
    inv["layers"].append({"key": "virtual", "title": "Virtual (DNS/anycast failover)"})
    return inv


def _address_owners(nodes: list[dict]) -> dict[str, str]:
    """ip -> node label, from every address the inventory knows (CML, config,
    device reads, manifest).  Used to name traceroute hops and to match
    captured packets to their sender.

    Live (device-read) addresses are taken from EVERY node before any stored
    one. Deep dive 2026-09-28: CML's stored configs for the edge routers and
    Firewall-DR-2 still carry old point-to-point/tunnel addressing, e.g.
    EdgeRouter-DR-1's stored Gi0/5 claims 10.255.3.1, which is live on
    EdgeRouter-HQ-1's Tunnel0. Taking each node's live-then-stored in turn
    let whichever node came first in the list win."""
    out: dict[str, str] = {}
    for n in nodes:
        for a in (n.get("live") or {}).get("addresses") or []:
            out.setdefault(a["ip"], n["label"])
    for n in nodes:
        for a in n.get("addresses") or []:
            if a.get("kind") and "virtual" in str(a.get("kind")):
                continue          # an HSRP VIP belongs to whichever peer is active
            out.setdefault(a["value"], n["label"])
    return out


def _address_holders(nodes: list[dict], ip: str) -> tuple[list[str], str | None]:
    """Every node that can answer `ip`, and why it is shared: "hsrp" (a
    gateway VIP the active router answers), "anycast" (several real copies,
    the network picks by routing), or None for an ordinary single owner.
    A stored-config claim is ignored when another node holds the address
    live - that is stale config, not sharing (see `_address_owners`)."""
    live = {n["label"] for n in nodes
            if ip in {a.get("ip") for a in (n.get("live") or {}).get("addresses") or []}}
    stored = {n["label"]: str(a.get("kind") or "") for n in nodes
              for a in n.get("addresses") or [] if a.get("value") == ip}
    hsrp = {k for k, kind in stored.items() if "virtual" in kind}
    if len(hsrp) > 1:
        return sorted(hsrp), "hsrp"
    holders = sorted(live or set(stored))
    return holders, ("anycast" if len(holders) > 1 else None)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/inventory")
def api_inventory():
    return jsonify(_inventory(force=request.args.get("fresh") == "1"))


@app.post("/api/trace")
def api_trace():
    body = request.get_json(force=True) or {}
    inv = _inventory(force=True)       # trace against the lab as it is right now
    by_label = {n["label"]: n for n in inv["nodes"]}
    owners = _address_owners(inv["nodes"])
    source = by_label.get(body.get("source", ""))
    target = by_label.get(body.get("target", ""))
    virtual = None
    if not target:
        virtual = next((v for v in VIRTUAL_TARGETS if v["label"] == body.get("target")), None)
    if not source or not (target or virtual):
        return jsonify({"error": "unknown source or target node"}), 404
    if not source["can_source_probe"]:
        return jsonify({"error": f"{source['label']} has no console this tool can drive, "
                                 f"so it cannot send the test traffic."}), 400
    if not source["up"]:
        return jsonify({"error": f"{source['label']} is {source['state'] or 'not running'}."}), 409
    virtual_fqdn = None
    vip_holders, vip_kind = [], None
    if virtual and virtual.get("ip"):
        # Static (anycast) failover: the address never changes, so there is
        # nothing to resolve. Whichever real node currently holds it is found
        # the same way a traceroute hop is named - via `owners` - purely for
        # a nicer path diagram; if nobody currently holds it that's itself a
        # real finding (no site is advertising the VIP), not an error here.
        target_ip = virtual["ip"]
        target = by_label.get(owners.get(target_ip)) or {"label": virtual["label"], "virtual": True}
        # Every node that holds the VIP (anycast: one per site). The trace proves
        # whichever copy the network actually delivers to (2026-09-28: the VPN
        # laptop reaches the HQ copy; the address map alone named the DR one).
        vip_holders, vip_kind = _address_holders(inv["nodes"], target_ip)
    elif virtual:
        # DNS-based failover: which real backend answers depends on which way
        # the record currently points, so it can only be found by resolving
        # it fresh, from the source's own site, inside the job itself (the
        # same way a real client would look it up) - see pathtrace._execute().
        target_ip = None
        target = {"label": virtual["label"], "virtual": True}
        virtual_fqdn = virtual["fqdn"]
    else:
        target_ip = body.get("target_ip") or target.get("live_ip") or target.get("primary_ip")
        if not target_ip:
            return jsonify({"error": f"No address known for {target['label']}."}), 400
        # Deep dive 2026-09-28: a gateway VIP (HSRP) is answered by whichever
        # peer is active, not by the node the user clicked - trace it like
        # anycast so the capture follows the packets to the real answerer.
        vip_holders, vip_kind = _address_holders(inv["nodes"], target_ip)
    # Confirmed live 2026-09-24: a node whose live harvest never ran (or was
    # lost - it's in-memory, so a dev-server restart empties it) has no
    # live_ip, and everything then falls back to primary_ip/cml-config -
    # CML's STORED config, which can silently drift from the real device.
    # It did here: the stored config's address belonged to a DIFFERENT node
    # entirely, and "the target didn't answer" cost real time to diagnose
    # before that surfaced. Flagged for the job to surface as a finding
    # rather than silently trusted.
    stale_target = bool(target) and not target.get("live_ip") and not virtual
    # 100 measured too low on this lab's busier trunks (factor: null on a
    # link running ~19 pkt/s background, clean calibration from ~150 up) —
    # see pathtrace._execute()'s retry logic. 150 as the default cuts how
    # often that retry is needed without slowing the common case much.
    count = max(20, min(int(body.get("count", 150)), 500))
    # Optional: also ask a resolver for the target's name, from the source
    # itself when it has a shell, else from a Linux node at the same site.
    # A DNS-based virtual target always needs a vantage, checkbox or not -
    # resolving it IS the trace, not an optional cross-check on top of it.
    vantage = None
    if virtual_fqdn or (body.get("dns") and (target.get("dns") or {}).get("fqdn")):
        vantage = probes.pick_dns_vantage(source, inv["nodes"], source.get("site"))
        if virtual_fqdn and not vantage:
            return jsonify({"error": f"No running Linux node available to resolve {virtual_fqdn}."}), 409
    job_id = pathtrace.submit({"source": source, "target": target, "target_ip": target_ip,
                               "count": count, "nodes": inv["nodes"], "dns": bool(body.get("dns")),
                               "capture": bool(body.get("capture")), "virtual_fqdn": virtual_fqdn,
                               "virtual_label": virtual["label"] if virtual else None,
                               "stale_target": stale_target,
                               "vip_holders": vip_holders if len(vip_holders) > 1 else None,
                               "vip_kind": vip_kind,
                               "vantage": vantage, "owners": owners})
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


@app.get("/api/export.csv")
def api_export_csv():
    inv = _inventory()
    csv_text = features.inventory_csv(inv["nodes"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(csv_text, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="p45-inventory-{stamp}.csv"'})


def _auto_harvest() -> None:
    """Read every device's real addresses once at start-up. The harvest is
    in memory only, so without this each launch (every NetView.exe start)
    traced against CML's STORED configs - measured 2026-09-28 to be stale
    for the four edge routers, Firewall-DR-2 and HomeRouter-WFH."""
    time.sleep(2)
    try:
        r = features.harvest_start(_inventory()["nodes"])
        print("start-up harvest:", r.get("error") or f"reading {r.get('total')} devices", flush=True)
    except Exception as e:                      # noqa: BLE001 - never stop the UI over it
        print(f"start-up harvest failed: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    if os.environ.get("NETVIEW_NO_AUTO_HARVEST") != "1":
        threading.Thread(target=_auto_harvest, daemon=True).start()
    print(f"NetView on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
