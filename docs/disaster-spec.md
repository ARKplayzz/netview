# NetView: "Disaster" trace (spec, 2026-09-29)

> The **Failover capture** mode (a toggle in the Disaster panel) is specified in
> [failover-capture-spec.md](failover-capture-spec.md).

## What it does
After choosing **From** and **To**, the user can add a **disaster**: pick any running
nodes to power off. NetView then:

1. runs the trace exactly as normal: the **baseline**;
2. powers the selected nodes off in CML;
3. measures the outage from the source to the target once per second, until the
   path recovers or a settle time runs out;
4. runs the same trace again: the **after-disaster** result;
5. powers the nodes back on and waits for them to boot.

Both results are shown: the baseline on top, and below it the after-disaster trace
with its own path diagram, the outage figures and a "what changed" summary.

## User interface
- **The disaster panel.** A "Disaster (optional)" section sits under From/To in the
  path panel, with:
  - a filterable checklist of RUNNING nodes, grouped by site;
  - "quick add" by zone (from the manifest's `zones`, e.g. "HQ Core+Dist");
  - chips showing the current selection.
- **Settle time:** 15 / 30 / 60 / 120 s, default 30. This is the longest NetView waits
  for recovery before the second trace. It moves on as soon as 5 consecutive pings
  succeed.
- **Restore afterwards:** default on. When off, the nodes stay down and the result
  says so.
- **Confirmation step.** The Trace button becomes "Run disaster trace". Clicking it
  shows an inline confirmation ("Power off 3 nodes: A, B, C. They are started again
  afterwards."), with Confirm and Cancel.
- **Blocked selections:**
  - the source, since its console runs the test;
  - nodes that are not running.
- **Warnings:**
  - the target is selected (the test then measures "does something else answer",
    which is valid for anycast and DNS failover);
  - Windows nodes are selected (about 6 min to boot again);
  - more than 8 nodes are selected;
  - a DNS vantage the trace needs is selected.
- **A banner on every open NetView page while a disaster is active:** "Disaster test
  running: N nodes powered off (…)". Normal traces started during it get a finding
  saying the network is deliberately degraded.
- **Results area:**
  - the Baseline card;
  - a Disaster card listing the downed nodes, the power-off time, the outage
    timeline strip (one mark per second, like the failover harness) with the longest
    outage and the recovery time, and the after-disaster trace in the same layout as
    a normal result, with downed nodes drawn crossed out;
  - "What changed": the path diff (hops removed or added), verdict before and after,
    replies before and after, and who answered if that changed (anycast or DNS);
  - a restore status line (restored N of N / still booting / failed).
- **Cancel** is available while the disaster runs. It stops after the current stage
  and always restores.

## Server
- **POST `/api/trace`** accepts `"disaster": {"nodes": [...], "settle_s": 30,
  "restore": true}`. The server validates:
  - that the nodes exist, are running, and are not the source;
  - that no other disaster is active (one at a time, global lock → HTTP 409).
- **Job stages:** `baseline trace` → `powering off N nodes` → `measuring outage` →
  `trace after disaster` → `restoring nodes` → `waiting for boot`.
- **The after-disaster trace** re-reads the inventory first, so node states, anycast
  holders, candidate paths and DNS-failover names reflect the degraded network.
  Holders that are powered off are dropped.
- **Outage measurement:** after the power-off, the source pings the target once per
  second (`console.BackgroundPing`, as `failover_test.py` does) until recovery or
  settle. That gives the per-second timeline, the longest outage and time-to-recover.
- **Restore always runs**, in a `finally`, even on error or cancel, and waits for
  BOOTED (up to 10 min). Nodes that don't come back are listed.
- **Crash safety.** Before powering anything off, the job writes a record
  `{nodes, started, job_id}` to `%LOCALAPPDATA%\NetView\disaster.json` (or a
  `netview/` file when run from source). It is deleted after a successful restore.
  If NetView restarts with a record present:
  - GET `/api/disaster` reports it;
  - the UI shows "N nodes were left powered off by an interrupted disaster test —
    Restore now";
  - POST `/api/disaster/restore` starts them.
- **Other endpoints:**
  - GET `/api/disaster` returns the active or interrupted disaster, for the banner;
  - POST `/api/trace/<id>/cancel`.
- **Errors:**
  - if CML refuses to stop a node (permissions), the job restores what it did stop
    and reports it;
  - a baseline failure (source down, no console) aborts before anything is powered off.

## Cable cuts (added 2026-10-06)
A disaster can also cut links: CML's link `state/stop` unplugs the cable at both
ends while both nodes keep running (a fibre cut, not a box failure). The panel
lists the cables of any node matching the filter; chips show them with a scissors
mark. Request field `disaster.links` is a list of link ids; the server names them
"A - B" from the live link map. Rules: at most 8 links; both ends must be running
and neither may also be powered off in the same disaster. The record written
before the first stop carries `links: {name: id}`, so Restore (and the banner's
Restore now) re-plugs them; cables are re-plugged before nodes are started.
In failover-capture mode the cut links are captured and snapshotted like the
links into a powered-off node, and F is the last test packet seen on a cut link.
Diagrams draw a cut cable red and dashed, labelled CUT.

## Not in scope
Shutting an interface from the device side; scheduling; more than one target.

## Tests
- **Unit** (`tests/test_disaster.py`, mocked CML and console):
  - validation (source selected, node not running, empty selection = plain trace,
    second disaster → 409);
  - the record is written before the power-off and cleared after restore;
  - restore runs when the after-trace raises, and on cancel;
  - the outage parser handles the per-second timeline;
  - the path diff.
- **Live:**
  1. Finance PC → ISP-1, disaster EdgeRouter-HQ-1: the baseline goes via ER-HQ-1,
     the after trace via ER-HQ-2; the outage is measured; the node is restored.
  2. Tech PC → anycast website, disaster WebsiteServer-DR-1: after the disaster it
     is answered by the HQ copy.
  3. Kill NetView mid-disaster, restart it: the interrupted record shows, and
     restore works.
  4. The same as test 1 through the rebuilt NetView.exe.
- **UI:** checked in the browser if the Chrome connection is available; otherwise
  by the DOM/JS review and the API contract.

## Deploy
Commit, push, build NetView.exe v1.1.0 (the secret guard applies), test the exe,
then release on GitHub.
