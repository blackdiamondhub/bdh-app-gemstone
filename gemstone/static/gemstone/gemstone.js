/* Gemstone Lights — hub UI. Plain JS, no build step. */
(function () {
  'use strict';
  const root = document.getElementById('gs');
  if (!root) return;
  const D = root.dataset;
  const csrf = (root.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  let S = JSON.parse($('gs-initial').textContent || '{}');

  // ------------------------------------------------------------ helpers
  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function sv(tag, attrs) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    return e;
  }
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const mod = (a, n) => ((a % n) + n) % n;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  function url(name, cid) {
    const u = D[name + 'Url'];
    return cid == null ? u : u.replace('CID', encodeURIComponent(cid));
  }
  async function api(u, body) {
    const init = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf, 'Accept': 'application/json' } };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(u, init); } catch (e) { throw new Error("Can't reach the hub"); }
    let j = {};
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(j.error || ('Something went wrong (' + r.status + ')'));
    return j;
  }
  function fmtTime(d) {
    if (!(d instanceof Date)) d = new Date(d);
    let hh = d.getHours(); const mm = d.getMinutes();
    if (hh === 0 && mm === 0) return 'Midnight';
    const ap = hh >= 12 ? 'PM' : 'AM';
    hh = hh % 12 || 12;
    return mm ? hh + ':' + String(mm).padStart(2, '0') + ' ' + ap : hh + ' ' + ap;
  }
  const hhmm = (d) => String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  const pretty = (name) => name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

  // ------------------------------------------------------------ toast + popover
  let toastTimer = null;
  function toast(parts, undoFn, err) {
    const t = $('gs-toast'), tx = $('gs-toast-text'), u = $('gs-toast-undo');
    tx.textContent = '';
    (Array.isArray(parts) ? parts : [parts]).forEach((p) => {
      if (p && typeof p === 'object') tx.appendChild(h('b', '', p.b));
      else tx.appendChild(document.createTextNode(String(p)));
    });
    t.classList.toggle('err', !!err);
    t.classList.toggle('has-u', !!undoFn);
    u.hidden = !undoFn;
    u.onclick = async () => { t.hidden = true; if (undoFn) await undoFn(); };
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, undoFn ? 8000 : 4000);
  }
  const fail = (e) => toast(e.message || String(e), null, true);

  const pop = $('gs-pop');
  function openPop(anchor, build) {
    pop.textContent = '';
    build(pop);
    pop.hidden = false;
    const r = anchor.getBoundingClientRect(), R = root.getBoundingClientRect();
    const w = pop.offsetWidth, ph = pop.offsetHeight;
    let left = clamp(r.right - R.left - w, 10, R.width - w - 10);
    let top = r.bottom - R.top + 10;
    if (top + ph > R.height - 10) top = Math.max(10, r.top - R.top - ph - 10);
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }
  const closePop = () => { pop.hidden = true; };
  document.addEventListener('pointerdown', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !e.target.closest('[data-pop]')) closePop();
  }, true);
  function popItem(text, cls, onClick, small) {
    const b = h('button', 'it' + (cls ? ' ' + cls : ''), text);
    if (small) b.appendChild(h('small', '', small));
    if (onClick) b.onclick = onClick;
    return b;
  }

  // ------------------------------------------------------------ controllers + selection
  const ctrls = () => S.controllers || [];
  const ctrlById = (id) => ctrls().find((c) => c.id === id);
  let sel = [];
  try { sel = JSON.parse(localStorage.getItem('gs-sel') || '[]') || []; } catch (e) { sel = []; }
  function fixSel() {
    const ids = ctrls().map((c) => c.id);
    sel = sel.filter((i) => ids.includes(i));
    if (!sel.length) sel = ids.slice();
  }
  const selCtrls = () => sel.map(ctrlById).filter(Boolean);
  const namesOf = (list) => list.map((c) => c.name).join(' + ');
  function saveSel() { try { localStorage.setItem('gs-sel', JSON.stringify(sel)); } catch (e) { /* private mode */ } }

  function thumb(c) {
    const t = h('span', 'th');
    if (c.layout && c.layout.photo) t.style.backgroundImage = 'url("' + photoUrl(c) + '")';
    else t.innerHTML = '<svg viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid slice">' + HOUSE + '</svg>';
    return t;
  }
  function renderChips() {
    root.querySelectorAll('[data-chips]').forEach((box) => {
      box.textContent = '';
      box.hidden = ctrls().length < 2;
      ctrls().forEach((c) => {
        const on = sel.includes(c.id);
        const b = h('button', 'chip-c' + (on ? ' on' : '') + (c.online ? '' : ' off'));
        b.appendChild(h('span', 'ck', '✓'));
        b.appendChild(thumb(c));
        b.appendChild(document.createTextNode(c.name));
        b.onclick = () => {
          if (on) { if (sel.length === 1) return toast('At least one controller stays selected'); sel = sel.filter((i) => i !== c.id); }
          else sel.push(c.id);
          saveSel();
          renderView();
        };
        box.appendChild(b);
      });
    });
  }

  // ------------------------------------------------------------ pictures + scenes
  const HOUSE =
    '<rect x="0" y="440" width="1000" height="60" fill="#0d1117"/>' +
    '<polygon points="10,440 62,290 114,440" fill="#0c1913"/><polygon points="70,440 112,330 154,440" fill="#0a1510"/>' +
    '<polygon points="872,440 928,270 984,440" fill="#0c1913"/><polygon points="836,440 872,350 908,440" fill="#0a1510"/>' +
    '<rect x="676" y="104" width="44" height="80" fill="#1a202c"/>' +
    '<rect x="150" y="212" width="700" height="228" fill="#151a24"/>' +
    '<polygon points="100,212 500,80 900,212" fill="#1d2330"/>' +
    '<rect x="100" y="208" width="800" height="8" fill="#232a38"/>' +
    '<rect x="220" y="268" width="120" height="86" rx="4" fill="#f2b45c" opacity=".5"/>' +
    '<rect x="660" y="268" width="120" height="86" rx="4" fill="#f2b45c" opacity=".42"/>' +
    '<rect x="380" y="268" width="60" height="50" rx="3" fill="#f2b45c" opacity=".3"/>' +
    '<rect x="470" y="330" width="60" height="110" fill="#0e1117"/>' +
    '<circle cx="500" cy="160" r="20" fill="#f2b45c" opacity=".25"/>';
  const photoUrl = (c) => url('photo', c.id) + '?v=' + ((c.layout && c.layout.v) || 0);
  const aspectOf = (lay) => (lay && lay.photo && lay.aspect > 0 ? lay.aspect : 2);

  function rnd(i) { const x = Math.sin(i * 12.9898) * 43758.5453; return x - Math.floor(x); }
  const MODES = {
    motionless: 'solid', starry: 'twinkle', glitter: 'twinkle', flicker: 'twinkle', glitch: 'twinkle',
    chase: 'chase', marquee: 'chase', pacman: 'chase', ghost: 'chase', pyramid_chase: 'chase',
    wave: 'wave', gradient_wave: 'wave', sway: 'wave', pulse: 'pulse', multipulse: 'pulse', tremor: 'pulse',
    fade: 'fade', isofade: 'fade', smooth: 'fade', fireworks: 'fireworks',
  };
  const STILL = new Set(['motionless', 'starry', 'fade', 'pulse', 'flicker', 'glitter']);
  // Colour and brightness (0..1) of LED ``i`` for pattern ``p`` at time ``t`` seconds.
  function colAt(i, p, t) {
    const cs = p.colors && p.colors.length ? p.colors : ['#fff1e0'];
    const n = cs.length;
    const T = t * (0.25 + 1.5 * ((p.speed == null ? 128 : p.speed) / 255)) * (p.direction === 1 ? -1 : 1);
    switch (MODES[p.animation] || 'flow') {
      case 'solid': return [cs[i % n], 1];
      case 'twinkle': return [cs[i % n], rnd(i * 7 + Math.floor(Math.abs(T) * 3)) > 0.72 ? 1 : 0.22];
      case 'chase': { const s = Math.floor(T * 6); return [cs[mod(Math.floor((i - s) / 4), n)], mod(i - s, 8) < 5 ? 1 : 0.06]; }
      case 'wave': return [cs[mod(Math.floor((i / 9 - T * 2) / Math.PI), n)], 0.2 + 0.8 * (Math.sin(i / 9 - T * 2) + 1) / 2];
      case 'pulse': return [cs[Math.floor(i / 12) % n], 0.25 + 0.75 * (Math.sin(Math.abs(T) * 2.5 + i / 40) + 1) / 2];
      case 'fade': return [cs[mod(Math.floor(i / 50) + Math.floor(Math.abs(T) / 1.5), n)], 0.55 + 0.45 * Math.abs(Math.sin(Math.abs(T) * 2.1))];
      case 'fireworks': {
        const b = Math.floor(i / 40), ph = mod(Math.abs(T) * 0.8 + rnd(b) * 3, 3), d = Math.abs((i % 40) - 20);
        return [cs[b % n], ph < 1 && d < ph * 20 ? 1 - ph * 0.7 : 0.05];
      }
      default: return [cs[mod(Math.floor((i - T * 8) / 20), n)], 1];
    }
  }
  function rgba(hex, a) {
    const v = parseInt(String(hex).slice(1), 16) || 0;
    return 'rgba(' + (v >> 16 & 255) + ',' + (v >> 8 & 255) + ',' + (v & 255) + ',' + a.toFixed(3) + ')';
  }

  // A picture (photo or built-in drawing) with LED dots along the light runs.
  class Scene {
    constructor(el, opts) {
      this.el = el;
      this.opts = opts || {};
      this.bg = h('canvas', 'scn');
      this.pic = h('div', 'scn pic');
      this.fx = h('canvas', 'scn');
      [this.bg, this.pic, this.fx].forEach((e) => { e.style.position = 'absolute'; e.style.pointerEvents = 'none'; });
      this.bg.style.inset = this.fx.style.inset = '0';
      this.bg.style.width = this.fx.style.width = '100%';
      this.bg.style.height = this.fx.style.height = '100%';
      el.prepend(this.bg, this.pic, this.fx);
      this.key = '';
      this.size = '';
    }
    set(ctrl, runs) {
      const lay = (ctrl && ctrl.layout) || { photo: false, runs: [] };
      this.ctrl = ctrl;
      this.aspect = aspectOf(lay);
      this.runs = runs || lay.runs || [];
      const key = ctrl ? ctrl.id + '|' + (lay.photo ? lay.v : 'drawing') : 'none';
      if (key !== this.key) {
        this.key = key;
        this.pic.textContent = '';
        if (ctrl && lay.photo) {
          const img = h('img');
          img.src = photoUrl(ctrl);
          img.alt = '';
          img.style.cssText = 'width:100%;height:100%;display:block';
          this.pic.appendChild(img);
        } else {
          this.pic.innerHTML = '<svg viewBox="0 0 1000 500" preserveAspectRatio="none" style="width:100%;height:100%;display:block">' + HOUSE + '</svg>';
        }
      }
      this.size = '';
    }
    fit() {
      const w = this.el.clientWidth, hh = this.el.clientHeight, A = this.aspect;
      const sz = w + 'x' + hh + '|' + A + '|' + JSON.stringify(this.runs);
      if (sz === this.size) return;
      this.size = sz;
      this.w = w; this.h = hh;
      let s;
      const contain = Math.min(w / A, hh), cover = Math.max(w / A, hh);
      let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
      this.runs.forEach((r) => r.points.forEach(([x, y]) => { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }));
      if (x1 < x0) { x0 = 0; x1 = 1; y0 = 0; y1 = 1; }
      if (this.opts.contain) s = contain;
      else {
        const bw = Math.max(x1 - x0, 0.2), bh = Math.max(y1 - y0, 0.15);
        s = Math.max(contain, Math.min(cover, Math.min(0.9 * w / (bw * A), 0.75 * hh / bh)));
      }
      const pw = A * s, ph = s;
      let ox, oy;
      if (this.opts.contain || pw <= w) ox = (w - pw) / 2;
      else ox = clamp(w / 2 - ((x0 + x1) / 2) * pw, w - pw, 0);
      if (this.opts.contain || ph <= hh) oy = (hh - ph) / 2;
      else oy = clamp(hh * 0.45 - ((y0 + y1) / 2) * ph, hh - ph, 0);
      Object.assign(this, { s, pw, ph, ox, oy });
      Object.assign(this.pic.style, { left: ox + 'px', top: oy + 'px', width: pw + 'px', height: ph + 'px' });
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      [this.bg, this.fx].forEach((c) => { c.width = Math.max(1, Math.round(w * dpr)); c.height = Math.max(1, Math.round(hh * dpr)); });
      this.dpr = dpr;
      const g = this.bg.getContext('2d');
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, hh);
      for (let i = 0; i < 70; i++) {
        g.fillStyle = 'rgba(255,255,255,' + (0.15 + rnd(i + 3) * 0.5).toFixed(2) + ')';
        const r = 0.6 + rnd(i + 9) * 1.2;
        g.beginPath(); g.arc(rnd(i) * w, rnd(i + 99) * hh * 0.6, r, 0, 7); g.fill();
      }
      this.dots = this.layoutDots();
    }
    toPx(p) { return [this.ox + p[0] * this.pw, this.oy + p[1] * this.ph]; }
    toPic(x, y) { return [clamp((x - this.ox) / this.pw, 0, 1), clamp((y - this.oy) / this.ph, 0, 1)]; }
    layoutDots() {
      const out = [];
      const gap = this.opts.small ? 5 : 7;
      let offset = 0;
      this.runs.forEach((run) => {
        const pts = run.points.map((p) => this.toPx(p));
        const leds = Math.max(1, run.leds | 0);
        if (pts.length >= 2) {
          const segs = []; let L = 0;
          for (let i = 1; i < pts.length; i++) { const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); segs.push(d); L += d; }
          const n = clamp(Math.floor(L / gap), 2, leds);
          let si = 0, acc = 0;
          for (let j = 0; j < n; j++) {
            const f = (j + 0.5) / n, d = f * L;
            while (si < segs.length - 1 && acc + segs[si] < d) { acc += segs[si]; si++; }
            const k = segs[si] ? (d - acc) / segs[si] : 0;
            const a = pts[si], b = pts[si + 1];
            let local = Math.min(leds - 1, Math.floor(f * leds));
            if (run.reverse) local = leds - 1 - local;
            out.push([a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, offset + local]);
          }
        }
        offset += leds;
      });
      return out;
    }
    draw(pattern, on, t) {
      if (!this.el.offsetParent) return;
      this.fit();
      const g = this.fx.getContext('2d');
      g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      g.clearRect(0, 0, this.w, this.h);
      const small = this.opts.small;
      const r = small ? 1.8 : 2.8, gr = small ? 5 : 8;
      if (!on || !pattern) {
        g.fillStyle = 'rgba(120,128,140,.45)';
        this.dots.forEach(([x, y]) => { g.beginPath(); g.arc(x, y, r * 0.8, 0, 7); g.fill(); });
        return;
      }
      const bs = 0.35 + 0.65 * clamp((pattern.brightness == null ? 255 : pattern.brightness) / 255, 0, 1);
      this.dots.forEach(([x, y, i]) => {
        const [c, a] = colAt(i, pattern, t);
        const al = a * bs;
        g.fillStyle = rgba(c, 0.28 * al);
        g.beginPath(); g.arc(x, y, gr, 0, 7); g.fill();
        g.fillStyle = rgba(c, Math.min(1, 0.25 + al));
        g.beginPath(); g.arc(x, y, r, 0, 7); g.fill();
      });
    }
  }

  // Small LED-strip previews for cards and animation tiles.
  function strip(canvas, p, t, arc) {
    const w = canvas.width, hh = canvas.height, g = canvas.getContext('2d');
    g.clearRect(0, 0, w, hh);
    const n = arc ? 26 : 22;
    for (let j = 0; j < n; j++) {
      const f = (j + 0.5) / n, x = w * (0.06 + 0.88 * f);
      const y = arc ? hh * (0.62 - 0.3 * Math.sin(f * Math.PI)) : hh * 0.5;
      const [c, a] = colAt(j * 3, p, t);
      g.fillStyle = rgba(c, 0.3 * a);
      g.beginPath(); g.arc(x, y, hh * 0.11, 0, 7); g.fill();
      g.fillStyle = rgba(c, Math.min(1, 0.25 + a));
      g.beginPath(); g.arc(x, y, hh * 0.045, 0, 7); g.fill();
    }
  }

  // ------------------------------------------------------------ data
  let OURS = null, LIB = null;
  async function loadOurs(force) {
    if (OURS && !force) return OURS;
    OURS = await api(url('ours'));
    return OURS;
  }
  async function loadLib() {
    if (!LIB) LIB = (await api(url('library'))).patterns || [];
    return LIB;
  }
  async function refreshState() {
    try {
      S = await api(url('state'));
      fixSel();
      renderView();
    } catch (e) { /* keep the last state */ }
  }
  const later = (fn, ms) => setTimeout(fn, ms);

  async function play(ref, preview) {
    const ids = sel.slice();
    try {
      const r = await api(url('play'), { ids, pattern: ref });
      selCtrls().forEach((c) => { c.state = { on: true, pattern: preview || (c.state && c.state.pattern) }; });
      renderView();
      toast(['Playing ', { b: r.name }, ' on ' + r.on], async () => {
        try { await api(url('undo'), { token: r.undo }); toast('Back to what was playing'); } catch (e) { fail(e); }
        later(refreshState, 600);
      });
      later(refreshState, 2500);
    } catch (e) { fail(e); }
  }
  const playPattern = (p) => play({ id: p.id }, p);
  const SOLIDS = [['Warm white', '#fff1e0'], ['White', '#ffffff'], ['Red', '#e3262b'], ['Orange', '#ff7a1a'], ['Gold', '#ffb800'],
    ['Green', '#1fa64a'], ['Teal', '#14b8a6'], ['Blue', '#2f6bff'], ['Purple', '#8b3dff'], ['Pink', '#ff4fa3']];
  const solidPat = (name, hex) => ({ id: 'bdh-solid-' + hex.slice(1), name, colors: [hex], animation: 'motionless', brightness: 255, speed: 0, direction: 0 });

  // ------------------------------------------------------------ cards
  function card(p, opts) {
    opts = opts || {};
    const c = h('div', 'g-card');
    c.setAttribute('role', 'button');
    const pv = h('div', 'pv'); const cv = h('canvas');
    cv.width = 540; cv.height = 224; cv.style.objectFit = 'cover';
    pv.appendChild(cv); c.appendChild(pv);
    strip(cv, p, 1.3, true);
    const nm = h('div', 'nm', p.name);
    nm.appendChild(h('span', 'an', pretty(p.animation || 'motionless')));
    c.appendChild(nm);
    const first = selCtrls()[0];
    if (first && first.state && first.state.on && first.state.pattern && first.state.pattern.id === p.id) {
      c.classList.add('on'); c.appendChild(h('span', 'pl', 'PLAYING'));
    }
    const more = h('button', 'more', '⋯');
    more.title = 'Open in Create';
    more.onclick = (e) => { e.stopPropagation(); openCreate(p); };
    c.appendChild(more);
    c.onclick = () => playPattern(p);
    return c;
  }
  function solidTile(name, hex) {
    const b = h('button', 'solid');
    const i = h('i'); i.style.background = hex; i.style.boxShadow = '0 0 18px ' + hex;
    b.append(i, document.createTextNode(name));
    b.onclick = () => play({ kind: 'solid', color: hex, name }, solidPat(name, hex));
    return b;
  }

  // ------------------------------------------------------------ views
  const VIEWS = ['tonight', 'patterns', 'create', 'setup'];
  let view = 'tonight';
  const scenes = {};
  function heroScene(key, el, opts) {
    if (!scenes[key]) scenes[key] = new Scene(el, opts);
    return scenes[key];
  }
  function routeFromHash() {
    let v = (location.hash || '').replace('#', '');
    if (!VIEWS.includes(v)) v = 'tonight';
    if (v === 'setup' && !S.admin) v = 'tonight';
    view = v;
    closePop();
    renderView();
  }
  window.addEventListener('hashchange', routeFromHash);

  function renderShell() {
    root.querySelectorAll('.gs-admin').forEach((e) => { e.hidden = !S.admin; });
    const who = $('gs-who');
    who.textContent = S.admin ? 'Admin' : 'Guest';
    who.classList.toggle('admin', !!S.admin);
    root.querySelectorAll('.ri').forEach((a) => a.classList.toggle('on', a.dataset.view === view));
    VIEWS.forEach((v) => { $('v-' + v).hidden = v !== view; });
    const msg = $('gs-msg');
    msg.textContent = '';
    if (!S.configured) {
      msg.append(h('b', '', 'Gemstone isn\u2019t set up yet. '),
        document.createTextNode(S.admin ? 'Enter the Gemstone account email and password in this app\u2019s Settings.'
          : 'Ask an admin to enter the Gemstone account in this app\u2019s Settings.'));
    } else if (S.error) {
      msg.append(h('b', '', S.error + '. '), document.createTextNode('Trying again shortly\u2026'));
    } else if (!ctrls().length) {
      msg.append(h('b', '', 'No controllers found. '), document.createTextNode('Add your lights in the Gemstone app first.'));
    }
    msg.hidden = !msg.textContent;
  }

  function renderView() {
    fixSel();
    renderShell();
    renderChips();
    if (view === 'tonight') renderTonight();
    else if (view === 'patterns') renderPatterns();
    else if (view === 'create') renderCreate();
    else if (view === 'setup') renderSetup();
  }

  // ------------------------------------------------------------ tonight
  let bright = null, brightTimer = null;
  function statusLine(list) {
    const on = list.filter((c) => c.state && c.state.on);
    if (!list.length) return '';
    if (!on.length) return 'Off · ' + namesOf(list);
    return 'Playing on ' + namesOf(on) + (on.length < list.length ? ' · ' + namesOf(list.filter((c) => !on.includes(c))) + ' off' : '');
  }
  function renderTonight() {
    const list = selCtrls(), first = list[0];
    const sc = heroScene('t', $('t-hero'));
    sc.set(first);
    const st = (first && first.state) || {};
    $('t-name').textContent = first ? (st.on ? ((st.pattern && st.pattern.name) || 'On') : 'Lights off') : '—';
    $('t-sub').textContent = statusLine(list);
    // insets for the other selected controllers
    const ins = $('t-insets');
    ins.textContent = '';
    list.slice(1, 4).forEach((c) => {
      const b = h('button', 'inset');
      const pic = h('div', 'pic'); b.appendChild(pic);
      const lb = h('div', 'lb'), tx = h('div');
      tx.append(h('b', '', c.name), h('small', '', c.state && c.state.on ? ((c.state.pattern && c.state.pattern.name) || 'On') : 'Off'));
      const dot = h('span', 'dot');
      if (c.state && c.state.on) { dot.style.background = '#3ddc84'; dot.style.boxShadow = '0 0 8px #3ddc84'; }
      lb.append(tx, dot); b.appendChild(lb);
      b.onclick = () => { sel = [c.id].concat(sel.filter((i) => i !== c.id)); saveSel(); renderView(); };
      ins.appendChild(b);
      const s = new Scene(pic, { small: true });
      s.set(c);
      b._scene = s;
    });
    const anyOn = list.some((c) => c.state && c.state.on);
    $('t-pwr').classList.toggle('on', anyOn);
    if (bright == null || !brightTimer) {
      const p = st.pattern;
      setSlider($('t-bslider'), p ? Math.round(p.brightness * 100 / 255) : 100);
    }
    $('t-bslider').classList.toggle('dis', !anyOn);
    $('t-bval').textContent = anyOn ? sliders.get($('t-bslider')).value + '%' : '—';
    renderTimeline(list);
    renderFavs();
  }
  function untilFor(list) {
    const u = S.until;
    return u && list.some((c) => u.ids.includes(c.id)) ? u : null;
  }
  function renderTimeline(list) {
    const u = untilFor(list), tl = $('t-tl');
    tl.classList.toggle('none', !u);
    if (!u) { $('t-left').textContent = 'No limit'; $('t-until-at').textContent = (S.admin ? 'No limit' : 'Pick a time') + ' ▾'; return; }
    const from = new Date(u.from).getTime(), at = new Date(u.at).getTime(), now = Date.now();
    const f = clamp((now - from) / Math.max(1, at - from), 0, 1);
    tl.querySelector('.done').style.width = (f * 100) + '%';
    tl.querySelector('.now').style.left = (f * 100) + '%';
    const left = Math.max(0, at - now), hh = Math.floor(left / 3600e3), mm = Math.round((left % 3600e3) / 60e3);
    $('t-left').textContent = 'Off at ' + fmtTime(new Date(at)) + ' · ' + (hh ? hh + ' h ' : '') + mm + ' min left';
    $('t-until-at').textContent = fmtTime(new Date(at)) + ' ▾';
  }
  async function renderFavs() {
    const row = $('t-favs');
    if (!S.configured || !ctrls().length) { row.textContent = ''; return; }
    let pats = [], title = 'Favourites';
    try {
      const o = await loadOurs();
      pats = o.patterns.filter((p) => p.fav);
      if (!pats.length) { pats = o.patterns.slice(); title = 'Ours'; }
      if (!pats.length) { pats = (await loadLib()).slice(0, 12); title = 'From the Gemstone library'; }
    } catch (e) { row.textContent = ''; row.appendChild(h('div', 'empty', e.message)); return; }
    if (view !== 'tonight') return;
    $('t-fav-h').textContent = title;
    row.textContent = '';
    pats.slice(0, 12).forEach((p) => row.appendChild(card(p)));
  }
  $('t-pwr').onclick = async () => {
    const list = selCtrls();
    if (!list.length) return;
    const on = !list.some((c) => c.state && c.state.on);
    try {
      await api(url('power'), { ids: sel, on });
      list.forEach((c) => { c.state = Object.assign({}, c.state, { on }); });
      toast([on ? 'Turned on ' : 'Turned off ', { b: namesOf(list) }]);
      renderView();
      later(refreshState, 1500);
    } catch (e) { fail(e); }
  };
  $('t-until').setAttribute('data-pop', '');
  $('t-until').onclick = (e) => {
    const btn = e.currentTarget;
    openPop(btn, (p) => {
      const now = new Date(), base = new Date(now); base.setMinutes(0, 0, 0);
      let opts = [];
      for (let i = 1; i <= 7; i++) opts.push(new Date(base.getTime() + i * 3600e3));
      if (!S.admin) {
        const dl = new Date(S.guest_deadline);
        opts = opts.filter((d) => d <= dl);
        if (!opts.length || opts[opts.length - 1] < dl) opts.push(dl);
      }
      const cur = untilFor(selCtrls());
      p.appendChild(h('div', 'it dis', S.admin ? 'Keep the lights on until' : 'Keep on until (guests: ' + fmtTime(new Date(S.guest_deadline)) + ' latest)'));
      opts.forEach((d) => p.appendChild(popItem(fmtTime(d), cur && Math.abs(new Date(cur.at) - d) < 60e3 ? 'on' : '', () => setUntil(hhmm(d)))));
      if (S.admin) { p.appendChild(h('hr')); p.appendChild(popItem('No limit', cur ? '' : 'on', () => setUntil(''))); }
    });
  };
  async function setUntil(at) {
    closePop();
    try {
      const r = await api(url('until'), { ids: sel, at });
      S.until = r.until;
      toast(r.until ? ['Lights go off at ', { b: fmtTime(new Date(r.until.at)) }] : 'No time limit');
      renderView();
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ sliders
  const sliders = new Map();
  function makeSlider(el, onMove, onCommit) {
    const st = { value: 0, onMove, onCommit };
    sliders.set(el, st);
    const at = (e) => { const r = el.getBoundingClientRect(); return Math.round(clamp((e.clientX - r.left) / r.width, 0, 1) * 100); };
    let dragging = false;
    el.addEventListener('pointerdown', (e) => { dragging = true; el.setPointerCapture(e.pointerId); setSlider(el, at(e)); onMove && onMove(st.value); });
    el.addEventListener('pointermove', (e) => { if (!dragging) return; setSlider(el, at(e)); onMove && onMove(st.value); });
    const end = () => { if (!dragging) return; dragging = false; onCommit && onCommit(st.value); };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }
  function setSlider(el, v) {
    const st = sliders.get(el);
    st.value = clamp(Math.round(v), 0, 100);
    el.querySelector('.f').style.width = Math.max(st.value, 4) + '%';
  }
  makeSlider($('t-bslider'), (v) => { $('t-bval').textContent = Math.max(1, v) + '%'; bright = v; },
    (v) => {
      clearTimeout(brightTimer);
      brightTimer = setTimeout(async () => {
        const pct = Math.max(1, v);
        try {
          await api(url('brightness'), { ids: sel, brightness: pct });
          selCtrls().forEach((c) => { if (c.state && c.state.pattern) c.state.pattern.brightness = Math.round(pct * 255 / 100); });
        } catch (e) { fail(e); }
        brightTimer = null;
      }, 300);
    });

  // ------------------------------------------------------------ patterns
  let folderFilter = '';
  async function renderPatterns() {
    const list = selCtrls(), first = list[0];
    heroScene('p', $('p-hero')).set(first);
    const st = (first && first.state) || {};
    $('p-name').textContent = first ? (st.on ? ((st.pattern && st.pattern.name) || 'On') : 'Lights off') : '—';
    $('p-sub').textContent = statusLine(list) + (list.length ? ' · tap a pattern to play it' : '');
    if (!S.configured || !ctrls().length) return;
    const solids = $('p-solids');
    if (!solids.childElementCount) SOLIDS.forEach(([n, c]) => solids.appendChild(solidTile(n, c)));
    let o, lib;
    try { [o, lib] = await Promise.all([loadOurs(), loadLib()]); } catch (e) { fail(e); return; }
    if (view !== 'patterns') return;
    const q = $('p-search').value.trim().toLowerCase();
    const match = (p) => !q || p.name.toLowerCase().includes(q);
    const favs = o.patterns.filter((p) => p.fav && match(p));
    $('p-fav-shelf').hidden = !favs.length;
    fill($('p-favs'), favs, 'No favourites yet');
    const fc = $('p-folders');
    fc.textContent = '';
    const folders = o.folders.filter((f) => f.count || f.guest);
    if (folderFilter && !folders.some((f) => f.id === folderFilter)) folderFilter = '';
    [{ id: '', name: 'All' }].concat(folders).forEach((f) => {
      const b = h('button', 'chip' + (folderFilter === f.id ? ' on' : ''), (f.guest ? '🔒 ' : '') + f.name);
      if (f.count != null) b.appendChild(h('span', 't3', '\u00a0' + f.count));
      b.onclick = () => { folderFilter = f.id; renderPatterns(); };
      fc.appendChild(b);
    });
    fill($('p-ours'), o.patterns.filter((p) => (!folderFilter || p.folder === folderFilter) && match(p)),
      q ? 'No matches' : 'Nothing here yet — make one in Create');
    const box = $('p-lib');
    box.textContent = '';
    const cats = new Map();
    lib.filter(match).forEach((p) => { const k = p.category || 'More'; if (!cats.has(k)) cats.set(k, []); cats.get(k).push(p); });
    cats.forEach((pats, cat) => {
      const sh = h('div', 'shelf');
      const hd = h('div', 'sh'); hd.appendChild(h('h2', '', cat)); hd.appendChild(h('span', 't3', pats.length + ' patterns'));
      const sc = h('div', 'scroller cards');
      sh.append(hd, sc);
      fill(sc, pats);
      box.appendChild(sh);
    });
  }
  function fill(el, pats, empty) {
    el.textContent = '';
    if (!pats.length && empty) { el.appendChild(h('div', 'empty', empty)); return; }
    pats.forEach((p) => el.appendChild(card(p)));
  }
  let searchT = null;
  $('p-search').addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(renderPatterns, 250); });
  $('p-golib').onclick = (e) => { e.preventDefault(); const b = $('p-lib'); if (b.firstChild) b.scrollIntoView({ behavior: 'smooth' }); };

  // ------------------------------------------------------------ create
  const QUICK = [['Red', '#e3262b'], ['Green', '#1fa64a'], ['Warm white', '#fdf4bc'], ['Gold', '#ffb800'], ['Blue', '#2f6bff'],
    ['Purple', '#8b3dff'], ['Orange', '#ff7a1a'], ['Pink', '#ff4fa3'], ['White', '#ffffff'], ['Teal', '#14b8a6']];
  const C = { design: null, id: '', folder: null, selIdx: 0, more: false, live: false, dirty: false,
    saving: false, again: false, saveT: null, liveT: null, guestName: '' };
  function newDesign() {
    C.design = { colors: ['#e3262b', '#1fa64a', '#fdf4bc', '#ffb800'], animation: 'starry', brightness: 100, speed: 128, direction: 0, name: 'Untitled design' };
    C.id = ''; C.folder = null; C.selIdx = 0; C.dirty = false; C.guestName = '';
    savedText('');
  }
  newDesign();
  const spec = () => ({ colors: C.design.colors.slice(), animation: C.design.animation, brightness: C.design.brightness,
    speed: C.design.speed, direction: C.design.direction, name: C.design.name });
  const previewPattern = () => ({ colors: C.design.colors, animation: C.design.animation, speed: C.design.speed,
    direction: C.design.direction, brightness: Math.round(C.design.brightness * 255 / 100) });

  function openCreate(p) {
    newDesign();
    C.design = { colors: (p.colors || ['#fff1e0']).slice(0, 20), animation: p.animation || 'motionless',
      brightness: clamp(Math.round((p.brightness == null ? 255 : p.brightness) * 100 / 255), 1, 100),
      speed: p.speed == null ? 128 : p.speed, direction: p.direction || 0, name: p.name || 'Untitled design' };
    const folder = OURS && p.folder ? OURS.folders.find((f) => f.id === p.folder) : null;
    if (folder && !folder.managed && (S.admin || folder.guest)) {
      C.id = p.id; C.folder = { id: folder.id, name: folder.name };
      if (!S.admin) C.guestName = p.name;
    } else if (S.admin) {
      C.design.name = p.name ? p.name + ' (copy)' : 'Untitled design';
    }
    C.more = !STILL.has(C.design.animation) && !S.animations.some((a) => a.name === C.design.animation);
    C.more = C.more || S.more_animations.includes(C.design.animation);
    location.hash = '#create';
    if (view === 'create') renderView();
  }
  function savedText(text, cls) {
    const el = $('c-saved');
    el.textContent = text;
    el.className = 'saved' + (cls ? ' ' + cls : '');
  }
  function defaultFolder() {
    if (C.folder) return C.folder;
    if (!S.admin) return { id: '', name: S.guest_folder };
    const own = OURS && OURS.folders.find((f) => !f.managed && !f.guest);
    return own ? { id: own.id, name: own.name } : { id: '', name: 'Our designs' };
  }
  function changed() {
    C.dirty = true;
    renderCreate();
    clearTimeout(C.saveT);
    savedText('Saving\u2026', 'wait');
    C.saveT = setTimeout(saveDesign, 1500);
    if (C.live) { clearTimeout(C.liveT); C.liveT = setTimeout(pushLive, 800); }
  }
  async function saveDesign() {
    if (C.saving) { C.again = true; return; }
    C.saving = true;
    try {
      const f = defaultFolder();
      const r = await api(url('design'), { pattern: spec(), id: C.id, folder: f.id, name: C.design.name });
      C.id = r.id;
      C.folder = r.folder;
      if (!S.admin) C.guestName = r.name;
      C.dirty = false;
      savedText('✓ Saved');
      if (OURS) loadOurs(true).catch(() => {});
      if (view === 'create') renderCreateHeader();
    } catch (e) {
      savedText('Not saved', 'err');
      fail(e);
    } finally {
      C.saving = false;
      if (C.again) { C.again = false; saveDesign(); }
    }
  }
  async function pushLive() {
    try { await api(url('live'), { ids: sel, pattern: spec() }); } catch (e) { fail(e); }
  }
  function renderCreateHeader() {
    const t = $('c-title');
    t.contentEditable = S.admin ? 'true' : 'false';
    if (document.activeElement !== t) t.textContent = S.admin ? C.design.name : (C.guestName || 'Guest design · named when saved');
    $('c-pen').hidden = !S.admin;
    const f = defaultFolder(), fb = $('c-fold');
    fb.textContent = '';
    if (S.admin) { fb.append(document.createTextNode('📁 ' + f.name + ' ')); fb.appendChild(h('small', '', '▾')); }
    else { fb.append(document.createTextNode('🔒 📁 ' + S.guest_folder)); }
    fb.classList.toggle('locked', !S.admin);
    $('c-pname').textContent = t.textContent;
  }
  function renderCreate() {
    const list = selCtrls();
    const sc = heroScene('c', $('c-hero'));
    sc.set(list[0]);
    renderCreateHeader();
    $('c-hero').classList.toggle('lv', C.live);
    $('c-live').classList.toggle('on', C.live);
    $('c-live').querySelector('span:nth-child(2)').textContent = C.live ? 'Live on ' + namesOf(list) : 'Live on lights';
    const ps = $('c-pswatch'); ps.textContent = '';
    C.design.colors.forEach((c) => { const i = h('i'); i.style.background = c; ps.appendChild(i); });
    // colours
    const sws = $('c-sws'); sws.textContent = '';
    $('c-count').textContent = C.design.colors.length + (C.design.colors.length === 1 ? ' colour' : ' colours');
    C.design.colors.forEach((c, i) => {
      const b = h('button', 'swc' + (i === C.selIdx ? ' sel' : ''));
      b.style.background = c;
      b.onclick = () => {
        if (i === C.selIdx) { const pk = $('c-picker'); pk.value = c; pk.click(); return; }
        C.selIdx = i; renderCreate();
      };
      if (i === C.selIdx && C.design.colors.length > 1) {
        const x = h('span', 'x', '✕');
        x.onclick = (e) => { e.stopPropagation(); C.design.colors.splice(i, 1); C.selIdx = Math.min(C.selIdx, C.design.colors.length - 1); changed(); };
        b.appendChild(x);
      }
      sws.appendChild(b);
    });
    if (C.design.colors.length < 20) {
      const add = h('button', 'swc add', '+');
      add.onclick = () => { C.design.colors.push(C.design.colors[C.design.colors.length - 1] || '#ffffff'); C.selIdx = C.design.colors.length - 1; changed(); };
      sws.appendChild(add);
    }
    const qk = $('c-quick');
    if (!qk.childElementCount) QUICK.forEach(([n, c]) => {
      const b = h('button', 'chip');
      const dot = h('i'); dot.style.cssText = 'width:22px;height:22px;border-radius:50%;margin-right:10px;display:inline-block;background:' + c;
      b.append(dot, document.createTextNode(n));
      b.onclick = () => { C.design.colors[C.selIdx] = c; changed(); };
      qk.appendChild(b);
    });
    // animations
    const ag = $('c-anims'); ag.textContent = '';
    const anims = S.animations.map((a) => [a.label, a.name]).concat(C.more ? S.more_animations.map((n) => [pretty(n), n]) : []);
    anims.forEach(([label, name]) => {
      const b = h('button', 'at' + (C.design.animation === name ? ' on' : ''));
      const cv = h('canvas'); cv.width = 300; cv.height = 64; cv.dataset.anim = name;
      b.append(cv, h('span', '', label));
      b.onclick = () => { C.design.animation = name; changed(); };
      ag.appendChild(b);
    });
    $('c-more').textContent = C.more ? 'Fewer animations ‹' : 'More animations (' + S.more_animations.length + ') ›';
    setSlider($('c-speed'), C.design.speed * 100 / 255);
    setSlider($('c-bright'), C.design.brightness);
    $('c-bval').textContent = C.design.brightness + '%';
    const still = STILL.has(C.design.animation);
    $('c-dir').classList.toggle('dim', still);
    $('c-dirnote').textContent = still ? '· not used by ' + pretty(C.design.animation) : '';
    $('c-dir').querySelectorAll('span').forEach((s) => s.classList.toggle('on', +s.dataset.d === C.design.direction));
  }
  $('c-picker').addEventListener('input', (e) => { C.design.colors[C.selIdx] = e.target.value; changed(); });
  $('c-more').onclick = (e) => { e.preventDefault(); C.more = !C.more; renderCreate(); };
  makeSlider($('c-speed'), (v) => { C.design.speed = Math.round(v * 255 / 100); }, () => changed());
  makeSlider($('c-bright'), (v) => { C.design.brightness = Math.max(1, v); $('c-bval').textContent = C.design.brightness + '%'; }, () => changed());
  $('c-dir').querySelectorAll('span').forEach((s) => { s.onclick = () => { C.design.direction = +s.dataset.d; changed(); }; });
  $('c-live').onclick = () => {
    C.live = !C.live;
    renderCreate();
    if (C.live) { pushLive(); toast(['Live: changes show on ', { b: namesOf(selCtrls()) }]); }
  };
  $('c-new').onclick = () => { newDesign(); renderCreate(); };
  const title = $('c-title');
  title.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); title.blur(); } });
  title.addEventListener('blur', () => {
    if (!S.admin) return;
    const n = title.textContent.replace(/\s+/g, ' ').trim().slice(0, 60) || 'Untitled design';
    if (n !== C.design.name) { C.design.name = n; changed(); } else title.textContent = n;
  });
  $('c-pen').onclick = () => { title.focus(); };
  $('c-fold').setAttribute('data-pop', '');
  $('c-fold').onclick = async (e) => {
    if (!S.admin) { toast('Guest designs are saved in "' + S.guest_folder + '" and cleaned up after ' + S.keep_days + ' days'); return; }
    const btn = e.currentTarget;
    try { await loadOurs(); } catch (err) { fail(err); return; }
    const cur = defaultFolder();
    openPop(btn, (p) => {
      p.appendChild(h('div', 'it dis', 'Save to folder'));
      OURS.folders.filter((f) => !f.managed).forEach((f) => {
        p.appendChild(popItem('📁 ' + f.name, f.id === cur.id ? 'on' : '', () => pickFolder(f),
          f.guest ? 'auto-cleans ' + S.keep_days + ' d' : String(f.count)));
      });
      p.appendChild(h('hr'));
      const nb = popItem('＋ New folder\u2026', 'new', () => {
        const inp = h('input'); inp.placeholder = 'Folder name'; inp.maxLength = 40;
        nb.replaceWith(inp); inp.focus();
        inp.addEventListener('keydown', async (ev) => {
          if (ev.key !== 'Enter') return;
          try {
            const f = await api(url('folders'), { name: inp.value });
            await loadOurs(true);
            pickFolder(f);
          } catch (err) { fail(err); }
        });
      });
      p.appendChild(nb);
    });
  };
  function pickFolder(f) {
    closePop();
    C.folder = { id: f.id, name: f.name };
    renderCreateHeader();
    if (C.id || C.dirty) changed();
  }

  // ------------------------------------------------------------ setup
  const SU = { cid: null, runs: [], active: 0, tool: 'draw', hist: [], open: -1, saveT: null, drag: null, svg: null, saving: null };
  function setupCtrl() { return ctrlById(SU.cid); }
  function loadSetup(cid) {
    const c = ctrlById(cid);
    SU.cid = cid;
    SU.runs = clone((c.layout && c.layout.runs) || []);
    SU.active = 0; SU.hist = []; SU.open = -1;
    sSaved('');
  }
  function sSaved(text, cls) { const el = $('s-saved'); el.textContent = text; el.className = 'saved' + (cls ? ' ' + cls : ''); }
  function renderSetup() {
    if (!S.admin) return;
    const cs = ctrls();
    if (!cs.length) return;
    if (!SU.cid || !ctrlById(SU.cid)) loadSetup((selCtrls()[0] || cs[0]).id);
    const c = setupCtrl();
    const box = $('s-ctrls'); box.textContent = '';
    cs.forEach((x) => {
      const b = h('button', 'chip-c' + (x.id === SU.cid ? ' on' : ''));
      b.appendChild(thumb(x)); b.appendChild(document.createTextNode(x.name));
      b.onclick = async () => { if (x.id === SU.cid) return; await flushLayout(); loadSetup(x.id); renderSetup(); };
      box.appendChild(b);
    });
    box.hidden = cs.length < 2;
    $('s-acct').textContent = '';
    $('s-acct').append(h('b', '', '✓ '), document.createTextNode('Gemstone connected · ' + cs.length + (cs.length === 1 ? ' controller' : ' controllers')));
    const lay = c.layout || {};
    const th = $('s-thumb'); th.textContent = '';
    if (lay.photo) th.style.backgroundImage = 'url("' + photoUrl(c) + '")';
    else { th.style.backgroundImage = ''; th.innerHTML = '<svg viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid slice">' + HOUSE + '</svg>'; }
    $('s-upload').textContent = lay.photo ? '📷 Replace photo' : '📷 Add a photo of the house';
    $('s-rmphoto').hidden = !lay.photo;
    $('s-look-f').hidden = !lay.photo;
    $('s-look').querySelectorAll('span').forEach((s) => s.classList.toggle('on', s.dataset.look === (lay.look || 'night')));
    $('s-ed').querySelectorAll('.tool [data-tool]').forEach((s) => s.classList.toggle('on', s.dataset.tool === SU.tool));
    const run = SU.runs[SU.active];
    const tip = $('s-tip'); tip.textContent = '';
    if (SU.tool === 'draw') tip.append(h('b', '', 'Tap along the lights'), document.createTextNode(' to add points to ' + (run ? '"' + run.name + '"' : 'a new run') + '. Drag a point to move it.'));
    else tip.append(h('b', '', 'Drag points'), document.createTextNode(' to move them; tap a line to add a bend.'));
    const sc = heroScene('s', $('s-ed'), { contain: true });
    sc.set(c, SU.runs);
    renderRuns();
    renderOverlay();
  }
  function renderRuns() {
    const box = $('s-runs'); box.textContent = '';
    SU.runs.forEach((r, i) => {
      const row = h('div', 'run' + (i === SU.active ? ' on' : ''));
      const hd = h('div', 'rh');
      const tx = h('div', 'grow'); tx.append(document.createTextNode(r.name), h('small', '', r.leds + ' lights · ' + r.points.length + ' points' + (r.reverse ? ' · starts at the far end' : '')));
      const dots = h('button', 'dots', '⋯');
      hd.append(h('span', 'st', '▶'), tx, dots);
      hd.onclick = () => { SU.active = i; renderSetup(); };
      dots.onclick = (e) => { e.stopPropagation(); SU.active = i; SU.open = SU.open === i ? -1 : i; renderSetup(); };
      row.appendChild(hd);
      if (SU.open === i) {
        const rx = h('div', 'rx');
        const nm = h('input', 'nm'); nm.value = r.name; nm.maxLength = 40; nm.placeholder = 'Name';
        nm.onchange = () => { pushHist(); r.name = nm.value.trim() || 'Run ' + (i + 1); runsChanged(); };
        const ld = h('input', 'ld'); ld.type = 'number'; ld.min = 1; ld.max = 5000; ld.value = r.leds;
        ld.onchange = () => { pushHist(); r.leds = clamp(parseInt(ld.value, 10) || 1, 1, 5000); runsChanged(); };
        const rev = h('button', 'g-btn', '⇄ Flip start');
        rev.onclick = () => { pushHist(); r.reverse = !r.reverse; runsChanged(); };
        const del = h('button', 'g-btn', 'Delete');
        del.disabled = SU.runs.length < 2;
        if (del.disabled) del.style.opacity = '.4';
        del.onclick = () => { if (SU.runs.length < 2) return; pushHist(); SU.runs.splice(i, 1); SU.active = 0; SU.open = -1; runsChanged(); };
        rx.append(nm, ld, h('span', 't3', 'lights'), rev, del);
        row.appendChild(rx);
      }
      box.appendChild(row);
    });
    $('s-add').disabled = SU.runs.length >= 8;
  }
  function renderOverlay() {
    const ed = $('s-ed'), sc = scenes.s;
    if (!sc || !ed.offsetParent) return;
    sc.fit();
    if (!SU.svg) { SU.svg = sv('svg', { class: 'ov' }); SU.svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none'; ed.insertBefore(SU.svg, $('s-tip')); }
    const g = SU.svg;
    g.setAttribute('width', sc.w); g.setAttribute('height', sc.h);
    g.textContent = '';
    SU.runs.forEach((r, ri) => {
      const act = ri === SU.active;
      const pts = r.points.map((p) => sc.toPx(p));
      if (pts.length > 1) {
        g.appendChild(sv('polyline', { points: pts.map((p) => p.join(',')).join(' '), fill: 'none',
          stroke: act ? '#5aa0ff' : 'rgba(255,255,255,.45)', 'stroke-width': act ? 4 : 3, 'stroke-dasharray': act ? '' : '8 6', 'stroke-linecap': 'round' }));
        const a = r.reverse ? pts[pts.length - 1] : pts[0], b = r.reverse ? pts[pts.length - 2] : pts[1];
        const ang = Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI;
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
        g.appendChild(sv('path', { d: 'M-9,-9 L5,0 L-9,9', fill: 'none', stroke: act ? '#fff' : 'rgba(255,255,255,.6)', 'stroke-width': 4,
          transform: 'translate(' + mx + ',' + my + ') rotate(' + ang + ')' }));
      }
      pts.forEach((p, pi) => g.appendChild(sv('circle', { cx: p[0], cy: p[1], r: act ? 11 : 8, fill: '#0B0D10',
        stroke: act ? '#5aa0ff' : 'rgba(255,255,255,.6)', 'stroke-width': 4 })));
      if (pts.length) {
        const s = r.reverse ? pts[pts.length - 1] : pts[0];
        g.appendChild(sv('circle', { cx: s[0], cy: s[1], r: 20, fill: '#34c759', stroke: '#0B0D10', 'stroke-width': 3 }));
        const t = sv('text', { x: s[0] + 1, y: s[1] + 7, 'text-anchor': 'middle', 'font-size': 18, fill: '#fff' }); t.textContent = '▶';
        g.appendChild(t);
        const lb = sv('text', { x: s[0], y: s[1] - 30, 'text-anchor': 'middle', 'font-size': 18, 'font-weight': 600, fill: '#fff', stroke: '#000', 'stroke-width': 4, 'paint-order': 'stroke' });
        lb.textContent = r.name;
        g.appendChild(lb);
      }
    });
  }
  function pushHist() { SU.hist.push(clone(SU.runs)); if (SU.hist.length > 60) SU.hist.shift(); }
  const runsValid = () => SU.runs.length >= 1 && SU.runs.every((r) => r.points.length >= 2 && r.leds >= 1);
  function runsChanged() {
    scenes.s && scenes.s.set(setupCtrl(), SU.runs);
    renderSetup();
    clearTimeout(SU.saveT);
    if (!runsValid()) { sSaved('Each run needs at least 2 points', 'wait'); return; }
    sSaved('Saving\u2026', 'wait');
    SU.saveT = setTimeout(() => saveLayout(), 800);
  }
  async function saveLayout(extra) {
    clearTimeout(SU.saveT); SU.saveT = null;
    const c = setupCtrl();
    if (!c) return;
    const body = Object.assign({}, extra || {});
    if (runsValid()) body.runs = SU.runs;
    if (!Object.keys(body).length) return;
    const p = api(url('layout', c.id), body).then((lay) => {
      c.layout = lay;
      sSaved('✓ Saved');
    }).catch((e) => { sSaved('Not saved', 'err'); fail(e); });
    SU.saving = p;
    await p;
  }
  async function flushLayout() {
    if (SU.saveT) await saveLayout();
    else if (SU.saving) await SU.saving;
  }
  $('s-ed').querySelectorAll('.tool [data-tool]').forEach((s) => { s.onclick = () => { SU.tool = s.dataset.tool; renderSetup(); }; });
  $('s-undo').onclick = () => {
    if (!SU.hist.length) return toast('Nothing to undo');
    SU.runs = SU.hist.pop();
    SU.active = Math.min(SU.active, SU.runs.length - 1);
    runsChanged();
  };
  function hitHandle(x, y) {
    let best = null, bd = 30;
    SU.runs.forEach((r, ri) => r.points.forEach((p, pi) => {
      const q = scenes.s.toPx(p), d = Math.hypot(q[0] - x, q[1] - y);
      if (d < bd || (d === bd && ri === SU.active)) { bd = d; best = { ri, pi }; }
    }));
    return best;
  }
  function hitSegment(x, y) {
    const r = SU.runs[SU.active];
    if (!r) return null;
    let best = null, bd = 22;
    for (let i = 1; i < r.points.length; i++) {
      const a = scenes.s.toPx(r.points[i - 1]), b = scenes.s.toPx(r.points[i]);
      const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy || 1;
      const t = clamp(((x - a[0]) * dx + (y - a[1]) * dy) / L2, 0, 1);
      const d = Math.hypot(a[0] + t * dx - x, a[1] + t * dy - y);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
  const ed = $('s-ed');
  ed.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.tool') || !scenes.s) return;
    const R = ed.getBoundingClientRect(), x = e.clientX - R.left, y = e.clientY - R.top;
    const hit = hitHandle(x, y);
    if (hit) {
      pushHist(); SU.active = hit.ri; SU.drag = hit; ed.setPointerCapture(e.pointerId); renderOverlay(); return;
    }
    if (SU.tool === 'draw') {
      pushHist();
      if (!SU.runs.length) SU.runs.push({ name: 'Run 1', points: [], leds: (setupCtrl() || {}).leds || 100, reverse: false });
      const r = SU.runs[SU.active];
      if (r.points.length >= 200) return toast('That run has the most points it can');
      r.points.push(scenes.s.toPic(x, y));
      runsChanged();
    } else {
      const seg = hitSegment(x, y);
      if (seg == null) return;
      pushHist();
      SU.runs[SU.active].points.splice(seg, 0, scenes.s.toPic(x, y));
      SU.drag = { ri: SU.active, pi: seg };
      ed.setPointerCapture(e.pointerId);
      renderOverlay();
    }
  });
  ed.addEventListener('pointermove', (e) => {
    if (!SU.drag) return;
    const R = ed.getBoundingClientRect();
    SU.runs[SU.drag.ri].points[SU.drag.pi] = scenes.s.toPic(e.clientX - R.left, e.clientY - R.top);
    scenes.s.set(setupCtrl(), SU.runs);
    renderOverlay();
  });
  const endDrag = () => { if (!SU.drag) return; SU.drag = null; runsChanged(); };
  ed.addEventListener('pointerup', endDrag);
  ed.addEventListener('pointercancel', endDrag);
  $('s-add').onclick = () => {
    if (SU.runs.length >= 8) return;
    pushHist();
    const used = SU.runs.reduce((a, r) => a + r.leds, 0), total = (setupCtrl() || {}).leds || 100;
    SU.runs.push({ name: 'Run ' + (SU.runs.length + 1), points: [], leds: Math.max(1, total - used) || 50, reverse: false });
    SU.active = SU.runs.length - 1; SU.open = SU.active; SU.tool = 'draw';
    runsChanged();
  };
  $('s-look').querySelectorAll('span').forEach((s) => {
    s.onclick = async () => {
      sSaved('Restyling\u2026', 'wait');
      await saveLayout({ look: s.dataset.look });
      renderSetup();
    };
  });
  $('s-upload').onclick = () => $('s-file').click();
  $('s-file').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    await flushLayout();
    const fd = new FormData();
    fd.append('photo', f);
    fd.append('look', (setupCtrl().layout || {}).look || 'night');
    sSaved('Uploading\u2026', 'wait');
    try {
      setupCtrl().layout = await api(url('upload', SU.cid), fd);
      sSaved('✓ Photo saved');
    } catch (err) { sSaved('Not saved', 'err'); fail(err); }
    renderSetup();
  });
  $('s-rmphoto').onclick = async () => {
    await flushLayout();
    const fd = new FormData(); fd.append('remove', '1');
    try { setupCtrl().layout = await api(url('upload', SU.cid), fd); sSaved('✓ Photo removed'); } catch (err) { fail(err); }
    renderSetup();
  };
  $('s-blink').onclick = async (e) => {
    const b = e.currentTarget;
    if (!runsValid()) return toast('Finish drawing the run first');
    await flushLayout();
    b.classList.add('busy');
    try {
      const r = await api(url('blink', SU.cid), { run: SU.active });
      toast(['Blinking the start of ', { b: SU.runs[SU.active].name }, ' for ' + Math.round(r.seconds || 10) + ' s, then putting it back']);
    } catch (err) { fail(err); }
    setTimeout(() => b.classList.remove('busy'), 3000);
  };

  // ------------------------------------------------------------ animation loop
  let tick = 0;
  function frame() {
    if (document.hidden) return;
    tick++;
    const t = tick / 4;
    const first = selCtrls()[0], st = (first && first.state) || {};
    if (view === 'tonight') {
      scenes.t && scenes.t.draw(st.pattern, st.on, t);
      $('t-insets').querySelectorAll('.inset').forEach((b, i) => {
        const c = selCtrls()[i + 1];
        if (b._scene && c) b._scene.draw(c.state && c.state.pattern, c.state && c.state.on, t);
      });
    } else if (view === 'patterns') {
      scenes.p && scenes.p.draw(st.pattern, st.on, t);
    } else if (view === 'create') {
      const p = previewPattern();
      scenes.c && scenes.c.draw(p, true, t);
      $('c-anims').querySelectorAll('canvas').forEach((cv) => strip(cv, Object.assign({}, p, { animation: cv.dataset.anim }), t, false));
    } else if (view === 'setup' && scenes.s) {
      const c = setupCtrl(), cst = (c && c.state) || {};
      scenes.s.draw(cst.on && cst.pattern ? cst.pattern : { colors: ['#fff1e0'], animation: 'motionless', brightness: 200 }, true, t);
      if (scenes.s.size && SU.svgSize !== scenes.s.size) { SU.svgSize = scenes.s.size; renderOverlay(); }
    }
    if (view === 'tonight' && tick % 20 === 0) renderTimeline(selCtrls());
  }
  setInterval(frame, 250);
  window.addEventListener('resize', () => { if (view === 'setup') renderOverlay(); });
  function busy() { const a = document.activeElement; return SU.drag || !pop.hidden || (a && (a.isContentEditable || a.tagName === 'INPUT')); }
  setInterval(() => { if (!document.hidden && !busy()) refreshState(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState(); });

  fixSel();
  routeFromHash();
})();
