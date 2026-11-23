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

// The body of ``function name(...) {...}`` in gemstone.js (naive brace match; fine for this file).
function body(name) {
  const i = js.search(new RegExp('function ' + name + '\\('));
  assert.ok(i >= 0, 'no function ' + name);
  let d = 0, j = js.indexOf('{', i);
  for (let k = j; k < js.length; k++) {
    if (js[k] === '{') d++;
    else if (js[k] === '}' && --d === 0) return js.slice(j, k + 1);
  }
  return js.slice(j);
}

test('card menu: stock designs get no Delete, only deletable ones do', () => {
  const info = body('designInfo');
  assert.match(info, /const stock = !mine \|\| !!mine\.stock/, 'library patterns (not in OURS) are stock');
  assert.match(info, /canDelete: !stock && \(S\.admin \|\| guest\)/);
  const menu = body('openCardMenu');
  assert.match(menu, /if \(info\.canDelete\) \{[^}]*Delete/, 'Delete is only added when allowed (hidden, not disabled)');
  assert.ok(!/disabled|'dis'/.test(menu), 'unavailable items are hidden, not disabled');
});

// The Custom tag and the heart must read the same wherever a card shows (a design was "Custom"
// in Favourites but not in its own folder, and favourites had no heart).
test('ownership tag and heart show on every card, whatever row it is in', () => {
  const c = body('card');
  assert.match(c, /if \(!info\.stock\) \{/);
  assert.match(c, /if \(isFav\(p\)\) \{/);
  assert.ok(!/opts\.(folder|favRow)/.test(c), 'card() hides the tag or heart in some rows');
  assert.ok(!/folder: true|favRow/.test(js), 'a caller still asks card() to hide them');
});
// Cloud writes take seconds. A tap on a card must change the screen first and write behind it
// (delete waited for the cloud delete and a full reload before the card went).
test('card actions update the screen before waiting on the cloud', () => {
  for (const name of ['deleteDesign', 'restoreDesign', 'setFav']) {
    const b = body(name);
    const r = b.indexOf('renderView()'), a = b.indexOf('await ');
    assert.ok(r >= 0 && r < a, name + ' awaits before it re-renders');
  }
  assert.ok(body('deleteDesign').indexOf('toast(') < body('deleteDesign').indexOf('await '), 'the Undo toast waits on the cloud');
  assert.match(body('loadOurs'), /gone\.has/, 'a reload mid-delete would bring the card back');
});
// A bare 🏠 on phones didn't say what it meant; the word must show at every width.
test('the Custom/Guest word is never hidden', () => {
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    if (/\.g-ownl\b/.test(m[1])) assert.ok(!/display:\s*none|visibility:\s*hidden/.test(m[2]), m[1].trim() + ' hides it');
  }
});

// Auto-save writes every edit, so Create must offer a way back to the design it opened with.
test('Discard changes cancels the pending save and saves the original back', () => {
  assert.match(html, /<button[^>]*id="c-discard"[^>]*hidden/);
  const d = body('discardEdits');
  assert.match(d, /if \(!revertable\(\)\) return/);
  assert.match(d, /clearTimeout\(C\.saveT\); C\.again = false/, 'a pending auto-save would re-apply the edits');
  assert.match(d, /o\.spec/);
  assert.match(d, /saveDesign\(\)/);
  assert.match(d, /if \(T\)[^\n]*pushLive\(\)/, 'a running try must show the original again');
  assert.match(body('renderCreateHeader'), /dc\.hidden = !revertable\(\)/);
  assert.match(body('openCreate'), /snapshot\(\);\s*location\.hash = '#create'/, 'snapshot after the copy name is set');
});

test('no Admin/guest label in the rail', () => {
  assert.ok(!/gs-who/.test(js + html), 'gs-who is back');
  assert.ok(!/\.who\b/.test(css), '.who rules are back');
});

test('lights badge says LIVE, not ON LIGHTS', () => {
  assert.ok(!/on lights/i.test(js + html));
  assert.ok(/'LIVE'/.test(js));
});

// Create must never touch the house except through an explicit "Show on house" try.
test('Create has no lights switch; edits only go live during a try', () => {
  const hero = html.match(/<div class="hero c-hero"[\s\S]*?<\/div>\s*<button class="c-show"/);
  assert.ok(hero, 'c-hero / c-show markup moved');
  assert.ok(!/data-lights|g-lights/.test(hero[0]), 'a lights switch is back in the Create hero');
  const ch = body('changed');
  assert.ok(/if \(T\)[^\n]*pushLive/.test(ch), 'changed() must push only while a try is running');
  assert.ok(!/lightsOn\(\)/.test(ch), 'changed() must not go live just because the lights are on');
  assert.ok(/if \(!T\) return/.test(body('pushLive')), 'pushLive must bail when the try has ended');
});

test('a try asks the server for an undo token and gives it back when it ends', () => {
  assert.ok(/start: true/.test(body('startTry')));
  const e = body('endTry');
  assert.ok(/url\('undo'\)/.test(e) && /token: t\.undo/.test(e));
  assert.ok(/url\('power'\)[^\n]*on: false/.test(e), 'nothing was on before: turn it back off');
});

test('leaving Create ends the try (puts the house back)', () => {
  assert.ok(/if \(T && view === 'create' && v !== 'create'\) endTry\(true, true\)/.test(body('routeFromHash')));
});

// The slim strip and the big scene must dim the same way with brightness.
test('both previews dim with brightness via dimOf', () => {
  assert.ok(/dimOf\(p\)/.test(body('strip')), 'strip() ignores brightness');
  assert.ok((js.match(/dimOf\(/g) || []).length >= 2, 'Scene ignores brightness');
});
