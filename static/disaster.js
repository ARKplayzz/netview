/* NetView: disaster traces (spec: docs/disaster-spec.md, 2026-09-29).
 * Pick running nodes to power off; the server runs a baseline trace, powers
 * them off, measures the outage once a second, traces again, then powers them
 * back on - always, even on error or Cancel. Loaded after app.js and uses its
 * globals (trace, BY, NODES, INV, el, $, getJSON, candidates, pathDiagram...). */

function disasterPanel() {
  const D = trace.disaster;
  const selectable = (x) => { const n = BY.get(x); return n && n.up && !n.ghost && x !== trace.source; };
  const redraw = () => { D.confirming = false; renderPathHead(); renderPathBody(); };
  const box = el('div', 'dz-panel');

  const h = el('div', 'dz-head');
  h.appendChild(el('b', null, 'Disaster'));
  h.appendChild(el('span', 's-dim', '  Run the trace, power these nodes off, measure the outage, run the trace again, ' +
    'then power them back on.'));
  const close = el('button', 'linkish', 'Close');
  close.addEventListener('click', () => { D.open = false; redraw(); });
  h.appendChild(close);
  box.appendChild(h);

  const chips = el('div', 'dz-chips');
  if (!D.nodes.length) chips.appendChild(el('span', 's-dim', 'No nodes chosen. Tick nodes below, or add a zone.'));
  D.nodes.forEach((x) => {
    const c = el('span', 'dz-chip', x);
    const b = el('button', 'linkish', '×');
    b.title = `Remove ${x}`;
    b.setAttribute('aria-label', `Remove ${x}`);
    b.addEventListener('click', () => { D.nodes = D.nodes.filter((y) => y !== x); redraw(); });
    c.appendChild(b);
    chips.appendChild(c);
  });
  if (D.nodes.length) {
    const clr = el('button', 'linkish', 'Clear all');
    clr.addEventListener('click', () => { D.nodes = []; redraw(); });
    chips.appendChild(clr);
  }
  box.appendChild(chips);

  const row = el('div', 'dz-row');
  const f = el('input');
  f.type = 'search'; f.placeholder = 'Filter nodes'; f.value = D.filter; f.id = 'dz-filter';
  f.setAttribute('aria-label', 'Filter nodes');
  row.appendChild(f);
  const zs = el('select');
  zs.id = 'dz-zone';
  zs.setAttribute('aria-label', 'Add every running node in a zone');
  const z0 = el('option', null, 'Add a zone...'); z0.value = ''; zs.appendChild(z0);
  (INV.zones || []).forEach((z) => {
    const n = z.nodes.filter(selectable);
    if (!n.length) return;
    const o = el('option', null, `${z.name} (${n.length} running)`); o.value = z.name; zs.appendChild(o);
  });
  zs.addEventListener('change', () => {
    const z = (INV.zones || []).find((q) => q.name === zs.value);
    if (z) z.nodes.filter(selectable).forEach((x) => { if (!D.nodes.includes(x)) D.nodes.push(x); });
    redraw();
  });
  row.appendChild(zs);
  const sl = el('label', null, 'Wait up to ');
  const ss = el('select'); ss.id = 'dz-settle';
  [15, 30, 60, 120].forEach((v) => { const o = el('option', null, `${v} s`); o.value = v; ss.appendChild(o); });
  ss.value = D.settle;
  ss.addEventListener('change', () => { D.settle = +ss.value; });
  sl.appendChild(ss);
  sl.title = 'The longest NetView waits for the path to recover before the second trace. ' +
             'It moves on as soon as 5 pings in a row are answered.';
  row.appendChild(sl);
  const rl = el('label');
  const rc = el('input'); rc.type = 'checkbox'; rc.checked = D.restore; rc.id = 'dz-restore';
  rc.addEventListener('change', () => { D.restore = rc.checked; D.confirming = false; renderPathBody(); });
  rl.appendChild(rc); rl.appendChild(document.createTextNode(' Power them back on afterwards'));
  row.appendChild(rl);
  box.appendChild(row);

  const list = el('div', 'dz-list');
  const renderList = () => {
    list.innerHTML = '';
    const q = D.filter.trim().toLowerCase();
    const bySite = new Map();
    NODES.filter((n) => !n.ghost && n.up && (!q || n.label.toLowerCase().includes(q)))
      .sort((a, b) => a.label.localeCompare(b.label))
      .forEach((n) => { const k = n.site || 'Other'; if (!bySite.has(k)) bySite.set(k, []); bySite.get(k).push(n); });
    [...bySite.keys()].sort().forEach((site) => {
      const g = el('div', 'dz-site');
      g.appendChild(el('div', 'dz-site-h', site));
      bySite.get(site).forEach((n) => {
        const lab = el('label', 'dz-item');
        const cb = el('input'); cb.type = 'checkbox';
        cb.checked = D.nodes.includes(n.label);
        const isSrc = n.label === trace.source;
        cb.disabled = isSrc;
        cb.addEventListener('change', () => {
          if (cb.checked) { if (!D.nodes.includes(n.label)) D.nodes.push(n.label); }
          else D.nodes = D.nodes.filter((y) => y !== n.label);
          D.confirming = false;
          renderPathHead();
          renderPathBody();
        });
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(' ' + n.label));
        if (isSrc) lab.appendChild(el('span', 's-dim', ' (the source: its console sends the test)'));
        else if (n.label === trace.target) lab.appendChild(el('span', 's-dim', ' (the target)'));
        g.appendChild(lab);
      });
      list.appendChild(g);
    });
    if (!list.children.length) list.appendChild(el('div', 's-dim', 'No running node matches.'));
  };
  f.addEventListener('input', () => { D.filter = f.value; renderList(); });
  renderList();
  box.appendChild(list);

  const warns = [];
  if (D.nodes.includes(trace.target)) {
    warns.push(`The target ${trace.target} is included: the second trace shows whether anything else answers (anycast, DNS failover).`);
  }
  const win = D.nodes.filter((x) => (BY.get(x) || {}).node_definition === 'win2025');
  if (win.length) warns.push(`${win.join(', ')}: Windows servers take about 6 minutes to boot again.`);
  if (D.nodes.length > 8) warns.push(`${D.nodes.length} nodes is a large disaster; the restore takes longer.`);
  if (!D.restore && D.nodes.length) warns.push('These nodes will be LEFT powered off. The banner offers Restore later.');
  warns.forEach((w) => box.appendChild(el('div', 'dz-warn', w)));

  if (D.confirming && D.nodes.length) {
    const cf = el('div', 'dz-confirm');
    cf.appendChild(el('div', null, `Power off ${D.nodes.length} node${D.nodes.length > 1 ? 's' : ''}: ${D.nodes.join(', ')}. ` +
      (D.restore ? 'They are powered on again when the test ends.' : 'They stay powered off afterwards.')));
    const go = el('button', 'default danger', 'Power off and run');
    go.id = 'dz-confirm-go';
    go.addEventListener('click', () => { D.confirming = false; runTrace(); });
    const no = el('button', null, 'Cancel');
    no.addEventListener('click', () => { D.confirming = false; renderPathBody(); });
    cf.appendChild(go); cf.appendChild(no);
    box.appendChild(cf);
  }
  return box;
}

function outageStrip(o) {
  const wrap = el('div', 'dz-outage');
  wrap.appendChild(el('div', 's-dim', `Ping from ${trace.source} to ${o.target}, once a second. ` +
    'The dark line marks where the power-off finished.'));
  const strip = el('div', 'dz-strip');
  [...(o.marks || '')].forEach((m, i) => {
    const c = el('i', m === '!' ? 'ok' : m === '.' ? 'lost' : 'unk');
    c.title = `${i} s: ${m === '!' ? 'answered' : m === '.' ? 'lost' : 'not measured'}`;
    if (i === o.power_off_done_at_mark) c.classList.add('mark');
    strip.appendChild(c);
  });
  wrap.appendChild(strip);
  const txt = !o.lost ? 'No outage: every ping was answered.'
    : o.still_down ? `No recovery yet: pings were still failing at the end of the wait (${o.longest_outage_s} s down).`
      : `Longest outage ${o.longest_outage_s} s; answered again ${o.recovered_after_s} s after the first loss.`;
  wrap.appendChild(el('div', !o.lost ? 's-ok' : o.still_down ? 's-bad' : 's-warn', txt));
  return wrap;
}

function disasterResult(res, width) {
  const d = res.disaster;
  const box = el('div', 'dz-result');
  box.appendChild(el('div', 'dz-title', `After the disaster: ${d.nodes.join(', ')} powered off`));
  (d.warnings || []).forEach((w) => box.appendChild(el('div', 'dz-warn', w)));
  if (d.error) box.appendChild(el('div', 's-bad', 'Stopped early: ' + d.error));
  const po = d.power_off || {};
  const notOff = Object.entries(po).filter(([, v]) => v !== 'STOPPED' && v !== 'DEFINED_ON_CORE');
  if (Object.keys(po).length) {
    box.appendChild(el('div', notOff.length ? 's-warn' : 's-dim', `Power-off took ${d.power_off_seconds ?? '?'} s.` +
      (notOff.length ? ` Not confirmed stopped: ${notOff.map(([k, v]) => `${k} (${v})`).join(', ')}.` : '')));
  }
  if (d.outage) box.appendChild(outageStrip(d.outage));
  const a = d.after;
  if (a) {
    const df = d.diff || { comparable: false, removed: [], added: [], same: false, why: 'no comparison available' };
    const ch = el('div', 'dz-changed');
    ch.appendChild(el('b', null, 'What changed'));
    ch.appendChild(el('div', null, df.comparable === false ? `Path: not compared - ${df.why}.`
      : df.same ? 'The path is the same.'
        : 'Path: ' + [df.removed.length ? `no longer through ${df.removed.join(', ')}` : '',
          df.added.length ? `now through ${df.added.join(', ')}` : ''].filter(Boolean).join('; ') + '.'));
    ch.appendChild(el('div', null, `Result: ${res.verdict.level} before, ${a.verdict.level} after.`));
    const rep = (x) => `${x.received == null ? 'not counted' : x.received}/${x.sent}`;
    ch.appendChild(el('div', null, `Replies: ${rep(res)} before, ${rep(a)} after.`));
    if (a.target && res.target && a.target !== res.target) {
      ch.appendChild(el('div', null, `Answered by ${res.target} before, ${a.target} after.`));
    }
    box.appendChild(ch);
    const st = el('div', 'path-status');
    if (a.mode === 'capture') st.appendChild(el('span', 'mode-tag verified', 'VERIFIED'));
    st.appendChild(el('span', a.verdict.level === 'ok' ? 's-ok' : a.verdict.level === 'fail' ? 's-bad' : 's-warn', a.verdict.text));
    box.appendChild(st);
    const down = new Set(d.nodes);
    const c2 = candidates(trace.source, a.virtual_target ? a.target : trace.target, down);
    box.appendChild(pathDiagram(a, c2, width, { down }));
    const f = el('div', 'path-findings');
    (a.findings || []).forEach((x) => f.appendChild(el('div', null, x)));
    if ((a.findings || []).length) box.appendChild(f);
    if (a.packet_log && a.packet_log.length) box.appendChild(packetLogPanel(a));
  } else if (d.after_error) {
    box.appendChild(el('div', 's-bad', 'The trace after the disaster failed: ' + d.after_error));
  }
  const r = d.restored || {};
  const bad = Object.entries(r).filter(([, v]) => v !== 'BOOTED');
  if (Object.keys(po).length) {
    box.appendChild(el('div', 'dz-restore ' + (d.restore === false || bad.length ? 's-warn' : 's-ok'),
      d.restore === false ? `Left powered off, as chosen: ${d.nodes.join(', ')}.`
        : bad.length ? `Restore incomplete: ${bad.map(([k, v]) => `${k} (${v})`).join(', ')}. Use Restore in the banner.`
          : `Restored: ${Object.keys(r).length} node${Object.keys(r).length === 1 ? '' : 's'} running again.`));
  }
  return box;
}

async function pollDisaster() {
  let st;
  try { st = await getJSON('/api/disaster'); } catch (e) { return; }
  const b = $('#disaster-banner');
  if (!b) return;
  b.innerHTML = '';
  if (st.active) {
    b.hidden = false; b.className = 'dz-banner run';
    b.appendChild(el('span', null, `Disaster test running: ${st.active.nodes.join(', ')} (${st.active.stage}). ` +
      'Traces started now see a deliberately degraded network.'));
  } else if (st.interrupted) {
    b.hidden = false; b.className = 'dz-banner stuck';
    const n = st.interrupted.nodes;
    b.appendChild(el('span', null, `A disaster test left ${n.length} node${n.length === 1 ? '' : 's'} powered off: ${n.join(', ')}.`));
    const btn = el('button', 'default', 'Restore now');
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = 'Restoring (waits for them to boot)...';
      try {
        const r = await fetch('/api/disaster/restore', { method: 'POST' });
        const j = await r.json();
        btn.textContent = j.error || 'Restored';
      } catch (e) { btn.textContent = 'Restore failed - try again'; btn.disabled = false; }
      setTimeout(pollDisaster, 1500);
    });
    b.appendChild(btn);
  } else {
    b.hidden = true;
  }
}
