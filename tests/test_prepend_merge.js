/* test_prepend_merge.js — a card that CHANGES SHAPE while the app is running.
 *
 * Run: node test_prepend_merge.js
 *
 * THE BUG THIS EXISTS TO STOP COMING BACK, reported by the author 2026-08-24: "I ran a few video runs —
 * with a matching card — and the card would appear, but not the set with the video. I restarted the
 * app, and then the sets get joined."
 *
 * A run writes its still and its video moments apart. If a background refresh lands between the
 * two, the still is indexed alone and gets a lone card. When the video arrives and the pair forms,
 * the server returns the SAME row id — now fronting a merged pair — and prependNewImages skipped it
 * with `if (have.has(id)) continue; // already its own card`.
 *
 * Every part of that line was reasonable. It is wrong because **already carded is not the same as
 * unchanged**: a card's shape is a property of its GROUP, and the group can gain a member long
 * after the row was first drawn. Restarting the app appeared to fix it because a full search
 * rebuilds every card from scratch, which is also why it looked like a background-refresh fault
 * rather than a rendering one.
 *
 * The generalisable form, worth more than the fix: **an incremental update must reconcile, not just
 * append.** Anything that decides "have I seen this id" is answering a different question from
 * "does what I drew still match what the server says".
 *
 * Two halves: the reconciliation decision, mirrored; and the wiring, asserted against app.js,
 * because the regression is a one-line "optimisation" away.
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

// ---- half 1: the decision ----------------------------------------------------------------------
// What prependNewImages does with one row of the server's answer, given what it already had.

function decide(have, it) {
  const cur = have.get(Number(it.id));
  if (cur) return JSON.stringify(cur) !== JSON.stringify(it) ? 'rerender' : 'leave';
  return 'new';
}

const still = { id: 20, filename: 'run_00001_.png', video_id: null, group_members: null, is_set: false };
const merged = { id: 20, filename: 'run_00001_.png', video_id: 21, group_members: '21,20', is_set: false };

console.log('a still that has since been joined by its video');
check('the row is one we already drew', new Map([[20, still]]).has(20), true);
check('...and it is NOT left alone', decide(new Map([[20, still]]), merged), 'rerender');
check('the old behaviour would have skipped it',
      (m => m.has(Number(merged.id)) ? 'leave' : 'new')(new Map([[20, still]])), 'leave');

console.log('\nand the cases that must not become busywork');
check('an untouched row is left alone', decide(new Map([[20, still]]), still), 'leave');
check('a genuinely new row is new', decide(new Map([[20, still]]), { id: 99 }), 'new');
// The shape changes in more ways than pairing — a video's dimensions backfill when its poster
// frame is made, a set gains a member, a quality score lands. A whole-item compare catches all of
// them; a list of fields to watch is a list that goes stale.
check('a backfilled dimension counts as changed',
      decide(new Map([[20, { id: 20, width: null }]]), { id: 20, width: 1024 }), 'rerender');
check('a set gaining a member counts as changed',
      decide(new Map([[20, { id: 20, group_count: 2 }]]), { id: 20, group_count: 3 }), 'rerender');

// ---- half 2: the wiring ------------------------------------------------------------------------

const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');
const fn = SRC.slice(SRC.indexOf('async function prependNewImages()'),
                     SRC.indexOf('\n}\n', SRC.indexOf('async function prependNewImages()')));
check('prependNewImages is still there', fn.length > 0, true);

console.log('\nthe wiring');
// The exact line that caused it. Skipping on id ALONE is the fault, whatever it is spelled with.
check('no longer skips a row just because its id is known',
      /if \(have\.has\([^)]*\)\) continue;/.test(fn), false);
check('`have` carries the ITEM, not just the id (a Set cannot be compared against)',
      /const have = new Map\(/.test(fn), true);
check('a known row is compared before being left alone',
      /JSON\.stringify\(cur\) !== JSON\.stringify\(it\)/.test(fn), true);
// The other half of merging: a member that used to be its own card stops being one.
check('members folded into a group lose their own cards',
      /dropFoldedMembers\(/.test(fn), true);
check('  and the paging window follows them',
      /state\.offset = Math\.max\(0, state\.offset - folded\)/.test(fn), true);

console.log('\n' + (failures ? `FAILED: ${failures} check(s)` : 'all checks passed'));
process.exit(failures ? 1 : 0);
