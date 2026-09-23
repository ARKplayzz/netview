"""NetView inventory model.

Derives one flat, fully-described node list from the three files that are already
the authority for this estate:

    data/manifest.yml      roles, sites, addressing, HA roles, VLANs, zones
    data/topology.json     the CML lab: node types, interfaces, links, canvas x/y
    data/address-lock.json every routed /30 that has ever been handed out

Nothing here is hand-typed.  DNS names come from `render_configs.dns_name()` —
the same function that writes the actual zone — so what this dashboard claims a
device is called is what the resolver genuinely answers for.  A second copy of
that rule would drift from the first within a week, and a stale A record on a
dashboard is worse than no dashboard.

Live node state is layered on top by app.py; this module never talks to CML, so
the whole inventory still renders with the lab powered off.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / ".claude" / "skills" / "cml" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

# Single source of truth for the naming rule.  Imported, never reimplemented.
from render_configs import (  # noqa: E402
    dns_name,
    safe,
    SERVICE_ALIASES,
    PUBLIC_ALIASES,
    PUBLIC_ZONE,
    ROLE_TEMPLATES,
    DHCP_POOL_FIRST,
    DHCP_POOL_LAST,
    DHCP_LEASE,
)


def _lease_seconds(spec: str) -> int:
    """'12h' -> 43200.  The renderer states the lease once; this reads it."""
    spec = str(spec).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if spec and spec[-1] in mult:
        return int(float(spec[:-1]) * mult[spec[-1]])
    return int(float(spec))


DHCP_LEASE_SECS = _lease_seconds(DHCP_LEASE)

MANIFEST = REPO / "data" / "manifest.yml"
TOPOLOGY = REPO / "data" / "topology.json"
ADDRESS_LOCK = REPO / "data" / "address-lock.json"


# --------------------------------------------------------------------------
# Layers
#
# Grouping is driven by the manifest's `role:`, NOT by matching the node label
# against a glob.  That distinction is load-bearing here.  The manifest's own
# zone block carries a comment describing the bug that motivated it: the DMZ
# globs named two superseded shells and missed the resolver that was actually
# running, so a scenario stopped the wrong nodes and then reported the DMZ as
# having survived the disaster.  A role is declared once, next to the address it
# owns; a glob is a guess about a string.
# --------------------------------------------------------------------------

LAYERS: list[tuple[str, str]] = [
    ("core",      "Core Layer"),
    ("dist",      "Distribution Layer"),
    ("access",    "Access Layer"),
    ("services",  "Core Services"),
    ("serverroom", "Server Room"),
    ("dmz",       "DMZ"),
    ("security",  "Security / Perimeter"),
    ("edge",      "Edge / WAN"),
    ("wireless",  "Wireless"),
    ("endpoints", "Endpoints"),
    ("remote",    "Remote / Home"),
    ("carrier",   "Carrier / Internet"),
    ("tooling",   "Test / Tooling"),
    ("unclassified", "Unclassified"),
]
LAYER_ORDER = {key: i for i, (key, _) in enumerate(LAYERS)}
LAYER_TITLE = dict(LAYERS)

ROLE_LAYER = {
    "l3_core":         "core",
    "l3_core_hsrp":    "core",
    "l3_dist":         "dist",
    "l2_access":       "access",
    "l2_server":       "serverroom",
    "l2_dmz":          "dmz",
    "dns_internal":    "services",
    "server_radius":   "services",
    "dns_public":      "dmz",
    "server_web":      "dmz",
    "firewall_asa":    "security",
    "edge_router":     "edge",
    "isp_router":      "carrier",
    "web_internet":    "carrier",
    "home_router":     "remote",
    "wfh_desktop":     "remote",
    "wlc":             "wireless",
    "ap":              "wireless",
    "wireless_client": "endpoints",
    "desktop_client":  "endpoints",
    "trex":            "tooling",
}

ROLE_TITLE = {
    "l3_core":         "L3 core switch",
    "l3_core_hsrp":    "L3 core switch (HSRP pair)",
    "l3_dist":         "L3 distribution switch",
    "l2_access":       "L2 access switch",
    "l2_server":       "L2 server-room switch",
    "l2_dmz":          "L2 DMZ switch",
    "dns_internal":    "Internal DNS + DHCP (dnsmasq)",
    "dns_public":      "DMZ / public DNS (dnsmasq)",
    "server_radius":   "RADIUS server",
    "server_web":      "Web server (DMZ)",
    "firewall_asa":    "ASA firewall",
    "edge_router":     "Edge router",
    "isp_router":      "Carrier router",
    "web_internet":    "Internet web server",
    "home_router":     "Home CPE router",
    "wfh_desktop":     "Remote worker desktop",
    "wlc":             "Wireless LAN controller",
    "ap":              "Access point",
    "wireless_client": "Wireless client",
    "desktop_client":  "User desktop",
    "trex":            "Traffic generator",
}

# Fallback layer inference for nodes that exist in the lab but are NOT declared
# in the manifest.  These get a best-guess layer so they are still visible in the
# right neighbourhood, and are always badged `undeclared` — an undeclared node
# renders no config at all, which is the failure mode the manifest documents for
# desktop-0/1 (no role -> no switchport VLAN -> no DHCP, invisible until a user
# tried to work).  Guessing a layer is presentation; never a claim it is managed.
LABEL_LAYER_HINTS: list[tuple[str, str]] = [
    (r"^dc-",            "services"),      # domain controllers (winserver migration)
    (r"^fileserver",     "serverroom"),
    (r"^win\d",          "serverroom"),
    (r"^splunk",         "tooling"),
    (r"^syslog",         "tooling"),
    (r"^trex",           "tooling"),
    (r"^ext-conn",       "carrier"),
    (r"^internet-",      "carrier"),
    (r"^isp-",           "carrier"),
    (r"^desktop",        "endpoints"),
    (r"^wireless-client", "endpoints"),
    (r"^ap-",            "wireless"),
    (r"^wlc-",           "wireless"),
    (r"^dns",            "services"),
    (r"^dhcp",           "services"),
    (r"^radius",         "services"),
    (r"^websiteserver",  "dmz"),
    (r"^coreswitch",     "core"),
    (r"^distswitch",     "dist"),
    (r"^accessswitch",   "access"),
    (r"^serverroomswitch", "serverroom"),
    (r"^dmzswitch",      "dmz"),
    (r"^edgerouter",     "edge"),
    (r"^firewall",       "security"),
    (r"^homerouter",     "remote"),
]

# --------------------------------------------------------------------------
# Probe capability
#
# Which console dialect a node speaks decides whether it can be a ping source at
# all, which command shape to use — and WHICH CONSOLE LINE to attach.
#
# Container nodes (dnsmasq, radius, nginx, splunk) expose TWO console lines:
# line 0 is the service's stdout (PID 1 — query logs, auth results), line 1 is a
# root shell.  An earlier version of this file measured line 0, saw silence, and
# concluded these images had no shell at all; the 2026-08-20 integration review
# (docs/cml-integration-review.md) re-measured on line 1 and got a root prompt
# and working pings on every container tried.  Attaching a container on line 0
# is worse than useless: it fails AND strands the session, poisoning the next
# node's attach.
#
# win2025 nodes have only line 0 and it is the Windows SAC prompt, not a shell.
# --------------------------------------------------------------------------

SHELL_DEFS = {"desktop", "wireless-client", "trex", "alpine"}       # getty on line 0
CONTAINER_DEFS = {"dnsmasq", "radius", "nginx", "splunk", "syslog",
                  "server"}                                          # shell on line 1
IOS_DEFS = {"iosv", "iol-xe", "ioll2-xe", "cat9800", "csr1000v", "iosvl2"}
ASA_DEFS = {"asav"}

NO_SOURCE_REASON = {
    "none": "no console this tool knows how to drive "
            "(win2025 exposes only the SAC prompt; vap and external "
            "connectors expose nothing)",
}


def probe_family(node_definition: str) -> str:
    if node_definition in SHELL_DEFS or node_definition in CONTAINER_DEFS:
        return "linux"
    if node_definition in IOS_DEFS:
        return "ios"
    if node_definition in ASA_DEFS:
        return "asa"
    return "none"


def console_line(node_definition: str) -> int:
    """Which console line has the shell.  Containers: 1.  Everything else: 0."""
    return 1 if node_definition in CONTAINER_DEFS else 0


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_manifest() -> dict:
    """The plan.  Optional: without it, everything comes from CML and there is
    simply nothing to compare against."""
    if not MANIFEST.exists():
        return {}
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}


def load_topology() -> dict:
    return json.loads(TOPOLOGY.read_text(encoding="utf-8-sig"))


def load_address_lock() -> dict:
    if not ADDRESS_LOCK.exists():
        return {}
    return json.loads(ADDRESS_LOCK.read_text(encoding="utf-8-sig"))


def _link_subnets(address_lock: dict) -> dict[tuple[str, str], dict]:
    """(node, iface) -> {subnet, pool} for every routed /30 ever allocated.

    Keys in address-lock.json look like
        "CoreSwitch-HQ-1|Ethernet0/2<->DistSwitch-HQ-1|Ethernet0/0"
    and each end of the link is indexed separately so either device can find it.
    """
    out: dict[tuple[str, str], dict] = {}
    for pool, links in address_lock.items():
        if not isinstance(links, dict):
            continue
        for key, subnet in links.items():
            for side in key.split("<->"):
                if "|" not in side:
                    continue
                node, iface = side.split("|", 1)
                out[(node.strip(), iface.strip())] = {"subnet": subnet, "pool": pool}
    return out


def _infer_layer(label: str) -> str:
    low = label.lower()
    for pattern, layer in LABEL_LAYER_HINTS:
        if re.match(pattern, low):
            return layer
    return "unclassified"


def _infer_site(label: str) -> str | None:
    m = re.search(r"-(HQ|DR)\b", label, re.I)
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------

def build_inventory(topology: dict | None = None,
                    manifest: dict | None = None) -> dict:
    """Return {"nodes": [...], "layers": [...], "sites": {...}, "vlans": {...}}.

    `topology` may be a live topology pulled from CML or the cached snapshot;
    the shape is identical either way.
    """
    manifest = manifest or load_manifest()
    topology = topology if topology is not None else load_topology()
    alock = load_address_lock()
    subnets = _link_subnets(alock)

    sites = manifest.get("sites", {})
    vlans = manifest.get("vlans", {})
    mnodes: dict = manifest.get("nodes", {})

    topo_nodes = topology.get("nodes", [])
    by_label = {n["label"].rstrip(): n for n in topo_nodes}

    # iface id -> (node label, iface label), for turning links into adjacency
    iface_by_id: dict[str, tuple[str, str]] = {}
    for n in topo_nodes:
        lbl = n["label"].rstrip()
        for i in n.get("interfaces", []) or []:
            iface_by_id[i["id"]] = (lbl, i.get("label", ""))

    adjacency: dict[str, list[dict]] = {lbl: [] for lbl in by_label}
    for link in topology.get("links", []) or []:
        a, b = link.get("interface_a"), link.get("interface_b")
        if a not in iface_by_id or b not in iface_by_id:
            continue
        (na, ia), (nb, ib) = iface_by_id[a], iface_by_id[b]
        for (src, si, dst, di) in ((na, ia, nb, ib), (nb, ib, na, ia)):
            info = subnets.get((src, si), {})
            adjacency[src].append({
                "local_iface": si,
                "peer": dst,
                "peer_iface": di,
                "subnet": info.get("subnet"),
                "pool": info.get("pool"),
                "link_id": link.get("id"),
            })
    for lbl in adjacency:
        adjacency[lbl].sort(key=lambda d: d["local_iface"])

    nodes: list[dict] = []

    for label, tn in sorted(by_label.items(), key=lambda kv: kv[0].lower()):
        nm = mnodes.get(label)
        managed = nm is not None
        nm = nm or {}
        role = nm.get("role")
        site = nm.get("site") or _infer_site(label)
        site_info = sites.get(site, {}) if site else {}

        layer = ROLE_LAYER.get(role) if role else None
        layer_inferred = layer is None
        if layer is None:
            layer = _infer_layer(label)

        nodes.append(_describe(
            label=label,
            topo=tn,
            nm=nm,
            managed=managed,
            role=role,
            site=site,
            site_info=site_info,
            layer=layer,
            layer_inferred=layer_inferred,
            manifest=manifest,
            vlans=vlans,
            links=adjacency.get(label, []),
        ))

    # Manifest entries with no live node.  These render nothing — render_configs
    # skips them by exact label — so a device that was renamed in the manifest
    # silently stops being configured.  They are surfaced as ghost rows rather
    # than being dropped, because "declared but absent" is exactly the state that
    # is invisible everywhere else.
    ghosts = []
    for label, nm in mnodes.items():
        if label in by_label:
            continue
        role = nm.get("role")
        ghosts.append({
            "label": label,
            "ghost": True,
            "managed": True,
            "present": False,
            "config_status": "absent",
            "role": role,
            "role_title": ROLE_TITLE.get(role, role or "unknown"),
            "site": nm.get("site"),
            "layer": ROLE_LAYER.get(role, "unclassified"),
            "layer_inferred": False,
            "node_definition": None,
            "state": None,
            "addresses": _addresses(nm),
            "dns": {},
            "links": [],
            "vlans": [],
            "probe_family": "none",
            "can_source_probe": False,
            "template": ROLE_TEMPLATES.get(role) if role else None,
        })

    return {
        "nodes": nodes,
        "ghosts": ghosts,
        "layers": [{"key": k, "title": t} for k, t in LAYERS],
        "sites": sites,
        "vlans": vlans,
        "public_zone": PUBLIC_ZONE,
    }


def _addresses(nm: dict) -> list[dict]:
    """Every address the manifest gives a node, labelled by what it IS.

    Order matters: the first entry is what the UI shows as the primary address
    and what a probe defaults to.  A service address (`ip`) outranks a management
    address, because pinging a device's mgmt SVI proves the mgmt VLAN works and
    tells you nothing about the service.
    """
    out: list[dict] = []
    if nm.get("ip"):
        out.append({"kind": "service", "label": "Service IP", "value": nm["ip"]})
    if nm.get("mgmt_ip"):
        out.append({"kind": "mgmt", "label": "Management", "value": nm["mgmt_ip"]})
    if nm.get("router_id"):
        out.append({"kind": "loopback", "label": "Loopback0 / OSPF RID",
                    "value": nm["router_id"]})
    if nm.get("router_id_dr"):
        out.append({"kind": "loopback", "label": "Loopback (DR POP)",
                    "value": nm["router_id_dr"]})
    if nm.get("wlc_mgmt_ip"):
        out.append({"kind": "service", "label": "Wireless management",
                    "value": nm["wlc_mgmt_ip"]})
    if nm.get("public_ip"):
        out.append({"kind": "public", "label": "Public (NAT)", "value": nm["public_ip"]})
    if nm.get("vpn_public"):
        out.append({"kind": "public", "label": "VPN head-end (public)",
                    "value": nm["vpn_public"]})
    if nm.get("home_ip"):
        out.append({"kind": "service", "label": "Home LAN", "value": nm["home_ip"]})
    if nm.get("lan_gw"):
        out.append({"kind": "gateway", "label": "Home LAN gateway", "value": nm["lan_gw"]})
    if nm.get("fo_peer_ip"):
        out.append({"kind": "ha", "label": "Failover peer", "value": nm["fo_peer_ip"]})
    return out


def _describe(*, label, topo, nm, managed, role, site, site_info, layer,
              layer_inferred, manifest, vlans, links) -> dict:
    node_def = topo.get("node_definition")
    addresses = _addresses(nm)
    primary = addresses[0]["value"] if addresses else None

    # ---------------------------------------------------------------- DNS
    short = dns_name(label)
    domain = site_info.get("domain")
    fqdn = f"{short}.{domain}" if domain else None

    # The A record this name resolves to is whatever the zone was rendered from:
    # `ip` if the node has a service address, else `mgmt_ip`.  Same expression as
    # render_configs uses when it builds the record, so the two agree by
    # construction rather than by review.
    a_record = nm.get("ip") or nm.get("mgmt_ip")

    cnames: list[str] = []
    if site and domain:
        for alias, target in SERVICE_ALIASES.items():
            if short == f"{target}-{site.lower()}-1":
                cnames.append(f"{alias}.{domain}")

    public: list[dict] = []
    if nm.get("public_ip") or nm.get("vpn_public"):
        for alias, resolver in PUBLIC_ALIASES.items():
            try:
                value = resolver(manifest, site)
            except (KeyError, StopIteration, TypeError):
                continue
            if value and value in (nm.get("public_ip"), nm.get("vpn_public")):
                public.append({"name": f"{alias}.{PUBLIC_ZONE}", "ip": value})

    # Which resolver this device is configured to ask.  The DMZ tier is the one
    # exception in the estate: it forwards to the CARRIER, never to the internal
    # resolver, because it must keep answering when the inside is gone.
    if role == "dns_public":
        resolver = site_info.get("isp_dns")
        resolver_note = "carrier resolver (DMZ tier never asks the inside)"
    elif role in ("isp_router", "web_internet"):
        resolver = None
        resolver_note = "carrier infrastructure — no enterprise resolver"
    else:
        resolver = site_info.get("dns")
        resolver_note = "site internal resolver" if resolver else None

    # ---------------------------------------------------------------- VLANs
    vlan_list: list[dict] = []
    for v in (nm.get("access_vlans") or []):
        info = vlans.get(v, {})
        vlan_list.append({"id": v, "name": info.get("name"), "kind": "access"})
    if nm.get("vlan"):
        info = vlans.get(nm["vlan"], {})
        vlan_list.append({"id": nm["vlan"], "name": info.get("name"), "kind": "member"})

    # ---------------------------------------------------------------- DHCP
    # Two genuinely different kinds of DHCP client, and one large group with no
    # lease at all.  Inventing a countdown for the third group would be fiction,
    # so it is reported as static instead.
    #
    #   probe   a switch with `dhcp_probe_vlan` — a second SVI that leases on
    #           purpose, so a scenario can prove the site still hands out
    #           addresses after a failover
    #   client  a user desktop, which has no address in the manifest because it
    #           is supposed to get one from the scope; its VLAN decides which
    #   (none)  everything else, addressed statically from the manifest
    dhcp = None
    scope = None
    if site and site.lower() in ("hq", "dr"):
        skey = site.lower()
    else:
        skey = None

    if nm.get("dhcp_probe_vlan"):
        v = nm["dhcp_probe_vlan"]
        vinfo = vlans.get(v, {})
        dhcp = {
            "kind": "probe",
            "vlan": v,
            "vlan_name": vinfo.get("name"),
            "interface": f"Vlan{v}",
            "expected_server": site_info.get("dns"),
            "expected_lease_secs": DHCP_LEASE_SECS,
            "scope": _scope_text(vinfo, skey),
        }
    elif role in ("desktop_client",) and not nm.get("ip") and nm.get("vlan"):
        v = nm["vlan"]
        vinfo = vlans.get(v, {})
        dhcp = {
            "kind": "client",
            "vlan": v,
            "vlan_name": vinfo.get("name"),
            "interface": nm.get("iface", "eth0"),
            "expected_server": site_info.get("dns"),
            "expected_lease_secs": DHCP_LEASE_SECS,
            "scope": _scope_text(vinfo, skey),
        }

    # ---------------------------------------------------------------- HA
    ha = {}
    if nm.get("hsrp_pri") is not None:
        ha["hsrp_priority"] = nm["hsrp_pri"]
        ha["hsrp_role"] = "active (preferred)" if nm["hsrp_pri"] >= 200 else "standby"
    if nm.get("fo_role"):
        ha["failover_role"] = nm["fo_role"]
    if nm.get("ha_role"):
        ha["wlc_role"] = nm["ha_role"]
    if nm.get("primary_isp"):
        ha["primary_isp"] = nm["primary_isp"]
    if nm.get("isp"):
        ha["primary_isp"] = nm["isp"]
    if nm.get("isp_backup"):
        ha["backup_isp"] = nm["isp_backup"]
    if nm.get("core_transit"):
        ha["core_transit"] = True

    family = probe_family(node_def)

    if managed:
        config_status = "managed" if ROLE_TEMPLATES.get(role) else "no-template"
    else:
        config_status = "undeclared"

    return {
        "label": label,
        "id": topo.get("id"),
        "node_definition": node_def,
        "x": topo.get("x"),
        "y": topo.get("y"),
        "present": True,
        "ghost": False,
        "managed": managed,
        "config_status": config_status,
        "template": ROLE_TEMPLATES.get(role) if role else None,
        "config_file": f"configs/{safe(label)}.cfg" if managed and ROLE_TEMPLATES.get(role) else None,
        "role": role,
        "role_title": ROLE_TITLE.get(role, role or "not declared in manifest"),
        "site": site,
        "domain": domain,
        "layer": layer,
        "layer_title": LAYER_TITLE.get(layer, layer),
        "layer_inferred": layer_inferred,
        "addresses": addresses,
        "primary_ip": primary,
        "dns": {
            "short": short,
            "fqdn": fqdn,
            "a_record": a_record,
            "cnames": cnames,
            "public": public,
        },
        "resolver": resolver,
        "resolver_note": resolver_note,
        "gateway": _gateway_for(nm, site, vlans),
        "vlans": vlan_list,
        "dhcp": dhcp,
        "addressing": "dhcp" if dhcp else ("static" if primary else "unaddressed"),
        "ha": ha,
        "floor": nm.get("floor"),
        "interfaces": [i.get("label") for i in (topo.get("interfaces") or [])],
        "interface_count": len(topo.get("interfaces") or []),
        "links": links,
        "probe_family": family,
        "console_line": console_line(node_def),
        "can_source_probe": family in ("linux", "ios", "asa"),
        "no_source_reason": NO_SOURCE_REASON.get(family),
        "state": None,          # filled in by app.py from live CML
    }


def _scope_text(vinfo: dict, skey: str | None) -> str | None:
    """'192.168.20.100 – 192.168.20.199' for the VLAN's scope at this site."""
    if not skey or not vinfo.get(skey):
        return None
    net = vinfo[skey].split("/")[0].rsplit(".", 1)[0]
    return f"{net}.{DHCP_POOL_FIRST} – {net}.{DHCP_POOL_LAST}"


def _gateway_for(nm: dict, site: str | None, vlans: dict) -> str | None:
    """The default gateway a node uses, where the manifest determines it."""
    if nm.get("home_gw"):
        return nm["home_gw"]
    if not site:
        return None
    key = site.lower()
    v = nm.get("vlan")
    if v and v in vlans:
        return vlans[v].get(f"{key}_gw")
    return None


if __name__ == "__main__":
    inv = build_inventory()
    print(f"{len(inv['nodes'])} nodes, {len(inv['ghosts'])} declared-but-absent")
    for layer in inv["layers"]:
        members = [n["label"] for n in inv["nodes"] if n["layer"] == layer["key"]]
        if members:
            print(f"\n{layer['title']} ({len(members)})")
            for m in members:
                print("   ", m)
