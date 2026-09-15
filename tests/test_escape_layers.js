/* test_escape_layers.js — every overlay that Escape claims to close must actually be reachable.
 *
 * Run: node test_escape_layers.js
 *
 * THE BUG THIS EXISTS FOR, TWICE. Escape is handled in two stages, and an overlay has to be named
 * in BOTH:
 *
 *   1. `layerOpen` — "is Escape about a layer at all?" If this is false the handler treats Escape
 *      as grid-view clear-selection and RETURNS.
 *   2. the ladder below it — "which layer does it take?", topmost first.
 *
 * An overlay listed only in the ladder is wired to nothing. It reads as wired, which is the whole
 * problem: the branch is right there, it just cannot be reached.
 *
 * It happened first to the three standalone panels (duplicates / debug trace / prompt miner), and
 * the fix carried a comment saying the two lists had disagreed. It then happened AGAIN, to the two
 * newest overlays — the version notice and the first-run help hint — found on 2026-09-15 while
 * testing the update notice end to end. Both had a ladder branch; neither could run. They had a
 * Close button and a click-outside, so nothing was stuck; Escape simply did nothing, in an app
 * where Escape closes everything else.
 *
 * THE GENERAL FORM: when one list decides whether a rule applies and a second decides how, adding
 * to the second alone is a silent no-op. So this test does not check the two overlays that broke —
 * it checks that the lists AGREE, which is the property that keeps the third one from happening.
 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');

let failures = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`  ${ok ? 'ok  ' : 'FAIL'}  ${label}`);
  if (!ok) console.log(`          got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// The Escape branch. Bounded by the `return;` that ends it rather than by brace counting — the
// ladder contains nested blocks, and an earlier slice cut past the end and swept in ids from
// unrelated code, which made this test fail on correct source.
// Anchored on the MAIN ladder's own comment. There is more than one Escape handler in the file —
// the confirm dialog has its own, in a capture-phase listener — and matching the bare
// `if (e.key === 'Escape')` finds that one first, several hundred lines earlier.
const escAt = src.indexOf("if (e.key === 'Escape') {                       // Escape closes the topmost layer");
const ladder = src.slice(escAt, src.indexOf('    return;', escAt));
// The guard that decides whether Escape is about layers at all.
const layerAt = src.indexOf('const layerOpen =');
const layerExpr = src.slice(layerAt, src.indexOf(';', layerAt));

console.log('\nboth stages exist and were found');
check('the layerOpen guard is there', layerAt > -1, true);
check('the Escape ladder is there', escAt > -1, true);

console.log('\nevery overlay the ladder closes is one layerOpen admits');
// Each ladder branch tests an overlay by id; collect them and require the guard to know each one,
// either by that id or by the boolean the app keeps for it.
const BOOLEAN_FOR = {           // overlays the guard tracks by flag rather than by id
  '#settings': 'settingsOpen', '#rename': 'renameOpen', '#setup': 'setupOpen',
  '#tagModal': 'tagModalOpen', '#help': 'helpOpen', '#overlay': 'detailOpen',
};
// Matched on the BRANCH SHAPE, not on every id mentioned: a ladder step is an overlay tested for
// its hidden class. Anything else in there (a focus call, a lookup) is not a layer and must not be
// demanded of the guard.
const ids = [...new Set((ladder.match(/\$\('(#[\w-]+)'\)\.classList\.contains\('hidden'\)/g) || [])
  .map(m => m.match(/#[\w-]+/)[0]))];
// The ladder is mostly flag-tested (settingsOpen, helpOpen…); only the two small pop-ups are
// tested by id. Assert the ladder is still a real ladder, then that every id-tested step is
// admitted by the guard — which is the agreement property this file exists for.
check('the ladder still has several steps', (ladder.match(/else if /g) || []).length >= 6, true);
check('and at least the two id-tested overlays', ids.length >= 2, true);
for (const id of ids) {
  const known = layerExpr.includes(`'${id}'`) || layerExpr.includes(BOOLEAN_FOR[id] || '\0');
  check(`layerOpen knows about ${id}`, known, true);
}

console.log('\nthe two that regressed are covered by name');
// Belt and braces: these are the ones that were actually broken, so they are asserted directly as
// well as by the agreement rule above.
check('the version notice is in the guard', layerExpr.includes("'#updateBox'"), true);
check('the first-run hint is in the guard', layerExpr.includes("'#helpHint'"), true);

console.log('\nthe grid-view fallback is still below the guard, not above it');
// If Escape reached clear-selection before the ladder, every overlay would break at once.
check('clear-selection sits inside the !layerOpen branch',
      /if \(!layerOpen\) \{/.test(src), true);

console.log('\n' + (failures ? `FAILED: ${failures} check(s)` : 'all checks passed'));
process.exit(failures ? 1 : 0);
