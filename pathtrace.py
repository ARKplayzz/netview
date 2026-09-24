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
  6. DNS (optional) ask a resolver for the target's name, from the source when
               it has a shell, else from a Linux node at the same site
               (probes.pick_dns_vantage), and compare the answer with the
               address being traced.

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
# A factor this small means the source's own links barely rose above their
# background estimate — the src_total driving it is a few packets of noise,
# not signal.  Measured 2026-09-24: healthy factors on this lab cluster
# 0.3-1.3; a firewall-sourced trace (Firewall-DR-2 -> Firewall-HQ-1, ping
# 100% successful, 1-2ms RTT) came back factor 0.006 — every one of the
# firewall's own candidate-facing links showed the SAME 12-15 raw packets as
# pure idle background, and the tiny leftover noise on one unrelated link
# became the entire calibration base.  Dividing every other link's delta by
# that near-zero number then inflated random background elsewhere into a
# confident-looking but physically impossible path (a "satellite" hop at
# 1-2ms RTT — real satellite RTT is hundreds of ms).  A factor below this
# floor is treated as no calibration at all, not a smaller-but-valid one.
MIN_FACTOR = 0.05

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
# No hostname ever appears between the IP and the time here — both traceroute
# commands below force numeric output (`numeric` on IOS, `-n` on BusyBox) — so
# there is nothing between them to skip.  A middle "optional hostname" group
# used to sit here and, being greedy with nothing real to match, swallowed the
# RTT number itself before the trailing `ms` group ever saw it: confirmed live
# 2026-09-24, every real hop from both dialects ("2 172.16.90.31 1 msec",
# "3  172.16.90.31  0.479 ms") parsed with rtt_ms=None. IOS spells the unit
# "msec", BusyBox "ms" — `m?s(?:ec)?` accepts either.
_HOP = re.compile(r"^\s*(\d+)\s+(?:(\d+\.\d+\.\d+\.\d+)|\*)\s*(?:([\d.]+)\s*m?s(?:ec)?)?", re.M)


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
# packet capture (ground truth for the counter-based trace above)
# --------------------------------------------------------------------------
#
# simulation_stats() counters say HOW MANY packets crossed a link; they can't
# say these specific packets did. A live capture can: start it on every
# candidate link before the burst, stop it after, and pull back the real
# ICMP echo request/reply packets by id+seq. CML's `bpfilter` is broken (any
# non-empty value captures 0 packets, verified 2026-09-24), so every link is
# captured unfiltered and the echo request/reply pair for THIS trace's target
# IP is picked out client-side by matching source/destination.
_ECHO_ID_SEQ = re.compile(r"id=(0x[0-9a-f]+), seq=(\d+)")


def _capture_start_all(cml, link_ids: list[str], maxtime: int) -> dict[str, str | None]:
    """Start an unfiltered capture on each candidate link. Returns
    {link_id: starttime} so packet offsets (relative to each link's own
    capture) can be converted to one shared absolute timeline. A link whose
    capture was already running (e.g. a previous job's cleanup lost a race)
    is treated as already-on rather than failing the whole trace over it."""
    starts: dict[str, str | None] = {}
    for lid in link_ids:
        try:
            r = cml.capture_start(lid, maxpackets=3000, maxtime=maxtime)
            starts[lid] = (r or {}).get("starttime")
        except RuntimeError as e:
            if "already running" not in str(e):
                raise
            st = cml.capture_status(lid)
            starts[lid] = (st or {}).get("starttime")
    return starts


def _parse_pcap(packets: list[dict], lid: str, l: dict, base_t: datetime, ip: str) -> list[dict]:
    """Turn CML's raw packet listing for one link into this trace's own echo
    events. Shared by the final (post-stop) pull and the live poller so both
    agree on what counts as a match."""
    events = []
    for p in packets:
        info = p.get("info") or ""
        if p.get("protocol") != "ICMP" or "Echo" not in info:
            continue
        if ip not in (p.get("source"), p.get("destination")):
            continue
        m = _ECHO_ID_SEQ.search(info)
        try:
            offset = float(p.get("time") or 0)
        except ValueError:
            offset = 0.0
        at = base_t.timestamp() + offset
        # CML annotates a reply with "(request in N)", so "request" alone
        # appears in BOTH lines - check which one this line opens with.
        direction = "reply" if info.lstrip().startswith("Echo (ping) reply") else "request"
        events.append({"link_id": lid, "a": l["a"], "ai": l["ai"], "b": l["b"], "bi": l["bi"],
                       "at": at, "dir": direction, "no": p.get("no"),
                       "src": p.get("source"), "dst": p.get("destination"),
                       "icmp_id": m.group(1) if m else None, "seq": m.group(2) if m else None})
    return events


def _live_poll(job_id: str, cml, link_ids: list[str], links: dict, starts: dict[str, str | None],
               ip: str, stop: threading.Event, interval: float = 0.5) -> None:
    """Poll every candidate link's capture WHILE it's still running (verified
    live 2026-09-24: /pcap/{id}/packets returns whatever has been captured so
    far, no need to stop first) and append newly-seen echoes to the job as
    they're seen, instead of only after the whole burst finishes. Runs in its
    own thread alongside `_burst()`'s blocking console read; `stop` ends it
    once the burst (and its retry, if any) is done. Cosmetic only - the final
    `_packet_log()` pull after capture/stop is still the source of truth."""
    base_ts: dict[str, datetime] = {}
    for lid, start in starts.items():
        try:
            base_ts[lid] = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    seen: dict[str, set] = {lid: set() for lid in link_ids}
    while not stop.is_set():
        for lid in link_ids:
            l, base_t = links.get(lid), base_ts.get(lid)
            if not l or not base_t:
                continue
            try:
                packets = cml.pcap_packets(lid)
            except RuntimeError:
                continue
            fresh = [p for p in packets if p.get("no") not in seen[lid]]
            if not fresh:
                continue
            seen[lid].update(p.get("no") for p in fresh)
            events = _parse_pcap(fresh, lid, l, base_t, ip)
            if events:
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job is not None:
                        job.setdefault("live_packets", []).extend(events)
        stop.wait(interval)


def _packet_log(cml, link_ids: list[str], links: dict, starts: dict[str, str | None],
                ip: str) -> list[dict]:
    """Stop capture on every candidate link and return the target IP's own
    echo request/reply packets, time-ordered across all of them. Each
    packet's `time` from CML is seconds since THAT link's own capture
    started, not a shared clock, so it's converted to one absolute timeline
    using the per-link starttime `_capture_start_all` recorded."""
    for lid in link_ids:
        try:
            cml.capture_stop(lid)
        except RuntimeError:
            pass
    events = []
    for lid in link_ids:
        l = links.get(lid)
        start = starts.get(lid)
        if not l or not start:
            continue
        try:
            base_t = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            packets = cml.pcap_packets(lid)
        except RuntimeError:
            continue
        events.extend(_parse_pcap(packets, lid, l, base_t, ip))
    events.sort(key=lambda e: e["at"])
    if events:
        t0 = events[0]["at"]
        for e in events:
            e["t_ms"] = round((e["at"] - t0) * 1000)
            e["at"] = datetime.fromtimestamp(e["at"], tz=timezone.utc).isoformat()
    return events


def _calibratable(src: dict, nodes: list[dict], raw_delta: dict, base: dict,
                  base_dt: float, dt: float, expected: int) -> bool:
    """Would `analyse()` get a usable factor out of this burst?  Same
    background-corrected sum over the source's own links that `analyse()`
    computes for real, checked early so a too-small burst can be retried
    with more packets before the console session closes, instead of only
    finding out from a `factor: null` (or, worse, a garbage-but-nonzero)
    result the caller has to re-request.

    `total > 0` alone isn't enough: a source whose own links show almost no
    rise above background still clears it on a few stray packets of noise,
    then `factor = total/expected` comes out implausibly tiny and every
    other link's delta gets divided by it, inflating unrelated background
    into a confident-looking but false path. Also require the prospective
    factor to clear MIN_FACTOR."""
    src_links = {l.get("link_id") for n in nodes if n["label"] == src["label"]
                for l in (n.get("links") or [])}
    total = 0.0
    for lid in src_links:
        d = raw_delta.get(lid)
        if not d:
            continue
        b = base.get(lid) or {"read": 0, "write": 0}
        rate = (b["read"] + b["write"]) / base_dt
        total += max(0.0, d["read"] + d["write"] - rate * dt)
    return total > 0 and expected > 0 and (total / expected) >= MIN_FACTOR


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
                              + count * per + 12 + (8 if spec.get("capture") else 0))}
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
            # lock every node this run drives, in sorted order (no deadlock)
            drive = sorted({spec["source"]["label"]}
                           | ({spec["vantage"]["label"]} if spec.get("vantage") else set()))
            locks = [probe_engine._node_lock(x) for x in drive]
            for lk in locks:
                lk.acquire()
            try:
                cml = cml_api.connect()
                _set(job_id, status="running", stage="opening console session")
                with Console(cml) as con:
                    result = _execute(job_id, cml, con, spec)
            finally:
                for lk in reversed(locks):
                    lk.release()
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

    capture = bool(spec.get("capture")) and bool(cand["links"])
    capture_starts: dict[str, str | None] = {}
    live_stop = threading.Event()
    live_thread = None
    link_map = _link_map(nodes) if capture else {}
    if capture:
        _set(job_id, stage="starting packet capture on candidate links")
        # Covers both the first attempt and a retry (see below) at up to 500
        # packets each, plus the traceroute that follows - generous rather
        # than tight, since a capture that stops early loses proof, not just
        # accuracy.
        capture_starts = _capture_start_all(cml, cand["links"], maxtime=45)
        _set(job_id, live_packets=[])
        live_thread = threading.Thread(target=_live_poll, daemon=True,
                                       args=(job_id, cml, cand["links"], link_map, capture_starts,
                                             ip, live_stop))
        live_thread.start()

    # A healthy, low-latency path can blast a full burst in a couple of
    # seconds regardless of count (IOS sends the next echo as soon as the
    # reply arrives, not on a fixed interval), so the requested packet count
    # does not reliably control the burst's wall-clock window.  On a busy
    # trunk that window can be too short for the signal to clear the
    # background rate — measured 2026-09-24: a 100-packet burst on a link
    # with ~19 pkt/s of HSRP background came back uncalibratable
    # (factor: null) while the SAME path at 150+ packets calibrated cleanly
    # (factor 0.994-1.003), and a different link elsewhere in this lab
    # (AccessSwitch-DR-1's core-transit trunk, which carries HSRP hellos for
    # eight VLANs at once) runs ~96 pkt/s at idle — five times busier. A
    # single fixed count cannot be a safe default across links that differ
    # that much, so retry once with a bigger burst rather than trusting one
    # static number to be enough everywhere.
    try:
        retried = False
        for attempt_count in (count, min(500, max(count * 3, 300))):
            _set(job_id, stage=f"sending {attempt_count} pings from {src['label']}")
            t2, s2 = _stats(cml)
            burst = _burst(con, src, ip, attempt_count, fast=not answering)
            t3, s3 = _stats(cml)
            dt = t3 - t2
            raw_delta = _delta(s2, s3)
            count = attempt_count
            expected = (burst["sent"] or attempt_count) + (burst["received"] or 0)
            if _calibratable(src, nodes, raw_delta, base, base_dt, dt, expected):
                break
            retried = True

        _set(job_id, stage=f"traceroute from {src['label']}")
        try:
            trace = _traceroute(con, src, ip)
        except Exception as e:                     # noqa: BLE001
            trace = {"cmd": None, "raw": f"{type(e).__name__}: {e}", "hops": []}

        dns = _dns(job_id, con, spec) if spec.get("dns") else None
    finally:
        # Always stop the poller, even if the burst/traceroute above raised -
        # it's a live REST loop independent of the console session, so left
        # running it would keep hitting CML's API forever.
        if capture:
            live_stop.set()
            live_thread.join(timeout=2)

    out = analyse(nodes, src, dst, ip, count, burst, raw_delta, base, base_dt, dt, cand, trace,
                  spec.get("owners") or {}, pre, answering)
    out["retried"] = retried
    if capture:
        _set(job_id, stage="collecting packet capture")
        out["packet_log"] = _packet_log(cml, cand["links"], link_map, capture_starts, ip)
    if retried and out.get("factor") is not None:
        out["findings"].insert(0, f"The first burst was too small to separate from background "
                                  f"traffic on this link; retried at {count} packets.")
    if dns:
        out["dns"] = dns
        if dns.get("ok") and not dns.get("matches"):
            out["findings"].insert(0, f"DNS: {dns['fqdn']} resolved to {dns['addresses'][0]}, not {ip}. "
                                      "The name and the address being traced disagree; one of them is stale.")
            if out["verdict"]["level"] == "ok":
                out["verdict"] = {"level": "warn", "text": out["verdict"]["text"] + " DNS answers a different address."}
        elif not dns.get("ok"):
            out["findings"].insert(0, f"DNS: {dns.get('fqdn') or 'the name'} did not resolve"
                                      f" ({dns.get('reason')}).")
            if out["verdict"]["level"] == "ok" and not dns.get("offnet"):
                out["verdict"] = {"level": "warn", "text": out["verdict"]["text"] + " The name did not resolve."}
    return out


def _dns(job_id: str, con: Console, spec: dict) -> dict:
    """Resolve the target's name the way a client would.  IOS/ASA sources
    ship `no ip domain lookup`, so the lookup runs from a Linux vantage and
    the result says which node asked."""
    src, dst, ip = spec["source"], spec["target"], spec["target_ip"]
    fqdn = (dst.get("dns") or {}).get("fqdn")
    vantage = spec.get("vantage")
    resolver = src.get("resolver") or dst.get("resolver")
    base = {"fqdn": fqdn, "resolver": resolver, "vantage": vantage["label"] if vantage else None,
            "offnet": bool(vantage and vantage.get("_offnet"))}
    if not fqdn:
        return {**base, "ok": False, "reason": "the target has no internal DNS name"}
    if not vantage:
        return {**base, "ok": False, "reason": "no running Linux node to ask from"}
    if not resolver:
        return {**base, "ok": False, "reason": "no resolver known for this site"}
    _set(job_id, stage=f"resolving {fqdn} from {vantage['label']}")
    raw = con.linux_exec(vantage["label"], f"nslookup {fqdn} {resolver}", timeout_ms=25000,
                         line=vantage.get("console_line", 0))
    parsed = probe_engine.parse_nslookup(raw or "")
    known = {ip} | {a.get("value") for a in dst.get("addresses") or []}
    parsed.update(base)
    parsed["matches"] = bool(parsed["ok"] and parsed["addresses"][0] in known)
    return parsed


def _link_map(nodes: list[dict]) -> dict[str, dict]:
    by_label = {n["label"]: n for n in nodes}
    links: dict[str, dict] = {}
    for n in nodes:
        for l in n.get("links") or []:
            lid = l.get("link_id")
            if lid and lid not in links and l["peer"] in by_label:
                links[lid] = {"id": lid, "a": n["label"], "ai": l["local_iface"],
                             "b": l["peer"], "bi": l["peer_iface"]}
    return links


def analyse(nodes, src, dst, ip, count, burst, raw_delta, base, base_dt, dt, cand, trace,
            owners, pre, answering) -> dict:
    links = _link_map(nodes)

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
    # Same floor as _calibratable(): even after the retry, the source's own
    # links may never clear background by enough to trust. Treat a factor
    # below it as no calibration at all rather than dividing every other
    # link's delta by a near-zero number and reporting the resulting noise
    # as a confident path (see MIN_FACTOR's comment for the case this fixes).
    if factor is not None and factor < MIN_FACTOR:
        factor = None

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
        findings.append("The source's own links didn't rise enough above background traffic to "
                        "calibrate the counters reliably, so no path could be attributed with "
                        "confidence. Is the source cabled, and is its link started? If so, this "
                        "link's background traffic may simply be too heavy for packet-counter "
                        "tracing; the ping itself may still be healthy.")
    if cand["paths"] > 1:
        idle = sorted(cand_nodes - path_set - {src["label"], dst["label"]})
        findings.append(f"{cand['paths']} equal-length paths exist between these nodes."
                        + (f" Not used this time: {', '.join(idle)}." if idle and path else ""))
    off = [x for x in path if x not in cand_nodes]
    if off and cand["nodes"] and answering:
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
