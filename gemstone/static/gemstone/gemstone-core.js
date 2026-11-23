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
  // LEDs per colour band in colAt, by mode.
  const BAND = { chase: 4, wave: 9 * Math.PI, pulse: 12, fade: 50, fireworks: 40, flow: 20 };
  // The LED index that dot ``j`` of an ``n``-dot strip preview samples. The dots
  // span exactly one cycle of the pattern's colours, closer together than a band,
  // so every colour shows (a 3-colour motionless strip was all red at j*3 spacing).
  function stripIndex(j, p, n) {
    n = n || 13;
    const mode = MODES[p.animation] || 'flow';
    if (mode === 'solid' || mode === 'twinkle') return j;
    const k = Math.max(1, (p.colors && p.colors.length) || 1);
    const band = BAND[mode];
    if (mode === 'fireworks') return Math.floor((j + 0.5) * k / n) * band + band / 2;
    return Math.floor((j + 0.5) * band * k / n);
  }

  // A card title without the animation it already shows as a label or an old copy suffix:
  // "Hungary - Motionless (copy)" -> "Hungary", "Motionless - Canucks" -> "Canucks".
  // "Untitled (n)" keeps its number: it's the only thing telling those designs apart.
  const ALIASES = { motionless: ['motionless', 'solid', 'static', 'still'] };
  function titleTidy(name, animation) {
    const full = String(name || '').trim();
    if (!full) return 'Untitled';
    const anim = String(animation || 'motionless').toLowerCase();
    const words = new Set([anim, anim.replace(/_/g, ' '), anim.replace(/_/g, '-')].concat(ALIASES[anim] || []));
    const copy = / \((copy|\d+)\)$/i.exec(full);
    let core = copy ? full.slice(0, copy.index).trim() : full;
    for (const w of words) {
      const esc = w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const suffix = new RegExp('\\s*[-–—:|]\\s*' + esc + '$', 'i');
      const prefix = new RegExp('^' + esc + '\\s*[-–—:|]\\s*', 'i');
      const next = core.replace(suffix, '').replace(prefix, '').trim();
      if (next) core = next;
    }
    return core + (copy && /^untitled$/i.test(core) ? copy[0] : '');
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

  // The name for a copy of ``name``: "Sunset" -> "Sunset 2", then "Sunset 3"... (first free,
  // ignoring case). A copy of "Sunset 2" or the old "Sunset (copy)" / "Sunset (1)" counts from "Sunset".
  function nextCopyName(name, names) {
    let base = String(name || '').trim().replace(/\s*\((copy|\d+)\)$/i, '').trim();
    const num = /^(.*\S)\s+\d+$/.exec(base);
    if (num) base = num[1];
    if (!base) base = 'Untitled';
    const taken = new Set((names || []).map((n) => String(n || '').trim().toLowerCase()));
    for (let i = 2; ; i++) if (!taken.has((base + ' ' + i).toLowerCase())) return base + ' ' + i;
  }

  // Whether the lights' pattern is still the design pushed from Create, or someone changed it.
  // Name and brightness are ignored: the cloud may rename it and brightness is a live slider.
  function sameDesign(pattern, spec) {
    if (!pattern || !spec) return false;
    const cols = (a) => (a || []).map((c) => String(c).toLowerCase()).join();
    return (pattern.animation || 'motionless') === (spec.animation || 'motionless')
      && cols(pattern.colors) === cols(spec.colors);
  }

  // How a pattern's brightness dims a drawn light: [glow, core] alpha factors.
  // Full brightness is [1, 1]; the core dims too so small previews show the change.
  function dimOf(p) {
    const b = Math.max(0, Math.min(1, ((p && p.brightness != null) ? p.brightness : 255) / 255));
    return [0.35 + 0.65 * b, 0.3 + 0.7 * b];
  }

  const api = { fitBox, offTimeOptions, nightMin, guestAfter, guestOk, heroHidden, GUEST_LAST, NIGHT_ENDS,
    mod, rnd, MODES, STILL, colAt, stripIndex, titleTidy, orderGroup, GROUP_PAGE, nextUntitled, nextCopyName, sameDesign,
    dimOf };
  root.GemstoneCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
