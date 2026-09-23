"""Read facts out of the node configurations CML stores.

CML keeps a configuration for every node (the day-0 config, or whatever was
last extracted into the lab).  That is the lab's own record of how each device
is set up, fetched over the API, so it follows the lab when someone edits a
node in CML - unlike the manifest, which only changes when someone edits the
repo.  It is still a stored config and not the running one; the console
harvest (features.py) is what reads the running state.

Three dialects are handled, best effort, because a parser that raises on an
unfamiliar line would blank the whole sidebar for one odd node:

    IOS / IOS-XE / IOL      interface blocks, ip address, standby (HSRP),
                            switchport access vlan, vlan names, gateway,
                            name servers, domain
    ASA                     the same interface shape plus nameif and failover
    Linux (alpine, desktop) the shell script CML runs at boot: ip addr add,
                            ifconfig, udhcpc, default route, resolv.conf
"""
from __future__ import annotations

import ipaddress
import re

_IP = r"\d+\.\d+\.\d+\.\d+"


def config_text(raw) -> str:
    """CML returns a node's configuration either as one string or, for images
    with several config files, as a list of {name, content}.  Join them."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(raw)


def _prefix(mask: str) -> int | None:
    try:
        return ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return None


def parse(raw, family: str) -> dict:
    """Return {hostname, domain, interfaces, vlans, gateway, nameservers,
    failover}.  Empty dict when there is nothing to read."""
    text = config_text(raw)
    if not text.strip():
        return {}
    try:
        if family == "linux":
            return _parse_linux(text)
        return _parse_ios(text)
    except Exception:                          # noqa: BLE001 - never break the page
        return {}


def _parse_ios(text: str) -> dict:
    out: dict = {"hostname": None, "domain": None, "interfaces": [], "vlans": {},
                 "gateway": None, "nameservers": [], "failover": None}
    iface = None
    vlan_id = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.lstrip().startswith("!"):
            continue
        indented = s[:1] in (" ", "\t")
        t = s.strip()

        if not indented:
            iface = None
            vlan_id = None
            m = re.match(r"^hostname\s+(\S+)", t)
            if m:
                out["hostname"] = m.group(1)
                continue
            m = re.match(r"^ip domain[ -]name\s+(\S+)", t) or re.match(r"^domain-name\s+(\S+)", t)
            if m:
                out["domain"] = m.group(1)
                continue
            m = re.match(rf"^ip name-server\s+(.+)$", t) or re.match(r"^dns name-server\s+(.+)$", t)
            if m:
                out["nameservers"] += re.findall(_IP, m.group(1))
                continue
            m = re.match(rf"^ip default-gateway\s+({_IP})", t) \
                or re.match(rf"^ip route 0\.0\.0\.0 0\.0\.0\.0\s+({_IP})", t) \
                or re.match(rf"^route \S+ 0(?:\.0\.0\.0)? 0(?:\.0\.0\.0)?\s+({_IP})", t)
            if m and not out["gateway"]:
                out["gateway"] = m.group(1)
                continue
            m = re.match(r"^failover lan unit (primary|secondary)", t)
            if m:
                out["failover"] = m.group(1)
                continue
            m = re.match(r"^vlan\s+(\d+)\s*$", t)
            if m:
                vlan_id = int(m.group(1))
                out["vlans"].setdefault(vlan_id, None)
                continue
            m = re.match(r"^interface\s+(\S+)", t)
            if m:
                iface = {"name": m.group(1), "ip": None, "prefix": None, "secondary": [],
                         "dhcp": False, "desc": None, "vlan": None, "mode": None,
                         "shutdown": False, "hsrp": [], "nameif": None}
                out["interfaces"].append(iface)
            continue

        if vlan_id is not None:
            m = re.match(r"^name\s+(.+)$", t)
            if m:
                out["vlans"][vlan_id] = m.group(1).strip()
            continue
        if iface is None:
            continue
        m = re.match(rf"^ip address\s+({_IP})\s+({_IP})(\s+secondary)?", t)
        if m:
            if m.group(3):
                iface["secondary"].append({"ip": m.group(1), "prefix": _prefix(m.group(2))})
            else:
                iface["ip"], iface["prefix"] = m.group(1), _prefix(m.group(2))
            continue
        if re.match(r"^ip address dhcp", t):
            iface["dhcp"] = True
            continue
        m = re.match(r"^description\s+(.+)$", t)
        if m:
            iface["desc"] = m.group(1).strip()
            continue
        m = re.match(r"^nameif\s+(\S+)", t)
        if m:
            iface["nameif"] = m.group(1)
            continue
        m = re.match(r"^switchport access vlan\s+(\d+)", t)
        if m:
            iface["vlan"] = int(m.group(1))
            continue
        m = re.match(r"^switchport mode\s+(\S+)", t)
        if m:
            iface["mode"] = m.group(1)
            continue
        if t == "shutdown":
            iface["shutdown"] = True
            continue
        m = re.match(rf"^standby\s+(\d+)\s+ip\s+({_IP})", t)
        if m:
            _hsrp(iface, int(m.group(1)))["vip"] = m.group(2)
            continue
        m = re.match(r"^standby\s+(\d+)\s+priority\s+(\d+)", t)
        if m:
            _hsrp(iface, int(m.group(1)))["priority"] = int(m.group(2))
            continue
    return out


def _hsrp(iface: dict, group: int) -> dict:
    for g in iface["hsrp"]:
        if g["group"] == group:
            return g
    g = {"group": group, "vip": None, "priority": 100}
    iface["hsrp"].append(g)
    return g


def _parse_linux(text: str) -> dict:
    out: dict = {"hostname": None, "domain": None, "interfaces": [], "vlans": {},
                 "gateway": None, "nameservers": [], "failover": None}
    by_name: dict[str, dict] = {}

    def iface(name: str) -> dict:
        if name not in by_name:
            by_name[name] = {"name": name, "ip": None, "prefix": None, "secondary": [],
                             "dhcp": False, "desc": None, "vlan": None, "mode": None,
                             "shutdown": False, "hsrp": [], "nameif": None}
            out["interfaces"].append(by_name[name])
        return by_name[name]

    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        m = re.search(rf"ip (?:-4 )?addr(?:ess)? add\s+({_IP})/(\d+)\s+dev\s+(\S+)", t)
        if m:
            f = iface(m.group(3))
            if f["ip"]:
                f["secondary"].append({"ip": m.group(1), "prefix": int(m.group(2))})
            else:
                f["ip"], f["prefix"] = m.group(1), int(m.group(2))
            continue
        m = re.search(rf"ifconfig\s+(\S+)\s+({_IP})(?:\s+netmask\s+({_IP}))?", t)
        if m:
            f = iface(m.group(1))
            f["ip"] = m.group(2)
            f["prefix"] = _prefix(m.group(3)) if m.group(3) else None
            continue
        m = re.search(r"(?:udhcpc|dhclient)\b.*?(?:-i\s+)?(eth\d+|ens\S+)", t)
        if m:
            iface(m.group(1))["dhcp"] = True
            continue
        m = re.search(rf"route add default (?:via|gw)\s+({_IP})", t) \
            or re.search(rf"ip route add default via\s+({_IP})", t)
        if m:
            out["gateway"] = m.group(1)
            continue
        m = re.search(rf"nameserver\s+({_IP})", t)
        if m:
            out["nameservers"].append(m.group(1))
            continue
        m = re.search(r"search\s+(\S+)", t)
        if m and "resolv" in t:
            out["domain"] = m.group(1)
            continue
        m = re.match(r"^hostname\s+(\S+)", t)
        if m:
            out["hostname"] = m.group(1)
    return out
