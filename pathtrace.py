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
        # ASA takes the identical `ping ... repeat N timeout 0` syntax and
        # genuinely does not wait for replies with it - verified live
        # 2026-09-24 (20 probes against a dead target: 3.8s, not ~20s).  This
        # used to check `fam == "ios"` only, so a firewall (fam == "asa")
        # against a confirmed-dead target always fell through to `timeout 1`
        # while `_burst()`'s timeout_ms budget below still used the FAST
        # per-packet multiplier for it (keyed on `fast` alone) - a budget
        # sized for a few-second command wrapped around one that actually
        # takes close to `count` seconds. A 150-packet burst measured 68.6s
        # against a 65s budget: `_drain()` gave up while the ASA was still
        # mid-ping, `_detach()` fired while the device was still printing `?`
        # marks, and the NEXT `_attach()` (traceroute) then walked into that
        # live, still-running session - matching no recognised prompt and
        # burning up to the full 60-iteration budget before recovering or
        # raising "could not reach privileged EXEC".
        fast_cli = fast and fam in ("ios", "asa")
        cmd = f"ping {ip} repeat {count} timeout {0 if fast_cli else 1}"
        raw = con.exec(source["label"], cmd,
                       timeout_ms=int(count * (300 if fast_cli else 1100) + 20000))
        m = _IOS_RATE.search(raw or "")
        sent, recv = (int(m.group(3)), int(m.group(2))) if m else (count, None)
        if fast_cli:
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
    """Start a FRESH, RUNNING capture on every candidate link.
    Returns {link_id: starttime} so each link's packet offsets can be put on one
    shared timeline. Links to stopped nodes are dropped from `link_ids`.

    Built on what CML measurably does (2026-09-28, NetView's incomplete paths):
      * a start on a link that ALREADY has a capture returns that old capture
        without an error - one left over from an earlier trace (2 h of health
        checks, buffer full) then records none of this test's packets;
      * a start within ~1 s of that link's previous stop usually (6 of 8) reports
        success but NEVER RUNS; re-starting it recovers it (5 of 6);
      * starts issued in PARALLEL collide the same way (29 of 43 came up), while
        40 sequential starts took 0.1-0.5 s each and 39 of 40 ran;
      * busy trunks carry ~100-300 pps of HSRP hellos, so 3000 packets filled in
        ~10 s, before the test's burst; 10000 is used.
    So: read every status (parallel reads are safe), stop only genuine
    leftovers, start SEQUENTIALLY, then verify and re-start what did not run."""
    from concurrent.futures import ThreadPoolExecutor

    def status(lid: str) -> dict:
        try:
            return cml.capture_status(lid) or {}
        except RuntimeError:
            return {}

    def statuses(ids: list[str]) -> dict[str, dict]:
        with ThreadPoolExecutor(max_workers=8) as ex:
            return dict(zip(ids, ex.map(status, ids)))

    skip: set[str] = set()

    def start(lid: str) -> None:
        try:
            cml.capture_start(lid, maxpackets=10000, maxtime=maxtime)
        except RuntimeError as e:
            if "not running" in str(e):
                skip.add(lid)                 # a link to a stopped node carries nothing to prove
            elif "already running" not in str(e):
                raise

    leftovers = [l for l, s in statuses(link_ids).items() if s.get("starttime")]
    for lid in leftovers:
        try:
            cml.capture_stop(lid)
        except RuntimeError:
            pass
    if leftovers:
        time.sleep(2.5)                       # a start sooner than this silently fails
    for lid in link_ids:
        start(lid)
    for lid in skip:
        link_ids.remove(lid)
    starts: dict[str, str | None] = {}
    for _ in range(3):
        time.sleep(1.5)
        st = statuses(link_ids)
        starts = {l: s.get("starttime") for l, s in st.items()}
        missing = [l for l, v in starts.items() if not v]
        if not missing:
            break
        for lid in missing:
            start(lid)
    return starts


def _parse_pcap(packets: list[dict], lid: str, l: dict, base_t: datetime, ip: str,
                src_label: str, owners: dict[str, str]) -> list[dict]:
    """Turn CML's raw packet listing for one link into this trace's own echo
    events. Shared by the final (post-stop) pull and the live poller so both
    agree on what counts as a match.

    Matching on the target IP alone is not enough: a link the source shares
    with other traffic can carry SOMEONE ELSE'S ping to (or from) the exact
    same target during the same capture window - HSRP health checks, another
    admin's test, a second trace run - and that traffic would get shown as
    this test's own proof.  IP routing preserves the IP header end to end
    (only the framing changes per hop), so the OTHER endpoint is the same
    address on every hop of a real run of this test: the source device's own
    address, whichever interface it sent from.  `owners` (built from every
    address the inventory knows) resolves that address back to a node label.

    Fails OPEN on an address `owners` doesn't recognise, not closed: measured
    live 2026-09-24, CoreSwitch-DR-1's own Ethernet0/2 address never made it
    into the harvested/config address list at all (a pre-existing gap in that
    data, not something introduced here), so a strict "must resolve to
    src_label" check rejected CoreSwitch-DR-1's own genuine echo traffic on
    its own outgoing interface. Only reject a packet whose other endpoint
    resolves to a DIFFERENT, KNOWN node - that's the actual failure mode this
    exists to catch (traffic from some other, identifiable device sharing the
    same target IP), not an unlabelled address."""
    events = []
    for p in packets:
        info = p.get("info") or ""
        if p.get("protocol") != "ICMP" or "Echo" not in info:
            continue
        src_ip, dst_ip = p.get("source"), p.get("destination")
        if dst_ip == ip:
            other = src_ip
        elif src_ip == ip:
            other = dst_ip
        else:
            continue
        owner = owners.get(other)
        if owner is not None and owner != src_label:
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


def _parse_esp(packets: list[dict], lid: str, l: dict, base_t: datetime,
               owners: dict[str, str]) -> list[dict]:
    """ESP (IPsec) packets on one link, with the node that owns each end, for
    `_packet_log()` to keep only when both ends are proven tunnel heads."""
    out = []
    for p in packets:
        if p.get("protocol") != "ESP":
            continue
        try:
            offset = float(p.get("time") or 0)
        except ValueError:
            offset = 0.0
        src_ip, dst_ip = p.get("source"), p.get("destination")
        out.append({"link_id": lid, "a": l["a"], "ai": l["ai"], "b": l["b"], "bi": l["bi"],
                    "at": base_t.timestamp() + offset, "dir": "esp", "no": p.get("no"),
                    "src": src_ip, "dst": dst_ip, "src_owner": owners.get(src_ip),
                    "dst_owner": owners.get(dst_ip), "icmp_id": None, "seq": None})
    return out


def _live_poll(job_id: str, cml, link_ids: list[str], links: dict, starts: dict[str, str | None],
               ip: str, src_label: str, owners: dict[str, str], stop: threading.Event,
               interval: float = 0.5) -> None:
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
    # Same ICMP-id fingerprinting as _packet_log(), applied prospectively as
    # it's learned rather than in one final pass: the source's own link is
    # polled first each cycle so `own_ids` fills in before the other links
    # are checked, but until it has anything, everything owner-matched is
    # shown - a live view can't hold packets back waiting for a cross-check
    # that might never resolve on a link that isn't a capture candidate.
    own_ids: set[str] = set()
    ordered = [lid for lid in link_ids if src_label in (links.get(lid, {}).get("a"), links.get(lid, {}).get("b"))] + \
             [lid for lid in link_ids if src_label not in (links.get(lid, {}).get("a"), links.get(lid, {}).get("b"))]
    while not stop.is_set():
        for lid in ordered:
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
            events = _parse_pcap(fresh, lid, l, base_t, ip, src_label, owners)
            if src_label in (l["a"], l["b"]):
                own_ids.update(e["icmp_id"] for e in events if e["icmp_id"])
            elif own_ids:
                events = [e for e in events if e["icmp_id"] in own_ids]
            if events:
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job is not None:
                        job.setdefault("live_packets", []).extend(events)
        stop.wait(interval)


def _packet_log(cml, link_ids: list[str], links: dict, starts: dict[str, str | None],
                ip: str, src_label: str, owners: dict[str, str]) -> list[dict]:
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
    events, esp = [], []
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
        events.extend(_parse_pcap(packets, lid, l, base_t, ip, src_label, owners))
        esp.extend(_parse_esp(packets, lid, l, base_t, owners))
    # The owners check (above) rules out traffic from a different, identified
    # device sharing the same target IP, but not a second ping session
    # between these exact same two endpoints - e.g. someone running the
    # identical test by hand on the device's own console at the same time.
    # IOS/ASA assigns one ICMP id per `ping` invocation, shared by every
    # packet in that run and never reused by a concurrent one, so the id(s)
    # actually seen on the source's OWN link - which by definition carries
    # only the source's own traffic - are this test's real fingerprint.
    # Fails open (keeps everything) if that link wasn't captured or came back
    # empty, rather than discarding real proof over a missing cross-check.
    own_ids = {e["icmp_id"] for e in events
              if e["icmp_id"] and src_label in (e["a"], e["b"])}
    if own_ids:
        events = [e for e in events if e["icmp_id"] in own_ids]
    # Through a VPN the test's pings are encrypted between the two tunnel
    # heads, so the links in between show ESP, not ICMP, and would leave a
    # hole in the proven path (measured 2026-09-27: WFH laptop -> file
    # server, path "proven" only as far as the home router). ESP can't be
    # matched to an ICMP id, so it is accepted only between two nodes this
    # capture has ALREADY proven handled the test's own pings - the tunnel
    # heads - which excludes any unrelated tunnel crossing the same links.
    proven = {n for e in events for n in (e["a"], e["b"])}
    events += [e for e in esp if e["src_owner"] in proven and e["dst_owner"] in proven
               and e["src_owner"] != e["dst_owner"]]
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
    owners = spec.get("owners") or {}
    resolved_note = None
    if spec.get("virtual_fqdn"):
        # A DNS-based failover target only exists as a name - which real
        # backend answers depends on which way the record currently points,
        # so it has to be resolved fresh here, from the source's own site,
        # the same way a real client would look it up, rather than assumed
        # from whatever it happened to point to when the inventory last
        # loaded. Once resolved, everything downstream (candidates, the
        # burst, the path diagram) runs exactly as if this were an ordinary
        # trace to that real node - dst is swapped for its real record, not
        # just relabeled.
        fqdn = spec["virtual_fqdn"]
        _set(job_id, stage=f"resolving {fqdn}")
        vantage = spec.get("vantage")
        if not vantage:
            raise RuntimeError(f"no running Linux node available to resolve {fqdn}")
        # No site resolver in the model (the home worker's site has none - its
        # resolver is whatever its house router hands out by DHCP) means: ask
        # the vantage's own configured resolver, exactly as a client would.
        resolver = src.get("resolver") or vantage.get("resolver")
        cmd = f"nslookup {fqdn} {resolver}" if resolver else f"nslookup {fqdn}"
        raw = con.linux_exec(vantage["label"], cmd, timeout_ms=25000,
                             line=vantage.get("console_line", 0))
        resolver = resolver or "its own configured resolver"
        parsed = probe_engine.parse_nslookup(raw or "")
        if not parsed["ok"] or not parsed["addresses"]:
            raise RuntimeError(f"{fqdn} did not resolve ({parsed.get('reason') or 'no answer'})")
        ip = parsed["addresses"][0]
        owner = owners.get(ip)
        by_label = {n["label"]: n for n in nodes}
        dst = by_label.get(owner) or {"label": owner or ip, "virtual": True, "up": True}
        resolved_note = (f"{fqdn} currently resolves to {owner or ip} ({ip}), via "
                         f"{vantage['label']} asking {resolver}.")
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
    # Capture the candidate links AND every link joining two candidate nodes.
    # Candidates are shortest paths by hop count, but real forwarding can take
    # a sideways hop between them - measured 2026-09-27: WFH -> FileServer-HQ-1
    # crossed CoreSwitch-HQ-1 -> CoreSwitch-HQ-2, a link on no shortest path,
    # so it was never captured and the proven path broke there.
    cand_nodes = set(cand["nodes"])
    scope = set(cand_nodes)
    # Hop-count candidates can't predict a VPN: the WFH laptop's traffic to a
    # DR host goes through the HQ tunnel by policy, a path no shortest-hop
    # search offers (measured 2026-09-27: WFH -> Tech PC "proven" only to the
    # home router). So when a candidate node is a tunnel head, also capture
    # source -> head, the head's underlay to its peer, and peer -> target.
    if capture:
        for x, y in _tunnels(nodes):
            for h, peer in ((x, y), (y, x)):
                if h in cand_nodes:
                    for a_, b_ in ((src["label"], h), (h, peer), (peer, dst["label"])):
                        scope |= set(candidates(nodes, a_, b_)["nodes"])
        # Firewalls are exactly where OSPF cost policy departs from hop count:
        # each unit's straight leg costs 10 and its crossover 100, so the
        # fewest-hops path (Firewall-HQ-1 inside2 -> CoreSwitch-HQ-2) is the one
        # traffic never takes (it leaves `inside` -> CoreSwitch-HQ-1). Measured
        # 2026-09-27: WFH -> ServerRoomSwitch-HQ-2 was "proven" only to the home
        # router because that leg went uncaptured. So capture EVERY leg of any
        # firewall in scope.
        # ANYCAST (2026-09-28): a static virtual IP is held by several nodes and
        # the network - not NetView - decides which one answers (the VPN laptop
        # reaches the HQ copy through the HQ tunnel; a DR desk reaches the DR
        # copy). Capture towards EVERY holder, so whichever copy the packets go
        # to is proven, not just the one the address map happened to name.
        for h in spec.get("vip_holders") or []:
            if h != dst["label"]:
                scope |= set(candidates(nodes, src["label"], h)["nodes"])
                for x, y in _tunnels(nodes):
                    for hd, peer in ((x, y), (y, x)):
                        if hd in scope:
                            scope |= set(candidates(nodes, peer, h)["nodes"])
        fws = {n["label"] for n in nodes if n.get("probe_family") == "asa"} & scope
        for l in link_map.values():
            if l["a"] in fws:
                scope.add(l["b"])
            elif l["b"] in fws:
                scope.add(l["a"])
    cap_links = sorted(set(cand["links"]) | {lid for lid, l in link_map.items()
                                             if l["a"] in scope and l["b"] in scope})
    if capture:
        _set(job_id, stage="starting packet capture on candidate links")
        # Covers both the first attempt and a retry (see below) at up to 500
        # packets each, plus the traceroute that follows - generous rather
        # than tight, since a capture that stops early loses proof, not just
        # accuracy.
        # 300 s, not 45 (2026-09-28): a retried burst from a Linux PC (300-500
        # paced pings) outlasted 45 s, so the first links' captures - the
        # source's own - expired mid-burst and paths "stopped" one hop out.
        # Every capture is stopped explicitly when the trace ends (or fails),
        # so this is only a backstop.
        capture_starts = _capture_start_all(cml, cap_links, maxtime=300)
        _set(job_id, live_packets=[])
        live_thread = threading.Thread(target=_live_poll, daemon=True,
                                       args=(job_id, cml, cap_links, link_map, capture_starts,
                                             ip, src["label"], owners, live_stop))
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
    completed = False
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
        completed = True
    finally:
        # Always stop the poller, even if the burst/traceroute above raised -
        # it's a live REST loop independent of the console session, so left
        # running it would keep hitting CML's API forever.
        if capture:
            live_stop.set()
            live_thread.join(timeout=2)
            if not completed:
                for lid in cap_links:           # a failed trace must not leave captures running
                    try:
                        cml.capture_stop(lid)
                    except Exception:           # noqa: BLE001
                        pass

    out = analyse(nodes, src, dst, ip, count, burst, raw_delta, base, base_dt, dt, cand, trace,
                  owners, pre, answering)
    out["retried"] = retried
    out["mode"] = "counters"
    if capture:
        _set(job_id, stage="collecting packet capture")
        out["packet_log"] = _packet_log(cml, cap_links, link_map, capture_starts, ip,
                                        src["label"], owners)
        out["capture_diag"] = {"links": len(cap_links),
                               "started": sum(1 for l in cap_links if capture_starts.get(l)),
                               "with_packets": len({e["link_id"] for e in out["packet_log"]})}
        # Capture mode is authoritative when it's on: the diagnosis (path,
        # per-link classification, verdict, findings) comes entirely from
        # proven packets, not byte counts, so nothing else happening on the
        # network at the time can change the answer. The counter-based
        # result computed above is kept as `counter_estimate` rather than
        # discarded - when the two disagree, that disagreement is itself
        # useful evidence of exactly the kind of noise this mode exists to
        # rule out.
        counter_estimate = {"path": out["path"], "verdict": out["verdict"]}
        cap = analyse_capture(nodes, src, dst, ip, out["packet_log"], cand, trace, owners,
                              pre, answering, out["sent"], out["received"],
                              holders=spec.get("vip_holders"))
        out["path"], out["stop_at"] = cap["path"], cap["stop_at"]
        out["links"], out["verdict"], out["findings"] = cap["links"], cap["verdict"], cap["findings"]
        if cap.get("answered_by") and cap["answered_by"] != dst["label"]:
            # anycast: adopt the copy the network actually delivered to
            dst = next((n for n in nodes if n["label"] == cap["answered_by"]),
                       {**dst, "label": cap["answered_by"]})
            out["target"] = dst["label"]
        out["mode"] = "capture"
        out["counter_estimate"] = counter_estimate
        if counter_estimate["path"] != cap["path"]:
            out["findings"].insert(0, "The counter-based estimate disagreed with this capture-"
                                      f"verified result: it suggested {' -> '.join(counter_estimate['path']) or '(no path)'}. "
                                      "That's exactly the kind of noise packet capture exists to rule out - "
                                      "trust the path above.")
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
    # A trace to a node CML itself reports as not running should say so, not
    # leave "did not answer" to be read as a network fault - the eval DCs power
    # off hourly and produced exactly that confusion (2026-09-27).
    if not answering and dst.get("state") and not dst.get("up"):
        out["findings"].insert(0, f"{dst['label']} is {dst['state']} in CML - it is not running, "
                                  "so it cannot answer. Start it and trace again.")
        out["verdict"] = {"level": "fail", "text": f"{dst['label']} is {dst['state']} in CML, so it did not answer."}
    # A VPN is a routing policy the hop-count prediction can't see, so a path
    # through both ends of a known tunnel legitimately differs from it.
    vpn = [t for t in _tunnels(nodes) if t[0] in out["path"] and t[1] in out["path"]]
    if vpn and out["verdict"]["level"] == "warn" and "not by the expected route" in out["verdict"]["text"]:
        x, y = vpn[0]
        out["verdict"] = {"level": "ok", "text": f"Reached {dst['label']} in {len(out['path']) - 1} hops "
                          f"through the VPN tunnel {x} ⇄ {y}"
                          + (", confirmed by packet capture." if out.get("mode") == "capture" else ".")}
        out["findings"] = [f for f in out["findings"] if not f.startswith("Traffic left the shortest paths")]
        out["findings"].insert(0, f"Routed through the VPN tunnel between {x} and {y}. The tunnel is a "
                                  "routing policy, so differing from the shortest-hop prediction is "
                                  "expected here, not a fault.")
    if spec.get("virtual_label"):
        out["virtual_target"] = spec["virtual_label"]
    if len(spec.get("vip_holders") or []) > 1 and out.get("mode") != "capture":
        out["findings"].insert(0, f"{spec['virtual_label']} is anycast - {', '.join(spec['vip_holders'])} "
                                  f"all hold {ip}, and the network picks one per source. Quick mode can't "
                                  f"tell which answered (it assumed {dst['label']}); run Verified mode to "
                                  f"see the copy this source actually reaches.")
    if resolved_note:
        out["findings"].insert(0, resolved_note)
    if spec.get("stale_target"):
        out["findings"].insert(0, f"{dst['label']} has no live-harvested address right now - "
                                  f"{ip} came from CML's stored config, which can silently drift "
                                  "from what the device is actually running (confirmed live "
                                  "2026-09-24: it pointed at a different node's address entirely). "
                                  "Run a harvest refresh before trusting a \"did not answer\" result.")
    # Measured 2026-09-27 across all four ASAs: a ping addressed TO a
    # firewall is only answered when it arrives on the interface that owns
    # the address, and its reply leaves the same way. The dmz/outside
    # addresses never answer from inside; inside/inside2 answer only from
    # the side whose routing lands on that interface. Through-traffic was
    # 100% on the same run - so this is how an ASA behaves, not a fault.
    if not answering and (dst.get("probe_family") == "asa" or src.get("probe_family") == "asa"):
        who = dst["label"] if dst.get("probe_family") == "asa" else src["label"]
        out["findings"].insert(0, f"{who} is a Cisco ASA. An ASA only answers a ping on the interface "
                                  "the packet arrives on, and drops a reply that returns on a different "
                                  "interface from the one its request left by. Pinging a firewall's "
                                  "far-side address (dmz/outside, or the other core's inside2), or pinging "
                                  "from a firewall to something behind its sibling, fails by design. To "
                                  "test a path through a firewall, trace to a host behind it instead.")
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


def _tunnels(nodes: list[dict]) -> list[tuple[str, str]]:
    """Logical tunnel links (VPN/GRE), paired from live-harvested Tunnel*
    interface addresses that share a /30. CML's topology has no idea these
    exist - they are configuration, not cables."""
    by_net: dict[int, set[str]] = {}
    for n in nodes:
        for a in (n.get("live") or {}).get("addresses") or []:
            if not str(a.get("iface", "")).lower().startswith("tunnel"):
                continue
            try:
                o = [int(x) for x in a["ip"].split(".")]
            except (KeyError, ValueError):
                continue
            key = ((o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]) >> 2
            by_net.setdefault(key, set()).add(n["label"])
    return [tuple(sorted(v)) for v in by_net.values() if len(v) == 2]


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
        # A link whose raw read and write deltas are close to each other
        # carried real traffic in both directions almost by definition - a
        # genuinely one-way link (asymmetric routing) shows a lopsided ratio
        # instead, since only one direction's packets cross it at all.
        # `share` alone can land just under BOTH_WAYS from ordinary noise in
        # the overall calibration even when both directions plainly crossed
        # this specific link - reproduced live 2026-09-24, repeatedly, on the
        # same link: share 0.73, read 176/write 176 (later runs: 446 read
        # /write on the SAME link, again near-identical) - every other link
        # on the same trace read "both" cleanly. Verified against packet
        # capture: that link carried exactly 150 requests AND 150 replies,
        # so "one-way" was wrong. Using the read/write balance as a second
        # signal (rather than just lowering BOTH_WAYS, which would risk
        # hiding a link that genuinely carries mostly one direction) fixes
        # this without weakening the real asymmetric-routing case.
        if cls == "one-way":
            r, w = raw_delta[lid]["read"], raw_delta[lid]["write"]
            if min(r, w) / max(r, w, 1) >= 0.6:
                cls = "both"
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
    # Same rule as analyse_capture(): only a firewall on a one-way link is a fault.
    fam = {n["label"]: n.get("probe_family") for n in nodes}
    fw_one_way = [r for r in one_way if "asa" in (fam.get(r["a"]), fam.get(r["b"]))]
    if one_way:
        findings.append(("Asymmetric THROUGH A FIREWALL: " if fw_one_way else "Asymmetric (ECMP): ")
                        + ", ".join(f"{r['a']} to {r['b']}" for r in (fw_one_way or one_way)) +
                        " carried only one direction. Requests and replies took different routes"
                        + (" - a stateful firewall drops that." if fw_one_way else
                           " - normal for a routed core with no firewall on them."))
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
    elif off or fw_one_way:
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


def analyse_capture(nodes, src, dst, ip, packet_log, cand, trace, owners, pre, answering,
                    sent, recv, holders=None) -> dict:
    """The same shape of result as analyse() - path, per-link classification,
    verdict, findings - but built entirely from packets the capture actually
    proved crossed each link, not from background-corrected byte counts.

    No calibration factor, no assumption that background traffic held
    steady during the burst: a link is "both" because captured request AND
    reply packets were both seen on it, full stop. Nothing else happening on
    the network at the same time can shift this - that's the whole point of
    this mode versus analyse()'s counter-based one, which this session
    caught misattributing traffic to unrelated links three separate times
    (a bogus "satellite" path, an unrelated switch's link, and a real link
    wrongly called one-way) before landing on capture as the fix."""
    links = _link_map(nodes)
    by_link: dict[str, dict] = {}
    for e in packet_log:
        by_link.setdefault(e["link_id"], {"request": 0, "reply": 0, "esp": 0})[e["dir"]] += 1

    rows = []
    for lid, l in links.items():
        c = by_link.get(lid)
        req, reply = (c["request"], c["reply"]) if c else (0, 0)
        tunnel = bool(c and c["esp"]) and not (req or reply)
        if tunnel:
            req = reply = c["esp"]      # both directions ride the same ESP pair
        if not req and not reply:
            cls = "none"
        elif recv == 0:
            cls = "forward"          # no replies exist at all this run, by construction
        elif req and reply:
            cls = "both"
        else:
            cls = "one-way"
        rows.append({**l, "read": reply, "write": req, "drops": 0, "packets": req + reply, "tunnel": tunnel,
                     "share": 1.0 if cls == "both" else (0.5 if cls != "none" else 0.0),
                     "use": cls, "candidate": lid in cand["links"]})

    used = [r for r in rows if r["use"] != "none"]
    adj: dict[str, list[tuple[str, dict]]] = {}
    for r in used:
        adj.setdefault(r["a"], []).append((r["b"], r))
        adj.setdefault(r["b"], []).append((r["a"], r))

    prev: dict[str, str | None] = {src["label"]: None}
    q = deque([src["label"]])
    while q:
        u = q.popleft()
        for v, _ in adj.get(u, []):
            if v not in prev:
                prev[v] = u
                q.append(v)
    # ANYCAST (2026-09-28): the path ends at whichever holder of the VIP the
    # captured packets actually reached - the network chose it, not NetView.
    anycast_note = None
    reached = [h for h in (holders or []) if h in prev]
    if reached:
        pick = min(reached, key=lambda k: _depth(prev, k))
        dst = {**dst, "label": pick}
        others = [h for h in holders if h != pick]
        anycast_note = (f"Anycast: {ip} is also held by {', '.join(others)}; the network delivered "
                        f"this source's packets to {pick}." if others else None)
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

    for h in trace["hops"]:
        h["node"] = owners.get(h["ip"]) if h.get("ip") else None

    findings = []
    cand_nodes = set(cand["nodes"])
    path_set = set(path)
    if anycast_note:
        findings.append(anycast_note)
    if not packet_log:
        findings.append("No packets matching this test were captured on any candidate link. "
                        "Either the burst never reached the source's own links, or the capture "
                        "window closed before the burst did - try again, or check the counter "
                        "estimate below for a rough signal.")
    if cand["paths"] > 1:
        idle = sorted(cand_nodes - path_set - {src["label"], dst["label"]})
        findings.append(f"{cand['paths']} equal-length paths exist between these nodes (by hop count)."
                        + (f" Not used this time: {', '.join(idle)}." if idle and path else ""))
    off = [x for x in path if x not in cand_nodes]
    if off and cand["nodes"] and answering:
        findings.append("Traffic left the shortest paths, through " + ", ".join(off) +
                        ". A routing preference, a blocked STP port or a failed link can cause this.")
    one_way = [r for r in used if r["use"] == "one-way"]
    # 2026-09-27: one-way links are only a PROBLEM when a stateful firewall
    # sits on one: it drops the half of a conversation it never saw. Between
    # plain routers/switches, a reply taking the other equal-cost branch
    # (CoreSwitch-HQ-1 -> DistSwitch-HQ-2 instead of -1) is ordinary ECMP, and
    # warning on it made healthy traces read as faults.
    fam = {n["label"]: n.get("probe_family") for n in nodes}
    fw_one_way = [r for r in one_way if "asa" in (fam.get(r["a"]), fam.get(r["b"]))]
    if fw_one_way:
        findings.append("Asymmetric THROUGH A FIREWALL: " +
                        ", ".join(f"{r['a']} to {r['b']}" for r in fw_one_way) +
                        " carried captured packets in only one direction. A stateful firewall "
                        "drops replies for requests it never saw - check OSPF costs either side.")
    elif one_way:
        findings.append("Replies returned by a different equal-cost branch (ECMP): " +
                        ", ".join(f"{r['a']} to {r['b']}" for r in one_way) +
                        " carried captured packets in only one direction. Normal for a routed "
                        "core with no firewall on these links.")
    tunneled = [r for r in used if r.get("tunnel")]
    if tunneled:
        findings.append("Carried inside the VPN (encrypted ESP between the two tunnel heads, both "
                        "proven by this test's own pings): " +
                        ", ".join(f"{r['a']} to {r['b']}" for r in tunneled) +
                        ". Encrypted packets can't be matched to the test's ICMP id, so these hops "
                        "are proven by the tunnel, not by the pings themselves.")
    stray = [r for r in used if r["a"] not in path_set or r["b"] not in path_set]
    if stray and path:
        findings.append("Also carried real packets from this test outside the walked path: " +
                        ", ".join(f"{r['a']} to {r['b']}" for r in stray) + ".")
    hop_nodes = [h["node"] for h in trace["hops"] if h.get("node")]
    missing = [x for x in hop_nodes if x not in path_set]
    if missing and path:
        findings.append("Traceroute named " + ", ".join(missing) +
                        " but no captured packets confirmed traffic through it.")

    if not answering and stop_at:
        level, text = "fail", f"{dst['label']} did not answer. Captured packets got as far as {stop_at}."
    elif not answering:
        level, text = "fail", f"{dst['label']} did not answer, and no captured packets left {src['label']}."
    elif not path or path[-1] != dst["label"]:
        level, text = "warn", "The target answered, but no captured packets showed a complete path."
    elif off or fw_one_way:
        level, text = "warn", f"Reached {dst['label']} in {len(path) - 1} hops, not by the expected route."
    else:
        level, text = "ok", f"Reached {dst['label']} in {len(path) - 1} hops, confirmed by packet capture" + \
            (f" (one of {cand['paths']} equal paths by hop count)." if cand["paths"] > 1 else ".")
        if one_way:
            text += " Replies used an equal-cost return branch."

    return {"path": path, "stop_at": stop_at, "links": rows, "findings": findings,
           "verdict": {"level": level, "text": text}, "answered_by": dst["label"]}


def _depth(prev: dict, k: str) -> int:
    d = 0
    while prev.get(k):
        k = prev[k]
        d += 1
    return d
