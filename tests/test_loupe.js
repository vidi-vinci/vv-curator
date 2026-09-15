/* Regression test for the loupe's coordinate math (the Loupe module in app/app.js).
 *
 * Run: node tests/test_loupe.js
 *
 * THE BUG THIS EXISTS TO STOP. Both the detail image and the set-cull panes are `object-fit:
 * contain`, so the PICTURE is letterboxed inside its element — narrower or shorter than the box,
 * centred. Map the cursor through the element's own rect and the lens shows the wrong part of the
 * image, drifting further the more the two aspect ratios differ.
 *
 * It is invisible in the obvious test: a square image in a square pane letterboxes by zero, so
 * element box and picture box are identical and wrong code passes. Every case below therefore uses
 * a shape that DOES letterbox, and the square is included only to prove the no-op case still works.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');
const a = src.indexOf('  function picture(img) {');
const end = src.indexOf('\n  }', a);
if (a < 0 || end < 0) throw new Error('could not find picture() in app/app.js');
const picture = new Function('return ' + src.slice(a, end + 4).trim())();

// A fake <img>: `rect` is the element box, `nw`/`nh` the file's own pixels.
const img = (rect, nw, nh) => ({
  naturalWidth: nw, naturalHeight: nh, getBoundingClientRect: () => rect,
});

let failed = 0;
function check(name, cond, got) {
  if (!cond) failed++;
  console.log(`${cond ? '  ok  ' : '  FAIL'} ${name}${got !== undefined ? '  ' + JSON.stringify(got) : ''}`);
}
const near = (x, y) => Math.abs(x - y) < 0.001;

console.log('PICTURE BOX vs ELEMENT BOX');

// A portrait image in a landscape pane: bars left and right.
let p = picture(img({ left: 0, top: 0, width: 1000, height: 500 }, 512, 1024));
check('portrait in a wide pane is letterboxed horizontally', near(p.w, 250) && near(p.h, 500), p);
check('  and centred, so x is the left bar', near(p.x, 375), p.x);
check('  with no vertical bar', near(p.y, 0), p.y);

// A landscape image in a portrait pane: bars top and bottom.
p = picture(img({ left: 0, top: 0, width: 500, height: 1000 }, 1024, 512));
check('landscape in a tall pane is letterboxed vertically', near(p.w, 500) && near(p.h, 250), p);
check('  and centred, so y is the top bar', near(p.y, 375), p.y);

// The no-op case, which is the one that hides the bug.
p = picture(img({ left: 0, top: 0, width: 600, height: 600 }, 1024, 1024));
check('a matching aspect letterboxes by nothing', near(p.w, 600) && near(p.h, 600) && near(p.x, 0) && near(p.y, 0), p);

// The element's own offset has to survive: panes sit side by side, so the second one is not at x=0.
p = picture(img({ left: 700, top: 40, width: 300, height: 300 }, 512, 1024));
check('a pane away from the origin keeps its offset', near(p.x, 775) && near(p.y, 40), p);

check('an image that has not loaded yields null, rather than NaN coordinates',
      picture(img({ left: 0, top: 0, width: 100, height: 100 }, 0, 0)) === null);
check('  and so does a collapsed element',
      picture(img({ left: 0, top: 0, width: 0, height: 0 }, 512, 512)) === null);

console.log('\nWHAT THE LENS THEN SHOWS');

// The mapping the module does with picture()'s output: cursor -> fraction -> background offset.
// Centre of the lens must land on the pixel under the cursor, at every zoom.
const SIZE = 260;
function shows(p, cx, cy, zoom) {
  const fx = (cx - p.x) / p.w, fy = (cy - p.y) / p.h;
  const bw = p.w * zoom, bh = p.h * zoom;
  const bgx = SIZE / 2 - fx * bw, bgy = SIZE / 2 - fy * bh;
  // Which point of the source image sits at the lens's centre, back in natural pixels.
  return { u: ((SIZE / 2 - bgx) / bw) * p.nw, v: ((SIZE / 2 - bgy) / bh) * p.nh, fx, fy };
}

p = picture(img({ left: 0, top: 0, width: 1000, height: 500 }, 512, 1024));
let s = shows(p, 375 + 125, 250, 3);        // dead centre of the picture
check('the lens centre is the pixel under the cursor', near(s.u, 256) && near(s.v, 512), s);
s = shows(p, 375, 0, 3);                    // top-left corner of the picture
check('  at the top-left corner too', near(s.u, 0) && near(s.v, 0), s);
s = shows(p, 375 + 250, 500, 8);            // bottom-right, and zoom must not move it
check('  and at the bottom-right, unchanged by zoom', near(s.u, 512) && near(s.v, 1024), s);

// Cursor over the letterbox bar is NOT over the picture — the module hides the lens there rather
// than showing a clamped edge, which would look like a frozen lens.
s = shows(p, 100, 250, 3);
check('a cursor on the letterbox bar reads as outside the picture', s.fx < 0, s.fx);

console.log(failed ? `\n${failed} failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
