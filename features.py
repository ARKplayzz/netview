"""NetView extras: baseline matrix, live DHCP lease, known-issue index, CSV.

Each of these reuses something the repo already owns rather than inventing a
parallel version of it:

    matrix        probe_matrix.PROBES — the curated baseline path list, including
                  its EXPECTED-FAIL convention
    dhcp          dhcp_check.parse_lease — the tested `show dhcp lease` regexes
    issues        docs/known-issues.md — parsed, not duplicated
"""
from __future__ import annotations

import csv
import io
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / ".claude" / "skills" / "cml" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import cml_api                              # noqa: E402
from console import Console                 # noqa: E402
from probe_matrix import PROBES             # noqa: E402
from dhcp_check import parse_lease          # noqa: E402

import model                                # noqa: E402
import probes as probe_engine               # noqa: E402


def _address_owners() -> dict[str, str]:
    """ip -> node label, for every address the manifest declares."""
    out: dict[str, str] = {}
    for label, nm in model.load_manifest().get("nodes", {}).items():
        for a in model._addresses(nm):
            out.setdefault(a["value"], label)
    return out


# ==========================================================================
# 1. Baseline reachability matrix
# ==========================================================================
#
# This is probe_matrix.py's list, run from the dashboard and rendered as a grid.
# The list is imported rather than copied: it carries a lot of hard-won judgement
# about WHICH paths are worth asking about, and two of its entries are declared
# must-fails.  A must-fail that starts passing is the more serious event of the
# two, so it is surfaced as a LEAK rather than quietly counted as a pass.

_matrix_state: dict = {
    "status": "idle",       # idle | running | done | error
    "started": None,
    "ended": None,
    "done_count": 0,
    "total": len(PROBES),
    "rows": [],
    "error": None,
    "count": 4,
}
_matrix_lock = threading.Lock()


def matrix_definition() -> list[dict]:
    """The probe list, with the expected-fail flag pulled out of the text."""
    out = []
    for src, dst, desc in PROBES:
        expected_fail = "EXPECTED FAIL" in desc
        clean = re.sub(r"\s*\[EXPECTED FAIL:.*?\]", "", desc).strip()
        out.append({"source": src, "dest": dst, "description": clean,
                    "expected_fail": expected_fail})
    return out


def matrix_status() -> dict:
    with _matrix_lock:
        return dict(_matrix_state, rows=list(_matrix_state["rows"]))


def matrix_start(count: int = 4) -> dict:
    with _matrix_lock:
        if _matrix_state["status"] == "running":
            return {"error": "a matrix run is already in progress"}
        _matrix_state.update({
            "status": "running", "started": datetime.now(timezone.utc).isoformat(),
            "ended": None, "done_count": 0, "rows": [], "error": None,
            "count": count, "total": len(PROBES),
        })
    threading.Thread(target=_matrix_run, args=(count,), daemon=True).start()
    return {"started": True, "total": len(PROBES),
            "eta_seconds": _matrix_eta(count)}


MATRIX_WORKERS = 4


def _matrix_eta(count: int) -> int:
    """Probes are spread across MATRIX_WORKERS parallel sessions (partitioned
    by source node, so no two sessions ever attach the same console).  The ETA
    is the longest worker's share, not the serial sum."""
    per_probe = probe_engine.COST_ATTACH_S + count * probe_engine.COST_PER_PACKET_S
    share = -(-len(PROBES) // MATRIX_WORKERS)     # ceil
    return int(probe_engine.COST_OPEN_S + share * per_probe)


def _matrix_run(count: int) -> None:
    defs = matrix_definition()
    try:
        cml = cml_api.connect()
        # State check BEFORE attaching.  A stopped node has no console on
        # the terminal server at all, so attaching to one spends the full
        # 90s retry loop to learn what one REST call says instantly — the
        # first matrix run burned 9 minutes on six probes from a source
        # that was simply switched off.
        states = {n["label"].rstrip(): n.get("state")
                  for n in cml.nodes_full().values()}
        owners = _address_owners()

        rows: dict[int, dict] = {}
        runnable: list[tuple[int, dict]] = []
        for i, d in enumerate(defs):
            # A dead ping to a switched-off box is lab state, not a network
            # fault; say which it is instead of making the reader work it out.
            owner = owners.get(d["dest"])
            if owner and states.get(owner) not in ("STARTED", "BOOTED", None):
                d = {**d, "target_down": owner}
            if states.get(d["source"]) not in ("STARTED", "BOOTED"):
                # A must-fail with a dead source is NOT "as expected" — nothing
                # was measured, and dns_check.py's rule applies: a probe that
                # cannot succeed must not be scored at all.
                rows[i] = {**d, "verdict": "skipped", "outcome": "source-down",
                           "loss_pct": None, "rtt_ms": None,
                           "packet_pattern": None, "elapsed_s": 0.0,
                           "error": f"source is "
                                    f"{states.get(d['source']) or 'absent'}"
                                    f" — not measured"}
            else:
                runnable.append((i, d))
        with _matrix_lock:
            _matrix_state["done_count"] = len(rows)
            _matrix_state["rows"] = [rows[k] for k in sorted(rows)]

        # Parallel across sessions, partitioned BY SOURCE: the terminal server
        # handles concurrent sessions on DIFFERENT nodes cleanly (measured,
        # 8/8 and 16/16 — docs/cml-integration-review.md), but two sessions on
        # the same node's console were never tested, so all of one source's
        # probes stay on one worker.
        by_source: dict[str, list[tuple[int, dict]]] = {}
        for i, d in runnable:
            by_source.setdefault(d["source"], []).append((i, d))
        buckets: list[list[tuple[int, dict]]] = [[] for _ in range(MATRIX_WORKERS)]
        for group in sorted(by_source.values(), key=len, reverse=True):
            min(buckets, key=len).extend(group)

        def worker(bucket: list[tuple[int, dict]]) -> None:
            if not bucket:
                return
            with probe_engine._CONSOLE_SLOTS:
                wcml = cml_api.connect()
                with Console(wcml) as con:
                    for i, d in bucket:
                        row = _matrix_one(con, d, count)
                        with _matrix_lock:
                            rows[i] = row
                            _matrix_state["done_count"] = len(rows)
                            _matrix_state["rows"] = [
                                rows[k] for k in sorted(rows)]

        threads = [threading.Thread(target=worker, args=(b,), daemon=True)
                   for b in buckets if b]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with _matrix_lock:
            _matrix_state["rows"] = [rows[k] for k in sorted(rows)]
            _matrix_state["done_count"] = len(rows)
            _matrix_state["status"] = "done"
            _matrix_state["ended"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:                       # noqa: BLE001
        with _matrix_lock:
            _matrix_state["status"] = "error"
            _matrix_state["error"] = f"{type(e).__name__}: {e}"
            _matrix_state["ended"] = datetime.now(timezone.utc).isoformat()


def _matrix_one(con: Console, d: dict, count: int) -> dict:
    started = time.time()
    try:
        if con.is_linux(d["source"]):
            # Containers keep their shell on console line 1 (line 0 is the
            # service's stdout, and attaching there strands the session).
            ndef = con._label_to_node.get(d["source"], {}).get("node_definition")
            r = con.linux_ping(d["source"], d["dest"], count=count,
                               line=model.console_line(ndef))
        else:
            r = con.ping(d["source"], d["dest"], count=count)
        loss = r.get("loss_pct")
        rtt = (r.get("rtt_ms") or {}).get("avg")
        pattern = r.get("packet_pattern")
        err = None
    except Exception as e:                       # noqa: BLE001
        loss, rtt, pattern, err = None, None, None, f"{type(e).__name__}: {e}"

    if loss is None:
        verdict = "error"
    elif loss == 0:
        verdict = "ok"
    elif loss < 100:
        verdict = "partial"
    else:
        verdict = "dead"

    # Four outcomes, not two.  A declared must-fail that fails is a PASS for the
    # perimeter; a declared must-fail that succeeds is a leak and the most
    # serious result this suite can produce.
    if d["expected_fail"]:
        outcome = "as-expected" if verdict in ("dead", "error") else "leak"
    elif verdict == "ok":
        outcome = "pass"
    elif pattern and len(pattern) > 1 and pattern[0] != "!" \
            and set(pattern[1:]) == {"!"}:
        # Only the first echo missed, every later one answered: the classic
        # cold-path ARP race (docs/cml-integration-review.md), not a fault.
        outcome = "pass-arp"
    elif d.get("target_down") and verdict == "dead":
        outcome = "target-down"
    else:
        outcome = "attention"

    return {**d, "verdict": verdict, "outcome": outcome, "loss_pct": loss,
            "rtt_ms": rtt, "packet_pattern": pattern, "error": err,
            "elapsed_s": round(time.time() - started, 1)}


# ==========================================================================
# 2. Live DHCP lease
# ==========================================================================
#
# `dhcp_check.parse_lease` does not capture Renewal/Rebind, and those two are
# exactly the "how long until this refreshes" numbers.  Real output:
#
#     Lease: 43200 secs,  Renewal: 20137 secs,  Rebind: 36337 secs
#
# So the tested regexes are reused for everything they cover and only the two
# missing fields are added here.
_RE_RENEW = re.compile(r"Renewal:\s*(\d+)\s*secs")
_RE_REBIND = re.compile(r"Rebind:\s*(\d+)\s*secs")


def read_lease(node: dict) -> dict:
    """Read the live lease off a node that actually leases one.

    Only IOS nodes can answer this.  The Linux desktops lease too, but their
    images have no shell that reports a lease in a parseable form, so claiming a
    countdown for them would be invention.
    """
    dhcp = node.get("dhcp")
    if not dhcp:
        return {"error": "this node is statically addressed — there is no lease"}
    if node.get("probe_family") not in ("ios",):
        return {"error": f"{node['label']} leases an address, but its image "
                         f"({node.get('node_definition')}) exposes no command "
                         f"that reports the remaining time"}
    if not node.get("up"):
        return {"error": f"{node['label']} is not running"}

    with probe_engine._CONSOLE_LOCK:
        cml = cml_api.connect()
        with Console(cml) as con:
            raw = con.exec(node["label"], "show dhcp lease", timeout_ms=25000)

    lease = parse_lease(raw)
    renew = _RE_RENEW.search(raw)
    rebind = _RE_REBIND.search(raw)
    lease["renewal_secs"] = int(renew.group(1)) if renew else None
    lease["rebind_secs"] = int(rebind.group(1)) if rebind else None
    lease["read_at"] = datetime.now(timezone.utc).isoformat()
    lease["expected_server"] = dhcp.get("expected_server")
    lease["server_matches"] = (
        lease.get("server") is not None
        and lease["server"] == dhcp.get("expected_server"))
    lease["raw"] = raw
    if not lease.get("address"):
        lease["error"] = ("no lease reported — the interface may not have bound, "
                          "or this device is not currently a DHCP client")
    return lease


# ==========================================================================
# 2b. DNS audit — every internal name checked against the manifest
# ==========================================================================
#
# The point probe can compare one name to the manifest; this does the whole
# estate in one console session.  The expensive part of a lookup is attaching to
# a console (~6s); the lookup itself is ~1s.  So the audit attaches ONCE per
# site vantage and runs every query through that session, which makes a
# 40-name audit cost about a minute instead of about eight.
#
# What a mismatch means: the resolver is answering confidently with an address
# the manifest no longer assigns.  No reachability test can see this — the ping
# to the stale address just fails and looks like an outage.

_dns_state: dict = {"status": "idle", "started": None, "ended": None,
                    "done_count": 0, "total": 0, "rows": [], "error": None}
_dns_lock = threading.Lock()


def dns_audit_status() -> dict:
    with _dns_lock:
        return dict(_dns_state, rows=list(_dns_state["rows"]))


def dns_audit_start(nodes: list[dict]) -> dict:
    """Audit every node that has an fqdn + a_record, from a same-site client.

    The vantage must be a legitimate CLIENT of the internal resolver.  DMZ
    residents are not: the perimeter denies DMZ->RFC1918 before its domain
    permits, so a DMZ vantage fails every lookup by design — the first audit
    run used DNS-DMZ-HQ-1 and reported 20 false 'resolver unreachable' rows.
    """
    plan: list[dict] = []
    for site in ("HQ", "DR"):
        vantage = next((n for n in nodes
                        if n.get("probe_family") == "linux"
                        and n.get("state") in ("STARTED", "BOOTED")
                        and n.get("site") == site
                        and not probe_engine._dmz_resident(n)), None)
        targets = [n for n in nodes
                   if n.get("site") == site
                   and (n.get("dns") or {}).get("fqdn")
                   and (n.get("dns") or {}).get("a_record")]
        if not targets:
            continue
        plan.append({"site": site, "vantage": vantage,
                     "resolver": targets[0].get("resolver"), "targets": targets})

    missing = [p["site"] for p in plan if not p["vantage"]]
    if missing:
        return {"error": f"no eligible vantage at {', '.join(missing)} — needs a "
                         f"running non-DMZ Linux node (a desktop, the internal "
                         f"resolver itself, or a RADIUS box) to ask from"}
    total = sum(len(p["targets"]) for p in plan)
    with _dns_lock:
        if _dns_state["status"] == "running":
            return {"error": "an audit is already in progress"}
        _dns_state.update({"status": "running", "rows": [], "error": None,
                           "done_count": 0, "total": total,
                           "started": datetime.now(timezone.utc).isoformat(),
                           "ended": None})
    threading.Thread(target=_dns_audit_run, args=(plan,), daemon=True).start()
    # Sites run in parallel (one session per vantage), so the ETA is the
    # largest site's share: one attach per 8-name chunk (~7s) plus ~2s a name.
    biggest = max(len(p["targets"]) for p in plan)
    return {"started": True, "total": total,
            "eta_seconds": int(probe_engine.COST_OPEN_S
                               + -(-biggest // 8) * 7 + biggest * 2)}


def _dns_audit_run(plan: list[dict]) -> None:
    try:
        # One worker per site — each drives a different vantage node, so the
        # sessions never touch the same console (the measured-safe case).
        def worker(p: dict) -> None:
            with probe_engine._CONSOLE_SLOTS:
                cml = cml_api.connect()
                with Console(cml) as con:
                    # Batched: linux_exec re-attaches the console on EVERY
                    # call (~5-8s each), which is where the first audit's
                    # 467s went.  One shell loop per chunk pays the attach
                    # once for ~8 lookups.
                    for chunk in _chunks(p["targets"], 8):
                        for row in _dns_audit_chunk(con, p, chunk):
                            with _dns_lock:
                                _dns_state["rows"].append(row)
                                _dns_state["done_count"] += 1

        threads = [threading.Thread(target=worker, args=(p,), daemon=True)
                   for p in plan]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with _dns_lock:
            _dns_state["status"] = "done"
            _dns_state["ended"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:                       # noqa: BLE001
        with _dns_lock:
            _dns_state["status"] = "error"
            _dns_state["error"] = f"{type(e).__name__}: {e}"
            _dns_state["ended"] = datetime.now(timezone.utc).isoformat()


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _dns_audit_chunk(con: Console, p: dict, targets: list[dict]) -> list[dict]:
    """One console attach, one shell loop, N lookups.

    Each lookup is fenced with ==NAME== markers so a chunk's output splits
    unambiguously even if a lookup prints nothing at all.
    """
    parts = []
    for t in targets:
        fq = t["dns"]["fqdn"]
        parts.append(f'echo "=={fq}=="; nslookup {fq} {p["resolver"]} 2>&1')
    raw = con.linux_exec(p["vantage"]["label"], "; ".join(parts),
                         timeout_ms=20000 + 8000 * len(targets),
                         line=p["vantage"].get("console_line", 0))

    rows = []
    for t in targets:
        fq = t["dns"]["fqdn"]
        m = re.search(r"==" + re.escape(fq) + r"==\n(.*?)(?=\n==[\w.-]+==|\Z)",
                      raw, re.DOTALL)
        section = m.group(1) if m else ""
        parsed = probe_engine.parse_nslookup(section)
        if not m:
            parsed = {"ok": False, "addresses": [],
                      "reason": "no output captured for this name (console "
                                "truncated the batch) — not measured"}
        answer = parsed["addresses"][0] if parsed["ok"] else None
        rows.append({
            "label": t["label"],
            "site": p["site"],
            "fqdn": fq,
            "expected": t["dns"]["a_record"],
            "answer": answer,
            "resolver": p["resolver"],
            "vantage": p["vantage"]["label"],
            "ok": parsed["ok"],
            "match": parsed["ok"] and answer == t["dns"]["a_record"],
            "reason": parsed.get("reason"),
        })
    return rows


# ==========================================================================
# 2c. Address harvest — live addresses read off the devices
# ==========================================================================
#
# The manifest declares what a node's addresses SHOULD be; the device knows what
# they ARE.  CML's own discovery cannot bridge that gap — measured 2026-08-20,
# `GET /labs/{id}/layer3_addresses` had discovered 1 interface out of 236 — so
# the only live source is the devices themselves:
#
#     IOS      show ip interface brief
#     ASA      show interface ip brief
#     Linux    ip -4 -o addr show        (containers on console line 1)
#
# Runs across the measured-safe console pool, partitioned one node per worker.
# Results carry their read time; a live address is a fact about a moment, and
# showing it undated would let it quietly go stale into a lie.

HARVEST_WORKERS = 6

_harvest_state: dict = {"status": "idle", "started": None, "ended": None,
                        "done_count": 0, "total": 0, "error": None,
                        "nodes": {}}          # label -> {addresses, at, error}
_harvest_lock = threading.Lock()


def harvest_status(include_nodes: bool = True) -> dict:
    with _harvest_lock:
        out = {k: v for k, v in _harvest_state.items() if k != "nodes"}
        if include_nodes:
            out["nodes"] = dict(_harvest_state["nodes"])
        return out


def harvest_nodes() -> dict:
    with _harvest_lock:
        return dict(_harvest_state["nodes"])


def harvest_start(nodes: list[dict]) -> dict:
    # Windows nodes are included too: SAC's `i` command lists a server's
    # addresses without logging in (verified 2026-09-23 on DC-HQ-2/DC-DR-1).
    targets = [n for n in nodes
               if (n.get("can_source_probe") or n.get("node_definition") == "win2025")
               and n.get("state") in ("STARTED", "BOOTED")]
    if not targets:
        return {"error": "no running node has a drivable console to read from"}
    with _harvest_lock:
        if _harvest_state["status"] == "running":
            return {"error": "a harvest is already in progress"}
        _harvest_state.update({
            "status": "running", "error": None, "done_count": 0,
            "total": len(targets),
            "started": datetime.now(timezone.utc).isoformat(), "ended": None})
    threading.Thread(target=_harvest_run, args=(targets,), daemon=True).start()
    share = -(-len(targets) // HARVEST_WORKERS)
    # Measured 2026-09-23: 86s on a session's first run, 40s on the next,
    # same 38 nodes.  Report a range rather than a single optimistic number.
    return {"started": True, "total": len(targets),
            "eta_seconds": int(probe_engine.COST_OPEN_S + share * 6),
            "eta_seconds_max": int(probe_engine.COST_OPEN_S + share * 12)}


# `show ip interface brief` / `show interface ip brief` — both are
# column-aligned with the address in column 2 and the protocol last; Status can
# be the two words "administratively down", which is why this splits rather
# than fixing column offsets.
def parse_ip_brief(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 6 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[1]):
            out.append({"iface": parts[0], "ip": parts[1],
                        "method": parts[3],
                        "status": " ".join(parts[4:-1]),
                        "proto": parts[-1]})
    return out


_LNX_ADDR = re.compile(r"^\d+:\s+(\S+?)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", re.M)


# CML's own per-container orchestration network — not part of P45.  Seen as a
# second eth0 address on RADIUS-HQ-1 (192.168.255.157/24) on 2026-09-23.
CML_INFRA_PREFIX = "192.168.255."


def parse_linux_addrs(raw: str) -> list[dict]:
    out = []
    for m in _LNX_ADDR.finditer(raw):
        iface, ip, plen = m.group(1), m.group(2), m.group(3)
        if iface.startswith("lo") or ip.startswith("127.") \
                or ip.startswith(CML_INFRA_PREFIX):
            continue
        out.append({"iface": iface, "ip": ip, "prefix": int(plen),
                    "method": None, "status": "up", "proto": "up"})
    return out


def _harvest_run(targets: list[dict]) -> None:
    try:
        buckets: list[list[dict]] = [[] for _ in range(HARVEST_WORKERS)]
        for i, n in enumerate(targets):
            buckets[i % HARVEST_WORKERS].append(n)

        def worker(bucket: list[dict]) -> None:
            if not bucket:
                return
            with probe_engine._CONSOLE_SLOTS:
                cml = cml_api.connect()
                with Console(cml) as con:
                    for n in bucket:
                        rec = _harvest_one(con, n)
                        with _harvest_lock:
                            _harvest_state["nodes"][n["label"]] = rec
                            _harvest_state["done_count"] += 1

        threads = [threading.Thread(target=worker, args=(b,), daemon=True)
                   for b in buckets if b]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with _harvest_lock:
            _harvest_state["status"] = "done"
            _harvest_state["ended"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:                       # noqa: BLE001
        with _harvest_lock:
            _harvest_state["status"] = "error"
            _harvest_state["error"] = f"{type(e).__name__}: {e}"
            _harvest_state["ended"] = datetime.now(timezone.utc).isoformat()


_SAC_NET = re.compile(r"Net:\s*(\d+),\s*Ip=(\d+\.\d+\.\d+\.\d+)\s+Subnet=(\d+\.\d+\.\d+\.\d+)")


def _harvest_windows(n: dict) -> list[dict]:
    """Read a Windows node's IPv4 addresses from SAC `i` — no credential needed.

    Output looks like `Net: 6, Ip=192.168.80.61  Subnet=255.255.255.0  Gateway=...`.
    Uses its own console session (the SAC channel is its own attach).
    """
    import ipaddress
    ws = str(REPO / ".claude" / "skills" / "winserver" / "scripts")
    if ws not in sys.path:
        sys.path.insert(0, ws)
    from win_console import WinConsole
    w = WinConsole(cml_api.connect(), n["label"])
    w.open()
    try:
        if not w.sac_prompt():
            raise RuntimeError("no SAC prompt (EMS not enabled?)")
        raw = w.sac("i", 3.0)
    finally:
        w.close()
    out = []
    for m in _SAC_NET.finditer(raw):
        ip, mask = m.group(2), m.group(3)
        if ip.startswith("169.254."):
            continue
        out.append({"iface": f"Net{m.group(1)}", "ip": ip,
                    "prefix": ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen,
                    "method": "SAC", "status": "up", "proto": "up"})
    return out


def _harvest_one(con: Console, n: dict) -> dict:
    at = datetime.now(timezone.utc).isoformat()
    try:
        fam = n.get("probe_family")
        if n.get("node_definition") == "win2025":
            addrs = _harvest_windows(n)
        elif fam == "linux":
            raw = con.linux_exec(n["label"], "ip -4 -o addr show",
                                 timeout_ms=20000,
                                 line=n.get("console_line", 0))
            addrs = parse_linux_addrs(raw)
        elif fam == "asa":
            raw = con.exec(n["label"], "show interface ip brief", timeout_ms=25000)
            addrs = parse_ip_brief(raw)
        else:
            raw = con.exec(n["label"], "show ip interface brief", timeout_ms=25000)
            addrs = parse_ip_brief(raw)
        if not addrs:
            return {"addresses": [], "at": at,
                    "error": "command returned no parseable addresses"}
        return {"addresses": addrs, "at": at, "error": None}
    except Exception as e:                       # noqa: BLE001
        return {"addresses": [], "at": at, "error": f"{type(e).__name__}: {e}"}


# ==========================================================================
# 3. Known-issue index
# ==========================================================================

_H = re.compile(r"^##\s+(?P<status>FIXED|OPEN|Historical\w*)[^\n]*?—\s*(?P<rest>.+)$")
_NUM = re.compile(r"^(?P<num>\d+[a-z]?)\.\s*(?P<title>.+)$")


def known_issues() -> list[dict]:
    """Parse docs/known-issues.md into sections, keeping OPEN ones separate."""
    path = REPO / "docs" / "known-issues.md"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    sections, current = [], None
    for i, line in enumerate(lines):
        m = _H.match(line.strip())
        if m:
            if current:
                sections.append(current)
            rest = m.group("rest").strip()
            nm = _NUM.match(rest)
            current = {
                "status": m.group("status").upper(),
                "id": nm.group("num") if nm else None,
                "title": nm.group("title") if nm else rest,
                "line": i + 1,
                "body": [],
            }
        elif current:
            current["body"].append(line)
    if current:
        sections.append(current)
    return sections


def issues_by_node(labels: list[str]) -> dict[str, list[dict]]:
    """label -> the OPEN issues whose section names that node.

    Only OPEN sections produce a badge.  A fixed issue that once named a device
    is history, and badging it would put a permanent warning on hardware that is
    working — which trains people to ignore the badge.
    """
    sections = [s for s in known_issues() if s["status"] == "OPEN"]
    out: dict[str, list[dict]] = {}
    for s in sections:
        text = "\n".join(s["body"])
        for label in labels:
            # Word-boundary match so `AP-HQ-1` does not also hit `AP-HQ-10`, and
            # so a bare `DNS` in prose does not tag every resolver.
            if len(label) < 4:
                continue
            if re.search(r"(?<![\w-])" + re.escape(label) + r"(?![\w-])", text):
                out.setdefault(label, []).append(
                    {"id": s["id"], "title": s["title"], "line": s["line"]})
    return out


# ==========================================================================
# 4. CSV export
# ==========================================================================

CSV_COLUMNS = [
    "label", "state", "role", "role_title", "site", "layer_title",
    "node_definition", "primary_ip", "fqdn", "a_record", "cnames",
    "resolver", "gateway", "addressing", "dhcp_vlan", "dhcp_scope",
    "vlans", "config_status", "template", "interface_count", "link_count",
    "hsrp", "failover", "wlc_ha", "open_issues",
]


def inventory_csv(nodes: list[dict], issues: dict[str, list[dict]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for n in nodes:
        dns = n.get("dns") or {}
        dhcp = n.get("dhcp") or {}
        ha = n.get("ha") or {}
        w.writerow([
            n.get("label"), n.get("state"), n.get("role"), n.get("role_title"),
            n.get("site"), n.get("layer_title"), n.get("node_definition"),
            n.get("primary_ip"), dns.get("fqdn"), dns.get("a_record"),
            " ".join(dns.get("cnames") or []),
            n.get("resolver"), n.get("gateway"), n.get("addressing"),
            dhcp.get("vlan"), dhcp.get("scope"),
            " ".join(f"{v['id']}:{v.get('name') or ''}" for v in (n.get("vlans") or [])),
            n.get("config_status"), n.get("template"),
            n.get("interface_count"), len(n.get("links") or []),
            ha.get("hsrp_role"), ha.get("failover_role"), ha.get("wlc_role"),
            " ".join(f"#{i['id']}" for i in issues.get(n.get("label"), [])),
        ])
    return buf.getvalue()
