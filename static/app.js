/* NetView client - lab workbench.
 *
 * CML already draws the whole lab, so NetView does not.  The main view is a
 * path: pick a From and a To, and the nodes between them are laid out left to
 * right in hop order, with redundant alternatives stacked in the same column.
 * A trace then shows which of those links the packets actually crossed.
 * Around it: the node list on the left, a properties sidebar for the selected
 * node on the right, and a bottom pane with the node table, the lab-wide
 * checks, the ping log and console tabs.
 *
 * The inventory is fetched once; only /api/state is polled on the tick.
 * Hidden nodes persist as an exclusion list: anything new in the lab defaults
 * to visible, because a dashboard that silently omits part of the estate is
 * worse than no dashboard.
 */
'use strict';

const REFRESH_S = 15;
const SITE_KEY = 'netview.site.v1';
const PREF_KEY = 'netview.workbench.v2';
const SVGNS = 'http://www.w3.org/2000/svg';

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

let INV = null, NODES = [], LINKS = [];
const BY = new Map();
let selected = null, countdown = REFRESH_S, paused = false;
let site = store.get(SITE_KEY, 'all');
if (!['all', 'HQ', 'DR', 'other'].includes(site)) site = 'all';
const prefs = Object.assign({
  theme: null, paneH: 260, paneMin: false,
}, store.get(PREF_KEY, {}));
const savePrefs = () => store.set(PREF_KEY, prefs);

let paneTab = 'trace';           // trace | matrix | dns | con:<label>
let sideTab = 'node';            // node | addresses | interfaces | dhcp
const consoles = [];             // labels with an open console tab
const consoleText = new Map();

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
  setInterval(tickSecond, 1000);
  Object.values(LAB).forEach((t) => t.refresh && t.refresh());
}

/* The whole inventory is re-read from the server on every refresh, and the
 * server re-reads CML (topology, states, addresses, configs) for it, so a
 * node added, moved, re-cabled or re-addressed in CML appears here on the
 * next tick.  Only per-session state (consoles, the trace) is kept. */
async function loadInventory(fresh) {
  const inv = await getJSON('/api/inventory' + (fresh ? '?fresh=1' : ''));
  const keep = new Map(NODES.map((n) => [n.label, { lastSource: n.lastSource }]));
  INV = inv;
  NODES = INV.nodes.concat(INV.ghosts.map((g) => Object.assign({
    ghost: true, up: false, dns: {}, links: [], vlans: [], addresses: [],
    ha: {}, interfaces: [], probe_family: 'none',
    can_source_probe: false, addressing: 'unaddressed',
  }, g))).concat((INV.virtual_targets || []).map((v) => ({
    label: v.label, virtual: true, virtual_kind: v.kind, note: v.note,
    up: true, ghost: false, dns: {}, links: [], vlans: [], addresses: [],
    ha: {}, interfaces: [], probe_family: 'none', can_source_probe: false,
    addressing: 'unaddressed', state: null, layer: 'virtual', layer_title: 'Virtual (DNS/anycast failover)',
    site: null, role: null,
    role_title: v.kind === 'dns-failover' ? 'DNS failover name' : 'Anycast failover VIP',
    config_status: 'n/a', primary_ip: v.ip || null, live_ip: null,
  })));
  BY.clear();
  NODES.forEach((n) => { Object.assign(n, keep.get(n.label) || {}); BY.set(n.label, n); });
  LINKS = buildLinks();
  if (selected && !BY.has(selected)) selected = null;
}

async function poll(fresh) {
  try {
    await loadInventory(fresh);
    if (typeof pollDisaster === 'function') pollDisaster();
    countdown = REFRESH_S;
    renderPathBody(); renderNodeList(); renderStatus(); renderPaneTabs();
    if (!$('#path-head').contains(document.activeElement)) renderPathHead();
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

function infobar() {
  const box = $('#banners');
  box.innerHTML = '';
  const m = INV.meta;
  const line = (cls, text) => {
    const d = el('div');
    d.appendChild(el('span', cls, text));
    const x = el('button', 'linkish', 'Dismiss');
    x.addEventListener('click', () => { d.remove(); box.hidden = !box.children.length; });
    d.appendChild(x);
    box.appendChild(d);
  };
  if (m.topology_source !== 'live') {
    const age = m.snapshot && m.snapshot.age_hours;
    line('bad', `CML is unreachable. Showing the cached snapshot (${age != null ? age + ' h old' : 'age unknown'}); ` +
      'anything changed in the lab since then is missing.');
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

/* Search and the site selector narrow the node list. */
function matches(n) {
  const q = $('#q').value.trim().toLowerCase();
  if (site !== 'all' && siteBucket(n) !== site) return false;
  return !q || hay(n).includes(q);
}

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
  if (n.virtual) return 'virtual';
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
  virtual: 'Virtual (failover) target',
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
  // A diamond, not a device shape - this isn't a physical box on a rack,
  // it's a name/address that resolves to whichever real one currently
  // answers, which is exactly what makes it worth tracing to.
  virtual: () => ['M0 -11L11 0L0 11L-11 0Z', 'M0 -5L0 5M-5 0L5 0'],
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

const shortIf = (s) => (s || '')
  .replace(/^GigabitEthernet/i, 'Gi').replace(/^TenGigabitEthernet/i, 'Te')
  .replace(/^FastEthernet/i, 'Fa').replace(/^Ethernet/i, 'e').replace(/^Management/i, 'Mgmt')
  .replace(/^Port-channel/i, 'Po').replace(/^Vlan/i, 'Vl');

/* ------------------------------------------------------------ selection -- */
function select(label) {
  selected = label;
  const n = label && BY.get(label);
  if (n && sideTab === 'dhcp' && !n.dhcp) sideTab = 'node';
  if (n) document.body.classList.add('side-open');
  markPathSelection(); renderSide(); renderStatus();
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

  const h = LAB.harvest.st;
  $('#harvest-line').textContent = h && h.status === 'running' ? `Reading consoles ${h.done_count}/${h.total}` : '';

  const n = selected && BY.get(selected);
  $('#sel-line').textContent = n ? `${n.label}  (${n.node_definition || 'not in lab'})` : '';
}

function renderAll() { renderPathHead(); renderPathBody(); renderNodeList(); renderStatus(); renderPane(); renderSide(); }

/* ============================================================== sidebar == */
const SIDE_TABS = [['node', 'Node'], ['addresses', 'Addresses'], ['interfaces', 'Interfaces'],
                   ['dhcp', 'DHCP']];

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
    const b = el('button', 'tab' + (sideTab === k ? ' on' : ''), t);
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
               dhcp: sideDhcp }[sideTab] || sideNode;
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

  const rows = [];
  const link = (n, text, cls) => {
    const a = el('a', cls, text);
    a.href = '#';
    a.addEventListener('click', (e) => { e.preventDefault(); select(n.label); });
    rows.push([n.label, a]);
  };
  NODES.filter((n) => n.ghost).forEach((n) => link(n, 'in manifest, not in lab', 's-bad'));
  NODES.filter((n) => n.config_status === 'undeclared').forEach((n) => link(n, 'in lab, not in manifest', 's-warn'));
  NODES.filter((n) => n.live_ip && n.live_ip !== n.primary_ip)
    .forEach((n) => link(n, `address ${n.live_ip}, manifest ${n.primary_ip || 'none'}`, 's-warn'));
  d.appendChild(sect('Needs attention', rows.length ? props(rows) : el('div', 'side-empty', 'Nothing flagged.')));
  d.appendChild(el('div', 'side-empty',
    'Click a node in the list to inspect it; right-click for actions. Choose From and To above the path to trace between two nodes.'));
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
  if (!n.ghost && !n.can_source_probe && n.no_source_reason) {
    d.appendChild(el('div', 'pmsg', 'Cannot send test traffic: ' + n.no_source_reason + '. It can still be a target.'));
  }

  const acts = el('div', 'acts');
  if (!n.ghost) {
    const c = el('button', null, 'Console log');
    c.addEventListener('click', () => openConsole(n.label));
    acts.appendChild(c);
    if (n.can_source_probe && n.up) {
      const t = el('button', null, 'Trace from here');
      t.addEventListener('click', () => setTraceEnds(n.label, trace.target === n.label ? null : trace.target));
      acts.appendChild(t);
    }
    if (trace.source && trace.source !== n.label) {
      const t2 = el('button', null, 'Trace to here');
      t2.addEventListener('click', () => setTraceEnds(trace.source, n.label));
      acts.appendChild(t2);
    }
  }
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
  b.addEventListener('click', () => LAB.harvest.run());
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
  d.appendChild(head);
  const links = new Map((n.links || []).map((l) => [l.local_iface, l]));
  const ifs = (n.iface_detail || []).length ? n.iface_detail
    : (n.links || []).map((l) => ({ name: l.local_iface, connected: true, ip4: [] }));
  if (!ifs.length) { d.appendChild(el('div', 'side-empty', 'No interfaces.')); return d; }
  const t = gridTable(['Interface', 'Peer', 'Address']);
  ifs.forEach((f) => {
    const l = links.get(f.name);
    const tr = el('tr', 'row' + (l ? '' : ' off'));
    const c0 = el('td', 'm', shortIf(f.name));
    c0.title = [f.name, f.mac && 'MAC ' + f.mac, f.desc, f.vlan && 'access VLAN ' + f.vlan, f.shutdown && 'shutdown in config']
      .filter(Boolean).join('\n');
    tr.appendChild(c0);
    const peer = el('td', 'm');
    if (l) {
      const a = el('a', l.state === 'STOPPED' ? 's-dim' : null, l.peer + ' ' + shortIf(l.peer_iface));
      a.href = '#';
      a.addEventListener('click', (e) => { e.preventDefault(); select(l.peer); });
      peer.appendChild(a);
      if (l.state === 'STOPPED') peer.appendChild(el('span', 'src', 'link stopped'));
    } else peer.textContent = '-';
    tr.appendChild(peer);
    tr.appendChild(el('td', 'm', (f.ip4 || []).concat(f.config_ip && !(f.ip4 || []).includes(f.config_ip) ? [f.config_ip] : []).join(', ') || (l && l.subnet) || '-'));
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

/* =========================================================== bottom pane == */
function showPane(k) {
  paneTab = k;
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
  add('trace', 'Trace details', trace.job ? { text: 'running', level: 'warn' }
    : trace.result ? { text: trace.result.verdict.level === 'ok' ? 'confirmed' : trace.result.verdict.level === 'fail' ? 'failed' : 'check',
                       level: trace.result.verdict.level === 'ok' ? 'ok' : trace.result.verdict.level === 'fail' ? 'bad' : 'warn' } : null);
  Object.entries(LAB).forEach(([k, t]) => { if (!t.hidden) add(k, t.title, t.badge()); });
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
  if (paneTab.startsWith('con:')) body.appendChild(consolePane(paneTab.slice(4)));
  else if (paneTab === 'trace') body.appendChild(traceDetails());
  else if (LAB[paneTab]) LAB[paneTab].draw(body);
  body.scrollTop = scroll;
}

function applyPane() {
  document.documentElement.style.setProperty('--pane-h', prefs.paneH + 'px');
  $('.center').classList.toggle('pane-min', prefs.paneMin);
}

/* ----------------------------------------------------------- node flags -- */
function flags(n) {
  const f = [];
  if (n.ghost) f.push('absent');
  if (n.config_status === 'undeclared') f.push('undeclared');
  if (displayIp(n).drift) f.push('address drift');
  return f;
}

function refilter() {
  renderNodeList(); renderStatus();
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
  if (paneTab === 'con:' + label) paneTab = consoles.length ? 'con:' + consoles[consoles.length - 1] : 'trace';
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
      'Paths from probe_matrix.py. "Must fail" rows are perimeter denies; one passing is a leak. Click a row to trace it.',
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
      const dst = ownerOf(p.dest) || (BY.has(p.dest) ? p.dest : null);
      tr.title = dst ? `Click to trace ${p.source} to ${dst}` : 'Destination is not a node in this lab';
      tr.addEventListener('click', () => {
        if (!dst || !BY.has(p.source)) return;
        setTraceEnds(p.source, dst, /^\d+\.\d+\.\d+\.\d+$/.test(p.dest) ? p.dest : null);
      });
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

// Read running addresses off every reachable device's console.  No tab of its
// own: results land in each node's Addresses tab and the node list's drift
// flags, and progress shows in the status bar.
LAB.harvest = labCheck({
  key: 'harvest', statusUrl: '/api/harvest', runUrl: '/api/harvest/run', hidden: true,
  onStatus(st) { applyHarvestToNodes(st); renderStatus(); },
});

/* Merge harvested per-node results into the in-memory NODES so the node
 * list, sidebar and trace targets reflect live data without an inventory reload.
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
    renderPathBody(); renderNodeList();
    renderNodeList();
    if (selected) renderSide();
  }
}

/* ============================================================ node list == */
/* Always on the left: every node, grouped, filterable.  Click to inspect,
 * From / To to set the ends of the path, right-click for everything else. */
function renderNodeList() {
  const box = $('#nlist');
  const scroll = box.scrollTop;
  box.innerHTML = '';
  const vis = NODES.filter((n) => matches(n)).sort(byLabel);
  if (!vis.length) { box.appendChild(el('div', 'side-empty', 'No nodes match.')); return; }
  const groups = new Map();
  vis.forEach((n) => {
    if (!groups.has(n.layer)) groups.set(n.layer, { title: n.layer_title || n.layer, nodes: [] });
    groups.get(n.layer).nodes.push(n);
  });
  INV.layers.map((l) => l.key).filter((k) => groups.has(k)).forEach((k) => {
    const g = groups.get(k);
    box.appendChild(el('div', 'nl-grp', `${g.title}  ${g.nodes.filter((n) => n.up).length}/${g.nodes.length}`));
    g.nodes.forEach((n) => box.appendChild(nodeListRow(n)));
  });
  box.scrollTop = scroll;
}

function nodeListRow(n) {
  const r = el('div', 'nl-row' + (n.up || n.ghost ? '' : ' off') + (n.label === selected ? ' sel' : '')
    + (n.label === trace.source ? ' is-from' : '') + (n.label === trace.target ? ' is-to' : ''));
  r.dataset.label = n.label;
  r.appendChild(lamp(n));
  const mid = el('div', 'nl-mid');
  mid.appendChild(el('div', 'nl-name', n.label));
  const ip = displayIp(n);
  const fl = flags(n);
  mid.appendChild(el('div', 'nl-sub' + (fl.length ? ' s-warn' : ''), fl.length ? fl.join(', ') : ip.text || KIND_NAME[kind(n)]));
  r.appendChild(mid);
  const acts = el('div', 'nl-acts');
  if (n.label === trace.source) acts.appendChild(el('span', 'nl-end', 'FROM'));
  else if (n.label === trace.target) acts.appendChild(el('span', 'nl-end', 'TO'));
  else {
    const f = el('button', 'linkish', 'From');
    f.disabled = !n.can_source_probe || !n.up;
    f.title = f.disabled ? 'Cannot send test traffic (not running, or no console this tool can drive)' : 'Trace from this node';
    f.addEventListener('click', (e) => { e.stopPropagation(); setTraceEnds(n.label, trace.target); });
    const t = el('button', 'linkish', 'To');
    t.disabled = !!n.ghost;
    t.title = 'Trace to this node';
    t.addEventListener('click', (e) => { e.stopPropagation(); setTraceEnds(trace.source, n.label); });
    acts.appendChild(f); acts.appendChild(t);
  }
  r.appendChild(acts);
  r.addEventListener('click', () => {
    if (pick) pickNode(n.label); else { select(n.label); document.body.classList.remove('nav-open'); }
  });
  r.addEventListener('dblclick', () => { if (!n.ghost) openConsole(n.label); });
  r.addEventListener('contextmenu', (e) => { e.preventDefault(); select(n.label); nodeMenu(n, e.clientX, e.clientY); });
  r.addEventListener('mouseenter', () => hoverPath(n.label));
  r.addEventListener('mouseleave', () => hoverPath(null));
  return r;
}

/* =========================================================== path trace == */
/* The main view.  Before a run it shows every shortest path over the running
 * part of the lab (the routes the packets COULD take).  A run sends a burst
 * of pings and reads CML's per-link packet counters before and after, so it
 * shows the links the packets DID cross, at layer 2 as well as layer 3.  Run
 * it, stop the active core switch in CML, run it again: the path moves. */
const trace = { source: null, target: null, ip: null, count: 100, repeat: false, dns: false,
                capture: true, job: null, stage: '', result: null, cand: null, history: [], error: null,
                // Disaster trace (2026-09-29, docs/disaster-spec.md): nodes to power off
                // between a baseline trace and a second one.
                disaster: { open: false, nodes: [], settle: 30, restore: true, confirming: false, filter: '' } };
let pick = null;           // null | 'source' | 'target'

function startPick(src) {
  if (src) { trace.source = src; pick = 'target'; } else { pick = 'source'; }
  pickBar();
  renderNodeList();
}
function cancelPick() { pick = null; pickBar(); }

function pickNode(label) {
  const n = BY.get(label);
  if (!n) return;
  if (pick === 'source') {
    if (!n.can_source_probe || !n.up) {
      pickBar(`${label} cannot send the test traffic (${!n.up ? 'not running' : 'no console this tool can drive'}). Pick another.`);
      return;
    }
    pick = 'target';
    setTraceEnds(label, trace.target === label ? null : trace.target);
    pickBar();
    return;
  }
  if (label === trace.source || n.ghost) return;
  pick = null;
  pickBar();
  setTraceEnds(trace.source, label);
}

function pickBar(msg) {
  const bar = $('#pickbar');
  bar.hidden = !pick;
  if (!pick) return;
  bar.innerHTML = '';
  bar.appendChild(el('span', 't', 'Trace path'));
  bar.appendChild(el('span', null, msg || (pick === 'source'
    ? 'Click the node to send from in the node list. It must be running and have a console.'
    : `From ${trace.source}. Now click the destination in the node list.`)));
  const c = el('button', 'linkish', 'Cancel');
  c.addEventListener('click', cancelPick);
  bar.appendChild(c);
}

/* ip -> node label, from every address the page knows. */
function ownerOf(ip) {
  for (const n of NODES) {
    if (n.live_ip === ip || n.primary_ip === ip) return n.label;
    if ((n.addresses || []).some((a) => a.value === ip && !/virtual/.test(a.kind || ''))) return n.label;
  }
  return null;
}

function setTraceEnds(src, dst, ip) {
  const changed = src !== trace.source || dst !== trace.target;
  trace.source = src || null;
  trace.target = dst && dst !== src ? dst : null;
  const t = BY.get(trace.target);
  if (ip) trace.ip = ip;
  else if (changed || !trace.ip) trace.ip = t ? (t.live_ip || t.primary_ip) : null;
  if (changed) {
    trace.result = [...trace.history].reverse()
      .find((h) => h.source === trace.source && askedTarget(h) === trace.target) || null;
    trace.error = null;
  }
  trace.cand = trace.source && trace.target ? candidates(trace.source, effectiveTarget()) : null;
  renderPathHead(); renderPathBody(); renderNodeList(); renderPaneTabs();
  if (paneTab === 'trace') renderPaneBody();
}

/* Every node and link on SOME shortest path, over running nodes and started
 * links: the set of routes the traffic could take. */
function candidates(src, dst, exclude) {
  const up = (n) => n && (n.up || n.label === src) && !(exclude && exclude.has(n.label));
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
  const none = { nodes: [], links: [], paths: 0, hops: null };
  if (!adj.has(src) || !adj.has(dst)) return none;
  const ds = bfs(src), dt = bfs(dst);
  if (!ds.has(dst)) return none;
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
  return { nodes: on, links: [...links], paths: ways.get(dst) || 0, hops: D };
}

async function runTrace() {
  if (!trace.source || !trace.target || trace.job) return;
  trace.error = null;
  trace.job = { t0: Date.now(), eta: 30 };
  trace.stage = 'starting';
  renderPathHead(); renderPathBody(); renderPaneTabs();
  let liveCount = 0;
  try {
    const r = await fetch('/api/trace', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: trace.source, target: trace.target, target_ip: trace.ip,
                             count: trace.count, dns: trace.dns, capture: trace.capture,
                             disaster: trace.disaster.nodes.length ? { nodes: trace.disaster.nodes,
                               settle_s: trace.disaster.settle, restore: trace.disaster.restore } : undefined }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    trace.job.id = j.job_id;
    trace.job.disaster = !!trace.disaster.nodes.length;
    trace.disaster.confirming = false;
    trace.job.eta = j.eta_seconds || 30;
    // Capture mode polls faster: CML lets a link's packets be read WHILE its
    // capture is still running (verified live), so each tick can flash the
    // wires that actually carried something since the last one, as the
    // burst happens - not a canned replay stitched together afterward.
    const pollMs = trace.capture ? 400 : 1200;
    for (;;) {
      await new Promise((ok) => setTimeout(ok, pollMs));
      const st = await getJSON('/api/trace/' + j.job_id);
      trace.stage = st.stage;
      const s = document.querySelector('[data-trace-stage]');
      if (s) s.textContent = st.stage;
      const live = st.live_packets || [];
      if (live.length > liveCount) {
        live.slice(liveCount).forEach((e) => flashLink(e.link_id, e.dir));
        liveCount = live.length;
        const c = document.querySelector('[data-live-count]');
        if (c) c.textContent = `${liveCount} packets captured so far`;
      }
      if (st.status === 'error') throw new Error(st.error);
      if (st.status === 'done') {
        const res = st.result;
        const prev = [...trace.history].reverse()
          .find((h) => h.source === res.source && askedTarget(h) === askedTarget(res));
        res.changed = prev && prev.path.join('>') !== res.path.join('>') ? { from: prev.path, at: prev.at } : null;
        trace.history.push(res);
        if (res.source === trace.source && askedTarget(res) === trace.target) trace.result = res;
        break;
      }
    }
  } catch (e) { trace.error = String(e.message || e); }
  trace.job = null;
  // Draw the result straight away; the inventory refresh below can take tens of
  // seconds when CML is slow, and the page used to show neither the progress
  // bar nor the result for that long (UI test 2026-09-30).
  renderPathHead(); renderPathBody(); renderPaneTabs();
  await poll(false);           // the lab may have changed; refresh before drawing
  if (trace.source && trace.target) trace.cand = candidates(trace.source, effectiveTarget());
  renderPathHead(); renderPathBody(); renderPaneTabs();
  if (paneTab === 'trace') renderPaneBody();
  if (trace.repeat && !trace.error) setTimeout(() => { if (trace.repeat && !trace.job) runTrace(); }, 5000);
}

/* A trace to a virtual (failover) target comes back labelled with the real
 * node that answered, so results are matched on what was ASKED for, and the
 * diagram is drawn to what actually ANSWERED. */
const askedTarget = (r) => r.virtual_target || r.target;

function resultFor() {
  const r = trace.result;
  return r && r.source === trace.source && askedTarget(r) === trace.target ? r : null;
}

function effectiveTarget() {
  const r = resultFor();
  return r && r.virtual_target ? r.target : trace.target;
}

/* ------------------------------------------------------- the controls --- */
function renderPathHead() {
  const head = $('#path-head');
  head.innerHTML = '';
  const sel = (items, value, onChange, empty) => {
    const s = el('select');
    const o = el('option', null, empty); o.value = ''; s.appendChild(o);
    items.forEach(([v, t]) => { const x = el('option', null, t); x.value = v; s.appendChild(x); });
    s.value = value || '';
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };
  const sources = NODES.filter((n) => n.can_source_probe && n.up).sort(byLabel).map((n) => [n.label, n.label]);
  const targets = NODES.filter((n) => !n.ghost && n.label !== trace.source).sort(byLabel).map((n) => [n.label, n.label]);
  const lf = el('label', null, 'From');
  lf.appendChild(sel(sources, trace.source, (v) => setTraceEnds(v || null, trace.target), 'choose a source'));
  head.appendChild(lf);
  const sw = el('button', null, 'Swap');
  sw.title = 'Trace the reverse direction';
  sw.disabled = !trace.source || !trace.target || !BY.get(trace.target).can_source_probe || !BY.get(trace.target).up;
  sw.addEventListener('click', () => setTraceEnds(trace.target, trace.source));
  head.appendChild(sw);
  const lt = el('label', null, 'To');
  lt.appendChild(sel(targets, trace.target, (v) => setTraceEnds(trace.source, v || null), 'choose a destination'));
  head.appendChild(lt);
  const t = BY.get(trace.target);
  if (t) {
    const ips = [];
    const seen = new Set();
    [trace.ip, t.live_ip, t.primary_ip, ...(t.addresses || []).filter((a) => !/virtual/.test(a.kind || '')).map((a) => a.value)]
      .forEach((ip) => { if (ip && !seen.has(ip)) { seen.add(ip); ips.push([ip, ip]); } });
    const la = el('label', null, 'Address');
    const s = sel(ips, trace.ip, (v) => { trace.ip = v; },
                 t.virtual && t.virtual_kind === 'dns-failover' ? 'resolved live at trace time' : 'none known');
    s.classList.add('m');
    la.appendChild(s);
    if (t.virtual && t.note) la.title = t.note;
    head.appendChild(la);
  }
  const lc = el('label', null, 'Packets');
  const cnt = el('input');
  cnt.type = 'number'; cnt.min = 20; cnt.max = 500; cnt.step = 20; cnt.value = trace.count;
  cnt.style.width = '62px';
  cnt.addEventListener('change', () => { trace.count = Math.max(20, Math.min(500, +cnt.value || 100)); });
  lc.appendChild(cnt);
  head.appendChild(lc);
  const dzOn = trace.disaster.nodes.length > 0;
  const run = el('button', 'default', trace.job ? 'Tracing...' : dzOn ? 'Run disaster trace' : 'Run trace');
  // A DNS failover name has no address until the trace resolves it, live, from
  // the source's own site - so it is runnable without one.
  run.disabled = !!trace.job || !trace.source || !trace.target ||
    (!trace.ip && !(BY.get(trace.target) || {}).virtual);
  run.addEventListener('click', () => {
    if (trace.disaster.nodes.length) {        // confirmation lives in the disaster panel
      trace.disaster.open = true; trace.disaster.confirming = true;
      renderPathBody();
      return;
    }
    runTrace();
  });
  head.appendChild(run);
  const dzb = el('button', dzOn ? 'dz-toggle on' : 'dz-toggle', dzOn ? `Disaster (${trace.disaster.nodes.length})` : 'Disaster...');
  dzb.title = 'Power off chosen nodes between two traces and compare the results';
  dzb.disabled = !!trace.job || !trace.source || !trace.target;
  dzb.addEventListener('click', () => { trace.disaster.open = !trace.disaster.open; trace.disaster.confirming = false; renderPathHead(); renderPathBody(); });
  head.appendChild(dzb);
  const dl = el('label');
  const dc = el('input'); dc.type = 'checkbox'; dc.checked = trace.dns;
  dc.disabled = !t || !(t.dns || {}).fqdn;
  dc.addEventListener('change', () => { trace.dns = dc.checked; });
  dl.appendChild(dc); dl.appendChild(document.createTextNode('Check DNS'));
  dl.title = t && (t.dns || {}).fqdn ? `Also resolve ${t.dns.fqdn} and compare the answer with the address being traced`
    : 'The destination has no internal DNS name';
  head.appendChild(dl);
  const ml = el('label', 'mode-pick');
  ml.appendChild(document.createTextNode('Mode '));
  const ms = el('select');
  [['quick', 'Quick (counters)'], ['verified', 'Verified (capture)']].forEach(([v, t]) => {
    const o = el('option', null, t); o.value = v; ms.appendChild(o);
  });
  ms.value = trace.capture ? 'verified' : 'quick';
  ms.addEventListener('change', () => { trace.capture = ms.value === 'verified'; });
  ml.appendChild(ms);
  ml.title = 'Quick reads before/after packet counters - fast, but a byte count can\'t tell this ' +
             'test\'s traffic from anything else crossing the same link at the same moment, so a ' +
             'result can vary between identical runs. Verified captures the actual packets on every ' +
             'candidate link and checks each one\'s IP endpoints and ICMP id against this exact test ' +
             'before counting it - slower (a capture start/stop per link), but the path and verdict ' +
             'shown can\'t be shifted by anything else happening on the network.';
  head.appendChild(ml);
  const rl = el('label');
  const rc = el('input'); rc.type = 'checkbox'; rc.checked = trace.repeat;
  rc.disabled = trace.disaster.nodes.length > 0;
  rc.addEventListener('change', () => { trace.repeat = rc.checked; if (rc.checked && !trace.job) runTrace(); });
  rl.appendChild(rc); rl.appendChild(document.createTextNode('Repeat'));
  rl.title = 'Run again 5 s after each trace finishes. Leave it on while you stop and start nodes in CML to watch the path move.';
  head.appendChild(rl);
}

/* ---------------------------------------------------------- the view --- */
function renderPathBody() {
  const body = $('#path-body');
  body.innerHTML = '';
  if (!trace.source || !trace.target) {
    const e = el('div', 'path-empty');
    e.appendChild(el('div', 'pe-t', trace.source ? `From ${trace.source}. Choose a destination.` : 'Choose two nodes to see the path between them.'));
    e.appendChild(el('div', null, 'Use From and To above, the From / To links in the node list, or right-click any node. ' +
      'The nodes between them are drawn here in hop order; redundant alternatives stack in the same column.'));
    body.appendChild(e);
    return;
  }
  const res = resultFor();
  const cand = trace.cand || { nodes: [], links: [], paths: 0 };
  if (trace.disaster.open && !trace.job) body.appendChild(disasterPanel());
  const top = el('div', 'path-status');
  if (trace.job) {
    const st = el('span', 's-dim', trace.stage);
    st.dataset.traceStage = '1';
    top.appendChild(el('b', null, 'Tracing  '));
    top.appendChild(st);
    const bar = el('div', 'meter'); const fill = el('i'); bar.appendChild(fill);
    const pct = () => Math.min(97, (Date.now() - trace.job.t0) / 1000 / trace.job.eta * 100) + '%';
    fill.style.width = pct();
    const iv = setInterval(() => { if (!fill.isConnected || !trace.job) clearInterval(iv); else fill.style.width = pct(); }, 500);
    top.appendChild(bar);
    if (trace.job.disaster && trace.job.id) {
      const cx = el('button', 'linkish', 'Cancel');
      cx.title = 'Stop after the current step. Powered-off nodes are always started again.';
      cx.addEventListener('click', async () => {
        cx.disabled = true; cx.textContent = 'Cancelling (nodes will be restored)...';
        try { await fetch(`/api/trace/${trace.job.id}/cancel`, { method: 'POST' }); } catch (e) { /* next poll shows it */ }
      });
      top.appendChild(document.createTextNode(' '));
      top.appendChild(cx);
    }
    if (trace.capture) {
      const lc = el('span', 's-dim');
      lc.dataset.liveCount = '1';
      top.appendChild(document.createTextNode(' '));
      top.appendChild(lc);
    }
  } else if (trace.error) {
    top.appendChild(el('span', 's-bad', 'Trace failed: ' + trace.error));
  } else if (res) {
    const v = res.verdict;
    if (res.virtual_target) {
      const tag = el('span', 'mode-tag virtual', '◆ ' + res.virtual_target);
      tag.title = `A virtual (failover) target - traced to ${res.target}, whichever real ` +
                  'backend currently answers for it.';
      top.appendChild(tag);
    }
    if (res.mode === 'capture') {
      const tag = el('span', 'mode-tag verified', 'VERIFIED');
      tag.title = 'Path and verdict came from captured packets, not byte counters - not affected by other traffic on the network.';
      top.appendChild(tag);
    }
    top.appendChild(el('span', v.level === 'ok' ? 's-ok' : v.level === 'fail' ? 's-bad' : 's-warn', v.text));
    top.appendChild(el('span', 's-dim', `   ${new Date(res.at).toLocaleTimeString()}, ${res.sent} sent, ` +
      `${res.received == null ? 'replies not counted' : res.received + ' answered'}, ${res.elapsed_s} s`));
  } else {
    top.appendChild(el('span', null, cand.paths
      ? `${cand.paths} possible path${cand.paths > 1 ? 's' : ''} of ${cand.hops} hops over the running lab. Run the trace to see which one the packets take.`
      : 'No path between these nodes over the running part of the lab.'));
  }
  body.appendChild(top);
  if (res && res.changed && !trace.job) {
    const was = res.changed.from.filter((x) => !res.path.includes(x));
    const now = res.path.filter((x) => !res.changed.from.includes(x));
    body.appendChild(el('div', 'path-changed', `Path changed since ${new Date(res.changed.at).toLocaleTimeString()}: ` +
      (was.length ? `no longer through ${was.join(', ')}` : '') + (was.length && now.length ? '; ' : '') +
      (now.length ? `now through ${now.join(', ')}` : '') + '.'));
  }
  body.appendChild(pathDiagram(res, cand, body.clientWidth));
  if (res) {
    const f = el('div', 'path-findings');
    res.findings.forEach((x) => f.appendChild(el('div', null, x)));
    if (res.findings.length) body.appendChild(f);
    if (res.packet_log && res.packet_log.length) body.appendChild(packetLogPanel(res));
    if (res.disaster) body.appendChild(disasterResult(res, body.clientWidth));
  }
  markPathSelection();
}

/* ------------------------------------------- packet log + replay on the topology --- */
const PKT_LOG_CAP = 300;   // above this, sample evenly - 2000+ DOM rows and timers is just jank, not more proof

function packetLogPanel(res) {
  const full = res.packet_log;
  const step = Math.max(1, Math.ceil(full.length / PKT_LOG_CAP));
  const log = step === 1 ? full : full.filter((_, i) => i % step === 0);
  const box = el('div', 'pkt-log');
  const head = el('div', 'pkt-log-head');
  head.appendChild(el('b', null, step === 1 ? `Packets captured (${log.length})`
                                            : `Packets captured (showing ${log.length} of ${full.length})`));
  head.appendChild(el('span', 's-dim', ' — real ICMP echoes seen on each candidate link, by timestamp'));
  const btn = el('button', 'default', 'Replay on topology');
  btn.addEventListener('click', () => replayPackets(log, btn));
  head.appendChild(btn);
  box.appendChild(head);

  const wrap = el('div', 'pkt-log-wrap');
  const t = el('table', 'g');
  const thead = el('tr');
  ['t (ms)', 'link', 'iface', '', 'direction', 'seq'].forEach((h) => thead.appendChild(el('th', null, h)));
  t.appendChild(thead);
  log.forEach((e, i) => {
    const tr = el('tr', 'row pkt-row ' + e.dir);
    tr.dataset.pktIndex = String(i);
    tr.appendChild(el('td', null, String(e.t_ms)));
    tr.appendChild(el('td', null, e.a));
    tr.appendChild(el('td', null, e.ai));
    tr.appendChild(el('td', null, e.dir === 'request' ? '→' : e.dir === 'esp' ? '⇄' : '←'));
    const nat = e.nat === 'snat' ? `, translated by ${e.nat_by}: ${e.src} → ${e.dst}`
      : e.nat === 'dnat' ? `, destination translated: ${e.src} → ${e.dst}` : '';
    tr.appendChild(el('td', null, e.dir === 'esp' ? `esp, encrypted (${e.src} → ${e.dst})` : `${e.dir} (${e.b} ${e.bi})${nat}`));
    tr.appendChild(el('td', null, e.seq || ''));
    t.appendChild(tr);
  });
  wrap.appendChild(t);
  box.appendChild(wrap);
  return box;
}

function flashLink(linkId, dir) {
  const w = document.querySelector(`.pdiag [data-link-id="${linkId}"]`);
  if (!w) return;
  w.classList.add('pkt-flash', dir === 'request' ? 'pkt-req' : 'pkt-reply');
  setTimeout(() => w.classList.remove('pkt-flash', 'pkt-req', 'pkt-reply'), 220);
}

function replayPackets(log, btn) {
  if (!log.length) return;
  btn.disabled = true;
  const origText = btn.textContent;
  const span = log[log.length - 1].t_ms - log[0].t_ms || 1;
  // Real captures finish in well under a second on this lab - too fast to
  // watch. Stretch playback to a length a person can actually follow while
  // keeping every packet's relative order and spacing intact.
  const playMs = Math.min(8000, Math.max(2500, span));
  const scale = playMs / span;
  let done = 0;
  log.forEach((e, i) => {
    setTimeout(() => {
      flashLink(e.link_id, e.dir);
      const row = document.querySelector(`tr.pkt-row[data-pkt-index="${i}"]`);
      if (row) {
        row.classList.add('hl');
        row.scrollIntoView({ block: 'nearest' });
        setTimeout(() => row.classList.remove('hl'), 220);
      }
      done++;
      if (done === log.length) { btn.disabled = false; btn.textContent = origText; }
    }, (e.t_ms - log[0].t_ms) * scale);
  });
  btn.textContent = 'Replaying...';
}

function markPathSelection() {
  document.querySelectorAll('.pdiag .node').forEach((g) => g.classList.toggle('sel', g.dataset.label === selected));
  document.querySelectorAll('#nlist .nl-row').forEach((r) => r.classList.toggle('sel', r.dataset.label === selected));
  document.querySelectorAll('#pane-body tr.row[data-label]').forEach((tr) => tr.classList.toggle('sel', tr.dataset.label === selected));
}
function hoverPath(label) {
  document.querySelectorAll('.pdiag .node').forEach((g) => g.classList.toggle('hov', g.dataset.label === label));
}

/* The nodes between the two ends, left to right by hop count from the
 * source.  Parallel boxes in one column are redundant alternatives.  After a
 * failover, the previous path's nodes stay on the diagram, dotted, so the
 * swap is visible rather than just described. */
function pathDiagram(res, cand, width, opts) {
  const down = (opts && opts.down) || new Set();
  const set = new Set(cand.nodes || []);
  const used = res ? res.links.filter((l) => l.use !== 'none') : [];
  if (res) { res.path.forEach((x) => set.add(x)); used.forEach((l) => { set.add(l.a); set.add(l.b); }); }
  const dstLabel = res && res.virtual_target ? res.target : trace.target;
  set.add(trace.source); set.add(dstLabel);
  const prevPath = res && res.changed ? res.changed.from.filter((x) => BY.has(x)) : [];
  prevPath.forEach((x) => set.add(x));
  const usedBy = new Map(used.map((l) => [l.id, l]));
  const candLinks = new Set(cand.links || []);
  const edges = [];
  const seen = new Set();
  const pairKey = (a, b) => [a, b].sort().join('|');
  [...set].forEach((u) => ((BY.get(u) || {}).links || []).forEach((l) => {
    if (!set.has(l.peer) || seen.has(l.link_id)) return;
    if (!usedBy.has(l.link_id) && !candLinks.has(l.link_id)) return;
    seen.add(l.link_id);
    edges.push({ a: u, b: l.peer, id: l.link_id, kind: usedBy.has(l.link_id) ? 'used' : 'cand' });
  }));
  const drawn = new Set(edges.map((e) => pairKey(e.a, e.b)));
  for (let i = 1; i < prevPath.length; i++) {
    const k = pairKey(prevPath[i - 1], prevPath[i]);
    if (!drawn.has(k)) { drawn.add(k); edges.push({ a: prevPath[i - 1], b: prevPath[i], kind: 'prev' }); }
  }
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
  cols.forEach((c) => c && c.sort((a, b) => (onPath.has(b) - onPath.has(a)) || a.localeCompare(b, undefined, { numeric: true })));

  // Lay the columns out like lines of text: when the path is longer than
  // the view is wide, it wraps onto the next row and carries on left to
  // right, so every hop stays readable without zooming or scrolling.
  const padX = 80, padY = 46, rowH = 100, minColW = 170, segGap = 70;
  const nCols = cols.length;
  const avail = Math.max(minColW * 2, (width || 1000) - padX * 2);
  const perSeg = Math.max(2, Math.min(nCols, Math.floor(avail / minColW) + 1));
  const colW = perSeg > 1 ? Math.min(240, avail / (perSeg - 1)) : 0;
  const rows = Math.max(1, ...cols.map((c) => (c ? c.length : 0)));
  const segH = (rows - 1) * rowH + 64;
  const nSegs = Math.ceil(nCols / perSeg);
  const W = padX * 2 + (Math.min(nCols, perSeg) - 1) * colW;
  const H = padY + nSegs * segH + (nSegs - 1) * segGap + 20;
  const pos = new Map();
  cols.forEach((c, d) => (c || []).forEach((u, i) => {
    const seg = Math.floor(d / perSeg);
    pos.set(u, { x: padX + (d % perSeg) * colW,
                 y: padY + seg * (segH + segGap) + i * rowH + (rows - c.length) * rowH / 2, d, seg });
  }));

  const box = el('div', 'pdiag');
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
  edges.forEach((e) => {
    let a = pos.get(e.a), b = pos.get(e.b);
    if (a.d > b.d) [a, b] = [b, a];
    const u = e.id && usedBy.get(e.id);
    const cls = 'wire ' + (e.kind === 'prev' ? 't-prev' : u ? (u.use === 'both' ? 't-both' : u.use === 'forward' ? 't-fwd' : u.use === 'inferred' ? 't-inf' : 't-one') : 't-cand');
    let lx, ly;
    if (a.seg === b.seg) {
      s.appendChild(svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: cls, 'data-link-id': e.id || '' }));
      lx = (a.x + b.x) / 2; ly = (a.y + b.y) / 2 - 7;
    } else {
      // wrap: out to the right, down into the gap, back to the left, in
      const gapY = padY + (a.seg + 1) * (segH + segGap) - segGap / 2 - 8;
      const r = a.x + padX * 0.6, l = b.x - padX * 0.6;
      s.appendChild(svg('polyline', { points: `${a.x},${a.y} ${r},${a.y} ${r},${gapY} ${l},${gapY} ${l},${b.y} ${b.x},${b.y}`, class: cls, 'data-link-id': e.id || '' }));
      lx = (r + l) / 2; ly = gapY - 6;
    }
    if (u && u.packets != null) {
      s.appendChild(svg('text', { x: lx, y: ly, class: 'rate' + (u.use === 'forward' ? ' bad' : ''),
                                  'text-anchor': 'middle' }, u.use === 'inferred' ? 'no capture' : u.packets + ' pkts'));
    }
  });
  const prevOnly = new Set(prevPath.filter((x) => !onPath.has(x)));
  pos.forEach((p, u) => {
    const n = BY.get(u);
    if (!n) return;
    const cls = ['node'];
    if (!n.up || down.has(u)) cls.push('off');
    if (down.has(u)) cls.push('dead');
    if (res && !onPath.has(u) && !used.some((l) => l.a === u || l.b === u)) cls.push('t-idle');
    if (prevOnly.has(u)) cls.push('t-was');
    if (u === trace.source || u === dstLabel) cls.push('t-end');
    if (res && res.stop_at === u) cls.push('t-stop');
    if (n.config_status === 'undeclared') cls.push('undecl');
    const g = svg('g', { class: cls.join(' '), transform: `translate(${p.x} ${p.y})`, 'data-label': u });
    g.appendChild(svg('rect', { x: -23, y: -23, width: 46, height: 46, class: 'sel-box' }));
    g.appendChild(nodeIcon(n, 36));
    if (!n.ghost) {
      const c = n.state === 'BOOTED' ? 'var(--ok)' : n.state === 'STARTED' ? 'var(--warn)' : 'var(--devoff)';
      g.appendChild(svg('rect', { x: 13, y: 13, width: 9, height: 9, fill: c, class: 'lamp' }));
    }
    const end = u === trace.source ? 'FROM' : u === dstLabel ? 'TO' : null;
    if (end) g.appendChild(svg('text', { y: -30, class: 't-tag' }, end));
    g.appendChild(svg('text', { y: 34, class: 'lbl' }, u));
    const ip = displayIp(n).text;
    const note = prevOnly.has(u) ? (n.up ? 'not used now' : 'stopped') : ip;
    if (note) g.appendChild(svg('text', { y: 47, class: 'ipl' + (prevOnly.has(u) ? ' drift' : '') }, note));
    g.appendChild(svg('title', {}, `${u}\n${KIND_NAME[kind(n)]}, ${stateWord(n)[0]}` +
      (res && !onPath.has(u) ? '\nnot used this run' : '')));
    g.addEventListener('click', () => select(u));
    g.addEventListener('dblclick', () => { if (!n.ghost) openConsole(u); });
    g.addEventListener('contextmenu', (ev) => { ev.preventDefault(); select(u); nodeMenu(n, ev.clientX, ev.clientY); });
    s.appendChild(g);
  });
  box.appendChild(s);
  const key = el('div', 'tkey');
  [['t-both', 'used, both ways'], ['t-one', 'used, one way only'], ['t-fwd', 'requests only'],
   ['t-cand', 'possible, not used'], ['t-prev', 'previous run']].forEach(([c, t]) => {
    if (c === 't-prev' && !prevPath.length) return;
    const k = el('span');
    const ks = svg('svg', { width: 28, height: 8 });
    ks.appendChild(svg('line', { x1: 0, y1: 4, x2: 28, y2: 4, class: 'wire ' + c }));
    k.appendChild(ks); k.appendChild(document.createTextNode(t));
    key.appendChild(k);
  });
  box.appendChild(key);
  return box;
}

/* ------------------------------------------------- trace details (pane) -- */
function traceDetails() {
  const wrap = el('div');
  const res = resultFor();
  if (!res) {
    wrap.appendChild(el('div', 'pmsg', trace.source && trace.target
      ? 'No run yet for this pair. Press Run trace above the path.'
      : 'Choose From and To above the path, then Run trace.'));
    return wrap;
  }
  const cols = el('div', 'tcols');
  const used = res.links.filter((l) => l.use !== 'none');
  const t1 = gridTable(['Link', 'Packets', 'Direction', 'Shortest path']);
  used.sort((a, b) => res.path.indexOf(a.a) - res.path.indexOf(b.a)).forEach((l) => {
    const tr = el('tr', 'row');
    tr.appendChild(el('td', 'm', `${l.a} ${shortIf(l.ai)}  to  ${l.b} ${shortIf(l.bi)}`));
    tr.appendChild(el('td', 'm', l.packets == null ? '-' : String(l.packets)));
    tr.appendChild(el('td', l.use === 'both' ? 's-ok' : 's-warn',
      { both: 'both ways', 'one-way': 'one way only', forward: 'requests only', inferred: 'not captured (inferred)' }[l.use]));
    tr.appendChild(el('td', l.candidate ? null : 's-warn', l.candidate ? 'yes' : 'no'));
    t1.appendChild(tr);
  });
  const a = el('div');
  a.appendChild(el('div', 'tsect-h', 'Links the packets crossed (CML link counters)'));
  a.appendChild(t1);
  cols.appendChild(a);
  const t2 = gridTable(['Hop', 'Address', 'Node', 'RTT']);
  res.trace.hops.forEach((h) => {
    const tr = el('tr', 'row');
    [String(h.ttl), h.ip || '*', h.node || (h.ip ? 'unknown' : ''), h.rtt_ms != null ? h.rtt_ms + ' ms' : ''].forEach((v, i) =>
      tr.appendChild(el('td', i === 1 ? 'm' : i === 2 && !h.node && h.ip ? 's-warn' : null, v)));
    t2.appendChild(tr);
  });
  const b = el('div');
  b.appendChild(el('div', 'tsect-h', 'Traceroute from ' + res.source));
  b.appendChild(t2);
  if (!res.trace.hops.length) b.appendChild(el('div', 'pmsg', 'Traceroute returned no hops.'));
  cols.appendChild(b);
  wrap.appendChild(cols);

  if (res.dns) {
    const d = res.dns;
    const dsec = el('div');
    dsec.appendChild(el('div', 'tsect-h', 'DNS'));
    dsec.appendChild(props([
      ['Name', d.fqdn || '-'],
      ['Asked', `${d.resolver || '-'} from ${d.vantage || '-'}${d.vantage && d.vantage !== res.source ? ' (the source has no resolver of its own)' : ''}`],
      ['Answer', d.ok ? colored(d.addresses.join(', ') + (d.matches ? '  (matches)' : '  (does not match ' + res.target_ip + ')'), d.matches ? 's-ok' : 's-bad')
        : colored(d.reason || 'no answer', d.offnet ? 's-warn' : 's-bad')],
    ]));
    wrap.appendChild(dsec);
  }
  const hist = trace.history.filter((h) => h.source === trace.source && askedTarget(h) === trace.target);
  const t3 = gridTable(['Time', 'Result', 'Path']);
  [...hist].reverse().forEach((h) => {
    const tr = el('tr', 'row' + (h === res ? ' sel' : ''));
    tr.appendChild(el('td', null, new Date(h.at).toLocaleTimeString()));
    tr.appendChild(el('td', h.verdict.level === 'ok' ? 's-ok' : h.verdict.level === 'fail' ? 's-bad' : 's-warn',
      h.verdict.level === 'ok' ? 'reached' : h.verdict.level === 'fail' ? 'failed' : 'check'));
    tr.appendChild(el('td', 'wrap' + (h.changed ? ' s-warn' : ''), h.path.join(' > ') + (h.changed ? '   (changed)' : '')));
    tr.addEventListener('click', () => { trace.result = h; renderPathBody(); renderPaneBody(); });
    t3.appendChild(tr);
  });
  const hs = el('div');
  hs.appendChild(el('div', 'tsect-h', 'Runs this session (click one to show it)'));
  hs.appendChild(t3);
  wrap.appendChild(hs);
  return wrap;
}

/* ================================================================ menus == */
let openMenu = null;

function menuItems(which) {
  const toggle = (label, on, fn, key) => ({ label, on, fn, key });
  if (which === 'lab') return [
    { label: 'Read running addresses from consoles', fn: () => LAB.harvest.run() },
    { label: 'Run baseline matrix', fn: () => { LAB.matrix.run(); showPane('matrix'); } },
    { label: 'Run DNS audit', fn: () => { LAB.dns.run(); showPane('dns'); } },
    '-',
    { label: 'Pick From and To in the node list', key: 'T', fn: () => startPick() },
    '-',
    { label: 'Refresh from CML now', key: 'R', fn: () => poll(true) },
    { label: 'Export inventory as CSV', fn: () => { location.href = '/api/export.csv'; } },
  ];
  if (which === 'view') return [
    { head: 'Theme' },
    toggle('Follow system', !prefs.theme, () => setTheme(null)),
    toggle('Light', prefs.theme === 'light', () => setTheme('light')),
    toggle('Dark', prefs.theme === 'dark', () => setTheme('dark')),
    '-',
    toggle('Pause auto-refresh', paused, togglePause),
  ];
  return [
    { label: 'Trace details', fn: () => showPane('trace') },
    { label: 'Baseline matrix', fn: () => showPane('matrix') },
    { label: 'DNS audit', fn: () => showPane('dns') },
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
    { label: 'Interfaces', fn: () => { sideTab = 'interfaces'; select(n.label); } },
    '-',
    { label: 'Copy address', disabled: !ip, fn: () => navigator.clipboard.writeText(ip) },
    { label: 'Copy DNS name', disabled: !fqdn, fn: () => navigator.clipboard.writeText(fqdn) },
    '-',
    { label: 'Trace from here', disabled: !n.can_source_probe || !n.up,
      fn: () => setTraceEnds(n.label, trace.target === n.label ? null : trace.target) },
    { label: 'Trace to here', disabled: n.ghost || trace.source === n.label,
      fn: () => setTraceEnds(trace.source, n.label) },
  ], box);
  box.hidden = false;
  const r = box.getBoundingClientRect();
  box.style.left = Math.min(x, innerWidth - r.width - 4) + 'px';
  box.style.top = Math.min(y, innerHeight - r.height - 4) + 'px';
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
  $('#pause').addEventListener('click', togglePause);

  $('#nav-toggle').addEventListener('click', () => document.body.classList.toggle('nav-open'));
  $('#refresh-btn').addEventListener('click', () => poll(true));

  document.querySelectorAll('.menu').forEach((m) => {
    m.firstElementChild.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(m); });
    m.addEventListener('mouseenter', () => { if (openMenu && openMenu !== m) toggleMenu(m); });
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.dropdown, .ctxmenu')) closeMenus(); });
  window.addEventListener('blur', closeMenus);

  wireSplitter();
  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(renderPathBody, 150); });
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
  });
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
