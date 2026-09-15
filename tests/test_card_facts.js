/* test_card_facts.js — the small print on a grid card.
 *
 * Run: node test_card_facts.js
 *
 * A card now carries a two-tier band: three short facts always (dimensions, duration, age) and
 * three more revealed with the card's other hover chrome (file size, model, folder). Which facts
 * sit in which tier is EXPECTED to move — the author asked for the first cut precisely so he could judge
 * the split by using it — so this pins the machinery and the formatting, not the membership.
 *
 * It runs the REAL functions, lifted out of app/app.js rather than mirrored here. Mirroring is the
 * house idiom for pure arithmetic (see test_undo_order.js), but these are string formatters whose
 * whole risk is an edge case in the exact code that ships, and a copy would drift.
 *
 * The three faults it exists to prevent:
 *
 *   1. `fmtBytes(null)` returns an em dash BY DESIGN, and its own comment tells callers wanting a
 *      different format to guard rather than teach it a second one. A file with no recorded size
 *      would otherwise print a bare "—" in the middle of the band. This is the likeliest way the
 *      feature ships subtly wrong, so it gets its own assertion.
 *   2. `365 / 365.25` floors to ZERO, so a naive year branch prints "0 years" on something plainly
 *      a year old — a fault that only ever shows up in production, on old files.
 *   3. A song's face already draws its own duration, tempo and key. A band there would print the
 *      length twice, on the one card design that is tight at every size.
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

// ---- lift the real implementations out of app.js -----------------------------------------------

// LINE ENDINGS ARE NORMALISED ON THE WAY IN. grab() below looks for boundaries spelled '\n}\n',
// and this repo is checked out with CRLF on Windows -- so every one of those searches found
// nothing and the test died at its first grab() before running a single check. It is invisible
// on a LF checkout, which is exactly how it survived: the file that decides whether this test
// works is .gitattributes, not this one. Reading the source as LF makes the boundaries mean what
// they say on either platform, and nothing below cares about the difference.
const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8').replace(/\r\n/g, '\n');

function grab(startsWith, endsWith) {
  const i = SRC.indexOf(startsWith);
  if (i === -1) throw new Error('not found in app.js: ' + startsWith);
  const j = SRC.indexOf(endsWith, i);
  if (j === -1) throw new Error('no end for: ' + startsWith);
  return SRC.slice(i, j + endsWith.length);
}

const parts = [
  'const state = {};',                       // the app's, stubbed to the one field these read
  grab('function esc(s)', '\n'),
  grab('function fmtBytes(n)', '\n}\n'),
  grab('function fmtAge(sec)', '\n}\n'),
  grab('function folderLeaf(f)', '\n}\n'),
  grab('function fmtDuration(', '\n}\n'),
  grab('const CARD_FACTS = [', '\n];\n'),
  grab('const CARD_FACT_TIERS =', '\n'),
  grab('const CARD_FACT_BY_KEY =', '\n'),
  grab('function cardFactList()', '\n}\n'),
  grab('function cardFactLine(it, tier, list)', '\n}\n'),
  grab('function cardFactsHTML(it)', '\n}\n'),
].join('\n');

const api = new Function(parts +
  '\nreturn { fmtAge, folderLeaf, cardFactsHTML, cardFactList, cardFactLine, CARD_FACTS, CARD_FACT_TIERS, state };')();
const { fmtAge, folderLeaf, cardFactsHTML, cardFactList, cardFactLine, CARD_FACTS, CARD_FACT_TIERS, state } = api;

const ago = secs => Math.floor(Date.now() / 1000) - secs;
const DAY = 86400;

// ---- 1. how old is it --------------------------------------------------------------------------

console.log('how old is it');
check('nothing to say', fmtAge(null), '');
check('seconds old', fmtAge(ago(5)), 'just now');
check('a minute short of an hour', fmtAge(ago(59 * 60)), '59 min');
check('exactly an hour', fmtAge(ago(3600)), '1 hr');
check('most of a day', fmtAge(ago(23 * 3600)), '23 hr');
check('a day', fmtAge(ago(DAY)), '1 day');
check('  singular, not "1 days"', fmtAge(ago(DAY)).endsWith('1 day'), true);
check('his example: 23 days', fmtAge(ago(23 * DAY)), '23 days');
check('the last day before months', fmtAge(ago(29 * DAY)), '29 days');
check('a month', fmtAge(ago(31 * DAY)), '1 month');
check('his example: 2 months', fmtAge(ago(70 * DAY)), '2 months');
check('the last month before years', fmtAge(ago(364 * DAY)), '11 months');
check('a year', fmtAge(ago(366 * DAY)), '1 year');
check('his example: 2 years', fmtAge(ago(2 * 365 * DAY)), '2 years');
// 365/365.25 floors to 0. A card a year old saying "0 years" is the fault this guards.
check('never "0 years" at the boundary', fmtAge(ago(365 * DAY)), '1 year');
check('a file dated in the future reads as new, not negative', fmtAge(ago(-DAY)), 'just now');

// ---- 2. the folder leaf ------------------------------------------------------------------------

console.log('\nwhich folder');
check('forward slashes', folderLeaf('a/b/c'), 'c');
check('backslashes, which is what Windows stores', folderLeaf('2026\\08\\wan'), 'wan');
check('a trailing separator is not a segment', folderLeaf('x/y/'), 'y');
check('the library root says nothing', folderLeaf('.'), '');
check('nothing says nothing', folderLeaf(''), '');
check('a single folder is its own leaf', folderLeaf('solo'), 'solo');

// ---- 3. the band -------------------------------------------------------------------------------

console.log('\nthe band');
const full = { width: 1024, height: 576, duration: 5, mtime: ago(3 * DAY),
               size: 3670016, model: 'ponyRealV11', folder: 'portraits' };

const both = cardFactsHTML(full);
check('two rows when there is something for each tier',
      (both.match(/class="cf-row/g) || []).length, 2);
check('the always row carries the three short facts, age first',
      /3 days · 1024×576 · 0:05/.test(both), true);
check('the hover row carries the long ones', /3\.5 MB · ponyRealV11 · portraits/.test(both), true);
// The band is anchored by its BOTTOM edge, so the permanent row is emitted LAST — that is what
// stops it moving when the hover row appears above it.
check('hover is emitted before always, so the permanent row never jumps',
      both.indexOf('cf-hover') < both.indexOf('cf-always'), true);

const img = cardFactsHTML({ width: 512, height: 512, mtime: ago(DAY) });
check('an image has no duration, and no gap where it would be',
      /class="cf-row cf-always"[^>]*>1 day · 512×512</.test(img), true);

// THE EM-DASH TRAP. fmtBytes(null) returns '—'; the call site guards instead of teaching fmtBytes
// a second format, because that comment asks callers to do exactly that.
const nosize = cardFactsHTML({ width: 8, height: 8, mtime: ago(DAY), size: null, model: 'm' });
check('a missing file size prints nothing, never a stray dash', nosize.includes('—'), false);
check('  and the rest of its row survives', /class="cf-row cf-hover"[^>]*>m</.test(nosize), true);

check('nothing known at all means no band', cardFactsHTML({}), '');
check('a song gets no band — its face already draws duration/bpm/key',
      cardFactsHTML({ is_audio: true, duration: 38, mtime: ago(DAY) }), '');

const longmodel = cardFactsHTML({ mtime: ago(DAY), model: 'wan2.2_t2v_high_noise_14B_fp8_scaled' });
check('the hover row carries its full text as a tooltip, since it is the one that ellipsises',
      longmodel.includes('title="wan2.2_t2v_high_noise_14B_fp8_scaled"'), true);

// ---- 4. the table itself -----------------------------------------------------------------------
// The Settings tab will render its list from this table rather than keep a second copy, so a
// half-filled entry would surface there as a blank checkbox.

console.log('\nthe fact table');
check('every entry is complete',
      CARD_FACTS.filter(f => !f.key || !f.tier || !f.label || typeof f.get !== 'function').length, 0);
check('every tier is one this renders', CARD_FACTS.every(f => CARD_FACT_TIERS.includes(f.tier)), true);
check('keys are unique', new Set(CARD_FACTS.map(f => f.key)).size, CARD_FACTS.length);
check('both tiers are populated',
      CARD_FACT_TIERS.every(t => CARD_FACTS.some(f => f.tier === t)), true);

// ---- 5. order and placement come from the saved config ------------------------------------------
// Settings -> Cards writes an ordered list; the table above only says what each fact IS and where
// it starts. The author asked for this specifically -- "I prefer the time to come before the image size".

console.log('\nthe saved order');
const sample = { width: 1024, height: 576, duration: 5, mtime: ago(3 * DAY),
                 size: 3670016, model: 'ponyRealV11', folder: 'portraits' };

state.cards = null;
check('with nothing saved, the table' + String.fromCharCode(39) + 's own defaults apply',
      cardFactList().map(r => r.def.key), CARD_FACTS.map(f => f.key));

// His example: age before dimensions.
state.cards = [
  { key: 'age', place: 'always' },
  { key: 'dims', place: 'always' },
  { key: 'duration', place: 'always' },
  { key: 'filesize', place: 'hover' },
  { key: 'model', place: 'hover' },
  { key: 'folder', place: 'hover' },
];
check('the saved order is the printed order',
      /3 days · 1024×576 · 0:05/.test(cardFactsHTML(sample)), true);

state.cards = [{ key: 'age', place: 'off' }, { key: 'dims', place: 'always' }];
check('a fact set to off is not printed', /3 days/.test(cardFactsHTML(sample)), false);
check('  and the rest still are', /1024×576/.test(cardFactsHTML(sample)), true);

// A fact can move tiers, which is the whole point of shipping a first cut.
state.cards = [{ key: 'model', place: 'always' }, { key: 'age', place: 'hover' }];
const moved = cardFactsHTML(sample);
check('a fact promoted to always lands in the permanent row',
      /class="cf-row cf-always"[^>]*>ponyRealV11</.test(moved), true);
check('and one demoted lands in the hover row',
      /class="cf-row cf-hover"[^>]*>3 days</.test(moved), true);

// A config written before a fact existed, or after one was retired.
state.cards = [{ key: 'age', place: 'always' }, { key: 'nonesuch', place: 'always' }];
check('a key this build does not know is ignored, not drawn',
      cardFactsHTML(sample).includes('nonesuch'), false);
check('  and the known one still prints', /3 days/.test(cardFactsHTML(sample)), true);
state.cards = null;

// ---- 6. the row text has ONE builder ------------------------------------------------------------
// The card's markup and the minute re-tick that keeps an age honest both ask this. A second copy
// of the join would be a second answer to what a row says, and the two would drift on the first
// change to either -- so the pin is that the rendered markup CONTAINS exactly what the builder
// returns, for both tiers.

console.log('\none builder for a row');
state.cards = null;
const html = cardFactsHTML(sample);
for (const tier of CARD_FACT_TIERS) {
  const line = cardFactLine(sample, tier);
  check(`the ${tier} row's markup says exactly what the builder does`,
        html.includes('>' + line + '<'), true);
}
// An age is the only fact that changes while you look at it, which is why the re-tick exists.
const fresh = { mtime: ago(30), width: 8, height: 8 };
const older = { mtime: ago(3 * DAY), width: 8, height: 8 };
check('the same file at two ages gives two different lines',
      cardFactLine(fresh, 'always') !== cardFactLine(older, 'always'), true);
check('  and only the age part moved',
      cardFactLine(fresh, 'always').includes('8×8')
      && cardFactLine(older, 'always').includes('8×8'), true);

console.log('\n' + (failures ? `FAILED: ${failures} check(s)` : 'all checks passed'));
process.exit(failures ? 1 : 0);
