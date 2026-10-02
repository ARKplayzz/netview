# NetView: "Failover capture" mode for disaster traces (plan, 2026-10-01)

Status: **BUILT 2026-10-02** (NetView: `failover.py`, `static/failover.js`; tests `tests/test_failover.py`).
Decisions from Andrew (§10) are recorded inline. §13 records what the build changed from this
plan. Read it before trusting §2/§5.2: the capture timing model turned out different.

## 1. What it is

A toggle **inside the existing Disaster panel**. With it off, a disaster trace works
as it does today: baseline trace, power off, settle, trace again, restore.

With it on, the disaster becomes a **recording of the failover itself**:

1. run the baseline trace (Verified mode, as today);
2. arm packet capture on every link the traffic could use, before or after the failure;
3. start a fast, continuous probe from the source to the target;
4. power the selected nodes off;
5. keep recording until **either**
   - the traffic has **recovered**, or reached an appropriate **new location by a new path**,
     and has stayed stable for a short confirmation period; **or**
   - the **window (default 20 s)** runs out;
6. stop, collect every captured packet, and build one organised result: headline
   failover figures, a millisecond timeline, the paths used before, during and after,
   where each lost probe died, and the full packet log;
7. restore the nodes (as today).

The point is to answer, with evidence: **how long did failover take, what path did
traffic take afterwards, and what happened to every probe in between?**

## 2. Facts this design is built on (measured 2026-10-01)

| Fact | Evidence | Consequence |
|---|---|---|
| BusyBox `ping -i 0.2` works with continuous `seq` | WFH client, 6/6 replies, seq 0-5 | Linux sources probe at **5/s → 0.2 s resolution** |
| IOS/ASA `ping` cannot set an interval | — | IOS sources stay at the existing paced **1/s** (one `repeat 1` per second; each probe has its own ICMP id) |
| CML capture accepts a BPF filter | `bpfilter: "icmp or esp or udp port 500 or udp port 4500"` → only ICMP/ESP/ISAKMP captured | Busy trunks (100-300 pps of HSRP/OSPF) can't fill the buffer; IKE keepalives/renegotiation become visible |
| Capture `starttime` has **1 s resolution** | `'2026-10-01T19:07:09Z'` | Per-link timelines can be ±1 s apart; links must be aligned using packets seen on several links (§5.2) |
| ESP `info` has the **SPI but no ESP sequence** | `ESP (SPI=0x6cad8b3c)` | Tunnel packets are matched across links by (SPI, order), not by sequence number |
| Captures started within ~1 s of a stop, or in parallel, often never run | known (pathtrace `_capture_start_all`) | Reuse `_capture_start_all` unchanged: sequential starts, verify, retry |
| CML `stop_node` takes 3-30 s, and a stopped node's peer link stays up | known-issues #3, today's runs (3-5 s) | The moment of failure is measured **from the packets**, not from the API call (§5.3) |
| Windows eval servers shut down ~1 h after boot | known-issues #34; it broke two runs today | Pre-flight warning (§7) |

## 3. The toggle and its options (UI)

In the Disaster panel, under the node chips:

```
Disaster (optional)
  [chips: EdgeRouter-HQ-1 × EdgeRouter-HQ-2 × CoreSwitch-HQ-1 × CoreSwitch-HQ-2 ×]
  Mode   (•) Standard     ( ) Failover capture   ⓘ
  ── Failover capture only ───────────────────────────────
  Window        [20 s ▾]   (10 / 20 / 30 / 60)
  Probe rate    5 per second (Linux source) · 1 per second (IOS/ASA source)   ← read-only, from the source
  Stop early    [x] when recovered or on a new path (stable for 2 s)
  Device logs   [ ] also collect routing/track syslog from the devices on both paths  (+~20 s)
  Restore afterwards [x]
```

- **Failover capture forces Verified (capture) mode.** If the user has Quick selected,
  it switches and says why in one line.
- **Repeat** stays disabled while a disaster is selected (as today).
- The confirmation step is unchanged, plus one line: *"Records up to 20 s of packets
  from the power-off; stops early once traffic is stable on a path."*
- The existing **settle** dropdown is hidden in this mode; **Window** replaces it.

## 4. Server flow (job stages)

| # | Stage shown in the UI | What happens |
|---|---|---|
| 1 | `pre-flight checks` | §7 checks; a refusal here powers nothing off |
| 2 | `baseline trace (before the disaster)` | Unchanged Verified-mode `_execute()` |
| 3 | `arming capture on N links` | Capture scope (§5.1) started with the BPF filter, `maxtime = window + 120 s`, `maxpackets = 20000` |
| 4 | `warm-up: watching the healthy path` | Fast probe started; wait for ≥ 3 s **and** ≥ 5 consecutive replies seen in capture on the baseline path (max 30 s, else abort with "baseline not stable", nothing powered off) |
| 5 | `powering off N nodes` | Write the crash record, then issue the stops (same `_power()`). Record **T₀** (stop requested) and each node's **STOPPED** time |
| 6 | `watching the failover (x.x s / 20 s)` | Live loop (§5.4): probe outcomes + live capture poll; early completion or window end |
| 7 | `collecting packets from N links` | Stop captures, pull all packets in parallel (existing `_packet_log` pattern) |
| 8 | `building the failover timeline` | Alignment, per-probe paths, metrics (§5.2-5.6) |
| 9 | `collecting device logs` (optional) | `show logging` from devices on both paths, in parallel (≤ 6 consoles) |
| 10 | `restoring N nodes and waiting for them to boot` | Unchanged `restore()` in `finally` |

**The window starts when every selected node reports STOPPED** (T₁), not at the
click. CML's stop takes 3-30 s: counting from the click could end the window before
the failure even happens. Both T₀ and T₁ are shown on the timeline, so the CML
latency is visible and never charged to the network. *(Decision for Andrew: §10 Q1.)*

**No second full trace by default.** The failover path comes from the capture
itself. An optional "also run a full trace afterwards" checkbox can be added later
(§10 Q6).

Cancel: stops the probe and captures, keeps whatever was recorded (marked
"cancelled at x s"), and still restores.

## 5. How the measurements are made

### 5.1 Capture scope: what gets captured

Captures are only useful if they are running **before** the failure, on the links the
traffic will use **after** it. Scope = union of:

- every link on the baseline's proven path;
- `candidates()` from source to target **with the disaster nodes treated as down**:
  the predicted post-failure paths;
- for anycast/HSRP targets, the same for **every holder** (existing `vip_holders` logic);
- tunnel heads in scope → source→head, head→peer underlay, peer→target (existing logic);
- every leg of any firewall in scope (existing logic);
- every link touching a disaster node (to record the last packets before it died).

Reported as **coverage**: links captured / links in scope, with any that failed to
start named. A probe whose path crosses an uncaptured link is marked "path partly
unknown" rather than guessed.

### 5.2 One clock for every link

CML gives each link's capture its own start time, rounded to a whole second. To put
all packets on one timeline:

1. Take the **source's own link** as the reference clock.
2. For every other link, find packets that also appear on an already-aligned
   neighbouring link: the same ICMP `id`+`seq`, or for ESP the same `(SPI, n-th packet)`.
   The per-link correction is the **median** difference, assuming a crossing takes
   about 0 ms (lab links are sub-millisecond).
3. Spread outward link by link (BFS over the capture scope).
4. Report the **residual alignment error** (spread of the differences) per link and the
   worst case. Links with no shared packets keep the ±1 s whole-second start time
   and are flagged.

All result times are then **seconds relative to the failure moment, F** (§5.3), with
a switch to "relative to the click (T₀)".

### 5.3 The moment of failure, F

F = the time of the **last packet seen on any link that touches a disaster node**.
That is when the network actually lost those nodes, independent of CML API latency
and of local clocks. If no such packet exists (nothing was flowing through them),
F falls back to T₁ and the result says so.

### 5.4 The probe and the live watch loop

**The probe.**
- **Linux source:** one continuous `ping -i 0.2 -W 1 <ip>` process. The `seq` field
  links each console line to its packets in the capture.
- **IOS/ASA source:** existing paced `BackgroundPing` (1/s). Each probe is linked to
  the capture by its ICMP id.
- **Target address:** the resolved address, fixed at the start. A DNS-failover target
  is resolved once (noted in the result).

**The live loop**, every 0.5 s:
- read new probe outcomes from the console;
- poll new packets on the **decision links** only: links adjacent to the target or any
  holder, plus the first and last hop of each predicted path. Polling all 30-40 links
  at 2/s is what starved CML before (#38).

**Completion (state machine), evaluated only after T₁:**

| State | Condition | Result |
|---|---|---|
| `recovered` | ≥ 1 lost probe after F, then **K consecutive replies** (K = 2 s worth: 10 at 5/s, 2 at 1/s) | Stop after the confirmation period |
| `new path` | Replies seen in the capture leaving a **different endpoint** (another holder) or crossing a link not on the baseline path, for K consecutive probes | Stop after the confirmation period (catches hitless failover) |
| `no impact` | Neither of the above, and no loss | **Run to the window end.** A late failure (e.g. a 40 s dead timer) must not be missed |
| `not recovered` | Window ends with loss still continuing | Stop at the window end; honest failure |
| `unstable` | More than one outage period (e.g. today's DR oscillation, 5 s on / 5 s off) | Never stops early; reported as unstable |

"Stop early" can be turned off (§3), so a run always records the full window.

### 5.5 Per-probe path reconstruction

For each probe (by seq or id):

- **request path:** the ordered links its request crossed (aligned timestamps), from the
  source's link onward; tunnels are a single "via tunnel A⇄B" hop, matched by ESP
  (SPI, order) and time ±50 ms;
- **reply path:** the same for its reply;
- **answered by:** the endpoint whose link carried the reply away (holder, or the target);
- **outcome:** `ok`, `lost-request` (request never reached an endpoint), `lost-reply`
  (endpoint answered, reply died), `unreachable` (an ICMP unreachable came back, with
  the sender named; today's Firewall-DR-1 Null0 drop would show as this), or `unknown`
  (crossed uncaptured links);
- **died at:** for lost probes, the last link it was seen on, and the device it was
  heading into.

Probes are then grouped into **paths** (identical request+reply link sequences):
`baseline`, `failover` (the final stable one), and `transient` (anything seen only
during convergence, such as a dead-end into a dropping firewall).

### 5.6 Headline metrics (exact definitions)

| Metric | Definition | Why this one |
|---|---|---|
| **Failover time** | first reply on the final path − F | The network's own recovery time; excludes CML latency |
| **Outage (probe view)** | send time of the first lost probe → send time of the first good probe after the last loss | What a user saw; ± one probe interval |
| **Detection** | F → the first probe that took a different path or died somewhere new | How long until the network reacted |
| **From click** | first good reply on the final path − T₀ | Includes CML's stop latency; for comparison with older runs |
| Probes lost | n / sent, after F | |
| Outage periods | count; more than 1 = unstable | Catches flapping |
| RTT | median before / median after (ms) | A slower path after failover is a finding |
| Path changed | yes/no; hops removed/added (existing `path_diff`) | |
| Answered by | before → after | Anycast/HSRP |
| Completed by | recovered / new path / no impact / window end / cancelled | |
| Resolution | probe interval, alignment error | Every figure is shown with its ± |

## 6. Result display ("well organised for testing")

Laid out top to bottom in the Disaster card when failover mode was used:

**A. Verdict bar.** One line, colour-coded:
`FAILED OVER in 5.8 s ±0.2` · `NO IMPACT (20 s watched)` · `NOT RECOVERED within 20 s` · `UNSTABLE: 3 outages`.
Under it, the headline metric tiles: Failover time · Outage · Probes lost · Path
changed · Answered by (before → after) · Completed by.

**B. Failover timeline**, the centrepiece, with a shared time axis
(−pre … 0 = F … +end), zoomable to the outage:

```
            -3s        -1s    F=0      +2s       +4s       +6s       +8s
Disaster    ▲T₀ click   ▲CoreSwitch-HQ-1 STOPPED ▲all STOPPED (T₁)
Probes      ■■■■■■■■■■■■■■■■■■│✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕□□□□□□□□□□□□
              baseline (blue)          lost (red)              new path (green)
Path A      ████████████████████│                                     (baseline: …HQ-1…)
Path B                                                 ████████████  (failover: …DR-1…)
Transient                        ▒▒ dead-end at Firewall-DR-1 (unreachable)
Events      first loss +0.1 · tunnel moved to Tunnel2 +4.9 · first reply on B +5.8 · stable +7.8
Device log  (optional) +0.9 OSPF nbr down CoreSwitch-DR-1 · +5.2 TRACK 10 Up→Down HomeRouter-WFH
```

Hovering a probe shows seq, send time, outcome, RTT, path and died-at.

**C. Topology overlay.** One diagram: baseline path in blue, failover path in green,
transient paths dotted, downed nodes crossed out, and per-link packet counts split
**before F / after F**. The existing *Replay on topology* plays the packet log
through the event at 0.25×/1×/4×.

**D. Paths table.** One row per path: role (baseline/failover/transient), hops,
first seen, last seen, probes carried, replies, median RTT, answered by.

**E. Lost probes table.** seq · sent at · outcome · died at (link / device) ·
probable cause. The cause is an inference from the evidence, shown in grey: *into a
downed node*, *dropped by firewall* (unreachable seen), *no route*, *reply lost*.

**F. Packet log.** Every captured packet of this test on one aligned timeline:
time (relative to F, ms) · link · direction · kind (ICMP request/reply/unreachable,
ESP, IKE) · seq/SPI · src → dst. Filters for kind, link, probe seq, phase
(before/during/after) and outcome; sortable. Default view: the ±2 s around F and
around recovery.

**G. Coverage & accuracy footer.** Links captured n/m (missing ones named) ·
alignment error worst x ms · probe interval · window start rule (T₁) · CML stop
latency (T₀→T₁).

**H. Export and history.**
- **Export JSON** (the whole result) · **CSV** (probes, packets, paths) · **PCAP**
  (per-link pcaps zipped, from CML's `/pcap/<capture key>`, for Wireshark).
- **Run history.** Every failover run is saved to
  `%LOCALAPPDATA%\NetView\failover\<time>_<source>_<target>.json`. A "Failover history"
  tab lists runs (when · source → target · disaster nodes · failover time · lost ·
  verdict) and compares two side by side. This is what turns it into a testing log:
  e.g. "WFH → fileserver: 13 s → 6 s after the SLA change".

## 7. Pre-flight checks (refuse or warn before powering anything off)

- **Refuse** if capture is unhealthy: start a capture on the source's link and check it
  counts packets within 3 s (today's "CML capture broken" state would otherwise give
  an empty log after a real outage).
- **Refuse** if the target/holder doesn't answer the warm-up probe (stage 4).
- **Warn** if any holder/target or DC is a Windows eval node close to its hourly
  shutdown. Uptime is read from the SAC console log's millisecond counter (free REST
  read): warn above 50 min. This alone would have saved two of today's runs.
- **Warn** if the capture scope is more than 40 links (start time grows; CML at
  0.1-0.5 s per start, 2-8 s when busy).
- The existing disaster checks apply unchanged: one disaster at a time; source not
  selectable; crash record.

## 8. Data model (job result; additive to today's disaster result)

```jsonc
"disaster": {
  "mode": "failover",               // absent = standard mode, unchanged
  "nodes": [...], "restore": true, "window_s": 20, "stop_early": true,
  "failover": {
    "clock": {"reference_link": "...", "aligned_links": 31, "unaligned": ["..."], "worst_error_ms": 4},
    "times": {"T0": 0.0, "stopped": {"EdgeRouter-HQ-1": 3.1, ...}, "T1": 4.9, "F": 3.4, "end": 12.7}, // seconds since T0
    "probe": {"interval_s": 0.2, "sent": 47, "family": "linux", "dest": "10.60.0.70"},
    "metrics": {"failover_s": 5.8, "outage_s": 6.0, "detection_s": 0.1, "from_click_s": 9.2,
                "lost": 30, "outage_periods": 1, "rtt_before_ms": 2.6, "rtt_after_ms": 2.4,
                "path_changed": true, "answered_by": ["FileServer-HQ-1", "FileServer-DR-1"],
                "completed_by": "recovered", "resolution_s": 0.2},
    "paths":  [{"id": "A", "role": "baseline", "hops": [...], "first": -3.0, "last": 0.0,
                "probes": 15, "replies": 15, "rtt_ms": 2.6, "answered_by": "FileServer-HQ-1"}, ...],
    "probes": [{"seq": 17, "sent": 0.1, "outcome": "lost-request", "path": null,
                "died_at": {"link": "...", "into": "EdgeRouter-HQ-1"}, "rtt_ms": null}, ...],
    "events": [{"t": 0.1, "kind": "first_loss"}, {"t": 4.9, "kind": "tunnel_switch", "detail": "..."}, ...],
    "packets": [{"t": -0.012, "link": "...", "a": "...", "b": "...", "dir": "request",
                 "kind": "icmp", "seq": 16, "src": "...", "dst": "..."}, ...],
    "coverage": {"in_scope": 34, "captured": 33, "missing": ["..."]},
    "device_logs": [{"node": "...", "t_device": "...", "t": 5.2, "line": "...", "meaning": "..."}]  // optional
  },
  "restored": {...}
}
```

The standard-mode fields (`outage`, `after`, `diff`, `verdict_change`) stay as they
are, so old results and the existing UI keep working.

## 9. Implementation outline

| Area | Change |
|---|---|
| `netview/failover.py` (new) | Capture scope, alignment, per-probe reconstruction, path grouping, metrics, completion state machine. Pure functions on packet lists → unit-testable without CML |
| `netview/disaster.py` | `run()` branches on `mode == "failover"`: arm → warm-up → power off → watch → collect. Restore/crash-record logic unchanged |
| `netview/pathtrace.py` | Reuse `_capture_start_all` (+ `bpfilter` parameter), `_packet_log`, `candidates`, scope helpers. Expose scope building for reuse |
| `cml_api.capture_start` | Add an optional `bpfilter` argument (default `""`, so today's callers are unchanged) |
| `console.py` | `ContinuousPing` for Linux sources: one `ping -i 0.2` process; lines streamed with arrival time and seq |
| `app.py` | Accept `disaster.mode/window_s/stop_early/device_logs`; validation; history + export endpoints (`/api/failover/history`, `/api/failover/<id>.json/.csv`, pcap zip) |
| `static/disaster.js` | The toggle and options; live watch view; result sections A-H |
| `static/style.css` | Timeline lanes, tiles |
| `docs/disaster-spec.md` | Link to this spec |

## 10. Decisions (Andrew, 2026-10-01)

1. **Window start:** from when the nodes are stopped (T₁), **selectable 10/20/30/60 s,
   default 20 s**. It only exists so a test can't run forever.
2. **Probe rate:** 5/s for Linux sources (0.2 s resolution), 1/s for IOS/ASA (IOS ping
   cannot set an interval). Shown in the panel and on every result.
3. **Early stop:** after 2 s of continuous replies (10 probes at 5/s, 2 at 1/s).
4. **History:** `%LOCALAPPDATA%\NetView\failover\` (one JSON per run, plus a `.pcaps.zip`).
5. **Device logs:** included, **off by default**.
6. **"Also run a full trace afterwards":** included, off by default.

## 11. Test plan

**Unit (`tests/test_failover.py`, synthetic packet logs, no CML):**
- clock alignment: links with a known ±0.7 s skew are recovered to < 5 ms; an
  unaligned link is flagged;
- per-probe reconstruction: request/reply paths; tunnel hop by ESP (SPI, order);
  lost-request vs lost-reply vs unreachable; died-at;
- path grouping: baseline/failover/transient;
- metrics on hand-made timelines:
  - a clean failover;
  - hitless (no loss, path change → `new path`);
  - never recovers (`not recovered`);
  - **today's DR oscillation** (5 s on/off → `unstable`, no early stop);
- completion state machine with a fake clock: early stop only after T₁ and after K
  replies; `no impact` runs to the window end;
- crash-record and restore paths unchanged (existing `test_disaster.py` stays green).

**Live, P45 lab (each compared against an independent per-probe measurement, e.g. a
`ping -i 0.2` log read from the console; must agree within one probe interval):**
1. WFH → fileserver.p45.nz, disaster HQ edges + cores: expect `recovered`, ~6 s,
   HQ → DR path, early stop.
2. Finance PC → ISP-1, disaster EdgeRouter-HQ-1: BGP/BFD, expect ~1-2 s.
3. Tech PC → company website (anycast), disaster WebsiteServer-DR-1: expect the HQ
   copy after ~14 s (SLA).
4. **No impact:** Finance PC → fileserver, disaster DistSwitch-HQ-2 (standby): runs the
   full 20 s, `no impact`.
5. **Not recovered:** a non-anycast target inside the disaster: 20 s, lost probes
   with died-at.
6. Cancel mid-window; kill NetView mid-window → interrupted record → Restore.
7. Rebuilt exe; extend `ui_disaster_e2e.py` with a failover-mode run (toggle on, window
   20 s) and screenshots of sections A-F.

**Acceptance:**
- every live run produces a complete, aligned packet log;
- the headline failover time matches the independent measurement within one probe
  interval;
- lost probes each name where they died;
- standard mode is byte-for-byte unchanged.

## 12. Risks

| Risk | Mitigation |
|---|---|
| CML capture starts slow under load (2-8 s each) | Arm before power-off with the existing budget; show coverage; refuse if the reference link won't capture |
| Live polling load on CML (#38) | Poll decision links only, 0.5 s; final pull once |
| Asymmetric paths make "path" ambiguous | Request and reply paths are kept separately per probe |
| ESP matching by order breaks with loss on one link | Fall back to ±50 ms time matching; mark as inferred |
| Eval Windows shutdowns mid-run | Uptime pre-flight warning (§7) |
| A very fast failover (< probe interval) looks "hitless" | Resolution is shown on every figure; the capture still shows the path switch |

## 13. What the build changed from the plan (2026-10-02, all measured live)

1. **CML's packet LISTING times count from the first captured packet**, not from the
   capture start (two links' first packets both read `0.000000`). §5.2's "align on
   whole-second start times" is therefore wrong. NetView's existing Verified mode
   (`pathtrace._packet_log`) makes the same assumption, so its cross-link timeline can be
   off by however long each link waited for its first packet. **Open, not fixed here.**
2. **Times come from the pcap file instead** (`GET /pcap/<link_capture_key>`):
   - absolute microsecond timestamps from the one CML host clock, for every link (the
     same ping 0.28 ms apart on two hops);
   - ESP **sequence numbers**, so the same encrypted packet is matched exactly across
     links;
   - the original probe inside an ICMP unreachable, so a drop is tied to its probe.

   CML writes the pcap big-endian. `align()` is now only a consistency check.
3. **CML tears down the captures on a stopped node's links.** In the first live run the
   19 links into the disaster nodes, exactly where the failure happens, came back empty.
   Fix: read every capture key at arming time, and keep a pcap **snapshot** (every ~0.7 s)
   of the links into the disaster nodes until shortly after they stop. The snapshot is
   used when the final download is shorter or missing. The result lists which links came
   from a snapshot.
4. **F must count ESP.** In a VPN path the edges and carriers only carry the encrypted
   copy, so "last packet a dead node forwarded" includes ESP seen on two links of the same
   node (matched by SPI + sequence number).
5. **ESP is kept when ONE tunnel end is proven.** The far head can be cut off and never
   show a plain ping in the recording.
6. **Hop order follows the links, not the timestamps.** Separate captures stamp packets
   tens of µs out of order, and a time-ordered stitch drew the home router twice. Paths
   are now a breadth-first walk over the links each probe was seen on.
7. **Device-log timing:** `show clock` is timed on an already-attached session
   (±~50 ms). Timing a whole `exec()` included the ~5 s attach and put ISP-2's BFD-down
   3.8 s before the failure.
8. **The local clock vs the CML clock:** this lab's CML host clock runs ~12 h ahead of the
   workstation (a timezone setting). The click and CML stop events are mapped onto the
   capture clock from console reply times vs the captured replies (Linux sources), or from
   capture start times (±1.5 s, IOS sources).

### Live results (P45, WFH → fileserver.p45.nz, disaster HQ edges + cores, 20 s window)

| Run | Verdict | Failover | Outage | Notes |
|---|---|---|---|---|
| 1 (before fixes 3-7) | failed over | 4.58 s | 4.4 s | F fell back to T₁; HQ links empty; paths scrambled |
| 2 | **failed over** | **8.80 s** | 8.4 s | F from packets (+3 ms after the click); baseline via EdgeRouter-HQ-1 › Firewall-HQ-1 › CoreSwitch-HQ-1; lost probes traced into ISP-1; home-router TRACK 10 down at +4.9 s; stopped early (recovered and stable) |

Run 2's 8.8 s against the ~6 s measured on 2026-10-01 by console pings alone: the console
measure ran at 1 probe/s and counted from the power-off *completion*. Here F is the moment
the edges actually stopped forwarding, 1.2 s before CML reported them stopped.
