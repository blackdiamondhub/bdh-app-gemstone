/* Gemstone Lights — the Autopilot card on the Plan tab (admins only). Plain JS, no build step.
   The Plan tab loads its data and calls GemstoneAutopilot.mount(data). */
(function () {
  'use strict';
  const root = document.getElementById('ap');
  if (!root || !document.getElementById('ap-body')) return;
  const $ = (id) => document.getElementById(id);
  const csrf = (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const API = root.dataset.autopilotUrl;
  const OFF_MIN = -120, OFF_MAX = 120, OFF_STEP = 5;
  let data = null, A = null, CATS = { general: [], teams: [], leagues: [] };

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const signed = (n) => (n < 0 ? '−' : n > 0 ? '+' : '') + Math.abs(n);
  function atToday(t) { const [hh, mm] = t.split(':').map(Number); const d = new Date(); d.setHours(hh, mm, 0, 0); return d; }
  function fmtTime(d) {
    let hh = d.getHours(); const mm = d.getMinutes();
    if (hh === 0 && mm === 0) return 'Midnight';
    const ap = hh >= 12 ? 'PM' : 'AM';
    hh = hh % 12 || 12;
    return mm ? hh + ':' + String(mm).padStart(2, '0') + ' ' + ap : hh + ' ' + ap;
  }
  const hhmm = (d) => String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  let toastT = null;
  function toast(text, err) {
    const t = $('gs-toast');
    $('gs-toast-text').textContent = text;
    t.classList.toggle('err', !!err);
    t.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { t.hidden = true; }, 4000);
  }

  // Every control saves straight away: show the change, send it, put it back if it fails.
  let chain = Promise.resolve();
  function save(body, apply, msg) {
    const before = clone(A);
    apply(A);
    render();
    chain = chain.then(async () => {
      try {
        const r = await fetch(API, { method: 'POST', credentials: 'same-origin',
          headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify(body) });
        let j = {};
        try { j = await r.json(); } catch (e) { /* not JSON */ }
        if (!r.ok) throw new Error(j.error || ('Something went wrong (' + r.status + ')'));
        A = j.settings;
        if (msg) toast(msg);
      } catch (e) {
        A = before;
        toast(e.message === 'Failed to fetch' ? "Can't reach the hub" : e.message, true);
      }
      render();
    });
  }
  function toggle(el, on) {
    el.classList.toggle('on', !!on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  // ---------------------------------------------------------------- schedule
  function offOptions() {
    const out = [];
    for (let m = 17 * 60; m <= 27 * 60; m += 15) {
      out.push(String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'));
    }
    const cur = A.schedule.off_time;
    if (cur && !out.includes(cur)) out.push(cur);
    const key = (t) => (t < '06:00' ? '1' : '0') + t;
    return out.sort((x, y) => (key(x) < key(y) ? -1 : 1));
  }
  let nudgeT = null, pendingOffset = null;
  function nudge(d) {
    const sc = A.schedule;
    pendingOffset = clamp((pendingOffset == null ? sc.on_offset : pendingOffset) + d, OFF_MIN, OFF_MAX);
    renderSchedule();
    clearTimeout(nudgeT);
    nudgeT = setTimeout(() => {
      const n = pendingOffset;
      pendingOffset = null;
      save({ schedule: { on_offset: n } }, (s) => { s.schedule.on_offset = n; s.schedule.on_time = 'sunset'; });
    }, 700);
  }
  function renderSchedule() {
    const sc = A.schedule, off = pendingOffset == null ? sc.on_offset : pendingOffset;
    $('ap-off').textContent = signed(off) + ' min';
    $('ap-minus').disabled = off <= OFF_MIN;
    $('ap-plus').disabled = off >= OFF_MAX;
    const sun = data.schedule && data.schedule.sunset;
    if (sun) {
      const on = atToday(sun); on.setMinutes(on.getMinutes() + off);
      $('ap-sun').textContent = 'Sunset today ' + fmtTime(atToday(sun)) + ' · lights on at ' + fmtTime(on);
    } else $('ap-sun').textContent = '';
    const sel = $('ap-offat');
    if (document.activeElement !== sel) {
      sel.textContent = '';
      offOptions().forEach((t) => {
        const o = h('option', '', fmtTime(atToday(t)) + (t < '06:00' ? ' (after midnight)' : ''));
        o.value = t;
        if (t === sc.off_time) o.selected = true;
        sel.appendChild(o);
      });
    }
  }
  $('ap-minus').onclick = () => nudge(-OFF_STEP);
  $('ap-plus').onclick = () => nudge(OFF_STEP);
  $('ap-offat').onchange = (e) => {
    const t = e.target.value;
    save({ schedule: { off_time: t } }, (s) => { s.schedule.off_time = t; }, 'Lights go off at ' + fmtTime(atToday(t)));
    e.target.blur();
  };

  // ---------------------------------------------------------------- lights
  function renderLights() {
    const box = $('ap-lights');
    box.textContent = '';
    const list = data.controllers || [];
    if (!list.length) box.appendChild(h('div', 'g-schs', 'No controllers on this account.'));
    list.forEach((c) => {
      const on = A.device_ids.includes(c.id);
      const b = h('button', 'ap-check' + (on ? ' on' : ''));
      b.appendChild(h('span', 'ap-box2', on ? '✓' : ''));
      b.appendChild(h('span', '', c.name));
      b.onclick = () => {
        const ids = on ? A.device_ids.filter((i) => i !== c.id) : A.device_ids.concat([c.id]);
        save({ device_ids: ids }, (s) => { s.device_ids = ids; s.enabled = !!ids.length; });
      };
      box.appendChild(b);
    });
  }

  // ---------------------------------------------------------------- patterns + brightness
  $('ap-anim').onclick = () => { const v = !A.allow_animated; save({ allow_animated: v }, (s) => { s.allow_animated = v; }); };
  $('ap-static').onclick = () => { const v = !A.allow_static; save({ allow_static: v }, (s) => { s.allow_static = v; }); };
  const bs = $('ap-bright');
  let dragging = false, bval = null;
  const at = (e) => { const r = bs.getBoundingClientRect(); return Math.max(1, Math.round(clamp((e.clientX - r.left) / r.width, 0, 1) * 100)); };
  function showBright(v) { bs.querySelector('.f').style.width = Math.max(v, 4) + '%'; $('ap-bval').textContent = v + '%'; }
  bs.addEventListener('pointerdown', (e) => { dragging = true; bs.setPointerCapture(e.pointerId); bval = at(e); showBright(bval); });
  bs.addEventListener('pointermove', (e) => { if (dragging) { bval = at(e); showBright(bval); } });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    const v = bval;
    save({ brightness: v }, (s) => { s.brightness = v; }, 'Autopilot brightness ' + v + '%');
  };
  bs.addEventListener('pointerup', endDrag);
  bs.addEventListener('pointercancel', endDrag);

  // ---------------------------------------------------------------- celebrate
  function setCat(id, on) {
    const body = on ? { add_categories: [id] } : { remove_categories: [id] };
    save(body, (s) => { s.category_ids = on ? s.category_ids.concat([id]) : s.category_ids.filter((i) => i !== id); });
  }
  function renderCats() {
    const box = $('ap-cats');
    box.textContent = '';
    let n = 0;
    CATS.general.forEach((c) => {
      const on = A.category_ids.includes(c.id);
      if (on) n++;
      const b = h('button', 'ap-tile' + (on ? ' on' : ''));
      b.appendChild(h('span', 'ap-em', c.emoji));
      b.appendChild(h('span', 'grow', c.name));
      b.appendChild(h('span', 'ap-tick', on ? '✓' : ''));
      b.onclick = () => setCat(c.id, !on);
      box.appendChild(b);
    });
    $('ap-ccount').textContent = n + ' of ' + CATS.general.length;
  }

  // ---------------------------------------------------------------- teams
  let teamById = new Map();
  function renderTeams() {
    const box = $('ap-teams');
    box.textContent = '';
    const mine = A.category_ids.filter((id) => teamById.has(id)).map((id) => teamById.get(id));
    if (!mine.length) box.appendChild(h('div', 'g-schs', 'No teams yet.'));
    mine.forEach((t) => {
      const r = h('div', 'ap-team');
      const tx = h('div', 'grow');
      tx.appendChild(h('b', '', t.name));
      const next = (data.upcoming || []).find((e) => e.category_id === t.id);
      tx.appendChild(h('small', '', t.league + (next ? ' · next game ' + day(next.start) + ' · ' + next.name : '')));
      const x = h('button', 'ap-x', '✕');
      x.setAttribute('aria-label', 'Remove ' + t.name);
      x.onclick = () => setCat(t.id, false);
      r.append(tx, x);
      box.appendChild(r);
    });
  }
  let league = 'All';
  function renderPick() {
    const pills = $('ap-leagues');
    pills.textContent = '';
    ['All'].concat(CATS.leagues).forEach((lg) => {
      const b = h('button', 'ap-pill' + (lg === league ? ' on' : ''), lg);
      b.onclick = () => { league = lg; renderPick(); };
      pills.appendChild(b);
    });
    const q = $('ap-q').value.trim().toLowerCase(), box = $('ap-pick');
    box.textContent = '';
    const list = CATS.teams.filter((t) => (league === 'All' || t.league === league) && (!q || t.name.toLowerCase().includes(q)));
    if (!list.length) box.appendChild(h('div', 'g-schs', 'No teams match.'));
    list.forEach((t) => {
      const on = A.category_ids.includes(t.id);
      const b = h('button', 'ap-check' + (on ? ' on' : ''));
      b.appendChild(h('span', 'grow', t.name));
      b.appendChild(h('span', 't3', t.league));
      b.appendChild(h('span', 'ap-box2', on ? '✓' : ''));
      b.onclick = () => setCat(t.id, !on);
      box.appendChild(b);
    });
  }
  $('ap-addteam').onclick = () => { $('ap-sheet').hidden = false; renderPick(); $('ap-q').focus(); };
  $('ap-done').onclick = () => { $('ap-sheet').hidden = true; };
  $('ap-sheet').onclick = (e) => { if (e.target === $('ap-sheet')) $('ap-sheet').hidden = true; };
  $('ap-q').oninput = renderPick;

  // ---------------------------------------------------------------- coming up
  const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  function parseDay(s) { const [y, m, d] = String(s).slice(0, 10).split('-').map(Number); return new Date(y, m - 1, d); }
  function day(s) { const d = parseDay(s); return MONTHS[d.getMonth()].charAt(0) + MONTHS[d.getMonth()].slice(1).toLowerCase() + ' ' + d.getDate(); }
  function renderEvents() {
    const box = $('ap-events');
    box.textContent = '';
    const list = data.upcoming || [];
    if (!list.some((e) => e.tonight)) {
      const t = h('div', 'ap-tonight');
      t.appendChild(h('small', '', 'TONIGHT'));
      t.appendChild(h('b', '', 'No event — your favourite plays'));
      box.appendChild(t);
    }
    if (!list.length) box.appendChild(h('div', 'g-schs', 'Nothing coming up. Pick some holidays or teams.'));
    $('ap-ecount').textContent = list.length ? String(list.length) : '';
    list.slice(0, 60).forEach((e) => {
      const d = parseDay(e.start);
      const r = h('div', 'ap-ev' + (e.tonight ? ' on' : ''));
      const dt = h('div', 'ap-date');
      dt.appendChild(h('small', '', e.tonight ? 'TONIGHT' : MONTHS[d.getMonth()]));
      dt.appendChild(h('b', '', String(d.getDate())));
      const tx = h('div', 'grow');
      tx.appendChild(h('b', '', (e.emoji ? e.emoji + ' ' : '') + e.name));
      tx.appendChild(h('small', '', (e.category || '') + (e.home ? ' · home game' : '') + ' · ' + e.count + (e.count === 1 ? ' pattern' : ' patterns')));
      if (e.color) { const bar = h('span', 'ap-bar'); bar.style.background = e.color; tx.appendChild(bar); }
      r.append(dt, tx);
      box.appendChild(r);
    });
  }

  // ---------------------------------------------------------------- page
  const FOLD_KEY = 'gs-ap-folded';
  let folded = false;
  try { folded = localStorage.getItem(FOLD_KEY) === '1'; } catch (e) { /* private mode */ }
  function nextEvent() {
    const list = (data && data.upcoming) || [];
    const e = list.find((x) => x.tonight) || list[0];
    if (!e) return '';
    return (e.tonight ? 'tonight: ' : 'next: ') + (e.emoji ? e.emoji + ' ' : '') + e.name + (e.tonight ? '' : ', ' + day(e.start));
  }
  function stateText() {
    if (!A.enabled) return folded ? ' · off' : " · off — the lights won't come on by themselves";
    const nx = folded ? nextEvent() : '';
    return ' · on' + (nx ? ' · ' + nx : '');
  }
  function renderFold() {
    root.classList.toggle('ap-folded', folded);
    $('ap-body').hidden = folded;
    $('ap-fold').setAttribute('aria-expanded', String(!folded));
    $('ap-fold').setAttribute('aria-label', folded ? 'Show Autopilot settings' : 'Hide Autopilot settings');
    if (A) $('ap-state').textContent = stateText();
  }
  $('ap-fold').onclick = () => {
    folded = !folded;
    try { localStorage.setItem(FOLD_KEY, folded ? '1' : '0'); } catch (e) { /* private mode */ }
    renderFold();
  };
  $('ap-on').onclick = () => {
    const v = !A.enabled;
    save({ enabled: v }, (s) => { s.enabled = v; if (!v) s.device_ids = []; }, v ? 'Autopilot on' : 'Autopilot off');
  };
  function render() {
    toggle($('ap-on'), A.enabled);
    $('ap-state').textContent = stateText();
    $('ap-body').classList.toggle('ap-dim', !A.enabled);
    toggle($('ap-anim'), A.allow_animated);
    toggle($('ap-static'), A.allow_static);
    if (!dragging) showBright(A.brightness || 100);
    renderSchedule();
    renderLights();
    renderCats();
    renderTeams();
    if (!$('ap-sheet').hidden) renderPick();
  }
  window.GemstoneAutopilot = {
    mount(d) {
      if (!d || !d.settings) return;
      data = d;
      A = d.settings;
      CATS = d.categories || { general: [], teams: [], leagues: [] };
      teamById = new Map(CATS.teams.map((t) => [t.id, t]));
      root.hidden = false;
      render();
      renderEvents();
      renderFold();
    },
  };
})();
