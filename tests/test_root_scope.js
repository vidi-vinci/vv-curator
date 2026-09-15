/* test_root_scope.js — unticking every library means NONE, not all.
 *
 * Run: node test_root_scope.js
 *
 * THE BUG THIS EXISTS TO STOP COMING BACK. `state.rootsSel = []` meant two opposite things at once:
 * "nothing has been chosen, so show everything" and "the user unticked the last box". The first
 * reading won everywhere, so unticking the last library silently put every library back on. The author,
 * 2026-09-14: "I never realized you could NOT deselect all libraries. it forces them all back on
 * if you do. that's just weird."
 *
 * So there are THREE values now — null (never chosen, all), an array (exactly these), and the empty
 * array (none) — and almost every check in this file is about keeping null and [] apart. Anything
 * that collapses one into the other reintroduces the bug, and the collapse is always spelled the
 * same way: `state.rootsSel || []`.
 *
 * TWO THINGS ARE LOAD-BEARING BEYOND THAT:
 *
 *   1. The "none" sentinel must be a value no real library key can ever equal. Every consumer of
 *      `roots=` turns it into a SQL `IN (...)`, which is what makes one client-side value give the
 *      grid, the facet counts and the totals the same answer with no server change at all. A real
 *      key is sha1(path)[:16] — 16 lowercase hex characters (server.py, _root_key).
 *   2. A stored `[]` from before this change meant ALL. Reading one the new way would open the app
 *      with every library switched off and nothing on screen to explain it, so only a blob carrying
 *      `rootsPicked` is allowed to mean none.
 */
'use strict';

const fs = require('fs');
const path = require('path');

let failures = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? '  ok    ' : '  FAIL  ') + name +
              (ok ? '' : `\n          got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`));
  if (!ok) failures++;
}

// CRLF normalised on the way in — grab() hunts for '\n}\n' boundaries, which find nothing on a
// CRLF checkout. See the same note in test_card_facts.js, where it cost a session.
const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8').replace(/\r\n/g, '\n');

function grab(startsWith, endsWith) {
  const i = SRC.indexOf(startsWith);
  if (i === -1) throw new Error('not found in app.js: ' + startsWith);
  const j = SRC.indexOf(endsWith, i);
  if (j === -1) throw new Error('no end for: ' + startsWith);
  return SRC.slice(i, j + endsWith.length);
}

const sentinel = /const ROOTS_NONE = '([^']+)'/.exec(SRC);
if (!sentinel) throw new Error('ROOTS_NONE not found in app.js');
const ROOTS_NONE = sentinel[1];

const build = new Function('state', 'renderLibTrigger',
  ["const ROOTS_NONE = " + JSON.stringify(ROOTS_NONE) + ";",
   grab('function allRootKeys()', '\n'),
   // rootsParam calls it since 2026-09-14: an unreachable library is out of scope, the same way an
   // unticked one is. Without it in the sandbox the whole file throws before the first check.
   grab('function isRootOffline(key)', '\n}\n'),
   grab('function rootsParam()', '\n}\n'),
   grab('function pruneRootKeys(a)', '\n}\n'),
   grab('function restoreRootsSel(f)', '\n}\n'),
   grab('function selectedRootKeys()', '\n}\n')].join('\n') +
  '\nreturn { rootsParam, selectedRootKeys, pruneRootKeys, restoreRootsSel };');

const KEYS = ['4546a0288e69986a', 'bb17c0d9e2f1a340', 'c0ffee1234567890'];
function api(rootsSel) {
  const state = { roots: KEYS.map(k => ({ key: k })), rootsSel };
  const fns = build(state, () => {});
  return { fns, state };
}

console.log('the sentinel cannot be mistaken for a library');
// A real key is 16 lowercase hex characters. If the sentinel ever became one, "show nothing" would
// silently become "show that library" for whoever happened to own it.
check('it is not shaped like a key', /^[0-9a-f]{16}$/.test(ROOTS_NONE), false);

console.log('\nwhat goes on the wire');
check('never chosen sends nothing, meaning all', api(null).fns.rootsParam(), '');
check('every key sends nothing, meaning all', api(KEYS.slice()).fns.rootsParam(), '');
check('a subset sends just those', api([KEYS[0], KEYS[2]]).fns.rootsParam(), KEYS[0] + ',' + KEYS[2]);
// THE WHOLE FIX. An empty selection used to come out as '' — indistinguishable from "all".
check('none chosen sends the sentinel', api([]).fns.rootsParam(), ROOTS_NONE);
check('a selection of only dead keys is none, not all', api(['gone1', 'gone2']).fns.rootsParam(), ROOTS_NONE);

console.log('\nwhat the checkboxes and the readout see');
check('never chosen means all of them', api(null).fns.selectedRootKeys(), KEYS);
check('none chosen means none of them', api([]).fns.selectedRootKeys(), []);
check('a subset means the subset', api([KEYS[1]]).fns.selectedRootKeys(), [KEYS[1]]);
check('a dead key is dropped, not honoured', api([KEYS[1], 'gone']).fns.selectedRootKeys(), [KEYS[1]]);

console.log('\nnull and [] must never collapse into each other');
// pruneRootKeys is the function most likely to be "tidied" back into returning [] for everything.
check('a missing value prunes to null, not []', api(null).fns.pruneRootKeys(undefined), null);
check('an empty array prunes to an empty array', api(null).fns.pruneRootKeys([]), []);
check('an all-dead array prunes to an empty array', api(null).fns.pruneRootKeys(['gone']), []);

console.log('\nreading a stored selection');
function restored(blob) {
  const { fns, state } = api(null);
  fns.restoreRootsSel(blob);
  return state.rootsSel;
}
// THE COMPATIBILITY CASE. Written before 2026-09-14, [] meant ALL. Honouring it as "none" would
// start the app with every library off and no way to tell why.
check('an old blob\'s [] still means all', restored({ roots: [] }), null);
check('a new blob\'s [] means none', restored({ roots: [], rootsPicked: true }), []);
check('a stored subset is honoured either way', restored({ roots: [KEYS[0]], rootsPicked: true }), [KEYS[0]]);
check('no stored roots at all means all', restored({}), null);
check('no stored blob at all means all', restored(null), null);

console.log('\nthe empty grid names the right cause');
// With no libraries selected nothing can match whatever the filters say, and Reset all does not
// touch the library selection — so the filtered line would point at a control that cannot help.
const hint = grab('function showNoMatchHint()', '\n}\n');
check('the message asks the selection, not just the filters',
      /selectedRootKeys\(\)\.length === 0/.test(hint), true);
check('and an empty selection outranks any filter',
      /filtersActive\(\)\s*&&\s*!noneSelected/.test(hint), true);
// Its wording has to be the scope button's, or the grid and the control it points at disagree
// about what is wrong. Both are asserted here so moving one alone fails.
check('it names the state in the scope button\'s words',
      hint.includes('No libraries selected.'), true);
check('and the scope button still says it too',
      SRC.includes('No libraries selected — select one to see its images'), true);

// The handler must start from `all` ONLY when nothing has ever been chosen. Starting there for any
// empty selection is the original bug, exactly.
const handler = SRC.slice(SRC.indexOf("$('#libList').addEventListener('change'"));
check('unticking the last box does not rebuild from all',
      /new Set\(state\.rootsSel === null \? all : state\.rootsSel\)/.test(handler.slice(0, 1200)), true);

// ---- an unreachable library is out of scope ----------------------------------------------------
// The author, 2026-09-14, after a share dropped under him: "I'm inclined to just hide cards from inactive
// Libraries. The experience is SO broken otherwise." A thumbnail is made on first view, so anything
// never opened while the drive was there has nothing cached and nothing to make a thumbnail from --
// half a library that half works. Now it simply leaves scope, as an unticked one does.
//
// THE CASE THAT MATTERS IS THE MIXED ONE. '' means "all of them" on the wire, so when an offline
// library is present the list has to be spelled out; returning '' there would ask the server for
// the very library being hidden, and nothing else in this file would have noticed.
console.log('\nan unreachable library leaves scope');

function scope(roots, rootsSel) {
  const state = { roots, rootsSel };
  return build(state, () => {}).rootsParam();
}
const [K1, K2] = KEYS;
const on1 = { key: K1, exists: true }, on2 = { key: K2, exists: true };
const off2 = { key: K2, exists: false }, off1 = { key: K1, exists: false };

check('two reachable, never chosen: still "all"', scope([on1, on2], null), '');
check('one offline, never chosen: the live one BY NAME, not "all"', scope([on1, off2], null), K1);
check('one offline, both ticked: the live one only', scope([on1, off2], [K1, K2]), K1);
check('the offline one was the only tick: nothing', scope([on1, off2], [K2]), ROOTS_NONE);
check('every library offline: nothing', scope([off1, off2], null), ROOTS_NONE);
check('a library with no `exists` yet counts as reachable', scope([{ key: K1 }], null), '');

// Their own ticks are never rewritten -- the library is out of scope because it is not here, not
// because they switched it off, and writing it into rootsSel would leave it hidden after the drive
// came back, with nobody able to guess why.
check('rootsParam does not write to state.rootsSel', /rootsParam\(\)\s*\{[^}]*state\.rootsSel\s*=/.test(SRC), false);

// The empty grid has to name the drive, because no filter change can bring these cards back --
// the same reason "no libraries selected" outranks the filtered message.
check('the empty grid has a state for it', hint.includes('is offline.'), true);
check('and offline outranks the filtered message',
      /filtersActive\(\)\s*&&\s*!noneSelected\s*&&\s*!allOff/.test(hint), true);

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
