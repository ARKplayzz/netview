# NetView

Inventory of the P45 estate: every node, its DNS name, addresses, layer, live
state — plus path probes, a baseline reachability matrix, live DHCP lease reads,
a live address harvest, open-issue badges and CSV export.

```
py -3.11 netview/app.py      ->  http://127.0.0.1:5057
```

Standalone. Shares `data/` and the `cml` skill's helpers; the legacy `dashboard/`
app is not used.

> **This repo is the `netview/` subfolder of a larger private project** (a Cisco
> Modeling Labs network-simulation repo). It imports `cml_api.py` / `console.py`
> from a sibling `.claude/skills/cml/scripts/` directory at runtime via
> `sys.path.insert`, and reads `data/manifest.yml`, `data/topology.json` and
> `data/address-lock.json` from the project root two levels up. Those files are
> **not included here** — this push is for reviewing/redesigning the app's own
> code (Flask routes, templates, `static/app.js`, `static/style.css`), not for
> running it standalone. See `app.py`, `model.py`, `probes.py`, `features.py`
> for the exact expected layout if you need to reconstruct it.

## UI

Laid out like the CML workbench, and styled as a desktop network tool rather
than a web dashboard:

- **Menu bar** (Lab / View / Tools): run the address harvest, baseline matrix
  and DNS audit, export CSV, toggle canvas labels, theme (system / light /
  dark), restore hidden nodes, pause refresh.
- **Toolbar**: fit and zoom, label toggles (addresses, interfaces, link
  traffic), site filter, and search (`/`).
- **Canvas**: every node at its CML x/y with the lab's real cabling. Icon shape
  gives the device class; fill and a square lamp give state (green booted,
  amber starting, grey stopped, dashed red outline absent). An orange outline
  marks nodes missing from the manifest. Drag to pan, wheel or pinch to zoom,
  `F` to fit, double-click for the console log, right-click for node actions.
  Filters dim the canvas instead of moving nodes. Nodes with no x/y (absent
  ghosts) sit in a "No canvas position" frame.
- **Properties sidebar** (right): lab summary and "needs attention" list with
  nothing selected; with a node selected, Node / Addresses / Interfaces / DHCP
  / Ping tabs.
- **Bottom pane**: a sortable, groupable node table, the Baseline matrix,
  DNS audit, Live addresses and Ping log tabs (tab titles carry their result
  counts), and one closable tab per console log. Hovering a matrix row draws
  that path on the canvas; a ping draws its path, dashed while running and
  coloured by verdict when done. Drag the divider to resize, double-click it
  to minimise.

Hidden nodes persist as an **exclusion** list, so anything new in the lab
defaults to visible. Canvas toggles, pane height, sort and grouping are
remembered per browser.

## Where the data comes from

| Shown | Derived from |
|---|---|
| Nodes, types, interfaces, cabling | live CML topology (falls back to `data/topology.json` with an age banner) |
| Row IP, ping target, address table | **live device read** (`Read addresses` button) when available, manifest otherwise — see "Live addresses" below |
| Role, site, addresses, VLANs, HA | `data/manifest.yml` (the declared plan — not assumed to be current; the harvest exists precisely because it drifts) |
| Link subnets | `data/address-lock.json` |
| DNS names / aliases / public records | `render_configs.dns_name()` + `SERVICE_ALIASES` / `PUBLIC_ALIASES` |
| DHCP scopes and lease length | `render_configs.DHCP_POOL_*` / `DHCP_LEASE` |
| Baseline probe list | `probe_matrix.PROBES` (imported, incl. its EXPECTED-FAIL convention) |
| Lease parsing | `dhcp_check.parse_lease` (+ Renewal/Rebind regexes added here) |
| Issue badges | `docs/known-issues.md`, OPEN sections only |

Nothing is hand-typed; every one of those is the file the rest of the repo
already treats as authoritative. Grouping comes from the manifest `role:`, never
from label globs (see the zone-glob bug documented in the manifest itself).

## Drift, surfaced rather than hidden

- **undeclared** — in the lab, not in the manifest → renders no config at all
- **absent** — in the manifest, not in the lab → skipped silently at render time
- **snapshot** — CML unreachable → cached topology served, with its age, loudly

## "How long till refresh"

Three different clocks, none conflated:

- header countdown = this page re-polling state (15s, pausable)
- design lease length (12h) shown on anything that leases: probe SVIs and desktops
- **live remaining time** — `Read live lease` on an IOS probe node runs
  `show dhcp lease` and shows Renewal/Rebind with a client-side countdown that
  names its read time. Desktops lease too but their image has no command that
  reports remaining time, and the page says so instead of inventing a number.

## Probes — resolve, then ping

Three facts reported separately: did the name resolve, does the answer match the
manifest (a resolver confidently serving a stale address is the failure no ping
can see), and did ICMP arrive (loss, RTT, per-packet pattern).

No device config is changed. IOS nodes ship `no ip domain lookup`, so the lookup
runs from a node that genuinely has a resolver — preferred order: the source
itself → a desktop at the same site → any shell node on-site → the other
enterprise site → anything left, marked **off-net** and scored *inconclusive* on
failure (an off-net client failing to reach an internal resolver is the design
working; the first version of the picker nearly reported that as a DNS fault).

Sources: IOS / ASA / desktop-family nodes **and the containers** (dnsmasq,
radius, nginx, splunk). Containers keep their root shell on **console line 1** —
line 0 is the service's stdout, which is why an earlier measurement concluded
they had no shell at all (docs/cml-integration-review.md corrected this). Only
`win2025` (SAC prompt), `vap` and connectors are targets-only.

Vantage nodes are never DMZ-resident: the perimeter denies DMZ→RFC1918 before
its domain permits, so a DMZ container asking the internal resolver fails **by
design** — the first audit run scored 20 false "resolver unreachable" rows that
way.

## Live addresses

`Read addresses` reads what each device's console actually reports —
`show ip interface brief` (IOS), `show interface ip brief` (ASA),
`ip -4 -o addr show` (Linux, containers on console line 1) — and prefers that
over the manifest everywhere: row IPs, the drawer's address table, and the
ping-target dropdown. The manifest declares intent; the device is truth, and
the two are shown side by side with drift highlighted, never silently merged.

Runs across the pooled console sessions (see Concurrency model). Measured:
38 nodes, 0 errors, ~40s. Node with no drivable console (see Known limits) are
skipped, not guessed at.

**Address-selection rule** (`app.py::_live_primary`): prefer the live
interface that carries the manifest's *declared primary* address specifically
— not just any address the manifest happens to know about. A router's
Loopback0 is also a manifest-known address (it's the OSPF router-id), and IOS
frequently lists it before the management SVI in `show ip interface brief`;
matching "any manifest address" picked the loopback for every multi-homed
router and reported it as drift against the mgmt-IP primary. Fixed and
verified 2026-09-23 (`docs/netview-issues-20260923.md`, bug #1).

## Baseline matrix

Runs `probe_matrix.py`'s 28 curated paths from the dashboard and renders a live
grid. Outcomes:

- **pass** / **partial** / **dead** / **error**
- **denied ✓** — a declared must-fail that failed (the perimeter working)
- **LEAK** — a declared must-fail that *worked*: the most serious result possible
- **src down / target off** — the endpoint is simply stopped in CML; lab state,
  not a network fault. Sources are checked *before* attaching: a stopped node has
  no console at all, and discovering that via the attach retry loop costs ~90s
  per probe (measured) versus one REST call.

## DNS audit

One button checks **every internal name** against the manifest — batched as one
shell loop per site vantage (one console attach per ~8 names), both sites in
parallel. 39 names in 25 s, measured. First real run found five genuine
problems: names still NXDOMAIN after node renames (the running zones are a
generation behind the manifest — known-issues #30's shape) and one name
answering its DHCP lease address instead of its static record.

## API

| Route | Purpose |
|---|---|
| `GET /api/inventory` | full model + state + issues + drift counts |
| `GET /api/state` | cheap poll: label → state |
| `POST /api/probe`, `GET /api/probe/<id>` | point probe (queued, pooled) |
| `GET /api/matrix`, `POST /api/matrix/run` | baseline matrix (4 parallel sessions) |
| `GET /api/dnsaudit`, `POST /api/dnsaudit/run` | estate DNS audit |
| `GET /api/dhcp/<label>` | live lease read (~15s) |
| `GET /api/harvest`, `POST /api/harvest/run` | live address harvest across all drivable nodes |
| `GET /api/simstats` | per-link packet/drop counters, 0.04s, no console |
| `GET /api/console-log/<label>?line=&lines=` | console scrollback, no attach |
| `GET /api/issues` | parsed known-issues index |
| `GET /api/export.csv` | full inventory as CSV |

## Concurrency model

Console work runs in a **pool of up to 6 sessions** (one `Console` per worker,
never shared), with per-node locks so two sessions never attach the same
console. This follows the measured findings in `docs/cml-integration-review.md`:
the terminal server multiplexes per SSH *session*, not globally — 8/8 and 16/16
concurrent sessions returned clean, correctly-attributed results. Whole-estate
runs (matrix, audit) partition their probes **by source node** across workers.

Measured effect: baseline matrix 9 min → **65 s**; DNS audit 467 s → **25 s**;
two point probes in parallel finish in the time of the slower one.

Where the REST API already knows the answer, the console is not used at all:
link counters (`simulation_stats`) and console logs are 0.02–0.05 s reads.

## Known limits

- Linux (BusyBox) pings report loss + pattern, no RTT; IOS sources report RTT.
- A point probe still costs ~10–17 s: the console attach is real work.
- Parallel console READS are proven safe; parallel config WRITES are not tested
  — anything that writes device config stays serial.
- **Windows (`win2025`) nodes: live addresses only.** The harvest reads them from
  SAC's `i` command, which needs no login (added 2026-09-23). They are still not
  probe sources or DNS-audit vantages — that needs a logged-in CMD channel
  (`winserver/scripts/win_console.py`), not wired into NetView.
- CML's own out-of-band management address (seen: `192.168.255.0/24`) can
  appear as a second address on a container's `eth0` in the live-address
  table. Cosmetic only — it does not affect address selection.
