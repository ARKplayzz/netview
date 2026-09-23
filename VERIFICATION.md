# CML API verification — 2026-09-24

Every item below was checked against the real CML server (156.62.138.32,
2.10.0), not assumed. Where the original design's assumption was wrong, the
code is fixed and the fix is verified live; where it was optional and not yet
built, the finding is reported without building it.

## 1. Node list/details shape — FIXED

`cpus`, `ram`, `image_definition` are top-level fields on both `topology()`
and `nodes_full()` output, and are **`None` on every node** except one with an
explicit per-node override — confirmed across a switch, a firewall, a RADIUS
container and a Windows server. The real values live under
`nodes_full()[id]["operational"]`, which `topology()` does not carry at all.
`cmlsource.apply()` never read `operational`, so hardware/image info was blank
for nearly every node. Fixed: falls back to `operational.{cpus,ram,image_definition}`
before the topology fallback.

`tags` is `[]` on every node checked — nothing in the lab is tagged yet
(item 12). The code already handles this correctly; there's nothing to fix,
just nothing to show until nodes are tagged.

## 2. `layer3_addresses` — confirmed unreliable, as before

Shape matches `_normalise_l3()`'s assumption exactly
(`{node_id: {name, interfaces: {mac: {id, label, ip4, ip6}}}}`). But it
returned **zero discovered addresses across all 38 running nodes** — every
`ip4`/`ip6` list empty. Worse than the 1/236 measured 2026-08-20. In practice
`cml-l3` never contributes an address on this lab; every address shown comes
from parsed config (item 3) or the manifest fallback. No code change needed —
the fallback chain already handles an empty result gracefully — but don't
expect this endpoint to ever supply real data here.

## 3. Stored node config — FIXED (real bug)

**`GET /nodes/{id}/config` returns HTTP 404 — this endpoint does not exist**
on CML 2.10.0. Checked the full OpenAPI spec for any config-read path: the
only config-shaped endpoint is `PUT .../extract_configuration`, which is a
**write** — it pulls the device's live running config back into CML. Not
something a read-only dashboard refresh should fire silently, so it isn't
used.

51 of 55 nodes have `configuration` embedded directly in `topology()` and
never needed the endpoint. The 4 that don't are all `win2025` — expected,
since they're staged over the network (winserver skill), not day-0. Every
refresh was retrying the dead endpoint for those 4 and getting a fresh 404,
forever. Fixed: `node_configs()` no longer attempts the fetch at all; nodes
with no embedded config are reported as "no config from CML for: ..." instead.

## 4. Link state — confirmed correct, no bug

`GET /links?data=true` has `state`: `STARTED` (76), `STOPPED` (21),
`DEFINED_ON_CORE` (1) on this lab. The code's filter
(`state in (None, "", "STARTED")` counts as traversable) is correct — a
stopped or not-yet-up link is correctly excluded from the path graph.

Also present on every link: `link_capture_key` (== the link's own id) —
relevant to item 9.

## 5. Packet counters (the trace's foundation) — confirmed real, one bug fixed

Confirmed with hand-verified control links (deliberately unrelated to the
path) that background-corrected counters cleanly separate the actual path
from everything else — e.g. real path links net ~51-56 packets on a 20-ping
burst against unrelated control links at ~12-15 (background). This is real
signal, not noise: verified end-to-end through the app's own `/api/trace` API,
not a hand-rolled reimplementation.

**Bug found and fixed:** a healthy, low-latency path (1ms RTT) can complete a
100-packet burst in ~3.4 seconds — IOS sends the next echo immediately on
reply, so packet count barely controls the burst's wall-clock window. On a
busy trunk (measured ~19 pkt/s of HSRP background) that window is too short
for the signal to clear the noise: **100 packets came back `factor: null`,
"could not be calibrated," on a completely healthy path.** 150+ packets
calibrated cleanly (factor 0.97–1.00) on the same path, repeatedly.

Background level varies hugely across the lab — AccessSwitch-DR-1's
core-transit trunk (carrying HSRP hellos for 8 VLANs at once) runs **~96
pkt/s** at idle, five times busier than the link above. No single fixed
packet count is safe everywhere. Fixed: the trace now checks calibratability
right after the first burst and automatically retries once at up to 500
packets if the first attempt didn't produce a usable signal — verified live,
including one real automatic retry (100 → 450 packets) firing in the wild.
Default burst size raised 100 → 150 to make the retry less often necessary.

## 6. Ping-burst commands — confirmed correct, all four forms

Tested live: IOS `repeat N timeout 1` (with replies), IOS `repeat N timeout 0`
("fast" mode against a genuinely dead target — correctly discards the receive
count since timeout 0 doesn't wait), ASA (identical syntax to IOS, confirmed
on a real firewall), and BusyBox `ping -q -c N -i 0.2 -W 1` (fractional
interval accepted, no fallback needed on this image). All four parse cleanly
against their respective regex. No bugs.

## 7. Traceroute — FIXED (real bug, both dialects affected)

**RTT was always `None`** for every real hop, both dialects — confirmed with
live 2-hop and 3-hop traces (IOS via the extended `numeric timeout 1 probe 1
ttl 1 12` form, and ASA, and BusyBox `-n -q 1 -w 1 -m 12`, all three tested
against real multi-hop paths). Cause: the hop regex had a middle "optional
hostname" group meant to skip a reverse-DNS name between the IP and the time.
Neither dialect ever prints a hostname here (`numeric`/`-n` suppress it), so
that group had nothing real to match — being greedy, it swallowed the RTT
number itself before the trailing `ms`-matching group ever saw it. Fixed:
removed the dead hostname group, matched IOS's `msec` and BusyBox's `ms`
directly. Verified against every real captured line from both dialects plus
the timed-out (`*`) case.

The IOS "Invalid" → plain `traceroute` fallback was never triggered — the
extended form worked on every platform tested (2 switch/router images, one
ASA). Not confirmed reachable code; also not proven wrong.

## 8. Tuning thresholds — supported by everything above, not separately stress-tested

0.75 (`both ways`) / 0.30 (`one way`) held up correctly in every real trace
run once burst size was adequate (item 5's fix). Didn't specifically
manufacture an asymmetric-routing or partial-loss scenario to probe the
boundary — the closest real measurement (a control link scoring 0.27, just
under the 0.30 cutoff, correctly excluded) suggests the margin is real but not
huge. Worth revisiting if a genuine asymmetric-routing trace ever looks
wrong in practice.

## 9. Packet capture verification — feasible, not built (as flagged optional)

Full capture workflow tested live end to end:

- `PUT capture/start` needs a body (`{maxpackets, maxtime, bpfilter, encap}`,
  at least one of maxpackets/maxtime required) — the earlier assumption of a
  bare PUT was wrong.
- **`bpfilter` is broken — any non-empty value (`icmp`, `ip proto 1`, both
  tried) silently captures zero packets.** With an empty filter, the exact
  same test correctly caught 178 packets including 40 real ICMP echoes.
  Confirmed by first proving capture itself works (unfiltered capture on a
  known-busy link caught real HSRPv2 hellos immediately — which also
  independently confirms item 5/8's background-traffic theory).
- The real per-packet listing is `GET /pcap/{link_id}/packets` (**not**
  `/wireless/pcap/...`, which 404s for a wired link with "Node not found" —
  that path is genuinely wireless-only despite appearing generic). Returns
  clean structured JSON per packet: `{no, time, source, destination,
  protocol, length, info}` — e.g.
  `{"protocol": "ICMP", "info": "Echo (ping) request id=0x008a, seq=0/0, ttl=255"}`.
  No pcap-parsing library needed for basic verification.
- The ICMP `id`/`seq` fields are exactly the marker Andrew's plan wanted —
  IOS keeps one id per ping session and increments seq, so capturing on every
  candidate link unfiltered and matching by id+seq+time would give exact,
  ordered proof of which links carried which echoes.

**Not built**: orchestrating capture start on every candidate link before the
burst, correlating packets across links, and surfacing it in the UI. That's
the one substantial addition on this list and was explicitly flagged as
optional — reporting the concrete API gotchas (body shape, broken bpfilter,
real endpoint path) here so building it next time doesn't require
rediscovering them.

## Not covered

Items 10–14 (server-side trace history, event-stream push updates, CML node
tagging, Windows nodes as trace sources, access control) are product/scope
decisions, not API assumptions to verify — left as originally described.

## Diagnostic: Firewall-DR-2 <-> Firewall-HQ-1 (2026-09-24)

Ping was 100% healthy the whole time (150/150, 1-2ms RTT) — the trace tool's
own calibration bug (above) made it *look* broken by fabricating a fake path.
After fixing that, the trace still showed a real, surprising route: traffic
left via DMZSwitch-DR-2 -> DMZSwitch-DR-1 -> Firewall-DR-1 -> CoreSwitch-DR-1
instead of Firewall-DR-2's own direct core links. Confirmed live (not a trace
artifact) via `show route` and `show running-config` on Firewall-DR-2: its
`outside`/`inside` interfaces had explicit `ospf cost 50`, but `dmz` was left
at Cisco's cheap default — cheaper than its own core uplinks. Every other
firewall in the lab avoids this (HQ-2 explicitly sets `dmz` to `ospf cost
200`; DR-1/HQ-1 leave both sides at matching defaults). Fixed by adding
`ospf cost 200` to Firewall-DR-2's `dmz` interface (`GigabitEthernet0/5`),
matching HQ-2's pattern, and saved. Re-verified: `show route` now sends
everything via its own `inside`/`inside2` interfaces, and a live trace
confirms a clean 4-hop path (Firewall-DR-2 -> CoreSwitch-DR-2 ->
CoreSwitch-DR-1 -> CoreSwitch-HQ-1 -> Firewall-HQ-1), factor 0.323, no
detour through Firewall-DR-1.

## Diagnostic: WFH user (VPN client) -> ServerRoomSwitch-HQ-1 (2026-09-24)

100% loss, by design. Traceroute reaches EdgeRouter-HQ-1 (10.255.10.5) then
stops; verdict correctly reports requests got as far as Firewall-HQ-1. Matches
known-issue #19 — the perimeter denies an untunneled internet client from
reaching RFC1918 space. Not a fault.
