// Pure helpers behind the Tonight page. Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../../gemstone/static/gemstone/gemstone-core.js');

// Small deterministic PRNG so failures reproduce.
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 2 ** 32; };
}

test('fitBox: every light lands inside the thumbnail, clear of the edges', () => {
  const r = rng(7);
  for (let i = 0; i < 2000; i++) {
    const aspect = 0.4 + r() * 3;
    const boxW = 80 + r() * 400, boxH = 60 + r() * 300, pad = 0.08;
    const n = 1 + Math.floor(r() * 40);
    // Mix of spread-out layouts, tight clusters, flat lines and lights right at the photo's edges.
    const cx = r(), cy = r(), spread = r() < 0.3 ? 0.02 : r(), flat = r() < 0.2;
    const pts = Array.from({ length: n }, () => [
      Math.min(1, Math.max(0, r() < 0.1 ? Math.round(r()) : cx + (r() - 0.5) * spread)),
      Math.min(1, Math.max(0, flat ? cy : cy + (r() - 0.5) * spread)),
    ]);
    const f = core.fitBox(pts, aspect, boxW, boxH, pad);
    assert.ok(Math.abs(f.pw / f.ph - aspect) < 1e-9, 'picture keeps its aspect ratio');
    const xs = pts.map((p) => f.ox + p[0] * f.pw), ys = pts.map((p) => f.oy + p[1] * f.ph);
    const eps = 1e-6;
    for (let k = 0; k < n; k++) {
      assert.ok(xs[k] >= -eps && xs[k] <= boxW + eps && ys[k] >= -eps && ys[k] <= boxH + eps,
        `light ${pts[k]} cut off (case ${i}): ${xs[k]},${ys[k]} in ${boxW}x${boxH}`);
    }
    // The padding is kept on every side, and the lights fill the box one way (contain, not smaller).
    const gapL = Math.min(...xs), gapR = boxW - Math.max(...xs), gapT = Math.min(...ys), gapB = boxH - Math.max(...ys);
    const minGap = Math.min(gapL, gapR, gapT, gapB);
    assert.ok(minGap > 0, `no margin round the lights (case ${i})`);
    const fillsW = Math.abs(gapL - gapR) < 1e-6 && Math.abs(gapL - minGap) < 1e-6;
    const fillsH = Math.abs(gapT - gapB) < 1e-6 && Math.abs(gapT - minGap) < 1e-6;
    assert.ok(fillsW || fillsH, `lights not scaled to fit (case ${i})`);
  }
});

test('fitBox: a single light or none still gives a usable picture', () => {
  const one = core.fitBox([[0.5, 0.5]], 1.5, 200, 100);
  assert.ok(Number.isFinite(one.s) && one.s > 0);
  assert.ok(Math.abs(one.ox + 0.5 * one.pw - 100) < 1e-9 && Math.abs(one.oy + 0.5 * one.ph - 50) < 1e-9);
  // No layout: the whole picture, contained.
  const none = core.fitBox([], 2, 200, 100);
  assert.ok(none.pw <= 200 + 1e-9 && none.ph <= 100 + 1e-9);
  assert.ok(Math.abs(none.pw - 200) < 1e-9 || Math.abs(none.ph - 100) < 1e-9);
});

const ON_TIMES = [null, '16:30', '17:00', '17:07', '18:45', '19:59', '21:30', '21:45', '21:59', '22:00', '22:30'];
const hhmm = (m) => String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');

test('offTimeOptions: guests only get times after the on time and by 10 PM', () => {
  const lim = core.nightMin(core.GUEST_LAST);
  for (const on of ON_TIMES) {
    for (const fallback of [undefined, '18:00']) {
      const after = core.nightMin(on || fallback || '17:00');
      const opts = core.offTimeOptions({ on_at: on }, { admin: false, guestFallback: fallback });
      for (const t of opts) {
        assert.ok(core.nightMin(t) > after, `${t} not after on time ${on || fallback}`);
        assert.ok(core.nightMin(t) <= lim, `${t} after 10 PM`);
      }
      // Nothing allowed is left out.
      for (let m = 17 * 60; m <= lim; m += 15) {
        if (m > after) assert.ok(opts.includes(hhmm(m)), `${hhmm(m)} missing for on time ${on}`);
      }
    }
  }
  assert.deepEqual(core.offTimeOptions({ on_at: '22:00' }, {}), []);
  assert.deepEqual(core.offTimeOptions({ on_at: '21:45' }, {}), ['22:00']);
});

test("offTimeOptions: an admin's later off time stays selectable for a guest, nothing else extra", () => {
  for (const on of ON_TIMES) {
    for (const off of ['17:15', '21:00', '22:00', '23:30', '01:00']) {
      const opts = core.offTimeOptions({ on_at: on, off_time: off }, { admin: false });
      const after = core.nightMin(on || '17:00');
      const extra = opts.filter((t) => !core.guestOk({ on_at: on }, t));
      if (core.nightMin(off) > after && !core.guestOk({ on_at: on }, off)) assert.deepEqual(extra, [off]);
      else assert.deepEqual(extra, []);
      if (core.nightMin(off) <= after) assert.ok(!opts.includes(off), `${off} offered before on time ${on}`);
      const sorted = opts.slice().sort((a, b) => core.nightMin(a) - core.nightMin(b));
      assert.deepEqual(opts, sorted, 'in night order');
    }
  }
});

test('offTimeOptions: admins get 5 PM to 3 AM in night order', () => {
  const opts = core.offTimeOptions({ on_at: '21:00' }, { admin: true });
  assert.equal(opts[0], '17:00');
  assert.equal(opts[opts.length - 1], '03:00');
  assert.equal(opts.length, 41);
  assert.ok(opts.indexOf('23:45') < opts.indexOf('00:00'));
});

test('heroHidden matches the pinned bar observer', () => {
  const root = { top: 0, bottom: 1000, height: 1000 };
  const hero = (top, h) => ({ top, bottom: top + h, height: h });
  assert.equal(core.heroHidden(hero(28, 500), root, 140), false, 'at the top');
  assert.equal(core.heroHidden(hero(-300, 500), root, 140), false, 'still showing under the bar');
  assert.equal(core.heroHidden(hero(-360, 500), root, 140), true, 'tucked under the bar');
  assert.equal(core.heroHidden(hero(-4000, 500), root, 140), true, 'scrolled far down');
  // A hidden view lays out at zero size; that must never pin.
  assert.equal(core.heroHidden({ top: 0, bottom: 0, height: 0 }, root, 140), false);
  assert.equal(core.heroHidden(hero(-4000, 500), { top: 0, bottom: 0, height: 0 }, 140), false);
});

test('stripIndex: every colour of every pattern shows on a card strip', () => {
  // accent/isofade draw their off gaps on purpose (see the accent test below).
  const anims = Object.keys(core.MODES).filter((a) => !['accent', 'isofade'].includes(a))
    .concat(['rainbow', 'unknown_future_anim']);
  const pal = ['#100000', '#200000', '#300000', '#400000', '#500000', '#600000',
    '#700000', '#800000', '#900000', '#a00000', '#b00000', '#c00000'];
  for (const animation of anims) {
    for (let k = 1; k <= pal.length; k++) {
      for (const direction of [0, 1]) {
        const p = { animation, colors: pal.slice(0, k), speed: 128, direction };
        const seen = new Set();
        for (let j = 0; j < 13; j++) seen.add(core.colAt(core.stripIndex(j, p, 13), p, 1.3)[0]);
        if (k <= 13) assert.equal(seen.size, k, `${animation} with ${k} colours showed ${seen.size}`);
      }
    }
  }
});

test('accent: "1 on, 3 off" lights every 4th light and leaves the rest dark', () => {
  const p = { animation: 'accent', colors: ['#ff7700'], params: { on: 1, off: 3 }, speed: 128, direction: 0 };
  for (let i = 0; i < 40; i++) {
    const [c, a] = core.colAt(i, p, 2.5);
    if (i % 4 === 0) assert.deepEqual([c.toLowerCase(), a > 0], ['#ff7700', true], `light ${i}`);
    else assert.equal(a, 0, `light ${i} should be off`);
  }
  // The card strip shows the gaps too, not a solid bar.
  const strip = Array.from({ length: 13 }, (_, j) => core.colAt(core.stripIndex(j, p, 13), p, 1.3)[1] > 0);
  assert.ok(strip.some(Boolean) && !strip.every(Boolean), 'strip shows lights and gaps');
  // Groups take the colours in turn; a background colour fills the off lights.
  const q = { animation: 'accent', colors: ['#ff0000', '#00ff00'], params: { on: 2, off: 1 }, background: '#0000ff' };
  assert.deepEqual([0, 1, 2, 3, 4, 5].map((i) => core.colAt(i, q, 0)[0].toLowerCase()),
    ['#ff0000', '#ff0000', '#0000ff', '#00ff00', '#00ff00', '#0000ff']);
  assert.ok(core.colAt(2, q, 0)[1] > 0, 'background is lit');
});

test('motionless: a length repeats each colour', () => {
  const p = { animation: 'motionless', colors: ['#ff0000', '#00ff00'], params: { length: 3 } };
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6].map((i) => core.colAt(i, p, 0)[0]),
    ['#ff0000', '#ff0000', '#ff0000', '#00ff00', '#00ff00', '#00ff00', '#ff0000']);
  const seen = new Set();
  for (let j = 0; j < 13; j++) seen.add(core.colAt(core.stripIndex(j, p, 13), p, 1.3)[0]);
  assert.equal(seen.size, 2, 'both colours on the card strip');
});

test('sameDesign: background, params and the device round trip of a colour', () => {
  const live = { animation: 'accent', colors: ['#ff7600'], params: { on: 1, off: 3 } };
  assert.ok(core.sameDesign(live, { animation: 'accent', colors: ['#ff7700'] }), 'nearby shade is the same light');
  assert.ok(core.sameDesign(live, { animation: 'accent', colors: ['#ff7700'], params: { off: 3, on: 1 } }));
  assert.ok(!core.sameDesign(live, { animation: 'accent', colors: ['#ff7700'], params: { on: 2, off: 3 } }));
  assert.ok(!core.sameDesign(live, { animation: 'accent', colors: ['#ff7700'], background: '#0000ff' }));
  assert.ok(!core.sameDesign(live, { animation: 'accent', colors: ['#ff0000'] }));
});

test('titleTidy: drops the animation the card already labels', () => {
  assert.equal(core.titleTidy('Hungary - Motionless (copy)', 'motionless'), 'Hungary');
  assert.equal(core.titleTidy('Slovenia - Wave', 'wave'), 'Slovenia');
  assert.equal(core.titleTidy('Motionless - Canucks', 'motionless'), 'Canucks');
  assert.equal(core.titleTidy('Candy Chase: chase', 'chase'), 'Candy Chase');
  assert.equal(core.titleTidy('Australia - Flag', 'motionless'), 'Australia - Flag');
  assert.equal(core.titleTidy('Motionless', 'motionless'), 'Motionless');
  assert.equal(core.titleTidy('Wave (2)', 'wave'), 'Wave');
  assert.equal(core.titleTidy('Sunset (copy)', 'wave'), 'Sunset');
  assert.equal(core.titleTidy('Untitled (1)', 'wave'), 'Untitled (1)');
  assert.equal(core.titleTidy('', 'wave'), 'Untitled');
  assert.equal(core.titleTidy(null), 'Untitled');
});

test('orderGroup: most played, then most recent, then downloads, then original order', () => {
  const pats = ['a', 'b', 'c', 'd', 'e'].map((id, i) => ({ id, downloads: [5, 0, 9, 0, 0][i] }));
  const plays = { b: { n: 3, at: '2026-01-01' }, d: { n: 3, at: '2026-02-01' }, e: { n: 1, at: '2026-03-01' } };
  assert.deepEqual(core.orderGroup(pats, plays).map((p) => p.id), ['d', 'b', 'e', 'c', 'a']);
  assert.deepEqual(core.orderGroup(pats).map((p) => p.id), ['c', 'a', 'b', 'd', 'e']);
  assert.equal(core.GROUP_PAGE, 18);
});

test('nextUntitled: Untitled, then the first free (n), ignoring case and spaces', () => {
  assert.equal(core.nextUntitled([]), 'Untitled');
  assert.equal(core.nextUntitled(['Sunset']), 'Untitled');
  assert.equal(core.nextUntitled([' untitled ']), 'Untitled (1)');
  assert.equal(core.nextUntitled(['Untitled', 'Untitled (1)', 'Untitled (3)']), 'Untitled (2)');
});

test('nextCopyName: "Name (copy)", then "(copy 2)"..., from the base name', () => {
  assert.equal(core.nextCopyName('Sunset', ['Sunset']), 'Sunset (copy)');
  assert.equal(core.nextCopyName('Sunset', ['Sunset', 'sunset (copy)']), 'Sunset (copy 2)');
  assert.equal(core.nextCopyName('Sunset (copy)', ['Sunset', 'Sunset (copy)', 'Sunset (copy 2)']), 'Sunset (copy 3)');
  assert.equal(core.nextCopyName('Sunset (copy 2)', ['Sunset (copy 2)']), 'Sunset (copy)');
  assert.equal(core.nextCopyName('Wave (4)', []), 'Wave (copy)');
  assert.equal(core.nextCopyName('Route 66', ['Route 66']), 'Route 66 (copy)', 'a trailing number is part of the name');
  assert.equal(core.nextCopyName('', []), 'Untitled (copy)');
});

test('titleTidy: my designs keep "(copy)" so a copy is told apart from its original', () => {
  assert.equal(core.titleTidy('Megan (copy)', 'wave', true), 'Megan (copy)');
  assert.equal(core.titleTidy('Hungary - Motionless (copy 2)', 'motionless', true), 'Hungary (copy 2)');
  assert.equal(core.titleTidy('Sunset (copy)', 'wave'), 'Sunset', 'stock: old copy suffix still dropped');
  assert.equal(core.titleTidy('Wave (2)', 'wave', true), 'Wave (2)');
});

test('canRevert: only an edited saved design, never a copy/new one or an unchanged one', () => {
  assert.equal(core.canRevert({ key: 'a', id: 'x' }, { key: 'b', id: 'x' }), true);
  assert.equal(core.canRevert({ key: 'a', id: 'x' }, { key: 'a', id: 'x' }), false, 'nothing changed');
  assert.equal(core.canRevert({ key: 'a', id: '' }, { key: 'b', id: 'y' }), false, 'copy/new made this visit: Delete covers it');
  assert.equal(core.canRevert({ key: 'a', id: 'x' }, { key: 'b', id: 'z' }), false, 'a different design');
  assert.equal(core.canRevert(null, { key: 'b', id: 'x' }), false);
});

test('sameDesign: matches on animation and colours only', () => {
  const spec = { animation: 'twinkle', colors: ['#FF0000', '#00ff00'], name: 'Mine', brightness: 200 };
  assert.ok(core.sameDesign({ animation: 'twinkle', colors: ['#ff0000', '#00FF00'], name: 'Renamed', brightness: 40 }, spec));
  assert.ok(!core.sameDesign({ animation: 'chase', colors: spec.colors }, spec));
  assert.ok(!core.sameDesign({ animation: 'twinkle', colors: ['#ff0000'] }, spec));
  assert.ok(core.sameDesign({ colors: [] }, { animation: 'motionless', colors: [] }));
  assert.ok(!core.sameDesign(null, spec));
});

test('dimOf: brightness dims both glow and core, monotonically, never to black', () => {
  assert.deepEqual(core.dimOf({ brightness: 255 }), [1, 1]);
  assert.deepEqual(core.dimOf({}), [1, 1]);
  let prev = [-1, -1];
  for (let b = 0; b <= 255; b += 5) {
    const d = core.dimOf({ brightness: b });
    assert.ok(d[0] > prev[0] && d[1] > prev[1], 'not increasing at ' + b);
    assert.ok(d[0] > 0.2 && d[1] > 0.2 && d[0] <= 1 && d[1] <= 1);
    prev = d;
  }
  assert.ok(core.dimOf({ brightness: 60 })[1] < 0.6, 'core must dim visibly for the slim preview');
});
