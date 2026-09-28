"""Offline checks of capture matching: PAT/DNAT recovery and the ECMP forward/return split.
Run: py -3.11 netview/tests/test_capture_matching.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / ".claude" / "skills" / "cml" / "scripts"))
sys.path.insert(0, str(HERE.parent))
from datetime import datetime, timezone
import pathtrace as pt

base = datetime(2026, 9, 28, tzinfo=timezone.utc)
L = lambda a, b: {"a": a, "ai": "e0", "b": b, "bi": "e1"}
links = {"l1": L("PC", "FW"), "l2": L("FW", "Edge"), "l3": L("Edge", "ISP"),
         "l4": L("FW", "Web")}
owners = {"10.0.0.5": "PC", "100.11.0.2": "FW", "100.11.0.1": "ISP",
          "10.50.100.10": "Web", "100.11.0.20": "FW"}


def pk(no, t, s, d, rep, iid, seq):
    kind = "Echo (ping) reply" if rep else "Echo (ping) request"
    return {"no": no, "time": str(t), "protocol": "ICMP", "source": s, "destination": d,
            "info": f"{kind}  id=0x{iid:04x}, seq={seq}/1, ttl=64"}


def run(ip, per_link, src="PC"):
    raw = []
    for lid, pkts in per_link.items():
        raw += pt._parse_echo(pkts, lid, links[lid], base, owners)
    ev = [e for e in raw if pt._strict_match(e, ip, src)]
    own_ids = {e["icmp_id"] for e in ev if src in (e["a"], e["b"])}
    ev = [e for e in ev if e["icmp_id"] in own_ids]
    return ev, pt._nat_events(raw, ev, ip, src, own_ids)


N = 60
# A: PAT at FW, ICMP id rewritten; plus FW's own SLA probe to the same target
l1 = [p for i in range(N) for p in (pk(2*i, i*.1, "10.0.0.5", "100.11.0.1", 0, 0x10, i),
                                    pk(2*i+1, i*.1+.01, "100.11.0.1", "10.0.0.5", 1, 0x10, i))]
l2 = [p for i in range(N) for p in (pk(2*i, i*.1+.002, "100.11.0.2", "100.11.0.1", 0, 0x99, i),
                                    pk(2*i+1, i*.1+.008, "100.11.0.1", "100.11.0.2", 1, 0x99, i))]
l2 += [pk(500+k, k*1.5, "100.11.0.2", "100.11.0.1", 0, 0x77, 1000+k) for k in range(4)]
l3 = [dict(p) for p in l2[:2*N]]
ev, nat = run("100.11.0.1", {"l1": l1, "l2": l2, "l3": l3})
print("PAT: strict", len(ev), "nat", len(nat), sorted({e["link_id"] for e in nat}),
      {e.get("nat_by") for e in nat},
      "SLA LEAKED" if any(e["icmp_id"] == "0x0077" for e in nat) else "sla excluded")
assert len(nat) == 4 * N

# B: static NAT inbound, id kept, destination rewritten on the inside leg
owners["100.11.0.9"] = "ISP"
links["l1"] = L("ISP", "FW")
o1 = [p for i in range(N) for p in (pk(2*i, i*.1, "100.11.0.9", "100.11.0.20", 0, 0x21, i),
                                    pk(2*i+1, i*.1+.01, "100.11.0.20", "100.11.0.9", 1, 0x21, i))]
o4 = [p for i in range(N) for p in (pk(2*i, i*.1+.003, "100.11.0.9", "10.50.100.10", 0, 0x21, i),
                                    pk(2*i+1, i*.1+.006, "10.50.100.10", "100.11.0.9", 1, 0x21, i))]
ev, nat = run("100.11.0.20", {"l1": o1, "l4": o4}, src="ISP")
print("DNAT: strict", len(ev), "nat", len(nat), {(e.get("nat"), e.get("nat_to")) for e in nat})
assert len(nat) == 2 * N

# The source's unrelated concurrent ping (other id, other seqs) must stay out
o5 = [pk(900+i, i*.1, "100.11.0.9", "10.50.100.10", 0, 0x55, 5000+i) for i in range(5)]
ev, nat = run("100.11.0.20", {"l1": o1, "l4": o5}, src="ISP")
print("unrelated:", len(nat))
assert not nat

# End to end through analyse_capture (PAT case)
links["l1"] = L("PC", "FW")
names = ("PC", "FW", "Edge", "ISP")
nodes = [{"label": n, "probe_family": "asa" if n == "FW" else "linux", "links": []} for n in names]
for lid in ("l1", "l2", "l3"):
    l = links[lid]
    nodes[names.index(l["a"])]["links"].append({"link_id": lid, "local_iface": "e0",
                                                "peer": l["b"], "peer_iface": "e1"})
ev, nat = run("100.11.0.1", {"l1": l1, "l2": l2, "l3": l3})
log = sorted(ev + nat, key=lambda e: e["at"])
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "ISP"}, "100.11.0.1", log,
                         {"nodes": [], "links": [], "paths": 1}, {"hops": []}, owners, {}, True, N, N)
print(cap["verdict"], cap["path"])
for f in cap["findings"]:
    print("  -", f)
assert cap["path"] == ["PC", "FW", "Edge", "ISP"]

# DNAT end to end: target labelled as the public-address owner (FW) -> answered_by Web
links["l1"] = L("ISP", "FW")
nodes = [{"label": n, "probe_family": "asa" if n == "FW" else "linux", "links": []} for n in ("ISP", "FW", "Web")]
nodes[0]["links"].append({"link_id": "l1", "local_iface": "e0", "peer": "FW", "peer_iface": "e1"})
nodes[1]["links"].append({"link_id": "l4", "local_iface": "e2", "peer": "Web", "peer_iface": "e0"})
ev, nat = run("100.11.0.20", {"l1": o1, "l4": o4}, src="ISP")
cap = pt.analyse_capture(nodes, {"label": "ISP"}, {"label": "FW"}, "100.11.0.20",
                         sorted(ev + nat, key=lambda e: e["at"]),
                         {"nodes": [], "links": [], "paths": 1}, {"hops": []}, owners, {}, True, N, N)
print(cap["verdict"], cap["path"], cap["answered_by"])
for f in cap["findings"]:
    print("  -", f)
assert cap["path"] == ["ISP", "FW", "Web"] and cap["answered_by"] == "Web"
print("ALL OK")

# ECMP: requests PC-A-B1-T, replies T-B2-A-PC -> path is the request route, return reported
links.update({"e1": L("PC", "A"), "e2": L("A", "B1"), "e3": L("B1", "T"), "e4": L("A", "B2"), "e5": L("B2", "T")})
owners.update({"10.9.0.1": "PC", "10.9.9.9": "T"})
names = ("PC", "A", "B1", "B2", "T")
nodes = [{"label": n, "probe_family": "ios", "links": []} for n in names]
for lid in ("e1", "e2", "e3", "e4", "e5"):
    l = links[lid]
    nodes[names.index(l["a"])]["links"].append({"link_id": lid, "local_iface": "e0", "peer": l["b"], "peer_iface": "e1"})
req = lambda no, i: pk(no, i*.1, "10.9.0.1", "10.9.9.9", 0, 0x5, i)
rep = lambda no, i: pk(no, i*.1+.01, "10.9.9.9", "10.9.0.1", 1, 0x5, i)
per = {"e1": [p for i in range(N) for p in (req(2*i, i), rep(2*i+1, i))],
       "e2": [req(i, i) for i in range(N)], "e3": [req(i, i) for i in range(N)],
       "e4": [rep(i, i) for i in range(N)], "e5": [rep(i, i) for i in range(N)]}
ev, nat = run("10.9.9.9", per)
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "T"}, "10.9.9.9", sorted(ev, key=lambda e: e["at"]),
                         {"nodes": list(names), "links": [], "paths": 2}, {"hops": []}, owners, {}, True, N, N)
print(cap["verdict"]["level"], cap["path"], "return:", cap["return_path"])
for f in cap["findings"]:
    print("  -", f[:160])
assert cap["path"] == ["PC", "A", "B1", "T"] and cap["return_path"] == ["T", "B2", "A", "PC"]
print("ECMP OK")

# Uncaptured link in the middle: PC-A captured, A-B1 capture never started, B1-T captured
per_gap = {"e1": per["e1"], "e3": [p for i in range(N) for p in (req(2*i, i), rep(2*i+1, i))]}
ev, nat = run("10.9.9.9", per_gap)
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "T"}, "10.9.9.9", sorted(ev, key=lambda e: e["at"]),
                         {"nodes": list(names), "links": [], "paths": 2}, {"hops": []}, owners, {}, True, N, N,
                         uncaptured={"e2"})
print(cap["verdict"]["level"], cap["path"])
for f in cap["findings"]:
    print("  -", f[:140])
assert cap["path"] == ["PC", "A", "B1", "T"] and cap["verdict"]["level"] == "ok"
assert any(r["id"] == "e2" and r["use"] == "inferred" for r in cap["links"])
# ...but no inference when the gap link is not needed (e4 uncaptured, path complete without it)
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "T"}, "10.9.9.9", sorted(run("10.9.9.9", per)[0], key=lambda e: e["at"]),
                         {"nodes": list(names), "links": [], "paths": 2}, {"hops": []}, owners, {}, True, N, N,
                         uncaptured={"e4"})
assert not any(r["use"] == "inferred" for r in cap["links"])
print("GAP OK")

# Nothing captured at all (CML started 1 of N captures): must NOT invent a path
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "T"}, "10.9.9.9", [],
                         {"nodes": list(names), "links": [], "paths": 2}, {"hops": []}, owners, {}, True, N, N,
                         uncaptured={"e1", "e2", "e3", "e4", "e5"})
assert not any(r["use"] == "inferred" for r in cap["links"]) and cap["verdict"]["level"] != "ok", cap["verdict"]
# Two consecutive uncaptured links (only PC-A captured): must NOT bridge both
ev, _ = run("10.9.9.9", {"e1": per["e1"]})
cap = pt.analyse_capture(nodes, {"label": "PC"}, {"label": "T"}, "10.9.9.9", sorted(ev, key=lambda e: e["at"]),
                         {"nodes": list(names), "links": [], "paths": 2}, {"hops": []}, owners, {}, True, N, N,
                         uncaptured={"e2", "e3"})
assert not any(r["use"] == "inferred" for r in cap["links"]) and cap["verdict"]["level"] != "ok", cap["verdict"]
print("NO-FABRICATION OK")
