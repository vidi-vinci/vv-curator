/* test_catchup_mark.js — which libraries are behind this build's reader, and who may catch them up.
 *
 * Run: node test_catchup_mark.js
 *
 * THE GAP THIS CLOSES. The launch dialog says SOME libraries need a rescan and then tells you to do
 * it from each library's ⋯ menu. The author, 2026-09-14: "if the user does it by hand, there is no way to
 * know which ones need a rescan." Worse, and proved on the day: that menu's Rescan could not do it
 * at all. An ordinary rescan of two stale rows reported `skipped: 2` and left them stale; only a
 * forced one reported `updated: 2`. The one-verb change of 2026-09-12 had deleted the per-library
 * forced action and left the advice pointing at the cheap one.
 *
 * So: a mark per row, and Rescan means "make this library right".
 *
 * THE ONE THAT MUST NEVER REGRESS is the last check here. refreshChanged() — the ✓ on the strip, and
 * the R key — calls rescanLib for every library that changed on disk. If that path ever learns to
 * force, a keystroke starts a multi-minute job on a library the user only wanted checked, which the
 * one-verb commit forbids in as many words. The catch-up lives on the MENU route alone.
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

const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const CSS = fs.readFileSync(path.join(__dirname, '..', 'app', 'style.css'), 'utf8').replace(/\r\n/g, '\n');

function grab(startsWith, endsWith) {
  const i = SRC.indexOf(startsWith);
  if (i === -1) throw new Error('not found in app.js: ' + startsWith);
  const j = SRC.indexOf(endsWith, i);
  if (j === -1) throw new Error('no end for: ' + startsWith);
  return SRC.slice(i, j + endsWith.length);
}

console.log('the cheap check stays cheap');

const rescanLib = grab('async function rescanLib(key, quiet)', '\n}\n');
// It must send NO force. This is the function refreshChanged() drives per changed library.
check('rescanLib sends no force flag', !/force/.test(rescanLib), rescanLib);
const refreshChanged = grab('async function refreshChanged()', '\n}\n');
check('the ✓ check calls rescanLib, not the catch-up route',
      refreshChanged.includes('rescanLib(') && !refreshChanged.includes('rescanLibFromMenu'),
      refreshChanged);

console.log('\nthe menu route makes the library right');

const fromMenu = grab('async function rescanLibFromMenu(key)', '\n}\n');
check('it falls through to the cheap scan when nothing is behind',
      /if \(!behind\) return rescanLib\(key\);/.test(fromMenu), fromMenu);
check('and does the forced re-read when something is',
      fromMenu.includes('rebuildOne(key)'), fromMenu);
// The price is what someone decides on, so it is asked before the long job and never guessed.
check('it asks first', fromMenu.includes('uiConfirm('), fromMenu);
check('quoting the file count', fromMenu.includes('behind.files'), fromMenu);
check('and a time only when one has been measured',
      /behind\.seconds\s*\?/.test(fromMenu), fromMenu);
check('the menu item is wired to it',
      /\{ rescan: rescanLibFromMenu,/.test(SRC), 'libMenu handler');

console.log('\nthe marks outlive the dialog that would have explained them');

const offer = grab('async function offerCatchUp()', '\n}\n');
const fetched = offer.indexOf("getJSON('/api/catchup')");
const noted = offer.indexOf('noteBehind(');
const snoozed = offer.indexOf('CATCHUP_SNOOZE');
// THE ORDER IS THE FEATURE. "Do it later" silences the offer, not the marks — and the person who
// answered "later" is exactly the one who then goes looking for which libraries need it.
check('the fetch and the marks come before the snooze check',
      fetched !== -1 && noted !== -1 && snoozed !== -1 && noted > fetched && noted < snoozed,
      `fetch ${fetched}, note ${noted}, snooze ${snoozed}`);
check('every scan refreshes what is behind',
      grab('async function afterScanRefresh(quiet)', '\n}\n').includes('refreshBehind()'), 'app.js');

console.log('\nthe mark itself');

const row = SRC.slice(SRC.indexOf('function renderLibList('), SRC.indexOf('let _libMenuKey'));
check('a row that is behind gets the mark', row.includes('data-catchup='), 'renderLibList');
check('it says what it is for', row.includes('Rescan for new version features'), 'renderLibList');
// A library nobody can reach cannot be caught up; a button that can only fail is worse than none.
check('an unreachable library gets none', /r\.exists !== false/.test(row), 'renderLibList');

// THE TRAP THAT ACTUALLY BIT, and it is invisible to every functional test: `.popmenu button` is
// (0,2,0) and beats a bare `.lib-catchup` (0,1,0). Without the panel scope the mark rendered 331px
// wide, white instead of amber, and stood its row 13px taller than its neighbour. `.lib-ref` already
// carries a comment about the same trap — this is the second control to walk into it.
check('the mark is scoped to #libPanel, or .popmenu button eats it',
      /#libPanel[^\n]*\.lib-catchup[^\n]*width: auto/.test(CSS), 'style.css');
check('including its colour, for the same reason',
      /#libPanel \.lib-catchup[^\n]*color: var\(--modified\)/.test(CSS), 'style.css');

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
