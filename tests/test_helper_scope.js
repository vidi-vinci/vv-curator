/* test_helper_scope.js — a helper declared inside one function, used inside another.
 *
 * Run: node test_helper_scope.js
 *
 * THE BUG THIS EXISTS TO STOP COMING BACK. On 2026-08-21 two "held back" counts were lifted out of
 * renderCount() into a new fillHeldBack(). They kept calling `fmt` — a one-line formatter declared
 * as a LOCAL inside renderCount, and separately as a local inside renderLibTrigger, and nowhere
 * globally. The extracted copy therefore threw a ReferenceError.
 *
 * It only threw on the branch reached when a count was above zero, so a clean test library never ran
 * it. On a real library it killed renderCount() before the grid rendered and openSettings() before
 * it could un-hide the dialog: a BLANK GRID under a header still correctly reading "Select all
 * 16,337 cards", and a settings button that did nothing. One missing declaration, two symptoms that
 * shared no visible surface.
 *
 * Nothing catches this on the way in. `node --check` parses the file happily, because an unresolved
 * identifier is only an error when the line RUNS — and that line ran only on the user's data.
 *
 * WHAT THIS CHECKS. Every top-level function in app/app.js is read as a block. A name declared as a
 * local helper inside one block (`const fmt = x => …`) and never declared at the top level is
 * flagged if any OTHER block uses it. That is the whole bug class, stated once.
 *
 * WHY THE HEURISTIC IS SAFE HERE. app/app.js declares every top-level function and const at column
 * zero, so a line beginning `function `/`async function `/`const ` at column zero starts a new
 * block. That is a property of this file rather than of JavaScript, which is the trade: a real
 * scope analyser needs a parser, and this needs none while covering the mistake that actually
 * shipped. If the file ever nests differently, this stops being sound — rewrite it rather than
 * loosening it.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
let failures = 0;
function check(name, cond, detail) {
  console.log((cond ? '  ok    ' : '  FAIL  ') + name + (cond || !detail ? '' : '\n          ' + detail));
  if (!cond) failures++;
}

// Strip line comments, block comments and string/template bodies before looking for identifiers:
// a name inside a comment or a message is not a reference, and this file is heavily commented.
// A TEMPLATE LITERAL KEEPS ITS ${…} SPANS, and that is not a detail: the call that shipped the bug
// was `${fmt(n)} hidden in this view right now.` Blanking the whole literal — the obvious way to
// write this — made the main assertion below pass on a file that still contained the defect. The
// mutation check at the end is what exposed that, which is the entire reason it exists.
function decomment(src) {
  const keepInterp = m => {
    let out = '';
    for (let i = 0; i < m.length; i++) {
      if (m[i] === '$' && m[i + 1] === '{') {
        let depth = 1, j = i + 2;
        while (j < m.length && depth) { if (m[j] === '{') depth++; else if (m[j] === '}') depth--; j++; }
        // SEMICOLONS, not spaces, between two interpolations. `${i} ${(32 - len) / 2}` would
        // otherwise join as `i  (32 - len) / 2` and read as a call to i(). A separator that cannot
        // begin a call is the difference between a real finding and a phantom one.
        out += ';' + m.slice(i + 2, j - 1) + ';';
        i = j - 1;
      } else out += (m[i] === '\n' ? '\n' : ' ');
    }
    return out;
  };
  return src
    // KEEP THE NEWLINES a block comment spans. Collapsing one to a single space merges the code
    // after it onto the comment's first line, and every `^function` anchor below that point stops
    // matching — the block split then silently reads one enormous block and finds nothing.
    .replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1')
    .replace(/`(?:\\.|[^`\\])*`/g, keepInterp)
    // A QUOTED STRING MUST NOT MATCH ACROSS A NEWLINE. Without \n in the excluded set, a single
    // unpaired apostrophe — "it's", inside a double-quoted string — swallows everything up to the
    // next apostrophe hundreds of lines later. That collapsed 6,088 lines to 2,314 and left the
    // main check passing on a file it had barely read. Only template literals may span lines.
    .replace(/'(?:\\.|[^'\\\n])*'/g, "''")
    .replace(/"(?:\\.|[^"\\\n])*"/g, '""');
}

const raw = fs.readFileSync(SRC, 'utf8');
const lines = decomment(raw).split('\n');

// --- split into top-level blocks -----------------------------------------------------------------
const TOP = /^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|^(?:const|let|var)\s+([A-Za-z_$][\w$]*)/;
const blocks = [];
let cur = { name: '(file head)', start: 1, lines: [] };
lines.forEach((ln, i) => {
  const m = TOP.exec(ln);
  if (m) { blocks.push(cur); cur = { name: m[1] || m[2], start: i + 1, lines: [] }; }
  cur.lines.push(ln);
});
blocks.push(cur);

// --- names declared at the TOP level are fine to use anywhere -------------------------------------
const globals = new Set();
for (const ln of lines) {
  const m = TOP.exec(ln);
  if (m) globals.add(m[1] || m[2]);
}

// --- names declared as INDENTED const/let helpers, per block --------------------------------------
const LOCAL = /^\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/;
const declaredIn = new Map();          // name -> Set(block names)
for (const b of blocks) {
  for (const ln of b.lines) {
    const m = LOCAL.exec(ln);
    if (m && !globals.has(m[1])) {
      if (!declaredIn.has(m[1])) declaredIn.set(m[1], new Set());
      declaredIn.get(m[1]).add(b.name);
    }
  }
}

// --- a call to a local-only name, from a block that doesn't declare it ----------------------------
// Calls only (`name(`), not bare mentions: a parameter or property sharing the name is not a
// reference to the helper, and a call is the shape the bug took.
const offences = [];
for (const b of blocks) {
  // WHAT COUNTS AS DECLARED HERE is deliberately wider than what counts as a local HELPER above.
  // The app's small modules (Trace, Job, Busy, Loupe, Undo) are object literals whose members are
  // method shorthand — `sync() { … }` — and they call each other by bare name inside the object.
  // Reading only `const x =` declarations flagged fifteen of those as offences. Anything that binds
  // the name in this block clears it: an indented const/let/var, a nested function declaration, or
  // a method shorthand.
  const declaresHere = new Set();
  for (const ln of b.lines) {
    const m = LOCAL.exec(ln); if (m) declaresHere.add(m[1]);
    const f = /^\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/.exec(ln); if (f) declaresHere.add(f[1]);
    const meth = /^\s+(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*\{/.exec(ln);
    if (meth) { declaresHere.add(meth[1]); meth[2].split(',').forEach(p => { const n = p.trim().replace(/[^\w$].*$/, ''); if (n) declaresHere.add(n); }); }
    // A GETTER BINDS THE NAME TOO — `get active() { … }`, `get name() { … }`. Loupe and Job both
    // expose state that way and call it back by bare name inside their own methods.
    const get = /^\s+get\s+([A-Za-z_$][\w$]*)\s*\(/.exec(ln); if (get) declaresHere.add(get[1]);
  }
  // The block's OWN parameters count as declared here — `debounce(fn, ms)` calls `fn()`, and a
  // local helper named `fn` in some other function is nothing to do with it. Arrow parameters
  // inside the block count too, for the same reason.
  const params = new Set();
  const sig = /^(?:async\s+)?function\s+[\w$]*\s*\(([^)]*)\)/.exec(b.lines[0] || '');
  if (sig) sig[1].split(',').forEach(p => { const n = p.trim().replace(/[^\w$].*$/, ''); if (n) params.add(n); });
  for (const ln of b.lines) {
    for (const m of ln.matchAll(/\(([^()]*)\)\s*=>/g)) {
      m[1].split(',').forEach(p => { const n = p.trim().replace(/[^\w$].*$/, ''); if (n) params.add(n); });
    }
    // A NESTED function's parameters bind inside this block as well: `function refreshAfterJob(fn)`
    // calls `fn()`, and a local helper called `fn` somewhere else is unrelated to it.
    for (const m of ln.matchAll(/(?:async\s+)?function\s*[\w$]*\s*\(([^()]*)\)/g)) {
      m[1].split(',').forEach(p => { const n = p.trim().replace(/[^\w$].*$/, ''); if (n) params.add(n); });
    }
    // A METHOD CALL IS NOT A REFERENCE TO A HELPER. `Date.now()`, `r.text()`, `set.has()` and
    // `map.set()` all collide with local helper names in this file, and without this guard the
    // check reported eighteen offences, none of them real. A leading `.` or `?.` disqualifies.
    for (const m of ln.matchAll(/(^|[^.\w$?])([A-Za-z_$][\w$]*)\s*\(/g)) {
      const name = m[2];
      if (declaresHere.has(name) || params.has(name) || globals.has(name)) continue;
      if (!declaredIn.has(name)) continue;
      const where = [...declaredIn.get(name)].filter(x => x !== b.name);
      if (where.length) offences.push(`${b.name}() calls ${name}(), which is a local of ${where.join(', ')}`);
    }
  }
}

check('no function calls a helper that is local to another function',
      offences.length === 0, offences.join('\n          '));

// --- mutation check: put the shipped bug back and confirm this test would have caught it ----------
// A static assertion pins MY code and carries MY blind spot, so it proves nothing until it is shown
// to fail on the real defect. This reconstructs it in memory rather than on disk.
//
// THE VICTIM IS FOUND, NOT NAMED. This used to hardcode fillHeldBack() and its `const fmt` -- the
// real function from the real bug -- and when that function was later removed from app.js the
// mutation stopped reproducing and this test started failing for a reason that had nothing to do
// with the defect it guards. A self-test that names one function is a self-test with an expiry
// date. So: find ANY top-level function that declares a local helper and calls it, delete that one
// declaration, and confirm the detector above fires on the result. Any of them will do, because
// the shape is what is being tested, not the identity.
function findVictim() {
  for (const b of blocks) {
    if (b.name === '(file head)') continue;
    for (const ln of b.lines) {
      const m = LOCAL.exec(ln);
      if (!m || globals.has(m[1])) continue;
      // It has to be CALLED in the same block, or deleting the declaration reproduces nothing.
      const called = b.lines.some(l => l !== ln && new RegExp(`(^|[^.\\w$?])${m[1]}\\s*\\(`).test(l));
      // And declared nowhere else, so the detector has a local-only name to object to.
      if (called && declaredIn.get(m[1]).size === 1) return { block: b.name, helper: m[1], line: ln };
    }
  }
  return null;
}
const victim = findVictim();
check('a function that declares and calls its own local helper still exists to mutate',
      victim !== null,
      'app.js no longer has the shape this mutation needs — the detector above is now unproven');

if (victim) {
  // MUTATE THE DECOMMENTED SOURCE, not `raw`. `blocks` is built from decomment(raw), so the line
  // held in `victim` is the decommented one — a declaration with a trailing `// comment` does not
  // appear in `raw` in that form at all, and matching against raw silently found nothing.
  const clean = lines.join('\n');
  const bugged = clean.split('\n').filter(ln => ln !== victim.line).join('\n');
  check(`the mutation removed ${victim.block}()'s own \`${victim.helper}\``, bugged !== clean,
        'the declaration line was not found in the decommented source');
  if (bugged !== clean) {
    const bl = bugged.split('\n');
    const bblocks = [];
    let bcur = { name: '(file head)', lines: [] };
    bl.forEach(ln => { const m = TOP.exec(ln); if (m) { bblocks.push(bcur); bcur = { name: m[1] || m[2], lines: [] }; } bcur.lines.push(ln); });
    bblocks.push(bcur);
    const target = bblocks.find(b => b.name === victim.block);
    const declares = target ? target.lines.some(ln => LOCAL.exec(ln) && LOCAL.exec(ln)[1] === victim.helper) : true;
    const calls = target ? target.lines.some(ln => new RegExp(`(^|[^.\\w$?])${victim.helper}\\s*\\(`).test(ln)) : false;
    check(`with the bug put back, ${victim.block}() calls a ${victim.helper} it does not declare`,
          calls && !declares,
          'the mutation no longer reproduces the original defect — this test may now be vacuous');
  }
}

console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
