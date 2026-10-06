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
import copy
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / ".claude" / "skills" / "cml" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import cml_api          # noqa: E402
import cmlsource        # noqa: E402
import features         # noqa: E402
import model            # noqa: E402
import disaster         # noqa: E402
import failover         # noqa: E402
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
    # 2026-10-01: was a DNS-failover name (~205 s to move after HQ core loss). Now
    # an internal anycast VIP (manifest internal_anycast:): HQ primary, DR's route
    # floats at AD 250 and appears when HQ's /32 is withdrawn from OSPF. The
    # Windows servers hold it as a SkipAsSource /32 that no harvest reports, so
    # the members are named here, primary first.
    {"label": "fileserver.p45.nz", "ip": "10.60.0.70", "kind": "anycast-vip",
     "holders": ["FileServer-HQ-1", "FileServer-DR-1"],
     "note": "Network-layer (anycast) failover: always 10.60.0.70. Normally FileServer-HQ-1; "
             "when HQ's route to it is withdrawn from OSPF, FileServer-DR-1's takes over. "
             "No DNS lookup involved. DC-DR-1's SMB health check still repoints the name at "
             "FileServer-DR-1's real address if the HQ server dies while HQ's network is up."},
    {"label": "(company)website.p45.nz", "ip": "10.50.100.20", "kind": "anycast-vip",
     "note": "Network-layer (anycast) failover: always the same address, 10.50.100.20 - "
             "whichever site currently advertises the best route to it is who answers. "
             "No DNS lookup involved; the failover already happened by the time a ping reaches it."},
]

PORT = 5057
TOPOLOGY_TTL_S = 30      # the topology carries every node's stored config
                         # (~280 KB); it changes only when the lab is edited,
                         # and forced reads (traces, ?fresh=1) still pull it live
STATE_TTL_S = 4          # how long node states are reused
INVENTORY_TTL_S = 10     # one built inventory serves every tab refreshing
                         # within this window (see _inventory)

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
        cml = cml_api.shared()
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
        # The same nodes?data=true read cmlsource makes for the inventory -
        # one request, not two (2026-09-30).
        lab = cmlsource.fetch(cml_api.shared(), force)
        full = lab["nodes_full"]
        err = lab["reads"].get("nodes")
        if err:
            raise RuntimeError(err)
        states = {n["label"].rstrip(): n.get("state") for n in full.values()}
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


_inv_cache: dict = {"data": None, "at": 0.0}
_inv_lock = threading.Lock()


def _inventory(force: bool = False) -> dict:
    """The built inventory, shared by every caller.

    Single-flight (2026-09-30): each browser tab refreshes every 15 s and
    every refresh used to rebuild from CML on its own - topology, node states
    twice, layer-3 addresses, links and three logins, ~4-5 s of controller
    work. A few open tabs kept the CML controller saturated (every API call
    3-11 s, so packet captures couldn't start in time). Now concurrent
    refreshes wait for the one in flight and reuse it for INVENTORY_TTL_S.
    A forced read always builds fresh, after this call arrived, and the
    caller gets its own copy, so a trace may change it freely."""
    with _inv_lock:
        cached, at = _inv_cache["data"], _inv_cache["at"]
        if not force and cached is not None and _now() - at < INVENTORY_TTL_S:
            return cached
        started = _now()
        inv = _build_inventory(force)
        _inv_cache.update(data=inv, at=started)
    return copy.deepcopy(inv) if force else inv


def _build_inventory(force: bool = False) -> dict:
    topo, source, topo_error = _fetch_topology(force)
    states, state_error = _fetch_states(force)
    inv = model.build_inventory(topology=topo)

    # CML is the source of truth for what each node is and holds; the
    # manifest values survive only as `declared`, for comparison.
    cml_reads: dict = {}
    lab_at = None
    if source == "live":
        try:
            cml = cml_api.shared()
            lab = cmlsource.fetch(cml)       # just read by _fetch_states (same force)
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
    # Zones from the manifest (label globs), resolved to node labels - the
    # disaster picker's "quick add" (2026-09-29).
    import fnmatch
    zones = (model.load_manifest().get("zones") or {}) if model.MANIFEST.exists() else {}
    labels = [n["label"] for n in inv["nodes"]]
    inv["zones"] = [{"name": zn, "nodes": sorted({l for l in labels for g in (z.get("match_labels") or [])
                                                  if fnmatch.fnmatch(l, g)})}
                    for zn, z in zones.items()]
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


def _trace_spec(body: dict) -> tuple[dict | None, tuple[dict, int] | None]:
    """The trace job spec for a request body, or (None, (error, status)).
    Split out of api_trace (2026-09-29) so a disaster job can rebuild it from a
    fresh inventory after the power-off."""
    inv = _inventory(force=True)       # trace against the lab as it is right now
    by_label = {n["label"]: n for n in inv["nodes"]}
    owners = _address_owners(inv["nodes"])
    source = by_label.get(body.get("source", ""))
    target = by_label.get(body.get("target", ""))
    virtual = None
    if not target:
        virtual = next((v for v in VIRTUAL_TARGETS if v["label"] == body.get("target")), None)
    if not source or not (target or virtual):
        return None, ({"error": "unknown source or target node"}, 404)
    if not source["can_source_probe"]:
        return None, ({"error": f"{source['label']} has no console this tool can drive, "
                                 f"so it cannot send the test traffic."}, 400)
    if not source["up"]:
        return None, ({"error": f"{source['label']} is {source['state'] or 'not running'}."}, 409)
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
        # Declared holders always count, primary first (2026-10-01): the live
        # harvest only sees whichever holders were up when it ran - an eval
        # file server in its hourly shutdown then dropped out of the list, the
        # trace saw ONE holder, and the reachable-holder fallback never ran.
        if virtual.get("holders"):
            vip_holders = list(dict.fromkeys(list(virtual["holders"]) + vip_holders))
            vip_kind = "anycast" if len(vip_holders) > 1 else vip_kind
        # A powered-off holder answers nothing (disaster traces, 2026-09-30:
        # the after-trace named the downed DR web server as the answerer).
        up_holders = [h for h in vip_holders if (by_label.get(h) or {}).get("up")]
        if up_holders and target.get("label") not in up_holders:
            target = by_label.get(up_holders[0]) or target
        vip_holders = up_holders or vip_holders
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
            return None, ({"error": f"No address known for {target['label']}."}, 400)
        # Deep dive 2026-09-28: a gateway VIP (HSRP) is answered by whichever
        # peer is active, not by the node the user clicked - trace it like
        # anycast so the capture follows the packets to the real answerer.
        vip_holders, vip_kind = _address_holders(inv["nodes"], target_ip)
        vip_holders = [h for h in vip_holders if (by_label.get(h) or {}).get("up")] or vip_holders
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
            return None, ({"error": f"No running Linux node available to resolve {virtual_fqdn}."}, 409)
    return ({"source": source, "target": target, "target_ip": target_ip,
                               "count": count, "nodes": inv["nodes"], "dns": bool(body.get("dns")),
                               "capture": bool(body.get("capture")), "virtual_fqdn": virtual_fqdn,
                               "virtual_label": virtual["label"] if virtual else None,
                               "stale_target": stale_target,
                               "vip_holders": vip_holders if len(vip_holders) > 1 else None,
                               "vip_kind": vip_kind,
                               "vantage": vantage, "owners": owners}), None


@app.post("/api/trace")
def api_trace():
    body = request.get_json(force=True) or {}
    spec, err = _trace_spec(body)
    if err:
        return jsonify(err[0]), err[1]
    warnings: list[str] = []
    dz = body.get("disaster") or {}
    if dz.get("nodes") or dz.get("links"):
        by_label = {n["label"]: n for n in spec["nodes"]}
        # Cables to cut, by link id; named "A - B" from the live link map so the
        # page cannot rename what it cuts.
        lmap = pathtrace._link_map(spec["nodes"])
        links = {}
        for lid in dz.get("links") or []:
            l = lmap.get(lid)
            links[f"{l['a']} - {l['b']}" if l else lid] = lid
        error, warnings = disaster.validate(list(dz.get("nodes") or []), by_label, spec["source"]["label"],
                                            (spec.get("target") or {}).get("label"), links, lmap)
        if error:
            return jsonify({"error": error}), 409
        spec["disaster"] = {"nodes": list(dz.get("nodes") or []), "links": links,
                            "settle_s": max(15, min(int(dz.get("settle_s", 30)), 120)),
                            "restore": bool(dz.get("restore", True)), "warnings": warnings}
        if dz.get("mode") == "failover":
            # Failover capture (spec: docs/failover-capture-spec.md). It needs packet
            # capture, so the baseline runs in Verified mode whatever the page asked.
            window = int(dz.get("window_s") or failover.DEFAULT_WINDOW_S)
            if window not in failover.WINDOWS_S:
                return jsonify({"error": f"window must be one of {failover.WINDOWS_S} s"}), 400
            spec["capture"] = True
            spec["disaster"].update(mode="failover", window_s=window,
                                    stop_early=bool(dz.get("stop_early", True)),
                                    device_logs=bool(dz.get("device_logs", False)),
                                    after_trace=bool(dz.get("after_trace", False)),
                                    settle_s=window + 90)   # only feeds the progress ETA
        plain = {k: v for k, v in body.items() if k != "disaster"}

        def respec():
            s2, e2 = _trace_spec(plain)
            if e2:
                raise RuntimeError(e2[0].get("error"))
            s2["_in_disaster"] = True
            return s2
        spec["respec"] = respec
    job_id = pathtrace.submit(spec)
    job = pathtrace.get(job_id)
    return jsonify({"job_id": job_id, "eta_seconds": job["eta_seconds"], "target_ip": spec["target_ip"],
                    "warnings": warnings})


@app.post("/api/trace/<job_id>/cancel")
def api_trace_cancel(job_id: str):
    return jsonify({"cancelled": disaster.cancel(job_id)})


@app.get("/api/disaster")
def api_disaster():
    return jsonify(disaster.status())


@app.post("/api/disaster/restore")
def api_disaster_restore():
    st = disaster.status()
    if st["active"]:
        return jsonify({"error": "a disaster test is still running; it restores its own nodes"}), 409
    rec = st["interrupted"]
    if not rec:
        return jsonify({"restored": {}})
    res = disaster.restore(cml_api.shared(), rec["nodes"], links=rec.get("links") or {})
    return jsonify({"restored": res})


@app.get("/api/trace/<job_id>")
def api_trace_get(job_id: str):
    job = pathtrace.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({k: v for k, v in job.items() if k != "spec"})


@app.get("/api/failover/history")
def api_failover_history():
    """Saved failover-capture runs, newest first (the testing log)."""
    return jsonify({"runs": failover.history(), "folder": str(failover.HISTORY_DIR)})


@app.get("/api/failover/run/<name>")
def api_failover_run(name: str):
    r = failover.load(name)
    return jsonify(r) if r else (jsonify({"error": "no such run"}), 404)


@app.get("/api/failover/run/<name>/<kind>.csv")
def api_failover_csv(name: str, kind: str):
    r = failover.load(name)
    txt = failover.to_csv(r, kind) if r else None
    if txt is None:
        return jsonify({"error": "no such run or export"}), 404
    return app.response_class(txt, mimetype="text/csv",
                              headers={"Content-Disposition": f"attachment; filename={name}_{kind}.csv"})


@app.get("/api/failover/run/<name>/pcaps.zip")
def api_failover_pcaps(name: str):
    f = failover.pcap_zip(name)
    if not f:
        return jsonify({"error": "no pcaps saved for this run"}), 404
    return send_file(f, as_attachment=True, download_name=f.name)


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
        cml = cml_api.shared()
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
