/* NetView client — a CML-style lab workbench.
 *
 * Node tree on the left, the lab canvas in the middle (nodes drawn at their
 * CML x/y, cabling from the topology), and a bottom dock holding the selected
 * node's tabs plus the lab-wide tools (baseline matrix, DNS audit, address
 * harvest, probe log).
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
const PREF_KEY = 'netview.workbench.v1';
const SVGNS = 'http://www.w3.org/2000/svg';

let INV = null, NODES = [], LINKS = [], BY = new Map();
let selected = null, hovered = null, countdown = REFRESH_S, paused = false;
let dockTab = 'summary';
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};
let site = store.get(SITE_KEY, 'all');                 // all | HQ | DR | other
if (typeof site !== 'string') site = 'all';
const hidden = new Set(store.get(HIDE_KEY, []));
const prefs = Object.assign({ ip: true, iface: false, traffic: false, dockH: 280, dockMin: false,
                              collapsed: [], theme: null }, store.get(PREF_KEY, {}));
const savePrefs = () => store.set(PREF_KEY, prefs);

// Probe jobs live here rather than in DOM nodes, so switching tabs or
// selecting another node never loses a run in flight.
const probeJobs = [];            // newest first: {id, source, target, target_ip, status, stage, eta, t0, result, error}

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

/* ---------------------------------------------------------------- data --- */
async function boot() {
  applyTheme();
  wire();
  INV = await getJSON('/api/inventory');
  NODES = INV.nodes.concat(INV.ghosts.map((g) => Object.assign({
    ghost: true, up: false, dns: {}, links: [], vlans: [], addresses: [],
    ha: {}, issues: [], interfaces: [], probe_family: 'none',
    can_source_probe: false, addressing: 'unaddressed',
  }, g)));
  NODES.forEach((n) => BY.set(n.label, n));
  LINKS = buildLinks();
  layoutUnplaced();
  banners();
  renderAll();
  fit();
  setInterval(tickSecond, 1000);
  if (prefs.traffic) startTraffic();
  // Pick up any lab-wide run that was already going when the page loaded.
  ['matrix', 'dns', 'harvest'].forEach((k) => LAB[k].refresh());
}

async function poll() {
  try {
    const j = await getJSON('/api/state');
    if (!j.states) return;
    NODES.forEach((n) => {
      if (n.ghost) return;
      if (j.states[n.label] !== undefined) n.state = j.states[n.label];
      n.up = n.state === 'STARTED' || n.state === 'BOOTED';
    });
    renderTree(); renderCanvas(); renderStatus();
    if (dockTab === 'summary') renderDock();
  } catch (e) { /* one missed poll is not worth an error state */ }
}

function tickSecond() {
  if (paused) return;
  countdown -= 1;
  if (countdown <= 0) { countdown = REFRESH_S; poll(); }
  $('#tick').textContent = countdown + 's';
}

/* Each link appears on both of its ends in the inventory; collapse to one. */
function buildLinks() {
  const seen = new Map();
  NODES.forEach((n) => (n.links || []).forEach((l) => {
    const key = l.link_id || [n.label + '|' + l.local_iface, l.peer + '|' + l.peer_iface].sort().join('<>');
    if (seen.has(key)) return;
    seen.set(key, { id: l.link_id, key, a: n.label, ai: l.local_iface, b: l.peer, bi: l.peer_iface,
                    subnet: l.subnet });
  }));
  return [...seen.values()].filter((l) => BY.has(l.a) && BY.has(l.b));
}

/* Nodes with no canvas position (declared-but-absent ghosts, or a topology
 * without x/y) are parked in a framed column right of the lab, so they are
 * still visible without pretending to be cabled anywhere. */
function layoutUnplaced() {
  const placed = NODES.filter((n) => typeof n.x === 'number' && typeof n.y === 'number');
  const loose = NODES.filter((n) => !placed.includes(n));
  if (!loose.length) return;
  const x0 = placed.length ? Math.max(...placed.map((n) => n.x)) + 200 : 0;
  const y0 = placed.length ? Math.min(...placed.map((n) => n.y)) : 0;
  const perCol = 6;
  loose.forEach((n, i) => {
    n.x = x0 + Math.floor(i / perCol) * 150;
    n.y = y0 + (i % perCol) * 100;
    n.parked = true;
  });
}

/* -------------------------------------------------------------- banners -- */
function banners() {
  const box = $('#banners');
  box.innerHTML = '';
  const m = INV.meta;
  const add = (b) => {
    const x = el('button', 'x', '×');
    x.title = 'Dismiss';
    x.addEventListener('click', () => b.remove());
    b.prepend(x);
    box.appendChild(b);
  };
  if (m.topology_source !== 'live') {
    const age = m.snapshot && m.snapshot.age_hours;
    const b = el('div', 'notice fail');
    b.innerHTML = '<b>Cached snapshot</b> — ';
    b.appendChild(document.createTextNode(
      `${age != null ? age + 'h old' : 'age unknown'}. CML is unreachable; anything changed in the lab since then is missing here.`));
    add(b);
  }
  if (m.counts.undeclared) {
    const b = el('div', 'notice');
    b.innerHTML = `<b>${m.counts.undeclared} undeclared</b> — `;
    b.appendChild(document.createTextNode(
      'in the lab but not in the manifest, so they get no generated config. Dashed outline on the canvas.'));
    add(b);
  }
}

/* --------------------------------------------------------------- filter -- */
/* "Other" = anything that is not HQ or DR: the carriers, the internet web
 * server, and the remote worker's house (site HOME).  A shared carrier has no
 * single site by design — see the manifest's note on the ISP nodes. */
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
 * would be disorienting); filters DIM on the canvas and REMOVE from the tree. */
function matches(n) {
  const q = $('#q').value.trim().toLowerCase();
  if (site !== 'all' && siteBucket(n) !== site) return false;
  if ($('#hide-down').checked && !n.up && !n.ghost) return false;
  if (q && !hay(n).includes(q)) return false;
  return true;
}
const shown = (n) => !hidden.has(n.label);

function groups(nodes) {
  const mode = $('#groupby').value;
  const key = {
    layer: (n) => [n.layer, n.layer_title || n.layer],
    site:  (n) => [n.site || '—', n.site || 'No site (carrier / shared)'],
    role:  (n) => [n.role || 'zz', n.role_title || 'Not declared in manifest'],
    state: (n) => n.ghost ? ['z-absent', 'Declared but absent']
                          : n.up ? ['a-up', 'Running'] : ['b-down', 'Stopped'],
  }[mode];
  const map = new Map();
  nodes.forEach((n) => {
    const [k, title] = key(n);
    if (!map.has(k)) map.set(k, { k, title, nodes: [] });
    map.get(k).nodes.push(n);
  });
  const order = mode === 'layer'
    ? INV.layers.map((l) => l.key).filter((k) => map.has(k))
    : [...map.keys()].sort();
  return order.map((k) => map.get(k));
}

/* ---------------------------------------------------------------- icons -- */
/* One glyph per device class, drawn in a 40×40 box centred on 0,0 — the same
 * visual vocabulary as the CML palette: round routers, square switches, a
 * brick firewall, tower servers, monitors for hosts, a cloud for the outside. */
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
  if (['dnsmasq', 'radius', 'nginx', 'splunk', 'syslog', 'server', 'win2025'].includes(d) || r.startsWith('server') || r.startsWith('dns')) return 'server';
  if (d === 'iol-xe') return n.layer === 'edge' || n.layer === 'carrier' ? 'router' : 'l3switch';
  return 'unknown';
}
const KIND_COLOR = {
  router: '--n-net', l3switch: '--n-net', switch: '--n-net', firewall: '--n-sec', server: '--n-srv',
  host: '--n-host', wlc: '--n-wifi', ap: '--n-wifi', tester: '--n-host', cloud: '--n-ext', unknown: '--n-ext',
};
const KIND_NAME = {
  router: 'Router', l3switch: 'L3 switch', switch: 'L2 switch', firewall: 'Firewall', server: 'Server / container',
  host: 'Host', wlc: 'WLC', ap: 'Access point', tester: 'Traffic gen', cloud: 'External', unknown: 'Other',
};
const ARROW = (x1, y1, x2, y2) => {       // line with a small head at x2,y2
  const a = Math.atan2(y2 - y1, x2 - x1), h = 3.6;
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
  const k = kind(n);
  const g = svg('g');
  const s = size / 2;
  const fill = `var(${KIND_COLOR[k]})`;
  if (ROUND.has(k)) g.appendChild(svg('circle', { r: s, class: 'body', fill }));
  else g.appendChild(svg('rect', { x: -s, y: -s, width: size, height: size, rx: size * 0.16, class: 'body', fill }));
  const gl = svg('g', { class: 'glyph', transform: `scale(${size / 34})` });
  GLYPH[k]().forEach((d) => gl.appendChild(svg('path', { d })));
  g.appendChild(gl);
  return g;
}

function miniIcon(n) {
  const s = svg('svg', { viewBox: '-12 -12 24 24', class: 'ico' });
  const g = nodeIcon(n, 22);
  g.classList.add('node');
  s.appendChild(g);
  if (!n.up) s.style.opacity = '.5';
  return s;
}

/* ---------------------------------------------------------------- tree --- */
function renderTree() {
  const box = $('#tree');
  const scroll = box.scrollTop;
  box.innerHTML = '';
  const vis = NODES.filter((n) => shown(n) && matches(n))
    .sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true }));
  if (!vis.length) { box.appendChild(el('div', 'empty', 'Nothing matches.')); }
  groups(vis).forEach((g) => {
    const det = el('details', 'tgroup');
    det.open = !prefs.collapsed.includes(g.k);
    const sum = el('summary', null, g.title);
    const gup = g.nodes.filter((n) => n.up).length;
    sum.appendChild(el('span', 'n', `${gup}/${g.nodes.length}`));
    det.appendChild(sum);
    det.addEventListener('toggle', () => {
      prefs.collapsed = prefs.collapsed.filter((k) => k !== g.k);
      if (!det.open) prefs.collapsed.push(g.k);
      savePrefs();
    });
    g.nodes.forEach((n) => det.appendChild(treeItem(n)));
    box.appendChild(det);
  });
  const hb = $('#hidden-btn');
  hb.hidden = hidden.size === 0;
  hb.textContent = `${hidden.size} hidden — show all`;
  box.scrollTop = scroll;
}

function treeItem(n) {
  const it = el('div', 'titem' + (n.up ? '' : ' down') + (n.label === selected ? ' sel' : ''));
  it.appendChild(miniIcon(n));
  const mid = el('div');
  mid.style.minWidth = '0';
  mid.appendChild(el('div', 'nm', n.label));
  mid.appendChild(el('div', 'sub', displayIp(n).text || n.role_title || ''));
  it.appendChild(mid);
  const rt = el('div', 'rt');
  if ((n.issues || []).length) rt.appendChild(el('span', 'tag warn', '#' + n.issues.map((i) => i.id).join(' #')));
  if (n.config_status === 'undeclared') rt.appendChild(el('span', 'tag warn', 'undecl'));
  const hide = el('button', 'hide-btn', 'hide');
  hide.title = 'Hide from the tree and canvas';
  hide.addEventListener('click', (e) => {
    e.stopPropagation();
    hidden.add(n.label);
    store.set(HIDE_KEY, [...hidden]);
    if (selected === n.label) select(null);
    renderAll();
  });
  rt.appendChild(hide);
  rt.appendChild(el('span', 'sdot ' + (n.ghost ? 'ghost' : n.state || '')));
  it.appendChild(rt);
  it.addEventListener('click', () => { select(n.label, true); closeNav(); });
  it.addEventListener('mouseenter', () => { hovered = n.label; highlight(); });
  it.addEventListener('mouseleave', () => { hovered = null; highlight(); });
  return it;
}

/* Live address (read off the device) outranks the manifest's declaration. */
function displayIp(n) {
  if (n.live_ip) return { text: n.live_ip, drift: n.live_ip !== n.primary_ip };
  if (n.primary_ip) return { text: n.primary_ip, drift: false, manifest: true };
  return { text: '', drift: false };
}

/* --------------------------------------------------------------- canvas -- */
const view = { x: 0, y: 0, k: 1 };
const NODE_SIZE = 38;

function applyView() {
  $('#viewport').setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`);
}

function fit() {
  const pts = NODES.filter(shown);
  if (!pts.length) return;
  const r = $('#canvas').getBoundingClientRect();
  const xs = pts.map((n) => n.x), ys = pts.map((n) => n.y);
  const minX = Math.min(...xs) - 70, maxX = Math.max(...xs) + 70;
  const minY = Math.min(...ys) - 60, maxY = Math.max(...ys) + 80;
  const pad = 30, top = 44;
  const k = Math.min((r.width - pad * 2) / (maxX - minX), (r.height - pad - top) / (maxY - minY), 1.6);
  view.k = Math.max(0.15, k);
  view.x = (r.width - (maxX - minX) * view.k) / 2 - minX * view.k;
  view.y = top + (r.height - top - (maxY - minY) * view.k) / 2 - minY * view.k;
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

function centerOn(n) {
  const r = $('#canvas').getBoundingClientRect();
  const sx = n.x * view.k + view.x, sy = n.y * view.k + view.y;
  if (sx > 60 && sx < r.width - 60 && sy > 60 && sy < r.height - 60) return;   // already on screen
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
    const up = a.up && b.up;
    g.appendChild(svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'lnk' + (up ? '' : ' down') }));
    const hit = svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'lnk-hit' });
    hit.appendChild(svg('title', {}, `${l.a} ${l.ai}  ⟷  ${l.b} ${l.bi}${l.subnet ? '\n' + l.subnet : ''}`));
    g.appendChild(hit);
    if (prefs.iface) {
      [[a, b, l.ai], [b, a, l.bi]].forEach(([p, q, name]) => {
        const len = Math.hypot(q.x - p.x, q.y - p.y) || 1;
        const t = Math.min(0.4, 42 / len);
        g.appendChild(svg('text', { x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t + 3,
                                    class: 'iflbl', 'text-anchor': 'middle' }, shortIf(name)));
      });
    }
    g.appendChild(svg('text', { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 4, class: 'pps',
                                'text-anchor': 'middle', 'data-pps': l.id || '' }, ''));
    lg.appendChild(g);
  });

  const parked = NODES.filter((n) => n.parked && shown(n));
  if (parked.length) {
    const xs = parked.map((n) => n.x), ys = parked.map((n) => n.y);
    const x = Math.min(...xs) - 70, y = Math.min(...ys) - 58;
    ng.appendChild(svg('rect', { x, y, width: Math.max(...xs) - x + 70, height: Math.max(...ys) - y + 62,
                                 rx: 8, class: 'park' }));
    ng.appendChild(svg('text', { x: x + 10, y: y + 16, class: 'park-t' }, 'NOT ON THE CANVAS'));
  }

  NODES.filter(shown).forEach((n) => {
    const cls = ['node'];
    if (!n.up) cls.push('down');
    if (n.config_status === 'undeclared' || n.ghost) cls.push('undeclared');
    if (n.label === selected) cls.push('sel');
    const g = svg('g', { class: cls.join(' '), transform: `translate(${n.x} ${n.y})`, 'data-label': n.label });
    const s = NODE_SIZE / 2;
    g.appendChild(svg('rect', { x: -s - 5, y: -s - 5, width: NODE_SIZE + 10, height: NODE_SIZE + 10,
                                rx: ROUND.has(kind(n)) ? s + 5 : 9, class: 'ring' }));
    g.appendChild(nodeIcon(n, NODE_SIZE));

    // state badge, top-right — green check booted, amber booting, grey stopped
    const bx = s - 2, by = -s + 2;
    const stateFill = n.ghost ? 'var(--fail)' : n.state === 'BOOTED' ? 'var(--ok)'
      : n.state === 'STARTED' ? 'var(--amber)' : 'var(--ink3)';
    g.appendChild(svg('circle', { cx: bx, cy: by, r: 7, class: 'badge-bg', fill: stateFill }));
    g.appendChild(svg('path', {
      d: n.ghost ? `M${bx - 3} ${by - 3}l6 6m0-6l-6 6`
        : n.state === 'BOOTED' ? `M${bx - 3.2} ${by}l2.2 2.3l4-4.4`
        : n.state === 'STARTED' ? `M${bx - 2} ${by - 3.2}v6.4l5-3.2z`
        : `M${bx - 2.6} ${by - 2.6}h5.2v5.2h-5.2z`,
      fill: n.state === 'STARTED' || !n.up && !n.ghost ? '#fff' : 'none',
      stroke: '#fff', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    }));
    // open-issue count, top-left
    if ((n.issues || []).length) {
      g.appendChild(svg('circle', { cx: -s + 2, cy: -s + 2, r: 7, class: 'badge-bg', fill: 'var(--warn)' }));
      g.appendChild(svg('text', { x: -s + 2, y: -s + 5, class: 'badge-t' }, String(n.issues.length)));
    }
    g.appendChild(svg('text', { y: s + 15, class: 'lbl' }, n.label));
    if (prefs.ip) {
      const ip = displayIp(n);
      const txt = n.ghost ? 'absent from lab' : ip.text ? ip.text + (ip.drift ? ' ⚠' : '') : '';
      if (txt) g.appendChild(svg('text', { y: s + 28, class: 'ipl' + (ip.drift || n.ghost ? ' drift' : '') }, txt));
    }
    g.appendChild(svg('title', {}, `${n.label}\n${n.role_title || ''}\n${n.ghost ? 'absent' : n.state || 'unknown'}`));
    ng.appendChild(g);
  });

  renderProbeOverlay();
  highlight();
  paintTraffic();
}

/* Dim anything the filters exclude; light up the focused node's cabling. */
function highlight() {
  const focus = hovered || selected;
  document.querySelectorAll('#nodes .node').forEach((g) => {
    const n = BY.get(g.dataset.label);
    g.classList.toggle('dim', !matches(n));
  });
  document.querySelectorAll('#links > g').forEach((g) => {
    const ln = g.querySelector('.lnk');
    const on = focus && (g.dataset.a === focus || g.dataset.b === focus);
    ln.classList.toggle('hl', !!on);
    ln.classList.toggle('dim', !matches(BY.get(g.dataset.a)) || !matches(BY.get(g.dataset.b)));
  });
}

/* A dashed path from source to target for the selected node's latest probe:
 * marching while it runs, then coloured by the verdict. */
function renderProbeOverlay() {
  const ov = $('#overlay');
  ov.innerHTML = '';
  const job = probeJobs.find((j) => selected && (j.target === selected || j.source === selected));
  if (!job) return;
  const a = BY.get(job.source), b = BY.get(job.target);
  if (!a || !b || !shown(a) || !shown(b)) return;
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
  const bend = Math.min(90, len * 0.25);
  const cx = mx - (dy / len) * bend, cy = my + (dx / len) * bend;
  const lvl = job.status === 'done' ? ((job.result.verdict || {}).level || 'warn') : job.status === 'error' ? 'fail' : 'run';
  ov.appendChild(svg('path', { d: `M${a.x} ${a.y}Q${cx} ${cy} ${b.x} ${b.y}`,
                               class: 'probe-line ' + (lvl === 'ok' ? 'ok' : lvl === 'fail' ? 'fail' : lvl === 'run' ? 'run' : 'warn') }));
}

/* Legend: only the device classes actually in this lab. */
function renderLegend() {
  const box = $('#legend');
  box.innerHTML = '';
  const kinds = new Set(NODES.map(kind));
  Object.keys(KIND_NAME).filter((k) => kinds.has(k)).forEach((k) => {
    const s = el('span');
    const i = el('i'); i.style.background = `var(${KIND_COLOR[k]})`;
    if (ROUND.has(k)) i.style.borderRadius = '50%';
    s.appendChild(i); s.appendChild(document.createTextNode(KIND_NAME[k]));
    box.appendChild(s);
  });
}

/* ------------------------------------------------------- link traffic ---- */
/* Two samples of the lab-wide link counters give live pkts/s and a cumulative
 * drop count per link — read over REST with no console attach (0.04s). */
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
          if (!p) return;
          linkRates[id] = {
            pps: Math.max(0, ((v.readpackets + v.writepackets) - (p.readpackets + p.writepackets)) / dt),
            drops: v.drops,
          };
        });
      }
      lastStats = { t, links: s.links };
      paintTraffic();
      if (dockTab === 'interfaces') renderDock();
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
    const ln = g.querySelector('.lnk'), tx = g.querySelector('.pps');
    ln.classList.toggle('busy', !!(r && r.pps >= 1));
    ln.classList.toggle('drops', !!(r && r.drops > 0));
    ln.style.strokeWidth = r && r.pps >= 1 ? String(Math.min(6, 1.6 + Math.log10(r.pps + 1) * 1.4)) : '';
    tx.textContent = r ? (r.drops > 0 ? `${Math.round(r.pps)}/s · ${r.drops} drop` : r.pps >= 1 ? Math.round(r.pps) + '/s' : '') : '';
    tx.classList.toggle('bad', !!(r && r.drops > 0));
  });
}

/* ------------------------------------------------------------ selection -- */
function select(label, center) {
  selected = label;
  // Like CML's bottom pane: picking a node brings up that node's tabs,
  // clearing the selection returns to the lab view.
  const n = label && BY.get(label);
  if (dockTab.startsWith('lab:') ? !!n : !n) dockTab = 'summary';
  if (n && ((dockTab === 'dhcp' && !n.dhcp) || (n.ghost && ['probe', 'console'].includes(dockTab)))) dockTab = 'summary';
  if (n && center) centerOn(n);
  if (n && prefs.dockMin) { prefs.dockMin = false; applyDock(); }
  renderTree(); renderCanvas(); renderDock(); renderStatus();
}

/* ----------------------------------------------------------- status bar -- */
function renderStatus() {
  const m = INV.meta;
  const src = $('#src-line');
  src.innerHTML = '';
  src.appendChild(el('span', m.topology_source === 'live' ? 'live' : 'cached',
    m.topology_source === 'live' ? '● live from CML' : '● CACHED SNAPSHOT'));
  src.appendChild(document.createTextNode(' · p45.local / ' + INV.public_zone));

  // Counts follow the site selection, so "3 down" means 3 down HERE.
  const scope = NODES.filter((n) => !n.ghost && (site === 'all' || siteBucket(n) === site));
  const up = scope.filter((n) => n.up).length;
  const vis = NODES.filter((n) => shown(n) && matches(n)).length;
  $('#counts').textContent = (site === 'all' ? '' : site + ': ') +
    `${up} up · ${scope.length - up} down · ${vis} shown` +
    (m.counts.absent ? ` · ${m.counts.absent} absent` : '');

  const all = NODES.filter((n) => !n.ghost), allUp = all.filter((n) => n.up).length;
  const pill = $('#lab-pill');
  pill.textContent = m.topology_source === 'live' ? `${allUp}/${all.length} running` : 'snapshot';
  pill.className = 'pill' + (m.topology_source !== 'live' ? ' fail' : allUp < all.length ? ' warn' : '');

  const n = selected && BY.get(selected);
  $('#sel-line').textContent = n ? `selected ${n.label} · ${n.node_definition || 'absent'}` : '';
}

function renderAll() { renderTree(); renderCanvas(); renderLegend(); renderStatus(); renderDock(); }

/* ================================================================= dock == */
const NODE_TABS = [
  ['summary', 'Summary'], ['addresses', 'Addresses & DNS'], ['interfaces', 'Interfaces'],
  ['dhcp', 'DHCP'], ['probe', 'Ping'], ['console', 'Console'],
];

function renderDock() { renderDockTabs(); renderDockBody(); }

function renderDockTabs() {
  const tabs = $('#dock-tabs');
  tabs.innerHTML = '';
  const n = selected && BY.get(selected);

  if (n) {
    const ctx = el('div', 'ctx');
    ctx.appendChild(miniIcon(n));
    ctx.appendChild(document.createTextNode(n.label));
    tabs.appendChild(ctx);
    NODE_TABS.forEach(([k, t]) => {
      if (k === 'dhcp' && !n.dhcp) return;
      if ((k === 'probe' || k === 'console') && n.ghost) return;
      const b = el('button', 'tab' + (dockTab === k ? ' on' : ''), t);
      if (k === 'probe') {
        const running = probeJobs.filter((j) => j.target === n.label && j.status !== 'done' && j.status !== 'error').length;
        if (running) b.appendChild(el('span', 'cnt', '…'));
      }
      b.addEventListener('click', () => { dockTab = k; ensureDockOpen(); renderDock(); });
      tabs.appendChild(b);
    });
    tabs.appendChild(el('div', 'sep'));
  } else if (!dockTab.startsWith('lab:')) {
    const b = el('button', 'tab on', 'Lab overview');
    tabs.appendChild(b);
  }

  Object.entries(LAB).forEach(([k, def]) => {
    const b = el('button', 'tab' + (dockTab === 'lab:' + k ? ' on' : ''), def.title);
    const c = def.badge();
    if (c) b.appendChild(el('span', 'cnt ' + (c.level || ''), c.text));
    b.addEventListener('click', () => openLab(k));
    tabs.appendChild(b);
  });
  tabs.appendChild(el('div', 'grow'));
  const min = el('button', 'min', prefs.dockMin ? '▴' : '▾');
  min.title = prefs.dockMin ? 'Expand panel' : 'Minimise panel';
  min.addEventListener('click', () => { prefs.dockMin = !prefs.dockMin; savePrefs(); applyDock(); renderDock(); });
  tabs.appendChild(min);

  document.querySelectorAll('.top-actions [data-tab]').forEach((b) =>
    b.classList.toggle('on', dockTab === 'lab:' + b.dataset.tab));
}

function renderDockBody() {
  if (prefs.dockMin) return;
  const n = selected && BY.get(selected);
  const body = $('#dock-body');
  const scroll = body.scrollTop;
  body.innerHTML = '';
  if (dockTab.startsWith('lab:')) { LAB[dockTab.slice(4)].draw(body); return; }
  if (!n) { body.appendChild(overview()); return; }
  const pane = {
    summary: paneSummary, addresses: paneAddresses, interfaces: paneInterfaces,
    dhcp: paneDhcp, probe: paneProbe, console: paneConsole,
  }[dockTab] || paneSummary;
  body.appendChild(pane(n));
  body.scrollTop = scroll;
}

function ensureDockOpen() { if (prefs.dockMin) { prefs.dockMin = false; savePrefs(); applyDock(); } }
function openLab(k) {
  dockTab = dockTab === 'lab:' + k && !prefs.dockMin ? 'summary' : 'lab:' + k;
  ensureDockOpen();
  renderDock();
  if (dockTab === 'lab:' + k) LAB[k].refresh();
}
function applyDock() {
  document.documentElement.style.setProperty('--dock-h', prefs.dockH + 'px');
  $('.main').classList.toggle('dock-min', prefs.dockMin);
}

/* Nothing selected: what a CML lab page shows — counts by class and state. */
function overview() {
  const d = el('div');
  d.appendChild(el('div', 'note', 'Select a node on the canvas or in the tree to open its tabs. ' +
    'Drag to pan, scroll to zoom, F to fit, / to filter.'));
  const cols = el('div', 'cols');
  const byKind = new Map();
  NODES.forEach((n) => {
    const k = kind(n);
    if (!byKind.has(k)) byKind.set(k, { up: 0, all: 0 });
    const r = byKind.get(k); r.all++; if (n.up) r.up++;
  });
  const left = el('div');
  left.appendChild(el('h3', null, 'Nodes by class'));
  left.appendChild(table([...byKind.entries()].map(([k, r]) => [KIND_NAME[k], `${r.up}/${r.all} running`])));
  cols.appendChild(left);
  const right = el('div');
  right.appendChild(el('h3', null, 'Needs attention'));
  const rows = [];
  NODES.filter((n) => n.ghost).forEach((n) => rows.push([n.label, clickable(n.label, 'declared in manifest, absent from lab')]));
  NODES.filter((n) => n.config_status === 'undeclared').forEach((n) => rows.push([n.label, clickable(n.label, 'in lab, not in manifest')]));
  NODES.filter((n) => n.live_ip && n.live_ip !== n.primary_ip).forEach((n) => rows.push([n.label, clickable(n.label, `address drift: ${n.live_ip} vs ${n.primary_ip || '—'}`)]));
  NODES.filter((n) => (n.issues || []).length).forEach((n) => rows.push([n.label, clickable(n.label, n.issues.map((i) => `#${i.id} ${i.title}`).join('; '))]));
  right.appendChild(rows.length ? table(rows) : el('div', 'note ok', 'Nothing flagged.'));
  cols.appendChild(right);
  d.appendChild(cols);
  return d;
}
function clickable(label, text) {
  const a = el('a', null, text);
  a.href = '#';
  a.style.color = 'inherit';
  a.addEventListener('click', (e) => { e.preventDefault(); select(label, true); });
  return a;
}

/* ------------------------------------------------------------ helpers ---- */
function table(rows, head) {
  const t = el('table', 't');
  if (head) {
    const hr = el('tr');
    head.forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
  }
  rows.forEach((row) => {
    const tr = el('tr');
    row.forEach((v, i) => {
      const td = el('td', i > 0 || head ? 'm' : null);
      if (v && v.nodeType) td.appendChild(v); else td.textContent = v == null || v === '' ? '—' : v;
      tr.appendChild(td);
    });
    t.appendChild(tr);
  });
  return t;
}

function copyable(text) {
  if (!text) return '—';
  const s = el('span', null, text);
  const b = el('button', 'copy', 'copy');
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      b.textContent = '✓'; setTimeout(() => (b.textContent = 'copy'), 1000);
    });
  });
  s.appendChild(b);
  return s;
}

/* ------------------------------------------------------------ summary ---- */
function paneSummary(n) {
  const d = el('div', 'cols');
  const a = el('div');
  a.appendChild(el('h3', null, 'Node'));
  const state = n.ghost ? el('span', 'tag fail', 'absent from the lab')
    : el('span', 'tag ' + (n.state === 'BOOTED' ? 'ok' : n.state === 'STARTED' ? 'warn' : ''), n.state || 'unknown');
  const ip = displayIp(n);
  a.appendChild(table([
    ['State', state],
    ['Role', n.role_title],
    ['Image', n.node_definition],
    ['Layer', (n.layer_title || n.layer) + (n.layer_inferred ? ' (guessed from label)' : '')],
    ['Site', n.site],
    ['Address', ip.text ? copyable(ip.text + (ip.manifest ? '' : ' (live)') + (ip.drift ? ' ⚠ manifest says ' + (n.primary_ip || '—') : '')) : '—'],
    ['DNS', copyable((n.dns || {}).fqdn)],
    ['Config', { managed: 'manifest + ' + (n.template || ''), undeclared: 'NOT in the manifest',
                 absent: 'manifest entry, no node', 'no-template': 'declared, no template' }[n.config_status] || '—'],
  ]));
  d.appendChild(a);

  const b = el('div');
  const rows = [];
  (n.vlans || []).forEach((v) => rows.push(['VLAN', `${v.id}${v.name ? ' — ' + v.name : ''}`]));
  Object.entries(n.ha || {}).forEach(([k, v]) => rows.push([k.replace(/_/g, ' '), String(v)]));
  if (n.floor) rows.push(['Location', n.floor]);
  if (n.parked && !n.ghost) rows.push(['Canvas', 'no x/y in topology — parked below the lab']);
  b.appendChild(el('h3', null, 'Placement & HA'));
  b.appendChild(rows.length ? table(rows) : el('div', 'note', 'No VLAN or HA declarations.'));
  if ((n.issues || []).length) {
    b.appendChild(el('h3', null, 'Open issues'));
    n.issues.forEach((i) => b.appendChild(el('div', 'note warn', `#${i.id} — ${i.title}`)));
  }
  if (n.config_status === 'undeclared') {
    b.appendChild(el('div', 'note warn', 'In the lab but not in the manifest — render_configs produces no config for it.'));
  }
  if (!n.ghost && !n.can_source_probe && n.no_source_reason) {
    b.appendChild(el('div', 'note', 'Cannot source probes: ' + n.no_source_reason + '. It can still be a target.'));
  }
  d.appendChild(b);
  return d;
}

/* ---------------------------------------------------------- addresses ---- */
function paneAddresses(n) {
  const d = el('div', 'cols');
  const left = el('div');
  const live = n.live;
  if (live && (live.addresses || []).length) {
    left.appendChild(el('h3', null, 'Live — read from the device'));
    const manifestIps = new Set((n.addresses || []).map((a) => a.value));
    const t = table(live.addresses.map((a) => [a.iface, copyable(a.ip), a.method || '—', a.status + '/' + a.proto]),
                    ['Interface', 'Address', 'Method', 'State']);
    [...t.querySelectorAll('tr')].slice(1).forEach((tr, i) => {
      if (!manifestIps.has(live.addresses[i].ip)) tr.classList.add('drift');
    });
    left.appendChild(t);
    // drift both ways: device holds what the plan doesn't, plan claims what
    // the device doesn't hold
    const liveIps = new Set(live.addresses.map((a) => a.ip));
    const extra = live.addresses.filter((a) => !manifestIps.has(a.ip));
    const missing = (n.addresses || []).filter((a) => !liveIps.has(a.value) && a.kind !== 'ha');
    if (extra.length || missing.length) {
      const bits = [];
      if (extra.length) bits.push('on the device but not in the manifest: ' +
        extra.map((a) => `${a.ip} (${a.iface})`).join(', '));
      if (missing.length) bits.push('in the manifest but not on the device: ' +
        missing.map((a) => `${a.value} (${a.label})`).join(', '));
      left.appendChild(el('div', 'note warn', bits.join(' · ')));
    }
    left.appendChild(el('div', 'note', 'Read at ' + live.at + '. Highlighted rows are addresses the manifest does not declare (DHCP leases, transit /30s, or drift).'));
  } else if (live && live.error) {
    left.appendChild(el('h3', null, 'Live'));
    left.appendChild(el('div', 'note warn', 'Read failed at ' + live.at + ': ' + live.error));
  } else {
    left.appendChild(el('h3', null, 'Live'));
    const nb = el('div', 'note', 'Not read yet. ');
    const b = el('button', null, 'Read addresses');
    b.addEventListener('click', () => openLab('harvest'));
    nb.appendChild(b);
    left.appendChild(nb);
  }
  d.appendChild(left);

  const right = el('div');
  right.appendChild(el('h3', null, 'Manifest (declared) & DNS'));
  const rows = [];
  (n.addresses || []).forEach((a) => rows.push([a.label, copyable(a.value)]));
  const dns = n.dns || {};
  if (dns.fqdn) rows.push(['Internal name', copyable(dns.fqdn)]);
  (dns.cnames || []).forEach((c) => rows.push(['Alias', copyable(c)]));
  (dns.public || []).forEach((p) => rows.push(['Public record', copyable(p.name + ' → ' + p.ip)]));
  if (n.resolver) rows.push(['Resolver', copyable(n.resolver + (n.resolver_note ? '  (' + n.resolver_note + ')' : ''))]);
  if (n.gateway) rows.push(['Gateway', copyable(n.gateway)]);
  right.appendChild(table(rows.length ? rows : [['(none declared)', '']]));
  d.appendChild(right);
  return d;
}

/* --------------------------------------------------------- interfaces ---- */
function paneInterfaces(n) {
  const d = el('div');
  const head = el('div', 'pane-head');
  head.appendChild(el('span', 'lbl', `${n.interface_count || 0} interfaces · ${(n.links || []).length} cabled`));
  const tb = el('label', 'chk');
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = prefs.traffic;
  cb.addEventListener('change', () => setTraffic(cb.checked));
  tb.appendChild(cb); tb.appendChild(document.createTextNode(' live traffic (REST counters, no console)'));
  head.appendChild(tb);
  d.appendChild(head);
  if (!(n.links || []).length) { d.appendChild(el('div', 'empty', 'No cabling.')); return d; }
  const rows = n.links.map((l) => {
    const r = prefs.traffic ? linkRates[l.link_id] : null;
    const drops = r ? el('span', r.drops > 0 ? 'tag fail' : null, String(r.drops)) : '—';
    return [shortIf(l.local_iface) + '  ' + l.local_iface, clickable(l.peer, l.peer + ' ' + l.peer_iface),
            l.subnet || '—', r ? (r.pps >= 1 ? Math.round(r.pps) + ' pkt/s' : 'quiet') : (prefs.traffic ? '…' : '—'), drops];
  });
  d.appendChild(table(rows, ['Local', 'Peer', 'Subnet', 'Traffic', 'Drops']));
  return d;
}

function setTraffic(on) {
  prefs.traffic = on; savePrefs();
  $('#show-traffic').checked = on;
  if (on) startTraffic(); else stopTraffic();
  if (dockTab === 'interfaces') renderDock();
}

/* --------------------------------------------------------------- dhcp ---- */
function paneDhcp(n) {
  const box = el('div');
  box.appendChild(el('h3', null, 'DHCP lease — design'));
  box.appendChild(table([
    [n.dhcp.kind === 'client' ? 'Leases on' : 'Probe SVI',
     `${n.dhcp.interface} (VLAN ${n.dhcp.vlan}${n.dhcp.vlan_name ? ' ' + n.dhcp.vlan_name : ''})`],
    ['Scope', n.dhcp.scope],
    ['Server / length', `${n.dhcp.expected_server} · ${n.dhcp.expected_lease_secs / 3600}h`],
  ]));

  if (n.probe_family === 'ios' && n.up) {
    box.appendChild(el('h3', null, 'Live lease'));
    const b = el('button', 'primary', 'Read live lease (~15s)');
    const out = el('div');
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Reading…';
      try {
        const j = await getJSON('/api/dhcp/' + encodeURIComponent(n.label));
        out.innerHTML = '';
        if (j.error) out.appendChild(el('div', 'note warn', j.error));
        else {
          out.appendChild(table([
            ['Address', j.address + ' / ' + (j.mask || '')],
            ['Server', j.server + (j.server_matches ? ' (matches design)' : ' — NOT the expected server')],
            ['State', j.state],
            ['Renews in', fmtSecs(j.renewal_secs)],
            ['Rebinds in', fmtSecs(j.rebind_secs)],
          ]));
          startLeaseTicker(out, j);
        }
      } catch (e) { out.appendChild(el('div', 'note fail', String(e))); }
      b.disabled = false; b.textContent = 'Read live lease (~15s)';
    });
    box.appendChild(b);
    box.appendChild(out);
  } else if (n.dhcp.kind === 'client') {
    box.appendChild(el('div', 'note',
      'The live remaining time exists only on the node itself, and this image ' +
      'has no command that reports it.'));
  }
  return box;
}

function fmtSecs(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m (${s}s)`;
}

/* Tick the renewal countdown client-side from the moment it was read.  The
 * number is honest: it names its read time rather than pretending to be live. */
function startLeaseTicker(container, lease) {
  if (lease.renewal_secs == null) return;
  const line = el('div', 'note');
  container.appendChild(line);
  const t0 = Date.now();
  const paint = () => {
    const gone = Math.floor((Date.now() - t0) / 1000);
    const left = lease.renewal_secs - gone;
    line.textContent = left > 0
      ? `Renewal in ${fmtSecs(left)} — counted from the read at ${lease.read_at}`
      : 'Renewal time has passed — read again for the current lease.';
  };
  paint();
  const iv = setInterval(() => { if (!line.isConnected) clearInterval(iv); else paint(); }, 1000);
}

/* ------------------------------------------------------------ console ---- */
/* Console scrollback with no attach.  For a container, line 0 is the service's
 * own log — dnsmasq queries, RADIUS rejections — which is direct evidence of
 * what the service did, at ~0.03s instead of a 6s console attach. */
const consoleCache = new Map();

function paneConsole(n) {
  const box = el('div');
  const bar = el('div', 'term-bar');
  const btn = el('button', 'primary',
    n.console_line === 1 ? 'Read service log (no attach)' : 'Read console scrollback');
  bar.appendChild(btn);
  bar.appendChild(el('span', 'lbl mono', `line ${n.console_line === 1 ? '0 — service stdout' : '0'} · last 60 lines`));
  box.appendChild(bar);
  const term = el('div', 'term');
  const cached = consoleCache.get(n.label);
  term.textContent = cached || `${n.label} — press the button to read the scrollback.\nNothing is attached; this is a REST read of the console log.`;
  box.appendChild(term);
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    term.textContent = 'reading…';
    try {
      const j = await getJSON('/api/console-log/' + encodeURIComponent(n.label) + '?lines=60');
      term.textContent = j.error ? '! ' + j.error : ((j.log || '').trim() || '(empty)');
      if (!j.error) consoleCache.set(n.label, term.textContent);
      term.scrollTop = term.scrollHeight;
    } catch (e) { term.textContent = '! ' + String(e); }
    btn.disabled = false;
  });
  requestAnimationFrame(() => { term.scrollTop = term.scrollHeight; });
  return box;
}

/* -------------------------------------------------------------- probe ---- */
function paneProbe(n) {
  const box = el('div');
  box.appendChild(el('h3', null, 'Ping ' + n.label + ' from…'));

  const sources = NODES.filter((x) => x.can_source_probe && x.up && x.label !== n.label)
    .sort((a, b) => a.label.localeCompare(b.label));
  if (!sources.length) {
    box.appendChild(el('div', 'note warn', 'No running node can source a probe right now.'));
  } else {
    const form = el('div', 'probe');
    const src = el('select');
    sources.forEach((x) => {
      const o = el('option', null, x.label + (x.site ? ' (' + x.site + ')' : ''));
      o.value = x.label;
      src.appendChild(o);
    });
    if (n.lastSource && sources.some((x) => x.label === n.lastSource)) src.value = n.lastSource;
    form.appendChild(src);

    // Target list: live-read addresses first (what the device actually holds
    // right now), manifest addresses after — labelled so it's clear which is
    // which. A live entry that matches the manifest's primary is marked so the
    // common case still reads as one obvious default.
    const dst = el('select');
    const liveAddrs = ((n.live || {}).addresses || []).filter((a) => a.proto === 'up');
    const liveIps = new Set(liveAddrs.map((a) => a.ip));
    liveAddrs.forEach((a) => {
      const isPrimary = a.ip === n.primary_ip;
      const o = el('option', null, `${a.ip} — ${a.iface} (live${isPrimary ? ', = manifest primary' : ''})`);
      o.value = a.ip;
      dst.appendChild(o);
    });
    (n.addresses || []).forEach((a) => {
      if (liveIps.has(a.value)) return;   // already listed as a live entry
      const o = el('option', null, `${a.value} — ${a.label} (manifest${n.live ? ', not confirmed live' : ''})`);
      o.value = a.value;
      dst.appendChild(o);
    });
    if (!dst.children.length) {
      const o = el('option', null, 'no address known — run "Read addresses" or pick a source only');
      o.value = ''; o.disabled = true;
      dst.appendChild(o);
    }
    form.appendChild(dst);

    const dnsToggle = el('label');
    const cDns = el('input'); cDns.type = 'checkbox';
    const hasShellVantage = NODES.some((x) => x.probe_family === 'linux' && x.up);
    cDns.checked = !!(n.dns || {}).fqdn && hasShellVantage;
    cDns.disabled = !(n.dns || {}).fqdn || !hasShellVantage;
    dnsToggle.appendChild(cDns);
    dnsToggle.appendChild(document.createTextNode('+ DNS'));
    form.appendChild(dnsToggle);

    const go = el('button', 'primary', 'Ping');
    go.addEventListener('click', () => {
      n.lastSource = src.value;
      runProbe(src.value, n.label, dst.value || null, cDns.checked ? ['ip', 'dns'] : ['ip']);
    });
    form.appendChild(go);
    box.appendChild(form);
  }

  const mine = probeJobs.filter((j) => j.target === n.label).slice(0, 6);
  if (mine.length) box.appendChild(el('h3', null, 'Results'));
  mine.forEach((j) => box.appendChild(jobBlock(j)));
  return box;
}

async function runProbe(source, target, target_ip, methods) {
  const job = { id: null, source, target, target_ip, status: 'queued', stage: 'Queuing', eta: 25, t0: Date.now() };
  probeJobs.unshift(job);
  refreshProbeViews();
  try {
    const r = await fetch('/api/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, target, methods, count: 5, target_ip }),
    });
    const j = await r.json();
    if (!r.ok) { job.status = 'error'; job.error = j.error; refreshProbeViews(); return; }
    Object.assign(job, { id: j.job_id, eta: j.eta_seconds || 25, target_ip: j.target_ip, status: 'running' });
    refreshProbeViews();
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 1500));
      const res = await getJSON('/api/probe/' + j.job_id);
      job.stage = res.stage || job.stage;
      if (res.status === 'done') { job.status = 'done'; job.result = res.result; break; }
      if (res.status === 'error') { job.status = 'error'; job.error = res.error; break; }
      refreshProbeViews(true);
    }
  } catch (e) { job.status = 'error'; job.error = String(e); }
  refreshProbeViews();
}

/* Progress ticks only repaint the live bits; a finished job repaints the pane. */
function refreshProbeViews(soft) {
  if (!soft) { renderProbeOverlay(); if (dockTab === 'probe' || dockTab === 'lab:probes') renderDock(); else renderDockTabs(); return; }
  document.querySelectorAll('[data-job-stage]').forEach((s) => {
    const j = probeJobs.find((x) => String(x.t0) === s.dataset.jobStage);
    if (j) s.textContent = (j.stage || '') + '…';
  });
}
function jobBlock(job) {
  if (job.status === 'done') return resultBlock(job.result);
  if (job.status === 'error') return el('div', 'note fail', `${job.source} → ${job.target}: probe failed — ${job.error}`);
  const box = el('div', 'note');
  box.appendChild(el('div', null, `${job.source} → ${job.target}${job.target_ip ? ' (' + job.target_ip + ')' : ''}`));
  const st = el('div', 'mono', (job.stage || '') + '…');
  st.dataset.jobStage = String(job.t0);
  box.appendChild(st);
  const bar = el('div', 'progress'); const fill = el('i'); bar.appendChild(fill);
  const pct = () => Math.min(97, (Date.now() - job.t0) / 1000 / job.eta * 100) + '%';
  fill.style.width = pct();
  const iv = setInterval(() => { if (!fill.isConnected) clearInterval(iv); else fill.style.width = pct(); }, 1000);
  box.appendChild(bar);
  return box;
}

function resultBlock(res) {
  const v = res.verdict || {};
  const box = el('div', 'note ' + (v.level === 'ok' ? 'ok' : v.level === 'fail' ? 'fail' : 'warn'));
  box.appendChild(el('div', null, `${res.source} → ${res.target}: ${v.text || ''}`));

  const bits = [];
  const p = res.ping_ip || res.ping_dns;
  if (p) {
    bits.push(`loss ${p.loss_pct}% (${p.received}/${p.sent})`);
    if (p.rtt_ms && p.rtt_ms.avg != null) bits.push(`rtt ${p.rtt_ms.min}/${p.rtt_ms.avg}/${p.rtt_ms.max}ms`);
  }
  if (res.dns && res.dns.ok) {
    bits.push(`dns ${res.dns.fqdn} → ${res.dns.addresses[0]}` +
      (res.dns.matches_manifest === false ? ' (MISMATCH vs manifest)' : ''));
  } else if (res.dns) {
    bits.push('dns: ' + (res.dns.reason || 'no answer'));
  }
  bits.push(res.elapsed_s + 's');
  box.appendChild(el('div', 'mono', bits.join(' · ')));

  if (p && p.packet_pattern) {
    const pat = el('div', 'pattern');
    [...p.packet_pattern].forEach((ch) => pat.appendChild(el('span', ch === '!' ? null : 'miss', ch)));
    box.appendChild(pat);
  }
  (res.notes || []).forEach((t) => box.appendChild(el('div', null, t)));
  return box;
}

/* ===================================================== lab-wide tools ==== */
/* Each lab tool has the same run/poll shape: a status endpoint, a run
 * endpoint, a 4s poll while running, and a pane in the dock.  The poll keeps
 * going while the tab is closed so the tab badge stays honest. */
function labTool(def) {
  const t = Object.assign({ st: null, timer: null }, def);
  t.refresh = async () => {
    try { t.st = await getJSON(t.statusUrl); } catch (e) { return; }
    if (t.onStatus) t.onStatus(t.st);
    const running = t.st.status === 'running';
    if (running && !t.timer) t.timer = setInterval(t.refresh, 4000);
    if (!running && t.timer) { clearInterval(t.timer); t.timer = null; if (t.onDone) t.onDone(t.st); }
    if (dockTab === 'lab:' + t.key) renderDock(); else renderDockTabs();
  };
  t.run = async (body) => {
    const r = await fetch(t.runUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body || '{}' });
    const j = await r.json();
    t.runError = j.error || null;
    t.eta = j.eta_seconds ? j : null;
    await t.refresh();
  };
  t.draw = (body) => {
    if (!t.st) { body.appendChild(el('div', 'empty', 'Loading…')); t.refresh(); return; }
    t.render(body, t.st);
  };
  return t;
}

function paneHead(title, btn, extra) {
  const head = el('div', 'pane-head');
  head.appendChild(el('h2', null, title));
  head.appendChild(btn);
  (extra || []).forEach((x) => x && head.appendChild(x));
  return head;
}

const LAB = {};

LAB.matrix = labTool({
  key: 'matrix', title: 'Baseline matrix', statusUrl: '/api/matrix', runUrl: '/api/matrix/run',
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    if (!st.rows || !st.rows.length) return null;
    const bad = st.rows.filter((r) => ['leak', 'dead', 'error', 'partial'].includes(r.outcome) ||
      (!['pass', 'pass-arp', 'as-expected', 'source-down', 'target-down'].includes(r.outcome)));
    const leak = st.rows.some((r) => r.outcome === 'leak');
    return bad.length ? { text: String(bad.length), level: leak ? 'fail' : 'warn' } : { text: '✓', level: 'ok' };
  },
  render(box, st) {
    const btn = el('button', 'primary', st.status === 'running' ? `Running ${st.done_count}/${st.total}…` : 'Run all ' + st.total);
    btn.disabled = st.status === 'running';
    btn.addEventListener('click', () => LAB.matrix.run());
    box.appendChild(paneHead('Baseline reachability', btn, [
      this.eta && st.status === 'running' ? el('span', 'lbl', `~${Math.round(this.eta.eta_seconds / 60)} min`) : null,
      st.status === 'done' ? el('span', 'lbl', 'finished ' + st.ended) : null,
      st.status === 'error' ? el('span', 'tag fail', st.error) : null,
      this.runError ? el('span', 'tag fail', this.runError) : null,
    ]));
    box.appendChild(el('div', 'note',
      'The curated baseline from probe_matrix.py — every path a disaster scenario relies on. ' +
      'Two paths are DECLARED must-fails (the perimeter denying an internet client): those ' +
      'passing would be a leak, which is the more serious event. Click a row to show the path on the canvas.'));
    const done = new Map((st.rows || []).map((r) => [r.source + '>' + r.dest, r]));
    const t = el('table', 't');
    const hr = el('tr');
    ['', 'Source', 'Destination', 'What it proves', 'Loss', 'RTT'].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    (st.probes || []).forEach((p) => {
      const r = done.get(p.source + '>' + p.dest);
      const tr = el('tr', 'click');
      const tag = !r ? el('span', 'tag', p.expected_fail ? 'must fail' : 'pending')
        : r.outcome === 'pass' ? el('span', 'tag ok', 'pass')
        : r.outcome === 'pass-arp' ? el('span', 'tag ok', 'pass (1st pkt ARP)')
        : r.outcome === 'as-expected' ? el('span', 'tag ok', 'denied ✓')
        : r.outcome === 'leak' ? el('span', 'tag fail', 'LEAK')
        : r.outcome === 'source-down' ? el('span', 'tag warn', 'src down')
        : r.outcome === 'target-down' ? el('span', 'tag warn', 'target off')
        : el('span', 'tag ' + (r.verdict === 'partial' ? 'warn' : 'fail'), r.verdict);
      const td0 = el('td'); td0.appendChild(tag); tr.appendChild(td0);
      tr.appendChild(el('td', 'm', p.source));
      tr.appendChild(el('td', 'm', p.dest));
      tr.appendChild(el('td', null, p.description));
      tr.appendChild(el('td', 'm', r && r.loss_pct != null ? r.loss_pct + '%' : '—'));
      tr.appendChild(el('td', 'm', r && r.rtt_ms != null ? r.rtt_ms + 'ms' : '—'));
      tr.addEventListener('mouseenter', () => flashPath(p.source, p.dest, r));
      tr.addEventListener('mouseleave', () => renderProbeOverlay());
      tr.addEventListener('click', () => { const n = BY.get(p.source); if (n) centerOn(n); flashPath(p.source, p.dest, r); });
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

/* Draw a matrix path on the canvas while its row is hovered.  Matrix rows name
 * nodes by label; a row naming something not on the canvas just draws nothing. */
function flashPath(src, dst, r) {
  const a = BY.get(src), b = BY.get(dst);
  const ov = $('#overlay');
  ov.innerHTML = '';
  if (!a || !b) return;
  const lvl = !r ? 'run' : ['pass', 'pass-arp', 'as-expected'].includes(r.outcome) ? 'ok'
    : r.outcome === 'leak' || r.verdict === 'dead' || r.verdict === 'error' ? 'fail' : 'warn';
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, bend = Math.min(90, len * 0.25);
  ov.appendChild(svg('path', { d: `M${a.x} ${a.y}Q${mx - dy / len * bend} ${my + dx / len * bend} ${b.x} ${b.y}`,
                               class: 'probe-line ' + lvl }));
}

LAB.dns = labTool({
  key: 'dns', title: 'DNS audit', statusUrl: '/api/dnsaudit', runUrl: '/api/dnsaudit/run',
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    if (!st.rows || !st.rows.length) return null;
    const bad = st.rows.filter((r) => !r.match).length;
    return bad ? { text: String(bad), level: 'fail' } : { text: '✓', level: 'ok' };
  },
  render(box, st) {
    const btn = el('button', 'primary', st.status === 'running' ? `Auditing ${st.done_count}/${st.total}…` : 'Check every name');
    btn.disabled = st.status === 'running';
    btn.addEventListener('click', () => LAB.dns.run());
    box.appendChild(paneHead('DNS audit', btn, [
      st.status === 'error' ? el('span', 'tag fail', st.error) : null,
      this.runError ? el('span', 'tag fail', this.runError) : null,
    ]));
    box.appendChild(el('div', 'note',
      'Asks each site’s resolver for every internal name, from a desktop at that ' +
      'site, and compares the answer to the manifest. A mismatch is a resolver ' +
      'confidently serving a stale address — the failure no ping test can see.'));
    const rows = st.rows || [];
    if (!rows.length) return;
    const bad = rows.filter((r) => !r.match);
    box.appendChild(el('div', 'note ' + (bad.length ? 'warn' : 'ok'),
      st.status === 'done'
        ? (bad.length ? `${bad.length} of ${rows.length} names do NOT match the manifest.`
                      : `All ${rows.length} names resolve to exactly what the manifest says.`)
        : `${rows.length} checked so far, ${bad.length} problem(s).`));
    const t = el('table', 't');
    const hr = el('tr');
    ['', 'Name', 'Manifest', 'Resolver answered'].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    // Problems first, then a compact confirmation of the rest.
    bad.concat(rows.filter((r) => r.match)).forEach((r) => {
      const tr = el('tr');
      const td0 = el('td');
      td0.appendChild(r.match ? el('span', 'tag ok', 'ok')
        : r.ok ? el('span', 'tag fail', 'STALE') : el('span', 'tag warn', 'no answer'));
      tr.appendChild(td0);
      tr.appendChild(el('td', 'm', r.fqdn));
      tr.appendChild(el('td', 'm', r.expected));
      tr.appendChild(el('td', 'm', r.answer || (r.reason || '—')));
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

// Read live addresses off every reachable device's console, so the whole page
// (canvas labels, ping targets, cabling) can prefer device truth over manifest
// intent.
LAB.harvest = labTool({
  key: 'harvest', title: 'Addresses', statusUrl: '/api/harvest', runUrl: '/api/harvest/run',
  onStatus(st) { applyHarvestToNodes(st); },
  badge() {
    const st = this.st;
    if (!st) return null;
    if (st.status === 'running') return { text: `${st.done_count}/${st.total}`, level: 'warn' };
    const read = NODES.filter((n) => n.live);
    if (!read.length) return null;
    const drift = read.filter((n) => n.live.error || (n.live_ip && n.live_ip !== n.primary_ip)).length;
    return drift ? { text: String(drift), level: 'warn' } : { text: '✓', level: 'ok' };
  },
  render(box, st) {
    const btn = el('button', 'primary', st.status === 'running' ? `Reading ${st.done_count}/${st.total}…` : 'Read every device');
    btn.disabled = st.status === 'running';
    btn.addEventListener('click', () => LAB.harvest.run());
    const eta = this.eta && st.status === 'running'
      ? el('span', 'lbl', `~${this.eta.eta_seconds}${this.eta.eta_seconds_max ? '–' + this.eta.eta_seconds_max : ''}s`) : null;
    box.appendChild(paneHead('Live addresses', btn, [
      eta,
      st.status === 'error' ? el('span', 'tag fail', st.error) : null,
      this.runError ? el('span', 'tag fail', this.runError) : null,
    ]));
    box.appendChild(el('div', 'note',
      'Reads addresses straight off each device — `show ip interface brief` (IOS), ' +
      '`show interface ip brief` (ASA), `ip -4 -o addr show` (Linux, containers on ' +
      'console line 1) — instead of trusting the manifest. Runs across a pool of ' +
      'parallel console sessions (docs/cml-integration-review.md).'));
    const nodes = NODES.filter((n) => n.live);
    if (!nodes.length) return;
    const drift = nodes.filter((n) => n.live.error || (n.live_ip && n.live_ip !== n.primary_ip));
    box.appendChild(el('div', 'note ' + (drift.length ? 'warn' : 'ok'),
      st.status === 'done'
        ? (drift.length ? `${drift.length} of ${nodes.length} read nodes disagree with, or lack, a manifest match.`
                        : `All ${nodes.length} read nodes match their manifest primary address.`)
        : `${nodes.length} read so far, ${drift.length} disagreement(s).`));
    const t = el('table', 't');
    const hr = el('tr');
    ['', 'Node', 'Live address', 'Manifest primary'].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    drift.concat(nodes.filter((n) => !drift.includes(n))).forEach((n) => {
      const tr = el('tr', 'click');
      const td0 = el('td');
      td0.appendChild(n.live.error ? el('span', 'tag fail', 'read failed')
        : n.live_ip === n.primary_ip ? el('span', 'tag ok', 'match')
        : el('span', 'tag warn', 'DRIFT'));
      tr.appendChild(td0);
      tr.appendChild(el('td', 'm', n.label));
      tr.appendChild(el('td', 'm', n.live.error || n.live_ip || '(no up interface with an address)'));
      tr.appendChild(el('td', 'm', n.primary_ip || '—'));
      tr.addEventListener('click', () => { select(n.label, true); dockTab = 'addresses'; renderDock(); });
      t.appendChild(tr);
    });
    box.appendChild(t);
  },
});

LAB.probes = {
  key: 'probes', title: 'Ping log',
  badge() {
    const run = probeJobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
    return run ? { text: run + '…', level: 'warn' } : probeJobs.length ? { text: String(probeJobs.length) } : null;
  },
  refresh() {},
  draw(box) {
    box.appendChild(paneHead('Ping log', el('span', 'lbl', 'this session · newest first')));
    if (!probeJobs.length) {
      box.appendChild(el('div', 'empty', 'No pings yet. Select a node → Ping tab.'));
      return;
    }
    probeJobs.forEach((j) => {
      const b = jobBlock(j);
      b.style.cursor = 'pointer';
      b.addEventListener('click', () => { select(j.target, true); dockTab = 'probe'; renderDock(); });
      box.appendChild(b);
    });
  },
};

/* Merge harvested per-node results into the in-memory NODES so the canvas,
 * tree and ping targets reflect live data without a full inventory reload.
 * Same selection rule as app.py::_live_primary: the interface carrying the
 * manifest's declared primary wins over any other manifest-known address. */
function applyHarvestToNodes(st) {
  if (!st || !st.nodes) return;
  let changed = false;
  NODES.forEach((n) => {
    const rec = st.nodes[n.label];
    if (!rec || rec === n.live) return;
    n.live = rec;
    const upAddrs = (rec.addresses || []).filter((a) => a.proto === 'up');
    const manifestIps = new Set((n.addresses || []).map((a) => a.value));
    const pick = upAddrs.find((a) => a.ip === n.primary_ip)
      || upAddrs.find((a) => manifestIps.has(a.ip)) || upAddrs[0];
    n.live_ip = pick ? pick.ip : null;
    changed = true;
  });
  if (changed) { renderTree(); renderCanvas(); }
}

/* ------------------------------------------------------------- controls -- */
function wire() {
  // site segment
  document.querySelectorAll('#site-seg button').forEach((b) => {
    b.classList.toggle('on', b.dataset.site === site);
    b.addEventListener('click', () => {
      site = b.dataset.site;
      store.set(SITE_KEY, site);
      document.querySelectorAll('#site-seg button').forEach((x) =>
        x.classList.toggle('on', x.dataset.site === site));
      renderTree(); highlight(); renderStatus();
    });
  });
  const refilter = () => { renderTree(); highlight(); renderStatus(); };
  $('#q').addEventListener('input', refilter);
  $('#groupby').addEventListener('change', renderTree);
  $('#hide-down').addEventListener('change', refilter);
  $('#hidden-btn').addEventListener('click', () => {
    hidden.clear();
    store.set(HIDE_KEY, []);
    renderAll();
  });

  // header actions open the matching dock tab
  document.querySelectorAll('.top-actions [data-tab]').forEach((b) =>
    b.addEventListener('click', () => openLab(b.dataset.tab)));
  $('#theme-btn').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    prefs.theme = dark ? 'light' : 'dark';
    savePrefs(); applyTheme();
  });
  $('#nav-toggle').addEventListener('click', () => document.body.classList.toggle('nav-open'));

  // canvas toggles
  $('#show-ip').checked = prefs.ip;
  $('#show-if').checked = prefs.iface;
  $('#show-traffic').checked = prefs.traffic;
  $('#show-ip').addEventListener('change', (e) => { prefs.ip = e.target.checked; savePrefs(); renderCanvas(); });
  $('#show-if').addEventListener('change', (e) => { prefs.iface = e.target.checked; savePrefs(); renderCanvas(); });
  $('#show-traffic').addEventListener('change', (e) => setTraffic(e.target.checked));
  $('#zoom-in').addEventListener('click', () => zoomAt(1.25));
  $('#zoom-out').addEventListener('click', () => zoomAt(0.8));
  $('#zoom-fit').addEventListener('click', fit);

  wireCanvas();
  wireSplitter();
  applyDock();

  $('#pause').addEventListener('click', () => {
    paused = !paused;
    $('#pause').textContent = paused ? 'Resume' : 'Pause';
    $('#tick').textContent = paused ? '⏸' : countdown + 's';
  });

  document.addEventListener('keydown', (e) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === '/' && !typing) { e.preventDefault(); closeNav(false); $('#q').focus(); }
    if (e.key === 'Escape') { if (typing) document.activeElement.blur(); else select(null); }
    if ((e.key === 'f' || e.key === 'F') && !typing && !e.metaKey && !e.ctrlKey) fit();
  });
}

function closeNav(close = true) {
  if (close) document.body.classList.remove('nav-open');
  else document.body.classList.add('nav-open');
}

function applyTheme() {
  if (prefs.theme) document.documentElement.dataset.theme = prefs.theme;
  else delete document.documentElement.dataset.theme;
}

/* Pan by dragging the background, zoom with the wheel or a pinch, click a
 * node to select it, click empty canvas to clear the selection. */
function wireCanvas() {
  const c = $('#canvas');
  let drag = null;
  const pointers = new Map();
  c.addEventListener('pointerdown', (e) => {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const nodeEl = e.target.closest('.node');
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false, node: nodeEl && nodeEl.dataset.label };
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {            // pinch zoom
      const [p, q] = [...pointers.values()];
      const d = Math.hypot(p.x - q.x, p.y - q.y);
      if (drag && drag.pinch) {
        const r = c.getBoundingClientRect();
        zoomAt(d / drag.pinch, (p.x + q.x) / 2 - r.left, (p.y + q.y) / 2 - r.top);
      }
      if (drag) { drag.pinch = d; drag.moved = true; }
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
    if (!drag.moved) select(drag.node || null);
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
    if (nodeEl) { dockTab = 'probe'; select(nodeEl.dataset.label); }
  });
  let t = null;
  window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(fit, 150); });
}

function wireSplitter() {
  const sp = $('#splitter');
  sp.addEventListener('pointerdown', (e) => {
    sp.setPointerCapture(e.pointerId);
    sp.classList.add('drag');
    const main = $('.main').getBoundingClientRect();
    const move = (ev) => {
      prefs.dockH = Math.round(Math.max(120, Math.min(main.height - 120, main.bottom - ev.clientY)));
      prefs.dockMin = false;
      applyDock();
    };
    const up = () => {
      sp.classList.remove('drag');
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      savePrefs(); renderDock();
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
  });
  sp.addEventListener('dblclick', () => { prefs.dockMin = !prefs.dockMin; savePrefs(); applyDock(); renderDock(); });
}

boot();
