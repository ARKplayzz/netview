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
  const rl = el('label', null, 'Repeat ');
  const rs = el('select'); rs.id = 'dz-repeat';
  [1, 3, 5].forEach((v) => { const o = el('option', null, v === 1 ? 'once' : `${v} times`); o.value = v; rs.appendChild(o); });
  rs.value = D.repeat || 1;
  rs.addEventListener('change', () => { D.repeat = +rs.value; D.confirming = false; });
  rl.appendChild(rs);
  rl.title = 'Run the whole test (baseline, failure, recording, restore) this many times, with a 45 s pause ' +
             'between runs, and show every run layered on one timeline with the average drawn dark.';
  row.appendChild(rl);
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
  box.appendChild(failoverChecksEditor(D));
  const fam = (BY.get(trace.source) || {}).probe_family;
  box.appendChild(el('div', 's-dim', `Probe: ${fam === 'linux' || fam === 'windows' ? '5 per second (0.2 s resolution)' :
    'one per second (1 s resolution: IOS/ASA ping cannot set an interval)'} from ${trace.source || 'the source'}. ` +
    'Uses Verified (packet capture) mode. Runs are saved and listed below.'));
  box.appendChild(failoverHistory());
  return box;
}

/* ------------------------------------------------------ service checks */
const FO_CHECK_KIND = { dns: 'DNS lookup', tcp: 'TCP connect', dhcp: 'DHCP lease' };

function failoverChecksEditor(D) {
  D.checks = D.checks || [];
  const box = el('div', 'fo-checks');
  const head = el('div', 'dz-row');
  head.appendChild(el('b', null, 'Service checks'));
  head.appendChild(el('span', 's-dim', ' Prove services, not just routing: each check runs before, during and after ' +
    'the failure and gets its own row on the timeline.'));
  box.appendChild(head);
  const eligible = (kind) => NODES.filter((n) => !n.ghost && n.up &&
    (kind === 'dhcp' ? ['windows', 'ios'].includes(n.probe_family) : n.probe_family === 'windows'))
    .map((n) => n.label).sort();
  const redraw = () => box.replaceWith(failoverChecksEditor(D));
  D.checks.forEach((c, i) => {
    const row = el('div', 'dz-row fo-check');
    row.appendChild(el('span', 'fo-check-k', FO_CHECK_KIND[c.kind]));
    const fl = el('label', null, 'from ');
    const fs = el('select'); fs.id = `dz-chk-${i}-from`;
    const opts = eligible(c.kind);
    if (!opts.length) fs.appendChild(el('option', null, c.kind === 'dhcp' ? '(no Windows PC or switch running)' : '(no Windows PC with the bridge running)'));
    opts.forEach((x) => { const o = el('option', null, x); o.value = x; fs.appendChild(o); });
    if (!c.from || !opts.includes(c.from)) c.from = opts.includes(trace.source) ? trace.source : opts[0];
    fs.value = c.from || '';
    fs.addEventListener('change', () => { c.from = fs.value; D.confirming = false; redraw(); });
    fl.appendChild(fs); row.appendChild(fl);
    const inp = (key, ph, w) => {
      const f = el('input'); f.id = `dz-chk-${i}-${key}`; f.placeholder = ph; f.value = c[key] ?? ''; f.style.width = w;
      f.setAttribute('aria-label', ph);
      f.addEventListener('input', () => { c[key] = f.value.trim(); D.confirming = false; });
      row.appendChild(f);
    };
    if (c.kind === 'dns') { inp('name', 'name to look up', '15em'); inp('server', 'server (blank: the PC\'s own)', '13em'); }
    if (c.kind === 'tcp') { inp('host', 'host or address', '12em'); inp('port', 'port', '5em'); }
    if (c.kind === 'dhcp' && (BY.get(c.from) || {}).probe_family === 'ios') inp('iface', 'DHCP-client interface, e.g. Vlan60', '15em');
    const rm = el('button', 'linkish', '×'); rm.title = 'Remove this check'; rm.setAttribute('aria-label', 'Remove this check');
    rm.addEventListener('click', () => { D.checks.splice(i, 1); D.confirming = false; redraw(); });
    row.appendChild(rm);
    box.appendChild(row);
  });
  const add = el('div', 'dz-row');
  Object.entries(FO_CHECK_KIND).forEach(([k, t]) => {
    const b = el('button', null, `+ ${t}`); b.id = `dz-chk-add-${k}`;
    b.addEventListener('click', () => {
      const c = { kind: k };
      if (k === 'tcp') { c.host = trace.ip || ''; c.port = '445'; }
      if (k === 'dns') { const t = BY.get(trace.target) || {}; c.name = t.fqdn || (t.virtual ? trace.target : ''); }
      D.checks.push(c); D.confirming = false; redraw();
    });
    add.appendChild(b);
  });
  if (!eligible('dns').length) {
    add.appendChild(el('span', 's-dim', ' DNS and TCP checks run from a Windows PC with NetView\'s bridge installed.'));
  }
  box.appendChild(add);
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
      tr.appendChild(el('td', null, `${r.source} → ${r.target}` + (r.series ? ` (×${r.series}, average)` : '')));
      tr.appendChild(el('td', 'wrap', (r.nodes || []).join(', ')));
      const v = FO_VERDICT[r.verdict] || ['s-dim', r.verdict || '?'];
      tr.appendChild(el('td', v[0], v[1]));
      const av = (k) => (r.avg && r.avg[k] ? r.avg[k].mean : null);
      tr.appendChild(el('td', null, foSecs(r.series ? av('failover_s') : r.failover_s, 2)));
      tr.appendChild(el('td', null, foSecs(r.series ? av('outage_s') : r.outage_s, 1)));
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
  box.appendChild(el('div', 'dz-title', `Failover capture: ${dzWhat(d)}`));
  (d.warnings || []).forEach((w) => box.appendChild(el('div', 'dz-warn', w)));
  if (d.error) box.appendChild(el('div', 's-bad', 'Stopped early: ' + d.error));
  if (!fo) {
    box.appendChild(el('div', 's-bad', 'No failover recording was made.'));
    box.appendChild(restoreLine(d));
    return box;
  }
  const m = fo.metrics;
  const ser = d.series;

  // A. verdict + tiles (a repeated test: the averages, then one row per run)
  if (ser) box.appendChild(failoverSeriesHead(ser, fo));
  const v = FO_VERDICT[m.verdict] || ['s-dim', m.verdict];
  const vb = el('div', 'fo-verdict ' + v[0]);
  vb.appendChild(el('b', null, v[1]));
  vb.appendChild(el('span', null, m.verdict === 'failed over' ? ` in ${foSecs(m.failover_s, 2)}` :
    m.verdict === 'no impact' ? ` (${fo.times.window_s} s watched)` :
    m.verdict === 'not recovered' ? ` within the ${fo.times.window_s} s window` :
    m.verdict === 'unstable' ? `: ${m.outage_periods} separate outages` : ''));
  if (m.blips) vb.appendChild(el('span', 's-dim', ` · ${m.blips} isolated lost probe${m.blips > 1 ? 's' : ''} after recovery`));
  if (!ser) box.appendChild(vb);
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
  if (!ser) box.appendChild(tiles);

  // B. timeline (every run layered when repeated)
  box.appendChild(failoverTimeline(fo, width, ser));
  if ((fo.services || []).length) box.appendChild(ser ? failoverSeriesServices(ser) : failoverServices(fo));
  if (ser) {
    box.appendChild(el('h4', 'fo-h fo-rep-h', `Run ${ser.representative} in detail (the run nearest the average)`));
    box.appendChild(vb);
    box.appendChild(tiles);
  }

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
    box.appendChild(pathDiagram(synth, { nodes: [], links: [] }, width, { down: new Set(d.nodes), cut: dzCut(d) }));
    box.appendChild(el('div', 's-dim', `Solid: the path after the failover (${fin.id}), packet counts after the failure. ` +
      (base && base !== fin ? `Dotted: the path before (${base.id}). ` : '') + 'Crossed out: powered off.' +
      (dzCut(d).length ? ' Red dashed: cut cable.' : '')));
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
  const bad = Object.entries(r).filter(([, v]) => v !== 'BOOTED' && v !== 'STARTED');
  return el('div', 'dz-restore ' + (d.restore === false || bad.length ? 's-warn' : 's-ok'),
    d.restore === false ? `Left powered off, as chosen: ${d.nodes.join(', ')}.`
      : bad.length ? `Restore incomplete: ${bad.map(([k, v]) => `${k} (${v})`).join(', ')}. Use Restore in the banner.`
        : Object.keys(r).length ? `Restored: ${Object.keys(r).length} item${Object.keys(r).length === 1 ? '' : 's'} back in service.` : '');
}

/* ------------------------------------------------------------ timeline */
// Path lanes are one per ROLE, not one per path (2026-10-08): a failover that
// wandered through four short-lived paths used to take four rows; they share
// the "Transient paths" row now, each bar still naming its own hops on hover.
const FO_LANE_ORDER = ['Baseline path', 'Transient paths', 'Failover path'];
const foRoleLane = (role) => (role || '').startsWith('baseline') ? 'Baseline path'
  : role === 'transient' ? 'Transient paths' : role === 'failover' ? 'Failover path' : `Path (${role})`;
const foMean = (st) => (st && st.mean != null ? st.mean : null);
const foRange = (st, dp = 2) => (st ? `${foSecs(st.mean, dp)} (${foSecs(st.min, dp)} – ${foSecs(st.max, dp)}, ${st.n} runs)` : '-');

let foZoom = 'failover';
function failoverTimeline(fo, width, series) {
  const wrap = el('div', 'fo-timeline');
  const head = el('div', 'fo-tl-head');
  head.appendChild(el('h4', 'fo-h', series
    ? `Timeline: ${series.runs.length} runs layered (seconds from each run's failure, F = 0)`
    : 'Timeline (seconds from the failure, F = 0)'));
  const zb = el('button', null, foZoom === 'failover' ? 'Show the whole recording' : 'Zoom to the failover');
  zb.addEventListener('click', () => { foZoom = foZoom === 'failover' ? 'all' : 'failover'; wrap.replaceWith(failoverTimeline(fo, width, series)); });
  head.appendChild(zb);
  wrap.appendChild(head);

  // Repeat (2026-10-08): every run is drawn faintly on the same rows, so where
  // the runs agree the colour builds up; the average is drawn dark on top.
  const runs = series ? series.runs : [{ n: 1, probes: fo.probes, paths: fo.paths, services: fo.services || [],
                                         events: fo.events, interval_s: fo.probe.interval_s }];
  const alpha = series ? Math.max(0.2, 0.75 / runs.length) : 1;
  const avg = series ? series.avg : null;
  const svcAvg = Object.fromEntries(((series || {}).services || []).map((v) => [v.id, v]));

  const allProbes = runs.flatMap((r) => r.probes);
  const allEvents = runs.flatMap((r) => r.events || []).concat(fo.events);
  // Service checks can outlast the probe (a source's own DHCP renewal runs after it).
  const svcTimes = runs.flatMap((r) => (r.services || []).flatMap((v) => v.attempts.flatMap((a) => [a.t, a.t_bound, a.t_done])));
  const times = allProbes.map((p) => p.sent).concat(allEvents.map((e) => e.t), svcTimes).filter((x) => x != null);
  let lo = Math.min(...times, -1), hi = Math.max(...times, 1);
  if (foZoom === 'failover') {
    const recs = allEvents.filter((e) => e.kind === 'first_reply_new').map((e) => e.t);
    lo = Math.max(lo, -2);
    hi = Math.min(hi, (recs.length ? Math.max(...recs) : Math.max(...allProbes.map((p) => p.sent), 2)) + 3);
  }
  const svcs = fo.services || [];
  const W = Math.max(480, width - 40), L = svcs.length ? 230 : 120, R = 14;
  const x = (t) => L + ((t - lo) / (hi - lo || 1)) * (W - L - R);
  const logs = fo.device_logs || [];
  // Lanes are keyed by check id (labels can share a long prefix - 2026-10-07 three
  // DNS checks collapsed onto one lane); the text shown is a short form.
  const svcLane = (v) => `svc:${v.id}`;
  const svcText = Object.fromEntries(svcs.map((v) => [svcLane(v), (v.kind === 'dns'
    ? v.label.replace(/^DNS \S+ via /, 'DNS via ').replace(/^DNS \S+ \(PC's own resolver\)$/, 'DNS (PC resolver)')
    : v.kind === 'tcp' ? v.label.replace(/^TCP /, '').replace(/ \(SMB\)$/, '').replace(/^/, v.label.includes('(SMB)') ? 'SMB ' : 'TCP ')
      : `DHCP ${v.from}`).slice(0, 32)]));
  const pathLanes = [...new Set(runs.flatMap((r) => r.paths.map((p) => foRoleLane(p.role))))]
    .sort((a, b) => (FO_LANE_ORDER.indexOf(a) + 1 || 9) - (FO_LANE_ORDER.indexOf(b) + 1 || 9));
  const lanes = ['Disaster', 'Probes', ...pathLanes, ...svcs.map(svcLane), 'Events']
    .concat(logs.length ? ['Device logs'] : []);
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
    s.appendChild(svg('text', { x: 4, y: top + i * LH + 15, class: 'fo-lane' }, svcText[name] || name));
    s.appendChild(svg('line', { x1: L, x2: W - R, y1: top + (i + 1) * LH, y2: top + (i + 1) * LH, class: 'fo-sep' }));
  });
  const laneY = (name) => top + lanes.indexOf(name) * LH;
  const inRange = (t) => t != null && t >= lo && t <= hi;
  const runTag = (r) => (series ? `run ${r.n}: ` : '');
  // disaster lane (the representative run's CML events)
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
  // probes, every run
  const py = laneY('Probes');
  runs.forEach((r) => {
    const iv = r.interval_s || fo.probe.interval_s;
    const pw = Math.max(2, (iv / (hi - lo)) * (W - L - R) - 1);
    const pathRole = Object.fromEntries(r.paths.map((p) => [p.id, p.role]));
    r.probes.filter((p) => inRange(p.sent)).forEach((p) => {
      const col = p.outcome === 'ok' ? (FO_ROLE_COLOR[pathRole[p.path]] || 'var(--ink3)')
        : p.outcome === 'unknown' ? 'var(--devoff)' : 'var(--bad)';
      const rc = svg('rect', { x: x(p.sent), y: py + 4, width: pw, height: LH - 8, style: `fill:${col};fill-opacity:${alpha}`,
                               class: p.outcome === 'unreachable' ? 'fo-unreach' : '' });
      rc.appendChild(svg('title', {}, `${runTag(r)}probe ${p.key} at ${foSecs(p.sent, 3)}: ${p.outcome}` +
        (p.path ? `, path ${p.path}` : '') + (p.rtt_ms != null ? `, ${p.rtt_ms} ms` : '') +
        (p.died_at ? ` - last seen ${p.died_at.link} → ${p.died_at.into}` : '')));
      s.appendChild(rc);
    });
  });
  // paths: one row per role, every run's bars on it
  runs.forEach((r) => {
    const iv = r.interval_s || fo.probe.interval_s;
    r.paths.forEach((p) => {
      const y = laneY(foRoleLane(p.role));
      const a = Math.max(lo, p.first), b = Math.min(hi, p.last + iv);
      if (b < lo || a > hi) return;
      const rc = svg('rect', { x: x(a), y: y + 6, width: Math.max(3, x(b) - x(a)), height: LH - 12,
                               style: `fill:${FO_ROLE_COLOR[p.role] || 'var(--ink3)'};fill-opacity:${alpha}`, rx: 2 });
      rc.appendChild(svg('title', {}, `${runTag(r)}${p.id} (${p.role}) ${foSecs(p.first, 2)} → ${foSecs(p.last, 2)}: ${p.hops.join(' › ')}`));
      s.appendChild(rc);
    });
  });
  // one label per path row, from the representative run
  pathLanes.forEach((lane) => {
    const ps = fo.paths.filter((p) => foRoleLane(p.role) === lane);
    if (!ps.length) return;
    const end = Math.min(hi, Math.max(...ps.map((p) => p.last + fo.probe.interval_s)));
    const txt = lane === 'Transient paths'
      ? `${ps.length} transient path${ps.length > 1 ? 's' : ''} (${ps.map((p) => p.id).join(', ')}): hover a bar for its hops`
      : `${ps.map((p) => p.id).join(', ')}: …${ps[ps.length - 1].hops.slice(-3).join(' › ')}`;
    s.appendChild(svg('text', { x: Math.min(x(end) + 4, W - R - 260), y: laneY(lane) + 15, class: 'fo-ptxt' }, txt));
  });
  // service checks: one square per attempt (DNS/TCP), one bar per renewal (DHCP), every run
  const svcY = Object.fromEntries(svcs.map((v) => [v.id, laneY(svcLane(v))]));
  runs.forEach((r) => (r.services || []).forEach((v) => {
    const y = svcY[v.id];
    if (y == null) return;
    v.attempts.forEach((a) => {
      if (v.kind === 'dhcp') {
        const a0 = a.t_released ?? a.t, a1 = a.t_bound ?? a.t_done ?? a.t;
        if (a1 < lo || a0 > hi) return;
        const rc = svg('rect', { x: x(Math.max(lo, a0)), y: y + 5, width: Math.max(4, x(Math.min(hi, a1)) - x(Math.max(lo, a0))),
                                 height: LH - 10, rx: 2, style: `fill:${a.ok ? 'var(--ok)' : 'var(--bad)'};fill-opacity:${alpha}` });
        rc.appendChild(svg('title', {}, `${runTag(r)}DHCP ${a.phase || ''} ${foSecs(a0, 2)} → ${foSecs(a1, 2)}: ` +
          (a.ok ? `${a.address} from ${a.server}` : (a.error || 'no lease'))));
        s.appendChild(rc);
        if (!series && a.ok && inRange(a1)) s.appendChild(svg('text', { x: Math.min(x(a1) + 4, W - R - 150), y: y + 15, class: 'fo-ptxt' }, `${a.address} from ${a.server}`));
        return;
      }
      if (!inRange(a.t)) return;
      const w = Math.max(3, (1 / (hi - lo)) * (W - L - R) - 2);
      const rc = svg('rect', { x: x(a.t), y: y + 5, width: w, height: LH - 10,
                               style: `fill:${a.ok ? 'var(--ok)' : 'var(--bad)'};fill-opacity:${alpha}` });
      rc.appendChild(svg('title', {}, `${runTag(r)}${v.label} at ${foSecs(a.t, 2)}: ${a.ok ? 'ok' : 'FAILED'} (${a.ms} ms)` +
        (a.answer ? ` → ${a.answer}` : '') + (a.error ? ` ${a.error}` : '')));
      s.appendChild(rc);
    });
  }));
  // the average, dark: the outage band and the recovery marker
  if (series) {
    const band = (y, t0, t1, tip) => {
      if (t0 == null || t1 == null || t1 < lo || t0 > hi) return;
      const rc = svg('rect', { x: x(Math.max(lo, t0)), y: y + LH - 7, width: Math.max(3, x(Math.min(hi, t1)) - x(Math.max(lo, t0))),
                               height: 5, class: 'fo-avg-band' });
      rc.appendChild(svg('title', {}, tip));
      s.appendChild(rc);
    };
    const mark = (y0, y1, t, label, tip) => {
      if (!inRange(t)) return;
      const g = svg('g', {});
      g.appendChild(svg('title', {}, tip));
      g.appendChild(svg('line', { x1: x(t), x2: x(t), y1: y0 + 2, y2: y1 - 2, class: 'fo-avg-line' }));
      if (label) g.appendChild(svg('text', { x: x(t) + 3, y: y0 + 12, class: 'fo-avg-t' }, label));
      s.appendChild(g);
    };
    band(py, foMean(avg.loss_start), foMean(avg.loss_end), `Average outage: first lost probe ${foRange(avg.loss_start)} → ` +
      `first answered ${foRange(avg.loss_end)}`);
    const lastPath = pathLanes.length ? laneY(pathLanes[pathLanes.length - 1]) + LH : py + LH;
    mark(py, lastPath, foMean(avg.failover_s), `avg ${foSecs(foMean(avg.failover_s), 2)}`,
         `Average failover (first reply on the final path): ${foRange(avg.failover_s)}`);
    svcs.forEach((v) => {
      const sa = svcAvg[v.id];
      if (!sa) return;
      const y = svcY[v.id];
      if (v.kind === 'dhcp') {
        if (sa.t_bound) mark(y, y + LH, foMean(sa.t_bound), `avg ${foSecs(foMean(sa.t_bound), 1)}`, `Average time the lease was bound after the failure: ${foRange(sa.t_bound, 1)}`);
        return;
      }
      if (!sa.impacted) {
        s.appendChild(svg('text', { x: W - R - 4, y: y + 15, class: 'fo-avg-t', 'text-anchor': 'end' }, `no impact in ${sa.runs.length} runs`));
        return;
      }
      band(y, foMean(sa.first_fail), foMean(sa.recovered_at), `${v.label}, average outage ${foRange(sa.down_s)} in ` +
        `${sa.impacted} of ${sa.runs.length} runs; back at ${foRange(sa.recovered_at)}`);
      mark(y, y + LH, foMean(sa.recovered_at), `avg ${foSecs(foMean(sa.recovered_at), 1)}`,
           `${v.label}: back on average at ${foRange(sa.recovered_at)}`);
    });
  }
  // events
  const ey = laneY('Events');
  (series ? runs.flatMap((r) => (r.events || []).map((e) => ({ ...e, run: r.n }))) : fo.events)
    .filter((e) => !['power_off_requested', 'node_stopped', 'all_stopped', 'end'].includes(e.kind) && inRange(e.t))
    .forEach((e) => {
      const g = svg('g', {});
      g.appendChild(svg('title', {}, `${e.run ? `run ${e.run}: ` : ''}${foSecs(e.t, 3)}: ${e.text}`));
      g.appendChild(svg('circle', { cx: x(e.t), cy: ey + LH / 2, r: e.kind === 'ike' ? 2.5 : 4.5,
                                    class: 'fo-ev fo-ev-' + e.kind, style: series ? `opacity:${Math.max(alpha, 0.35)}` : '' }));
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
  [['var(--sel)', 'baseline path'], ['var(--ok)', 'failover path'], ['var(--warn)', 'transient path'],
   ['var(--bad)', 'lost / unreachable'], ['var(--devoff)', 'not captured']].forEach(([c, t]) => {
    const i = el('i', 'fo-swatch'); i.style.background = c; key.appendChild(i); key.appendChild(document.createTextNode(` ${t}  `));
  });
  if (series) {
    const i = el('i', 'fo-swatch fo-avg-swatch'); key.appendChild(i);
    key.appendChild(document.createTextNode(` average of the ${runs.length} runs (outage band + recovery line)  `));
  }
  key.appendChild(document.createTextNode('◆ CML stop events · ● network events (hover for details) · red line: the failure, F' +
    (series ? ` · each run drawn faintly; colour builds up where they agree · CML events and device logs: run ${series.representative}` : '')));
  wrap.appendChild(key);
  // event list (readable, not just markers)
  const evl = el('div', 'fo-events');
  if (series) evl.appendChild(el('div', 's-dim', `Events and device logs below: run ${series.representative} (the run nearest the average).`));
  fo.events.filter((e) => e.kind !== 'ike').forEach((e) => evl.appendChild(el('div', null, `${foSecs(e.t, 3).padStart(9)}  ${e.text}`)));
  const ike = fo.events.filter((e) => e.kind === 'ike').length;
  if (ike) evl.appendChild(el('div', 's-dim', `(+ ${ike} IKE packets - tunnel keepalives or renegotiation - on the timeline)`));
  logs.forEach((l) => evl.appendChild(el('div', 's-dim', `${foSecs(l.t, 2).padStart(9)}  ${l.node}: ${l.code} ${l.text}`)));
  wrap.appendChild(evl);
  return wrap;
}

/* ------------------------------------------------- repeated runs (series) */
// Repeat (2026-10-08): the same test N times. The head shows the average of
// each measurement with its spread, then one row per run.
function failoverSeriesHead(ser, fo) {
  const box = el('div', 'fo-series');
  const a = ser.avg;
  const vb = el('div', 'fo-verdict ' + (Object.keys(ser.verdicts).every((k) => k === 'failed over' || k === 'no impact') ? 's-ok' : 's-bad'));
  vb.appendChild(el('b', null, `${ser.runs.length} RUNS`));
  vb.appendChild(el('span', null, ': ' + Object.entries(ser.verdicts).map(([k, n]) => `${(FO_VERDICT[k] || [0, k])[1]} ×${n}`).join(', ') +
    (foMean(a.failover_s) != null ? ` · average failover ${foSecs(foMean(a.failover_s), 2)}` : '')));
  if (ser.asked > ser.runs.length) vb.appendChild(el('span', 's-warn', ` · ${ser.asked} asked for`));
  box.appendChild(vb);
  const tiles = el('div', 'fo-tiles');
  const tile = (label, st, dp, note, cls) => {
    const t = el('div', 'fo-tile ' + (cls || ''));
    t.appendChild(el('div', 'fo-tile-l', label));
    t.appendChild(el('div', 'fo-tile-v', st ? (dp == null ? `${+st.mean.toFixed(1)}` : foSecs(st.mean, dp)) : '-'));
    t.appendChild(el('div', 'fo-tile-n', st ? `range ${dp == null ? `${st.min}–${st.max}` : `${foSecs(st.min, dp)} – ${foSecs(st.max, dp)}`}` +
      (note ? ` · ${note}` : '') : note || ''));
    tiles.appendChild(t);
  };
  tile('Failover time (avg)', a.failover_s, 2, 'failure → first reply on the final path');
  tile('Outage (avg)', a.outage_s, 1, 'first lost → first good probe');
  tile('Detection (avg)', a.detection_s, 2, 'failure → first probe that broke or moved');
  tile('Probes lost (avg)', a.lost, null, `of about ${Math.round(foMean(a.sent_after_F) || 0)} after F`, foMean(a.lost) ? 's-bad' : 's-ok');
  tile('From the click (avg)', a.from_click_s, 1, 'includes CML\'s own stop time');
  box.appendChild(tiles);
  const t = el('table', 'g fo-table');
  const h = el('tr');
  ['run', 'verdict', 'failover', 'outage', 'lost', 'answered by', 'stopped because', ''].forEach((x) => h.appendChild(el('th', null, x)));
  t.appendChild(h);
  ser.runs.forEach((r) => {
    const m = r.metrics || {};
    const tr = el('tr', 'row' + (r.n === ser.representative ? ' fo-rep' : ''));
    tr.appendChild(el('td', null, `${r.n}${r.n === ser.representative ? ' (shown in detail)' : ''}`));
    const v = FO_VERDICT[m.verdict] || ['s-dim', m.verdict || r.error || '?'];
    tr.appendChild(el('td', v[0], v[1]));
    tr.appendChild(el('td', null, foSecs(m.failover_s, 2)));
    tr.appendChild(el('td', null, foSecs(m.outage_s, 1)));
    tr.appendChild(el('td', null, m.sent_after_F ? `${m.lost}/${m.sent_after_F}` : '-'));
    tr.appendChild(el('td', null, (m.answered_by || []).join(' → ') || '-'));
    tr.appendChild(el('td', 's-dim', m.completed_by || r.error || '-'));
    const act = el('td');
    if (r.run_name) {
      const op = el('button', 'linkish', 'Open');
      op.addEventListener('click', async () => {
        const run = await getJSON('/api/failover/run/' + encodeURIComponent(r.run_name));
        trace.history.push(run); trace.result = run; renderPathBody();
      });
      act.appendChild(op);
    }
    tr.appendChild(act);
    t.appendChild(tr);
  });
  box.appendChild(t);
  return box;
}

function failoverSeriesServices(ser) {
  const box = el('div', 'fo-svc');
  box.appendChild(el('h4', 'fo-h', `Service checks (${ser.runs.length} runs)`));
  const t = el('table', 'g fo-table');
  const h = el('tr');
  ['Check', 'Impacted', 'Outage (avg, range)', 'Back at (avg, range)', 'Each run'].forEach((x) => h.appendChild(el('th', null, x)));
  t.appendChild(h);
  ser.services.forEach((v) => {
    const tr = el('tr', 'row');
    tr.appendChild(el('td', null, v.label));
    if (v.kind === 'dhcp') {
      const ok = v.runs.filter((x) => x.ok).length;
      tr.appendChild(el('td', ok === v.runs.length ? 's-ok' : 's-bad', `leased ${ok}/${v.runs.length}`));
      tr.appendChild(el('td', null, '-'));
      tr.appendChild(el('td', null, foRange(v.t_bound, 1)));
      tr.appendChild(el('td', 's-dim', v.runs.map((x) => `${x.n}: ${x.ok ? `${foSecs(x.t_bound, 1)} from ${x.server}` : 'no lease'}`).join(' · ')));
    } else {
      tr.appendChild(el('td', v.impacted ? 's-warn' : 's-ok', `${v.impacted} of ${v.runs.length}`));
      tr.appendChild(el('td', null, v.impacted ? foRange(v.down_s, 1) : 'none'));
      tr.appendChild(el('td', null, v.impacted ? foRange(v.recovered_at, 1) : 'no impact'));
      tr.appendChild(el('td', 's-dim', v.runs.map((x) => `${x.n}: ${x.down_s ? `out ${foSecs(x.down_s, 1)}, back ${foSecs(x.recovered_at, 1)}` : (x.verdict || '-')}`).join(' · ')));
    }
    t.appendChild(tr);
  });
  box.appendChild(t);
  box.appendChild(el('div', 's-dim', 'Outage: first failed attempt → first of three good ones in a row. Back at: seconds from each run\'s F.'));
  return box;
}

/* ------------------------------------------------------ services table */
function failoverServices(fo) {
  const box = el('div', 'fo-svc');
  box.appendChild(el('h4', 'fo-h', 'Service checks'));
  const t = el('table', 'g fo-table');
  const h = el('tr');
  ['Check', 'From', 'Before F', 'After F', 'Outage', 'Back at', 'Answered by (packet capture)'].forEach((x) => h.appendChild(el('th', null, x)));
  t.appendChild(h);
  const who = (m) => Object.entries(m || {}).map(([k, n]) => `${k} (${n})`).join(', ') || '-';
  fo.services.forEach((v) => {
    const s = v.summary;
    const tr = el('tr');
    tr.appendChild(el('td', null, v.label));
    tr.appendChild(el('td', null, v.from));
    if (v.kind === 'dhcp') {
      const ls = (l) => (!l ? '-' : l.ok ? `${l.address} from ${l.server}${l.lease_s ? `, ${l.lease_s} s lease` : ''}` : `FAILED: ${l.error || 'no lease'}`);
      tr.appendChild(el('td', null, ls(s.before_lease)));
      tr.appendChild(el('td', (s.after_lease || {}).ok ? 's-ok' : 's-bad', ls(s.after_lease)));
      const a = s.after_lease || {};
      tr.appendChild(el('td', null, a.t_bound != null && a.t_released != null ? `renewed in ${(a.t_bound - a.t_released).toFixed(1)} s` : '-'));
      tr.appendChild(el('td', null, a.t_bound != null ? foSecs(a.t_bound, 1) : '-'));
      tr.appendChild(el('td', null, a.server ? `lease server ${a.server}` : '-'));
    } else {
      tr.appendChild(el('td', null, `${s.before_ok}/${s.before} ok`));
      tr.appendChild(el('td', s.after_ok === s.after ? 's-ok' : 's-warn', `${s.after_ok}/${s.after} ok`));
      tr.appendChild(el('td', s.down_s ? 's-warn' : null, (s.down_s != null ? `${s.down_s.toFixed(1)} s` : 'none') +
        (s.blips ? ` + ${s.blips} blip${s.blips > 1 ? 's' : ''}` : '')));
      tr.appendChild(el('td', null, s.recovered_at != null ? foSecs(s.recovered_at, 1) : s.verdict));
      const ev = s.evidence || {};
      tr.appendChild(el('td', 'wrap', ev.what ? `before: ${who(ev.before)}; after: ${who(ev.after)}` : '-'));
    }
    t.appendChild(tr);
  });
  box.appendChild(t);
  box.appendChild(el('div', 's-dim', 'DNS and TCP run every second; DHCP releases and renews once before the failure and once ' +
    'a few seconds after it. "Answered by" counts the replies seen in the packet capture either side of F.'));
  return box;
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
  mk('Kind', [['all', 'all'], ['echo', 'ping requests/replies'], ['esp', 'ESP (VPN)'], ['unreach', 'ICMP errors'], ['ike', 'IKE'],
              ['dns', 'DNS'], ['tcp', 'TCP handshakes']], 'kind');
  const q = el('input'); q.type = 'search'; q.placeholder = 'Filter link, node or probe';
  q.addEventListener('input', () => { f.text = q.value.toLowerCase(); draw(); });
  bar.appendChild(q);
  box.appendChild(bar);
  const wrap = el('div', 'pkt-log-wrap fo-pkt-wrap');
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
      tr.appendChild(el('td', null, p.kind === 'echo' ? `ping ${p.dir}` : p.kind === 'dns' ? `DNS ${p.dir}`
        : p.kind === 'tcp' ? `TCP ${p.dir.toUpperCase()}` : p.kind));
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
