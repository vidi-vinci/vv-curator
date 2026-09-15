/* test_undo_order.js — undoing a recycle must restore the ORDER, not just the items.
 *
 * Run: node test_undo_order.js
 *
 * THE BUG THIS EXISTS TO STOP COMING BACK. deleteSelected() removes cards one at a time and records
 * {idx, item} for each, where idx is measured against `state.items` AS IT STANDS AT THAT MOMENT —
 * an array already shrunk by the removals before it. Delete three adjacent cards and all three
 * records carry the SAME index. Re-inserting them forward therefore reverses the group:
 *
 *     ABCDEFGH  minus D,E,F  ->  undo gave  ABCFEDGH
 *
 * That shipped. It reported as "undo works, but the grid is jumbled afterwards and I had to
 * refresh". The inverse of a sequence of splices is those splices undone in REVERSE order.
 *
 * How it got past a browser check is the part worth keeping: the check printed the restored
 * positions as [10,9,8] against [8,9,10], and the filenames were all present and correct. Presence
 * read as success. THE ORDER IS THE ASSERTION — a list of the right items in the wrong order looks
 * right at a glance, so it has to be machine-checked rather than eyeballed.
 *
 * Pure logic, no DOM: this is the index arithmetic from undoRestorer() and its capture loop, which
 * is where the fault was. Kept in step with app.js by construction — if the capture loop there
 * changes shape, this mirrors it and the numbers stop matching.
 */
'use strict';

let failures = 0;
function check(name, got, want) {
  const ok = String(got) === String(want);
  console.log((ok ? '  ok    ' : '  FAIL  ') + name + (ok ? '' : `\n          got ${got}, want ${want}`));
  if (!ok) failures++;
}

// --- the two halves of the mechanism, mirrored from app/app.js -----------------------------------

// deleteSelected()/deleteCurrent(): remove in the order the ids arrive, recording the index each
// one had at the moment it went.
function capture(items, ids) {
  const removed = [];
  for (const id of ids) {
    const idx = items.findIndex(x => x === id);
    if (idx !== -1) { removed.push({ idx, item: items[idx] }); items.splice(idx, 1); }
  }
  return removed;
}

// undoRestorer(): unwind in REVERSE removal order. This is the line under test.
function restore(items, removed) {
  for (let i = removed.length - 1; i >= 0; i--)
    items.splice(Math.min(removed[i].idx, items.length), 0, removed[i].item);
  return items;
}

function roundTrip(original, ids) {
  const items = [...original];
  const removed = capture(items, ids);
  return restore(items, removed).join('');
}

const G = 'ABCDEFGH'.split('');

console.log('\nUndo restores the order, not just the items\n');

check('three adjacent cards (the reported bug)', roundTrip(G, ['D', 'E', 'F']), 'ABCDEFGH');
check('  and the shrinking capture really does repeat an index',
      capture([...G], ['D', 'E', 'F']).map(r => r.idx).join(','), '3,3,3');
check('two adjacent', roundTrip(G, ['A', 'B']), 'ABCDEFGH');
check('a single card', roundTrip(G, ['E']), 'ABCDEFGH');
check('the whole grid', roundTrip(G, [...G]), 'ABCDEFGH');
check('scattered, picked left to right', roundTrip(G, ['B', 'E', 'G']), 'ABCDEFGH');
// Ctrl-clicking builds a selection in click order, not grid order — the ids can arrive any way at
// all, so the restore may not assume they are sorted.
check('scattered, picked OUT OF ORDER', roundTrip(G, ['G', 'B', 'E']), 'ABCDEFGH');
check('reverse order', roundTrip(G, ['F', 'E', 'D']), 'ABCDEFGH');
check('the first and last card', roundTrip(G, ['H', 'A']), 'ABCDEFGH');
check('a run reaching the end', roundTrip(G, ['G', 'H']), 'ABCDEFGH');

// The clamp exists because the array can be shorter than a captured index by the time undo runs —
// another batch committed in between, or a filter narrowed the view. It must not throw or misplace.
console.log('\nwhen the array has shrunk under the captured index');
const shrunk = ['A', 'B', 'C'];
const late = [{ idx: 99, item: 'Z' }];
check('a stale index clamps to the end rather than throwing',
      restore([...shrunk], late).join(''), 'ABCZ');

// The forward version, kept as the counter-example so the reason for the reverse loop stays visible.
console.log('\nthe shipped-and-wrong version, for the record');
function restoreForward(items, removed) {
  for (const { idx, item } of removed) items.splice(Math.min(idx, items.length), 0, item);
  return items;
}
const bad = (() => { const it = [...G]; const rm = capture(it, ['D', 'E', 'F']);
                     return restoreForward(it, rm).join(''); })();
check('inserting forward reverses the group (this is what the user saw)', bad, 'ABCFEDGH');

console.log('\n' + (failures ? `${failures} FAILED` : 'all passed') + '\n');
process.exit(failures ? 1 : 0);
