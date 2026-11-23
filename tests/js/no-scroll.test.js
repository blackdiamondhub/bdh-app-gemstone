// Tapping a pattern must never move the page (people try the next card along).
// Builds a rough call graph of gemstone.js and checks nothing reachable from a
// tap (play, preview, favourite) scrolls, focuses or re-routes.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '../../gemstone/static/gemstone/gemstone.js'), 'utf8');

// From an opening bracket at i, the index just past its match (strings and comments skipped).
function matchEnd(s, i) {
  const open = s[i], close = { '{': '}', '(': ')', '[': ']' }[open];
  let depth = 0;
  for (; i < s.length; i++) {
    const ch = s[i];
    if (ch === "'" || ch === '"' || ch === '`') {
      for (i++; i < s.length && s[i] !== ch; i++) if (s[i] === '\\') i++;
      continue;
    }
    if (ch === '/' && s[i + 1] === '/') { i = s.indexOf('\n', i); continue; }
    if (ch === open) depth++;
    else if (ch === close && --depth === 0) return i + 1;
  }
  throw new Error('unbalanced ' + open);
}

// End of the statement starting at i (first ; or newline at depth 0).
function stmtEnd(s, i) {
  for (; i < s.length; i++) {
    const ch = s[i];
    if ('{(['.includes(ch)) { i = matchEnd(s, i) - 1; continue; }
    if (ch === "'" || ch === '"' || ch === '`') { for (i++; s[i] !== ch; i++) if (s[i] === '\\') i++; continue; }
    if (ch === ';' || ch === '\n') return i;
  }
  return i;
}

function functions() {
  const out = new Map();
  const decl = /(?:async\s+)?function\s+(\w+)\s*\(/g;
  let m;
  while ((m = decl.exec(src))) {
    const args = matchEnd(src, m.index + m[0].length - 1);
    const b = src.indexOf('{', args);
    out.set(m[1], src.slice(b, matchEnd(src, b)));
  }
  const arrow = /(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*/g;
  while ((m = arrow.exec(src))) {
    const at = m.index + m[0].length;
    out.set(m[1], src[at] === '{' ? src.slice(at, matchEnd(src, at)) : src.slice(at, stmtEnd(src, at)));
  }
  return out;
}

// Code that only runs on a later, separate user action: element handlers and toast actions ({ onTap }).
function withoutHandlers(body) {
  const re = /\.on\w+\s*=|\bon[A-Z]\w*\s*:|addEventListener\s*\(/g;
  let out = '', last = 0, m;
  while ((m = re.exec(body))) {
    if (m.index < last) continue;
    const end = m[0].endsWith('(') ? matchEnd(body, m.index + m[0].length - 1) : stmtEnd(body, m.index + m[0].length);
    out += body.slice(last, m.index);
    last = end;
    re.lastIndex = end;
  }
  return out + body.slice(last);
}

const FNS = functions();
function reachable(starts) {
  const seen = new Set(), todo = starts.slice();
  while (todo.length) {
    const f = todo.pop();
    if (seen.has(f) || !FNS.has(f)) continue;
    seen.add(f);
    const body = withoutHandlers(FNS.get(f));
    for (const name of FNS.keys()) if (new RegExp('\\b' + name + '\\b').test(body)) todo.push(name);
  }
  return seen;
}

// What a card / solid tile tap runs (not ⋯, which opens the card menu), plus the menu's favourite toggle.
function tapEntries() {
  let code = '';
  for (const fn of ['card', 'solidTile']) {
    const body = FNS.get(fn), re = /(\w+)\.onclick\s*=/g;
    let m;
    while ((m = re.exec(body))) if (m[1] !== 'dots') code += body.slice(m.index, stmtEnd(body, m.index + m[0].length)) + '\n';
  }
  return [...FNS.keys()].filter((n) => new RegExp('\\b' + n + '\\b').test(code)).concat(['setFav']);
}

test('the parser finds the play path', () => {
  for (const name of ['card', 'solidTile', 'choose', 'play', 'playPattern', 'setFav', 'renderView', 'scrollToAll']) {
    assert.ok(FNS.has(name), name);
  }
  const starts = tapEntries();
  for (const name of ['playPattern', 'choose', 'setFav']) assert.ok(starts.includes(name), name);
  const r = reachable(starts);
  for (const name of ['play', 'renderView', 'renderTonight', 'renderAll', 'fillCards', 'renderMiniScene']) {
    assert.ok(r.has(name), `${name} should be reachable from a tap`);
  }
});

test('tapping a pattern never scrolls the page', () => {
  const r = reachable(tapEntries());
  for (const name of ['scrollToAll', 'pickCat', 'routeFromHash']) {
    assert.ok(!r.has(name), `${name} is reachable from a pattern tap`);
  }
  const bad = /\bscroll(?:To|By|IntoView)\s*\(|\bscrollTop\s*[-+]?=|\.focus\s*\(|location\.hash\s*=/;
  for (const name of r) {
    const hit = withoutHandlers(FNS.get(name)).match(bad);
    assert.ok(!hit, `${name}() (reachable from a pattern tap) does ${hit && hit[0]}`);
  }
});

test('cards are swapped in one go so the page height never dips', () => {
  const fill = FNS.get('fillCards');
  assert.match(fill, /replaceChildren\(/);
  assert.doesNotMatch(fill, /textContent\s*=\s*''/);
});

test("the pinned bar's picture is cropped to every light with fitBox", () => {
  const i = src.indexOf('if (this.opts.crop)');
  assert.ok(i > 0);
  const block = src.slice(i, matchEnd(src, src.indexOf('{', i)));
  assert.match(block, /this\.runs\.forEach\(\(\w+\) => \w+\.points\.forEach\(\(p\) => pts\.push\(p\)\)\)/);
  assert.match(block, /GemstoneCore\.fitBox\(pts,/);
  assert.match(FNS.get('renderMiniScene'), /crop:\s*true/);
});
