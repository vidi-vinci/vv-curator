'use strict';

const $ = (s) => document.querySelector(s);
const state = { terms: [], xterms: [], model: '', folder: '', modelFolder: '', meta: '', type: '', group: true, sets: true, tags: [], favOnly: false,
                hasNote: false,
                // Selected root keys for show/hide ([] = all roots). SCOPE, NOT A FILTER: Reset
                // never clears it and a snapshot never carries it — it is which libraries you are
                // working in, not a search you ran inside them. Persisted alongside the filter
                // blob (see saveFilters). Written in exactly two places, restoreRootsSel() and the
                // checkbox handler; a grep is the invariant, so keep it that way.
                // THREE VALUES, NOT TWO: `null` = never chosen, so all of them; an ARRAY = exactly
                // these, INCLUDING the empty array, which means none. It was `[]` for both "all"
                // and "none" until 2026-09-14, which is why unticking the last library silently
                // put every one of them back — the author: "that's just weird."
                rootsSel: null,
                rmin: '', rmax: '', dfrom: '', dto: '', sort: 'date', order: 'desc',
                // The Random sort's deal. Fresh per page load, then only when you ask (the dice) —
                // NOT on every filter change, so narrowing a shuffled grid keeps the images you can
                // already see where they are instead of dealing a new one under you.
                seed: 1 + Math.floor(Math.random() * 999999),
                // 30, not 120: a page is inserted and laid out synchronously, and a big page was long enough to
  // visibly stall the scroll. Smaller pages mean smaller stalls and a set that refreshes faster, at
  // the cost of more round trips — the fetch was never the slow part. topUpIfShort() chains pages
  // until the viewport is actually full, so a small page can't leave the first screen half-empty.
  offset: 0, limit: 30, total: 0, loading: false, done: false,
                items: [], index: -1, current: null,
                roots: [], activeRoot: null, metricRange: [0, 1], theme: {}, labels: [],
                // What /api/config said is installed. NULL until it answers, and that is
                // load-bearing: extActive() reads null as "don't know yet" and leaves every
                // control alone, so the Quality field doesn't flash in and out on every boot.
                extensions: null,
                // Declared true so it FAILS SAFE: boot() overwrites it from config, but until then
                // (and if boot throws) an undefined would be falsy — i.e. confirms silently off.
                // A guard's default has to survive its own initialisation not happening.
                confirmRecycle: true,
                snapshots: [], snapshotName: null };   // saved snapshots (server-side)

// ---- Metric (pyiqa reward) <-> friendly 1..10 scale (fixed linear rescale of metricRange) ----
function metricTo10(raw) {                 // raw reward -> 1..10 for display
  const [lo, hi] = state.metricRange;
  if (raw == null || hi === lo) return null;
  return Math.max(1, Math.min(10, (raw - lo) / (hi - lo) * 9 + 1));
}
function scaleToRaw(v, tol = 0) {           // a 1..10 filter value -> raw reward (''=unset)
  if (v === '' || v == null) return '';     // `tol` shifts the bound by half the badge's rounding
  const n = parseFloat(v); if (isNaN(n)) return '';   // step (badges use 1 decimal) so a filter of
  const [lo, hi] = state.metricRange;                 // "8" includes everything that DISPLAYS as 8.
  const s = Math.max(1, Math.min(10, n)) + tol;
  return (s - 1) / 9 * (hi - lo) + lo;
}
function rawToScale(raw, tol = 0) {         // raw reward -> the 1..10 <option> value ('' = unset)
  if (raw === '' || raw == null) return '';  // scaleToRaw's exact inverse, tol included: `state` holds
  const n = parseFloat(raw);                 // the RAW value but the filter <select> only offers whole
  if (isNaN(n)) return '';                   // 1..10, so restoring the raw number matches no <option>
  const [lo, hi] = state.metricRange;        // and the select silently falls blank while the filter is
  if (hi === lo) return '';                  // still applied. Pass the SAME tol the handler used.
  const s = (n - lo) / (hi - lo) * 9 + 1 - tol;
  return String(Math.max(1, Math.min(10, Math.round(s))));
}
// Human-readable file size from a byte count (— when unknown).
// THE ONLY fmtBytes. A second one was once declared further down and, because function
// declarations hoist, it silently won for EVERY caller — so the duplicate finder reported a 3.5 GB
// cull as "3584.0 MB" and a 500-byte file as "0 KB". Nothing looked broken; the numbers were just
// quietly worse everywhere. If you need a different format, take an argument.
function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n, i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
// How old something is, in words, from an epoch-second timestamp. '' when unknown.
//
// Reads `mtime` -- the same timestamp the Date sort and the date-range filter use -- so a card and
// the sidebar can never disagree about how old a thing is.
//
// Minutes and hours are abbreviated and days, months and years are spelled out, which is not a
// stylistic wobble: the first two are states nobody reads twice, and the last three are the ones
// you compare between cards. The author named those three ("1 day", "23 days", "2 months", "2 years").
//
// It is computed AT RENDER, so a card left on screen overnight keeps yesterday's answer. The grid
// re-renders on every filter, sort, snapshot and page, so this is invisible in practice -- and a
// ticking re-render across 200 cards would cost far more than the accuracy is worth.
function fmtAge(sec) {
  if (sec == null) return '';
  const d = Date.now() / 1000 - sec;
  if (!isFinite(d)) return '';
  if (d < 60) return 'just now';                       // also catches a future date / clock skew
  if (d < 3600) return `${Math.floor(d / 60)} min`;
  if (d < 86400) return `${Math.floor(d / 3600)} hr`;
  const days = Math.floor(d / 86400);
  const plur = (n, u) => `${n} ${u}${n === 1 ? '' : 's'}`;
  if (days < 30) return plur(days, 'day');
  // The switch to years is made on DAYS, not on the month count: months are 30.44 days here, so
  // 365 days floors to ELEVEN of them, and a file exactly a year old announced itself as
  // "11 months". Deciding each unit against days keeps the boundaries where they read.
  if (days < 365) return plur(Math.max(1, Math.floor(days / 30.44)), 'month');
  // 365, not 365.25 — two years to the day should say "2 years", and this is a glance, not an
  // almanac. Math.max keeps the year branch from ever printing "0 years".
  return plur(Math.max(1, Math.floor(days / 365)), 'year');
}
// The last segment of a library-relative folder. A card's small print has ~39 characters, and
// "2026/08/wan/upscaled" spends most of them on ancestry you already know; the detail view carries
// the full path.
function folderLeaf(f) {
  // BOTH separators: `folder` comes from os.path.dirname on Windows, so it is backslashed --
  // splitting on '/' alone would return the whole path as its own last segment.
  const s = String(f || '').replace(/[\\/]+$/, '');
  return (!s || s === '.') ? '' : s.split(/[\\/]/).pop();
}
// ---- styled confirm/alert/prompt dialogs (replace the browser-native popups) ----
let _dlgResolve = null;
function _dlgClose(result) {
  $('#dialog').classList.add('hidden');
  const r = _dlgResolve; _dlgResolve = null;
  if (r) r(result);
}
// `cancel` is a LABEL, and it is assigned on every open rather than only when passed — the button
// is one element reused by every dialog, so a custom word would otherwise stick to the next
// confirm that didn't ask for one. Same reason the default lives here and not in index.html.
function _dlgOpen({ msg, title = null, ok = 'OK', cancel = 'Cancel', danger = false,
                   showCancel = true, input = null, choices = null }) {
  return new Promise(resolve => {
    _dlgResolve = resolve;
    // A heading only when one is asked for, and cleared when it is not — this dialog is reused for
    // every confirm in the app, so a title left standing would reappear over the next unrelated
    // question.
    const t = $('#dlgTitle');
    t.textContent = title || '';
    t.classList.toggle('hidden', !title);
    $('#dlgMsg').textContent = msg;
    const inp = $('#dlgInput');
    if (input !== null) { inp.classList.remove('hidden'); inp.value = input; }
    else inp.classList.add('hidden');
    // CHOICES: a confirm that asks WHICH as well as whether. Built here rather than written into
    // the markup because the options differ per caller and some only exist conditionally.
    // OK is gated on at least one being ticked -- an empty choice would be a destructive action
    // that does nothing, reported as if it had worked.
    const box = $('#dlgChoices');
    box.innerHTML = '';
    box.classList.toggle('hidden', !choices || !choices.length);
    if (choices && choices.length) {
      box.innerHTML = choices.map(c =>
        `<label><input type="checkbox" data-choice="${esc(c.id)}"${c.checked ? ' checked' : ''}>` +
        `<span>${esc(c.label)}` +
        (c.hint ? `<span class="dlg-choice-hint">${esc(c.hint)}</span>` : '') +
        `</span></label>`).join('');
      const sync = () => { $('#dlgOk').disabled = !box.querySelector('input:checked'); };
      box.addEventListener('change', sync);
      sync();
    } else {
      $('#dlgOk').disabled = false;
    }
    const okBtn = $('#dlgOk');
    okBtn.textContent = ok; okBtn.classList.toggle('danger', !!danger);
    const cancelBtn = $('#dlgCancel');
    cancelBtn.textContent = cancel;
    cancelBtn.classList.toggle('hidden', !showCancel);
    $('#dialog').classList.remove('hidden');
    setTimeout(() => (input !== null ? inp : okBtn).focus(), 30);
  });
}
function _dlgSubmit() {   // OK: the ticked choices, or the input value for prompts, else true
  const box = $('#dlgChoices');
  if (!box.classList.contains('hidden')) {
    _dlgClose([...box.querySelectorAll('input:checked')].map(i => i.dataset.choice));
    return;
  }
  const inp = $('#dlgInput');
  _dlgClose(inp.classList.contains('hidden') ? true : inp.value);
}
function uiConfirm(msg, opts = {}) {
  return _dlgOpen({ msg, title: opts.title, ok: opts.ok || 'OK', cancel: opts.cancel,
                    danger: opts.danger })
    .then(v => v === true);
}
// The title is OPTIONAL and most callers still pass none. An ordinary alert is one sentence and a
// heading above it would only label the thing it is already saying -- The author, 2026-09-12, on headings
// generally: "a heading on an ordinary confirm is a label reading 'Confirm' above the thing being
// confirmed." It earns one where the dialog ARRIVES UNINVITED, because then the first job is saying
// what this is about, not what to do about it.
function uiAlert(msg, title = null) { return _dlgOpen({ msg, title, ok: 'OK', showCancel: false }); }
function uiPrompt(msg, def = '') { return _dlgOpen({ msg, input: def == null ? '' : def }); }  // resolves value | null
// Resolves the array of ticked ids, or null if cancelled. `choices` are {id, label, hint, checked}.
function uiChoose(msg, choices, opts = {}) {
  return _dlgOpen({ msg, choices, ok: opts.ok || 'OK', danger: opts.danger })
    .then(v => (Array.isArray(v) ? v : null));
}
// Recycling is non-destructive here (Recycle Bin, or _ToRecycle on a share), so its confirm is a
// speed bump rather than a safety net — and culling means hitting it constantly. Settings ->
// General turns it off.
//
// THE SCOPE RULE, and it is the whole design: a confirm earns its place when it states something
// the SCREEN DOES NOT. Only the two that restate a visible selection are gated — recycling a grid
// selection, and recycling a whole set, where every affected card is already in front of you. The
// three that reach past the screen (Apply to folder, the duplicate culler, Delete copies) keep
// calling uiConfirm directly and always ask, because their dialog is the only place the file count
// and the affected libraries ever appear. Silencing those would not be skipping a prompt, it would
// be acting blind. Do not "finish the job" by converting them.
function confirmRecycle(msg, opts) {
  return state.confirmRecycle ? uiConfirm(msg, opts) : Promise.resolve(true);
}

const selection = new Set();   // selected image ids (as strings)
let lastSelIndex = null;       // anchor for shift-click range select

// Global app name (set from API config)
let _appName = '';
// The RELEASE number (server's APP_VERSION), not the build stamp the brand's tooltip carries. Used
// where a person is being spoken to about a version rather than told which files are on disk.
let _appVersion = '';
let _build = '';        // when this build's code was last changed; shown under the name

// ---- helpers ----
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function esc(s) { return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
// True when focus is in a text field, so global key shortcuts (arrows, etc.) should stand down.
// "Does this control legitimately own the keystroke?" — the gate every bare shortcut (R, X, a–e,
// Delete, the detail view's arrows) is checked against. It used to answer YES for any INPUT at all,
// which silently killed the shortcuts on the control you had just used: click Has notes, or nudge
// the Quality slider, and R and X did nothing until you clicked somewhere else. A checkbox consumes
// Space, a range consumes arrows — neither has any claim on a letter. Only things that actually take
// TEXT do, so the test is the input's type, not its tag.
// SELECT stays: a focused one uses letters for type-ahead and arrows to change value, so it has a
// real claim. That's why it gets the different treatment below (handed back on a pointer choice)
// rather than being excluded here.
const TEXT_INPUT_TYPES = new Set(['text', 'search', 'url', 'email', 'tel', 'password', 'number',
                                  'date', 'datetime-local', 'month', 'week', 'time']);
function isTypingTarget(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  if (el.tagName === 'INPUT') return TEXT_INPUT_TYPES.has((el.type || 'text').toLowerCase());
  return false;
}

// ---- debug trace (Settings → General → "Record a debug trace") ------------------------------
// Off by default. Records what the app DID, with timings, so a "why does this feel slow" report can
// be read rather than guessed at — three theories died to code-reading alone before this existed.
//
// Two things make it worth having rather than a console.log:
//   · the SERVER's own time comes back in X-Elapsed-Ms, so "the query was slow" and "the browser was
//     busy" are separable — a round trip measured here alone bundles them together;
//   · it records supersession. An abandoned search looks exactly like a fast one from the outside,
//     and mistaking one for the other is precisely how a trace misleads.
const Trace = (() => {
  const KEY = 'vv_trace';
  const MAX = 600;                       // a few minutes of real use; oldest fall off the front
  let on = localStorage.getItem(KEY) === '1';
  let rows = [], t0 = performance.now();
  const at = () => ((performance.now() - t0) / 1000).toFixed(2).padStart(7);
  return {
    get on() { return on; },
    set(v) {
      on = !!v;
      localStorage.setItem(KEY, on ? '1' : '0');
      if (on) { rows = []; t0 = performance.now(); this.add('trace', 'recording started'); }
    },
    add(kind, detail) {
      if (!on) return;
      rows.push(`${at()}s  ${kind.padEnd(12)} ${detail}`);
      if (rows.length > MAX) rows.splice(0, rows.length - MAX);
    },
    // Returns a closure that records how long something took, so callers read as one line.
    start(kind, detail) {
      if (!on) return () => {};
      const t = performance.now();
      return (extra) => this.add(kind, `${detail}${extra ? ' · ' + extra : ''} · ${Math.round(performance.now() - t)}ms`);
    },
    clear() { rows = []; t0 = performance.now(); this.add('trace', 'cleared'); },
    text() {
      return rows.length ? rows.join('\n') : '(nothing recorded — switch the trace on, then use the app)';
    },
  };
})();

// Every API read goes through here, so this is the one place that can time them all. The URL is
// trimmed to its path plus the parameters that actually change the answer — a full search query
// string is 300 characters of mostly-empty filters and would bury the trace it is meant to explain.
function traceUrl(url) {
  const [path, qs] = String(url).split('?');
  if (!qs) return path;
  const keep = ['limit', 'offset', 'sort', 'order', 'folder', 'q', 'sets', 'group', 'id', 'keep'];
  const p = new URLSearchParams(qs);
  const bits = keep.filter(k => p.get(k)).map(k => `${k}=${p.get(k)}`);
  return path + (bits.length ? '?' + bits.join('&') : '');
}
async function getJSON(url) {
  if (!Trace.on) { const r = await fetch(url); return r.json(); }
  const t = performance.now();
  const r = await fetch(url);
  const server = r.headers.get('X-Elapsed-Ms');
  const j = await r.json();
  const trip = Math.round(performance.now() - t);
  // server vs round trip: the gap is queueing, transfer and JSON parsing, i.e. everything that is
  // NOT the query. On a slow view that difference is the whole diagnosis.
  // X-Phases breaks the server time into named parts. Without it a slow request can only be
  // reported as slow — which is exactly how a regression hunt ends up guessing.
  const phases = r.headers.get('X-Phases');
  Trace.add('GET', `${traceUrl(url)} · ${trip}ms` + (server != null ? ` (server ${server}ms)` : '')
    + (phases ? ` [${phases}]` : ''));
  return j;
}
async function postJSON(url, body) {
  const t = performance.now();
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  Trace.add('POST', `${traceUrl(url)} · ${Math.round(performance.now() - t)}ms`);
  return j;
}

// ---- scroll cost: the metric for "how fast can I get down a folder" ---------------------------
// Speed alone does not describe the complaint. What is actually felt is BLOCKED time — the page
// stops dead while it catches up with the last scroll, and the next push of the wheel goes nowhere.
// There are two of those, they have different causes, so they are counted apart:
//   · blocked MID-SCROLL — the gesture is still live but frames are not being produced;
//   · CATCH-UP — you have stopped and the page is STILL working: finishing a page insert, laying
//     out, decoding the thumbnails you just flew past. Invisible to any within-gesture measure,
//     because by then the gesture is over. This is the half that reads as "it won't let me scroll".
//
// A frame gap is the whole instrument: requestAnimationFrame cannot run while the main thread is
// busy, so the gap between frames IS the block. That also makes the two suspected causes separable
// without any extra machinery — a block spent waiting on the NETWORK still produces frames, a block
// spent decoding or laying out does not. The longtask observer below names the second kind.
//
// The sampler exists only between the first scroll event and the end of the catch-up window, so it
// costs nothing at rest — and nothing at all with the trace off, which is the normal state.
const ScrollMetric = (() => {
  const GESTURE_END_MS = 150;    // stillness that ends a gesture
  const BLOCK_MS = 50;           // a gap longer than this is a stall, not a frame
  const QUIET_MS = 20;           // a frame this short means the main thread is free again...
  const QUIET_RUN = 3;           // ...three in a row, so one lucky frame can't call it settled
  const MAX_CATCHUP_MS = 4000;   // hard stop: never leave the sampler running behind a wedged page
  const MIN_DIST_PX = 100;       // a nudge has nothing to say about traversal; don't record one
  let live = false, phase = '', raf = 0;
  let t0 = 0, tLastScroll = 0, yPrev = 0, dist = 0, pages = 0;
  let tFrame = 0, blocked = 0, worst = 0, catchup = 0, quiet = 0, tCatch = 0;

  function stop() { live = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

  function emit() {
    stop();
    const dur = Math.max(1, tLastScroll - t0);
    if (dist < MIN_DIST_PX) return;
    const n = (v) => Math.round(v).toLocaleString();
    Trace.add('scroll',
      `${n(dist)}px in ${(dur / 1000).toFixed(1)}s · ${n(dist / (dur / 1000))}px/s · ` +
      `blocked ${(blocked / 1000).toFixed(1)}s (${Math.round((blocked / dur) * 100)}%) · ` +
      `worst ${n(worst)}ms · +${n(catchup)}ms catch-up · ` +
      `${pages} page${pages === 1 ? '' : 's'} · ${n($('#grid').childElementCount)} in DOM`);
  }

  function frame(now) {
    if (!live) return;
    if (!Trace.on) return stop();          // switched off mid-gesture: drop it, don't half-report
    const gap = now - tFrame;
    tFrame = now;
    // WHICH KIND OF BLOCK, and it is the only judgement this module makes. Input events dispatch on
    // the main thread exactly like this callback, so a block the user scrolled THROUGH releases its
    // queued scroll events at the END of the block, immediately before the frame that measures it —
    // whereas a block that began after the user stopped has its last scroll event a full gesture-gap
    // in the past. So "was there a scroll just now?" separates them, and a block straddling the
    // moment you stop lands in the right bucket instead of whichever phase the flip happened to hit.
    const stillScrolling = now - tLastScroll <= GESTURE_END_MS;
    if (gap > BLOCK_MS) {
      if (stillScrolling) { blocked += gap; if (gap > worst) worst = gap; }
      else catchup += gap;
    }
    if (phase === 'scroll' && !stillScrolling) { phase = 'catch'; tCatch = now; quiet = 0; }
    if (phase === 'catch') {
      quiet = gap < QUIET_MS ? quiet + 1 : 0;
      if (quiet >= QUIET_RUN || now - tCatch > MAX_CATCHUP_MS) return emit();
    }
    raf = requestAnimationFrame(frame);
  }

  return {
    // Called from the one passive scroll listener the app already has — deliberately not a second
    // listener, so this whole module comes out again by deleting it and one line.
    onScroll() {
      const y = window.scrollY;
      if (!live) {
        if (!Trace.on) return;
        live = true; phase = 'scroll';
        t0 = tFrame = tLastScroll = performance.now();
        dist = blocked = worst = catchup = pages = 0;
        yPrev = y;
        raf = requestAnimationFrame(frame);
        return;
      }
      dist += Math.abs(y - yPrev);
      yPrev = y;
      tLastScroll = performance.now();
      if (phase === 'catch') phase = 'scroll';   // moved again before it settled: same gesture
    },
    // A page landing mid-scroll is the likeliest single cause of a block, so the count belongs on
    // the same line as the block it probably explains.
    notePage() { if (live) pages++; },
  };
})();

// Names the blocks the scroll line counts. Only the big ones: the API reports everything over 50ms
// and a heavy scroll would otherwise bury the trace in them.
// try/catch as well as the support test: this is a diagnostic, and a diagnostic that can throw
// during module evaluation would take the whole app down with it.
try {
  if (window.PerformanceObserver && (PerformanceObserver.supportedEntryTypes || []).includes('longtask')) {
    new PerformanceObserver(list => {
      if (!Trace.on) return;
      for (const e of list.getEntries()) {
        if (e.duration >= 100) Trace.add('longtask', `${Math.round(e.duration)}ms of main thread — nothing could paint`);
      }
    }).observe({ type: 'longtask', buffered: false });
  }
} catch (e) { /* no long-task reporting here; the scroll line still works */ }

// ---- "press Enter to keep this": the ⏎ that follows the text -----------------------------------
// The box searches as you type, so Enter looks like it does nothing. What it does -- turn the text
// into a chip you can add another beside -- was the part nobody discovered, and the placeholder
// alone did not carry it.
//
// IT TRACKS THE END OF THE TEXT, not the edge of the field, so it reads as belonging to what you
// just typed. Nothing in CSS can see where the text inside an <input> ends, so the width is
// MEASURED: a mirror span carrying the input's own computed font, rather than canvas measureText,
// because the font shorthand has to be rebuilt by hand for canvas and gets letter-spacing wrong.
// One span for the whole app -- it is only ever read synchronously.
//
// CLAMPED short of the ✕. Once the text is long enough to reach it the mark parks there instead of
// sliding under the button, which is the case the author asked for by name.
let _measureSpan = null;
function _textWidth(input) {
  if (!_measureSpan) {
    _measureSpan = document.createElement('span');
    // Off-screen rather than hidden: display:none has no box and measures 0.
    _measureSpan.style.cssText = 'position:absolute;left:-9999px;top:-9999px;white-space:pre;';
    document.body.appendChild(_measureSpan);
  }
  const cs = getComputedStyle(input);
  for (const p of ['fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize',
                   'fontFamily', 'letterSpacing', 'textTransform'])
    _measureSpan.style[p] = cs[p];
  _measureSpan.textContent = input.value;
  return _measureSpan.getBoundingClientRect().width;
}
// `mark` is placed against `.searchbox`, which is position:relative.
function syncEnterMark(input, mark, clearBtn) {
  if (!input || !mark) return;
  const show = !!input.value.trim();
  mark.classList.toggle('hidden', !show);
  if (!show) return;
  const box = input.parentElement.getBoundingClientRect();
  const inp = input.getBoundingClientRect();
  const cs = getComputedStyle(input);
  const GAP = 6;
  // Where the text ends, in the box's coordinates. Capped at the input's own right edge first:
  // once the text overflows, the input scrolls and its visible end IS that edge.
  const textEnd = Math.min(
    inp.left - box.left + parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth) + _textWidth(input),
    inp.right - box.left);
  // The ✕ is only in the flow when it is visible; when it is not, the field's own padding is the
  // stop instead — otherwise the mark would sit hard against the border.
  const rightStop = (clearBtn && !clearBtn.classList.contains('hidden'))
    ? clearBtn.getBoundingClientRect().left - box.left
    : box.width - parseFloat(getComputedStyle(input.parentElement).paddingRight);
  mark.style.left = Math.max(0, Math.min(textEnd + GAP, rightStop - mark.offsetWidth - GAP)) + 'px';
}
function syncEnterMarks() {
  syncEnterMark($('#q'), $('#qEnter'), $('#qclear'));
  syncEnterMark($('#xq'), $('#xqEnter'), $('#xqclear'));
}

// ---- search terms (chips) ----
function queryString() {
  const pending = $('#q').value.trim();
  return [...state.terms, pending].filter(Boolean).join(' ');
}
function renderChips() {
  $('#chips').innerHTML = state.terms.map((t, i) =>
    `<span class="chip"><span title="${esc(t)}">${esc(t)}</span><button data-i="${i}" title="Remove"></button></span>`).join('');
  $('#qclear').classList.toggle('hidden', state.terms.length === 0 && !$('#q').value);
  syncEnterMark($('#q'), $('#qEnter'), $('#qclear'));   // after the ✕: the clamp reads its position
}
function addTerm(t) {
  t = t.trim();
  if (!t) return;
  state.terms.push(t);
  $('#q').value = '';
  renderChips();
  search(true);
}
function removeTerm(i) {
  state.terms.splice(i, 1);
  renderChips();
  search(true);
}
function clearTerms() {
  state.terms = [];
  $('#q').value = '';
  renderChips();
  search(true);
}
// ---- exclude terms (chips, inverse of search) ----
function excludeString() {
  const pending = $('#xq').value.trim();
  return [...state.xterms, pending].filter(Boolean).join(' ');
}
function renderXChips() {
  $('#xchips').innerHTML = state.xterms.map((t, i) =>
    `<span class="chip"><span title="${esc(t)}">${esc(t)}</span><button data-i="${i}" title="Remove"></button></span>`).join('');
  $('#xqclear').classList.toggle('hidden', state.xterms.length === 0 && !$('#xq').value);
  syncEnterMark($('#xq'), $('#xqEnter'), $('#xqclear'));
}
function addXTerm(t) {
  t = t.trim();
  if (!t) return;
  state.xterms.push(t);
  $('#xq').value = '';
  renderXChips();
  search(true);
}
function removeXTerm(i) {
  state.xterms.splice(i, 1);
  renderXChips();
  search(true);
}
function clearXTerms() {
  state.xterms = [];
  $('#xq').value = '';
  renderXChips();
  search(true);
}
// ---- filter persistence (localStorage) ----
// THE FILTER SET. What Reset clears, what a snapshot saves, and what the drift dot compares — those
// are one list by design, and this is it. One thing is deliberately NOT here, and the instinct to
// add it is the thing to resist. (`peek`, the Hidden mark's "show them anyway" flag, was the other
// until the mark was removed on 2026-09-08.)
//
//   `roots` — the library show/hide selection. It escapes because it is SCOPE: which libraries you
//             are working in, not a search you ran inside them. So Reset must not clear it (the author,
//             2026-08-20: "I often just work with the local [laptop] library... but I want to clear
//             filters") and a snapshot must not carry it. The server has always agreed — the roots
//             clause sits above _filters()'s global-exclusion boundary, so it has never set the
//             user-filter flag.
//
// `roots` is still PERSISTED, just not from here: saveFilters() adds it to the stored blob on the
// way out and restoreRootsSel() reads it back. See both.
function serializeFilters() {
  return {
    terms: state.terms, xterms: state.xterms, model: state.model, folder: state.folder, modelFolder: state.modelFolder,
    meta: state.meta, type: state.type, group: state.group, sets: state.sets, tags: state.tags, favOnly: state.favOnly,
    hasNote: state.hasNote,
    rmin: state.rmin, rmax: state.rmax,
    dfrom: state.dfrom, dto: state.dto,
    sort: state.sort, order: state.order,
  };
}
// The <select id="sort"> value that corresponds to the current sort/order.
function sortSelectValue() {
  return (state.sort === 'date' && state.order === 'asc') ? 'date-asc' : state.sort;
}
// Read from the <select> rather than a hardcoded list, so retiring a sort option means deleting
// one <option> and nothing else. A value with no matching option would otherwise leave Sort blank
// while `state` kept the bad value — which is what the old LLM-score sorts did.
function sortOptionExists(v) {
  const sel = $('#sort');
  if (!sel || !sel.options.length) return true;   // pre-DOM: don't rewrite what we can't check
  // Disabled options are the group SEPARATORS, and an <option> with no value attribute reports its
  // text as its value — so without this they would answer to a stored sort of "──────────────".
  // Unreachable in practice; excluded because a separator is not a sort, not because it bit.
  return [...sel.options].some(o => !o.disabled && o.value === v);
}
// Read-only apart from the DOM lookup above: a stored filter object -> the exact shape `state`
// would hold for it. Extracted from applyFilters so the snapshot drift check can normalise a STORED
// snapshot the same way applying it would — otherwise a snapshot with defaults filled in or a
// retired sort reads as "modified" the instant it loads. applyFilters is the only other caller;
// keep the two in step by construction rather than by copying the rules.
function normalizeFilters(f) {
  f = f || {};
  const o = {};
  o.terms = Array.isArray(f.terms) ? f.terms.slice() : [];
  o.xterms = Array.isArray(f.xterms) ? f.xterms.slice() : [];
  o.model = f.model || ''; o.folder = f.folder || ''; o.modelFolder = f.modelFolder || ''; o.meta = f.meta || '';
  o.type = f.type || '';
  o.group = (f.group === undefined ? true : !!f.group);   // merge still+video pairs, default on
  o.sets = (f.sets === undefined ? true : !!f.sets);      // merge image sets (MAIN/DET/REFINE), default on
  o.tags = Array.isArray(f.tags) ? f.tags.slice() : [];
  o.favOnly = !!f.favOnly;
  o.hasNote = !!f.hasNote;
  // NO `o.roots` — and its absence is load-bearing rather than an omission. filtersFingerprint()
  // compares the whole KEY SET, so if this built a key that serializeFilters() no longer emits,
  // every stored snapshot would normalise to `roots: ['A']` against a live `roots: []` and read as
  // permanently dirty: the amber drift dot stuck on, and restoreLoadedSnapshot() dropping the
  // snapshot name on every boot. Silent, and no test covers it. The pruning that used to live here
  // moved to pruneRootKeys(), which the two restore sites call.
  o.rmin = f.rmin || ''; o.rmax = f.rmax || '';
  o.dfrom = f.dfrom || ''; o.dto = f.dto || '';
  o.sort = f.sort || 'date'; o.order = f.order || 'desc';
  // A sort whose <option> no longer exists falls back HERE rather than in applyFilters, so the
  // normalised shape and the applied shape agree on it — otherwise a snapshot holding a retired
  // sort would read as "modified" the moment it loaded.
  if (!sortOptionExists(o.sort === 'date' && o.order === 'asc' ? 'date-asc' : o.sort)) {
    o.sort = 'date'; o.order = 'desc';
  }
  return o;
}
// Apply a saved filter object (or defaults when `f` is null) to state + the DOM controls.
// model/folder options are re-affirmed by loadFacets; tags/favOnly render via loadTags.
function applyFilters(f) {
  const n = normalizeFilters(f);
  state.terms = n.terms; state.xterms = n.xterms;
  state.model = n.model; state.folder = n.folder; state.modelFolder = n.modelFolder; state.meta = n.meta;
  state.type = n.type; state.group = n.group; state.sets = n.sets;
  state.tags = n.tags;
  state.favOnly = n.favOnly;
  state.hasNote = n.hasNote;
  state.rmin = n.rmin; state.rmax = n.rmax;
  state.dfrom = n.dfrom; state.dto = n.dto;
  state.sort = n.sort; state.order = n.order;
  $('#q').value = ''; renderChips();
  $('#xq').value = ''; renderXChips();
  if ($('#tagSearch')) $('#tagSearch').value = '';
  renderFacetDD('model'); renderFacetDD('folder');
  $('#sort').value = sortSelectValue();   // normalizeFilters already guaranteed the option exists
  renderMediaType();
  if ($('#hasNote')) $('#hasNote').checked = state.hasNote;
  if ($('#mfolder')) $('#mfolder').value = state.modelFolder;   // options refill on loadFacets
  renderSetsMode();
  // state holds the RAW metric value; the <option>s are the 1..10 scale. Same tol as the handlers.
  $('#rmin').value = rawToScale(state.rmin, -0.05);
  $('#rmax').value = rawToScale(state.rmax, +0.05);
  $('#dfrom').value = state.dfrom; $('#dto').value = state.dto;
  renderLabelList();     // reflect the active label filter (lives in state.tags)
  // NOTHING ABOUT LIBRARIES HAPPENS HERE, and that is the test this function has to keep passing.
  // Two of its four callers are Reset and snapshot-restore, and neither may move the library
  // selection; the two that legitimately restore it (boot, and a library being added or removed)
  // call restoreRootsSel() themselves, which owns the strip's paint as well as the value.
}
// File type is a segmented icon bar; paint the selected segment from state so a click, a reload
// and Reset can't disagree. Mirrors applyCardSize's toggle idiom. '' (All) is a real value here,
// so compare against `|| ''` rather than truthiness.
function renderMediaType() {
  document.querySelectorAll('#mediaType button').forEach(b => {
    const on = (b.dataset.type || '') === state.type;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    // aria-label is the base, not title, because title is what this rewrites — reading the value it
    // is about to overwrite would compound the suffix on every repaint.
    paintSegConflict(b, 'type', b.dataset.type || '', b.getAttribute('aria-label'));
  });
  // A bar sitting at its default is filtering nothing, so it must not be the loudest thing in the
  // rail — the accent fill was saying "strong filter on" for "All". Set from the SAME state that
  // paints .active, so the two can never disagree; deliberately not inferred from :first-child,
  // which is true today and would break silently if a segment were ever reordered.
  $('#mediaType').classList.toggle('at-default', !state.type);
}
// The "Sets" dropdown maps to the two independent collapse flags: state.group (video sets =
// still+video pairs) and state.sets (image sets). One control, four combinations.
function setsModeValue() {
  if (state.group && state.sets) return 'all';
  if (state.group) return 'video';
  if (state.sets) return 'images';
  return 'none';
}
// TWO CONTROLS THAT BOTH NAME A MEDIA KIND, so two of their combinations are empty BY CONSTRUCTION
// rather than merely narrow. Reported 2026-08-15 after months of it being possible.
//
// Sets 'images' and 'video' are sets-ONLY modes: exactly one collapse flag on means the grid shows
// only multi-member groups of that kind and hides every lone file. So:
//   File type Video  + Sets Image sets → an image set contains no video, so nothing survives.
//   File type Images + Sets Video sets → a video set is DEFINED by having a video member; filter the
//                                        videos out and the group stops being one.
// Neither is a narrow result you might widen — both are structurally empty, always.
//
// Returns the OTHER control's reset if this segment would produce that, or '' if the pair is fine.
// One function, both bars, so the rule cannot be stated twice and drift.
function segConflict(bar, value) {
  const sets = setsModeValue();
  // Songs join the same rule from the far side: a set is a group of stills and a pair is a still
  // plus a video, so no song is ever in either. Both sets-only modes are empty against Songs.
  if (bar === 'type') {
    if (value === 'video' && sets === 'images') return 'Sets';
    if (value === 'image' && sets === 'video') return 'Sets';
    if (value === 'audio' && (sets === 'images' || sets === 'video')) return 'Sets';
  } else {
    if (value === 'images' && state.type === 'video') return 'File type';
    if (value === 'video' && state.type === 'image') return 'File type';
    if ((value === 'images' || value === 'video') && state.type === 'audio') return 'File type';
  }
  return '';
}
// Dim it, TELL you what will happen, and still let you press it — the author's call over disabling it.
// Disabling would have forced an order of operations (clear File type first, then pick the set kind)
// for a combination the app can resolve on its own.
// Deliberately NOT the app's `disabled` look or its :disabled state: "greyed and unclickable" has to
// keep meaning that everywhere else, so this is its own class, keeps the pointer cursor, and stays
// reachable by keyboard. The tooltip is what makes it honest rather than a surprise.
function paintSegConflict(btn, bar, value, baseTitle) {
  const other = segConflict(bar, value);
  btn.classList.toggle('conflict', !!other);
  btn.title = other ? `${baseTitle} — also sets ${other} to All` : baseTitle;
}
function applySetsMode(v) {
  state.group = (v === 'all' || v === 'video');
  state.sets  = (v === 'all' || v === 'images');
}
// Paint the Sets segmented bar from the two flags, never from the last click — the flags are the
// truth and setsModeValue() is the only thing that knows how they collapse into four modes.
function renderSetsMode() {
  const v = setsModeValue();
  document.querySelectorAll('#setsMode button').forEach(b => {
    const on = b.dataset.mode === v;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    paintSegConflict(b, 'sets', b.dataset.mode, b.getAttribute('aria-label'));
  });
  $('#setsMode').classList.toggle('at-default', v === 'all');   // see renderMediaType
}
// Merged DB: one global blob for the rail's saved state (it was per-root back when a root was a
// mode you switched into rather than one library among several).
const FILTERS_KEY = 'cv:filters';
// THE SPREAD IS THE POINT — don't tidy it into serializeFilters(). The blob carries the filter set
// AND the library scope, but only the filter set may reach a snapshot, and serializeFilters() is
// what snapshots are built from. Two payloads, one store: this is the seam between them.
function saveFilters() {
  try {
    // `rootsPicked` marks a blob written since the empty array started meaning "none". Without it
    // a stored `[]` is from the old scheme, where it meant "all" — and reading one of those the new
    // way would open the app with every library switched off and nothing to explain it.
    localStorage.setItem(FILTERS_KEY,
                         JSON.stringify({ ...serializeFilters(), roots: state.rootsSel,
                                          rootsPicked: true }));
  } catch (e) {}
}
function loadSavedFilters(_key) {
  try { return JSON.parse(localStorage.getItem(FILTERS_KEY)); } catch (e) { return null; }
}
// Roots show/hide: the `roots=` query value. '' when every library is in scope — the server reads
// an empty value as "no filter" — and a comma list of keys for anything narrower.
//
// NONE SELECTED SENDS A KEY THAT CANNOT EXIST. Every consumer of `roots=` turns the value into an
// `IN (...)`, so a key no library has yields zero rows in all of them at once — the grid, the facet
// counts and the totals agree without a single server change, and without a second "show nothing"
// concept for three call sites to interpret differently. A real key is 16 lowercase hex characters
// (see _root_key in server.py), so this can never collide with one; test_root_scope.js asserts that.
const ROOTS_NONE = '__none__';
function allRootKeys() { return (state.roots || []).map(r => r.key); }
// AN UNREACHABLE LIBRARY IS OUT OF SCOPE, exactly as an unticked one is. The author, 2026-09-14, after
// testing a share dropped mid-session: "I'm inclined to just hide cards from inactive Libraries.
// The experience is SO broken otherwise. I suggest we act as if the library is unchecked."
//
// What he met: a grid of cards whose thumbnails were mostly broken (a thumbnail is made on first
// view, so anything never opened while the drive was there has no cached copy and nothing to make
// one from), and a detail view that quietly showed the cached thumbnail in place of the file —
// announced by a banner, which he read, and still "it wasn't clear the images were the cached
// images". Half a library that half works is worse than a library that is plainly not here.
//
// EXCLUDED WHILE UNREACHABLE, NOT UNTICKED. Writing it into state.rootsSel would leave it hidden
// after the drive came back, and the user would go looking for a library they never switched off.
// Their own ticks are untouched; this is a second, temporary filter on top.
function rootsParam() {
  const all = allRootKeys(), sel = state.rootsSel;
  const base = sel === null ? all : sel.filter(k => all.includes(k));
  const live = base.filter(k => !isRootOffline(k));
  if (!live.length) return ROOTS_NONE;               // nothing ticked, or nothing ticked is here
  // '' means "all of them" to the server, so it is only honest when nothing was dropped — with an
  // offline library present the list has to be spelled out.
  return live.length >= all.length ? '' : live.join(',');
}
// Drop keys for libraries that no longer exist. rootsParam() already ignores a dead key, so an
// unpruned one made the grid and the checkboxes disagree: the grid reverted to showing every
// library while renderLibList still painted the stale selection.
//
// A NON-ARRAY STAYS null rather than becoming [], because those two now mean opposite things: null
// is "never chosen, show everything" and [] is "chosen nothing, show nothing". Collapsing the first
// into the second is how a missing stored value would blank the grid.
function pruneRootKeys(a) {
  if (!Array.isArray(a)) return null;
  return a.filter(k => allRootKeys().includes(k));
}
// Restore the library scope from a stored blob — the ONLY way state.rootsSel is set outside the
// checkbox handler, and deliberately separate from applyFilters(): the selection is scope, not a
// filter, so Reset and restoring a snapshot must leave it alone.
//
// CALL THIS BEFORE applyFilters(), not after. It carries the paint as well as the value for the
// reason the old comment inside applyFilters gave: the strip's other route to a repaint is
// loadFacets() → renderLibList(), i.e. after a round trip, so a late paint shows a grey "3/3" and
// then flips. Keeping the two together makes that unrepresentable.
function restoreRootsSel(f) {
  // THE ONE PIECE OF COMPATIBILITY IN HERE. Blobs written before 2026-09-14 stored `[]` for "all",
  // and reading one of those under the new meaning would start the app with every library off. A
  // blob from the new scheme carries `rootsPicked`, so only those are allowed to mean "none".
  const raw = f && f.roots;
  state.rootsSel = (Array.isArray(raw) && !raw.length && !(f && f.rootsPicked))
    ? null : pruneRootKeys(raw);
  renderLibTrigger();
}
function curRootKey() { return (state.current && state.current.root_key) || ''; }
// Reset rewrites the ENTIRE sidebar, not just the grid, so it dims the whole window like the other
// whole-sidebar operations (boot, a library switch, restoring a snapshot) rather than scrimming the
// results pane alone — which left the sidebar sharp and interactive beside a dimmed grid, a
// half-done state that read as a glitch. Same shape as applySnapshot() on purpose.
// It also needs loadFacets(): Reset used to skip it, so the library checkboxes only caught up on
// whatever search() happened to refresh next.
async function resetFilters() {
  // Busy.during: the release is in its finally, so nothing below can strand the rail. `rail` covers
  // the results pane too — the two halves are one state and the tier rule says so in one place now.
  return Busy.during('rail', async () => {
    clearLoadedSnapshot();  // Reset means "start clean"; a snapshot name over zero filters would
    applyFilters(null);     // leave the update button poised to blank that snapshot.
    // In PARALLEL, not one after another: all three are behind the same dim, they write disjoint
    // parts of the page (facets → the two dropdowns + model type + the Libraries list; tags → the
    // tag/label lists; search → the grid and the count), and the server handles a request per
    // thread. Sequentially this was three round trips before a single card could appear — on a
    // network share, most of the wait. allSettled so one failure can't leave the others as
    // unhandled rejections part-way through rewriting the rail.
    await refreshView();
  });
}

// ---- snapshots (a named capture of the filter state; stored server-side in config) --------------
// The whole feature rides serializeFilters()/applyFilters — a snapshot IS that object plus a name.
// "Snapshot" is Native Instruments' word (Reaktor/Kontakt): the saved state of every control on a
// panel. Note Lightroom uses it for a single photo's edit state — a different scale, chosen against.
const SNAPSHOT_NAME_KEY = 'cv:snapshotName';
// A comparable form of a filter set. TWO orderings have to be neutralised or the drift dot lies:
// the term/tag arrays (re-checking two tags would otherwise read as a change), and the KEY order —
// the server rebuilds the object in its own whitelist order, so a fingerprint taken from a
// server-returned snapshot would never match one taken from serializeFilters().
// `roots` is absent because it is no longer a filter: changing which libraries are shown is not
// drift from a snapshot, since the snapshot never claimed a library selection in the first place.
function filtersFingerprint(f) {
  const arr = new Set(['terms', 'xterms', 'tags']);
  return JSON.stringify(Object.keys(f || {}).sort().map(k =>
    [k, arr.has(k) ? (f[k] || []).slice().sort() : f[k]]));
}
function snapshotByName(n) {
  const k = String(n == null ? '' : n).toLowerCase();
  return (state.snapshots || []).find(v => v.name.toLowerCase() === k) || null;
}
// Do the live filters still match the loaded snapshot? Both sides go through normalizeFilters, so
// defaults, a pruned library key and a retired sort can't read as a change.
function snapshotIsDirty() {
  const s = snapshotByName(state.snapshotName);
  if (!s) return false;
  return filtersFingerprint(normalizeFilters(serializeFilters())) !== filtersFingerprint(normalizeFilters(s.filters));
}
function setLoadedSnapshot(name) {
  state.snapshotName = name;
  try {
    if (name) localStorage.setItem(SNAPSHOT_NAME_KEY, name);
    else localStorage.removeItem(SNAPSHOT_NAME_KEY);
  } catch (e) {}
  renderSnapshotRow();
  // The MENU too, not just the row: the picker marks the loaded snapshot, so the two would
  // otherwise drift — clear a snapshot and the next open still shows the old one highlighted.
  // Hooked to the one function that knows the value changed, rather than to each caller.
  renderSnapshotMenu();
}
function clearLoadedSnapshot() { setLoadedSnapshot(null); }
// SHOW OR HIDE THE WHOLE SNAPSHOTS SECTION. The author, 2026-09-08: he barely uses it, and reckons some
// people will not want the row at all. The feature stays and the saved snapshots stay; only the
// rail stops carrying it.
//
// THE RULE GOES WITH THE ROW. .rail-sep is what makes Snapshots read as the parent of the filters
// below it, so a boundary left behind would mark the edge of a section that is not there.
//
// AND A LOADED SNAPSHOT IS DETACHED ON THE WAY OUT, filters untouched — the same thing None does.
// Leaving one attached would be state with no UI: a name governing nothing you can see, no way to
// tell it is set and no way to clear it. That is the fault the hide-large removal was about.
function applyShowSnapshots(on) {
  const show = on !== false;
  const row = $('.snap-row'), sep = $('#snapSep');
  if (row) row.classList.toggle('hidden', !show);
  if (sep) sep.classList.toggle('hidden', !show);
  if (!show && state.snapshotName) clearLoadedSnapshot();
}
// THE RESTART RULE, and the reason the drift dot is usable at all. The live filters persist
// independently (cv:filters), so restoring the name unconditionally meant any tweak made before a
// restart came back as permanent drift — the dot was lit from launch, forever, which is why the
// first version of it was deleted. Restoring the name ONLY when the filters still match bounds the
// dot to a single session: it can never be lit at startup.
function restoreLoadedSnapshot() {
  let n = null;
  try { n = localStorage.getItem(SNAPSHOT_NAME_KEY); } catch (e) {}
  const s = n && snapshotByName(n);          // gone if it was deleted elsewhere, or never saved
  state.snapshotName = s ? s.name : null;    // set directly: snapshotIsDirty() needs it in place
  setLoadedSnapshot(s && !snapshotIsDirty() ? s.name : null);
}
function renderSnapshotRow() {
  const nm = $('#snapName'); if (!nm) return;
  const name = state.snapshotName;
  // The field says what is LOADED, not what the field is called — the row's cap label carries the
  // name now. "None selected" rather than blank: an empty field reads as broken, and the mode
  // signal (idle vs curating) still lands, just in the value instead of the name.
  nm.textContent = name || 'None selected';
  nm.title = name || 'No snapshot restored';
  $('#snapTrigger').classList.toggle('active', !!name);
  $('#snapDirty').classList.toggle('hidden', !(name && snapshotIsDirty()));
  $('#snapUpdate').classList.toggle('hidden', !name);   // hidden, not greyed — see index.html
  // The ⋯ goes with it, for the same reason and now by the same rule. Its actions are Rename and
  // Delete, both of which need a loaded snapshot, so with nothing restored the button opened a menu
  // of two dead items — a control that can never do anything in its resting state. It had been
  // disabling the ITEMS instead, which is the right pattern for a menu that is sometimes useful and
  // the wrong one for a menu that is only ever useful in one mode.
  // #snapMore is gone: Rename and Delete live on every ROW of the picker now, where they are
  // reachable without restoring the snapshot first — which was the whole complaint.
}
// The dropdown is a pure picker — saved snapshots and nothing else. New/update have their own
// buttons on the row; the ⋯ keeps only the rare actions.
function renderSnapshotMenu() {
  if (!$('#snapMenu')) return;
  // EACH ROW CARRIES ITS OWN ⋯, the same anatomy as a library row one section above. The author's call,
  // and the reasoning is the good part: a ⋯ is a DISCLOSURE, not an action, so mis-clicking one
  // costs nothing — which is the safety a confirm was being considered for, without the modal.
  // The destructive item then sits one deliberate step in.
  // It also fixes Rename, which had Delete's exact problem: both lived in the trigger's ⋯, which
  // only appears once a snapshot is RESTORED — so renaming one meant applying its filters first.
  // The loaded one is marked, because a picker that cannot say which is current is a list, not a
  // picker. The author: "the active snapshot should be highlighted in someway in the dropdown."
  const rows = (state.snapshots || []).map(v =>
    `<div class="snap-item${v.name === state.snapshotName ? ' active' : ''}">` +
    `<button data-snap="${esc(v.name)}" title="${esc(v.name)}">${esc(v.name)}</button>` +
    `<button class="lib-more snap-more" data-more="${esc(v.name)}" type="button" title="Snapshot actions" aria-haspopup="true" aria-label="Actions for ${esc(v.name)}"></button></div>`).join('');
  // NONE WAS HERE for about an hour on 2026-09-08 and came out again. It detached the name and
  // kept the filters, which is a coherent idea that nonetheless needed explaining -- and a
  // one-word picker item that needs explaining is a smell. The author, on being asked what it was for:
  // "i prefer removing it and using the Reset." Reset already says "start clean", switching
  // snapshots covers the rest, and a picker's None reading as "keep the effect, drop the label"
  // was the part nobody would guess.
  // CLEAR ALL, at the foot of the list it acts on — the same anatomy as the Libraries menu, where
  // the list is followed by a rule and then the action on the whole list.
  // Deleting one snapshot costs THREE actions and applies its filters on the way past: you must
  // restore it before its ⋯ appears. The author: "if someone has 10 snapshots they'd like to clear for a
  // reset, that's 30 actions" — and ten view changes nobody asked for.
  // Offered from one, not two: at a single snapshot this is still the shorter path, because Delete
  // is behind restoring the thing first.
  const clear = rows
    ? `<div class="popmenu-sep"></div><button data-act="clearall" class="danger">Clear all snapshots…</button>` : '';
  // THE LIST SCROLLS, THE ACTION DOES NOT. The menu caps at 260px, and with everything in one
  // scroller Clear all sat below the fold once there were more than about eight snapshots — you had
  // to scroll to reach the thing that empties the list, and at the boundary it rendered half-cut.
  // The author found it and named the fix. So the rows go in their own scrolling box and the footer is a
  // sibling of it, outside the scroll.
  $('#snapMenu').innerHTML = rows
    ? `<div class="snap-list">${rows}</div>${clear}`
    : '<button disabled>No snapshots yet</button>';
}
// Empty the list in one go. The filters are NOT touched, the same promise deleteSnapshot makes:
// you lose the saved captures, not what you are looking at.
async function clearAllSnapshots() {
  const n = (state.snapshots || []).length;
  if (!n) return;
  if (!(await uiConfirm(`Delete ${n === 1 ? 'the only snapshot' : `all ${n} snapshots`}?

` +
      "Your filters are not affected — you lose the saved captures, not what you are looking at. " +
      "This can't be undone.",
      { ok: n === 1 ? 'Delete' : `Delete ${n}`, danger: true }))) return;
  state.snapshots = [];
  if (await persistSnapshots(`Deleted ${n} snapshot${n === 1 ? '' : 's'}`)) clearLoadedSnapshot();
}
async function persistSnapshots(okMsg) {
  const j = await postJSON('/api/snapshots', { snapshots: state.snapshots });
  if (!j || j.error) { uiAlert((j && j.error) || 'Could not save snapshots.'); return false; }
  state.snapshots = j.snapshots || [];   // adopt the server's list: it truncates names and de-dupes
  renderSnapshotMenu(); renderSnapshotRow();
  if (okMsg) toast(okMsg);
  return true;
}
// queryString()/excludeString() fold the uncommitted #q/#xq text into the LIVE search, but
// serializeFilters() only sees committed chips — so a snapshot would silently drop a term the user
// is looking at. Commit it exactly the way Enter does.
function commitPendingTerms() {
  const q = $('#q').value.trim(), x = $('#xq').value.trim();
  if (!q && !x) return false;
  if (q) { state.terms.push(q); $('#q').value = ''; renderChips(); }
  if (x) { state.xterms.push(x); $('#xq').value = ''; renderXChips(); }
  return true;
}
async function applySnapshot(name) {
  const v = snapshotByName(name); if (!v) return;
  await Busy.during('rail', async () => {
    applyFilters(v.filters);
    setLoadedSnapshot(v.name);
    // facets repaint the library checkboxes and tags the tag checklist (applyFilters does neither);
    // in parallel, and search skips its own facets fetch, so the rail paints once. Same as resetFilters.
    await refreshView();
  });
  warnMissingSnapshotTags();
}
// A snapshot's tags are ANDed, so one that no longer exists just empties the grid. Say so rather
// than leaving it looking like a broken filter. Not auto-pruned: the tag list is scoped to the
// visible libraries, so a tag can be absent here and still exist elsewhere.
function warnMissingSnapshotTags() {
  const have = new Set((state._tags || []).map(t => t.name));
  const gone = (state.tags || []).filter(t => !have.has(t));
  if (gone.length) toast('Snapshot restored · ' +
    (gone.length === 1 ? '1 tag no longer exists' : `${gone.length} tags no longer exist`));
}
async function saveSnapshotAs() {
  if (commitPendingTerms()) await search(true);   // same funnel as pressing Enter in the box
  const name = await uiPrompt('Save these filters as a snapshot:', '');
  if (name == null || !name.trim()) return;
  const clean = name.trim();
  const existing = snapshotByName(clean);
  if (existing && !(await uiConfirm(`Replace the snapshot "${existing.name}"?`, { ok: 'Replace' }))) return;
  const entry = { name: existing ? existing.name : clean, filters: serializeFilters() };
  state.snapshots = existing ? state.snapshots.map(v => (v === existing ? entry : v))
                             : state.snapshots.concat([entry]);
  if (await persistSnapshots(`Saved snapshot "${entry.name}"`)) setLoadedSnapshot(entry.name);
}
async function renameSnapshot(which) {
  const v = snapshotByName(which || state.snapshotName); if (!v) return;
  const name = await uiPrompt('Rename this snapshot:', v.name);
  if (name == null || !name.trim()) return;
  const clean = name.trim();
  const clash = snapshotByName(clean);
  if (clash && clash !== v) { uiAlert(`A snapshot called "${clash.name}" already exists.`); return; }
  const wasLoaded = state.snapshotName === v.name;
  v.name = clean;
  // Only follow the rename if it was the RESTORED one; renaming a row you are not using must not
  // quietly load it.
  if (await persistSnapshots('Snapshot renamed') && wasLoaded) setLoadedSnapshot(clean);
}
async function updateSnapshot() {
  const v = snapshotByName(state.snapshotName); if (!v) return;
  if (commitPendingTerms()) await search(true);
  v.filters = serializeFilters();
  if (await persistSnapshots(`Updated "${v.name}"`)) setLoadedSnapshot(v.name);
}
// NAME, not the loaded snapshot: these are reached from a row now, and the row may not be the one
// restored. Falls back to the loaded one so nothing that still calls them bare changes behaviour.
async function deleteSnapshot(name) {
  const v = snapshotByName(name || state.snapshotName); if (!v) return;
  if (!(await uiConfirm(`Delete the snapshot "${v.name}"?`, { ok: 'Delete', danger: true }))) return;
  state.snapshots = state.snapshots.filter(x => x !== v);
  // The filters stay applied — you lose the saved capture, not what you're looking at.
  const wasLoaded = state.snapshotName === v.name;
  if (await persistSnapshots('Snapshot deleted') && wasLoaded) clearLoadedSnapshot();
}

// ---- facets (reactive: reflect the current filter) ----
async function loadFacets() {
  // EVERY NARROWING THE GRID APPLIES, or the counts describe a different library than the one on
  // screen. `type` and `meta` were missing until 2026-09-14: filter to Videos and every dropdown
  // went on counting the images, so the Model list offered checkpoints you had only ever used for
  // stills and handed you an empty grid when you picked one. A dropdown that offers a choice
  // yielding nothing is the failure here, not the number being off by a bit.
  //
  // `group` RIDES WITH `type` and is not optional. The server decides what counts as an Image from
  // what the whole GROUP holds, not the row, so `type=image` means something different depending
  // on whether pairs are collapsing (see _filters, `pairs_on`). Sending one without the other
  // would trade a visible disagreement for a subtler one.
  //
  // Not `sets`: _filters does not read it, and the sets-only case is the OTHER half of this --
  // facets tally files where the grid draws cards, so a count can exceed the cards you get. That
  // is in Help under Known gaps and is not what this line fixes.
  const p = new URLSearchParams({
    q: queryString(), model: state.model, folder: state.folder, mfolder: state.modelFolder,
    meta: state.meta, type: state.type, group: state.group ? '1' : '',
    tags: state.tags.join(','), fav: state.favOnly ? '1' : '', note: state.hasNote ? '1' : '', x: excludeString(),
    roots: rootsParam(), rmin: state.rmin, rmax: state.rmax,
    after: dateAfter(), before: dateBefore(),
  });
  const f = await getJSON('/api/facets?' + p.toString());
  state._facets = { model: f.models || [], folder: f.folders || [] };
  renderFacetDD('model');
  renderFacetDD('folder');
  renderModelTypeFacet(f.mtypes || []);
  window._facets = f;
  renderLibList(f.roots);
}
// Model-type filter = the model's top-level folder. Simple <select> (few values); reactive counts.
function renderModelTypeFacet(mtypes) {
  const sel = $('#mfolder'); if (!sel) return;
  const cur = state.modelFolder;
  let html = '<option value="">All model types</option>';
  for (const m of mtypes) {
    const v = m.name || '';
    html += `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)} (${(m.count || 0).toLocaleString()})</option>`;
  }
  sel.innerHTML = html;
  sel.value = cur;   // keep the current pick even if it isn't in the (filtered) list
}
// Searchable single-select facet dropdown (Model / Folder). One generic renderer keyed by facet:
// sorts by a per-facet mode (default 'count' — the seam for a later Count/A-Z toggle), filters by
// the panel's own search box (substring, case-insensitive), and reflects the current pick.
const _facetSort = (() => {   // per-facet sort: 'count' (default) or 'name' (A-Z); remembered
  try { return Object.assign({ model: 'count', folder: 'count' }, JSON.parse(localStorage.getItem('cv:facetSort') || '{}')); }
  catch (e) { return { model: 'count', folder: 'count' }; }
})();
function saveFacetSort() { try { localStorage.setItem('cv:facetSort', JSON.stringify(_facetSort)); } catch (e) {} }
const _FACET_ALL = { model: 'All models', folder: 'All folders' };
function renderFacetDD(facet) {
  const dd = document.querySelector('.facet-dd[data-facet="' + facet + '"]'); if (!dd) return;
  const cur = state[facet] || '';
  const lbl = dd.querySelector('.facet-label');
  lbl.textContent = cur || _FACET_ALL[facet];
  lbl.title = cur || '';                  // the selected model/folder ellipses in a narrow sidebar
  const q = (dd.querySelector('.facet-search').value || '').trim().toLowerCase();
  let rows = (state._facets && state._facets[facet]) ? state._facets[facet].slice() : [];
  rows.sort(_facetSort[facet] === 'name'
    ? (a, b) => a.name.localeCompare(b.name)
    : (a, b) => b.count - a.count || a.name.localeCompare(b.name));
  if (q) rows = rows.filter(r => r.name.toLowerCase().includes(q));
  const all = `<div class="facet-row${cur === '' ? ' sel' : ''}" data-val=""><span class="fname">${_FACET_ALL[facet]}</span></div>`;
  dd.querySelector('.facet-list').innerHTML = all + rows.map(r =>
    `<div class="facet-row${r.name === cur ? ' sel' : ''}" data-val="${esc(r.name)}" title="${esc(r.name)}">` +
    `<span class="fname">${esc(r.name)}</span><span class="fcount">${(r.count || 0).toLocaleString()}</span></div>`).join('');
  dd.querySelectorAll('.facet-sortmenu button[data-sort]').forEach(b =>
    b.classList.toggle('active', b.dataset.sort === _facetSort[facet]));
}
function openFacetDD(facet) {
  closeMenus();
  const dd = document.querySelector('.facet-dd[data-facet="' + facet + '"]'); if (!dd) return;
  const s = dd.querySelector('.facet-search'); s.value = '';
  renderFacetDD(facet);
  dd.querySelector('.facet-panel').classList.remove('hidden');
  setTimeout(() => s.focus(), 20);
}
function closeFacetDDs() {
  document.querySelectorAll('.facet-panel, .facet-sortmenu').forEach(p => p.classList.add('hidden'));
  // The Libraries scope button has no chevron, so aria-expanded is the only thing telling a screen
  // reader the list shut. Reset it HERE rather than in the click handler: Escape, an outside click
  // and every other opener all route through this, and none of them touches that handler.
  const sc = $('#libScope'); if (sc) sc.setAttribute('aria-expanded', 'false');
}
// Close EVERY popmenu (facet panels/sort menus, the library ⋯ menu, the ☰ app menu). Openers call
// this first so only one is ever open — and it doesn't depend on a click reaching the document
// handler (controls that stopPropagation would otherwise leave another menu stuck open).
function closeMenus() {
  closeFacetDDs();
  // Every JS-placed menu at once. .placed is added by placeMenu(), so a new one is closed by
  // having been opened rather than by being remembered here — this was a hand-listed pair of ids,
  // and the third one (the detail view's Extensions menu) duly stayed open underneath the next
  // menu to open over it.
  document.querySelectorAll('.popmenu.placed').forEach(m => m.classList.add('hidden'));
  const eb = $('#dExtBtn'); if (eb) eb.setAttribute('aria-expanded', 'false');
  const am = $('#appMenu'); if (am) am.classList.add('hidden');
  // the snapshot menus carry neither .facet-panel nor .facet-sortmenu, so closeFacetDDs misses them
  const vm = $('#snapMenu'); if (vm) vm.classList.add('hidden');
  const va = $('#snapActions'); if (va) va.classList.add('hidden');
  const sm = $('#selMoreMenu'); if (sm) sm.classList.add('hidden');
  const smb = $('#selMore'); if (smb) smb.setAttribute('aria-expanded', 'false');
}
function pickFacet(facet, val) {
  state[facet] = val;
  closeFacetDDs();
  renderFacetDD(facet);
  search(true);
}
function libName(key) { return ((state.roots || []).find(r => r.key === key) || {}).name || 'this library'; }
// A library whose folder isn't reachable from this machine right now (network share offline, drive
// unplugged). Its rows, tags and CACHED THUMBNAILS still work — only the files themselves don't, so
// we keep browsing/curation available and mark what can't work. `exists` comes from /api/config.
function isRootOffline(key) {
  const r = (state.roots || []).find(x => x.key === key);
  return !!r && r.exists === false;
}
// The effective library selection. `null` means all of them; an array means exactly those, and an
// empty one means none — see the note on state.rootsSel.
function selectedRootKeys() {
  const all = allRootKeys();
  if (state.rootsSel === null) return all;
  return state.rootsSel.filter(k => all.includes(k));
}
// The collapsed Libraries row. The per-library rows live in the dropdown; this is everything worth
// seeing without opening it — the scope, and the two things that would otherwise go unnoticed:
// a library that's unreachable right now, and a library whose folder changed on disk.
function renderLibTrigger() {
  const n = $('#libScopeN'); if (!n) return;
  const all = state.roots || [], sel = selectedRootKeys();
  n.textContent = `${sel.length}/${all.length}`;
  // WITH NO LIBRARY, THIS STRIP IS THE ONLY THING TO DO AND TWO THIRDS OF IT IS DEAD. The scope
  // button is where a new user has to go, and the two refresh controls beside it cannot check a
  // disk nobody has named yet. The author, 2026-09-07, on the auto-refresh clock: "It's a false signal,
  // and will draw attention from the Folder" -- it sat lit, in the accent colour, next to the one
  // control that mattered. The tick is swept with it because it is dead for the same reason; the
  // reported control and the one beside it fail as a class.
  // HIDDEN, NOT DIMMED, per the rule at applyExtensions: dimmed-but-pressable means idle, and
  // these two do not exist yet. Derived HERE because this function already knows all.length and
  // already owns both controls -- a second place deciding it is a second place to get it wrong.
  const noLib = !all.length;
  setNoLibraryStrip(noLib);
  const btn = $('#libScope');
  if (btn) {
    // The strip's OWN green, and since the scope left filtersActive() this is the only thing
    // saying it. A subset changes what is IN scope, not what matched inside it — the server has
    // always classed it that way (_filters puts the roots clause with the global exclusions:
    // "a library being switched off is library visibility, not a search term"), so the count
    // beside this one no longer lights for it. Set HERE, beside the number it colours, so the
    // two cannot disagree. (The green used to be a ring on the field; there is no field now.)
    btn.classList.toggle('filtered', !!rootsParam());
    // THE HOVER CARRIES TWO THINGS NOW: which libraries, and how much is in them. The size half
    // moved here when the matched-card readout left the strip (see renderCount) — it is the only
    // surface anywhere that states the library's FILE count as against its card count, and this is
    // the control that already means "what is in scope", so it is where the sentence belongs
    // rather than a second readout to house it.
    const fmt = x => (x || 0).toLocaleString();
    const matched = state.total || 0;
    const libCards = state.rootTotal != null ? state.rootTotal : matched;
    const libFiles = state.rootFiles != null ? state.rootFiles : libCards;
    const scope = collapsingNow() ? `${fmt(libCards)} cards · ${fmt(libFiles)} files`
                                  : `${fmt(libFiles)} files`;
    const size = filtersActive() ? `${fmt(matched)} matched · ${scope} in scope`
                                 : `${scope} in scope`;
    const which = !all.length ? 'No libraries yet — click to add one'
      // Its own line, because the subset sentence below would otherwise read "In scope:" followed
      // by nothing at all. Says what to do, since this is the one state the grid cannot resolve.
      : !sel.length ? 'No libraries selected — select one to see its images'
      : sel.length >= all.length ? `All ${all.length} librar${all.length === 1 ? 'y' : 'ies'} in scope`
      : `In scope: ${sel.map(libName).join(', ')}`;
    // Totals are absent until the first search lands; say only what is known rather than "0 files".
    btn.title = (state.rootTotal == null && state.total == null) ? which : `${which}\n${size}`;
  }
  // NO OFFLINE ROLL-UP HERE ANY MORE (2026-08-21). It reported that SOME selected library was
  // unreachable; the rows' own offline mark and each affected card's offline badge both survive,
  // and they say WHICH — see the strip's comment block in index.html.
  // ALWAYS on the strip now, muted. It used to be hidden until something changed, which made the
  // R key's action discoverable only by it appearing. Amber + underline = at least one library's
  // folder moved on disk. The aria-label stays constant across both states because the ACTION is
  // the same either way (refreshChanged() checks, then rescans whatever it found).
  // No FILE count here, on purpose: /api/changes is one stat per indexed folder, so it knows only
  // THAT a folder moved — and it moves on deletes and renames too, not just new files. A number
  // would mean a full walk of every dirty library, which on a network share is the cost this
  // check was built to avoid. "Pending" is the honest word for what it actually knows.
  // ...and NOT while auto-refresh is on. Amber is a call to action — "your move, press R". With the
  // timer running there is no move to make: it picks new files up on its own, so the mark would be
  // nagging about something already being handled. The clock is then the single indicator (on =
  // watching, spinning = working). The trade: with the timer on, files landing between ticks aren't
  // announced. That's the deal — they're folded in within the minute, and R still forces a check.
  const r = $('#libRefreshAll');
  if (r) {
    const dirty = !_autoOn && Object.values(state._changes || {}).some(Boolean);
    r.classList.toggle('dirty', dirty);
    r.title = dirty ? 'New files pending' : 'Check for new files on disk (R)';
  }
}
// The first-run state of the Libraries strip: the scope button asks to be clicked, and the two
// controls that need a library stand down until there is one.
// The pulse rides the GLYPH (cv-pulse), not the fill, because this button is not a filled pill
// here -- the same choice the auto-refresh clock's own comment records for the opposite case.
function setNoLibraryStrip(noLib) {
  const btn = $('#libScope');
  if (btn) btn.classList.toggle('nudge', noLib);
  for (const id of ['#libRefreshAll', '#autoRefresh']) {
    const el = $(id);
    if (el) el.classList.toggle('hidden', noLib);
  }
}
// The Libraries list, inside the collapsed row's dropdown: each row is show/hide (checkbox) + name
// + count, a per-library ↻ when its folder changed on disk, and a ⋯ actions menu. Rows stay in
// config order (they don't reorder by count) so a library never jumps around under your cursor.
function renderLibList(facetRoots) {
  renderLibTrigger();   // the readouts on the rail are derived from the same state as the rows
  const box = $('#libList'); if (!box) return;
  const all = state.roots || [];
  if (!all.length) {
    // No ＋ here: the panel's own "＋ Add library…" row sits directly below this line, and two Add
    // affordances in one popmenu is a choice where there isn't one.
    box.innerHTML = '<div class="lib-empty">No libraries yet.</div>';
    return;
  }
  // `null` ticks everything; an array ticks exactly its members, so an empty one ticks nothing.
  const sel = new Set(state.rootsSel === null ? all.map(r => r.key) : state.rootsSel);
  // THE LIBRARY'S SIZE, not how much of it matched. `count` is the facet number — "how many of your
  // current matches live here" — and reading it as the library's size made every row shrink as you
  // typed, and the list re-order itself while you were looking at it. A library's size is a fact
  // about the library; it has no business moving when you search.
  const counts = {}; (facetRoots || []).forEach(r => { counts[r.key] = r.total != null ? r.total : r.count; });
  // Suppressed while the timer is on, for the reason in renderLibTrigger: one signal at two scopes,
  // so the row marks and the strip mark must never disagree about whether there's anything to do.
  const changes = _autoOn ? {} : (state._changes || {});
  // This list is repainted by BACKGROUND work now (probeChanges → checkChanges fires off the back
  // of a filter change), and `innerHTML =` resets scrollTop to 0. With enough libraries to scroll
  // and the panel open, that would jump the list out from under the cursor.
  const keepScroll = box.scrollTop;
  box.innerHTML = all.map(r => {
    // "OFFLINE", NOT "(missing)". One condition had two names: the row said missing, while the
    // dialog, the toast, the empty grid and every disabled control said offline. The author, 2026-09-14:
    // "let's align on 'offline' as the term." "Missing" also said the wrong thing -- it reads as
    // a folder that has been deleted or moved, which is a different problem with a different fix.
    const miss = r.exists === false ? ' <span class="lib-miss">offline</span>' : '';
    // THE SAME MARK AS THE STRIP'S, and it has to stay that way: the strip's button is the
    // roll-up of these, so two different glyphs would make the roll-up look like a different claim.
    // Both are a bare tick as of 2026-08-21 — "check the files". See the block above
    // #libRefreshAll in index.html for the run of glyphs before it and why each one failed.
    const ref = changes[r.key]
      ? `<button class="lib-ref" data-ref="${esc(r.key)}" title="New files pending — rescan this library"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg></button>` : '';
    // BEHIND THIS BUILD'S READER — a different question from the tick beside it, and deliberately a
    // different silhouette. The tick asks "are there new files on disk?"; this asks "can the app now
    // read more than it could when this library was indexed?". They are told apart by shape rather
    // than by tooltip alone, per the author's call, 2026-09-14: a rotate, because the answer is to re-read
    // what is already here, where the tick's answer is to go and look for more.
    // SAME AMBER AS THE TICK, not a second colour. The colour means "this library wants attention";
    // which kind is the silhouette's job. Giving them a colour each would have made the shapes
    // decorative and put two meanings on one channel -- and it needs #libPanel scope to survive at
    // all, which cost an hour on the day it was written: see the note by #libPanel .lib-catchup.
    // Not shown for a library that is not there — its rows are behind and will stay behind, and
    // offering a rescan of a folder nobody can reach is a button that can only fail.
    const behind = state._behind && state._behind[r.key];
    const cat = (behind && r.exists !== false)
      ? `<button class="lib-catchup" data-catchup="${esc(r.key)}" title="Rescan for new version features"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg></button>` : '';
    return `<div class="librow" data-key="${esc(r.key)}">` +
      `<input type="checkbox" class="lib-vis" data-root="${esc(r.key)}"${sel.has(r.key) ? ' checked' : ''} title="Show / hide this library in the grid">` +
      `<span class="tname" title="${esc(r.path || r.name)}">${esc(r.name)}${miss}</span>` +
      `<em class="tcount">${(counts[r.key] || 0).toLocaleString()}</em>${cat}${ref}` +
      `<button class="lib-more" data-more="${esc(r.key)}" title="Library actions" aria-haspopup="true" aria-label="Library actions"></button>` +
      `</div>`;
  }).join('');
  box.scrollTop = keepScroll;
  // No sizeTagList() here any more: the rail above the tag list is a fixed-height row now, so this
  // list's contents can't move the taglist's offset. Switching to the Tags tab and window resize
  // still fit it — those are the only two things that change the offset.
}
let _libMenuKey = null;
// Float the shared library ⋯ menu next to the clicked row's button (fixed-positioned, clamped
// on-screen). One menu, repositioned per row, keyed to _libMenuKey for its actions.
function placeMenu(m, btn) {
  m.classList.remove('hidden');
  // Before measuring: .placed is what makes it position:fixed and raises it above the overlay it
  // opens over, and it carries a min-width, so measuring first would measure the wrong box.
  m.classList.add('placed');
  const r = btn.getBoundingClientRect(), mw = m.offsetWidth || 180, mh = m.offsetHeight || 200;
  m.style.left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8)) + 'px';
  m.style.top = (r.bottom + mh > window.innerHeight ? r.top - mh - 2 : r.bottom + 2) + 'px';
}
// The row's ⋯ now sits INSIDE the library dropdown, so the usual closeMenus() would shut the panel
// the button lives in — the menu would open beside a list that vanished under the cursor. Close
// everything else, then put the panel back.
function openLibMenu(key, btn) {
  const panel = $('#libPanel');
  const wasOpen = panel && !panel.classList.contains('hidden');
  closeMenus();
  if (wasOpen) panel.classList.remove('hidden');
  _libMenuKey = key;
  // Only offered where there is something to open. A library that recycles to the OS bin never
  // grows this folder, so the item would be a dead end on the author's local library -- his report.
  const r = (state.roots || []).find(x => x.key === key);
  const menu = $('#libMenu');
  menu.querySelector('[data-act="recyclefolder"]')
    .classList.toggle('hidden', !(r && r.has_recycle));
  // AN OFFLINE LIBRARY STILL OFFERED EVERYTHING. The author, travelling with three of five libraries on a
  // machine he was away from: "their menus seem active." Every item was live, and the ones needing
  // the files would simply fail.
  //
  // EVERYTHING GOES DEAD EXCEPT REMOVE. I first disabled only the two that touch the disk, on the
  // grounds that the rest are pure index work and technically fine -- clearing tags, renaming,
  // scanning for tags (run_mine is one SELECT and no I/O). The author overruled it: "doesn't make sense
  // to take any action on an offline folder, except removing it. e.g. Model folder is LIKELY
  // offline too."
  //
  // He is right, and the Models folder is the case that shows why: a library on an unreachable
  // machine almost certainly has its checkpoints on that machine too, so a dialog that validates
  // that path is a dead end. The general form -- CAN this run is not the same question as SHOULD it
  // -- matters more. A rule the user can hold ("it is away; you can remove it, that is all") beats
  // a taxonomy they have to learn to explain why four of ten items are live.
  //
  // Remove stays because it is the one thing you might genuinely need: dropping a library you no
  // longer have. It deletes nothing on disk, so it is safe with the share down.
  const off = isRootOffline(key);
  for (const b of menu.querySelectorAll('button[data-act]')) {
    const dead = off && b.dataset.act !== 'delete';
    b.setAttribute('aria-disabled', String(dead));
    if (dead) {
      if (b.dataset.tipWas === undefined) b.dataset.tipWas = b.title || '';
      b.title = `${libName(key)} is offline — reconnect to use this`;
    } else if (b.dataset.tipWas !== undefined) {
      b.title = b.dataset.tipWas;
    }
  }
  placeMenu(menu, btn);
}

// ---- curation labels (exclusive one-key disposition marks) ----
const labelBySlug = s => (state.labels || []).find(l => l.slug === s) || null;
const labelByKey = k => (state.labels || []).find(l => l.key === k) || null;
function labelName(slug) { const l = labelBySlug(slug); return l ? l.name : slug; }
function labelCount(slug) { const t = (state._tags || []).find(x => x.name === 'label:' + slug); return t ? t.count : 0; }
// The single active label filter (a `label:<slug>` entry in state.tags), or null.
function activeLabel() { const t = (state.tags || []).find(x => x.startsWith('label:')); return t ? t.slice(6) : null; }

// POST a label change for `ids` (slug null = clear), then refresh cards + counts in place.
async function applyLabels(ids, slug) {
  if (!ids.length) return;
  await postJSON('/api/label', { ids, label: slug });
  ids.forEach(id => {
    const it = state.items.find(x => String(x.id) === String(id));
    if (it) { it.label = slug; refreshCard(id); }
  });
  if (state.current && ids.map(String).includes(String(state.current.id))) {
    state.current.label = slug; renderDetailLabels();
  }
  loadTags();     // label counts live in the tag table
}
// Grid selection: toggle off only when every selected item already has this label, else set it.
function labelSelection(slug) {
  const ids = [...selection].map(Number);
  if (!ids.length) return;
  const items = ids.map(id => state.items.find(x => String(x.id) === String(id))).filter(Boolean);
  const allHave = items.length && items.every(it => it.label === slug);
  // The whole card, like the star and Hide — otherwise filtering to the label splits a pair
  // apart and the card comes back as a lone image. See expandCardIds.
  applyLabels(expandCardIds(ids), allHave ? null : slug);
}
// Sidebar Labels section: one row per label with swatch, name, count, hotkey; click filters
// (single-select — labels are exclusive so an AND filter is meaningless), click again clears.
function renderLabelList() {
  const box = $('#labelList'); if (!box) return;
  const act = activeLabel();
  const rows = (state.labels || []).map(l =>
    `<div class="label-row${l.slug === act ? ' active' : ''}" data-label="${esc(l.slug)}" title="Filter to ${esc(l.name)}">` +
    `<span class="lsw label-${esc(l.slug)}"></span>` +
    `<span class="lname">${esc(l.name)}</span>` +
    `<span class="lcount">${labelCount(l.slug).toLocaleString()}</span>` +
    `<span class="lkey">${esc(l.key)}</span></div>`).join('');
  // NO "Unreviewed" ROW. It was retired on 2026-08-19 — see filterByLabel below.
  box.innerHTML = rows;
}
function filterByLabel(slug) {
  const on = activeLabel() === slug;
  state.tags = (state.tags || []).filter(t => !t.startsWith('label:'));   // single-select
  if (!on) state.tags.push('label:' + slug);
  renderLabelList();
  search(true);
}
// Detail Labels row: one button per label, the active one outlined; click toggles (same = clear).
function renderDetailLabels() {
  const box = $('#dLabels'); if (!box) return;
  const cur = state.current ? state.current.label : null;
  box.innerHTML = (state.labels || []).map(l =>
    `<button class="label-btn${l.slug === cur ? ' active' : ''}" data-label="${esc(l.slug)}" title="${esc(l.name)} (${esc(l.key)})">` +
    `<span class="lsw label-${esc(l.slug)}"></span>${esc(l.name)}</button>`).join('');
}
function labelDetail(slug) {
  if (!state.current) return;
  const cur = state.current.label;
  applyLabels(detailMarkIds(state.current), cur === slug ? null : slug);
}

// ---- tags (sidebar list + filtering) ----
async function loadTags() {
  // THE SAME NARROWING THE GRID GETS. It used to send `roots` and nothing else, so every label,
  // tag and favourite count was a whole-library total: the rail read "To publish 10" beside a grid
  // holding one, which is how the author found it. The server drops `tags` and `fav` at its end so a tag
  // does not narrow itself -- tick one label and the other four still say what switching would
  // give -- but everything else has to arrive for the number to mean anything.
  //
  // Sent even when empty, unlike the old roots-only URL: an absent parameter and an empty one mean
  // the same thing to the server, and building the string the same way every time is what stops
  // the next filter being forgotten here the way `type` was forgotten in loadFacets.
  const p = new URLSearchParams({
    q: queryString(), x: excludeString(), model: state.model, folder: state.folder,
    mfolder: state.modelFolder, meta: state.meta, type: state.type,
    group: state.group ? '1' : '', note: state.hasNote ? '1' : '', roots: rootsParam(),
    rmin: state.rmin, rmax: state.rmax, after: dateAfter(), before: dateBefore(),
  });
  const data = await getJSON('/api/tags?' + p.toString());
  state._tags = data.tags || [];
  state._favCount = data.favorites || 0;
  renderTagList();
  renderLabelList();     // label counts come from the same tag table
  // Datalist + checklist exclude label: entries (they live in the Labels section, not Tags).
  $('#tagoptions').innerHTML = state._tags.filter(t => !t.name.startsWith('label:'))
    .map(t => `<option value="${esc(t.name)}">`).join('');
}
// Render the tag checkbox list, filtered by the search box text. Checked = active filter (AND).
function renderTagList() {
  const q = (($('#tagSearch') || {}).value || '').trim().toLowerCase();
  const favChk = state.favOnly ? ' checked' : '';
  // The trailing hidden .tagdel keeps the count aligned with tag rows (which have a real × button).
  let html = `<label class="tagrow fav"><input type="checkbox" data-fav="1"${favChk}><span class="tname">Favorites</span><em class="tcount">${state._favCount || 0}</em><span class="tagdel" aria-hidden="true" style="visibility:hidden">×</span></label>`;
  for (const t of (state._tags || [])) {
    if (t.name.startsWith('label:')) continue;   // labels live in their own sidebar section
    if (q && !t.name.toLowerCase().includes(q)) continue;
    const chk = state.tags.includes(t.name) ? ' checked' : '';
    // Machine tags look exactly like the rest here — the author's call. The server still says which
    // are only an extension's (`t.machine`, true only when no user row exists for that tag), and
    // that goes into the tooltip rather than into the type.
    const mtitle = t.machine ? ' — found by an extension' : '';
    html += `<label class="tagrow" title="${esc(t.name)}${mtitle}"><input type="checkbox" data-tag="${esc(t.name)}"${chk}><span class="tname">${esc(t.name)}</span><em class="tcount">${t.count}</em><button class="tagdel" data-del="${esc(t.name)}" title="Delete tag everywhere">×</button></label>`;
  }
  $('#taglist').innerHTML = html;
}
// Type a new tag + Enter in the search box -> apply it to the currently selected images.
async function addTagToSelection(tag) {
  tag = (tag || '').trim().toLowerCase();
  if (!tag) return;
  const ids = [...selection].map(Number);
  if (!ids.length) { toast('Select images first, then type a tag + Enter'); return; }
  // The whole card, like the star and the labels — see expandCardIds. The toast still counts
  // CARDS, which is what you selected and what you'd count on screen.
  await fetch('/api/tag', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: expandCardIds(ids), tag, op: 'add' }) });
  $('#tagSearch').value = '';
  await loadTags();
  search(true);
  toast(`Tagged ${ids.length} as "${tag}"`);
}
async function deleteTag(name) {
  if (!(await uiConfirm(`Delete the tag "${name}" from all images?\n(The images themselves are not deleted.)`, { ok: 'Delete', danger: true }))) return;
  await fetch('/api/tags/delete', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tag: name }) });
  state.tags = state.tags.filter(t => t !== name);
  await loadTags();
  search(true);
}

// ---- search / grid ----
// Thumbnails are generated lazily on first view, so an image never opened while the library was
// online has NO cached thumb — and offline there's no source file to make one from. Mark those
// cards instead of leaving a broken-image icon. (Only offline cards get this: for a reachable
// library a missing thumb is a real error worth seeing.)
// Each thumbnail eases in as it DECODES, not when its markup lands — lazy images arrive one by one
// while you scroll, and without this each one snaps to full opacity. Inline handlers (the house
// pattern here, see the offline onerror) rather than listeners: they're attached at parse time, so
// there's no window where a fast cache hit can fire before JS wires anything up.
// `.in` only TRIGGERS an animation whose resting state is the image's own opacity — it is never
// what makes the image visible. Same rule as .grid.swap-in: if the class never lands, the thumbnail
// appears without a fade rather than not at all. onerror gets it too, so a broken thumb still
// animates in as a broken-image marker instead of being silently skipped.
// THERE IS NO PER-THUMBNAIL FADE. It was added 2026-07-30 ("lazy images arrive one at a time, and
// each used to snap to full opacity") and removed 2026-08-04 because it was the cause of a
// long-running flicker report: cards appeared and then visibly repainted a moment later.
//
// The mechanism: `cv-fade-up` starts at opacity 0 and the class was added from the image's own load
// handler, so the browser could paint the picture at full opacity for one frame before the
// animation's first keyframe applied — the image appeared, blinked out, and faded back up.
//
// FOUR fixes were attempted from code-reading first (the hover upgrade, the grid re-sort, the
// pagination spinner, and suppressing the fade for images already cached at insert) and every one
// was consistent with the code and wrong about the app. What settled it was the USER'S idea: run
// the build from just before this feature side by side. That version does not flicker.
// The general lesson, worth more than the fix: **when reasoning has been wrong twice, bisect.**
// A decorative animation is not worth a defect you can see on every card.
// Nothing to do on a plain broken thumbnail now the fade is gone — it just shows the browser's own
// broken-image mark. An OFFLINE card still needs its class, so the card can say "no cached preview"
// rather than showing a broken image for a file on a disconnected share.
const ONERR_OFFLINE = ` onerror="this.closest('.card').classList.add('no-thumb')"`;

// The Hidden mark's glyph — a crossed-out eye, used at two scales: 16px on a card and 17px in the
// Libraries strip. Kept as ONE constant that both render from, because the strip's twin is written
// into index.html by hand like its neighbours and two hand-drawn copies would drift.
// Drawn as an outline + pupil + full-width slash: the slash crossing the outline is what the icon
// is meant to read as, unlike Reset's rejected slashed funnel, which was two DETAILED shapes
// overlapping inside 15px and resolved to neither. Ink fills ~78% of the viewBox (DESIGN.md: an
// icon's size is its ink, not its box).
// Favorite. Lucide `star`, replacing the ★ TEXT GLYPH it was for months — and the reason is the one
// DESIGN.md already gives for every remaining text glyph: a character renders at the item's font
// size with its own internal bearings, so it cannot be box-aligned with the icons beside it and no
// amount of padding fixes that. It was the odd mark out in the card's top row, sitting at a
// different size and a different vertical centre from the check and the hide mark.
// An svg also buys a state the glyph could not have: OUTLINE when it is not a favorite, FILLED when
// it is, rather than one solid shape that only changes colour.
const ICON_STAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z" /></svg>';


// ---- Songs: the card IS the artwork ------------------------------------------------------------
// Audio has no picture, so a song's card is drawn rather than fetched. Everything it shows comes
// out of the file (see comfy_meta.extract_audio), and anything the file didn't state is simply
// absent — a song whose caption never mentioned a tempo shows no tempo, not a blank labelled one.

function fmtDuration(secs) {
  if (secs == null) return '';
  const s = Math.max(0, Math.round(secs));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// A stable hue per genre, so the grid reads as bands of colour before a word is read: every
// country track is the same shade, and a screen of them separates at a glance. Songs with NO
// genre stay neutral rather than borrowing a hue from something else — an untinted card is how
// you see that the caption never said.
function songHue(genre) {
  let h = 0;
  for (const ch of String(genre || '')) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

// The waveform, as one path of vertical strokes. A path rather than 96 rects because a full screen
// of songs would otherwise add thousands of elements to the grid for decoration.
function wavePath(peaks) {
  if (!Array.isArray(peaks) || !peaks.length) return '';
  const n = peaks.length;
  let d = '';
  for (let i = 0; i < n; i++) {
    const len = Math.max(1, (Number(peaks[i]) || 0) * 0.28);
    d += `M${i} ${(32 - len) / 2}v${len.toFixed(1)}`;
  }
  return `<svg class="song-wave" viewBox="0 0 ${n} 32" preserveAspectRatio="none" aria-hidden="true">`
       + `<path d="${d}"/></svg>`;
}

function songFaceHTML(it) {
  const facts = [fmtDuration(it.duration), it.bpm ? `${it.bpm} bpm` : '', it.key || '']
    .filter(Boolean).join(' · ');
  // When the headline chain fell all the way through to the genre, the genre IS the title — so it
  // doesn't get printed a second time directly underneath itself.
  const subGenre = (it.genre && it.genre !== it.title) ? it.genre : '';
  const sub = [subGenre, it.vocal ? `${it.vocal} vocal` : ''].filter(Boolean).join(' · ');
  const tinted = it.genre ? ` style="--song-hue:${songHue(it.genre)}"` : '';
  // Cover art is an INSET, not the card face — deliberately. It keeps the drawn card's contrast
  // under our control (text never sits on an unpredictable picture) and keeps the genre tint and
  // waveform doing their work, while still giving the grid something to recognise a song by.
  const cover = it.cover_url
    ? `<img class="song-cover" loading="lazy" draggable="false" alt="" src="${thumbAt(it.cover_url, 128)}">` : '';
  return `<div class="song-face${it.genre ? ' tinted' : ''}${cover ? ' has-cover' : ''}"${tinted}>
    <div class="song-head">${cover}<div class="song-heading">
      <div class="song-title">${esc(it.title || it.filename || '')}</div>
      ${sub ? `<div class="song-sub">${esc(sub)}</div>` : ''}
    </div></div>
    ${wavePath(it.peaks)}
    ${facts ? `<div class="song-facts">${esc(facts)}</div>` : ''}
  </div>`;
}

// ---- Card facts: the small print over the picture ---------------------------------------------
// ONE TABLE, and it is the whole feature. Each entry names a fact, the tier it belongs to, a label,
// and how to print it. Moving a fact between tiers -- or dropping one -- is editing ONE WORD here,
// deliberately: which facts earn their place is not knowable until the grid has been lived with,
// and the author said plainly he needs to see this before he can judge the split.
//
// `label` is unused today. It is what a future Settings tab renders its list from, so that tab
// becomes a READER of this table rather than a second copy of it.
//
// A `get` returns '' for a fact this item hasn't got, and an absent fact is simply ABSENT -- no
// dash, no empty slot, no orphaned separator. Same rule the detail view's rows follow.
// The ORDER here is the default order, and age leads it on the author's preference (2026-08-24): "I
// prefer the time to come before the image size". Kept in step with DEFAULT_CARD_FACTS in
// server.py, which is what a saved config is filled from.
const CARD_FACTS = [
  { key: 'age',      tier: 'always', label: 'Age',        get: it => fmtAge(it.mtime) },
  { key: 'dims',     tier: 'always', label: 'Dimensions', get: it => (it.width && it.height) ? `${it.width}×${it.height}` : '' },
  { key: 'duration', tier: 'always', label: 'Duration',   get: it => fmtDuration(it.duration) },
  // fmtBytes(null) returns an em dash BY DESIGN, and its comment asks callers needing a different
  // format to take an argument rather than teach it a second one -- so this guards at the call
  // site. A stray dash in the band is the likeliest way this ships subtly wrong.
  { key: 'filesize', tier: 'hover',  label: 'File size',  get: it => it.size != null ? fmtBytes(it.size) : '' },
  { key: 'model',    tier: 'hover',  label: 'Model',      get: it => it.model || '' },
  { key: 'folder',   tier: 'hover',  label: 'Folder',     get: it => folderLeaf(it.folder) },
];
// HOVER FIRST, ALWAYS LAST, and the order is load-bearing. The band is anchored by its BOTTOM edge,
// so putting the extra row ABOVE the permanent one means the permanent one never moves when you
// point at a card -- the band grows upward into the picture instead of shoving its own first line.
const CARD_FACT_TIERS = ['hover', 'always'];
const CARD_FACT_BY_KEY = Object.fromEntries(CARD_FACTS.map(f => [f.key, f]));

// ORDER AND PLACEMENT COME FROM THE CONFIG; the table above only says what each fact IS and where
// it starts. Settings -> Cards writes this list, the server validates membership and nothing else,
// and an unknown key there is dropped while a fact missing from it is appended at its default —
// so adding a seventh fact one day makes it appear rather than hide.
//
// `state.cards` is the ordered [{key, place}]. Until /api/config lands, the table's own `tier` is
// the answer, which is what keeps the very first paint correct rather than empty.
function cardFactList() {
  const cfg = state.cards;
  if (!Array.isArray(cfg) || !cfg.length) {
    return CARD_FACTS.map(f => ({ def: f, place: f.tier }));
  }
  return cfg.map(r => ({ def: CARD_FACT_BY_KEY[r.key], place: r.place }))
            .filter(r => r.def && r.place !== 'off');
}

// ONE PLACE BUILDS A ROW'S TEXT, because two things now need it: the card's markup, and the
// re-tick that keeps an age honest without rebuilding the card. A second copy of this join is a
// second answer to "what does this row say".
function cardFactLine(it, tier, list) {
  return (list || cardFactList()).filter(r => r.place === tier)
    .map(r => r.def.get(it)).filter(Boolean).join(' · ');
}

function cardFactsHTML(it) {
  // A song's face already draws its own duration, tempo and key, and it is tight at every card
  // size -- a second band would print the length twice and fight the drawing. Same reason .cap is
  // suppressed there (see style.css).
  if (it.is_audio) return '';
  const list = cardFactList();
  let rows = '';
  for (const tier of CARD_FACT_TIERS) {
    const line = cardFactLine(it, tier, list);
    if (!line) continue;
    // The hover row holds the long strings and is the one that ellipsises, so it carries the whole
    // text as its tooltip.
    const tip = tier === 'hover' ? ` title="${esc(line)}"` : '';
    rows += `<span class="cf-row cf-${tier}"${tip}>${esc(line)}</span>`;
  }
  return rows ? `<div class="card-facts">${rows}</div>` : '';
}
// AN AGE IS THE ONE FACT THAT GOES WRONG WHILE YOU LOOK AT IT. Every other fact on a card is a
// property of the file and is true until the file changes; "3 hr" is true for an hour. And nothing
// repaints a card that is already on screen — every render path is either one card after an edit or
// an APPEND of new ones — so a grid painted at nine still said "just now" at five. The author asked
// whether the labels would stay honest (2026-08-24); they would not, and the error was exactly as
// large as the time the window had been open, which is his whole working pattern.
//
// So the TEXT is re-ticked, not the cards. No markup is rebuilt, no image reloads, no card is
// replaced: each visible row's string is recomputed and written only when it actually differs,
// which on a normal minute is never. A rebuild of 200 cards a minute would cost far more than the
// accuracy is worth; this costs a few hundred string joins and, almost always, zero DOM writes.
const AGE_TICK_MS = 60000;      // the finest unit a label can carry is a minute
function refreshCardAges() {
  if (document.hidden) return;                       // a hidden tab is caught up on return, below
  const list = cardFactList();
  if (!list.some(r => r.def.key === 'age')) return;  // age switched off: nothing here can go stale
  const byId = new Map(state.items.map(it => [String(it.id), it]));
  document.querySelectorAll('#grid .card').forEach(el => {
    const it = byId.get(el.dataset.id);
    if (!it) return;
    for (const tier of CARD_FACT_TIERS) {
      const row = el.querySelector('.cf-' + tier);
      if (!row) continue;
      const line = cardFactLine(it, tier, list);
      if (row.textContent !== line) row.textContent = line;   // the guard is what makes this free
    }
  });
  // The detail view can sit open for hours too, and its Date row carries the age beside the date.
  const d = state.current;
  if (d && d.mtime && !$('#overlay').classList.contains('hidden')) {
    $('#dDate').textContent = `${new Date(d.mtime * 1000).toLocaleString()} · ${fmtAge(d.mtime)}`;
  }
}
setInterval(refreshCardAges, AGE_TICK_MS);

// Every card again, from the items already in hand. No fetch, no scroll reset, no selection lost —
// cardHTML reads `selection` itself, so a repaint restores the ticks rather than clearing them.
// This is what a Settings change costs, instead of search(true): a full re-query measured 3.6s on
// a real trace, which is why saveSettings only re-searches when the RESULTS would differ.
function repaintCards() {
  document.querySelectorAll('#grid .card').forEach(el => {
    const it = state.items.find(x => String(x.id) === String(el.dataset.id));
    if (it) el.outerHTML = cardHTML(it);
  });
}

function cardHTML(it) {
  const sel = selection.has(String(it.id)) ? ' selected' : '';
  const star = `<span class="star${it.fav ? ' on' : ''}" title="Favorite">${ICON_STAR}</span>`;
  const reward = it.reward != null && extActive('quality')
    ? `<span class="reward" title="Quality 1-10 (raw ${it.reward.toFixed(3)})">${metricTo10(it.reward).toFixed(1)}</span>` : '';
  // Bottom-left cluster: two orthogonal marks. ▶ = "contains/opens a video" (a lone video, animated
  // gif/webp, or a merged still+video set). The stacked-cards mark = "this card is a collapsed SET".
  // So: single image → neither; single video → ▶; image set → cards; video set → ▶ + cards.
  // (A lone video reports its own id as video_id, so require the face to be a still to count as a pair.)
  // (Audio joins the ▶ set for the same reason a video is in it: opening the card plays something.)
  const paired = it.video_id != null && !it.is_video;   // merged still+video = a video set
  const playKind = it.is_audio ? 'Song' : ((it.is_video || paired) ? 'Video' : 'Animated');
  const motion = (it.motion || it.is_video || it.is_audio || paired)
    ? `<span class="motion-badge${it.is_audio ? ' song' : ''}" title="${playKind}"></span>` : '';
  const setBadge = (it.is_set || paired)
    ? `<span class="set-badge" title="${paired ? 'Video set — still + video (opens the video)' : 'Image set — open to keep one, recycle the rest'}"></span>` : '';
  const pattr = paired ? ` data-video-id="${it.video_id}" data-members="${it.group_members || ''}"`
    : (it.is_set ? ` data-members="${it.group_members || ''}"` : '');
  // Curation label: a colored band along the bottom edge carrying the label's NAME (absent when
  // unlabeled). The name is in the band rather than only in its `title` because colour alone can't
  // be read by everyone — see the .label-strip rule in style.css for the full reasoning. The title
  // stays: it is what a hover still offers when the band is too narrow to show the whole word.
  const label = it.label
    ? `<div class="label-strip label-${esc(it.label)}" title="${esc(labelName(it.label))}">${esc(labelName(it.label))}</div>` : '';
  // NB nothing marks a card as "unreviewed". A per-card dot was removed 2026-08-05 as carrying no
  // signal (filter by it and every card wears it; browse unfiltered and most of a library does),
  // and the sidebar filter that outlived it went the same way on 2026-08-19.
  // Offline library: the thumbnail is cached locally so the card still renders, but the file itself
  // is unreachable — mark it so the ⛶ isn't a surprise. Tagging/labelling still work (DB only).
  const off = isRootOffline(it.root_id)
    ? `<span class="offline-badge" title="${esc(libName(it.root_id))} is offline — cached thumbnail. Tag and label still work; opening, export and recycle need the library."></span>` : '';
  // Drag-to-ComfyUI (like the detail view): drag the card's image to load its workflow. ComfyUI reads
  // the actual dropped FILE, and the browser only attaches file bytes when the dragged <img> holds a
  // fully-loaded image — so on hover we swap the face to the original /file/ PNG (see gridUpgrade),
  // and the dragstart handler only lets the drag fly once that original has loaded. Skip an offline
  // card (file unreachable).
  //
  // A LONE VIDEO is draggable too, and rides this exact route rather than any of its own: it is
  // swapped to a PICTURE of its first frame carrying its workflow (/dragpng/), because a browser
  // will only carry a file between windows when the file is the one behind an <img>. Marked with
  // data-kind so gridUpgrade knows which URL to ask for; everything after that is identical.
  const loneVideo = !!it.is_video && !paired;
  const canDrag = !isRootOffline(it.root_id);
  // A song has no thumbnail to fetch and nothing to drag into ComfyUI, so its face is drawn in
  // place of the <img> — same box, same chrome, same badges around it.
  // A SONG's drag source is an invisible picture covering the card, and it has to exist because a
  // drawn card has no <img> at all — a browser will only carry a file it owns, i.e. the bytes
  // behind an image. Unlike a video's, it needs no hover swap: /dragpng/ IS the final file, so it
  // is loaded straight away and marked full on load. First in the card, because gridDraggableImg
  // takes the card's first <img>.
  const songDrag = (it.is_audio && canDrag)
    ? `<img class="song-drag" loading="lazy" draggable="true" alt="" aria-hidden="true"
           data-fn="${esc(it.filename || '')}" onload="this.dataset.full='1'"
           src="${esc(originalUrlFor(it.thumb_url || `/thumb/${it.id}?v=0&r=${it.root_id || ''}`, it.filename, 'audio'))}">`
    : '';
  const face = it.is_audio
    ? songDrag + songFaceHTML(it)
    : `<img loading="lazy" draggable="${canDrag}" data-fn="${esc(it.filename || '')}"${loneVideo ? ' data-kind="video"' : ''} src="${thumbAt(it.thumb_url, Math.max(_cardPx, _cardDrawPx))}"${off ? ONERR_OFFLINE : ''}>`;
  return `<a class="card${sel}${off ? ' card-offline' : ''}" data-id="${it.id}"${pattr} title="${esc(it.filename)}">
    <span class="check"></span>${star}
    <button class="zoom" title="Open (or double-click)" aria-label="Open"></button>
    ${face}
    <div class="card-bl">${motion}${setBadge}${off}</div>
    <div class="card-br">${reward}${it.has_note ? `<span class="note-badge" title="${esc(it.note || 'Has a note')}"></span>` : ''}</div>
    <div class="cap">${esc(it.filename || '')}</div>${cardFactsHTML(it)}${label}</a>`;
}
// ---- thumbnail size: ask for the size the picture is actually DRAWN at ------------------------
// The server can serve any of the four card sizes (see serve_thumb); asking for the right one is
// the whole point. Every card used to fetch the 512px thumbnail whatever size it was drawn at, so
// an S card pulled ~22x the bytes it could possibly show — ~116KB against ~5KB, i.e. ~13MB for a
// screenful of 117 cards instead of ~0.6MB.
//
// The bytes were never the visible cost by themselves. What they did was occupy the browser's ~6
// connections, so THE NEXT PAGE OF CARDS queued behind pictures whose detail was being thrown away.
// A trace of a 907-image folder showed 43 thumbnails still in flight after 1.2s and page fetches
// stuck 300-1400ms behind them, against 2ms in a small folder that had no backlog.
//
// devicePixelRatio, and round UP: on a scaled display a 128px card is 160 or 256 real pixels, and
// serving the CSS size there would look visibly soft. The only regression this change could
// produce that anyone would notice is a blurry thumbnail, so it errs at too many pixels, never too
// few — an upscaled thumbnail is a bug, an oversized one is just the old behaviour.
const THUMB_SIZES = [128, 192, 256, 512];
let _cardPx = 192;                       // kept in step by applyCardSize
let _stripPx = 160;                      // ...and by applyStripSize
function thumbSizeFor(cssPx) {
  const want = (cssPx || 192) * (window.devicePixelRatio || 1);
  return THUMB_SIZES.find(s => s >= want) || 512;
}
function thumbAt(url, cssPx) {
  return String(url || '').replace(/([?&])s=\d+/, `$1s=${thumbSizeFor(cssPx)}`);
}

// Any user filter/tag/search narrowing the grid (the global hide-large is NOT a user filter).
// A CONTENT filter is active — i.e. the grid shows a subset of the library. Drives the
// "matched / library" counter. The merge toggles (Video pairs / Image sets) are NOT filters:
// they change how many CARDS the same content collapses into, not which items are shown, so
// they must not flip this on (that produced a redundant "59,689 / 59,689").
//
// THE LIBRARY SCOPE IS EXCLUDED TOO, and it is the only one of the three that used to be counted.
// Two reasons, and the second is the one that settles it:
//   * this predicate is also what enables Reset (via anythingToReset), so counting a library subset
//     lit the master control for something Reset no longer clears — press it, the whole rail dims,
//     three round trips, nothing changes. A dead control;
//   * the green it drove was already a lie. The server folds the roots clause into its GLOBAL
//     exclusions, so `root_total` is scoped to the shown libraries and `total` is served straight
//     from it when no user filter is set — a subset alone renders "12,430 matched · 12,430 in
//     scope" and lights it. That is precisely the redundant "59,689 / 59,689" the paragraph above
//     says must not flip this on.
// The strip keeps its own signal: renderLibTrigger() colours the `▤ 1/4` readout beside the count.
function filtersActive() {
  return !!(state.terms.length || state.xterms.length || state.model || state.folder || state.modelFolder || state.meta || state.type ||
            state.tags.length || state.favOnly || state.hasNote ||
            state.rmin !== '' || state.rmax !== '' ||
            state.dfrom || state.dto ||
            ($('#q') && $('#q').value.trim()) || ($('#xq') && $('#xq').value.trim()));
}
// File-date (mtime) range -> Unix-second bounds at local day edges (inclusive). '' when unset.
function dateAfter() { return state.dfrom ? String(Math.floor(new Date(state.dfrom + 'T00:00:00').getTime() / 1000)) : ''; }
function dateBefore() { return state.dto ? String(Math.floor(new Date(state.dto + 'T23:59:59.999').getTime() / 1000)) : ''; }
// Reset clears exactly what a snapshot saves, which is wider than "content filters": the merge
// toggles and the SORT are both captured by a snapshot, so both must enable Reset. Deliberately
// NOT folded into filtersActive() — that drives the Filters tab badge and the green matched-count,
// and neither should react to a sort change, which narrows nothing.
function sortIsDefault() { return state.sort === 'date' && state.order === 'desc'; }
function anythingToReset() {
  return filtersActive() || state.group === false || state.sets === false || !sortIsDefault();
}
// The strip's card readout: HOW MANY CARDS ARE IN THE GRID, and nothing else. It used to show
// "matched / total", but the total is a constant you already know and restating it on every repaint
// cost half the strip's width — so the whole breakdown (both scales, and the file count that is
// never displayed) moved to the hover title:
//   filtered + merging:    179  ->  "179 matched · 1,876 cards · 3,457 files in scope"
//   unfiltered, no merge:  3,457 -> "3,457 files in scope"
// Green when a content filter is narrowing the grid. The scope readout beside it uses the same
// .filtered class, and since the library selection stopped being a filter the two answer about
// DIFFERENT things — so the shared meaning is one level up: green in this strip means "this
// readout is not showing you everything". Scope: not every library. Count: not every card in
// scope. One fact each, and neither speaks for the other.
// IS A CARD STANDING FOR MORE THAN ONE FILE ANYWHERE IN SCOPE? Read by the counter and by
// "Select all N", which must agree about it or the strip contradicts the button underneath it.
//
// The test is the two scope numbers differing, NOT the merge toggles being on. Those are on by
// default, so keying off them would label every count in a library that merges nothing — and the
// word is only worth its width when the two numbers genuinely can disagree.
function collapsingNow() {
  return state.rootTotal != null && state.rootFiles != null && state.rootFiles !== state.rootTotal;
}
function renderCount() {
  const fmt = x => (x || 0).toLocaleString();
  // THE MATCHED-CARD READOUT LEFT THIS STRIP on 2026-08-20. It restated a number the action bar
  // above the grid already carries in "Select all 3,487 cards" — the same `state.total`, rendered
  // twice — and the author's read was that it earned less than the width it cost. Its HOVER did carry
  // something nothing else showed (the card-versus-file breakdown), so that moved to the scope
  // readout, which is already the "what's in scope" control; renderLibTrigger composes it.
  //
  // NOTE THE SHAPE OF WHAT IS LEFT. This function used to open `const el = $('#count'); if (!el)
  // return;` — a guard that, once the element was deleted, would have silently stopped the two
  // held-back chips below from ever updating. They are a different job that happened to share a
  // function, and deleting an element out from under a guard is how that goes unnoticed.
  renderLibTrigger();                         // the scope readout owns the breakdown now
  // The two "held back right now" counts that used to be painted here went with the settings they
  // reported: Hide large images and the Hidden mark, both removed 2026-09-08. Nothing in Settings
  // holds anything back from the grid now.
}
// ---- filter tabs (Filters / Labels / Tags) : one pane at a time + active-count badges ----
function tabCounts() {
  const q = ($('#q') && $('#q').value.trim()) ? 1 : 0;
  const xq = ($('#xq') && $('#xq').value.trim()) ? 1 : 0;
  let filters = 0;
  if (state.terms.length || q) filters++;
  if (state.xterms.length || xq) filters++;
  if (state.model) filters++;
  if (state.folder) filters++;
  if (state.modelFolder) filters++;
  if (state.meta) filters++;
  if (state.type) filters++;
  if (state.group === false || state.sets === false) filters++;   // Sets away from default (merge both)
  if (state.rmin !== '' || state.rmax !== '') filters++;
  if (state.dfrom || state.dto) filters++;
  if (state.hasNote) filters++;
  const labels = (state.tags || []).filter(t => t.startsWith('label:')).length;
  const tags = (state.tags || []).filter(t => !t.startsWith('label:')).length + (state.favOnly ? 1 : 0);
  return { filters, labels, tags };
}
function renderTabBadges() {
  const c = tabCounts();
  document.querySelectorAll('.filter-tabs .ftab').forEach(btn => {
    const n = c[btn.dataset.tab] || 0;
    const b = btn.querySelector('.ftab-badge');
    if (!b) return;
    b.textContent = n;
    b.classList.toggle('hidden', n === 0);   // hide the badge at 0
  });
}
const ACTIVE_TAB_KEY = 'cv:activeTab';
function setActiveTab(name) {
  if (!['filters', 'labels', 'tags'].includes(name)) name = 'filters';
  const sb = $('#sidebar');
  sb.classList.remove('tab-filters', 'tab-labels', 'tab-tags');
  sb.classList.add('tab-' + name);
  document.querySelectorAll('.filter-tabs .ftab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  try { localStorage.setItem(ACTIVE_TAB_KEY, name); } catch (e) {}
  if (name === 'tags') sizeTagList();
}
// Size the Tags list so it flows down to the window bottom (offset above it is dynamic).
function sizeTagList() {
  const el = $('#taglist');
  if (!el || $('#sidebar').classList.contains('tab-tags') === false) return;
  const top = el.getBoundingClientRect().top;
  el.style.maxHeight = Math.max(120, window.innerHeight - top - 16) + 'px';
}
window.addEventListener('resize', sizeTagList);
// Green-outline the controls that are currently narrowing the grid.
function markActiveFilters() {
  const set = (el, on) => el && el.classList.toggle('active', !!on);
  set(document.querySelector('#modelDD .facet-trigger'), state.model);
  set(document.querySelector('#folderDD .facet-trigger'), state.folder);
  // (The Libraries scope is NOT set here, and no longer could be: it isn't a filter. This function
  // rings the fields a filter is set on; the scope's own readout is coloured by renderLibTrigger()
  // beside the number it describes.)
  set($('#mfolder'), state.modelFolder);
  // Sort gets the OTHER tier. It narrows nothing, so it must not take the green ring — that is the
  // same rule that keeps it out of filtersActive() (see sortIsDefault), and a green Sort would make
  // the rail claim a filter that isn't there. But a non-default sort IS doing something, so the
  // field stays drawn: .active = narrowing the grid (green edge), .set = away from its default
  // (plain skin, no ring). Before the rail went quiet there was nowhere to say this at all.
  const so = $('#sort'); if (so) so.classList.toggle('set', !sortIsDefault());
  // The dice is always drawn — it is how you REACH Random, not just how you re-deal it, and a
  // button that appears only once you have arrived cannot be the way you get there.
  set($('#searchbox'), state.terms.length || ($('#q') && $('#q').value.trim()));
  set($('#xsearchbox'), state.xterms.length || ($('#xq') && $('#xq').value.trim()));
  set($('#rmin') && $('#rmin').closest('.score-range'), state.rmin !== '' || state.rmax !== '');
  // From and To are separate rows now, so each lights on its own value rather than the pair's
  set($('#dfrom') && $('#dfrom').closest('.score-range'), state.dfrom !== '');
  set($('#dto') && $('#dto').closest('.score-range'), state.dto !== '');
  // each date row shows its own × only when that field has a value
  const dfc = $('#dfromClear'); if (dfc) dfc.classList.toggle('hidden', !state.dfrom);
  const dtc = $('#dtoClear'); if (dtc) dtc.classList.toggle('hidden', !state.dto);
  // checked tag rows are highlighted purely via CSS (:has(input:checked))
  const rb = $('#resetFilters'); if (rb) rb.disabled = !anythingToReset();   // grey out "Reset" when nothing differs from defaults
  renderTabBadges();    // keep the per-tab active-count badges in sync
  renderSnapshotRow();  // every filter change lands here, so this is the single hook that keeps the
}                       // drift dot honest — no per-control wiring

let _searchSeq = 0;   // supersede token: a filter change invalidates any in-flight/older request
// The /api/search query for the current filters at a given offset. Shared by search() and the
// auto-refresh prepend so the two can never drift out of sync.
function searchParams(offset, limit) {
  return new URLSearchParams({
    q: queryString(), x: excludeString(), model: state.model, folder: state.folder, mfolder: state.modelFolder, meta: state.meta,
    type: state.type, group: state.group ? '1' : '', sets: state.sets ? '1' : '', tags: state.tags.join(','), fav: state.favOnly ? '1' : '', note: state.hasNote ? '1' : '', roots: rootsParam(),
    rmin: state.rmin, rmax: state.rmax, after: dateAfter(), before: dateBefore(),
    sort: state.sort, order: state.order, limit: (limit || state.limit), offset: offset,
    // Only meaningful to sort=random, and harmless otherwise. It MUST be identical for every page
    // of one shuffle — that is the whole reason a seed exists rather than ORDER BY RANDOM() — so it
    // is read from state here and re-dealt only by reshuffle() / a new search.
    seed: state.seed,
  });
}
// A new deal. Not persisted and deliberately not part of a snapshot: a snapshot restores which sort
// you were in, not which shuffle — asking for Random again should give you something new.
function reshuffle() { state.seed = 1 + Math.floor(Math.random() * 999999); }
// Auto-refresh's non-disruptive grid update: fetch the newest page and fold in only genuinely-new
// files, prepending them at the top rather than clearing the grid (which search(true) does, resetting
// scroll). Only prepends when the newest-first sort is active AND the user is at the top; otherwise it
// just keeps the counts honest and lets the next top-of-page idle tick fold them in — no scroll yank.
async function prependNewImages() {
  const data = await getJSON('/api/search?' + searchParams(0).toString());
  state.total = data.total;
  state.rootTotal = (data.root_total != null ? data.root_total : data.total);
  state.rootFiles = (data.root_files != null ? data.root_files : null);
  renderCount();
  // The bar tracks the COUNT, not just the selection -- "Select all 1,204" is a number this
  // function has just changed. It went unpainted here for as long as that was only a stale label;
  // it stopped being cosmetic when an empty result set started dimming the whole bar, because then
  // cards arriving found the bar still idle and nothing repainted it until the next filter change.
  updateSelBar();
  const have = new Map(state.items.map(x => [Number(x.id), x]));
  const fresh = [];
  let folded = 0;
  // A group's OTHER members stop being cards of their own the moment it collapses. If one of them
  // was already on the grid — the video arrived first and sat there alone for a tick — its card has
  // to go, or the run shows twice: once merged and once as the leftover half.
  const dropFoldedMembers = (memberIds, keepId) => {
    for (const m of memberIds) {
      if (m === keepId) continue;
      const el = $(`.card[data-id="${m}"]`);
      if (el) el.remove();
      const idx = state.items.findIndex(x => Number(x.id) === m);
      if (idx >= 0) { state.items.splice(idx, 1); folded++; }
    }
  };
  for (const it of data.items) {
    const memberIds = it.group_members ? String(it.group_members).split(',').map(Number) : [];
    const cur = have.get(Number(it.id));
    if (cur) {
      // ALREADY CARDED IS NOT THE SAME AS UNCHANGED, and assuming it was is the whole bug the author
      // reported on 2026-08-24: "the card would appear, but not the set with the video".
      //
      // A run writes its still and its video moments apart. If a background tick lands between the
      // two, the still is indexed alone and gets a lone card. When the video arrives, the server
      // returns the SAME row id — now fronting a merged pair — and this loop used to skip it as
      // one it already had. So the card stayed a lone still until something forced a full search,
      // which is exactly why restarting the app "fixed" it.
      //
      // A whole-item compare rather than a list of fields: the shape can change in more ways than
      // pairing (a video's dimensions backfill when its poster frame is made, a set gains a member,
      // a score lands), and enumerating them is a list that would go stale. Cost is a couple of
      // stringifies per card on an idle tick, and the compare means the DOM is touched only when
      // something really moved.
      if (JSON.stringify(cur) !== JSON.stringify(it)) {
        const el = $(`.card[data-id="${it.id}"]`);
        if (el) el.outerHTML = cardHTML(it);
        const idx = state.items.findIndex(x => Number(x.id) === Number(it.id));
        if (idx >= 0) state.items[idx] = it;
        refreshStripItem(it.id);   // the strip draws the same marks and is keyed on the LIST, not its contents
      }
      dropFoldedMembers(memberIds, Number(it.id));
      continue;
    }
    // A new file may fold into a group whose representative card is already shown — refresh that
    // card in place (keeps its members/badges current) instead of adding a duplicate.
    const repEl = memberIds.map(m => $(`.card[data-id="${m}"]`)).find(Boolean);
    if (repEl) {
      // The group's representative may have changed id; keep both DOM and state.items in sync so
      // selection/recycle/detail-nav (which look cards up by id in state.items) stay correct.
      const oldId = Number(repEl.getAttribute('data-id'));
      repEl.outerHTML = cardHTML(it);
      const idx = state.items.findIndex(x => Number(x.id) === oldId);
      if (idx >= 0) state.items[idx] = it;
      refreshStripItem(it.id, oldId);   // same in-place swap, same stale strip (see recycleSetOthers)
      dropFoldedMembers(memberIds, Number(it.id));
      continue;
    }
    fresh.push(it);
  }
  // Two cards becoming one moves the paging window back by one, the mirror of the += below.
  if (folded) state.offset = Math.max(0, state.offset - folded);
  if (!fresh.length) { _deferredNew = false; return; }
  // Only prepend seamlessly when newest-first and parked at the top; else defer (don't yank) and
  // remember, so the next idle tick folds them in once the user is back at the top.
  // AN OPEN IMAGE DEFERS, exactly like being scrolled down. The scan itself has already run by the
  // time we get here — the library is indexed and the counts are updated above — and this is only
  // the moment the cards would join the grid. Doing that under an open detail view would move the
  // three things it is standing on: the filmstrip IS the result set, `state.index` is a position in
  // it, and on newest-first the new cards land at the front, which is the worst case for both. The author
  // settled it 2026-08-18: **lock the detail view, let the library keep up.** So the strip and the
  // arrows stay exactly as they were, and closing folds the new cards in.
  // THE THREE DEFERRALS ARE TRACED INDIVIDUALLY, because this is the end of the road where "it
  // didn't update" is most often true and correct: the files ARE indexed, the scan DID run, and the
  // cards are deliberately not being shown yet. From the grid that is indistinguishable from a
  // refresh that never happened, and the fix for each is different — close the image, scroll up, or
  // change the sort.
  if (!$('#overlay').classList.contains('hidden')) {
    _deferredNew = true;
    Trace.add('refresh', `${fresh.length} new card(s) held back — an image is open`);
    return;
  }
  if (state.sort !== 'date' || state.order !== 'desc' || window.scrollY > 4) {
    _deferredNew = true;
    Trace.add('refresh', `${fresh.length} new card(s) held back — `
      + (window.scrollY > 4 ? `scrolled ${Math.round(window.scrollY)}px down`
                            : `sort is ${state.sort} ${state.order}, not newest-first`));
    return;
  }
  // BELOW the three deferral returns above, deliberately: a deferred prepend leaves the grid as it
  // found it, so a panel standing over an empty result set must still be standing. Only the branch
  // that actually inserts gets to contradict it.
  clearEmptyHint();
  $('#grid').insertAdjacentHTML('afterbegin', fresh.map(cardHTML).join(''));
  syncThumbsToCardWidth();      // cards are drawn stretched; match the thumbnails to that width
  state.items = fresh.concat(state.items);
  state.offset += fresh.length;                             // keep the pagination window consistent
  Trace.add('refresh', `${fresh.length} new card(s) added to the top of the grid`);
  _deferredNew = false;
}
// ---- Busy: ONE owner for every "the app is working" indicator ---------------------------------
// Written after four bugs in a row turned out to be the same bug: an indicator switched off by
// something other than what switched it on. Previously these four overlays were toggled from
// fourteen places, with the precedence rule re-derived at seven of them.
//
// TIERS, loudest first. Exactly one is ever visible; a louder one silences the ones below it, and
// they come BACK when it lifts rather than being lost:
//   job   a full-screen block for cancellable work (a scan) — the grid is stale, browsing it is moot
//   rail  the SIDEBAR ITSELF is being rewritten (boot, Reset, a snapshot, a library switch)
//   pane  the results are being replaced (any filter change)
//   more  another page is loading as you scroll
// `rail` implies `pane`: a whole-rail operation always replaces the results too, and that invariant
// lives HERE, in the paint, rather than one setter reaching across the file into another. Same for
// "a job hides the pane dim" — which used to be a one-way door, because raising the job scrim
// LOWERED the pane one outright and nothing put it back.
//
// OWNERSHIP. raise() hands back a token and only that token can release. Releasing with a stale
// token is a silent no-op, which is exactly right for an operation that has been superseded: the
// newer owner keeps the screen. This is the rule the whole module exists for.
//
// GUARANTEED RELEASE. Prefer Busy.during(tier, fn) — it releases in a `finally`, so a throw can't
// strand a scrim. Hand-paired raise/release is what left the results pane dimmed and click-blocked
// on several paths.
//
// The SCRIM is immediate (the pause should be felt at once); the SPINNER is delayed, because most
// queries answer inside PANE_BUSY_DELAY_MS and a spinner that flashes on every filter click is worse
// than none. 400ms, not the 180ms this used when the spinner was the ONLY signal: the scrim now
// announces the pause instantly, so the spinner is an escalation — "still working".
const PANE_BUSY_DELAY_MS = 400;
// Shorter than the pane's 400ms, deliberately. The pane already shows a scrim the instant you act,
// so its spinner is a second-stage "still working"; this one is the ONLY signal pagination has, so
// waiting 400ms would leave a genuinely slow page looking like nothing was happening. 250ms clears
// the ~135ms page that caused the strobe while still catching one that stalls.
const MORE_BUSY_DELAY_MS = 250;
const Busy = (() => {
  const owner = { job: 0, rail: 0, pane: 0, more: 0 };
  let seq = 0, paneT = null, paneShown = false, moreT = null, moreShown = false;
  const held = t => !!owner[t];
  // Lazily built overlays: #gridBusy and #moreBusy are in the markup, the other two are made once.
  function lazy(id, cls, parent) {
    let ov = document.getElementById(id);
    if (!ov && parent) {
      ov = document.createElement('div');
      ov.id = id; ov.className = cls + ' hidden';
      parent.appendChild(ov);
    }
    return ov;
  }
  const el = {
    job:  () => lazy('taskScrim', 'task-scrim', document.body),
    rail: () => lazy('sidebarBusy', 'sidebar-busy', $('#sidebar')),   // blur + block; no spinner of its own
    pane: () => $('#gridBusy'),
    more: () => $('#moreBusy'),
  };
  // The ONLY place that decides what is on screen. Everything else just changes who owns what.
  function paint() {
    const job  = held('job');
    const rail = held('rail');
    const pane = (held('pane') || rail) && !job;
    const more = held('more') && !pane && !job;
    const j = el.job(), r = el.rail(), m = el.more(), p = el.pane();
    if (j) j.classList.toggle('hidden', !job);
    if (r) r.classList.toggle('hidden', !rail);
    // The pagination spinner is DELAYED, for the same reason the pane's is (see PANE_BUSY_DELAY_MS)
    // and it took a real defect to notice the rule applies here too. This spinner is an IN-FLOW
    // element: showing it makes the document ~48px taller and hiding it shrinks it back. That was
    // invisible while a page took ~1s — one appearance, held, gone. Once pages dropped to ~135ms
    // (see api_search's want_counts) the same code strobed the page height every 150ms while
    // scrolling, which reads as the cards themselves jittering. Measured from a trace:
    // raise 81.40 → lower 81.62 → raise 81.73 → lower 82.74 → raise 84.95 → lower 85.10.
    // Delayed, a page that answers quickly never shows a spinner at all — which is the honest
    // signal, since nothing was ever kept waiting.
    if (m && more !== moreShown) {
      clearTimeout(moreT);
      if (more) moreT = setTimeout(() => {
        m.classList.remove('hidden');
        // The trace records OWNERSHIP on raise/lower; this is the only line that means the spinner
        // was actually on screen, which is the half that can shift the layout.
        Trace.add('indicator', 'more spinner shown — this page was slow enough to say so');
      }, MORE_BUSY_DELAY_MS);
      else m.classList.add('hidden');
      moreShown = more;
    }
    if (p && pane !== paneShown) {
      // Edge-triggered, which is what makes a second raise leave the pending spinner delay ALONE —
      // previously a hand-written early-return whose ordering was load-bearing and easy to break.
      clearTimeout(paneT);
      if (pane) {
        p.classList.remove('hidden');
        paneT = setTimeout(() => p.classList.add('slow'), PANE_BUSY_DELAY_MS);
      } else {
        p.classList.add('hidden');
        p.classList.remove('slow');   // fades out with the scrim; both transitions are 0.18s
      }
      paneShown = pane;
    }
  }
  return {
    raise(tier) {
      owner[tier] = ++seq; paint();
      // What the user can SEE, which is the half a network log can't explain: `more` is the little
      // spinner under the grid, `pane` the dim over the cards, `job`/`rail` the blocking scrims.
      Trace.add('indicator', `${tier} raised` +
        (tier === 'more' ? ` (spinner under the grid, deferred ${MORE_BUSY_DELAY_MS}ms)` : ''));
      return owner[tier];
    },
    // A token is REQUIRED. There is no force-release: every caller holds what it raised, which is
    // the whole point of the module and the one rule all four of this arc's bugs broke. Releasing
    // with a stale or absent token is a silent no-op — correct for a superseded operation.
    release(tier, token) {
      if (!token || owner[tier] !== token) return;
      owner[tier] = 0; paint();
      Trace.add('indicator', `${tier} lowered`);
    },
    async during(tier, fn) {
      const t = this.raise(tier);
      try { return await fn(); } finally { this.release(tier, t); }
    },
    held,
  };
})();
// A refresh scheduled by a FINISHING job must not raise the WORKING dim. The job already owned the
// screen, and a fresh dim the instant its scrim lifts reads as a second, unexplained pause — the grid
// visibly went clear, dim, clear. The raise runs synchronously inside search()'s reset branch, so
// wrapping the call is enough; no async plumbing needed. Only the NON-blocking jobs need this now
// (Quality scoring): a blocking one still owns the `job` tier while it refreshes, and the
// tier rule silences `pane` underneath it without anyone asking.
let _suppressPaneBusy = false;
function refreshAfterJob(fn) {
  _suppressPaneBusy = true;
  try { return fn(); } finally { _suppressPaneBusy = false; }
}
// Legacy shims. Every existing call site keeps working; they force-release, which is the thing the
// remaining migration removes one caller at a time.
// THE RUNWAY: how far past the fold the sentinel must sit before pagination stops asking for more.
// One constant because THREE things have to agree about it — the first page's size, every later
// page's size, and the top-up test itself. They did not, and the disagreement was structural: a
// 30-card page adds ~400px at the author's card size, the test demands 600px, so EVERY page failed the
// check that immediately followed it and fetched again. His trace says so on nearly every page —
// "sentinel 402px past the fold (needs 600) — fetching another page". Every screenful cost two
// round trips, forever, by construction.
const TOPUP_GAP_PX = 600;
const MIN_PAGE = 30;

// Grid geometry: the column count and the height of one row. Shared, because the whole fault above
// was two callers computing the same thing to different answers.
function gridGeom() {
  const g = $('#grid');
  const cell = _cardPx + 2 + 4;   // image box + the card's 1px border each side + the grid's 4px min gutter
  return { cell, cols: Math.max(1, Math.floor((((g && g.clientWidth) || window.innerWidth) + 4) / cell)) };
}
// How many cards it takes to COVER THE SCREEN right now. The FIRST page is its own question: it
// lands behind the blocking scrim, where one bigger insert costs nothing visible and a SECOND page
// costs everything — it arrives after the dim has lifted and reads as the view rebuilding itself.
// Derived at call time from the live window and the live card size (the user changes both).
function firstPageSize() {
  const { cell, cols } = gridGeom();
  const rows = Math.ceil((window.innerHeight + TOPUP_GAP_PX) / cell) + 1;
  return Math.max(MIN_PAGE, Math.min(400, cols * rows));   // server caps the page at 500
}
// How many cards a SCROLL page should carry: enough to clear the runway on its own, plus a row.
// The old answer was a flat 30, justified in a comment as "a big page is inserted and laid out
// synchronously and stalls the scroll visibly". That was written before anything measured it, and
// it is false: the trace puts a 30-card insert at 1ms and a 117-card insert at 2-3ms, with the main
// thread never blocked for a single frame across whole sessions. The cost that was real is the one
// it caused — a second round trip for every screenful.
function nextPageSize() {
  const { cell, cols } = gridGeom();
  const rows = Math.ceil(TOPUP_GAP_PX / cell) + 1;
  return Math.max(MIN_PAGE, Math.min(200, cols * rows));
}
// opts.facets === false: the caller loads the facets itself (every whole-rail caller does), so
// skip the fire-and-forget one below. Reset used to fire BOTH — same params, one awaited by the
// caller and one not — and the unawaited answer landed after the grid swap, repainting the whole rail
// a second time.
async function search(reset, opts) {
  // Pagination (scroll) waits while a load is in flight; a filter change (reset) always supersedes.
  if (state.loading && !reset) return;
  // ONLY a reset claims the supersede token. A paginating search APPENDS — it has no business
  // invalidating a reset that is still settling. Bumping it unconditionally handed it ownership: the
  // reset's own `seq === _searchSeq` checks then failed and it never lowered the scrim it had raised,
  // leaving the pane blocked. (And since the bump came before the early returns below, a pagination
  // call that did nothing at all could still strand one.) A non-reset is still DROPPED by the
  // post-fetch check when a real reset supersedes it — that half is what the token is for.
  const seq = reset ? ++_searchSeq : _searchSeq;
  Trace.add('search', `${reset ? 'RESET (filter/sort change)' : 'page ' + state.offset} · sort=${state.sort} ${state.order}`
    + (reset ? '' : ` · asking for ${nextPageSize()}`));
  let paneTok = 0, moreTok = 0;
  if (reset) {
    saveFilters();     // persist this root's filters on every filter change (the common funnel)
    // Paint the controls from state NOW, not after the round trip. Every field in the rail is quiet
    // until it's doing something, so waiting on the fetch leaves the field you just set looking
    // UNSET for the length of the query — and a fetch that throws would leave it that way for good.
    // Pure synchronous DOM work off state, and idempotent, so the post-fetch call below still runs:
    // that one settles what only the response can (the count, the badges).
    markActiveFilters();
    state.offset = 0; state.done = false;
    // Reactive facets: the model/folder lists for this filter -- AND the label, tag and favourite
    // counts beside them, which became filter-aware on 2026-09-14 and so have to be re-asked here
    // like everything else. They rode the old boot-only path until then, which is exactly why they
    // could sit at a whole-library total while the grid showed one card.
    if (!opts || opts.facets !== false) { loadFacets(); loadTags(); }
    // The dim belongs to THIS search: hold its token and only it can lower it. A newer search
    // raising its own makes this one's token stale, and a stale release is a silent no-op — which is
    // the right answer for a superseded search and used to need a hand-written seq check at each of
    // three lowering sites. (_suppressPaneBusy: a finishing job already owns the screen.)
    paneTok = _suppressPaneBusy ? 0 : Busy.raise('pane');   // dim the CURRENT cards; they stay put
    probeChanges();    // fire-and-forget: has anything landed on disk? (throttled; never awaited)
  }
  // Paging past the end. Nothing to lower: a non-reset never RAISES the dim, and lowering one it
  // doesn't own is how a scroll used to un-dim a reset that was still working. (Unreachable on a
  // reset anyway — state.done was just cleared above.)
  if (state.done) return;
  state.loading = true;
  if (!reset) moreTok = Busy.raise('more');   // QUIET: appending, so just a small marker
  const limit = reset ? firstPageSize() : nextPageSize();
  // The thumbnail size is on this line because it is NOT a constant: it falls out of the card size
  // and the display's scaling together, and there is a real combination (an M card on a 1.5x
  // display, needing 288px) where it lands back on 512 and the bytes are unchanged. Without this
  // recorded, a trace showing no improvement is ambiguous between "the change did nothing" and
  // "the change did not apply here", which is exactly the ambiguity a trace exists to remove.
  if (reset) Trace.add('pagesize', `asked for ${limit} cards to cover ${window.innerWidth}x${window.innerHeight}`
    + ` · thumbs ${thumbSizeFor(_cardPx)}px (cards ${_cardPx}px @${window.devicePixelRatio || 1}x)`);
  try {
    const data = await getJSON('/api/search?' + searchParams(state.offset, limit).toString());
    // A superseded search is INDISTINGUISHABLE from a fast one unless the trace says so — and
    // reading an abandoned request as a completed one is exactly how a trace lies.
    if (seq !== _searchSeq) { Trace.add('search', 'superseded — results dropped'); return; }
    if (reset) {
      // THE SWAP POINT. Everything the user sees changes in one frame: old cards out, new cards in,
      // selection cleared, count updated. Doing any of it before the await is what made a filter
      // change read as "nothing matched" — and left the header count stale over an empty grid.
      $('#grid').innerHTML = '';
      state.items = [];            // kept in step with the DOM, not emptied ahead of it
      clearSelection();            // a new query/filter is a fresh result set
      window.scrollTo(0, 0);       // the grid used to collapse to the top by being emptied early
    }
    // A PAGING response carries no totals — they cost four library-wide counts and cannot have
    // changed since the page that opened this view (see api_search). `in`, not truthiness: 0 is a
    // real total, and treating it as "absent" would leave the counter showing the previous view's.
    if ('total' in data) {
      state.total = data.total;
      state.rootTotal = (data.root_total != null ? data.root_total : data.total);
      state.rootFiles = (data.root_files != null ? data.root_files : null);
      renderCount();
      markActiveFilters();
      // The bar is on screen the whole time now, so its "Select all N" has to track the count
      // rather than being written the first time something is selected.
      updateSelBar();
    }
    // NO LIBRARY, NO GRID -- the welcome belongs to a STATE, not to one code path. It used to be
    // painted only by boot() and by the cancel path, so any repaint that happened afterwards wiped
    // it: cancelling a first index left a blank screen, because the caller's own refresh ran after
    // the cancel had drawn the hint. Deciding it here, where the grid is actually built, means every
    // route into "there are no libraries" ends up looking the same without anyone sequencing it.
    // No early return: the settle sequence below (dim release, top-up, fade) still has to run, and
    // with no libraries there are no cards to insert -- the append below is a no-op over the hint.
    if (reset && !(state.roots || []).length) showNoRootHint();
    // THE OTHER EMPTY STATE, and the `else` is what guarantees a fresh install never gets both:
    // with no libraries there are also no items, so an independent `if` would paint "no cards
    // match" over the welcome. One chain, and the more specific state wins -- you cannot have
    // filtered your way to nothing before you have added anything to filter.
    //
    // `data.items`, not `state.total`: the panel is a statement about what is IN THE GRID, so it is
    // decided by what is going into the grid. They agree in every normal case, and where they
    // disagree the items are the ones telling the truth about the screen being looked at.
    else if (reset && !data.items.length) showNoMatchHint();
    state.items = state.items.concat(data.items);
    const tBuild = Trace.start('cards', `${reset ? 'rebuilt grid with' : 'appended'} ${data.items.length} cards`);
    $('#grid').insertAdjacentHTML('beforeend', data.items.map(cardHTML).join(''));
    // AFTER THE FIRST PAINT, not only on resize: a card's drawn width is not known until there is a
    // card to measure, so the very first page would otherwise keep the thumbnail its unstretched
    // size asked for. This also fixes _cardDrawPx for every page appended after it.
    syncThumbsToCardWidth();
    tBuild(`${state.items.length} on screen of ${state.total}`);
    if (!reset) ScrollMetric.notePage();   // a page landing mid-scroll is the likeliest block
    if (reset) {
      // Play the fade-up now the cards are in the DOM. remove -> reflow -> add is the standard way
      // to RESTART a CSS animation; without the reflow the browser coalesces it and the animation
      // never replays on the second and later swaps.
      const g = $('#grid');
      g.classList.remove('swap-in');
      void g.offsetWidth;
      g.classList.add('swap-in');
    }
    state.offset += data.items.length;
    // state.total, not data.total: a paging response no longer carries one, and `>= undefined` is
    // always false — which would leave "have we reached the end?" resting on the short-page test alone.
    if (data.items.length < limit || state.offset >= state.total) state.done = true;
  } catch (e) {
    // A failed fetch must never leave the pane dimmed AND click-blocked. This lives in a catch rather
    // than the finally because the settle sequence below (top-up, wait for pictures, fade) is skipped
    // on a throw — deliberately, or a dead server would have each retry trigger the next.
    Busy.release('pane', paneTok);
    throw e;
  } finally {
    // Guarded by seq: a superseded search must not clear the scrim or the loading flag out from
    // under the newer one that owns them. The finally also means a failed fetch can't strand
    // either — previously an error left state.loading true and blocked further pagination.
    if (seq === _searchSeq) state.loading = false;
    Busy.release('more', moreTok);   // ours or nothing; no seq check needed
  }
  if (seq !== _searchSeq) return;
  // The whole settle, in order, under ONE dim. Only a reset gets here with the scrim up, and only a
  // reset lowers it — so the sequence is: fill the screen, wait for those pictures to decode, fade.
  // Anything that lands after the fade reads as the view rebuilding itself a second time, which is
  // the bug this ordering exists to prevent.
  await topUpIfShort();   // normally a no-op after a reset: firstPageSize() already covered the screen
  if (seq !== _searchSeq) return;
  if (reset) {
    // Hold the dim until the first screenful of thumbnails has actually arrived, so the pane
    // reveals PICTURES rather than empty card frames that fill in a moment later. Images are
    // loading="lazy", so only the ones near the viewport ever load — waiting on the rest would
    // wait forever. Capped, because this scrim also blocks clicks: a slow or broken thumbnail
    // must never be able to strand it.
    await firstScreenReady();
    Busy.release('pane', paneTok);   // ours or nothing, even if a newer search started while waiting
  }
}
// The IntersectionObserver reports TRANSITIONS, not a standing state — so if a page lands and the
// sentinel is STILL in view, no further callback ever comes and pagination dies silently. That is
// exactly what happened on a cold start: the first callback arrives while the grid is empty (and is
// ignored, since there is nothing to paginate from yet), and if one page doesn't fill the viewport
// plus the observer's 600px margin — a wide window, small cards, or simply a small page — the
// sentinel never leaves view, so nothing re-triggers. Changing the grid size relaid it out and
// jolted it back to life, which is what made the bug look mysterious.
// So: after every page, if the sentinel is still within reach, fetch the next one. Bounded by
// state.done, and by state.loading via search()'s own guard.
// Awaited, not fired and forgotten: on a reset this is what "the view is ready" means, so the scrim
// must not come down until the chain has settled. Each page's own tail calls it again, so one await
// covers however many pages the window needs — bounded by state.done and by the sentinel moving out
// of reach, exactly as before.
async function topUpIfShort() {
  if (state.done || state.loading) return;
  const s = $('#sentinel'); if (!s) return;
  const gap = Math.round(s.getBoundingClientRect().top - window.innerHeight);
  if (gap < TOPUP_GAP_PX) {
    // The one the sort-flip report points at: after a reset this is meant to be a no-op, because
    // firstPageSize() already covered the window. If it fires here, the first page was too small.
    Trace.add('top-up', `sentinel ${gap}px past the fold (needs ${TOPUP_GAP_PX}) — fetching another page`);
    await search(false);
  }
}
const FIRST_PAINT_MAX_MS = 1200;
function firstScreenReady() {
  const near = [...document.querySelectorAll('#grid img')].filter(im => {
    const r = im.getBoundingClientRect();
    return r.top < window.innerHeight + 200 && r.bottom > -200;
  });
  const pending = near.filter(im => !im.complete);
  // `complete` already true = the bytes were in hand before we looked, i.e. cache. The split is the
  // point: a screenful that is entirely cached and STILL slow means the delay is not the pictures.
  Trace.add('pictures', `${near.length} on the first screen · ${near.length - pending.length} already in hand · waiting for ${pending.length}`);
  if (!pending.length) return Promise.resolve();
  const t = performance.now();
  return new Promise(res => {
    let left = pending.length, capped = true;
    const finish = () => { capped = false; Trace.add('pictures', `arrived · ${Math.round(performance.now() - t)}ms`); res(); };
    const done = () => { if (--left <= 0) finish(); };
    pending.forEach(im => {
      im.addEventListener('load', done, { once: true });
      im.addEventListener('error', done, { once: true });   // a broken thumb still counts as settled
    });
    setTimeout(() => {
      // Hitting the cap is itself a finding: the dim held for the full 1.2s and the view then
      // revealed pictures that were still arriving — which is what "it repaints afterwards" is.
      if (capped) Trace.add('pictures', `GAVE UP after ${FIRST_PAINT_MAX_MS}ms with ${left} still loading`);
      res();
    }, FIRST_PAINT_MAX_MS);
  });
}

// ---- selection ----
function setCardSelected(id, on) {
  const el = document.querySelector(`.card[data-id="${id}"]`);
  if (el) el.classList.toggle('selected', on);
}
// The bar is ALWAYS drawn; only its buttons go quiet. This is deliberately the opposite of the
// sidebar rail's "a control is drawn when it's doing something" rule, and the two coexist because
// they answer different questions: an idle filter is noise among thirty controls you already know
// about, while this bar is the ONLY thing telling you selection mode exists. Hiding it meant
// discovering multi-select by accident — and it also shoved the grid down the moment you ticked a
// card, because the bar is in normal flow.
//
// **The real reason, in the author's words once he had used it:** "the app is a tool, and you instantly
// get a better sense of that when the top bar is visible from the start." That is a stronger claim
// than discoverability — a visible toolbar states what KIND of thing this is. A viewer is something
// you look at; a tool is something you do things with, and the row of verbs across the top is what
// says which. Worth keeping in mind before hiding any other control that names an action.
//
// Idle buttons are dimmed with aria-disabled, NOT the `disabled` attribute, and that is the point
// rather than a detail: a disabled button doesn't reliably show its tooltip, and the tooltips are
// what make an always-visible bar teach anything. Clicks are stopped by one capture-phase guard
// (see below) instead of a check inside every handler.
function updateSelBar() {
  const n = selection.size;
  $('#selCount').textContent = n.toLocaleString() + ' selected';
  const total = Number.isFinite(state.total) ? state.total : null;
  // "Select all 32 CARDS" while anything is merging. The number is cards and always has been, but
  // the sidebar's facet counts are FILES, so the two disagree by design and read as a bug: pick a
  // model type showing 44 and the button offers 32, because 12 of those files share cards with
  // each other. Naming the unit is the whole fix — the author's call, 2026-08-20, after being confused
  // by it more than once. The counts themselves are correct and stay as they are.
  //
  // It also pre-explains the other jump: pressing "Select all 32 cards" reports "56 selected",
  // since a card hands over every file behind it.
  const unit = collapsingNow() ? ' cards' : '';
  // A COUNT OF ZERO IS NOT A COUNT. "Select all 0 cards" names an act that cannot happen, and it
  // sat there as a live offer above an empty grid -- The author's screenshot, 2026-09-14. It falls back
  // to the same plain label the not-yet-counted case uses, because they are the same sentence: the
  // button has no number worth showing.
  $('#selAll').textContent = !total ? 'Select all'
    : `Select all ${total.toLocaleString()}${unit}`;
  // Select all is the way IN to selection mode and works perfectly well on an empty selection, so
  // it stays live throughout; everything else needs something selected to act on. The ONE exception
  // is an empty result set -- there is nothing for it to be the way in to, so it goes idle with the
  // rest.
  //
  // THE TWO TESTS DIFFER ON PURPOSE, and this is the line a later tidy-up will try to unify:
  // `!total` for the label, `total === 0` for the dim. `null` means NOT COUNTED YET, before the
  // first response has landed -- dimming the whole bar on a count that has not arrived would flick
  // it live again a moment later.
  for (const b of $('#selbar').querySelectorAll('button')) {
    const needsSelection = b.id !== 'selAll';
    b.setAttribute('aria-disabled', String(total === 0 || (needsSelection && n === 0)));
  }
}
function toggleSelect(card, shift) {
  const id = card.dataset.id;
  const idx = state.items.findIndex(x => String(x.id) === id);
  if (shift && lastSelIndex !== null && idx !== -1) {
    const a = Math.min(lastSelIndex, idx), b = Math.max(lastSelIndex, idx);
    for (let i = a; i <= b; i++) {
      const iid = String(state.items[i].id);
      selection.add(iid); setCardSelected(iid, true);
    }
  } else {
    if (selection.has(id)) { selection.delete(id); setCardSelected(id, false); }
    else { selection.add(id); setCardSelected(id, true); }
    lastSelIndex = idx;
  }
  updateSelBar();
}
async function selectAllMatching() {
  const p = new URLSearchParams({ q: queryString(), x: excludeString(), model: state.model, folder: state.folder, mfolder: state.modelFolder,
    meta: state.meta, type: state.type, group: state.group ? '1' : '', sets: state.sets ? '1' : '', tags: state.tags.join(','), fav: state.favOnly ? '1' : '', note: state.hasNote ? '1' : '', roots: rootsParam(), rmin: state.rmin, rmax: state.rmax, after: dateAfter(), before: dateBefore(),
  });
  const data = await getJSON('/api/ids?' + p.toString());
  selection.clear();
  data.ids.forEach(id => selection.add(String(id)));
  document.querySelectorAll('.card').forEach(el => el.classList.toggle('selected', selection.has(el.dataset.id)));
  updateSelBar();
}
function clearSelection() {
  selection.clear();
  lastSelIndex = null;
  document.querySelectorAll('.card.selected').forEach(el => el.classList.remove('selected'));
  updateSelBar();
}
function toast(msg) {
  const st = $('#status');
  Undo.release();                        // whoever claims the pill owns it; an offer cannot outlive it
  $('#stopScan').style.display = 'none';
  $('#undoDelete').style.display = 'none';   // unconditional, like Stop: release() no-ops if idle
  st.classList.remove('has-progress');   // a message is not a job; it has nothing to fill a bar with
  $('#statusText').textContent = msg;
  st.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => st.classList.remove('show'), 3500);
}
// A progress watcher taking over #status MUST call this first. toast() and every watcher share the
// one status pill, and toast's 3.5s auto-hide was only ever cleared by toast itself — so a toast
// fired just before a job (refreshChanged does exactly that: "Checking for changes…" then a scan)
// would, 3.5s in, strip .show and take the progress bar AND the Stop button with it, leaving the
// user staring at a full-screen scrim with no way out until the job finished on its own.
function claimStatus() { clearTimeout(toast._t); toast._t = null; }

// ---- Undo a recycle ---------------------------------------------------------------------------
// A recycle is QUEUED server-side, not performed: nothing moves for its window, and Undo cancels
// the job rather than restoring anything. So this module's only job is to hold the offer open and
// put the CARDS back — the files and their rows never left.
//
// It owns #status the way Job does, and for the same reason: toast()'s 3.5s auto-hide is cleared
// only by toast itself, so an offer built on toast() would lose its own button a third of the way
// through the window. It claims the pill, and releases the moment anything else claims it — an
// undo you cannot see is not an undo, so the offer dies with its affordance rather than lingering
// invisibly on a keystroke.
const Undo = (() => {
  let cur = null;                        // {batch, restore, label, tick, expire, until}
  const btn = () => $('#undoDelete');
  // The countdown is TEXT, not the progress trough. The trough would make this read as a job in
  // progress, and nothing is progressing — the app is waiting to see if you meant it. Seconds also
  // say the one thing a draining bar cannot: how long is left, in a unit you can act on.
  function paint() {
    if (!cur) return;
    const left = cur.until - Date.now();
    if (left <= 0) { release(true); return; }
    $('#statusText').textContent = `${cur.label} · ${Math.ceil(left / 1000)}s`;
  }
  function release(expired) {
    if (!cur) return;
    clearInterval(cur.tick); clearTimeout(cur.expire);
    const c = cur; cur = null;
    btn().style.display = 'none';
    if (expired) {
      $('#status').classList.remove('show');
      // The recycle has happened by now, and with it deferred a FAILURE arrives after the cards
      // already left the grid. Ask once, and only say anything if there is something to say.
      fetch('/api/delete/result?batch=' + c.batch).then(r => r.json()).then(j => {
        if (j && j.failed && j.failed.length)
          uiAlert(`${j.failed.length} could not be recycled: ` +
                  (j.failed[0].error || 'unknown error'));
      }).catch(() => {});
    }
  }
  return {
    get active() { return !!cur; },
    // `restore` puts the cards back exactly where they were. Deliberately a closure captured at
    // removal time rather than a re-search: search(true) jumps to the top of the grid, which is
    // the one thing a mid-cull undo must not do.
    //
    // THERE IS ONLY EVER ONE PENDING BATCH. A second recycle commits the first at once (see
    // commitNow) rather than stacking timers or resetting a shared countdown. Blocking the second
    // delete was considered and rejected: it would lock the grid for the window after every press,
    // which fights the fast culling this whole feature exists to make safe. And nothing is lost by
    // committing early — the older batch stopped being undoable the moment you acted again, so the
    // only difference is that its files reach the bin sooner and one timer dies instead of ten.
    offer(batch, label, windowMs, restore) {
      release();
      claimStatus();
      cur = { batch, restore, label, until: Date.now() + windowMs };
      const st = $('#status');
      st.classList.add('show');
      st.classList.remove('has-progress');   // a wait is not progress; an empty trough reads as hung
      // Stop belongs to a Job, and there is no job here. Left up it reads as a second, contradictory
      // action beside Undo — "stop what?" — on the one pill the app has.
      $('#stopScan').style.display = 'none';
      const b = btn(); b.style.display = ''; b.disabled = false;
      paint();
      cur.tick = setInterval(paint, 250);
      cur.expire = setTimeout(() => release(true), windowMs + 50);
    },
    // Commit whatever is pending RIGHT NOW, without waiting out its window. Called before queueing
    // a new recycle so there is only ever one undoable action.
    async commitNow() {
      if (!cur) return;
      const b = cur.batch;
      release(false);
      try {
        await fetch('/api/delete/commit', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ batch: b }) });
      } catch (e) { /* the timer is still its backstop; nothing is lost by failing here */ }
    },
    release() { release(false); },
    async run() {
      if (!cur) return;
      const c = cur;
      release(false);
      let j = null;
      try {
        j = await (await fetch('/api/delete/undo', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ batch: c.batch }) })).json();
      } catch (e) { /* server gone: say so below rather than pretending it worked */ }
      if (!j || !j.undone) { toast('Too late to undo — already recycled'); return; }
      c.restore();
      renderCount();
      updateSelBar();
      toast(`Restored ${j.restored}`);
    },
  };
})();

// Capture what a recycle removed from the grid, so Undo can put it back in place.
//
// `entries` is [{idx, item}] IN REMOVAL ORDER, and each idx was measured against the array as it
// stood at that moment — i.e. against an array already shrunk by the removals before it. So they
// are not absolute positions: deleting three adjacent cards captures the SAME index three times.
//
// THE INVERSE OF A SEQUENCE OF SPLICES IS THOSE SPLICES UNDONE IN REVERSE ORDER. Re-inserting
// forward instead reverses the group — ABCDEFGH minus D,E,F came back as ABCFEDGH. That shipped,
// and it is what "the grid is jumbled after undo" was. Worth naming how it got past me: the
// browser check printed the restored positions as [10,9,8] against [8,9,10] and I read the
// filenames, saw them all present, and moved on. Presence is not order. test_undo_order.js now
// asserts the sequence, because an eyeballed list of the right items in the wrong order looks
// correct at a glance and never will again.
function undoRestorer(entries, deltas, selIds) {
  return () => {
    const grid = $('#grid');
    // Culling the last matching card leaves the no-match panel standing. It has to go BEFORE the
    // insert below, because that insert is positional -- `grid.children[at]` with the panel present
    // is the panel itself, so a restored card would land above a box announcing there are none.
    clearEmptyHint();
    // Re-select FIRST: cardHTML reads `selection` to decide the .selected class, so the cards are
    // built already marked rather than needing a second pass. Undo means "put it back the way it
    // was", and the selection you were working with is part of that — losing it mid-cull means
    // re-picking every card by hand.
    for (const id of (selIds || [])) selection.add(String(id));
    for (let i = entries.length - 1; i >= 0; i--) {
      const { idx, item } = entries[i];
      const at = Math.min(idx, state.items.length);
      state.items.splice(at, 0, item);
      const holder = document.createElement('div');
      holder.innerHTML = cardHTML(item);
      grid.insertBefore(holder.firstElementChild, grid.children[at] || null);
    }
    for (const [k, v] of Object.entries(deltas || {}))
      if (state[k] != null) state[k] += v;
  };
}

// WHAT A MARK ON A CARD COVERS: everything the card holds. A merged card is one thing on screen —
// a still and the video behind it, a song and its cover art, a set and its stages — so recycling,
// hiding, starring or labelling it acts on every file in it.
//
// Recycle and Hide always did this; the star and the labels did not, and the gap was invisible
// until you FILTERED by one. The star sat on the front picture alone, so with Favorites on the
// video half didn't match, the group arrived one member short, and a favourited pair came back as
// a plain image — no ▶, and no playback when you opened it. A labelled set did the same, losing
// its member count and its ▤ badge.
//
// EXPANDED CLIENT-SIDE ON PURPOSE: `group_members` only arrives while collapsing is on, so with
// the Sets dropdown off the members are separate cards and one click marks exactly the one you
// clicked. Moving this to the server would lose that distinction — the request looks the same.
function expandCardIds(ids) {
  const out = new Set(ids.map(Number));
  for (const id of ids) {
    const it = state.items.find(x => String(x.id) === String(id));
    if (it && it.group_members) String(it.group_members).split(',').forEach(m => m && out.add(Number(m)));
  }
  return [...out];
}
async function deleteSelected() {
  const ids = [...selection].map(Number);
  if (!ids.length) return;
  const sendIds = expandCardIds(ids);
  // NO EXACT FILE COUNT WHILE CARDS ARE MERGING, because the browser does not have one: a card's
  // membership omits any member the view is hiding, and the server adds those back. The old wording
  // ("plus 2 grouped files") therefore promised a number it could undercount, on the one dialog
  // where the number is the whole point. Naming the unit and saying what a card carries is both
  // honest and free.
  const label = collapsingNow()
    ? `${ids.length.toLocaleString()} card(s) — and every file in them`
    : `${ids.length.toLocaleString()} image(s)`;
  // Gated: every card being recycled is selected and on screen, so this restates rather than
  // reveals. See confirmRecycle().
  // "Recycle", not "Move to the Recycle Bin": a selection can span libraries, and on a network one
  // the files never touch the bin. Naming the action is true everywhere; naming a destination is not.
  const ok = await confirmRecycle(`Recycle ${label}?`, { ok: 'Recycle', danger: true });
  if (!ok) return;
  await Undo.commitNow();          // only one batch is ever undoable; settle the previous one
  // `collapsed` lets the server finish the job this expansion starts: group_members only ever lists
  // the members the GRID was shown, so a hidden or oversized file in the same run is missing from
  // it and used to be left on disk. The server takes the rest of the card when this is true.
  const r = await fetch('/api/delete', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: sendIds, collapsed: collapsingNow() }) });
  const j = await r.json();
  if (j.error) { uiAlert('Delete failed: ' + j.error); return; }
  // The recycle is QUEUED, not done — so there is no `failed` list to consult yet, and every id
  // sent is treated as gone. A late failure comes back through Undo's result check.
  // Remove the recycled cards in place so the scroll position holds for multi-pass culling
  // (a full search() would reset to the top), capturing enough to put them back.
  const gone = ids.map(String);
  const wasSelected = [...gone];   // undo puts the selection back too, not just the cards
  const removed = [];
  for (const id of gone) {
    const idx = state.items.findIndex(x => String(x.id) === id);
    if (idx !== -1) removed.push({ idx, item: state.items[idx] });
    if (idx !== -1) state.items.splice(idx, 1);
    document.querySelector(`.card[data-id="${id}"]`)?.remove();
    selection.delete(id);
  }
  // NOT sorted: undoRestorer unwinds these in reverse removal order, and the indices are only
  // meaningful in that sequence.
  lastSelIndex = null;
  state.total = Math.max(0, state.total - gone.length);
  // The SERVER's count, not sendIds.length: it may have taken hidden or oversized members this
  // side never saw, and the file total is the one readout that would then drift.
  const wentFiles = Number.isFinite(j.pending) ? j.pending : sendIds.length;
  if (state.rootFiles != null)   // files actually recycled (incl. grouped riders)
    state.rootFiles = Math.max(0, state.rootFiles - wentFiles);
  state.offset = Math.max(0, state.offset - gone.length);   // keep pagination aligned for load-more
  if (state.rootTotal != null) state.rootTotal = Math.max(0, state.rootTotal - gone.length);
  updateSelBar();
  renderCount();
  markActiveFilters();
  // CULLED DOWN TO NOTHING is the same blank grid a filter can produce, reached by your own hand.
  // The cards are removed in place here rather than by a re-search (to hold the scroll), so nothing
  // else on this path would ever say so. Undo takes it back out again -- see undoRestorer.
  if (!state.items.length) showNoMatchHint();
  // Deliberately NOT rebuilding facets/tags here: that would reset a filter control whose last
  // matching image was just recycled (its facet drops to 0 and vanishes). Leave filters frozen;
  // counts refresh on the next search/filter change. (Matches the single-image deleteCurrent.)
  RecycleDebt.soon();
  Undo.offer(j.batch, `${gone.length.toLocaleString()} recycled`, (j.window || 5) * 1000,
             undoRestorer(removed, { total: gone.length, rootTotal: gone.length,
                                     rootFiles: wentFiles, offset: gone.length },
                          wasSelected));
}

// The strip item's corner row, or nothing at all when the item carries no marks — an empty flex
// row would still be a box over the picture, and every strip item would grow one.
function stripMarks() {
  const marks = Array.prototype.filter.call(arguments, Boolean).join('');
  return marks ? `<span class="strip-bl">${marks}</span>` : '';
}
// ---- filmstrip (detail view) -----------------------------------------------------------------
// The current result set, in the same order ← / → walk, so the strip is a map of where you are
// rather than a second, separate list. Backed by state.items — the pages actually loaded — which is
// also what navDetail() steps through, and it grows the same way (navDetail pulls a page at the end).
//
// REBUILT ONLY WHEN THE SET CHANGES, not on every open. state.items can hold thousands of rows after
// a long scroll, and re-rendering that on each arrow press would stall the keypress AND throw away
// the scroll position — the strip would jump to the left edge every time you moved one image.
// Navigation therefore just moves a class and scrolls; `_stripKey` is what decides which happened.
let _stripKey = '';
// Whether the next re-centre should glide or jump — see the scrollTo in syncFilmstrip().
let _stripSmooth = false;
function renderFilmstrip() {
  const box = $('#stripScroll'); if (!box) return;
  const items = state.items || [];
  // Identity of the LIST, not of the selection: length plus both ends catches a new search, a new
  // page, and a filter change that happens to keep the count.
  // The thumbnail SIZE is part of the identity: the strip has its own S/M/L/XL, and changing it
  // has to re-request the pictures, not just restyle the ones already there.
  const key = (items.length ? `${items.length}:${items[0].id}:${items[items.length - 1].id}` : '0')
    + `@${thumbSizeFor(_stripPx)}`;
  if (key === _stripKey) return;
  _stripKey = key;
  box.innerHTML = items.map(stripItemHTML).join('');
  updateStripEdges();
}
// ONE strip item, the way cardHTML is one card. Pulled out of renderFilmstrip's map so a single
// item can be rebuilt when its marks change — favouriting or labelling from the detail view has to
// show up in the strip immediately, and the strip as a whole is rebuilt only when the LIST changes.
function stripItemHTML(it, i) {
  {
    // `paired` is the STILL half of a still+video pair, exactly as cardHTML means it. The strip
    // already derived `vid` from it and then threw the distinction away, which is why a set never
    // got a mark here: a pure image set is not `vid` at all, so it drew nothing.
    const paired = it.video_id != null && !it.is_video;
    const vid = it.is_video || paired;
    // The <img> stays INLINE here rather than being hoisted into a variable: its draggable="false"
    // is the guarantee that stopped .webp thumbnails landing in ComfyUI's input folder, and both a
    // reader and the regression test look for it inside this markup.
    return `<button class="strip-item" type="button" data-i="${i}" data-id="${it.id}" ` +
      `title="${esc(it.filename || '')}">` +
      // A song has no thumbnail to request, so the strip draws the same tinted face the grid does —
      // just the title, since nothing else survives at strip size.
      // The strip's job is "which one is this", and a column of truncated filenames answers it
      // worse than the waveforms do — they are the one thing that differs at a glance. So the
      // title is clamped small above its own waveform, with the length under it.
      // A SONG is draggable from the strip, and it is the one exception to the rule below rather
      // than a hole in it. That rule exists because a thumbnail is the WRONG FILE — it reached
      // ComfyUI as a 160px WebP with no workflow. A song has no right original to drag at all, so
      // /dragpng/ is the only correct artifact at any size; there is no smaller, worse version of
      // it to hand over by mistake.
      (it.is_audio
        ? `<img class="song-drag" loading="lazy" draggable="true" alt="" aria-hidden="true"
               data-fn="${esc(it.filename || '')}"
               src="${esc(originalUrlFor(it.thumb_url || `/thumb/${it.id}?v=0&r=${it.root_id || ''}`, it.filename, 'audio'))}">`
          + `<span class="strip-song${it.genre ? ' tinted' : ''}"${it.genre ? ` style="--song-hue:${songHue(it.genre)}"` : ''}>`
          + `<span class="strip-song-title">${esc(it.title || it.filename || '')}</span>`
          + wavePath(it.peaks)
          + (it.duration != null ? `<span class="strip-song-dur">${fmtDuration(it.duration)}</span>` : '')
          + `</span>`
        // draggable="false" ON PURPOSE — see NOT-DRAGGABLE below. This is a thumbnail: dragging it
        // into ComfyUI delivered a 160px WebP with no workflow in it.
        : `<img loading="lazy" draggable="false" src="${thumbAt(it.thumb_url, _stripPx)}">`) +
      // The same corner row the grid card carries, in the same order and from the same components:
      // ▶ then the set stack. Both titles are cardHTML's, word for word — two surfaces describing
      // one item differently is the thing that made the missing mark read as a bug in the first
      // place. Emitted only when there is something to put in it, so an ordinary still is unmarked.
      stripMarks(
        vid || it.is_audio ? `<span class="strip-play${it.is_audio ? ' song' : ''}"></span>` : '',
        (it.is_set || paired)
          ? `<span class="set-badge" title="${paired ? 'Video set — still + video (opens the video)' : 'Image set — open to keep one, recycle the rest'}"></span>` : ''
      ) +
      // THE MARKS YOU CURATE BY, carried through from the grid. The author, 2026-09-05: reviewing means
      // leaning on the favourite and the label, and both vanished the moment you opened an image —
      // so the view you review IN was the one view that could not show you what you had decided.
      //
      // The star is ALWAYS emitted, unlike the label, because setCardFav toggles a class on an
      // element that has to already be there; it is invisible until `on`. Display-only on this
      // surface (the author's call): the whole face of a strip item is one navigation button, and a
      // clickable target inside it would be a small mis-hit away from jumping somewhere.
      `<span class="star${it.fav ? ' on' : ''}" aria-hidden="true">${ICON_STAR}</span>` +
      (it.label
        ? `<div class="label-strip label-${esc(it.label)}" title="${esc(labelName(it.label))}">${esc(labelName(it.label))}</div>` : '') +
      '</button>';
  }
}
// One strip item, rebuilt in place. The strip as a whole re-renders only when the LIST changes, so
// a mark that changed on an item already on screen needs this — otherwise labelling from the detail
// view is invisible until the next search. Keeps `.current`: that class is the strip's cursor, and
// replacing the element under it would drop the highlight on whatever you are looking at.
// `oldId` is for the case where the item did not just CHANGE but was REPLACED by a different row —
// culling a set to its MAIN swaps a card whose id was the REFINE's for one with the MAIN's, and the
// strip element still carries the old id. Defaults to `id`, which is the ordinary mark-changed call.
function refreshStripItem(id, oldId = id) {
  const el = document.querySelector(`.strip-item[data-id="${oldId}"]`);
  if (!el) return;
  const it = state.items.find(x => String(x.id) === String(id));
  if (!it) return;
  const wasCurrent = el.classList.contains('current');
  const i = Number(el.dataset.i);
  el.outerHTML = stripItemHTML(it, i);
  if (wasCurrent) {
    const now = document.querySelector(`.strip-item[data-id="${id}"]`);
    if (now) now.classList.add('current');
  }
}
// Move the highlight and bring it into view. Separate from the render so the common case — pressing
// an arrow — costs one class swap and one scroll, whatever the set size.
function syncFilmstrip() {
  const box = $('#stripScroll'); if (!box) return;
  renderFilmstrip();
  const prev = box.querySelector('.strip-item.current');
  if (prev) prev.classList.remove('current');
  const el = box.children[state.index];
  if (!el) return;
  el.classList.add('current');
  // Manual centring rather than scrollIntoView(): that scrolls EVERY scrollable ancestor, which
  // here means it would also scroll the grid underneath the overlay — so closing the detail would
  // leave the page somewhere you never put it.
  // MEASURED FROM RECTS, not offsetLeft. offsetLeft is relative to the nearest POSITIONED ancestor,
  // and the scroller isn't one — .detail is — so it silently carried the strip's own offset and
  // left every item ~9px off centre. Rects are relative to the viewport for both, so the delta
  // between two of them is the real distance whatever the offsetParent turns out to be.
  const eb = el.getBoundingClientRect(), bb = box.getBoundingClientRect();
  // EASED while you are navigating, INSTANT when the strip is being positioned for the first time.
  // Both matter: gliding to the next thumbnail is the point, but animating the very first placement
  // would make the strip visibly slide in from image 1 every time the detail opens. _stripSmooth is
  // set by whoever knows which case this is — syncFilmstrip is called twice per open (once before
  // the fetch, once after the overlay is revealed), so the flag has to outlive a single call.
  box.scrollTo({ left: box.scrollLeft + (eb.left + eb.width / 2) - (bb.left + bb.width / 2),
                 behavior: _stripSmooth ? 'smooth' : 'auto' });
  updateStripEdges();
  updateStripPos();
}
// WHERE YOU ARE IN THE WHOLE RESULT SET, under the size picker.
// state.total, NOT state.items.length: the strip holds the pages loaded so far and grows as you
// reach the end, so the loaded count would climb while you navigate and the denominator would never
// stand still — "3 / 200" becoming "201 / 400" tells you nothing about how far there is to go. This
// is the same number the selection bar's "Select all N cards" reports, and it is CARDS in both
// places, so a collapsed run counts once here exactly as it does there.
// The same guard updateSelBar uses, and the same fallback: a total is not always in hand.
function updateStripPos() {
  const el = $('#stripPos'); if (!el) return;
  const total = Number.isFinite(state.total) ? state.total : (state.items || []).length;
  const i = state.index;
  // TWO SPANS, because only the first number is highlighted — see .strip-pos-n. innerHTML is safe
  // here in a way it usually is not: both halves are numbers this function formatted itself.
  el.innerHTML = (!total || i == null || i < 0) ? ''
    : `<span class="strip-pos-n">${(i + 1).toLocaleString()}</span><span>/ ${total.toLocaleString()}</span>`;
}
// Which way the strip can still go. Four things decide it — how many items there are, how big they
// are drawn, how wide the window is, and where you have scrolled to — so it is called from the two
// functions that change the first three and listened for on the fourth, rather than left to any one
// of them. A smooth scrollTo settles over several frames; the scroll listener is what makes the
// cues right at the END of one rather than at the moment it was asked for.
function updateStripEdges() {
  const box = $('#stripScroll'); if (!box) return;
  const p = $('#stripPrev'), n = $('#stripNext');
  if (!p || !n) return;
  // 1px of slack: a scroller sitting at its maximum lands on a fractional value at most zoom
  // levels, so `>= max` would leave the cue drawn on a side with nothing behind it.
  const max = box.scrollWidth - box.clientWidth;
  p.classList.toggle('hidden', box.scrollLeft <= 1);
  n.classList.toggle('hidden', box.scrollLeft >= max - 1);
}
const STRIP_SIZE_KEY = 'cv:stripSize';
function applyStripSize(px) {
  // A stored value from the old, smaller scale (80/112/144/208) simply isn't in this list and falls
  // back to M — self-healing, and a view preference is not worth a migration.
  px = [112, 160, 224, 336].includes(px) ? px : 160;
  _stripPx = px;                         // the strip requests its own thumbnail size, not the grid's
  document.documentElement.style.setProperty('--strip-h', px + 'px');
  const sel = $('#stripSize'); if (sel) sel.value = String(px);
  try { localStorage.setItem(STRIP_SIZE_KEY, String(px)); } catch (e) {}
  // Item widths just changed, so the centred item is no longer centred. Re-centre rather than
  // leaving the strip pointing at wherever the old geometry happened to land — and JUMP, because
  // this is a re-measure of the same position, not a move to a different image.
  if (!$('#overlay').classList.contains('hidden')) { _stripSmooth = false; syncFilmstrip(); }
}

// ---- detail ----
// The status pill belongs to whatever is currently showing the pictures, so it is MOVED between two
// parents rather than positioned at them from a distance: the document (fixed, bottom-left of the
// results pane) over the grid, and the image box (absolute, bottom-left of the picture) in the
// detail view.
//
// WHY MOVE THE NODE. "Bottom-left of the image" is not derivable from the viewport. The image box is
// whatever is left of a centred window after the filmstrip above it and the Notes box and LM panel
// below it have taken their share, and both of those come and go — so positioning against it from
// outside means measuring it and then watching it for changes. Re-parenting makes the image box do
// that work itself, for free, and it is the same reasoning that put the prev/next arrows in there.
//
// Safe to call unconditionally: it no-ops when the pill is already in the right place, so there is
// no path that strands it inside a hidden overlay.
function hostStatus() {
  const st = $('#status');
  const host = $('#overlay').classList.contains('hidden') ? document.body : $('.detail-img');
  if (host && st.parentElement !== host) host.appendChild(st);
}

async function openDetail(id) {
  // Record the view. NOTHING READS IT TODAY — the "Least seen" sort it fed was retired with the
  // per-card impression tracking (CLN-1), and this deliberately outlived that: viewing history
  // can't be rebuilt, the sort may come back, and one write per open costs nothing where an
  // observer plus a dwell timer on every card did. Fire-and-forget — every open path funnels
  // through here (grid click, arrow nav, set keeper), and a failed bump must never
  // interrupt viewing.
  fetch('/api/opened', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify({ id: Number(id) }) }).catch(() => {});
  state.index = state.items.findIndex(x => String(x.id) === String(id));
  // BEFORE the fetch, not after. An answer is about the file that was open when it was asked, so
  // it has to go the moment the panel commits to a different one — clearing it after the await
  // leaves it standing under the new picture for as long as the request takes, which is exactly
  // the window in which it would be read as being about that picture.
  clearAnswer();
  // Already up = you are stepping between images, so the strip glides. Closed = this is the first
  // placement, which must not animate from wherever the strip happened to be left.
  _stripSmooth = !$('#overlay').classList.contains('hidden');
  syncFilmstrip();   // before the await: the strip should move with the click, not after a fetch
  // A merged pair is represented by its STILL (the PNG, which carries the ComfyUI metadata). Load
  // the still's record so Model/LoRAs/prompt come from it, but PLAY the paired video in the viewer.
  const it = state.index !== -1 ? state.items[state.index] : null;
  const paired = !!(it && it.video_id != null && !it.is_video);
  const d = await getJSON('/api/image/' + id);   // for a pair, `id` is the still (representative)
  state.current = d;
  // Image set: show all members side by side for a keep-one cull instead of a single media element —
  // but only when the set is actually collapsed in the grid (Image-sets toggle on, so it.is_set is
  // true). With the toggle off the members are separate cards, so open the clicked image on its own.
  const isSet = !!(it && it.is_set) && Array.isArray(d.set_members) && d.set_members.length > 1;
  state.setMembers = isSet ? d.set_members : null;
  state.setKeep = null;
  document.querySelector('.detail').classList.add('detail-max');   // always near-fullscreen (single + set panes)
  // Right-panel recycle: whole-set (with confirm) in set mode, plain single recycle otherwise.
  const dDel = $('#dDelete');   // icon button — set the tooltip/label, never textContent (would wipe the icon)
  setNaturalTitle(dDel, isSet ? 'Recycle set — every image in it, not just the one shown'
                             : 'Recycle this image');
  dDel.setAttribute('aria-label', isSet ? 'Recycle set' : 'Recycle');
  dDel.disabled = false;
  stopDetailMedia();   // see its note: the branch below must never own this
  if (isSet) {
    renderSetView(d.set_members);
    hidePairSource();
  } else if (paired) {
    hideSetView();
    playPairVideo(it.video_id);            // show <video>, hide <img>
    setPairSource(d);                      // draggable PNG thumbnail for drag-to-ComfyUI
  } else {
    hideSetView();
    showDetailMedia(d);
    // A lone video gets the same handle a paired one has — it is the only way to drag it out of
    // this view, since the <video> element cannot be a drag source. An offline library has no
    // reachable file to build a frame from, so it gets nothing rather than a broken picture.
    if (d.is_video && !isRootOffline(d.root_key)) setVideoSource(d);
    else if (d.is_audio && !isRootOffline(d.root_key)) setSongSource(d);
    else hidePairSource();
  }
  $('#dName').textContent = d.filename;
  $('#dModel').textContent = d.model_name || '—';
  $('#dModelType').textContent = d.model_type || '—';
  const nLoras = (d.loras || []).length;
  $('#dLorasLabel').textContent = nLoras ? `LoRAs (${nLoras})` : 'LoRAs';
  $('#dLoras').innerHTML = lorasHTML(d.loras);
  $('#dFolder').textContent = d.folder;
  $('#dRoot').textContent = d.root_name || ((state.roots || []).find(r => r.key === d.root_key) || {}).name || '—';
  $('#dSize').textContent = (d.width && d.height) ? `${d.width}×${d.height}` : '—';
  $('#dFilesize').textContent = fmtBytes(d.size);
  // The date AND how long ago, in one row rather than two. The author flagged the right pane as already
  // dense, so age earns its place by riding a row that was there anyway.
  $('#dDate').textContent = d.mtime
    ? `${new Date(d.mtime * 1000).toLocaleString()} · ${fmtAge(d.mtime)}` : '—';
  $('#dDuration').textContent = fmtDuration(d.duration) || '—';
  fillGenSettings(d);
  hideEmptyDetailFields(d);
  syncQualityDetail();
  $('#dPos').value = d.positive || '';
  $('#dNeg').value = d.negative || '';
  // Export for Civitai is PNG-only (that's where the ComfyUI graph + a spliceable text chunk live).
  $('#dCivitaiExport').classList.toggle('hidden', d.is_video || (d.ext || '').toLowerCase() !== '.png');
  renderComfyButton(d);
  renderDetailTags();
  renderDetailLabels();
  const note = $('#dNote');
  note.value = d.note || '';
  note.classList.toggle('hidden', isSet);   // per-image note has no meaning across a set's members
  // Same rule as the reply panel: the server skips videos, and "this image" is ambiguous in a set.
  // A song has nothing for a VISION model to look at either.
  $('#dFav').classList.toggle('on', !!d.favorite);   // outline vs filled star; see .mark-toggle
  applyOfflineState(d);
  $('#overlay').classList.remove('hidden');
  hostStatus();          // the pill moves into the image box; see hostStatus for why it moves at all
  // AFTER the reveal, not only in openDetail. The overlay is display:none until this line, so on the
  // first open every rect inside it measures 0 — the strip set its highlight but could not scroll,
  // and opening image 500 left the strip sitting at image 1. Navigation looked fine because by then
  // the overlay was already up. Cheap to repeat: renderFilmstrip() is keyed, so this only re-scrolls.
  syncFilmstrip();
  // MAXIMIZED SURVIVES ←/→ BUT NOT A FRESH OPEN. _stripSmooth, captured at the top of this function,
  // is the "was the detail already up?" signal: stepping between images calls openDetail too, so
  // resetting unconditionally dropped you out of maximized on EVERY picture — which is precisely the
  // full-bleed browsing the mode exists for. Same rule the pinned loupe follows: navigation is the
  // one action that must not cancel the thing you set up to navigate with.
  // Runs here, after the reveal, for syncFilmstrip's reason above — the panes it marks are created by
  // renderSetView earlier in this function, and nothing inside the overlay measures until it is shown.
  if (_stripSmooth) Maxi.rebind(); else Maxi.reset();
}
// Offline library: everything DB-backed still works (tags, labels, notes, favorites, prompts — and
// the cached thumbnail), but anything that must READ THE FILE cannot. Say so plainly and disable
// those, rather than letting them fail with a raw error.
// Runs LAST in showDetail: several handlers above unconditionally re-enable these buttons.
// ---- what is unavailable, and why ---------------------------------------------------------------
// TWO REASONS CAN BLOCK THE SAME CONTROL -- the library being offline, and a job holding the
// database -- so ONE place decides. They used to be separate: offline set `disabled` outright, so
// whichever ran last won and could re-enable a button the other had turned off.
//
// The busy half exists because of what the author hit: opening the app starts a background catch-up, the
// scorer refuses while one is running, and nothing on screen said so -- the auto-refresh clock is
// the only indicator for that path and it is a small mark in the rail. "If features are blocked,
// then let's let the user know." A greyed button that says why in its tooltip is that, without
// making every background refresh raise a pill the quiet path deliberately avoids.
let _busyWhy = null;

// [selector, why-when-offline (null = offline does not block it), blocked-by-a-job?]
const BLOCKABLE = [
  ['#dDelete',      'Library is offline — reconnect to recycle this file',        false],
  ['#dReveal',      'Library is offline — nothing to show in Explorer',           false],
  ['#dExtBtn',      'Library is offline — an extension needs to read the file',   true],
  ['#selReward',    null,                                                         true],
  ['#libClearScores', null,                                                       true],
];

// THE ONLY WAY to change a BLOCKABLE control's tooltip. syncBlocked caches the "natural" title so
// it can put it back when an offline/busy reason lifts — and it caches ONCE, for the life of the
// page, which is right for the four whose titles are fixed in the markup and silently wrong for the
// one that is not. #dDelete's tooltip is rewritten per image ("Recycle this image" vs "Recycle
// set"), and writing .title alone meant the next syncBlocked — which openDetail itself calls, a few
// lines later — restored the FIRST image's wording over it and kept doing so forever. Open a single
// image first and every set afterwards said "Recycle this image"; its aria-label, which syncBlocked
// does not touch, stayed correct the whole time, which is why the two disagreed.
function setNaturalTitle(el, text) { if (el) { el.title = text; el.dataset.title = text; } }

function syncBlocked(offline) {
  for (const [sel, offWhy, byJob] of BLOCKABLE) {
    const el = $(sel);
    if (!el) continue;
    // Remember the real tooltip once, so restoring it does not leave a stale reason behind --
    // which the offline version did: it set the title and never put the original back.
    if (el.dataset.title == null) el.dataset.title = el.title || '';
    const isOff = offline && offWhy;
    const isBusy = byJob && _busyWhy;
    el.disabled = !!(isOff || isBusy);
    el.title = isOff ? offWhy : (isBusy ? _busyWhy : el.dataset.title);
  }
}

// Raised and lowered by whatever owns the job -- see the clock's comment in autoRefreshTick, and
// rule 1 of DESIGN.md's Feedback state: the thing that raises an indicator is the only thing that
// may lower it.
function setBusyActions(why) {
  _busyWhy = why || null;
  syncBlocked(state.current ? isRootOffline(state.current.root_key) : false);
}

function applyOfflineState(d) {
  const offline = isRootOffline(d.root_key);
  const banner = $('#dOffline');
  banner.classList.toggle('hidden', !offline);
  if (offline) {
    banner.textContent = `${libName(d.root_key)} is offline — this is the cached thumbnail and ` +
      `saved details. Tags, labels and notes still save. Reconnect to open, export or recycle it.`;
  }
  syncBlocked(offline);
  if (offline) $('#dCivitaiExport').classList.add('hidden');   // export must read the PNG
}
function renderDetailTags() {
  const tags = (state.current && state.current.tags) || [];
  $('#dTags').innerHTML = tags.filter(t => !t.startsWith('label:')).map(t =>   // labels shown as buttons, not chips
    `<span class="chip"><span>${esc(t)}</span><button data-rmtag="${esc(t)}" title="Remove"></button></span>`).join('');
  // A SEPARATE, LABELLED row — never merged into the one above. The chips above are what you
  // decided; these are what something found, and the app must not let the two become the same
  // thing by accident. The chips themselves are ordinary: the heading carries the difference.
  // They take the same × — removing a tag you disagree with should not depend on who wrote it.
  const ext = (state.current && state.current.ext_tags) || [];
  $('#dExtTagsWrap').classList.toggle('hidden', !ext.length);
  $('#dExtTags').innerHTML = ext.map(t =>
    `<span class="chip" title="Found by ${esc(t.by)}"><span>${esc(t.tag)}</span>` +
    `<button data-rmtag="${esc(t.tag)}" title="Remove"></button></span>`).join('');
}
function setCardFav(id, on) {
  const el = document.querySelector(`.card[data-id="${id}"] .star`);
  if (el) el.classList.toggle('on', on);
  // AND THE FILMSTRIP'S COPY. Both surfaces show the same mark, so both are updated from the one
  // place that knows it changed — every favourite path (grid click, detail view, bulk) already
  // funnels through here, which is why this is the hook rather than each caller.
  const sel = document.querySelector(`.strip-item[data-id="${id}"] .star`);
  if (sel) sel.classList.toggle('on', on);
  const it = state.items.find(x => String(x.id) === String(id));
  if (it) it.fav = on ? 1 : 0;
}
// Re-render one grid card in place from its (updated) state.items entry. Card clicks are
// delegated on #grid, so replacing the element's HTML keeps everything working.
function refreshCard(id) {
  const it = state.items.find(x => String(x.id) === String(id));
  const el = document.querySelector(`.card[data-id="${id}"]`);
  if (it && el) el.outerHTML = cardHTML(it);
  refreshStripItem(id);      // the same item in the filmstrip carries the same marks
}
async function addDetailTag(tag) {
  tag = (tag || '').trim(); if (!tag || !state.current) return;
  await fetch('/api/tag', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: detailMarkIds(state.current), tag, op: 'add' }) });
  state.current.tags = state.current.tags || [];
  if (!state.current.tags.includes(tag)) state.current.tags.push(tag);
  $('#dTagInput').value = '';
  renderDetailTags();
  loadTags();
}
async function removeDetailTag(tag) {
  if (!state.current) return;
  await fetch('/api/tag', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: detailMarkIds(state.current), tag, op: 'remove' }) });
  state.current.tags = (state.current.tags || []).filter(t => t !== tag);
  renderDetailTags();
  loadTags();
}
// A mark set from the DETAIL view covers a PAIR — one artifact stored as two files, a still and its
// video or a song and its cover — but stays on the single image inside a SET, whose side-by-side
// panes exist precisely to tell members apart. In the grid there is no such distinction to make:
// the set is one card, so expandCardIds takes all of it.
function detailMarkIds(d) {
  return (d && d.group_kind === 'pair' && Array.isArray(d.group_members) && d.group_members.length)
    ? d.group_members.map(Number) : [Number(d.id)];
}
async function toggleDetailFav() {
  if (!state.current) return;
  // Same rule as the card: what the star is showing wins, since that is what was clicked.
  const shown = $('#dFav').classList.contains('on');
  const on = !(state.current.favorite != null ? state.current.favorite : shown);
  let r;
  try {
    r = await postJSON('/api/favorite', { ids: detailMarkIds(state.current), on });
  } catch (e) { r = { error: 'offline' }; }
  if (r && r.error) return toast('Could not change the favorite.');
  state.current.favorite = on;
  $('#dFav').classList.toggle('on', !!on);
  setCardFav(state.current.id, on);
  loadTags();
}
async function toggleCardFav(card) {
  const id = card.dataset.id;
  const it = state.items.find(x => String(x.id) === id);
  // THE SCREEN DECIDES, NOT THE IN-MEMORY LIST, and that is the fix for a bug the author hit twice --
  // "it just stays yellow", intermittently, "dependent on something". A card whose row is missing
  // from state.items made `!(it && it.fav)` true forever: every click asked the server to turn the
  // favourite ON, so the star lit and never went out however many times it was pressed.
  //
  // The list can fall out of step in more ways than are worth enumerating (a background tick that
  // rebuilt a card, a group that changed representative, a filter that re-queried) -- and the star
  // on screen is the thing the user is actually toggling, so it is the honest source for "what is
  // it now". The list is still preferred when it HAS the row, since it is what the rest of the
  // grid reads.
  const star = card.querySelector('.star');
  const on = it ? !it.fav : !(star && star.classList.contains('on'));
  let r;
  try {
    r = await postJSON('/api/favorite', { ids: expandCardIds([Number(id)]), on });
  } catch (e) { r = { error: 'offline' }; }
  // A FAILURE USED TO LOOK EXACTLY LIKE A SUCCESS. Neither path checked the reply, so a rejected
  // request still lit the star locally and the next refresh silently put it back.
  if (r && r.error) return toast('Could not change the favorite.');
  setCardFav(id, on);
  loadTags();
}

// ---- Send to ComfyUI ---------------------------------------------------------------------------
// Opens this file's workflow on ComfyUI's canvas: the one-click version of dragging it there, and
// it stops where a drop stops — nothing is queued and nothing is saved.
//
// TWO DIFFERENT ABSENCES, shown differently on purpose. A file that carries no workflow gets NO
// button, because that will never change for that file. ComfyUI being unreachable gets a DIMMED
// button with the reason, because that is temporary and the fix is to start ComfyUI — hiding it
// would mean never learning the feature exists.
let _comfyReady = null;                       // null = not asked yet this session
async function comfyReady(force) {
  if (_comfyReady !== null && !force) return _comfyReady;
  try {
    const j = await getJSON('/api/comfy/status');
    _comfyReady = !!(j && j.ready);
  } catch (e) { _comfyReady = false; }
  return _comfyReady;
}
async function renderComfyButton(d) {
  const b = $('#dComfyOpen'); if (!b) return;
  b.classList.toggle('hidden', !d.has_workflow);
  if (!d.has_workflow) return;
  const ready = await comfyReady();
  b.setAttribute('aria-disabled', String(!ready));
  // NAMES SOMETHING YOU CAN ACTUALLY GO AND GET. This said "install the VV Bridge package" until
  // 2026-09-12, and the bridge stopped being a package of its own when it was folded into the
  // nodes — so the one instruction the dimmed button gave sent people looking for a download that
  // does not exist. The author hit exactly that. What Help now explains, this has to agree with.
  b.title = ready ? 'Open this workflow in ComfyUI'
                  : "ComfyUI isn't answering — start it, or install the VV nodes";
}
$('#dComfyOpen').addEventListener('click', async e => {
  const b = e.currentTarget;
  if (b.getAttribute('aria-disabled') === 'true') {
    // Re-check rather than refusing: the usual reason for pressing a dimmed one is that you just
    // started ComfyUI, and making you reopen the image to clear a cached "no" would be silly.
    if (!(await comfyReady(true))) return toast("ComfyUI isn't answering on this machine");
    b.setAttribute('aria-disabled', 'false');
  }
  b.classList.add('busy');
  try {
    const j = await postJSON('/api/comfy/open', { id: Number(state.current.id) });
    toast(j && j.error ? '⚠ ' + j.error : 'Opened in ComfyUI');
    if (j && j.error) _comfyReady = null;      // it may have gone away; ask again next time
  } catch (err) {
    toast("⚠ couldn't reach the viewer's server");
  } finally { b.classList.remove('busy'); }
});

// Bulk tag modal — the detail view's tag UI (autocomplete input + removable chips) applied to the
// grid selection. Adds/removes apply to a snapshot of the selection immediately; chips show what
// was changed this session.
function openTagModal() {
  const ids = [...selection].map(Number);
  if (!ids.length) return;
  state.tagModalIds = ids;
  state.tagModalAdded = [];
  $('#tagModalTitle').textContent = `Add tags to ${ids.length} image${ids.length > 1 ? 's' : ''}`;
  $('#mTagInput').value = '';
  $('#mTagStatus').textContent = '';
  renderModalTags();
  $('#tagModal').classList.remove('hidden');
  setTimeout(() => $('#mTagInput').focus(), 30);
}
function renderModalTags() {
  $('#mTags').innerHTML = (state.tagModalAdded || []).map(t =>
    `<span class="chip"><span>${esc(t)}</span><button data-rmtag="${esc(t)}" title="Remove from the selection"></button></span>`).join('');
}
async function addModalTag(tag) {
  tag = (tag || '').trim();
  const ids = state.tagModalIds;
  if (!tag || !ids) { $('#mTagInput').value = ''; return; }
  if (!state.tagModalAdded.includes(tag)) {
    await fetch('/api/tag', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: expandCardIds(ids), tag, op: 'add' }) });
    state.tagModalAdded.push(tag);
    renderModalTags();
    loadTags();       // refresh the sidebar tag list + the shared datalist
    $('#mTagStatus').textContent = `Tagged ${ids.length} image${ids.length > 1 ? 's' : ''} as "${tag}"`;
  }
  $('#mTagInput').value = '';
}
async function removeModalTag(tag) {
  const ids = state.tagModalIds;
  if (!ids) return;
  await fetch('/api/tag', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: expandCardIds(ids), tag, op: 'remove' }) });
  state.tagModalAdded = state.tagModalAdded.filter(t => t !== tag);
  renderModalTags();
  loadTags();
  $('#mTagStatus').textContent = `Removed "${tag}" from ${ids.length} image${ids.length > 1 ? 's' : ''}`;
}
function closeTagModal() {
  $('#tagModal').classList.add('hidden');
  state.tagModalIds = null;
  state.tagModalAdded = [];
}

// ---- prompt miner: mine candidate tags across one library, review, apply ----
// Invoked from a library's ⋯ menu. Reopens the prior list if we've already mined THAT library
// (so winnowing + picks survive close/reopen); Re-mine (or a different library) runs a fresh scan.
async function mineTags(key) {
  if (state.minerCands && state.minerKey === key) { openMinerModal(false); return; }
  await runMineScan(key);
}
async function runMineScan(key) {
  if (key) state.minerKey = key;
  const re = $('#minerRemine');
  if (re) { re.disabled = true; re.classList.add('busy'); re.textContent = 'Scanning…'; }
  // Open the panel immediately with a loading scrim: the scan over a big library takes a
  // beat, and a delay before anything appears feels like the click did nothing.
  $('#minerTitle').innerHTML = 'Suggested tags <span class="mt-sub">scanning…</span>';
  $('#minerList').innerHTML = '<div class="miner-loading"><div class="spinner"></div>' +
    "<span>Scanning this library's prompts, folders &amp; filenames…</span></div>";
  $('#minerApply').disabled = true;
  $('#minerStatus').textContent = '';
  $('#minerModal').classList.remove('hidden');
  try {
    const j = await postJSON('/api/miner/scan', { key: state.minerKey });
    if (j.error) {
      $('#minerList').innerHTML = `<div class="miner-empty">Mine failed: ${esc(j.error)}</div>`;
      if (re) { re.disabled = false; re.classList.remove('busy'); re.textContent = 'Scan again'; }
      return;
    }
    watchMine();          // the mine now runs on a worker thread; poll it
  } catch (e) {
    $('#minerList').innerHTML = `<div class="miner-empty">Mine failed: ${esc(String(e))}</div>`;
    if (re) { re.disabled = false; re.classList.remove('busy'); re.textContent = 'Scan again'; }
  }
}
// Poll the miner job, driving the SHARED status bar + Stop (#status is z-55, above the miner
// modal at z-50, so Stop stays clickable). Mirrors watchScan.
// ---- Job: one shape for every long-running server task ----------------------------------------
// Five of these grew independently — scan, Quality scoring, the tag miner, the Civitai
// export — and drifted apart on every decision that matters: whether to block the screen, what to do
// when the server stops answering, how long the completion message lingers, and whether to refresh
// before or after tearing the indicators down. They also shared one interval handle and one "what
// does Stop cancel" variable with no notion of who owned them, which is how starting one job could
// silently kill another's progress tracking and leave its button disabled for the session.
//
// One slot, one owner, one policy:
//   - ONE JOB AT A TIME. start() refuses while another holds the slot, so nothing can steal the
//     timer, the Stop button or the pill from a job still running.
//   - STOP TARGETS THE SLOT, not the last thing that happened to set a variable.
//   - THE REFRESH COMES FIRST AND IS AWAITED. finish(s) runs before any indicator comes down, so the
//     screen never uncovers onto results the job hasn't produced yet.
//   - THE COMPLETION MESSAGE IS OWNED. Its timer is cancelled when the next job claims the pill, so
//     a finished job can't strip the status bar off a running one.
//   - LOSING THE SERVER ENDS THE JOB. One policy, not "terminate" in two of five and "retry forever
//     in silence with the Stop button up" in the other three.
//   - RELEASE IS GUARANTEED — teardown is in a finally, so a throw in finish() can't strand anything.
//
// Job.during(fn) wraps SEVERAL server jobs in one presentation (a refresh scanning three libraries):
// the pill and the block are raised once for the run and the individual jobs skip their own, so the
// screen can't flash between them.
const Job = (() => {
  let cur = null;        // the one running job
  let runTok = 0;        // non-zero while a Job.during run owns the presentation
  let runBlockTok = 0;   // the run's block, raised LAZILY by its first blocking job
  let poll = null, tailT = null, seq = 0;
  const st = () => $('#status'), stopBtn = () => $('#stopScan');
  function pill(on) {
    if (on) {
      clearTimeout(tailT); tailT = null;
      claimStatus();     // a toast's own hide timer must not strip this job's Stop button
      Undo.release();    // ...and an undo offer must not leave its button beside this job's Stop
      $('#undoDelete').style.display = 'none';
      st().classList.add('show');
      st().classList.remove('has-progress');   // nothing measurable yet — no empty trough
      $('#pbarFill').style.width = '0';
      // 'Stop' unless the running job asked for another word. Only the first index of a new library
      // does: it offers Cancel, because cancelling THAT throws the library away rather than keeping
      // what it managed. One button, two meanings, so the word has to carry the difference.
      const b = stopBtn(); b.style.display = ''; b.disabled = false;
      b.textContent = (cur && cur.stopLabel) || 'Stop';
    } else {
      stopBtn().style.display = 'none';
    }
  }
  // '' retires the pill at once. Anything else lingers, and the timer is OURS — the next claimant
  // cancels it rather than letting it fire over their progress bar.
  function retire(msg, ms) {
    st().classList.remove('has-progress');   // the run is over; a full bar is not information
    if (!msg) { st().classList.remove('show'); return; }
    $('#statusText').textContent = msg;
    clearTimeout(tailT);
    tailT = setTimeout(() => { st().classList.remove('show'); tailT = null; }, ms);
  }
  return {
    get busy() { return !!cur; },
    get inRun() { return !!runTok; },
    get name() { return cur ? cur.name : null; },
    get stopUrl() { return cur ? cur.stopUrl : null; },
    get onStop() { return cur ? cur.onStop : null; },
    // The stop button's word, settable mid-flight: a reattached scan does not know it is a first
    // index until the first status poll comes back.
    setStopLabel(text) {
      if (!cur || cur.stopLabel === text) return;
      cur.stopLabel = text;
      const b = stopBtn();
      if (b.style.display !== 'none' && !b.disabled) b.textContent = text;
    },
    // The pill is claimed for the WHOLE run, from before the first server call, and the text is
    // updated in place — a run used to open with a toast, whose own 3.5s timer would hide it
    // mid-way, so the message vanished and a second one appeared in its place. On a network share
    // the opening check outlives that timer every time.
    // The BLOCK is not raised here: it is raised by the first blocking job inside the run, so a
    // read-only check at the start doesn't freeze the app for its duration.
    // fn's return value is the closing message ('' retires the pill at once).
    async during(fn) {
      if (runTok) return fn();               // already inside a run; don't nest presentations
      runTok = ++seq;
      pill(true);
      let msg = '';
      try { msg = (await fn()) || ''; }
      finally {
        if (runBlockTok) { Busy.release('job', runBlockTok); runBlockTok = 0; }
        stopBtn().style.display = 'none';
        // Shorter than a toast's 3.5s: a run's closing line is an acknowledgement ("Libraries up to
        // date"), not something to read, and it had outstayed its welcome by the time you looked up.
        retire(msg, msg ? 1800 : 0);
        runTok = 0;
      }
      return msg;
    },
    // Update the message without touching the pill's lifetime. What a run says while it works.
    // `fresh` drops the previous subject's bar with the text. A run that moves to the next library
    // would otherwise show the new name against the old library's 98% for as long as the first poll
    // takes to come back — a bar that runs to the end and jumps back reads as a fault.
    say(text, fresh) {
      $('#statusText').textContent = text;
      if (fresh) { st().classList.remove('has-progress'); $('#pbarFill').style.width = '0'; }
    },
    // Resolves with the final status object, or null if the job never started or contact was lost.
    // Awaitable, so a chain of jobs is a for-loop rather than a callback that has to remember to
    // hand the presentation back.
    async start(spec) {
      if (cur) {
        if (!spec.silent) toast((spec.blockedMsg || 'Another job') + ' is already running');
        return null;
      }
      // stopLabel/onStop let ONE job change what its stop button says and asks, without every other
      // job learning about it. onStop returns false to call the whole thing off (the user changed
      // their mind at the confirm), which is why stopScan awaits it before touching the button.
      const job = cur = { name: spec.name, stopUrl: spec.stopUrl, ending: false,
                          stopLabel: spec.stopLabel || 'Stop', onStop: spec.onStop || null };
      const owns = !spec.silent && !runTok;   // a run owns the presentation instead, if there is one
      if (owns) pill(true);
      let busyTok = 0;
      if (spec.blocking) {
        if (runTok) { if (!runBlockTok) runBlockTok = Busy.raise('job'); }   // the run holds it, once
        else busyTok = Busy.raise('job');
      }
      if (poll) { clearInterval(poll); poll = null; }
      return new Promise(resolve => {
        const settle = async (s, lost) => {
          if (job.ending) return;
          job.ending = true;
          clearInterval(poll); poll = null;
          try { if (spec.finish) await spec.finish(s); }
          catch (e) { /* a refresh error must not leave the indicators stuck */ }
          finally {
            if (busyTok) Busy.release('job', busyTok);
            cur = null;
            try { if (spec.restore) spec.restore(); } catch (e) {}
            if (!spec.silent) {
              stopBtn().style.display = 'none';
              const msg = lost ? 'Lost contact with the server — reload to check.'
                        : (s && s.error) ? 'Error: ' + s.error
                        : (spec.summary ? spec.summary(s) : '');
              // An error is not a summary: it stays long enough to be read, or a failed job vanishes
              // before the user knows it failed.
              if (!runTok || msg) retire(msg, (lost || (s && s.error)) ? 8000 : (spec.tail || 0));
            }
            resolve(lost ? null : s);
          }
        };
        const tick = async () => {
          if (job.ending) return;
          let s;
          try { s = await getJSON(typeof spec.statusUrl === 'function' ? spec.statusUrl() : spec.statusUrl); }
          catch (e) { settle(null, true); return; }
          if (job.ending) return;
          if (!s.running) { settle(s, false); return; }
          // `silent` does FOUR jobs, and a run needs three of them but not this one. It suppresses
          // the blocked toast, the pill's lifetime and the teardown that would hide the run's Stop
          // between libraries -- all correct inside a Job.during. It also threw the progress away,
          // which is why Rescan all showed "(1 of 5)" and nothing else while each library's file
          // count was being computed and discarded. `runProgress` opts that one back in: the run
          // still owns the pill, and the job paints into it.
          if (spec.silent && !spec.runProgress) return;
          const p = spec.progress ? spec.progress(s) : null;
          if (p) {
            if (p.pct != null) { st().classList.add('has-progress'); $('#pbarFill').style.width = p.pct + '%'; }
            if (p.text != null) $('#statusText').textContent = p.text;
          }
        };
        tick();
        poll = setInterval(tick, spec.every || 400);
      });
    },
  };
})();
// Progress arithmetic every watcher was doing by hand.
function jobPct(s) { const t = s.total || 0; return t ? Math.floor((s.seen || 0) / t * 100) : (s.running ? 0 : 100); }
function jobEta(s) {
  const done = s.seen || 0, total = s.total || 0;
  const rate = s.elapsed > 0 ? done / s.elapsed : 0;
  return (rate > 0 && total > done) ? (total - done) / rate : null;
}
// The trailing "· 1192/s · ETA 0:41", or nothing at all. Shared by every job bar, because the way
// this misleads is shared: a rate and an ETA are only worth showing while there is real time left
// to predict. Two things made them nonsense on a scan. At the end of a run they read as
// "99% · ETA 0:00" — a prediction of nothing, presented as a prediction. And on an UPDATE, where
// nearly every file is unchanged and skipped in an instant, the rate is the speed of deciding not
// to do anything (1192/s), which describes no work the user is waiting for.
// The two hide TOGETHER: they are one clause about pace, not two independent numbers, and a rate
// with no ETA beside it invites exactly the same arithmetic the ETA was doing wrong.
function jobPace(s, opts) {
  const eta = jobEta(s);
  if (eta == null || eta < 1) return '';        // jobEta guarantees elapsed > 0 when it isn't null
  const rate = (opts && opts.rate)
    ? ` · ${Math.round((s.seen || 0) / s.elapsed).toLocaleString()}/s` : '';
  return `${rate} · ETA ${fmtTime(eta)}`;
}
// A SCAN HAS TWO PHASES AND ONLY ONE OF THEM HAS A DENOMINATOR. The file total does not exist
// until the whole tree has been walked, so a first index of a big folder — worst of all a network
// share, where the walk is the slow part — sat behind a full-screen block reading "0/0 (0%)" on an
// empty bar for minutes. Zero of zero is not a small amount of progress, it is no information at
// all, and it reads as stalled at the exact moment a new user is deciding whether this works.
//
// So while total is 0 we say what is happening and show NO BAR: `pct: null` is already how Job
// says "nothing measurable yet" (it is what the ask-an-extension job returns), and the trough only
// appears once there is a real fraction to put in it. Counting is also why the elapsed clock stays
// — it is the one honest signal that work is happening.
//
// Separated from watchScan so a test can lift it; the two must not drift.
// `runLabel` is the position inside a multi-library run — "Rescanning Photos (1 of 5)". When it is
// given it REPLACES the verb rather than joining it: the run's own sentence already says what is
// happening, and "Rescanning Photos (1 of 5) Rebuilding 1,204/9,000" is two verbs for one act.
// Without it, every string here is exactly what it was, which is the point — a single library's
// scan is the thing people already know the look of.
function scanProgress(s, label, runLabel) {
  const lead = runLabel ? `${runLabel} · ` : `${label} `;
  if (!(s.total > 0)) {
    return { pct: null, text: (runLabel ? `${runLabel} · ` : '') +
                              `Counting files… · ${fmtTime(s.elapsed)} elapsed` };
  }
  return { pct: jobPct(s),
    text: `${lead}${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} (${jobPct(s)}%) · ` +
          `${fmtTime(s.elapsed)} elapsed${jobPace(s, { rate: true })}` };
}
function watchMine() {
  return Job.start({
    name: 'miner', blockedMsg: 'A job', every: 400,
    statusUrl: '/api/miner/status', stopUrl: '/api/miner/stop',
    progress: s => ({ pct: jobPct(s),
      text: `Scanning for tags ${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} (${jobPct(s)}%) · ${fmtTime(s.elapsed)} elapsed` }),
    // The RESULT is the point of this job and it renders before anything comes down, same as every
    // other job's refresh does now.
    finish: s => {
      if (!s) return;
      if (s.error) { $('#minerList').innerHTML = `<div class="miner-empty">Mine failed: ${esc(s.error)}</div>`; return; }
      state.minerCands = s.candidates || [];
      state.minerPicks = new Set();
      state.minerScanned = s.seen || 0;
      openMinerModal(true);
    },
    // Elapsed is shown deliberately: it's what tells us whether a resumable mine is worth building.
    summary: s => !s ? '' : (s.stopped
      ? `Stopped at ${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} · ${(s.found || 0).toLocaleString()} candidates · ${fmtTime(s.elapsed)}`
      : `Read ${(s.seen || 0).toLocaleString()} images in ${fmtTime(s.elapsed)} · ${(s.found || 0).toLocaleString()} candidates`),
    tail: 5000,
    restore: () => { const re = $('#minerRemine'); if (re) { re.disabled = false; re.classList.remove('busy'); re.textContent = 'Scan again'; } },
  });
}
// fresh=true resets the filter/threshold/source controls (new scan); fresh=false reopens
// the cached list untouched, preserving the user's in-progress winnowing and ticks.
function openMinerModal(fresh) {
  const n = state.minerCands.length;
  $('#minerTitle').innerHTML =
    `Suggested tags — ${n} candidate${n === 1 ? '' : 's'}` +
    ` <span class="mt-sub">from ${state.minerScanned.toLocaleString()} images</span>`;
  if (fresh) {
    $('#minerFilter').value = '';
    // Anchor the min-images threshold to the smallest count present (the server already
    // applied the configured min_count floor, so this just puts the slider at the bottom).
    $('#minerThresh').value = n ? state.minerCands[n - 1].count : 1;
    document.querySelectorAll('.minerSrc').forEach(c => { c.checked = true; });
  }
  renderMinerCandidates();
  $('#minerModal').classList.remove('hidden');
  setTimeout(() => $('#minerFilter').focus(), 30);
}
function minerVisible() {
  const q = ($('#minerFilter').value || '').trim().toLowerCase();
  const thresh = parseInt($('#minerThresh').value, 10) || 1;
  const srcs = new Set([...document.querySelectorAll('.minerSrc:checked')].map(c => c.value));
  return (state.minerCands || []).filter(c =>
    c.count >= thresh && (!q || c.tag.includes(q)) && c.sources.some(s => srcs.has(s)));
}
function minerExistingTags() {   // tag names already on this root (so we can mark them "done")
  return new Set((state._tags || []).map(t => t.name));
}
function renderMinerCandidates() {
  const rows = minerVisible(), picks = state.minerPicks || new Set();
  const existing = minerExistingTags();
  $('#minerList').innerHTML = rows.length ? rows.map(c => {
    const applied = existing.has(c.tag);   // already a tag on this root
    const checked = (applied || picks.has(c.tag)) ? ' checked' : '';
    const dis = applied ? ' disabled' : '';
    const badges = c.sources.map(s => `<span class="src-badge src-${s}" title="${s}">${s[0].toUpperCase()}</span>`).join('');
    const doneMark = applied ? '<span class="mr-applied has-ico" title="Already a tag in this library">tag</span>' : '';
    return `<label class="miner-row${applied ? ' applied' : ''}"><input type="checkbox" class="miner-pick" data-tag="${esc(c.tag)}"${checked}${dis}>` +
      `<span class="mr-tag">${esc(c.tag)}</span>${doneMark}<span class="mr-count">${c.count}</span>` +
      `<span class="mr-src">${badges}</span></label>`;
  }).join('') : '<div class="miner-empty">No candidates match.</div>';
  updateMinerStatus();
}
function updateMinerStatus() {
  const n = state.minerPicks ? state.minerPicks.size : 0;
  $('#minerStatus').textContent = n ? `${n} tag${n === 1 ? '' : 's'} selected` : 'Tick candidates to apply';
  $('#minerApply').disabled = !n;
  $('#minerApply').textContent = n ? `Apply ${n}` : 'Apply';
}
async function applyMinedTags() {
  const tags = [...(state.minerPicks || [])];
  if (!tags.length) return;
  const nImgs = (state.minerCands || []).filter(c => state.minerPicks.has(c.tag))
    .reduce((a, c) => a + c.count, 0);
  if (!(await uiConfirm(`Apply ${tags.length} tag${tags.length === 1 ? '' : 's'} to the matching images?\n` +
      `Up to ~${nImgs.toLocaleString()} image-tags added. They become normal tags.`, { ok: 'Apply' }))) return;
  const btn = $('#minerApply'); btn.disabled = true; btn.textContent = 'Applying…';
  const j = await postJSON('/api/miner/apply', { tags });
  if (j.error) {
    // "database is locked" is a QUEUE, not a fault: something else (usually a scan of a library you
    // just added) held the single writer for longer than the wait. Saying so turns an alarming
    // message into an instruction.
    uiAlert(/locked/i.test(j.error)
      ? 'Could not apply the tags — the library is busy, most likely still being scanned. Try again when the scan finishes.'
      : 'Apply failed: ' + j.error);
    updateMinerStatus(); return;
  }
  // Keep the candidates in the cached list — after loadTags() below refreshes state._tags,
  // reopening marks the just-applied ones as "✓ tag" (checked + greyed) rather than dropping
  // them, so you can see what's done vs. what's left.
  state.minerPicks = new Set();
  closeMinerModal();
  toast(`Applied ${j.tags_applied} tag${j.tags_applied === 1 ? '' : 's'} (${j.rows_added} added)`);
  await refreshView();
}
function closeMinerModal() {   // just hide — the mined list is cached so Mine can reopen it
  // Refuse while a mine is in flight: dismissing used to leave the job running with no way back
  // to its result. Stop (in the status bar) is the way out.
  if (Job.name === 'miner') return;
  $('#minerModal').classList.add('hidden');
}
// ONE ITEM, ASKING WHICH — folded 2026-09-08. There were two menu entries, Clear tags and Clear
// machine tags, and the pair was the clutter the author asked about: same verb, same noun, split by where
// the tags came from. They act on DISJOINT sets (source='user' against source='ext:*'), so neither
// was a superset of the other and "remove everything" cost two trips through the menu.
// The extension option only exists while a tagger does — with none installed there are no machine
// tags to offer, so this falls back to a plain confirm and reads exactly as it did before.
async function clearTagsLib(key) {
  const hasMachine = !Array.isArray(state.extensions)
    || state.extensions.some(e => e.active && e.produces === 'tags');
  const lib = libName(key);
  let mine = true, machine = false;
  if (hasMachine) {
    const picked = await uiChoose(`Clear tags in “${lib}”?`, [
      { id: 'mine', label: 'Tags you added', checked: true,
        hint: 'Including any applied from Mine tags.' },
      { id: 'machine', label: 'Tags an extension found',
        hint: 'These can be found again by re-running the extension.' },
    ], { ok: 'Clear tags', danger: true });
    if (!picked) return;
    mine = picked.includes('mine');
    machine = picked.includes('machine');
  } else if (!(await uiConfirm(`Remove your own tags from “${lib}”?

` +
      "Your labels and favorites are kept. Image files are not touched. This can't be undone.",
      { ok: 'Clear tags', danger: true }))) {
    return;
  }
  let removed = 0;
  if (mine) {
    const j = await postJSON('/api/tags/clear', { key });
    if (j.error) { uiAlert('Clear failed: ' + j.error); return; }
    removed += j.removed || 0;
  }
  if (machine) {
    const j = await postJSON('/api/tags/clear-machine', { key });
    if (j.error) { uiAlert('Clear failed: ' + j.error); return; }
    removed += j.removed || 0;
  }
  if (state.minerKey === key) state.minerCands = null;   // its mine postings were discarded server-side
  await loadTags();
  if (state.current && state.current.id != null) await openDetail(state.current.id);
  toast(`Cleared ${removed.toLocaleString()} tag${removed === 1 ? '' : 's'}`);
  await refreshView();
}
async function favSelected() {
  const ids = [...selection].map(Number);
  if (!ids.length) return;
  await fetch('/api/favorite', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: expandCardIds(ids), on: true }) });
  ids.forEach(id => setCardFav(id, true));
  await loadTags();
  toast(`Favorited ${ids.length}`);
}

// ---- bulk rename (find & replace) ----
function openRename() {
  if (!selection.size) return;
  $('#rnFind').value = ''; $('#rnReplace').value = '';
  $('#rnSummary').textContent = `${selection.size.toLocaleString()} file(s) selected — type text to find.`;
  $('#rnPreview').innerHTML = '';
  $('#rnApply').disabled = true;
  $('#rename').classList.remove('hidden');
  setTimeout(() => $('#rnFind').focus(), 40);
}
function closeRename() { $('#rename').classList.add('hidden'); }
const previewRename = debounce(async () => {
  const find = $('#rnFind').value;
  const ids = [...selection].map(Number);
  if (find === '') {
    $('#rnSummary').textContent = `${ids.length.toLocaleString()} file(s) selected — type text to find.`;
    $('#rnPreview').innerHTML = ''; $('#rnApply').disabled = true; return;
  }
  const data = await postJSON('/api/rename', { ids, find, replace: $('#rnReplace').value, dry_run: true });
  const changes = data.changes || 0;
  $('#rnApply').disabled = changes === 0;
  $('#rnSummary').textContent = changes
    ? `${changes.toLocaleString()} of ${ids.length.toLocaleString()} will be renamed:`
    : `No selected filenames contain “${find}”.`;
  const rows = (data.plan || []).filter(p => p.status === 'rename');
  const shown = rows.slice(0, 60);
  $('#rnPreview').innerHTML = shown.map(p =>
    `<div class="rn-row"><span class="old">${esc(p.old)}</span><span class="arrow">→</span><span class="new">${esc(p.new)}</span></div>`
  ).join('') + (rows.length > shown.length
    ? `<div class="rn-row"><span class="old">…and ${(rows.length - shown.length).toLocaleString()} more</span></div>` : '');
}, 250);
async function applyRename() {
  const find = $('#rnFind').value;
  if (find === '' || !selection.size) return;
  const ids = [...selection].map(Number);
  const btn = $('#rnApply'); btn.disabled = true; btn.textContent = 'Renaming…';
  const data = await postJSON('/api/rename', { ids, find, replace: $('#rnReplace').value });
  btn.textContent = 'Rename';
  if (data.error) { uiAlert('Rename failed: ' + data.error); btn.disabled = false; return; }
  closeRename();
  clearSelection();
  await search(true);
  const errs = (data.errors && data.errors.length) ? `, ${data.errors.length} failed` : '';
  toast(`Renamed ${(data.renamed || 0).toLocaleString()} file(s)${errs}`);
}
// Swap the detail media element: <video controls> for true videos, <img> otherwise
// (animated gif/webp animate natively in the img). Always stop any prior video first.
// Point the viewer at a new picture and RE-ARM its fade. A CSS animation runs once per element, and
// #dImg is reused for every image in the set — so the class has to come off, with a reflow between,
// or only the first image of a session would ever ease in. The class goes back on via the tag's own
// onload (see index.html), which is also what keeps the fade from being load-bearing: no load event,
// no fade, but still a visible image.
function setDetailImg(img, src) {
  img.classList.remove('in');
  void img.offsetWidth;      // flush the removal, so re-adding .in restarts the animation
  img.src = src;
}
// The detail view of a song: the same drawn face the card uses, at full size, with a transport and
// the words. Rendered rather than sourced, for the same reason the card is — there is no picture.
function renderSongDetail(d) {
  const box = $('#dSong');
  if (!box) return;
  const facts = [fmtDuration(d.duration), d.bpm ? `${d.bpm} bpm` : '', d.key || '',
                 d.vocal ? `${d.vocal} vocal` : ''].filter(Boolean).join(' · ');
  const tinted = d.genre ? ` style="--song-hue:${songHue(d.genre)}"` : '';
  const cover = d.cover_url
    ? `<img class="song-hero-cover" draggable="false" alt="" src="${esc(d.cover_url)}">` : '';
  box.innerHTML = `
    <div class="song-hero${d.genre ? ' tinted' : ''}"${tinted}>
      <div class="song-hero-head">${cover}<div class="song-hero-heading">
      <div class="song-detail-title">${esc(d.title || d.filename || '')}</div>
      ${(d.genre && d.genre !== d.title) ? `<div class="song-detail-genre">${esc(d.genre)}</div>` : ''}
      </div></div>
      ${wavePath(d.peaks)}
      ${facts ? `<div class="song-detail-facts">${esc(facts)}</div>` : ''}
      <audio id="dAudio" controls preload="metadata" src="${esc(d.file_url)}"></audio>
    </div>
    ${d.lyrics ? `<div class="song-lyrics">${esc(d.lyrics)}</div>` : ''}`;
  box.classList.remove('hidden');
  const a = $('#dAudio');
  if (a && state.autoplay) a.play().catch(() => {});
}
function hideSongDetail() {
  const box = $('#dSong');
  if (!box) return;
  box.classList.add('hidden');
  box.innerHTML = '';        // drops the <audio>, which is what actually stops playback
}

// ---- the read-only half of the detail panel shows only what it HAS -----------------------------
// A field with nothing in it was drawn anyway, as an em-dash or an empty box you could open — most
// visibly the Negative prompt, which is empty on every Flux/Krea image and every song. An empty
// labelled field reads as a fault ("why is this blank?") where an absent one reads as "this file
// doesn't carry that", which is the truth.
//
// **The cost was a CLICK, not just clutter** — the author's reason, and the better one. These sections are
// collapsed `<details>`, so a closed one gives no clue whether anything is inside it: the only way
// to find out was to open it, and the answer was usually no. A disclosure control that has to be
// operated to discover it had nothing to disclose is worse than absent.
//
// THE INVARIANT THAT BUYS, and it is worth protecting: **if a section is visible, it has content.**
// So the triangle is now a real signal rather than a question, and anything added to this panel
// later has to hold that line — a new read-only field that can be empty belongs in the list below,
// or it quietly costs the click back.
//
// THE LINE IS READ-ONLY vs EDITABLE, and it is the whole rule. Labels, Add tags, Notes and Quality
// are empty precisely until you put something there — hiding those would remove the way to fill
// them. Only fields that merely REPORT what was read from the file are in this list.
//
// Generation settings is not here because it already had this treatment (see fillGenSettings), and
// the Folder, Library, File size and Date rows are not here because they are never empty — every
// indexed file has all four.
function hideEmptyDetailFields(d) {
  const empty = [
    // [wrapper to hide, is it empty?]
    ['#dModel', () => !d.model_name],
    // Not every model has a family the rule could name — a loose checkpoint whose filename says
    // nothing recognisable comes back empty, and an empty labelled row reads as a fault.
    ['#dModelType', () => !d.model_type],
    // Pixel dimensions are not a thing a song HAS. A video keeps the row even before its poster
    // frame backfills them, because they are real and merely not measured yet.
    ['#dSizeRow', () => d.is_audio || !(d.width && d.height)],
    // A song's length is already in its hero, and an image has none — so in practice this is the
    // video row, and it appears only when the file actually said how long it is.
    ['#dDurRow', () => d.is_audio || !d.duration],
    ['#dLorasWrap', () => !(d.loras || []).length],
    ['#dPosWrap', () => !(d.positive || '').trim()],
    ['#dNegWrap', () => !(d.negative || '').trim()],
  ];
  for (const [sel, isEmpty] of empty) {
    const el = $(sel); if (!el) continue;
    // A .kv row is hidden as a whole; a <details> is its own wrapper already.
    (el.closest('.kv') || el).classList.toggle('hidden', !!isEmpty());
  }
}

function showDetailMedia(d) {
  const img = $('#dImg'), vid = $('#dVid');
  // Nothing to stop here — openDetail has already called stopDetailMedia(). This function SHOWS.
  // Offline library: /file/ 404s, so show the CACHED THUMBNAIL rather than a broken-image icon.
  // It's low-res, which the banner explains — far better than nothing, and a video can't play at
  // all, so it falls back to its poster thumb too.
  if (isRootOffline(d.root_key)) {
    vid.classList.add('hidden');
    // A song has no cached thumbnail to fall back to, but its face is drawn from indexed values —
    // so it still renders in full, and only the transport is dead. Better than a broken image.
    if (d.is_audio) {
      img.classList.add('hidden'); img.removeAttribute('src');
      renderSongDetail(d);
      return;
    }
    // NOT-DRAGGABLE (see renderFilmstrip): this is the cached THUMBNAIL standing in for an
    // unreachable original, so a drag would hand ComfyUI a small WebP with no workflow in it.
    img.draggable = false;
    img.classList.remove('hidden'); setDetailImg(img, d.thumb_url);
    return;
  }
  if (d.is_audio) {
    img.classList.add('hidden'); img.removeAttribute('src');
    vid.classList.add('hidden');
    renderSongDetail(d);
  } else if (d.is_video) {
    img.classList.add('hidden'); img.removeAttribute('src');
    vid.src = d.file_url; vid.classList.remove('hidden');
    if (state.autoplay) vid.play().catch(() => {});
  } else {
    vid.classList.add('hidden');
    img.draggable = true;                 // the real file — drag-to-ComfyUI works from here
    img.classList.remove('hidden'); setDetailImg(img, d.file_url);
  }
}
// PUT AWAY WHATEVER WAS PLAYING, before anything decides what to show next.
// The author, 2026-09-10: "when I view an audio/song file, then hit right arrow to a set card, the song
// keeps playing." It did — and so did navigating from a song to a merged still+video pair, which
// nobody had hit yet. Both are the same fault. openDetail picks one of three things to put in the
// media pane (a set's panes, a pair's video, or the ordinary image/video/song), and each of the
// three was independently responsible for silencing the other two. showDetailMedia remembered;
// renderSetView and playPairVideo each remembered the video and forgot the song.
// That is not three bugs to fix, it is one job in the wrong place. Stopping is now done ONCE by the
// caller, before the branch, so a fourth kind of media cannot reintroduce this by being written the
// same way the first three were.
function stopDetailMedia() {
  stopDetailVideo();
  hideSongDetail();   // drops the <audio>, which is what actually stops playback
}
function stopDetailVideo() {
  const vid = $('#dVid');
  if (!vid) return;
  try { vid.pause(); } catch (e) {}
  vid.removeAttribute('src'); vid.load();   // release the file + stop buffering/audio
}
// Merged pair: play the video in the main viewer (metadata still comes from the paired still).
function playPairVideo(videoId) {
  const img = $('#dImg'), vid = $('#dVid');
  img.classList.add('hidden'); img.removeAttribute('src');
  vid.src = '/file/' + videoId + '?r=' + curRootKey();   // per-item root of the open detail
  vid.classList.remove('hidden');
  if (state.autoplay) vid.play().catch(() => {});
}
// The small draggable PNG beside a playing video, so drag-to-ComfyUI works in the detail view too.
// The <video> itself can never be the drag source — no browser hands a video out as a file — so the
// handle IS the feature, not a decoration.
//
// Two sources, one component. A PAIRED video has a real still saved beside it, and that still is
// the workflow-bearing original (/file/). A LONE video has no still, so it borrows the same picture
// its card drags: its own first frame, carrying its own workflow (/dragpng/). Only the label
// differs, because calling a decoded frame a "source still" would be a small lie.
// ONE SENTENCE, ONE COPY. What the picture IS changes with the source; the gesture never does, and
// it had been spelled out four times (three here plus a static default in index.html). `name` is a
// literal from the call sites below, never user data -- nothing here is escaped.
function dragSourceLabel(name) { return `${name}<br><span>Drag to ComfyUI</span>`; }
function setPairSource(still) {
  const box = $('#dPairSrc'); if (!box) return;
  $('#dPairThumb').src = still.file_url || ('/file/' + still.id + '?v=' + Math.floor(still.mtime || 0) + '&r=' + curRootKey());
  $('#dPairSrcLabel').innerHTML = dragSourceLabel('Still');
  box.classList.remove('hidden');
  setPairStills(still.pair_stills, still.id);
}
// A run often makes SEVERAL stills and then animates them, and this box only ever showed the one
// that fronts the card — so a three-frame run looked exactly like a lone video (BR-11, raised by
// The author 2026-08-07). Now it steps.
//
// THE ASK WAS VISIBILITY, NOT CULLING — the author's words were "I wouldn't cull them, but would want to
// know they are there". So the video keeps playing, the metadata does not move (one run, one set of
// settings, so stepping a still would change nothing it shows), and the only thing that changes is
// which picture is displayed and dragged. Deliberately NOT the side-by-side keep-one panes an image
// set opens: those exist to choose between alternatives, and these are not alternatives.
//
// The box is the DRAG HANDLE first — that is the job it was built for — so stepping swaps the whole
// file_url, never just the id: the dropped file takes its name from that URL (test_drag_name.py).
function setPairStills(stills, curId) {
  const nav = $('#dPairSrcNav'), box = $('#dPairSrc');
  if (!nav || !box) return;
  state.pairStills = (Array.isArray(stills) && stills.length > 1) ? stills : null;
  state.pairIdx = 0;
  if (state.pairStills) {
    const at = state.pairStills.findIndex(m => m.id === curId);
    state.pairIdx = at === -1 ? 0 : at;   // open on the still that fronts the card, not on the first
  }
  box.classList.toggle('stepped', !!state.pairStills);
  nav.classList.toggle('hidden', !state.pairStills);
  if (state.pairStills) renderPairStill();
}
// Wraps at both ends rather than disabling there: the count sits between the two arrows and already
// says where you are, so a dead button would explain less than it costs (cf. DESIGN.md on reserving
// `disabled` for genuinely unavailable, not merely at-a-boundary).
function stepPairStill(by) {
  const n = (state.pairStills || []).length; if (!n) return;
  state.pairIdx = (state.pairIdx + by + n) % n;
  renderPairStill();
}
function renderPairStill() {
  const m = (state.pairStills || [])[state.pairIdx]; if (!m) return;
  $('#dPairThumb').src = m.file_url;
  $('#dPairThumb').title = m.filename + ' — drag into ComfyUI to load the workflow';
  $('#dPairPos').textContent = (state.pairIdx + 1) + ' / ' + state.pairStills.length;
}
function setVideoSource(d) {
  const box = $('#dPairSrc'); if (!box) return;
  setPairStills(null);
  $('#dPairThumb').src = originalUrlFor(d.thumb_url, d.filename, 'video');
  $('#dPairSrcLabel').innerHTML = dragSourceLabel('Frame');
  box.classList.remove('hidden');
}
// A song, same component and same reason as a video: it cannot be dragged as itself, so the handle
// is the picture that carries its workflow. The label names what the picture IS, because calling a
// drawn waveform "cover art" would be a small lie on a song that has none.
function setSongSource(d) {
  const box = $('#dPairSrc'); if (!box) return;
  setPairStills(null);
  $('#dPairThumb').src = originalUrlFor(d.thumb_url, d.filename, 'audio');
  $('#dPairSrcLabel').innerHTML = dragSourceLabel(d.cover_url ? 'Cover' : 'Waveform');
  box.classList.remove('hidden');
}
function hidePairSource() {
  const box = $('#dPairSrc'); if (box) box.classList.add('hidden');
  setPairStills(null);
  const t = $('#dPairThumb'); if (t) t.removeAttribute('src');
}

// ---- image-set keep-one cull view ----
// Render the set members side by side. Nothing is marked initially; marking a keeper (digit/click)
// dims the others and enables the Recycle button. Delete (or the button) recycles the non-kept ones.
function renderSetView(members) {
  const img = $('#dImg'), vid = $('#dVid');
  img.classList.add('hidden'); img.removeAttribute('src');
  vid.classList.add('hidden');
  const box = $('#dSetView');
  box.innerHTML =
    `<div class="set-panes">` +
    members.map((m, i) => {
      const label = m.role ? m.role.toUpperCase() : ('#' + (i + 1));
      // The facts you actually compare when culling — which one is the upscale, which is heavier —
      // rather than something to infer by eye. Muted and after the stage, so the stage still reads
      // first; the pane can be narrow, so the line ellipsises and carries the whole thing as a title.
      // ORDER IS DELIBERATE: size and resolution before the filename, though the request said the
      // other way round. A pane is a third of the media column, so this line ellipsises at any real
      // window — and with the filename first, the two facts you are actually comparing are the ones
      // that get cut. The filename is also the least comparable part: members of a set share a
      // prefix and differ only by the stage already shown in bold.
      const facts = [(m.width && m.height) ? `${m.width}×${m.height}` : null,
                     m.size != null ? fmtBytes(m.size) : null,
                     m.filename].filter(Boolean).join(' · ');
      // The score sits top-RIGHT, opposite the digit key, and uses the grid card's own .reward
      // recipe -- the same gold badge meaning the same thing two clicks apart. Absent, not zero,
      // where a member has never been scored: a set part-way through a run must not imply that a
      // blank pane scored badly.
      const score = m.reward != null
        ? `<span class="reward" title="pyiqa quality score (1-10) — raw ${m.reward.toFixed(3)}">${metricTo10(m.reward).toFixed(1)}</span>`
        : '';
      return `<figure class="set-pane" data-idx="${i}">
        <span class="set-key">${i + 1}</span>${score}
        <img src="${m.file_url || ('/file/' + m.id + '?r=' + curRootKey())}" alt="${esc(m.filename)}">
        <figcaption title="${esc(label + ' · ' + facts)}"><b>${esc(label)}</b>${facts ? ' · <span>' + esc(facts) + '</span>' : ''}</figcaption>
      </figure>`;
    }).join('') +
    `</div>` +
    `<div class="set-bar">` +
      // SHORT ON PURPOSE. It said "Press 1–2 or click to mark the keeper · Delete recycles the
      // others — or the whole set when nothing is marked" — three facts, and the author, 2026-09-10:
      // "once someone knows how it works, we don't need all that explanation."
      // What went: "Press", which the bold key notation already says; and the whole-set clause,
      // which is the ONE case here that raises a confirm naming exactly what it is about to do
      // (recycleSetFull). A hint should carry what you cannot find out safely by trying.
      `<span class="set-hint"><b>1–${members.length}</b> or click marks the keeper · <b>Delete</b> recycles the rest</span>` +
      `<div class="set-cull">` +
        `<label class="label-row"><input type="checkbox" id="setCullFolder" disabled> Apply to folder</label>` +
        `<span class="set-cull-count" id="setCullCount"></span>` +
      `</div>` +
      `<button id="dSetRecycle" class="danger" disabled>Recycle others</button>` +
    `</div>`;
  box.classList.remove('hidden');
  $('#dSetRecycle').addEventListener('click', recycleSetOthers);
  $('#setCullFolder').addEventListener('change', setCullPreview);
}
// One press per folder. After a cull has run, the box stays dead for every set in the SAME folder —
// the decision has already been applied there, and a second press could only act on what the first
// one left behind. Opening a set in a different folder is a new question, so it comes back.
function setCullSpent() {
  const c = state.current;
  return !!(state.culledFolder && c && state.culledFolder === (c.root_key + '/' + c.folder));
}
// "Apply to folder": count how many sets in this folder match the marked keeper's role, and say so
// under the checkbox. The COUNT is all this does — the checkbox is a scope switch, not an action, so
// Recycle stays live either way and pressing it is what commits (see recycleSetOthers).
// state.setCull holds the last preview, so the confirm quotes the numbers that were on screen.
async function setCullPreview() {
  const cb = $('#setCullFolder'), out = $('#setCullCount');
  if (!cb || !out) return;
  state.setCull = null;
  // Unticked says nothing — except where the box is dead because this folder has already had its
  // cull, which otherwise looks like a broken control.
  if (!cb.checked) { out.textContent = setCullSpent() ? 'Applied to this folder already' : ''; return; }
  const keeper = (state.setMembers || []).find(m => m.id === state.setKeep);
  if (!keeper) { cb.checked = false; return; }
  out.textContent = 'Counting…';
  try {
    const d = await getJSON(`/api/setcull/preview?id=${keeper.id}&keep=${encodeURIComponent(keeper.role || '')}`);
    // getJSON doesn't throw on a 4xx — the server's stated reason is in the body, and it is a
    // better thing to show than a generic failure ("that library is offline").
    if (d.error) { out.textContent = d.error; cb.checked = false; return; }
    // The count always includes THIS set, so it can never be 0 — a "nothing matched" branch here
    // would be dead code. `1/4 sets` is the honest way to say "nothing else in here matches".
    state.setCull = d;
    out.textContent = `${d.sets}/${d.total_sets} sets`;
  } catch (e) {
    out.textContent = 'Could not count this folder';
    cb.checked = false;
  }
}
// Press Recycle with the box ticked: the same keep decision, applied to every matching set in the
// folder. Confirms with the FILE count (the count line reads in sets; the bin takes files), then runs
// as a normal background job — scrim, status bar, Stop — committed one set at a time.
async function recycleSetFolder() {
  const d = state.setCull, keeper = (state.setMembers || []).find(m => m.id === state.setKeep);
  if (!d || !keeper) return;
  // NOT gated by confirmRecycle: this reaches every matching set in the FOLDER, most of them never
  // on screen, and this dialog is the only place the FILE count appears.
  const ok = await uiConfirm(
    `Keep the ${(d.keep_role || 'marked').toUpperCase()} in ${d.sets} set${d.sets === 1 ? '' : 's'} ` +
    `in this folder and recycle the other ${d.files} file${d.files === 1 ? '' : 's'}?`,
    { ok: 'Recycle', danger: true });
  if (!ok) return;
  const r = await postJSON('/api/setcull', { id: keeper.id, keep: keeper.role });
  if (r.error) { uiAlert('Could not start: ' + r.error); return; }
  const culledFolder = d.root_id + '/' + d.folder;
  closeDetail();
  // Say what this job is BEFORE the first status poll: the pill shows immediately but its text is
  // whatever the last job left there, so a short run can flash "Indexing…" and never correct it.
  Job.say(`Culling ${d.sets} sets in this folder…`);
  await Job.start({
    name: 'setcull', blockedMsg: 'A job', blocking: true, every: 400,
    statusUrl: '/api/setcull/status', stopUrl: '/api/setcull/stop',
    progress: s => ({ pct: jobPct(s), text: `Culling set ${s.seen || 0}/${s.total || 0} in ${s.folder || 'this folder'}` }),
    finish: async s => {
      if (!s || s.error) return;
      // One press per folder: the decision has been applied, and a second identical press could
      // only ever act on what the first one left behind.
      state.culledFolder = culledFolder;
      await refreshView({ afterJob: true });
    },
    summary: s => !s ? '' : (s.error ? '' :
      `${s.stopped ? 'Stopped — kept' : 'Kept'} ${s.seen || 0} of ${s.total || 0} sets · recycled ${s.moved || 0}` +
      ((s.purged || 0) ? ` · ${s.purged} already gone from disk` : '') +
      ((s.failed || 0) ? ` · ${s.failed} failed` : '')),
    tail: 5000,
  });
}
function hideSetView() {
  const box = $('#dSetView'); if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
  state.setMembers = null; state.setKeep = null;
}

// ---- Maximized view ----------------------------------------------------------------------------
// `f`, or the ⤢ in the corner: take the filmstrip, the metadata panel and the notes strip away and
// give the picture the whole window. The SIZING is entirely CSS (.detail.chrome-off) — three
// display:none rules, after which .detail-img and .set-pane take the reclaimed space by themselves.
// Nothing here measures anything.
// What this owns is the one real behaviour change: inside a SET the arrows stop walking the result
// list and start flipping the set's own members, one at a time in the same box. That is the whole
// point of the mode — the author compares sharpness across a run's members, and two pictures in the same
// screen position, swapped, is a far better test of that than two pictures side by side. It also
// hands the loupe a synced magnifier for nothing: hold the cursor on an eye, tap →, and the lens is
// already over the same spot of the next member because neither the lens nor the box moved.
const Maxi = (() => {
  let on = false, idx = 0;

  // A "set" here means one worth flipping through. A single-member set is a single image.
  const members = () => (Array.isArray(state.setMembers) && state.setMembers.length > 1) ? state.setMembers : null;

  // NO QUALITY SCORE IN THIS MODE, and that is the point of the mode. The header used to carry the
  // badge for parity with the set panes it replaces -- but .detail.chrome-off already hides the
  // panes' own rewards, so the header was the last score standing in a view built for judging with
  // your eyes. The author, 2026-09-15: "take scoring out. no need for it here. the entire point is-
  // this is by eye." A number beside the picture is an answer offered before you have looked.
  // One keycap. The two arrows are DRAWN (see .keycap in style.css); everything else is its letter.
  const key = k => k === '<' ? '<span class="keycap k-left"></span>'
                 : k === '>' ? '<span class="keycap k-right"></span>'
                 : `<span class="keycap">${esc(k)}</span>`;
  const act = (keys, label) => `<span class="cap-act">${keys.map(key).join('')}` +
                               `<span class="lab">${esc(label)}</span></span>`;

  // The only thing left on screen besides the picture, and the reason the mode is usable at all:
  // The author's ask was that he can still tell which file he is looking at without the metadata panel.
  function caption() {
    const cap = $('#dMaxiCap'); if (!cap) return;
    // A video owns the bottom of its own box with native controls, so a bar there would sit on top
    // of them. Maximizing still works for video — it just has no caption. Same for a song, which is
    // a drawn card carrying its own title already.
    const drawn = !$('#dVid').classList.contains('hidden') || !$('#dSong').classList.contains('hidden');
    if (!on || drawn) { cap.classList.add('hidden'); cap.innerHTML = ''; return; }
    const ms = members();
    let lead, facts, right;
    if (ms) {
      const m = ms[idx];
      // THE STAGE LEADS AND THE FILENAME IS REFERENCE. Within a set every member's name is the same
      // string but for the stage token, so the name repeats what the bold word already said. The
      // author: "the STAGE is most important, e.g. Raw, Detail, etc. filename should match, so not
      // as important, yes?" It stays on the line -- it is still how you know WHICH run you are in --
      // but behind the dimensions rather than in front of them.
      lead  = m.role ? m.role.toUpperCase() : ('#' + (idx + 1));
      facts = [(m.width && m.height) ? `${m.width}×${m.height}` : null,
               m.filename].filter(Boolean).join(' · ');
      right = `<span>${idx + 1} of ${ms.length}</span>`;
    } else {
      // A SINGLE IMAGE HAS NO STAGE, NO COUNTER AND NOTHING TO KEEP, so it collapses to the least
      // that still says what you are looking at -- the author's pick, put to him as three options.
      const d = state.current || {};
      lead  = d.filename || '';
      facts = (d.width && d.height) ? `${d.width}×${d.height}` : '';
      right = '';
    }
    // THE ACTION ROW IS THE WHOLE REASON K IS DISCOVERABLE. There is no other chrome in this mode,
    // so a key nobody is told about is a key nobody uses -- the author asked for the hint in the same
    // breath as the key itself. Only where there IS a set: a single image has no others to recycle
    // and its arrows behave the way they do everywhere else, so it gets the identity row alone.
    const others = ms ? ms.length - 1 : 0;
    const idRow =
      `<span class="cap-row"><b>${esc(lead)}</b>` +
        (facts ? `<span class="cap-facts">· ${esc(facts)}</span>` : '') +
        (right ? `<span class="cap-right">${right}</span>` : '') +
      `</span>`;
    // HELD (see _keepHold): the arrows have just gone quiet and this is the only surface that can
    // say so. Exactly the fault we fixed for the set case -- a key that silently changes what it
    // does, with nothing on screen to explain it -- so the hold gets the same treatment, in the same
    // shape as the row it replaces rather than as a sentence.
    if (_keepHold) {
      cap.innerHTML = idRow +
        `<span class="cap-acts"><span class="cap-act"><span class="lab">Kept.</span></span>` +
        act(['Shift', '<', '>'], 'Next card') + `</span>`;
      cap.classList.remove('hidden');
      return;
    }
    // ALL THREE, because the arrows silently change hands in this view and nothing said so. The
    // author found it the hard way: cull a set, arrow onward, land on another set, and the arrows
    // start flipping ITS members -- wrapping forever, so the filmstrip appears to stop dead. The
    // gesture that gets you out is the one thing the view could not tell you about.
    const acts = others
      ? `<span class="cap-acts">` +
          act(['<', '>'], 'Step this set') +
          act(['Shift', '<', '>'], 'Next card') +
          act(['K'], `Keep this one, recycle the other${others === 1 ? '' : ' ' + others}`) +
        `</span>`
      : '';
    cap.innerHTML = idRow + acts;
    cap.classList.remove('hidden');
  }

  // A CLASS SWAP, NOT A RELOAD. Every member's <img> is already in the DOM and decoded from the
  // side-by-side render, so flipping is instant — which is the difference between a blink
  // comparison and a slideshow, and the reason this shows/hides panes instead of retargeting #dImg.
  function panes() {
    const ms = members();
    document.querySelectorAll('#dSetView .set-pane').forEach(p =>
      p.classList.toggle('maxi-on', !!ms && on && Number(p.dataset.idx) === idx));
  }

  // The arrows change meaning in a maximized set, so they have to change what they SAY.
  // The size controls do NOT need this any more: since 2026-09-15 they are two buttons, #dMaxiBtn
  // in the actions row to go in and #dMaxi in the corner to come out, each visible only in the mode
  // where its one label is true. The branch that used to rewrite the corner button's title and
  // aria-label on every toggle went with it.
  function chrome() {
    const setwise = on && !!members();
    const p = $('#dPrev'), n = $('#dNext');
    if (p) p.title = setwise ? 'Previous in this set (←)' : 'Previous (←)';
    if (n) n.title = setwise ? 'Next in this set (→)'     : 'Next (→)';
  }

  function set(v) {
    const was = on;
    on = !!v;
    const d = document.querySelector('.detail');
    if (d) d.classList.toggle('chrome-off', on);
    panes(); caption(); chrome();
    // The strip was display:none while maximized, so the centring it does on every step ran against
    // a zero-width box. Re-run it on the way OUT, or the strip comes back scrolled to wherever it
    // was when the mode started. Only on a real transition — openDetail already syncs it once.
    // LEAVING focus view drops the magnifier with it (2026-09-15). The lens is for inspecting one
    // picture filling the screen; carrying it back out to a view that is mostly metadata means the
    // next ←/→ lands under a lens nobody asked for. Entering does NOT raise it — going in is not a
    // request to magnify.
    if (was && !on) { syncFilmstrip(); releaseKeepHold(); Loupe.off(); }   // both are focus-view state
    Loupe.refresh();
  }

  return {
    get on() { return on; },
    get index() { return idx; },          // which member is on screen; K needs it
    // "Maximized AND looking at a set" — the state in which the arrows and digits change hands.
    get setwise() { return on && !!members(); },
    toggle() { set(!on); },
    off() { set(false); },
    // Every open starts unmaximized on the first member: the mode answers a question about one
    // thing, it is not a way you leave the viewer set up. The author's call.
    reset() { idx = 0; set(false); },
    // A NEW IMAGE UNDER AN UNCHANGED MODE, i.e. you pressed ←/→ while maximized. Keep `on`, start the
    // new set at its first member, and repaint: without this the caption would still describe the
    // file you just left, and no pane in the new set would be marked visible — a blank box.
    rebind() { idx = 0; set(on); },
    // WRAPS on purpose — 1→2→3→1. Comparing two members means flipping back and forth between them,
    // and stopping dead at the last one makes every other press a reach for the other arrow.
    // Returns false when it is not this function's business, so the caller falls through to navDetail.
    step(dir) {
      const ms = members(); if (!on || !ms) return false;
      idx = (idx + dir + ms.length) % ms.length;
      panes(); caption();
      Loupe.refresh();     // the picture changed under a lens that did not move — that IS the compare
      return true;
    },
    jump(i) {
      const ms = members(); if (!on || !ms || i < 0 || i >= ms.length) return false;
      idx = i; panes(); caption(); Loupe.refresh();
      return true;
    },
    recaption: caption,
  };
})();
// Mark pane `idx` as the keeper; marking the same pane again UNMARKS it. Exactly one of
// the two recycle buttons is active at a time: marked -> "Recycle others" (panes bar);
// unmarked -> "Recycle set" (right panel, whole set behind a confirm).
function markSetKeep(idx) {
  const members = state.setMembers;
  if (!members || idx < 0 || idx >= members.length) return;
  const unmark = state.setKeep === members[idx].id;
  state.setKeep = unmark ? null : members[idx].id;
  document.querySelectorAll('#dSetView .set-pane').forEach(p => {
    const keep = !unmark && Number(p.dataset.idx) === idx;
    p.classList.toggle('keep', keep);
    p.classList.toggle('drop', !unmark && !keep);
  });
  const btn = $('#dSetRecycle');
  if (btn) { btn.disabled = unmark; btn.textContent = unmark ? 'Recycle others' : `Recycle other ${members.length - 1}`; }
  const del = $('#dDelete');
  if (del) del.disabled = !unmark;
  // "Apply to folder" needs a demonstration to repeat, so it's dead until a keeper is marked — and
  // any change of keeper invalidates a count that was about the previous role.
  const cull = $('#setCullFolder');
  if (cull) { cull.disabled = unmark || setCullSpent(); cull.checked = false; setCullPreview(); }
}
// Whole-set recycle (the right panel's "Recycle set" while nothing is marked): confirm,
// then reuse deleteCurrent, which recycles all group members and advances the grid.
// Gated: renderSetView() draws every member as a pane, so this restates what you can see.
async function recycleSetFull() {
  const members = state.setMembers;
  if (!members) return;
  const ok = await confirmRecycle(`Recycle this entire set — all ${members.length} images?`,
                                  { ok: 'Recycle set', danger: true });
  if (ok) deleteCurrent();
}
// Build a grid item (cardHTML shape) for a single image id, from its /api/image record.
async function gridItemFor(id) {
  const d = await getJSON('/api/image/' + id);
  return {
    id: d.id, filename: d.filename, folder: d.folder,
    width: d.width, height: d.height, model: d.model_name,
    has_meta: d.has_meta, fav: d.favorite ? 1 : 0,
    reward: d.quality ? d.quality.reward : null,
    motion: d.motion, is_video: d.is_video,
    group_count: 1, video_id: null, group_members: null, is_set: false,
    has_note: !!d.note, note: d.note || null,
    thumb_url: d.thumb_url,
  };
}
async function recycleSetOthers() {
  const members = state.setMembers, keep = state.setKeep;
  if (!members || keep == null) return;                      // no-op until a keeper is marked
  // The checkbox is a SCOPE switch, not a second button: same press, same decision, wider reach.
  const cb = $('#setCullFolder');
  if (cb && cb.checked) return recycleSetFolder();
  const drop = members.filter(m => m.id !== keep).map(m => m.id);
  if (!drop.length) return;
  await Undo.commitNow();          // only one batch is ever undoable; settle the previous one
  const r = await fetch('/api/delete', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: drop }) });
  const j = await r.json();
  if (j.error) { uiAlert('Recycle failed: ' + j.error); return; }
  // Queued, not done — a late failure surfaces via Undo's result check.
  const keptRole = (members.find(m => m.id === keep) || {}).role;
  drop.concat([keep]).forEach(id => selection.delete(String(id)));
  // Replace the set's grid card (its representative, which for a kept REFINE IS the keeper)
  // with the kept image as a normal singleton — previously the card was removed, so the
  // keeper disappeared from the grid until a manual refresh.
  const repId = state.current ? state.current.id : keep;
  const idx = state.items.findIndex(x => String(x.id) === String(repId));
  const oldCard = document.querySelector(`.card[data-id="${repId}"]`);
  const oldItem = idx !== -1 ? state.items[idx] : null;
  let kept = null;
  try { kept = await gridItemFor(keep); } catch (e) { /* fall back to removal below */ }
  if (state.rootFiles != null) state.rootFiles = Math.max(0, state.rootFiles - drop.length);
  let restore;
  if (kept && idx !== -1) {
    // The set card becomes the keeper's card: no net card change, so the counters
    // (which count CARDS on both sides of the "/") stay as they are.
    state.items[idx] = kept;
    if (oldCard) oldCard.outerHTML = cardHTML(kept);
    // The FILMSTRIP carries the same stacked-cards badge the card does, and it does not repaint
    // itself here: renderFilmstrip is keyed on the list's length and its two end ids, and a cull
    // replaces one item in place — same length, usually the same ends — so the key is unchanged and
    // the whole render is skipped. The author, 2026-09-11: the set icon stayed on the strip after culling
    // to one image. The keeper's id may differ from the card's, hence the second argument.
    refreshStripItem(kept.id, repId);
    // Undo here is a REPLACE back, not an insert: this path swapped the set card for the keeper's
    // rather than removing anything, so restoring means putting the SET card back where it was.
    restore = () => {
      state.items[idx] = oldItem;
      const cur = document.querySelector(`.card[data-id="${kept.id}"]`);
      if (cur) cur.outerHTML = cardHTML(oldItem);
      refreshStripItem(oldItem.id, kept.id);   // and the badge comes back with the set
      if (state.rootFiles != null) state.rootFiles += drop.length;
      // AND THE OPEN VIEW, which this used to leave behind. Undo put the grid card back and nothing
      // else: state.setMembers stays null, because the last openDetail ran on the kept SINGLE and a
      // single has no members. So the set view did not come back and the arrows had nothing to step
      // through -- the author, having culled from the zoomed view: "after undo, I can't use the
      // arrows to move between two images from 1 run (e.g. Raw and Detail)."
      // Safe to re-fetch here: Undo.run() awaits the server's restore before calling this, so the
      // files are back and /api/image returns the set with its members again.
      // ONLY IF YOU ARE STILL LOOKING AT IT. With the Keep behavior set to "next" the view has
      // already moved on to another card, and yanking it back to the set you just undid would be a
      // second surprise on top of the first.
      // AND THE ARROW HOLD GOES WITH IT. Keeping from the focus view parks the arrows on purpose --
      // you have just decided something about this picture, so a reflexive left/right must not carry
      // you off it, and the caption says "Kept · Shift + arrows for the next card". Undo never
      // released that, so the set came back and the arrows stayed dead on top of it, under a caption
      // still claiming a keep that no longer exists. The author: "it stays on the Kept card, but you
      // can't scroll after... it is confusing."
      // Unconditional: the hold only exists because of the keep being undone here, and with the Keep
      // behaviour on "next" it was never set in the first place.
      // BEFORE the reopen, so the caption openDetail rebuilds is the honest one.
      releaseKeepHold();
      const showing = state.current ? String(state.current.id) : null;
      const wasThisSet = showing && (showing === String(kept.id) || drop.map(String).includes(showing));
      if (wasThisSet && !$('#overlay').classList.contains('hidden')) openDetail(oldItem.id);
    };
  } else {
    if (idx !== -1) state.items.splice(idx, 1);
    if (oldCard) oldCard.remove();
    state.total = Math.max(0, state.total - 1);   // couldn't rebuild the keeper's card
    if (state.rootTotal != null) state.rootTotal = Math.max(0, state.rootTotal - 1);
    restore = undoRestorer(oldItem ? [{ idx, item: oldItem }] : [],
                           { total: 1, rootTotal: 1, rootFiles: drop.length });
  }
  renderCount();
  updateSelBar();                                   // the total moved; the bar reads the total
  if (!state.items.length) showNoMatchHint();       // culled the last card — see deleteSelected
  RecycleDebt.soon();
  Undo.offer(j.batch, `Kept ${keptRole ? keptRole.toUpperCase() : 'one'}, ${drop.length} to bin`,
             (j.window || 5) * 1000, restore);
  // Post-keep (Keep behavior setting): "stay" reopens the kept single in place; "next" (default)
  // advances to the following card. The kept card sits AT idx, so the next card is idx+1.
  if (state.keepBehavior === 'stay' && kept && idx !== -1) {
    await openDetail(kept.id);
    // AFTER the reopen, not before: openDetail re-enters the maximized mode and would clear it.
    // Only in the focus view — outside it the arrows were never the problem.
    _keepHold = Maxi.on;
    Maxi.recaption();            // the caption is the only thing that can say the arrows are held
    return;
  }
  if (idx === -1 || state.items.length === 0) { closeDetail(); return; }
  const nextIdx = (kept && idx !== -1) ? idx + 1 : idx;   // on failure the array shrank, so idx is already the next
  if (nextIdx >= state.items.length && !state.done) { await search(false); }   // page in more if stepping off the end
  if (nextIdx >= state.items.length) { closeDetail(); return; }
  openDetail(state.items[nextIdx].id);
}


// ---- Loupe -------------------------------------------------------------------------------------
// A circle of the image at its own pixels, for checking hands and faces while culling. Costs no
// server work at all: the detail view already loads the full original (/file/, not a thumbnail), so
// the pixels are in the browser and this is a background-position, not a fetch. It works on the
// side-by-side set panes for free, because those are plain <img> inside .detail-img too — which is
// the case it was asked for.
const Loupe = (() => {
  const SIZE = 390;                    // lens diameter
  const MIN_ZOOM = 2;                  // never less than 2x, however large the image is drawn
  // Zoom is a multiple of the image's OWN pixels, not of however large it happens to be drawn — so
  // level 1 is 1:1 with the file and level 2 is genuinely double, whatever the window size. A fixed
  // screen multiple would mean the same setting showed different detail on different images.
  const LEVELS = [1, 2, 3, 4];
  let pinned = false, held = false, level = 0, last = null;   // last = {x, y} in client coords

  const el = () => document.getElementById('dLoupe');
  const active = () => pinned || held;
  // The step readout, created on first use rather than sitting in index.html: it has no meaning
  // outside a raised lens, and an empty box in the markup is one more thing to remember to hide.
  function badge() {
    const lens = el(); if (!lens) return null;
    let b = lens.firstElementChild;
    if (!b) { b = document.createElement('div'); b.className = 'loupe-lvl'; lens.appendChild(b); }
    return b;
  }

  // Where the PICTURE actually is, which is not where the <img> is: object-fit: contain letterboxes
  // it, so the element box is wider or taller than the pixels. Mapping the cursor through the
  // element box instead makes the lens drift off-target on any image whose shape differs from its
  // pane — invisible on a square test image, obvious on a portrait one.
  function picture(img) {
    const r = img.getBoundingClientRect(), nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh || !r.width || !r.height) return null;
    const k = Math.min(r.width / nw, r.height / nh), w = nw * k, h = nh * k;
    return { x: r.left + (r.width - w) / 2, y: r.top + (r.height - h) / 2, w, h, nw, nh };
  }

  function draw() {
    const lens = el();
    if (!lens) return;
    if (!active() || !last) return hide();
    const img = document.elementFromPoint(last.x, last.y);
    if (!img || img.tagName !== 'IMG' || !img.closest('.detail-img')) return hide();
    const p = picture(img);
    if (!p) return hide();
    const fx = (last.x - p.x) / p.w, fy = (last.y - p.y) / p.h;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return hide();   // over the letterbox, not the picture
    // At least MIN_ZOOM, and at least 1:1 with the file's own pixels — so a big image shown small
    // gets more magnification automatically, which is the whole point when checking for artefacts.
    const zoom = Math.max(MIN_ZOOM, p.nw / p.w) * LEVELS[level];
    const bw = p.w * zoom, bh = p.h * zoom;
    lens.style.backgroundImage = `url("${img.currentSrc || img.src}")`;
    lens.style.backgroundSize = `${bw}px ${bh}px`;
    lens.style.backgroundPosition = `${SIZE / 2 - fx * bw}px ${SIZE / 2 - fy * bh}px`;
    const top = last.y - SIZE / 2;
    lens.style.width = lens.style.height = SIZE + 'px';
    lens.style.left = (last.x - SIZE / 2) + 'px';
    lens.style.top = top + 'px';
    const b = badge();
    if (b) {
      b.textContent = LEVELS[level] + '×';
      // Above the circle, unless the circle is near the top of the window and the label would be
      // clipped — then below it instead. 24px is the label's own height plus its gap; measuring it
      // would be a layout read on every mousemove for a box whose size never changes.
      b.classList.toggle('below', top < 24);
    }
    lens.classList.add('on');
  }

  function hide() { const l = el(); if (l) l.classList.remove('on'); }

  function sync() {
    // `.on` is the action bar's EXISTING filled state — not a new class. Since #dHide moved up to
    // the title row (2026-08-26) this is the bar's ONLY user of it; the title row's two marks
    // carry the same `.on` under .mark-toggle, and #dHide's on-state is drawn to match this one.
    // A `.da-btn.active` was written first and silently did nothing: the shared
    // `.detail-actions .da-btn` rule has the same specificity and is declared later, so it won on
    // source order. Reusing the component avoids both the dead rule and the second idiom.
    const b = document.getElementById('dLoupeBtn');
    if (b) b.classList.toggle('on', pinned);
    active() ? draw() : hide();
  }

  return {
    // Held is momentary; pinned survives ←/→ on purpose — stepping through images with the lens up
    // IS the culling flow, so navigation is the one action that must not cancel it.
    hold(on) { held = on; sync(); },
    toggle() { pinned = !pinned; if (!pinned) level = 0; sync(); },
    // Every way OFF resets the zoom, not just the icon. Escape and closing the detail used to leave
    // it behind, so the next glance opened at a magnification nobody had asked for — and with the
    // lens down there is nothing on screen saying which level it will come back at.
    // Step the magnification, clamped. The wheel drives this one; `z` walks its own cycle below.
    step(d) { level = Math.min(LEVELS.length - 1, Math.max(0, level + d)); sync(); },
    // What `z` does (2026-09-15): ONE key walking off → 1× → 2× → 3× → 4× → off, so the lens can be
    // switched on from the keyboard at all. It could not before — `z` was momentary and only the
    // icon pinned it, which meant the review flow this exists for (set a magnification, then walk
    // the results with ←/→) began with a mouse trip to the action bar.
    // OFF IS A STATE OF THE CYCLE rather than a wrap back to 1×: a ring with no exit strands anyone
    // who reached it from the keyboard, since the key that turned it on would then never turn it
    // off. The wheel's `step` clamps instead, because a wheel has somewhere else to go.
    tap() {
      if (!pinned) { pinned = true; level = 0; }
      else if (level >= LEVELS.length - 1) { pinned = false; level = 0; }
      else level += 1;
      sync();
    },
    get level() { return LEVELS[level]; },
    get showing() { return active() && !!el() && el().classList.contains('on'); },
    off() { pinned = held = false; level = 0; sync(); },
    move(x, y) { last = { x, y }; if (active()) draw(); },
    // After ←/→ swaps the picture, or a set pane re-renders, the lens is still up but showing the
    // old image until the pointer twitches. Redraw from the remembered position instead.
    refresh() { if (active()) draw(); },
  };
})();

function closeDetail() {
  Loupe.off();
  releaseKeepHold();               // nothing is held once the view is gone                     // a lens left on would come back with the next image, unasked
  $('#overlay').classList.add('hidden'); $('#dImg').src = ''; stopDetailMedia(); hidePairSource(); hideSetView();
  hostStatus();          // take the pill back out before the overlay goes display:none under it
  // Anything that landed while the view was open was deliberately held back (see prependNewImages).
  // Fold it in now, so closing drops you onto a current grid rather than one that is quietly a
  // generation behind. Fire-and-forget: it is a background catch-up, and a failed one must never
  // make closing an image feel like it did something.
  if (_deferredNew) foldInDeferred();
}
// The deferred cards, on the same terms the idle tick folds them in on: newest-first and parked at
// the top, or they stay deferred rather than yanking the view you came back to.
function foldInDeferred() {
  if (!_deferredNew) return;
  if (window.scrollY > 4 || state.sort !== 'date' || state.order !== 'desc') return;
  prependNewImages().catch(() => {});
}

// WHAT THE ARROWS MEAN RIGHT NOW, in one place. Maximized inside a set they flip that set's own
// members; everywhere else they walk the result list, as they always have.
// It is a function and not an inline condition because THREE call sites need the same answer — the
// two arrow buttons and the capture-phase key handler below — and the first cut wrote it out
// longhand in the buttons and the BUBBLE key handler, missing the capture one that actually owns
// ←/→ while the detail is open. The keys then navigated straight past the set they were meant to be
// comparing, while the buttons beside them did the right thing.
// HELD ON WHAT YOU JUST KEPT. Set by a K cull that left you on the keeper — which is only the
// "Stay on card" setting, since "Next card" has already moved you on and the author's instruction was
// explicitly to leave that case alone. While it holds, a plain arrow in the focus view does
// nothing: you have just decided something about this picture and asked to stay on it, so a
// reflexive ←/→ must not carry you off it. Shift+arrow is the deliberate way onward and releases it.
let _keepHold = false;
function releaseKeepHold() { _keepHold = false; }

function stepDetail(dir, cardwise) {
  if (cardwise) { releaseKeepHold(); return navDetail(dir); }
  if (_keepHold && Maxi.on) return;        // held — Shift+arrow is the way out, see _keepHold
  if (!Maxi.step(dir)) navDetail(dir);
}

// K in the maximized set view: keep the member on screen and recycle the rest, in one press. The author
// chose the letter over Enter — "Enter to me feels too easy, I'd like something more explicit" — and
// chose the combined action over mark-then-Delete, so the whole decision is one deliberate key.
// IT CONFIRMS, where the set bar's own "Recycle others" button does not and should not: that is a
// labelled button you aimed at, this is a single letter. Honours the "confirm before recycling"
// setting, so anyone who has turned confirms off still gets the one-key version they asked for.
async function keepShownMember() {
  const members = state.setMembers, i = Maxi.index;
  if (!members || !members[i]) return;
  const others = members.length - 1;
  if (!others) return;
  const role = (members[i].role || '').toUpperCase();
  const ok = await confirmRecycle(
    `Keep ${role || 'this image'} and recycle the other ${others === 1 ? 'one' : others}?`,
    { ok: 'Keep this one', danger: true });
  if (!ok) return;
  // ALWAYS SET THE KEEPER, NEVER TOGGLE. markSetKeep flips an already-marked pane back to unmarked,
  // so pressing K on a member marked earlier would silently un-mark it and recycleSetOthers would
  // then no-op. Clearing first guarantees it marks — and markSetKeep is also what unticks "Apply to
  // folder", which matters more than it reads: that checkbox is off-screen in this mode, and a
  // stale tick would quietly turn one keystroke into a cull of every matching set in the folder.
  state.setKeep = null;
  markSetKeep(i);
  await recycleSetOthers();
}

async function navDetail(dir) {
  if ($('#overlay').classList.contains('hidden')) return;
  let next = state.index + dir;
  // pull another page if stepping off the end and more results exist
  if (next >= state.items.length && !state.done) { await search(false); }
  if (next < 0 || next >= state.items.length) return;
  openDetail(state.items[next].id);
}

async function copyField(which, btn) {
  const d = state.current; if (!d) return;
  let txt = '';
  if (which === 'pos') txt = d.positive || '';
  else if (which === 'neg') txt = d.negative || '';
  else if (which === 'model') txt = d.model_name || '';
  else if (which === 'loras') txt = (d.loras || [])
    .map(l => `<lora:${l.name}:${l.strength != null ? l.strength : 1}>`).join(', ');
  copyFlash(txt, btn || document.querySelector(`.copy[data-copy="${which}"]`));
}

// The generation settings block: seed, steps, CFG, sampler, scheduler, read from the BASE sampler
// (the one that made the original image, before any detailer). Every image of a multi-stage run
// carries the whole workflow, so these are the run's numbers rather than that stage's -- which is
// the deliberate choice, not a limitation.
//
// The WHOLE BLOCK hides when the file carries none, rather than showing a column of dashes. A
// library indexed before this shipped has them empty until a Rebuild metadata, and empty rows would
// read as something broken rather than as something not yet read.
//
// The VAE is one of these rows since 2026-08-23 — the author's placement, moved out of the Details block.
// It rides the same `any` test, so a file carrying ONLY a VAE still opens the block, and it has
// exactly ONE owner: hideEmptyDetailFields must not also list it, or the two will disagree about
// whether the row is empty.
function fillGenSettings(d) {
  // Kept in the order the rows are drawn in index.html, so the two can be read side by side. The
  // order itself is display-only — the markup is what decides it.
  const rows = [['#dSampler', d.gp_sampler], ['#dScheduler', d.gp_scheduler], ['#dCfg', d.gp_cfg],
                ['#dSteps', d.gp_steps], ['#dSeed', d.gp_seed], ['#dVae', d.vae_name]];
  // != null, not truthiness: a seed of 0 and a CFG of 0 are real values a workflow can carry, and
  // reading them as absent would blank a row that has an answer.
  const any = rows.some(([, v]) => v != null && v !== '');
  for (const [sel, v] of rows) {
    const el = $(sel);
    if (!el) continue;
    el.textContent = (v == null || v === '') ? '—'
      : (typeof v === 'number' ? String(Number(v.toFixed(4))) : String(v));
    el.parentElement.classList.toggle('hidden', v == null || v === '');
  }
  $('#dGenWrap').classList.toggle('hidden', !any);
}

function lorasHTML(loras) {
  if (!loras || !loras.length) return '<span class="muted">—</span>';
  return loras.map(l => `<div class="lora-line">${esc(l.name)}` +
    (l.strength != null ? ` <span class="muted">(${esc(String(l.strength))})</span>` : '') + `</div>`).join('');
}
// An extension's OUTPUT is a detail like any native one -- it appends to the Details list and
// says nothing when it has nothing to say. TWO conditions decide the one class (the extension is
// switched on, and this file has actually been scored), so ONE place decides: a generic
// [data-ext] sweep and a per-image fill both writing .hidden would take turns undoing each other.
// That is why the row does NOT carry data-ext.
function syncQualityDetail() {
  const d = state.current;
  const has = !!(d && d.quality && d.quality.reward != null);
  $('#dQualRow').classList.toggle('hidden', !has || !extActive('quality'));
  if (has) {
    $('#dReward').textContent = metricTo10(d.quality.reward).toFixed(1);
    $('#dReward').title = `pyiqa quality score (1-10) — raw ${d.quality.reward.toFixed(3)}`;
  }
  Maxi.recaption();   // maximized, the caption is the only thing showing this score (see paintSetScores)
}

// THE SAME BACKGROUND JOB THE SELECTION BAR USES, not a synchronous request. The author, 2026-09-10:
// "if i run quality scorer from details view, it shows no countdown and the quality score never
// appears." Both, and they were one fault wearing two faces.
//
// The old path POSTed /api/reward and waited, with an ellipsis in the Quality row as its only
// feedback. That row was written when the run was imagined to be quick; it is not. The server's own
// note on _score_one_reward says it "spawns a fresh venv worker (reloads the model), so it's slower
// than a batch" -- tens of seconds, every time, with nothing moving. And a wait that looks broken is
// one you navigate away from, which is exactly when the second fault fires: the response updated the
// object it was started on, then repainted from state.current, which by then was a different image.
// The score reached the database and never reached the screen.
//
// Going through Job fixes both at the root rather than separately: a progress bar with a count and a
// pace, a Stop, and a finish that repaints from the database instead of from a closure that may have
// gone stale. There is no longer any way for the result to be "lost" -- it is read back, not carried.
//
// A SET SCORES EVERY MEMBER. The author, same report: "also unclear which image in a set is scored?" It
// scored state.current -- which for a set is the member FRONTING the card (SET_FACE_RANK_SQL, the
// most finished stage), so in a MAIN/DET pair it silently picked DET. His call between the options:
// score them all. That is also the only answer that makes the number useful, since the set view
// exists to choose between the panes and one score against nothing is not a comparison.
async function rewardImage() {
  const d = state.current; if (!d) return;
  const members = state.setMembers || [];
  const ids = members.length ? members.map(m => Number(m.id)) : [Number(d.id)];
  // skip_scored:false, the same blunt overwrite the selection bar uses -- asking for a score on the
  // image in front of you means you want it recomputed, not skipped because one already exists.
  const j = await postJSON('/api/reward/batch', { ids, skip_scored: false });
  if (j.error) { uiAlert('Quality scoring failed: ' + j.error); return; }
  await watchReward(() => refreshDetailQuality(ids));
}
// What the job does when it finishes WITH THE DETAIL VIEW OPEN. Deliberately not the grid rebuild
// the selection-bar path runs: search(true) refetches and replaces state.items, which is what the
// filmstrip and the arrow keys index into, so rebuilding it under an open overlay moves the ground
// beneath the thing you are looking at. Re-read the one item instead, and patch the cards that
// changed in place.
async function refreshDetailQuality(ids) {
  // READ BACK THE IDS THAT WERE SCORED, not whatever the detail view happens to be showing. Those
  // are the same thing only if you stood still, and standing still is precisely what the old
  // synchronous path assumed and got wrong. A run is tens of seconds; arrowing on during it is
  // normal, and the scored image's card badge must still be right when you scroll past it.
  // At most four requests -- a set is two to four members, a lone image is one.
  const fetched = await Promise.all(ids.map(id =>
    getJSON('/api/image/' + id).then(j => (j && !j.error) ? j : null).catch(() => null)));
  for (const j of fetched) {
    if (!j) continue;
    const it = state.items.find(x => String(x.id) === String(j.id));
    if (it) it.reward = (j.quality || {}).reward;
    refreshCard(j.id);   // the grid is behind the overlay: patch its badge, never rebuild it
  }
  // Repaint the panel only if it is still showing one of them. If you navigated away, there is
  // nothing to correct here -- reopening the image fetches it fresh.
  const d = state.current; if (!d) return;
  const mine = fetched.find(j => j && String(j.id) === String(d.id));
  if (!mine) return;
  d.quality = mine.quality;
  syncQualityDetail();
  if (mine.set_members) {
    state.setMembers = mine.set_members;
    paintSetScores(mine.set_members);
  }
}
// Write each pane's score without re-rendering the set view. A re-render would drop the keeper mark
// and the classes that carry it -- and the panes are the thing you are mid-decision about.
function paintSetScores(members) {
  members.forEach((m, i) => {
    const pane = document.querySelector(`.set-pane[data-idx="${i}"]`);
    if (!pane) return;
    const badge = pane.querySelector('.reward');
    if (m.reward == null) { if (badge) badge.remove(); return; }
    const el = badge || pane.insertAdjacentElement('afterbegin', document.createElement('span'));
    el.className = 'reward';
    el.textContent = metricTo10(m.reward).toFixed(1);
    el.title = `pyiqa quality score (1-10) — raw ${m.reward.toFixed(3)}`;
  });
  // Maximized, the pane badges are hidden and the caption is carrying the score instead — so a run
  // that finishes while the mode is up has to repaint that too, or the one place showing a score
  // goes stale exactly when it becomes interesting.
  Maxi.recaption();
}

// ---- settings dialog ----
const GENERAL_FIELDS = { autoplay: 'setAutoplay', show_snapshots: 'setShowSnapshots', keep_behavior: 'setKeepBehavior', confirm_recycle: 'setConfirmRecycle', models_dir: 'setModelsDir',
  recycle_warn: 'setRecycleWarn', recycle_warn_files: 'setRecycleFiles', recycle_warn_gb: 'setRecycleGb',
  update_check: 'setUpdateCheck' };   // config key -> input id (General section)

// The two number fields follow their checkbox. Dimmed AND inert when it is off -- see .set-grid.is-off.
function syncRecycleWarn() {
  const on = $('#setRecycleWarn') && $('#setRecycleWarn').checked;
  const box = $('#setRecycleNums');
  if (box) box.classList.toggle('is-off', !on);
}
// SNAP BACK IN FRONT OF THE USER. The server clamps these anyway, but the Miner block already shows
// what silent clamping feels like: type 999999, get 100000, with nothing said and the field still
// reading 999999 until the dialog is reopened. Correcting on blur means the number on screen is
// always the number that will be saved.
function clampField(el) {
  if (!el) return;
  const lo = parseFloat(el.min), hi = parseFloat(el.max);
  let n = parseFloat(el.value);
  if (!isFinite(n)) { el.value = el.defaultValue || lo; return; }
  n = Math.max(lo, Math.min(hi, n));
  el.value = el.step && el.step.includes('.') ? String(Math.round(n * 10) / 10) : String(Math.round(n));
}
let settingsDefaults = null;
// What the grid looked like before the dialog was opened — see saveSettings. Nothing in here
// changes which images the grid shows any more; what is compared is the card FACTS.
function fillFields(fields, obj) {
  for (const [k, id] of Object.entries(fields)) {
    const el = $('#' + id); if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!obj[k];
    else el.value = (obj[k] == null ? '' : obj[k]);         // null -> blank (server default)
  }
}
function readFields(fields) {
  const out = {};
  for (const [k, id] of Object.entries(fields)) {
    const el = $('#' + id); if (!el) continue;
    out[k] = (el.type === 'checkbox') ? el.checked : el.value;
  }
  return out;
}
// Miner settings: flat numbers via MINER_FIELDS; sources (nested) + stoplist (textarea) by hand.
const MINER_FIELDS = { min_count: 'setMinerMinCount', max_candidates: 'setMinerMaxCand',
  min_word_len: 'setMinerMinWord', max_phrase_words: 'setMinerMaxPhrase' };
function fillMiner(m) {
  m = m || {};
  fillFields(MINER_FIELDS, m);
  const s = m.sources || {};
  $('#setMinerSrcPrompt').checked = s.prompt !== false;
  $('#setMinerSrcFolder').checked = s.folder !== false;
  $('#setMinerSrcFilename').checked = s.filename !== false;
  $('#setMinerStoplist').value = (m.stoplist || []).join('\n');
}
function readMiner() {
  const out = readFields(MINER_FIELDS);
  out.sources = { prompt: $('#setMinerSrcPrompt').checked, folder: $('#setMinerSrcFolder').checked,
                  filename: $('#setMinerSrcFilename').checked };
  out.stoplist = $('#setMinerStoplist').value;   // the server splits the string into a list
  return out;
}
// ---- Settings -> Cards: what a card's small print says, and in what order ----------------------
// A WORKING COPY, like the theme editor's: edits apply on Save and Cancel reverts, because that is
// what the dialog's Save button promises. The rows are built from CARD_FACTS, so this panel can
// never offer a fact the grid cannot draw, and never miss one it can.
let _cardsEdit = null;

function renderCardFactRows() {
  const host = $('#cardFactRows'); if (!host) return;
  const rows = _cardsEdit || [];
  host.innerHTML = rows.map((r, i) => {
    const def = CARD_FACT_BY_KEY[r.key];
    if (!def) return '';
    const opt = (v, label) => `<option value="${v}"${r.place === v ? ' selected' : ''}>${label}</option>`;
    return `<div class="fact-row" data-key="${esc(r.key)}">
      <span class="fact-name">${esc(def.label)}</span>
      <select class="quiet-field fact-place" aria-label="Where ${esc(def.label)} shows">
        ${opt('always', 'Always')}${opt('hover', 'On hover')}${opt('off', 'Off')}
      </select>
      <button type="button" class="icon-btn fact-up" title="Move up" aria-label="Move ${esc(def.label)} up"${i === 0 ? ' disabled' : ''}>&#9650;</button>
      <button type="button" class="icon-btn fact-down" title="Move down" aria-label="Move ${esc(def.label)} down"${i === rows.length - 1 ? ' disabled' : ''}>&#9660;</button>
    </div>`;
  }).join('');
}
// Up/down rather than drag, the author's call: with six rows you are never more than a few clicks from
// any arrangement, it works from the keyboard, and there is no way to drop one in the wrong place.
function moveCardFact(key, by) {
  const i = (_cardsEdit || []).findIndex(r => r.key === key);
  const j = i + by;
  if (i < 0 || j < 0 || j >= _cardsEdit.length) return;
  const [row] = _cardsEdit.splice(i, 1);
  _cardsEdit.splice(j, 0, row);
  renderCardFactRows();
}

// Settings tabs: which one is open is remembered across sessions (same idiom as the sidebar
// filter tabs). Only tabs that EXIST and are VISIBLE may be restored — showSettingsSection matches
// panels purely on data-sec, so a stale value (a tab since removed, or one hidden with
// display:none) would hide every panel and open Settings on a blank pane.
const SETTINGS_TAB_KEY = 'vv_settings_tab';
function settingsSectionExists(sec) {
  const t = [...document.querySelectorAll('.settings-tab')].find(x => x.dataset.sec === sec);
  // NB: computed display, not offsetParent — this runs while the Settings overlay is still hidden,
  // where offsetParent is null for every tab and the check would always fail.
  return !!(t && getComputedStyle(t).display !== 'none')
      && !!document.querySelector(`.settings-panel[data-sec="${sec}"]`);
}
function showSettingsSection(sec) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.sec === sec));
  document.querySelectorAll('.settings-panel').forEach(p => p.classList.toggle('hidden', p.dataset.sec !== sec));
  try { localStorage.setItem(SETTINGS_TAB_KEY, sec); } catch (e) {}
}
// ---- theme editor (Appearance tab) ----
// Curated, user-meaningful tokens (must match server THEME_TOKENS; all default to 6-digit hex).
const THEME_GROUPS = [
  ['Core', [['--accent', 'Accent'], ['--active', 'Active filter'], ['--modified', 'Unsaved change'],
            ['--danger', 'Danger'], ['--success', 'Success'], ['--star', 'Star']]],
  ['Surfaces & text', [['--bg', 'Background'], ['--bg2', 'Panel'], ['--bg3', 'Raised surface'],
            ['--sidebar-bg', 'Sidebar'],
            ['--fg', 'Text'], ['--muted', 'Muted text'], ['--border', 'Border']]],
  ['Badges & tags', [['--reward-bg', 'Quality badge'], ['--tag-fav', 'Favorite tag']]],
  ['Labels', [['--label-publish', 'To publish'], ['--label-published', 'Published'],
            ['--label-refine', 'To refine'], ['--label-explore', 'To explore'],
            ['--label-video', 'For video']]],
];
const THEME_TOKENS = THEME_GROUPS.flatMap(([, toks]) => toks);   // flat list for apply/save
const THEME_LIGHT = {
  // THE GROUND STEPPED DOWN so a white card reads as a card. It was #f6f7f9 against a #ffffff
  // card -- a 1.07 separation, which is why the grid looked "a bit too uniform": nothing but a
  // 1.3:1 hairline said where one card ended and the page began. The whole ramp moved with it,
  // because --bg3 was already #eceef2 and the page landing on it would have dissolved every input
  // into the page. Order preserved, sidebar darkest through to the white panels.
  // --muted moved two points with the ground. Darkening the surfaces costs muted text contrast,
  // and on the sidebar -- the darkest of the four -- it would have fallen to 4.40 against the 4.5
  // bar. #59616d holds 4.60 on all four. Nobody will see the difference in the colour; they would
  // have seen it in the failure.
  '--bg': '#eceef2', '--bg2': '#ffffff', '--bg3': '#e2e5eb', '--sidebar-bg': '#d9dde5',
  // --border DARKENED for WCAG 2.1 AA 1.4.11 (Non-text Contrast), which asks 3:1 of the boundary of
  // anything you can operate. --border-control is mixed from this and --fg, and in the light preset
  // it landed at 2.49:1 on the page and 2.12:1 in the rail -- a text field whose edge you could
  // barely find. #a7afbf takes the derived edge to #767c89, 3.08:1 on the darkest of the four light
  // surfaces.
  // PER-THEME, WHICH IS THE WHOLE REASON THIS WAS CHEAP. The mix percentage is shared, so for weeks
  // I had this recorded as a change that would lighten the DARK theme's edges too, and the author
  // held the decision on that basis. It does not: --border is a THEME_LIGHT value, so the light edge moves
  // alone and dark is untouched. The cost that is real is dividers and card frames going a little
  // heavier in light, since they read --border directly -- his call, taken 2026-09-16 once he knew
  // it was a quality choice and not an ADA obligation (it is not: ADA sets no contrast numbers, and
  // a local tool is not a place of public accommodation).
  '--fg': '#1b1e24', '--muted': '#59616d', '--border': '#a7afbf',
  // FOUR OF THESE WERE RAISED TO CLEAR WCAG on 2026-09-15, the first time the contrast audit was
  // acted on rather than just run. Each keeps its hue and loses a little lightness -- the same
  // colour a step deeper, not a new one -- and each is the value that clears its bar against EVERY
  // light surface (--bg, --bg2, --bg3, --sidebar-bg), not only the pairs the audit happens to
  // check, so a new pairing later cannot quietly reopen this.
  //   --accent  4.38 -> 4.65   --danger  4.08 -> 4.63
  //   --success 3.20 -> 4.63   --active  2.82 -> 3.10 (a state mark, so its bar is 3.0)
  // --star is deliberately UNCHANGED. Darkening gold far enough to clear 3.0 lands on #b78216,
  // which is not gold any more; it gets --star-edge instead, a rim, which is what WCAG's 3.0 asks
  // for on a component to begin with. The author's call after seeing both.
  // --star IS NO LONGER HERE, and that is deliberate. It was #d99a1a, a gold darkened to carry
  // itself against a pale page -- but the star's main home is the corner of a PICTURE, and a
  // picture is not themed, so the light value made it worse exactly where it is used most. The rim
  // added earlier today is what makes leaving it alone possible: the gold stays #ffca3a everywhere
  // and --star-edge carries the 3:1 on whatever themed surface it lands on. One value, both jobs,
  // instead of a token that was quietly serving two.
  // --active went one more step down WITH the rail. It was tuned to 3.10 against the old
  // #e6e9ee sidebar, and stepping the ground down took it to 2.77 -- re-breaking, the same day,
  // the exact pair that morning's contrast pass had fixed. #2a8e45 holds 3.05 on the darkest of
  // the four light surfaces. Moving a ground is never just the ground: every mark measured
  // against it moves too.
  '--accent': '#2061d3', '--active': '#2a8e45', '--danger': '#c42b2b',
  '--success': '#23763a', '--modified': '#8f5d0c',   // 4.6:1 on the light rail
};
let _themeEdit = {};   // working overrides while the Appearance tab is open
// Apply overrides live; a token absent from `obj` reverts to its CSS default.
function applyTheme(obj) {
  const s = document.documentElement.style;
  for (const [t] of THEME_TOKENS) (obj && obj[t]) ? s.setProperty(t, obj[t]) : s.removeProperty(t);
  // Follow --bg with the title-bar tint of an --app window, or the light preset would leave a
  // near-black bar above a white UI. Must run after the loop: tokenHex reads the computed value.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', tokenHex('--bg'));
  // And follow --bg with what the BROWSER draws for itself: the <select> popup and its separators,
  // scrollbars, date pickers. Those ignore our CSS entirely and pick a light or dark palette from
  // color-scheme alone — which is why the Sort separators came out near-black on grey. Derived from
  // the effective --bg for the same reason the title bar above is: a hardcoded `dark` would be
  // wrong the moment the light preset is chosen, which is the exact bug this replaces (it had been
  // pinned to dark on .date-range input).
  s.setProperty('color-scheme', isLightHex(tokenHex('--bg')) ? 'light' : 'dark');
  // And give every curation label an ink that can actually be read on it. The label band carries
  // its own name now, and these five colours are user-editable, so the text colour cannot be
  // authored in the stylesheet: whatever someone picks, the word on top of it has to stay legible.
  // Runs here because applyTheme is the one place that knows a colour changed, and after the loop
  // for the same reason the title bar does — tokenHex reads the COMPUTED value, so it sees the
  // override and the CSS default alike.
  for (const [t] of LABEL_TOKENS) s.setProperty(t + '-ink', inkOn(tokenHex(t)));
  // --active is a BACKGROUND now, not just a highlight: the first-run nudge fills the Libraries
  // button with it. So it needs the same treatment as a label for the same reason -- it is
  // user-editable, so no ink authored in the stylesheet can be right for every value someone picks.
  // The default green is the case that proves it: white on #3fb950 is 2.6:1, under even the 3:1
  // floor for a graphical object, while near-black on it is 8.2:1. --on-accent was wrong here and
  // inkOn already knew.
  s.setProperty('--active-ink', inkOn(tokenHex('--active')));
  // And the Quality badge, which needs it more than either: it is gold in both themes and sits on
  // a PICTURE, so an ink that followed the page read at 1.85:1 in the light preset. Same treatment
  // as the labels because it has the same shape -- a user-editable ground with text on it.
  s.setProperty('--reward-bg-ink', inkOn(tokenHex('--reward-bg')));
}
// The five label tokens, read off the theme editor's own grouping so there is one list, not two.
const LABEL_TOKENS = (THEME_GROUPS.find(([g]) => g === 'Labels') || [, []])[1];
// Which of near-black or white reads better on `hex`. WCAG relative luminance, NOT the perceived-
// brightness shortcut isLightHex uses: that one asks "does this colour look light", which is the
// right question for choosing a light-or-dark UI but the wrong one for contrast. They disagree on
// the default Published blue — it reads as a dark colour and still takes dark text, because white
// on it is only 3.8:1. Every default label clears 4.5:1 against the near-black.
function inkOn(hex) {
  const n = parseInt(hex.slice(1), 16);
  if (!Number.isFinite(n)) return '#10141a';
  const lin = c => (c /= 255) <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  const L = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  return L > 0.179 ? '#10141a' : '#ffffff';   // 0.179 is the black/white crossover point
}
// Perceived luminance, not a plain average: green dominates how bright a colour looks, and a
// mid-tone theme should land on the side it actually reads as.
function isLightHex(h) {
  const n = parseInt(h.slice(1), 16);
  if (!Number.isFinite(n)) return false;
  return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) > 140;
}
// Current effective value of a token (override or CSS default) as #rrggbb.
function tokenHex(t) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(t).trim();
  return /^#[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : '#000000';
}
function renderThemeRows() {
  const box = $('#themeRows'); if (!box) return;
  box.innerHTML = THEME_GROUPS.map(([title, toks]) =>
    `<div class="theme-group">${esc(title)}</div>` +
    toks.map(([t, label]) =>
      `<label class="theme-row"><input type="color" data-token="${t}" value="${tokenHex(t)}">` +
      `<span class="tr-name">${esc(label)}</span><code class="tr-hex">${tokenHex(t)}</code></label>`).join('')
  ).join('');
}
// Load a preset/override set into the working copy, apply it, and refresh the pickers.
function setThemeEdit(obj) { _themeEdit = { ...obj }; applyTheme(_themeEdit); renderThemeRows(); syncThemeSeg(); }

/* ---- which theme segment is TRUE ------------------------------------------------------------
   The bar reports a state, it does not remember a click. Asking the colours themselves means a
   theme restored from config.json lights the right segment without anything having to have stored
   which button was last pressed -- and editing one swatch moves you to Custom on its own, which is
   the thing the old row could not say. */
function sameTheme(a, b) {
  const ka = Object.keys(a || {}), kb = Object.keys(b || {});
  return ka.length === kb.length &&
         ka.every(k => String(a[k] || '').toLowerCase() === String(b[k] || '').toLowerCase());
}
function themeMode(obj) {
  if (!obj || !Object.keys(obj).length) return 'dark';   // no overrides at all IS the dark default
  return sameTheme(obj, THEME_LIGHT) ? 'light' : 'custom';
}
// Your own colours, held aside while you look at a preset, so previewing Light and coming back
// does not cost you the set you were building. Null until there is one to keep.
let _themeCustom = null;
function syncThemeSeg() {
  const mode = themeMode(_themeEdit);
  if (mode === 'custom') _themeCustom = { ..._themeEdit };   // keep the latest as you edit
  document.querySelectorAll('#themeSeg button').forEach(b => {
    b.classList.toggle('active', b.dataset.theme === mode);
    // Custom is a real segment only once there is something to go back TO. Disabled rather than
    // hidden: a bar that changes width when you touch a swatch is a bar that moves under the
    // pointer, and the segment has to be visible for its absence to mean anything.
    if (b.dataset.theme === 'custom') b.disabled = !_themeCustom;
  });
}

async function openSettings(section) {
  const cfg = await getJSON('/api/config');
  fillFields(GENERAL_FIELDS, cfg.general || {});
  syncRecycleWarn();       // the number fields follow their checkbox from the moment it opens

  // Deliberately NOT a config.json setting: the trace is a property of this browser tab (it holds
  // the recording), not of the library, and it must survive nothing at all.
  $('#setTrace').checked = Trace.on;
  fillMiner(cfg.miner || {});
  // From the config we just fetched: the tab is never a stale snapshot of a setup that
  // finished while the dialog was closed. Which is also why the "we launched a setup" marks are
  // dropped here — this IS the fresh look, so what is on disk outranks what we remember starting.
  _extSetupStarted.clear();
  if (Array.isArray(cfg.extensions)) state.extensions = cfg.extensions;
  renderExtRows();
  if (applyExtensions()) search(true);
  state.theme = cfg.theme || {};
  _themeEdit = { ...state.theme }; renderThemeRows();   // start editing from the saved theme
  // A saved theme that matches neither preset IS your custom one, so Custom is reachable the moment
  // the dialog opens rather than only after you touch a swatch in this sitting.
  _themeCustom = themeMode(state.theme) === 'custom' ? { ...state.theme } : null;
  syncThemeSeg();
  // Same working-copy contract. Deep-copied, or Cancel would leave the edits behind in state.cards.
  state.cards = cfg.cards || state.cards;
  _cardsEdit = (state.cards || []).map(r => ({ ...r }));
  renderCardFactRows();
  // explicit section (a deep link) wins; otherwise reopen on the tab last used
  let sec = section;
  if (!sec) {
    try { sec = localStorage.getItem(SETTINGS_TAB_KEY); } catch (e) { sec = null; }
  }
  showSettingsSection(settingsSectionExists(sec) ? sec : 'general');
  $('#setStatus').textContent = '';
  $('#settings').classList.remove('hidden');
}
function closeSettings() {
  applyTheme(state.theme);              // revert any unsaved live theme edits to the saved theme
  $('#settings').classList.add('hidden');
}
async function saveSettings() {
  const btn = $('#setSave'); btn.disabled = true; btn.textContent = 'Saving…';
  // ONE SAVE FOR THE WHOLE WINDOW. An extension's panel is a tab of Settings like any other, so
  // it saves with the Save button rather than growing one of its own -- a second Save inside a
  // dialog that already has one is a question about which button did what. The exception stays
  // the on/off switch in the installed list, which is immediate on purpose (see its handler).
  for (const ex of readExtSettings()) {
    const r = await postJSON('/api/extensions/settings', ex);
    if (r.error) {
      btn.disabled = false; btn.textContent = 'Save';
      $('#setStatus').textContent = '⚠ ' + r.error;
      return;
    }
    state.extensions = r.extensions || state.extensions;
  }
  const j = await postJSON('/api/settings',
    { general: readFields(GENERAL_FIELDS), miner: readMiner(), theme: _themeEdit,
      cards: _cardsEdit || undefined });
  btn.disabled = false; btn.textContent = 'Save';
  if (j.error) { $('#setStatus').textContent = '⚠ ' + j.error; return; }
  state.theme = j.theme || {};       // server echoes the validated theme
  // The echo again, not the selects: the server drops unknown keys and appends any fact this
  // config predates, so its answer is the one the grid must draw from.
  const cardsBefore = JSON.stringify(state.cards || []);
  if (j.cards) state.cards = j.cards;
  state.autoplay = !!(j.general && j.general.autoplay);
  state.keepBehavior = (j.general && j.general.keep_behavior) || 'next';
  // Read back from the ECHO, not from the checkbox: without this the setting only takes effect
  // after a reload, which reads as "the toggle doesn't work".
  state.confirmRecycle = !(j.general && j.general.confirm_recycle === false);
  applyShowSnapshots(!(j.general && j.general.show_snapshots === false));   // read back from the echo
  // From the echo too, and then acted on IMMEDIATELY in both directions: switching it on asks now
  // rather than next launch, and switching it off takes the mark down rather than leaving it lit
  // over a feature the user just turned off. A setting whose effect waits for a restart is one the
  // user reasonably reads as broken.
  state.updateCheck = !(j.general && j.general.update_check === false);
  if (state.updateCheck) checkForUpdate();
  else { _update = null; $('#btnUpdate').classList.add('hidden'); closeUpdateBox(); }
  closeSettings();
  toast('Settings saved');
  // ONLY IF THE GRID'S CONTENTS ACTUALLY CHANGED. Saving used to rebuild the whole grid whatever
  // you had touched — a 3.6s stall, on a real trace, for switching the debug trace on.
  // NOTHING IN THIS DIALOG CHANGES WHICH IMAGES THE GRID SHOWS any more: "Hide large images" and
  // "Show hidden files" were the only two, and both were removed on 2026-09-08. Everything left —
  // autoplay, the keep behaviour, the recycle confirm, the theme — either changes nothing on
  // screen or is applied live above. So the only repaint left is the card facts, which change what
  // the cards SAY rather than which ones there are, and repaint from the items already in hand.
  if (JSON.stringify(state.cards || []) !== cardsBefore) {
    // The card facts change what the cards SAY, not which ones there are — so this repaints from
    // the items already in hand rather than re-querying. Deliberately in the `else`: a re-search
    // rebuilds every card anyway, and doing both would paint twice.
    repaintCards();
  }
}


// Fetch the finished export (server splices the parameters chunk) and save it. Fast: any model
// hashing was already warmed by the prepare step, so this just builds + downloads.
async function _downloadCivitai(d) {
  const resp = await fetch(`/export/civitai/${d.id}?r=${encodeURIComponent(d.root_key || '')}`);
  if (!resp.ok) {
    let msg = 'Export failed';
    try { const j = await resp.json(); if (j.error) msg = j.error; } catch (e) {}
    uiAlert(msg); return false;
  }
  const blob = await resp.blob();
  const cd = resp.headers.get('Content-Disposition') || '';
  const m = /filename="?([^"]+)"?/.exec(cd);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : `${d.filename || d.id}.civitai.png`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast('Exported ' + a.download);
  return true;
}
// Export for Civitai: first PREPARE (hash any not-yet-cached model/LoRA files — the slow part —
// with a real progress bar + Stop), then download. Nothing to hash -> download straight away.
async function exportForCivitai() {
  const d = state.current; if (!d) return;
  const btn = $('#dCivitaiExport');   // icon button — progress shows in the status bar, so just disable it
  btn.disabled = true;
  const restore = () => { btn.disabled = false; };
  try {
    const j = await postJSON('/api/export/civitai/prepare', { id: d.id });
    if (j.error) { uiAlert(j.error); restore(); return; }
    if (j.ready) { await _downloadCivitai(d); restore(); return; }
    watchExport(d, restore);   // hashing running — show the bar, download when it finishes
  } catch (e) {
    uiAlert('Export failed: ' + e); restore();
  }
}
// Poll the export-prep job; drive the shared status bar + Stop button; download on completion.
function watchExport(d, onDone) {
  return Job.start({
    name: 'export', blockedMsg: 'A job', every: 500,
    statusUrl: '/api/export/civitai/status', stopUrl: '/api/export/civitai/stop',
    progress: s => {
      const done = s.done_bytes || 0, total = s.total_bytes || 0;
      const pct = total ? Math.floor(done / total * 100) : 0;
      // Say "local file" + "once": a bar counting to 20+GB otherwise looks like an internet download.
      return { pct, text: total
        ? `${s.phase || 'Reading models'} · ${fmtBytes(done)} / ${fmtBytes(total)} (${pct}%) · local file, hashed once`
        : (s.phase || 'Preparing…') };
    },
    // The download IS the job's product, so it is awaited before the pill retires — the old order
    // hid Stop, then downloaded, then hid the pill off a timer regardless of whether it worked.
    finish: async s => {
      $('#pbarFill').style.width = '100%';
      if (s && !s.error && !s.stopped) await _downloadCivitai(d);
    },
    summary: s => !s ? '' : (s.stopped ? 'Export canceled' : 'Hashing complete — image saved'),
    tail: 3500,
    restore: () => { if (onDone) onDone(); },
  });
}

async function copyFlash(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch (e) {}
  if (!btn) return;                            // icon-safe: flash a class, never touch innerHTML
  btn.classList.add('done');
  setTimeout(() => btn.classList.remove('done'), 1200);
}
async function copyAllCivitai(btn) {
  const d = state.current; if (!d) return;
  // A video (or a video's paired still) skips the sampler settings — they don't matter for video.
  const isVideo = d.is_video || d.video_id != null;
  let j;
  try {
    j = await (await fetch('/api/civitai_text/' + d.id + (isVideo ? '?video=1' : ''))).json();
  } catch (e) { j = null; }
  if (!j || !j.text) { toast('No metadata to copy'); return; }
  copyFlash(j.text, btn);
  toast('Copied metadata for Civitai');
}
// Library-wide duplicate report. Read-only: it deletes nothing. The per-folder cull can only
// surface duplicates you're already standing in, so it's no use for finding out whether you have
// any — this sweeps every reachable library. Scope is global, not the row whose ⋯ opened it.
const DUPE_BATCH = 200;                 // sets per recycle press — see api_dupes_cull for why
let _dupeFreed = 0;                     // running total across batches, for this modal session

async function findDuplicates(keepTotals) {
  if (!keepTotals) _dupeFreed = 0;
  $('#dupesSummary').textContent = 'Scanning…';
  $('#dupesList').innerHTML = '';
  $('#dupesNote').textContent = '';
  $('#dupesCull').classList.add('hidden');
  $('#dupesModal').classList.remove('hidden');
  let j;
  try { j = await getJSON('/api/dupes/scan'); }
  catch (e) { $('#dupesSummary').textContent = 'Could not scan for duplicates.'; return; }
  const offline = (j.offline || []).length ? ` Not scanned — offline: ${j.offline.join(', ')}.` : '';
  const freed = _dupeFreed ? ` ${fmtBytes(_dupeFreed)} recycled so far.` : '';
  if (!j.sets) {
    $('#dupesSummary').textContent = 'No duplicate files found.' + freed + offline;
    $('#dupesList').innerHTML = '<div class="dupes-empty">'
      + (_dupeFreed ? 'All done — every duplicate has been recycled.'
                    : 'Nothing to clean up — no file appears in more than one folder.') + '</div>';
    return;
  }
  const batch = Math.min(DUPE_BATCH, j.sets);
  const cull = $('#dupesCull');
  cull.textContent = `Recycle ${batch} ${batch === 1 ? 'set' : 'sets'}`;
  cull.title = `Recycle the duplicates of the ${batch} largest sets, keeping one file from each`;
  cull.classList.remove('hidden');
  cull.disabled = false;
  $('#dupesSummary').textContent =
    `${j.sets} duplicate ${j.sets === 1 ? 'set' : 'sets'} · ${j.copies} extra ` +
    `${j.copies === 1 ? 'copy' : 'copies'} · ${fmtBytes(j.bytes)} recoverable.` + freed + offline;
  const when = t => t ? new Date(t * 1000).toLocaleString() : '—';
  const row = (cls, label, r) => `<div class="dupe-row"><span class="dupe-tag ${cls}">${label}</span>
    <span>${esc(r.library)} · ${esc(r.folder)}</span><span class="dupe-when">${when(r.mtime)}</span></div>`;
  const set = s => `
    <div class="dupe-set">
      <div class="dupe-name">${esc(s.filename)}<span>${fmtBytes(s.size)} · ${fmtBytes(s.recoverable)} recoverable</span></div>
      ${row('keep', 'keep', s.keep)}
      ${s.dupes.map(d => row('copy', 'copy', d)).join('')}
    </div>`;
  const head = t => `<div class="dupe-head">${t}</div>`;
  const smalls = j.smallest || [];
  $('#dupesList').innerHTML =
    (smalls.length ? head(`${j.largest.length} largest (by space recoverable)`) : '')
    + j.largest.map(set).join('')
    + (smalls.length ? head(`${smalls.length} smallest`) + smalls.map(set).join('') : '');
  const shown = j.largest.length + smalls.length;
  $('#dupesNote').textContent = shown < j.sets
    ? `Showing ${shown} of ${j.sets} sets — both ends of the list` : `Showing all ${j.sets} sets`;
}
function closeDupesModal() { $('#dupesModal').classList.add('hidden'); }

// Recycle one batch of the largest sets, then re-scan so the list reflects what's left. Batched
// on purpose (see api_dupes_cull): thousands of files at once can overrun the Recycle Bin quota,
// after which Windows silently purges the oldest bin contents.
async function cullDuplicates() {
  const btn = $('#dupesCull');
  const sets = parseInt(btn.textContent.replace(/\D+/g, ''), 10) || DUPE_BATCH;
  // NOT gated by confirmRecycle: sweeps every reachable library, 200 sets a press, and the
  // report only rendered a sample of them.
  const ok = await uiConfirm(
    `Recycle the duplicates of the ${sets} largest ${sets === 1 ? 'set' : 'sets'}?\n\n` +
    `One file is kept from each — the most-curated copy — and any tags, labels, notes or scores ` +
    `on the copies move onto it. The rest are recycled.`,
    { ok: `Recycle ${sets} ${sets === 1 ? 'set' : 'sets'}`, danger: true });
  if (!ok) return;
  btn.disabled = true; btn.textContent = 'Recycling…';
  let j;
  try { j = await postJSON('/api/dupes/cull', { sets: DUPE_BATCH }); }
  catch (e) { btn.disabled = false; return uiAlert('Recycling the duplicates failed.'); }
  if (j.error) { btn.disabled = false; return uiAlert(j.error); }
  _dupeFreed += j.bytes || 0;
  if (j.failed && j.failed.length) {
    uiAlert(`${j.deleted} recycled; ${j.failed.length} could not be: ${j.failed[0].error}`);
  } else {
    // say when rows were dropped without a file moving, or "recycled N" reads as N files in the bin
    toast(`Recycled ${j.deleted} ${j.deleted === 1 ? 'copy' : 'copies'} · ${fmtBytes(j.bytes)} freed`
          + (j.purged ? ` · ${j.purged} already gone from disk` : ''));
  }
  RecycleDebt.soon();                    // a bulk cull is the likeliest way to cross a threshold
  await refreshView();                   // recycled copies may have been on screen
  await findDuplicates(true);            // refresh the report, keeping the running total
}

// Folder ⋯ → Set filter: point the sidebar's Folder facet at the open image's folder and drop back
// to the grid. The answer to "Random found me one of these — show me the rest": the detail closes
// because the result the filter produces is the point, and going through pickFacet means the facet
// dropdown, the active-filter paint and the search all update exactly as if it had been picked there.
function filterToThisFolder() {
  const f = state.current && state.current.folder; if (!f) return;
  closeDetail();
  pickFacet('folder', f);
  toast('Folder filter: ' + f);
}

// Recycle copies of this folder's files that sit in OTHER folders — same name, size and timestamp.
// The file in this folder is the one kept. Anything a copy carried (tags, label, favorite, note,
// scores) is merged onto the keeper server-side BEFORE the delete, so culling can't cost curation.
// Libraries that aren't reachable right now are skipped entirely and reported, never treated as
// "files are gone".
async function deleteFolderCopies() {
  const d = state.current; if (!d) return;
  const id = d.id;
  let s;
  try { s = await getJSON('/api/folder_dupes?id=' + id); }
  catch (e) { return uiAlert('Could not check for copies.'); }
  if (s.error) return uiAlert(s.error);
  const offline = (s.offline || []).length
    ? ` Not searched — offline: ${s.offline.join(', ')}.` : '';
  if (!s.copies) return toast('No copies found.' + offline);
  const files = s.groups === 1 ? 'file' : 'files';
  const where = Object.entries(s.by_library || {}).map(([n, c]) => `${n} ${c}`).join(', ');
  const curated = s.curated
    ? ` ${s.curated} of them ${s.curated === 1 ? 'carries' : 'carry'} tags, labels or notes — ` +
      `${s.curated === 1 ? 'that moves' : 'those move'} to the kept ${files}.` : '';
  // NOT gated by confirmRecycle: reaches other folders and other libraries, and this is the only
  // place they are named.
  const ok = await uiConfirm(
    `Recycle ${s.copies} ${s.copies === 1 ? 'copy' : 'copies'} of ${s.groups} ${files}? ` +
    `The ${files} in this folder ${s.groups === 1 ? 'is' : 'are'} kept.${curated} ` +
    `Copies are in: ${where}.${offline}`,
    { ok: 'Recycle copies', danger: true });
  if (!ok) return;
  let j;
  try { j = await postJSON('/api/folder_dupes/apply', { id }); }
  catch (e) { return uiAlert('Recycling the copies failed.'); }
  if (j.error) return uiAlert(j.error);
  if (j.failed && j.failed.length) {
    uiAlert(`${j.deleted} recycled; ${j.failed.length} could not be: ${j.failed[0].error}`);
  } else {
    toast(`Recycled ${j.deleted} ${j.deleted === 1 ? 'copy' : 'copies'}`
          + (j.purged ? ` · ${j.purged} already gone from disk` : ''));
  }
  RecycleDebt.soon();
  await refreshView();                 // merged tags change the sidebar counts too
  openDetail(id);                      // the keeper may have gained tags, a label or a note
}

async function deleteCurrent() {
  const d = state.current; if (!d) return;
  // Let go of the video BEFORE asking the server to recycle it. A <video> that has played is still
  // holding a /file/ range request open — and the server streams that request with the file open,
  // parked in a blocking write once the player stops reading — so Windows refuses to move the file
  // and the delete fails as "in use". Detaching the src aborts the request, which unblocks that
  // thread and closes the handle. Harmless for stills: it's a no-op when no video is attached.
  stopDetailVideo();
  await Undo.commitNow();          // only one batch is ever undoable; settle the previous one
  // For a merged pair, group_members holds both the still and the video — recycle them together.
  const members = (Array.isArray(d.group_members) && d.group_members.length)
    ? d.group_members.map(Number) : [Number(d.id)];
  const r = await fetch('/api/delete', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: members }) });
  const j = await r.json();
  if (j.error) { uiAlert('Delete failed: ' + j.error); return; }
  // Queued, not done — no `failed` to check yet; a late failure surfaces via Undo's result check.
  // Remove whichever member owns the grid card (the still, for a pair) and advance from there.
  let idx = -1;
  const removed = [];
  for (const m of members) {
    const mi = state.items.findIndex(x => String(x.id) === String(m));
    if (mi !== -1) {
      if (idx === -1) idx = mi;
      removed.push({ idx: mi, item: state.items[mi] });
      state.items.splice(mi, 1);
    }
    document.querySelector(`.card[data-id="${m}"]`)?.remove();
    selection.delete(String(m));
  }
  // NOT sorted — see undoRestorer.
  state.total = Math.max(0, state.total - 1);   // one grid card (group) removed
  if (state.rootTotal != null) state.rootTotal = Math.max(0, state.rootTotal - 1);  // counts CARDS, like total
  if (state.rootFiles != null) state.rootFiles = Math.max(0, state.rootFiles - members.length);
  renderCount();
  updateSelBar();                                   // the total moved; the bar reads the total
  if (!state.items.length) showNoMatchHint();       // culled the last card — see deleteSelected
  // Say HOW MANY when it's a set or a pair. With the confirm gated off this pill is the only
  // signal that anything happened, and "Moved to Recycle Bin" understates binning three files.
  RecycleDebt.soon();
  Undo.offer(j.batch, `${members.length} recycled`, (j.window || 5) * 1000,
             undoRestorer(removed, { total: 1, rootTotal: 1, rootFiles: members.length }));
  if (idx === -1 || state.items.length === 0) { closeDetail(); return; }
  openDetail(state.items[Math.min(idx, state.items.length - 1)].id);  // advance to next (or last)
}

// ---- scan / rescan / set root ----
function fmtTime(sec) {
  if (sec == null || !isFinite(sec)) return '—';
  sec = Math.max(0, Math.round(sec));
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}
// ---- refreshView: the one way to bring the view back in line ----------------------------------
// "Something changed underneath — repaint what that affects." There were SEVEN hand-assembled
// versions of this, and no two agreed: three orders (facets-then-tags, tags-then-facets, all three
// at once), the grid sometimes awaited and sometimes not, two callers passing no {facets:false} so
// the facets were fetched TWICE, and only one remembering the on-disk marks. Every "the sidebar
// didn't update" and "it refreshed twice" bug in this arc lived in that spread.
//
// Callers now say WHAT happened; this decides what to repaint, in what order, and awaits all of it:
//   grid: 'replace' (default) — the result set may be different: rebuild it
//         'prepend'           — same set plus new arrivals at the top (auto-refresh; keeps scroll)
//         'none'              — the grid is already right; only the rail is stale
//   changes: true             — also re-check the on-disk marks (after something touched the index)
//   afterJob: true            — a finishing job already owned the screen; don't raise a second dim
//
// The three fetches go out TOGETHER — they write disjoint parts of the page (facets → the two
// dropdowns, model type and the Libraries list; tags → the tag and label lists; search → the grid
// and the count) and the server takes one connection per request. markActiveFilters() settles the
// rail once they have all landed, because it reads state only the responses can fill in.
async function refreshView(opts) {
  const o = opts || {};
  const paintGrid = o.grid === 'prepend' ? () => prependNewImages()
                  : o.grid === 'none'    ? null
                  : () => search(true, { facets: false });
  const gridJob = !paintGrid ? Promise.resolve()
                : o.afterJob ? refreshAfterJob(paintGrid)
                : paintGrid();
  // The on-disk check rides ALONGSIDE the others rather than after them. It used to be a fourth,
  // serial round trip tacked on the end — free locally, a real wait on a share — and it has no
  // reason to wait: the scan that prompted it has already finished. Awaited with the rest, so the
  // "new files pending" mark still settles before the reveal rather than clearing a beat later.
  const jobs = [loadFacets(), loadTags(), gridJob];
  if (o.changes) jobs.push(fetchChanges());
  await Promise.allSettled(jobs);
  markActiveFilters();
  if (o.changes) renderLibList(window._facets && window._facets.roots);
}
// What a finished scan is FOR. One call whether it follows one scan or five, so the grid is rebuilt
// once per run rather than once per library. Quiet mode prepends instead of resetting the grid,
// which is the whole point of it.
async function afterScanRefresh(quiet) {
  await refreshView({ grid: quiet ? 'prepend' : 'replace', afterJob: true, changes: true });
  // AFTER every scan, whichever kind and whoever started it: this is the one place they all end, so
  // a caught-up library loses its mark here rather than in each caller. It has to be a re-read and
  // not a decrement — a run that was stopped half way leaves some rows stamped and some not, so the
  // only count that is true is the one the index can still see.
  await refreshBehind();
}
// quiet=true (auto-refresh) means no pill, no Stop, no block: the clock is the only indicator and
// autoRefreshTick owns it for the whole catch-up. Completion PREPENDS new cards instead of the
// scroll-resetting search(true), which is the whole point of the quiet path.
// A FIRST INDEX CANCELS; EVERY OTHER SCAN STOPS. The author, 2026-09-07: "if you STOP indexing of a new
// library - you have no idea it didn't finish", and it was worse than that — a cancelled scan
// stores no folder signature, so changes_detected() returns false, the tick never lights, R
// rescans nothing, and re-adding the folder finds rows already there and skips the scan. The
// library was half-indexed with no way in the UI to finish it.
// His call, and the right one: don't report the partial state, don't let it exist. Cancelling a
// first index removes the library and its rows, so the only outcome is "added" or "not added".
// Safe ONLY for a first index — a library seconds old has no labels, tags, notes or scores to lose,
// which is exactly what removing an established one would take.
// `first_index` and `key` come from the server on every status poll, so a browser that reloaded
// mid-scan and reattached gets the same button and the same question.
let _cancelledFirstIndex = null;    // the library key to remove once the scan has actually stopped
let _scanFirst = null;              // the latest scan status, so the stop button can read it
function watchScan(label, quiet, runLabel) {
  _cancelledFirstIndex = null;   // belt and braces: no answer from a previous scan survives into this
  return Job.start({
    name: 'scan', blockedMsg: 'A scan', every: 400, silent: !!quiet, blocking: !quiet,
    runProgress: !!runLabel,   // silent, but still paints into the run's pill -- see Job.start
    statusUrl: '/api/scan/status', stopUrl: '/api/scan/stop',
    onStop: async () => {
      const s = _scanFirst;
      if (!s || !s.first_index) return true;            // an ordinary scan: stop means stop
      const ok = await uiConfirm(
        'Cancel indexing? The library will be removed, and you will need to add it again.',
        { ok: 'Yes, cancel', cancel: 'No, continue' });
      if (!ok) return false;
      // A SHORT SCAN CAN FINISH WHILE THE QUESTION IS ON SCREEN -- it happened on the first run of
      // this test, at 6,000 files. Setting the flag then would arm a removal with no job left to
      // consume it, and the NEXT scan's finish would delete a library nobody cancelled. So the
      // answer only counts if the job it was asked about is still running.
      if (!Job.stopUrl) {
        toast('Indexing had already finished — the library is here.');
        return false;
      }
      _cancelledFirstIndex = s.key || null;
      return true;
    },
    progress: s => {
      // Told by the poll rather than by the caller, so the reattach path agrees with this one.
      _scanFirst = s;
      Job.setStopLabel(s && s.first_index ? 'Cancel…' : 'Stop');
      return scanProgress(s, label, runLabel);
    },
    // The refresh the scan exists for, before anything comes down. Inside a Job.during run this is
    // the run's job instead — one rebuild for the whole run, not one per library.
    // The removal happens HERE, after the scan has actually stopped: /api/roots/remove refuses
    // while a job holds the index, so doing it at the click would have been a no-op with a
    // reassuring dialog in front of it.
    finish: async s => {
      if (_cancelledFirstIndex) {
        const key = _cancelledFirstIndex; _cancelledFirstIndex = null;
        // delete_index, or the partial rows outlive the library and the re-add finds images
        // already there and skips its scan -- the same dead end by a different route.
        try {
          await postJSON('/api/roots/remove', { key, delete_index: true });
        } catch (e) {}
        await loadRoots();
        await refreshView();      // paints the welcome itself now — see the reset branch in search()
        return;
      }
      if (s && !Job.inRun) await afterScanRefresh(quiet);
    },
    // No completion tail on a CLEAN scan, deliberately: a bar sitting on screen after the block
    // had gone read as a separate, unfinished thing. An error still lingers — Job gives it 8s.
    //
    // It speaks only when files were skipped. The scan now survives a file it cannot read rather
    // than ending on it, and that trade is only safe if it SAYS SO: a refresh that quietly drops
    // files is worse than one that stops, because nothing would give you a reason to look.
    summary: s => {
      const f = (s && s.stats && s.stats.failed) || 0;
      if (!f) return '';
      const why = (s.stats.first_error || '').trim();
      return f === 1 ? `Couldn't read ${why || '1 file'}`
                     : `Couldn't read ${f.toLocaleString()} files${why ? ' — first: ' + why : ''}`;
    },
  });
}
// ---- extensions ------------------------------------------------------------------------------
// An extension is a folder the server found, not a module this file knows about. The one thing
// the client has to do is show and hide the controls each one owns -- and those are declared IN
// THE MARKUP, `data-ext="<id>"`, rather than listed here. Adding an extension's controls means
// tagging them; it never means editing applyExtensions().
//
// WHY HIDE RATHER THAN DISABLE: an extension that isn't installed has no controls to explain. The
// Quality button, filter and sort used to be drawn on a machine where the scorer was never set up,
// and every one of them failed when clicked. Dimmed-but-pressable is for a control that is idle
// (see DESIGN.md); this one doesn't exist.
function extActive(id) {
  if (!Array.isArray(state.extensions)) return true;   // before /api/config lands: touch nothing
  const e = state.extensions.find(x => x.id === id);
  return !!(e && e.active);
}
// True when something the GRID shows changed and the caller has to re-query or repaint.
function applyExtensions() {
  let changed = false;
  // `data-ext-produces="tags"` is for a control that belongs to a KIND of extension rather than
  // to one by name — Clear machine tags is meaningful while any tagger is on, and meaningless
  // when none is. Without it that button would have to name every tagger ever installed.
  document.querySelectorAll('[data-ext-produces]').forEach(el => {
    const kind = el.dataset.extProduces;
    const on = !Array.isArray(state.extensions)
            || state.extensions.some(e => e.active && e.produces === kind);
    el.classList.toggle('hidden', !on);
  });
  document.querySelectorAll('[data-ext]').forEach(el => {
    const on = extActive(el.dataset.ext);
    // An <option> can't take the class: `.hidden` is scoped per component, and a display rule
    // doesn't remove an option from a select's keyboard navigation. The HTML attribute does both.
    if (el.tagName === 'OPTION') el.hidden = !on;
    else el.classList.toggle('hidden', !on);
  });
  // A hidden control must not still be acting. A sort that no longer appears would leave the grid
  // in an order nothing on screen explains, and a filter you cannot see is the exact fault the
  // sidebar's "drawn only when it's doing something" rule exists to prevent.
  const sortSel = $('#sort');
  const cur = [...sortSel.options].find(o => o.value === state.sort);
  if (cur && cur.hidden) { state.sort = 'date'; sortSel.value = 'date'; changed = true; }
  if (!extActive('quality') && (state.rmin !== '' || state.rmax !== '')) {
    state.rmin = ''; state.rmax = '';
    $('#rmin').value = ''; $('#rmax').value = '';
    changed = true;
  }
  // The two surfaces the markup cannot declare: a menu whose items are data, and a detail row
  // whose visibility also depends on whether THIS file has been scored.
  renderExtMenu();
  syncQualityDetail();
  return changed;
}

// Settings -> Extensions. Rows are built from the server's list, so the panel cannot show an
// extension that isn't on disk, and cannot miss one that is.
// Extensions whose setup we have LAUNCHED this time the dialog was opened. Deliberately not a
// job: setup runs in its own console window, outside the app, so there is no progress to poll and
// nothing to Stop — the app knows it started one and honestly cannot know when it ends.
//
// Cleared every time Settings opens, because that re-reads the folder from disk and a fresh look
// is a better answer than a remembered one. Reopening is the "has it finished?" gesture.
const _extSetupStarted = new Set();

function extStatus(e) {
  // Before the on-disk states: a repair leaves Ready -> Ready, so without this the row would say
  // exactly what it said before the click and the button would look broken. The author's report.
  if (_extSetupStarted.has(e.id)) {
    return { text: 'Setting up…', cls: 'busy',
             note: 'Running in its own window — it says when it has finished. Reopen Settings '
                 + 'to check.' };
  }
  if (e.error) return { text: 'Not loading', cls: 'bad', note: e.error };
  if (!e.installed) return { text: 'Not set up', cls: 'off',
                             note: e.has_setup ? 'Needs a one-time setup before it can run.'
                                               : 'Its files are incomplete.' };
  if (!e.enabled) return { text: 'Off', cls: 'off', note: 'Its controls are hidden.' };
  return { text: 'Ready', cls: 'ok', note: '' };
}
const EXT_PRODUCES = { score: 'Adds a quality score.', tags: 'Adds tags.',
                       text: 'Answers a question about a file.' };

// ---- an extension's own Settings tab ----------------------------------------------------------
// An extension that declares settings gets a TAB OF ITS OWN under Extensions, not a clump of
// fields inside its row in the installed list. Two reasons, and the second is the one that
// decided it: the installed list answers "what is here and is it working", which a form buried in
// it stops answering; and a tab is a whole panel, so an extension can grow a real form without
// the list turning into one.
//
// The tab machinery is untouched. showSettingsSection() matches on `data-sec` alone, so a tab
// built here behaves like the five in the markup — and settingsSectionExists() already refuses to
// restore a remembered tab that has gone, which is what happens when an extension's folder is
// deleted while its tab was the last one open.
const EXT_SEC = id => 'ext:' + id;

function extFieldRow(e, f) {
  const id = `extf_${e.id}_${f.key}`;
  const val = (e.values || {})[f.key];
  // A password whose value the server deliberately withheld. Say it is set rather than drawing an
  // empty box that reads as "nothing here" — and say what leaving it empty will do, because the
  // honest behaviour (blank means unchanged) is invisible otherwise.
  const isSecret = f.type === 'password';
  const secretSet = isSecret && (e.secrets_set || []).includes(f.key);
  const help = secretSet ? 'Saved. Leave blank to keep it, or type a new one to replace it.'
                         : (f.help || '');
  const ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : '';
  let control;
  if (f.type === 'checkbox') {
    return `<div class="set-row set-check">
      <label><input type="checkbox" id="${esc(id)}" data-ext-field="${esc(f.key)}"
        ${val ? 'checked' : ''}> ${esc(f.label)}</label>
      ${help ? `<p class="set-help">${esc(help)}</p>` : ''}
    </div>`;
  }
  if (f.type === 'textarea') {
    control = `<textarea id="${esc(id)}" data-ext-field="${esc(f.key)}" rows="4"
      spellcheck="false"${ph}>${esc(val == null ? '' : String(val))}</textarea>`;
  } else {
    const type = f.type === 'number' ? 'number' : f.type === 'password' ? 'password' : 'text';
    control = `<input type="${type}" id="${esc(id)}" data-ext-field="${esc(f.key)}"
      autocomplete="off"${ph} value="${esc(val == null ? '' : String(val))}">`;
  }
  return `<div class="set-row">
    <label for="${esc(id)}">${esc(f.label)}</label>
    ${control}
    ${help ? `<p class="set-help">${esc(help)}</p>` : ''}
  </div>`;
}

function renderExtTabs() {
  const nav = $('.settings-nav'), panels = $('.settings-panels');
  if (!nav || !panels) return;
  // Rebuilt from scratch every time, so an extension deleted from disk takes its tab with it.
  nav.querySelectorAll('.settings-tab-sub').forEach(el => el.remove());
  panels.querySelectorAll('.settings-panel[data-ext]').forEach(el => el.remove());
  for (const e of state.extensions || []) {
    if (!(e.settings || []).length) continue;
    const sec = EXT_SEC(e.id);
    const tab = document.createElement('button');
    tab.className = 'settings-tab settings-tab-sub';
    tab.dataset.sec = sec;
    tab.textContent = e.name;
    tab.title = e.name;          // the rail ellipsises a long name; this is where the rest lives
    nav.appendChild(tab);
    const panel = document.createElement('section');
    panel.className = 'settings-panel hidden';
    panel.dataset.sec = sec;
    panel.dataset.ext = e.id;
    panel.innerHTML =
      `<p class="set-help">${esc(e.description || '')}</p>`
      + (e.enabled ? '' : '<p class="set-help">Switched off — turn it back on under Extensions '
                        + 'to use it. Settings here are still saved.</p>')
      + e.settings.map(f => extFieldRow(e, f)).join('')
      // Offered only by an extension that says it can check itself. The app has no idea what
      // "working" means for one, so it cannot invent this button for an extension that hasn't
      // declared it — it would press something that does nothing.
      // The status sits LAST, below the help line, so an answer arriving doesn't shove the help
      // text down the panel while you are reading it.
      + (e.can_test ? `<div class="set-row ext-test-row">
          <button class="secondary ext-test" data-id="${esc(e.id)}">Test connection</button>
          <p class="set-help">Checks what is typed above, without saving it.</p>
          <span class="set-test ext-test-status"></span>
        </div>` : '');
    panels.appendChild(panel);
  }
}

// What one extension's panel currently holds. The panel IS the working copy — it is rebuilt from
// the server's echo on every save — so there is no second place for the two to disagree.
function readExtPanel(panel) {
  const values = {};
  panel.querySelectorAll('[data-ext-field]').forEach(el => {
    values[el.dataset.extField] = el.type === 'checkbox' ? el.checked : el.value;
  });
  return { id: panel.dataset.ext, values };
}
function readExtSettings() {
  return [...document.querySelectorAll('.settings-panel[data-ext]')].map(readExtPanel);
}

// Test connection. Sends what is ON SCREEN and saves nothing — testing an address is how you find
// out whether it is the right one, and a button that had to write it down first would make the
// wrong address the thing you keep. The server merges it the same way Save does (merge_ext_values),
// so a blank password field means "use the saved key" here exactly as it does there.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.ext-test');
  if (!btn) return;
  const panel = btn.closest('.settings-panel[data-ext]');
  const out = panel.querySelector('.ext-test-status');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Testing…';
  out.className = 'set-test ext-test-status';
  out.textContent = 'asking…';
  try {
    const j = await postJSON('/api/extensions/test', readExtPanel(panel));
    // THREE ANSWERS, NOT TWO. "It answered, but not the way you have it set up" is the one that
    // matters here — a text-only model with Pictures ticked replies perfectly and ignores every
    // image — and forcing it into a tick or a cross makes the mark contradict the sentence.
    const state = !j || !j.ok ? 'err' : j.warn ? 'warn' : 'ok';
    out.className = 'set-test ext-test-status ' + state;
    out.textContent = (state === 'ok' ? '✓ ' : '⚠ ')
      + (j && j.ok ? (j.detail || 'It answered.') : ((j && j.error) || 'It did not answer.'));
  } catch (e) {
    out.className = 'set-test ext-test-status err';
    out.textContent = '⚠ Could not reach the app to run the test.';
  }
  btn.disabled = false; btn.textContent = label;
});

function renderExtRows() {
  const box = $('#extRows');
  const list = state.extensions || [];
  // The sub-tabs come from the same list and are redrawn from the same call, so a tab cannot
  // outlive its row -- the three callers of this function get both or neither.
  renderExtTabs();
  if (!list.length) {
    box.innerHTML = '<p class="set-help">Nothing installed yet.</p>';
    return;
  }
  box.innerHTML = list.map(e => {
    const st = extStatus(e);
    // Only an installed, loadable extension can be switched: there is nothing to turn on when the
    // thing it turns on isn't there. This is the genuinely-unavailable case DESIGN.md reserves the
    // real `disabled` attribute for.
    const canToggle = e.installed && !e.error;
    const setupBusy = _extSetupStarted.has(e.id);
    return `<div class="ext-row">
      <label class="ext-head">
        <input type="checkbox" class="ext-on" data-id="${esc(e.id)}"
               ${e.enabled ? 'checked' : ''} ${canToggle ? '' : 'disabled'}>
        <span class="ext-name">${esc(e.name)}</span>
        ${e.version ? `<span class="ext-ver">${esc(e.version)}</span>` : ''}
      </label>
      <span class="ext-status ext-${st.cls}">${esc(st.text)}</span>
      <p class="set-help ext-desc">${esc(e.description || EXT_PRODUCES[e.produces] || '')}
        ${st.note ? `<span class="ext-note">${esc(st.note)}</span>` : ''}</p>
      ${e.has_setup && !e.error
        ? `<div class="ext-actions"><button class="${e.installed ? 'tertiary' : 'secondary'} btn-sm ext-setup"
             data-id="${esc(e.id)}" ${setupBusy ? 'disabled' : ''}>${
               setupBusy ? 'Setup running…' : e.installed ? 'Re-run setup…' : 'Set up…'
             }</button></div>` : ''}
    </div>`;
  }).join('');
}
// ---- an extension's actions, in the detail view ----------------------------------------------
// ONE icon in the action bar, every extension inside it. A second extension adds a row to
// EXT_ACTIONS, never another button to that bar -- a bar that grows by one per installed thing is
// how the wrapping action bar (DS-9) happened in the first place. An extension therefore has
// exactly two surfaces in the detail view: an ACTION here, and a DETAIL in the list above.
// Named actions, for an extension whose action is peculiar to it.
const EXT_ACTIONS = {
  quality: { label: 'Get quality score', bulk: 'Get quality scores', run: () => rewardImage(),
             runBulk: () => rewardSelected() },
};
// What one extension offers to do to the file in front of you. An extension that PRODUCES TAGS
// gets its action for free, by kind rather than by name — so installing a third tagger adds a menu
// item without this file being edited, which was the whole point of the manifest.
function extAction(e) {
  if (EXT_ACTIONS[e.id]) return EXT_ACTIONS[e.id];
  if (e.produces === 'tags') {
    return { label: `Tag with ${e.name}`, bulk: `Tag with ${e.name}`,
             run: () => tagImages([state.current.id], e.id),
             runBulk: ids => tagImages(ids, e.id) };
  }
  if (e.produces === 'text') {
    // Both scopes, and they END DIFFERENTLY, which is the whole reason they are two code paths:
    // one file answers into the detail panel beneath its tags, a selection answers into a list you
    // read and dismiss. Neither keeps anything.
    return { label: `${e.name}…`, bulk: `${e.name}…`,
             bulkHint: 'Ask about each of the selected images in turn',
             run: () => askText(state.current.id, e.id),
             runBulk: ids => askTextMany(ids, e.id, e.name) };
  }
  return null;
}
function extActions() {
  return (state.extensions || [])
    .filter(e => e.active && extAction(e))
    .map(e => ({ id: e.id, act: extAction(e) }));
}
function renderExtMenu() {
  const live = extActions();
  // Gone, not empty. An icon that opens a menu with nothing in it promises something the app
  // cannot do -- the same rule the sidebar rail follows.
  $('#dExtBtn').classList.toggle('hidden', !live.length);
  $('#extMenu').innerHTML = live.map(e =>
    `<button data-ext-act="${esc(e.id)}">${esc(e.act.label)}</button>`).join('');
  // The same list in the selection menu, minus Quality — that one has had its own button there
  // since before extensions existed, and moving it would take a control out from under a habit.
  const box = $('#selExtActions');
  if (box) {
    // `act.bulk` is what decides it, not the kind: an extension with nothing sensible to do to a
    // hundred files at once declares no bulk action and is simply absent here.
    // The tooltip comes from the action too. It used to say "Tag the selected images" for every
    // entry, which was true while every entry was a tagger and became a lie the moment one wasn't.
    box.innerHTML = live.filter(e => e.id !== 'quality' && e.act.bulk).map(e =>
      `<button data-ext-bulk="${esc(e.id)}" title="${esc(e.act.bulkHint || 'Tag the selected images')}">${esc(e.act.bulk)}</button>`).join('');
  }
}
$('#dExtBtn').addEventListener('click', e => {
  e.stopPropagation();                            // else the document closer eats it immediately
  const wasOpen = !$('#extMenu').classList.contains('hidden');
  closeMenus();
  if (!wasOpen && state.current) placeMenu($('#extMenu'), e.currentTarget);
  e.currentTarget.setAttribute('aria-expanded', wasOpen ? 'false' : 'true');
});
$('#extMenu').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-ext-act]'); if (!b) return;
  $('#extMenu').classList.add('hidden');
  const ext = (state.extensions || []).find(x => x.id === b.dataset.extAct);
  const a = ext && extAction(ext);
  if (a) a.run();
});
$('#selMoreMenu').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-ext-bulk]'); if (!b) return;
  closeMenus();
  const ext = (state.extensions || []).find(x => x.id === b.dataset.extBulk);
  const a = ext && extAction(ext);
  if (a) a.runBulk([...selection].map(Number));
});

// ---- tagging ----------------------------------------------------------------------------------
// One path for one image and for a selection: a single image is a batch of one, so the progress
// bar, the Stop button and the one-job-at-a-time rule are the same machinery either way rather
// than a second, quieter code path that behaves subtly differently.
async function tagImages(ids, extId) {
  ids = (ids || []).filter(x => x != null).map(Number);
  if (!ids.length) { toast('Select images first'); return; }
  const j = await postJSON('/api/tagger/batch', { ids: expandCardIds(ids), ext: extId });
  if (j.error) { uiAlert(j.error); return; }
  await watchTagging();
}
function watchTagging() {
  return Job.start({
    name: 'tagging', blockedMsg: 'A job', every: 700,
    statusUrl: '/api/tagger/status', stopUrl: '/api/tagger/stop',
    progress: s => ({ pct: jobPct(s),
      text: `${s.ext || 'Tagging'}${s.device ? ' [' + s.device + ']' : ''} ` +
            `${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} (${jobPct(s)}%) · ` +
            `${fmtTime(s.elapsed)}${jobPace(s)}` }),
    // Tags change the sidebar list and its counts as well as the open image, so both are refreshed
    // before the bar comes down — a count that updates a beat later reads as a bug in the count.
    finish: async () => {
      await loadTags();
      // openDetail takes an ID, not an index — reopening on state.index fetches image #3 when you
      // are looking at the third card, which is a different picture on every library but the one
      // where they happen to line up.
      if (state.current && state.current.id != null) await openDetail(state.current.id);
    },
    summary: s => {
      const fails = s.failed ? ` · ${s.failed.toLocaleString()} failed` : '';
      return `Tagged ${(s.ok || 0).toLocaleString()} image(s) in ${fmtTime(s.elapsed)}${fails}`;
    },
  });
}

// ---- asking a text extension about the open image ----------------------------------------------
// Rides the same Job machinery as every other extension run, for one image. That looks like
// overkill for a single request and is not: it is where the wait state, the Stop button and the
// one-job-at-a-time rule live, and a local model on a cold cache can take a minute — long enough
// that a click with no feedback reads as nothing having happened.
function showAnswer(text, heading) {
  const wrap = $('#dAnswerWrap');
  if (!wrap) return;
  $('#dAnswerHead').textContent = heading || 'Answer';
  $('#dAnswer').textContent = text || '';
  wrap.classList.toggle('hidden', !text);
}
// Called wherever the panel changes file. An answer left standing over the next image is the one
// failure that matters here: it would read as being about THAT picture.
function clearAnswer() { showAnswer(''); }

async function askText(id, extId) {
  if (id == null) { toast('Open an image first'); return; }
  clearAnswer();
  const j = await postJSON('/api/text/ask', { ids: [id], ext: extId });
  if (j.error) { uiAlert(j.error); return; }
  const askedAbout = id;
  return Job.start({
    name: 'asking', blockedMsg: 'A job', every: 500,
    statusUrl: '/api/text/status', stopUrl: '/api/text/stop',
    // No count: it is one file, so "1/1 (100%)" is noise. The elapsed time is the real signal —
    // it is the only way to tell a slow model from a stuck one.
    progress: s => ({ pct: null, text: `${s.ext || 'Asking'} · ${fmtTime(s.elapsed)}` }),
    finish: async (s) => {
      const a = (s && s.answers || [])[0];
      if (a && a.error) { uiAlert(a.error); return; }
      // Only onto the image it was asked about. The panel can have moved on while a slow model
      // was thinking, and an answer about the previous picture appearing under this one is worse
      // than no answer at all.
      if (state.current && state.current.id === askedAbout && a && a.text) {
        showAnswer(a.text, s.ext || 'Answer');
      }
    },
    summary: () => '',
  });
}

// ---- answers over a selection ------------------------------------------------------------------
// The grid's ⋯ scope. Same run, same Stop, different ending: a list you read and then throw away.
// Rows arrive AS THEY ARE ANSWERED rather than all at once at the end — over twenty files on a
// local model that is minutes, and a modal that sits empty for minutes is indistinguishable from a
// modal that is broken.
let _answersSeen = 0;          // how many of this run's answers are already on screen

function answerRowHTML(a) {
  const body = a.error
    ? `<p class="answer-text is-error">${esc(a.error)}</p>`
    : `<p class="answer-text">${esc(a.text || '')}</p>`;
  // The thumbnail URL is the server's, not one built here — it carries the v= and r= that keep a
  // browser cache entry from being shared between two libraries' images.
  // Number(), not esc(): esc is string-only — `(s || '').replace` — so handing it an id throws,
  // and inside a Job's progress callback that throw is swallowed, which looks exactly like a model
  // that answered nothing. Every other card in this file writes an id bare for the same reason.
  //
  // NO loading="lazy", which the grid's cards all carry. Copied in from there it looked harmless
  // and left every thumbnail here blank: inserted into an open overlay these never begin loading
  // at all, while the same URL loads at once without it. Lazy pays for itself across a grid of
  // thousands; this list is a screenful of 128px thumbnails and it only bought the bug.
  return `<div class="answer-row" data-id="${Number(a.id)}">
    <img draggable="false" src="${esc(a.thumb_url || '')}" alt="">
    <div class="answer-body">
      <p class="answer-name" title="${esc(a.name || '')}">${esc(a.name || '')}</p>
      ${body}
    </div>
  </div>`;
}

function closeAnswers() {
  $('#answersModal').classList.add('hidden');
  $('#answersList').innerHTML = '';       // closing IS the discard; there is nowhere else it lives
  _answersSeen = 0;
}

async function askTextMany(ids, extId, extName) {
  ids = (ids || []).filter(x => x != null).map(Number);
  if (!ids.length) { toast('Select images first'); return; }
  const j = await postJSON('/api/text/ask', { ids, ext: extId });
  if (j.error) { uiAlert(j.error); return; }
  _answersSeen = 0;
  $('#answersTitle').textContent = extName || 'Answers';
  $('#answersList').innerHTML = '';
  $('#answersStatus').textContent = `Asking about ${ids.length.toLocaleString()}…`;
  $('#answersCancel').textContent = 'Cancel';
  $('#answersCancel').disabled = false;
  $('#answersModal').classList.remove('hidden');
  const drain = (s) => {
    for (const a of (s.answers || [])) {
      $('#answersList').insertAdjacentHTML('beforeend', answerRowHTML(a));
    }
    _answersSeen = s.answers_total != null ? s.answers_total : _answersSeen;
  };
  return Job.start({
    name: 'asking', blockedMsg: 'A job', every: 700,
    // The cursor rides the status URL, so the server sends only what has not been drawn yet.
    statusUrl: () => `/api/text/status?since=${_answersSeen}`,
    stopUrl: '/api/text/stop',
    progress: s => {
      drain(s);
      $('#answersStatus').textContent =
        `${(s.seen || 0).toLocaleString()} of ${(s.total || 0).toLocaleString()} · ${fmtTime(s.elapsed)}`;
      return { pct: jobPct(s), text: `${s.ext || 'Asking'} ${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} · ${fmtTime(s.elapsed)}` };
    },
    finish: (s) => {
      if (s) drain(s);           // the last answers land between the final poll and the finish
      const n = $('#answersList').children.length;
      $('#answersStatus').textContent = s && s.stopped
        ? `Stopped — ${n.toLocaleString()} answered`
        : `${n.toLocaleString()} answered · not saved, closing discards them`;
      // The one button changes job rather than a second appearing beside it: while it runs it
      // calls the run off, and afterwards the only thing left to do is dismiss the list.
      $('#answersCancel').textContent = 'Close';
      $('#answersCancel').disabled = false;
    },
    summary: () => '',
  });
}

$('#answersCancel').addEventListener('click', async () => {
  if (Job.busy && Job.name === 'asking') {
    $('#answersCancel').disabled = true;
    $('#answersCancel').textContent = 'Stopping…';
    await postJSON('/api/text/stop', {});
    return;                      // finish() re-enables it as Close when the run actually ends
  }
  closeAnswers();
});
$('#answersClose').addEventListener('click', closeAnswers);
$('#answersModal').querySelector('.overlay-bg').addEventListener('click', () => {
  if (!(Job.busy && Job.name === 'asking')) closeAnswers();
});

$('#extRows').addEventListener('change', async (ev) => {
  const cb = ev.target.closest('.ext-on'); if (!cb) return;
  // Saved on the spot, not on Save. This switch changes what the window BEHIND the dialog is
  // showing; deferring it to a footer button that also saves the theme and the card facts would
  // make one visible change wait on an unrelated one.
  const j = await postJSON('/api/extensions/enable', { id: cb.dataset.id, enabled: cb.checked });
  if (j.error) { uiAlert(j.error); cb.checked = !cb.checked; return; }
  state.extensions = j.extensions || [];
  renderExtRows();
  // Either way the cards change: the sort/filter case needs a re-query, the badge-only case just
  // needs the cards drawn again from what is already in hand.
  if (applyExtensions()) search(true); else repaintCards();
});
$('#extRows').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.ext-setup'); if (!btn) return;
  const j = await postJSON('/api/extensions/setup', { id: btn.dataset.id });
  if (j.error) { uiAlert(j.error); return; }
  _extSetupStarted.add(btn.dataset.id);
  renderExtRows();
  // The setup window owns it from here. Nothing polls: it can take an hour, it can be cancelled in
  // its own console, and the app has no business waiting on either.
  toast('Setup started in its own window. Reopen Settings when it has finished.');
});

// ---- pyiqa reward scoring (local, in the scorer venv) ----
// `after` replaces the default finish. The selection bar wants the grid rebuilt; the detail view
// wants the opposite (see refreshDetailQuality). One job, two endings, rather than two jobs.
function watchReward(after) {
  const bits = s => {
    const fails = s.failed ? ` · ${s.failed.toLocaleString()} failed` : '';
    const dev = s.device ? ` [${s.device}]` : '';
    return { fails, dev };
  };
  return Job.start({
    name: 'reward', blockedMsg: 'A job', every: 700,
    statusUrl: '/api/reward/status', stopUrl: '/api/reward/stop',
    progress: s => { const { fails, dev } = bits(s); return { pct: jobPct(s),
      text: `Quality scoring${dev} ${(s.seen || 0).toLocaleString()}/${(s.total || 0).toLocaleString()} (${jobPct(s)}%) · ` +
            `${fmtTime(s.elapsed)}${jobPace(s)}${fails}` }; },
    // Quality badges appear on the cards, so the grid has to be rebuilt — and awaited, before the
    // progress bar comes down. It used to fire unwatched after the teardown, so the summary sat over
    // the old grid and the badges arrived a beat later.
    finish: async () => {
      if (after) { await after(); return; }
      await refreshAfterJob(() => search(true));
    },
    summary: s => { if (!s) return ''; const { fails, dev } = bits(s);
      return s.stopped ? `Stopped · ${(s.ok || 0).toLocaleString()} scored${fails} · ${fmtTime(s.elapsed)}`
                       : `Quality-scored ${(s.ok || 0).toLocaleString()} image(s)${dev} in ${fmtTime(s.elapsed)}${fails}`; },
    tail: 6000,
  });
}
async function rewardSelected() {
  const ids = [...selection].map(Number);
  if (!ids.length) return;
  // Blunt overwrite: always re-score the whole selection (skip_scored:false). Stop cancels.
  const j = await postJSON('/api/reward/batch', { ids, skip_scored: false });
  if (j.error) { uiAlert('Quality scoring failed: ' + j.error); return; }
  if (!j.todo) { toast('All selected images were already scored'); return; }
  watchReward();
}
// Per-library "files changed on disk" check: paints a ↻ on each library row whose folder
// changed since its last scan (cheap folder-mtime check server-side), driving a targeted rescan.
// Split so refreshView can fetch this alongside the other three and paint the rail once. Standalone
// callers (the background probe, a library switch, boot) still want fetch-and-paint in one call.
// Resolves TRUE when a library's reachability flipped, so the caller can repaint more than the row.
async function fetchChanges() {
  try {
    const j = await getJSON('/api/changes');
    state._changes = (j && j.changes) || {};
    // The only thing that re-reads reachability on a running app. /api/config has it too, but that
    // is fetched at boot and on library edits, so a share coming back stayed marked offline until a
    // restart — and, worse, kept its files marked unusable while the grid went on showing CACHED
    // thumbnails, which made it look recovered when it was not (the author, 2026-09-06).
    const seen = (j && j.exists) || null;
    // WHICH WAY IT FLIPPED, not just that it did: a library leaving interrupts with a dialog and one
    // arriving gets a toast, so the caller cannot treat the two the same. `went` and `came` hold the
    // keys; `flipped` is still the boolean the repaint decision has always used.
    const went = [], came = [];
    if (seen) for (const r of (state.roots || [])) {
      if (!(r.key in seen) || r.exists === seen[r.key]) continue;
      r.exists = seen[r.key];
      (r.exists === false ? went : came).push(r.key);
    }
    return { flipped: went.length + came.length > 0, went, came };
  } catch (e) { return { flipped: false, went: [], came: [] }; }   // leave rows as-is on a failed check
}
// ONE SENTENCE FOR BOTH MOMENTS -- a library that has just gone, and one that was already gone when
// the app started. Two variants were drafted and cut: at this size "went offline unexpectedly" and
// "was already offline" do not need separate sentences, and an edge case does not earn two strings
// to keep in step. Awaited by the caller, so the view refreshes as it is dismissed rather than
// rearranging underneath the dialog.
function offlineAlert() {
  return uiAlert('One or more libraries are offline. They\'ll be disabled until they\'re back online.',
                 'Libraries offline');
}
// ONE CHECK AT A TIME, SHARED BY EVERY CALLER. Returning to the window wakes two of them at once --
// probeChanges(), which keeps the strip's ↻ honest whether or not the clock is on, and the refresh
// tick itself -- and each asked independently. On five libraries across network shares a trace
// showed that as two overlapping /api/changes on EVERY return, about a second each, the second
// reporting a much smaller server time because it was queued behind the first.
//
// SINGLE-FLIGHT SITS HERE, NOT ON fetchChanges, because this function is not only a fetch: it
// repaints the library list, may raise a toast about a share going or coming back, and on a flip
// re-runs the whole search. Sharing just the network call would still have done all of that twice,
// including a second full search -- the expensive half.
//
// FIXED HERE RATHER THAN BY TEACHING THE CALLERS ABOUT EACH OTHER. Both fire for good reasons and
// neither is wrong to ask; what is wrong is asking again while the answer is already on its way. A
// future caller gets this for free without knowing the rule exists.
//
// Cleared in a finally, so one failed check cannot wedge every later one.
let _changesInFlight = null;
function checkChanges() {
  if (!_changesInFlight) {
    _changesInFlight = _checkChanges().finally(() => { _changesInFlight = null; });
  }
  return _changesInFlight;
}
async function _checkChanges() {
  const { flipped, went, came } = await fetchChanges();
  renderLibList(window._facets && window._facets.roots);
  // A LIBRARY LEAVING INTERRUPTS; ONE ARRIVING DOES NOT. The author's call, 2026-09-14: losing a share
  // mid-session takes cards off the screen, and a grid that quietly shrinks by a third is the kind
  // of thing a tester reports as data loss. Coming back only ADDS, so it gets a toast.
  //
  // Deliberately NOT named, and no instructions for getting them back. His reasoning is that this
  // is an edge case -- most people have no network drives at all -- so it is worth saying clearly
  // once and not worth a paragraph.
  if (went.length) offlineAlert();
  // NAMED when there is one, counted when there are several -- a toast has room for one fact, and
  // "which one" is the useful one right after plugging a drive back in. "back online" rather than
  // "back", to echo the dialog that said they would be disabled until they were.
  else if (came.length) toast(came.length === 1 ? `${libName(came[0])} is back online`
                                                : `${came.length} libraries are back online`);
  // A library arriving or leaving changes what every one of its CARDS can do — drag, open, play,
  // the offline badge — not just its row in this list. Repainting only on a flip is the point: this
  // runs off every filter change, and a grid repaint per probe would be a real cost for a fact that
  // changes almost never.
  // THE FACETS GO WITH IT since 2026-09-14, and that is a change of meaning rather than a tidy-up:
  // an unreachable library is now out of scope entirely, so a flip moves what the whole rail is
  // counting. Skipping them here would leave the dropdowns tallying a library the grid had just
  // stopped showing — the same disagreement the Type filter caused, arriving by a different door.
  if (flipped) search(true);
}
// Resolves with the scan's final status, or null if it never started. Awaitable, so a run over
// several libraries is a for-loop instead of a callback that has to remember to hand the
// presentation back — that callback is where a refused second scan used to hang the whole app.
async function rescanLib(key, quiet) {
  let j = {};
  try { j = await (await fetch('/api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) })).json(); }
  catch (e) {}
  // A scan is refused (409) when another job holds the index. Don't watch one that never started;
  // record why so the run can report it once, after its own teardown.
  if (j && j.error) { if (!quiet) _runError = j.error; return null; }
  return watchScan('Scanning', quiet);
}
// RESCAN MEANS "MAKE THIS LIBRARY RIGHT", which is the thing the one-verb decision of 2026-09-12
// intended and did not deliver. That change deleted `Rebuild metadata` from the menu and left the
// remaining Rescan as the cheap check, so a library behind this build's reader could not be caught
// up from its own menu at all — while the launch dialog told you to do exactly that. Proved rather
// than reasoned, 2026-09-14: an ordinary rescan of two stale rows reported `skipped: 2` and left
// them stale; the forced one reported `updated: 2` and cleared them.
//
// So the menu route asks the index first and does whichever scan the answer calls for. There is
// still ONE verb and the user still chooses nothing — the app decides how much work "right" is.
//
// NOT folded into rescanLib itself, and that is the important part: refreshChanged() (the ✓, and
// the R key) calls rescanLib for every library that changed on disk. Teaching THAT path to force
// would make a keystroke start a multi-minute job, which the same commit forbids in as many words.
// The cheap check stays cheap; only the menu's Rescan is allowed to be expensive, and only after
// the confirm below.
async function rescanLibFromMenu(key) {
  const behind = state._behind && state._behind[key];
  if (!behind) return rescanLib(key);
  const cost = behind.seconds
    ? `Takes ${fmtRoughTime(behind.seconds)} for ${behind.files.toLocaleString()} files.`
    : `There are ${behind.files.toLocaleString()} files to rescan.`;
  const ok = await uiConfirm(
    `${libName(key)} was indexed before this version, so some of what the app can now read is ` +
    `missing from it. Rescanning re-reads every file in it.\n\n${cost}`,
    { title: 'Rescan for new version features', ok: 'Rescan' });
  if (!ok) return null;
  const r = await rebuildOne(key);
  await refreshBehind();
  return r;
}
// One library's forced re-read. Shared by Rebuild metadata and Rebuild all libraries, so the two can
// never drift in what "rebuild" means. Returns falsy when the scan was refused (another job holds
// the index) or contact was lost, which is a signal to a multi-library run to stop rolling on.
async function rebuildOne(key, quiet, runLabel) {
  let j = {};
  try {
    j = await (await fetch('/api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify({ force: true, key }) })).json();
  } catch (e) {}
  if (j && j.error) { _runError = j.error; return null; }
  return watchScan('Rebuilding', quiet, runLabel);
}
// Every library in one run, and the only thorough re-read left in the interface. It exists because a
// build that learns to read something new fills only the libraries you remember to catch up — the author
// had rebuilt one of four and read the other three's blank generation settings as a parsing bug.
//
// ONE Job for the whole run so the screen can't flash between libraries, ONE refresh at the end, and
// Stop ends the run rather than the library in front of it. Stopping part-way is safe in a way it
// was not before: each row carries the reader version that read it, so the next prompt picks up
// exactly where this stopped.
// A DURATION IN PROSE, not on a stopwatch. fmtTime is right on a job bar, where "12:00" sits beside
// a count that is also moving; in a sentence someone reads once before deciding, "12:00" is a clock
// face where a length of time was meant. Rounded on purpose — this is an estimate, and a precise
// number would claim more than it knows.
function fmtRoughTime(sec) {
  if (!sec || !isFinite(sec)) return '';
  if (sec < 90) return 'under a minute';
  const mins = Math.round(sec / 60);
  if (mins < 60) return `about ${mins} minutes`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return `about ${h} hour${h === 1 ? '' : 's'}` + (m ? ` ${m} minutes` : '');
}
// "About 12 minutes for 110,000 files" or, when nothing has been measured yet, the count alone.
// NEVER A GUESSED FIGURE. A fresh install has no measured rate, and the honest answer there is the
// file count on its own — this is the number someone decides on, and a made-up minute figure is
// worse than no figure.
// THE MENU'S PRICE IS ABOUT EVERY FILE, not the backlog: Rescan all libraries re-reads the lot, and
// it can be chosen when nothing is behind at all — which is most of the time. Reading the backlog
// here left it saying "this can take a while" on a machine whose speed we had measured.
async function rescanCostLine() {
  let j = {};
  try { j = await getJSON('/api/catchup'); } catch (e) { return 'This can take a while.'; }
  if (!j.files_all) return 'This can take a while.';
  const n = j.files_all.toLocaleString();
  return j.seconds_all ? `Takes ${fmtRoughTime(j.seconds_all)} for ${n} files.`
                       : `${n} files to rescan.`;
}
// THE OFFER, on the first launch of a build that reads more than the one which indexed a library.
// The author's design and the author's words, 2026-09-12: "that version should include an optional trigger to
// recommend a File Rescan on first launch."
//
// A dialog, deliberately, and it cannot collide with the first-run sequence — it only ever appears
// for an EXISTING library on an UPDATED build. Nothing here starts on its own: this asks, and the
// long job is the user's click. See the no-background-jobs rule.
//
// "Do it later" is remembered for this SESSION only, so it returns next launch. A postponed rescan
// that never comes back is a lost one, and the blanks stay with nothing left to prompt them —
// sessionStorage is exactly "until you next start the app", where localStorage would be "never
// again" and config would be "never again on this machine".
const CATCHUP_SNOOZE = 'vv.catchup.snoozed';
// WHICH libraries are behind this build's reader, keyed by library. The launch dialog says THAT
// some are; this is what lets each row say whether it is one of them, which is the question someone
// who dismissed the dialog is left holding. The author, 2026-09-14: "if the user does it by hand, there
// is no way to know which ones need a rescan."
//
// Re-read from the server rather than decremented as scans finish: a stopped run leaves some rows
// stamped and some not, so the only honest count is the one the index can still see.
function noteBehind(libs) {
  const was = JSON.stringify(state._behind || null);
  state._behind = {};
  for (const l of (libs || [])) state._behind[l.key] = l;
  // Repaint only on a real change — this runs after every scan, and the list resets its own scroll.
  if (JSON.stringify(state._behind) !== was) renderLibList(window._facets && window._facets.roots);
}
async function refreshBehind() {
  try { noteBehind((await getJSON('/api/catchup')).libraries); } catch (e) {}
}
async function offerCatchUp() {
  // THE FETCH HAPPENS EVEN WHEN THE DIALOG WILL NOT, and the order is the whole point: the snooze
  // silences the OFFER, not the per-library marks. Someone who answers "Do it later" is exactly the
  // person who then goes looking for which libraries need it, so returning before the fetch would
  // blank the marks for the only user who wants them.
  let j;
  try { j = await getJSON('/api/catchup'); } catch (e) { return; }
  noteBehind(j.libraries);
  try { if (sessionStorage.getItem(CATCHUP_SNOOZE)) return; } catch (e) {}
  const live = (j.libraries || []).filter(l => l.reachable);
  const off = (j.libraries || []).filter(l => !l.reachable);
  if (!live.length) return;                    // nothing to offer, or nothing reachable to offer it for
  const cost = j.seconds
    ? `Rescanning takes ${fmtRoughTime(j.seconds)} for ${j.files.toLocaleString()} files.`
    : `There are ${j.files.toLocaleString()} files to rescan.`;
  // Offline libraries are NAMED, not silently dropped: they keep their old stamps, so they are
  // offered again next launch, and saying so is the difference between a skip and a surprise.
  const skip = off.length
    ? `\n\nSkipping ${off.length} that ${off.length === 1 ? 'is' : 'are'} offline: ` +
      `${off.map(l => l.name).join(', ')}. You'll be asked again when ${off.length === 1 ? 'it is' : 'they are'} back.`
    : '';
  // THE MENU ROUTE IS NAMED IN THE DIALOG, not only in the message after declining. Two reasons:
  // it makes "Do it later" a choice rather than a dead end, and it is the only place the user
  // learns they can take ONE library rather than all of them — which is the option this dialog
  // does not otherwise offer, and the one someone with five libraries and one slow share wants.
  // THE VERSION IS IN THE HEADING, so the sentence can be about what to do rather than spending its
  // first clause establishing that an update happened. The author's line. The number falls out of the
  // title when the server has not sent one, rather than printing "version  of" — a release with no
  // version is a bug in the build, not something to announce to the user mid-sentence.
  const title = _appVersion
    ? `Welcome to version ${_appVersion} of ${_appName || 'VV Curator'}!`
    : `${_appName || 'VV Curator'} has been updated`;
  const go = await uiConfirm(
    // THE RELATIONSHIP, NOT THE CHANGE, and that is what makes it last. Every wording that named
    // WHAT improved would need revisiting per release: "new features" points at the interface,
    // where nothing changed; "reads more metadata" is false for a version that fixes something it
    // read WRONGLY, and for one that merely groups files differently -- the model-family merge
    // already needs a rescan for exactly that reason. The author asked the right question of my own
    // proposal: "will 1 ALWAYS be true?" It would not have been. This sentence is true of every
    // bump there can be, because it describes the gap rather than what filled it. What improved
    // belongs in the release notes, which have room to say it.
    `Your libraries were indexed by an older version. Rescanning brings them up to date with ` +
    `this one.` +
    `\n\n${cost}${skip}\n\nOr rescan them yourself: Rescan in each library's ⋯ menu, or ` +
    `Rescan all libraries in the Libraries menu.`,
    { title, ok: 'Rescan now', cancel: 'Do it later' });
  if (!go) {
    try { sessionStorage.setItem(CATCHUP_SNOOZE, '1'); } catch (e) {}
    // WHERE IT WENT, so "later" is not a door closing. Named exactly as the menu item reads.
    toast('You can do this any time from Libraries → Rescan all libraries…');
    return;
  }
  await runRescanAll(live.map(l => l.key), off.map(l => l.key));
}
async function rescanAllLibs() {
  // state.roots, not the facets copy: it is what libName and isRootOffline read, and `exists` (the
  // offline flag) only lives there.
  const keys = allRootKeys().filter(Boolean);
  const live = keys.filter(k => !isRootOffline(k));
  const off = keys.filter(k => isRootOffline(k));
  if (!live.length) { toast(off.length ? 'Every library is offline' : 'No libraries to rescan'); return; }
  // Offline libraries are named in the confirm, not discovered afterwards — being told what a run
  // will skip BEFORE it takes ten minutes is the whole difference between a skip and a surprise.
  const skip = off.length ? `\n\nSkipping ${off.length} offline: ${off.map(libName).join(', ')}.` : '';
  // ONE WORD FOR THE ACT, everywhere the user meets it: rescan. The author, 2026-09-12. "Re-read" is the
  // mechanism and belongs in the comments; a second word for the same thing in the interface is how
  // Rescan and Rebuild metadata became two ideas in the first place.
  // "all 1 libraries" is what counting without reading gives you, and one library is the common
  // case for anyone who is not the author.
  const scope = live.length === 1 ? `“${libName(live[0])}”` : `all ${live.length} libraries`;
  if (!(await uiConfirm(`Rescan every file in ${scope}?
Keeps tags, labels & favorites. ${await rescanCostLine()}${skip}`, { ok: 'Rescan all' }))) return;
  await runRescanAll(live, off);
}
// The run itself, with no question in front of it: the menu item asks one way and the update offer
// asks another, and a second confirm behind either would be the app asking twice.
async function runRescanAll(live, off) {
  off = off || [];
  _runStopped = false; _runError = null;
  await Job.during(async () => {
    let done = 0;
    for (const k of live) {
      if (_runStopped) break;
      // SAID TWICE ON PURPOSE, and they are not the same message. This one goes up the instant the
      // library changes, before the scan has started or counted anything, so the pill is never
      // blank and never still naming the library we just finished. The poll then replaces it with
      // the same line carrying this library's file count and bar — the detail a single rescan has
      // always shown and a run of five used to throw away.
      const at = `Rescanning ${libName(k)} (${done + 1} of ${live.length})`;
      Job.say(`${at}…`, true);   // true: this library's bar starts empty, not where the last one ended
      if (!(await rebuildOne(k, true, at))) break;  // refused, or contact lost — don't roll on
      done++;
    }
    // Runs even when the run was cut short: whatever did get re-read should still show.
    await afterScanRefresh(false);
    return done === live.length
      ? `Rescanned ${done} librar${done === 1 ? 'y' : 'ies'}${off.length ? ` · ${off.length} offline skipped` : ''}`
      : `Rescanned ${done} of ${live.length}`;
  });
  if (_runError) { toast(_runError); _runError = null; }
}
let _runStopped = false;   // Stop ends the whole run, not just the library in front of it
let _runError = null;      // why a run ended early; reported AFTER its teardown so nothing wipes it
async function stopScan() {
  // ASKED BEFORE ANYTHING IS TOUCHED. A job can put a question in front of its own stop button, and
  // "no" here has to leave the run completely undisturbed -- so this returns before the button is
  // disabled or relabelled, not after.
  const ask = Job.onStop;
  if (ask && !(await ask())) return;
  const b = $('#stopScan'); b.disabled = true; b.textContent = 'Stopping…';
  _runStopped = true;
  const url = Job.stopUrl;                      // whatever owns the slot RIGHT NOW, not the last
  if (url) await fetch(url, { method: 'POST' });  // thing that happened to set a variable
}
// R (grid view): the keyboard equivalent of clicking every ↻ at once. Forces the cheap on-disk
// change check to re-run (so files deleted/added by hand in Explorer are noticed now, not on the
// next periodic poll), then rescans ONLY the libraries it flags — each rescan prunes vanished files
// and keeps curation, and the offline guard means an unreachable share is skipped, not wiped.
let _refreshBusy = false;
async function refreshChanged() {
  if (_refreshBusy || Job.busy) return;         // already checking, or a job is already in flight
  // ONE presentation for the whole run: the pill goes up before the first request and stays up,
  // its text changing as the run moves on. Job.during also holds the block across every scan in the
  // run, so the screen can't flash between libraries, and ONE refresh runs at the end rather than
  // one per library. _runStopped/_runError are cleared at the START of the run that reads them:
  // Stop is also reachable from scans this function never started (the per-library mark, an index
  // build), and a flag left standing by one of those would make the next refresh scan nothing.
  _runStopped = false; _runError = null;
  await Job.during(async () => {
    Job.say('Checking for changes…');           // stats every folder — not instant on a share
    _refreshBusy = true;
    _lastChangeProbe = Date.now();              // shares the probe's clock so the two can't double up
    try { await checkChanges(); }               // re-run /api/changes and repaint the indicators
    finally { _refreshBusy = false; }
    const keys = Object.keys(state._changes || {}).filter(k => state._changes[k]);
    if (!keys.length) return 'Libraries up to date';
    for (const k of keys) {
      if (_runStopped) break;
      if (!(await rescanLib(k))) break;         // refused, or contact lost — don't roll on
    }
    // Whatever did get indexed should still show, so this runs even when the run was cut short.
    await afterScanRefresh(false);
    return '';
  });
  // After the run's teardown, never before: it retires the pill on its way out, so a message raised
  // earlier would be swept away by the very cleanup that lets the user act on it.
  if (_runError) { toast(_runError); _runError = null; }
}
// Re-check /api/changes off the back of a filter change, so the strip's ↻ can go amber on its own.
// Deliberately NOT a timer: a poll that runs while you stare at the same grid is pure cost, while a
// filter change is a real signal — it means you looked away from the results and came back to them.
// Two properties are load-bearing:
//   FIRE-AND-FORGET — never awaited by search(). /api/changes stats every library folder, which on
//     a network share is a round trip you can feel; a filter change must not carry it.
//   THROTTLED, LEADING EDGE — filter changes arrive in bursts (typing chips, clicking facets), and
//     each burst would otherwise be a stat storm. Leading edge so the FIRST change after a pause —
//     the one that means you just came back — is checked immediately, with no timer left running.
// Skipped while a scan or a manual R owns the index: checkChanges() would race the scan's own
// post-run check and repaint the list under a spinning row.
const CHANGE_PROBE_MS = 20000;
let _lastChangeProbe = 0;
// The OTHER half of "it didn't update": this is what turns the strip's ↻ amber, and it runs whether
// or not the auto-refresh clock is on. A mark that never appears and a grid that never fills are
// the same complaint from the outside and different bugs underneath, so the trace has to tell them
// apart. Not summarised like the tick's gates — probeChanges has only two exits and its callers are
// user actions (a search, coming back to the window), so it cannot run away with the buffer.
function probeChanges() {
  if (Job.busy || _refreshBusy || _autoScanning) {
    return Trace.add('probe', 'skipped — something is already running');
  }
  const now = Date.now();
  if (now - _lastChangeProbe < CHANGE_PROBE_MS) {
    return Trace.add('probe', `skipped — asked ${Math.round((now - _lastChangeProbe) / 1000)}s ago `
                            + `(one per ${CHANGE_PROBE_MS / 1000}s)`);
  }
  _lastChangeProbe = now;
  Trace.add('probe', 'asking whether anything landed on disk');
  checkChanges();          // deliberately NOT awaited
}
// ---- auto-refresh (idle-driven) --------------------------------------------------------------
// Opt-in: when the user goes quiet, quietly pick up newly-generated files. It fires ONLY after
// IDLE_MS of no interaction (so an active user is never interrupted), only when the tab is visible
// and no overlay/detail is open and no scan is already running — and even then only scans libraries
// that actually changed. New files are PREPENDED at the top (prependNewImages), never via a
// scroll-resetting search(true), and the scan runs quiet (no blocking scrim).
const AUTO_REFRESH_KEY = 'cv:autoRefresh';
const IDLE_MS = 30000;        // fire only after this much inactivity (fixed for now; one constant to change)
const AUTO_CHECK_MS = 5000;   // how often the checker re-evaluates idleness
let _autoOn = false;
let _autoTimer = null;        // its own interval handle; Job owns the job poller
let _lastActivity = Date.now();
let _lastAutoRefresh = 0;
let _autoScanning = false;
let _deferredNew = false;     // new files indexed while scrolled down — fold in once back at the top

// ---- what the trace sees of all this ----------------------------------------------------------
// "It didn't update" was undiagnosable, because the tick below has NINE ways to decide against
// doing anything and every one of them was a bare `return`. Two such gates were fixed on
// 2026-08-23 by reading the code and guessing; this is how the next one gets caught instead.
//
// A ROW PER TICK WOULD DROWN THE TRACE. The timer runs every 5s, so an idle app would file twelve
// rows a minute and push everything else off the front of a 600-row buffer in under an hour — the
// trace would record only its own heartbeat. So a gated tick is logged when the REASON CHANGES,
// and the run of identical ones is summarised by count when it ends. A tick that actually RUNS is
// always logged, because at most one fires per idle stretch and it is the interesting case.
// Where the refresh story stands RIGHT NOW, written at the top of a fresh recording. Everything
// else here is an event; this is the state those events happen against.
function traceRefreshState() {
  // Forget any run of skips in progress. Switching the trace on CLEARS the rows but not this
  // module's memory, so without it a fresh recording can open with "…and 10 more stopped the same
  // way" and never say which way that was — a summary of rows the reader cannot see.
  _gateWas = null; _gateRun = 0;
  Trace.add('refresh', `clock is ${_autoOn ? 'ON' : 'OFF'}`
    + (_autoOn ? ` — checks every ${AUTO_CHECK_MS / 1000}s, acts after ${IDLE_MS / 1000}s of quiet` : '')
    + ` · ${(state.roots || []).length} librar${(state.roots || []).length === 1 ? 'y' : 'ies'}`
    + ` · sort=${state.sort} ${state.order}`
    + (_deferredNew ? ' · new files are being held back' : ''));
}
// THE RUN IS KEYED ON WHICH GATE, NOT ON ITS WORDING. The wording carries live numbers ("active 5s
// ago", then 6s, then 11s), so comparing sentences makes every tick look like a new reason and the
// flood this exists to prevent comes back — which is exactly what the first version did, at one row
// per 5s tick. The key is the gate's identity; the sentence is written once, for the row that opens
// the run, where the numbers are still worth having.
let _gateWas = null, _gateRun = 0;
function traceGate(key, detail) {
  if (!Trace.on) return;
  if (key === _gateWas) { _gateRun++; return; }
  if (_gateWas && _gateRun) Trace.add('refresh', `…and ${_gateRun} more stopped the same way`);
  _gateWas = key; _gateRun = 0;
  // A null key means the tick got through, and it writes NO row of its own: the line immediately
  // below it already says "checking for new files", so announcing the same event twice would just
  // push the rest of the trace off the front of the buffer. Its whole job here is to close any run
  // of skips above.
  if (key) Trace.add('refresh', `tick stopped — ${detail}`);
}
// "Am I mid-task in a modal?" — renaming, changing a setting, tagging a selection. Refreshing the
// library under one of those is obviously wrong, so background work stands down for them.
//
// THE DETAIL VIEW IS DELIBERATELY NOT IN THIS LIST, and having it here cost the author months of "the
// grid doesn't update". It is where he spends most of his time, and leaving an image open while
// generating in ComfyUI is precisely the case auto-refresh exists for — so the one gate meant to
// protect mid-task modals was also excluding the commonest foreground use of the feature. What the
// gate was actually protecting was the FILMSTRIP, not the grid, and it stopped both; the freeze now
// lives where it belongs, in prependNewImages(), which is the only step that would move anything.
//
// (This replaced an `anyLayerOpen()` that counted the detail view. Its comment claimed the keydown
// handlers shared it; they had long since stopped — each one needs to know WHICH layer is topmost,
// not merely that one is, so they build their own booleans and this had exactly one caller.)
function anyModalOpen() {
  return ['#settings', '#rename', '#setup', '#tagModal']
    .some(sel => !$(sel).classList.contains('hidden'));
}
// Any interaction just stamps the time (cheap; no per-event work) — this is what keeps auto-refresh
// paused while the user is engaged. passive so scroll/wheel stay smooth.
['mousemove', 'scroll', 'keydown', 'click', 'touchstart', 'wheel'].forEach(ev =>
  window.addEventListener(ev, () => { _lastActivity = Date.now(); }, { passive: true }));
// Coming BACK to the tab: check straight away rather than making the user wait out another idle
// period — returning from ComfyUI is exactly when they want to see what landed. (Deliberately does
// NOT stamp _lastActivity: that would delay the refresh at the very moment it's wanted.)
// probeChanges() runs here whether or not auto-refresh is ON, because coming back to the tab is the
// one moment the strip's ↻ can go amber without the user having touched a filter — and that IS the
// workflow: generate in ComfyUI, switch back, change nothing. Same throttle, so flicking between
// tabs costs one round trip rather than one per flick.
document.addEventListener('visibilitychange', () => {
  // BOTH DIRECTIONS are traced, and the hidden one matters most: browsers throttle a background
  // tab's timers to roughly once a minute, so the 5s cadence quietly becomes 60s and a gap in the
  // trace that looks like a bug is the browser doing its job. Nothing else records that boundary.
  Trace.add('refresh', document.hidden
    ? 'tab hidden — timers now throttled by the browser (~1/min)'
    : 'tab visible again — checking straight away');
  if (document.hidden) return;
  probeChanges();
  autoRefreshTick('return');
  refreshCardAges();      // a backgrounded tab's timer is throttled, so catch the labels up here
});
// A window can be given back WITHOUT ever having been hidden — alt-tab to a viewer that was never
// covered fires `focus` and no visibilitychange at all — so coming back had two doors and only one
// of them woke anything up. Same 'return' tick, so the once-per-idle-stretch throttle still means
// flicking between windows costs one catch-up, not one per flick.
window.addEventListener('focus', () => { autoRefreshTick('return'); refreshCardAges(); });
// Three ways in, and they differ ONLY in which timing gates they skip. The in-flight guards below
// are never skipped by any of them: they stop a tick stacking on a scan, and no caller has more
// right to do that than the timer does.
//
//   (nothing)  the 5s timer — both gates apply
//   'force'    switching the timer on, or launching with it on: catch up NOW, skip both
//   'return'   the window was given back — skip the ACTIVITY gate, keep the throttle
//
// 'RETURN' EXISTS BECAUSE THE APP SAT STALE EXACTLY WHEN IT WAS BEING WATCHED, which is the one
// moment it must not (reported by the author 2026-08-23, and the frustration was months old). The idle
// gate fires only after IDLE_MS of TOTAL inactivity, and returning to a window always produces a
// mousemove, which stamps `_lastActivity`. So the tick ran, hit the gate, and returned — and kept
// doing so for as long as the mouse kept moving. You had to sit perfectly still for 30 seconds to
// see files that had landed while you were away.
//
// The old comment on the visibilitychange handler said it deliberately does NOT stamp
// `_lastActivity`, "that would delay the refresh at the very moment it's wanted". The intent was
// right and the code could not carry it: the browser stamps it for you, through the pointer.
// **An input gate keyed on "the user isn't touching anything" cannot be read at the instant the
// user reaches for the window.**
//
// The THROTTLE is deliberately kept on return, unlike 'force'. checkChanges() is one stat per
// folder — a real round trip on a network share — and flicking between two windows would otherwise
// pay it every flick. probeChanges() already made exactly this trade.
async function autoRefreshTick(mode) {
  const forced = mode === 'force';
  const returning = mode === 'return';
  // NOTE: a hidden tab is deliberately NOT a pause condition — it's the STRONGEST form of "not
  // interacting with the viewer" and it's the core use case (generating in ComfyUI while this tab
  // sits in the background), so refreshing then is the whole point. Browsers throttle timers in
  // background tabs to ~1/min, so the effective cadence there is slower than AUTO_CHECK_MS.
  if (!_autoOn) return traceGate('off', 'the auto-refresh clock is switched off');
  if (anyModalOpen()) return traceGate('modal', 'a modal is open (Settings, Rename, Setup or Tags)');
  // Nothing to refresh before a library exists -- and the switch can arrive ON without one, since
  // it is restored from localStorage during script evaluation, before boot() has loaded anything.
  // Guarded HERE rather than by forcing the switch off: the saved preference is the user's, and a
  // first run must not silently spend it.
  if (!(state.roots || []).length) return traceGate('noroots', 'no library yet');
  if (Job.busy || _refreshBusy || _autoScanning) {         // a scan / manual R is already in flight
    return Job.busy      ? traceGate('job',     'a job is already running')
         : _refreshBusy  ? traceGate('manual',  'a manual refresh is already running')
                         : traceGate('overlap', 'the previous auto-refresh has not finished');
  }
  if (!forced) {
    // Skipped on return: see above — the act of coming back is what stamps this.
    if (!returning && Date.now() - _lastActivity < IDLE_MS) {          // engaged → stay paused
      return traceGate('active',
        `you were active ${Math.round((Date.now() - _lastActivity) / 1000)}s ago `
        + `(needs ${IDLE_MS / 1000}s of quiet)`);
    }
    if (Date.now() - _lastAutoRefresh < IDLE_MS) {         // at most one fire per idle stretch
      return traceGate('throttled',
        `already refreshed ${Math.round((Date.now() - _lastAutoRefresh) / 1000)}s ago `
        + `(one per ${IDLE_MS / 1000}s)`);
    }
  }
  traceGate(null);   // closes any run of skips above, and says this one is going ahead
  const done = Trace.start('refresh',
    `checking for new files (${forced ? 'switched on' : returning ? 'came back to the window' : 'idle timer'})`);
  _lastAutoRefresh = Date.now();
  _lastChangeProbe = Date.now();   // shares the probe's clock so the two paths can't double up
  _autoScanning = true;
  // The clock is the ONE indicator for this whole path, and the tick owns it end to end — including
  // the /api/changes check, which on a network share is a real round trip that used to show nothing.
  // watchScan(quiet) used to add and remove it per library, so catching up several libraries made
  // the spin stutter between them; one owner, one spin.
  const clock = $('#autoRefresh');
  if (clock) clock.classList.add('busy');
  // The clock says the app is working; this says which controls that costs you. Raised and lowered
  // by the same owner, in the same try/finally, so it cannot be left on.
  setBusyActions('A background refresh is running — try again when it finishes');
  try {
    await checkChanges();                                  // cheap /api/changes (one stat per folder)
    const keys = Object.keys(state._changes || {}).filter(k => state._changes[k]);
    if (!keys.length) {
      // No new disk changes — but if we deferred some while scrolled down and we're back at the top,
      // fold them in now (no scan needed; they're already indexed).
      const canFold = _deferredNew && window.scrollY <= 4 && state.sort === 'date' && state.order === 'desc';
      if (canFold) await prependNewImages();
      // Says WHICH of the three "nothing happened" endings this was. They look identical from the
      // grid and mean completely different things: nothing on disk, versus files waiting that the
      // view cannot accept yet.
      done(!_deferredNew ? 'nothing new on disk'
         : canFold ? 'nothing new on disk · folded in the files held from earlier'
         : `nothing new on disk · ${window.scrollY > 4 ? 'holding new files until you scroll back to the top'
                                                       : 'holding new files until the sort is newest-first'}`);
      return;
    }
    done(`${keys.length} librar${keys.length === 1 ? 'y has' : 'ies have'} new files — rescanning`);
    for (const k of keys) { if (!(await rescanLib(k, true))) break; }   // one at a time, quietly
  } catch (e) {
    // NOT silent any more, to the trace. Still silent on screen — an auto-refresh nobody asked for
    // must not raise an error — but a failing refresh and a quiet one looked identical from here,
    // which is the single worst case for "it didn't update".
    Trace.add('refresh', `FAILED — ${e && e.message ? e.message : e}`);
  }
  finally {
    _autoScanning = false;
    if (clock) clock.classList.remove('busy');
    setBusyActions(null);
  }
}
// kick=true means "and act on it now". Turning the timer on — or launching with it on — IS the
// instruction to keep the library current, so it should not sit on a flagged change for 30 seconds.
function setAutoRefresh(on, kick) {
  // The first thing anyone needs to know when reading a trace about auto-refresh: was it even on?
  // It is restored from localStorage before boot, so a trace can otherwise open mid-story.
  Trace.add('refresh', `clock switched ${on ? 'ON' : 'OFF'}`
    + (on ? ` — checks every ${AUTO_CHECK_MS / 1000}s, acts after ${IDLE_MS / 1000}s of quiet` : ''));
  _autoOn = on;
  const b = $('#autoRefresh');
  if (b) {
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (!on) b.classList.remove('busy');
  }
  try { localStorage.setItem(AUTO_REFRESH_KEY, on ? '1' : ''); } catch (e) {}
  if (on) {
    _lastActivity = Date.now();     // spaces out the next AUTOMATIC fire; the kick below is explicit
    if (!_autoTimer) _autoTimer = setInterval(autoRefreshTick, AUTO_CHECK_MS);
    if (kick) autoRefreshTick('force');
  } else if (_autoTimer) {
    clearInterval(_autoTimer); _autoTimer = null;
  }
  // The pending mark is suppressed while the timer is on, so both edges have to repaint it — and via
  // renderLibList, not renderLibTrigger: the list calls the trigger, so this is the one call that
  // keeps the row marks and the strip mark saying the same thing. (renderLibTrigger alone cleared
  // the strip and left the rows still flagged.)
  renderLibList(window._facets && window._facets.roots);
}
if ($('#autoRefresh')) {
  $('#autoRefresh').addEventListener('click', () => setAutoRefresh(!_autoOn, true));
  // Restore only — no kick. This runs during script evaluation, before boot() has loaded a thing;
  // boot owns the launch catch-up, once there's a library to catch up on.
  setAutoRefresh(localStorage.getItem(AUTO_REFRESH_KEY) === '1');
}
// ---- roots (multiple, fully separate libraries) ----
async function loadRoots() {
  const cfg = await getJSON('/api/config');
  state.roots = cfg.roots || [];
  state.activeRoot = cfg.active || null;   // server's default management target (not user-facing)
  if (Array.isArray(cfg.labels)) { state.labels = cfg.labels; renderLabelList(); }
  if (Array.isArray(cfg.metric_range) && cfg.metric_range.length === 2) state.metricRange = cfg.metric_range;
  if (Array.isArray(cfg.extensions)) { state.extensions = cfg.extensions; applyExtensions(); }
  state.snapshots = Array.isArray(cfg.snapshots) ? cfg.snapshots : [];
  state.seenHelpHint = !!cfg.seen_help_hint;   // the one-time Help pop-up — see showHelpHint
  renderSnapshotMenu(); renderSnapshotRow();
  renderLibList(window._facets && window._facets.roots);   // facet counts fill in after loadFacets
  return cfg;
}
// Refresh everything after the active root changes (fresh, isolated library).
// (The whole-rail and full-screen blocks are tiers of Busy now — see the top of the file. The JOB
// tier is deliberately NOT dismissible: the way out is the Stop button, which stays usable because
// #status is z-55 and the scrim is z-50, see .task-scrim in style.css.)
async function afterRootChange(scanning, label) {
  await Busy.during('rail', async () => {
    clearSelection();
    await loadRoots();
    const saved = loadSavedFilters(state.activeRoot);
    restoreRootsSel(saved);    // the library SCOPE — before applyFilters, which never touches it
    applyFilters(saved);       // restore the saved filters (or defaults)
    restoreLoadedSnapshot();   // the live filters came back; the row must agree with them
    await refreshView();
  });
  if (scanning) {
    await watchScan(label || 'Indexing');
    await afterScanRefresh(false);
    // BEFORE showHelpHint, and the order is load-bearing: answering "Remove library" can leave no
    // libraries at all, and the help pop-up must not land on top of the welcome screen. That is the
    // same rule the cancelled-first-index path already follows -- see showHelpHint's own note.
    await askAboutEmptyIndex();
    showHelpHint();      // AFTER the scan: its scrim blocks the whole app, so anything shown
  }                      // during it is unreachable and would be dismissed blind
  else checkChanges();   // on root open: flag the ↻ icon if the folder changed on disk
}
// A FOLDER WITH NOTHING THE APP CAN READ, answered where it happens. Point the app at Documents, or
// at a folder of PSDs, and the index finishes having found nothing: the library is real, the grid is
// empty, and every explanation arrives too late to be connected to the folder you just chose. The author's
// idea, 2026-09-14 -- ask at the one moment the choice is still in mind. It is also why the empty
// grid's own message can stay short instead of trying to teach.
//
// `indexed` comes from the server (_root_has_images) rather than from the grid's total or the facet
// counts, both of which answer a FILTERED question -- a library restores its saved filters when it
// opens, so an empty grid is not evidence the library is empty.
//
// Fresh roots, because ours were loaded before the scan ran, when the library was empty by
// definition. Silent on failure: this is a courtesy question, and an app that refuses to finish
// adding a library because a status request failed would be worse than one that never asked.
async function askAboutEmptyIndex() {
  const key = state.activeRoot;
  if (!key) return;                       // cancelled first index -- the library is already gone
  let root;
  try {
    const cfg = await loadRoots();
    root = (cfg.roots || []).find(r => r.key === key);
  } catch (e) { return; }
  if (!root || root.indexed !== false) return;
  const keep = await uiConfirm(
    'It holds no images, video or audio the app can read, so the library will be empty. ' +
    'Keeping it is fine — anything you put in the folder later gets picked up. ' +
    'Removing it leaves the folder itself untouched.',
    { title: 'Nothing to index in that folder', ok: 'Keep it', cancel: 'Remove library' });
  if (keep) return;
  // delete_index too, matching the cancelled-first-index removal: partial rows outliving the
  // library are what make a later re-add find images already there and skip its scan.
  try {
    const j = await postJSON('/api/roots/remove', { key, delete_index: true });
    if (j.error) { uiAlert(j.error); return; }
    state.activeRoot = j.active;
  } catch (e) { return; }
  // The full reopen, not just a repaint: removing a library changes the scope, the filters and the
  // facets, and with the last one gone it is the welcome screen that has to appear.
  await afterRootChange(false);
}
// Shown once, when a first library has finished indexing. It does not teach -- it points at the ?,
// which is where the getting-started content lives and can be re-read by whoever dismisses this.
// The glyph is LIFTED from the live button, the same construction the welcome uses: a copied SVG
// would go on describing a mark the sidebar no longer wears.
// THE SEEN FLAG IS THE INSTALL'S, NOT THE BROWSER'S. It was localStorage, which is keyed to the
// address the app is served at -- so every copy of the folder shares one flag on localhost:8770,
// and a fresh copy came up already marked as seen. Nobody could ever see this a second time,
// including the person testing it. It lives in config.json now, where "once" means once per
// install. The server decides what absent means: absent with libraries already added is SEEN, so
// an existing user is never told where to start.
//
// Not shown when there is no library, which is the CANCELLED FIRST INDEX: cancelling removes the
// library and paints the welcome, and a pop-up landing on top of that would spend the one showing
// this ever gets on a library that no longer exists. The cancel path reloads the roots before we
// run, so an empty list here is exactly that case.
function showHelpHint() {
  if (state.seenHelpHint) return;
  if (!(state.roots || []).length) return;
  const ico = $('#btnHelp svg');
  const glyph = ico ? '<span class="inline-ico" aria-hidden="true">' + ico.outerHTML + '</span>' : '?';
  $('#helpHintMsg').innerHTML =
    'To get the most out of VV Curator, open the Help docs from the ' + glyph +
    ' icon at the top of the sidebar.';
  $('#helpHint').classList.remove('hidden');
  // Remembered locally first so a failed write cannot show it twice in one session, then persisted
  // fire-and-forget: failing to remember a HINT must never fail the app, and the cost of a lost
  // write is one extra pop-up next time.
  state.seenHelpHint = true;
  postJSON('/api/settings', { seen_help_hint: true }).catch(() => {});
}
// The tick-box rides this pop-up rather than earning its own, so its answer is saved when the
// pop-up goes -- by EITHER button, and by Escape and the backdrop, which is why it lives here and
// not on a click handler. Someone who dismisses without reading has answered "yes" by leaving the
// default alone, which is the same thing the sentence beside it described.
// Written even when it matches the default: the point of asking is that a stored answer exists,
// and "absent" is how the app recognises someone who was never asked.
function closeHelpHint() {
  const box = $('#helpHintUpdates');
  if (box) {
    state.updateCheck = box.checked;
    postJSON('/api/settings', { general: { update_check: box.checked } }).catch(() => {});
    // Asked and answered yes, on the one launch where the answer arrives AFTER boot already
    // skipped the check. Without this the first version notice would wait for a restart.
    if (box.checked) checkForUpdate();
  }
  $('#helpHint').classList.add('hidden');
}
// ---- "there is a newer version" ---------------------------------------------------------------
// ONE AMBER ARROW BESIDE THE LOGO, and a pop-up behind it holding the version, the notes and the
// link. It checks and it tells; it never downloads and never installs. An app that overwrites
// itself while running is a class of problem worth not having, and an update here is a folder copy.
//
// EVERY FAILURE IS SILENT, and that is the design rather than laziness: the user did not ask a
// question. Offline, no releases yet, GitHub having a bad day -- all of them mean no icon this
// launch, which is also what "no update" looks like. The one thing that must never happen is an
// error about a request nobody made.
let _update = null;
async function checkForUpdate() {
  if (!state.updateCheck) return;
  let j;
  try { j = await getJSON('/api/update'); } catch (e) { return; }
  if (!j || !j.available) return;
  _update = j;
  const btn = $('#btnUpdate');
  if (!btn) return;
  btn.classList.remove('hidden');
  // The version goes in the tooltip as well as the pop-up: hovering is cheaper than clicking, and
  // "Version 1.4 is available" answers the whole question for anyone who only wanted the number.
  const label = `Version ${j.version} is available`;
  btn.title = label;
  btn.setAttribute('aria-label', label);
  Trace.add('update', `${j.version} available`);
}
function openUpdateBox() {
  if (!_update) return;
  const j = _update;
  // The release's TITLE when it has one, the bare version when it doesn't -- a release with no
  // name is an ordinary thing on GitHub, and "Version 1.4 — " is not a sentence.
  $('#updateTitle').textContent = j.name || `Version ${j.version} is available`;
  $('#updateSub').textContent = `You have ${_appVersion || 'an earlier version'}. `
    + `Version ${j.version} is published on GitHub.`;
  const notes = $('#updateNotes');
  // mdToHtml escapes before it builds any HTML and only emits http(s) links, which is what makes
  // it safe for the one string in this app that comes off the internet.
  if (j.notes) { notes.innerHTML = mdToHtml(j.notes); notes.classList.remove('hidden'); }
  else { notes.innerHTML = ''; notes.classList.add('hidden'); }
  $('#updateBox').classList.remove('hidden');
}
function closeUpdateBox() { $('#updateBox').classList.add('hidden'); }
function _modelsDirCheckMsg(c) {
  if (!c || !c.exists) return '⚠ path not found from this machine';
  const subs = [c.checkpoints ? 'checkpoints/' : 'no checkpoints/', c.loras ? 'loras/' : 'no loras/'];
  return '✓ found — ' + subs.join(', ');
}
// Per-library ComfyUI models-folder override (for Export-for-Civitai resource hashing). Falls back
// to the global default (Settings → General) when left blank.
// ---- the recycle folder filling up ------------------------------------------------------------
// Files recycled on a network share go to a folder nothing ever empties, so it grows forever and
// silently. The author asked to be nudged as it builds: "Time to recycle? You have 1,202 files."
//
// A TOAST, NOT A DIALOG, and that was his call once the trade-off was put to him: a modal that
// interrupts you about housekeeping will interrupt again at the next threshold whether or not you
// decided to act. This says its piece and leaves.
//
// THE MARK IS PER BROWSER, not server state. All the server does is count; deciding whether a count
// is worth mentioning is a UI question, and localStorage means no config write, no migration, and
// nothing to clean up if the feature goes.
const RecycleDebt = (() => {
  let timer = null;
  const mark = key => 'vv_recycle_mark_' + key;

  async function check() {
    let d;
    try { d = await (await fetch('/api/recycle-status')).json(); } catch (e) { return; }
    if (!d.enabled) return;          // switched off in Settings; marks are left alone, not cleared,
                                     // so turning it back on doesn't replay every threshold passed
    for (const r of d.roots || []) {
      // Whichever threshold it has crossed more of. A library of video hits the size one first; a
      // library of stills hits the count one. Step 0 means it has crossed neither.
      const step = Math.max(Math.floor(r.files / d.warn_files),
                            Math.floor(r.bytes / d.warn_bytes));
      const seen = Number(localStorage.getItem(mark(r.key)) || 0);
      if (step <= seen) {
        // Emptied (or partly) -- re-arm at whatever level it is at now, so the next nudge comes at
        // the next threshold rather than never. A root that is OFFLINE is absent from the list
        // entirely, so it never reaches this line and never loses its mark.
        if (step < seen) localStorage.setItem(mark(r.key), String(step));
        continue;
      }
      localStorage.setItem(mark(r.key), String(step));
      toast(`${r.name}: ${r.files.toLocaleString()} files in the recycle folder · ${fmtBytes(r.bytes)}`);
      break;                    // one library at a time; the rest keep their marks for next time
    }
  }

  return {
    // AFTER the undo window, not on the click. toast() takes the pill, and the pill is showing an
    // Undo offer for the next 5 seconds -- toasting now would cancel the user's own undo.
    soon() { clearTimeout(timer); timer = setTimeout(check, 7000); },
    check,
  };
})();

// Opens the folder a recycle lands in when the OS bin isn't available -- which on a network share
// is every recycle. It only OPENS it: emptying is permanent there, and that is a file manager's job.
async function openRecycleFolder(key) {
  const r = await postJSON('/api/recycle-folder', { key });
  if (r && r.ok) return;
  // Missing is the ordinary answer for a local library, where recycling goes to the OS bin and this
  // folder is never created -- so it is a plain statement, not a failure.
  toast(r && r.reason === 'none'
    ? 'Nothing has been recycled from this library to a folder.'
    : 'Could not open the recycle folder.');
}
async function setLibModelsDir(key) {
  const r = (state.roots || []).find(x => x.key === key);
  if (!r) return;
  const val = await uiPrompt(
    `ComfyUI models folder for “${r.name}”\n(the folder holding checkpoints/ and loras/ — used by ` +
    `Export for Civitai to hash resources. Leave blank to use the global default.)`, r.models_dir || '');
  if (val == null) return;                       // cancelled
  const j = await postJSON('/api/roots/models_dir', { key, models_dir: val.trim() });
  if (j && j.error) { uiAlert(j.error); return; }
  await loadRoots();
  toast(val.trim() ? ('Models folder set · ' + _modelsDirCheckMsg(j && j.check)) : 'Models folder cleared (using global default)');
}
async function renameLib(key) {
  const r = (state.roots || []).find(x => x.key === key);
  if (!r) return;
  const name = await uiPrompt('Rename this library:', r.name);
  if (!name || !name.trim()) return;
  await postJSON('/api/roots/rename', { key, name: name.trim() });
  await loadRoots(); await loadFacets();
}
async function removeLib(key) {
  const r = (state.roots || []).find(x => x.key === key);
  if (!r) return;
  if (!(await uiConfirm(`Remove library "${r.name}" from the list?\n\nThe image files on disk are NOT touched.`, { ok: 'Remove', danger: true }))) return;
  const del = await uiConfirm('Also delete its saved index (tags, favorites, scores for THIS library)?\n\n' +
                             'OK = delete the index · Cancel = keep it (re-adding this folder later restores it).', { ok: 'Delete index', danger: true });
  const j = await postJSON('/api/roots/remove', { key, delete_index: del });
  if (j.error) { uiAlert(j.error); return; }
  state.activeRoot = j.active;
  await afterRootChange(false);
}
// THE OTHER EMPTY GRID: nothing matched, rather than nothing is indexed. Same component as the
// welcome below -- `.empty-hint` carries the measure, the centring and the `.panel` ground, and the
// `:has()` rule in the stylesheet is keyed on that class, so a box that dropped it would render
// 194px wide inside a card track (see style.css, above `.grid:has(> .empty-hint)`).
//
// TWO MESSAGES, because there are two reasons a grid can be empty and only one of them is filters.
// filtersActive() deliberately does NOT count the library selection (its own comment explains why)
// -- and that selection is exactly what can empty a grid with every filter off: libraries in view
// that hold nothing, or one that indexed nothing. Saying "no cards match these filters" there would
// be a plain lie, and the second line would send the reader to press a Reset that changes nothing.
//
// So the second line is on the FILTERED branch only. With no filters set there is nothing to change
// and Reset is already disabled (anythingToReset), so the instruction would name two dead controls.
// It is deliberately NOT `.empty-formats`: that class draws a border-top, which means "a separate
// aside", and this line is the instruction belonging to the line directly above it.
//
// The author's wording, both lines, 2026-09-14.
function showNoMatchHint() {
  // NO LIBRARIES SELECTED BEATS ANY FILTER, and it has to: nothing can match, whatever the filters
  // say, and Reset all deliberately leaves the library selection alone — so the filtered line would
  // send someone to a control that cannot fix what they are looking at. It is a state of its own
  // rather than a shade of "nothing in the selected libraries", which reads oddly when there are
  // none selected to have nothing in. Its words are the scope button's, so the grid and the control
  // it points at say the same thing — see renderLibTrigger.
  const noneSelected = (state.roots || []).length > 0 && selectedRootKeys().length === 0;
  // OFFLINE BEATS THE FILTERS TOO, and for the same reason "no libraries selected" does: no filter
  // change can bring these cards back, so sending someone to Reset all would be sending them to a
  // control that cannot fix what they are looking at. Their ticks are untouched — the library is
  // out of scope because it is not here, not because they switched it off — so the words have to
  // name the drive rather than the selection.
  const offSel = selectedRootKeys().filter(isRootOffline);
  const allOff = !noneSelected && offSel.length > 0 && offSel.length === selectedRootKeys().length;
  const filtered = filtersActive() && !noneSelected && !allOff;
  const title = noneSelected ? 'No libraries selected.'
              : allOff      ? (offSel.length === 1 ? `${libName(offSel[0])} is offline.`
                                                   : 'The selected libraries are offline.')
              : filtered    ? 'No cards match these filters.'
                            : 'Nothing in the selected libraries.';
  // Split across the two lines rather than crammed into one, which is also what keeps it from
  // reading "No libraries selected — select one…" in a single breath.
  const line = noneSelected ? 'Select one to see its images.'
             : allOff      ? (offSel.length === 1 ? 'Reconnect it and the cards come back on their own.'
                                                  : 'Reconnect them and the cards come back on their own.')
             : filtered    ? 'Change selected filters, or use Reset all to start over.'
                           : '';
  $('#grid').innerHTML =
    '<div class="empty-hint panel">' +
      '<div class="empty-title">' + title + '</div>' + line +
    '</div>';
}
// Every path that puts cards INTO the grid has to take the panel out first, because a panel saying
// there are no cards is simply false the moment there is one. Removing it also puts the grid back
// into grid flow on its own: the layout switch is `.grid:has(> .empty-hint)`, not a class anyone
// has to remember to strip -- the whole reason that rule is written with `:has()`.
function clearEmptyHint() {
  const el = $('#grid > .empty-hint');
  if (el) el.remove();
}
// Shown when no library is configured — a hint, not a forced popup.
//
// THE AUTHOR'S WORDING, from his own clean-copy run on 2026-09-07, with one change: he wrote "a root
// folder" and `root` is a word DESIGN.md's glossary forbids showing a user (it is the internal
// name for the same thing the UI calls a Library). The old text was two sentences using "folder"
// and "library" for one act and defining neither, which is exactly what he reported.
//
// THE ICON IS DRAWN, NOT DESCRIBED. Naming a control by shape ("the double-folder icon") only
// works if the reader can match it, so the same glyph the #libScope button wears is rendered
// inline mid-sentence. This overrides the note that used to sit here arguing against a leading
// mark -- that was about decoration in front of a link, not about identifying a control, and the
// link it defended is gone.
function showNoRootHint() {
  // LIFTED FROM THE LIVE BUTTON rather than written out again. A second copy of the glyph is a
  // second thing to update, and the failure mode is silent: the sentence would go on pointing at
  // a shape the strip no longer wears. This strip has changed its marks four times in a fortnight
  // (see the block above #libRefreshAll in index.html), so that is not hypothetical.
  const ico = $('#libScope svg');
  const glyph = ico ? '<span class="inline-ico" aria-hidden="true">' + ico.outerHTML + '</span>' : '';
  $('#grid').innerHTML =
    '<div class="empty-hint panel">' +
      '<div class="empty-title">Welcome to VV Curator</div>' +
      'To get started, add your first library, which is a top folder on a local or ' +
      'remote hard drive.<br>' +
      'Click the ' + glyph + ' icon under the VV Curator logo.' +
      // WHAT IT READS, in one line, pointing AWAY from here. The author asked for the full list and then
      // corrected the placement -- reference material does not belong on a screen someone sees once
      // and never again, so docs/help.md's "What it reads" carries the list and this says only
      // enough to correct the assumption that this is a PNG viewer. His wording.
      // A list here would also be a SECOND list: test_first_run pins Help's against the scanner's
      // own extensions, and a copy on this screen would have nothing keeping it honest.
      '<div class="empty-formats">Supports still, video and audio formats. ' +
      'See Help for details.</div>' +
    '</div>';
}
// ONE MESSAGE, WHETHER THIS IS YOUR FIRST LIBRARY OR YOUR SIXTH. The author, 2026-09-07: "let's keep the
// exact same wording when adding n+1 libraries. No need to customize it." He is right that the
// customisation bought nothing -- the instruction is the same instruction, and the word "another"
// only told you something you already knew.
//
// It also retires a whole class of bug. The two strings used to be chosen by WHICH CONTROL was
// clicked, so the Libraries-strip route greeted a user with no libraries at all with "another
// output folder... as a separate library"; that was fixed a few hours ago by asking the state
// instead. With one string there is no question to answer and nothing left to get wrong.
//
// NO ComfyUI IN IT. It said "your ComfyUI output folder", from when that was the only thing this
// indexed -- "that's very outdated". A library is any folder of media now.
//
// IT SAYS SUBFOLDERS ARE INCLUDED, because the walker takes the WHOLE tree below the path with no
// depth limit (index_db.walk_media -- it prunes only _ToRecycle and refuses to follow junctions),
// and a user typing a drive-level path deserves to know that before they press the button rather
// than after 100k files.
function openAddRoot() {
  const inp = $('#setupPath');
  inp.value = '';
  $('#setupError').textContent = '';
  $('#setupName').value = '';                    // an override, and blank is the normal case
  $('#setupMsg').textContent =
    'Enter the full path to a folder containing image, video or audio files. ' +
    'All files and subfolders are indexed.';
  // A network path is the thing people do not know is allowed, and it is where the author's own
  // libraries live. One clause, in the box, rather than a sentence nobody sees (see index.html).
  $('#setupHint').textContent = 'A network path like \\\\server\\share works too.';
  syncSetupGo();                                 // the field is empty, so Start indexing starts off
  $('#setup').classList.remove('hidden');
  setTimeout(() => { inp.focus(); inp.select(); }, 40);
}
// START INDEXING IS DEAD UNTIL THERE IS A PATH TO INDEX. It used to be pressable with an empty
// field and answer "Please enter a folder path." -- an error for a state the button could simply
// have declined to be in. The author asked for the gate, 2026-09-07.
// DISABLED, not hidden: this one is IDLE (it has a job, it just cannot do it yet), which is the
// other half of the rule that hides the strip's refresh controls. See DESIGN.md.
function syncSetupGo() {
  const go = $('#setupGo');
  if (go) go.disabled = !$('#setupPath').value.trim();
}
function closeSetup() { $('#setup').classList.add('hidden'); }
// Open the OS folder chooser (runs on the server/localhost) and drop the path into the field.
async function pickFolder() {
  const btn = $('#setupBrowse'); const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Choose in dialog…';
  $('#setupError').textContent = '';
  try {
    const j = await postJSON('/api/pick-folder', {});
    if (j.error) $('#setupError').textContent = j.error;
    else if (j.path) $('#setupPath').value = j.path;   // empty = user cancelled: leave field as-is
  } catch (e) {
    $('#setupError').textContent = 'Could not open the folder picker.';
  }
  btn.disabled = false; btn.textContent = label;
  // The picker fills the field without an input event, so the gate has to be told. This is the
  // path a user is MOST likely to take and the one a keystroke-only listener would have missed.
  syncSetupGo();
  $('#setupPath').focus();
}
// THE CHECK CAN TAKE SECONDS, so the dialog has to look like it is working. A path on a machine
// that is off holds the answer for as long as the server's probe allows (DIR_PROBE_SECS), and
// through all of it Start indexing stayed live and pressable under the word "Checking…" — so a
// second press queued a second add of the same folder. The buttons stand down for the duration and
// come back with the error, which is the same shape as pickFolder() above.
async function submitSetup() {
  const path = $('#setupPath').value.trim();
  if (!path) { $('#setupError').textContent = 'Please enter a folder path.'; return; }
  $('#setupError').textContent = 'Checking the folder…';
  const go = $('#setupGo'), browse = $('#setupBrowse');
  const goWas = go.disabled, browseWas = browse ? browse.disabled : false;
  go.disabled = true; if (browse) browse.disabled = true;
  // The server has always taken a `name` and fallen back to the folder's own (_default_name);
  // nothing had ever sent one. Blank stays blank rather than becoming '' explicitly, so the
  // fallback keeps firing exactly as it does for every library added before this field existed.
  const name = $('#setupName').value.trim();
  let j;
  try {
    j = await postJSON('/api/roots/add', name ? { path, name } : { path });
  } catch (e) {
    j = { error: 'The app stopped answering. Try again.' };
  } finally {
    // RESTORED ON EVERY PATH, including the successful one. The dialog is reused: openAddRoot()
    // re-derives Start indexing's state from the field, but nothing re-enables Browse, so a
    // disable left behind here would come back dead the next time the dialog opened.
    go.disabled = goWas;
    if (browse) browse.disabled = browseWas;
  }
  if (j.error) { $('#setupError').textContent = j.error; return; }
  closeSetup();
  state.activeRoot = j.active;
  await afterRootChange(j.scanning);
}

// ---- events ----
$('#q').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); addTerm($('#q').value); }
  else if (e.key === 'Backspace' && !$('#q').value && state.terms.length) { removeTerm(state.terms.length - 1); }
});
$('#q').addEventListener('input', debounce(() => { renderChips(); search(true); }, 250));
$('#chips').addEventListener('click', e => {
  const b = e.target.closest('button[data-i]');
  if (b) removeTerm(Number(b.dataset.i));
});
// Committing by click has to do exactly what Enter does, so it calls the same function.
$('#qEnter').addEventListener('mousedown', e => e.preventDefault());   // keep the caret in the field
$('#qEnter').addEventListener('click', () => { addTerm($('#q').value); $('#q').focus(); });
$('#xqEnter').addEventListener('mousedown', e => e.preventDefault());
$('#xqEnter').addEventListener('click', () => { addXTerm($('#xq').value); $('#xq').focus(); });
// The clamp is a function of the field's width, and the sidebar is draggable — so it is recomputed
// when the box changes size, not only when the text does. Without this the mark keeps a position
// measured at the old width and drifts under the ✕ as the rail narrows.
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => syncEnterMarks());
  for (const el of [$('#searchbox'), $('#xsearchbox')]) if (el) ro.observe(el);
}
$('#qclear').addEventListener('click', clearTerms);
$('#xq').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); addXTerm($('#xq').value); }
  else if (e.key === 'Backspace' && !$('#xq').value && state.xterms.length) { removeXTerm(state.xterms.length - 1); }
});
$('#xq').addEventListener('input', debounce(() => { renderXChips(); search(true); }, 250));
$('#xchips').addEventListener('click', e => {
  const b = e.target.closest('button[data-i]');
  if (b) removeXTerm(Number(b.dataset.i));
});
$('#xqclear').addEventListener('click', clearXTerms);
$('#resetFilters').addEventListener('click', resetFilters);
// Searchable Model/Folder facet dropdowns: trigger toggles the panel; search filters; row picks.
document.querySelectorAll('.facet-dd').forEach(dd => {
  const facet = dd.dataset.facet;
  dd.querySelector('.facet-trigger').addEventListener('click', e => {
    if (e.target.closest('.facet-sortbtn')) return;   // the ⋯ opens its own sort menu, not the list
    e.stopPropagation();
    const open = !dd.querySelector('.facet-panel').classList.contains('hidden');
    open ? closeFacetDDs() : openFacetDD(facet);
  });
  dd.querySelector('.facet-search').addEventListener('input', () => renderFacetDD(facet));
  dd.querySelector('.facet-list').addEventListener('click', e => {
    const row = e.target.closest('.facet-row'); if (row) pickFacet(facet, row.dataset.val);
  });
  // ⋯ (on the trigger, left of the arrow) opens a Sort by menu; picking re-sorts and is remembered.
  dd.querySelector('.facet-sortbtn').addEventListener('click', e => {
    e.stopPropagation();
    const menu = dd.querySelector('.facet-sortmenu');
    const wasHidden = menu.classList.contains('hidden');
    closeMenus();
    if (wasHidden) { renderFacetDD(facet); menu.classList.remove('hidden'); }
  });
  dd.querySelector('.facet-sortmenu').addEventListener('click', e => {
    const b = e.target.closest('button[data-sort]'); if (!b) return;
    e.stopPropagation();
    _facetSort[facet] = b.dataset.sort; saveFacetSort();
    closeFacetDDs();
    renderFacetDD(facet);
  });
  dd.querySelector('.facet-panel').addEventListener('click', e => e.stopPropagation());   // clicks inside stay open
});
// Escape closes an open facet dropdown (capture phase, so it fires wherever focus is). Only acts
// when a panel is actually open, so it never swallows Escape from other contexts.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.querySelector('.facet-panel:not(.hidden), .facet-sortmenu:not(.hidden), #libMenu:not(.hidden), #folderMenu:not(.hidden), #appMenu:not(.hidden), #snapMenu:not(.hidden), #snapActions:not(.hidden)')) {
    e.stopPropagation(); closeMenus();
  }
}, true);
// File type is a segmented icon bar, not a <select>: one delegated click, and the selected
// segment is painted from state (same idiom as applyCardSize) so restore and click agree.
$('#mediaType').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  const v = b.dataset.type || '';
  // The segment you just pressed wins; the one it cannot coexist with goes back to All. See
  // segConflict — the pairs it catches are empty by construction, never merely narrow.
  if (segConflict('type', v)) applySetsMode('all');
  state.type = v;
  // BOTH bars repaint, because each one's conflict state is a function of the other. Painting only
  // the bar that was clicked would leave the other still dimming a segment that is now fine.
  renderMediaType(); renderSetsMode();
  search(true);
});
$('#hasNote').addEventListener('change', e => { state.hasNote = e.target.checked; search(true); });
$('#mfolder').addEventListener('change', e => { state.modelFolder = e.target.value; search(true); });
// filter tabs: switch panes + restore the last-used tab across sessions
document.querySelector('.filter-tabs').addEventListener('click', e => {
  const b = e.target.closest('.ftab'); if (b) setActiveTab(b.dataset.tab);
});
setActiveTab(localStorage.getItem(ACTIVE_TAB_KEY) || 'filters');
renderTabBadges();
$('#setsMode').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  if (segConflict('sets', b.dataset.mode)) state.type = '';
  applySetsMode(b.dataset.mode);
  renderMediaType(); renderSetsMode();
  search(true);
});
// Grid card size (view pref): S/M/L sets --card-min (the image box, a multiple of 16 so the 384px
// cached thumbnail scales on a clean ratio). Pure CSS reflow — no re-search. Persisted per-install.
const CARD_SIZE_KEY = 'cv:cardSize';
// HOW FAR A CARD MAY STRETCH, in CSS pixels: the point where it would need more pixels than the
// biggest cached thumbnail has. THUMB_SIZES tops out at 512, so past 512/dpr the picker has nothing
// larger to hand back and the card is simply upscaled — the one visible failure mode in the grid.
// Floored at the card's own size, because a display scaled past 2.7× is already being served an
// upscaled thumbnail at the base size and stretching costs it nothing further; there a card just
// does not stretch rather than the max going below the min, which is not a legal track.
// Re-read on zoom: Ctrl+/- changes devicePixelRatio, so a limit computed once drifts.
function setCardMax() {
  const cap = Math.max(_cardPx, Math.floor(THUMB_SIZES[THUMB_SIZES.length - 1] / (window.devicePixelRatio || 1)));
  document.documentElement.style.setProperty('--card-max', cap + 'px');
}
matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener('change', setCardMax);

// A STRETCHED CARD IS DRAWN WIDER THAN THE SIZE ITS THUMBNAIL WAS ASKED FOR, so the request has to
// follow the RENDERED width or stretching would reintroduce exactly the softness the cap exists to
// prevent. The cap guarantees the needed size is one the cache actually has, so this can only ever
// find a bigger thumbnail, never fail to.
//
// MEASURED, because the rendered width is a layout result — the tracks answer to the window, the
// sidebar's drag handle and the card size together, and no arithmetic here would stay true through
// all three. That makes it a CACHE, so it is re-read on every event that can invalidate it (below)
// rather than computed once.
//
// Only ever UPGRADES, the same rule applyCardSize follows and for the same reason: a card already
// on screen holding a larger thumbnail than it needs costs nothing, and re-fetching hundreds of
// smaller ones to reclaim bytes already spent is the opposite of the point.
let _cardDrawPx = 192;             // the width cards are ACTUALLY drawn at, once stretched
function syncThumbsToCardWidth() {
  const card = document.querySelector('#grid .card img');
  if (!card) return;
  // getBoundingClientRect forces layout, so this reads the width the tracks have JUST resolved to
  // rather than the one they had a moment ago. Called synchronously for that reason -- inside a
  // requestAnimationFrame it measured the PREVIOUS size and every card kept a thumbnail one step
  // too small, which is the exact softness the cap exists to prevent.
  const w = Math.round(card.getBoundingClientRect().width);
  if (!w) return;                                  // not laid out yet (hidden tab, mid-swap)
  _cardDrawPx = w;                                 // cards built from here on ask for this instead
  const want = thumbSizeFor(w);
  for (const im of document.querySelectorAll('#grid img')) {
    const cur = Number((im.getAttribute('src') || '').match(/[?&]s=(\d+)/)?.[1] || 0);
    if (cur && cur < want) im.setAttribute('src', thumbAt(im.getAttribute('src'), w));
  }
}
// The three things that change a card's rendered width: the window, the sidebar drag, and the card
// size control (which calls it directly). One observer on the grid catches the first two, since
// both resize it.
if (window.ResizeObserver) {
  let _t = null;
  const ro = new ResizeObserver(() => { clearTimeout(_t); _t = setTimeout(syncThumbsToCardWidth, 150); });
  const startGridObserver = () => { const g = $('#grid'); if (g) ro.observe(g); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startGridObserver);
  else startGridObserver();
}

function applyCardSize(px) {
  px = [128, 192, 256, 512].includes(px) ? px : 192;
  _cardPx = px;                          // cards built from here on ask for the matching thumbnail
  document.documentElement.style.setProperty('--card-min', px + 'px');
  setCardMax();
  // The facts band is Large and Extra-large only: three values want ~155px and a Small card's whole
  // content box is 128. Gated from HERE rather than by a container query on .card, because this
  // function is the one place that knows the size, the band's only size-dependent behaviour is
  // on/off, and `container-type` on .card would create a stacking context per card AND silently
  // wake a dormant @container rule that has .song-face as its subject. Still pure CSS reflow.
  $('#grid').classList.toggle('cards-lg', px >= 256);
  // And a second step at Extra-large, for the card marks only: --card-badge-h was flat at every
  // size, so a badge went from 12.5% of a Small card to 3.1% of this one. cards-lg cannot do both
  // jobs -- it also turns the second facts tier on, which XL does not want twice.
  $('#grid').classList.toggle('cards-xl', px >= 512);
  document.querySelectorAll('#cardSize button').forEach(b =>
    b.classList.toggle('active', Number(b.dataset.size) === px));
  try { localStorage.setItem(CARD_SIZE_KEY, String(px)); } catch (e) {}
  // Cards already on screen: only ever UPGRADE them. Growing the cards would otherwise leave the
  // pictures you can already see upscaled and soft, which is the one visible failure mode here.
  // Shrinking them deliberately changes nothing — the thumbnails in hand are merely larger than
  // they need to be, and re-fetching hundreds of smaller ones to reclaim bytes already spent is
  // the exact opposite of the point. So this costs a burst only when going up, never coming down.
  // Synchronous: the tracks have already been resized by the --card-min/--card-max writes above,
  // and the measurement inside forces the layout that proves it.
  syncThumbsToCardWidth();
}
document.querySelectorAll('#cardSize button').forEach(b =>
  b.addEventListener('click', () => applyCardSize(Number(b.dataset.size))));
applyCardSize(Number(localStorage.getItem(CARD_SIZE_KEY)) || 192);   // restore saved size on load
$('#dfrom').addEventListener('change', e => { state.dfrom = e.target.value; search(true); });
$('#dto').addEventListener('change', e => { state.dto = e.target.value; search(true); });
// one × per date row, each clearing only its own field
document.querySelectorAll('.date-range [data-clear]').forEach(b => b.addEventListener('click', () => {
  const k = b.dataset.clear;                    // 'dfrom' | 'dto'
  state[k] = ''; $('#' + k).value = '';
  search(true);
}));
for (const id of ['rmin', 'rmax']) {   // Quality filter: 1..10 dropdowns (parallel to the LLM score)
  $('#' + id).innerHTML = '<option value="">-</option>' + Array.from({ length: 10 }, (_, i) => `<option>${i + 1}</option>`).join('');
}
$('#rmin').addEventListener('change', e => { state.rmin = scaleToRaw(e.target.value, -0.05); search(true); });
$('#rmax').addEventListener('change', e => { state.rmax = scaleToRaw(e.target.value, +0.05); search(true); });
$('#sort').addEventListener('change', e => {
  const v = e.target.value;
  if (v === 'date-asc') { state.sort = 'date'; state.order = 'asc'; }
  else { state.sort = v; state.order = 'desc'; }
  // Arriving at Random deals a new hand, so leaving and coming back is never the same grid twice.
  if (v === 'random') reshuffle();
  search(true);
});
// One press does both jobs, and which one it did depends only on where you already were: from any
// other sort it SELECTS Random, and from Random it deals again. Same button, same press — the
// dropdown is kept in step by hand because setting .value fires no `change` event.
$('#reshuffle').addEventListener('click', () => {
  if (state.sort !== 'random') {
    state.sort = 'random'; state.order = 'desc';
    $('#sort').value = 'random';
  }
  reshuffle();
  search(true);
});
// header buttons: gear = Settings. The power button that sat beside it was removed 2026-09-05 —
// it stopped the server, it was one fat finger from Settings, and it asked you to manage a thing
// you never asked to run. Closing the window now stops the server by itself; see the check-in
// below and server.py's client watchdog.
$('#btnSettings').addEventListener('click', () => openSettings());   // no arg: reopen on the last tab
// dialog (confirm/alert/prompt) controls
$('#dlgOk').addEventListener('click', _dlgSubmit);
$('#dlgCancel').addEventListener('click', () => _dlgClose(null));
$('#dialog .overlay-bg').addEventListener('click', () => _dlgClose(null));
$('#dlgInput').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); _dlgSubmit(); } });
document.addEventListener('keydown', e => {   // capture: beat the detail-view Esc/arrow handler
  if ($('#dialog').classList.contains('hidden')) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); _dlgClose(null); }
  else if (e.key === 'Enter' && document.activeElement !== $('#dlgInput')) { e.preventDefault(); e.stopPropagation(); _dlgSubmit(); }
}, true);
// Libraries strip: the scope button opens the list; the other items are their own buttons and no
// longer sit INSIDE it, so the old "don't also open the panel" bail-out is gone with the field.
// e.stopPropagation() stays — that one guards against the DOCUMENT's closeMenus(), not the trigger.
$('#libScope').addEventListener('click', e => {
  e.stopPropagation();   // the document's click->closeMenus would re-hide what we just opened
  const wasOpen = !$('#libPanel').classList.contains('hidden');
  closeMenus();
  if (!wasOpen) {
    $('#libPanel').classList.remove('hidden');
    // Opening this list is the moment you ASK whether a library is back, so re-check on the way in.
    // Throttled and fire-and-forget via probeChanges: on a downed share the check is a wait for a
    // timeout, and this button can be clicked repeatedly.
    probeChanges();
  }
  e.currentTarget.setAttribute('aria-expanded', wasOpen ? 'false' : 'true');
});
$('#libPanel').addEventListener('click', e => e.stopPropagation());   // clicks inside keep it open

// hand over: refreshChanged() returns once the scans it queued have STARTED, and from there the
// JOB-tier scrim + status bar own the screen. try/finally so an early return can't leave it spinning.
$('#libRefreshAll').addEventListener('click', async e => {
  e.stopPropagation();
  const b = e.currentTarget;
  b.classList.add('busy');
  try { await refreshChanged(); }          // same path as the R key: check all, rescan the changed
  finally { b.classList.remove('busy'); }
});
$('#libAdd').addEventListener('click', () => openAddRoot());
$('#libRescanAll').addEventListener('click', () => { closeMenus(); rescanAllLibs(); });
$('#libList').addEventListener('change', e => {
  const cb = e.target.closest('input[data-root]'); if (!cb) return;
  const all = allRootKeys();
  // Starting from `all` only when nothing has ever been chosen. It used to start there whenever the
  // selection was EMPTY too, which is what made unticking the last box put every library back: the
  // set was rebuilt from all, the one being unticked was removed, and you landed on "everything but
  // this one" instead of "none". Unticking the last box is now allowed to mean what it looks like.
  const sel = new Set(state.rootsSel === null ? all : state.rootsSel);
  if (cb.checked) sel.add(cb.dataset.root); else sel.delete(cb.dataset.root);
  state.rootsSel = [...sel];
  // TICKING AN OFFLINE ONE HAS TO SAY SOMETHING. The tick lands, the row shows it ticked, and the
  // grid does not change -- because an offline library is out of scope whatever the box says. The author,
  // 2026-09-14, on finding it: "a user can check an offline library, and there really is no
  // message." The toast is the right size for it: the tick is not refused and nothing is undone, so
  // this is a fact rather than an error, and the same fact the row's own mark already carries.
  if (cb.checked && isRootOffline(cb.dataset.root)) toast(`${libName(cb.dataset.root)} is offline`);
  renderLibTrigger();   // the scope label is on the rail now, so it can't wait for the next facet load
  search(true); loadTags();
});
$('#libList').addEventListener('click', e => {
  const more = e.target.closest('[data-more]');
  if (more) { e.stopPropagation(); openLibMenu(more.dataset.more, more); return; }
  const ref = e.target.closest('[data-ref]');
  // QUIET-tier feedback: spin the ↻ straight away. There's a POST round trip plus the first status
  // poll before the JOB scrim appears, and without this the click looks like it did nothing. The
  // class needs no explicit clearing — the row is re-rendered by renderLibList() when the scan ends.
  if (ref) { e.preventDefault(); ref.classList.add('busy');
    rescanLib(ref.dataset.ref).then(s => { if (s) afterScanRefresh(false); }); return; }
  // The catch-up mark is a SHORTCUT TO THE MENU'S RESCAN, not a second way of doing it: same
  // function, same confirm, same price. A mark that acted on its own would be a third scan verb
  // hiding behind an icon, which is the thing the one-verb decision exists to prevent.
  const cat = e.target.closest('[data-catchup]');
  if (cat) { e.preventDefault(); cat.classList.add('busy');
    rescanLibFromMenu(cat.dataset.catchup).then(s => { if (s) afterScanRefresh(false); })
      .finally(() => cat.classList.remove('busy')); return; }
});
// One guard for the whole menu, in the CAPTURE phase so it lands before the dispatcher below —
// the same shape #selbar uses, and for the same reason: a check inside each action is a place to
// forget. aria-disabled rather than the attribute, because a disabled button does not reliably
// show its title, and here the title is the payload: it says WHY.
$('#libMenu').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b && b.getAttribute('aria-disabled') === 'true') { e.preventDefault(); e.stopPropagation(); }
}, true);
$('#libMenu').addEventListener('click', e => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  const key = _libMenuKey; $('#libMenu').classList.add('hidden');
  // EVERY ITEM HERE ACTS ON THE LIBRARY WHOSE MENU WAS OPENED. `rebuildall` used to be the one that
  // did not, ignoring the key it was handed; it moved to the Libraries menu on 2026-09-12, which is
  // the menu about all of them. `rebuild` went in the same pass — see index.html.
  ({ rescan: rescanLibFromMenu, mine: mineTags,
     cleartags: clearTagsLib, clearscores: clearScoresLib,
     modelsdir: setLibModelsDir, recyclefolder: openRecycleFolder,
     rename: renameLib, delete: removeLib }[b.dataset.act] || (() => {}))(key);
});
// Snapshots. Two menus off one trigger — the picker, and the ⋯ actions for the loaded snapshot —
// plus two buttons beside it. Each toggle runs closeMenus() first, so opening one closes the rest.
$('#snapTrigger').addEventListener('click', e => {
  if (e.target.closest('#snapUpdate')) return;   // its own handler
  e.stopPropagation();   // document's click->closeMenus would otherwise re-hide what we just opened
  const wasOpen = !$('#snapMenu').classList.contains('hidden');
  closeMenus();
  if (!wasOpen) { renderSnapshotMenu(); $('#snapMenu').classList.remove('hidden'); }
});
// The row's ⋯ opens the shared actions menu beside it, keyed to that row — exactly how a library
// row drives #libMenu. One menu repositioned per row, never one per row in the DOM.
let _snapMenuName = null;
$('#snapMenu').addEventListener('click', e => {
  const more = e.target.closest('button[data-more]');
  if (more) {
    e.stopPropagation();      // must not also restore the row it sits in, nor close the picker
    _snapMenuName = more.dataset.more;
    const wasOpen = !$('#snapActions').classList.contains('hidden');
    $('#snapActions').classList.add('hidden');
    if (!wasOpen) placeMenu($('#snapActions'), more);
    return;
  }
  const act = e.target.closest('button[data-act]');
  if (act && !act.disabled) {
    $('#snapMenu').classList.add('hidden');
    if (act.dataset.act === 'clearall') clearAllSnapshots();
    return;
  }
  const b = e.target.closest('button[data-snap]'); if (!b || b.disabled) return;
  $('#snapMenu').classList.add('hidden');
  applySnapshot(b.dataset.snap);
});
$('#snapActions').addEventListener('click', e => {
  const b = e.target.closest('button[data-act]'); if (!b || b.disabled) return;
  $('#snapActions').classList.add('hidden');
  $('#snapMenu').classList.add('hidden');   // the picker it opened over goes too
  ({ rename: renameSnapshot, delete: deleteSnapshot }[b.dataset.act] || (() => {}))(_snapMenuName);
});
$('#snapNew').addEventListener('click', saveSnapshotAs);
// Inside the trigger, so it must not also open the picker on its way up.
$('#snapUpdate').addEventListener('click', e => { e.stopPropagation(); updateSnapshot(); });
// Settings → General. Closes Settings first rather than stacking: .overlay is z-50 and #settings is
// z-60, so the report would otherwise open UNDERNEATH the panel that launched it.
// ---- Help window ----
// The user guide and the reference, rendered from the same markdown that ships in docs/. There is
// no second copy of the text anywhere: the window IS the doc. That is the point. Settings used to
// carry paragraphs restating docs/reference.md, and they drifted, because two copies of a sentence
// always do — the Duplicates help described a matching rule the reference had already changed.

// Markdown -> HTML. Deliberately small and deliberately incomplete: it renders OUR docs, and their
// markdown is a known subset (scanned 2026-08-22 — headings, tables, bold, code spans, fenced code,
// bullets, numbered lists, blockquotes, rules, links, two images; no nested lists, no reference
// links). A real implementation would be the frontend's first dependency, and there is no package
// manager here to install one with. If a doc ever needs something this lacks, change the doc.

// The two documents in the window, in reading order.
//
// BOTH get an id prefix, and the reference's is not optional. Its headings slug to `settings`,
// `sidebar`, `grid` and `keyboard` — and #settings, #sidebar and #grid are elements of the app
// itself. Rendered bare, getElementById('settings') returned the Settings DIALOG, so the contents
// list highlighted the right entry and scrolled nowhere. The docs' own cross-links are unaffected
// because they resolve through this same prefix.
const HELP_SOURCES = [
  { key: 'help', file: 'help.md', prefix: 'ref-' },
];

// GitHub's slug algorithm, because the docs' own 35 cross-links were written against it and have
// to keep resolving. Inline markers and link syntax come off first so `## **Bold** heading` and a
// plain one slug identically.
function mdSlug(t) {
  return t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[`*_]/g, '')
          .toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '-');
}

function mdInline(s, prefix) {
  // Code spans are stashed BEFORE escaping and restored last, so `**this**` inside backticks stays
  // literal. The reference is full of code spans holding markdown punctuation, so this is not a
  // hypothetical case — it is most of the file.
  const spans = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => '\uE000' + (spans.push(c) - 1) + '\uE000');
  s = esc(s);
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => `<img src="${encodeURI(src)}" alt="${alt}">`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, href) => {
    if (href.startsWith('#')) return `<a href="#" data-jump="${prefix}${href.slice(1)}">${text}</a>`;
    if (/^https?:\/\//.test(href)) return `<a href="${encodeURI(href)}" target="_blank" rel="noopener">${text}</a>`;
    // The guide links to the reference and back, and BOTH are open in this window — so those
    // become in-document jumps rather than dead text. `reference.md#keyboard` lands on the section;
    // a bare `reference.md` lands on the top of that document.
    const rel = /^(?:\.\/)?([\w.-]+\.md)(?:#(.+))?$/.exec(href);
    const d = rel && HELP_SOURCES.find(x => x.file === rel[1]);
    if (d) return `<a href="#" data-jump="${rel[2] ? d.prefix + rel[2] : 'doc-' + d.key}">${text}</a>`;
    // Anything else is ../DESIGN.md or a notes/ file — real documents, but not ones this window
    // holds and not ones the server has a route for. Keep the words, drop the link, rather than
    // offer one that 404s.
    return text;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^\w*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return s.replace(/\uE000(\d+)\uE000/g, (m, i) => `<code>${esc(spans[i])}</code>`);
}

function mdToHtml(src, prefix = '') {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const inline = s => mdInline(s, prefix);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];

    if (/^```/.test(ln)) {                                   // fenced code
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;                                                   // the closing fence
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }

    // A table is a pipe row whose NEXT line is the |---| separator. Requiring the separator is what
    // stops an ordinary sentence containing a pipe from being swallowed as a one-row table.
    if (/^\s*\|/.test(ln) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim()));
      const head = cells(ln);
      const body = [];
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) body.push(cells(lines[i++]));
      out.push('<div class="help-table"><table><thead><tr>'
        + head.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
        + body.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>');
      continue;
    }

    const h = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (h) {
      out.push(`<h${h[1].length} id="${prefix}${mdSlug(h[2])}">${inline(h[2])}</h${h[1].length}>`);
      i++; continue;
    }

    if (/^---+\s*$/.test(ln)) { out.push('<hr>'); i++; continue; }

    if (/^>/.test(ln)) {                                     // blockquote — recurse on its body
      const buf = [];
      while (i < lines.length && /^>/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      out.push(`<blockquote>${mdToHtml(buf.join('\n'), prefix)}</blockquote>`);
      continue;
    }

    if (/^\s*(?:[-*]|\d+\.)\s+/.test(ln)) {
      const ordered = /^\s*\d/.test(ln);
      const items = [];
      while (i < lines.length) {
        const m = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (m) { items.push(m[1]); i++; }
        // An indented continuation belongs to the item above it. The docs wrap long bullets at 100
        // columns, so without this every second line would become its own paragraph.
        else if (items.length && lines[i].trim() && /^\s+/.test(lines[i])) {
          items[items.length - 1] += ' ' + lines[i].trim(); i++;
        } else break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>` + items.map(t => `<li>${inline(t)}</li>`).join('') + `</${tag}>`);
      continue;
    }

    if (!ln.trim()) { i++; continue; }

    const buf = [];                                          // paragraph, to a blank or a new block
    while (i < lines.length && lines[i].trim()
           && !/^(?:#{1,6}\s|```|>|---+\s*$)/.test(lines[i])
           && !/^\s*(?:[-*]|\d+\.)\s/.test(lines[i])) buf.push(lines[i++]);
    if (buf.length) out.push(`<p>${inline(buf.join(' '))}</p>`);
    else i++;      // guard: never spin on a line no branch above consumed
  }
  return out.join('\n');
}

// Where the reference stops being about USING the app and starts being about how it is built. The
// contents list splits here, so the technical tail is reachable without being in the way.
let _helpLoaded = false;

async function loadHelp() {
  if (_helpLoaded) return;
  const doc = $('#helpDoc'), toc = $('#helpToc');
  doc.innerHTML = '<p class="help-empty">Loading…</p>';
  const parts = [];
  for (const s of HELP_SOURCES) {
    let md;
    try {
      const r = await fetch('/api/doc?name=' + s.key);
      if (!r.ok) throw new Error(r.status);
      md = await r.text();
    } catch {
      // Two ways to get here and the message has to serve both: a working copy deployed before
      // docs/ was added to update.bat's copy list has the code and not the text, and a downloaded
      // copy can simply be incomplete. Naming the dev deploy script told a stranger to run
      // something they do not have.
      doc.innerHTML = '<p class="help-empty">The documentation is missing from this copy.</p>';
      toc.innerHTML = '';
      return;
    }
    parts.push(`<section id="doc-${s.key}" data-doc="${s.key}">${mdToHtml(md, s.prefix)}</section>`);
  }
  doc.innerHTML = `<div class="help-col">${parts.join('')}</div>`;
  buildHelpToc();
  _helpLoaded = true;
}

// One entry per section, in document order. It used to build three named groups across two
// documents; there is one short document now, and a contents list that needs its own headings is
// a contents list for a document that is too long.
function buildHelpToc() {
  const toc = $('#helpToc');
  toc.innerHTML = '';
  for (const h of $('#helpDoc').querySelectorAll('h2')) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = h.textContent; b.dataset.target = h.id;
    toc.appendChild(b);
  }
}

// Scroll the pane, not the page: the document lives in an overflow container, so scrollIntoView
// would move the window behind the modal instead.
function helpJumpTo(id) {
  const doc = $('#helpDoc');
  // Scoped to the document pane, not getElementById: the prefixes above should make a collision
  // impossible, but the failure mode when one slips through is silent — the list highlights and
  // nothing moves — so the lookup refuses to leave the pane in the first place.
  const el = id && doc.querySelector('[id="' + CSS.escape(id) + '"]');
  if (!el) return;
  doc.scrollTop += el.getBoundingClientRect().top - doc.getBoundingClientRect().top - 8;
  $('#helpToc').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b.dataset.target === id));
}

async function openHelp(anchor) {
  $('#help').classList.remove('hidden');
  await loadHelp();
  if (anchor) helpJumpTo(anchor); else $('#helpDoc').scrollTop = 0;
}
function closeHelp() { $('#help').classList.add('hidden'); }

$('#btnHelp').addEventListener('click', () => openHelp());
// From Settings, land on the Settings section rather than the top — the question was asked there.
// THE ANCHOR IS THE FULL SLUG OF THE HEADING, not a shortened name for it. This asked for
// 'ref-settings' while the heading is "## Settings, and what each one starts as", i.e.
// 'ref-settings-and-what-each-one-starts-as' -- helpJumpTo finds nothing and returns silently, so
// the button opened Help at the top and looked like it simply scrolled badly. Older bug, found
// 2026-09-08 while adding the second of these. If a heading is renamed, both of these move with it.
$('#settingsHelp').addEventListener('click', () => openHelp('ref-settings-and-what-each-one-starts-as'));
// Recycling's blurb. The detail lives in Things that surprise people, which is where the
// network-drive behaviour is written out.
$('#helpRecycling').addEventListener('click', () => {
  closeSettings(); openHelp('ref-things-that-surprise-people');
});
$('#helpClose').addEventListener('click', closeHelp);
$('#help').querySelector('.overlay-bg').addEventListener('click', closeHelp);
$('#helpToc').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b) helpJumpTo(b.dataset.target);
});
$('#helpDoc').addEventListener('click', e => {
  const a = e.target.closest('a[data-jump]');
  if (a) { e.preventDefault(); helpJumpTo(a.dataset.jump); }
});

// ---- debug trace panel ----
// The trace lives in this tab and leaves only when you press Copy — hence a panel with a Copy
// button rather than a log file: the app's own folder isn't somewhere I can read from here.
function openTraceModal() {
  const out = $('#traceOut');
  out.textContent = Trace.text();
  $('#traceTitle').textContent = 'Debug trace' + (Trace.on ? '' : ' — not recording');
  $('#traceModal').classList.remove('hidden');
  out.scrollTop = out.scrollHeight;      // newest last, so open at the end
}
function closeTraceModal() { $('#traceModal').classList.add('hidden'); }
$('#setTrace').addEventListener('change', e => {
  Trace.set(e.target.checked);
  // Switching the trace on CLEARS it, so anything the app decided before this moment is gone —
  // including whether the auto-refresh clock is running, which is restored from localStorage before
  // boot and never mentioned again. Without this line a trace of "it didn't update" can open with
  // the clock switched off and no way to tell.
  if (e.target.checked) traceRefreshState();
  toast(e.target.checked ? 'Recording a trace — use the app, then Show trace' : 'Trace off');
});
$('#showTrace').addEventListener('click', () => { closeSettings(); openTraceModal(); });
$('#traceCopy').addEventListener('click', e => copyFlash(Trace.text(), e.currentTarget));
$('#traceClear').addEventListener('click', () => { Trace.clear(); $('#traceOut').textContent = Trace.text(); });
$('#traceClose').addEventListener('click', closeTraceModal);
$('#traceModal').querySelector('.overlay-bg').addEventListener('click', closeTraceModal);

$('#findDupes').addEventListener('click', () => { closeSettings(); findDuplicates(); });
$('#dupesCull').addEventListener('click', cullDuplicates);
$('#dupesClose').addEventListener('click', closeDupesModal);
$('#dupesDone').addEventListener('click', closeDupesModal);
$('#dupesModal').querySelector('.overlay-bg').addEventListener('click', closeDupesModal);

// folder ⋯ in the detail view — same shared-menu pattern as the library rail's ⋯
$('#dFolderMore').addEventListener('click', e => {
  e.stopPropagation();                            // else the document closer eats it immediately
  const wasOpen = !$('#folderMenu').classList.contains('hidden');
  closeMenus();
  if (!wasOpen && state.current) placeMenu($('#folderMenu'), e.currentTarget);
});
$('#folderMenu').addEventListener('click', e => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  $('#folderMenu').classList.add('hidden');
  ({ setfilter: filterToThisFolder, dupes: deleteFolderCopies }[b.dataset.act] || (() => {}))();
});
document.addEventListener('click', closeMenus);   // outside/item click closes every popmenu
$('#stopScan').addEventListener('click', stopScan);
$('#undoDelete').addEventListener('click', () => Undo.run());
// Click the pill to put it away (the author, 2026-08-15). A 5-second offer you have already read is just
// something sitting on your pictures.
//
// IT HIDES THE PILL AND NOTHING ELSE. The queued recycle still commits on its own timer, so a stray
// click can never bring a deletion forward — and Ctrl+Z keeps working for the rest of the window,
// which is what makes hiding the offer safe rather than final. Dismissing deliberately does NOT
// mean "yes, do it now": that reading would make an accidental click destructive, and the whole
// feature exists to make fast culling safe.
//
// NOT while a job owns the pill. It is then carrying a progress bar and the Stop button, and hiding
// those would take away the only way to stop the work — a dismiss gesture must never remove the
// user's control over something still running.
$('#status').addEventListener('click', e => {
  if (e.target.closest('button')) return;                 // Undo and Stop do their own thing
  if ($('#stopScan').style.display !== 'none') return;    // a job is running; Stop must stay reachable
  $('#status').classList.remove('show');
});
$('#grid').addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  e.preventDefault();
  if (e.target.closest('.star')) { toggleCardFav(card); return; }
  if (e.target.closest('.zoom')) { openDetail(card.dataset.id); return; }   // openDetail plays the video for a pair
  toggleSelect(card, e.shiftKey);        // click anywhere on the cell = select
});
$('#grid').addEventListener('dblclick', e => {
  const card = e.target.closest('.card');
  if (card) { e.preventDefault(); openDetail(card.dataset.id); }
});
// Drag-to-ComfyUI. ComfyUI loads a workflow only from the actual dropped FILE, and the browser only
// attaches file bytes when the dragged <img> holds a fully-loaded image. Grid cards show a
// metadata-less thumbnail, so on hover (or press) we swap the face to the original /file/ PNG; once
// it has loaded, dragging the card behaves exactly like the detail view — Chrome attaches the
// original bytes (workflow intact) and ComfyUI loads the graph.
let _gridUp = null;                          // the one card img currently swapped to full-res
function gridRevert() {
  if (_gridUp && _gridUp.dataset.thumb) {
    _gridUp.onload = null;
    _gridUp.src = _gridUp.dataset.thumb;     // restore the thumbnail; frees the big image
    _gridUp.loading = 'lazy';
    delete _gridUp.dataset.thumb; delete _gridUp.dataset.full;
    // Traced because this is the one thing that REPLACES a card's picture, and it is invisible to
    // the rest of the trace: the swap is an image load, not a JSON request. Two src changes per
    // card — thumb to original and back — each repainting a picture that was already on screen.
    Trace.add('card-face', 'reverted to the thumbnail');
  }
  _gridUp = null;
}
// NOT-DRAGGABLE — the rule every thumbnail in the app follows.
//
// A browser lets you drag any <img>, and it hands the drop target THAT IMAGE'S BYTES. So dragging a
// thumbnail into ComfyUI delivers a 160px WebP carrying no workflow — it loads, it looks roughly
// right, and it is the wrong file. Found 2026-08-11 by looking in ComfyUI's own input folder and
// finding .webp files there.
//
// Only two things in the app may be dragged, because only these two ARE the original: a grid card
// (which swaps its face to the real file first — below) and the detail view's main picture. Every
// other <img> that shows a /thumb/ URL carries draggable="false": the filmstrip, and the detail
// picture of an offline library.
// A thumbnail URL rewritten to fetch the ORIGINAL, carrying the file's own name.
//
// The name is a trailing path SEGMENT rather than a query parameter, because that is what the
// browser reads when it names a dragged-out file — a drop into ComfyUI used to arrive as the row
// number, `28442.png`. The server ignores the segment and routes on the id (see _file_url).
//
// Query string preserved intact: ?v= and &r= are what keep one library's picture out of another's
// cache, and dropping them here would be invisible until two libraries disagreed.
//
// A VIDEO asks for /dragpng/ instead, and under a .png name. There is no version of this that
// hands ComfyUI the .mp4 — a browser only carries a file between windows when that file is the one
// behind the <img> being dragged, so the dragged thing has to BE a picture. The server answers with
// the video's first frame carrying the video's own workflow, which is what ComfyUI actually reads.
function originalUrlFor(thumbUrl, filename, kind) {
  // A SONG rides the same route as a video and for the same reason: it cannot be dragged as itself,
  // so the server hands back a picture carrying its workflow.
  const video = kind === 'video' || kind === 'audio';
  const url = String(thumbUrl || '').replace('/thumb/', video ? '/dragpng/' : '/file/');
  const cut = url.indexOf('?');
  const base = cut < 0 ? url : url.slice(0, cut);
  const qs = cut < 0 ? '' : url.slice(cut);
  let fn = String(filename || '').replace(/[^A-Za-z0-9 _.\-]/g, '_').trim();
  // The dropped file IS a PNG, so it must not arrive called .mp4 — ComfyUI would take the name at
  // face value and put a file in its input folder that no tool can open.
  if (video && fn) fn = fn.replace(/\.[^.]*$/, '') + '.png';
  return (fn ? `${base}/${encodeURIComponent(fn)}` : base) + qs;
}
function gridUpgrade(img) {                   // point the face <img> at the original PNG
  if (!img || _gridUp === img) return;
  const thumb = img.getAttribute('src') || '';
  if (thumb.indexOf('/thumb/') < 0) return;
  gridRevert();                              // keep at most one full-res image in memory
  img.dataset.thumb = thumb;
  img.dataset.full = '0';
  img.loading = 'eager';                     // else loading="lazy" can defer the swapped-in original
  const t = performance.now();
  img.onload = () => {
    const src = img.getAttribute('src') || '';
    if (src.indexOf('/file/') >= 0 || src.indexOf('/dragpng/') >= 0) img.dataset.full = '1';
    // How long the card showed its thumbnail before the original replaced it — i.e. exactly how
    // long after appearing the picture repaints.
    Trace.add('card-face', `original decoded and swapped in · ${Math.round(performance.now() - t)}ms`);
  };
  img.src = originalUrlFor(thumb, img.dataset.fn, img.dataset.kind);
  Trace.add('card-face', `swapping a card to its FULL-SIZE original (drag-to-ComfyUI preload)`);
  _gridUp = img;
}
function gridDraggableImg(e) {
  const card = e.target.closest('.card'); if (!card) return null;
  const img = card.querySelector('img');
  return (img && img.getAttribute('draggable') === 'true') ? img : null;
}
let _gridHoverT = null;
$('#grid').addEventListener('mouseover', e => {           // preload the original on a settled hover
  const img = gridDraggableImg(e); if (!img || _gridUp === img) return;
  clearTimeout(_gridHoverT);
  // NOT while a page is still coming. This preload is a guess that you MIGHT drag this card into
  // ComfyUI; a page in flight is content you are already looking at, and the two compete for the
  // browser's ~6 connections. A full-size original is megabytes — one measured at 4,650ms — and
  // while it runs it holds a connection that the next page of cards then queues behind, which
  // showed up as a 2.2s stall in a folder where every other page took 145ms.
  //
  // Worth being precise about the history: this path was NOT the cause when it was first
  // suspected, and the traces said so — the queueing then was the server and the connection
  // limit, and hovering changed nothing measurable. It only became the biggest thing left after
  // those were fixed. A cause ruled out under one set of conditions is not ruled out forever.
  //
  // Checked when the timer FIRES, not when it is set: a page can land in that 120ms, and the
  // question is whether the connections are busy now.
  _gridHoverT = setTimeout(() => { if (!state.loading) gridUpgrade(img); }, 120);
});
$('#grid').addEventListener('mousedown', e => {           // pressing to drag: start the load now
  const img = gridDraggableImg(e); if (img) { clearTimeout(_gridHoverT); gridUpgrade(img); }
});
$('#grid').addEventListener('mouseleave', () => { clearTimeout(_gridHoverT); gridRevert(); });
$('#grid').addEventListener('dragstart', e => {
  const img = gridDraggableImg(e);
  // Only fly once the original has loaded — otherwise we'd hand ComfyUI the thumbnail (Load Image).
  if (!img || img.dataset.full !== '1') { e.preventDefault(); return; }
  e.dataTransfer.effectAllowed = 'copy';     // else the browser's native image drag carries the file
});
$('#selAll').addEventListener('click', selectAllMatching);
$('#selClear').addEventListener('click', clearSelection);
$('#selDelete').addEventListener('click', deleteSelected);
$('#selTag').addEventListener('click', openTagModal);
$('#mTagInput').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addModalTag(e.target.value); } });
$('#mTagInput').addEventListener('input', e => {   // datalist pick -> add chip immediately (see #dTagInput)
  const v = e.target.value;
  if (e.inputType === 'insertReplacementText' || (!e.inputType && (state._tags || []).some(t => t.name === v))) addModalTag(v);
});
$('#mTags').addEventListener('click', e => { const b = e.target.closest('[data-rmtag]'); if (b) removeModalTag(b.dataset.rmtag); });
$('#tagModalClose').addEventListener('click', closeTagModal);
$('#tagModalDone').addEventListener('click', closeTagModal);
$('#tagModal .overlay-bg').addEventListener('click', closeTagModal);
// prompt miner review modal (Mine + Clear tags are launched from a library's ⋯ menu)
$('#minerClose').addEventListener('click', closeMinerModal);
$('#minerModal .overlay-bg').addEventListener('click', closeMinerModal);
$('#minerRemine').addEventListener('click', () => runMineScan());   // no arg: re-mine the current library (don't pass the click Event as the key)
$('#minerApply').addEventListener('click', applyMinedTags);
$('#minerFilter').addEventListener('input', renderMinerCandidates);
$('#minerThresh').addEventListener('input', renderMinerCandidates);
document.querySelectorAll('.minerSrc').forEach(c => c.addEventListener('change', renderMinerCandidates));
$('#minerAll').addEventListener('click', () => {
  const existing = minerExistingTags();
  minerVisible().forEach(c => { if (!existing.has(c.tag)) state.minerPicks.add(c.tag); });   // don't re-pick already-applied
  renderMinerCandidates();
});
$('#minerNone').addEventListener('click', () => { state.minerPicks.clear(); renderMinerCandidates(); });
$('#minerList').addEventListener('change', e => {
  const cb = e.target.closest('.miner-pick'); if (!cb) return;
  if (cb.checked) state.minerPicks.add(cb.dataset.tag); else state.minerPicks.delete(cb.dataset.tag);
  updateMinerStatus();
});
$('#selFav').addEventListener('click', favSelected);
$('#selReward').addEventListener('click', rewardSelected);
// One guard for the whole bar, in the CAPTURE phase so it lands before any button's own handler.
// The alternative — an early return inside each of the nine handlers — is nine places to forget.
$('#selbar').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b && b.getAttribute('aria-disabled') === 'true') { e.preventDefault(); e.stopPropagation(); }
}, true);
// The bar's ⋯ overflow. Same shape as #dMore: stopPropagation so the document's click→closeMenus
// doesn't shut what this click just opened; the ITEMS are deliberately not stopped, so clicking one
// runs its action and lets the document handler close the menu.
$('#selMore').addEventListener('click', e => {
  e.stopPropagation();
  const menu = $('#selMoreMenu');
  const wasOpen = !menu.classList.contains('hidden');
  closeMenus();
  if (!wasOpen) menu.classList.remove('hidden');
  e.currentTarget.setAttribute('aria-expanded', wasOpen ? 'false' : 'true');
});
$('#selRename').addEventListener('click', openRename);
$('#rnFind').addEventListener('input', previewRename);
$('#rnReplace').addEventListener('input', previewRename);
$('#rnApply').addEventListener('click', applyRename);
$('#rnCancel').addEventListener('click', closeRename);
$('#rename .overlay-bg').addEventListener('click', closeRename);
// tag list: checkbox toggles a filter (AND); hover × deletes the tag; search box filters + adds
$('#taglist').addEventListener('change', e => {
  const cb = e.target;
  if (!cb.dataset) return;
  if (cb.dataset.fav) { state.favOnly = cb.checked; search(true); return; }
  if (cb.dataset.tag) {
    const name = cb.dataset.tag, i = state.tags.indexOf(name);
    if (cb.checked && i === -1) state.tags.push(name);
    else if (!cb.checked && i !== -1) state.tags.splice(i, 1);
    search(true);
  }
});
$('#taglist').addEventListener('click', e => {
  const del = e.target.closest('.tagdel');
  if (del) { e.preventDefault(); e.stopPropagation(); deleteTag(del.dataset.del); }
});
$('#tagSearch').addEventListener('input', renderTagList);
$('#tagSearch').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addTagToSelection(e.target.value); } });
// Labels: sidebar rows filter (single-select); detail buttons set/clear the current image's label.
$('#labelList').addEventListener('click', e => {
  const r = e.target.closest('[data-label]'); if (r) filterByLabel(r.dataset.label);
});
$('#dLabels').addEventListener('click', e => { const b = e.target.closest('[data-label]'); if (b) labelDetail(b.dataset.label); });
// detail tags + favorite
$('#dTagInput').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addDetailTag(e.target.value); } });
// Picking an existing tag from the datalist adds the chip immediately (no Enter needed).
// A pick inserts the whole option at once: Chromium reports inputType 'insertReplacementText';
// other browsers report a null inputType, so treat a null-inputType value that exactly matches a
// known tag as a pick too. Plain typing (inputType 'insertText'/'deleteContentBackward') never
// auto-commits, so multi-word tags can still be typed and confirmed with Enter.
$('#dTagInput').addEventListener('input', e => {
  const v = e.target.value;
  if (e.inputType === 'insertReplacementText' || (!e.inputType && (state._tags || []).some(t => t.name === v))) {
    addDetailTag(v);
  }
});
$('#dTags').addEventListener('click', e => { const b = e.target.closest('[data-rmtag]'); if (b) removeDetailTag(b.dataset.rmtag); });
// Notes: auto-save (debounced while typing, and immediately on blur — e.g. before navigating away).
// Persists to images.note and updates the grid card's ✎ indicator in place.
let _noteTimer = null;
async function saveNote() {
  const d = state.current; if (!d) return;
  const text = $('#dNote').value;
  const j = await postJSON('/api/note', { id: Number(d.id), text });
  if (!j || !j.ok) return;
  d.note = j.has_note ? text : null;
  const it = state.items.find(x => String(x.id) === String(d.id));
  if (it && (it.note || null) !== d.note) {   // badge presence OR hover text changed
    it.has_note = j.has_note; it.note = d.note;
    refreshCard(d.id);
  }
}
$('#dNote').addEventListener('input', () => { clearTimeout(_noteTimer); _noteTimer = setTimeout(saveNote, 500); });
$('#dNote').addEventListener('blur', () => { clearTimeout(_noteTimer); saveNote(); });
$('#dSetView').addEventListener('click', e => { const p = e.target.closest('.set-pane'); if (p) markSetKeep(Number(p.dataset.idx)); });
$('#cardFactRows').addEventListener('click', e => {
  const row = e.target.closest('.fact-row'); if (!row) return;
  if (e.target.closest('.fact-up')) moveCardFact(row.dataset.key, -1);
  else if (e.target.closest('.fact-down')) moveCardFact(row.dataset.key, 1);
});
$('#cardFactRows').addEventListener('change', e => {
  const row = e.target.closest('.fact-row'); if (!row || !e.target.classList.contains('fact-place')) return;
  const r = (_cardsEdit || []).find(x => x.key === row.dataset.key);
  if (r) r.place = e.target.value;
});
$('#dPairPrev').addEventListener('click', () => stepPairStill(-1));
$('#dPairNext').addEventListener('click', () => stepPairStill(1));
$('#dFav').addEventListener('click', toggleDetailFav);
$('#closeDetail').addEventListener('click', closeDetail);
$('#overlay .overlay-bg').addEventListener('click', closeDetail);
// Same fall-through as the ←/→ keys: in a maximized set these flip the set's members, otherwise
// they walk the results. The arrows are the same control whichever they are doing, so they must not
// be two different buttons.
$('#dPrev').addEventListener('click', () => stepDetail(-1));
$('#dNext').addEventListener('click', () => stepDetail(1));
// The two halves of one mode: #dMaxiBtn in the actions row goes in, #dMaxi in the corner comes out.
// Both call the same toggle — each is only on screen in the mode where its own label is true, so
// neither needs to know which way it is going.
$('#dMaxiBtn').addEventListener('click', () => Maxi.toggle());
$('#dMaxi').addEventListener('click', () => Maxi.toggle());
// Filmstrip: click to jump. Delegated, because the strip is rebuilt wholesale on a new result set.
$('#stripScroll').addEventListener('click', e => {
  const b = e.target.closest('.strip-item');
  if (b && b.dataset.id !== String(state.current && state.current.id)) openDetail(b.dataset.id);
});
// A vertical wheel over a horizontal strip does nothing by default, which reads as a dead control.
// passive:false because translating the axis means preventing the default vertical scroll — without
// it the page behind the overlay would scroll instead.
$('#stripScroll').addEventListener('wheel', e => {
  if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;   // a real horizontal wheel: leave it alone
  e.preventDefault();
  $('#stripScroll').scrollLeft += e.deltaY;
}, { passive: false });
$('#stripSize').addEventListener('change', e => applyStripSize(Number(e.target.value)));
// The cues follow the scroll however it was caused — a wheel, a press on the cue itself, or the
// glide syncFilmstrip runs when you press an arrow.
$('#stripScroll').addEventListener('scroll', updateStripEdges, { passive: true });
// A window resize changes how much of the strip fits, and so whether there is anything past either
// end. The strip is only in the DOM while the detail view is open, and the toggle is a no-op when
// nothing changed, so this needs no debounce of its own.
window.addEventListener('resize', updateStripEdges);
applyStripSize(Number(localStorage.getItem(STRIP_SIZE_KEY)) || 160);
// setup modal
$('#setupGo').addEventListener('click', submitSetup);
// `input` rather than `keyup`: it fires for a PASTE and for the field being cleared with the
// mouse, which is how a Windows path usually arrives here.
$('#setupPath').addEventListener('input', syncSetupGo);
$('#setupBrowse').addEventListener('click', pickFolder);
$('#setupCancel').addEventListener('click', closeSetup);
$('#setup .overlay-bg').addEventListener('click', closeSetup);
// The help hint: its whole point is the Help window, so Open Help closes it on the way there
// rather than leaving two layers stacked.
$('#helpHintGo').addEventListener('click', () => { closeHelpHint(); openHelp(); });
$('#helpHintClose').addEventListener('click', closeHelpHint);
$('#helpHint .overlay-bg').addEventListener('click', closeHelpHint);
$('#btnUpdate').addEventListener('click', openUpdateBox);
$('#updateClose').addEventListener('click', closeUpdateBox);
$('#updateBox .overlay-bg').addEventListener('click', closeUpdateBox);
// The release page, in the user's real browser. `noopener` because the opened page must not get a
// handle back to this window -- the one rule that matters when opening somewhere you don't control.
$('#updateLink').addEventListener('click', () => {
  if (_update && _update.url) window.open(_update.url, '_blank', 'noopener');
});
$('#setupPath').addEventListener('keydown', e => { if (e.key === 'Enter') submitSetup(); });
const SCROLL_KEYS = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End'];
// A <select> KEEPS FOCUS after you pick from it, so the control you just used goes on owning the
// keyboard. Measured, both real keystrokes: choosing a Model type and pressing X did nothing, and
// pressing R with Sort focused type-ahead-jumped it to "Random" and reshuffled the grid instead of
// refreshing — a shortcut doing something actively wrong, which is worse than doing nothing.
// So hand the keyboard back once the choice is made — but ONLY when it was made with the POINTER.
// Arrowing through options fires `change` on every step, and blurring there would rip focus away
// mid-navigation: the same bug in the other direction.
// pointerdown in CAPTURE so a handler that stops propagation can't hide it; the blur is deferred to
// a microtask so it lands after the control's own change handler has run with focus still intact.
let _selectByPointer = false;
document.addEventListener('pointerdown', e => {
  _selectByPointer = !!(e.target && e.target.tagName === 'SELECT');
}, true);
document.addEventListener('change', e => {
  if (!_selectByPointer || !e.target || e.target.tagName !== 'SELECT') return;
  _selectByPointer = false;
  const sel = e.target;
  queueMicrotask(() => { if (document.activeElement === sel) sel.blur(); });
}, true);
// Dupes, debug trace and prompt miner closed only by their own mark or a backdrop click until the author
// asked for Escape, 2026-09-11. Returns the closer for whichever one is showing, else null.
// ONE list, because two callers need it and must not disagree about what "a dialog is open" means:
// the Escape ladder, and the loupe guard that runs before it. A fourth panel remembered in only one
// of them is precisely the bug this shape prevents.
// Not here: #dialog (z 70), which claims Escape in its own capture handler, and Settings/Help,
// which carry open-flags of their own.
function escPanel() {
  const hit = [['#dupesModal', closeDupesModal], ['#traceModal', closeTraceModal],
               ['#minerModal', closeMinerModal],
               // The answers panel joins the ladder, with one condition the others don't need:
               // while the run is still going Escape must not discard it, or a slip halfway
               // through a selection throws away every answer already paid for. Cancel the run
               // first, then Escape closes it like the rest.
               ['#answersModal', () => { if (!(Job.busy && Job.name === 'asking')) closeAnswers(); }],
              ].find(([sel]) => !$(sel).classList.contains('hidden'));
  return hit ? hit[1] : null;
}
document.addEventListener('keydown', e => {
  // Figure out the topmost open layer; keys only act on it and never leak to the background.
  const settingsOpen = !$('#settings').classList.contains('hidden');
  const renameOpen = !$('#rename').classList.contains('hidden');
  const setupOpen = !$('#setup').classList.contains('hidden');
  const tagModalOpen = !$('#tagModal').classList.contains('hidden');
  const helpOpen = !$('#help').classList.contains('hidden');
  const detailOpen = !$('#overlay').classList.contains('hidden');
  // Whichever of the three standalone panels is up, as its closer. Resolved HERE with the other
  // open-flags rather than inside the Escape branch, because more than one place below needs the
  // same answer: the ladder itself, and the arrow-key guard further down.
  const panelClose = escPanel();
  // ONE expression for "is any layer open", read by every test below. It was TWO lists and they
  // disagreed: the grid-view guard did not know about the three panels, so with only the duplicates
  // window up it concluded nothing was open, treated Escape as clear-selection and returned before
  // the ladder ever ran — the Escape support looked wired and did nothing.
  // THE TWO SMALL POP-UPS COUNT TOO, and leaving them out is the same bug the paragraph above
  // describes, recurred on the two newest overlays. The ladder below already had a branch for each
  // of them; neither could ever run, because Escape was treated as grid-view clear-selection and
  // returned first. Found 2026-09-15 while testing the version notice end to end.
  //
  // THE RULE THIS KEEPS BREAKING: a new overlay has to be added HERE as well as to the ladder.
  // One list decides whether Escape is even about layers; the other decides which layer it takes.
  // An overlay in only the second is wired to nothing, and looks wired to anyone reading it.
  const layerOpen = settingsOpen || renameOpen || setupOpen || tagModalOpen || helpOpen
                 || detailOpen || !!panelClose
                 || !$('#updateBox').classList.contains('hidden')
                 || !$('#helpHint').classList.contains('hidden');
  // Ctrl+Z, ABOVE the layer split on purpose: a recycle from the detail view leaves the detail
  // open (it advances to the next image), so a grid-only binding would be dead in the one place
  // you most often delete from. Guarded on the offer actually standing, so it never means anything
  // the pill isn't showing — and on not typing, where Ctrl+Z is the browser's own text undo.
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'z' || e.key === 'Z')
      && Undo.active && !isTypingTarget(e.target)) {
    e.preventDefault(); Undo.run(); return;
  }
  if (!layerOpen) {                                 // nothing open: grid view
    if (isTypingTarget(e.target)) return;          // typing in the search/tag box owns its keys
    if (e.ctrlKey || e.metaKey || e.altKey) {       // modified keys are never bare shortcuts (Ctrl+A != label 'a')
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'a' || e.key === 'A')) {
        e.preventDefault(); selectAllMatching();     // select every card matching the current filter
      }
      return;
    }
    if (e.key === 'Escape') { if (selection.size) { e.preventDefault(); clearSelection(); } return; }
    if (e.key === 'r' || e.key === 'R') { e.preventDefault(); refreshChanged(); return; }   // refresh: rescan changed libraries
    // Clear: the keyboard twin of the Reset button. NOT 'c' — that is the "To refine" label, and the
    // labels own the whole a-e run. Guarded on anythingToReset() for the same reason the button is
    // disabled without it: resetFilters() dims the entire rail and makes three round trips, so on an
    // already-clean rail this would blank the sidebar to achieve nothing.
    if (e.key === 'x' || e.key === 'X') { e.preventDefault(); if (anythingToReset()) resetFilters(); return; }
    const l = labelByKey(e.key);                    // a-d: label the selection (exclusive)
    if (l) { if (selection.size) { e.preventDefault(); labelSelection(l.slug); } return; }
    if (e.key === 'Delete' && selection.size) { e.preventDefault(); deleteSelected(); }
    return;
  }

  if (e.key === 'Escape') {                       // Escape closes the topmost layer
    // THE MAGNIFIER IS NOT A LAYER (reversed 2026-09-15; see docs/decisions.md). It used to take
    // the first Escape, on the reasoning that dropping the lens beats closing the view out from
    // under someone who only wanted the magnifier gone. That held while pinning meant a deliberate
    // click on the icon. Now `z` pins it, so a lens is the ordinary state of the review flow — set a
    // magnification, then walk the set with ←/→ — and a setting must not eat the key that leaves
    // the view. From maximized it is two presses to the grid whether or not the lens is up.
    // Nothing is stranded: `z` cycles round to off, the icon toggles, and closeDetail clears it.
    // Help FIRST: it opens from the Settings header and sits over it, so Escape has to take the
    // top layer off. Testing settingsOpen first would shut Settings out from under an open Help.
    if (helpOpen) closeHelp();
    // Above Settings for the same reason Help is: the hint is only ever the topmost layer, and it
    // is the one thing on screen a new user might want gone before anything else.
    else if (!$('#helpHint').classList.contains('hidden')) closeHelpHint();
    // Same tier as the help hint and for the same reason: it is only ever the topmost layer, since
    // the only way to it is a click on the rail with nothing else open.
    else if (!$('#updateBox').classList.contains('hidden')) closeUpdateBox();
    else if (settingsOpen) closeSettings();
    else if (renameOpen) closeRename();
    else if (setupOpen) closeSetup();
    else if (tagModalOpen) closeTagModal();
    // Dupes / debug trace / prompt miner. They sit at the SAME z as the detail overlay and can open
    // over it — the folder menu's "Delete copies…" is reachable with an image open — so they take
    // Escape before it: whatever is on top owns the key.
    else if (panelClose) panelClose();
    // Maximized IS a layer: step back to the normal detail view rather than closing it out from
    // under someone who only wanted the chrome back. So from maximized it is Esc for the detail
    // view, Esc again for the grid — two presses to the grid from anywhere in here, which is the
    // whole point of the magnifier no longer taking one of them.
    else if (Maxi.on) Maxi.off();
    else closeDetail();
    return;
  }
  if (isTypingTarget(e.target)) return;           // a focused field owns its own keys (cursor, spin…)

    // Arrow-key image nav only when the detail view is the topmost layer (no dialog over it).
    // !panelClose matters MORE than it looks: when the capture-phase arrow handler bails because a
    // panel is open it does NOT stopPropagation, so the event arrives here instead — and this block
    // would happily navigate the pictures behind the modal. Blocking in one handler only moves the
    // bug; both guards have to know the same things.
    if (detailOpen && !settingsOpen && !renameOpen && !setupOpen && !helpOpen && !panelClose) {
      // a-d: label the current image (letters don't clash with set-cull digits). Ignore when a
      // modifier is held so e.g. Ctrl+A stays the browser's own action, not the 'a' label.
      const l = (e.ctrlKey || e.metaKey || e.altKey) ? null : labelByKey(e.key);
      if (l) { e.preventDefault(); labelDetail(l.slug); return; }
      // z steps the magnifier: off → 1× → 2× → 3× → 4× → off. Shift+Z is the momentary peek that
      // ends when you let go — the behaviour plain z used to have, moved aside on 2026-09-15 so the
      // lens could be switched on without holding a key down while arrowing through a set.
      //
      // BRANCH ON e.shiftKey, NEVER ON THE LETTER'S CASE. `e.key` is 'Z' for Shift+z AND for a
      // plain z under Caps Lock, so a case test hands anyone with Caps Lock on the exact opposite of
      // both behaviours. The letter test stays case-insensitive; only the modifier decides which.
      //
      // `repeat` is ignored for both: autorepeat would re-cycle the zoom on a held key, and
      // re-raising the peek on every repeat would fight the pointer.
      if (!(e.ctrlKey || e.metaKey || e.altKey) && (e.key === 'z' || e.key === 'Z')) {
        if (!e.repeat) e.shiftKey ? Loupe.hold(true) : Loupe.tap();
        e.preventDefault(); return;
      }
      // f: maximize / restore. NOT Tab, which is Lightroom's key for this — Tab is how a keyboard
      // reaches the action bar, and this app's keyboard story is thin enough (README) without
      // taking the one key that moves through it.
      if (!(e.ctrlKey || e.metaKey || e.altKey) && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault(); Maxi.toggle(); return;
      }
      if (state.setMembers) {          // image-set cull view: digits mark the keeper, Delete recycles the rest
        // K: the entire cull, and ONLY while maximized — one picture fills the screen, so "this one"
        // is unambiguous in a way it is not beside three panes, where the digits already mean it.
        if (!(e.ctrlKey || e.metaKey || e.altKey) && (e.key === 'k' || e.key === 'K')) {
          e.preventDefault();
          if (Maxi.setwise) keepShownMember();
          return;
        }
        // Maximized, the digits SHOW that member instead of marking it: there is one picture on
        // screen and no panes to mark, and the mode is explicitly for looking rather than culling —
        // The author, on agreeing it: "I can return to the default detail view to complete the action."
        if (e.key >= '1' && e.key <= '9') {
          e.preventDefault();
          const i = Number(e.key) - 1;
          if (!Maxi.jump(i)) markSetKeep(i);
          return;
        }
        // Delete follows whichever recycle button is active: keeper marked -> recycle the
        // others; nothing marked -> whole-set recycle (behind its confirm).
        if (e.key === 'Delete' || e.key === 'Backspace') {
          e.preventDefault();
          if (state.setKeep != null) recycleSetOthers(); else recycleSetFull();
          return;
        }
      } else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteCurrent(); return; }
      // Maximized inside a set, the arrows walk the SET; everywhere else they walk the results, as
      // they always have. Maxi.step returns false when it is not its business, so this is one
      // branch rather than a condition repeated on both keys.
      if (e.key === 'ArrowLeft')  { e.preventDefault(); stepDetail(-1, e.shiftKey); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); stepDetail(1,  e.shiftKey); return; }
    }
  // Otherwise stop scroll keys from moving the grid/page behind the open overlay.
  if (SCROLL_KEYS.includes(e.key)) e.preventDefault();
});

// The lens's own listeners. keyup is on window and UNCONDITIONAL: the keydown that raised it was
// gated on the detail being open, but if anything closes in between, a gated keyup would never fire
// and the lens would stick on with no key held. Releasing is the half that must not have conditions
// — the same rule the busy indicators follow (DESIGN.md: whatever raises may lower, and the lower
// is the half that needs an owner).
window.addEventListener('keyup', e => { if (e.key === 'z' || e.key === 'Z') Loupe.hold(false); });
window.addEventListener('blur', () => Loupe.hold(false));   // alt-tabbing away eats the keyup
document.addEventListener('mousemove', e => Loupe.move(e.clientX, e.clientY));
// The wheel zooms while the lens is up, in either mode. Only claimed when it is actually showing,
// so an ordinary scroll over the detail view is untouched the rest of the time.
document.addEventListener('wheel', e => {
  if (!Loupe.showing) return;
  e.preventDefault();
  Loupe.step(e.deltaY < 0 ? 1 : -1);
}, { passive: false });
// CAPTURE, because `load` does not bubble. Stepping with ←/→ swaps the picture while the pointer
// sits still, so without this the lens shows the previous image until the mouse twitches.
document.addEventListener('load', e => {
  if (e.target.tagName === 'IMG' && e.target.closest && e.target.closest('.detail-img')) Loupe.refresh();
}, true);
document.addEventListener('click', e => {
  if (e.target.closest('#dLoupeBtn')) { e.preventDefault(); Loupe.toggle(); }
});
// Detail-view keys claimed in the CAPTURE phase so a focused <video controls> can't swallow them
// first: ←/→ step prev/next (video natively seeks), Space toggles play/pause.
document.addEventListener('keydown', e => {
  const isArrow = e.key === 'ArrowLeft' || e.key === 'ArrowRight';
  const isSpace = e.key === ' ' || e.key === 'Spacebar';
  if (!isArrow && !isSpace) return;
  const detailOpen = !$('#overlay').classList.contains('hidden');
  const blocked = !$('#settings').classList.contains('hidden')
               || !$('#rename').classList.contains('hidden')
               || !$('#setup').classList.contains('hidden')
               || !$('#dialog').classList.contains('hidden')
               // A panel over the detail view must not let ←/→ drive the pictures behind it. Same
               // list as the Escape ladder's, via the same helper, for the same reason.
               || !!escPanel();
  // A focused control inside the FILMSTRIP never owns ←/→. The strip is the arrow navigation, so
  // after picking a size the select keeps focus and silently swallowed every arrow press — changing
  // the thumbnail size instead of the image, and forcing you to click elsewhere first.
  // isTypingTarget() counts every <select>, and deliberately still does: it also guards the grid's
  // a–e / R / X hotkeys, where a focused Sort select really should keep its own keys. This is the
  // one place that has to disagree, and only for the two arrows.
  const stripCtl = isArrow && e.target && e.target.closest && e.target.closest('.filmstrip');
  if (!detailOpen || blocked || (isTypingTarget(e.target) && !stripCtl)) return;
  if (isSpace) {
    if (e.target.closest && e.target.closest('.detail button')) return;   // only in-detail buttons keep Space; not a stale grid button
    const vid = $('#dVid');
    if (!vid || vid.classList.contains('hidden')) return;         // only when a video is showing
    e.preventDefault(); e.stopPropagation();
    if (vid.paused) vid.play().catch(() => {}); else vid.pause();
    return;
  }
  e.preventDefault(); e.stopPropagation();   // beat the video's native scrub + the bubble handler
  stepDetail(e.key === 'ArrowLeft' ? -1 : 1, e.shiftKey);
}, true);
document.querySelectorAll('.copy').forEach(b => b.addEventListener('click', e => {
  e.preventDefault(); e.stopPropagation();   // don't toggle the collapsible prompt field
  copyField(b.dataset.copy, b);
}));
$('#dCopyAll').addEventListener('click', e => copyAllCivitai(e.currentTarget));
$('#dCivitaiExport').addEventListener('click', exportForCivitai);
$('#dReveal').addEventListener('click', () => { if (state.current) fetch('/api/reveal/' + state.current.id, { method: 'POST' }); });
$('#dDelete').addEventListener('click', () => state.setMembers ? recycleSetFull() : deleteCurrent());
// settings dialog
$('#settingsClose').addEventListener('click', closeSettings);
$('#settingsCancel').addEventListener('click', closeSettings);
$('#settings .overlay-bg').addEventListener('click', closeSettings);
$('.settings-nav').addEventListener('click', e => {
  const t = e.target.closest('.settings-tab'); if (t) showSettingsSection(t.dataset.sec);
});
$('#setRecycleWarn').addEventListener('change', syncRecycleWarn);
$('#setRecycleFiles').addEventListener('blur', e => clampField(e.target));
$('#setRecycleGb').addEventListener('blur', e => clampField(e.target));
$('#setSave').addEventListener('click', saveSettings);
// Global models folder: live "found / not found" hint so an unreachable path is obvious.
$('#setModelsDir').addEventListener('blur', async () => {
  const el = $('#setModelsDirStatus'); const p = $('#setModelsDir').value.trim();
  if (!p) { el.textContent = ''; el.className = 'set-test'; return; }
  try {
    const c = await getJSON('/api/models/check?path=' + encodeURIComponent(p));
    el.textContent = _modelsDirCheckMsg(c);
    el.className = 'set-test ' + (c.exists ? 'ok' : 'err');
  } catch (e) { el.textContent = ''; el.className = 'set-test'; }
});
// theme editor (Appearance tab): live color edits + presets
$('#themeRows').addEventListener('input', e => {
  const inp = e.target.closest('input[data-token]'); if (!inp) return;
  _themeEdit[inp.dataset.token] = inp.value;
  document.documentElement.style.setProperty(inp.dataset.token, inp.value);
  const hex = inp.parentElement.querySelector('.tr-hex'); if (hex) hex.textContent = inp.value;
  // Touching a swatch is what MAKES it custom, so the bar has to follow the edit rather than the
  // last button pressed. Not setThemeEdit: that re-renders every row, which would tear the open
  // colour picker out from under the pointer mid-drag. syncThemeSeg only repaints the segments.
  syncThemeSeg();
});
document.querySelectorAll('#themeSeg button').forEach(b => b.addEventListener('click', () => {
  const want = b.dataset.theme;
  if (want === themeMode(_themeEdit)) return;    // already true; a segment is not a re-apply button
  // Bank what you built BEFORE leaving it, or previewing a preset would throw it away. syncThemeSeg
  // keeps _themeCustom current while you are in Custom, so by here it is already the right set.
  if (want === 'custom') { if (_themeCustom) setThemeEdit(_themeCustom); return; }
  setThemeEdit(want === 'light' ? THEME_LIGHT : {});   // dark = no overrides = the CSS defaults
}));
// BOTH TAKE THE LIBRARY AS AN ARGUMENT, and that is the whole fix. They used to be Settings buttons
// with no library to name, so the server fell back to its invisible "management target" -- a pointer
// set by adding a library or opening its menu, which nothing on screen shows and nobody chooses.
// Two IRREVERSIBLE actions were clearing a library the user had never picked. The author, 2026-09-08:
// "There is no 'active root' - so what is going on?"
// They live in that library's own menu now, so the library IS the click, and each confirm says its
// NAME instead of a phrase that named nothing the user has.
async function clearScoresLib(key) {
  if (!key) return;
  if (!(await uiConfirm(`Clear Quality scores in “${libName(key)}”?

` +
                        "Images, tags and favorites are kept. This can't be undone.",
                        { ok: 'Clear', danger: true }))) return;
  const j = await postJSON('/api/scores/clear', { what: 'metric', key });
  if (j.error) { uiAlert('Clear failed: ' + j.error); return; }
  toast('Quality scores cleared');
  await refreshView();   // badges + counts refresh
}

// Infinite scroll. Requires items already on screen: on a cold start the grid is empty, so the
// sentinel sits in the viewport from the first frame and this fired immediately — a duplicate
// offset-0 fetch racing the initial search, and its "loading more" spinner orbiting the big boot
// one. There is nothing to paginate from until a page has actually landed.
const io = new IntersectionObserver(entries => {
  if (entries[0].isIntersecting && state.items.length) search(false);
}, { rootMargin: '600px' });
io.observe($('#sentinel'));

// ---- where the window is, so it opens there again --------------------------------------------
// The app remembers its OWN geometry rather than leaving it to the browser. Chromium does remember
// an --app window's bounds, but keys them to an app identity derived from the launch URL — so
// anything that changes that URL between opening and closing loses the position. The boot splash
// did exactly that and the window reopened full-height at the left of the screen every time; the
// splash was reverted, and this stayed, because it is the thing that does not depend on Chromium
// behaving.
//
// Doing it here rather than coaxing Chromium's memory is deliberate: it works whatever the browser
// does, and it can be TESTED — checking the browser's own behaviour would mean opening desktop
// windows, which is a standing no.
//
// THE TRAP, and the reason this is not four lines: --window-position takes the window FRAME's
// top-left, while screenX/screenY report the VIEWPORT's. Store one and restore it as the other and
// the window drops by the title-bar height EVERY session — a window that creeps down the screen
// until it walks off the bottom. It looks perfect for the first launch or two, which is exactly why
// it needs a round-trip test rather than a look.
function windowRect() {
  // Frame decoration: side borders split the horizontal difference; the rest of the vertical
  // difference is the title bar. On a Chromium --app window on Win11 that is ~0 and ~39.
  const borderX = Math.max(0, Math.round((window.outerWidth - window.innerWidth) / 2));
  const titleBar = Math.max(0, Math.round(window.outerHeight - window.innerHeight - borderX));
  return {
    x: Math.round(window.screenX - borderX),
    y: Math.round(window.screenY - titleBar),
    w: Math.round(window.outerWidth),
    h: Math.round(window.outerHeight),
  };
}
// Minimised or otherwise nonsense geometry must never be stored — restoring a 0x0 window at
// (-32000, -32000) would lose the app entirely, and the user's only clue would be that it stopped
// opening. Bounds are generous: the point is to reject the absurd, not to police window sizes.
function rectIsSane(r) {
  if (!r) return false;            // a guard, so this returns a BOOLEAN rather than the falsy input
  return Number.isFinite(r.x) && Number.isFinite(r.y)
    && r.w >= 400 && r.h >= 300 && r.w <= 20000 && r.h <= 20000
    && r.x > -20000 && r.y > -20000 && r.x < 20000 && r.y < 20000;
}
function saveWindowRect(leaving) {
  const r = windowRect();
  if (!rectIsSane(r)) return;
  const body = JSON.stringify(r);
  // On the way out a normal fetch is killed with the page. sendBeacon survives it; keepalive below
  // is the fallback for the same reason.
  if (leaving && navigator.sendBeacon
      && navigator.sendBeacon('/api/window', new Blob([body], { type: 'application/json' }))) return;
  return fetch('/api/window', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body, keepalive: true }).catch(() => {});
}

// ---- telling the server this window is still here -----------------------------------------------
// There is no power button any more: the server stops when these stop arriving. The rules and the
// arithmetic live in server.py's client watchdog — read that, not this. The one thing that has to
// hold HERE is that CLIENT_BEAT_MS stays well inside the server's CLIENT_GRACE_S, because a second
// tab closing arms that countdown and this beat is what cancels it.
const CLIENT_BEAT_MS = 5000;
// THE BEAT IS ALSO THE DETECTOR. Every control in this app opens by asking the server something --
// Settings starts by fetching your settings and simply never opens if that throws -- so once the
// server goes, they all fail the same silent way and the app reads as frozen rather than gone.
// The author hit exactly that: clicked Settings, nothing happened. Nothing new polls for this; the beat
// was already running and already throwing its failures away.
//
// TWO failures, not one: a fetch only rejects when the connection is refused, but update.bat
// restarts the server under an open window, and one missed beat there is a blip, not a death.
//
// NO RELOAD WHEN IT COMES BACK, deliberately. start.bat launches a browser window unconditionally,
// and Chromium's --app makes a new one every time -- so the server returning means a fresh window
// already exists, and reviving this one would leave the author with two live copies to sort out. This
// window's whole remaining job is to explain itself and get out of the way.
let _beatFails = 0, _goneShown = false;
function clientAlive() {
  fetch('/api/alive', { method: 'POST' })
    .then(() => { _beatFails = 0; if (_goneShown) serverIsBack(); })
    .catch(() => { if (++_beatFails >= 2) showGone(); });
}
clientAlive();
setInterval(clientAlive, CLIENT_BEAT_MS);

function showGone() {
  if (_goneShown) return;
  _goneShown = true;
  $('#goneMsg').textContent =
    'Nothing was lost — your library and everything you marked are on disk. '
    + 'Run start.bat to open it again.';
  $('#gone').classList.remove('hidden');
}
function serverIsBack() {
  $('#goneTitle').textContent = 'VV Curator is running again';
  $('#goneMsg').textContent = 'It opened in a new window. This one is out of date — close it.';
}
// CLOSE HAS TO PROVE ITSELF. A page cannot close a window a script did not open; an --app window is
// usually the exception, but that is a browser behaviour we do not control and cannot test from
// here. So it tries, and if the window is still standing a moment later it says what to do by hand
// -- which is the same fault we are fixing, and it is not allowed to reappear in the fix.
$('#goneClose').addEventListener('click', e => {
  const btn = e.currentTarget;
  window.close();
  setTimeout(() => {
    btn.disabled = true;
    btn.textContent = 'Close it from the title bar';
  }, 500);
});

window.addEventListener('pagehide', () => {
  saveWindowRect(true);
  // Arms the server's countdown — it does NOT stop it. This fires on F5 exactly as it does on a
  // real close, and the two only become distinguishable a second later, when a reloaded page either
  // does or does not check back in. sendBeacon for the same reason saveWindowRect uses it: a normal
  // fetch is killed with the page.
  if (!(navigator.sendBeacon && navigator.sendBeacon('/api/bye', new Blob([], { type: 'application/json' }))))
    fetch('/api/bye', { method: 'POST', keepalive: true }).catch(() => {});
});

// Back to top. Lives as its own sticky row at the foot of the sidebar rather than floating over the
// grid: the top-right corner is where every card's ⛶ sits and where the selection bar sticks, so an
// overlay there would fight both. Hidden until there's something to go back up to.
const BACKTOP_AT = 400;   // px scrolled before it's worth offering
(function initBackTop() {
  const btn = $('#backTop'); if (!btn) return;
  // Disabled, not hidden: it lives in the anchored viewport row, which must not reflow the moment
  // you start scrolling, and a greyed control is easier to find later than one that appears.
  // Also where the scroll metric is fed, so the app keeps exactly one scroll listener (see
  // ScrollMetric). It no-ops entirely unless a trace is recording.
  const sync = () => { btn.disabled = window.scrollY <= BACKTOP_AT; ScrollMetric.onScroll(); };
  window.addEventListener('scroll', sync, { passive: true });
  btn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  sync();
})();

// ---- resizable sidebar (drag the divider; width persisted) ----
(function initSidebarResize() {
  const KEY = 'sidebarWidth', MIN = 200, MAX = 640;
  const setW = (w) => document.documentElement.style.setProperty('--sidebar-w', w + 'px');
  const saved = parseInt(localStorage.getItem(KEY), 10);
  if (saved >= MIN && saved <= MAX) setW(saved);
  const rz = $('#resizer');
  if (!rz) return;
  let w = saved || 420;
  const onMove = (e) => { w = Math.min(MAX, Math.max(MIN, e.clientX)); setW(w); };
  const onUp = () => {
    document.body.classList.remove('resizing');
    localStorage.setItem(KEY, w);
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  };
  rz.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    document.body.classList.add('resizing');
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  });
  // double-click the divider to reset to the default width
  rz.addEventListener('dblclick', () => { w = 280; setW(w); localStorage.setItem(KEY, w); });
})();

// ---- boot ----
async function boot() {
  // Everything up to the first search runs inside Busy.during, whose release is in a finally: a
  // throw in any of it (an unreachable share, a config the server can't read) used to leave the app
  // blurred and click-blocked with nothing said and no way back.
  let cfg;
  await Busy.during('rail', async () => {
    cfg = await loadRoots();
    state.theme = cfg.theme || {}; applyTheme(state.theme);   // apply the saved color theme on startup
    // BEFORE the first search paints, or the very first grid would draw the default facts and then
    // silently disagree with Settings until something re-rendered it.
    state.cards = cfg.cards || null;
    state.autoplay = !!(cfg.general && cfg.general.autoplay);  // start videos playing when opened
    state.keepBehavior = (cfg.general && cfg.general.keep_behavior) || 'next';   // post-set-keep: advance vs stay
    // Absent means ON. A config.json written before this setting existed must not read as "off" —
    // a new setting may never silently switch a safeguard off on an existing install.
    state.confirmRecycle = !(cfg.general && cfg.general.confirm_recycle === false);
    applyShowSnapshots(!(cfg.general && cfg.general.show_snapshots === false));   // absent means ON
    state.updateCheck = !(cfg.general && cfg.general.update_check === false);     // absent means ON
    const savedFilters = loadSavedFilters(state.activeRoot);
    restoreRootsSel(savedFilters);   // the library SCOPE — before applyFilters, which never touches it
    applyFilters(savedFilters);      // restore the saved filters on startup
    restoreLoadedSnapshot();   // and the snapshot they came from, if they still match it
    _appVersion = cfg.version || '';
    // NOT AWAITED: boot must not wait on someone else's server, and nothing downstream of here
    // needs the answer. It paints an icon whenever it arrives, or never.
    //
    // GATED ON HAVING BEEN ASKED, read from this payload rather than from state so it cannot
    // depend on whether loadRoots() happened to run first. A brand-new install has not seen the
    // first-run pop-up yet, and making the request before the question would make the tick-box a
    // formality. closeHelpHint() fires the check the moment they answer yes, so the first launch
    // is not a launch without the feature -- only a launch where the asking comes first.
    // Someone already past first run (every existing install) is covered by the README and the
    // Settings switch, which is the only honest answer for a question that can no longer be asked
    // at the right moment.
    if (cfg.seen_help_hint) checkForUpdate();
    // Apply app name (from API) to title and brand
    if (cfg.app_name) {
      _appName = cfg.app_name;
      // Update brand
      const brandEl = document.querySelector('[data-app-name]');
      // title too: the name ellipses in a narrow sidebar, yielding before the gear/power buttons
      if (brandEl) { brandEl.textContent = _appName; brandEl.title = _appName; }
    }
    // WHEN THE CODE YOU ARE RUNNING WAS LAST CHANGED. The question this answers is "did
    // update.bat actually pick that up?", asked after every update, and it was previously
    // answerable only by looking for the change and hoping you would recognise it.
    // Derived from the code files' timestamps, not a version string anyone maintains — see
    // build_stamp().
    //
    // A TOOLTIP ON THE BRAND, deliberately not a line in the rail. It shipped as a visible line
    // first and that was wrong: this is asked once, right after updating, and then ignored for
    // days — which is a tooltip's job description. Permanent space in the rail is for things you
    // read, and the rail's own rule is that a control only draws itself when it is doing
    // something. It rides the title the brand already carries for its ellipsed name, so it costs
    // no element and no style.
    if (cfg.build) {
      _build = cfg.build;
      // TWO QUESTIONS, AND THEY ARE NOT THE SAME ONE. The build stamp answers "did update.bat pick
      // that up?"; the VERSION answers "which release am I on?", which is the one a bug report and
      // the GitHub releases page both need, and it was answerable nowhere at all until now -- it
      // appeared only in the first-run greeting and the update pop-up, neither of which can be
      // summoned. The author, 2026-09-15, on the tooltip: "it may not match the new versioning for
      // github." It could not: one is a file timestamp, the other is APP_VERSION.
      // ONE STRING, BUILT ONCE, so the brand and the Settings header cannot drift about what you
      // are running.
      const verText = (_appVersion ? `${_appVersion} — ` : '') + `build ${cfg.build}`;
      const label = `${_appName || 'VV Curator'} ${verText}`;
      // AND A PLACE TO GO AND LOOK. The line in the RAIL was removed for being permanent space
      // given to something read once and then ignored for days, and that still holds -- but
      // Settings is a dialog you open on purpose, so the same fact costs nothing until it is
      // wanted. The tooltip stays; what it could not do is be read out when someone asks you for
      // your version, because you cannot hover on request.
      const verEl = document.getElementById('setVersion');
      if (verEl) verEl.textContent = _appVersion ? `Version ${verText}` : verText;
      // Both the row and the name: the row so the LOGO is a hover target too, the name because an
      // inner title wins over its parent's and the span already had one. Same text, so which one
      // the pointer lands on makes no difference. The gear and power buttons keep their own.
      const brandRow = document.querySelector('#sidebar .brand');
      if (brandRow) brandRow.title = label;
      const nameEl = document.querySelector('[data-app-name]');
      if (nameEl) nameEl.title = label;
      // Also into the trace: three times now I have had to work out which build a trace came from
      // by spotting which fixes its lines mention. The trace should simply say.
      Trace.add('build', cfg.build);
    }
    // Did the launcher's --window-position actually take? Chromium applies --window-size to this
    // window but appears to ignore --window-position for an --app window, so the app checks where
    // it ACTUALLY is against where it was told to be, says so in the trace, and moves itself if it
    // has to. Two guesses about this were wrong before it was instrumented; the trace line is the
    // point, and the move is the fix that may or may not be permitted.
    if (cfg.window && rectIsSane(cfg.window)) {
      const want = cfg.window, got = windowRect();
      Trace.add('window', `wanted ${want.x},${want.y} ${want.w}x${want.h} · `
        + `actually ${got.x},${got.y} ${got.w}x${got.h}`);
      // 8px of slack: frame metrics differ by a pixel or two between measuring and restoring, and
      // chasing that would move the window on every launch for no visible reason.
      if (Math.abs(got.x - want.x) > 8 || Math.abs(got.y - want.y) > 8) {
        try { window.moveTo(want.x, want.y); } catch (e) { /* not permitted; the trace will say */ }
        const now = windowRect();
        const moved = Math.abs(now.x - want.x) <= 8 && Math.abs(now.y - want.y) <= 8;
        Trace.add('window', moved
          ? `moved itself into place · now ${now.x},${now.y}`
          : `REFUSED to move — the browser ignored moveTo, still ${now.x},${now.y}`);
      }
    }
    await refreshView();
  });
  // AFTER the first paint, never during it. A library already unreachable at launch gets the same
  // interruption as one that leaves mid-session -- The author's call, 2026-09-14, against staying silent
  // at startup: nothing else on screen explains why a third of the grid is missing, and the mark in
  // the Libraries strip only helps someone who already suspects the answer. The cost he accepted is
  // that a drive left unplugged for a week says this on every launch.
  // Not awaited: boot has work after this that a modal must not hold up.
  if (allRootKeys().some(isRootOffline)) offlineAlert();
  if (!cfg || !cfg.active) {
    // BEFORE the early return, and that is the whole bug: the title starts as the placeholder
    // "Loading..." (index.html) and every other path reaches updateTitleFromRoot on the way past.
    // This one returns first, so a first-time user read "Loading..." in the title bar and taskbar
    // for as long as they had no library -- an app that had finished starting saying it had not.
    // Reported 2026-09-07: "Why does the app bar read Loading...?"
    updateTitleFromRoot();
    showNoRootHint();                            // no root yet — guide, don't nag with a popup
    return;
  }
  // Reattach to whatever is already in flight. This is what makes the scan scrim honest across a
  // reload: watchScan re-raises it, so a refresh can't be used to escape the block.
  const s = await getJSON('/api/scan/status');
  if (s.running) { await watchScan('Building index'); await afterScanRefresh(false); }   // an index build was already going
  else {
    const rw = await getJSON('/api/reward/status');
    if (rw.running) { watchReward(); return; }    // reattach to an in-flight reward run
    const m = await getJSON('/api/miner/status');
    if (m.running) { $('#minerModal').classList.remove('hidden'); watchMine(); return; }
    // With the timer on, launching IS the instruction to catch up: act on the change rather than
    // flagging it and waiting out the idle gate. Off, it stays a flag — that's the manual workflow.
    // The probe stamp matters: this call bypasses the 20s throttle without recording itself, so the
    // first filter change after launch used to fire a second, pointless /api/changes.
    if (_autoOn) autoRefreshTick('force');
    else { checkChanges(); _lastChangeProbe = Date.now(); }
    // AFTER the reattach branches, never before, and only on the branch where nothing is running:
    // asking someone to start a ten-minute job while a scan is already going would be a question
    // they cannot act on. A job in flight means the offer waits for the next launch, which is
    // exactly what "Do it later" does anyway.
    offerCatchUp();
  }
  // Restore dynamic title (e.g., "VV Curator — MyRoot") after boot
  updateTitleFromRoot();
  // Deferred, not awaited: this reads a directory that may be on a network share, and nothing about
  // the app is waiting for it.
  RecycleDebt.soon();
}
boot();

// Dynamic title: "AppName — RootName"
function updateTitleFromRoot() {
  const titleEl = document.querySelector('title[data-title-base]');
  if (!titleEl) return;
  // GATED ON THE NAME, NOT ON THE SUFFIX. The suffix is empty now that the title is just the app
  // name, and the old `baseSuffix && _appName` would have made this a no-op -- leaving whatever
  // index.html shipped, which is exactly the stuck-title bug this function was written to fix.
  const baseSuffix = titleEl.dataset.titleBase || '';
  if (_appName) {
    document.title = _appName + baseSuffix;
  }
}
