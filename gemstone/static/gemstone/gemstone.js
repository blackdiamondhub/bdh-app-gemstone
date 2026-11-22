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
  // The same script drives the app and its Settings → Light setup page, which only has the setup elements.
  const SETUP = D.mode === 'setup';

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
  // Gap between preview lights, as a share of the picture's width.
  const DOT_GAP = 1 / 30;
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
  function toast(parts, err) {
    const t = $('gs-toast'), tx = $('gs-toast-text');
    tx.textContent = '';
    (Array.isArray(parts) ? parts : [parts]).forEach((p) => {
      if (p && typeof p === 'object') tx.appendChild(h('b', '', p.b));
      else tx.appendChild(document.createTextNode(String(p)));
    });
    t.classList.toggle('err', !!err);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
  }
  const fail = (e) => toast(e.message || String(e), true);

  const pop = $('gs-pop');
  function openPop(anchor, build, cls) {
    pop.className = 'pop' + (cls ? ' ' + cls : '');
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
      // Blurred copy of the photo filling the bars beside a whole-photo view (like the screensaver).
      this.blur = h('div', 'scn blur');
      [this.blur, this.bg, this.pic, this.fx].forEach((e) => { e.style.position = 'absolute'; e.style.pointerEvents = 'none'; });
      this.bg.style.inset = this.fx.style.inset = '0';
      this.bg.style.width = this.fx.style.width = '100%';
      this.bg.style.height = this.fx.style.height = '100%';
      this.blur.hidden = true;
      el.prepend(this.blur, this.bg, this.pic, this.fx);
      this.key = '';
      this.size = '';
    }
    set(ctrl, runs) {
      const lay = (ctrl && ctrl.layout) || { photo: false, runs: [] };
      this.ctrl = ctrl;
      this.photo = !!(ctrl && lay.photo);
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
          this.blur.style.backgroundImage = 'url("' + photoUrl(ctrl) + '")';
        } else {
          this.blur.style.backgroundImage = '';
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
      // Photos are always shown whole: filling the box when it's close to the
      // photo's shape, otherwise letterboxed over a blurred copy.
      const r = w / hh / A;
      const fill = this.photo && !this.opts.contain && r > 0.9 && r < 1.11;
      this.blur.hidden = !this.photo || this.opts.contain || fill;
      if (this.opts.contain || (this.photo && !fill)) s = contain;
      else if (fill) s = cover;
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
      // Thumbnails zoom to the lights: every light in the box, letterboxed with the background.
      if (this.opts.crop) {
        const pts = [];
        this.runs.forEach((r2) => r2.points.forEach((p) => pts.push(p)));
        Object.assign(this, window.GemstoneCore.fitBox(pts, A, w, hh, 0.08));
        this.blur.hidden = true;
      }
      Object.assign(this.pic.style, { left: this.ox + 'px', top: this.oy + 'px', width: this.pw + 'px', height: this.ph + 'px' });
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      [this.bg, this.fx].forEach((c) => { c.width = Math.max(1, Math.round(w * dpr)); c.height = Math.max(1, Math.round(hh * dpr)); });
      this.dpr = dpr;
      const g = this.bg.getContext('2d');
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, hh);
      for (let i = 0; i < (this.photo ? 0 : 70); i++) {
        g.fillStyle = 'rgba(255,255,255,' + (0.15 + rnd(i + 3) * 0.5).toFixed(2) + ')';
        const r = 0.6 + rnd(i + 9) * 1.2;
        g.beginPath(); g.arc(rnd(i) * w, rnd(i + 99) * hh * 0.6, r, 0, 7); g.fill();
      }
      this.dots = this.layoutDots();
    }
    toPx(p) { return [this.ox + p[0] * this.pw, this.oy + p[1] * this.ph]; }
    toPic(x, y) { return [clamp((x - this.ox) / this.pw, 0, 1), clamp((y - this.oy) / this.ph, 0, 1)]; }
    layoutDots() {
      // One dot per light, evenly spaced along the drawn paths. Spacing is a
      // share of the picture's width so a scene looks the same at any size;
      // the pattern flows on from one run into the next.
      const out = [];
      const gap = Math.max(this.opts.small ? 5 : 9, this.pw * DOT_GAP);
      let offset = 0;
      this.runs.forEach((run) => {
        const pts = run.points.map((p) => this.toPx(p));
        if (pts.length < 2) return;
        const segs = []; let L = 0;
        for (let i = 1; i < pts.length; i++) { const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); segs.push(d); L += d; }
        const n = Math.max(2, Math.round(L / gap));
        let si = 0, acc = 0;
        for (let j = 0; j < n; j++) {
          const d = ((j + 0.5) / n) * L;
          while (si < segs.length - 1 && acc + segs[si] < d) { acc += segs[si]; si++; }
          const k = segs[si] ? (d - acc) / segs[si] : 0;
          const a = pts[si], b = pts[si + 1];
          out.push([a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, offset + j]);
        }
        offset += n;
      });
      return out;
    }
    draw(pattern, on, t) {
      if (!this.el.offsetParent) return;
      this.fit();
      // Motionless or off scenes look the same every tick; don't repaint them.
      const still = !on || !pattern || (pattern.animation || 'motionless') === 'motionless';
      const sig = still ? this.size + '|' + (on ? JSON.stringify(pattern) : 'off') : '';
      if (still && sig === this.drawn) return;
      this.drawn = sig;
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
  function strip(canvas, p, t) {
    const w = canvas.width, hh = canvas.height, g = canvas.getContext('2d');
    g.clearRect(0, 0, w, hh);
    const n = 13, y = hh * 0.5;
    for (let j = 0; j < n; j++) {
      const x = w * (0.06 + 0.88 * (j + 0.5) / n);
      const [c, a] = colAt(j * 3, p, t);
      g.fillStyle = rgba(c, 0.3 * a);
      g.beginPath(); g.arc(x, y, hh * 0.11, 0, 7); g.fill();
      g.fillStyle = rgba(c, Math.min(1, 0.25 + a));
      g.beginPath(); g.arc(x, y, hh * 0.045, 0, 7); g.fill();
    }
  }

  // ------------------------------------------------------------ data
  let OURS = null, LIB = null, oursP = null;
  async function loadOurs(force) {
    if (OURS && !force) return OURS;
    // Tonight asks from several places at once: share one request.
    if (!oursP || force) { const p = api(url('ours')).finally(() => { if (oursP === p) oursP = null; }); oursP = p; }
    OURS = await oursP;
    return OURS;
  }
  async function loadLib() {
    if (!LIB) LIB = (await api(url('library'))).patterns || [];
    return LIB;
  }
  // Writes reach the lights a couple of seconds after a tap (the cloud needs a
  // gap between commands), so the screen changes straight away and the write
  // goes out behind it. While any write is out, polled state is ignored so it
  // can't flip the screen back to what the lights showed before the tap.
  let pending = 0, stateT = null;
  async function refreshState() {
    if (pending) return;
    try {
      const s = await api(url('state'));
      if (pending) return;
      S = s;
      fixSel();
      renderView();
    } catch (e) { /* keep the last state */ }
  }
  function refreshSoon(ms) { clearTimeout(stateT); stateT = setTimeout(refreshState, ms); }
  // Show ``apply`` now, send ``send`` in the background, put things back if it fails.
  async function optimistic(list, apply, send) {
    const before = list.map((c) => [c, c.state]);
    apply();
    renderView();
    pending++;
    try { return await send(); } catch (e) {
      before.forEach(([c, st]) => { c.state = st; });
      renderView();
      fail(e);
      return null;
    } finally {
      pending--;
      if (!pending) refreshSoon(2000);
    }
  }

  async function play(ref, preview) {
    const ids = sel.slice(), list = selCtrls();
    const name = (preview && preview.name) || ref.name || 'pattern';
    toast(['Playing ', { b: name }, ' on ' + namesOf(list)]);
    await optimistic(list,
      () => list.forEach((c) => { c.state = { on: true, pattern: preview || (c.state && c.state.pattern) }; }),
      () => api(url('play'), { ids, pattern: ref }));
  }

  // ------------------------------------------------------------ lights switch (off = preview on the picture only)
  // The switch mirrors the real lights: it is on when any selected controller is on.
  let PV = null; // { ref, pat } previewed on the picture while the lights are off
  const lightsOn = () => selCtrls().some((c) => c.state && c.state.on);
  function choose(ref, pat) {
    if (lightsOn()) { PV = null; play(ref, pat); return; }
    PV = { ref, pat };
    renderView();
  }
  async function setLights(on) {
    const list = selCtrls(), ids = sel.slice();
    if (!list.length) return;
    if (on && view === 'create') {
      const body = { ids, pattern: spec() }, pat = previewPattern();
      toast(['Live: changes show on ', { b: namesOf(list) }]);
      await optimistic(list, () => list.forEach((c) => { c.state = { on: true, pattern: pat }; }),
        () => api(url('live'), body));
    } else if (on && PV) {
      const p = PV; PV = null;
      await play(p.ref, p.pat);
    } else {
      toast([on ? 'Turning on ' : 'Turning off ', { b: namesOf(list) }]);
      await optimistic(list, () => list.forEach((c) => { c.state = Object.assign({}, c.state, { on }); }),
        () => api(url('power'), { ids, on }));
    }
  }
  function renderLights() {
    const on = lightsOn();
    root.querySelectorAll('[data-lights]').forEach((b) => {
      b.classList.toggle('on', on);
      b.querySelector('.g-st').textContent = on ? 'LIVE' : 'PREVIEW';
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    ['t-hero', 'c-hero'].forEach((id) => {
      const el = $(id);
      el.classList.toggle('g-lv', on);
      el.classList.toggle('g-pvw', !on && (id === 'c-hero' || !!PV));
    });
  }
  if (!SETUP) root.querySelectorAll('[data-lights]').forEach((b) => { b.onclick = () => setLights(!lightsOn()); });
  // What the Tonight / Patterns pictures show: the preview while the lights are off, else the real lights.
  function shown(c) {
    if (PV && !lightsOn()) return { pattern: PV.pat, on: true, preview: true };
    const st = (c && c.state) || {};
    return { pattern: st.pattern, on: !!st.on, preview: false };
  }
  function heroText(list, nameId, subId, aId) {
    const first = list[0], s = shown(first);
    if (aId) $(aId).textContent = s.preview ? 'Preview' : 'Now playing';
    if (s.preview) {
      $(nameId).textContent = s.pattern.name || 'Preview';
      $(subId).textContent = 'On the picture only · real lights unchanged';
      return;
    }
    $(nameId).textContent = first ? (s.on ? ((s.pattern && s.pattern.name) || 'On') : 'Lights off') : '—';
    $(subId).textContent = statusLine(list);
  }
  const playPattern = (p) => choose({ id: p.id }, p);
  const SOLIDS = [['Warm white', '#fff1e0'], ['White', '#ffffff'], ['Red', '#e3262b'], ['Orange', '#ff7a1a'], ['Gold', '#ffb800'],
    ['Green', '#1fa64a'], ['Teal', '#14b8a6'], ['Blue', '#2f6bff'], ['Purple', '#8b3dff'], ['Pink', '#ff4fa3']];
  // Must match service.solid_id: the cloud only takes UUID pattern ids.
  const solidId = (hex) => 'bd450000-0000-4000-8000-000000' + hex.slice(1).toLowerCase();
  const solidPat = (name, hex) => ({ id: solidId(hex), name, colors: [hex], animation: 'motionless', brightness: 255, speed: 0, direction: 0 });

  // ------------------------------------------------------------ cards
  function card(p, opts) {
    opts = opts || {};
    const c = h('div', 'g-card');
    c.setAttribute('role', 'button');
    const pv = h('div', 'pv'); const cv = h('canvas');
    cv.width = 540; cv.height = 224; cv.style.objectFit = 'cover';
    pv.appendChild(cv); c.appendChild(pv);
    strip(cv, p, 1.3);
    const tx = h('div', 'g-tx');
    const fav = isFav(p);
    const ht = h('button', 'g-ht' + (fav ? ' g-fav' : ''), fav ? '♥' : '♡');
    ht.setAttribute('aria-label', fav ? 'Remove from favourites' : 'Add to favourites');
    ht.setAttribute('aria-pressed', fav ? 'true' : 'false');
    ht.onclick = (e) => { e.stopPropagation(); ht.classList.add('g-pop'); setFav(p, !isFav(p)); };
    const nm = h('div', 'nm', p.name);
    nm.appendChild(h('span', 'an', pretty(p.animation || 'motionless')));
    tx.append(ht, nm);
    c.appendChild(tx);
    const first = selCtrls()[0];
    if (PV && !lightsOn() && PV.pat.id === p.id) {
      c.classList.add('g-pvw'); c.appendChild(h('span', 'pl g-pvb', 'PREVIEW'));
    } else if (first && first.state && first.state.on && first.state.pattern && first.state.pattern.id === p.id) {
      c.classList.add('on'); c.appendChild(h('span', 'pl', 'ON LIGHTS'));
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
    b.onclick = () => choose({ kind: 'solid', color: hex, name }, solidPat(name, hex));
    return b;
  }

  // ------------------------------------------------------------ views
  const VIEWS = ['tonight', 'create'];
  let view = SETUP ? 'setup' : 'tonight';
  const scenes = {};
  const insets = new Map();
  function heroScene(key, el, opts) {
    if (!scenes[key]) scenes[key] = new Scene(el, opts);
    return scenes[key];
  }
  function routeFromHash() {
    let v = (location.hash || '').replace('#', '');
    // The old Patterns page is now the "All patterns" part of Tonight.
    const toAll = v === 'patterns';
    if (!VIEWS.includes(v)) v = 'tonight';
    view = v;
    closePop();
    renderView();
    if (toAll) requestAnimationFrame(scrollToAll);
  }
  if (!SETUP) window.addEventListener('hashchange', routeFromHash);

  function renderShell() {
    const who = $('gs-who');
    who.textContent = S.admin ? 'Admin' : 'Guest';
    who.classList.toggle('admin', !!S.admin);
    root.querySelectorAll('.ri').forEach((a) => a.classList.toggle('on', a.dataset.view === view));
    VIEWS.forEach((v) => { $('v-' + v).hidden = v !== view; });
    renderMsg();
  }
  function renderMsg() {
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
    if (SETUP) {
      renderMsg();
      $('v-setup').hidden = !S.configured || !ctrls().length;
      renderSetup();
      return;
    }
    renderShell();
    renderChips();
    renderLights();
    if (view === 'tonight') { renderTonight(); syncPinned(); }
    else if (view === 'create') renderCreate();
    if (view !== 'tonight') setPinned(false);
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
    heroText(list, 't-name', 't-sub', 't-a');
    // insets for the other selected controllers
    // Insets (and their canvases) are kept per controller and only updated,
    // so re-rendering doesn't pile up canvases on the display.
    const ins = $('t-insets'), want = list.slice(1, 4);
    const keep = new Set(want.map((c) => c.id));
    insets.forEach((v, id) => { if (!keep.has(id)) insets.delete(id); });
    want.forEach((c, i) => {
      let it = insets.get(c.id);
      if (!it) {
        const b = h('button', 'inset');
        const pic = h('div', 'pic'); b.appendChild(pic);
        const lb = h('div', 'lb'), tx = h('div'), nm = h('b'), sub = h('small'), dot = h('span', 'dot');
        tx.append(nm, sub); lb.append(tx, dot); b.appendChild(lb);
        b.onclick = () => { sel = [c.id].concat(sel.filter((x) => x !== c.id)); saveSel(); renderView(); };
        b._scene = new Scene(pic, { small: true });
        it = { b, nm, sub, dot };
        insets.set(c.id, it);
      }
      const on = !!(c.state && c.state.on);
      it.nm.textContent = c.name;
      it.sub.textContent = on ? ((c.state.pattern && c.state.pattern.name) || 'On') : 'Off';
      it.dot.style.background = on ? '#3ddc84' : '';
      it.dot.style.boxShadow = on ? '0 0 8px #3ddc84' : '';
      it.b._scene.set(c);
      if (ins.children[i] !== it.b) ins.insertBefore(it.b, ins.children[i] || null);
    });
    while (ins.children.length > want.length) ins.lastChild.remove();
    const anyOn = list.some((c) => c.state && c.state.on);
    if (bright == null || !brightTimer) {
      const p = st.pattern;
      setSlider($('t-bslider'), p ? Math.round(p.brightness * 100 / 255) : 100);
    }
    setSlider($('m-bslider'), sliders.get($('t-bslider')).value);
    ['t-bslider', 'm-bslider'].forEach((id) => $(id).classList.toggle('dis', !anyOn));
    $('t-bval').textContent = $('m-bval').textContent = anyOn ? sliders.get($('t-bslider')).value + '%' : '—';
    heroText(list, 'm-name', 'm-sub', 'm-k');
    renderMiniScene(first);
    renderMiniDots();
    renderSched();
    renderEvent();
    renderFavs();
    renderAll();
  }

  // Tonight's schedule: Autopilot's sunset-relative on time and its off time.
  const OFF_MIN = -120, OFF_MAX = 120, OFF_STEP = 5, GUEST_LAST = '22:00', NIGHT_ENDS = '06:00';
  const atToday = (t) => { const [hh, mm] = t.split(':').map(Number); const d = new Date(); d.setHours(hh, mm, 0, 0); return d; };
  const signed = (n) => (n < 0 ? '−' : '+') + Math.abs(n);
  function onLabel(sc) {
    if (sc.on_time === 'sunset') return '🌅 Sunset' + (sc.on_offset ? ' ' + signed(sc.on_offset) : '');
    if (sc.on_time === 'sunrise') return '🌄 Sunrise' + (sc.on_offset ? ' ' + signed(sc.on_offset) : '');
    return '🕔 ' + fmtTime(atToday(sc.on_time));
  }
  // When tonight's lights go off: an off time before morning is after midnight.
  function offAt(sc) {
    if (!/^\d\d:\d\d$/.test(sc.off_time || '')) return null;
    const d = atToday(sc.off_time), now = new Date();
    const late = sc.off_time < NIGHT_ENDS, early = hhmm(now) < NIGHT_ENDS;
    if (late && !early) d.setDate(d.getDate() + 1);
    if (!late && early) d.setDate(d.getDate() - 1);
    return d;
  }
  function leftTxt(ms) {
    const m = Math.max(1, Math.round(ms / 60e3)), hh = Math.floor(m / 60);
    return (hh ? hh + 'h ' : '') + (m % 60) + 'm';
  }
  function renderSched() {
    const sc = S.schedule, b = $('t-sched');
    b.hidden = !sc;
    if (!sc) return;
    b.classList.toggle('off', !!sc.dark);
    if (sc.dark) {
      $('t-sched-a').textContent = '🌙 Schedule off ▾';
      $('t-sched-b').textContent = "Lights won't come on";
      return;
    }
    const off = offAt(sc);
    $('t-sched-a').textContent = onLabel(sc) + ' → ' + (off ? fmtTime(off) : '—') + ' ▾';
    const now = new Date(), on = sc.on_at ? atToday(sc.on_at) : null;
    let sub;
    if (!sc.autopilot) sub = 'Autopilot is off';
    else if (off && now >= off) sub = 'Off for the night';
    else if (on && now < on && hhmm(now) >= NIGHT_ENDS) sub = 'Comes on at ' + fmtTime(on);
    else if (off) sub = 'On · off in ' + leftTxt(off - now);
    else sub = 'On';
    $('t-sched-b').textContent = sub;
  }
  // Guests: off strictly after tonight's on time (or the fallback without a sunset), by GUEST_LAST.
  const core = window.GemstoneCore;
  const nightMin = core.nightMin;
  const guestAfter = (sc) => core.guestAfter(sc, S.guest_fallback);
  const offOptions = (sc) => core.offTimeOptions(sc, { admin: !!S.admin, guestFallback: S.guest_fallback });
  function buildSched(p) {
    const sc = S.schedule;
    const hd = h('div', 'g-schh');
    hd.appendChild(h('b', '', 'Schedule'));
    const tg = h('button', 'g-tg' + (sc.dark ? '' : ' on'));
    tg.setAttribute('aria-label', sc.dark ? 'Schedule off tonight' : 'Schedule on');
    tg.appendChild(h('span', 'g-sw'));
    tg.onclick = () => setSchedule({ dark: !sc.dark });
    hd.appendChild(tg);
    p.appendChild(hd);
    if (sc.dark) {
      p.appendChild(h('div', 'g-schn', "Lights stay off tonight. They follow the schedule again tomorrow, or when someone turns them on."));
      return;
    }
    if (!sc.autopilot) p.appendChild(h('div', 'g-schn', 'Autopilot is off, so the lights only come on by hand.'));
    const r1 = h('div', 'g-schr');
    r1.appendChild(h('span', 'g-schl', 'On at sunset'));
    const st = h('div', 'g-step');
    const minus = h('button', '', '−'), plus = h('button', '', '+');
    st.append(minus, h('span', '', (sc.on_offset ? signed(sc.on_offset) : '0') + ' min'), plus);
    minus.disabled = sc.on_offset <= OFF_MIN;
    plus.disabled = sc.on_offset >= OFF_MAX;
    minus.onclick = () => nudgeOffset(-OFF_STEP);
    plus.onclick = () => nudgeOffset(OFF_STEP);
    r1.appendChild(st);
    p.appendChild(r1);
    if (sc.on_at) p.appendChild(h('div', 'g-schs', 'Today: lights on at ' + fmtTime(atToday(sc.on_at))));
    const r2 = h('div', 'g-schr');
    r2.appendChild(h('span', 'g-schl', 'Off at'));
    const s = h('select', 'g-sel');
    const opts = offOptions(sc);
    opts.forEach((t) => {
      const o = h('option', '', fmtTime(atToday(t)) + (t < NIGHT_ENDS ? ' (after midnight)' : ''));
      o.value = t;
      if (t === sc.off_time) o.selected = true;
      s.appendChild(o);
    });
    s.disabled = !opts.length;
    if (!opts.length) s.appendChild(h('option', '', 'No times left before ' + fmtTime(atToday(GUEST_LAST))));
    s.onchange = () => setSchedule({ off_time: s.value });
    r2.appendChild(s);
    p.appendChild(r2);
    if (!S.admin) p.appendChild(h('div', 'g-schs', 'Guests: lights off after they come on, by '
      + fmtTime(atToday(GUEST_LAST)) + ' at the latest'));
  }
  function redrawSched() {
    if (!pop.hidden && pop.classList.contains('g-schp')) { pop.textContent = ''; buildSched(pop); }
  }
  let nudgeT = null, nudgeFrom = null;
  function nudgeOffset(d) {
    const sc = S.schedule;
    if (nudgeFrom == null) nudgeFrom = clone(sc);
    sc.on_offset = clamp(sc.on_offset + d, OFF_MIN, OFF_MAX);
    if (sc.on_time !== 'sunrise') sc.on_time = 'sunset';
    if (sc.sunset) { const t = atToday(sc.sunset); t.setMinutes(t.getMinutes() + sc.on_offset); sc.on_at = hhmm(t); }
    // A guest's off time must stay after the new on time: snap to the first valid option.
    if (!S.admin && sc.off_time && nightMin(sc.off_time) <= nightMin(guestAfter(sc))) {
      const first = offOptions(sc)[0];
      if (first) sc.off_time = first;
    }
    renderSched(); redrawSched();
    clearTimeout(nudgeT);
    nudgeT = setTimeout(() => {
      const prev = nudgeFrom; nudgeFrom = null;
      const body = { on_offset: sc.on_offset };
      if (sc.off_time !== prev.off_time) body.off_time = sc.off_time;
      setSchedule(body, prev);
    }, 700);
  }
  async function setSchedule(body, prev) {
    prev = prev || clone(S.schedule);
    const before = ctrls().map((c) => [c, c.state]);
    if ('off_time' in body) { S.schedule.off_time = body.off_time; toast(['Lights go off at ', { b: fmtTime(atToday(body.off_time)) }]); }
    if ('on_offset' in body && S.schedule.on_at) toast(['Lights come on at ', { b: fmtTime(atToday(S.schedule.on_at)) }]);
    if ('dark' in body) {
      S.schedule.dark = body.dark ? { until: '' } : null;
      if (body.dark) ctrls().forEach((c) => { c.state = Object.assign({}, c.state, { on: false }); });
      toast(body.dark ? "Lights stay off tonight" : 'Schedule back on');
    }
    renderView(); redrawSched();
    pending++;
    try {
      const r = await api(url('schedule'), body);
      S.schedule = r.schedule;
    } catch (e) {
      S.schedule = prev;
      before.forEach(([c, st]) => { c.state = st; });
      fail(e);
    } finally { pending--; renderView(); redrawSched(); if (!pending) refreshSoon(2000); }
  }

  // Autopilot's event tonight (a holiday or a game): its patterns, folded away until asked for.
  // Whether it's open is remembered across reloads.
  const EV_KEY = 'gemstone.eventBar.expanded';
  let evOpen = false;
  try { evOpen = localStorage.getItem(EV_KEY) === '1'; } catch (e) { /* private mode */ }
  function renderEvent() {
    const ev = S.tonight_event, box = $('t-ev');
    box.hidden = !ev;
    if (!ev) return;
    $('t-ev-name').textContent = (ev.emoji ? ev.emoji + ' ' : '') + ev.name;
    const pats = ev.patterns || [];
    const dots = $('t-ev-dots'), cols = [];
    pats.forEach((p) => (p.colors || []).forEach((c) => { if (!cols.includes(c)) cols.push(c); }));
    if (dots._key !== cols.join()) {
      dots._key = cols.join(); dots.textContent = '';
      cols.slice(0, 5).forEach((c) => { const i = h('i'); i.style.background = c; dots.appendChild(i); });
    }
    $('t-ev-n').textContent = pats.length + (pats.length === 1 ? ' pattern' : ' patterns');
    $('t-ev-x').textContent = evOpen ? 'Hide ▴' : 'Show ▾';
    $('t-evh').setAttribute('aria-expanded', evOpen ? 'true' : 'false');
    const row = $('t-evrow');
    row.hidden = !evOpen;
    if (!evOpen) return;
    const first = selCtrls()[0], fs = (first && first.state) || {};
    const key = JSON.stringify([ev.id, pats.map((p) => p.id), PV && !lightsOn() ? PV.pat.id : null,
      fs.on && fs.pattern ? fs.pattern.id : null]);
    if (row._key === key && row.children.length) return;
    row._key = key;
    row.textContent = '';
    pats.forEach((p) => row.appendChild(card(p)));
  }
  // Favourites strip: hearted patterns only.
  async function renderFavs() {
    const row = $('t-favs');
    if (!S.configured || !ctrls().length) { row.textContent = ''; return; }
    let pats = [];
    try {
      pats = (await loadOurs()).patterns.filter((p) => p.fav);
    } catch (e) { row.textContent = ''; row.appendChild(h('div', 'empty', e.message)); return; }
    if (view !== 'tonight') return;
    if (!pats.length) {
      if (row._key !== 'empty') { row._key = 'empty'; row.textContent = ''; row.appendChild(h('div', 'empty', 'No favourites yet — tap ♡ on any pattern below.')); }
      return;
    }
    fillCards(row, pats);
  }
  // Only rebuild the cards when what they show has changed.
  function cardsKey(pats) {
    const first = selCtrls()[0], fs = (first && first.state) || {};
    return JSON.stringify([pats.map((p) => [p.id, p.name, p.animation, p.colors, isFav(p)]),
      PV && !lightsOn() ? PV.pat.id : null, fs.on && fs.pattern ? fs.pattern.id : null]);
  }
  function fillCards(el, pats, empty) {
    const key = cardsKey(pats) + (empty || '');
    if (el._key === key && el.children.length) return;
    el._key = key;
    // Swap in one go: an emptied grid would shrink the page and clamp its scroll for a frame.
    if (!pats.length && empty) { el.replaceChildren(h('div', 'empty', empty)); return; }
    el.replaceChildren(...pats.map((p) => card(p)));
  }

  // ------------------------------------------------------------ favourites (the ♡ on every card)
  const favOver = new Map(); // id -> fav, shown until the saved list catches up
  function isFav(p) {
    if (favOver.has(p.id)) return favOver.get(p.id);
    const mine = OURS && OURS.patterns.find((q) => q.id === p.id);
    return !!(mine ? mine.fav : p.fav);
  }
  async function setFav(p, fav) {
    const saved = OURS && OURS.patterns.some((q) => q.id === p.id);
    favOver.set(p.id, fav);
    renderView();
    pending++;
    try {
      await api(url('fav'), { id: p.id, fav });
      toast(fav ? (saved ? ['Added ', { b: p.name }, ' to favourites'] : ['Saved ', { b: p.name }, ' to your patterns ♥'])
        : ['Removed ', { b: p.name }, ' from favourites']);
      await loadOurs(true);
    } catch (e) { fail(e); }
    finally { favOver.delete(p.id); pending--; renderView(); if (!pending) refreshSoon(2000); }
  }

  // ------------------------------------------------------------ all patterns (chips + grid) and the library
  let cat = '';
  async function renderAll() {
    const grid = $('t-grid'), chips = $('t-chips');
    if (!S.configured || !ctrls().length) { grid.textContent = ''; chips.textContent = ''; $('t-count').textContent = ''; return; }
    let o;
    try { o = await loadOurs(); } catch (e) { grid.textContent = ''; grid.appendChild(h('div', 'empty', e.message)); return; }
    if (view !== 'tonight') return;
    const folders = o.folders.filter((f) => f.count || f.guest);
    if (cat && cat !== 'solid' && !folders.some((f) => f.id === cat)) cat = '';
    const list = [{ id: '', name: 'All', count: o.patterns.length }].concat(folders,
      [{ id: 'solid', name: 'Solid colours', count: SOLIDS.length }]);
    const ckey = JSON.stringify([cat, list.map((f) => [f.id, f.name, f.count, !!f.guest])]);
    if (chips._key !== ckey) {
      chips._key = ckey;
      chips.textContent = '';
      list.forEach((f) => {
        const b = h('button', 'g-chip' + (cat === f.id ? ' on' : ''), (f.guest ? '🔒 ' : '') + f.name);
        if (f.count != null) b.appendChild(h('em', '', String(f.count)));
        b.onclick = () => pickCat(f.id);
        chips.appendChild(b);
      });
    }
    $('t-count').textContent = o.patterns.length + (o.patterns.length === 1 ? ' pattern' : ' patterns');
    if (cat === 'solid') {
      if (grid._key !== 'solid') {
        grid._key = 'solid'; grid.textContent = '';
        SOLIDS.forEach(([n, c]) => grid.appendChild(solidTile(n, c)));
      }
    } else {
      fillCards(grid, o.patterns.filter((p) => !cat || p.folder === cat), 'Nothing here yet — make one in Create');
    }
    renderLib();
  }
  // Picking a chip keeps the header where it is when it's stuck, rather than jumping to the top.
  function pickCat(id) {
    cat = id;
    renderAll();
    const sc = $('v-tonight'), stk = $('t-stk');
    const top = sc.getBoundingClientRect().top + (pinned ? miniH() : 0);
    if (stk.getBoundingClientRect().top <= top + 1) scrollToAll();
  }
  // Scroll Tonight so the All patterns header sits at its stuck position.
  function scrollToAll() {
    const sc = $('v-tonight'), all = $('t-all');
    sc.scrollTop += all.getBoundingClientRect().top - sc.getBoundingClientRect().top - miniH();
  }
  let libOpen = false, libErr = '';
  async function renderLib() {
    const box = $('t-lib');
    $('t-libx').textContent = libOpen ? 'Hide ▴' : 'Show ▾';
    $('t-libh').setAttribute('aria-expanded', libOpen ? 'true' : 'false');
    box.hidden = !libOpen;
    if (!libOpen) return;
    let o, lib;
    try { [o, lib] = await Promise.all([loadOurs(), loadLib()]); libErr = ''; } catch (e) {
      libErr = e.message; box.textContent = ''; box._key = ''; box.appendChild(h('div', 'empty', libErr)); return;
    }
    if (view !== 'tonight' || !libOpen) return;
    const mine = new Set(o.patterns.map((p) => p.id));
    fillCards(box, lib.filter((p) => !mine.has(p.id)), "You've saved every library pattern");
  }
  if (!SETUP) {
    $('t-libh').onclick = () => {
      libOpen = !libOpen;
      if (libOpen) { const box = $('t-lib'); box._key = ''; box.textContent = ''; box.appendChild(h('div', 'empty', 'Loading the library…')); }
      renderLib();
    };
  }

  // ------------------------------------------------------------ pinned now-playing bar
  // Slides in when the hero scrolls away; the sticky chips header then sits below it.
  const MINI_H = 132;
  const miniH = () => $('g-mini').offsetHeight || MINI_H;
  let pinned = false;
  function setPinned(on) {
    if (pinned === on) return;
    pinned = on;
    $('g-mini').classList.toggle('g-show', on);
    $('g-mini').setAttribute('aria-hidden', on ? 'false' : 'true');
    $('v-tonight').classList.toggle('g-pinned', on);
  }
  // The observer below only fires on a change, so coming back to Tonight already
  // scrolled down (the hero was out of view the whole time) needs a manual check.
  function syncPinned() {
    if (SETUP || view !== 'tonight') return;
    const vt = $('v-tonight'), hero = $('t-hero');
    setPinned(window.GemstoneCore.heroHidden(hero.getBoundingClientRect(), vt.getBoundingClientRect(), MINI_H + 8));
  }
  // The bar's picture: the hero's scene zoomed to fit every light (fitBox), or
  // the card strip when there's no photo layout to show.
  const miniPhoto = (c) => !!(c && c.layout && c.layout.photo && (c.layout.runs || []).some((r) => r.points.length));
  function renderMiniScene(first) {
    const ph = $('m-ph');
    let cv = ph.querySelector('canvas.g-mstrip');
    if (!cv) { cv = h('canvas', 'g-mstrip'); cv.width = 600; cv.height = 220; ph.appendChild(cv); }
    const photo = miniPhoto(first);
    cv.hidden = photo;
    if (photo) {
      const sc = heroScene('m', ph, { crop: true, small: true });
      sc.set(first);
      [sc.blur, sc.bg, sc.pic, sc.fx].forEach((e) => { e.hidden = false; });
      sc.blur.hidden = true;
      if (pinned) sc.fit();
    } else if (scenes.m) {
      [scenes.m.blur, scenes.m.bg, scenes.m.pic, scenes.m.fx].forEach((e) => { e.hidden = true; });
    }
  }
  function renderMiniDots() {
    const s = shown(selCtrls()[0]), dots = $('m-dots');
    const cols = ((s.on && s.pattern && s.pattern.colors) || []).slice(0, 8);
    if (dots._key === cols.join()) return;
    dots._key = cols.join(); dots.textContent = '';
    cols.forEach((c) => { const i = h('i'); i.style.background = c; dots.appendChild(i); });
  }
  if (!SETUP) {
    const vt = $('v-tonight');
    // A popover is placed against its button; it'd be left behind by a scroll.
    vt.addEventListener('scroll', () => { if (!pop.hidden) closePop(); }, { passive: true });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => setPinned(!e.isIntersecting && view === 'tonight')),
        { root: vt, rootMargin: '-' + (MINI_H + 8) + 'px 0px 0px 0px' }).observe($('t-hero'));
      new IntersectionObserver((es) => es.forEach((e) => $('t-stk').classList.toggle('g-stuck', e.intersectionRatio < 1)),
        { root: vt, threshold: [1], rootMargin: '-1px 0px 0px 0px' }).observe($('t-stk'));
    }
    // Drag the horizontal strips with a mouse; a drag isn't a tap.
    root.querySelectorAll('.g-drag').forEach((el) => {
      let down = false, x0 = 0, s0 = 0, moved = false;
      el.addEventListener('mousedown', (e) => { down = true; moved = false; x0 = e.pageX; s0 = el.scrollLeft; });
      window.addEventListener('mousemove', (e) => {
        if (!down) return;
        const dx = e.pageX - x0;
        if (Math.abs(dx) > 5) moved = true;
        if (moved) el.scrollLeft = s0 - dx;
      });
      window.addEventListener('mouseup', () => { down = false; });
      el.addEventListener('click', (e) => { if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; } }, true);
      el.addEventListener('wheel', (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && el.scrollWidth > el.clientWidth) { el.scrollLeft += e.deltaY; e.preventDefault(); }
      }, { passive: false });
    });
  }
  if (!SETUP) {
  $('t-sched').onclick = (e) => {
    if (!S.schedule) return;
    openPop(e.currentTarget, buildSched, 'g-schp');
  };
  $('t-evh').onclick = () => {
    evOpen = !evOpen;
    try { localStorage.setItem(EV_KEY, evOpen ? '1' : '0'); } catch (e) { /* private mode */ }
    renderEvent();
  };
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
  // The pinned bar's slider and the main one are the same control.
  const onBright = (src) => (v) => {
    $('t-bval').textContent = $('m-bval').textContent = Math.max(1, v) + '%'; bright = v;
    ['t-bslider', 'm-bslider'].forEach((id) => { if ($(id) !== src) setSlider($(id), v); });
  };
  const commitBright = (v) => {
      clearTimeout(brightTimer);
      brightTimer = setTimeout(async () => {
        const pct = Math.max(1, v), list = selCtrls();
        const old = list.map((c) => [c, c.state && c.state.pattern && c.state.pattern.brightness]);
        list.forEach((c) => { if (c.state && c.state.pattern) c.state.pattern.brightness = Math.round(pct * 255 / 100); });
        brightTimer = null;
        pending++;
        try {
          await api(url('brightness'), { ids: sel, brightness: pct });
        } catch (e) {
          old.forEach(([c, b]) => { if (b != null && c.state && c.state.pattern) c.state.pattern.brightness = b; });
          fail(e);
          renderView();
        } finally { pending--; if (!pending) refreshSoon(2000); }
      }, 300);
    };
  if (!SETUP) ['t-bslider', 'm-bslider'].forEach((id) => makeSlider($(id), onBright($(id)), commitBright));

  // ------------------------------------------------------------ create
  const QUICK = [['Red', '#e3262b'], ['Green', '#1fa64a'], ['Warm white', '#fdf4bc'], ['Gold', '#ffb800'], ['Blue', '#2f6bff'],
    ['Purple', '#8b3dff'], ['Orange', '#ff7a1a'], ['Pink', '#ff4fa3'], ['White', '#ffffff'], ['Teal', '#14b8a6']];
  const C = { design: null, id: '', folder: null, selIdx: 0, more: false, dirty: false,
    saving: false, again: false, saveT: null, liveT: null, guestName: '' };
  function newDesign() {
    C.design = { colors: ['#e3262b', '#1fa64a', '#fdf4bc', '#ffb800'], animation: 'starry', brightness: 100, speed: 128, direction: 0, name: 'Untitled design' };
    C.id = ''; C.folder = null; C.selIdx = 0; C.dirty = false; C.guestName = '';
    if (!SETUP) savedText('');
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
    if (lightsOn()) { clearTimeout(C.liveT); C.liveT = setTimeout(pushLive, 800); }
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
  // One live push at a time; edits made while one is in flight send just the latest spec after it.
  async function pushLive() {
    if (C.pushing) { C.pushAgain = true; return; }
    C.pushing = true;
    try { await api(url('live'), { ids: sel.slice(), pattern: spec() }); } catch (e) { fail(e); }
    finally {
      C.pushing = false;
      if (C.pushAgain) { C.pushAgain = false; if (lightsOn()) pushLive(); }
    }
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
    $('c-a').textContent = lightsOn() ? 'Live on ' + namesOf(list.filter((c) => c.state && c.state.on)) : 'Preview · real lights unchanged';
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
  if (!SETUP) {
  $('c-picker').addEventListener('input', (e) => { C.design.colors[C.selIdx] = e.target.value; changed(); });
  $('c-more').onclick = (e) => { e.preventDefault(); C.more = !C.more; renderCreate(); };
  makeSlider($('c-speed'), (v) => { C.design.speed = Math.round(v * 255 / 100); }, () => changed());
  makeSlider($('c-bright'), (v) => { C.design.brightness = Math.max(1, v); $('c-bval').textContent = C.design.brightness + '%'; }, () => changed());
  $('c-dir').querySelectorAll('span').forEach((s) => { s.onclick = () => { C.design.direction = +s.dataset.d; changed(); }; });
  $('c-new').onclick = () => { newDesign(); renderCreate(); };
  $('c-back').onclick = () => { location.hash = '#tonight'; };
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
  }
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
    SU.active = 0; SU.hist = [];
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
    $('s-upload').textContent = lay.photo ? '📷 Replace photo' : '📷 Add a photo';
    $('s-rmphoto').hidden = !lay.photo;
    $('s-ed').querySelectorAll('.tool [data-tool]').forEach((s) => s.classList.toggle('on', s.dataset.tool === SU.tool));
    const run = SU.runs[SU.active];
    const tip = $('s-tip'); tip.textContent = '';
    if (SU.tool === 'draw') tip.append(h('b', '', 'Tap along the lights'), document.createTextNode(' to add points to ' + (run ? runLabel(SU.active).toLowerCase() : 'a new run') + '. Drag a point to move it, tap it to delete it.'));
    else tip.append(h('b', '', 'Drag points'), document.createTextNode(' to move them. Tap a point to delete it, tap a line to cut it, or drag a line to bend it.'));
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
      const tx = h('div', 'grow'); tx.append(document.createTextNode(runLabel(i)), h('small', '', r.points.length + ' points'));
      hd.appendChild(tx);
      // A run's points are in flow order, so reversing them flips which way patterns travel.
      const rev = h('button', 'dots rev', '⇄');
      rev.title = 'Reverse direction';
      rev.disabled = r.points.length < 2;
      rev.onclick = (e) => { e.stopPropagation(); pushHist(); r.points.reverse(); SU.active = i; runsChanged(); };
      hd.appendChild(rev);
      if (SU.runs.length > 1) {
        const del = h('button', 'dots', '🗑');
        del.title = 'Delete run';
        del.onclick = (e) => { e.stopPropagation(); pushHist(); SU.runs.splice(i, 1); SU.active = Math.max(0, Math.min(SU.active, SU.runs.length - 1)); runsChanged(); };
        hd.appendChild(del);
      }
      hd.onclick = () => { SU.active = i; renderSetup(); };
      row.appendChild(hd);
      box.appendChild(row);
    });
    $('s-add').disabled = SU.runs.length >= 8;
  }
  const SETUP_LIGHT = '#5aa0ff';
  function renderOverlay() {
    const ed = $('s-ed'), sc = scenes.s;
    if (!sc || !ed.offsetParent) return;
    sc.fit();
    if (!SU.svg) { SU.svg = sv('svg', { class: 'ov' }); SU.svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none'; ed.insertBefore(SU.svg, $('s-tip')); }
    const g = SU.svg;
    g.setAttribute('width', sc.w); g.setAttribute('height', sc.h);
    g.textContent = '';
    // Setup shows where the lights are, not a pattern: every light is the same steady blue,
    // drawn above the run lines so none hide behind them; handles go on top.
    const runPts = SU.runs.map((r) => r.points.map((p) => sc.toPx(p)));
    runPts.forEach((pts, ri) => {
      const act = ri === SU.active;
      if (pts.length > 1) {
        g.appendChild(sv('polyline', { points: pts.map((p) => p.join(',')).join(' '), fill: 'none',
          stroke: act ? 'rgba(90,160,255,.5)' : 'rgba(255,255,255,.3)', 'stroke-width': act ? 3 : 2, 'stroke-dasharray': act ? '' : '8 6', 'stroke-linecap': 'round' }));
      }
    });
    (sc.dots || []).forEach(([x, y]) => g.appendChild(sv('circle', { class: 'lt', cx: x, cy: y, r: 4, fill: SETUP_LIGHT, stroke: '#0B0D10', 'stroke-width': 1 })));
    // Arrows along each run show which way patterns flow (first point to last).
    runPts.forEach((pts, ri) => flowArrows(pts).forEach(([x, y, a]) => g.appendChild(sv('path', {
      class: 'flow', d: 'M-9,-8 L5,0 L-9,8', transform: 'translate(' + x + ',' + y + ') rotate(' + a + ')', fill: 'none',
      stroke: ri === SU.active ? '#fff' : 'rgba(255,255,255,.6)', 'stroke-width': 4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'paint-order': 'stroke' }))));
    runPts.forEach((pts, ri) => {
      const act = ri === SU.active;
      pts.forEach((p) => g.appendChild(sv('circle', { cx: p[0], cy: p[1], r: act ? 11 : 8, fill: '#0B0D10',
        stroke: act ? '#5aa0ff' : 'rgba(255,255,255,.6)', 'stroke-width': 4 })));
      if (pts.length) {
        const s = pts[0];
        const lb = sv('text', { x: s[0], y: s[1] - 22, 'text-anchor': 'middle', 'font-size': 18, 'font-weight': 600, fill: '#fff', stroke: '#000', 'stroke-width': 4, 'paint-order': 'stroke' });
        lb.textContent = String(ri + 1);
        g.appendChild(lb);
      }
    });
  }
  // Evenly spaced [x, y, degrees] chevrons along a polyline, pointing toward its end.
  function flowArrows(pts) {
    const segs = []; let L = 0;
    for (let i = 1; i < pts.length; i++) { const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); segs.push(d); L += d; }
    if (L < 40) return [];
    const n = clamp(Math.round(L / 160), 1, 12), out = [];
    let si = 0, acc = 0;
    for (let j = 0; j < n; j++) {
      const d = ((j + 0.5) / n) * L;
      while (si < segs.length - 1 && acc + segs[si] < d) { acc += segs[si]; si++; }
      const a = pts[si], b = pts[si + 1], k = segs[si] ? (d - acc) / segs[si] : 0;
      out.push([a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI]);
    }
    return out;
  }
  // Runs aren't named by people; the number is all that's shown and saved.
  function runLabel(i) { return 'Run ' + (i + 1); }
  function pushHist() { SU.hist.push(clone(SU.runs)); if (SU.hist.length > 60) SU.hist.shift(); }
  const runsValid = () => SU.runs.length >= 1 && SU.runs.every((r) => r.points.length >= 2);
  function runsChanged() {
    SU.runs.forEach((r, i) => { r.name = runLabel(i); });
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
  if (SETUP) {
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
  // A tap (no drag) on a point or on the selected run's line opens a small menu
  // there instead of moving anything. A tap that only closes an open menu does nothing.
  let popWasOpen = false;
  window.addEventListener('pointerdown', () => { popWasOpen = !pop.hidden; }, true);
  function popAt(cx, cy, build) {
    openPop({ getBoundingClientRect: () => ({ left: cx, right: cx + 90, top: cy, bottom: cy }) }, build, 'mini');
  }
  function deletePoint(ri, pi) {
    pushHist();
    const r = SU.runs[ri];
    r.points.splice(pi, 1);
    if (!r.points.length) {
      SU.runs.splice(ri, 1);
      SU.active = clamp(SU.active > ri ? SU.active - 1 : SU.active, 0, Math.max(0, SU.runs.length - 1));
    }
    runsChanged();
  }
  function cutLine(ri, i) {
    const r = SU.runs[ri], a = r.points.slice(0, i), b = r.points.slice(i);
    if (a.length >= 2 && b.length >= 2 && SU.runs.length >= 8) return toast('You already have the most runs (8)');
    pushHist();
    const parts = [];
    if (a.length >= 2) parts.push({ name: r.name, points: a });
    if (b.length >= 2) parts.push({ name: parts.length ? r.name + ' 2' : r.name, points: b });
    SU.runs.splice(ri, 1, ...parts);
    SU.active = clamp(ri, 0, Math.max(0, SU.runs.length - 1));
    if (!SU.runs.length) SU.active = 0;
    runsChanged();
  }
  ed.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.tool') || !scenes.s || popWasOpen) return;
    const R = ed.getBoundingClientRect(), x = e.clientX - R.left, y = e.clientY - R.top;
    const hit = hitHandle(x, y);
    if (hit) {
      pushHist(); SU.active = hit.ri;
      SU.drag = Object.assign({ kind: 'point', x: e.clientX, y: e.clientY, moved: false }, hit);
      ed.setPointerCapture(e.pointerId); renderOverlay(); return;
    }
    if (SU.tool === 'draw') {
      pushHist();
      if (!SU.runs.length) SU.runs.push({ name: 'Run 1', points: [] });
      const r = SU.runs[SU.active];
      if (r.points.length >= 200) return toast('That run has the most points it can');
      r.points.push(scenes.s.toPic(x, y));
      runsChanged();
    } else {
      const seg = hitSegment(x, y);
      if (seg == null) return;
      pushHist();
      SU.runs[SU.active].points.splice(seg, 0, scenes.s.toPic(x, y));
      SU.drag = { kind: 'line', ri: SU.active, pi: seg, x: e.clientX, y: e.clientY, moved: false };
      ed.setPointerCapture(e.pointerId);
      renderOverlay();
    }
  });
  ed.addEventListener('pointermove', (e) => {
    const d = SU.drag;
    if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 5) return;
    d.moved = true;
    const R = ed.getBoundingClientRect();
    SU.runs[d.ri].points[d.pi] = scenes.s.toPic(e.clientX - R.left, e.clientY - R.top);
    scenes.s.set(setupCtrl(), SU.runs);
    renderOverlay();
  });
  const endDrag = (e) => {
    const d = SU.drag;
    if (!d) return;
    SU.drag = null;
    if (d.moved) return runsChanged();
    // A tap: put things back as they were, then offer the menu.
    SU.runs = SU.hist.pop();
    renderOverlay();
    if (e.type !== 'pointerup') return;
    if (d.kind === 'point') {
      popAt(d.x, d.y, (p) => p.append(popItem('🗑 Delete point', '', () => { closePop(); deletePoint(d.ri, d.pi); })));
    } else {
      popAt(d.x, d.y, (p) => p.append(popItem('✂ Cut line here', '', () => { closePop(); cutLine(d.ri, d.pi); })));
    }
  };
  ed.addEventListener('pointerup', endDrag);
  ed.addEventListener('pointercancel', endDrag);
  $('s-add').onclick = () => {
    if (SU.runs.length >= 8) return;
    pushHist();
    SU.runs.push({ name: 'Run ' + (SU.runs.length + 1), points: [] });
    SU.active = SU.runs.length - 1; SU.tool = 'draw';
    runsChanged();
  };
  $('s-upload').onclick = () => $('s-file').click();
  $('s-file').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    await flushLayout();
    const fd = new FormData();
    fd.append('photo', f);
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
  }

  // ------------------------------------------------------------ animation loop
  let tick = 0;
  function frame() {
    if (document.hidden) return;
    tick++;
    const t = tick / 4;
    const first = selCtrls()[0], st = (first && first.state) || {}, sh = shown(first);
    if (view === 'tonight') {
      scenes.t && scenes.t.draw(sh.pattern, sh.on, t);
      $('t-insets').querySelectorAll('.inset').forEach((b, i) => {
        const c = selCtrls()[i + 1];
        if (b._scene && c) b._scene.draw(c.state && c.state.pattern, c.state && c.state.on, t);
      });
      if (pinned) {
        const pc = $('m-ph');
        pc.classList.toggle('g-dim', !sh.on);
        if (miniPhoto(first)) scenes.m && scenes.m.draw(sh.pattern, sh.on, t);
        else if (sh.pattern) strip(pc.querySelector('canvas.g-mstrip'), sh.pattern, t);
      }
    } else if (view === 'create') {
      const p = previewPattern();
      scenes.c && scenes.c.draw(p, true, t);
      $('c-anims').querySelectorAll('canvas').forEach((cv) => strip(cv, Object.assign({}, p, { animation: cv.dataset.anim }), t));
    } else if (view === 'setup' && scenes.s) {
      // No pattern here: the lights are drawn static in the overlay (renderOverlay).
      if (scenes.s.el.offsetParent) scenes.s.fit();
      if (scenes.s.size && SU.svgSize !== scenes.s.size) { SU.svgSize = scenes.s.size; renderOverlay(); }
    }
    if (view === 'tonight' && tick % 20 === 0) renderSched();
  }
  setInterval(frame, 250);
  window.addEventListener('resize', () => { if (view === 'setup') renderOverlay(); });
  function busy() { const a = document.activeElement; return SU.drag || !pop.hidden || (a && (a.isContentEditable || a.tagName === 'INPUT')); }
  setInterval(() => { if (!document.hidden && !busy()) refreshState(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState(); });

  fixSel();
  if (SETUP) renderView();
  else routeFromHash();
})();
