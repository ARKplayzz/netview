/* NetView: failover capture, the toggle inside a disaster trace
 * (spec: docs/failover-capture-spec.md, 2026-10-01).
 * Loaded after app.js and disaster.js; uses their globals
 * (trace, BY, el, svg, getJSON, pathDiagram, replayPackets, setTraceEnds, renderPathBody...). */

const FO_WINDOWS = [10, 20, 30, 60];
const FO_ROLE_COLOR = { 'baseline': 'var(--sel)', 'baseline (unchanged)': 'var(--sel)',
                        'failover': 'var(--ok)', 'transient': 'var(--warn)' };
const FO_VERDICT = {
  'failed over': ['s-ok', 'FAILED OVER'], 'no impact': ['s-ok', 'NO IMPACT'],
  'not recovered': ['s-bad', 'NOT RECOVERED'], 'unstable': ['s-bad', 'UNSTABLE'],
};
const foSecs = (x, dp = 1) => (x == null ? '-' : `${x >= 0 ? '' : '−'}${Math.abs(x).toFixed(dp)} s`);

/* ---------------------------------------------------------- panel options */
function failoverOptions(D, redraw) {
  const box = el('div', 'fo-opts');
  const mode = el('div', 'dz-row');
  mode.appendChild(el('b', null, 'Mode'));
  [['standard', 'Standard', 'Baseline trace, power off, wait for recovery, trace again.'],
   ['failover', 'Failover capture', 'Baseline trace, then a packet-level recording of the failover itself: ' +
    'every probe followed through the network, stopping once traffic is stable or the window ends.']]
    .forEach(([v, label, tip]) => {
      const l = el('label');
      l.title = tip;
      const r = el('input'); r.type = 'radio'; r.name = 'dz-mode'; r.value = v; r.checked = D.mode === v;
      r.id = `dz-mode-${v}`;
      r.addEventListener('change', () => { D.mode = v; D.confirming = false; renderPathHead(); redraw(); });
      l.appendChild(r); l.appendChild(document.createTextNode(' ' + label));
      mode.appendChild(l);
    });
  box.appendChild(mode);
  if (D.mode !== 'failover') return box;

  const row = el('div', 'dz-row fo-row');
  const wl = el('label', null, 'Window ');
  const ws = el('select'); ws.id = 'dz-window';
  FO_WINDOWS.forEach((v) => { const o = el('option', null, `${v} s`); o.value = v; ws.appendChild(o); });
  ws.value = D.window;
  ws.addEventListener('change', () => { D.window = +ws.value; D.confirming = false; });
  wl.appendChild(ws);
  wl.title = 'Longest recording after the nodes have STOPPED (CML takes 3-30 s to stop a node, so the ' +
             'window starts then, not at the click). Stops earlier if "Stop early" applies.';
  row.appendChild(wl);
  const cb = (id, key, text, tip) => {
    const l = el('label'); l.title = tip;
    const c = el('input'); c.type = 'checkbox'; c.id = id; c.checked = !!D[key];
    c.addEventListener('change', () => { D[key] = c.checked; D.confirming = false; });
    l.appendChild(c); l.appendChild(document.createTextNode(' ' + text));
    row.appendChild(l);
  };
  cb('dz-stop-early', 'stopEarly', 'Stop early when stable',
     'Stop once traffic has recovered (or moved to a new path) and stayed up for 2 s. A run with no impact ' +
     'always records the whole window, so a late failure is not missed; an unstable run never stops early.');
  cb('dz-device-logs', 'deviceLogs', 'Device logs',
     'Also read routing/track syslog (OSPF, BGP, HSRP, IP SLA...) from the devices on the path, placed on the ' +
     'same timeline. Adds about 20 s.');
  cb('dz-after-trace', 'afterTrace', 'Also run a full trace afterwards',
     'After the recording, run the normal after-disaster trace too (adds about 1-2 minutes).');
  box.appendChild(row);
  const fam = (BY.get(trace.source) || {}).probe_family;
  box.appendChild(el('div', 's-dim', `Probe: ${fam === 'linux' ? '5 per second (0.2 s resolution)' :
    'one per second (1 s resolution: IOS/ASA ping cannot set an interval)'} from ${trace.source || 'the source'}. ` +
    'Uses Verified (packet capture) mode. Runs are saved and listed below.'));
  box.appendChild(failoverHistory());
  return box;
}

/* --------------------------------------------------------------- history */
let foHistoryOpen = false;
function failoverHistory() {
  const wrap = el('div', 'fo-hist');
  const btn = el('button', 'linkish', foHistoryOpen ? 'Hide previous failover runs' : 'Previous failover runs...');
  btn.id = 'fo-hist-toggle';
  wrap.appendChild(btn);
  const body = el('div');
  wrap.appendChild(body);
  const load = async () => {
    body.textContent = 'Loading...';
    let j;
    try { j = await getJSON('/api/failover/history'); } catch (e) { body.textContent = 'Could not load the history.'; return; }
    body.innerHTML = '';
    if (!j.runs.length) { body.appendChild(el('div', 's-dim', `No runs saved yet (${j.folder}).`)); return; }
    const t = el('table', 'g fo-table');
    const hr = el('tr');
    ['when', 'source → target', 'disaster', 'verdict', 'failover', 'outage', 'lost', ''].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    j.runs.forEach((r) => {
      const tr = el('tr', 'row');
      tr.appendChild(el('td', 's-dim', r.at ? new Date(r.at).toLocaleString() : r.name.slice(0, 15)));
      tr.appendChild(el('td', null, `${r.source} → ${r.target}`));
      tr.appendChild(el('td', 'wrap', (r.nodes || []).join(', ')));
      const v = FO_VERDICT[r.verdict] || ['s-dim', r.verdict || '?'];
      tr.appendChild(el('td', v[0], v[1]));
      tr.appendChild(el('td', null, foSecs(r.failover_s, 2)));
      tr.appendChild(el('td', null, foSecs(r.outage_s, 1)));
      tr.appendChild(el('td', null, r.sent ? `${r.lost}/${r.sent}` : '-'));
      const act = el('td');
      const open = el('button', 'linkish', 'Open');
      open.addEventListener('click', async () => {
        const run = await getJSON('/api/failover/run/' + encodeURIComponent(r.name));
        setTraceEnds(run.source, run.virtual_target || run.target);
        trace.history.push(run);
        trace.result = run;
        renderPathBody();
      });
      act.appendChild(open);
      act.appendChild(document.createTextNode(' '));
      const a = el('a', null, 'JSON'); a.href = '/api/failover/run/' + encodeURIComponent(r.name); a.target = '_blank';
      act.appendChild(a);
      tr.appendChild(act);
      t.appendChild(tr);
    });
    body.appendChild(t);
    body.appendChild(el('div', 's-dim', `Saved in ${j.folder}`));
  };
  btn.addEventListener('click', () => {
    foHistoryOpen = !foHistoryOpen;
    btn.textContent = foHistoryOpen ? 'Hide previous failover runs' : 'Previous failover runs...';
    if (foHistoryOpen) load(); else body.innerHTML = '';
  });
  if (foHistoryOpen) load();
  return wrap;
}

/* ------------------------------------------------------------ live view */
function failoverLive(live) {
  const box = document.querySelector('[data-failover-live]');
  if (!box) return;
  box.innerHTML = '';
  const strip = el('div', 'dz-strip fo-live-strip');
  [...(live.marks || '')].slice(-300).forEach((m) => strip.appendChild(el('i', m === '!' ? 'ok' : 'lost')));
  box.appendChild(strip);
  const pct = Math.min(100, (live.elapsed_s / live.window_s) * 100);
  const meter = el('div', 'meter fo-window'); const fill = el('i'); fill.style.width = pct + '%'; meter.appendChild(fill);
  box.appendChild(meter);
  const cls = live.state === 'down' || live.state === 'unstable' ? 's-bad' : live.state === 'recovering' ? 's-warn' : 's-dim';
  box.appendChild(el('div', cls, `${live.elapsed_s.toFixed(1)} s of ${live.window_s} s since all nodes stopped · ` +
    `${live.sent} probes sent, ${live.lost} lost · ${live.state}` +
    (live.new_path ? ` · traffic seen on ${live.new_path}` : '') +
    (live.stopped && live.stopped.length ? ` · stopped: ${live.stopped.join(', ')}` : '')));
}

/* --------------------------------------------------------------- result */
function failoverResult(res, width) {
  const d = res.disaster;
  const fo = d.failover;
  const box = el('div', 'dz-result fo-result');
  box.appendChild(el('div', 'dz-title', `Failover capture: ${d.nodes.join(', ')} powered off`));
  (d.warnings || []).forEach((w) => box.appendChild(el('div', 'dz-warn', w)));
  if (d.error) box.appendChild(el('div', 's-bad', 'Stopped early: ' + d.error));
  if (!fo) {
    box.appendChild(el('div', 's-bad', 'No failover recording was made.'));
    box.appendChild(restoreLine(d));
    return box;
  }
  const m = fo.metrics;

  // A. verdict + tiles
  const v = FO_VERDICT[m.verdict] || ['s-dim', m.verdict];
  const vb = el('div', 'fo-verdict ' + v[0]);
  vb.appendChild(el('b', null, v[1]));
  vb.appendChild(el('span', null, m.verdict === 'failed over' ? ` in ${foSecs(m.failover_s, 2)}` :
    m.verdict === 'no impact' ? ` (${fo.times.window_s} s watched)` :
    m.verdict === 'not recovered' ? ` within the ${fo.times.window_s} s window` :
    m.verdict === 'unstable' ? `: ${m.outage_periods} separate outages` : ''));
  box.appendChild(vb);
  const tiles = el('div', 'fo-tiles');
  const tile = (label, value, note, cls) => {
    const t = el('div', 'fo-tile ' + (cls || ''));
    t.appendChild(el('div', 'fo-tile-l', label));
    t.appendChild(el('div', 'fo-tile-v', value));
    if (note) t.appendChild(el('div', 'fo-tile-n', note));
    tiles.appendChild(t);
  };
  const res_s = m.resolution_s;
  tile('Failover time', foSecs(m.failover_s, 2), `failure → first reply on the final path (±${res_s} s)`);
  tile('Outage', foSecs(m.outage_s, 1), `first lost probe → first good one after (±${res_s} s)`);
  tile('Detection', foSecs(m.detection_s, 2), 'failure → first probe that broke or moved');
  tile('Probes lost', `${m.lost} / ${m.sent_after_F}`, m.outage_periods > 1 ? `${m.outage_periods} outages` : 'after the failure',
       m.lost ? 's-bad' : 's-ok');
  tile('Path', m.path_changed ? 'changed' : 'unchanged', m.rtt_before_ms != null ?
       `RTT ${m.rtt_before_ms} → ${m.rtt_after_ms ?? '-'} ms` : '');
  tile('Answered by', `${m.answered_by[0] || '?'} → ${m.answered_by[1] || '?'}`, '');
  tile('From the click', foSecs(m.from_click_s, 1), 'includes CML\'s own stop time');
  tile('Stopped because', m.completed_by, '');
  box.appendChild(tiles);

  // B. timeline
  box.appendChild(failoverTimeline(fo, width));

  // C. topology: failover path drawn, baseline dotted, downed nodes crossed
  box.appendChild(el('h4', 'fo-h', 'Topology: before and after'));
  const base = fo.paths.find((p) => p.role.startsWith('baseline'));
  const fin = fo.paths.find((p) => p.role === 'failover') || base;
  if (fin) {
    const linkId = (u, v2) => (((BY.get(u) || {}).links || []).find((l) => l.peer === v2) || {}).link_id;
    const counts = {};
    fo.packets.forEach((p) => { if (p.t >= 0) counts[p.link_id] = (counts[p.link_id] || 0) + 1; });
    const links = [];
    for (let i = 1; i < fin.hops.length; i++) {
      const id = linkId(fin.hops[i - 1], fin.hops[i]);
      if (id) links.push({ id, a: fin.hops[i - 1], b: fin.hops[i], use: 'both', packets: counts[id] || 0 });
    }
    const synth = { path: fin.hops, links, target: fin.hops[fin.hops.length - 1], virtual_target: res.virtual_target,
                    changed: base && base !== fin ? { from: base.hops, at: null } : null };
    box.appendChild(pathDiagram(synth, { nodes: [], links: [] }, width, { down: new Set(d.nodes) }));
    box.appendChild(el('div', 's-dim', `Solid: the path after the failover (${fin.id}), packet counts after the failure. ` +
      (base && base !== fin ? `Dotted: the path before (${base.id}). ` : '') + 'Crossed out: powered off.'));
    const rp = el('button', 'default', 'Replay the failover on the topology');
    rp.addEventListener('click', () => {
      const t0 = fo.packets.length ? fo.packets[0].t : 0;
      const log = fo.packets.filter((p) => p.kind === 'echo' || p.kind === 'esp')
        .map((p) => ({ link_id: p.link_id, dir: p.dir, t_ms: (p.t - t0) * 1000 }));
      replayPackets(log, rp, box);
    });
    box.appendChild(rp);
  }

  // D. paths
  box.appendChild(el('h4', 'fo-h', 'Paths used'));
  const pt = el('table', 'g fo-table');
  const ph = el('tr');
  ['', 'role', 'hops (request)', 'from', 'to', 'probes', 'replies', 'RTT', 'answered by'].forEach((h) => ph.appendChild(el('th', null, h)));
  pt.appendChild(ph);
  fo.paths.forEach((p) => {
    const tr = el('tr', 'row');
    const sw = el('td'); const i = el('i', 'fo-swatch'); i.style.background = FO_ROLE_COLOR[p.role] || 'var(--ink3)'; sw.appendChild(i);
    sw.appendChild(document.createTextNode(' ' + p.id));
    tr.appendChild(sw);
    tr.appendChild(el('td', null, p.role));
    const hops = el('td', 'wrap', p.hops.join(' › '));
    if (p.reply_hops.length && p.reply_hops.join() !== [...p.hops].reverse().join()) {
      hops.title = 'Reply: ' + p.reply_hops.join(' › ');
      hops.appendChild(el('div', 's-dim', 'reply: ' + p.reply_hops.join(' › ')));
    }
    tr.appendChild(hops);
    tr.appendChild(el('td', null, foSecs(p.first, 2)));
    tr.appendChild(el('td', null, foSecs(p.last, 2)));
    tr.appendChild(el('td', null, String(p.probes)));
    tr.appendChild(el('td', null, String(p.replies)));
    tr.appendChild(el('td', null, p.rtt_ms != null ? `${p.rtt_ms} ms` : '-'));
    tr.appendChild(el('td', null, p.answered_by || '-'));
    pt.appendChild(tr);
  });
  box.appendChild(pt);

  // E. lost probes
  const lost = fo.probes.filter((p) => p.outcome !== 'ok');
  box.appendChild(el('h4', 'fo-h', `Lost probes (${lost.length})`));
  if (lost.length) {
    const groups = {};
    lost.forEach((p) => {
      const k = p.died_at ? `${p.outcome}|${p.died_at.into}|${p.died_at.unreachable_from || ''}` : p.outcome;
      (groups[k] = groups[k] || []).push(p);
    });
    const sum = el('div', 'fo-lostsum');
    Object.values(groups).sort((a, b) => b.length - a.length).forEach((g) => {
      const p = g[0];
      sum.appendChild(el('div', null, `${g.length} × ${foCause(p)} (probes ${g[0].key}–${g[g.length - 1].key}, ` +
        `${foSecs(g[0].sent, 2)} to ${foSecs(g[g.length - 1].sent, 2)})`));
    });
    box.appendChild(sum);
    const lt = el('table', 'g fo-table');
    const lh = el('tr');
    ['probe', 'sent', 'outcome', 'last seen on', 'heading into', 'probable cause'].forEach((h) => lh.appendChild(el('th', null, h)));
    lt.appendChild(lh);
    lost.slice(0, 400).forEach((p) => {
      const tr = el('tr', 'row');
      tr.appendChild(el('td', null, String(p.key)));
      tr.appendChild(el('td', null, foSecs(p.sent, 2)));
      tr.appendChild(el('td', p.outcome === 'unknown' ? 's-dim' : 's-bad', p.outcome));
      tr.appendChild(el('td', null, p.died_at ? `${p.died_at.link} (${foSecs(p.died_at.t, 3)})` : '-'));
      tr.appendChild(el('td', null, p.died_at ? p.died_at.into : '-'));
      tr.appendChild(el('td', 's-dim', foCause(p)));
      lt.appendChild(tr);
    });
    const wrap = el('div', 'pkt-log-wrap'); wrap.appendChild(lt);
    box.appendChild(wrap);
  }

  // F. packet log
  box.appendChild(failoverPacketLog(fo));

  // G. coverage & accuracy
  const c = fo.coverage || {};
  const g = el('div', 'fo-foot s-dim');
  g.appendChild(el('div', null, `Captured ${c.captured}/${c.in_scope} links in scope` +
    (c.missing && c.missing.length ? ` (not captured: ${c.missing.join(', ')})` : '') +
    `; ${c.with_packets ?? '?'} carried this test's packets. Armed in ${c.arm_seconds ?? '?'} s.`));
  if (c.from_snapshot && c.from_snapshot.length) {
    g.appendChild(el('div', null, `From a snapshot taken up to ~1 s before the node stopped (CML discards a stopped ` +
      `node's captures): ${c.from_snapshot.join(', ')}.`));
  }
  if (c.no_pcap && c.no_pcap.length) {
    g.appendChild(el('div', 's-warn', `No packets retrievable: ${c.no_pcap.join(', ')}.`));
  }
  g.appendChild(el('div', null, `Clock: ${fo.clock.source}. Failure moment F: ${fo.clock.F_basis}. ` +
    `Local→capture clock: ${fo.clock.local_clock_basis || 'not available'}` +
    (fo.clock.console_lag_ms != null ? ` (console lag up to ${fo.clock.console_lag_ms} ms)` : '') + '.'));
  g.appendChild(el('div', null, `Probe: every ${fo.probe.interval_s} s to ${fo.probe.dest}, ${fo.probe.sent} probes. ` +
    `Window ${fo.times.window_s} s from all-stopped; CML took ${foSecs(fo.times.T1 != null && fo.times.T0 != null ? fo.times.T1 - fo.times.T0 : null, 1)} ` +
    `to stop the nodes. F = ${fo.times.F_utc} (CML clock).`));
  if (fo.probe_error) g.appendChild(el('div', 's-warn', 'Probe: ' + fo.probe_error));
  box.appendChild(g);

  // H. exports
  if (d.run_name) {
    const ex = el('div', 'fo-exports');
    ex.appendChild(el('b', null, 'Export '));
    const base_ = '/api/failover/run/' + encodeURIComponent(d.run_name);
    [['JSON', base_], ['Packets CSV', base_ + '/packets.csv'], ['Probes CSV', base_ + '/probes.csv'],
     ['Paths CSV', base_ + '/paths.csv']].concat(d.pcaps ? [['PCAPs (Wireshark)', base_ + '/pcaps.zip']] : [])
      .forEach(([t, href]) => { const a = el('a', null, t); a.href = href; a.target = '_blank'; ex.appendChild(a); });
    ex.appendChild(el('span', 's-dim', ` saved as ${d.run_name}`));
    box.appendChild(ex);
  }
  if (d.after) {
    box.appendChild(el('h4', 'fo-h', 'Full trace after the disaster'));
    const st = el('div', 'path-status');
    if (d.after.mode === 'capture') st.appendChild(el('span', 'mode-tag verified', 'VERIFIED'));
    st.appendChild(el('span', d.after.verdict.level === 'ok' ? 's-ok' : 's-warn', d.after.verdict.text));
    box.appendChild(st);
  } else if (d.after_error) {
    box.appendChild(el('div', 's-bad', 'The full trace afterwards failed: ' + d.after_error));
  }
  box.appendChild(restoreLine(d));
  return box;
}

function foCause(p) {
  const da = p.died_at || {};
  if (p.outcome === 'unreachable') return `dropped: ${da.unreachable_from || 'a router'} sent "unreachable"`;
  if (p.outcome === 'lost-reply') return 'the target answered; the reply was lost on the way back';
  if (p.outcome === 'unknown') return 'not seen in the capture (console only)';
  if (da.into && (trace.result && (trace.result.disaster || {}).nodes || []).includes(da.into)) {
    return `sent into ${da.into}, which was powered off`;
  }
  return da.into ? `last seen heading into ${da.into}` : 'lost';
}

function restoreLine(d) {
  const r = d.restored || {};
  const bad = Object.entries(r).filter(([, v]) => v !== 'BOOTED');
  return el('div', 'dz-restore ' + (d.restore === false || bad.length ? 's-warn' : 's-ok'),
    d.restore === false ? `Left powered off, as chosen: ${d.nodes.join(', ')}.`
      : bad.length ? `Restore incomplete: ${bad.map(([k, v]) => `${k} (${v})`).join(', ')}. Use Restore in the banner.`
        : Object.keys(r).length ? `Restored: ${Object.keys(r).length} node${Object.keys(r).length === 1 ? '' : 's'} running again.` : '');
}

/* ------------------------------------------------------------ timeline */
let foZoom = 'failover';
function failoverTimeline(fo, width) {
  const wrap = el('div', 'fo-timeline');
  const head = el('div', 'fo-tl-head');
  head.appendChild(el('h4', 'fo-h', 'Timeline (seconds from the failure, F = 0)'));
  const zb = el('button', null, foZoom === 'failover' ? 'Show the whole recording' : 'Zoom to the failover');
  zb.addEventListener('click', () => { foZoom = foZoom === 'failover' ? 'all' : 'failover'; wrap.replaceWith(failoverTimeline(fo, width)); });
  head.appendChild(zb);
  wrap.appendChild(head);

  const probes = fo.probes;
  const times = probes.map((p) => p.sent).concat(fo.events.map((e) => e.t)).filter((x) => x != null);
  let lo = Math.min(...times, -1), hi = Math.max(...times, 1);
  if (foZoom === 'failover') {
    const rec = fo.events.find((e) => e.kind === 'first_reply_new');
    lo = Math.max(lo, -2);
    hi = Math.min(hi, (rec ? rec.t : Math.max(...probes.map((p) => p.sent), 2)) + 3);
  }
  const W = Math.max(480, width - 40), L = 96, R = 14;
  const x = (t) => L + ((t - lo) / (hi - lo || 1)) * (W - L - R);
  const paths = fo.paths;
  const logs = fo.device_logs || [];
  const lanes = ['Disaster', 'Probes', ...paths.map((p) => `Path ${p.id}`), 'Events'].concat(logs.length ? ['Device logs'] : []);
  const LH = 22, top = 26;
  const H = top + lanes.length * LH + 30;
  const s = svg('svg', { width: W, height: H, class: 'fo-tl' });
  // axis
  const step = (hi - lo) > 30 ? 5 : (hi - lo) > 12 ? 2 : (hi - lo) > 5 ? 1 : 0.5;
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) {
    s.appendChild(svg('line', { x1: x(t), x2: x(t), y1: top - 4, y2: H - 24, class: 'fo-grid' }));
    s.appendChild(svg('text', { x: x(t), y: top - 8, class: 'fo-axis', 'text-anchor': 'middle' }, `${+t.toFixed(1)}`));
  }
  lanes.forEach((name, i) => {
    s.appendChild(svg('text', { x: 4, y: top + i * LH + 15, class: 'fo-lane' }, name));
    s.appendChild(svg('line', { x1: L, x2: W - R, y1: top + (i + 1) * LH, y2: top + (i + 1) * LH, class: 'fo-sep' }));
  });
  const laneY = (name) => top + lanes.indexOf(name) * LH;
  const inRange = (t) => t != null && t >= lo && t <= hi;
  // disaster lane
  const dy = laneY('Disaster');
  fo.events.filter((e) => ['power_off_requested', 'node_stopped', 'all_stopped'].includes(e.kind) && inRange(e.t)).forEach((e) => {
    const g = svg('g', {});
    g.appendChild(svg('title', {}, `${foSecs(e.t, 3)}: ${e.text}`));
    g.appendChild(svg('path', { d: `M${x(e.t)} ${dy + 4} l5 7 l-5 7 l-5 -7 z`, class: 'fo-m-' + e.kind }));
    s.appendChild(g);
  });
  // F line
  s.appendChild(svg('line', { x1: x(0), x2: x(0), y1: top - 4, y2: H - 24, class: 'fo-F' }));
  s.appendChild(svg('text', { x: x(0) + 3, y: H - 12, class: 'fo-axis fo-F-t' }, 'F'));
  // probes
  const py = laneY('Probes');
  const pw = Math.max(2, ((fo.probe.interval_s) / (hi - lo)) * (W - L - R) - 1);
  const pathRole = Object.fromEntries(paths.map((p) => [p.id, p.role]));
  probes.filter((p) => inRange(p.sent)).forEach((p) => {
    const col = p.outcome === 'ok' ? (FO_ROLE_COLOR[pathRole[p.path]] || 'var(--ink3)')
      : p.outcome === 'unknown' ? 'var(--devoff)' : 'var(--bad)';
    const r = svg('rect', { x: x(p.sent), y: py + 4, width: pw, height: LH - 8, style: `fill:${col}`,
                            class: p.outcome === 'unreachable' ? 'fo-unreach' : '' });
    r.appendChild(svg('title', {}, `probe ${p.key} at ${foSecs(p.sent, 3)}: ${p.outcome}` +
      (p.path ? `, path ${p.path}` : '') + (p.rtt_ms != null ? `, ${p.rtt_ms} ms` : '') +
      (p.died_at ? ` - last seen ${p.died_at.link} → ${p.died_at.into}` : '')));
    s.appendChild(r);
  });
  // paths
  paths.forEach((p) => {
    const y = laneY(`Path ${p.id}`);
    const a = Math.max(lo, p.first), b = Math.min(hi, p.last + fo.probe.interval_s);
    if (b < lo || a > hi) return;
    const r = svg('rect', { x: x(a), y: y + 6, width: Math.max(3, x(b) - x(a)), height: LH - 12,
                            style: `fill:${FO_ROLE_COLOR[p.role] || 'var(--ink3)'}`, rx: 2 });
    r.appendChild(svg('title', {}, `${p.id} (${p.role}): ${p.hops.join(' › ')}`));
    s.appendChild(r);
    s.appendChild(svg('text', { x: Math.min(x(b) + 4, W - R - 160), y: y + 15, class: 'fo-ptxt' },
      `${p.role}: …${p.hops.slice(-3).join(' › ')}`));
  });
  // events
  const ey = laneY('Events');
  fo.events.filter((e) => !['power_off_requested', 'node_stopped', 'all_stopped'].includes(e.kind) && inRange(e.t))
    .forEach((e) => {
      const g = svg('g', {});
      g.appendChild(svg('title', {}, `${foSecs(e.t, 3)}: ${e.text}`));
      g.appendChild(svg('circle', { cx: x(e.t), cy: ey + LH / 2, r: e.kind === 'ike' ? 2.5 : 4.5, class: 'fo-ev fo-ev-' + e.kind }));
      s.appendChild(g);
    });
  if (logs.length) {
    const ly = laneY('Device logs');
    logs.filter((l) => inRange(l.t)).forEach((l) => {
      const g = svg('g', {});
      g.appendChild(svg('title', {}, `${foSecs(l.t, 2)} ${l.node}: ${l.code} ${l.text}${l.meaning ? '\n' + l.meaning : ''}`));
      g.appendChild(svg('rect', { x: x(l.t) - 2, y: ly + 5, width: 4, height: LH - 10, class: 'fo-log' }));
      s.appendChild(g);
    });
  }
  const sc = el('div', 'fo-tl-scroll'); sc.appendChild(s);
  wrap.appendChild(sc);
  const key = el('div', 'fo-key s-dim');
  key.innerHTML = '';
  [['var(--sel)', 'baseline path'], ['var(--ok)', 'failover path'], ['var(--warn)', 'transient path'],
   ['var(--bad)', 'lost / unreachable'], ['var(--devoff)', 'not captured']].forEach(([c, t]) => {
    const i = el('i', 'fo-swatch'); i.style.background = c; key.appendChild(i); key.appendChild(document.createTextNode(` ${t}  `));
  });
  key.appendChild(document.createTextNode('◆ CML stop events · ● network events (hover for details) · red line: the failure, F'));
  wrap.appendChild(key);
  // event list (readable, not just markers)
  const evl = el('div', 'fo-events');
  fo.events.filter((e) => e.kind !== 'ike').forEach((e) => evl.appendChild(el('div', null, `${foSecs(e.t, 3).padStart(9)}  ${e.text}`)));
  const ike = fo.events.filter((e) => e.kind === 'ike').length;
  if (ike) evl.appendChild(el('div', 's-dim', `(+ ${ike} IKE packets - tunnel keepalives or renegotiation - on the timeline)`));
  logs.forEach((l) => evl.appendChild(el('div', 's-dim', `${foSecs(l.t, 2).padStart(9)}  ${l.node}: ${l.code} ${l.text}`)));
  wrap.appendChild(evl);
  return wrap;
}

/* ----------------------------------------------------------- packet log */
function failoverPacketLog(fo) {
  const box = el('div', 'fo-pkts');
  box.appendChild(el('h4', 'fo-h', `Packet log (${fo.packets.length} packets of this test)`));
  const f = { kind: 'all', phase: 'around', text: '' };
  const bar = el('div', 'dz-row');
  const mk = (label, opts, key) => {
    const l = el('label', null, label + ' ');
    const s = el('select');
    opts.forEach(([v, t]) => { const o = el('option', null, t); o.value = v; s.appendChild(o); });
    s.value = f[key];
    s.addEventListener('change', () => { f[key] = s.value; draw(); });
    l.appendChild(s); bar.appendChild(l);
  };
  mk('Show', [['around', 'around the failure and recovery (±2 s)'], ['all', 'everything']], 'phase');
  mk('Kind', [['all', 'all'], ['echo', 'ping requests/replies'], ['esp', 'ESP (VPN)'], ['unreach', 'ICMP errors'], ['ike', 'IKE']], 'kind');
  const q = el('input'); q.type = 'search'; q.placeholder = 'Filter link, node or probe';
  q.addEventListener('input', () => { f.text = q.value.toLowerCase(); draw(); });
  bar.appendChild(q);
  box.appendChild(bar);
  const wrap = el('div', 'pkt-log-wrap');
  box.appendChild(wrap);
  const rec = (fo.events.find((e) => e.kind === 'first_reply_new') || {}).t;
  const near = (t) => Math.abs(t) <= 2 || (rec != null && Math.abs(t - rec) <= 2);
  const draw = () => {
    const rows = fo.packets.filter((p) => (f.phase === 'all' || near(p.t)) && (f.kind === 'all' || p.kind === f.kind) &&
      (!f.text || `${p.a} ${p.b} ${p.probe ?? ''} ${p.seq ?? ''}`.toLowerCase().includes(f.text)));
    wrap.innerHTML = '';
    const t = el('table', 'g fo-table');
    const h = el('tr');
    ['t (s from F)', 'link', '', 'kind', 'probe / SPI', 'src → dst', 'note'].forEach((x) => h.appendChild(el('th', null, x)));
    t.appendChild(h);
    rows.slice(0, 3000).forEach((p) => {
      const tr = el('tr', 'row pkt-row ' + (p.dir === 'request' ? 'request' : p.dir === 'reply' ? 'reply' : p.dir));
      tr.appendChild(el('td', null, p.t.toFixed(4)));
      tr.appendChild(el('td', null, `${p.a} (${p.ai}) – ${p.b} (${p.bi})`));
      tr.appendChild(el('td', null, p.dir === 'request' ? '→' : p.dir === 'reply' ? '←' : p.dir === 'esp' ? '⇄' : '!'));
      tr.appendChild(el('td', null, p.kind === 'echo' ? `ping ${p.dir}` : p.kind));
      tr.appendChild(el('td', null, p.kind === 'esp' ? `${p.spi}` : p.probe != null ? String(p.probe) : ''));
      tr.appendChild(el('td', null, `${p.src} → ${p.dst}`));
      tr.appendChild(el('td', 's-dim', p.info || ''));
      t.appendChild(tr);
    });
    wrap.appendChild(t);
    if (rows.length > 3000) wrap.appendChild(el('div', 's-dim', `Showing 3000 of ${rows.length}; export the CSV for all.`));
    if (!rows.length) wrap.appendChild(el('div', 's-dim', 'No packets match.'));
  };
  draw();
  return box;
}
