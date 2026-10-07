"""NetView console helpers: the session pool, per-node locks, ping, and the
resolver-vantage rules shared by the path trace, the matrix and the DNS audit.

The standalone point-probe job that used to live here was folded into the
path trace (pathtrace.py), which measures reachability and the route in one
run.  The reasoning below still governs how DNS is checked.

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

# Rough wall-clock cost of each stage, used for the client-side progress bar.
# Recalibrated 2026-08-20 against measurements in docs/cml-integration-review.md:
# connect+open is ~2s, a full one-command job ~5s serially.  Jobs sharing the
# pool stretch individually while the batch finishes sooner, so these are
# per-job estimates under light load, not promises.
COST_OPEN_S = 3
COST_ATTACH_S = 4
COST_PER_PACKET_S = 1.4


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
    # A node the harvest read as having no address at all can't reach any
    # resolver - measured 2026-09-27: with both DHCP servers down, Finance PC
    # had no IPv4 and was still picked, and a correctly working DNS failover
    # name "did not resolve". Only skipped on positive evidence (a harvest
    # record with no address); no record at all means unknown, not broken.
    pool = [n for n in nodes
            if n.get("probe_family") == "linux" and _is_up(n)
            and not _dmz_resident(n) and not _harvested_unaddressed(n)]

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


def _harvested_unaddressed(node: dict) -> bool:
    # The harvest reports a box with no addresses as an "error" with this
    # wording; any OTHER error (console unreachable etc.) means unknown.
    live = node.get("live")
    if not live or live.get("addresses"):
        return False
    err = live.get("error") or ""
    return not err or "no parseable addresses" in err


def _dmz_resident(node: dict) -> bool:
    """Does this node live in the DMZ (VLAN 55)?  Those nodes cannot reach the
    internal resolver — the ASA denies DMZ→RFC1918 before anything else."""
    if node.get("role") in ("dns_public", "server_web"):
        return True
    return any(v.get("id") == 55 for v in (node.get("vlans") or []))


def _ping(con: Console, source: dict, dest: str, count: int) -> dict:
    if source.get("probe_family") == "linux":
        # console_line 1 for containers — line 0 is the service's stdout, and
        # attaching there fails AND strands the session for the next attach.
        return con.linux_ping(source["label"], dest, count=count,
                              line=source.get("console_line", 0))
    if source.get("probe_family") == "windows":
        return con.win_ping(source["label"], dest, count=count)
    return con.ping(source["label"], dest, count=count)
