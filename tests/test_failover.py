"""Offline tests of the failover-capture analysis (synthetic pcaps, no CML).
Run: py -3.11 netview/tests/test_failover.py

Topology (the WFH -> file server shape the feature was built for):

  WFH -L1- HR -L2- ISP -L3- EdgeHQ -L4- FWHQ -L5- CoreHQ -L6- FSHQ     (HQ, before)
                    ISP -L7- EdgeDR -L8- FWDR -L9- CoreDR -L10- FSDR   (DR, after)

Inner ICMP on L1 and inside each site; ESP (HR <-> FWHQ / FWDR) on the carrier links.
"""
import socket
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / ".claude" / "skills" / "cml" / "scripts"))
sys.path.insert(0, str(HERE.parent))
import failover as fo  # noqa: E402

T = 1_800_000_000.0                   # the CML host clock
WFH, VIP, HR = "192.168.1.10", "10.60.0.70", "100.64.1.1"
FWHQ, FWDR, EDR = "10.255.4.2", "10.255.4.18", "10.255.4.17"
OWNERS = {WFH: "WFH", HR: "HR", FWHQ: "FWHQ", FWDR: "FWDR", EDR: "EdgeDR", VIP: "FSHQ"}
LINKS = {f"L{i}": {"id": f"L{i}", "a": a, "ai": "e0", "b": b, "bi": "e1"} for i, (a, b) in enumerate([
    ("WFH", "HR"), ("HR", "ISP"), ("ISP", "EdgeHQ"), ("EdgeHQ", "FWHQ"), ("FWHQ", "CoreHQ"), ("CoreHQ", "FSHQ"),
    ("ISP", "EdgeDR"), ("EdgeDR", "FWDR"), ("FWDR", "CoreDR"), ("CoreDR", "FSDR")], start=1)}
HOP = 0.0003


def ip_hdr(src, dst, proto, payload):
    tot = 20 + len(payload)
    return struct.pack(">BBHHHBBH4s4s", 0x45, 0, tot, 0, 0, 64, proto, 0,
                       socket.inet_aton(src), socket.inet_aton(dst)) + payload


def eth(ipb, vlan=None):
    tag = struct.pack(">HH", 0x8100, vlan) if vlan else b""
    return b"\x00" * 12 + tag + b"\x08\x00" + ipb


def icmp_echo(src, dst, typ, seq, iid=0x34C8):
    return ip_hdr(src, dst, 1, struct.pack(">BBHHH", typ, 0, 0, iid, seq) + b"x" * 8)


def icmp_unreach(src, dst, orig):
    return ip_hdr(src, dst, 1, struct.pack(">BBHI", 3, 1, 0, 0) + orig[:28])


def esp(src, dst, spi, eseq):
    return ip_hdr(src, dst, 50, struct.pack(">II", spi, eseq) + b"e" * 40)


class Caps:
    """Collects (link, time, frame) and writes one big-endian pcap per link, like CML."""
    def __init__(self):
        self.pk = {k: [] for k in LINKS}
        self.eseq = {}

    def add(self, link, t, frame):
        self.pk[link].append((t, frame))

    def esp(self, link, t, src, dst, spi):
        n = self.eseq.setdefault((spi, link), 0) + 1
        self.eseq[(spi, link)] = n
        self.add(link, t, eth(esp(src, dst, spi, n)))

    def pcaps(self):
        out = {}
        for k, pk in self.pk.items():
            b = struct.pack(">IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
            for t, f in sorted(pk, key=lambda x: x[0]):
                b += struct.pack(">IIII", int(t), int(round((t % 1) * 1e6)), len(f), len(f)) + f
            out[k] = b
        return out


def via_hq(c, seq, t):
    """A healthy probe through HQ, and its reply."""
    c.add("L1", t, eth(icmp_echo(WFH, VIP, 8, seq)))
    for i, l in enumerate(("L2", "L3", "L4")):
        c.esp(l, t + HOP * (i + 1), HR, FWHQ, 0xAAAA)
    c.add("L5", t + HOP * 4, eth(icmp_echo(WFH, VIP, 8, seq), vlan=80))
    c.add("L6", t + HOP * 5, eth(icmp_echo(WFH, VIP, 8, seq), vlan=80))
    r = t + 0.002
    c.add("L6", r, eth(icmp_echo(VIP, WFH, 0, seq), vlan=80))
    c.add("L5", r + HOP, eth(icmp_echo(VIP, WFH, 0, seq), vlan=80))
    for i, l in enumerate(("L4", "L3", "L2")):
        c.esp(l, r + HOP * (i + 2), FWHQ, HR, 0xBBBB)
    c.add("L1", r + HOP * 5, eth(icmp_echo(VIP, WFH, 0, seq)))


def via_dr(c, seq, t):
    c.add("L1", t, eth(icmp_echo(WFH, VIP, 8, seq)))
    for i, l in enumerate(("L2", "L7", "L8")):
        c.esp(l, t + HOP * (i + 1), HR, FWDR, 0xCCCC)
    c.add("L9", t + HOP * 4, eth(icmp_echo(WFH, VIP, 8, seq)))
    c.add("L10", t + HOP * 5, eth(icmp_echo(WFH, VIP, 8, seq)))
    r = t + 0.002
    c.add("L10", r, eth(icmp_echo(VIP, WFH, 0, seq)))
    c.add("L9", r + HOP, eth(icmp_echo(VIP, WFH, 0, seq)))
    for i, l in enumerate(("L8", "L7", "L2")):
        c.esp(l, r + HOP * (i + 2), FWDR, HR, 0xDDDD)
    c.add("L1", r + HOP * 5, eth(icmp_echo(VIP, WFH, 0, seq)))


def lost_into_hq(c, seq, t):
    """HQ is gone but the home router still uses the HQ tunnel: dies at EdgeHQ."""
    c.add("L1", t, eth(icmp_echo(WFH, VIP, 8, seq)))
    c.esp("L2", t + HOP, HR, FWHQ, 0xAAAA)
    c.esp("L3", t + 2 * HOP, HR, FWHQ, 0xAAAA)


def dropped_at_fwdr(c, seq, t):
    """Reaches the DR firewall but it has no route yet: an unreachable comes back."""
    req = icmp_echo(WFH, VIP, 8, seq)
    c.add("L1", t, eth(req))
    for i, l in enumerate(("L2", "L7", "L8")):
        c.esp(l, t + HOP * (i + 1), HR, FWDR, 0xCCCC)
    un = icmp_unreach(EDR, WFH, req)
    for i, l in enumerate(("L8", "L7", "L2")):
        c.esp(l, t + 0.001 + HOP * i, FWDR, HR, 0xDDDD)
    c.add("L1", t + 0.002, eth(un))


def run(pattern, interval=0.2):
    """pattern: one char per probe - H (via HQ), D (via DR), x (lost into HQ), u (unreachable at FWDR)."""
    c = Caps()
    console = {}
    for seq, ch in enumerate(pattern):
        t = T + seq * interval
        {"H": via_hq, "D": via_dr, "x": lost_into_hq, "u": dropped_at_fwdr}[ch](c, seq, t)
        if ch in "HD":
            console[seq] = t + 0.0023 + 0.05        # local clock == capture clock + 50 ms console lag
    first_loss = pattern.find("x") if "x" in pattern else len(pattern)
    times_local = {"T0": T + 0.05 + first_loss * interval - 1.0, "T1": T + 0.05 + first_loss * interval + 0.5,
                   "stopped": {"EdgeHQ": T + 0.05 + first_loss * interval, "CoreHQ": T + 0.05 + first_loss * interval + 0.5},
                   "end": T + 0.05 + len(pattern) * interval}
    return fo.analyse(c.pcaps(), LINKS, OWNERS, src="WFH", ip=VIP, endpoints={"FSHQ", "FSDR"},
                      down={"EdgeHQ", "CoreHQ"}, baseline_path=[], single_id=True, interval=interval,
                      console=console, times_local=times_local, window_s=20,
                      completed_by="recovered and stable", coverage={"in_scope": 10, "captured": 10})


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  - ' + str(detail)) if detail else ''}")
    return ok


def main():
    ok = True
    # parse: VLAN-tagged frames, ESP sequence numbers, unreachable carrying the probe
    c = Caps()
    via_hq(c, 7, T)
    dropped_at_fwdr(c, 8, T + 0.2)
    caps = c.pcaps()
    ev5 = fo.parse_pcap(caps["L5"], "L5", LINKS["L5"], OWNERS)
    ok &= check("pcap: 802.1Q-tagged echo parsed", any(e["kind"] == "echo" and e["seq"] == 7 for e in ev5))
    ev2 = fo.parse_pcap(caps["L2"], "L2", LINKS["L2"], OWNERS)
    ok &= check("pcap: ESP SPI + sequence number", any(e["kind"] == "esp" and e["spi"] == "0xaaaa" and e["esp_seq"] == 1 for e in ev2))
    ev1 = fo.parse_pcap(caps["L1"], "L1", LINKS["L1"], OWNERS)
    un = [e for e in ev1 if e["kind"] == "unreach"]
    ok &= check("pcap: unreachable carries the original probe (seq 8)", un and un[0]["seq"] == 8 and un[0]["orig_dst"] == VIP)
    ok &= check("pcap: absolute microsecond timestamps", abs(ev1[0]["at"] - T) < 1e-5, ev1[0]["at"] - T)

    # 1. clean failover: 15 on HQ, 15 lost into HQ, 3 dropped at FWDR, then DR
    r = run("H" * 15 + "x" * 15 + "u" * 3 + "D" * 20)
    m = r["metrics"]
    ok &= check("failover: verdict", m["verdict"] == "failed over", m["verdict"])
    ok &= check("failover: path changed, HQ -> DR answerer", m["path_changed"] and m["answered_by"] == ["FSHQ", "FSDR"],
                m["answered_by"])
    ok &= check("failover: lost = 18 (15 into HQ + 3 unreachable)", m["lost"] == 18, m["lost"])
    ok &= check("failover: outage = 18 probes x 0.2 s = 3.6 s", abs(m["outage_s"] - 3.6) < 0.01, m["outage_s"])
    ok &= check("failover: failover time = first DR reply (probe 33, +6.6035) - F (probe 14 reply at CoreHQ, +2.8023)", abs(m["failover_s"] - 3.801) < 0.002, m["failover_s"])
    ok &= check("failover: one outage period", m["outage_periods"] == 1)
    ok &= check("failover: F is the last packet a dead node forwarded (CoreHQ, probe 14)",
                "forwarded" in r["clock"]["F_basis"])
    roles = {p["role"] for p in r["paths"]}
    ok &= check("failover: baseline + failover paths found", {"baseline", "failover"} <= roles, roles)
    base = next(p for p in r["paths"] if p["role"] == "baseline")
    ok &= check("failover: baseline path stitched through the tunnel underlay",
                base["hops"] == ["WFH", "HR", "ISP", "EdgeHQ", "FWHQ", "CoreHQ", "FSHQ"], base["hops"])
    fail = next(p for p in r["paths"] if p["role"] == "failover")
    ok &= check("failover: failover path via DR", fail["hops"] == ["WFH", "HR", "ISP", "EdgeDR", "FWDR", "CoreDR", "FSDR"],
                fail["hops"])
    lost = [p for p in r["probes"] if p["outcome"] == "lost-request"]
    ok &= check("failover: lost probes died going into EdgeHQ",
                lost and all(p["died_at"]["into"] == "EdgeHQ" for p in lost), lost[0]["died_at"] if lost else None)
    un = [p for p in r["probes"] if p["outcome"] == "unreachable"]
    ok &= check("failover: unreachables matched to their probes, sender named",
                len(un) == 3 and un[0]["died_at"]["unreachable_from"] == "EdgeDR", un[0]["died_at"] if un else None)
    ok &= check("failover: local clock mapped (console lag 50 ms)",
                abs(r["clock"]["local_to_capture_s"] - (0.05 + HOP * 0)) < 0.01, r["clock"]["local_to_capture_s"])
    ok &= check("failover: timeline has failure + first reply events",
                {"failure", "first_reply_new", "first_loss"} <= {e["kind"] for e in r["events"]})
    ok &= check("failover: packets relative to F", any(p["t"] < 0 for p in r["packets"]) and any(p["t"] > 0 for p in r["packets"]))

    # 1b. the live-run case (2026-10-02): the core links were NOT captured, only
    #     ESP crosses the dead edge -> F must come from ESP transit (spi + seq),
    #     and the lost probes must die going into EdgeHQ, not "at the home router"
    c = Caps()
    pattern = "H" * 15 + "x" * 15 + "D" * 20
    console = {}
    for seq, ch in enumerate(pattern):
        {"H": via_hq, "D": via_dr, "x": lost_into_hq}[ch](c, seq, T + seq * 0.2)
        if ch in "HD":
            console[seq] = T + seq * 0.2 + 0.05
    caps = c.pcaps()
    del caps["L5"], caps["L6"]                      # CML discarded the core's captures
    r = fo.analyse(caps, LINKS, OWNERS, src="WFH", ip=VIP, endpoints={"FSHQ", "FSDR"}, down={"EdgeHQ", "CoreHQ"},
                   baseline_path=[], single_id=True, interval=0.2, console=console,
                   times_local={"T0": T, "T1": T + 9, "stopped": {}, "end": T + 10}, window_s=20,
                   completed_by="x", coverage={})
    ok &= check("ESP-only: F from an ESP packet crossing the dead edge", "forwarded" in r["clock"]["F_basis"],
                r["clock"]["F_basis"])
    lost = [p for p in r["probes"] if p["outcome"] == "lost-request"]
    ok &= check("ESP-only: lost probes die going into EdgeHQ", lost and all(p["died_at"]["into"] == "EdgeHQ" for p in lost),
                lost[0]["died_at"] if lost else None)
    ok &= check("ESP-only: failover = probe 30 reply (+6.0035) - probe 14 reply ESP leaving EdgeHQ (+2.8029)",
                abs(r["metrics"]["failover_s"] - 3.2006) < 0.002, r["metrics"]["failover_s"])

    # 1c. capture timestamps slightly out of order between links: hop order must still be right
    c = Caps()
    via_dr(c, 0, T)
    c.pk["L8"] = [(t - 0.0009, f) for t, f in c.pk["L8"]]       # stamped before L7/L2
    r = fo.analyse(c.pcaps(), LINKS, OWNERS, src="WFH", ip=VIP, endpoints={"FSHQ", "FSDR"}, down=set(),
                   baseline_path=[], single_id=True, interval=0.2, console={0: T + 0.05},
                   times_local={"T0": T, "T1": T + 1, "stopped": {}, "end": T + 2}, window_s=20,
                   completed_by="x", coverage={})
    hops = r["paths"][0]["hops"] if r["paths"] else None
    ok &= check("jitter: hop order follows the links, not the timestamps",
                hops == ["WFH", "HR", "ISP", "EdgeDR", "FWDR", "CoreDR", "FSDR"], hops)

    # 2. no impact: nothing through the disaster nodes changes
    r = run("D" * 40)
    ok &= check("no impact: verdict", r["metrics"]["verdict"] == "no impact", r["metrics"]["verdict"])
    ok &= check("no impact: F falls back to power-off completion", "power-off" in r["clock"]["F_basis"], r["clock"]["F_basis"])

    # 3. never recovers
    r = run("H" * 10 + "x" * 40)
    ok &= check("not recovered: verdict", r["metrics"]["verdict"] == "not recovered", r["metrics"]["verdict"])
    ok &= check("not recovered: no failover time", r["metrics"]["failover_s"] is None)

    # 4. today's DR oscillation: 5 s on / 5 s off
    r = run("H" * 10 + "x" * 25 + "D" * 25 + "x" * 25 + "D" * 25)
    ok &= check("unstable: verdict (two outage periods)", r["metrics"]["verdict"] == "unstable"
                and r["metrics"]["outage_periods"] == 2, (r["metrics"]["verdict"], r["metrics"]["outage_periods"]))

    # 5. completion logic (live): early stop only after loss + K replies
    st = fo._live_state({i: 0 for i in range(10)} | {i: 0 for i in range(20, 32)},
                        [100 + i * 0.2 for i in range(32)], now=100 + 32 * 0.2 + 2, t0=100, t1=101,
                        interval=0.2, new_path_seen=False)
    ok &= check("live: K = 10 at 5/s", st["k"] == 10, st["k"])
    ok &= check("live: loss then 12 replies -> streak >= K", st["had_loss"] and st["streak"] >= 10, st)
    st2 = fo._live_state({i: 0 for i in range(32)}, [100 + i * 0.2 for i in range(32)], now=110, t0=100, t1=101,
                         interval=0.2, new_path_seen=False)
    ok &= check("live: no loss, no new path -> not a stop condition", not st2["had_loss"] and not st2["new_path"])

    # 6. exports
    res = {"source": "WFH", "target": "FSDR", "disaster": {"mode": "failover", "failover": run("H" * 15 + "x" * 15 + "D" * 20)}}
    for kind in ("packets", "probes", "paths"):
        txt = fo.to_csv(res, kind)
        ok &= check(f"csv: {kind}", txt and txt.count("\n") > 2)
    # 7. cable cut: F is the last test packet seen on the cut link
    ev = [{"kind": "echo", "probe": 1, "dir": "request", "link_id": "CUT", "a": "X", "b": "Y", "t": 5.0},
          {"kind": "echo", "probe": 2, "dir": "request", "link_id": "CUT", "a": "X", "b": "Y", "t": 5.2},
          {"kind": "echo", "probe": 3, "dir": "request", "link_id": "OTHER", "a": "X", "b": "Z", "t": 6.0}]
    ok &= check("cut: F = last packet on the cut link", fo.failure_moment([], ev, set(), {"CUT"}) == 5.2)
    ok &= check("cut: nothing cut, nothing down -> no F", fo.failure_moment([], ev, set(), set()) is None)
    # 8. console truth: a reply the source saw beats a capture that missed it
    pr = [{"key": k, "sent": k * 0.2, "outcome": o, "req_path": [], "rep_path": []}
          for k, o in ((1, "ok"), (2, "lost-reply"), (3, "lost-request"), (4, "lost-reply"))]
    out = {p["key"]: p for p in fo.fill_missing(pr, {1: 0.2, 2: 0.4, 3: 0.6}, 0.2, True)}
    ok &= check("console: replied probes become ok", out[2]["outcome"] == "ok" and out[3]["outcome"] == "ok"
                and out[2].get("reply_inferred"), out)
    ok &= check("console: unanswered stays lost", out[4]["outcome"] == "lost-reply", out[4])
    # 9. repeat: a series averages its runs and keeps each run's timeline
    def one(fs, lost_at, back_at, smb_down):
        prs = [{"key": k, "sent": -1 + k * 0.2, "outcome": "ok" if not (lost_at <= -1 + k * 0.2 < back_at) else "lost-reply",
                "path": "A"} for k in range(30)]
        return {"disaster": {"run_name": f"r{fs}", "failover": {
            "metrics": {"verdict": "failed over", "failover_s": fs, "outage_s": back_at - lost_at, "lost": 3, "sent_after_F": 25},
            "probes": prs, "paths": [{"id": "A", "role": "baseline", "first": -1, "last": 5, "hops": ["x", "y"]}],
            "events": [{"t": fs, "kind": "first_reply_new", "text": "x"}], "probe": {"interval_s": 0.2},
            "services": [{"id": "c1", "kind": "tcp", "label": "TCP", "attempts": [{"t": 1, "ok": False, "ms": 1000}],
                          "summary": {"verdict": "recovered" if smb_down else "no impact", "down_s": smb_down,
                                      "first_fail": 1.0 if smb_down else None, "recovered_at": 1 + smb_down if smb_down else None}}]}}}
    ser = fo.series_summary([one(2.0, 0.4, 2.0, 2.0), one(3.0, 0.4, 3.0, 3.0), one(4.0, 0.4, 4.0, None)])
    ok &= check("series: failover mean/min/max", ser["avg"]["failover_s"] == {"mean": 3.0, "min": 2.0, "max": 4.0, "n": 3},
                ser["avg"]["failover_s"])
    ok &= check("series: loss window averaged", abs(ser["avg"]["loss_start"]["mean"] - 0.4) < 0.01
                and abs(ser["avg"]["loss_end"]["mean"] - 3.0) < 0.01, (ser["avg"]["loss_start"], ser["avg"]["loss_end"]))
    smb = ser["services"][0]
    ok &= check("series: service outage averages only impacted runs", smb["impacted"] == 2
                and smb["down_s"]["mean"] == 2.5 and smb["recovered_at"]["mean"] == 3.5, smb)
    ok &= check("series: every run's probes kept", [len(r["probes"]) for r in ser["runs"]] == [30, 30, 30])
    ok &= check("series: verdicts counted", ser["verdicts"] == {"failed over": 3}, ser["verdicts"])

    print("ALL FAILOVER TESTS OK" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
