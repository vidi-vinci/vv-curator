/* The scan bar before it knows how much there is (scanProgress in app.js).
 *
 * Run: node test_scan_counting.js
 *
 * A scan has two phases and only the second one has a denominator. index_db walks the whole tree
 * first and reports the file total only when that walk is finished, so everything up to that point
 * was rendered as "Building index 0/0 (0%)" on an empty trough -- behind a full-screen block, on a
 * first index, which is the one scan a new user ever watches. On a network share the walk is the
 * slow part, so that is minutes of an app that looks stalled at the exact moment someone is
 * deciding whether this thing works. Found 2026-09-07 mapping the cold start; it is the third of
 * the four first-run faults.
 *
 * Zero of zero is not a small amount of progress. It is no information, drawn as if it were.
 *
 * The rules under test:
 *   1. While the total is unknown, no percentage and no count are shown -- neither in the text nor
 *      as a bar. `pct: null` is the existing way to say "nothing measurable yet" (Job only paints a
 *      trough when pct is a number), so this needs no new mechanism and must not grow one.
 *   2. The elapsed clock stays, because it is the one honest sign that work is happening.
 *   3. The moment a total arrives the ordinary bar returns, unchanged -- including the 100% case
 *      that test_scan_progress.py pins from the server side.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('could not find ' + name + ' in app/app.js');
  let depth = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}' && --depth === 0) return src.slice(i, k + 1);
  }
  throw new Error('unbalanced braces reading ' + name);
}
const { scanProgress } = new Function(
  grab('jobPct') + grab('jobEta') + grab('fmtTime') + grab('jobPace') + grab('scanProgress') +
  'return { scanProgress };')();

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  ok    ' + name); return; }
  console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : ''));
  failures++;
}

// ---- 1. counting: no denominator, so no fraction ----------------------------------------------
const counting = scanProgress({ running: true, seen: 0, total: 0, elapsed: 12 }, 'Building index');
check('no progress bar while counting', counting.pct === null, 'pct = ' + counting.pct);
check('  no percentage in the text', !counting.text.includes('%'), counting.text);
check('  no 0/0', !counting.text.includes('0/0'), counting.text);
check('  it says what is happening', /counting/i.test(counting.text), counting.text);
check('  and keeps the clock', counting.text.includes('0:12'), counting.text);

// The walk reports files as it finds them on some paths, so `seen` can climb while `total` is still
// unknown. That is still a fraction with no denominator.
const seenNoTotal = scanProgress({ running: true, seen: 4210, total: 0, elapsed: 30 },
                                 'Building index');
check('seen without a total is still the counting phase', seenNoTotal.pct === null,
      'pct = ' + seenNoTotal.pct);
check('  and shows no fraction', !seenNoTotal.text.includes('%') && !seenNoTotal.text.includes('/'),
      seenNoTotal.text);

// ---- 2. the total arrives: the ordinary bar, unchanged -----------------------------------------
const running = scanProgress({ running: true, seen: 250, total: 1000, elapsed: 10 },
                             'Building index');
check('a real total brings the bar back', running.pct === 25, 'pct = ' + running.pct);
check('  with the label, the count and the percentage',
      running.text.startsWith('Building index 250/1,000 (25%)'), running.text);
check('  and the elapsed clause', running.text.includes('0:10 elapsed'), running.text);

const done = scanProgress({ running: true, seen: 6246, total: 6246, elapsed: 5 }, 'Building index');
check('100% still reads 100%', done.pct === 100 && done.text.includes('(100%)'), done.text);
check('  and no ETA once there is nothing left to predict', !done.text.includes('ETA'), done.text);

console.log('');
if (failures) { console.log('FAILED: ' + failures); process.exit(1); }
console.log('all good');
