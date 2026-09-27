"""Run NetView's own Verified-mode traces for the paths that mattered, via its API.

    py -3.11 netview/trace_suite.py [--no-harvest] [--quick]

Re-harvests first (the harvest is in-memory, so after a restart or an address
change targets fall back to CML's stored config). Each trace is submitted with
capture=True (Verified) unless --quick, polled to completion, and summarised.
Exit 1 if any trace does not reach its target. Written 2026-09-27.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5057"
TRACES = [
    ("Firewall-DR-2", "Firewall-HQ-1"),
    ("Firewall-HQ-1", "Firewall-DR-2"),
    ("Firewall-HQ-2", "EdgeRouter-HQ-1"),
    ("WFH user (VPN client)", "ServerRoomSwitch-HQ-2"),
    ("CoreSwitch-DR-1", "ServerRoomSwitch-HQ-2"),
    ("ServerRoomSwitch-HQ-2", "CoreSwitch-DR-1"),
    ("AccessSwitch-HQ-2", "WebsiteServer-DR-1"),
    ("Finance PC", "fileserver.p45.nz"),
    ("Tech/support dept PC", "(company)website.p45.nz"),
    ("WFH user (VPN client)", "fileserver.p45.nz"),
    ("CoreSwitch-HQ-1", "DMZSwitch-DR-1"),
]


def call(path: str, body: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") | {"http": e.code}


def main() -> int:
    if "--no-harvest" not in sys.argv:
        print("harvest:", call("/api/harvest/run", {}).get("error") or "started", flush=True)
        for _ in range(120):
            time.sleep(5)
            st = call("/api/harvest")
            if st.get("ended") and not st.get("running"):
                break
        errs = {k: v["error"] for k, v in (st.get("nodes") or {}).items() if v.get("error")}
        print(f"harvest done: {st.get('done_count')} nodes, errors: {errs or 'none'}", flush=True)
    capture = "--quick" not in sys.argv
    failed = 0
    for src, dst in TRACES:
        r = call("/api/trace", {"source": src, "target": dst, "capture": capture, "count": 60})
        if "job_id" not in r:
            print(f"ERROR {src} -> {dst}: {r}")
            failed += 1
            continue
        job = {}
        for _ in range(200):
            time.sleep(3)
            job = call(f"/api/trace/{r['job_id']}")
            if job.get("status") in ("done", "error", "failed"):
                break
        res = job.get("result") or {}
        v = res.get("verdict") or {}
        verdict = (f"[{v.get('level')}] {v.get('text')}" if v else (job.get("error") or job.get("status")))
        path = " > ".join(p if isinstance(p, str) else str(p.get("label", p)) for p in (res.get("path") or []))
        reached = bool(res.get("received")) and v.get("level") in ("ok", "warn")
        failed += 0 if reached else 1
        print(f"\n{'OK ' if reached else 'BAD'} {src} -> {dst}  [{r.get('target_ip')}]"
              f"  {res.get('received')}/{res.get('sent')} replies\n"
              f"    {verdict}\n    path: {path}", flush=True)
        for f in (res.get("findings") or [])[:4]:
            print(f"    finding: {(f.get('text') if isinstance(f, dict) else str(f))[:220]}")
    print(f"\n{len(TRACES) - failed}/{len(TRACES)} traces reached their target")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
