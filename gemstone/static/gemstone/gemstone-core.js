/* Gemstone Lights — pure helpers shared by the UI and its Node tests (tests/js). No DOM. */
(function (root) {
  'use strict';

  // Scale and offset that show every point (photo coordinates, 0..1 of the
  // picture's width/height) inside a boxW×boxH box. The points' bounding box,
  // padded by ``pad`` of its larger side, is fitted whole (contain) and centred;
  // the rest of the box is letterboxed. ``aspect`` is the picture's width/height.
  // Returns { s, pw, ph, ox, oy }: the picture is drawn pw×ph at (ox, oy).
  function fitBox(points, aspect, boxW, boxH, pad) {
    const A = aspect > 0 ? aspect : 2;
    pad = pad == null ? 0.08 : pad;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    (points || []).forEach((p) => {
      x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
      y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
    });
    if (!(x1 >= x0)) { x0 = 0; x1 = 1; y0 = 0; y1 = 1; pad = 0; }
    // Bounding box in units of the picture's height.
    const bw = (x1 - x0) * A, bh = y1 - y0;
    const m = pad * Math.max(bw, bh, 0.05);
    const s = Math.min(boxW / (bw + 2 * m), boxH / (bh + 2 * m));
    const pw = A * s, ph = s;
    const ox = boxW / 2 - ((x0 + x1) / 2) * pw;
    const oy = boxH / 2 - ((y0 + y1) / 2) * ph;
    return { s, pw, ph, ox, oy };
  }

  // Off times offered in the Schedule popover ("HH:MM", in night order).
  // Guests: strictly after tonight's on time (or the fallback without a sunset)
  // and no later than GUEST_LAST. Admins: any quarter hour from 17:00 to 03:00.
  const GUEST_LAST = '22:00', NIGHT_ENDS = '06:00';
  const nightMin = (t) => { const [hh, mm] = t.split(':').map(Number); return hh * 60 + mm + (t < NIGHT_ENDS ? 1440 : 0); };
  const guestAfter = (sc, fallback) => sc.on_at || fallback || '17:00';
  function guestOk(sc, t, fallback) {
    return nightMin(t) > nightMin(guestAfter(sc, fallback)) && nightMin(t) <= nightMin(GUEST_LAST);
  }
  function offTimeOptions(sc, o) {
    o = o || {};
    const admin = !!o.admin, out = [], last = admin ? 27 * 60 : 22 * 60;
    for (let m = 17 * 60; m <= last; m += 15) {
      const t = String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
      if (admin || guestOk(sc, t, o.guestFallback)) out.push(t);
    }
    // An admin's later off time stays selectable for guests, as long as it's after the on time.
    if (sc.off_time && !out.includes(sc.off_time)
        && (admin || nightMin(sc.off_time) > nightMin(guestAfter(sc, o.guestFallback)))) out.push(sc.off_time);
    const key = (t) => (t < NIGHT_ENDS ? '1' : '0') + t;
    return out.sort((x, y) => (key(x) < key(y) ? -1 : 1));
  }

  // Whether the Tonight hero has scrolled out from under the pinned bar: the same
  // test as the IntersectionObserver (root shrunk by ``top`` px at the top).
  function heroHidden(hero, rootRect, top) {
    if (!hero || !rootRect || !(hero.height > 0) || !(rootRect.height > 0)) return false;
    return hero.bottom <= rootRect.top + top || hero.top >= rootRect.bottom;
  }

  // ---------------------------------------------------------- pattern drawing
  const mod = (a, n) => ((a % n) + n) % n;
  function rnd(i) { const x = Math.sin(i * 12.9898) * 43758.5453; return x - Math.floor(x); }
  const MODES = {
    motionless: 'solid', starry: 'twinkle', glitter: 'twinkle', flicker: 'twinkle', glitch: 'twinkle',
    chase: 'chase', marquee: 'chase', pacman: 'chase', ghost: 'chase', pyramid_chase: 'chase',
    wave: 'wave', gradient_wave: 'wave', sway: 'wave', pulse: 'pulse', multipulse: 'pulse', tremor: 'pulse',
    fade: 'fade', isofade: 'isofade', smooth: 'smooth', fireworks: 'fireworks', accent: 'accent',
  };
  const STILL = new Set(['motionless', 'starry', 'fade', 'pulse', 'flicker', 'glitter', 'accent']);
  // A design's lengths (from the cloud's extraParameters), with the controller's defaults.
  const pint = (v, d) => { const x = Math.round(Number(v)); return v != null && x >= 0 && x <= 200 ? x : d; };
  function lengths(p) {
    const q = p.params || {};
    switch (MODES[p.animation]) {
      case 'accent': case 'isofade':
        return { on: Math.max(1, pint(q.on, 1)), off: pint(q.off, p.animation === 'accent' ? 3 : 0) };
      case 'smooth': return { on: Math.max(1, pint(q.length, 10)), off: pint(q.spacing, 0) };
      case 'solid': return { on: Math.max(1, pint(q.length, 1)), off: 0 };
      default: return null;
    }
  }
  // An unlit LED: the design's background colour, or dark.
  const OFF = (p) => (p.background ? [p.background, 1] : ['#000000', 0]);
  // A dimmed LED in modes that dim rather than switch off: the background replaces the dim.
  const dim = (p, c, a) => (p.background ? [p.background, 1] : [c, a]);
  // Colour and brightness (0..1) of LED ``i`` for pattern ``p`` at time ``t`` seconds.
  function colAt(i, p, t) {
    const cs = p.colors && p.colors.length ? p.colors : ['#fff1e0'];
    const n = cs.length;
    const T = t * (0.25 + 1.5 * ((p.speed == null ? 128 : p.speed) / 255)) * (p.direction === 1 ? -1 : 1);
    switch (MODES[p.animation] || 'flow') {
      case 'solid': return [cs[Math.floor(i / lengths(p).on) % n], 1];
      case 'accent': {
        const L = lengths(p), g = L.on + L.off;
        return mod(i, g) < L.on ? [cs[Math.floor(i / g) % n], 1] : OFF(p);
      }
      case 'isofade': {
        const L = lengths(p), g = L.on + L.off;
        if (mod(i, g) >= L.on) return OFF(p);
        return [cs[mod(Math.floor(i / g) + Math.floor(Math.abs(T) / 1.5), n)], 0.55 + 0.45 * Math.abs(Math.sin(Math.abs(T) * 2.1))];
      }
      case 'smooth': {
        const L = lengths(p), g = L.on + L.off, x = i - T * 8;
        return mod(x, g) < L.on ? [cs[mod(Math.floor(x / g), n)], 1] : OFF(p);
      }
      case 'twinkle': return rnd(i * 7 + Math.floor(Math.abs(T) * 3)) > 0.72 ? [cs[i % n], 1] : dim(p, cs[i % n], 0.22);
      case 'chase': { const s = Math.floor(T * 6), c = cs[mod(Math.floor((i - s) / 4), n)]; return mod(i - s, 8) < 5 ? [c, 1] : dim(p, c, 0.06); }
      case 'wave': return [cs[mod(Math.floor((i / 9 - T * 2) / Math.PI), n)], 0.2 + 0.8 * (Math.sin(i / 9 - T * 2) + 1) / 2];
      case 'pulse': return [cs[Math.floor(i / 12) % n], 0.25 + 0.75 * (Math.sin(Math.abs(T) * 2.5 + i / 40) + 1) / 2];
      case 'fade': return [cs[mod(Math.floor(i / 50) + Math.floor(Math.abs(T) / 1.5), n)], 0.55 + 0.45 * Math.abs(Math.sin(Math.abs(T) * 2.1))];
      case 'fireworks': {
        const b = Math.floor(i / 40), ph = mod(Math.abs(T) * 0.8 + rnd(b) * 3, 3), d = Math.abs((i % 40) - 20);
        return ph < 1 && d < ph * 20 ? [cs[b % n], 1 - ph * 0.7] : dim(p, cs[b % n], 0.05);
      }
      default: return [cs[mod(Math.floor((i - T * 8) / 20), n)], 1];
    }
  }
  // LEDs per colour band in colAt, by mode.
  const BAND = { chase: 4, wave: 9 * Math.PI, pulse: 12, fade: 50, fireworks: 40, flow: 20 };
  // The LED index that dot ``j`` of an ``n``-dot strip preview samples. The dots
  // span exactly one cycle of the pattern's colours, closer together than a band,
  // so every colour shows (a 3-colour motionless strip was all red at j*3 spacing).
  // Spaced designs (accent, isofade) sample LED by LED so the gaps show.
  function stripIndex(j, p, n) {
    n = n || 13;
    const mode = MODES[p.animation] || 'flow';
    if (mode === 'twinkle' || mode === 'accent' || mode === 'isofade') return j;
    const k = Math.max(1, (p.colors && p.colors.length) || 1);
    if (mode === 'solid') {
      const L = lengths(p).on;
      return L === 1 ? j : Math.floor((j + 0.5) * k * L / n);
    }
    if (mode === 'smooth') { const L = lengths(p); return Math.floor((j + 0.5) * (L.on + L.off) * k / n); }
    const band = BAND[mode];
    if (mode === 'fireworks') return Math.floor((j + 0.5) * k / n) * band + band / 2;
    return Math.floor((j + 0.5) * band * k / n);
  }
  // A card title without the animation it already shows as a label or an old copy suffix:
  // "Hungary - Motionless (copy)" -> "Hungary", "Motionless - Canucks" -> "Canucks".
  // "Untitled (n)" keeps its number: it's the only thing telling those designs apart.
  // ``mine`` (saved here, not stock) keeps "(copy)" / "(copy n)": it tells a copy from its original.
  const ALIASES = { motionless: ['motionless', 'solid', 'static', 'still'] };
  function titleTidy(name, animation, mine) {
    const full = String(name || '').trim();
    if (!full) return 'Untitled';
    const anim = String(animation || 'motionless').toLowerCase();
    const words = new Set([anim, anim.replace(/_/g, ' '), anim.replace(/_/g, '-')].concat(ALIASES[anim] || []));
    const copy = (mine ? / \((copy(?: \d+)?)\)$/i : / \((copy|\d+)\)$/i).exec(full);
    let core = copy ? full.slice(0, copy.index).trim() : full;
    for (const w of words) {
      const esc = w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const suffix = new RegExp('\\s*[-–—:|]\\s*' + esc + '$', 'i');
      const prefix = new RegExp('^' + esc + '\\s*[-–—:|]\\s*', 'i');
      const next = core.replace(suffix, '').replace(prefix, '').trim();
      if (next) core = next;
    }
    return core + (copy && (mine || /^untitled$/i.test(core)) ? copy[0] : '');
  }

  // Patterns in the order a group shows them: most played here (then most
  // recently), then most downloaded, then the order they came in.
  function orderGroup(pats, plays) {
    plays = plays || {};
    return (pats || []).map((p, i) => ({ p, i, pl: plays[p.id] || {} }))
      .sort((a, b) => ((b.pl.n || 0) - (a.pl.n || 0))
        || String(b.pl.at || '').localeCompare(String(a.pl.at || ''))
        || ((b.p.downloads || 0) - (a.p.downloads || 0))
        || (a.i - b.i))
      .map((x) => x.p);
  }
  const GROUP_PAGE = 18;

  // A new design's name: "Untitled", or "Untitled (n)" with the first free n.
  function nextUntitled(names) {
    const taken = new Set((names || []).map((n) => String(n || '').trim().toLowerCase()));
    if (!taken.has('untitled')) return 'Untitled';
    for (let i = 1; ; i++) if (!taken.has('untitled (' + i + ')')) return 'Untitled (' + i + ')';
  }

  // The name for a copy of ``name``: "Sunset" -> "Sunset (copy)", then "Sunset (copy 2)"... (first
  // free, ignoring case). A copy of a copy, or an old "Sunset (1)", counts from "Sunset".
  function nextCopyName(name, names) {
    let base = String(name || '').trim().replace(/\s*\((copy(?: \d+)?|\d+)\)$/i, '').trim();
    if (!base) base = 'Untitled';
    const taken = new Set((names || []).map((n) => String(n || '').trim().toLowerCase()));
    if (!taken.has((base + ' (copy)').toLowerCase())) return base + ' (copy)';
    for (let i = 2; ; i++) if (!taken.has((base + ' (copy ' + i + ')').toLowerCase())) return base + ' (copy ' + i + ')';
  }

  // Whether Create's "Discard changes" can put back the saved design it opened. ``orig`` is the
  // snapshot taken when Create opened ({key, id}), ``now`` the current {key, id}; ``key`` is the
  // design + folder as a string. A copy or new design made this visit has no saved original
  // (orig.id is ''), so Delete covers it instead.
  function canRevert(orig, now) {
    return !!(orig && now && orig.id && now.id === orig.id && now.key !== orig.key);
  }

  // Whether the lights' pattern is still the design pushed from Create, or someone changed it.
  // Name and brightness are ignored: the cloud may rename it and brightness is a live slider.
  function sameDesign(pattern, spec) {
    if (!pattern || !spec) return false;
    const pc = pattern.colors || [], sc = spec.colors || [];
    const prm = (x) => JSON.stringify(Object.keys((x && x.params) || {}).sort().map((k) => [k, x.params[k]]));
    return (pattern.animation || 'motionless') === (spec.animation || 'motionless')
      && pc.length === sc.length && pc.every((c, i) => sameColor(c, sc[i]))
      && (!spec.background || sameColor(pattern.background, spec.background))
      && (!spec.params || prm(pattern) === prm(spec));
  }

  // The device colour (gamma-corrected RGB, as service.hex_to_int) a screen colour is sent as.
  const LED_GAMMA = 2.8;
  function toDevice(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
    if (!m) return null;
    const v = parseInt(m[1], 16), rgb = [(v >> 16) & 255, (v >> 8) & 255, v & 255], top = Math.max(...rgb);
    return top ? rgb.map((c) => Math.round(top * Math.pow(c / top, LED_GAMMA))) : rgb;
  }
  // Whether two screen colours drive the lights the same (the cloud hands back the
  // device colour, so an edited colour comes back as a nearby shade).
  function sameColor(a, b) {
    if (String(a || '').toLowerCase() === String(b || '').toLowerCase()) return true;
    const x = toDevice(a), y = toDevice(b);
    return !!(x && y) && x.every((c, i) => Math.abs(c - y[i]) <= 2);
  }
  // How a pattern's brightness dims a drawn light: [glow, core] alpha factors.
  // Full brightness is [1, 1]; the core dims too so small previews show the change.
  function dimOf(p) {
    const b = Math.max(0, Math.min(1, ((p && p.brightness != null) ? p.brightness : 255) / 255));
    return [0.35 + 0.65 * b, 0.3 + 0.7 * b];
  }

  const api = { fitBox, offTimeOptions, nightMin, guestAfter, guestOk, heroHidden, GUEST_LAST, NIGHT_ENDS,
    mod, rnd, MODES, STILL, lengths, colAt, stripIndex, titleTidy, orderGroup, GROUP_PAGE, nextUntitled, nextCopyName, canRevert, sameDesign, sameColor, toDevice,
    dimOf };
  root.GemstoneCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
