/* Regression test for the rate/ETA clause on every job bar (jobPace in app.js).
 *
 * Run: node test_job_pace.js
 *
 * What went wrong: the scan bar ended every update reading "6,200/6,246 (99%) · 0:05 elapsed ·
 * 1192/s · ETA 0:00". Two separate lies in one line. "ETA 0:00" is a prediction of nothing dressed
 * up as a prediction -- it appears exactly when there is nothing left to predict, which is when it
 * is least useful and most confusing. And on an UPDATE, where nearly every file is unchanged and
 * skipped instantly, the rate is the speed of deciding not to do any work, not the speed of doing
 * it.
 *
 * The rules under test:
 *   1. "ETA 0:00" must never appear. Not at the end, not on the last file, not ever.
 *   2. The rate and the ETA hide TOGETHER -- they are one clause about pace. A rate left standing
 *      alone invites the reader to do the same division the ETA was getting wrong.
 *   3. A genuinely long job (a rebuild, a quality-scoring run) still gets both, because there the
 *      ETA is the one number worth having.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

// Lift a whole `function name(...) { ... }` out of the source by brace matching.
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
const { jobPct, jobEta, fmtTime, jobPace } = new Function(
  grab('jobPct') + grab('jobEta') + grab('fmtTime') + grab('jobPace') +
  '\nreturn { jobPct, jobEta, fmtTime, jobPace };')();

// The scan bar's real format string, kept in step with watchScan.
const scanLine = (label, s) =>
  `${label} ${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} (${jobPct(s)}%) · ` +
  `${fmtTime(s.elapsed)} elapsed${jobPace(s, { rate: true })}`;

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

console.log('\nJob bar pace clause\n');

// 1. THE ONE THAT MATTERS. Sweep the whole life of a fast update -- the 6,246-file library from the
//    report, walked at ~1200/s -- and assert the nonsense string never appears at any point in it.
{
  const total = 6246;
  let bad = null;
  for (let seen = 0; seen <= total; seen += 1) {
    const line = scanLine('Scanning', { running: 1, seen, total, elapsed: seen / 1192 });
    if (line.includes('ETA 0:00')) { bad = line; break; }
  }
  check('"ETA 0:00" never appears across a whole update', bad === null, bad);
}

// 2. The end state from the bug report: finished, so no pace clause at all.
{
  const line = scanLine('Scanning', { running: 1, seen: 6246, total: 6246, elapsed: 5.2 });
  check('a finished scan shows neither rate nor ETA',
    line === 'Scanning 6,246/6,246 (100%) · 0:05 elapsed', line);
}

// 3. The last file before the end -- the frame the old bar died on.
{
  const line = scanLine('Scanning', { running: 1, seen: 6245, total: 6246, elapsed: 5.2 });
  check('one file from the end shows no pace clause', !/\/s|ETA/.test(line), line);
}

// 4. They hide together, and appear together. Never one without the other.
{
  const cases = [
    { seen: 0, total: 0, elapsed: 0 },              // before the walk has counted anything
    { seen: 0, total: 6246, elapsed: 0 },           // counted, not started -- no rate to divide by
    { seen: 3000, total: 6246, elapsed: 2.5 },      // mid update
    { seen: 2900, total: 6246, elapsed: 80 },       // mid rebuild
    { seen: 500, total: 500, elapsed: 900 },        // complete
  ];
  let bad = null;
  for (const s of cases) {
    const clause = jobPace(s, { rate: true });
    if (clause.includes('/s') !== clause.includes('ETA')) { bad = JSON.stringify(s) + ' -> ' + clause; break; }
  }
  check('rate and ETA always appear or vanish together', bad === null, bad);
}

// 5. A long job still gets its ETA -- the whole point of not deleting the clause outright.
{
  const line = scanLine('Rebuilding', { running: 1, seen: 2900, total: 6246, elapsed: 80 });
  check('a rebuild in flight keeps rate and ETA',
    line === 'Rebuilding 2,900/6,246 (46%) · 1:20 elapsed · 36/s · ETA 1:32', line);
}

// 6. Bars that never showed a rate must not sprout one (reward scoring).
{
  const clause = jobPace({ seen: 40, total: 500, elapsed: 120 });
  check('without {rate} the clause is ETA only', clause === ' · ETA 23:00', JSON.stringify(clause));
}

// 7. THE RUN LABEL, added 2026-09-14. Rescanning every library showed "Rescanning Photos (1 of 5)…"
//    and nothing else: each library's scan ran silent so the run could own the pill, and silent threw
//    the progress away as well as the pill — so the file count and the bar were computed every 400ms
//    and discarded. The author, comparing it to a single library's rescan: "can we include a more detailed
//    progress indicator?"
//
//    The real scanProgress is lifted here rather than mirrored, which also retires the copy above:
//    this is the one place the scan line's format is asserted.
{
  const { scanProgress } = new Function(
    grab('jobPct') + grab('jobEta') + grab('fmtTime') + grab('jobPace') + grab('scanProgress') +
    '\nreturn { scanProgress };')();
  const s = { seen: 1204, total: 9000, elapsed: 130 };

  // A SINGLE LIBRARY IS UNTOUCHED, and that is half the point — it is the look people already know.
  const one = scanProgress(s, 'Rebuilding');
  check('one library still reads exactly as it did',
    one.text === 'Rebuilding 1,204/9,000 (13%) · 2:10 elapsed · 9/s · ETA 14:02', one.text);

  // In a run the position REPLACES the verb: "Rescanning Photos (1 of 5) Rebuilding 1,204/…" would be
  // two verbs for one act.
  const run = scanProgress(s, 'Rebuilding', 'Rescanning Photos (1 of 5)');
  check('a run leads with its position instead of the verb',
    run.text === 'Rescanning Photos (1 of 5) · 1,204/9,000 (13%) · 2:10 elapsed · 9/s · ETA 14:02', run.text);
  check('and still reports a measurable bar', run.pct === 13, String(run.pct));

  // Counting is the phase before anything is measurable. It keeps the run's position — a pill that
  // dropped the library name mid-run would leave you unable to tell which one had stalled — and
  // still shows NO bar, which is what `pct: null` means.
  const counting = { seen: 0, total: 0, elapsed: 3 };
  check('counting keeps the library name in a run',
    scanProgress(counting, 'Rebuilding', 'Rescanning Photos (1 of 5)').text
      === 'Rescanning Photos (1 of 5) · Counting files… · 0:03 elapsed',
    scanProgress(counting, 'Rebuilding', 'Rescanning Photos (1 of 5)').text);
  check('counting alone is unchanged outside a run',
    scanProgress(counting, 'Rebuilding').text === 'Counting files… · 0:03 elapsed',
    scanProgress(counting, 'Rebuilding').text);
  check('and neither draws a bar', scanProgress(counting, 'Rebuilding', 'x').pct === null);

  // The wiring, which no string check can reach: the job must opt back into painting while silent,
  // and the run must clear the previous library's bar as it changes subject.
  check('the scan opts into painting inside a run', /runProgress: !!runLabel/.test(src), 'watchScan');
  check('and Job.start honours that over silent',
    /spec\.silent && !spec\.runProgress/.test(src), 'Job.start');
  check('each library starts with an empty bar',
    /Job\.say\(`\$\{at\}…`, true\)/.test(src), 'runRescanAll');
}

console.log();
process.exit(failures ? 1 : 0);
