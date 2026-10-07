"""Service checks inside a failover capture (2026-10-06).

A disaster test proves routing with pings; a user needs DNS to answer, a file
share to accept a connection, and a fresh DHCP lease. Each check here runs
through the same baseline -> failure -> recovery window as the pings, and
becomes its own row on the failover timeline.

    {"kind": "dns",  "from": <node>, "name": "fileserver.p45.nz", "server": "192.168.1.1"}
    {"kind": "tcp",  "from": <node>, "host": "10.60.0.70", "port": 445}
    {"kind": "dhcp", "from": <node>, "iface": "Vlan60"}      # iface: IOS only

DNS and TCP run every second from a Windows node (NetView's bridge). From the
test's own source they ride the bridge's `watch` stream next to the 5/s pings,
because a console takes one session at a time. DHCP runs twice: before the
failure (the power-off waits for it) and `dhcp_after_s` after it, from an IOS
switch's DHCP-client SVI (release/renew) or a Windows node (ipconfig).
"""
from __future__ import annotations

import re
import threading
import time

KINDS = ("dns", "tcp", "dhcp")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
_IFACE = re.compile(r"^[A-Za-z][A-Za-z\-]*\d+(/\d+)*$")
_SVC_LINE = re.compile(r"^nv(dns|tcp) (\S+) (ok|fail) ms=(\d+)(?: answer=(\S*))?")
DHCP_AFTER_S = 5


# ------------------------------------------------------------- validation
def validate(checks: list[dict], by_label: dict, source: str) -> tuple[str | None, list[dict]]:
    """(error, normalised checks). An error refuses the run before anything happens."""
    out = []
    for i, c in enumerate(checks or []):
        kind = c.get("kind")
        frm = c.get("from") or source
        n = by_label.get(frm)
        if kind not in KINDS:
            return f"Unknown check type: {kind}.", []
        if not n or not n.get("up"):
            return f"Check {i + 1}: {frm} is not running.", []
        fam = n.get("probe_family")
        if kind in ("dns", "tcp") and fam != "windows":
            return f"Check {i + 1}: DNS and TCP checks run from a Windows PC with NetView's bridge; {frm} is {fam}.", []
        if kind == "dhcp" and fam not in ("windows", "ios"):
            return f"Check {i + 1}: DHCP checks run from a Windows PC or an IOS switch; {frm} is {fam}.", []
        d = {"id": f"c{i + 1}", "kind": kind, "from": frm, "family": fam}
        if kind == "dns":
            name, server = str(c.get("name") or ""), str(c.get("server") or "")
            if not _HOST.match(name) or (server and not _HOST.match(server)):
                return f"Check {i + 1}: bad DNS name or server.", []
            d.update(name=name, server=server,
                     label=f"DNS {name}" + (f" via {server}" if server else " (PC's own resolver)"))
        elif kind == "tcp":
            host = str(c.get("host") or "")
            try:
                port = int(c.get("port"))
            except (TypeError, ValueError):
                port = 0
            if not _HOST.match(host) or not 1 <= port <= 65535:
                return f"Check {i + 1}: bad TCP host or port.", []
            d.update(host=host, port=port, label=f"TCP {host}:{port}" + (" (SMB)" if port == 445 else ""))
        else:
            iface = str(c.get("iface") or "")
            if fam == "ios" and not _IFACE.match(iface):
                return f"Check {i + 1}: DHCP from a switch needs its DHCP-client interface (e.g. Vlan60).", []
            d.update(iface=iface, label=f"DHCP lease from {frm}" + (f" {iface}" if iface else ""))
        out.append(d)
    if len(out) > 8:
        return "At most 8 service checks.", []
    return None, out


def token(c: dict) -> str:
    """The bridge `watch` argument for a DNS/TCP check."""
    if c["kind"] == "dns":
        return f"dns={c['name']}" + (f"@{c['server']}" if c["server"] else "")
    return f"tcp={c['host']}:{c['port']}"


# ----------------------------------------------------------------- runner
class Runner:
    """Runs every check that is NOT folded into the source's own stream."""

    def __init__(self, cml, checks: list[dict], source: str, source_family: str, log=None):
        self.cml, self.checks, self.source, self.log = cml, checks, source, log or (lambda *_: None)
        self.results: dict[str, list[dict]] = {c["id"]: [] for c in checks}
        self.by_token = {}
        self.folded = [c for c in checks if c["kind"] in ("dns", "tcp") and c["from"] == source
                       and source_family == "windows"]
        for c in self.folded:
            self.by_token[token(c)] = c["id"]
        self._stop = threading.Event()
        self.failed_at: float | None = None             # local time the failure finished (T1)
        self._threads: list[threading.Thread] = []
        self._pre_done = threading.Event()

    # -- lines from any bridge stream (the source's, or another node's)
    def feed(self, line: str, now: float, node: str | None = None) -> None:
        m = _SVC_LINE.match(line.strip())
        if not m:
            return
        cid = self.by_token.get(f"{m.group(1)}={m.group(2)}" if node is None else f"{node}|{m.group(1)}={m.group(2)}")
        if cid is None:
            return
        ms = int(m.group(4))
        self.results[cid].append({"t_local": now - ms / 1000.0, "ok": m.group(3) == "ok", "ms": ms,
                                  "answer": m.group(5) or None})

    def start(self) -> None:
        others: dict[str, list[dict]] = {}
        for c in self.checks:
            if c["kind"] in ("dns", "tcp") and c not in self.folded:
                others.setdefault(c["from"], []).append(c)
        for node, cs in others.items():
            for c in cs:
                self.by_token[f"{node}|{token(c)}"] = c["id"]
            t = threading.Thread(target=self._watch_other, args=(node, cs), daemon=True)
            t.start()
            self._threads.append(t)
        dh = [c for c in self.checks if c["kind"] == "dhcp"]
        if dh:
            t = threading.Thread(target=self._dhcp_all, args=(dh,), daemon=True)
            t.start()
            self._threads.append(t)
        else:
            self._pre_done.set()

    def wait_ready(self, timeout: float = 90) -> bool:
        """Block until the before-failure DHCP renewals are done."""
        return self._pre_done.wait(timeout)

    def failure_done(self, t_local: float) -> None:
        self.failed_at = self.failed_at or t_local

    def stop(self, timeout: float = 60) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)

    # -- DNS/TCP from a Windows node other than the source
    def _watch_other(self, node: str, cs: list[dict]) -> None:
        from winclient import WinBridge, _read_until  # noqa: PLC0415
        import socket                                  # noqa: PLC0415
        first = cs[0].get("host") or cs[0].get("server") or "127.0.0.1"
        try:
            with WinBridge(self.cml, node) as b:
                ch = b.con._ch
                ch.settimeout(0.2)
                ch.send(f"watch {first} 1000 1000 " + " ".join(token(c) for c in cs) + "\r")
                buf = ""
                while not self._stop.is_set():
                    try:
                        chunk = ch.recv(65536).decode("utf-8", errors="replace")
                    except socket.timeout:
                        continue
                    now = time.time()
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for ln in lines:
                        self.feed(ln, now, node)
                ch.send("\r")
                _read_until(ch, "NV> ", 5)
        except Exception as e:                           # noqa: BLE001 - reported as a failed check
            for c in cs:
                self.results[c["id"]].append({"t_local": time.time(), "ok": False, "ms": 0,
                                              "error": f"{type(e).__name__}: {e}"})

    # -- DHCP: once before the failure, once after
    def _dhcp_all(self, dh: list[dict]) -> None:
        try:
            for c in dh:
                self._dhcp_one(c, "before")
        finally:
            self._pre_done.set()
        while self.failed_at is None and not self._stop.is_set():
            time.sleep(0.5)
        if self._stop.is_set():
            return
        time.sleep(max(0.0, self.failed_at + DHCP_AFTER_S - time.time()))
        for c in dh:
            if not self._stop.is_set():
                self._dhcp_one(c, "after")

    def _dhcp_one(self, c: dict, phase: str) -> None:
        t0 = time.time()
        try:
            r = _dhcp_windows(self.cml, c["from"]) if c["family"] == "windows" else _dhcp_ios(self.cml, c["from"], c["iface"])
        except Exception as e:                            # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        r.update(phase=phase, t_local=t0, t_done=time.time())
        r["ms"] = int((r["t_done"] - t0) * 1000)
        self.results[c["id"]].append(r)
        self.log(f"DHCP {phase} from {c['from']}: {'ok' if r.get('ok') else 'FAILED'} "
                 f"{r.get('address') or ''} server {r.get('server') or '?'}")


def _dhcp_ios(cml, node: str, iface: str) -> dict:
    """release + renew on an IOS DHCP-client SVI; proves the release cleared first."""
    from console import Console                       # noqa: PLC0415
    rx = re.compile(rf"^{re.escape(iface)}\s+(\d+\.\d+\.\d+\.\d+)", re.M)
    with Console(cml) as con:
        before = rx.search(con.exec(node, f"show ip interface brief | include ^{iface} ", timeout_ms=20000) or "")
        con.exec(node, f"release dhcp {iface}", timeout_ms=25000)
        t_rel = time.time()
        for _ in range(8):
            if not rx.search(con.exec(node, f"show ip interface brief | include ^{iface} ", timeout_ms=20000) or ""):
                break
            time.sleep(1)
        else:
            return {"ok": False, "error": "release did not clear the address, so a renew would prove nothing"}
        con.exec(node, f"renew dhcp {iface}", timeout_ms=25000)
        t_ren, got = time.time(), None
        while time.time() - t_ren < 45:
            got = rx.search(con.exec(node, f"show ip interface brief | include ^{iface} ", timeout_ms=20000) or "")
            if got:
                break
            time.sleep(2)
        t_bound = time.time()
        lease = con.exec(node, "show dhcp lease", timeout_ms=25000) if got else ""
    srv = re.search(r"DHCP Lease server:\s*(\d+\.\d+\.\d+\.\d+)", lease or "")
    secs = re.search(r"Lease:\s*(\d+)\s*secs", lease or "")
    return {"ok": bool(got), "address": got.group(1) if got else None, "previous": before.group(1) if before else None,
            "server": srv.group(1) if srv else None, "lease_s": int(secs.group(1)) if secs else None,
            "t_released": t_rel, "t_renew": t_ren, "t_bound": t_bound if got else None,
            "error": None if got else "no address within 45 s of the renew"}


def _dhcp_windows(cml, node: str) -> dict:
    from winclient import WinBridge                    # noqa: PLC0415
    with WinBridge(cml, node) as b:
        t_ren = time.time()
        raw = b.run("dhcp", 90)
        t_bound = time.time()
    addrs = re.findall(r"nvip if=\S+ addr=([\d.]+).*?dhcpserver=([\d.]*).*?expires=(.*)$", raw, re.M)
    addrs = [a for a in addrs if not a[0].startswith("169.254.")]
    ok = bool(addrs and addrs[0][1])
    return {"ok": ok, "address": addrs[0][0] if addrs else None, "server": addrs[0][1] if addrs else None,
            "expires": addrs[0][2].strip() if addrs else None, "t_renew": t_ren, "t_bound": t_bound if ok else None,
            "error": None if ok else "no DHCP address after ipconfig /renew"}


# ---------------------------------------------------------------- analysis
def summarise(checks: list[dict], results: dict, to_rel, events: list[dict], endpoints: set[str],
              owners: dict) -> list[dict]:
    """Per check: every attempt on the F timeline, before/during/after counts,
    the outage, and what the packet capture says answered."""
    out = []
    # A DNS reply can come from a VIP another check names (the home router forwards
    # to the DNS anycast address): every VIP holder in the run can be credited.
    all_holders = {h for c in checks for h in (c.get("holders") or [])}
    for c in checks:
        rs = sorted(results.get(c["id"]) or [], key=lambda r: r.get("t_local") or 0)
        att = []
        for r in rs:
            t = to_rel(r.get("t_local"))
            if t is None:
                continue
            a = {"t": t, "ok": bool(r.get("ok")), "ms": r.get("ms")}
            for k in ("answer", "address", "server", "lease_s", "expires", "phase", "error", "previous"):
                if r.get(k) is not None:
                    a[k] = r[k]
            for k in ("t_released", "t_renew", "t_bound", "t_done"):
                if r.get(k) is not None:
                    a[k] = to_rel(r[k])
            att.append(a)
        s = {"before_ok": sum(1 for a in att if a["t"] < 0 and a["ok"]),
             "before": sum(1 for a in att if a["t"] < 0),
             "after_ok": sum(1 for a in att if a["t"] >= 0 and a["ok"]),
             "after": sum(1 for a in att if a["t"] >= 0)}
        fails = [a for a in att if a["t"] >= 0 and not a["ok"]]
        if c["kind"] != "dhcp":
            if fails:
                # The outage runs from the first failure to the first SUSTAINED
                # recovery (3 successes in a row, or every attempt left if fewer).
                # Isolated failures after that are blips, not a longer outage
                # (2026-10-07: one stray SMB timeout at +60 s had stretched an
                # 11 s outage to 61 s).
                after = [a for a in att if a["t"] >= 0]
                i0 = after.index(fails[0])
                rec = None
                for j in range(i0 + 1, len(after)):
                    run = after[j:j + 3]
                    if all(x["ok"] for x in run):
                        rec = after[j]
                        break
                blips = sum(1 for a in after if rec and a["t"] > rec["t"] and not a["ok"])
                s.update(first_fail=fails[0]["t"], last_fail=fails[-1]["t"],
                         recovered_at=rec["t"] if rec else None,
                         down_s=round((rec["t"] if rec else after[-1]["t"]) - fails[0]["t"], 2),
                         blips=blips, verdict="recovered" if rec else "not recovered")
            else:
                s["verdict"] = "no impact" if s["after"] else "no data"
            ans = [a.get("answer") for a in att if a["ok"] and a.get("answer")]
            if ans:
                s["answers"] = sorted(set(ans))
        else:
            ph = {a.get("phase"): a for a in att}
            s.update(before_lease=ph.get("before"), after_lease=ph.get("after"),
                     verdict=("leased after the failure" if (ph.get("after") or {}).get("ok")
                              else "no lease after the failure" if ph.get("after") else "no after-failure renewal"))
        s["evidence"] = _evidence(dict(c, holders=list(set(c.get("holders") or []) | all_holders))
                                  if c["kind"] == "dns" else c, events, endpoints, owners)
        out.append({"id": c["id"], "kind": c["kind"], "from": c["from"], "label": c["label"],
                    "attempts": att, "summary": s})
    return out


def _evidence(c: dict, events: list[dict], endpoints: set[str], owners: dict) -> dict:
    """Who answered, from the packet capture, split at F (events carry rel `t`)."""
    def who(e, ip):
        return owners.get(ip) or ip
    before, after = {}, {}
    if c["kind"] == "tcp":
        for e in events:
            if e.get("kind") == "tcp" and e.get("dir") == "synack" and e.get("src") == c["host"] \
                    and (e.get("tcp_ports") or (0,))[0] == c["port"]:
                holder = e["a"] if e["a"] in endpoints else e["b"] if e["b"] in endpoints else None
                if holder:
                    (before if e["t"] < 0 else after)[holder] = (before if e["t"] < 0 else after).get(holder, 0) + 1
        return {"what": "SYN-ACK seen leaving", "before": before, "after": after}
    if c["kind"] == "dns":
        srv = c.get("server")
        groups: dict = {}
        for e in events:
            if e.get("kind") != "dns" or e.get("dir") != "response" or (e.get("dns_name") or "").lower() != c["name"].lower():
                continue
            if srv and srv not in (e.get("src"), e.get("dst")):
                continue
            groups.setdefault((e.get("src"), e.get("dst"), e.get("seq")), []).append(e)
        for (src_ip, _dst, _id), sightings in groups.items():
            sightings.sort(key=lambda x: x["t"])
            # Who sent it: the end of its FIRST link that is not on its second.
            # An anycast resolver (one address on two DCs) is told apart this way;
            # the address alone would name neither (2026-10-07, DNS anycast 10.60.0.53).
            first = sightings[0]
            # A declared holder of the address (anycast: each DC carrying it) on
            # any link the reply crossed is the answerer - exact, unlike ordering
            # by timestamp, which ties at microsecond resolution (2026-10-07 run:
            # 31 of 53 DR replies were mis-credited that way).
            holders = set(c.get("holders") or []) | set(endpoints)
            n = next((x for s_ in sightings for x in (s_["a"], s_["b"]) if x in holders), None)
            if n is None and len(sightings) > 1:
                nxt = {sightings[1]["a"], sightings[1]["b"]}
                ends = [x for x in (first["a"], first["b"]) if x not in nxt]
                n = ends[0] if len(ends) == 1 else None
            n = n or who(first, src_ip)
            (before if first["t"] < 0 else after)[n] = (before if first["t"] < 0 else after).get(n, 0) + 1
        return {"what": "DNS answers from", "before": before, "after": after}
    return {}
