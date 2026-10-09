"""Disaster traces: baseline -> power nodes off -> measure the outage -> trace
again -> power them back on (spec: docs/disaster-spec.md, 2026-09-29).

Safety rules this module exists to enforce:
  * one disaster at a time (a second is refused, not queued);
  * the nodes to be powered off are written to disk BEFORE the first stop, and
    the record is only removed once every one of them is running again, so a
    NetView crash or a killed exe can never silently leave the lab degraded -
    the next start shows "interrupted" and offers Restore;
  * restore runs in a `finally`: errors, a failed after-trace and Cancel all
    still power the nodes back on (unless the user chose not to restore).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from console import BackgroundPing

MAX_NODES = 12
MAX_LINKS = 8
STATE_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetView" / "disaster.json"

_lock = threading.Lock()
_active: dict | None = None
_cancel: dict[str, threading.Event] = {}
_series_job: str | None = None   # a repeated test, also between its runs


# ---------------------------------------------------------------- state ---
def _write(rec: dict | None) -> None:
    try:
        if rec is None:
            STATE_FILE.unlink(missing_ok=True)
        else:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    except OSError:
        pass


def interrupted() -> dict | None:
    """A record left by a disaster that never finished restoring."""
    if _active is not None:
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status() -> dict:
    with _lock:
        act = dict(_active) if _active else None
    return {"active": act, "interrupted": interrupted()}


def is_active() -> bool:
    return _active is not None or _series_job is not None


def cancel(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    return bool(ev)


# ----------------------------------------------------------- validation ---
def validate(sel: list[str], by_label: dict, source: str, target: str | None,
             links: dict[str, str] | None = None, link_map: dict | None = None) -> tuple[str | None, list[str]]:
    """(error, warnings). An error refuses the request before anything runs.
    `links` is {name: link_id} of cables to cut (the nodes stay running)."""
    links = links or {}
    if not sel and not links:
        return "Nothing chosen for the disaster: pick nodes to power off or links to cut.", []
    if len(links) > MAX_LINKS:
        return f"At most {MAX_LINKS} links per disaster.", []
    for name, lid in links.items():
        l = (link_map or {}).get(lid)
        if not l:
            return f"Unknown link: {name}.", []
        for end in (l["a"], l["b"]):
            if not (by_label.get(end) or {}).get("up"):
                return f"{name}: {end} is not running, so the link carries nothing to cut.", []
            if end in sel:
                return f"{name}: {end} is also being powered off; cutting its cable as well measures nothing extra.", []
    if len(set(sel)) != len(sel):
        return "A node is listed twice.", []
    if len(sel) > MAX_NODES:
        return f"At most {MAX_NODES} nodes per disaster.", []
    if source in sel:
        return f"{source} is the source - its console sends the test traffic, so it cannot be powered off.", []
    for x in sel:
        n = by_label.get(x)
        if not n:
            return f"Unknown node: {x}.", []
        if not n.get("up"):
            return f"{x} is not running, so it cannot be powered off.", []
    if _active is not None:
        return "Another disaster test is already running. Wait for it to finish and restore.", []
    if interrupted():
        return "An earlier disaster test left nodes powered off. Restore them first.", []
    warns = []
    if target in sel:
        warns.append(f"The target {target} is in the disaster: the second trace shows whether anything else answers.")
    win = [x for x in sel if by_label[x].get("node_definition") == "win2025"]
    if win:
        warns.append(f"{', '.join(win)} {'is a Windows server' if len(win) == 1 else 'are Windows servers'}: "
                     "about 6 minutes to boot again after the restore.")
    if len(sel) > 8:
        warns.append(f"{len(sel)} nodes is a large disaster; the restore takes longer.")
    return None, warns


# ------------------------------------------------------------- power ---
def _state(cml, nid: str) -> str | None:
    for n in cml.nodes_full().values():
        if n["id"] == nid:
            return n.get("state")
    return None


def _power(cml, ids: dict[str, str], action: str, want: set[str], deadline_s: int) -> dict[str, str]:
    """Stop or start each node, then poll until it reaches a wanted state.
    CML's API has been measured taking 5-30 s per call (and timing out while
    still acting), so a failed call is re-checked by state, not trusted."""
    res: dict[str, str] = {}
    for label, nid in ids.items():
        try:
            (cml.stop_node if action == "stop" else cml.start_node)(nid)
        except Exception as e:                    # noqa: BLE001 - verified by state below
            res[label] = f"call failed ({type(e).__name__}); checking state"
    t0 = time.time()
    pending = set(ids)
    retried: dict[str, float] = {}
    while pending and time.time() - t0 < deadline_s:
        for label in list(pending):
            try:
                st = _state(cml, ids[label])
            except Exception:                     # noqa: BLE001
                continue
            if st in want:
                res[label] = st
                pending.discard(label)
            elif action == "start" and st == "STOPPED" and time.time() - retried.get(label, t0) > 30:
                # Live 2026-10-07: CoreSwitch-HQ-1's start was accepted but it sat
                # STOPPED for the whole 600 s (CML over-allocated, #45); one more
                # start booted it in 10 s. Ask again every 30 s while it is STOPPED.
                retried[label] = time.time()
                try:
                    cml.start_node(ids[label])
                except Exception:                 # noqa: BLE001
                    pass
        if pending:
            time.sleep(5)
    for label in pending:
        try:
            res[label] = f"still {_state(cml, ids[label])} after {deadline_s} s"
        except Exception:                         # noqa: BLE001
            res[label] = f"unknown after {deadline_s} s"
    return res


def _carrier(cml, ends: list[dict], up: bool) -> dict[str, str]:
    """`shutdown` / `no shutdown` on IOS link ends: one console session per NODE
    (nodes in parallel, at most 6), each end read back and retried once.

    CML's link stop does NOT drop the link on IOL: measured 2026-10-07, all four
    HQ core <-> server-room links STOPPED and the cores still showed the ports
    up/up trunking, so the SVI stayed up and nothing reacted to the "cut". A real
    cut cable drops carrier at both ends; shutting both ends is how that is
    emulated. Not saved - a reload brings the port back up.

    2026-10-08: this used to open one session per END. A switch with two cut
    cables then had two sessions attached at once (the skill's one rule for the
    console server), and one `no shutdown` was lost: CoreSwitch-HQ-1 Et0/1 and
    ServerRoomSwitch-HQ-1 Et0/2 stayed shut after a restore, cutting the HQ
    server room off for the next run. Every end is now confirmed."""
    if not ends:
        return {}
    from concurrent.futures import ThreadPoolExecutor   # noqa: PLC0415
    from console import Console                         # noqa: PLC0415
    by_node: dict[str, list[str]] = {}
    for e in ends:
        by_node.setdefault(e["node"], []).append(e["iface"])

    def node_job(item):
        node, ifaces = item
        out: dict[str, str] = {}
        try:
            with Console(cml) as con:
                want = "up" if up else "shut"
                for _attempt in range(2):
                    todo = [i for i in ifaces if out.get(f"{node} {i}") != want]
                    if not todo:
                        break
                    con.configure_batch(node, [[f"interface {i}", "no shutdown" if up else "shutdown"] for i in todo])
                    for i in todo:                    # read back: was it applied?
                        cfg = con.exec(node, f"show running-config interface {i} | include ^ shutdown")
                        is_shut = any(l.strip() == "shutdown" for l in cfg.splitlines())
                        out[f"{node} {i}"] = want if is_shut != up else f"still {'shut' if is_shut else 'up'}"
        except Exception as ex:                       # noqa: BLE001 - reported to the caller
            for i in ifaces:
                out.setdefault(f"{node} {i}", f"failed: {type(ex).__name__}")
        return out
    res: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(by_node))) as ex:
        for r in ex.map(node_job, by_node.items()):
            res.update(r)
    return res


def _cut(cml, links: dict[str, str], action: str, deadline_s: int = 60,
         carrier: list[dict] | None = None) -> dict[str, str]:
    """Stop (unplug) or start (re-plug) each link, then poll until it reports
    the wanted state. Same shape as _power: a failed call is re-checked by state.
    `carrier`: IOS link ends to shut after the stop / no-shut before the start."""
    want = "STOPPED" if action == "stop" else "STARTED"
    res: dict[str, str] = {}
    car = _carrier(cml, carrier, up=True) if action == "start" and carrier else {}
    for name, lid in links.items():
        try:
            (cml.stop_link if action == "stop" else cml.start_link)(lid)
        except Exception as e:                    # noqa: BLE001 - verified by state below
            res[name] = f"call failed ({type(e).__name__}); checking state"
    t0 = time.time()
    pending = set(links)
    while pending and time.time() - t0 < deadline_s:
        for name in list(pending):
            try:
                if cml.link_state(links[name]) == want:
                    res[name] = want
                    pending.discard(name)
            except Exception:                     # noqa: BLE001
                continue
        if pending:
            time.sleep(1)
    for name in pending:
        res[name] = f"not {want} after {deadline_s} s"
    if action == "stop" and carrier:
        car = _carrier(cml, carrier, up=False)
    # A link end whose carrier did not change is reported against its link, so
    # a restore that left a port shut is never read as "back in service".
    for e in carrier or []:
        k = f"{e['node']} {e['iface']}"
        v = car.get(k)
        name = e.get("link")
        if v not in (None, "up", "shut") and name in res and res[name] == want:
            res[name] = f"{want}, but {k}: {v}"
    return res


def restore(cml, labels: list[str], deadline_s: int = 600, links: dict[str, str] | None = None,
            carrier: list[dict] | None = None) -> dict[str, str]:
    """Start every node that is not running and re-plug every cut link; the
    record is cleared only if all of them come back."""
    links = links or {}
    ids = {n["label"]: n["id"] for n in cml.nodes_full().values() if n["label"] in labels}
    down = {k: v for k, v in ids.items() if _state(cml, v) != "BOOTED"}
    res = {k: "BOOTED" for k in ids if k not in down}
    if links:                                     # cables first: they are instant, the nodes are not
        res.update(_cut(cml, links, "start", carrier=carrier))
    if down:
        res.update(_power(cml, down, "start", {"BOOTED"}, deadline_s))
    if all(v in ("BOOTED", "STARTED") for v in res.values()) and set(res) == set(labels) | set(links):
        _write(None)
    return res


def describe(sel: list[str], links: dict[str, str]) -> str:
    """'powering off A, B; cutting X - Y' for stage lines."""
    parts = []
    if sel:
        parts.append(f"powering off {len(sel)} node{'s' if len(sel) > 1 else ''}: {', '.join(sel)}")
    if links:
        parts.append(f"cutting {len(links)} link{'s' if len(links) > 1 else ''}: {', '.join(links)}")
    return "; ".join(parts)


# ------------------------------------------------------------ analysis ---
def outage(marks: str) -> dict:
    """Per-second marks ('!' reply, '.' lost, '?' unmeasured) -> figures."""
    longest = cur = 0
    first_loss = None
    for i, m in enumerate(marks):
        if m == ".":
            cur += 1
            longest = max(longest, cur)
            if first_loss is None:
                first_loss = i
        elif m == "!":
            cur = 0
    recovered = None
    if first_loss is not None:
        last_loss = marks.rfind(".")
        tail = marks[last_loss + 1:]
        if tail.count("!") >= 1 and "." not in tail:
            recovered = last_loss + 1 - first_loss
    return {"marks": marks, "lost": marks.count("."), "answered": marks.count("!"),
            "longest_outage_s": longest, "recovered_after_s": recovered,
            "still_down": first_loss is not None and recovered is None}


def _complete(res: dict) -> list[str]:
    """A trace's path only if it actually reaches the target; a partial path
    (e.g. a Quick trace whose counters could not calibrate) is not compared -
    live UI test 2026-09-30 reported every hop past the gap as 'no longer used'."""
    p = res.get("path") or []
    return p if p and p[-1] == res.get("target") else []


def path_diff(before: list[str], after: list[str]) -> dict:
    """What changed between the two paths - or, when either trace could not show
    a path at all, say so instead of reporting every hop as 'removed' (live
    test 2026-09-30: a Quick after-trace that failed to calibrate did exactly that)."""
    if not before or not after:
        return {"comparable": False, "removed": [], "added": [], "same": False,
                "why": "the " + ("baseline" if not before else "after-disaster") + " trace could not show a complete path"}
    return {"comparable": True, "removed": [x for x in before if x not in after],
            "added": [x for x in after if x not in before], "same": before == after}


# ----------------------------------------------------------------- run ---
MAX_REPEAT = 5
BETWEEN_RUNS_S = 45     # after a restore: routing fails back, STP / loop guard clear


def run(job_id: str, cml, con, spec: dict, execute, set_, respec) -> dict:
    """The disaster job. `execute` runs one trace (pathtrace._execute), `set_`
    updates the job's stage, `respec()` rebuilds the trace spec from a fresh
    inventory (node states, holders and DNS vantage as the network is NOW).

    Repeat (2026-10-08, failover mode): the whole test - baseline, failure,
    recording, restore - runs `repeat` times with a pause between, and the
    result carries every run plus their average (failover.series_summary).
    One run can be luck; three show whether the number holds."""
    global _series_job
    d = spec["disaster"]
    n = max(1, min(int(d.get("repeat") or 1), MAX_REPEAT)) if d.get("mode") == "failover" else 1
    stop = threading.Event()
    _cancel[job_id] = stop
    _keep_awake(True)
    try:
        if n == 1:
            return _run_once(job_id, cml, con, spec, execute, set_, respec, stop, "")
        _series_job = job_id
        results: list[dict] = []
        for i in range(n):
            # The same spec each time: every run starts from the restored network,
            # which is the network the spec was built from. Each repeat gets its
            # own console session: the job's one can die during a long series
            # (2026-10-08: "Socket is closed" after the laptop slept) and would
            # then fail every later run.
            try:
                if i == 0:
                    r = _run_once(job_id, cml, con, spec, execute, set_, respec, stop, f"run {i + 1} of {n}: ")
                else:
                    from console import Console                 # noqa: PLC0415
                    with Console(cml) as con_i:
                        r = _run_once(job_id, cml, con_i, spec, execute, set_, respec, stop, f"run {i + 1} of {n}: ")
            except Exception as e:                 # noqa: BLE001 - keep the runs already finished
                if not results:
                    raise
                results[-1].setdefault("disaster", {}).setdefault("warnings", []).append(
                    f"Run {i + 1} failed before its disaster: {type(e).__name__}: {e}")
                break
            dd = r.get("disaster") or {}
            if not dd.get("failover"):             # never reached its disaster: not a run
                if not results:
                    return r
                results[-1]["disaster"].setdefault("warnings", []).append(
                    f"Run {i + 1} did not start its disaster: {dd.get('error') or 'no recording'}")
                break
            results.append(r)
            bad = [k for k, v in (dd.get("restored") or {}).items() if v not in ("BOOTED", "STARTED")]
            if stop.is_set() or dd.get("error") or bad:
                break                              # never stack a disaster on an unrestored one
            if i < n - 1:
                for left in range(BETWEEN_RUNS_S, 0, -5):
                    set_(job_id, stage=f"run {i + 1} of {n} done; letting the network settle ({left} s)")
                    if stop.wait(5):
                        break
                if stop.is_set():
                    break
        import failover                            # noqa: PLC0415
        return failover.combine_series(results, n)
    finally:
        _series_job = None
        _cancel.pop(job_id, None)
        _keep_awake(False)


def _keep_awake(on: bool) -> None:
    """Ask Windows not to idle-sleep while a disaster test is active.

    2026-10-08: the laptop slept 8.5 h in the middle of a run, so the HQ server
    room stayed cut for 8.5 h and the run's console died. A cut or a power-off
    must never outlive the test because the PC running it went to sleep. Covers
    idle sleep only (not a closed lid). No-op off Windows."""
    try:
        import ctypes                                   # noqa: PLC0415
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:                                   # noqa: BLE001 - best effort
        pass


def _run_once(job_id: str, cml, con, spec: dict, execute, set_, respec, stop, prefix: str) -> dict:
    """One complete disaster test (see run)."""
    global _active
    d = spec["disaster"]
    sel, settle, do_restore = d["nodes"], int(d.get("settle_s", 30)), bool(d.get("restore", True))
    links = dict(d.get("links") or {})
    carrier = list(d.get("carrier") or [])
    with _lock:
        if _active is not None:
            raise RuntimeError("another disaster test is already running")
        _active = {"job_id": job_id, "nodes": sel, "links": links, "stage": "baseline trace",
                   "since": datetime.now(timezone.utc).isoformat(), "restore": do_restore}

    def stage(txt: str) -> None:
        set_(job_id, stage=prefix + txt)
        with _lock:
            if _active:
                _active["stage"] = prefix + txt

    out: dict = {"nodes": sel, "links": links, "settle_s": settle, "restore": do_restore, "warnings": d.get("warnings", [])}
    failover_mode = d.get("mode") == "failover"
    if failover_mode:
        out.update(mode="failover", window_s=d.get("window_s"), stop_early=d.get("stop_early", True),
                   device_logs=bool(d.get("device_logs")), after_trace=bool(d.get("after_trace")))
    powered_off = False
    baseline = None
    result = None
    try:
        try:
            stage("baseline trace (before the disaster)")
            baseline = execute(job_id, cml, con, spec)
            if stop.is_set():
                raise RuntimeError("cancelled before any node was powered off")
            if failover_mode:
                import failover                    # noqa: PLC0415 - failover capture (spec: failover-capture-spec.md)
                failover.run(job_id, cml, con, spec, baseline, d, stop, stage, set_, out,
                             write_record=lambda: _write({"job_id": job_id, "nodes": sel, "links": links,
                                                          "carrier": carrier,
                                                          "restore": do_restore,
                                                          "started": datetime.now(timezone.utc).isoformat()}))
                powered_off = bool(out.get("power_off"))
            else:
                powered_off = _disaster(job_id, cml, spec, baseline, sel, links, settle, do_restore, stop, stage, out)
            if stop.is_set():
                raise RuntimeError("cancelled after the power-off")
            # Failover mode reads the after-path from its own capture; the full
            # second trace is optional there ("also run a full trace afterwards").
            if not failover_mode or d.get("after_trace"):
                stage("trace after the disaster")
                try:
                    after = execute(job_id, cml, con, respec())
                    out["after"] = after
                    out["diff"] = path_diff(_complete(baseline), _complete(after))
                    out["verdict_change"] = [(baseline.get("verdict") or {}).get("level"),
                                             (after.get("verdict") or {}).get("level")]
                except Exception as e:            # noqa: BLE001 - reported; restore still runs
                    out["after_error"] = f"{type(e).__name__}: {e}"
        except Exception as e:
            if baseline is None:
                raise                             # failed before anything was powered off
            out["error"] = f"{type(e).__name__}: {e}"
            powered_off = powered_off or bool(out.get("power_off"))
        result = dict(baseline)
        result["disaster"] = out
        return result
    finally:
        try:
            n_back = len(sel) + len(links)
            if out.get("power_off") and do_restore:
                stage(f"restoring {n_back} item{'s' if n_back > 1 else ''}" +
                      (" and waiting for the nodes to boot" if sel else ""))
                out["restored"] = restore(cml, sel, links=links, carrier=carrier)
            elif out.get("power_off"):
                out["restored"] = {x: "left powered off (restore was not selected)" for x in sel}
                out["restored"].update({x: "left cut (restore was not selected)" for x in links})
            if failover_mode and result is not None and out.get("failover"):
                import failover                    # noqa: PLC0415
                failover.save(result)              # history + pcaps; after restore so it records the restore
        finally:
            out.pop("_pcaps", None)
            with _lock:
                _active = None


def _disaster(job_id, cml, spec, baseline, sel, links, settle, do_restore, stop, stage, out) -> bool:
    """Power the nodes off / cut the links while a paced probe watches source -> target."""
    ids = {n["label"]: n["id"] for n in cml.nodes_full().values() if n["label"] in sel}
    src = spec["source"]
    # BusyBox resolves a DNS-failover NAME on every ping, the way a client would;
    # an IOS source pings the address the baseline resolved.
    dest = (spec.get("virtual_fqdn") if src.get("probe_family") == "linux" and spec.get("virtual_fqdn")
            else baseline.get("target_ip") or spec.get("target_ip"))
    seen: list[str] = []
    off_at = {"i": None}
    probe_stop = threading.Event()

    def tick(i, mark, raw):
        seen.append(mark)
        # after the power-off: 5 answers in a row = recovered (or never broke)
        if off_at["i"] is not None and len(seen) - off_at["i"] >= 5 and "".join(seen[-5:]) == "!!!!!":
            probe_stop.set()

    bp = BackgroundPing(cml, node=src["label"], dest=dest, duration_s=settle + 240,
                        on_tick=tick, stop_event=probe_stop)
    bp.start()
    # Pre-failure marks: wait until the probe has actually answered a few times
    # (opening its console can take many seconds), up to 60 s - live test
    # 2026-09-30 got its first mark only as the power-off finished.
    t_wait = time.time()
    while len(seen) < 3 and time.time() - t_wait < 60 and not stop.is_set():
        time.sleep(0.5)
    _write({"job_id": job_id, "nodes": sel, "links": links, "carrier": list(spec["disaster"].get("carrier") or []),
            "started": datetime.now(timezone.utc).isoformat(),
            "restore": do_restore})
    stage(describe(sel, links))
    t_off = time.time()
    pre = len(seen)
    out["power_off"] = {x: "requested" for x in [*sel, *links]}   # restore is owed from this point on
    res = _cut(cml, links, "stop", carrier=spec["disaster"].get("carrier")) if links else {}
    if ids:
        res.update(_power(cml, ids, "stop", {"STOPPED", "DEFINED_ON_CORE"}, 180))
    out["power_off"] = res
    out["power_off_seconds"] = round(time.time() - t_off)
    off_at["i"] = len(seen)
    stage(f"measuring the outage (up to {settle} s after the power-off)")
    deadline = time.time() + settle
    while time.time() < deadline and not probe_stop.is_set() and not stop.is_set():
        time.sleep(1)
    probe_stop.set()
    bp.join()
    out["outage"] = outage("".join(seen))
    out["outage"].update({"pre_failure_marks": pre, "power_off_done_at_mark": off_at["i"], "target": dest})
    return True
