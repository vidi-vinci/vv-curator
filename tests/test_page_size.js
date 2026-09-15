/* Regression test for pagination page size (gridGeom / firstPageSize / nextPageSize in app.js).
 *
 * Run: node test_page_size.js
 *
 * THE INVARIANT: one scroll page must add more height than the runway topUpIfShort() demands.
 * If it adds less, the check that runs immediately after every page fails every time, and the app
 * fetches twice for every screenful — forever, silently, by construction. That is what a flat
 * 30-card page did: ~400px against a 600px requirement, visible on nearly every line of a real
 * trace as "sentinel 402px past the fold (needs 600) — fetching another page".
 *
 * It is a geometry bug, so it only appears at particular card sizes and window widths — which is
 * exactly why it survived: at large cards a 30-card page DOES clear the runway, so any casual test
 * passes. The table below sweeps the combinations instead.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');
const a = src.indexOf('const TOPUP_GAP_PX = ');
const end = src.indexOf('\n}', src.indexOf('function nextPageSize('));
if (a < 0 || end < 0) throw new Error('could not find the page-size helpers in app/app.js');
const BODY = src.slice(a, end + 2);

function build(cardPx, winW, winH) {
  const $ = (s) => (s === '#grid' ? { clientWidth: winW } : null);
  const win = { innerWidth: winW, innerHeight: winH };
  return new Function('$', 'window', '_cardPx',
    BODY + '\nreturn { TOPUP_GAP_PX, MIN_PAGE, gridGeom, firstPageSize, nextPageSize };')($, win, cardPx);
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

console.log('\nPagination page size\n');

// The four card sizes against a range of real window widths, including the author's 1707x996.
const CARDS = [128, 192, 256, 512];
const WINDOWS = [[1280, 800], [1707, 996], [2560, 1400], [800, 600]];

console.log('  one scroll page vs the 600px runway it has to clear:\n');
console.log(`    ${'cards'.padEnd(7)}${'window'.padEnd(12)}${'cols'.padEnd(6)}${'page'.padEnd(7)}${'adds'.padEnd(8)}was(30)`);
let anyFail = false;
for (const cardPx of CARDS) {
  for (const [w, h] of WINDOWS) {
    const m = build(cardPx, w, h);
    const { cell, cols } = m.gridGeom();
    const page = m.nextPageSize();
    const adds = Math.ceil(page / cols) * cell;
    const oldAdds = Math.ceil(30 / cols) * cell;      // what a flat 30 would have added
    const ok = adds >= m.TOPUP_GAP_PX;
    if (!ok) anyFail = true;
    console.log(`    ${String(cardPx).padEnd(7)}${(w + 'x' + h).padEnd(12)}${String(cols).padEnd(6)}`
      + `${String(page).padEnd(7)}${(adds + 'px').padEnd(8)}${oldAdds}px${oldAdds < m.TOPUP_GAP_PX ? '  <- short' : ''}`);
  }
}
console.log('');
check('every card size / window combination clears the runway in ONE page', !anyFail);

// The specific case from the trace: the author's window, smallest cards. The old code added 402px
// against a 600px requirement — the exact number his trace kept printing.
{
  const m = build(128, 1707, 996);
  const { cell, cols } = m.gridGeom();
  const oldAdds = Math.ceil(30 / cols) * cell;
  check('  the reported case (128px cards, 1707px window) really was short before',
    oldAdds < m.TOPUP_GAP_PX, `a 30-card page added ${oldAdds}px, needed ${m.TOPUP_GAP_PX}px`);
  check('  and is not short now', Math.ceil(m.nextPageSize() / cols) * cell >= m.TOPUP_GAP_PX);
}

// A page must stay a PAGE — the fix must not turn scrolling into "fetch the whole library".
for (const cardPx of CARDS) {
  for (const [w, h] of WINDOWS) {
    const m = build(cardPx, w, h);
    const page = m.nextPageSize();
    if (page < m.MIN_PAGE || page > 200) {
      check(`page size stays sane at ${cardPx}px / ${w}x${h}`, false, `got ${page}`);
    }
  }
}
check('page size stays within its bounds everywhere', true);

// The first page still covers the window PLUS the runway — it must not have been made smaller by
// sharing geometry with the scroll page.
for (const cardPx of CARDS) {
  for (const [w, h] of WINDOWS) {
    const m = build(cardPx, w, h);
    const { cell, cols } = m.gridGeom();
    const covers = Math.ceil(m.firstPageSize() / cols) * cell;
    const need = h + m.TOPUP_GAP_PX;
    if (covers < need && m.firstPageSize() < 400) {
      check(`first page covers the window at ${cardPx}px / ${w}x${h}`, false,
        `covers ${covers}px, needs ${need}px`);
    }
  }
}
check('the first page still covers the window plus the runway', true);

// The first page is always at least as big as a scroll page — it has strictly more to cover.
{
  let ok = true;
  for (const cardPx of CARDS) for (const [w, h] of WINDOWS) {
    const m = build(cardPx, w, h);
    if (m.firstPageSize() < m.nextPageSize()) ok = false;
  }
  check('the first page is never smaller than a scroll page', ok);
}

console.log(failures ? `\n${failures} FAILED\n` : '\nall passed\n');
process.exit(failures ? 1 : 0);
