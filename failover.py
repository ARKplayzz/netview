"""Failover capture: record a disaster's failover itself (spec:
docs/failover-capture-spec.md, 2026-10-01).

A toggle inside a disaster trace. After the baseline trace, packet capture is
armed on every link the traffic could use (before or after the failure), a
fast continuous probe runs from the source, the nodes are powered off, and the
recording stops once the traffic is stable again (recovered, or on a new path)
or the window ends. The packets are then put on ONE aligned clock and every
probe is followed through the network, so the result can say exactly when the
failure happened, how long failover took, which path the traffic used before,
during and after, and where every lost probe died.

Layout: pure analysis functions first (no CML, unit-tested in
tests/test_failover.py), then the live orchestration `run()` used by
disaster.run(), then history/export helpers used by app.py.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import socket
import statistics
import struct
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_BPF_L3 = "icmp or esp or udp port 500 or udp port 4500"
# The second half matters: a bare `icmp` only matches UNTAGGED frames, so every
# 802.1Q trunk (core <-> server room, core <-> core) came back as an empty pcap
# and the path walk stopped at the first switch (live run 2026-10-06, DCI cut).
BPF = f"{_BPF_L3} or (vlan and ({_BPF_L3}))"
CONFIRM_S = 2.0                 # replies needed in a row (as time) before stopping early
LINUX_INTERVAL_S = 0.2          # `ping -i 0.2`: 5 probes a second (verified on BusyBox)
IOS_INTERVAL_S = 1.0            # IOS/ASA can't set an interval: one `repeat 1` per second
WINDOWS_S = (10, 20, 30, 60)
DEFAULT_WINDOW_S = 20
WARMUP_MAX_S = 30
LIVE_POLL_LINKS = 16
HISTORY_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetView" / "failover"

_ESP_SPI = re.compile(r"SPI=(0x[0-9a-f]+)", re.I)
_ECHO_ID_SEQ = re.compile(r"id=(0x[0-9a-f]+), seq=(\d+)")


# =========================================================== capture scope ===
def capture_scope(nodes: list[dict], src: str, targets: list[str], baseline_links: list[str],
                  down: set[str], cut: set[str] = frozenset()) -> list[str]:
    """Every link worth recording: the baseline's proven links, every shortest
    path to every target/holder BEFORE and AFTER the disaster nodes go, tunnel
    underlays, every leg of any firewall in scope, and the links into the
    disaster nodes (their last packets mark the moment of failure). Only links
    whose two ends are running NOW are returned (a capture on a link to a
    stopped node fails)."""
    import pathtrace                                       # noqa: PLC0415 - avoid an import cycle
    after = [dict(n, up=bool(n.get("up")) and n["label"] not in down,
                  links=[l for l in n.get("links") or [] if l.get("link_id") not in cut]) for n in nodes]
    scope: set[str] = {src}
    for t in targets:
        for ns in (nodes, after):
            scope |= set(pathtrace.candidates(ns, src, t)["nodes"])
    link_map = pathtrace._link_map(nodes)
    for lid in [*baseline_links, *cut]:
        l = link_map.get(lid)
        if l:
            scope |= {l["a"], l["b"]}
    tunnels = pathtrace._tunnels(nodes)
    for x, y in tunnels:
        for head, peer in ((x, y), (y, x)):
            if head in scope:
                for a_, b_ in [(src, head), (head, peer)] + [(peer, t) for t in targets]:
                    for ns in (nodes, after):
                        scope |= set(pathtrace.candidates(ns, a_, b_)["nodes"])
    by_label = {n["label"]: n for n in nodes}
    fws = {n for n in scope if (by_label.get(n) or {}).get("probe_family") == "asa"}
    for l in link_map.values():
        if l["a"] in fws or l["b"] in fws:
            scope |= {l["a"], l["b"]}
    for l in link_map.values():            # the disaster nodes' links into the scope
        if (l["a"] in down and l["b"] in scope) or (l["b"] in down and l["a"] in scope):
            scope |= {l["a"], l["b"]}
    up = {n["label"] for n in nodes if n.get("up")}
    out = [lid for lid, l in link_map.items()
           if l["a"] in scope and l["b"] in scope and l["a"] in up and l["b"] in up]
    return sorted(set(out) | {l for l in [*baseline_links, *cut] if l in link_map})


# ================================================================ parsing ===
def parse_link(packets: list[dict], lid: str, meta: dict, base: float, owners: dict) -> list[dict]:
    """CML's packet listing for one link -> events on that link's own clock
    (`at` = capture start + offset, seconds since the epoch)."""
    out = []
    for p in packets:
        proto = p.get("protocol") or ""
        info = p.get("info") or ""
        try:
            at = base + float(p.get("time") or 0)
        except ValueError:
            continue
        e = {"link_id": lid, "a": meta["a"], "ai": meta.get("ai"), "b": meta["b"], "bi": meta.get("bi"),
             "at": at, "no": p.get("no"), "src": p.get("source"), "dst": p.get("destination"),
             "src_owner": owners.get(p.get("source")), "dst_owner": owners.get(p.get("destination")),
             "icmp_id": None, "seq": None, "spi": None, "info": info}
        if proto == "ICMP" and "Echo" in info:
            m = _ECHO_ID_SEQ.search(info)
            e.update(kind="echo", dir="reply" if info.lstrip().startswith("Echo (ping) reply") else "request",
                     icmp_id=m.group(1) if m else None, seq=int(m.group(2)) if m else None)
        elif proto == "ICMP":
            e.update(kind="unreach", dir="unreach")       # unreachable / TTL exceeded / redirect
        elif proto == "ESP":
            m = _ESP_SPI.search(info)
            e.update(kind="esp", dir="esp", spi=m.group(1).lower() if m else None)
        elif proto in ("ISAKMP", "UDPENCAP"):
            e.update(kind="ike", dir="ike")
        else:
            continue
        out.append(e)
    return out


def parse_pcap(data: bytes, lid: str, meta: dict, owners: dict) -> list[dict]:
    """A link's capture as downloaded from CML (/pcap/<capture key>) -> events.

    Measured 2026-10-01: CML's packet LISTING times count from the first
    captured packet (both links' first packet read 0.000000), so they can't
    be put on a shared timeline. The pcap file carries absolute microsecond
    timestamps from the one CML host clock for every link, plus fields the
    listing drops: ESP sequence numbers, and the original probe inside an ICMP
    unreachable. Big-endian (magic a1b2c3d4 in file order) as CML writes it;
    either byte order and the nanosecond variant are accepted."""
    if len(data or b"") < 24:
        return []
    magic = data[:4].hex()
    if magic in ("a1b2c3d4", "a1b23c4d"):
        order = ">"
    elif magic in ("d4c3b2a1", "4d3cb2a1"):
        order = "<"
    else:
        return []
    nano = magic in ("a1b23c4d", "4d3cb2a1")
    out, off, no = [], 24, 0
    while off + 16 <= len(data):
        ts_s, ts_f, incl, _orig = struct.unpack(order + "IIII", data[off:off + 16])
        pkt = data[off + 16: off + 16 + incl]
        off += 16 + incl
        no += 1
        at = ts_s + ts_f / (1e9 if nano else 1e6)
        i = 12
        etype = pkt[i:i + 2].hex()
        while etype in ("8100", "88a8") and len(pkt) >= i + 6:      # 802.1Q / QinQ tags (trunks)
            i += 4
            etype = pkt[i:i + 2].hex()
        if etype != "0800" or len(pkt) < i + 22:
            continue
        ipo = i + 2
        ihl = (pkt[ipo] & 15) * 4
        proto = pkt[ipo + 9]
        src, dst = socket.inet_ntoa(pkt[ipo + 12:ipo + 16]), socket.inet_ntoa(pkt[ipo + 16:ipo + 20])
        pl = ipo + ihl
        e = {"link_id": lid, "a": meta["a"], "ai": meta.get("ai"), "b": meta["b"], "bi": meta.get("bi"),
             "at": at, "no": no, "src": src, "dst": dst, "src_owner": owners.get(src), "dst_owner": owners.get(dst),
             "icmp_id": None, "seq": None, "spi": None, "esp_seq": None, "info": None}
        if proto == 1 and len(pkt) >= pl + 8:
            typ, code = pkt[pl], pkt[pl + 1]
            if typ in (0, 8):
                iid, seq = struct.unpack(">HH", pkt[pl + 4:pl + 8])
                e.update(kind="echo", dir="reply" if typ == 0 else "request", icmp_id=f"{iid:#06x}", seq=seq)
            elif typ in (3, 5, 11):
                what = {3: "unreachable", 5: "redirect", 11: "TTL exceeded"}[typ]
                e.update(kind="unreach", dir="unreach", info=f"ICMP {what} (code {code})")
                inner = pl + 8                    # the original packet: IP header + first 8 bytes
                if len(pkt) >= inner + 20:
                    iihl = (pkt[inner] & 15) * 4
                    e["orig_src"] = socket.inet_ntoa(pkt[inner + 12:inner + 16])
                    e["orig_dst"] = socket.inet_ntoa(pkt[inner + 16:inner + 20])
                    if pkt[inner + 9] == 1 and len(pkt) >= inner + iihl + 8 and pkt[inner + iihl] == 8:
                        iid, seq = struct.unpack(">HH", pkt[inner + iihl + 4:inner + iihl + 8])
                        e.update(icmp_id=f"{iid:#06x}", seq=seq)
            else:
                continue
        elif proto == 50 and len(pkt) >= pl + 8:
            spi, eseq = struct.unpack(">II", pkt[pl:pl + 8])
            e.update(kind="esp", dir="esp", spi=f"{spi:#x}", esp_seq=eseq)
        elif proto == 17 and len(pkt) >= pl + 8:
            sp, dp = struct.unpack(">HH", pkt[pl:pl + 4])
            if 4500 in (sp, dp) and len(pkt) >= pl + 16 and pkt[pl + 8:pl + 12] != b"\0\0\0\0":
                spi, eseq = struct.unpack(">II", pkt[pl + 8:pl + 16])    # ESP inside UDP (NAT-T)
                e.update(kind="esp", dir="esp", spi=f"{spi:#x}", esp_seq=eseq)
            elif 500 in (sp, dp) or 4500 in (sp, dp):
                e.update(kind="ike", dir="ike", info=f"IKE UDP {sp}->{dp}")
            else:
                continue
        else:
            continue
        out.append(e)
    return out


def select(events: list[dict], ip: str, src_label: str, src_links: set[str]) -> list[dict]:
    """This test's own packets: echoes between the source and the target
    address (by the ICMP id seen on the source's own link), unreachables sent
    back to the source, and ESP/IKE between nodes the echoes prove are tunnel
    heads. Same rules as pathtrace's capture mode."""
    echo = [e for e in events if e["kind"] == "echo"]
    mine = []
    for e in echo:
        if e["dst"] == ip:
            other = e["src_owner"]
        elif e["src"] == ip:
            other = e["dst_owner"]
        else:
            continue
        if other is None or other == src_label:
            mine.append(e)
    own_ids = {e["icmp_id"] for e in mine if e["link_id"] in src_links and e["icmp_id"]}
    if own_ids:
        mine = [e for e in mine if e["icmp_id"] in own_ids]
    src_addrs = {e["src"] for e in mine if e["dir"] == "request"}
    unreach = [e for e in events if e["kind"] == "unreach"
               and (e.get("orig_dst") == ip or e["dst"] in src_addrs or e["dst_owner"] == src_label)
               and (not own_ids or e["icmp_id"] is None or e["icmp_id"] in own_ids)]
    # Tunnel traffic counts when ONE end is a node this test's pings are proven
    # to cross (the source-side head, e.g. the home router): the far head may
    # be cut off and never carry a plain ping in the recording (live run
    # 2026-10-02: Firewall-HQ-1's links died with the disaster, so requiring
    # both ends dropped every ESP packet and the lost probes looked like they
    # died at the home router). Unrelated tunnels (edge<->edge GRE/IPsec) have
    # neither end proven and stay out.
    proven = {n for e in mine for n in (e["a"], e["b"])}
    tun = [e for e in events if e["kind"] in ("esp", "ike") and e["src_owner"] != e["dst_owner"]
           and (e["src_owner"] in proven or e["dst_owner"] in proven)]
    return sorted(mine + unreach + tun, key=lambda e: e["at"])


# ======================================================= clock alignment ===
def _pair_offset(ea: list[dict], eb: list[dict], single_id: bool) -> tuple[float, int, float] | None:
    """Offset of link B's clock against link A's: (median, matches, spread_ms).
    Echoes match exactly on (dir, seq) (or (dir, id, seq) for one-id-per-probe
    sources); ESP, which carries no sequence number in CML's listing, matches
    on SPI by the most common time difference."""
    def key(e):
        return (e["dir"], e["seq"]) if single_id else (e["dir"], e["icmp_id"], e["seq"])
    ka = {}
    for e in ea:
        if e["kind"] == "echo" and e["seq"] is not None:
            ka.setdefault(key(e), e["at"])
    diffs = [e["at"] - ka[key(e)] for e in eb
             if e["kind"] == "echo" and e["seq"] is not None and key(e) in ka]
    if not diffs:                       # ESP: same SPI + ESP sequence number = the same packet
        ea_esp = {(e["spi"], e.get("esp_seq")): e["at"] for e in ea
                  if e["kind"] == "esp" and e.get("esp_seq") is not None}
        diffs = [e["at"] - ea_esp[(e["spi"], e.get("esp_seq"))] for e in eb
                 if e["kind"] == "esp" and (e["spi"], e.get("esp_seq")) in ea_esp]
    if not diffs:
        return None
    med = statistics.median(diffs)
    spread = statistics.median([abs(d - med) for d in diffs]) * 1000 if len(diffs) > 1 else 0.0
    return med, len(diffs), spread


def align(events: list[dict], link_map: dict, ref_links: list[str], single_id: bool) -> dict:
    """Consistency check of the shared clock. pcap timestamps all come from the
    CML host clock, so the same packet (echo seq, or ESP SPI + sequence number)
    seen on two neighbouring links should be one forwarding hop apart (~0 ms).
    Reported, never applied; a large value means a capture problem.
    Returns {"offset": {lid: s}, "quality": {lid: {...}}, "unaligned": [lid]}."""
    by_link: dict[str, list[dict]] = {}
    for e in events:
        by_link.setdefault(e["link_id"], []).append(e)
    links = [l for l in by_link if l in link_map]
    ref = next((l for l in ref_links if l in by_link), links[0] if links else None)
    offset: dict[str, float] = {}
    quality: dict[str, dict] = {}
    if ref is None:
        return {"offset": {}, "quality": {}, "unaligned": [], "reference": None}
    offset[ref] = 0.0
    quality[ref] = {"via": None, "matches": None, "spread_ms": 0.0}
    frontier = [ref]
    while frontier:
        nxt = []
        for la in frontier:
            na = {link_map[la]["a"], link_map[la]["b"]}
            for lb in links:
                if lb in offset or not ({link_map[lb]["a"], link_map[lb]["b"]} & na):
                    continue
                r = _pair_offset(by_link[la], by_link[lb], single_id)
                if r and r[1] >= 2:
                    offset[lb] = offset[la] + r[0]
                    quality[lb] = {"via": la, "matches": r[1], "spread_ms": round(r[2], 2)}
                    nxt.append(lb)
        frontier = nxt
    unaligned = [l for l in links if l not in offset]
    for l in unaligned:                                    # keep the whole-second start time
        offset[l] = 0.0
        quality[l] = {"via": None, "matches": 0, "spread_ms": None, "unaligned": True}
    return {"offset": offset, "quality": quality, "unaligned": unaligned, "reference": ref}


# ======================================================== per-probe paths ===
def _stitch(src: str, evs: list[dict]) -> list[str]:
    """Node path from a time-ordered list of link events, starting at src."""
    path = [src]
    for e in evs:
        a, b = e["a"], e["b"]
        if path[-1] == a and b != a:
            nxt = [b]
        elif path[-1] == b:
            nxt = [a]
        elif a in path and b not in path:
            nxt = [b]
        elif b in path and a not in path:
            nxt = [a]
        elif a not in path and b not in path:
            nxt = [a, b]                    # a gap (e.g. an uncaptured link): keep order
        else:
            nxt = []
        for x in nxt:
            if x != path[-1]:
                path.append(x)
    return path


def _walk(src: str, evs: list[dict], endpoints: set[str]) -> list[str]:
    """Hop path over the links a packet was SEEN on: breadth-first from the
    source over that subgraph, to an endpoint if one was reached, else to the
    node seen last in time. Each capture stamps its own packets, so two hops
    can be tens of microseconds out of order (live run 2026-10-02 drew the home
    router twice); following the links, not the timestamps, can't do that."""
    if not evs:
        return [src]
    adj: dict[str, set[str]] = {}
    for e in evs:
        adj.setdefault(e["a"], set()).add(e["b"])
        adj.setdefault(e["b"], set()).add(e["a"])
    if src not in adj:
        return _stitch(src, sorted(evs, key=lambda e: e["t"]))
    prev = {src: None}
    q = [src]
    while q:
        u = q.pop(0)
        for v in sorted(adj.get(u, ())):
            if v not in prev:
                prev[v] = u
                q.append(v)
    goal = next((n for n in endpoints if n in prev and n != src), None)
    if goal is None:
        last_t: dict[str, float] = {}
        for e in evs:
            for n in (e["a"], e["b"]):
                last_t[n] = max(last_t.get(n, e["t"]), e["t"])
        far = [n for n in prev if n != src]
        if not far:
            return [src]
        # the node seen last, preferring the deepest one on a tie
        depth = {}
        for n in prev:
            d, x = 0, n
            while prev[x] is not None:
                x, d = prev[x], d + 1
            depth[n] = d
        goal = max(far, key=lambda n: (depth[n], last_t.get(n, 0)))
    path, x = [], goal
    while x is not None:
        path.append(x)
        x = prev[x]
    return path[::-1]


def build_probes(sel: list[dict], src: str, single_id: bool, endpoints: set[str],
                 src_links: set[str], interval: float) -> list[dict]:
    """One record per probe: send time, request path, reply path, outcome,
    who answered, where it died. Times are already aligned (`t`)."""
    def pkey(e):
        return e["seq"] if single_id else e["icmp_id"]
    echo = [e for e in sel if e["kind"] == "echo" and pkey(e) is not None]
    esp = [e for e in sel if e["kind"] == "esp"]
    unreach = [e for e in sel if e["kind"] == "unreach"]
    groups: dict = {}
    for e in echo:
        groups.setdefault(pkey(e), []).append(e)
    probes = []
    for k, evs in groups.items():
        req = sorted([e for e in evs if e["dir"] == "request"], key=lambda e: e["t"])
        rep = sorted([e for e in evs if e["dir"] == "reply"], key=lambda e: e["t"])
        if not req and not rep:
            continue
        own_req = [e for e in req if e["link_id"] in src_links]
        sent = own_req[0]["t"] if own_req else (req[0]["t"] if req else rep[0]["t"] - 0.003)
        # ESP that carried THIS request through a tunnel: in the gap between two
        # request sightings that don't share a node, or after the last one.
        # Only ESP leaving a node this request has reached counts (not the reply's
        # ESP coming back, nor another tunnel's). After the LAST sighting the
        # window is open only for an unanswered probe: that is where it died.
        tun = []
        for i, e in enumerate(req):
            lo = e["t"]
            if i + 1 < len(req):
                if {e["a"], e["b"]} & {req[i + 1]["a"], req[i + 1]["b"]}:
                    continue
                hi = req[i + 1]["t"]
            elif rep:
                continue
            else:
                hi = lo + 0.15
            seen_nodes = {n for x in req[:i + 1] for n in (x["a"], x["b"])}
            tun += [x for x in esp if lo - 0.002 <= x["t"] <= hi + 0.002 and x["src_owner"] in seen_nodes]
        req_path = _walk(src, req + tun, endpoints)
        rtun = []
        for i, e in enumerate(rep):
            if i + 1 < len(rep) and not ({e["a"], e["b"]} & {rep[i + 1]["a"], rep[i + 1]["b"]}):
                rtun += [x for x in esp if e["t"] - 0.002 <= x["t"] <= rep[i + 1]["t"] + 0.002]
        rep_evs = sorted(rep + rtun, key=lambda e: e["t"])
        answered_by = None
        if rep:
            first = rep[0]
            answered_by = next((n for n in (first["a"], first["b"]) if n in endpoints), None)
        if answered_by is None:
            answered_by = next((n for n in reversed(req_path) if n in endpoints), None) if rep else None
        rep_path = list(reversed(_walk(src, rep_evs, endpoints))) if rep else []
        got = any(e["link_id"] in src_links for e in rep)
        # An ICMP error carries the original probe's id/seq: match on it exactly;
        # fall back to timing only when the error didn't include it.
        un = [u for u in unreach if (u["seq"] is not None and pkey(u) == k)
              or (u["seq"] is None and sent <= u["t"] <= sent + 1.5)]
        reached = any(n in endpoints for e in req for n in (e["a"], e["b"]))
        if got:
            outcome = "ok"
        elif un:
            outcome = "unreachable"
        elif reached:
            outcome = "lost-reply"
        else:
            outcome = "lost-request"
        died = None
        if outcome != "ok" and len(req_path) >= 2:
            # the deepest link the request reached, and the node it was heading into
            a_, b_ = req_path[-2], req_path[-1]
            on = [e for e in req + tun if {e["a"], e["b"]} == {a_, b_}]
            last = max(on, key=lambda e: e["t"]) if on else (req + tun)[-1]
            died = {"link_id": last["link_id"], "link": f"{a_} - {b_}", "into": b_, "t": round(last["t"], 4)}
            if un:
                died["unreachable_from"] = un[0]["src_owner"] or un[0]["src"]
        rtt = None
        if got and own_req:
            back = [e for e in rep if e["link_id"] in src_links]
            rtt = round((back[0]["t"] - own_req[0]["t"]) * 1000, 2)
        probes.append({"key": k, "sent": sent, "outcome": outcome, "req_path": req_path,
                       "rep_path": rep_path, "answered_by": answered_by, "died_at": died, "rtt_ms": rtt,
                       "req_links": [e["link_id"] for e in req + tun], "rep_links": [e["link_id"] for e in rep_evs]})
    probes.sort(key=lambda p: p["sent"])
    return probes


def fill_missing(probes: list[dict], console: dict[int, float], interval: float, single_id: bool) -> list[dict]:
    """Probes the console sent but the capture never saw anywhere (e.g. the
    source link's capture failed) - added as 'unknown'/'ok' from the console so
    the probe count is honest. Only possible for seq-numbered (Linux) probes."""
    if not single_id or not probes:
        return probes
    have = {p["key"] for p in probes}
    known = [(p["key"], p["sent"]) for p in probes if isinstance(p["key"], int)]
    if len(known) < 2:
        return probes
    (k0, t0), (k1, t1) = known[0], known[-1]
    rate = (t1 - t0) / (k1 - k0) if k1 != k0 else interval
    for s in range(min(k for k, _ in known), max(max(k for k, _ in known), max(console or [0])) + 1):
        if s not in have:
            probes.append({"key": s, "sent": t0 + (s - k0) * rate,
                           "outcome": "ok" if s in console else "unknown", "req_path": [], "rep_path": [],
                           "answered_by": None, "died_at": None, "rtt_ms": None, "req_links": [],
                           "rep_links": [], "inferred": True})
    probes.sort(key=lambda p: p["sent"])
    return probes


# =========================================================== the analysis ===
def failure_moment(probes: list[dict], sel: list[dict], down: set[str], cut: set[str] = frozenset()) -> float | None:
    """F: the last moment a now-dead node FORWARDED this test's traffic - the
    same packet (a probe's echo, or an ESP packet by SPI + sequence number)
    seen on two different links of the same disaster node, at the later of the
    two sightings. Independent of CML's API latency and of local clocks.
    ESP matters: in a VPN path the routers in the middle (edges, carriers)
    only ever carry the encrypted copy."""
    best = None
    groups: dict = {}
    for e in sel:
        if e["kind"] == "echo" and e.get("probe") is not None:
            groups.setdefault(("echo", e["probe"], e["dir"]), []).append(e)
        elif e["kind"] == "esp" and e.get("esp_seq") is not None:
            groups.setdefault(("esp", e["spi"], e["esp_seq"]), []).append(e)
    for evs in groups.values():
        for n in down:
            on = sorted([e for e in evs if n in (e["a"], e["b"])], key=lambda e: e["t"])
            links = {e["link_id"] for e in on}
            if len(links) >= 2:
                best = max(best or on[-1]["t"], on[-1]["t"])
    # A cut cable: the last of this test's packets seen on it (a link is one
    # capture point, so no second sighting is needed - nothing else writes it).
    for e in sel:
        if e["link_id"] in cut and e["kind"] in ("echo", "esp"):
            best = max(best or e["t"], e["t"])
    return best


def _runs(flags: list[bool]) -> int:
    n, prev = 0, False
    for f in flags:
        if f and not prev:
            n += 1
        prev = f
    return n


def analyse(pcaps: dict, link_map: dict, owners: dict, *, src: str, ip: str,
            endpoints: set[str], down: set[str], baseline_path: list[str], single_id: bool,
            interval: float, console: dict[int, float], times_local: dict, window_s: int,
            completed_by: str, coverage: dict, device_logs: list[dict] | None = None,
            cut: dict[str, str] | None = None) -> dict:
    """Everything the result shows, from the raw pulls. `times_local` holds local
    epoch times: T0 (stop requested), stopped {label: t}, T1 (all stopped),
    end (recording stopped), probe_started. Returns the `failover` dict (spec §8)."""
    events = []
    for lid, data in pcaps.items():
        meta = link_map.get(lid)
        if meta and data:
            events += parse_pcap(data, lid, meta, owners)
    src_links = {lid for lid, l in link_map.items() if src in (l["a"], l["b"])}
    sel = select(events, ip, src, src_links)
    for e in sel:
        e["t"] = e["at"]                  # one CML host clock, microseconds, on every link
    al = align(sel, link_map, sorted(src_links), single_id)     # a check, not a correction
    probes = build_probes(sel, src, single_id, endpoints, src_links, interval)
    pk = {}
    for p in probes:
        for lid in p["req_links"] + p["rep_links"]:
            pk.setdefault(lid, set()).add(p["key"])
    for e in sel:
        if e["kind"] == "echo":
            e["probe"] = e["seq"] if single_id else e["icmp_id"]
    # local clock -> capture clock, from console reply arrival vs the reply on the source link
    lc = []
    if single_id and console:
        rep_at_src = {e["seq"]: e["t"] for e in sel if e["kind"] == "echo" and e["dir"] == "reply"
                      and e["link_id"] in src_links}
        lc = [console[s] - rep_at_src[s] for s in console if s in rep_at_src]
    l2c = statistics.median(lc) if lc else None
    console_lag_ms = round((statistics.median(lc) - min(lc)) * 1000) if lc else None
    l2c_basis = "console replies vs captured replies" if lc else None
    if l2c is None and times_local.get("capture_started") and times_local.get("capture_started_cml"):
        # IOS/ASA sources: no per-reply console times; the capture start on the
        # source link (whole seconds) vs when NetView asked for it: about +-1.5 s.
        cml_t = datetime.strptime(times_local["capture_started_cml"], "%Y-%m-%dT%H:%M:%SZ")             .replace(tzinfo=timezone.utc).timestamp()
        l2c = times_local["capture_started"] - cml_t - 0.5
        l2c_basis = "capture start times (about +-1.5 s)"

    def to_c(t_local):
        return None if t_local is None or l2c is None else t_local - l2c
    probes = fill_missing(probes, {s: (to_c(t) or t) for s, t in console.items()}, interval, single_id)

    cut = cut or {}
    F = failure_moment(probes, sel, down, set(cut.values()))
    T0c, T1c = to_c(times_local.get("T0")), to_c(times_local.get("T1"))
    F_src = ("last packet a disaster node forwarded" if not cut else
             "last packet across a cut link" if not down else
             "last packet a disaster node forwarded or a cut link carried")
    if F is None:
        F = T1c if T1c is not None else (probes[0]["sent"] if probes else 0.0)
        F_src = ("no packet crossed a disaster node; power-off completion used" if not cut else
                 "no packet crossed the failure; the moment CML reported it done used") if T1c is not None \
            else "no reference available"

    def rel(t):
        return None if t is None else round(t - F, 3)

    base_set = [p for p in probes if p["sent"] < F and p["outcome"] == "ok" and p["req_path"]]
    base_sig = None
    if base_set:
        sigs = {}
        for p in base_set:
            sigs[tuple(p["req_path"])] = sigs.get(tuple(p["req_path"]), 0) + 1
        base_sig = max(sigs, key=sigs.get)
    elif baseline_path:
        base_sig = tuple(baseline_path)
    after = [p for p in probes if p["sent"] >= F]
    ok_after = [p for p in after if p["outcome"] == "ok"]
    final_sig = tuple(ok_after[-1]["req_path"]) if ok_after and ok_after[-1]["req_path"] else None

    # paths
    paths: dict[tuple, dict] = {}
    for p in probes:
        if not p["req_path"]:
            continue
        sig = (tuple(p["req_path"]), tuple(p["rep_path"]))
        d = paths.setdefault(sig, {"hops": list(sig[0]), "reply_hops": list(sig[1]), "first": p["sent"],
                                   "last": p["sent"], "probes": 0, "replies": 0, "rtts": [],
                                   "answered_by": {}, "outcomes": {}})
        d["first"], d["last"] = min(d["first"], p["sent"]), max(d["last"], p["sent"])
        d["probes"] += 1
        d["outcomes"][p["outcome"]] = d["outcomes"].get(p["outcome"], 0) + 1
        if p["outcome"] == "ok":
            d["replies"] += 1
            if p["rtt_ms"] is not None:
                d["rtts"].append(p["rtt_ms"])
            if p["answered_by"]:
                d["answered_by"][p["answered_by"]] = d["answered_by"].get(p["answered_by"], 0) + 1
    plist = []
    for i, (sig, d) in enumerate(sorted(paths.items(), key=lambda kv: kv[1]["first"])):
        role = ("baseline" if sig[0] == base_sig and d["first"] < F else
                "failover" if sig[0] == final_sig and d["replies"] else
                "transient")
        if role == "baseline" and sig[0] == final_sig and d["last"] >= F:
            role = "baseline (unchanged)"
        plist.append({"id": chr(65 + i) if i < 26 else f"P{i}", "role": role, "hops": d["hops"],
                      "reply_hops": d["reply_hops"], "first": rel(d["first"]), "last": rel(d["last"]),
                      "probes": d["probes"], "replies": d["replies"], "outcomes": d["outcomes"],
                      "rtt_ms": round(statistics.median(d["rtts"]), 2) if d["rtts"] else None,
                      "answered_by": max(d["answered_by"], key=d["answered_by"].get) if d["answered_by"] else None})
    pid = {tuple(p["hops"]) + ("|",) + tuple(p["reply_hops"]): p["id"] for p in plist}
    for p in probes:
        p["path_id"] = pid.get(tuple(p["req_path"]) + ("|",) + tuple(p["rep_path"])) if p["req_path"] else None

    # metrics
    lost_flags = [p["outcome"] != "ok" and p["outcome"] != "unknown" for p in after]
    lost_after = [p for p in after if p["outcome"] not in ("ok", "unknown")]
    first_lost = lost_after[0] if lost_after else None
    last_lost = lost_after[-1] if lost_after else None
    first_ok_after_loss = next((p for p in after if last_lost and p["sent"] > last_lost["sent"]
                                and p["outcome"] == "ok"), None)
    first_ok_final = next((p for p in after if p["outcome"] == "ok" and final_sig
                           and tuple(p["req_path"]) == final_sig and
                           (not last_lost or p["sent"] > last_lost["sent"])), None)
    reply_t = None
    if first_ok_final:
        reps = [e["t"] for e in sel if e["kind"] == "echo" and e["dir"] == "reply"
                and e.get("probe") == first_ok_final["key"] and e["link_id"] in src_links]
        reply_t = min(reps) if reps else first_ok_final["sent"]
    changed = bool(base_sig and final_sig and base_sig != final_sig)
    detect = next((p for p in after if p["outcome"] != "ok" or
                   (p["req_path"] and base_sig and tuple(p["req_path"]) != base_sig)), None)
    periods = _runs(lost_flags)
    tail_lost = bool(after) and after[-1]["outcome"] not in ("ok", "unknown")
    if periods > 1:
        verdict = "unstable"
    elif not lost_after and not changed:
        verdict = "no impact"
    elif tail_lost or (lost_after and not first_ok_after_loss):
        verdict = "not recovered"
    else:
        verdict = "failed over"
    rtt_b = [p["rtt_ms"] for p in probes if p["sent"] < F and p["rtt_ms"] is not None]
    rtt_a = [p["rtt_ms"] for p in after if p["rtt_ms"] is not None and (not last_lost or p["sent"] > last_lost["sent"])]
    ans_b = [p["answered_by"] for p in base_set if p["answered_by"]]
    ans_a = [p["answered_by"] for p in after if p["outcome"] == "ok" and p["answered_by"]
             and (not last_lost or p["sent"] > last_lost["sent"])]
    metrics = {
        "verdict": verdict,
        "failover_s": round(reply_t - F, 3) if (reply_t is not None and (lost_after or changed)) else None,
        "outage_s": round(first_ok_after_loss["sent"] - first_lost["sent"], 3)
        if (first_lost and first_ok_after_loss) else None,
        "detection_s": round(detect["sent"] - F, 3) if detect else None,
        "from_click_s": round(reply_t - T0c, 3) if (reply_t is not None and T0c is not None and
                                                     (lost_after or changed)) else None,
        "sent_after_F": len(after), "lost": len(lost_after), "outage_periods": periods,
        "rtt_before_ms": round(statistics.median(rtt_b), 2) if rtt_b else None,
        "rtt_after_ms": round(statistics.median(rtt_a), 2) if rtt_a else None,
        "path_changed": changed,
        "answered_by": [max(set(ans_b), key=ans_b.count) if ans_b else None,
                        max(set(ans_a), key=ans_a.count) if ans_a else None],
        "completed_by": completed_by, "resolution_s": interval,
    }
    # events (timeline markers), relative to F
    ev = []
    if T0c is not None:
        ev.append({"t": rel(T0c), "kind": "power_off_requested",
                   "text": ("Cable cut requested (click)" if cut and not down else
                            "Power-off and cable cut requested (click)" if cut else "Power-off requested (click)")})
    for lab, t in sorted((times_local.get("stopped") or {}).items(), key=lambda kv: kv[1] or 0):
        if to_c(t) is not None:
            ev.append({"t": rel(to_c(t)), "kind": "node_stopped",
                       "text": f"{lab} cable cut (link STOPPED by CML)" if lab in cut
                       else f"{lab} reported STOPPED by CML"})
    if T1c is not None:
        ev.append({"t": rel(T1c), "kind": "all_stopped",
                   "text": ("All links cut" if cut and not down else "All nodes stopped" +
                            (" and links cut" if cut else "")) + ": the window starts"})
    ev.append({"t": 0.0, "kind": "failure", "text": f"Failure observed (F): {F_src}"})
    if first_lost:
        ev.append({"t": rel(first_lost["sent"]), "kind": "first_loss", "text": f"First lost probe ({first_lost['key']})"})
    if detect and detect is not first_lost and detect["outcome"] == "ok":
        ev.append({"t": rel(detect["sent"]), "kind": "path_change", "text": "First probe on a different path"})
    if reply_t is not None and (lost_after or changed):
        ev.append({"t": rel(reply_t), "kind": "first_reply_new", "text": "First reply on the final path"})
    for e in sel:
        if e["kind"] == "ike":
            ev.append({"t": rel(e["t"]), "kind": "ike", "text": f"IKE {e['src_owner'] or e['src']} -> "
                                                                 f"{e['dst_owner'] or e['dst']}: {e['info'][:60]}"})
    if times_local.get("end") and to_c(times_local["end"]) is not None:
        ev.append({"t": rel(to_c(times_local["end"])), "kind": "end", "text": f"Recording stopped ({completed_by})"})
    ev.sort(key=lambda x: x["t"])

    packets = []
    for e in sel:
        packets.append({"t": rel(e["t"]), "link_id": e["link_id"], "a": e["a"], "ai": e["ai"], "b": e["b"],
                        "bi": e["bi"], "kind": e["kind"], "dir": e["dir"], "seq": e["seq"], "icmp_id": e["icmp_id"],
                        "spi": e["spi"], "src": e["src"], "dst": e["dst"], "probe": e.get("probe"),
                        "info": e["info"] if e["kind"] in ("unreach", "ike") else None})
    probe_out = [{"key": p["key"], "sent": rel(p["sent"]), "outcome": p["outcome"], "path": p.get("path_id"),
                  "answered_by": p["answered_by"], "rtt_ms": p["rtt_ms"],
                  "died_at": dict(p["died_at"], t=rel(p["died_at"]["t"])) if p["died_at"] else None,
                  "inferred": p.get("inferred", False)} for p in probes]
    dl = []
    for d in device_logs or []:
        t = to_c(d.get("t_local"))
        dl.append(dict({k: v for k, v in d.items() if k != "t_local"}, t=rel(t)))
    return {
        "clock": {"source": "CML host clock: pcap timestamps (microseconds), the same clock on every link",
                  "hop_check_links": len(al["offset"]) - len(al["unaligned"]),
                  "worst_hop_ms": round(max([abs(o) * 1000 for o in al["offset"].values()] or [0]), 3),
                  "local_to_capture_s": round(l2c, 3) if l2c is not None else None,
                  "console_lag_ms": console_lag_ms, "F_basis": F_src, "local_clock_basis": l2c_basis},
        "times": {"T0": rel(T0c), "T1": rel(T1c), "end": rel(to_c(times_local.get("end"))),
                  "stopped": {k: rel(to_c(v)) for k, v in (times_local.get("stopped") or {}).items()},
                  "window_s": window_s, "F_utc": datetime.fromtimestamp(F, tz=timezone.utc).isoformat()},
        "probe": {"interval_s": interval, "sent": len(probes), "family": "linux" if single_id else "ios",
                  "dest": ip},
        "metrics": metrics, "paths": plist, "probes": probe_out, "events": ev, "packets": packets,
        "coverage": coverage, "device_logs": dl,
    }


# =========================================================== orchestration ===
def _live_state(console_ok: dict, sent_times: list[float], now: float, t0: float, t1: float | None,
                interval: float, new_path_seen: bool) -> dict:
    """Decide-so-far view of the probes from the console: ok / lost / pending."""
    decided = [(i, t) for i, t in enumerate(sent_times) if now - t > 1.2]
    marks = "".join("!" if i in console_ok else "." for i, _ in decided)
    after = [(i, t) for i, t in decided if t >= t0]
    flags = [i not in console_ok for i, _ in after]
    k = max(1, math.ceil(CONFIRM_S / interval))
    streak = 0
    for f in reversed(flags):
        if f:
            break
        streak += 1
    return {"marks": marks, "sent": len(sent_times), "lost": sum(flags), "periods": _runs(flags),
            "streak": streak, "k": k, "had_loss": any(flags), "new_path": new_path_seen}


def run(job_id: str, cml, con, spec: dict, baseline: dict, d: dict, stop: threading.Event,
        stage, set_, out: dict, write_record) -> None:
    """The failover-capture part of a disaster job (between the baseline trace
    and the restore). Fills out["power_off"] (restore is owed once it exists)
    and out["failover"]."""
    import disaster                                        # noqa: PLC0415 - shared power helpers
    import pathtrace                                       # noqa: PLC0415
    from console import BackgroundPing, Console, ContinuousPing   # noqa: PLC0415

    sel = d["nodes"]
    cut = dict(d.get("links") or {})                       # {name: link_id}: cables to unplug
    cut_ids = set(cut.values())
    window = int(d.get("window_s") or DEFAULT_WINDOW_S)
    stop_early = bool(d.get("stop_early", True))
    src, nodes = spec["source"], spec["nodes"]
    ip = baseline.get("target_ip") or spec.get("target_ip")
    holders = list(spec.get("vip_holders") or [])
    tgt = baseline.get("target") or (spec.get("target") or {}).get("label")
    endpoints = set(holders) | ({tgt} if tgt else set())
    linux = src.get("probe_family") == "linux"
    interval = LINUX_INTERVAL_S if linux else IOS_INTERVAL_S
    link_map = pathtrace._link_map(nodes)
    base_links = [l["id"] for l in (baseline.get("links") or []) if l.get("use") not in (None, "none")]
    down = set(sel)

    # -- arm capture
    scope = capture_scope(nodes, src["label"], sorted(endpoints), base_links, down, cut_ids)
    stage(f"arming packet capture on {len(scope)} links")
    t_arm = time.time()
    starts = pathtrace._capture_start_all(cml, scope, maxtime=window + 240, bpfilter=BPF, maxpackets=20000)
    started = [l for l, v in starts.items() if v]

    # Capture keys NOW, while every node is up (live run 2026-10-02: CML tears
    # down the captures on a stopped node's links, so keys read after the
    # window were gone and those 19 links - the ones the failure happens on -
    # came back empty).
    def key_of(lid):
        try:
            return lid, ((cml.capture_status(lid) or {}).get("config") or {}).get("link_capture_key")
        except Exception:                              # noqa: BLE001
            return lid, None
    with ThreadPoolExecutor(max_workers=8) as ex:
        keys = dict(ex.map(key_of, started))
    doomed = [l for l in started if keys.get(l) and (link_map[l]["a"] in down or link_map[l]["b"] in down
                                                    or l in cut_ids)]
    snapshots: dict[str, bytes] = {}
    snap_stop = threading.Event()

    def snapshot_loop():
        """Keep the latest pcap of every link into a disaster node until shortly
        after the nodes stop - the fallback if CML has discarded those captures."""
        def one(lid):
            try:
                b = cml.capture_pcap(keys[lid])
                if b and len(b) > 24 and len(b) >= len(snapshots.get(lid, b"")):
                    snapshots[lid] = b
            except Exception:                          # noqa: BLE001 - the node may already be gone
                pass
        while not snap_stop.is_set():
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(one, doomed))
            snap_stop.wait(0.7)
    snap_thread = threading.Thread(target=snapshot_loop, daemon=True)
    coverage = {"in_scope": len(scope), "captured": len(started), "arm_seconds": round(time.time() - t_arm, 1),
                "missing": [f"{link_map[l]['a']} - {link_map[l]['b']}" for l in scope
                            if not starts.get(l) and l in link_map]}
    t_cap_local = time.time()
    src_links = {lid for lid in scope if src["label"] in (link_map[lid]["a"], link_map[lid]["b"])}
    if not any(starts.get(l) for l in src_links):
        for lid in started:
            try:
                cml.capture_stop(lid)
            except Exception:                          # noqa: BLE001
                pass
        raise RuntimeError("packet capture did not start on the source's own link, so the failover "
                           "could not be timed; nothing was powered off")

    # -- probe + warm-up
    console_ok: dict = {}
    sent_times: list[float] = []
    probe_err = {}
    if linux:
        cp = ContinuousPing(cml, src["label"], ip, interval, line=src.get("console_line", 0),
                            on_reply=lambda s, t: console_ok.__setitem__(s, t))
        stage("warm-up: starting the probe")
        if not cp.start():
            raise RuntimeError(f"the probe could not start on {src['label']}: {cp.error}")
        t_probe = cp.started_at

        def refresh_sent(now):
            n = int((now - t_probe) / interval) + 1
            while len(sent_times) < n:
                sent_times.append(t_probe + len(sent_times) * interval)
        stopper = cp.stop
    else:
        marks: list = []

        def tick(i, mark, raw):
            sent_times.append(time.time() - 0.5)
            if mark == "!":
                console_ok[i] = time.time()
            marks.append(mark)
        bp = BackgroundPing(cml, node=src["label"], dest=ip, duration_s=window + 300, on_tick=tick,
                            stop_event=threading.Event())
        bp.start()
        t_probe = time.time()

        def refresh_sent(now):
            return None

        def stopper():
            bp.stop_event.set()
            res = bp.join()
            if res.get("error"):
                probe_err["error"] = res["error"]
    try:
        t_w = time.time()
        while time.time() - t_w < WARMUP_MAX_S and not stop.is_set():
            now = time.time()
            refresh_sent(now)
            ok = sorted(console_ok)
            stage(f"warm-up: watching the healthy path ({len(ok)} replies)")
            if now - t_w >= 3 and len(ok) >= 5 and all(i in console_ok for i in range(len(sent_times) - 7, len(sent_times) - 2)
                                                       if i >= 0):
                break
            time.sleep(0.5)
        else:
            if not stop.is_set():
                raise RuntimeError("the healthy path never answered steadily during warm-up; nothing was powered off")
        if stop.is_set():
            raise RuntimeError("cancelled before any node was powered off")

        # -- power off
        ids = {n["label"]: n["id"] for n in cml.nodes_full().values() if n["label"] in sel}
        write_record()
        stage(disaster.describe(sel, cut))
        T0 = time.time()
        out["power_off"] = {x: "requested" for x in [*sel, *cut]}
        stopped: dict[str, float] = {}

        def power():
            res = disaster._cut(cml, cut, "stop") if cut else {}
            if ids:
                res.update(disaster._power(cml, ids, "stop", {"STOPPED", "DEFINED_ON_CORE"}, 180))
            out["power_off"] = res
        pt = threading.Thread(target=power, daemon=True)
        if doomed:
            snap_thread.start()
        pt.start()
        # Watch while CML works: per-node STOPPED times come from polling here.
        T1 = None
        live_links = [l for l in scope if l not in base_links and starts.get(l)][:LIVE_POLL_LINKS]
        live_seen: dict[str, int] = {l: 0 for l in live_links}
        new_path_seen = False
        new_path_link = None
        completed_by = None
        last_poll = 0.0
        last_state = 0.0
        t_ok_since = None
        while True:
            now = time.time()
            refresh_sent(now)
            if T1 is None and now - last_state >= 1.0:
                last_state = now
                if sel:
                    for n in cml.nodes_full().values():
                        if n["label"] in sel and n["label"] not in stopped and n.get("state") in ("STOPPED", "DEFINED_ON_CORE"):
                            stopped[n["label"]] = time.time()
                for name, lid in cut.items():
                    if name not in stopped:
                        try:
                            if cml.link_state(lid) == "STOPPED":
                                stopped[name] = time.time()
                        except Exception:              # noqa: BLE001 - polled again next second
                            pass
                if len(stopped) == len(sel) + len(cut) or not pt.is_alive():
                    T1 = time.time()
                    for x in [*sel, *cut]:
                        stopped.setdefault(x, T1)
            if T1 is not None and now - last_poll >= 1.0 and live_links and not new_path_seen:
                last_poll = now
                for lid in live_links:
                    try:
                        pk = cml.pcap_packets(lid)
                    except Exception:                  # noqa: BLE001 - cosmetic early-stop input only
                        continue
                    if len(pk) > live_seen[lid]:
                        fresh = pk[live_seen[lid]:]
                        live_seen[lid] = len(pk)
                        evs = select(parse_link(fresh, lid, link_map[lid], 0.0, spec.get("owners") or {}),
                                     ip, src["label"], set())
                        if any(e["kind"] == "echo" for e in evs):
                            new_path_seen, new_path_link = True, f"{link_map[lid]['a']} - {link_map[lid]['b']}"
                            break
            st = _live_state(console_ok, sent_times, now, T0, T1, interval, new_path_seen)
            el = max(0.0, now - T1) if T1 else 0.0
            state = ("waiting for CML to stop the nodes" if T1 is None else
                     "unstable" if st["periods"] > 1 else
                     "recovering" if st["had_loss"] and st["streak"] else
                     "down" if st["had_loss"] else "no loss yet")
            set_(job_id, failover_live={"marks": st["marks"], "sent": st["sent"], "lost": st["lost"],
                                        "elapsed_s": round(el, 1), "window_s": window, "state": state,
                                        "stopped": sorted(stopped), "new_path": new_path_link,
                                        "interval_s": interval})
            stage(f"watching the failover: {el:.1f} s / {window} s - {st['sent']} sent, {st['lost']} lost, {state}")
            if stop.is_set():
                completed_by = "cancelled"
                break
            if T1 is not None:
                cond = None
                if stop_early and st["periods"] <= 1 and st["streak"] >= st["k"]:
                    if st["had_loss"]:
                        cond = "recovered and stable"
                    elif new_path_seen:
                        cond = "stable on a new path"
                if cond:
                    t_ok_since = t_ok_since or now
                    completed_by = cond
                    break
                if el >= window:
                    completed_by = "window end"
                    break
            time.sleep(0.25)
        T_end = time.time()
        stage("stopping the probe")
        stopper()
        pt.join(timeout=200)
        out["power_off_seconds"] = round((T1 or T_end) - T0)

        # -- collect
        stage(f"collecting packets from {len(started)} links")
        snap_stop.set()
        for lid in started:
            try:
                cml.capture_stop(lid)
            except Exception:                          # noqa: BLE001
                pass

        def pcap(item):
            lid, k = item
            if not k:
                return lid, None
            try:
                return lid, cml.capture_pcap(k)
            except Exception:                          # noqa: BLE001
                return lid, None
        with ThreadPoolExecutor(max_workers=8) as ex:
            pcaps = {lid: b for lid, b in ex.map(pcap, keys.items()) if b}
        used_snap = []
        for lid in doomed:                    # CML discarded it, or it is shorter than the snapshot
            if lid in snapshots and len(pcaps.get(lid) or b"") < len(snapshots[lid]):
                pcaps[lid] = snapshots[lid]
                used_snap.append(f"{link_map[lid]['a']} - {link_map[lid]['b']}")
        coverage["from_snapshot"] = used_snap
        coverage["with_packets"] = sum(1 for v in pcaps.values() if v and len(v) > 24)
        coverage["no_pcap"] = [f"{link_map[l]['a']} - {link_map[l]['b']}" for l in started
                               if l not in pcaps and l in link_map]

        # -- device logs (optional)
        logs = []
        if d.get("device_logs"):
            stage("collecting device logs")
            ends = [e for lid in cut_ids for e in (link_map[lid]["a"], link_map[lid]["b"])]
            logs = collect_device_logs(cml, spec, baseline, sel, T0 - 10, T_end + 5, first=ends)

        stage("building the failover timeline")
        fo = analyse(pcaps, link_map, spec.get("owners") or {}, src=src["label"], ip=ip,
                     endpoints=endpoints, down=down, baseline_path=list(baseline.get("path") or []),
                     single_id=linux, interval=interval,
                     console=dict(console_ok) if linux else {},
                     times_local={"T0": T0, "T1": T1, "stopped": stopped, "end": T_end, "probe_started": t_probe,
                                  "capture_started": t_cap_local,
                                  "capture_started_cml": max((v for l, v in starts.items() if v and l in src_links),
                                                             default=None)},
                     window_s=window, completed_by=completed_by or "window end", coverage=coverage,
                     device_logs=logs, cut=cut)
        fo["link_names"] = {lid: f"{l['a']} - {l['b']}" for lid, l in link_map.items() if lid in scope}
        if probe_err:
            fo["probe_error"] = probe_err["error"]
        out["failover"] = fo
        out["_pcaps"] = {fo["link_names"].get(l, l): b for l, b in pcaps.items()}
    finally:
        snap_stop.set()
        if not out.get("failover"):
            try:
                stopper()
            except Exception:                          # noqa: BLE001
                pass
            for lid in started:
                try:
                    cml.capture_stop(lid)
                except Exception:                      # noqa: BLE001
                    pass


# ============================================================ device logs ===
_LOG_TS = re.compile(r"^\*?(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<hms>\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
_CLOCK = re.compile(r"\*?(?P<hms>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\S+\s+\w{3}\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})")


def _dev_dt(mon, day, hms, year):
    fmt = "%b %d %H:%M:%S.%f %Y" if "." in hms else "%b %d %H:%M:%S %Y"
    return datetime.strptime(f"{mon} {int(day):02d} {hms} {year}", fmt)


def parse_device_log(node: str, clock_line: str, t_cmd_local: float, log_text: str,
                     lo_local: float, hi_local: float) -> list[dict]:
    """Syslog lines from one device, placed on the LOCAL clock via the device's
    own `show clock` taken at t_cmd_local (device clocks are NTP-synced to an
    estate master that may itself be in the disaster, so never trusted raw)."""
    try:
        import log_annotator                               # noqa: PLC0415
    except ImportError:
        log_annotator = None
    m = _CLOCK.search(clock_line or "")
    if not m:
        return []
    dev_now = _dev_dt(m.group("mon"), m.group("day"), m.group("hms"), m.group("year"))
    out = []
    for line in (log_text or "").splitlines():
        lm = _LOG_TS.search(line.strip())
        if not lm or "%" not in line:
            continue
        try:
            dt = _dev_dt(lm.group("mon"), lm.group("day"), lm.group("hms"), m.group("year"))
        except ValueError:
            continue
        t_local = t_cmd_local + (dt - dev_now).total_seconds()
        if not (lo_local <= t_local <= hi_local):
            continue
        meaning = ""
        code = line[line.index("%"):].split(":")[0]
        if log_annotator:
            ev = log_annotator.parse(line.strip())
            if ev:
                meaning, code = ev.explanation, f"%{ev.facility}-{ev.severity}-{ev.mnemonic}"
        out.append({"node": node, "t_local": t_local, "code": code,
                    "text": line[line.index("%"):].split(":", 1)[-1].strip(), "meaning": meaning})
    return out


def collect_device_logs(cml, spec, baseline, down, lo_local, hi_local, max_nodes: int = 8,
                        first: list[str] | None = None) -> list[dict]:
    """`show clock` + `show logging` from the running IOS devices on the baseline
    path (and their neighbours in scope), up to 6 consoles in parallel. `first`:
    nodes to read before the path (the two ends of a cut cable see it first)."""
    from console import Console                            # noqa: PLC0415
    by = {n["label"]: n for n in spec["nodes"]}
    cand = [x for x in [*(first or []), *(baseline.get("path") or [])] if x not in down
            and (by.get(x) or {}).get("probe_family") == "ios" and (by.get(x) or {}).get("up")]
    for h in spec.get("vip_holders") or []:
        pass
    nodes = list(dict.fromkeys(cand))[:max_nodes]

    from console import _drain                             # noqa: PLC0415

    def one(node):
        # One attach, then `show clock` timed tightly on the open session. Timing
        # a whole exec() (live run 2026-10-02) included the ~5 s attach and put
        # ISP-2's BFD-down 3.8 s BEFORE the failure; the midpoint of a bare
        # send/response is good to about +-50 ms.
        try:
            with Console(cml) as c:
                c._attach(node)
                c._pager_off(node)
                _drain(c._ch, 200, 2000)
                t0 = time.time()
                c._ch.send("show clock\r\n")
                clk = _drain(c._ch, 120, 5000)
                t1 = time.time() - 0.12
                c._ch.send("show logging | include %\r\n")
                log = _drain(c._ch, 600, 30000)
                c._detach()
                return parse_device_log(node, clk, (t0 + t1) / 2, log, lo_local, hi_local)
        except Exception:                                  # noqa: BLE001 - best effort
            return []
    res = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(nodes)))) as ex:
        for r in ex.map(one, nodes):
            res += r
    return sorted(res, key=lambda x: x["t_local"])


# ================================================================ history ===
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", s or "").strip("-")[:40] or "x"


def save(result: dict) -> str | None:
    """Save a finished failover run (and its pcaps) to the history folder."""
    d = result.get("disaster") or {}
    if d.get("mode") != "failover":
        return None
    pcaps = d.pop("_pcaps", None) or {}
    name = (f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{_slug(result.get('source'))}_to_"
            f"{_slug(result.get('virtual_target') or result.get('target'))}")
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        d["run_name"] = name
        (HISTORY_DIR / f"{name}.json").write_text(json.dumps(result, default=str), encoding="utf-8")
        if pcaps:
            with zipfile.ZipFile(HISTORY_DIR / f"{name}.pcaps.zip", "w", zipfile.ZIP_DEFLATED) as z:
                for link, data in pcaps.items():
                    z.writestr(_slug(link) + ".pcap", data)
            d["pcaps"] = True
            (HISTORY_DIR / f"{name}.json").write_text(json.dumps(result, default=str), encoding="utf-8")
    except OSError:
        return None
    return name


def history() -> list[dict]:
    out = []
    if not HISTORY_DIR.exists():
        return out
    for f in sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:200]:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d = r.get("disaster") or {}
        fo = d.get("failover") or {}
        m = fo.get("metrics") or {}
        out.append({"name": f.stem, "source": r.get("source"), "target": r.get("virtual_target") or r.get("target"),
                    "nodes": d.get("nodes"), "at": (fo.get("times") or {}).get("F_utc"), "verdict": m.get("verdict"),
                    "failover_s": m.get("failover_s"), "outage_s": m.get("outage_s"), "lost": m.get("lost"),
                    "sent": m.get("sent_after_F"), "path_changed": m.get("path_changed"),
                    "answered_by": m.get("answered_by"), "pcaps": (HISTORY_DIR / f"{f.stem}.pcaps.zip").exists()})
    return out


def load(name: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return None
    f = HISTORY_DIR / f"{name}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pcap_zip(name: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return None
    f = HISTORY_DIR / f"{name}.pcaps.zip"
    return f if f.exists() else None


def to_csv(result: dict, kind: str) -> str | None:
    fo = ((result.get("disaster") or {}).get("failover")) or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "packets":
        w.writerow(["t_rel_F_s", "link", "a", "a_iface", "b", "b_iface", "kind", "dir", "seq", "icmp_id",
                    "spi", "src", "dst", "probe", "info"])
        for p in fo.get("packets") or []:
            w.writerow([p["t"], (fo.get("link_names") or {}).get(p["link_id"], p["link_id"]), p["a"], p["ai"],
                        p["b"], p["bi"], p["kind"], p["dir"], p["seq"], p["icmp_id"], p["spi"], p["src"], p["dst"],
                        p["probe"], p.get("info") or ""])
    elif kind == "probes":
        w.writerow(["probe", "sent_rel_F_s", "outcome", "path", "answered_by", "rtt_ms", "died_link",
                    "died_into", "died_t", "unreachable_from", "inferred"])
        for p in fo.get("probes") or []:
            dd = p.get("died_at") or {}
            w.writerow([p["key"], p["sent"], p["outcome"], p["path"], p["answered_by"], p["rtt_ms"],
                        dd.get("link"), dd.get("into"), dd.get("t"), dd.get("unreachable_from"), p.get("inferred")])
    elif kind == "paths":
        w.writerow(["path", "role", "hops", "reply_hops", "first_rel_F_s", "last_rel_F_s", "probes", "replies",
                    "rtt_ms", "answered_by"])
        for p in fo.get("paths") or []:
            w.writerow([p["id"], p["role"], " > ".join(p["hops"]), " > ".join(p["reply_hops"]), p["first"],
                        p["last"], p["probes"], p["replies"], p["rtt_ms"], p["answered_by"]])
    else:
        return None
    return buf.getvalue()
