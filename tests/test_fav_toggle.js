/* test_fav_toggle.js — the star must be able to turn OFF again.
 *
 * Run: node tests/test_fav_toggle.js
 *
 * THE BUG, reported by the author twice and intermittent both times: "I can't tap the star to favorite",
 * then "it seems to not update state when clicked. now it just stays yellow", then — after it
 * appeared to fix itself — "I also hit the favorite not working again. it seems to be dependent on
 * something."
 *
 * It was dependent on something. `toggleCardFav` decided which way to toggle by looking the card up
 * in `state.items`:
 *
 *     const it = state.items.find(x => String(x.id) === id);
 *     const on = !(it && it.fav);          // row missing -> ALWAYS true
 *
 * A card whose row is missing from that list therefore asked the server to turn the favourite ON on
 * every press. The star lit and never went out, however many times it was clicked — and since the
 * list falls out of step only sometimes, so did the bug.
 *
 * THE GENERAL FORM, and the reason this is worth a test rather than a one-line fix: a control's
 * state was read from a MODEL that can drift, when the thing being toggled is what is on SCREEN.
 * For a two-state control the screen is the honest source, because it is what the user is
 * responding to. The model is still preferred where it has the row, since the rest of the grid
 * reads it.
 *
 * Two halves, because the fault could return through either: the DECISION, mirrored here; and the
 * WIRING, asserted against app.js, since deleting the fallback is a one-line "simplification".
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

// ---- 1. the decision ---------------------------------------------------------------------------
// Mirrored from toggleCardFav. `it` is the row from state.items (undefined when it has drifted);
// `shown` is whether the star on the card is currently lit.
function wantsOn(it, shown) {
  return it ? !it.fav : !shown;
}

// A card the list knows about: the row decides, and it alternates.
let row = { fav: 0 };
let seq = [];
for (let i = 0; i < 4; i++) { const on = wantsOn(row, false); seq.push(on); row.fav = on ? 1 : 0; }
check('with the row present it alternates', seq, [true, false, true, false]);

// THE REGRESSION: the row is missing. Before the fix this returned true forever.
let shown = false;
seq = [];
for (let i = 0; i < 4; i++) { const on = wantsOn(undefined, shown); seq.push(on); shown = on; }
check('with the row MISSING it still alternates', seq, [true, false, true, false]);
check('  ...and can reach OFF at all, which is the whole bug', seq.includes(false), true);

// A lit star with no row must turn OFF, not on again — the exact press the author was making.
check('a lit star with no row turns off', wantsOn(undefined, true), false);
// And the row still wins where it exists, even if the DOM disagrees (a half-painted card).
check('the row outranks the screen when it is there', wantsOn({ fav: 1 }, false), false);

// ---- 2. the wiring -----------------------------------------------------------------------------
const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');
const body = src.slice(src.indexOf('async function toggleCardFav'),
                       src.indexOf('// ---- the Hidden mark'));
check('toggleCardFav falls back to the star on screen',
      /classList\.contains\('on'\)/.test(body), true);
check('  ...and only when the row is absent', /it \? !it\.fav :/.test(body), true);
// A failed request used to look exactly like a success: the star lit locally either way.
check('a failed request is reported rather than painted as success',
      /r && r\.error/.test(body), true);

const detail = src.slice(src.indexOf('async function toggleDetailFav'),
                         src.indexOf('async function toggleCardFav'));
check('the detail view checks the reply too', /r && r\.error/.test(detail), true);

// ---- 3. the star must stay gold under the pointer ----------------------------------------------
// A SECOND BUG ON THE SAME CONTROL, reported straight after the first: "the star turns white until
// I release the mouse. just make it yellow." `.card .star.on` and `.card .star:hover` are both
// (0,3,0), so with equal specificity the LATER rule wins -- and the hover was later. Hovering a
// favourited star therefore dragged the gold back to the pale hover colour, which reads as the
// click not having taken.
//
// Pinned as a rule-ordering fact rather than a colour, because that is what can silently come back:
// the guard is one `:not(.on)` a future edit could drop while the stylesheet still looks right.
// COMMENTS STRIPPED FIRST, added 2026-09-20. This matched raw text, so writing the forbidden
// selector inside a comment EXPLAINING why it is forbidden turned the guard red -- which happened
// the first time anyone documented it next to the rule. A guard that punishes prose about itself
// teaches people to stay quiet next to it, and the comment is the part that survives longest.
const css = fs.readFileSync(path.join(__dirname, '..', 'app', 'style.css'), 'utf8')
              .replace(/\/\*[\s\S]*?\*\//g, '');
check('the card star still has its hover', css.includes('.card .star:not(.on):hover'), true);
check('  ...and no bare hover that would outrank the gold',
      /(^|[^)])\.card \.star:hover/m.test(css), false);

console.log('');
console.log(failures ? `${failures} FAILED` : 'all passed');
console.log('');
process.exit(failures ? 1 : 0);
