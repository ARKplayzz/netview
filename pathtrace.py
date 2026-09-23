"""Path trace: which links did the packets actually cross?

A ping says whether B answers A.  It does not say which way the packets went,
and in a lab built around redundancy (HSRP core pairs, dual uplinks, two
firewalls, two carriers) that is the question a failover test is really
asking: did traffic move to the standby when the active went away?

CML can answer it without touching a device's config.  Every link in a CML
lab has packet counters, readable over REST (`simulation_stats`, ~0.04s for the
whole lab).  So a trace is:

  1. PREDICT   every shortest path between the two nodes over the running
               part of the topology.  With redundancy there are several; this
               is the set the traffic *could* take.
  2. BASELINE  sample the counters twice, a few seconds apart, to learn each
               link's background rate (OSPF, HSRP and STP hellos, other tests).
  3. BURST     send a known number of pings from the source's console, with
               counter samples immediately before and after.
  4. MEASURE   subtract the background from each link's delta.  A link on the
               path carries the requests and the replies (about 2N packets);
               a link carrying about N carried one direction only, which is
               asymmetric routing; a link near zero was not used.
  5. TRACEROUTE from the same source, so routed hops can be named too.

The counter scale is calibrated from the source's own links rather than
assumed: every request leaves the source on one of its links and every reply
arrives on one, so the sum over the source's links is (sent + received) times
whatever CML counts per packet.  That one number converts every other link's
delta into packets.

When the target does not answer, the counters still show how far the requests
got, so the trace reports where they stopped.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone

import cml_api                              # noqa: E402  (path set up by app.py)
from console import Console                 # noqa: E402

import probes as probe_engine

BASELINE_S = 3.0
BOTH_WAYS = 0.75     # share of (sent+received) above which a link carried both directions
ONE_WAY = 0.30       # above this and below BOTH_WAYS: one direction only

_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------
# graph helpers
# --------------------------------------------------------------------------

def adjacency(nodes: list[dict], running_only: bool) -> dict[str, list[tuple[str, dict]]]:
    """label -> [(peer, link)] over the inventory's cabling."""
    up = {n["label"] for n in nodes if not running_only or n.get("up")}
    adj: dict[str, list[tuple[str, dict]]] = {n["label"]: [] for n in nodes if n["label"] in up}
    for n in nodes:
        if n["label"] not in up:
            continue
        for l in n.get("links") or []:
            if l["peer"] in up and (l.get("state") in (None, "", "STARTED")):
                adj[n["label"]].append((l["peer"], l))
    return adj


def _bfs(adj, start) -> dict[str, int]:
    dist = {start: 0}
    q = deque([start])
    while q:
        u = q.popleft()
        for v, _ in adj.get(u, []):
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def candidates(nodes: list[dict], src: str, dst: str) -> dict:
    """Every node and link that lies on SOME shortest path from src to dst,
    over running nodes only.  Several equal paths = redundancy."""
    adj = adjacency(nodes, running_only=True)
    if src not in adj or dst not in adj:
        return {"nodes": [], "links": [], "hops": None, "paths": 0}
    ds, dt = _bfs(adj, src), _bfs(adj, dst)
    if dst not in ds:
        return {"nodes": [], "links": [], "hops": None, "paths": 0}
    d = ds[dst]
    on = {u for u in adj if u in ds and u in dt and ds[u] + dt[u] == d}
    links = set()
    for u in on:
        for v, l in adj[u]:
            if v in on and ds[v] == ds[u] + 1 and l.get("link_id"):
                links.add(l["link_id"])
    # count distinct shortest paths (small graphs; dynamic programming)
    ways = {src: 1}
    for u in sorted(on, key=lambda x: ds[x]):
        for v, _ in adj[u]:
            if v in on and ds[v] == ds[u] + 1:
                ways[v] = ways.get(v, 0) + ways.get(u, 0)
    return {"nodes": sorted(on, key=lambda x: (ds[x], x)), "links": sorted(links),
            "hops": d, "paths": ways.get(dst, 0), "depth": {u: ds[u] for u in on}}


# --------------------------------------------------------------------------
# console commands
# --------------------------------------------------------------------------

_IOS_RATE = re.compile(r"Success rate is (\d+) percent \((\d+)/(\d+)\)")
_LNX_RATE = re.compile(r"(\d+) packets transmitted,\s*(\d+) (?:packets )?received")
_HOP = re.compile(r"^\s*(\d+)\s+(?:(\d+\.\d+\.\d+\.\d+)|\*)(?:\s+\(?\S*\)?)?(?:\s+([\d.]+)\s*ms)?", re.M)


def _burst(con: Console, source: dict, ip: str, count: int, fast: bool) -> dict:
    """Send `count` pings as quickly as the platform allows.  Returns
    {sent, received, raw, cmd}.  `fast` = the target did not answer a
    pre-check, so do not wait for replies (IOS timeout 0) - the counters
    still show how far the requests got."""
    fam = source.get("probe_family")
    if fam == "linux":
        cmd = f"ping -q -c {count} -i 0.2 -W 1 {ip}"
        raw = con.linux_exec(source["label"], cmd, timeout_ms=int(count * 400 + 15000),
                             line=source.get("console_line", 0))
        m = _LNX_RATE.search(raw or "")
        if not m and "invalid" in (raw or "").lower():      # BusyBox without fractional -i
            cmd = f"ping -q -c {count} {ip}"
            raw = con.linux_exec(source["label"], cmd, timeout_ms=int(count * 1200 + 15000),
                                 line=source.get("console_line", 0))
            m = _LNX_RATE.search(raw or "")
        sent, recv = (int(m.group(1)), int(m.group(2))) if m else (count, None)
    else:
        cmd = f"ping {ip} repeat {count} timeout {0 if fast and fam == 'ios' else 1}"
        raw = con.exec(source["label"], cmd,
                       timeout_ms=int(count * (300 if fast else 1100) + 20000))
        m = _IOS_RATE.search(raw or "")
        sent, recv = (int(m.group(3)), int(m.group(2))) if m else (count, None)
        if fast and fam == "ios":
            recv = None          # timeout 0 does not wait, so its count means nothing
    return {"sent": sent, "received": recv, "raw": raw, "cmd": cmd}


def _traceroute(con: Console, source: dict, ip: str) -> dict:
    fam = source.get("probe_family")
    if fam == "linux":
        cmd = f"traceroute -n -q 1 -w 1 -m 12 {ip}"
        raw = con.linux_exec(source["label"], cmd, timeout_ms=40000,
                             line=source.get("console_line", 0))
    else:
        cmd = f"traceroute {ip} numeric timeout 1 probe 1 ttl 1 12"
        raw = con.exec(source["label"], cmd, timeout_ms=45000)
        if "%" in (raw or "") and "Invalid" in raw:
            cmd = f"traceroute {ip}"
            raw = con.exec(source["label"], cmd, timeout_ms=90000)
    hops = []
    for m in _HOP.finditer(raw or ""):
        hops.append({"ttl": int(m.group(1)), "ip": m.group(2),
                     "rtt_ms": float(m.group(3)) if m.group(3) else None})
    return {"cmd": cmd, "raw": raw, "hops": hops}


# --------------------------------------------------------------------------
# counters
# --------------------------------------------------------------------------

def _stats(cml) -> tuple[float, dict]:
    s = cml.simulation_stats()
    return time.time(), (s or {}).get("links", {}) or {}


def _delta(a: dict, b: dict) -> dict[str, dict]:
    out = {}
    for lid, v in b.items():
        p = a.get(lid)
        if not p:
            continue
        out[lid] = {"read": (v.get("readpackets") or 0) - (p.get("readpackets") or 0),
                    "write": (v.get("writepackets") or 0) - (p.get("writepackets") or 0),
                    "drops": (v.get("drops") or 0) - (p.get("drops") or 0)}
    return out


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def submit(spec: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    count = spec["count"]
    per = 0.2 if spec["source"].get("probe_family") == "linux" else 0.05
    job = {"id": job_id, "spec": spec, "status": "queued", "stage": "waiting for a console session",
           "created": datetime.now(timezone.utc).isoformat(), "result": None, "error": None,
           "eta_seconds": int(probe_engine.COST_OPEN_S + BASELINE_S + 3 * probe_engine.COST_ATTACH_S
                              + count * per + 12)}
    with _jobs_lock:
        _jobs[job_id] = job
        while len(_jobs) > 100:
            _jobs.popitem(last=False)
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def get(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _set(job_id: str, **kw) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def _run(job_id: str) -> None:
    job = get(job_id)
    spec = job["spec"]
    started = time.time()
    try:
        with probe_engine._CONSOLE_SLOTS:
            lock = probe_engine._node_lock(spec["source"]["label"])
            with lock:
                cml = cml_api.connect()
                _set(job_id, status="running", stage="opening console session")
                with Console(cml) as con:
                    result = _execute(job_id, cml, con, spec)
        result["elapsed_s"] = round(time.time() - started, 1)
        _set(job_id, status="done", stage="complete", result=result,
             ended=datetime.now(timezone.utc).isoformat())
    except Exception as e:                     # noqa: BLE001 - surfaced to the UI
        _set(job_id, status="error", stage="failed", error=f"{type(e).__name__}: {e}",
             ended=datetime.now(timezone.utc).isoformat())


def _execute(job_id: str, cml, con: Console, spec: dict) -> dict:
    src, dst, ip, count = spec["source"], spec["target"], spec["target_ip"], spec["count"]
    nodes = spec["nodes"]
    cand = candidates(nodes, src["label"], dst["label"])

    _set(job_id, stage="checking the target answers")
    pre = probe_engine._ping(con, src, ip, 3)
    answering = pre.get("loss_pct") is not None and pre["loss_pct"] < 100

    _set(job_id, stage=f"measuring background traffic ({BASELINE_S:.0f} s)")
    t0, s0 = _stats(cml)
    time.sleep(BASELINE_S)
    t1, s1 = _stats(cml)
    base = _delta(s0, s1)
    base_dt = max(0.5, t1 - t0)

    _set(job_id, stage=f"sending {count} pings from {src['label']}")
    t2, s2 = _stats(cml)
    burst = _burst(con, src, ip, count, fast=not answering)
    t3, s3 = _stats(cml)
    dt = t3 - t2
    raw_delta = _delta(s2, s3)

    _set(job_id, stage=f"traceroute from {src['label']}")
    try:
        trace = _traceroute(con, src, ip)
    except Exception as e:                     # noqa: BLE001
        trace = {"cmd": None, "raw": f"{type(e).__name__}: {e}", "hops": []}

    return analyse(nodes, src, dst, ip, count, burst, raw_delta, base, base_dt, dt, cand, trace,
                   spec.get("owners") or {}, pre, answering)


def analyse(nodes, src, dst, ip, count, burst, raw_delta, base, base_dt, dt, cand, trace,
            owners, pre, answering) -> dict:
    by_label = {n["label"]: n for n in nodes}
    links: dict[str, dict] = {}
    for n in nodes:
        for l in n.get("links") or []:
            lid = l.get("link_id")
            if lid and lid not in links and l["peer"] in by_label:
                links[lid] = {"id": lid, "a": n["label"], "ai": l["local_iface"],
                              "b": l["peer"], "bi": l["peer_iface"]}

    # background-corrected packet delta per link
    net: dict[str, float] = {}
    for lid, d in raw_delta.items():
        b = base.get(lid) or {"read": 0, "write": 0}
        rate = (b["read"] + b["write"]) / base_dt
        net[lid] = max(0.0, d["read"] + d["write"] - rate * dt)

    sent = burst["sent"] or count
    recv = burst["received"]
    expected = sent + (recv if recv is not None else 0)
    src_links = [lid for lid, l in links.items() if src["label"] in (l["a"], l["b"])]
    src_total = sum(net.get(lid, 0) for lid in src_links)
    factor = src_total / expected if expected and src_total > 0 else None

    rows = []
    for lid, l in links.items():
        if lid not in raw_delta:
            continue
        pk = net.get(lid, 0) / factor if factor else None
        share = pk / expected if pk is not None and expected else 0
        cls = "both" if share >= BOTH_WAYS else "one-way" if share >= ONE_WAY else "none"
        # with no replies at all, a link carrying the requests is "one-way" by
        # construction; call it forward so the page does not claim asymmetry
        if recv == 0 and cls != "none":
            cls = "forward"
        rows.append({**l, "read": raw_delta[lid]["read"], "write": raw_delta[lid]["write"],
                     "drops": raw_delta[lid]["drops"], "packets": round(pk) if pk is not None else None,
                     "share": round(share, 2), "use": cls, "candidate": lid in cand["links"]})

    used = [r for r in rows if r["use"] != "none"]
    adj: dict[str, list[tuple[str, dict]]] = {}
    for r in used:
        adj.setdefault(r["a"], []).append((r["b"], r))
        adj.setdefault(r["b"], []).append((r["a"], r))

    # walk the used links from the source; the target is reached or it isn't
    prev: dict[str, str | None] = {src["label"]: None}
    q = deque([src["label"]])
    while q:
        u = q.popleft()
        for v, _ in adj.get(u, []):
            if v not in prev:
                prev[v] = u
                q.append(v)
    path, stop_at = [], None
    if dst["label"] in prev:
        x = dst["label"]
        while x:
            path.append(x)
            x = prev[x]
        path.reverse()
    elif len(prev) > 1:
        far = max(prev, key=lambda k: _depth(prev, k))
        stop_at = far
        x = far
        while x:
            path.append(x)
            x = prev[x]
        path.reverse()

    # name the traceroute hops from every address the inventory knows
    for h in trace["hops"]:
        h["node"] = owners.get(h["ip"]) if h.get("ip") else None

    findings = []
    cand_nodes = set(cand["nodes"])
    path_set = set(path)
    if factor is None:
        findings.append("No traffic was measured on the source's links, so the counters could not "
                        "be calibrated. Is the source cabled, and is its link started?")
    if cand["paths"] > 1:
        idle = sorted(cand_nodes - path_set - {src["label"], dst["label"]})
        findings.append(f"{cand['paths']} equal-length paths exist between these nodes."
                        + (f" Not used this time: {', '.join(idle)}." if idle and path else ""))
    off = [x for x in path if x not in cand_nodes]
    if off and cand["nodes"]:
        findings.append("Traffic left the shortest paths, through " + ", ".join(off) +
                        ". A routing preference, a blocked STP port or a failed link can cause this.")
    one_way = [r for r in used if r["use"] == "one-way"]
    if one_way:
        findings.append("Asymmetric: " + ", ".join(f"{r['a']} to {r['b']}" for r in one_way) +
                        " carried only one direction. Requests and replies took different routes.")
    stray = [r for r in used if r["a"] not in path_set or r["b"] not in path_set]
    if stray and path:
        findings.append("Also busy during the burst: " + ", ".join(f"{r['a']} to {r['b']}" for r in stray) +
                        ". Either the return path or unrelated traffic.")
    dropped = [r for r in rows if r["drops"] > 0]
    if dropped:
        findings.append("CML counted drops on " + ", ".join(f"{r['a']} to {r['b']} ({r['drops']})" for r in dropped) + ".")
    hop_nodes = [h["node"] for h in trace["hops"] if h.get("node")]
    missing = [x for x in hop_nodes if x not in path_set]
    if missing and path:
        findings.append("Traceroute named " + ", ".join(missing) +
                        " but the counters did not show traffic through it. Check the burst result.")

    loss = None if recv is None else round(100 * (1 - recv / sent)) if sent else None
    if not answering and stop_at:
        level, text = "fail", f"{dst['label']} did not answer. Requests got as far as {stop_at}."
    elif not answering:
        level, text = "fail", f"{dst['label']} did not answer, and no requests were seen leaving {src['label']}."
    elif not path or path[-1] != dst["label"]:
        level, text = "warn", "The target answered, but the counters did not show a complete path."
    elif off or one_way:
        level, text = "warn", f"Reached {dst['label']} in {len(path) - 1} hops, not by the expected route."
    else:
        level, text = "ok", f"Reached {dst['label']} in {len(path) - 1} hops" + \
            (f", on one of {cand['paths']} equal paths." if cand["paths"] > 1 else ", on the shortest path.")

    return {
        "source": src["label"], "target": dst["label"], "target_ip": ip, "count": count,
        "sent": sent, "received": recv, "loss_pct": loss, "answering": answering,
        "precheck": {k: pre.get(k) for k in ("loss_pct", "received", "sent")},
        "burst_cmd": burst["cmd"], "burst_raw": burst["raw"],
        "factor": round(factor, 3) if factor else None, "burst_s": round(dt, 1),
        "candidates": cand, "path": path, "stop_at": stop_at, "links": rows,
        "trace": trace, "findings": findings, "verdict": {"level": level, "text": text},
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _depth(prev: dict, k: str) -> int:
    d = 0
    while prev.get(k):
        k = prev[k]
        d += 1
    return d
