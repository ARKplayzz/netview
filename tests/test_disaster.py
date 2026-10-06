"""Offline tests of the disaster-trace logic (mocked CML, console and traces).
Run: py -3.11 netview/tests/test_disaster.py"""
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / ".claude" / "skills" / "cml" / "scripts"))
sys.path.insert(0, str(HERE.parent))
import disaster as dz  # noqa: E402

dz.STATE_FILE = Path(tempfile.mkdtemp()) / "disaster.json"


class FakeCML:
    def __init__(self, labels):
        self.nodes = {f"id-{l}": {"id": f"id-{l}", "label": l, "state": "BOOTED"} for l in labels}
        self.calls = []

    def nodes_full(self):
        return self.nodes

    def stop_node(self, nid):
        self.calls.append(("stop", nid))
        assert dz.STATE_FILE.exists(), "record must be written BEFORE the first power-off"
        self.nodes[nid]["state"] = "STOPPED"

    def start_node(self, nid):
        self.calls.append(("start", nid))
        self.nodes[nid]["state"] = "BOOTED"

    links: dict = {}

    def stop_link(self, lid):
        self.calls.append(("cut", lid))
        assert dz.STATE_FILE.exists(), "record must be written BEFORE the first cut"
        self.links[lid] = "STOPPED"

    def start_link(self, lid):
        self.calls.append(("plug", lid))
        self.links[lid] = "STARTED"

    def link_state(self, lid):
        return self.links.get(lid, "STARTED")


class FakePing:
    """Replays a mark pattern: marks before the power-off, then after it."""
    pattern = "!!!!!" + "...!!!!!!!!"

    def __init__(self, cml, node, dest, duration_s, on_tick, stop_event):
        self.on_tick, self.stop = on_tick, stop_event

    def start(self):
        def go():
            import time
            for i, m in enumerate(self.pattern):
                if self.stop.is_set():
                    return
                self.on_tick(i, m, "")
                time.sleep(0.01 if i < 5 else 0.02)
        self.t = threading.Thread(target=go)
        self.t.start()

    def join(self):
        self.t.join()
        return {}


dz.BackgroundPing = FakePing
import time as _t, types  # noqa: E401,E402
dz.time = types.SimpleNamespace(time=_t.time, sleep=lambda s: _t.sleep(min(s, 0.02)))


def trace(path, level="ok"):
    return {"path": path, "verdict": {"level": level, "text": level}, "target_ip": "10.0.0.9", "findings": [],
            "target": path[-1] if path else "T"}


def spec(nodes, **d):
    return {"source": {"label": "PC", "probe_family": "linux"}, "target_ip": "10.0.0.9",
            "disaster": {"nodes": nodes, "settle_s": 20, "restore": True, **d}}


BY = {"PC": {"up": True}, "A": {"up": True}, "B": {"up": True}, "T": {"up": True},
      "DOWN": {"up": False}, "DC": {"up": True, "node_definition": "win2025"}}

# ---- validation
assert dz.validate(["PC"], BY, "PC", "T")[0].startswith("PC is the source")
assert "not running" in dz.validate(["DOWN"], BY, "PC", "T")[0]
assert dz.validate([], BY, "PC", "T")[0]
assert "twice" in dz.validate(["A", "A"], BY, "PC", "T")[0]
assert "At most" in dz.validate([f"n{i}" for i in range(13)], BY, "PC", "T")[0]
err, warns = dz.validate(["T", "DC"], BY, "PC", "T")
assert err is None and any("target" in w for w in warns) and any("Windows" in w for w in warns), warns
print("validation OK")

# ---- outage figures
o = dz.outage("!!!!!...!!!!!")
assert o["longest_outage_s"] == 3 and o["recovered_after_s"] == 3 and not o["still_down"], o
o = dz.outage("!!!!!.....")
assert o["still_down"] and o["recovered_after_s"] is None, o
o = dz.outage("!!!!!!!!")
assert o["longest_outage_s"] == 0 and not o["still_down"], o
print("outage OK")

# ---- a full run: baseline, power-off, outage, after-trace, restore, record cleared
cml = FakeCML(["A", "B", "PC", "T"])
calls = []
execute = lambda j, c, con, s: calls.append(s.get("_in_disaster")) or (trace(["PC", "A", "T"]) if not s.get("_in_disaster")
                                                                       else trace(["PC", "B", "T"]))
res = dz.run("j1", cml, None, spec(["A"]), execute, lambda *a, **k: None,
             lambda: {"_in_disaster": True, "source": {"label": "PC"}})
d = res["disaster"]
assert res["path"] == ["PC", "A", "T"] and d["after"]["path"] == ["PC", "B", "T"], res
assert d["diff"] == {"comparable": True, "removed": ["A"], "added": ["B"], "same": False}, d["diff"]
nd = dz.path_diff(["PC", "A", "T"], [])
assert nd["comparable"] is False and not nd["removed"] and "after-disaster" in nd["why"], nd
assert d["power_off"] == {"A": "STOPPED"} and d["restored"] == {"A": "BOOTED"}, d
assert d["outage"]["longest_outage_s"] == 3, d["outage"]
assert not dz.STATE_FILE.exists(), "record must be cleared after a successful restore"
assert not dz.is_active()
partial = dict(trace(["PC", "A"]), target="T")          # stops short of the target
assert dz._complete(partial) == [] and dz._complete(trace(["PC", "A", "T"])) == ["PC", "A", "T"]
assert dz.path_diff(dz._complete(trace(["PC", "A", "T"])), dz._complete(partial))["comparable"] is False
print("full run OK")

# ---- after-trace fails: restore still runs
cml = FakeCML(["A", "PC", "T"])
def boom(j, c, con, s):
    if s.get("_in_disaster"):
        raise RuntimeError("console died")
    return trace(["PC", "A", "T"])
res = dz.run("j2", cml, None, spec(["A"]), boom, lambda *a, **k: None, lambda: {"_in_disaster": True})
assert "console died" in res["disaster"]["after_error"] and res["disaster"]["restored"] == {"A": "BOOTED"}
assert cml.nodes["id-A"]["state"] == "BOOTED" and not dz.STATE_FILE.exists()
print("restore-after-failure OK")

# ---- baseline fails: nothing is powered off, error propagates
cml = FakeCML(["A", "PC", "T"])
try:
    dz.run("j3", cml, None, spec(["A"]), lambda *a: (_ for _ in ()).throw(RuntimeError("no console")),
           lambda *a, **k: None, lambda: {})
    raise AssertionError("should have raised")
except RuntimeError as e:
    assert "no console" in str(e)
assert not cml.calls and not dz.is_active() and not dz.STATE_FILE.exists()
print("baseline-failure OK")

# ---- restore=False: nodes stay off, record stays, a new disaster is refused until restored
cml = FakeCML(["A", "PC", "T"])
res = dz.run("j4", cml, None, spec(["A"], restore=False), execute, lambda *a, **k: None,
             lambda: {"_in_disaster": True})
assert cml.nodes["id-A"]["state"] == "STOPPED" and dz.STATE_FILE.exists()
assert "Restore them first" in dz.validate(["B"], BY, "PC", "T")[0]
assert dz.status()["interrupted"]["nodes"] == ["A"]
assert dz.restore(cml, ["A"]) == {"A": "BOOTED"} and not dz.STATE_FILE.exists()
print("no-restore + interrupted record OK")

# ---- one disaster at a time
dz._active = {"job_id": "other", "nodes": ["B"]}
assert "already running" in dz.validate(["A"], BY, "PC", "T")[0]
try:
    dz.run("j5", FakeCML(["A"]), None, spec(["A"]), execute, lambda *a, **k: None, lambda: {})
    raise AssertionError("should refuse")
except RuntimeError as e:
    assert "already running" in str(e)
dz._active = None
print("single-disaster lock OK")

# ---- cable cuts: validate, cut, record, restore re-plugs
LM = {"L1": {"a": "A", "b": "B"}, "L2": {"a": "A", "b": "DOWN"}}
assert dz.validate([], BY, "PC", "T", {"A - B": "L1"}, LM)[0] is None
assert "Nothing chosen" in dz.validate([], BY, "PC", "T", {}, LM)[0]
assert "Unknown link" in dz.validate([], BY, "PC", "T", {"x": "L9"}, LM)[0]
assert "not running" in dz.validate([], BY, "PC", "T", {"A - DOWN": "L2"}, LM)[0]
assert "also being powered off" in dz.validate(["A"], BY, "PC", "T", {"A - B": "L1"}, LM)[0]
cml = FakeCML(["A", "B", "PC", "T"])
cml.links = {}
res = dz.run("j6", cml, None, spec([], links={"A - B": "L1"}), execute, lambda *a, **k: None,
             lambda: {"_in_disaster": True, "source": {"label": "PC"}})
d = res["disaster"]
assert d["power_off"] == {"A - B": "STOPPED"} and d["restored"] == {"A - B": "STARTED"}, d
assert cml.calls == [("cut", "L1"), ("plug", "L1")], cml.calls
assert all(n["state"] == "BOOTED" for n in cml.nodes.values()) and not dz.STATE_FILE.exists()
# left cut: the record keeps the link and restore() re-plugs it
cml = FakeCML(["A", "B", "PC", "T"])
cml.links = {}
dz.run("j7", cml, None, spec([], links={"A - B": "L1"}, restore=False), execute, lambda *a, **k: None,
       lambda: {"_in_disaster": True})
rec = dz.status()["interrupted"]
assert rec["links"] == {"A - B": "L1"} and cml.links["L1"] == "STOPPED", rec
assert dz.restore(cml, rec["nodes"], links=rec["links"]) == {"A - B": "STARTED"} and not dz.STATE_FILE.exists()
assert dz.describe(["A"], {"A - B": "L1"}) == "powering off 1 node: A; cutting 1 link: A - B"
print("cable cuts OK")
print("ALL DISASTER TESTS OK")
