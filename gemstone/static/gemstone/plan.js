/* Gemstone Lights — the Plan tab (admins only): Coming up, Autopilot and schedules.
   Plain JS, no build step. gemstone.js calls GemstonePlan.show(state) when the tab opens.
   Schedules are Gemstone "timers"; the UI never calls them that. */
(function () {
  'use strict';
  const root = document.getElementById('gs');
  if (!root || !document.getElementById('v-plan')) return;
  const $ = (id) => document.getElementById(id);
  const D = root.dataset;
  const csrf = (root.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const OFF_MIN = -120, OFF_MAX = 120, OFF_STEP = 5;

  let S = {}, P = null, OURS = null, days = 14, F = null, pollT = null, seenJob = null, apMounted = false;

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  async function api(u, body) {
    const init = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf, 'Accept': 'application/json' } };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(u, init); } catch (e) { throw new Error("Can't reach the hub"); }
    let j = {};
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(j.error || ('Something went wrong (' + r.status + ')'));
    return j;
  }
  let toastT = null;
  function toast(text, err) {
    const t = $('gs-toast');
    if (!t) return;
    $('gs-toast-text').textContent = text;
    const act = $('gs-toast-act');
    if (act) act.hidden = true;
    t.classList.toggle('err', !!err);
    t.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { t.hidden = true; }, err ? 6000 : 3000);
  }

  // ------------------------------------------------------------ formatting
  const pad = (n) => String(n).padStart(2, '0');
  function clock(hm) {
    let [hh, mm] = hm.split(':').map(Number);
    if (hh === 0 && mm === 0) return 'Midnight';
    const ap = hh >= 12 ? 'PM' : 'AM';
    hh = hh % 12 || 12;
    return mm ? hh + ':' + pad(mm) + ' ' + ap : hh + ' ' + ap;
  }
  const at = (iso) => { const d = new Date(iso); return clock(pad(d.getHours()) + ':' + pad(d.getMinutes())); };
  const mmdd = (v) => MONTHS[Number(v.slice(0, 2)) - 1] + ' ' + Number(v.slice(3));
  function onText(on, off) {
    if (on !== 'sunset' && on !== 'sunrise') return clock(on);
    if (!off) return on === 'sunset' ? 'Sunset' : 'Sunrise';
    return Math.abs(off) + ' min ' + (off < 0 ? 'before ' : 'after ') + on;
  }
  const offText = (off) => (off === 'sunrise' ? 'sunrise' : clock(off));
  function repeatText(s) {
    if (s.repeat === 'yearly') return s.start === s.end ? mmdd(s.start) : mmdd(s.start) + ' – ' + mmdd(s.end);
    const d = s.days || [];
    if (s.repeat === 'daily' || d.length === 7) return 'Every day';
    if (d.length === 2 && d.includes(0) && d.includes(6)) return 'Weekends';
    if (d.length === 5 && !d.includes(0) && !d.includes(6)) return 'Weekdays';
    return d.map((x) => DAYS[x]).join(', ');
  }
  const whenText = (s) => repeatText(s) + ' · ' + onText(s.on, s.on_offset) + ' → ' + offText(s.off);
  function swatch(colors, cls) {
    const e = h('span', cls || 'pl-sw');
    const c = (colors && colors.length ? colors : ['#333']).slice(0, 6);
    e.style.background = c.length === 1 ? c[0] : 'linear-gradient(90deg,' + c.join(',') + ')';
    return e;
  }
  function nightText(row) {
    const a = new Date(row.first + 'T12:00'), b = new Date(row.last + 'T12:00');
    const today = new Date(); today.setHours(12, 0, 0, 0);
    const dd = (d) => d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    const one = (d) => {
      const n = Math.round((d - today) / 864e5);
      return n === 0 ? 'Tonight' : n === 1 ? 'Tomorrow' : dd(d);
    };
    return row.first === row.last ? one(a) : one(a) + ' – ' + dd(b);
  }

  // ------------------------------------------------------------ loading
  let loadedAt = 0;
  async function load() {
    loadedAt = Date.now();
    const jobs = [api(D.planUrl + '?days=' + days)];
    if (!apMounted) jobs.push(api(D.autopilotApiUrl).then((d) => {
      if (window.GemstoneAutopilot) window.GemstoneAutopilot.mount(d);
      apMounted = true;
    }).catch(() => { /* the card stays hidden; try again next time */ }));
    if (!OURS) jobs.push(api(D.oursUrl).then((o) => { OURS = o.patterns || []; }).catch(() => { OURS = null; }));
    try {
      P = (await Promise.all(jobs))[0];
    } catch (e) {
      $('pl-tl').textContent = '';
      $('pl-tl').appendChild(h('div', 'pl-empty', e.message));
      return;
    }
    render();
    watchJob(P.job);
  }
  function watchJob(job) {
    clearTimeout(pollT);
    const box = $('pl-job');
    const running = job && job.state === 'running';
    box.hidden = !running;
    if (running) {
      box.textContent = { save: 'Saving schedule…', toggle: 'Updating schedule…', delete: 'Deleting schedule…' }[job.what] || 'Saving…';
      pollT = setTimeout(() => { if (!$('v-plan').hidden) load(); else pollT = null; }, 2000);
      seenJob = job.id;
      return;
    }
    if (job && job.id === seenJob) {
      seenJob = null;
      if (job.state === 'error') toast(job.error || 'Couldn’t save that', true);
      else toast({ save: 'Schedule saved', toggle: 'Schedule updated', delete: 'Schedule deleted' }[job.what] || 'Saved');
    }
  }
  async function send(url, body) {
    try {
      const r = await api(url, body);
      seenJob = r.job && r.job.id;
      watchJob(r.job);
      return true;
    } catch (e) {
      toast(e.message, true);
      return false;
    }
  }

  // ------------------------------------------------------------ coming up
  function playingKeys() {
    const now = Date.now(), keys = new Set();
    ((P && P.timeline) || []).slice(0, 1).forEach((row) => row.groups.forEach((g) => g.segments.forEach((s) => {
      if (s.kind !== 'autopilot' && new Date(s.start) <= now && now < new Date(s.end)) keys.add(s.key);
    })));
    return keys;
  }
  function renderTimeline() {
    const tl = $('pl-tl');
    tl.textContent = '';
    const rows = P.timeline || [];
    if (!rows.length || rows.every((r) => r.groups.every((g) => !g.segments.length))) {
      tl.appendChild(h('div', 'pl-empty', 'Nothing planned'));
      return;
    }
    const several = ctrlCount() > 1;
    rows.forEach((row) => {
      const r = h('div', 'pl-row');
      r.appendChild(h('div', 'pl-when', nightText(row)));
      const body = h('div', 'pl-what');
      row.groups.forEach((g) => {
        const gr = h('div', 'pl-grp');
        if (several && row.groups.length > 1) gr.appendChild(h('div', 'pl-gl', g.names.join(' + ')));
        if (!g.segments.length) gr.appendChild(h('div', 'pl-line pl-none', 'Off'));
        g.segments.forEach((s) => {
          const ln = h('div', 'pl-line' + (s.kind === 'autopilot' ? ' ap' : ''));
          ln.appendChild(swatch(s.colors));
          const t = h('div', 'pl-lt');
          t.appendChild(h('b', '', (s.kind === 'autopilot' ? '' : '\u{1F4C5} ') + s.name));
          t.appendChild(h('small', '', at(s.start) + ' → ' + at(s.end)));
          ln.appendChild(t);
          gr.appendChild(ln);
        });
        g.losers.forEach((l) => {
          const ln = h('div', 'pl-line lose');
          ln.appendChild(h('span', 'pl-sw'));
          const t = h('div', 'pl-lt');
          t.appendChild(h('b', '', l.name));
          t.appendChild(h('small', '', at(l.start) + ' → ' + at(l.end)));
          ln.appendChild(t);
          gr.appendChild(ln);
        });
        body.appendChild(gr);
      });
      r.appendChild(body);
      tl.appendChild(r);
    });
  }
  const ctrlCount = () => (S.controllers || []).length;

  // ------------------------------------------------------------ schedule cards
  function renderList() {
    const list = $('pl-list');
    list.textContent = '';
    const scheds = P.schedules || [];
    if (!scheds.length) { list.appendChild(h('div', 'pl-empty', 'No schedules yet')); return; }
    const busy = P.job && P.job.state === 'running';
    const playing = playingKeys();
    scheds.forEach((s) => {
      const c = h('div', 'pl-card' + (s.enabled ? '' : ' off'));
      c.appendChild(swatch(s.design && s.design.colors, 'pl-strip'));
      const main = h('button', 'pl-cm');
      main.setAttribute('aria-label', 'Edit ' + s.name);
      const top = h('div', 'pl-ct');
      top.appendChild(h('b', '', s.name));
      if (s.enabled && playing.has(s.id)) top.appendChild(h('span', 'pl-now', '● Playing now'));
      main.appendChild(top);
      main.appendChild(h('div', 'pl-cw', whenText(s)));
      const chips = h('div', 'pl-chips');
      if (s.design && s.design.name !== s.name) chips.appendChild(h('span', 'pl-chip d', s.design.name));
      s.light_names.forEach((n) => chips.appendChild(h('span', 'pl-chip', n)));
      main.appendChild(chips);
      main.onclick = () => openEditor(s);
      c.appendChild(main);
      const tg = h('button', 'g-tg' + (s.enabled ? ' on' : ''));
      tg.setAttribute('aria-label', s.enabled ? 'Turn ' + s.name + ' off' : 'Turn ' + s.name + ' on');
      tg.appendChild(h('span', 'g-sw'));
      tg.disabled = busy;
      tg.onclick = () => send(D.scheduleToggleUrl, { id: s.id, enabled: !s.enabled });
      c.appendChild(tg);
      list.appendChild(c);
    });
  }
  function render() {
    root.querySelectorAll('#pl-range button').forEach((b) => b.classList.toggle('on', Number(b.dataset.days) === days));
    renderTimeline();
    renderList();
    $('pl-new').disabled = !!(P.job && P.job.state === 'running');
  }

  // ------------------------------------------------------------ editor
  function blank() {
    const lights = (S.controllers || []).map((c) => c.id);
    return { id: null, name: '', lights, design: null, repeat: 'daily', days: [5, 6], start: '12-24', end: '12-26',
      on: 'sunset', ontime: '18:00', offset: 0, off: 'time', offtime: '22:00', enabled: true };
  }
  function fromSched(s) {
    const sun = s.on === 'sunset' || s.on === 'sunrise';
    return { id: s.id, name: s.name === (s.design && s.design.name) ? '' : s.name, lights: s.lights.slice(),
      design: s.design && s.design.id, designObj: s.design, repeat: s.repeat,
      days: s.repeat === 'weekly' ? s.days.slice() : [5, 6], start: s.start || '12-24', end: s.end || s.start || '12-26',
      on: sun ? s.on : 'time', ontime: sun ? '18:00' : s.on, offset: s.on_offset || 0,
      off: s.off === 'sunrise' ? 'sunrise' : 'time', offtime: s.off === 'sunrise' ? '22:00' : s.off, enabled: s.enabled };
  }
  function openEditor(s) {
    F = s ? fromSched(s) : blank();
    $('pl-ed-title').textContent = s ? 'Edit schedule' : 'New schedule';
    $('pl-name').value = F.name;
    $('pl-q').value = '';
    $('pl-ont').value = F.ontime;
    $('pl-offt').value = F.offtime;
    $('pl-del').hidden = !s;
    delArmed = false;
    $('pl-del').textContent = '🗑 Delete';
    $('pl-ed').hidden = false;
    renderEditor();
    renderDesigns();
  }
  const closeEditor = () => { $('pl-ed').hidden = true; F = null; };
  function seg(id, value, onPick) {
    root.querySelectorAll('#' + id + ' button').forEach((b) => {
      b.classList.toggle('on', b.dataset.v === value);
      b.onclick = () => { onPick(b.dataset.v); renderEditor(); };
    });
  }
  function fillSelect(sel, opts, value) {
    if (!sel.options.length) opts.forEach(([v, t]) => { const o = h('option', '', t); o.value = v; sel.appendChild(o); });
    sel.value = value;
  }
  const monthOpts = MONTHS.map((m, i) => [pad(i + 1), m]);
  const dayOpts = Array.from({ length: 31 }, (_, i) => [pad(i + 1), String(i + 1)]);
  function designName() {
    const d = (OURS || []).find((p) => p.id === F.design) || F.designObj;
    return d ? d.name : '';
  }
  function renderEditor() {
    seg('pl-repeat', F.repeat, (v) => { F.repeat = v; });
    seg('pl-on', F.on, (v) => { F.on = v; if (v === 'time') F.offset = 0; });
    seg('pl-off', F.off, (v) => { F.off = v; });
    const lights = $('pl-lights');
    lights.textContent = '';
    (S.controllers || []).forEach((c) => {
      const on = F.lights.includes(c.id);
      const b = h('button', 'pl-pick' + (on ? ' on' : ''));
      b.appendChild(h('span', 'pl-ck', on ? '✓' : ''));
      b.appendChild(document.createTextNode(c.name));
      b.onclick = () => {
        F.lights = on ? F.lights.filter((x) => x !== c.id) : F.lights.concat([c.id]);
        renderEditor();
      };
      lights.appendChild(b);
    });
    const dd = $('pl-days');
    dd.hidden = F.repeat !== 'weekly';
    dd.textContent = '';
    DAYS.forEach((n, i) => {
      const b = h('button', 'pl-day' + (F.days.includes(i) ? ' on' : ''), n);
      b.onclick = () => {
        F.days = F.days.includes(i) ? F.days.filter((x) => x !== i) : F.days.concat([i]).sort();
        renderEditor();
      };
      dd.appendChild(b);
    });
    $('pl-dates').hidden = F.repeat !== 'yearly';
    fillSelect($('pl-sm'), monthOpts, F.start.slice(0, 2));
    fillSelect($('pl-sd'), dayOpts, F.start.slice(3));
    fillSelect($('pl-em'), monthOpts, F.end.slice(0, 2));
    fillSelect($('pl-edd'), dayOpts, F.end.slice(3));
    const sun = F.on !== 'time';
    $('pl-offs').hidden = !sun;
    $('pl-ont').hidden = sun;
    $('pl-offv').textContent = F.offset ? (F.offset > 0 ? '+' : '−') + Math.abs(F.offset) + ' min' : 'On time';
    $('pl-minus').disabled = F.offset <= OFF_MIN;
    $('pl-plus').disabled = F.offset >= OFF_MAX;
    $('pl-offt').hidden = F.off !== 'time';
    $('pl-name').placeholder = designName() || 'Name';
    const b = body();
    $('pl-sum').textContent = b.design ? whenText(b) : 'Pick a design';
    $('pl-save').disabled = !b.design || !b.lights.length || (b.repeat === 'weekly' && !b.days.length);
  }
  function renderDesigns() {
    const box = $('pl-designs');
    box.textContent = '';
    if (!OURS) { box.appendChild(h('div', 'pl-empty', 'Couldn’t load designs')); return; }
    const q = $('pl-q').value.trim().toLowerCase();
    const list = OURS.filter((p) => !q || (p.name || '').toLowerCase().includes(q));
    if (!list.length) box.appendChild(h('div', 'pl-empty', 'No designs match'));
    list.forEach((p) => {
      const b = h('button', 'pl-des' + (p.id === F.design ? ' on' : ''));
      b.appendChild(swatch(p.colors));
      b.appendChild(h('span', '', p.name));
      b.onclick = () => {
        F.design = p.id; F.designObj = p;
        box.querySelectorAll('.pl-des').forEach((x) => x.classList.toggle('on', x === b));
        renderEditor();
      };
      box.appendChild(b);
    });
  }
  function body() {
    const on = F.on === 'time' ? ($('pl-ont').value || F.ontime) : F.on;
    const off = F.off === 'time' ? ($('pl-offt').value || F.offtime) : 'sunrise';
    return { id: F.id, name: $('pl-name').value.trim() || designName(), lights: F.lights, design: F.design,
      repeat: F.repeat, days: F.repeat === 'weekly' ? F.days : [], start: F.start, end: F.end,
      on, on_offset: F.on === 'time' ? 0 : F.offset, off, enabled: F.enabled };
  }
  let delArmed = false, delT = null;

  // ------------------------------------------------------------ wiring
  root.querySelectorAll('#pl-range button').forEach((b) => {
    b.onclick = () => { days = Number(b.dataset.days); render(); load(); };
  });
  $('pl-new').onclick = () => openEditor(null);
  $('pl-cancel').onclick = closeEditor;
  $('pl-q').addEventListener('input', renderDesigns);
  $('pl-name').addEventListener('input', renderEditor);
  $('pl-ont').addEventListener('change', () => { F.ontime = $('pl-ont').value || F.ontime; renderEditor(); });
  $('pl-offt').addEventListener('change', () => { F.offtime = $('pl-offt').value || F.offtime; renderEditor(); });
  const dateSel = () => {
    F.start = $('pl-sm').value + '-' + $('pl-sd').value;
    F.end = $('pl-em').value + '-' + $('pl-edd').value;
    renderEditor();
  };
  ['pl-sm', 'pl-sd', 'pl-em', 'pl-edd'].forEach((id) => $(id).addEventListener('change', dateSel));
  $('pl-minus').onclick = () => { F.offset = Math.max(OFF_MIN, F.offset - OFF_STEP); renderEditor(); };
  $('pl-plus').onclick = () => { F.offset = Math.min(OFF_MAX, F.offset + OFF_STEP); renderEditor(); };
  $('pl-save').onclick = async () => {
    const b = body();
    $('pl-save').disabled = true;
    if (await send(D.scheduleSaveUrl, b)) closeEditor();
    else renderEditor();
  };
  // Delete asks once more on the same button (no pop-up dialogs on the kiosk).
  $('pl-del').onclick = async () => {
    if (!delArmed) {
      delArmed = true;
      $('pl-del').textContent = 'Tap again to delete';
      clearTimeout(delT);
      delT = setTimeout(() => { delArmed = false; $('pl-del').textContent = '🗑 Delete'; }, 4000);
      return;
    }
    clearTimeout(delT);
    delArmed = false;
    if (await send(D.scheduleDeleteUrl, { id: F.id })) closeEditor();
  };

  window.GemstonePlan = {
    // Called on every screen refresh while the tab shows; the plan is re-read at most
    // every 30 s (a running save polls on its own).
    show(state) {
      S = state || {};
      if (P) render();
      if (Date.now() - loadedAt > 30000) load();
    },
  };
})();
