/* Regression test for remembered window geometry (windowRect / rectIsSane in app/app.js).
 *
 * Run: node test_window_rect.js
 *
 * WHY THIS IS TESTED RATHER THAN LOOKED AT. `--window-position` takes the window FRAME's top-left;
 * `screenX`/`screenY` report the VIEWPORT's. Store one and restore it as the other and the window
 * drops by the title-bar height every single session — it creeps down the screen until it walks off
 * the bottom. The first launch or two look perfect, so eyeballing it actively cannot catch this.
 * The round-trip case below is the one that matters: feed the stored rect back in as the restored
 * position and the window must land exactly where it started, repeatedly.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');
const a = src.indexOf('function windowRect()');
const end = src.indexOf('\n}', src.indexOf('function rectIsSane('));
if (a < 0 || end < 0) throw new Error('could not find windowRect/rectIsSane in app/app.js');
const BODY = src.slice(a, end + 2);

// A stand-in for a Chromium --app window: TITLE is the title bar, BORDER the side/bottom frame.
// The browser reports the VIEWPORT position; the frame sits above and left of it.
function makeWindow(frameX, frameY, frameW, frameH, TITLE, BORDER) {
  return {
    screenX: frameX + BORDER,
    screenY: frameY + TITLE,
    outerWidth: frameW,
    outerHeight: frameH,
    innerWidth: frameW - BORDER * 2,
    innerHeight: frameH - TITLE - BORDER,
  };
}
function build(win) {
  return new Function('window', BODY + '\nreturn { windowRect, rectIsSane };')(win);
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

console.log('\nRemembered window geometry\n');

// 1. A Chromium --app window on Win11: 39px title bar, no visible side border.
{
  const m = build(makeWindow(300, 200, 1400, 900, 39, 0));
  const r = m.windowRect();
  check('recovers the FRAME position, not the viewport',
    r.x === 300 && r.y === 200, JSON.stringify(r));
  check('  and reports the outer size', r.w === 1400 && r.h === 900, JSON.stringify(r));
}

// 2. THE ONE THAT MATTERS. Restore the stored rect as the window's frame, measure again, and it
//    must be identical — ten times over. Any per-cycle offset is the creeping-window bug, and it is
//    invisible in a single pass.
{
  const TITLE = 39, BORDER = 0;
  let cur = { x: 300, y: 200, w: 1400, h: 900 };
  const first = { ...cur };
  for (let i = 0; i < 10; i++) {
    const m = build(makeWindow(cur.x, cur.y, cur.w, cur.h, TITLE, BORDER));
    cur = m.windowRect();
  }
  check('survives ten open/close cycles without drifting',
    cur.x === first.x && cur.y === first.y && cur.w === first.w && cur.h === first.h,
    `started ${JSON.stringify(first)}, ended ${JSON.stringify(cur)}`);
}

// 3. Same, with a visible side border — the general case, where the horizontal correction is not 0.
{
  const TITLE = 32, BORDER = 8;
  let cur = { x: 120, y: 64, w: 1024, h: 768 };
  const first = { ...cur };
  for (let i = 0; i < 10; i++) cur = build(makeWindow(cur.x, cur.y, cur.w, cur.h, TITLE, BORDER)).windowRect();
  check('no drift with a bordered frame either',
    cur.x === first.x && cur.y === first.y, `started ${JSON.stringify(first)}, ended ${JSON.stringify(cur)}`);
}

// 4. A second monitor to the left gives legitimately NEGATIVE coordinates; they must survive.
{
  const m = build(makeWindow(-1800, 140, 1200, 800, 39, 0));
  const r = m.windowRect();
  check('a window on a left-hand second monitor keeps its negative x',
    r.x === -1800 && r.y === 140, JSON.stringify(r));
  check('  and is still considered storable', m.rectIsSane(r) === true);
}

// 5. Nonsense must be REFUSED, not stored. A minimised window reports (-32000, -32000) on Windows,
//    and storing that means an app that silently stops appearing — the worst failure this can have,
//    because nothing on screen would explain it.
{
  const m = build(makeWindow(0, 0, 1000, 700, 39, 0));
  const bad = [
    [{ x: -32000, y: -32000, w: 1000, h: 700 }, 'a minimised window'],
    [{ x: 10, y: 10, w: 0, h: 0 }, 'a zero size'],
    [{ x: 10, y: 10, w: 120, h: 90 }, 'an absurdly small window'],
    [{ x: 10, y: 10, w: 999999, h: 700 }, 'an absurdly wide window'],
    [{ x: NaN, y: 10, w: 1000, h: 700 }, 'NaN coordinates'],
    [null, 'nothing at all'],
  ];
  for (const [r, what] of bad) check(`refuses to store ${what}`, m.rectIsSane(r) === false, JSON.stringify(r));
  check('accepts an ordinary window', m.rectIsSane({ x: 300, y: 200, w: 1400, h: 900 }) === true);
}

console.log(failures ? `\n${failures} FAILED\n` : '\nall passed\n');
process.exit(failures ? 1 : 0);
