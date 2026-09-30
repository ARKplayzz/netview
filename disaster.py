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
STATE_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetView" / "disaster.json"

_lock = threading.Lock()
_active: dict | None = None
_cancel: dict[str, threading.Event] = {}


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
    return _active is not None


def cancel(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    return bool(ev)


# ----------------------------------------------------------- validation ---
def validate(sel: list[str], by_label: dict, source: str, target: str | None) -> tuple[str | None, list[str]]:
    """(error, warnings). An error refuses the request before anything runs."""
    if not sel:
        return "No nodes chosen for the disaster.", []
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
    while pending and time.time() - t0 < deadline_s:
        for label in list(pending):
            try:
                st = _state(cml, ids[label])
            except Exception:                     # noqa: BLE001
                continue
            if st in want:
                res[label] = st
                pending.discard(label)
        if pending:
            time.sleep(5)
    for label in pending:
        try:
            res[label] = f"still {_state(cml, ids[label])} after {deadline_s} s"
        except Exception:                         # noqa: BLE001
            res[label] = f"unknown after {deadline_s} s"
    return res


def restore(cml, labels: list[str], deadline_s: int = 600) -> dict[str, str]:
    """Start every node that is not running; the record is cleared only if all boot."""
    ids = {n["label"]: n["id"] for n in cml.nodes_full().values() if n["label"] in labels}
    down = {k: v for k, v in ids.items() if _state(cml, v) != "BOOTED"}
    res = {k: "BOOTED" for k in ids if k not in down}
    if down:
        res.update(_power(cml, down, "start", {"BOOTED"}, deadline_s))
    if all(v == "BOOTED" for v in res.values()) and set(res) == set(labels):
        _write(None)
    return res


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
def run(job_id: str, cml, con, spec: dict, execute, set_, respec) -> dict:
    """The disaster job. `execute` runs one trace (pathtrace._execute), `set_`
    updates the job's stage, `respec()` rebuilds the trace spec from a fresh
    inventory (node states, holders and DNS vantage as the network is NOW)."""
    global _active
    d = spec["disaster"]
    sel, settle, do_restore = d["nodes"], int(d.get("settle_s", 30)), bool(d.get("restore", True))
    stop = threading.Event()
    with _lock:
        if _active is not None:
            raise RuntimeError("another disaster test is already running")
        _active = {"job_id": job_id, "nodes": sel, "stage": "baseline trace",
                   "since": datetime.now(timezone.utc).isoformat(), "restore": do_restore}
    _cancel[job_id] = stop

    def stage(txt: str) -> None:
        set_(job_id, stage=txt)
        with _lock:
            if _active:
                _active["stage"] = txt

    out: dict = {"nodes": sel, "settle_s": settle, "restore": do_restore, "warnings": d.get("warnings", [])}
    powered_off = False
    baseline = None
    try:
        try:
            stage("baseline trace (before the disaster)")
            baseline = execute(job_id, cml, con, spec)
            if stop.is_set():
                raise RuntimeError("cancelled before any node was powered off")
            powered_off = _disaster(job_id, cml, spec, baseline, sel, settle, do_restore, stop, stage, out)
            if stop.is_set():
                raise RuntimeError("cancelled after the power-off")
            stage("trace after the disaster")
            try:
                after = execute(job_id, cml, con, respec())
                out["after"] = after
                out["diff"] = path_diff(_complete(baseline), _complete(after))
                out["verdict_change"] = [(baseline.get("verdict") or {}).get("level"),
                                         (after.get("verdict") or {}).get("level")]
            except Exception as e:                # noqa: BLE001 - reported; restore still runs
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
            if out.get("power_off") and do_restore:
                stage(f"restoring {len(sel)} node{'s' if len(sel) > 1 else ''} and waiting for them to boot")
                out["restored"] = restore(cml, sel)
            elif out.get("power_off"):
                out["restored"] = {x: "left powered off (restore was not selected)" for x in sel}
        finally:
            with _lock:
                _active = None
            _cancel.pop(job_id, None)


def _disaster(job_id, cml, spec, baseline, sel, settle, do_restore, stop, stage, out) -> bool:
    """Power the nodes off while a paced probe watches source -> target."""
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
    _write({"job_id": job_id, "nodes": sel, "started": datetime.now(timezone.utc).isoformat(),
            "restore": do_restore})
    stage(f"powering off {len(sel)} node{'s' if len(sel) > 1 else ''}: {', '.join(sel)}")
    t_off = time.time()
    pre = len(seen)
    out["power_off"] = {x: "requested" for x in sel}   # restore is owed from this point on
    out["power_off"] = _power(cml, ids, "stop", {"STOPPED", "DEFINED_ON_CORE"}, 180)
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
