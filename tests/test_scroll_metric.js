/* Regression test for the scroll-cost metric (ScrollMetric in app/app.js).
 *
 * Run: node test_scroll_metric.js
 *
 * The module is EXTRACTED FROM app/app.js rather than copied here, for the same reason
 * test_set_face.py imports SET_FACE_RANK_SQL from server.py: a copy would pass forever while the
 * app drifted away from it.
 *
 * What is worth testing here is the attribution, not the arithmetic. The metric's whole job is to
 * tell two kinds of frozen page apart — blocked WHILE scrolling versus the catch-up AFTER you stop —
 * and a block that straddles the boundary is the case that decides whether the number means
 * anything. Everything else is guarding against a sampler that runs when it shouldn't.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const MARK = 'const ScrollMetric = (() => {';
const END = '\n})();';

function extract() {
  const src = fs.readFileSync(SRC, 'utf8');
  const a = src.indexOf(MARK);
  if (a < 0) throw new Error(`could not find "${MARK}" in app/app.js — did the module get renamed?`);
  const b = src.indexOf(END, a);
  if (b < 0) throw new Error('could not find the end of the ScrollMetric IIFE');
  return src.slice(a, b + END.length);
}
const BODY = extract();

/* A virtual clock and a single-slot rAF queue, so a test decides exactly when each frame lands and
 * how big the gap before it was. That is the only way to stage a stall deterministically. */
function harness({ traceOn = true, cards = 1240 } = {}) {
  let now = 0, scrollY = 0, pending = null, id = 0;
  const rows = [];
  const Trace = { get on() { return traceOn; }, add: (kind, detail) => rows.push({ kind, detail }) };
  const $ = (s) => (s === '#grid' ? { childElementCount: cards } : null);
  const win = { get scrollY() { return scrollY; } };
  const perf = { now: () => now };
  const raf = (cb) => { pending = cb; return ++id; };
  const caf = () => { pending = null; };
  const SM = new Function('$', 'Trace', 'window', 'performance',
    'requestAnimationFrame', 'cancelAnimationFrame',
    BODY + '\nreturn ScrollMetric;')($, Trace, win, perf, raf, caf);

  return {
    rows,
    scroll(y, t) { now = t; scrollY = y; SM.onScroll(); },
    frame(t) { now = t; const cb = pending; pending = null; if (cb) cb(t); },
    page() { SM.notePage(); },
    armed: () => pending !== null,
    line: () => (rows.find(r => r.kind === 'scroll') || {}).detail,
    lines: () => rows.filter(r => r.kind === 'scroll').map(r => r.detail),
  };
}

/* Scroll steadily at `stepPx` every `stepMs`, one frame per step, then hold still until the
 * gesture settles. Returns the time it left off at. */
function glide(h, { from = 0, steps = 40, stepPx = 60, stepMs = 16, t0 = 0 } = {}) {
  h.scroll(from, t0);                      // the event that opens the gesture
  let t = t0, y = from;
  for (let i = 1; i <= steps; i++) {
    t = t0 + i * stepMs;
    h.frame(t);                            // frame first, then the scroll that follows it
    y = from + i * stepPx;
    h.scroll(y, t + 1);
  }
  return { t: t + 1, y };
}

/* Frames every 16ms with nothing else happening, until the gesture emits or we give up. */
function settle(h, t, max = 60) {
  for (let i = 1; i <= max && h.armed(); i++) { t += 16; h.frame(t); }
  return t;
}

const num = (line, re) => { const m = line.match(re); return m ? parseFloat(m[1].replace(/,/g, '')) : null; };
let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`  ok    ${name}`); }
  else { console.log(`  FAIL  ${name}${detail ? `\n          ${detail}` : ''}`); failures++; }
}

console.log('\nScroll-cost metric\n');

/* 1. A clean glide reports its speed and no blocked time. The baseline every other case is read
 *    against — if this one shows blocked time, the sampler itself is the thing being measured. */
{
  const h = harness();
  const { t } = glide(h, { steps: 40, stepPx: 60 });
  settle(h, t);
  const line = h.line();
  check('a clean scroll emits one line', h.lines().length === 1, `got ${h.lines().length}`);
  check('  distance is the sum of the steps', num(line, /^([\d,]+)px/) === 2400, line);
  check('  nothing counted as blocked', num(line, /blocked ([\d.]+)s/) === 0, line);
  check('  no catch-up', num(line, /\+([\d,]+)ms catch-up/) === 0, line);
  check('  reports the DOM size', /1,240 in DOM/.test(line), line);
}

/* 2. A stall the user scrolls THROUGH is blocked time. The scroll events queue behind the blocked
 *    main thread and dispatch before the frame that measures the gap — which is what makes this
 *    distinguishable from case 3 at all. */
{
  const h = harness();
  const t0 = 0;
  h.scroll(0, t0);
  h.frame(16);
  h.scroll(60, 17);
  h.frame(32);
  h.scroll(120, 33);
  h.scroll(400, 430);            // input dispatched after a 400ms freeze...
  h.frame(432);                  // ...and the frame that reveals it lands after
  h.scroll(460, 440);
  settle(h, 440);
  const line = h.line();
  check('a stall scrolled through counts as blocked', num(line, /blocked ([\d.]+)s/) === 0.4, line);
  check('  and sets the worst block', num(line, /worst ([\d,]+)ms/) === 400, line);
  check('  and is NOT called catch-up', num(line, /\+([\d,]+)ms catch-up/) === 0, line);
}

/* 3. THE CASE THE METRIC EXISTS FOR. The user stops; the page then freezes finishing its work.
 *    No scroll event falls inside the gap, so it is catch-up — not mid-scroll blocked time. */
{
  const h = harness();
  const { t } = glide(h, { steps: 20, stepPx: 60 });
  h.frame(t + 300);              // a 300ms freeze with no scroll in it
  settle(h, t + 300);
  const line = h.line();
  check('a freeze after you stop is catch-up', num(line, /\+([\d,]+)ms catch-up/) >= 295, line);
  check('  and does not inflate blocked time', num(line, /blocked ([\d.]+)s/) === 0, line);
}

/* 4. A nudge says nothing about traversal and must not clutter the trace. */
{
  const h = harness();
  const { t } = glide(h, { steps: 2, stepPx: 20 });
  settle(h, t);
  check('a short nudge is not recorded', h.lines().length === 0, `got ${h.lines().length}`);
}

/* 5. Scrolling again before it settles is the SAME gesture, not two. Otherwise ordinary
 *    stop-start-stop scrolling would report a stream of tiny unusable lines. */
{
  const h = harness();
  const a = glide(h, { steps: 15, stepPx: 60 });
  let t = a.t;
  for (let i = 0; i < 6; i++) { t += 16; h.frame(t); }   // still, but under the 150ms cutoff
  const b = glide(h, { steps: 15, stepPx: 60, from: a.y, t0: t + 1 });
  settle(h, b.t);
  check('a pause under the cutoff keeps one gesture', h.lines().length === 1, `got ${h.lines().length}`);
  check('  and its distance covers both halves', num(h.line(), /^([\d,]+)px/) === 1800, h.line());
}

/* 6. With the trace off nothing runs at all — not the sampler, not a frame callback. */
{
  const h = harness({ traceOn: false });
  glide(h, { steps: 20, stepPx: 60 });
  check('trace off: nothing recorded', h.rows.length === 0, `got ${h.rows.length}`);
  check('trace off: no frame loop armed', !h.armed());
}

/* 7. Pages landing mid-scroll are counted onto the line they probably explain. */
{
  const h = harness();
  const t0 = 0;
  h.scroll(0, t0);
  let t = t0;
  for (let i = 1; i <= 20; i++) { t = i * 16; h.frame(t); h.scroll(i * 60, t + 1); if (i % 7 === 0) h.page(); }
  settle(h, t + 1);
  check('pages landing during the scroll are counted', /· 2 pages ·/.test(h.line()), h.line());
}

/* 8. A page that never settles must not leave a rAF loop running for the session. */
{
  const h = harness();
  const { t } = glide(h, { steps: 20, stepPx: 60 });
  let u = t;
  for (let i = 0; i < 200 && h.armed(); i++) { u += 120; h.frame(u); }   // every frame is a stall
  check('a page that never settles still stops', !h.armed());
  check('  and reports what it saw', h.lines().length === 1, `got ${h.lines().length}`);
}

console.log(failures ? `\n${failures} FAILED\n` : '\nall passed\n');
process.exit(failures ? 1 : 0);
