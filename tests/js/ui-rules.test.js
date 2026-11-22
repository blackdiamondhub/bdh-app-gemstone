// Static checks for UI bugs people hit on the touchscreen. Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = path.join(__dirname, '../../gemstone');
const css = fs.readFileSync(path.join(base, 'static/gemstone/gemstone.css'), 'utf8');
const js = fs.readFileSync(path.join(base, 'static/gemstone/gemstone.js'), 'utf8');
const html = fs.readFileSync(path.join(base, 'templates/gemstone/index.html'), 'utf8');

// A selection ring drawn outside an element (box-shadow 0 0 0 Npx, not inset) is
// cut off by any scrolling/overflow-hidden parent. Draw it inside (::after with
// inset:0), like .g-card and .at. Exceptions must have room around them.
const OUTER_RING_OK = new Set([
  '.gs .hero.g-pvw', '.gs .hero.g-lv', // the hero sits in the page's padding
  '.gs .swc.sel', // .c-sws pads 12px on every side, more than the 8px ring
]);
test('selection rings are drawn inside the element', () => {
  const bad = [];
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const sel = m[1].trim();
    if (!/\.(on|sel|g-pvw|g-lv)\b/.test(sel)) continue;
    if (!/box-shadow:\s*0 0 0 \d/.test(m[2])) continue;
    sel.split(',').map((s) => s.trim()).forEach((s) => { if (!OUTER_RING_OK.has(s)) bad.push(s); });
  }
  assert.deepEqual(bad, [], 'outer box-shadow rings that a scrolling parent can clip');
});

// Every view other than Tonight needs an on-screen way back (the nav rail is
// hidden on small screens and guests don't think to use it).
test('every secondary view has a back button to Tonight', () => {
  const views = JSON.parse(js.match(/const VIEWS = (\[[^\]]*\])/)[1].replace(/'/g, '"'));
  for (const v of views.filter((x) => x !== 'tonight')) {
    const sec = html.match(new RegExp('<section[^>]*id="v-' + v + '"[\\s\\S]*?</section>'));
    assert.ok(sec, 'no section for ' + v);
    const ids = [...sec[0].matchAll(/<button[^>]*class="[^"]*\bc-back\b[^"]*"[^>]*id="([^"]+)"/g)].map((x) => x[1]);
    assert.ok(ids.length, v + ' has no back button');
    for (const id of ids) {
      assert.match(js, new RegExp("\\$\\('" + id + "'\\)\\.onclick = \\(\\) => \\{ location\\.hash = '#tonight'"),
        id + ' must route back to #tonight');
    }
  }
});

// The pinned bar is driven by an IntersectionObserver, which only fires on a
// change. Showing Tonight again must re-check it or the bar stays missing.
test('showing Tonight re-syncs the pinned bar', () => {
  const rv = js.match(/function renderView\(\) \{([\s\S]*?)\n  \}/)[1];
  assert.match(rv, /view === 'tonight'\) \{[^}]*syncPinned\(\)/);
  assert.match(js, /function syncPinned\(\)[\s\S]*?heroHidden\(/);
});
test('the swatch row leaves room for the 8px selected ring', () => {
  const pad = css.match(/\.gs \.c-sws\{[^}]*padding:([^;}]+)/)[1].trim().split(/\s+/).map((x) => parseInt(x, 10));
  assert.ok(pad.every((p) => p >= 8), 'c-sws padding ' + pad.join(' '));
});
