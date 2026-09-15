/* test_empty_state.js — what the app says when the grid comes back empty.
 *
 * Run: node test_empty_state.js
 *
 * THE BUG THIS EXISTS TO STOP COMING BACK. Filter until nothing matches and the grid was simply
 * blank — no message, nothing. The author reported it with a screenshot on 2026-09-14, and the screenshot
 * showed a second fault in the same frame: the selection bar above the void offering
 * "Select all 0 cards", live and clickable, an act that cannot happen.
 *
 * Two halves, because the change has two kinds of risk.
 *
 * HALF ONE runs the REAL updateSelBar(), lifted out of app/app.js with its one dependency, against
 * stub elements. The label and the dim are decided by two DELIBERATELY DIFFERENT tests — `!total`
 * for the label, `total === 0` for the dim — because `null` means "not counted yet", before the
 * first response has landed, and dimming the bar on a count that has not arrived would flick it
 * live a moment later. That asymmetry looks like an oversight and is the thing a later tidy-up
 * will try to unify, so each side of it gets an assertion.
 *
 * HALF TWO is source assertions, for the parts with no headless form: the branch that decides which
 * empty state paints, and the three inserters that have to take the panel out before putting cards
 * in. They are regex checks on purpose — what matters is the ORDER of two statements, which is not
 * observable from outside the function.
 *
 * WHAT IT CANNOT CHECK, and so is not pretended here: anything geometric. The panel is centred at
 * 46rem by `.grid:has(> .empty-hint)`, and a box that lost that class renders 194px wide inside a
 * single card track — a real failure, from 2026-09-07. That needs computed layout in a browser.
 */
'use strict';

const fs = require('fs');
const path = require('path');

let failures = 0;
function check(name, cond, detail) {
  console.log((cond ? '  ok    ' : '  FAIL  ') + name +
              (cond || detail === undefined ? '' : '\n          ' + detail));
  if (!cond) failures++;
}

// LINE ENDINGS NORMALISED ON THE WAY IN — grab() looks for '\n}\n' boundaries, and on a CRLF
// checkout every one of those searches finds nothing and the test dies before its first check.
// The same note is on test_card_facts.js, where it cost a debugging session.
const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const CSS = fs.readFileSync(path.join(__dirname, '..', 'app', 'style.css'), 'utf8').replace(/\r\n/g, '\n');

function grab(startsWith, endsWith) {
  const i = SRC.indexOf(startsWith);
  if (i === -1) throw new Error('not found in app.js: ' + startsWith);
  const j = SRC.indexOf(endsWith, i);
  if (j === -1) throw new Error('no end for: ' + startsWith);
  return SRC.slice(i, j + endsWith.length);
}

// ---- half one: run the real updateSelBar() -----------------------------------------------------

const buildBar = new Function('state', 'selection', '$',
  [grab('function collapsingNow()', '\n}\n'),
   grab('function updateSelBar()', '\n}\n')].join('\n') + '\nreturn updateSelBar;');

function runBar(st, selSize) {
  const mk = id => ({ id, textContent: '', aria: null,
                      setAttribute(k, v) { if (k === 'aria-disabled') this.aria = v; } });
  const selAll = mk('selAll');
  const buttons = [selAll, mk('selClear'), mk('selDelete'), mk('selTag')];
  const els = { '#selCount': { textContent: '' }, '#selAll': selAll,
                '#selbar': { querySelectorAll: () => buttons } };
  buildBar(st, { size: selSize }, s => els[s])();
  return { label: selAll.textContent,
           count: els['#selCount'].textContent,
           dim: Object.fromEntries(buttons.map(b => [b.id, b.aria])) };
}

console.log('the selection bar over an empty result set');

let r = runBar({ total: 0, rootTotal: null, rootFiles: null }, 0);
// Asserted as "carries no digit" rather than against the exact string, so the wording stays the
// author's to change — the fault was a COUNT of zero being shown at all, not a particular label.
check('a total of zero shows no number', !/\d/.test(r.label), r.label);
check('and Select all goes idle with the rest', r.dim.selAll === 'true', JSON.stringify(r.dim));

r = runBar({ total: 0, rootTotal: null, rootFiles: null }, 3);
check('still idle with a stale selection behind it', r.dim.selAll === 'true', JSON.stringify(r.dim));

r = runBar({ total: 3487, rootTotal: null, rootFiles: null }, 0);
// THE REGRESSION GUARD. One careless placement of `total === 0 ||` dims Select all permanently,
// which would take away the only way into selection mode.
check('a real total leaves Select all live', r.dim.selAll === 'false', JSON.stringify(r.dim));
check('  and still names the number', r.label.includes('3,487'), r.label);
check('  while the acting buttons stay idle', r.dim.selDelete === 'true', JSON.stringify(r.dim));

r = runBar({ total: null, rootTotal: null, rootFiles: null }, 0);
// NOT COUNTED YET is not the same as counted zero: before the first response lands the bar must
// stay live, or it visibly flicks on a moment later.
check('an uncounted total leaves Select all live', r.dim.selAll === 'false', JSON.stringify(r.dim));
check('  and shows no number either', !/\d/.test(r.label), r.label);

r = runBar({ total: 32, rootTotal: 32, rootFiles: 56 }, 0);
check('the unit word survives when cards are merging', / cards$/.test(r.label), r.label);

// ---- half two: the wiring, by source ----------------------------------------------------------

console.log('\nwhich empty state paints, and who clears it');

const branch = SRC.slice(SRC.indexOf('if (reset && !(state.roots || []).length) showNoRootHint();'));
// `else if`, not a second `if`. With no libraries there are also no items, so two independent
// checks would paint "no cards match" over the welcome on a fresh install.
check('the no-match branch is chained to the no-library one',
      /showNoRootHint\(\);\s*(\/\/[^\n]*\n\s*)*else if \(reset && !data\.items\.length\) showNoMatchHint\(\);/.test(branch.slice(0, 1200)),
      branch.slice(0, 700));

const hint = grab('function showNoMatchHint()', '\n}\n');
check('the panel carries .empty-hint, which the :has() rule keys on', hint.includes('empty-hint'), hint);
check('and .panel, which draws its box', hint.includes('empty-hint panel'), hint);
check('it asks filtersActive() rather than taking an argument',
      hint.includes('filtersActive()') && /function showNoMatchHint\(\)\s*\{/.test(hint), hint);
check('the stylesheet still carries the layout switch',
      CSS.includes('.grid:has(> .empty-hint)'), 'style.css');

// The panel has to go BEFORE cards arrive, not after — both of these inserters are positional, and
// with the panel present it is `grid.children[0]`.
for (const [fn, start, insert] of [
  ['prependNewImages', 'async function prependNewImages()', "insertAdjacentHTML('afterbegin'"],
  ['undoRestorer', 'function undoRestorer(', 'grid.insertBefore('],
]) {
  const body = grab(start, '\n}\n');
  const cleared = body.indexOf('clearEmptyHint()');
  const inserted = body.indexOf(insert);
  check(`${fn} clears the panel before it inserts`,
        cleared !== -1 && inserted !== -1 && cleared < inserted,
        `clearEmptyHint at ${cleared}, insert at ${inserted}`);
}

// Culling the last card empties the grid without any search running, so nothing else would say so.
check('recycling the last card paints the message',
      /if \(!state\.items\.length\) showNoMatchHint\(\);/.test(SRC), 'app.js');

// After the dim became real, a stale bar is a DEAD bar rather than a wrong number.
check('prependNewImages repaints the bar it just changed the total of',
      grab('async function prependNewImages()', '\n}\n').includes('updateSelBar()'), 'app.js');

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
