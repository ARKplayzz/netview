/* NetView client - lab workbench.
 *
 * Layout follows the CML workbench: menu bar and toolbar on top, the lab
 * canvas (nodes at their CML x/y, cabling from the topology), a properties
 * sidebar for whatever is selected, and a bottom pane holding the node table,
 * the lab-wide checks, the ping log and console tabs.
 *
 * The inventory is fetched once; only /api/state is polled on the tick.
 * Hidden nodes persist as an exclusion list: anything new in the lab defaults
 * to visible, because a dashboard that silently omits part of the estate is
 * worse than no dashboard.
 */
'use strict';

const REFRESH_S = 15;
const HIDE_KEY = 'netview.hidden.v2';
const SITE_KEY = 'netview.site.v1';
const PREF_KEY = 'netview.workbench.v2';
const SVGNS = 'http://www.w3.org/2000/svg';

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

let INV = null, NODES = [], LINKS = [];
const BY = new Map();
let selected = null, hovered = null, countdown = REFRESH_S, paused = false;
let site = store.get(SITE_KEY, 'all');
if (!['all', 'HQ', 'DR', 'other'].includes(site)) site = 'all';
const hidden = new Set(store.get(HIDE_KEY, []));
const prefs = Object.assign({
  ip: true, iface: false, traffic: false, theme: null, paneH: 260, paneMin: false,
  groupBy: 'layer', hideDown: false, sort: 'label', sortDir: 1,
}, store.get(PREF_KEY, {}));
const savePrefs = () => store.set(PREF_KEY, prefs);

let paneTab = 'nodes';           // nodes | matrix | dns | harvest | pings | con:<label>
let sideTab = 'node';            // node | addresses | interfaces | dhcp | ping
const consoles = [];             // labels with an open console tab
const consoleText = new Map();
// Ping jobs live here rather than in DOM nodes, so switching tabs or
// selecting another node never loses a run in flight.
const probeJobs = [];

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const svg = (tag, attrs, text) => {
  const n = document.createElementNS(SVGNS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => { if (v != null) n.setAttribute(k, v); });
  if (text !== undefined) n.textContent = text;
  return n;
};
const getJSON = async (url, opts) => (await fetch(url, opts)).json();
const byLabel = (a, b) => a.label.localeCompare(b.label, undefined, { numeric: true });

/* ---------------------------------------------------------------- data --- */
async function boot() {
  applyTheme();
  wire();
  await loadInventory(false);
  infobar();
  renderAll();
  fit();
  setInterval(tickSecond, 1000);
  if (prefs.traffic) startTraffic();
  Object.values(LAB).forEach((t) => t.refresh && t.refresh());
}

/* The whole inventory is re-read from the server on every refresh, and the
 * server re-reads CML (topology, states, addresses, configs) for it, so a
 * node added, moved, re-cabled or re-addressed in CML appears here on the
 * next tick.  Only per-session state (pings, consoles, the trace) is kept. */
async function loadInventory(fresh) {
  const inv = await getJSON('/api/inventory' + (fresh ? '?fresh=1' : ''));
  const keep = new Map(NODES.map((n) => [n.label, { lastSource: n.lastSource }]));
  INV = inv;
  NODES = INV.nodes.concat(INV.ghosts.map((g) => Object.assign({
    ghost: true, up: false, dns: {}, links: [], vlans: [], addresses: [],
    ha: {}, issues: [], interfaces: [], probe_family: 'none',
    can_source_probe: false, addressing: 'unaddressed',
  }, g)));
  BY.clear();
  NODES.forEach((n) => { Object.assign(n, keep.get(n.label) || {}); BY.set(n.label, n); });
  LINKS = buildLinks();
  parkUnplaced();
  if (selected && !BY.has(selected)) selected = null;
}

async function poll(fresh) {
  try {
    await loadInventory(fresh);
    countdown = REFRESH_S;
    renderCanvas(); renderStatus(); renderPaneTabs();
    if (paneTab === 'nodes') renderPaneBody();
    if (!selected || ['node', 'addresses', 'interfaces'].includes(sideTab)) renderSide();
    if (fresh) infobar();
  } catch (e) { /* one missed poll is not worth an error state */ }
}

function tickSecond() {
  if (paused) return;
  countdown -= 1;
  if (countdown <= 0) { countdown = REFRESH_S; poll(false); }
  $('#tick').textContent = countdown + 's';
}

/* Each link appears on both of its ends in the inventory; collapse to one. */
function buildLinks() {
  const seen = new Map();
  NODES.forEach((n) => (n.links || []).forEach((l) => {
    const key = l.link_id || [n.label + '|' + l.local_iface, l.peer + '|' + l.peer_iface].sort().join('<>');
    if (!seen.has(key)) {
      seen.set(key, { id: l.link_id, a: n.label, ai: l.local_iface, b: l.peer, bi: l.peer_iface, subnet: l.subnet });
    }
  }));
  return [...seen.values()].filter((l) => BY.has(l.a) && BY.has(l.b));
}

/* Nodes with no canvas position (declared-but-absent ghosts, or a topology
 * without x/y) are parked in a frame right of the lab, so they stay visible
 * without pretending to be cabled anywhere. */
function parkUnplaced() {
  const placed = NODES.filter((n) => typeof n.x === 'number' && typeof n.y === 'number');
  const loose = NODES.filter((n) => !placed.includes(n));
  if (!loose.length) return;
  const x0 = placed.length ? Math.max(...placed.map((n) => n.x)) + 200 : 0;
  const y0 = placed.length ? Math.min(...placed.map((n) => n.y)) : 0;
  loose.forEach((n, i) => {
    n.x = x0 + Math.floor(i / 6) * 150;
    n.y = y0 + (i % 6) * 100;
    n.parked = true;
  });
}

function infobar() {
  const box = $('#banners');
  box.innerHTML = '';
  const m = INV.meta;
  const line = (cls, text) => {
    const d = el('div');
    d.appendChild(el('span', cls, text));
    const x = el('button', 'linkish', 'Dismiss');
    x.addEventListener('click', () => { d.remove(); box.hidden = !box.children.length; fit(); });
    d.appendChild(x);
    box.appendChild(d);
  };
  if (m.topology_source !== 'live') {
    const age = m.snapshot && m.snapshot.age_hours;
    line('bad', `CML is unreachable. Showing the cached snapshot (${age != null ? age + ' h old' : 'age unknown'}); ` +
      'anything changed in the lab since then is missing.');
  }
  if (m.counts.undeclared) {
    line(null, `${m.counts.undeclared} node(s) are in the lab but not in the manifest and get no generated config. ` +
      'They have an orange outline on the canvas.');
  }
  box.hidden = !box.children.length;
}

/* --------------------------------------------------------------- filter -- */
/* "Other" = anything that is not HQ or DR: the carriers, the internet web
 * server, and the remote worker's house (site HOME).  A shared carrier has no
 * single site by design - see the manifest's note on the ISP nodes. */
function siteBucket(n) {
  return n.site === 'HQ' || n.site === 'DR' ? n.site : 'other';
}

function hay(n) {
  const p = [n.label, n.role, n.role_title, n.site, n.layer_title, n.state,
             n.node_definition, n.primary_ip, n.live_ip, (n.dns || {}).fqdn];
  (n.addresses || []).forEach((a) => p.push(a.value));
  (n.vlans || []).forEach((v) => p.push('vlan' + v.id, v.name));
  return p.filter(Boolean).join(' ').toLowerCase();
}

/* The canvas keeps every node in place (moving the lab around under a filter
 * would be disorienting); filters DIM on the canvas and REMOVE from the table. */
function matches(n) {
  const q = $('#q').value.trim().toLowerCase();
  if (site !== 'all' && siteBucket(n) !== site) return false;
  if (prefs.hideDown && !n.up && !n.ghost) return false;
  return !q || hay(n).includes(q);
}
const shown = (n) => !hidden.has(n.label);

/* Live address (read off the device) outranks the manifest's declaration. */
function displayIp(n) {
  if (n.live_ip) return { text: n.live_ip, drift: n.live_ip !== n.primary_ip, live: true };
  if (n.primary_ip) return { text: n.primary_ip, drift: false, live: false };
  return { text: '', drift: false, live: false };
}

function stateWord(n) {
  if (n.ghost) return ['absent', 's-bad'];
  if (n.state === 'BOOTED') return ['booted', 's-ok'];
  if (n.state === 'STARTED') return ['starting', 's-warn'];
  return [(n.state || 'unknown').toLowerCase(), 's-dim'];
}
function lamp(n) { return el('span', 'lamp-i ' + (n.ghost ? 'ghost' : n.state || '')); }

/* ---------------------------------------------------------------- icons -- */
/* One glyph per device class in a 34-unit box centred on 0,0: round routers,
 * square switches, a brick firewall, tower servers, monitors for hosts, a
 * cloud for the outside.  Shape carries the class; colour carries state. */
function kind(n) {
  const d = n.node_definition || '', r = n.role || '';
  if (r === 'firewall_asa' || d === 'asav') return 'firewall';
  if (['edge_router', 'isp_router', 'home_router'].includes(r) || ['iosv', 'csr1000v'].includes(d)) return 'router';
  if (['l3_core', 'l3_core_hsrp', 'l3_dist'].includes(r)) return 'l3switch';
  if (r.startsWith('l2_') || ['ioll2-xe', 'iosvl2'].includes(d)) return 'switch';
  if (r === 'wlc' || d === 'cat9800') return 'wlc';
  if (r === 'ap' || d === 'vap') return 'ap';
  if (r === 'trex' || d === 'trex') return 'tester';
  if (d === 'external_connector' || r === 'web_internet' || /^ext-conn|^internet-/i.test(n.label)) return 'cloud';
  if (['desktop', 'wireless-client', 'alpine'].includes(d) || /desktop|client/.test(r)) return 'host';
  if (['dnsmasq', 'radius', 'nginx', 'splunk', 'syslog', 'server', 'win2025'].includes(d) ||
      r.startsWith('server') || r.startsWith('dns')) return 'server';
  if (d === 'iol-xe') return n.layer === 'edge' || n.layer === 'carrier' ? 'router' : 'l3switch';
  return 'unknown';
}
const KIND_NAME = {
  router: 'Router', l3switch: 'L3 switch', switch: 'L2 switch', firewall: 'Firewall', server: 'Server',
  host: 'Host', wlc: 'WLC', ap: 'Access point', tester: 'Traffic generator', cloud: 'External', unknown: 'Other',
};
const ARROW = (x1, y1, x2, y2) => {
  const a = Math.atan2(y2 - y1, x2 - x1), h = 3.4;
  return `M${x1} ${y1}L${x2} ${y2}M${x2 - h * Math.cos(a - 0.6)} ${y2 - h * Math.sin(a - 0.6)}` +
         `L${x2} ${y2}L${x2 - h * Math.cos(a + 0.6)} ${y2 - h * Math.sin(a + 0.6)}`;
};
const GLYPH = {
  router: () => [ARROW(-10, -3, -2, -3), ARROW(10, 3, 2, 3), ARROW(3, -10, 3, -2), ARROW(-3, 10, -3, 2)],
  l3switch: () => [ARROW(-2, -4, -10, -4), ARROW(2, -4, 10, -4), ARROW(-2, 4, -10, 4), ARROW(2, 4, 10, 4)],
  switch: () => [ARROW(-10, -4, 10, -4), ARROW(10, 4, -10, 4)],
  firewall: () => ['M-10 -7H10M-10 0H10M-10 7H10M-4 -7V0M5 -7V0M0 0V7M-7 0V7M7 0V7M-10 -10H10V10H-10Z'],
  server: () => ['M-7 -11H7V11H-7Z', 'M-4 -6H4M-4 -2H4M-4 2H4'],
  host: () => ['M-11 -9H11V5H-11Z', 'M-4 10H4M0 5V10'],
  wlc: () => ['M-9 6H9', 'M-6 1a8 8 0 0 1 12 0', 'M-9.5 -3a13 13 0 0 1 19 0'],
  ap: () => ['M0 8V-1', 'M-5 -3a7 7 0 0 1 10 0', 'M-8.5 -7a12 12 0 0 1 17 0'],
  tester: () => ['M-11 1H-6L-3 -8L2 9L5 -2L7 1H11'],
  cloud: () => ['M-9 6a5 5 0 0 1 0-10a7 7 0 0 1 13-2a5 5 0 0 1 5 12Z'],
  unknown: () => ['M-4 -5a4 4 0 1 1 5 4c-1 .5-1 1.5-1 3', 'M0 7v.5'],
};
const ROUND = new Set(['router', 'ap', 'cloud', 'unknown']);

function nodeIcon(n, size) {
  const k = kind(n), s = size / 2;
  const g = svg('g');
  if (ROUND.has(k)) g.appendChild(svg('circle', { r: s, class: 'body' }));
  else g.appendChild(svg('rect', { x: -s, y: -s, width: size, height: size, rx: 3, class: 'body' }));
  const gl = svg('g', { class: 'glyph', transform: `scale(${size / 34})` });
  GLYPH[k]().forEach((d) => gl.appendChild(svg('path', { d })));
  g.appendChild(gl);
  return g;
}

const X_ICON = () => {
  const s = svg('svg', { viewBox: '0 0 8 8' });
  s.appendChild(svg('path', { d: 'M1 1L7 7M7 1L1 7' }));
  return s;
};

/* --------------------------------------------------------------- canvas -- */
const view = { x: 0, y: 0, k: 1 };
const NODE_SIZE = 36;

function applyView() {
  $('#viewport').setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`);
  $('#zoomread').textContent = Math.round(view.k * 100) + '%';
}

function fit() {
  const pts = NODES.filter(shown);
  if (!pts.length) return;
  const r = $('#canvas').getBoundingClientRect();
  const xs = pts.map((n) => n.x), ys = pts.map((n) => n.y);
  const minX = Math.min(...xs) - 70, maxX = Math.max(...xs) + 70;
  const minY = Math.min(...ys) - 50, maxY = Math.max(...ys) + 60;
  const k = Math.min((r.width - 40) / (maxX - minX), (r.height - 30) / (maxY - minY), 1.5);
  view.k = Math.max(0.15, k);
  view.x = (r.width - (maxX - minX) * view.k) / 2 - minX * view.k;
  view.y = (r.height - (maxY - minY) * view.k) / 2 - minY * view.k;
  applyView();
}

function zoomAt(factor, cx, cy) {
  const r = $('#canvas').getBoundingClientRect();
  if (cx == null) { cx = r.width / 2; cy = r.height / 2; }
  const k = Math.min(4, Math.max(0.12, view.k * factor));
  view.x = cx - (cx - view.x) * (k / view.k);
  view.y = cy - (cy - view.y) * (k / view.k);
  view.k = k;
  applyView();
}

function centerOn(n, force) {
  const r = $('#canvas').getBoundingClientRect();
  const sx = n.x * view.k + view.x, sy = n.y * view.k + view.y;
  if (!force && sx > 60 && sx < r.width - 60 && sy > 60 && sy < r.height - 60) return;
  view.x = r.width / 2 - n.x * view.k;
  view.y = r.height / 2 - n.y * view.k;
  applyView();
}

const shortIf = (s) => (s || '')
  .replace(/^GigabitEthernet/i, 'Gi').replace(/^TenGigabitEthernet/i, 'Te')
  .replace(/^FastEthernet/i, 'Fa').replace(/^Ethernet/i, 'e').replace(/^Management/i, 'Mgmt')
  .replace(/^Port-channel/i, 'Po').replace(/^Vlan/i, 'Vl');

function renderCanvas() {
  const lg = $('#links'), ng = $('#nodes');
  lg.innerHTML = ''; ng.innerHTML = '';

  LINKS.forEach((l) => {
    const a = BY.get(l.a), b = BY.get(l.b);
    if (!shown(a) || !shown(b)) return;
    const g = svg('g', { 'data-a': l.a, 'data-b': l.b, 'data-link': l.id || '' });
    g.appendChild(svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'wire' + (a.up && b.up ? '' : ' off') }));
    const hit = svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'wire-hit' });
    hit.appendChild(svg('title', {}, `${l.a} ${l.ai}  to  ${l.b} ${l.bi}${l.subnet ? '\n' + l.subnet : ''}`));
    g.appendChild(hit);
    if (prefs.iface) {
      [[a, b, l.ai], [b, a, l.bi]].forEach(([p, q, name]) => {
        const len = Math.hypot(q.x - p.x, q.y - p.y) || 1;
        const t = Math.min(0.4, 40 / len);
        g.appendChild(svg('text', { x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t + 3,
                                    class: 'iflbl', 'text-anchor': 'middle' }, shortIf(name)));
      });
    }
    g.appendChild(svg('text', { x: (a.x + b.x) / 2 + 4, y: (a.y + b.y) / 2 - 3, class: 'rate' }, ''));
    lg.appendChild(g);
  });

  const parked = NODES.filter((n) => n.parked && shown(n));
  if (parked.length) {
    const xs = parked.map((n) => n.x), ys = parked.map((n) => n.y);
    const x = Math.min(...xs) - 70, y = Math.min(...ys) - 56;
    ng.appendChild(svg('rect', { x, y, width: Math.max(...xs) - x + 70, height: Math.max(...ys) - y + 60, class: 'park' }));
    ng.appendChild(svg('text', { x: x + 8, y: y + 15, class: 'park-t' }, 'No canvas position'));
  }

  const s = NODE_SIZE / 2;
  NODES.filter(shown).forEach((n) => {
    const cls = ['node'];
    if (!n.up) cls.push('off');
    if (n.ghost) cls.push('ghost');
    if (n.config_status === 'undeclared') cls.push('undecl');
    if (n.label === selected) cls.push('sel');
    const g = svg('g', { class: cls.join(' '), transform: `translate(${n.x} ${n.y})`, 'data-label': n.label });
    g.appendChild(svg('rect', { x: -s - 4, y: -s - 4, width: NODE_SIZE + 8, height: NODE_SIZE + 8, class: 'sel-box' }));
    g.appendChild(nodeIcon(n, NODE_SIZE));
    // state lamp, bottom-right: the same square used in the node table
    if (!n.ghost) {
      const c = n.state === 'BOOTED' ? 'var(--ok)' : n.state === 'STARTED' ? 'var(--warn)' : 'var(--devoff)';
      g.appendChild(svg('rect', { x: s - 5, y: s - 5, width: 9, height: 9, fill: c, class: 'lamp' }));
    }
    // open known-issues count, top-left
    if ((n.issues || []).length) {
      g.appendChild(svg('rect', { x: -s - 4, y: -s - 4, width: 12, height: 12, fill: 'var(--warn)', class: 'lamp' }));
      g.appendChild(svg('text', { x: -s + 2, y: -s + 5, class: 'iss' }, String(n.issues.length)));
    }
    g.appendChild(svg('text', { y: s + 15, class: 'lbl' }, n.label));
    if (prefs.ip) {
      const ip = displayIp(n);
      const txt = n.ghost ? 'not in lab' : ip.text ? ip.text + (ip.drift ? ' (drift)' : '') : '';
      if (txt) g.appendChild(svg('text', { y: s + 27, class: 'ipl' + (ip.drift || n.ghost ? ' drift' : '') }, txt));
    }
    g.appendChild(svg('title', {}, `${n.label}\n${KIND_NAME[kind(n)]}, ${stateWord(n)[0]}`));
    ng.appendChild(g);
  });

  renderPathOverlay();
  highlight();
  paintTraffic();
  paintTrace();
}

/* Dim anything the filters exclude; light up the focused node's cabling. */
function highlight() {
  if (traceShown()) { paintTrace(); return; }
  const focus = hovered || selected;
  document.querySelectorAll('#nodes .node').forEach((g) => g.classList.toggle('dim', !matches(BY.get(g.dataset.label))));
  document.querySelectorAll('#links > g').forEach((g) => {
    const w = g.querySelector('.wire');
    w.classList.toggle('hl', !!focus && (g.dataset.a === focus || g.dataset.b === focus));
    w.classList.toggle('dim', !matches(BY.get(g.dataset.a)) || !matches(BY.get(g.dataset.b)));
  });
}

/* A curved line from source to target: dashed while a ping runs, then drawn
 * in the verdict's colour.  Also used when hovering a baseline-matrix row. */
function drawPath(src, dst, level) {
  const ov = $('#overlay');
  ov.innerHTML = '';
  const a = BY.get(src), b = BY.get(dst);
  if (!a || !b || !shown(a) || !shown(b)) return;
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, bend = Math.min(80, len * 0.22);
  ov.appendChild(svg('path', { d: `M${a.x} ${a.y}Q${mx - dy / len * bend} ${my + dx / len * bend} ${b.x} ${b.y}`,
                               class: 'path ' + level }));
}
function renderPathOverlay() {
  $('#overlay').innerHTML = '';
  const job = probeJobs.find((j) => selected && (j.target === selected || j.source === selected));
  if (!job) return;
  const v = job.status === 'done' ? (job.result.verdict || {}).level : null;
  drawPath(job.source, job.target, job.status === 'error' ? 'bad'
    : job.status !== 'done' ? 'run' : v === 'ok' ? 'ok' : v === 'fail' ? 'bad' : 'warn');
}

/* ------------------------------------------------------- link traffic ---- */
/* Two samples of the lab-wide link counters give live pkts/s and a cumulative
 * drop count per link, read over REST with no console attach (0.04s). */
let trafficTimer = null, lastStats = null, linkRates = {};

function startTraffic() {
  stopTraffic();
  const sample = async () => {
    try {
      const s = await getJSON('/api/simstats');
      if (s.error) throw s.error;
      const t = Date.now();
      if (lastStats) {
        const dt = (t - lastStats.t) / 1000;
        linkRates = {};
        Object.entries(s.links).forEach(([id, v]) => {
          const p = lastStats.links[id];
          if (p) linkRates[id] = {
            pps: Math.max(0, ((v.readpackets + v.writepackets) - (p.readpackets + p.writepackets)) / dt),
            drops: v.drops,
          };
        });
      }
      lastStats = { t, links: s.links };
      paintTraffic();
      if (selected && sideTab === 'interfaces') renderSide();
    } catch (e) { linkRates = {}; paintTraffic(); }
  };
  sample();
  trafficTimer = setInterval(sample, 3000);
}
function stopTraffic() {
  clearInterval(trafficTimer); trafficTimer = null; lastStats = null; linkRates = {};
  paintTraffic();
}
function paintTraffic() {
  document.querySelectorAll('#links > g').forEach((g) => {
    const r = prefs.traffic ? linkRates[g.dataset.link] : null;
    const w = g.querySelector('.wire'), tx = g.querySelector('.rate');
    w.classList.toggle('busy', !!(r && r.pps >= 1));
    w.classList.toggle('drops', !!(r && r.drops > 0));
    w.style.strokeWidth = r && r.pps >= 1 ? String(Math.min(5, 1.5 + Math.log10(r.pps + 1))) : '';
    tx.textContent = !r ? '' : r.drops > 0 ? `${Math.round(r.pps)} pps, ${r.drops} drops`
      : r.pps >= 1 ? Math.round(r.pps) + ' pps' : '';
    tx.classList.toggle('bad', !!(r && r.drops > 0));
  });
}
function setTraffic(on) {
  prefs.traffic = on; savePrefs();
  $('#show-traffic').checked = on;
  if (on) startTraffic(); else stopTraffic();
  if (selected && sideTab === 'interfaces') renderSide();
}

/* ------------------------------------------------------------ selection -- */
function select(label, center) {
  selected = label;
  const n = label && BY.get(label);
  if (n && ((sideTab === 'dhcp' && !n.dhcp) || (n.ghost && sideTab === 'ping'))) sideTab = 'node';
  if (n && center) centerOn(n);
  if (n) document.body.classList.add('side-open');
  renderCanvas(); renderSide(); renderStatus();
  if (paneTab === 'nodes') markTableSelection();
}

function hideNode(label) {
  hidden.add(label);
  store.set(HIDE_KEY, [...hidden]);
  if (selected === label) selected = null;
  renderAll();
}

/* ----------------------------------------------------------- status bar -- */
function renderStatus() {
  const m = INV.meta;
  const src = $('#src-line');
  src.innerHTML = '';
  if (m.topology_source === 'live') {
    const t = m.lab_read_at ? new Date(m.lab_read_at).toLocaleTimeString() : '';
    src.textContent = 'Read from CML ' + t;
    const bad = Object.entries(m.cml_reads || {}).filter(([, v]) => v).map(([k]) => k);
    if (bad.length) {
      const w = el('span', 's-warn', '  (not answered: ' + bad.join(', ') + ')');
      w.title = Object.entries(m.cml_reads).filter(([, v]) => v).map(([k, v]) => k + ': ' + v).join('\n');
      src.appendChild(w);
    }
  } else src.appendChild(el('span', 'snap', 'CML UNREACHABLE: cached snapshot'));

  // Counts follow the site selection, so "3 stopped" means 3 stopped HERE.
  const scope = NODES.filter((n) => !n.ghost && (site === 'all' || siteBucket(n) === site));
  const up = scope.filter((n) => n.up).length;
  $('#counts').textContent = `${site === 'all' ? '' : site === 'other' ? 'Carrier/home: ' : site + ': '}` +
    `${up} running, ${scope.length - up} stopped` + (m.counts.absent ? `, ${m.counts.absent} absent` : '');

  const all = NODES.filter((n) => !n.ghost), allUp = all.filter((n) => n.up).length;
  $('#lab-state').textContent = m.topology_source === 'live'
    ? `${allUp} of ${all.length} nodes running` : 'CML unreachable';

  const hb = $('#hidden-btn');
  hb.hidden = hidden.size === 0;
  hb.textContent = `${hidden.size} hidden`;
  hb.title = 'Show hidden nodes again: ' + [...hidden].join(', ');

  const n = selected && BY.get(selected);
  $('#sel-line').textContent = n ? `${n.label}  (${n.node_definition || 'not in lab'})` : '';
}

function renderAll() { renderCanvas(); renderStatus(); renderPane(); renderSide(); }

/* ============================================================== sidebar == */
const SIDE_TABS = [['node', 'Node'], ['addresses', 'Addresses'], ['interfaces', 'Interfaces'],
                   ['dhcp', 'DHCP'], ['ping', 'Ping']];

function renderSide() {
  const tabs = $('#side-tabs'), body = $('#side-body');
  const n = selected && BY.get(selected);
  tabs.innerHTML = '';
  const scroll = body.scrollTop;
  body.innerHTML = '';
  if (!n) {
    tabs.appendChild(el('button', 'tab on', 'Lab'));
    body.appendChild(labSummary());
    return;
  }
  SIDE_TABS.forEach(([k, t]) => {
    if (k === 'dhcp' && !n.dhcp) return;
    if (k === 'ping' && n.ghost) return;
    const b = el('button', 'tab' + (sideTab === k ? ' on' : ''), t);
    if (k === 'ping' && probeJobs.some((j) => j.target === n.label && !['done', 'error'].includes(j.status))) {
      b.appendChild(el('span', 'n', 'running'));
    }
    b.addEventListener('click', () => { sideTab = k; renderSide(); });
    tabs.appendChild(b);
  });
  const close = el('button', 'tab close');
  close.title = 'Clear selection (Esc)';
  close.appendChild(X_ICON());
  close.style.marginLeft = 'auto';
  close.addEventListener('click', () => { select(null); document.body.classList.remove('side-open'); });
  tabs.appendChild(close);

  const fn = { node: sideNode, addresses: sideAddresses, interfaces: sideInterfaces,
               dhcp: sideDhcp, ping: sidePing }[sideTab] || sideNode;
  body.appendChild(fn(n));
  body.scrollTop = scroll;
}

function sect(title, content) {
  const s = el('div', 'sect');
  if (title) s.appendChild(el('h4', null, title));
  if (content) (Array.isArray(content) ? content : [content]).forEach((c) => c && s.appendChild(c));
  return s;
}

function props(rows) {
  const t = el('table', 'p');
  rows.forEach(([k, v, cls]) => {
    const tr = el('tr', cls || null);
    tr.appendChild(el('td', null, k));
    const td = el('td');
    if (v && v.nodeType) td.appendChild(v); else td.textContent = v == null || v === '' ? '-' : v;
    tr.appendChild(td);
    t.appendChild(tr);
  });
  return t;
}

function copyable(text, mono = true) {
  if (!text) return '-';
  const s = el('span', mono ? 'm' : null, text);
  const b = el('button', 'copy', 'copy');
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      b.textContent = 'copied'; setTimeout(() => (b.textContent = 'copy'), 1000);
    });
  });
  s.appendChild(b);
  return s;
}

function colored(text, cls) { return el('span', cls, text); }

/* Nothing selected: the lab's own properties, like CML's lab sidebar. */
function labSummary() {
  const d = el('div');
  const m = INV.meta;
  const all = NODES.filter((n) => !n.ghost);
  d.appendChild(sect('Lab', props([
    ['Topology', m.topology_source === 'live' ? 'live from CML'
      : colored(`cached snapshot, ${m.snapshot && m.snapshot.age_hours} h old`, 's-bad')],
    ['Zones', 'p45.local, ' + INV.public_zone],
    ['Nodes', `${all.length} in lab, ${all.filter((n) => n.up).length} running`],
    ['Undeclared', m.counts.undeclared ? colored(String(m.counts.undeclared), 's-warn') : '0'],
    ['Absent', m.counts.absent ? colored(String(m.counts.absent), 's-bad') : '0'],
    ['Last read', m.lab_read_at ? new Date(m.lab_read_at).toLocaleTimeString() : '-'],
    ...Object.entries(m.cml_reads || {}).map(([k, v]) => ['  ' + k.replace(/_/g, ' '), v ? colored('failed: ' + v, 's-bad') : colored('ok', 's-ok')]),
    ['Manifest', m.manifest ? 'loaded, used for comparison' : 'not found (nothing to compare)'],
  ])));

  const byKind = new Map();
  NODES.forEach((n) => {
    const k = kind(n);
    if (!byKind.has(k)) byKind.set(k, { up: 0, all: 0 });
    const r = byKind.get(k); r.all++; if (n.up) r.up++;
  });
  d.appendChild(sect('Device types', props([...byKind.entries()]
    .sort((a, b) => b[1].all - a[1].all)
    .map(([k, r]) => [KIND_NAME[k], `${r.up} of ${r.all} running`]))));

  const rows = [];
  const link = (n, text, cls) => {
    const a = el('a', cls, text);
    a.href = '#';
    a.addEventListener('click', (e) => { e.preventDefault(); select(n.label, true); });
    rows.push([n.label, a]);
  };
  NODES.filter((n) => n.ghost).forEach((n) => link(n, 'in manifest, not in lab', 's-bad'));
  NODES.filter((n) => n.config_status === 'undeclared').forEach((n) => link(n, 'in lab, not in manifest', 's-warn'));
  NODES.filter((n) => n.live_ip && n.live_ip !== n.primary_ip)
    .forEach((n) => link(n, `address ${n.live_ip}, manifest ${n.primary_ip || 'none'}`, 's-warn'));
  NODES.filter((n) => (n.issues || []).length)
    .forEach((n) => link(n, n.issues.map((i) => `#${i.id} ${i.title}`).join('; ')));
  d.appendChild(sect('Needs attention', rows.length ? props(rows) : el('div', 'side-empty', 'Nothing flagged.')));
  d.appendChild(el('div', 'side-empty',
    'Click a node to inspect it. Right-click for actions. Drag to pan, wheel to zoom, F to fit.'));
  return d;
}

const SRC_NAME = { cml: 'CML', 'cml-l3': 'seen by CML', 'cml-config': 'CML config', device: 'device',
                   manifest: 'manifest', label: 'from label' };
/* A value plus where it came from, so the sidebar never passes the plan
 * off as the lab. */
function withSrc(value, src) {
  const box = el('span');
  if (value && value.nodeType) box.appendChild(value); else box.appendChild(document.createTextNode(value == null || value === '' ? '-' : value));
  if (src) {
    const t = el('span', 'src' + (src === 'manifest' || src === 'label' ? ' s-warn' : ''), SRC_NAME[src] || src);
    t.title = src === 'manifest' ? 'CML did not provide this; shown from the manifest'
      : src === 'label' ? 'Guessed from the node label' : 'Read from the CML API on the last refresh';
    box.appendChild(t);
  }
  return box;
}

function sideNode(n) {
  const d = el('div');
  const [sw, sc] = stateWord(n);
  const ip = displayIp(n);
  const src = n.sources || {};
  d.appendChild(sect('General', props([
    ['Label', withSrc(n.label, n.ghost ? 'manifest' : 'cml')],
    ['State', withSrc(colored(sw, sc), n.ghost ? null : 'cml')],
    ['Type', KIND_NAME[kind(n)]],
    ['Image', withSrc(n.image || n.node_definition, n.ghost ? null : 'cml')],
    ['Resources', n.cpus || n.ram ? `${n.cpus || '?'} vCPU, ${n.ram || '?'} MB` : '-'],
    ['Tags', (n.tags || []).length ? withSrc(n.tags.join(', '), 'cml') : '-'],
    ['Role', withSrc(n.role_title, src.role)],
    ['Layer', withSrc(n.layer_title || n.layer, src.layer)],
    ['Site', withSrc(n.site, src.site)],
    ['Address', ip.text ? withSrc(copyable(ip.text), ip.live ? 'device' : src.primary_ip) : '-', ip.drift ? 'drift' : null],
    ['DNS name', withSrc(copyable((n.dns || {}).fqdn), src.dns)],
    ['Gateway', withSrc(n.gateway, n.gateway ? src.gateway : null)],
    ['Resolver', withSrc(n.resolver, n.resolver ? src.resolver : null)],
    ['In manifest', { managed: 'yes, ' + (n.template || ''), undeclared: colored('no', 's-warn'),
                      absent: colored('yes, but not in the lab', 's-bad'), 'no-template': 'yes, no template' }[n.config_status] || '-'],
  ])));
  if (ip.drift) d.appendChild(el('div', 'pmsg warn', `The device holds ${n.live_ip}; the manifest declares ${n.primary_ip || 'nothing'}.`));

  const rows = [];
  (n.vlans || []).forEach((v) => rows.push(['VLAN ' + v.id, withSrc(`${v.name || ''}${v.iface ? '  (' + v.iface + ')' : ''}`, src.vlans)]));
  Object.entries(n.ha || {}).forEach(([k, v]) => rows.push([k.replace(/_/g, ' '), withSrc(String(v), src.ha)]));
  if (n.floor) rows.push(['Location', withSrc(n.floor, 'manifest')]);
  if (rows.length) d.appendChild(sect('VLANs and redundancy', props(rows)));

  // plan against lab: only the fields where they disagree
  const dec = n.declared;
  if (dec) {
    const diff = [];
    if (dec.site && dec.site !== n.site) diff.push(['Site', `manifest ${dec.site}, CML ${n.site || '-'}`]);
    if (dec.role && dec.role !== n.role) diff.push(['Role', `manifest ${dec.role}, CML ${n.role || '-'}`]);
    if (dec.primary_ip && dec.primary_ip !== n.primary_ip) diff.push(['Address', `manifest ${dec.primary_ip}, lab ${n.primary_ip || 'none'}`]);
    const labIps = new Set((n.addresses || []).filter((a) => a.source !== 'manifest').map((a) => a.value));
    const gone = (dec.addresses || []).filter((a) => a.kind !== 'ha' && !labIps.has(a.value));
    if (gone.length && labIps.size) diff.push(['Not in the lab', gone.map((a) => `${a.value} (${a.label})`).join(', ')]);
    d.appendChild(sect('Lab against manifest', diff.length ? props(diff.map(([k, v]) => [k, colored(v, 's-warn')]))
      : el('div', 'side-empty', 'No differences in site, role or addresses.')));
  }
  if ((n.issues || []).length) {
    d.appendChild(sect('Known issues', props(n.issues.map((i) => ['#' + i.id, i.title]))));
  }
  if (!n.ghost && !n.can_source_probe && n.no_source_reason) {
    d.appendChild(el('div', 'pmsg', 'Cannot send test traffic: ' + n.no_source_reason + '. It can still be a target.'));
  }

  const acts = el('div', 'acts');
  if (!n.ghost) {
    const c = el('button', null, 'Console log');
    c.addEventListener('click', () => openConsole(n.label));
    acts.appendChild(c);
    const p = el('button', null, 'Ping...');
    p.addEventListener('click', () => { sideTab = 'ping'; renderSide(); });
    acts.appendChild(p);
    if (n.can_source_probe && n.up) {
      const t = el('button', null, 'Trace from here...');
      t.addEventListener('click', () => startPick(n.label));
      acts.appendChild(t);
    }
  }
  const ce = el('button', null, 'Center');
  ce.addEventListener('click', () => centerOn(n, true));
  acts.appendChild(ce);
  const h = el('button', null, 'Hide');
  h.title = 'Hide from the canvas and node table (restore from the status bar)';
  h.addEventListener('click', () => hideNode(n.label));
  acts.appendChild(h);
  d.appendChild(acts);
  return d;
}

function sideAddresses(n) {
  const d = el('div');
  // one table, every source side by side: seen on the wire by CML, in the
  // config CML holds, read off the console, declared in the manifest
  const rows = new Map();
  (n.addresses || []).forEach((a) => {
    const r = rows.get(a.value) || { ip: a.value, iface: a.iface, kind: a.kind, label: a.label, src: new Set() };
    r.src.add(a.source);
    if (!r.iface && a.iface) r.iface = a.iface;
    rows.set(a.value, r);
  });
  ((n.live || {}).addresses || []).forEach((a) => {
    const r = rows.get(a.ip) || { ip: a.ip, iface: a.iface, src: new Set() };
    r.src.add('device');
    if (!r.iface) r.iface = a.iface;
    rows.set(a.ip, r);
  });
  if (rows.size) {
    const t = gridTable(['Address', 'Interface', 'Found in']);
    [...rows.values()].forEach((r) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'm', r.ip));
      tr.appendChild(el('td', 'm', (r.iface || r.label || '-') + (r.kind && r.kind !== 'mgmt' && r.kind !== 'service' ? '  ' + r.kind : '')));
      const only = r.src.size === 1 && r.src.has('manifest');
      tr.appendChild(el('td', only ? 's-warn' : null, [...r.src].map((x) => SRC_NAME[x] || x).join(', ') +
        (only ? '  (not found in the lab)' : '')));
      t.appendChild(tr);
    });
    d.appendChild(sect('Addresses', t));
  } else {
    d.appendChild(sect('Addresses', el('div', 'side-empty', 'CML reports no addresses for this node, and its config sets none.')));
  }
  const live = n.live;
  if (live && live.error) d.appendChild(el('div', 'pmsg bad', 'Console read failed at ' + live.at + ': ' + live.error));
  else if (live) d.appendChild(el('div', 'pmsg', 'Console read at ' + new Date(live.at).toLocaleString() + '.'));
  const acts = el('div', 'acts');
  const b = el('button', null, live ? 'Read consoles again' : 'Read running addresses from consoles');
  b.title = '"Seen by CML" and "CML config" refresh on their own. This reads the running state off each device console (about 40 s for the lab).';
  b.addEventListener('click', () => { LAB.harvest.run(); showPane('harvest'); });
  acts.appendChild(b);
  d.appendChild(acts);

  const dns = n.dns || {};
  const drows = [];
  if (dns.fqdn) drows.push(['Internal name', withSrc(copyable(dns.fqdn), (n.sources || {}).dns)]);
  (dns.cnames || []).forEach((c) => drows.push(['Alias', copyable(c)]));
  (dns.public || []).forEach((p) => drows.push(['Public record', copyable(p.name + ' ' + p.ip)]));
  if (drows.length) d.appendChild(sect('DNS', props(drows)));
  return d;
}

function sideInterfaces(n) {
  const d = el('div');
  const head = el('div', 'phead');
  head.appendChild(el('span', 'd', `${(n.iface_detail || []).length || n.interface_count || 0} interfaces, ${(n.links || []).length} connected`));
  const lab = el('label');
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = prefs.traffic;
  cb.addEventListener('change', () => setTraffic(cb.checked));
  lab.appendChild(cb); lab.appendChild(document.createTextNode('Live counters'));
  head.appendChild(lab);
  d.appendChild(head);
  const links = new Map((n.links || []).map((l) => [l.local_iface, l]));
  const ifs = (n.iface_detail || []).length ? n.iface_detail
    : (n.links || []).map((l) => ({ name: l.local_iface, connected: true, ip4: [] }));
  if (!ifs.length) { d.appendChild(el('div', 'side-empty', 'No interfaces.')); return d; }
  const t = gridTable(['Interface', 'Peer', 'Address', 'Rate']);
  ifs.forEach((f) => {
    const l = links.get(f.name);
    const r = l && prefs.traffic ? linkRates[l.link_id] : null;
    const tr = el('tr', 'row' + (l ? '' : ' off'));
    const c0 = el('td', 'm', shortIf(f.name));
    c0.title = [f.name, f.mac && 'MAC ' + f.mac, f.desc, f.vlan && 'access VLAN ' + f.vlan, f.shutdown && 'shutdown in config']
      .filter(Boolean).join('\n');
    tr.appendChild(c0);
    const peer = el('td', 'm');
    if (l) {
      const a = el('a', l.state === 'STOPPED' ? 's-dim' : null, l.peer + ' ' + shortIf(l.peer_iface));
      a.href = '#';
      a.addEventListener('click', (e) => { e.preventDefault(); select(l.peer, true); });
      peer.appendChild(a);
      if (l.state === 'STOPPED') peer.appendChild(el('span', 'src', 'link stopped'));
    } else peer.textContent = '-';
    tr.appendChild(peer);
    tr.appendChild(el('td', 'm', (f.ip4 || []).concat(f.config_ip && !(f.ip4 || []).includes(f.config_ip) ? [f.config_ip] : []).join(', ') || (l && l.subnet) || '-'));
    tr.appendChild(el('td', 'm' + (r && r.drops > 0 ? ' s-bad' : ''), r ? (r.pps >= 1 ? Math.round(r.pps) + ' pps' : 'idle') + (r.drops ? `, ${r.drops} drops` : '') : '-'));
    t.appendChild(tr);
  });
  d.appendChild(t);
  return d;
}

function sideDhcp(n) {
  const d = el('div');
  d.appendChild(sect('Design', props([
    [n.dhcp.kind === 'client' ? 'Leases on' : 'Probe SVI',
     `${n.dhcp.interface} (VLAN ${n.dhcp.vlan}${n.dhcp.vlan_name ? ' ' + n.dhcp.vlan_name : ''})`],
    ['Scope', n.dhcp.scope],
    ['Server', n.dhcp.expected_server],
    ['Lease length', n.dhcp.expected_lease_secs / 3600 + ' h'],
  ])));

  if (n.probe_family === 'ios' && n.up) {
    const out = el('div');
    const acts = el('div', 'acts');
    const b = el('button', 'default', 'Read live lease');
    b.title = 'Runs show dhcp lease on the node; about 15 s';
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Reading...';
      try {
        const j = await getJSON('/api/dhcp/' + encodeURIComponent(n.label));
        out.innerHTML = '';
        if (j.error) out.appendChild(el('div', 'pmsg warn', j.error));
        else {
          out.appendChild(props([
            ['Address', j.address + ' / ' + (j.mask || '')],
            ['Server', j.server_matches ? j.server : colored(j.server + ' (not the expected server)', 's-bad')],
            ['State', j.state],
            ['Renews in', fmtSecs(j.renewal_secs)],
            ['Rebinds in', fmtSecs(j.rebind_secs)],
          ]));
          startLeaseTicker(out, j);
        }
      } catch (e) { out.appendChild(el('div', 'pmsg bad', String(e))); }
      b.disabled = false; b.textContent = 'Read live lease';
    });
    acts.appendChild(b);
    d.appendChild(sect('Live lease', [acts, out]));
  } else if (n.dhcp.kind === 'client') {
    d.appendChild(el('div', 'pmsg',
      'The remaining lease time exists only on the node itself, and this image has no command that reports it.'));
  }
  return d;
}

function fmtSecs(s) {
  if (s == null) return '-';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h} h ${m} min (${s} s)`;
}

/* Tick the renewal countdown client-side from the moment it was read.  The
 * number is honest: it names its read time rather than pretending to be live. */
function startLeaseTicker(container, lease) {
  if (lease.renewal_secs == null) return;
  const line = el('div', 'pmsg');
  container.appendChild(line);
  const t0 = Date.now();
  const paint = () => {
    const left = lease.renewal_secs - Math.floor((Date.now() - t0) / 1000);
    line.textContent = left > 0
      ? `Renewal in ${fmtSecs(left)}, counted from the read at ${lease.read_at}.`
      : 'Renewal time has passed. Read again for the current lease.';
  };
  paint();
  const iv = setInterval(() => { if (!line.isConnected) clearInterval(iv); else paint(); }, 1000);
}

/* ---------------------------------------------------------------- ping --- */
function sidePing(n) {
  const d = el('div');
  const sources = NODES.filter((x) => x.can_source_probe && x.up && x.label !== n.label).sort(byLabel);
  if (!sources.length) {
    d.appendChild(el('div', 'pmsg warn', 'No running node can source a ping right now.'));
  } else {
    const f = el('div', 'form');
    const l1 = el('label', null, 'From');
    const src = el('select');
    sources.forEach((x) => {
      const o = el('option', null, x.label + (x.site ? '  (' + x.site + ')' : ''));
      o.value = x.label;
      src.appendChild(o);
    });
    if (n.lastSource && sources.some((x) => x.label === n.lastSource)) src.value = n.lastSource;
    l1.appendChild(src);
    f.appendChild(l1);

    // Target list: live-read addresses first (what the device actually holds
    // right now), manifest addresses after, labelled so it's clear which is
    // which.
    const l2 = el('label', null, 'To address');
    const dst = el('select');
    const liveAddrs = ((n.live || {}).addresses || []).filter((a) => a.proto === 'up');
    const liveIps = new Set(liveAddrs.map((a) => a.ip));
    liveAddrs.forEach((a) => {
      const o = el('option', null, `${a.ip}  ${a.iface}, live${a.ip === n.primary_ip ? ', manifest primary' : ''}`);
      o.value = a.ip;
      dst.appendChild(o);
    });
    (n.addresses || []).forEach((a) => {
      if (liveIps.has(a.value)) return;
      const o = el('option', null, `${a.value}  ${a.label}, manifest${n.live ? ', not seen live' : ''}`);
      o.value = a.value;
      dst.appendChild(o);
    });
    if (!dst.children.length) {
      const o = el('option', null, 'No address known. Read addresses first.');
      o.value = ''; o.disabled = true;
      dst.appendChild(o);
    }
    l2.appendChild(dst);
    f.appendChild(l2);

    const row = el('div', 'row2');
    const lab = el('label');
    lab.style.display = 'flex';
    const cDns = el('input'); cDns.type = 'checkbox';
    const hasShellVantage = NODES.some((x) => x.probe_family === 'linux' && x.up);
    cDns.checked = !!(n.dns || {}).fqdn && hasShellVantage;
    cDns.disabled = !(n.dns || {}).fqdn || !hasShellVantage;
    lab.appendChild(cDns);
    lab.appendChild(document.createTextNode('Also resolve the DNS name'));
    row.appendChild(lab);
    const go = el('button', 'default', 'Ping');
    go.style.marginLeft = 'auto';
    go.addEventListener('click', () => {
      n.lastSource = src.value;
      runProbe(src.value, n.label, dst.value || null, cDns.checked ? ['ip', 'dns'] : ['ip']);
    });
    row.appendChild(go);
    f.appendChild(row);
    d.appendChild(sect('Ping ' + n.label, f));
  }
  const mine = probeJobs.filter((j) => j.target === n.label).slice(0, 6);
  if (mine.length) d.appendChild(sect('Results', mine.map(jobBlock)));
  return d;
}

async function runProbe(source, target, target_ip, methods) {
  const job = { source, target, target_ip, status: 'queued', stage: 'queued', eta: 25, t0: Date.now() };
  probeJobs.unshift(job);
  refreshProbeViews();
  try {
    const r = await fetch('/api/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, target, methods, count: 5, target_ip }),
    });
    const j = await r.json();
    if (!r.ok) { job.status = 'error'; job.error = j.error; refreshProbeViews(); return; }
    Object.assign(job, { eta: j.eta_seconds || 25, target_ip: j.target_ip, status: 'running' });
    refreshProbeViews();
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 1500));
      const res = await getJSON('/api/probe/' + j.job_id);
      job.stage = res.stage || job.stage;
      if (res.status === 'done') { job.status = 'done'; job.result = res.result; break; }
      if (res.status === 'error') { job.status = 'error'; job.error = res.error; break; }
      document.querySelectorAll(`[data-job="${job.t0}"]`).forEach((s) => { s.textContent = job.stage; });
    }
  } catch (e) { job.status = 'error'; job.error = String(e); }
  refreshProbeViews();
}

function refreshProbeViews() {
  renderPathOverlay();
  if (selected && sideTab === 'ping') renderSide();
  if (paneTab === 'pings') renderPaneBody();
  renderPaneTabs();
}

function jobBlock(job) {
  if (job.status === 'done') return resultBlock(job.result);
  const box = el('div', 'result');
  const h = el('div', 'h');
  h.appendChild(el('span', null, `${job.source} to ${job.target}`));
  if (job.status === 'error') {
    h.appendChild(el('span', 's-bad', 'failed'));
    box.appendChild(h);
    box.appendChild(el('div', 's-bad', job.error));
    return box;
  }
  h.appendChild(el('span', 's-dim', job.target_ip || ''));
  box.appendChild(h);
  const st = el('div', 's-dim', job.stage);
  st.dataset.job = String(job.t0);
  box.appendChild(st);
  const bar = el('div', 'meter'); const fill = el('i'); bar.appendChild(fill);
  const pct = () => Math.min(97, (Date.now() - job.t0) / 1000 / job.eta * 100) + '%';
  fill.style.width = pct();
  const iv = setInterval(() => { if (!fill.isConnected) clearInterval(iv); else fill.style.width = pct(); }, 500);
  box.appendChild(bar);
  return box;
}

function resultBlock(res) {
  const v = res.verdict || {};
  const box = el('div', 'result');
  const h = el('div', 'h');
  h.appendChild(el('span', null, `${res.source} to ${res.target}`));
  h.appendChild(el('span', v.level === 'ok' ? 's-ok' : v.level === 'fail' ? 's-bad' : 's-warn', v.text || ''));
  box.appendChild(h);
  const bits = [];
  const p = res.ping_ip || res.ping_dns;
  if (p) {
    bits.push(`loss ${p.loss_pct}% (${p.received}/${p.sent})`);
    if (p.rtt_ms && p.rtt_ms.avg != null) bits.push(`rtt ${p.rtt_ms.min}/${p.rtt_ms.avg}/${p.rtt_ms.max} ms`);
  }
  if (res.dns && res.dns.ok) {
    bits.push(`${res.dns.fqdn} = ${res.dns.addresses[0]}` +
      (res.dns.matches_manifest === false ? ' (does NOT match manifest)' : ''));
  } else if (res.dns) {
    bits.push('dns: ' + (res.dns.reason || 'no answer'));
  }
  bits.push(res.elapsed_s + ' s');
  box.appendChild(el('div', 'm', bits.join('   ')));
  if (p && p.packet_pattern) {
    const pat = el('div', 'pattern');
    [...p.packet_pattern].forEach((ch) => pat.appendChild(el('span', ch === '!' ? null : 'miss', ch)));
    box.appendChild(pat);
  }
  (res.notes || []).forEach((t) => box.appendChild(el('div', 's-dim', t)));
  return box;
}

/* =========================================================== bottom pane == */
function showPane(k) {
  const was = paneTab;
  paneTab = k;
  if ((was === 'trace') !== (k === 'trace')) setTimeout(() => { renderCanvas(); }, 0);
  if (prefs.paneMin) { prefs.paneMin = false; savePrefs(); applyPane(); }
  renderPane();
  if (LAB[k] && LAB[k].refresh) LAB[k].refresh();
}

function renderPane() { renderPaneTabs(); renderPaneBody(); }

function renderPaneTabs() {
  const tabs = $('#pane-tabs');
  tabs.innerHTML = '';
  const add = (key, title, badge, closable) => {
    const b = el('button', 'tab' + (paneTab === key ? ' on' : ''), title);
    if (badge) b.appendChild(el('span', 'n ' + (badge.level || ''), badge.text));
    b.addEventListener('click', () => showPane(key));
    if (closable) {
      const x = el('span', 'close');
      x.title = 'Close';
      x.appendChild(X_ICON());
      x.addEventListener('click', (e) => { e.stopPropagation(); closeConsole(key.slice(4)); });
      b.appendChild(x);
    }
    tabs.appendChild(b);
  };
  const vis = NODES.filter((n) => shown(n) && matches(n)).length;
  add('nodes', 'Nodes', { text: String(vis) });
  add('trace', 'Path trace', trace.job ? { text: 'running', level: 'warn' }
    : trace.result ? { text: trace.result.verdict.level === 'ok' ? 'confirmed' : trace.result.verdict.level === 'fail' ? 'failed' : 'check',
                       level: trace.result.verdict.level === 'ok' ? 'ok' : trace.result.verdict.level === 'fail' ? 'bad' : 'warn' } : null);
  Object.entries(LAB).forEach(([k, t]) => add(k, t.title, t.badge()));
  consoles.forEach((l) => add('con:' + l, 'Console: ' + l, null, true));
  const min = el('button', 'end', prefs.paneMin ? 'Restore' : 'Minimize');
  min.addEventListener('click', () => { prefs.paneMin = !prefs.paneMin; savePrefs(); applyPane(); renderPane(); });
  tabs.appendChild(min);
}

function renderPaneBody() {
  if (prefs.paneMin) return;
  const body = $('#pane-body');
  const scroll = body.scrollTop;
  body.innerHTML = '';
  if (paneTab === 'nodes') body.appendChild(nodeTable());
  else if (paneTab.startsWith('con:')) body.appendChild(consolePane(paneTab.slice(4)));
  else if (paneTab === 'trace') body.appendChild(tracePane());
  else if (LAB[paneTab]) LAB[paneTab].draw(body);
  body.scrollTop = scroll;
}

function applyPane() {
  document.documentElement.style.setProperty('--pane-h', prefs.paneH + 'px');
  $('.center').classList.toggle('pane-min', prefs.paneMin);
}

/* ------------------------------------------------------------ node table - */
const COLS = [
  ['label', 'Node', (n) => n.label],
  ['state', 'State', (n) => stateWord(n)[0]],
  ['kind', 'Type', (n) => KIND_NAME[kind(n)]],
  ['role', 'Role', (n) => n.role_title || ''],
  ['site', 'Site', (n) => n.site || ''],
  ['ip', 'Address', (n) => displayIp(n).text],
  ['dns', 'DNS name', (n) => (n.dns || {}).fqdn || ''],
  ['issues', 'Flags', (n) => flags(n).join(', ')],
];

function flags(n) {
  const f = [];
  if (n.ghost) f.push('absent');
  if (n.config_status === 'undeclared') f.push('undeclared');
  if (displayIp(n).drift) f.push('address drift');
  (n.issues || []).forEach((i) => f.push('#' + i.id));
  return f;
}

function groupKey(n) {
  return {
    layer: () => [n.layer, n.layer_title || n.layer],
    site:  () => [n.site || '~', n.site || 'No site (carrier / shared)'],
    role:  () => [n.role || '~', n.role_title || 'Not declared in manifest'],
    state: () => n.ghost ? ['c', 'Declared but absent'] : n.up ? ['a', 'Running'] : ['b', 'Stopped'],
    none:  () => ['', ''],
  }[prefs.groupBy]();
}

function nodeTable() {
  const wrap = el('div');
  const head = el('div', 'phead');
  const gl = el('label', null, 'Group by ');
  const gs = el('select');
  [['layer', 'Layer'], ['site', 'Site'], ['role', 'Role'], ['state', 'State'], ['none', 'None']].forEach(([v, t]) => {
    const o = el('option', null, t); o.value = v; gs.appendChild(o);
  });
  gs.value = prefs.groupBy;
  gs.addEventListener('change', () => { prefs.groupBy = gs.value; savePrefs(); renderPaneBody(); });
  gl.appendChild(gs);
  head.appendChild(gl);
  const hl = el('label');
  const hc = el('input'); hc.type = 'checkbox'; hc.checked = prefs.hideDown;
  hc.addEventListener('change', () => { prefs.hideDown = hc.checked; savePrefs(); refilter(); });
  hl.appendChild(hc); hl.appendChild(document.createTextNode('Hide stopped'));
  head.appendChild(hl);
  head.appendChild(el('span', 'd', 'Double-click a row for its console log. Right-click for actions.'));
  wrap.appendChild(head);

  const vis = NODES.filter((n) => shown(n) && matches(n));
  if (!vis.length) { wrap.appendChild(el('div', 'pmsg', 'No nodes match the filter.')); return wrap; }
  const col = COLS.find((c) => c[0] === prefs.sort) || COLS[0];
  vis.sort((a, b) => (String(col[2](a)).localeCompare(String(col[2](b)), undefined, { numeric: true }) || byLabel(a, b)) * prefs.sortDir);

  const t = el('table', 'g');
  const hr = el('tr');
  COLS.forEach(([k, title]) => {
    const th = el('th', 'sortable' + (prefs.sort === k ? ' sorted' : ''), title + (prefs.sort === k ? (prefs.sortDir > 0 ? '  (a-z)' : '  (z-a)') : ''));
    th.addEventListener('click', () => {
      prefs.sortDir = prefs.sort === k ? -prefs.sortDir : 1;
      prefs.sort = k; savePrefs(); renderPaneBody();
    });
    hr.appendChild(th);
  });
  t.appendChild(hr);

  const groups = new Map();
  vis.forEach((n) => {
    const [k, title] = groupKey(n);
    if (!groups.has(k)) groups.set(k, { title, nodes: [] });
    groups.get(k).nodes.push(n);
  });
  const order = prefs.groupBy === 'layer'
    ? INV.layers.map((l) => l.key).filter((k) => groups.has(k)) : [...groups.keys()].sort();
  order.forEach((k) => {
    const g = groups.get(k);
    if (prefs.groupBy !== 'none') {
      const tr = el('tr', 'grp');
      const td = el('td', null, `${g.title}   ${g.nodes.filter((n) => n.up).length} of ${g.nodes.length} running`);
      td.colSpan = COLS.length;
      tr.appendChild(td);
      t.appendChild(tr);
    }
    g.nodes.forEach((n) => t.appendChild(nodeRow(n)));
  });
  wrap.appendChild(t);
  return wrap;
}

function nodeRow(n) {
  const tr = el('tr', 'row' + (n.up || n.ghost ? '' : ' off') + (n.label === selected ? ' sel' : ''));
  tr.dataset.label = n.label;
  const c0 = el('td'); c0.appendChild(lamp(n)); c0.appendChild(document.createTextNode(n.label)); tr.appendChild(c0);
  const [sw, sc] = stateWord(n);
  tr.appendChild(el('td', sc, sw));
  tr.appendChild(el('td', null, KIND_NAME[kind(n)]));
  tr.appendChild(el('td', null, n.role_title || ''));
  tr.appendChild(el('td', null, n.site || ''));
  const ip = displayIp(n);
  const ipSrc = ip.live ? null : (n.sources || {}).primary_ip;
  tr.appendChild(el('td', 'm' + (ip.drift || ipSrc === 'manifest' ? ' s-warn' : ''),
    ip.text + (ip.text && ipSrc === 'manifest' ? '  (manifest only)' : '')));
  tr.appendChild(el('td', 'm', (n.dns || {}).fqdn || ''));
  const fl = flags(n);
  tr.appendChild(el('td', fl.includes('absent') ? 's-bad' : fl.length ? 's-warn' : null, fl.join(', ')));
  tr.addEventListener('click', () => { if (pick) pickNode(n.label); else select(n.label, true); });
  tr.addEventListener('dblclick', () => { if (!n.ghost) openConsole(n.label); });
  tr.addEventListener('contextmenu', (e) => { e.preventDefault(); select(n.label); nodeMenu(n, e.clientX, e.clientY); });
  tr.addEventListener('mouseenter', () => { hovered = n.label; highlight(); });
  tr.addEventListener('mouseleave', () => { hovered = null; highlight(); });
  return tr;
}

function markTableSelection() {
  document.querySelectorAll('#pane-body tr.row').forEach((tr) => tr.classList.toggle('sel', tr.dataset.label === selected));
}

function refilter() {
  renderPaneTabs();
  if (paneTab === 'nodes') renderPaneBody();
  highlight(); renderStatus();
}

/* ------------------------------------------------------------- console --- */
/* Console scrollback with no attach.  For a container, line 0 is the service's
 * own log (dnsmasq queries, RADIUS rejections), which is direct evidence of
 * what the service did, at ~0.03s instead of a 6s console attach. */
function openConsole(label) {
  if (!consoles.includes(label)) consoles.push(label);
  showPane('con:' + label);
  if (!consoleText.has(label)) readConsole(label);
}
function closeConsole(label) {
  const i = consoles.indexOf(label);
  if (i >= 0) consoles.splice(i, 1);
  if (paneTab === 'con:' + label) paneTab = consoles.length ? 'con:' + consoles[consoles.length - 1] : 'nodes';
  renderPane();
}
async function readConsole(label) {
  consoleText.set(label, 'reading...');
  if (paneTab === 'con:' + label) renderPaneBody();
  try {
    const j = await getJSON('/api/console-log/' + encodeURIComponent(label) + '?lines=60');
    consoleText.set(label, j.error ? 'error: ' + j.error : ((j.log || '').trim() || '(empty)'));
  } catch (e) { consoleText.set(label, 'error: ' + e); }
  if (paneTab === 'con:' + label) renderPaneBody();
}
function consolePane(label) {
  const n = BY.get(label);
  const wrap = el('div', 'term-wrap');
  const head = el('div', 'phead');
  const b = el('button', null, 'Read again');
  b.addEventListener('click', () => readConsole(label));
  head.appendChild(b);
  head.appendChild(el('span', 'd', n && n.console_line === 1
    ? 'Console line 0 (service output). Read over REST, no console attach. Last 60 lines.'
    : 'Console line 0 scrollback. Read over REST, no console attach. Last 60 lines.'));
  wrap.appendChild(head);
  const sc = el('div', 'scroll');
  sc.appendChild(el('pre', 'term', consoleText.get(label) || ''));
  wrap.appendChild(sc);
  requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
  return wrap;
}

/* ===================================================== lab-wide checks ==== */
/* Each check has the same run/poll shape: a status endpoint, a run endpoint,
 * a 4s poll while running, and a tab in the bottom pane.  The poll keeps
 * going while the tab is closed so the tab's count stays honest. */
function labCheck(def) {
  const t = Object.assign({ st: null, timer: null }, def);
  t.refresh = async () => {
    try { t.st = await getJSON(t.statusUrl); } catch (e) { return; }
    if (t.onStatus) t.onStatus(t.st);
    const running = t.st.status === 'running';
    if (running && !t.timer) t.timer = setInterval(t.refresh, 4000);
    if (!running && t.timer) { clearInterval(t.timer); t.timer = null; }
    renderPaneTabs();
    if (paneTab === t.key) renderPaneBody();
  };
  t.run = async () => {
    const r = await fetch(t.runUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const j = await r.json();
    t.runError = j.error || null;
    t.eta = j.eta_seconds ? j : null;
    await t.refresh();
  };
  t.draw = (body) => {
    if (!t.st) { body.appendChild(el('div', 'pmsg', 'Loading...')); return; }
    t.render(body, t.st);
  };
  return t;
}

function checkHead(t, st, label, runningText, desc, extra) {
  const head = el('div', 'phead');
  const b = el('button', 'default', st.status === 'running' ? runningText : label);
  b.disabled = st.status === 'running';
  b.addEventListener('click', () => t.run());
  head.appendChild(b);
  if (st.status === 'running') head.appendChild(el('span', 's-warn', `running, ${st.done_count} of ${st.total}`));
  if (extra) head.appendChild(extra);
  if (st.status === 'error') head.appendChild(el('span', 's-bad', st.error));
  if (t.runError) head.appendChild(el('span', 's-bad', t.runError));
  head.appendChild(el('span', 'd', desc));
  return head;
}

function gridTable(heads) {
  const t = el('table', 'g');
  const hr = el('tr');
  heads.forEach((h) => hr.appendChild(el('th', null, h)));
  t.appendChild(hr);
  return t;
}

const LAB = {};

const MATRIX_OK = ['pass', 'pass-arp', 'as-expected'];
LAB.matrix = labCheck({
  key: 'matrix', title: 'Baseline matrix', statusUrl: '/api/matrix', runUrl: '/api/matrix/run',
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    if (!st.rows || !st.rows.length) return null;
    const bad = st.rows.filter((r) => !MATRIX_OK.includes(r.outcome) && !['source-down', 'target-down'].includes(r.outcome));
    if (st.rows.some((r) => r.outcome === 'leak')) return { text: 'LEAK', level: 'bad' };
    return bad.length ? { text: bad.length + ' failed', level: 'bad' } : { text: 'all pass', level: 'ok' };
  },
  render(box, st) {
    box.appendChild(checkHead(this, st, `Run all ${st.total}`, 'Running...',
      'Paths from probe_matrix.py. "Must fail" rows are perimeter denies; one passing is a leak. Hover a row to see it on the canvas.',
      st.status === 'done' ? el('span', 's-dim', 'finished ' + st.ended) : null));
    const done = new Map((st.rows || []).map((r) => [r.source + '>' + r.dest, r]));
    const t = gridTable(['Result', 'Source', 'Destination', 'Checks', 'Loss', 'RTT']);
    (st.probes || []).forEach((p) => {
      const r = done.get(p.source + '>' + p.dest);
      const [txt, cls] = !r ? [p.expected_fail ? 'must fail' : 'not run', 's-dim']
        : r.outcome === 'pass' ? ['pass', 's-ok']
        : r.outcome === 'pass-arp' ? ['pass (1st packet ARP)', 's-ok']
        : r.outcome === 'as-expected' ? ['denied, as expected', 's-ok']
        : r.outcome === 'leak' ? ['LEAK', 's-bad']
        : r.outcome === 'source-down' ? ['source stopped', 's-warn']
        : r.outcome === 'target-down' ? ['target stopped', 's-warn']
        : [r.verdict, r.verdict === 'partial' ? 's-warn' : 's-bad'];
      const tr = el('tr', 'row');
      tr.appendChild(el('td', cls, txt));
      tr.appendChild(el('td', 'm', p.source));
      tr.appendChild(el('td', 'm', p.dest));
      tr.appendChild(el('td', 'wrap', p.description));
      tr.appendChild(el('td', 'm', r && r.loss_pct != null ? r.loss_pct + '%' : '-'));
      tr.appendChild(el('td', 'm', r && r.rtt_ms != null ? r.rtt_ms + ' ms' : '-'));
      const lvl = !r ? 'run' : MATRIX_OK.includes(r.outcome) ? 'ok'
        : r.outcome === 'leak' || ['dead', 'error'].includes(r.verdict) ? 'bad' : 'warn';
      tr.addEventListener('mouseenter', () => drawPath(p.source, p.dest, lvl));
      tr.addEventListener('mouseleave', renderPathOverlay);
      tr.addEventListener('click', () => { const n = BY.get(p.source); if (n) centerOn(n); });
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

LAB.dns = labCheck({
  key: 'dns', title: 'DNS audit', statusUrl: '/api/dnsaudit', runUrl: '/api/dnsaudit/run',
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    if (!st.rows || !st.rows.length) return null;
    const bad = st.rows.filter((r) => !r.match).length;
    return bad ? { text: bad + ' wrong', level: 'bad' } : { text: 'all match', level: 'ok' };
  },
  render(box, st) {
    box.appendChild(checkHead(this, st, 'Check every name', 'Checking...',
      'Asks each site\'s resolver for every internal name and compares the answer with the manifest.'));
    const rows = st.rows || [];
    if (!rows.length) return;
    const bad = rows.filter((r) => !r.match);
    box.appendChild(el('div', 'pmsg ' + (bad.length ? 'bad' : 'ok'),
      st.status === 'done'
        ? (bad.length ? `${bad.length} of ${rows.length} names do not match the manifest.`
                      : `All ${rows.length} names resolve to what the manifest says.`)
        : `${rows.length} checked so far, ${bad.length} wrong.`));
    const t = gridTable(['Result', 'Name', 'Manifest', 'Resolver answered']);
    // Problems first, then the rest.
    bad.concat(rows.filter((r) => r.match)).forEach((r) => {
      const tr = el('tr', 'row');
      const [txt, cls] = r.match ? ['ok', 's-ok'] : r.ok ? ['STALE', 's-bad'] : ['no answer', 's-warn'];
      tr.appendChild(el('td', cls, txt));
      tr.appendChild(el('td', 'm', r.fqdn));
      tr.appendChild(el('td', 'm', r.expected));
      tr.appendChild(el('td', 'm', r.answer || r.reason || '-'));
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

// Read live addresses off every reachable device's console, so the canvas,
// node table and ping targets can prefer device truth over manifest intent.
LAB.harvest = labCheck({
  key: 'harvest', title: 'Live addresses', statusUrl: '/api/harvest', runUrl: '/api/harvest/run',
  onStatus(st) { applyHarvestToNodes(st); },
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    const read = NODES.filter((n) => n.live);
    if (!read.length) return null;
    const drift = read.filter((n) => n.live.error || (n.live_ip && n.live_ip !== n.primary_ip)).length;
    return drift ? { text: drift + ' differ', level: 'warn' } : { text: 'all match', level: 'ok' };
  },
  render(box, st) {
    const eta = this.eta && st.status === 'running'
      ? el('span', 's-dim', `about ${this.eta.eta_seconds}${this.eta.eta_seconds_max ? '-' + this.eta.eta_seconds_max : ''} s`) : null;
    box.appendChild(checkHead(this, st, 'Read every device', 'Reading...',
      'Reads interface addresses off each device console (IOS, ASA, Linux) instead of trusting the manifest.', eta));
    const nodes = NODES.filter((n) => n.live);
    if (!nodes.length) return;
    const drift = nodes.filter((n) => n.live.error || (n.live_ip && n.live_ip !== n.primary_ip));
    box.appendChild(el('div', 'pmsg ' + (drift.length ? 'warn' : 'ok'),
      st.status === 'done'
        ? (drift.length ? `${drift.length} of ${nodes.length} nodes differ from, or have no match in, the manifest.`
                        : `All ${nodes.length} nodes match their manifest primary address.`)
        : `${nodes.length} read so far, ${drift.length} differ.`));
    const t = gridTable(['Result', 'Node', 'Live address', 'Manifest primary']);
    drift.concat(nodes.filter((n) => !drift.includes(n))).forEach((n) => {
      const tr = el('tr', 'row');
      const [txt, cls] = n.live.error ? ['read failed', 's-bad'] : n.live_ip === n.primary_ip ? ['match', 's-ok'] : ['differs', 's-warn'];
      tr.appendChild(el('td', cls, txt));
      tr.appendChild(el('td', 'm', n.label));
      tr.appendChild(el('td', 'm', n.live.error || n.live_ip || '(no up interface with an address)'));
      tr.appendChild(el('td', 'm', n.primary_ip || '-'));
      tr.addEventListener('click', () => { sideTab = 'addresses'; select(n.label, true); });
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

LAB.pings = {
  key: 'pings', title: 'Ping log',
  badge() {
    const run = probeJobs.filter((j) => !['done', 'error'].includes(j.status)).length;
    return run ? { text: run + ' running', level: 'warn' } : probeJobs.length ? { text: String(probeJobs.length) } : null;
  },
  draw(box) {
    if (!probeJobs.length) {
      box.appendChild(el('div', 'pmsg', 'No pings this session. Select a node, then use its Ping tab.'));
      return;
    }
    probeJobs.forEach((j) => {
      const b = jobBlock(j);
      b.style.cursor = 'pointer';
      b.addEventListener('click', () => { sideTab = 'ping'; select(j.target, true); });
      box.appendChild(b);
    });
  },
};

/* Merge harvested per-node results into the in-memory NODES so the canvas,
 * node table and ping targets reflect live data without an inventory reload.
 * Same rule as app.py::_live_primary: the interface carrying the manifest's
 * declared primary wins over any other manifest-known address. */
function applyHarvestToNodes(st) {
  if (!st || !st.nodes) return;
  let changed = false;
  NODES.forEach((n) => {
    const rec = st.nodes[n.label];
    if (!rec || JSON.stringify(rec) === JSON.stringify(n.live)) return;
    n.live = rec;
    const up = (rec.addresses || []).filter((a) => a.proto === 'up');
    const manifestIps = new Set((n.addresses || []).map((a) => a.value));
    const pick = up.find((a) => a.ip === n.primary_ip) || up.find((a) => manifestIps.has(a.ip)) || up[0];
    n.live_ip = pick ? pick.ip : null;
    changed = true;
  });
  if (changed) {
    renderCanvas();
    if (paneTab === 'nodes') renderPaneBody();
    if (selected) renderSide();
  }
}

/* =========================================================== path trace == */
/* Pick a source and a destination; the canvas narrows to the nodes between
 * them.  Before a run it shows every shortest path over the running part of
 * the lab (the routes the packets COULD take).  A run sends a burst of pings
 * and reads CML's per-link packet counters before and after, so it shows the
 * links the packets DID cross, at layer 2 as well as layer 3.  Run it, stop
 * the active core switch in CML, run it again: the path moves. */
const trace = { source: null, target: null, ip: null, count: 100, repeat: false,
                job: null, stage: '', result: null, cand: null, history: [], error: null };
let pick = null;           // null | 'source' | 'target'

function traceShown() { return !!pick || (paneTab === 'trace' && !!trace.source && !!trace.target); }

function startPick(src) {
  if (src) { trace.source = src; pick = 'target'; } else { pick = 'source'; }
  pickBar();
  renderCanvas();
}
function cancelPick() { pick = null; pickBar(); renderCanvas(); }

function pickNode(label) {
  const n = BY.get(label);
  if (!n) return;
  if (pick === 'source') {
    if (!n.can_source_probe || !n.up) {
      pickBar(`${label} cannot send the test traffic (${!n.up ? 'not running' : 'no console this tool can drive'}). Pick another.`);
      return;
    }
    trace.source = label;
    pick = 'target';
    pickBar();
    renderCanvas();
    return;
  }
  if (label === trace.source || n.ghost) return;
  pick = null;
  pickBar();
  setTraceEnds(trace.source, label);
  showPane('trace');
}

function pickBar(msg) {
  const bar = $('#pickbar');
  bar.hidden = !pick;
  $('#canvas').classList.toggle('picking', !!pick);
  if (!pick) return;
  bar.innerHTML = '';
  bar.appendChild(el('span', 't', 'Trace path'));
  bar.appendChild(el('span', null, msg || (pick === 'source'
    ? 'Click the node to send from. It must be running and have a console (router, switch, firewall or Linux host).'
    : `From ${trace.source}. Now click the destination.`)));
  const c = el('button', 'linkish', 'Cancel');
  c.addEventListener('click', cancelPick);
  bar.appendChild(c);
}

function setTraceEnds(src, dst) {
  const changed = src !== trace.source || dst !== trace.target;
  trace.source = src;
  trace.target = dst;
  const t = BY.get(dst);
  if (changed || !trace.ip) trace.ip = t ? (t.live_ip || t.primary_ip) : null;
  if (changed) {
    trace.result = [...trace.history].reverse().find((h) => h.source === src && h.target === dst) || null;
    trace.error = null;
  }
  trace.cand = candidates(src, dst);
  renderPane();
  renderCanvas();
}

/* Every node and link on SOME shortest path, over running nodes and started
 * links: the set of routes the traffic could take. */
function candidates(src, dst) {
  const up = (n) => n && (n.up || n.label === src);
  const adj = new Map();
  NODES.forEach((n) => {
    if (!up(n)) return;
    adj.set(n.label, (n.links || []).filter((l) => up(BY.get(l.peer)) && l.state !== 'STOPPED'));
  });
  const bfs = (start) => {
    const d = new Map([[start, 0]]);
    const q = [start];
    while (q.length) {
      const u = q.shift();
      (adj.get(u) || []).forEach((l) => { if (!d.has(l.peer)) { d.set(l.peer, d.get(u) + 1); q.push(l.peer); } });
    }
    return d;
  };
  if (!adj.has(src) || !adj.has(dst)) return { nodes: [], links: [], paths: 0, hops: null, depth: {} };
  const ds = bfs(src), dt = bfs(dst);
  if (!ds.has(dst)) return { nodes: [], links: [], paths: 0, hops: null, depth: {} };
  const D = ds.get(dst);
  const on = [...adj.keys()].filter((u) => ds.has(u) && dt.has(u) && ds.get(u) + dt.get(u) === D);
  const onSet = new Set(on);
  const links = new Set();
  const ways = new Map([[src, 1]]);
  on.sort((a, b) => ds.get(a) - ds.get(b)).forEach((u) => {
    adj.get(u).forEach((l) => {
      if (onSet.has(l.peer) && ds.get(l.peer) === ds.get(u) + 1) {
        if (l.link_id) links.add(l.link_id);
        ways.set(l.peer, (ways.get(l.peer) || 0) + (ways.get(u) || 0));
      }
    });
  });
  const depth = {};
  on.forEach((u) => { depth[u] = ds.get(u); });
  return { nodes: on, links: [...links], paths: ways.get(dst) || 0, hops: D, depth };
}

async function runTrace() {
  if (!trace.source || !trace.target || trace.job) return;
  trace.error = null;
  trace.job = { t0: Date.now(), eta: 30 };
  trace.stage = 'starting';
  renderPane();
  try {
    const r = await fetch('/api/trace', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: trace.source, target: trace.target, target_ip: trace.ip, count: trace.count }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    trace.job.eta = j.eta_seconds || 30;
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 1200));
      const st = await getJSON('/api/trace/' + j.job_id);
      trace.stage = st.stage;
      const s = document.querySelector('[data-trace-stage]');
      if (s) s.textContent = st.stage;
      if (st.status === 'error') throw new Error(st.error);
      if (st.status === 'done') {
        const res = st.result;
        const prev = [...trace.history].reverse().find((h) => h.source === res.source && h.target === res.target);
        res.changed = prev && prev.path.join('>') !== res.path.join('>')
          ? { from: prev.path, at: prev.at } : null;
        trace.history.push(res);
        if (res.source === trace.source && res.target === trace.target) trace.result = res;
        break;
      }
    }
  } catch (e) { trace.error = String(e.message || e); }
  trace.job = null;
  await poll(false);           // the lab may have changed; refresh before drawing
  trace.cand = candidates(trace.source, trace.target);
  renderPane(); renderCanvas();
  if (trace.repeat && !trace.error) setTimeout(() => { if (trace.repeat && !trace.job) runTrace(); }, 5000);
}

function resultFor() {
  const r = trace.result;
  return r && r.source === trace.source && r.target === trace.target ? r : null;
}

/* Narrow the canvas to the trace: everything off the candidate and observed
 * paths fades, candidate links are dashed, used links are drawn solid with
 * the packet count the counters measured. */
function paintTrace() {
  const nodesEl = document.querySelectorAll('#nodes .node');
  const linkEl = document.querySelectorAll('#links > g');
  document.querySelectorAll('#nodes .t-tag').forEach((t) => t.remove());
  nodesEl.forEach((g) => g.classList.remove('t-dim', 't-end', 't-stop', 't-idle', 't-nopick'));
  linkEl.forEach((g) => g.querySelector('.wire').classList.remove('t-cand', 't-both', 't-one', 't-fwd', 't-dim'));
  if (!traceShown()) return;
  nodesEl.forEach((g) => g.classList.remove('dim'));
  linkEl.forEach((g) => g.querySelector('.wire').classList.remove('dim', 'hl'));
  const tag = (label, text) => {
    const g = document.querySelector(`#nodes .node[data-label="${CSS.escape(label)}"]`);
    if (!g) return;
    g.classList.add('t-end');
    g.appendChild(svg('text', { y: -NODE_SIZE / 2 - 9, class: 't-tag' }, text));
  };
  if (pick) {
    if (pick === 'source') nodesEl.forEach((g) => {
      const n = BY.get(g.dataset.label);
      if (!n.can_source_probe || !n.up) g.classList.add('t-nopick');
    });
    if (trace.source) tag(trace.source, 'FROM');
    return;
  }
  const cand = trace.cand || { nodes: [], links: [] };
  const res = resultFor();
  const inSet = new Set(cand.nodes);
  const used = new Map();
  if (res) {
    res.path.forEach((x) => inSet.add(x));
    res.links.filter((l) => l.use !== 'none').forEach((l) => { used.set(l.id, l); inSet.add(l.a); inSet.add(l.b); });
  }
  const onPath = new Set(res ? res.path : []);
  nodesEl.forEach((g) => {
    const l = g.dataset.label;
    if (!inSet.has(l)) g.classList.add('t-dim');
    else if (res && !onPath.has(l) && ![...used.values()].some((u) => u.a === l || u.b === l)) g.classList.add('t-idle');
    if (res && res.stop_at === l) g.classList.add('t-stop');
  });
  const candLinks = new Set(cand.links);
  linkEl.forEach((g) => {
    const w = g.querySelector('.wire'), tx = g.querySelector('.rate');
    const u = used.get(g.dataset.link);
    if (u) {
      w.classList.add(u.use === 'both' ? 't-both' : u.use === 'forward' ? 't-fwd' : 't-one');
      tx.textContent = u.packets != null ? u.packets + ' pkts' : '';
      tx.classList.toggle('bad', u.use === 'forward');
    } else if (candLinks.has(g.dataset.link)) {
      w.classList.add('t-cand');
    } else {
      w.classList.add('t-dim');
    }
  });
  tag(trace.source, 'FROM');
  tag(trace.target, 'TO');
}

/* ------------------------------------------------------------ the pane --- */
function tracePane() {
  const wrap = el('div');
  const head = el('div', 'phead');
  const sel = (items, value, onChange, empty) => {
    const s = el('select');
    if (empty) { const o = el('option', null, empty); o.value = ''; s.appendChild(o); }
    items.forEach(([v, t]) => { const o = el('option', null, t); o.value = v; s.appendChild(o); });
    s.value = value || '';
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };
  const sources = NODES.filter((n) => n.can_source_probe && n.up).sort(byLabel).map((n) => [n.label, n.label]);
  const targets = NODES.filter((n) => !n.ghost && n.label !== trace.source).sort(byLabel).map((n) => [n.label, n.label]);
  const lf = el('label', null, 'From ');
  lf.appendChild(sel(sources, trace.source, (v) => setTraceEnds(v || null, trace.target), 'choose'));
  head.appendChild(lf);
  const lt = el('label', null, 'To ');
  lt.appendChild(sel(targets, trace.target, (v) => setTraceEnds(trace.source, v || null), 'choose'));
  head.appendChild(lt);
  const t = BY.get(trace.target);
  if (t) {
    const ips = [];
    const seen = new Set();
    [t.live_ip, t.primary_ip, ...(t.addresses || []).filter((a) => !/virtual/.test(a.kind || '')).map((a) => a.value)]
      .forEach((ip) => { if (ip && !seen.has(ip)) { seen.add(ip); ips.push([ip, ip]); } });
    const la = el('label', null, 'Address ');
    la.appendChild(sel(ips, trace.ip, (v) => { trace.ip = v; }));
    head.appendChild(la);
  }
  const lc = el('label', null, 'Packets ');
  const cnt = el('input');
  cnt.type = 'number'; cnt.min = 20; cnt.max = 500; cnt.step = 20; cnt.value = trace.count;
  cnt.style.width = '64px';
  cnt.addEventListener('change', () => { trace.count = Math.max(20, Math.min(500, +cnt.value || 100)); });
  lc.appendChild(cnt);
  head.appendChild(lc);
  const run = el('button', 'default', trace.job ? 'Tracing...' : 'Run trace');
  run.disabled = !!trace.job || !trace.source || !trace.target;
  run.addEventListener('click', runTrace);
  head.appendChild(run);
  const rl = el('label');
  const rc = el('input'); rc.type = 'checkbox'; rc.checked = trace.repeat;
  rc.addEventListener('change', () => { trace.repeat = rc.checked; if (rc.checked && !trace.job) runTrace(); });
  rl.appendChild(rc); rl.appendChild(document.createTextNode('Repeat'));
  rl.title = 'Run again 5 s after each trace finishes. Leave it running while you stop and start nodes in CML to watch the path move.';
  head.appendChild(rl);
  const pk = el('button', null, 'Pick on canvas');
  pk.addEventListener('click', () => startPick());
  head.appendChild(pk);
  wrap.appendChild(head);

  if (!trace.source || !trace.target) {
    wrap.appendChild(el('div', 'pmsg', 'Choose From and To above, or use Pick on canvas (T). Right-click a node for "Trace path from here".'));
    return wrap;
  }
  if (trace.job) {
    const box = el('div', 'result');
    const st = el('div', 's-dim', trace.stage);
    st.dataset.traceStage = '1';
    box.appendChild(st);
    const bar = el('div', 'meter'); const fill = el('i'); bar.appendChild(fill);
    const pct = () => Math.min(97, (Date.now() - trace.job.t0) / 1000 / trace.job.eta * 100) + '%';
    fill.style.width = pct();
    const iv = setInterval(() => { if (!fill.isConnected || !trace.job) clearInterval(iv); else fill.style.width = pct(); }, 500);
    box.appendChild(bar);
    wrap.appendChild(box);
  }
  if (trace.error) wrap.appendChild(el('div', 'pmsg bad', 'Trace failed: ' + trace.error));

  const res = resultFor();
  const cand = trace.cand || { nodes: [], paths: 0 };
  if (!res) {
    wrap.appendChild(el('div', 'pmsg', cand.paths
      ? `Predicted: ${cand.paths} shortest path${cand.paths > 1 ? 's' : ''} of ${cand.hops} hops over the running lab (dashed). Run the trace to see which links the packets actually cross.`
      : 'No path between these nodes over the running part of the lab.'));
  } else {
    const v = res.verdict;
    wrap.appendChild(el('div', 'pmsg ' + (v.level === 'ok' ? 'ok' : v.level === 'fail' ? 'bad' : 'warn'),
      v.text + `  (${new Date(res.at).toLocaleTimeString()}, ${res.sent} sent, ` +
      `${res.received == null ? 'replies not counted' : res.received + ' answered'}, ${res.elapsed_s} s)`));
    if (res.changed) {
      const was = res.changed.from.filter((x) => !res.path.includes(x));
      const now = res.path.filter((x) => !res.changed.from.includes(x));
      wrap.appendChild(el('div', 'pmsg warn', `Path changed since ${new Date(res.changed.at).toLocaleTimeString()}: ` +
        (was.length ? `no longer through ${was.join(', ')}` : '') + (was.length && now.length ? '; ' : '') +
        (now.length ? `now through ${now.join(', ')}` : '') + '.'));
    }
  }
  wrap.appendChild(traceDiagram(res, cand));
  if (res) {
    res.findings.forEach((f) => wrap.appendChild(el('div', 'pmsg', f)));
    const cols = el('div', 'tcols');
    const used = res.links.filter((l) => l.use !== 'none');
    const t1 = gridTable(['Link', 'Packets', 'Direction', 'Shortest path']);
    used.sort((a, b) => res.path.indexOf(a.a) - res.path.indexOf(b.a)).forEach((l) => {
      const tr = el('tr', 'row');
      tr.appendChild(el('td', 'm', `${l.a} ${shortIf(l.ai)}  to  ${l.b} ${shortIf(l.bi)}`));
      tr.appendChild(el('td', 'm', l.packets == null ? '-' : String(l.packets)));
      tr.appendChild(el('td', l.use === 'both' ? 's-ok' : 's-warn',
        { both: 'both ways', 'one-way': 'one way only', forward: 'requests only' }[l.use]));
      tr.appendChild(el('td', l.candidate ? null : 's-warn', l.candidate ? 'yes' : 'no'));
      t1.appendChild(tr);
    });
    const a = el('div'); a.appendChild(t1); cols.appendChild(a);
    const t2 = gridTable(['Hop', 'Address', 'Node', 'RTT']);
    res.trace.hops.forEach((h) => {
      const tr = el('tr', 'row');
      [String(h.ttl), h.ip || '*', h.node || (h.ip ? 'unknown' : ''), h.rtt_ms != null ? h.rtt_ms + ' ms' : ''].forEach((v, i) =>
        tr.appendChild(el('td', i === 1 ? 'm' : i === 2 && !h.node && h.ip ? 's-warn' : null, v)));
      t2.appendChild(tr);
    });
    const b = el('div');
    b.appendChild(t2);
    if (!res.trace.hops.length) b.appendChild(el('div', 'pmsg', 'Traceroute returned no hops.'));
    cols.appendChild(b);
    wrap.appendChild(cols);
  }
  const hist = trace.history.filter((h) => h.source === trace.source && h.target === trace.target);
  if (hist.length > 1) {
    const t3 = gridTable(['Time', 'Result', 'Path']);
    [...hist].reverse().forEach((h) => {
      const tr = el('tr', 'row');
      tr.appendChild(el('td', null, new Date(h.at).toLocaleTimeString()));
      tr.appendChild(el('td', h.verdict.level === 'ok' ? 's-ok' : h.verdict.level === 'fail' ? 's-bad' : 's-warn',
        h.verdict.level === 'ok' ? 'reached' : h.verdict.level === 'fail' ? 'failed' : 'check'));
      tr.appendChild(el('td', 'wrap' + (h.changed ? ' s-warn' : ''), h.path.join(' > ') + (h.changed ? '   (changed)' : '')));
      tr.addEventListener('click', () => { trace.result = h; renderPane(); renderCanvas(); });
      t3.appendChild(tr);
    });
    const hs = el('div', 'tsect');
    hs.appendChild(el('div', 'tsect-h', 'Runs this session (click one to show it)'));
    hs.appendChild(t3);
    wrap.appendChild(hs);
  }
  return wrap;
}

/* The nodes between the two ends, laid out left to right by hop count from
 * the source.  Parallel boxes in one column are redundant alternatives. */
function traceDiagram(res, cand) {
  const set = new Set(cand.nodes || []);
  const used = res ? res.links.filter((l) => l.use !== 'none') : [];
  if (res) { res.path.forEach((x) => set.add(x)); used.forEach((l) => { set.add(l.a); set.add(l.b); }); }
  set.add(trace.source); set.add(trace.target);
  const usedBy = new Map(used.map((l) => [l.id, l]));
  const candLinks = new Set(cand.links || []);
  const edges = [];
  const seen = new Set();
  [...set].forEach((u) => (BY.get(u) ? BY.get(u).links || [] : []).forEach((l) => {
    if (!set.has(l.peer) || seen.has(l.link_id)) return;
    if (!usedBy.has(l.link_id) && !candLinks.has(l.link_id)) return;
    seen.add(l.link_id);
    edges.push({ a: u, b: l.peer, id: l.link_id, ai: l.local_iface, bi: l.peer_iface });
  }));
  // hop depth over the drawn edges
  const depth = new Map([[trace.source, 0]]);
  const q = [trace.source];
  while (q.length) {
    const u = q.shift();
    edges.forEach((e) => {
      const v = e.a === u ? e.b : e.b === u ? e.a : null;
      if (v && !depth.has(v)) { depth.set(v, depth.get(u) + 1); q.push(v); }
    });
  }
  let maxD = Math.max(0, ...depth.values());
  [...set].forEach((u) => { if (!depth.has(u)) depth.set(u, ++maxD); });
  const cols = [];
  depth.forEach((d, u) => { (cols[d] = cols[d] || []).push(u); });
  const onPath = new Set(res ? res.path : []);
  cols.forEach((c) => c.sort((a, b) => (onPath.has(b) - onPath.has(a)) || a.localeCompare(b, undefined, { numeric: true })));
  const avail = ($('#pane-body').clientWidth || 1000) - 140;
  const colW = Math.max(110, Math.min(140, avail / Math.max(1, cols.length - 1))), rowH = 70, padX = 60, padY = 34;
  const rows = Math.max(1, ...cols.map((c) => (c ? c.length : 0)));
  const W = padX * 2 + (cols.length - 1) * colW, H = padY + rows * rowH;
  const pos = new Map();
  cols.forEach((c, d) => (c || []).forEach((u, i) => pos.set(u, {
    x: padX + d * colW, y: padY + i * rowH + (rows - c.length) * rowH / 2,
  })));

  const box = el('div', 'tdiag');
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
  edges.forEach((e) => {
    const a = pos.get(e.a), b = pos.get(e.b);
    const u = usedBy.get(e.id);
    const cls = 'wire ' + (u ? (u.use === 'both' ? 't-both' : u.use === 'forward' ? 't-fwd' : 't-one') : 't-cand');
    s.appendChild(svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: cls }));
    if (u && u.packets != null) {
      s.appendChild(svg('text', { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 5, class: 'rate' + (u.use === 'forward' ? ' bad' : ''),
                                  'text-anchor': 'middle' }, String(u.packets)));
    }
  });
  pos.forEach((p, u) => {
    const n = BY.get(u);
    if (!n) return;
    const cls = ['node'];
    if (!n.up) cls.push('off');
    if (res && !onPath.has(u) && !used.some((l) => l.a === u || l.b === u)) cls.push('t-idle');
    if (u === trace.source || u === trace.target) cls.push('t-end');
    if (res && res.stop_at === u) cls.push('t-stop');
    const g = svg('g', { class: cls.join(' '), transform: `translate(${p.x} ${p.y})` });
    g.appendChild(svg('rect', { x: -17, y: -17, width: 34, height: 34, class: 'sel-box' }));
    g.appendChild(nodeIcon(n, 26));
    g.appendChild(svg('text', { y: 27, class: 'lbl' }, u));
    const ip = displayIp(n).text;
    if (ip) g.appendChild(svg('text', { y: 38, class: 'ipl' }, ip));
    g.appendChild(svg('title', {}, `${u}\n${KIND_NAME[kind(n)]}${res && !onPath.has(u) ? '\nnot used this run' : ''}`));
    g.addEventListener('click', () => select(u));
    s.appendChild(g);
  });
  box.appendChild(s);
  const key = el('div', 'tkey');
  [['t-both', 'used, both ways'], ['t-one', 'used, one way'], ['t-fwd', 'requests only'], ['t-cand', 'possible, not used']]
    .forEach(([c, t]) => {
      const k = el('span');
      const ks = svg('svg', { width: 26, height: 8 });
      ks.appendChild(svg('line', { x1: 0, y1: 4, x2: 26, y2: 4, class: 'wire ' + c }));
      k.appendChild(ks); k.appendChild(document.createTextNode(t));
      key.appendChild(k);
    });
  box.appendChild(key);
  return box;
}

/* ================================================================ menus == */
let openMenu = null;

function menuItems(which) {
  const toggle = (label, on, fn, key) => ({ label, on, fn, key });
  if (which === 'lab') return [
    { label: 'Read addresses from devices', fn: () => { LAB.harvest.run(); showPane('harvest'); } },
    { label: 'Run baseline matrix', fn: () => { LAB.matrix.run(); showPane('matrix'); } },
    { label: 'Run DNS audit', fn: () => { LAB.dns.run(); showPane('dns'); } },
    '-',
    { label: 'Trace a path...', key: 'T', fn: startPick },
    '-',
    { label: 'Refresh from CML now', key: 'R', fn: () => poll(true) },
    { label: 'Export inventory as CSV', fn: () => { location.href = '/api/export.csv'; } },
  ];
  if (which === 'view') return [
    toggle('Address labels', prefs.ip, () => { prefs.ip = !prefs.ip; $('#show-ip').checked = prefs.ip; savePrefs(); renderCanvas(); }),
    toggle('Interface labels', prefs.iface, () => { prefs.iface = !prefs.iface; $('#show-if').checked = prefs.iface; savePrefs(); renderCanvas(); }),
    toggle('Link traffic', prefs.traffic, () => setTraffic(!prefs.traffic)),
    '-',
    { label: 'Fit lab to window', key: 'F', fn: fit },
    { label: 'Clear selection', key: 'Esc', fn: () => select(null) },
    '-',
    { head: 'Theme' },
    toggle('Follow system', !prefs.theme, () => setTheme(null)),
    toggle('Light', prefs.theme === 'light', () => setTheme('light')),
    toggle('Dark', prefs.theme === 'dark', () => setTheme('dark')),
    '-',
    { label: `Show hidden nodes (${hidden.size})`, disabled: !hidden.size, fn: unhideAll },
    toggle('Pause auto-refresh', paused, togglePause),
  ];
  return [
    { label: 'Node table', fn: () => showPane('nodes') },
    { label: 'Baseline matrix', fn: () => showPane('matrix') },
    { label: 'DNS audit', fn: () => showPane('dns') },
    { label: 'Live addresses', fn: () => showPane('harvest') },
    { label: 'Ping log', fn: () => showPane('pings') },
  ];
}

function buildMenu(items, box) {
  items.forEach((it) => {
    if (it === '-') { box.appendChild(el('div', 'msep')); return; }
    if (it.head) { box.appendChild(el('div', 'mhead', it.head)); return; }
    const b = el('button', 'mi' + (it.on ? ' on' : ''));
    b.appendChild(el('span', null, it.label));
    if (it.key) b.appendChild(el('span', 'k', it.key));
    b.disabled = !!it.disabled;
    b.addEventListener('click', (e) => { e.stopPropagation(); closeMenus(); it.fn(); });
    box.appendChild(b);
  });
}

function toggleMenu(m) {
  const was = openMenu === m;
  closeMenus();
  if (was) return;
  const dd = el('div', 'dropdown');
  buildMenu(menuItems(m.dataset.menu), dd);
  m.appendChild(dd);
  m.classList.add('open');
  openMenu = m;
}

function closeMenus() {
  document.querySelectorAll('.dropdown').forEach((d) => d.remove());
  document.querySelectorAll('.menu.open').forEach((m) => m.classList.remove('open'));
  $('#ctxmenu').hidden = true;
  openMenu = null;
}

function nodeMenu(n, x, y) {
  closeMenus();
  const box = $('#ctxmenu');
  box.innerHTML = '';
  const ip = displayIp(n).text, fqdn = (n.dns || {}).fqdn;
  buildMenu([
    { label: 'Properties', fn: () => { sideTab = 'node'; select(n.label); } },
    { label: 'Console log', disabled: n.ghost, fn: () => openConsole(n.label) },
    { label: 'Ping this node...', disabled: n.ghost, fn: () => { sideTab = 'ping'; select(n.label); } },
    { label: 'Interfaces', fn: () => { sideTab = 'interfaces'; select(n.label); } },
    '-',
    { label: 'Copy address', disabled: !ip, fn: () => navigator.clipboard.writeText(ip) },
    { label: 'Copy DNS name', disabled: !fqdn, fn: () => navigator.clipboard.writeText(fqdn) },
    '-',
    { label: 'Trace path from here...', disabled: !n.can_source_probe || !n.up, fn: () => startPick(n.label) },
    { label: trace.source && trace.source !== n.label ? `Trace path from ${trace.source} to here` : 'Trace path to here',
      disabled: !trace.source || trace.source === n.label || n.ghost,
      fn: () => { setTraceEnds(trace.source, n.label); showPane('trace'); } },
    '-',
    { label: 'Center in view', fn: () => centerOn(n, true) },
    { label: 'Hide node', fn: () => hideNode(n.label) },
  ], box);
  box.hidden = false;
  const r = box.getBoundingClientRect();
  box.style.left = Math.min(x, innerWidth - r.width - 4) + 'px';
  box.style.top = Math.min(y, innerHeight - r.height - 4) + 'px';
}

function unhideAll() {
  hidden.clear();
  store.set(HIDE_KEY, []);
  renderAll();
}

function togglePause() {
  paused = !paused;
  $('#pause').textContent = paused ? 'Resume' : 'Pause';
  $('#tick').textContent = paused ? 'paused' : countdown + 's';
}

function setTheme(t) { prefs.theme = t; savePrefs(); applyTheme(); }
function applyTheme() {
  if (prefs.theme) document.documentElement.dataset.theme = prefs.theme;
  else delete document.documentElement.dataset.theme;
}

/* ------------------------------------------------------------- controls -- */
function wire() {
  const siteSel = $('#site');
  siteSel.value = site;
  siteSel.addEventListener('change', () => { site = siteSel.value; store.set(SITE_KEY, site); refilter(); });
  $('#q').addEventListener('input', refilter);
  $('#hidden-btn').addEventListener('click', unhideAll);
  $('#pause').addEventListener('click', togglePause);

  $('#show-ip').checked = prefs.ip;
  $('#show-if').checked = prefs.iface;
  $('#show-traffic').checked = prefs.traffic;
  $('#show-ip').addEventListener('change', (e) => { prefs.ip = e.target.checked; savePrefs(); renderCanvas(); });
  $('#show-if').addEventListener('change', (e) => { prefs.iface = e.target.checked; savePrefs(); renderCanvas(); });
  $('#show-traffic').addEventListener('change', (e) => setTraffic(e.target.checked));
  $('#zoom-in').addEventListener('click', () => zoomAt(1.25));
  $('#zoom-out').addEventListener('click', () => zoomAt(0.8));
  $('#zoom-fit').addEventListener('click', fit);
  $('#trace-btn').addEventListener('click', () => (pick ? cancelPick() : startPick()));
  $('#refresh-btn').addEventListener('click', () => poll(true));

  document.querySelectorAll('.menu').forEach((m) => {
    m.firstElementChild.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(m); });
    m.addEventListener('mouseenter', () => { if (openMenu && openMenu !== m) toggleMenu(m); });
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.dropdown, .ctxmenu')) closeMenus(); });
  window.addEventListener('blur', closeMenus);

  wireCanvas();
  wireSplitter();
  applyPane();

  document.addEventListener('keydown', (e) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      closeMenus();
      if (pick) { cancelPick(); return; }
      if (typing) document.activeElement.blur();
      else { select(null); document.body.classList.remove('side-open'); }
    }
    if (typing) return;
    if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
    if ((e.key === 't' || e.key === 'T') && !e.metaKey && !e.ctrlKey) startPick();
    if ((e.key === 'r' || e.key === 'R') && !e.metaKey && !e.ctrlKey) poll(true);
    if ((e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey) fit();
  });
}

/* Drag the background to pan, wheel or pinch to zoom, click a node to select
 * it, click empty canvas to clear, double-click for the console log,
 * right-click for the node menu. */
function wireCanvas() {
  const c = $('#canvas');
  let drag = null;
  const pointers = new Map();
  c.addEventListener('pointerdown', (e) => {
    if (e.button === 2) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const nodeEl = e.target.closest('.node');
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false, node: nodeEl && nodeEl.dataset.label };
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [p, q] = [...pointers.values()];
      const dist = Math.hypot(p.x - q.x, p.y - q.y);
      if (drag && drag.pinch) {
        const r = c.getBoundingClientRect();
        zoomAt(dist / drag.pinch, (p.x + q.x) / 2 - r.left, (p.y + q.y) / 2 - r.top);
      }
      if (drag) { drag.pinch = dist; drag.moved = true; }
      return;
    }
    if (!drag) {
      const nodeEl = e.target.closest && e.target.closest('.node');
      const h = nodeEl ? nodeEl.dataset.label : null;
      if (h !== hovered) { hovered = h; highlight(); }
      return;
    }
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    drag.moved = true;
    c.classList.add('panning');
    view.x = drag.vx + dx; view.y = drag.vy + dy;
    applyView();
  });
  const end = (e) => {
    pointers.delete(e.pointerId);
    if (!drag) return;
    if (!drag.moved && pick && drag.node) { pickNode(drag.node); }
    else if (!drag.moved) {
      select(drag.node || null);
      if (!drag.node) document.body.classList.remove('side-open');
    }
    if (pointers.size === 0) drag = null;
    c.classList.remove('panning');
  };
  c.addEventListener('pointerup', end);
  c.addEventListener('pointercancel', end);
  c.addEventListener('pointerleave', () => { if (!drag && hovered) { hovered = null; highlight(); } });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  c.addEventListener('dblclick', (e) => {
    const nodeEl = e.target.closest('.node');
    if (nodeEl && !BY.get(nodeEl.dataset.label).ghost) openConsole(nodeEl.dataset.label);
  });
  c.addEventListener('contextmenu', (e) => {
    const nodeEl = e.target.closest('.node');
    e.preventDefault();
    if (!nodeEl) return;
    const n = BY.get(nodeEl.dataset.label);
    select(n.label);
    nodeMenu(n, e.clientX, e.clientY);
  });
  let t = null;
  window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(fit, 150); });
}

function wireSplitter() {
  const sp = $('#splitter');
  sp.addEventListener('pointerdown', (e) => {
    sp.setPointerCapture(e.pointerId);
    const box = $('.center').getBoundingClientRect();
    const move = (ev) => {
      prefs.paneH = Math.round(Math.max(100, Math.min(box.height - 120, box.bottom - ev.clientY)));
      prefs.paneMin = false;
      applyPane();
    };
    const up = () => {
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      savePrefs(); renderPane();
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
  });
  sp.addEventListener('dblclick', () => { prefs.paneMin = !prefs.paneMin; savePrefs(); applyPane(); renderPane(); });
}

boot();
