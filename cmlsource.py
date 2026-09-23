"""Everything NetView can learn about the lab from the CML API.

The inventory used to be the manifest with the live topology laid over it.
This module turns that around: the node list, cabling, state, tags, image,
interfaces, the addresses CML has seen on the wire and the configuration CML
holds for each node all come from the API on every refresh, so a node added,
renamed, re-cabled, re-tagged or re-addressed in CML shows up on the next
refresh without anyone touching the repo.

The manifest is still read, but only as the plan to compare against: where
the lab and the plan disagree, both are shown and the difference is flagged.
Nothing here needs the manifest to exist.

Each API read is independent and failure-tolerant.  Which reads worked is
reported to the page (`meta.cml_reads`), so a missing field is visibly "CML
did not answer" rather than silently blank.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import cfgparse

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
TTL_S = 5            # a page refresh never waits on a fetch younger than this


def _get(cml, path: str):
    return cml._request("GET", f"/api/v0/labs/{cml.lab_id}{path}")


def fetch(cml, force: bool = False) -> dict:
    """{nodes_full, l3, links, configs, reads, at}.  Never raises."""
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < TTL_S:
            return _cache["data"]

    reads: dict[str, str | None] = {}
    out: dict = {"nodes_full": {}, "l3": {}, "links": {}, "configs": {}, "reads": reads}

    try:
        out["nodes_full"] = cml.nodes_full() or {}
        reads["nodes"] = None
    except Exception as e:                     # noqa: BLE001
        reads["nodes"] = f"{type(e).__name__}: {e}"

    # Addresses CML itself has learned for each interface (CML watches ARP /
    # ND on the lab's links).  No console, no credentials, one request.
    try:
        out["l3"] = _normalise_l3(_get(cml, "/layer3_addresses"))
        reads["layer3_addresses"] = None
    except Exception as e:                     # noqa: BLE001
        reads["layer3_addresses"] = f"{type(e).__name__}: {e}"

    try:
        links = _get(cml, "/links?data=true")
        out["links"] = {l["id"]: l for l in links if isinstance(l, dict) and l.get("id")}
        reads["links"] = None
    except Exception as e:                     # noqa: BLE001
        reads["links"] = f"{type(e).__name__}: {e}"

    out["at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cache.update({"at": time.time(), "data": out})
    return out


def node_configs(cml, topo_nodes: list[dict], force: bool = False) -> tuple[dict, str | None]:
    """node id -> config text, from whatever `topology()` embeds.

    There is no GET endpoint to read a node's stored config beyond what
    topology() already carries — confirmed 2026-09-24 against CML 2.10.0's
    own OpenAPI spec: `/nodes/{id}/config` doesn't exist (404) and the only
    config-shaped path is `PUT .../extract_configuration`, a WRITE that pulls
    the device's live running config back into CML. That's a state-changing
    action, wrong to fire silently on a read-only dashboard refresh, so it is
    not used here. Nodes topology() has no configuration for (measured: every
    `win2025` node, which is staged over the network per the winserver skill,
    not day-0) simply have none to show — reported as such, not retried.
    """
    have = {n["id"]: n.get("configuration") for n in topo_nodes
           if n.get("id") and n.get("configuration")}
    without = [n["label"].rstrip() for n in topo_nodes
              if n.get("id") and n["id"] not in have]
    err = (f"no config from CML for: {', '.join(sorted(without))}"
          if without else None)
    return {k: cfgparse.config_text(v) for k, v in have.items()}, err


def _normalise_l3(raw) -> dict:
    """label -> [{iface, mac, ip4: [..], ip6: [..]}].

    CML answers {node_id: {name, interfaces: {mac: {id, label, ip4, ip6}}}};
    older builds used a list.  Accept both, and keep only the address part of
    anything written as address/prefix."""
    out: dict[str, list[dict]] = {}
    items = raw.values() if isinstance(raw, dict) else (raw or [])
    for node in items:
        if not isinstance(node, dict):
            continue
        label = (node.get("name") or node.get("label") or "").rstrip()
        ifaces = node.get("interfaces") or {}
        rows = []
        for mac, i in (ifaces.items() if isinstance(ifaces, dict) else enumerate(ifaces)):
            if not isinstance(i, dict):
                continue
            rows.append({
                "iface": i.get("label") or i.get("name"),
                "mac": i.get("mac_address") or (mac if isinstance(mac, str) else None),
                "ip4": [str(a).split("/")[0] for a in (i.get("ip4") or [])],
                "ip6": [str(a).split("/")[0] for a in (i.get("ip6") or [])],
            })
        if label:
            out[label] = rows
    return out


def _tags(node: dict) -> dict:
    """CML node tags as key:value pairs.  `site:HQ`, `role:l3_core` and friends
    let the lab itself say what a node is; a bare tag is kept as a flag."""
    out: dict = {"_all": []}
    for t in node.get("tags") or []:
        t = str(t)
        out["_all"].append(t)
        if ":" in t:
            k, v = t.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def apply(inv: dict, topology: dict, lab: dict, configs: dict, role_layer: dict,
          layer_title: dict, role_title: dict) -> None:
    """Rewrite each inventory node so CML is the source of truth.

    Every field that changed source gets a `sources[field]` entry naming
    where the shown value came from: `cml` (node data or tags), `cml-l3`
    (addresses CML observed), `cml-config` (the config CML holds), `manifest`
    or `label` (inferred from the name).  The manifest's own values are kept
    under `declared` so the page can show plan against lab side by side."""
    full_by_label = {n.get("label", "").rstrip(): n for n in lab["nodes_full"].values()}
    topo_by_label = {n["label"].rstrip(): n for n in topology.get("nodes", [])}
    link_state = {lid: (l.get("state") or "").upper() for lid, l in lab["links"].items()}

    for n in inv["nodes"]:
        src: dict = {}
        tn = topo_by_label.get(n["label"], {})
        fn = full_by_label.get(n["label"], {})
        tags = _tags({"tags": fn.get("tags") or tn.get("tags")})
        n["tags"] = tags["_all"]
        # `cpus`/`ram`/`image_definition` are top-level fields that hold the
        # PER-NODE OVERRIDE and are None on every node that just takes the
        # image's default (confirmed live 2026-09-23: None on every node
        # checked except one with an explicit RAM override; the running
        # values are always in `operational`, which nodes_full() carries and
        # topology() does not, so the topology() fallback below can never
        # supply what nodes_full() didn't).
        op = fn.get("operational") or {}
        n["image"] = fn.get("image_definition") or op.get("image_definition") or tn.get("image_definition")
        n["cpus"] = fn.get("cpus") or op.get("cpus") or tn.get("cpus")
        n["ram"] = fn.get("ram") or op.get("ram") or tn.get("ram")
        if fn.get("x") is not None:
            n["x"], n["y"] = fn.get("x"), fn.get("y")

        # the plan, kept for comparison only
        n["declared"] = {
            "role": n.get("role"), "site": n.get("site"),
            "addresses": list(n.get("addresses") or []),
            "primary_ip": n.get("primary_ip"),
            "vlans": list(n.get("vlans") or []), "ha": dict(n.get("ha") or {}),
            "gateway": n.get("gateway"), "resolver": n.get("resolver"),
        } if n.get("managed") else None

        # ---- identity: tags in CML first, then the plan, then the label
        if tags.get("site"):
            n["site"], src["site"] = tags["site"].upper(), "cml"
        elif n.get("managed") and n.get("site"):
            src["site"] = "manifest"
        elif n.get("site"):
            src["site"] = "label"
        if tags.get("role"):
            r = tags["role"]
            n["role"], src["role"] = r, "cml"
            n["role_title"] = role_title.get(r, r)
            if r in role_layer:
                n["layer"], n["layer_inferred"] = role_layer[r], False
                n["layer_title"] = layer_title.get(n["layer"], n["layer"])
        elif n.get("role"):
            src["role"] = "manifest"
        src["layer"] = "cml" if tags.get("role") else ("label" if n.get("layer_inferred") else "manifest")

        # ---- addresses: what CML saw on the wire, then what its config says
        cfg = cfgparse.parse(configs.get(n.get("id")), n.get("probe_family") or "")
        n["config"] = {k: v for k, v in cfg.items() if k != "interfaces"} if cfg else None
        n["config_interfaces"] = cfg.get("interfaces", []) if cfg else []
        addrs: list[dict] = []
        seen: set = set()

        def add(ip, iface, source, prefix=None, kind=None):
            if not ip or ip in seen or ip.startswith("127.") or ip.startswith("169.254."):
                return
            seen.add(ip)
            addrs.append({"value": ip, "iface": iface, "prefix": prefix,
                          "source": source, "kind": kind, "label": iface or kind or source})

        for row in lab["l3"].get(n["label"], []):
            for ip in row["ip4"]:
                add(ip, row["iface"], "cml-l3")
        for f in n["config_interfaces"]:
            if f.get("ip"):
                add(f["ip"], f["name"], "cml-config", f.get("prefix"))
            for s2 in f.get("secondary") or []:
                add(s2["ip"], f["name"], "cml-config", s2.get("prefix"), "secondary")
            for g in f.get("hsrp") or []:
                if g.get("vip"):
                    add(g["vip"], f["name"], "cml-config", None, f"HSRP {g['group']} virtual")
        # keep manifest-only addresses visible, marked as such, so a declared
        # address the lab no longer holds is shown rather than vanishing
        for a in (n["declared"] or {}).get("addresses", []):
            if a["value"] not in seen:
                seen.add(a["value"])
                addrs.append({"value": a["value"], "iface": None, "prefix": None,
                              "source": "manifest", "kind": a.get("kind"), "label": a.get("label")})
        n["addresses"] = addrs

        # primary: the plan's primary if the lab confirms it, otherwise the
        # first address CML has actually seen, otherwise the config's
        declared_primary = (n["declared"] or {}).get("primary_ip")
        lab_ips = [a for a in addrs if a["source"] != "manifest" and a.get("kind") is None]
        if declared_primary and any(a["value"] == declared_primary for a in lab_ips):
            n["primary_ip"], src["primary_ip"] = declared_primary, \
                next(a["source"] for a in lab_ips if a["value"] == declared_primary)
        elif lab_ips:
            loop = [a for a in lab_ips if (a["iface"] or "").lower().startswith("loopback")]
            seen_ip = [a for a in lab_ips if a["source"] == "cml-l3"]
            pick = (seen_ip or loop or lab_ips)[0]
            n["primary_ip"], src["primary_ip"] = pick["value"], pick["source"]
        elif declared_primary:
            src["primary_ip"] = "manifest"
        else:
            n["primary_ip"] = None

        # ---- VLANs, HSRP, failover, gateway, resolver from the config
        if cfg:
            vlan_names = cfg.get("vlans") or {}
            vl = []
            for f in n["config_interfaces"]:
                if f.get("vlan"):
                    vl.append({"id": f["vlan"], "name": vlan_names.get(f["vlan"]), "kind": "access",
                               "iface": f["name"]})
                m = f["name"].lower()
                if m.startswith("vlan") and m[4:].isdigit():
                    v = int(m[4:])
                    vl.append({"id": v, "name": vlan_names.get(v), "kind": "svi", "iface": f["name"]})
            uniq = {}
            for v in vl:
                uniq.setdefault((v["id"], v["kind"]), v)
            if uniq:
                n["vlans"], src["vlans"] = sorted(uniq.values(), key=lambda v: v["id"]), "cml-config"
            ha = {}
            for f in n["config_interfaces"]:
                for g in f.get("hsrp") or []:
                    ha[f"HSRP {f['name']} group {g['group']}"] = \
                        f"priority {g.get('priority', 100)}, virtual {g.get('vip') or '-'}"
            if cfg.get("failover"):
                ha["ASA failover unit"] = cfg["failover"]
            if ha:
                n["ha"], src["ha"] = ha, "cml-config"
            if cfg.get("gateway"):
                n["gateway"], src["gateway"] = cfg["gateway"], "cml-config"
            if cfg.get("nameservers"):
                n["resolver"], src["resolver"] = cfg["nameservers"][0], "cml-config"
            if cfg.get("domain") and n.get("dns"):
                short = n["dns"].get("short")
                if short:
                    n["dns"]["fqdn"], src["dns"] = f"{short}.{cfg['domain']}", "cml-config"
            if any(f.get("dhcp") for f in n["config_interfaces"]):
                n["addressing"] = "dhcp"
        for k in ("vlans", "ha", "gateway", "resolver", "dns"):
            src.setdefault(k, "manifest" if n.get("managed") and n.get(k) else None)

        # ---- interfaces, with CML's MAC and the addresses seen on each
        l3_by_if = {r["iface"]: r for r in lab["l3"].get(n["label"], [])}
        cfg_by_if = {f["name"]: f for f in n["config_interfaces"]}
        n["iface_detail"] = []
        for i in tn.get("interfaces") or []:
            name = i.get("label", "")
            r = l3_by_if.get(name) or {}
            f = cfg_by_if.get(name) or {}
            n["iface_detail"].append({
                "name": name, "mac": i.get("mac_address") or r.get("mac"),
                "connected": any(l["local_iface"] == name for l in n.get("links") or []),
                "ip4": r.get("ip4") or [], "config_ip": f.get("ip"),
                "desc": f.get("desc"), "vlan": f.get("vlan"), "shutdown": f.get("shutdown"),
            })
        for l in n.get("links") or []:
            l["state"] = link_state.get(l.get("link_id")) or None
        n["sources"] = src
