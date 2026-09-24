/* Regression test for Rescan all libraries (rebuildOne / rescanAllLibs / runRescanAll in app.js).
 *
 * Run: node test_rescan_all.js
 *
 * Why this test exists. A thorough re-read used to be PER LIBRARY, and that cost real confusion: a
 * feature that adds a new metadata field only fills the libraries you remember to catch up, so the author
 * did one of four, saw blank generation settings in the other three, and reported it as a parsing
 * bug. Two rounds of diagnosis went looking for a parser gap that was never there.
 *
 * Renamed from test_rebuild_all.js on 2026-09-12 with the feature: `Rebuild metadata` and
 * `Rebuild all libraries…` left the per-library menu, which now has one scan verb, and the
 * all-libraries action moved to the menu that is actually about all the libraries.
 *
 * The whole feature turns on ONE flag. `force: true` is what makes a scan re-read files it has
 * already seen — a plain rescan skips anything whose mtime and size are unchanged, which is every
 * file in the case this button exists for. Drop that flag and the button still runs, still shows
 * progress, still reports success, and changes NOTHING. That is the failure this pins, and it is
 * invisible from the outside.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'index.html'), 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

function slice(startMarker, endMarker) {
  const a = src.indexOf(startMarker);
  if (a < 0) throw new Error('could not find "' + startMarker + '" in app/app.js');
  const b = src.indexOf(endMarker, a + startMarker.length);
  if (b < 0) throw new Error('could not find end marker "' + endMarker + '" after ' + startMarker);
  return src.slice(a, b);
}
// One function's body: from its declaration to the closing brace in column 0. Used where there is
// no stable next-declaration to anchor on — a marker that silently runs past the end of the
// function makes every assertion below it meaningless, which is exactly what happened once here.
function fnBody(decl) {
  const a = src.indexOf(decl);
  if (a < 0) throw new Error('could not find "' + decl + '" in app/app.js');
  const b = src.indexOf('\n}', a);
  if (b < 0) throw new Error('unterminated function: ' + decl);
  return src.slice(a, b + 2);
}

(async () => {
  console.log('\nRescan all libraries\n');

  // ---- 1. THE FLAG. The real function, run against a stubbed fetch. -------------------------
  {
    const body = fnBody('async function rebuildOne(');
    const calls = [];
    const rebuildOne = new Function('fetch', 'watchScan', body + '\nreturn rebuildOne;')(
      async (url, opts) => { calls.push({ url, body: JSON.parse(opts.body) }); return { json: async () => ({}) }; },
      async () => true);

    await rebuildOne('LIB1', true);
    check('rebuildOne posts to /api/scan', calls.length === 1 && calls[0].url === '/api/scan',
      JSON.stringify(calls));
    check('  with force:true — WITHOUT THIS THE BUTTON DOES NOTHING',
      !!calls[0] && calls[0].body.force === true,
      'a scan without force skips every unchanged file, i.e. all of them: '
      + JSON.stringify(calls[0] && calls[0].body));
    check('  for the library it was handed', !!calls[0] && calls[0].body.key === 'LIB1',
      JSON.stringify(calls[0] && calls[0].body));

    // A refused scan (another job holds the index) must stop a multi-library run rolling on.
    const refused = [];
    const rebuildRefused = new Function('fetch', 'watchScan', body + '\nreturn rebuildOne;')(
      async (url, opts) => { refused.push(opts); return { json: async () => ({ error: 'busy' }) }; },
      async () => true);
    check('  and a refused scan returns falsy rather than pretending it ran',
      !(await rebuildRefused('LIB1', true)));
  }

  // ---- 2. offline libraries are skipped, not attempted ---------------------------------------
  // The author's choice, and the failure it avoids is worse than a skip: a forced scan of an unreachable
  // share is how a library could get pruned to nothing.
  const all = fnBody('async function rescanAllLibs(');
  check('rescan-all filters out offline libraries', /isRootOffline\(/.test(all));
  check('  and names them in the confirm, before the run starts',
    /Skipping/.test(all) && all.indexOf('Skipping') < all.indexOf('uiConfirm'),
    'being told what a run will skip BEFORE it takes ten minutes is the point');
  check('  and bails when every library is offline', /if \(!live\.length\)/.test(all));

  // ---- 3. ONE confirm and ONE run, not one per library ---------------------------------------
  const run = fnBody('async function runRescanAll(');
  check('exactly one confirm for the whole run',
    (all.match(/uiConfirm\(/g) || []).length === 1 && !/uiConfirm\(/.test(run),
    'the menu asks once and the update offer asks once; a confirm inside the run would ask twice');
  check('  wrapped in a single Job.during, so the screen cannot flash between libraries',
    /Job\.during\(/.test(run));
  check('  Stop ends the RUN, not just the library in front of it', /_runStopped/.test(run));
  check('  and one refresh at the end, not one per library',
    (run.match(/afterScanRefresh\(/g) || []).length === 1);
  check('  and every library goes through the same forced helper', /rebuildOne\(/.test(run));

  // ---- 4. both ways in share one run ----------------------------------------------------------
  // The menu item and the after-an-update offer. If these diverge, "rescan all" quietly means two
  // different things depending on which door you came through.
  const offer = fnBody('async function offerCatchUp(');
  check('the update offer runs the same function the menu does', /runRescanAll\(/.test(offer));
  check('  and asks before it, never during', /uiConfirm\(/.test(offer));
  // TWO TIERS, and they must not collapse into one. "Do it later" stays sessionStorage, so a
  // postponed rescan returns next launch rather than being lost. The checkbox is the other
  // answer and is allowed to persist -- but only KEYED ON THE READER, which is what makes it
  // reset on a build that reads more. A bare localStorage key here would be "never again",
  // and the user would never learn a later version left their library behind.
  check('  "Do it later" is remembered for the session only, so it returns next launch',
    /sessionStorage\.setItem\(CATCHUP_SNOOZE/.test(offer));
  check('  and the opt-out persists, scoped to the reader version',
    /localStorage\.setItem\(hushKey/.test(offer) && /CATCHUP_HUSH\}\.\$\{j\.reader\}/.test(offer));
  // PRESENCE BEFORE ORDER: indexOf returns -1 for a string that is gone, which is less than
  // everything, so an order-only check goes green the moment the thing it guards is deleted.
  check('    read on EITHER button, so Escape and Rescan now both honour it',
    /dlgChecked\(\)/.test(offer) && /if \(!go\)/.test(offer)
    && offer.indexOf('dlgChecked()') < offer.indexOf('if (!go)'));
  check('  and says where the action went', /Rescan all libraries/.test(offer));

  // ---- 5. reachable from the menu -------------------------------------------------------------
  // THE SCOPE IS THE MENU IT IS IN. It acts on every library, so it belongs in the Libraries menu
  // and not in a row's ⋯ — which is where it used to sit, ignoring the row it was opened from.
  check('the Libraries menu offers it', /id="libRescanAll"/.test(html));
  check('  above Add library', html.indexOf('id="libRescanAll"') < html.indexOf('id="libAdd"'));
  check('  and it is wired up', /#libRescanAll'\)\.addEventListener/.test(src));
  check('the per-library menu no longer carries either rebuild item',
    !/data-act="rebuild"/.test(html) && !/data-act="rebuildall"/.test(html));

  console.log('\n' + (failures ? failures + ' FAILED' : 'all passed') + '\n');
  process.exit(failures ? 1 : 0);
})();
