/* test_auto_refresh.js — the library must keep up when the app is left alone, and catch up the
 * moment it is picked back up.
 *
 * Run: node test_auto_refresh.js
 *
 * TWO FAULTS, ONE COMPLAINT — the author lived with "the grid doesn't update in the background" for
 * months, and it was two separate gates, each individually defensible, each stopping the feature in
 * the exact situation it exists for.
 *
 * 1. COMING BACK DID NOTHING. Auto-refresh fires only after IDLE_MS of TOTAL inactivity — right,
 *    while someone is working. But returning to a window always produces a mousemove, which stamps
 *    `_lastActivity`. So the tick fired on return, hit the activity gate, and returned having done
 *    nothing — and kept doing so for as long as the mouse kept moving. Seeing what had landed
 *    required sitting perfectly still for thirty seconds. The handler even carried a comment saying
 *    it deliberately does not stamp activity, "that would delay the refresh at the very moment it's
 *    wanted": the intent was right and the code could not carry it, because the browser stamps it
 *    for you, through the pointer, before the handler runs.
 *
 *    THE GENERAL FORM: a gate keyed on "the user isn't touching anything" cannot be read at the
 *    instant the user reaches for the window.
 *
 * 2. AN OPEN IMAGE STOPPED IT DEAD. The detail view was counted among the mid-task modals, so with
 *    an image open the tick returned before even probing for changes. But that is where the app is
 *    USED, and leaving an image up while generating in ComfyUI is precisely the case auto-refresh
 *    exists for. The gate was never protecting the grid — it was protecting the FILMSTRIP, which
 *    is the result set the arrows walk — and it stopped both. The freeze now lives in the one step
 *    that would move something under the open view.
 *
 * Two halves, because either fault could return through either route:
 *   1. the GATE logic, mirrored from autoRefreshTick();
 *   2. the WIRING, asserted against app.js itself — these regressions are one word wide.
 */
'use strict';

const fs = require('fs');
const path = require('path');

let failures = 0;
function check(name, got, want) {
  const ok = String(got) === String(want);
  console.log((ok ? '  ok    ' : '  FAIL  ') + name + (ok ? '' : `\n          got ${got}, want ${want}`));
  if (!ok) failures++;
}

// --- half 1: the gates, mirrored from autoRefreshTick() -----------------------------------------

const IDLE_MS = 30000;

// Whether the tick gets past its two TIMING gates. The in-flight guards (a scan already running, a
// manual refresh in flight) are deliberately not modelled: no mode skips those, so they cannot be
// the thing that regresses here.
function passesGates(mode, msSinceActivity, msSinceLastRefresh) {
  const forced = mode === 'force';
  const returning = mode === 'return';
  if (!forced) {
    if (!returning && msSinceActivity < IDLE_MS) return false;
    if (msSinceLastRefresh < IDLE_MS) return false;
  }
  return true;
}

console.log('the timer tick — unchanged, and the reason the gate exists');
check('stands down while the user is working', passesGates(undefined, 100, 999999), false);
check('fires once they have gone quiet', passesGates(undefined, IDLE_MS + 1, 999999), true);
check('and not twice in one idle stretch', passesGates(undefined, IDLE_MS + 1, 500), false);

console.log('\ncoming back to the window');
// 0ms since activity is not an edge case here, it is the normal case: the mousemove that comes with
// reaching for the window lands before the handler does.
check('catches up even with the mouse moving', passesGates('return', 0, 999999), true);
check('...which the old behaviour did not', passesGates(undefined, 0, 999999), false);
check('still throttled, so flicking costs one catch-up', passesGates('return', 0, 500), false);

console.log('\nswitching the timer on, or launching with it on');
check('catches up regardless of both gates', passesGates('force', 0, 0), true);

// --- half 2: the wiring, asserted against the real source ---------------------------------------

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'app.js'), 'utf8');

console.log('\nboth doors back into the app ask for a catch-up');
// A window that was hidden fires visibilitychange; one that was merely unfocused fires only focus.
// Wiring one and not the other leaves half the returns stale, which is how this reads as
// intermittent rather than broken.
const visAt = src.indexOf("document.addEventListener('visibilitychange'");
check('visibilitychange asks for it',
      /autoRefreshTick\('return'\)/.test(src.slice(visAt, visAt + 700)), true);
check('window focus asks for it too',
      /window\.addEventListener\('focus',[\s\S]{0,80}?autoRefreshTick\('return'\)/.test(src), true);

console.log('\nthe gate itself still exempts a return');
// Matched loosely on purpose: each gate now names itself to the trace, so the `return` is a
// traceGate() call rather than a bare one. What must not change is WHICH clock each gate reads and
// that only the activity one is exempt on return.
check('the activity gate is skipped when returning',
      /if \(!returning && Date\.now\(\) - _lastActivity < IDLE_MS\)/.test(src), true);
check('the throttle is NOT skipped when returning',
      /if \(Date\.now\(\) - _lastAutoRefresh < IDLE_MS\)/.test(src), true);
check('no caller passes a bare truthy mode any more', /autoRefreshTick\(true\)/.test(src), false);

console.log('\nan open image freezes the view, not the library');
check('the tick asks about modals, not layers',
      /if \(anyModalOpen\(\)\) return traceGate\(/.test(src), true);
const modalFn = /function anyModalOpen\(\)[\s\S]{0,220}?\]/.exec(src);
check('anyModalOpen() exists', !!modalFn, true);
check('and the open image is not one of them', modalFn[0].includes('#overlay'), false);
// The freeze moved to the one step that would actually move something under the open view: the
// filmstrip is the result set, state.index is a position in it, and on newest-first the new cards
// land at the front — the worst case for both.
check('new cards are held back while it is open',
      /#overlay'\)\.classList\.contains\('hidden'\)\) \{[\s\S]{0,240}?_deferredNew = true;[\s\S]{0,240}?return;/
        .test(src), true);
check('and folded in when it closes',
      /function closeDetail\(\)[\s\S]{0,800}?foldInDeferred\(\)/.test(src), true);

console.log('\nevery way the tick declines is visible to the trace');
// THE WHOLE REASON THE TWO FAULTS ABOVE TOOK MONTHS: each was a bare `return`, so "it didn't
// update" looked identical whichever gate had stopped it, and both were eventually found by reading
// the code and guessing. A new gate added without a name is how the third one hides, so the shape
// is pinned rather than trusted.
const tick = /async function autoRefreshTick\(mode\)[\s\S]*?\n\}/.exec(src)[0];
const gateRegion = tick.slice(0, tick.indexOf('traceGate(null)'));
const bareReturns = (gateRegion.match(/\breturn\s*;/g) || []).length;
check('no gate returns without naming itself', bareReturns, 0);
// Counted as CALLS, not as `return traceGate(` — the three busy gates share one return through a
// ternary, and a shape check that assumed otherwise would fail on correct code.
check('every gate names itself', (gateRegion.match(/traceGate\(/g) || []).length >= 8, true);
// Keyed on the gate, not its wording: the sentences carry live numbers ("active 5s ago"), so
// comparing them makes every tick a new reason and the 5s timer floods a 600-row buffer.
check('the run is keyed on the gate, not the sentence',
      /function traceGate\(key, detail\)[\s\S]{0,200}?if \(key === _gateWas\)/.test(src), true);
check('a fresh recording forgets any run in progress',
      /function traceRefreshState\(\)[\s\S]{0,300}?_gateWas = null/.test(src), true);
// A failing refresh and a quiet one were the same from the outside, which is the worst case of all.
check('a failed refresh is recorded, not swallowed',
      /catch \(e\) \{[\s\S]{0,400}?Trace\.add\('refresh', `FAILED/.test(src), true);
// Each deferral names its own FIX, because they are three different things for the user to do:
// close the image, scroll back to the top, or change the sort. Asserted by phrase rather than by
// counting rows — two of them share one template literal.
check('held back: an open image says so', /held back — [\s\S]{0,40}an image is open/.test(src), true);
check('held back: being scrolled down says so', /scrolled \$\{[\s\S]{0,40}px down/.test(src), true);
check('held back: the wrong sort says so', /not newest-first/.test(src), true);

console.log('\n' + (failures ? `FAILED: ${failures} check(s)` : 'all checks passed'));
process.exit(failures ? 1 : 0);
