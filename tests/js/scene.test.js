// Runs the real Scene class (lifted out of gemstone.js) against fake canvases.
// Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const core = require('../../gemstone/static/gemstone/gemstone-core.js');

const js = fs.readFileSync(path.join(__dirname, '../../gemstone/static/gemstone/gemstone.js'), 'utf8');

function classSource(name) {
  const start = js.indexOf('class ' + name + ' {');
  assert.ok(start >= 0, 'class ' + name + ' not found');
  let depth = 0;
  for (let i = js.indexOf('{', start); i < js.length; i++) {
    if (js[i] === '{') depth++;
    else if (js[i] === '}' && --depth === 0) return js.slice(start, i + 1);
  }
  throw new Error('unbalanced class ' + name);
}

// A canvas that remembers whether anything visible is on it. Setting its size
// clears it, like a real one.
function fakeCanvas() {
  const c = { painted: false, style: {}, hidden: false };
  let w = 300, hh = 150;
  Object.defineProperty(c, 'width', { get: () => w, set: (v) => { w = v; c.painted = false; } });
  Object.defineProperty(c, 'height', { get: () => hh, set: (v) => { hh = v; c.painted = false; } });
  const g = {
    setTransform() {}, beginPath() {}, arc() {}, fillStyle: '',
    clearRect() { c.painted = false; },
    fill() { c.painted = true; },
  };
  c.getContext = () => g;
  return c;
}
function h(tag) {
  return tag === 'canvas' ? fakeCanvas() : { style: {}, hidden: false, textContent: '', innerHTML: '', appendChild() {} };
}

function makeScene(opts) {
  const factory = new Function('h', 'clamp', 'rnd', 'colAt', 'DOT_GAP', 'window', 'rgba', 'HOUSE', 'aspectOf', 'photoUrl', 'dimOf',
    classSource('Scene') + '\nreturn Scene;');
  const Scene = factory(h, (v, lo, hi) => Math.max(lo, Math.min(hi, v)), core.rnd, core.colAt, 1 / 30,
    { GemstoneCore: core, devicePixelRatio: 1 }, () => 'rgba(0,0,0,1)', '',
    (lay) => (lay && lay.photo && lay.aspect > 0 ? lay.aspect : 2), () => '', core.dimOf);
  const el = { clientWidth: 400, clientHeight: 120, offsetParent: {}, prepend() {} };
  return { sc: new Scene(el, opts), el };
}

const ctrl = { id: 'c1', layout: { photo: false, runs: [{ points: [[0.1, 0.5], [0.9, 0.5]] }] } };
const still = { name: 'Hungary', colors: ['#ff0000', '#ffffff', '#00ff00'], brightness: 200, animation: 'motionless' };
const moving = Object.assign({}, still, { animation: 'wave' });

// Bug: the pinned slim preview blinked off every 30 s, because the state
// refresh re-rendered it and re-fitting cleared its lights until the next tick.
for (const [label, opts] of [['big', {}], ['slim', { crop: true, small: true }], ['inset', { small: true }]]) {
  for (const [kind, pat] of [['still', still], ['animated', moving]]) {
    test(`${label} preview (${kind}) stays lit when re-rendered between ticks`, () => {
      const { sc, el } = makeScene(opts);
      sc.set(ctrl);
      sc.draw(pat, true, 1);
      assert.ok(sc.fx.painted, 'first frame painted');
      // A state refresh re-renders: set() again with the same controller, then fit().
      sc.set(ctrl);
      sc.fit();
      assert.ok(sc.fx.painted, 'lights blank after a re-render');
      // And a real resize.
      el.clientWidth = 500;
      sc.fit();
      assert.ok(sc.fx.painted, 'lights blank after a resize');
    });
  }
}
