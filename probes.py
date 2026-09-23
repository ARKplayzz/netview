"""NetView probe engine — resolve-then-ping, from one device to another.

WHAT A PROBE HERE ACTUALLY MEASURES
-----------------------------------
Three separate facts, reported separately, because conflating them is how a
dashboard ends up claiming "DNS is broken" when the truth is a dead route:

  1. RESOLUTION — did the resolver answer with an address, and which one?
  2. RECORD MATCH — is that address the one the manifest says it should be?
     A resolver that answers confidently with a stale address is the failure
     that no ping test can see, because the ping to the stale address simply
     fails and looks like an outage.
  3. REACHABILITY — does ICMP get there, from the source the user picked?

NO DEVICE CONFIG IS CHANGED.  Every IOS node in this estate ships
`no ip domain lookup`, so `ping <fqdn>` from a switch cannot resolve anything.
The alternative — temporarily setting `ip name-server`, pinging, and restoring —
mutates a live device to run a read-only query, and a probe that leaves the
estate different from how it found it is not a probe.  So DNS resolution runs
from a node that genuinely has a resolver (a Linux node), and the UI always
names which node asked.  When the source itself is Linux, that node IS the
source and the measurement is end-to-end.  When it is not, the vantage is
displayed as a separate hop rather than quietly attributed to the source.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / ".claude" / "skills" / "cml" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import cml_api          # noqa: E402
from console import Console   # noqa: E402


# A bounded POOL of console sessions, not a single lock.
#
# The original version serialised everything on the belief that the terminal
# server multiplexes every node through one shell.  That is a property of one
# SHARED Console object (one SSH channel), not of the server: separate SSH
# sessions get separate channels and separate console attachments.  Measured
# 2026-08-20 (docs/cml-integration-review.md): 8 concurrent sessions on 8
# different nodes returned 8/8 correctly-attributed results, reproduced twice;
# 16/16 clean at N=16.  A four-probe set dropped 31.3s -> 11.0s.
#
# Two rules keep it safe:
#   * one Console instance per worker, never shared between threads
#   * never attach two sessions to the SAME node concurrently — per-node
#     serialisation is enforced with _node_locks below
# The cap is 6: at N=16 the slowest worker took 3x the median, so past ~8 you
# buy latency variance rather than throughput.
_CONSOLE_SLOTS = threading.Semaphore(6)
# Kept as an alias: features.py serialises whole-estate runs (matrix, audit)
# through single slots too.
_CONSOLE_LOCK = _CONSOLE_SLOTS

_node_locks: dict[str, threading.Lock] = {}
_node_locks_guard = threading.Lock()


def _node_lock(label: str) -> threading.Lock:
    with _node_locks_guard:
        return _node_locks.setdefault(label, threading.Lock())

_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()
_MAX_JOBS = 200

# Rough wall-clock cost of each stage, used for the client-side progress bar.
# Recalibrated 2026-08-20 against measurements in docs/cml-integration-review.md:
# connect+open is ~2s, a full one-command job ~5s serially.  Jobs sharing the
# pool stretch individually while the batch finishes sooner, so these are
# per-job estimates under light load, not promises.
COST_OPEN_S = 3
COST_ATTACH_S = 4
COST_PER_PACKET_S = 1.4


def estimate_seconds(methods: list[str], count: int) -> int:
    stages = 1 + (1 if "dns" in methods else 0) + (1 if "ip" in methods else 0)
    pings = sum(1 for m in methods if m in ("ip", "dns"))
    return int(COST_OPEN_S + stages * COST_ATTACH_S + pings * count * COST_PER_PACKET_S)


# --------------------------------------------------------------------------
# nslookup
# --------------------------------------------------------------------------

# BusyBox nslookup, as it prints on these Alpine nodes:
#
#     Server:     192.168.80.53
#     Address:    192.168.80.53:53
#
#     Name:       coreswitch-hq-1.hq.p45.local
#     Address:    192.168.90.11
#
# The server's OWN address appears first and is not an answer.  Splitting on the
# blank line before `Name:` is what keeps the resolver's address from being
# reported as the record — a bug that makes every lookup "succeed" and every
# resolved-address ping go to the resolver.
_NAME_BLOCK = re.compile(r"^Name:\s*(?P<name>\S+)\s*$", re.M)
_ADDR = re.compile(r"^Address(?:\s+\d+)?:\s*(?P<ip>\d+\.\d+\.\d+\.\d+)\s*$", re.M)
_NXDOMAIN = re.compile(r"can't resolve|NXDOMAIN|No answer|server can't find", re.I)


def parse_nslookup(raw: str) -> dict:
    """Return {ok, addresses, name, reason}."""
    if _NXDOMAIN.search(raw):
        return {"ok": False, "addresses": [], "name": None,
                "reason": "resolver answered NXDOMAIN / could not resolve"}
    m = _NAME_BLOCK.search(raw)
    if not m:
        return {"ok": False, "addresses": [], "name": None,
                "reason": "no answer section in the reply (resolver silent, "
                          "refused, or unreachable)"}
    answers = [a.group("ip") for a in _ADDR.finditer(raw[m.end():])]
    if not answers:
        return {"ok": False, "addresses": [], "name": m.group("name"),
                "reason": "name section with no address — refused or empty answer"}
    return {"ok": True, "addresses": answers, "name": m.group("name"), "reason": None}


ENTERPRISE_SITES = ("HQ", "DR")


def pick_dns_vantage(source: dict, nodes: list[dict], site: str | None) -> dict | None:
    """Which node should run the nslookup.

    Preference order, best evidence first:
      1. the source itself, if it has a shell — the measurement is then genuinely
         end-to-end and nothing has to be caveated
      2. a running user desktop at the same site — a real client of that resolver
      3. any running shell node at the same site (containers count: they have a
         root shell on console line 1 — see docs/cml-integration-review.md)
      4. a running shell node at the OTHER enterprise site
      5. anything else left, flagged as off-net

    Tiers 4 and 5 are separated because of a wrong answer this produced on its
    first run: the fallback picked `WFH user (VPN client)`, and the lookup of an
    internal name "failed".  That node is a home broadband user sitting outside
    the perimeter — it is SUPPOSED to be unable to reach 192.168.80.53, so the
    probe had measured the design working and reported it as a DNS fault.  An
    off-net vantage is a valid thing to measure from, but never a default, and
    never silently.
    """
    if source.get("probe_family") == "linux" and _is_up(source):
        return source

    # DMZ-resident nodes are excluded outright.  The perimeter's DMZ-IN ACL
    # denies them any packet to RFC1918 space BEFORE its domain permits, so a
    # DMZ container asking the internal resolver fails BY DESIGN — measured
    # 2026-08-20, when a DMZ vantage produced 20 false "unreachable resolver"
    # results in one audit.  Same class of bug as the off-net WFH vantage.
    pool = [n for n in nodes
            if n.get("probe_family") == "linux" and _is_up(n)
            and not _dmz_resident(n)]

    for pred in (
        lambda n: n.get("site") == site and n.get("role") == "desktop_client",
        lambda n: n.get("site") == site,
        lambda n: n.get("site") in ENTERPRISE_SITES,
    ):
        found = sorted([n for n in pool if pred(n)], key=lambda n: n["label"])
        if found:
            return found[0]

    found = sorted(pool, key=lambda n: n["label"])
    if found:
        v = dict(found[0])
        v["_offnet"] = True
        return v
    return None


def _is_up(node: dict) -> bool:
    return node.get("state") in ("STARTED", "BOOTED")


def _dmz_resident(node: dict) -> bool:
    """Does this node live in the DMZ (VLAN 55)?  Those nodes cannot reach the
    internal resolver — the ASA denies DMZ→RFC1918 before anything else."""
    if node.get("role") in ("dns_public", "server_web"):
        return True
    return any(v.get("id") == 55 for v in (node.get("vlans") or []))


# --------------------------------------------------------------------------
# Job management
# --------------------------------------------------------------------------

def submit(spec: dict) -> str:
    """Queue a probe.  Returns a job id to poll with `get`."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "spec": spec,
        "status": "queued",
        "stage": "waiting for the console channel",
        "created": datetime.now(timezone.utc).isoformat(),
        "eta_seconds": estimate_seconds(spec.get("methods", ["ip"]),
                                        spec.get("count", 5)),
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        while len(_jobs) > _MAX_JOBS:
            _jobs.popitem(last=False)
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def get(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def recent(limit: int = 40) -> list[dict]:
    with _jobs_lock:
        return list(reversed(list(_jobs.values())))[:limit]


def _set(job_id: str, **kw) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)


def _run(job_id: str) -> None:
    job = get(job_id)
    if not job:
        return
    spec = job["spec"]
    started = time.time()

    # Per-node serialisation: the pool allows 6 concurrent sessions, but two
    # sessions attaching the SAME node's console is untested and presumed
    # unsafe, so a job locks every node it will drive, in sorted order to
    # avoid deadlock.
    drive = sorted({spec["source"]["label"]}
                   | ({spec["vantage"]["label"]} if spec.get("vantage") else set()))
    try:
        with _CONSOLE_SLOTS:
            _set(job_id, status="running", stage="opening console session",
                 started_at=datetime.now(timezone.utc).isoformat())
            locks = [_node_lock(x) for x in drive]
            for lk in locks:
                lk.acquire()
            try:
                cml = cml_api.connect()
                with Console(cml) as con:
                    result = _execute(job_id, con, spec)
            finally:
                for lk in reversed(locks):
                    lk.release()
        result["elapsed_s"] = round(time.time() - started, 1)
        _set(job_id, status="done", stage="complete", result=result,
             ended=datetime.now(timezone.utc).isoformat())
    except Exception as e:                     # noqa: BLE001 - surfaced to the UI
        _set(job_id, status="error", stage="failed",
             error=f"{type(e).__name__}: {e}",
             ended=datetime.now(timezone.utc).isoformat())


def _execute(job_id: str, con: Console, spec: dict) -> dict:
    source = spec["source"]                 # full node dict
    target = spec["target"]                 # full node dict
    methods = spec.get("methods", ["ip"])
    count = int(spec.get("count", 5))
    target_ip = spec.get("target_ip")
    vantage = spec.get("vantage")           # node dict or None

    out: dict = {
        "source": source["label"],
        "target": target["label"],
        "methods": methods,
        "count": count,
        "dns": None,
        "ping_ip": None,
        "ping_dns": None,
        "notes": [],
    }

    # ------------------------------------------------------------ 1. resolve
    resolved_ip = None
    if "dns" in methods:
        fqdn = (target.get("dns") or {}).get("fqdn")
        resolver = source.get("resolver") or target.get("resolver")
        if not fqdn:
            out["dns"] = {"ok": False, "reason":
                          "this node has no name in the internal zone (no site "
                          "or no address in the manifest)"}
        elif not vantage:
            out["dns"] = {"ok": False, "reason":
                          "no running Linux node available to ask a resolver "
                          "from — start a desktop or a dnsmasq node"}
        elif not resolver:
            out["dns"] = {"ok": False, "reason":
                          "no resolver is defined for this source's site"}
        else:
            _set(job_id, stage=f"resolving {fqdn} from {vantage['label']}")
            raw = con.linux_exec(vantage["label"],
                                 f"nslookup {fqdn} {resolver}", timeout_ms=25000,
                                 line=vantage.get("console_line", 0))
            parsed = parse_nslookup(raw)
            expected = (target.get("dns") or {}).get("a_record")
            parsed.update({
                "fqdn": fqdn,
                "resolver": resolver,
                "vantage": vantage["label"],
                "vantage_is_source": vantage["label"] == source["label"],
                "expected": expected,
                "raw": raw,
            })
            if parsed["ok"]:
                resolved_ip = parsed["addresses"][0]
                parsed["matches_manifest"] = (
                    expected is not None and resolved_ip == expected)
            parsed["offnet_vantage"] = bool(vantage.get("_offnet"))
            out["dns"] = parsed

            # An off-net vantage failing to reach an internal resolver is the
            # design working, not a fault, and must never be reported as one.
            if vantage.get("_offnet"):
                out["notes"].append(
                    f"{vantage['label']} sits OUTSIDE the enterprise perimeter, and "
                    f"it was the only node with a usable shell that was running. An "
                    f"internal resolver is not supposed to answer it, so a failed "
                    f"lookup here is expected behaviour rather than evidence of a "
                    f"DNS fault. Start a desktop inside the estate for a real test.")
                if not parsed["ok"]:
                    parsed["inconclusive"] = True

            if not parsed.get("vantage_is_source"):
                out["notes"].append(
                    f"{source['label']} is an IOS/ASA device and ships "
                    f"`no ip domain lookup`, so it cannot resolve a name itself. "
                    f"The lookup was run from {vantage['label']} against the same "
                    f"resolver ({resolver}); the ping below still comes from "
                    f"{source['label']}.")

    # ------------------------------------------------------------ 2. ping by IP
    if "ip" in methods and target_ip:
        _set(job_id, stage=f"pinging {target_ip} from {source['label']}")
        out["ping_ip"] = _ping(con, source, target_ip, count)
        out["ping_ip"]["dest"] = target_ip
        out["ping_ip"]["via"] = "manifest address"

    # ------------------------------------------------------- 3. ping resolved
    if resolved_ip:
        if target_ip and resolved_ip == target_ip and out.get("ping_ip"):
            # Same address by both paths: DNS is correct and one measurement
            # covers both.  Re-pinging would double the run time to produce a
            # second sample of the identical path, which is not more evidence.
            out["ping_dns"] = dict(out["ping_ip"])
            out["ping_dns"]["via"] = "resolved address (identical to manifest)"
            out["ping_dns"]["reused"] = True
        else:
            _set(job_id, stage=f"pinging resolved {resolved_ip} from {source['label']}")
            out["ping_dns"] = _ping(con, source, resolved_ip, count)
            out["ping_dns"]["dest"] = resolved_ip
            out["ping_dns"]["via"] = "resolved address"

    out["verdict"] = _verdict(out)
    return out


def _ping(con: Console, source: dict, dest: str, count: int) -> dict:
    if source.get("probe_family") == "linux":
        # console_line 1 for containers — line 0 is the service's stdout, and
        # attaching there fails AND strands the session for the next attach.
        return con.linux_ping(source["label"], dest, count=count,
                              line=source.get("console_line", 0))
    return con.ping(source["label"], dest, count=count)


def _verdict(out: dict) -> dict:
    """A single sentence that says what was and was not established."""
    ip_r, dns_r, dns = out.get("ping_ip"), out.get("ping_dns"), out.get("dns")

    best = None
    for r in (ip_r, dns_r):
        if r and r.get("loss_pct") is not None:
            best = r if best is None else min(best, r, key=lambda x: x["loss_pct"])

    if best is None:
        return {"level": "unknown",
                "text": "No ping summary came back — the source console did not "
                        "return a parseable result. Treat this as an untested "
                        "path, not a failure."}

    loss = best["loss_pct"]
    rtt = (best.get("rtt_ms") or {}).get("avg")
    rtt_txt = f", {rtt} ms average" if rtt is not None else ""

    if dns and dns.get("ok") and dns.get("matches_manifest") is False:
        return {"level": "warn",
                "text": f"Reachable ({100 - loss}% of packets{rtt_txt}), but DNS "
                        f"answered {dns['addresses'][0]} where the manifest says "
                        f"{dns.get('expected')}. The name and the address plan "
                        f"disagree — one of them is stale."}

    # A working ping with a failed lookup is not a pass.  Reporting only the ping
    # would say "reachable" about a service that nothing can find by name.
    if dns and not dns.get("ok") and not dns.get("inconclusive"):
        return {"level": "warn",
                "text": f"ICMP got through ({100 - loss}% of packets{rtt_txt}) but "
                        f"the name did not resolve: {dns.get('reason')}. The address "
                        f"works; the name does not."}
    if dns and dns.get("inconclusive"):
        return {"level": "warn",
                "text": f"Reachable ({100 - loss}% of packets{rtt_txt}). DNS was NOT "
                        f"established — the only available vantage was outside the "
                        f"perimeter, where an internal name is not meant to resolve."}
    if loss == 0:
        return {"level": "ok",
                "text": f"Reachable — no packet loss{rtt_txt}."}
    if loss < 100:
        gap = best.get("longest_gap_s")
        gap_txt = f" Longest unanswered run: {gap}s." if gap else ""
        return {"level": "warn",
                "text": f"Partial: {loss}% loss{rtt_txt}.{gap_txt}"}
    return {"level": "fail",
            "text": "Unreachable — every packet was lost. This says nothing about "
                    "why; check the path, not the endpoint."}
