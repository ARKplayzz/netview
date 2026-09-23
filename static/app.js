/* NetView client — one column, rows expand in place.
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

let INV = null, NODES = [], open = null, countdown = REFRESH_S, paused = false;
let site = localStorage.getItem(SITE_KEY) || 'all';   // all | HQ | DR | other
const hidden = new Set(JSON.parse(localStorage.getItem(HIDE_KEY) || '[]'));
const probeHistory = new Map();

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ---------------------------------------------------------------- data --- */
async function boot() {
  wire();
  const r = await fetch('/api/inventory');
  INV = await r.json();
  NODES = INV.nodes.concat(INV.ghosts.map((g) => Object.assign({
    ghost: true, up: false, dns: {}, links: [], vlans: [], addresses: [],
    ha: {}, issues: [], interfaces: [], probe_family: 'none',
    can_source_probe: false, addressing: 'unaddressed',
  }, g)));
  banners();
  render();
  setInterval(tickSecond, 1000);
}

async function poll() {
  try {
    const j = await (await fetch('/api/state')).json();
    if (!j.states) return;
    NODES.forEach((n) => {
      if (n.ghost) return;
      if (j.states[n.label] !== undefined) n.state = j.states[n.label];
      n.up = n.state === 'STARTED' || n.state === 'BOOTED';
    });
    render();
  } catch (e) { /* one missed poll is not worth an error state */ }
}

function tickSecond() {
  if (paused) return;
  countdown -= 1;
  if (countdown <= 0) { countdown = REFRESH_S; poll(); }
  $('#tick').textContent = countdown + 's';
}

/* -------------------------------------------------------------- banners -- */
function banners() {
  const box = $('#banners');
  box.innerHTML = '';
  const m = INV.meta;
  if (m.topology_source !== 'live') {
    const age = m.snapshot && m.snapshot.age_hours;
    box.appendChild(el('div', 'banner fail',
      `Showing the cached snapshot (${age != null ? age + 'h old' : 'age unknown'}) — ` +
      `CML is unreachable. Anything changed in the lab since then is missing here.`));
  }
  if (m.counts.undeclared) {
    box.appendChild(el('div', 'banner warn',
      `${m.counts.undeclared} node(s) are in the lab but not in the manifest — ` +
      `they get no generated config at all. Marked "undeclared" below.`));
  }
}

/* --------------------------------------------------------------- filter -- */
/* "Other" = anything that is not HQ or DR: the carriers, the internet web
 * server, and the remote worker's house (site HOME).  A shared carrier has no
 * single site by design — see the manifest's note on the ISP nodes. */
function siteBucket(n) {
  return n.site === 'HQ' || n.site === 'DR' ? n.site : 'other';
}

function visible() {
  const q = $('#q').value.trim().toLowerCase();
  return NODES.filter((n) => {
    if (site !== 'all' && siteBucket(n) !== site) return false;
    if (hidden.has(n.label)) return false;
    if ($('#hide-down').checked && !n.up && !n.ghost) return false;
    if (!q) return true;
    return hay(n).includes(q);
  });
}

function hay(n) {
  const p = [n.label, n.role, n.role_title, n.site, n.layer_title, n.state,
             n.node_definition, n.primary_ip, (n.dns || {}).fqdn];
  (n.addresses || []).forEach((a) => p.push(a.value));
  (n.vlans || []).forEach((v) => p.push('vlan' + v.id, v.name));
  return p.filter(Boolean).join(' ').toLowerCase();
}

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

/* --------------------------------------------------------------- render -- */
function render() {
  const vis = visible();
  // Counts follow the site selection, so "3 down" means 3 down HERE.
  const scope = NODES.filter((n) => !n.ghost && (site === 'all' || siteBucket(n) === site));
  const up = scope.filter((n) => n.up).length;
  $('#counts').textContent =
    (site === 'all' ? '' : site + ': ') +
    `${up} up · ${scope.length - up} down · ${vis.length} shown`;
  $('#src-line').textContent =
    (INV.meta.topology_source === 'live' ? 'live from CML' : 'CACHED SNAPSHOT') +
    ' · p45.local / ' + INV.public_zone;

  const hb = $('#hidden-btn');
  hb.hidden = hidden.size === 0;
  hb.textContent = `${hidden.size} hidden — show`;

  const box = $('#groups');
  box.innerHTML = '';
  if (!vis.length) {
    box.appendChild(el('div', 'empty', 'Nothing matches.'));
    return;
  }
  groups(vis.sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true })))
    .forEach((g) => {
      const sec = el('section', 'group');
      const h = el('h2', null, g.title);
      const gup = g.nodes.filter((n) => n.up).length;
      h.appendChild(el('span', 'n', `${gup}/${g.nodes.length} up`));
      sec.appendChild(h);
      g.nodes.forEach((n) => sec.appendChild(row(n)));
      box.appendChild(sec);
    });
}

function row(n) {
  const r = el('div', 'row' + (n.up ? '' : ' down') + (n.label === open ? ' open' : ''));
  const head = el('div', 'row-head');
  head.appendChild(el('span', 'dot ' + (n.ghost ? 'warn' : n.up ? 'up' : '')));

  const nameBox = el('div');
  nameBox.appendChild(el('div', 'name', n.label));
  nameBox.appendChild(el('div', 'ip', n.role_title || ''));
  head.appendChild(nameBox);

  // Live address (read off the device) outranks the manifest's declaration.
  const ipTxt = n.live_ip
    ? n.live_ip + (n.live_ip === n.primary_ip ? '' : ' ⚠')
    : (n.primary_ip ? n.primary_ip + ' (manifest)' : '—');
  head.appendChild(el('span', 'ip', ipTxt));
  head.appendChild(el('span', 'dns', (n.dns || {}).fqdn || '—'));

  const tags = el('div', 'tags');
  if (n.ghost) tags.appendChild(el('span', 'tag fail', 'absent'));
  if (n.config_status === 'undeclared') tags.appendChild(el('span', 'tag warn', 'undeclared'));
  (n.issues || []).forEach((i) => tags.appendChild(el('span', 'tag warn', '#' + i.id)));
  if (n.site) tags.appendChild(el('span', 'tag', n.site));
  const hide = el('button', 'hide-btn', 'hide');
  hide.addEventListener('click', (e) => {
    e.stopPropagation();
    hidden.add(n.label);
    localStorage.setItem(HIDE_KEY, JSON.stringify([...hidden]));
    if (open === n.label) open = null;
    render();
  });
  tags.appendChild(hide);
  head.appendChild(tags);

  head.addEventListener('click', () => { open = open === n.label ? null : n.label; render(); });
  r.appendChild(head);
  if (n.label === open) r.appendChild(detail(n));
  return r;
}

/* --------------------------------------------------------------- detail -- */
function table(rows) {
  const t = el('table', 't');
  rows.forEach(([k, v]) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, k));
    const td = el('td', 'm');
    if (v && v.nodeType) td.appendChild(v); else td.textContent = v == null || v === '' ? '—' : v;
    tr.appendChild(td);
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

function detail(n) {
  const d = el('div', 'detail');

  // ---- live addresses first: what the device itself reported ----
  const live = n.live;
  if (live && (live.addresses || []).length) {
    d.appendChild(el('h3', null, 'Live addresses — read from the device'));
    const t = el('table', 't');
    const hr = el('tr');
    ['Interface', 'Address', 'Method', 'State'].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    const manifestIps = new Set((n.addresses || []).map((a) => a.value));
    live.addresses.forEach((a) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'm', a.iface));
      const td = el('td', 'm'); td.appendChild(copyable(a.ip)); tr.appendChild(td);
      tr.appendChild(el('td', 'm', a.method || '—'));
      tr.appendChild(el('td', 'm', a.status + '/' + a.proto));
      if (!manifestIps.has(a.ip)) tr.style.background = 'var(--warnbg)';
      t.appendChild(tr);
    });
    d.appendChild(t);
    // drift both ways: device holds what the plan doesn't, plan claims what
    // the device doesn't hold
    const liveIps = new Set(live.addresses.map((a) => a.ip));
    const extra = live.addresses.filter((a) => !manifestIps.has(a.ip));
    const missing = (n.addresses || []).filter((a) => !liveIps.has(a.value)
      && !['ha'].includes(a.kind));
    if (extra.length || missing.length) {
      const bits = [];
      if (extra.length) bits.push('on the device but not in the manifest: ' +
        extra.map((a) => `${a.ip} (${a.iface})`).join(', '));
      if (missing.length) bits.push('in the manifest but not on the device: ' +
        missing.map((a) => `${a.value} (${a.label})`).join(', '));
      d.appendChild(el('div', 'note warn', bits.join(' · ')));
    }
    d.appendChild(el('div', 'note', 'Read at ' + live.at + '. Highlighted rows are addresses the manifest does not declare (DHCP leases, transit /30s, or drift).'));
  } else if (live && live.error) {
    d.appendChild(el('h3', null, 'Live addresses'));
    d.appendChild(el('div', 'note warn', 'Read failed at ' + live.at + ': ' + live.error));
  }

  d.appendChild(el('h3', null, live ? 'Manifest (declared) & DNS' : 'Addresses & DNS — from the manifest (run "Read addresses" for live data)'));
  const rows = [];
  (n.addresses || []).forEach((a) => rows.push([a.label, copyable(a.value)]));
  const dns = n.dns || {};
  if (dns.fqdn) rows.push(['Internal name', copyable(dns.fqdn)]);
  (dns.cnames || []).forEach((c) => rows.push(['Alias', copyable(c)]));
  (dns.public || []).forEach((p) => rows.push(['Public record', copyable(p.name + ' → ' + p.ip)]));
  if (n.resolver) rows.push(['Resolver', copyable(n.resolver)]);
  if (n.gateway) rows.push(['Gateway', copyable(n.gateway)]);
  d.appendChild(table(rows.length ? rows : [['(none declared)', '']]));

  d.appendChild(el('h3', null, 'Detail'));
  const rows2 = [
    ['State', n.ghost ? 'absent from the lab' : n.state || 'unknown'],
    ['Config', { managed: 'manifest + ' + (n.template || ''), undeclared: 'NOT in the manifest',
                 absent: 'manifest entry, no node', 'no-template': 'declared, no template' }[n.config_status] || '—'],
  ];
  (n.vlans || []).forEach((v) => rows2.push(['VLAN', `${v.id}${v.name ? ' — ' + v.name : ''}`]));
  Object.entries(n.ha || {}).forEach(([k, v]) => rows2.push([k.replace(/_/g, ' '), String(v)]));
  if (n.floor) rows2.push(['Location', n.floor]);
  (n.issues || []).forEach((i) => rows2.push(['Open issue', `#${i.id} — ${i.title}`]));
  d.appendChild(table(rows2));

  if (n.dhcp) d.appendChild(dhcpSection(n));

  if ((n.links || []).length) {
    d.appendChild(el('h3', null, 'Cabling'));
    const t = el('table', 't');
    const hr = el('tr');
    ['Local', 'Peer', 'Subnet', 'Traffic', 'Drops'].forEach((h) => hr.appendChild(el('th', null, h)));
    t.appendChild(hr);
    n.links.forEach((l) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'm', l.local_iface));
      tr.appendChild(el('td', 'm', l.peer + ' ' + l.peer_iface));
      tr.appendChild(el('td', 'm', l.subnet || '—'));
      const traffic = el('td', 'm', '…'); traffic.dataset.link = l.link_id || '';
      const drops = el('td', 'm', '…'); drops.dataset.linkDrops = l.link_id || '';
      tr.appendChild(traffic); tr.appendChild(drops);
      t.appendChild(tr);
    });
    d.appendChild(t);
    fillLinkStats(t);   // async — 0.04s REST read, two samples for pkts/s
  }

  if (!n.ghost) d.appendChild(consoleLogSection(n));
  if (!n.ghost) d.appendChild(probeSection(n));
  return d;
}

/* Two samples of the lab-wide link counters, 2s apart, give live pkts/s and a
 * cumulative drop count per link — read over REST with no console attach. */
async function fillLinkStats(tableEl) {
  try {
    const a = await (await fetch('/api/simstats')).json();
    if (a.error) throw a.error;
    await new Promise((ok) => setTimeout(ok, 2000));
    const b = await (await fetch('/api/simstats')).json();
    if (!tableEl.isConnected) return;
    tableEl.querySelectorAll('[data-link]').forEach((td) => {
      const id = td.dataset.link;
      const s0 = a.links[id], s1 = b.links[id];
      if (!s0 || !s1) { td.textContent = '—'; return; }
      const pps = ((s1.readpackets + s1.writepackets) - (s0.readpackets + s0.writepackets)) / 2;
      td.textContent = pps > 0 ? pps.toFixed(0) + ' pkt/s' : 'quiet';
    });
    tableEl.querySelectorAll('[data-link-drops]').forEach((td) => {
      const s = b.links[td.dataset.linkDrops];
      td.textContent = s ? String(s.drops) : '—';
      if (s && s.drops > 0) td.style.color = 'var(--fail)';
    });
  } catch (e) {
    tableEl.querySelectorAll('[data-link],[data-link-drops]').forEach((td) => { td.textContent = '—'; });
  }
}

/* Console scrollback with no attach.  For a container, line 0 is the service's
 * own log — dnsmasq queries, RADIUS rejections — which is direct evidence of
 * what the service did, at ~0.03s instead of a 6s console attach. */
function consoleLogSection(n) {
  const box = el('div');
  box.appendChild(el('h3', null, 'Console log'));
  const btn = el('button', null,
    n.console_line === 1 ? 'Read service log (no attach)' : 'Read console scrollback');
  const out = el('div');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const j = await (await fetch('/api/console-log/' + encodeURIComponent(n.label) + '?lines=60')).json();
      out.innerHTML = '';
      if (j.error) out.appendChild(el('div', 'note warn', j.error));
      else {
        const pre = el('div', 'note mono');
        pre.style.whiteSpace = 'pre-wrap';
        pre.style.maxHeight = '260px';
        pre.style.overflowY = 'auto';
        pre.textContent = (j.log || '').trim() || '(empty)';
        out.appendChild(pre);
      }
    } catch (e) { out.appendChild(el('div', 'note fail', String(e))); }
    btn.disabled = false;
  });
  box.appendChild(btn);
  box.appendChild(out);
  return box;
}

/* ----------------------------------------------------------------- dhcp -- */
function dhcpSection(n) {
  const box = el('div');
  box.appendChild(el('h3', null, 'DHCP lease'));
  box.appendChild(table([
    [n.dhcp.kind === 'client' ? 'Leases on' : 'Probe SVI',
     `${n.dhcp.interface} (VLAN ${n.dhcp.vlan}${n.dhcp.vlan_name ? ' ' + n.dhcp.vlan_name : ''})`],
    ['Scope', n.dhcp.scope],
    ['Server / length', `${n.dhcp.expected_server} · ${n.dhcp.expected_lease_secs / 3600}h`],
  ]));

  if (n.probe_family === 'ios' && n.up) {
    const b = el('button', null, 'Read live lease (~15s)');
    const out = el('div');
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Reading…';
      try {
        const j = await (await fetch('/api/dhcp/' + encodeURIComponent(n.label))).json();
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

/* ---------------------------------------------------------------- probe -- */
function probeSection(n) {
  const box = el('div');
  box.appendChild(el('h3', null, 'Ping this node from…'));

  const sources = NODES.filter((x) => x.can_source_probe && x.up && x.label !== n.label)
    .sort((a, b) => a.label.localeCompare(b.label));
  if (!sources.length) {
    box.appendChild(el('div', 'note warn', 'No running node can source a probe right now.'));
    return box;
  }

  const form = el('div', 'probe');
  const src = el('select');
  sources.forEach((x) => {
    const o = el('option', null, x.label + (x.site ? ' (' + x.site + ')' : ''));
    o.value = x.label;
    src.appendChild(o);
  });
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
  dnsToggle.appendChild(el('span', null, ' + DNS'));
  form.appendChild(dnsToggle);

  const go = el('button', 'dark', 'Ping');
  form.appendChild(go);
  box.appendChild(form);

  const out = el('div');
  box.appendChild(out);
  (probeHistory.get(n.label) || []).forEach((r) => out.appendChild(resultBlock(r)));

  go.addEventListener('click', async () => {
    go.disabled = true;
    const status = el('div', 'note', 'Queuing…');
    const bar = el('div', 'progress'); const fill = el('i'); bar.appendChild(fill);
    out.prepend(bar); out.prepend(status);
    try {
      const methods = cDns.checked ? ['ip', 'dns'] : ['ip'];
      const r = await fetch('/api/probe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: src.value, target: n.label, methods, count: 5, target_ip: dst.value || null }),
      });
      const j = await r.json();
      if (!r.ok) { status.className = 'note fail'; status.textContent = j.error; bar.remove(); go.disabled = false; return; }
      const eta = j.eta_seconds || 25;
      let t = 0;
      const iv = setInterval(() => { t++; fill.style.width = Math.min(97, t / eta * 100) + '%'; }, 1000);
      let res;
      for (;;) {
        await new Promise((ok) => setTimeout(ok, 1500));
        res = await (await fetch('/api/probe/' + j.job_id)).json();
        status.textContent = (res.stage || '') + '…';
        if (res.status === 'done' || res.status === 'error') break;
      }
      clearInterval(iv);
      status.remove(); bar.remove();
      if (res.status === 'error') out.prepend(el('div', 'note fail', 'Probe failed: ' + res.error));
      else {
        const hist = probeHistory.get(n.label) || [];
        hist.unshift(res.result);
        probeHistory.set(n.label, hist.slice(0, 5));
        out.prepend(resultBlock(res.result));
      }
    } catch (e) {
      status.className = 'note fail'; status.textContent = String(e);
    }
    go.disabled = false;
  });
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

/* --------------------------------------------------------------- matrix -- */
let matrixPoll = null;

async function toggleMatrix() {
  const box = $('#matrix');
  if (!box.hidden) { box.hidden = true; clearInterval(matrixPoll); matrixPoll = null; return; }
  box.hidden = false;
  await drawMatrix();
}

async function drawMatrix() {
  const box = $('#matrix');
  const st = await (await fetch('/api/matrix')).json();
  box.innerHTML = '';

  const head = el('div', 'mx-head');
  head.appendChild(el('h2', null, 'Baseline reachability'));
  const runBtn = el('button', 'dark',
    st.status === 'running' ? `Running ${st.done_count}/${st.total}…` : 'Run all ' + st.total);
  runBtn.disabled = st.status === 'running';
  runBtn.addEventListener('click', async () => {
    const r = await fetch('/api/matrix/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const j = await r.json();
    if (j.eta_seconds) head.appendChild(el('span', 'lbl', ` ~${Math.round(j.eta_seconds / 60)} min`));
    if (!matrixPoll) matrixPoll = setInterval(drawMatrix, 4000);
    drawMatrix();
  });
  head.appendChild(runBtn);
  if (st.status === 'done') head.appendChild(el('span', 'lbl', 'finished ' + st.ended));
  if (st.status === 'error') head.appendChild(el('span', 'tag fail', st.error));
  box.appendChild(head);

  box.appendChild(el('div', 'note',
    'The curated baseline from probe_matrix.py — every path a disaster scenario relies on. ' +
    'Two paths are DECLARED must-fails (the perimeter denying an internet client): those ' +
    'passing would be a leak, which is the more serious event.'));

  if (st.status === 'running' && !matrixPoll) matrixPoll = setInterval(drawMatrix, 4000);
  if (st.status !== 'running' && matrixPoll) { clearInterval(matrixPoll); matrixPoll = null; }

  const done = new Map(st.rows.map((r) => [r.source + '>' + r.dest, r]));
  const t = el('table', 't');
  const hr = el('tr');
  ['', 'Source', 'Destination', 'What it proves', 'Loss', 'RTT'].forEach((h) => hr.appendChild(el('th', null, h)));
  t.appendChild(hr);
  st.probes.forEach((p) => {
    const r = done.get(p.source + '>' + p.dest);
    const tr = el('tr');
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
    t.appendChild(tr);
  });
  box.appendChild(t);
}

/* ------------------------------------------------------------- harvest --- */
// Read live addresses off every reachable device's console, so the whole page
// (row IPs, ping targets, cabling) can prefer device truth over manifest
// intent. Same run/poll shape as the matrix and DNS audit.
let harvestPoll = null;

async function toggleHarvest() {
  const box = $('#harvest');
  if (!box.hidden) { box.hidden = true; clearInterval(harvestPoll); harvestPoll = null; return; }
  box.hidden = false;
  await drawHarvest();
}

async function drawHarvest() {
  const box = $('#harvest');
  const st = await (await fetch('/api/harvest')).json();
  box.innerHTML = '';

  const head = el('div', 'mx-head');
  head.appendChild(el('h2', null, 'Live addresses'));
  const btn = el('button', 'dark',
    st.status === 'running' ? `Reading ${st.done_count}/${st.total}…` : 'Read every device');
  btn.disabled = st.status === 'running';
  btn.addEventListener('click', async () => {
    const r = await fetch('/api/harvest/run', { method: 'POST' });
    const j = await r.json();
    if (j.error) { box.prepend(el('div', 'note fail', j.error)); return; }
    if (j.eta_seconds) head.appendChild(el('span', 'lbl',
      ` ~${j.eta_seconds}${j.eta_seconds_max ? '–' + j.eta_seconds_max : ''}s`));
    if (!harvestPoll) harvestPoll = setInterval(async () => {
      await drawHarvest();
      applyHarvestToNodes();   // keep row IPs and ping targets live too
    }, 4000);
    drawHarvest();
  });
  head.appendChild(btn);
  if (st.status === 'error') head.appendChild(el('span', 'tag fail', st.error));
  box.appendChild(head);

  box.appendChild(el('div', 'note',
    'Reads addresses straight off each device — `show ip interface brief` (IOS), ' +
    '`show interface ip brief` (ASA), `ip -4 -o addr show` (Linux, containers on ' +
    'console line 1) — instead of trusting the manifest. Runs across a pool of ' +
    'parallel console sessions (docs/cml-integration-review.md). Highlighted rows ' +
    'are addresses the manifest does not declare.'));

  if (st.status === 'running' && !harvestPoll) harvestPoll = setInterval(async () => {
    await drawHarvest();
    applyHarvestToNodes();
  }, 4000);
  if (st.status !== 'running' && harvestPoll) { clearInterval(harvestPoll); harvestPoll = null; applyHarvestToNodes(); }

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
    const tr = el('tr');
    const td0 = el('td');
    td0.appendChild(n.live.error ? el('span', 'tag fail', 'read failed')
      : n.live_ip === n.primary_ip ? el('span', 'tag ok', 'match')
      : el('span', 'tag warn', 'DRIFT'));
    tr.appendChild(td0);
    tr.appendChild(el('td', 'm', n.label));
    tr.appendChild(el('td', 'm', n.live.error || n.live_ip || '(no up interface with an address)'));
    tr.appendChild(el('td', 'm', n.primary_ip || '—'));
    t.appendChild(tr);
  });
  box.appendChild(t);
}

/* Merge freshly-harvested per-node results into the in-memory NODES so open
 * rows/drawers reflect live data without a full inventory reload. */
async function applyHarvestToNodes() {
  const st = await (await fetch('/api/harvest')).json();
  if (!st.nodes) return;
  NODES.forEach((n) => {
    const rec = st.nodes[n.label];
    if (!rec) return;
    n.live = rec;
    const upAddrs = (rec.addresses || []).filter((a) => a.proto === 'up');
    const manifestIps = new Set((n.addresses || []).map((a) => a.value));
    const onManifest = upAddrs.find((a) => manifestIps.has(a.ip));
    n.live_ip = onManifest ? onManifest.ip : (upAddrs[0] ? upAddrs[0].ip : null);
  });
  render();   // render() rebuilds the open row's detail() too — no separate call needed
}

/* ------------------------------------------------------------ dns audit -- */
let dnsPoll = null;

async function toggleDnsAudit() {
  const box = $('#dnsaudit');
  if (!box.hidden) { box.hidden = true; clearInterval(dnsPoll); dnsPoll = null; return; }
  box.hidden = false;
  await drawDnsAudit();
}

async function drawDnsAudit() {
  const box = $('#dnsaudit');
  const st = await (await fetch('/api/dnsaudit')).json();
  box.innerHTML = '';

  const head = el('div', 'mx-head');
  head.appendChild(el('h2', null, 'DNS audit'));
  const btn = el('button', 'dark',
    st.status === 'running' ? `Auditing ${st.done_count}/${st.total}…` : 'Check every name');
  btn.disabled = st.status === 'running';
  btn.addEventListener('click', async () => {
    const r = await fetch('/api/dnsaudit/run', { method: 'POST' });
    const j = await r.json();
    if (j.error) { box.prepend(el('div', 'note fail', j.error)); return; }
    if (!dnsPoll) dnsPoll = setInterval(drawDnsAudit, 4000);
    drawDnsAudit();
  });
  head.appendChild(btn);
  if (st.status === 'error') head.appendChild(el('span', 'tag fail', st.error));
  box.appendChild(head);

  box.appendChild(el('div', 'note',
    'Asks each site’s resolver for every internal name, from a desktop at that ' +
    'site, and compares the answer to the manifest. A mismatch is a resolver ' +
    'confidently serving a stale address — the failure no ping test can see.'));

  if (st.status === 'running' && !dnsPoll) dnsPoll = setInterval(drawDnsAudit, 4000);
  if (st.status !== 'running' && dnsPoll) { clearInterval(dnsPoll); dnsPoll = null; }

  if (!st.rows.length) return;
  const bad = st.rows.filter((r) => !r.match);
  box.appendChild(el('div', 'note ' + (bad.length ? 'warn' : 'ok'),
    st.status === 'done'
      ? (bad.length ? `${bad.length} of ${st.rows.length} names do NOT match the manifest.`
                    : `All ${st.rows.length} names resolve to exactly what the manifest says.`)
      : `${st.rows.length} checked so far, ${bad.length} problem(s).`));

  const t = el('table', 't');
  const hr = el('tr');
  ['', 'Name', 'Manifest', 'Resolver answered'].forEach((h) => hr.appendChild(el('th', null, h)));
  t.appendChild(hr);
  // Problems first, then a compact confirmation of the rest.
  bad.concat(st.rows.filter((r) => r.match)).forEach((r) => {
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
}

/* ------------------------------------------------------------- controls -- */
function wire() {
  document.querySelectorAll('#site-seg button').forEach((b) => {
    b.classList.toggle('on', b.dataset.site === site);
    b.addEventListener('click', () => {
      site = b.dataset.site;
      localStorage.setItem(SITE_KEY, site);
      document.querySelectorAll('#site-seg button').forEach((x) =>
        x.classList.toggle('on', x.dataset.site === site));
      render();
    });
  });
  $('#q').addEventListener('input', render);
  $('#groupby').addEventListener('change', render);
  $('#hide-down').addEventListener('change', render);
  $('#hidden-btn').addEventListener('click', () => {
    hidden.clear();
    localStorage.setItem(HIDE_KEY, '[]');
    render();
  });
  $('#pause').addEventListener('click', () => {
    paused = !paused;
    $('#pause').textContent = paused ? 'Resume' : 'Pause';
    $('#tick').textContent = paused ? '⏸' : countdown + 's';
  });
  $('#harvest-btn').addEventListener('click', toggleHarvest);
  $('#matrix-btn').addEventListener('click', toggleMatrix);
  $('#dns-btn').addEventListener('click', toggleDnsAudit);
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== $('#q')) { e.preventDefault(); $('#q').focus(); }
    if (e.key === 'Escape') { open = null; render(); }
  });
}

boot();
