"""Offline tests of the service checks (services.py) and the DNS/TCP capture parsing.
Run: py -3.11 netview/tests/test_services.py"""
import socket
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / ".claude" / "skills" / "cml" / "scripts"))
sys.path.insert(0, str(HERE.parent))
import failover as fo  # noqa: E402
import services as sv  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + label + (f"  - {detail}" if detail and not cond else ""))
    ok &= bool(cond)


# ---- DNS wire format: a response for fileserver.p45.nz -> 10.60.0.70 (with a compressed name)
q = b"".join(bytes([len(x)]) + x.encode() for x in "fileserver.p45.nz".split(".")) + b"\0"
msg = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + q + struct.pack(">HH", 1, 1)
msg += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("10.60.0.70")
d = fo.parse_dns(msg)
check("dns: name", d["name"] == "fileserver.p45.nz", d)
check("dns: response + answer", d["response"] and d["answers"] == ["10.60.0.70"], d)
check("dns: truncated message does not raise", fo.parse_dns(msg[:20]) is not None)

# ---- a pcap with one DNS response and one TCP SYN-ACK, on a VLAN-tagged trunk


def frame(ip_payload, proto, src, dst):
    iph = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(ip_payload), 0, 0, 64, proto, 0,
                      socket.inet_aton(src), socket.inet_aton(dst))
    return b"\x00" * 12 + b"\x81\x00\x00\x50" + b"\x08\x00" + iph + ip_payload


udp = struct.pack(">HHHH", 53, 53000, 8 + len(msg), 0) + msg
tcp = struct.pack(">HHIIBBHHH", 445, 50000, 1, 2, 0x50, 0x12, 1000, 0, 0)
pkts = [frame(udp, 17, "172.16.80.53", "192.168.1.1"), frame(tcp, 6, "10.60.0.70", "192.168.1.10")]
pcap = struct.pack(">IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
for i, p in enumerate(pkts):
    pcap += struct.pack(">IIII", 1000 + i, 0, len(p), len(p)) + p
ev = fo.parse_pcap(pcap, "L1", {"a": "FileServer-DR-1", "b": "SR"}, {"172.16.80.53": "DC-DR-1"})
check("pcap: dns event", ev and ev[0]["kind"] == "dns" and ev[0]["dir"] == "response" and ev[0]["dns_answers"] == ["10.60.0.70"], ev)
check("pcap: tcp synack event", len(ev) == 2 and ev[1]["kind"] == "tcp" and ev[1]["dir"] == "synack" and ev[1]["tcp_ports"] == (445, 50000), ev)

# ---- capture filter widens for the checks, tagged and untagged
b = fo.bpf_for([{"kind": "dns"}, {"kind": "tcp", "port": 445}])
check("bpf: dns + tcp ports added", "udp port 53" in b and "tcp port 445" in b and "vlan and (" in b, b)
check("bpf: no checks = base", fo.bpf_for([]) == fo.BPF)
for combo in ([{"kind": "dns"}], [{"kind": "tcp", "port": 445}], [{"kind": "dns"}, {"kind": "tcp", "port": 445}],
              [{"kind": "dns"}] + [{"kind": "tcp", "port": p_} for p_ in (445, 80, 443, 3389, 8080)]):
    f_ = fo.bpf_for(combo)
    check(f"bpf: <= 128 chars for {len(combo)} checks", len(f_) <= 128 and "icmp" in f_ and "vlan and (" in f_, f"{len(f_)}: {f_}")
check("bpf: base <= 128", len(fo.BPF) <= 128)

# ---- validation
BY = {"WIN": {"up": True, "probe_family": "windows"}, "SW": {"up": True, "probe_family": "ios"},
      "LNX": {"up": True, "probe_family": "linux"}, "OFF": {"up": False, "probe_family": "windows"}}
err, cs = sv.validate([{"kind": "dns", "name": "fileserver.p45.nz", "server": "192.168.1.1"},
                       {"kind": "tcp", "host": "10.60.0.70", "port": "445"},
                       {"kind": "dhcp", "from": "SW", "iface": "Vlan60"}], BY, "WIN")
check("validate: good set", err is None and len(cs) == 3 and cs[0]["from"] == "WIN", err)
check("validate: tcp label", cs and cs[1]["label"] == "TCP 10.60.0.70:445 (SMB)", cs and cs[1])
check("validate: dns from linux refused", sv.validate([{"kind": "dns", "name": "x"}], BY, "LNX")[0])
check("validate: stopped node refused", sv.validate([{"kind": "dhcp", "from": "OFF"}], BY, "WIN")[0])
check("validate: switch dhcp needs iface", sv.validate([{"kind": "dhcp", "from": "SW"}], BY, "WIN")[0])
check("validate: injection refused", sv.validate([{"kind": "dns", "name": "x;Get-Process"}], BY, "WIN")[0])
check("token", sv.token(cs[0]) == "dns=fileserver.p45.nz@192.168.1.1" and sv.token(cs[1]) == "tcp=10.60.0.70:445")

# ---- runner feed + summary: ok, 3 s outage, recovered
r = sv.Runner(None, cs[:2], "WIN", "windows")
for i, t in enumerate(range(-5, 10)):
    good = not (0 <= t < 3)
    r.feed(f"nvtcp 10.60.0.70:445 {'ok' if good else 'fail'} ms={2 if good else 1000}", 100.0 + t + (0.002 if good else 1.0))
check("feed: tcp results kept", len(r.results[cs[1]["id"]]) == 15)
ev2 = [{"kind": "tcp", "dir": "synack", "src": "10.60.0.70", "tcp_ports": (445, 1), "a": "FS-HQ", "b": "SR", "t": -1.0},
       {"kind": "tcp", "dir": "synack", "src": "10.60.0.70", "tcp_ports": (445, 1), "a": "SR", "b": "FS-DR", "t": 4.0}]
out = sv.summarise(cs[:2], r.results, lambda t: t - 100.0, ev2, {"FS-HQ", "FS-DR"}, {})
tcp_s = out[1]["summary"]
check("summary: before all ok", tcp_s["before_ok"] == tcp_s["before"] == 5, tcp_s)
check("summary: outage 3 s, back at 3", tcp_s["down_s"] == 3.0 and tcp_s["recovered_at"] == 3.0, tcp_s)
r2 = sv.Runner(None, cs[:2], "WIN", "windows")
for t_, good in [(-2, 1), (-1, 1), (0, 0), (1, 0), (2, 1), (3, 0), (4, 1), (5, 1), (6, 1), (7, 1), (8, 0), (9, 1)]:
    r2.feed(f"nvtcp 10.60.0.70:445 {'ok' if good else 'fail'} ms=5", 100.0 + t_ + 0.005)
s2 = sv.summarise(cs[:2], r2.results, lambda t: t - 100.0, [], set(), {})[1]["summary"]
check("summary: blip after sustained recovery", s2["recovered_at"] == 4.0 and s2["down_s"] == 4.0 and s2["blips"] == 1, s2)
check("summary: evidence holder moved", tcp_s["evidence"]["before"] == {"FS-HQ": 1} and tcp_s["evidence"]["after"] == {"FS-DR": 1}, tcp_s["evidence"])

# ---- anycast DNS: the answering DC is the origin end of the reply's first link
dc = {"id": "c9", "kind": "dns", "name": "fileserver.p45.nz", "server": "10.60.0.53"}
evs = [{"kind": "dns", "dir": "response", "dns_name": "fileserver.p45.nz", "src": "10.60.0.53", "dst": "192.168.1.1",
        "seq": 7, "a": "DC-DR-1", "b": "SR-DR-1", "t": 3.0},
       {"kind": "dns", "dir": "response", "dns_name": "fileserver.p45.nz", "src": "10.60.0.53", "dst": "192.168.1.1",
        "seq": 7, "a": "SR-DR-1", "b": "Core-DR-1", "t": 3.001},
       {"kind": "dns", "dir": "response", "dns_name": "fileserver.p45.nz", "src": "10.60.0.53", "dst": "192.168.1.1",
        "seq": 4, "a": "SR-HQ-1", "b": "DC-HQ-2", "t": -2.0},
       {"kind": "dns", "dir": "response", "dns_name": "fileserver.p45.nz", "src": "10.60.0.53", "dst": "192.168.1.1",
        "seq": 4, "a": "Core-HQ-1", "b": "SR-HQ-1", "t": -1.999}]
ev9 = sv._evidence(dc, evs, set(), {})
dc_h = dict(dc, holders=["DC-HQ-2", "DC-DR-1"])
tie = [dict(e, t=5.0) for e in evs[:2]]                       # identical timestamps
check("anycast dns: holder wins over timestamp ties", sv._evidence(dc_h, tie, set(), {})["after"] == {"DC-DR-1": 1})
check("anycast dns: answering DC before/after", ev9["before"] == {"DC-HQ-2": 1} and ev9["after"] == {"DC-DR-1": 1}, ev9)

print("ALL SERVICE TESTS OK" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
