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
  const anims = Object.keys(core.MODES).concat(['rainbow', 'unknown_future_anim']);
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

test('titleTidy: drops the animation the card already labels', () => {
  assert.equal(core.titleTidy('Hungary - Motionless (copy)', 'motionless'), 'Hungary (copy)');
  assert.equal(core.titleTidy('Slovenia - Wave', 'wave'), 'Slovenia');
  assert.equal(core.titleTidy('Motionless - Canucks', 'motionless'), 'Canucks');
  assert.equal(core.titleTidy('Candy Chase: chase', 'chase'), 'Candy Chase');
  assert.equal(core.titleTidy('Australia - Flag', 'motionless'), 'Australia - Flag');
  assert.equal(core.titleTidy('Motionless', 'motionless'), 'Motionless');
  assert.equal(core.titleTidy('Wave (2)', 'wave'), 'Wave (2)');
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
