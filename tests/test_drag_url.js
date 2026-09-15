/* Regression test for the URL a grid card is swapped to before it can be dragged
 * (originalUrlFor in app.js).
 *
 * Run: node test_drag_url.js
 *
 * Two things ride on this one string, and they fail in opposite directions.
 *
 * Get the NAME wrong and a drop into ComfyUI arrives as `28442.png` — the row number — which is
 * what the browser derives from a URL ending in the id. That was the reported bug: a workflow
 * reopened later shows a red Load Image node, because nothing about that name says what it was.
 *
 * Get the QUERY wrong and it is far worse and much quieter: ?v= and &r= are what keep one
 * library's picture out of another's 24h cache. Rowids are per-root, so dropping &r= serves the
 * wrong library's image and nothing complains. So the name must be added WITHOUT disturbing them.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');
const a = src.indexOf('function originalUrlFor(');
const end = src.indexOf('\n}', a);
if (a < 0 || end < 0) throw new Error('could not find originalUrlFor in app/app.js');
const { originalUrlFor } = new Function(src.slice(a, end + 2) + '\nreturn { originalUrlFor };')();

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

console.log('\nDrag URL: the original, under its own name\n');

const THUMB = '/thumb/28442?v=1700000000&r=libA&s=512';

// 1. The point of the change: the last path segment is the filename, not the id.
{
  const u = originalUrlFor(THUMB, 'VV_00123_krea_x2.png');
  check('points at the original, not the thumbnail', u.indexOf('/file/') === 0, u);
  const lastSeg = u.split('?')[0].split('/').pop();
  check('  and the browser would name the drop after the file', lastSeg === 'VV_00123_krea_x2.png', lastSeg);
  check('  not after the row number', lastSeg !== '28442', lastSeg);
}

// 2. THE QUIET ONE. Root scoping and versioning must survive untouched.
{
  const u = originalUrlFor(THUMB, 'a.png');
  check('keeps ?v= (the cache-buster)', /[?&]v=1700000000\b/.test(u), u);
  check('keeps &r= (the root scope)', /[?&]r=libA\b/.test(u), u);
  check('  and the query still starts at the first ?', u.split('?').length === 2, u);
  check('  with the name BEFORE the query, not inside it',
    u === '/file/28442/a.png?v=1700000000&r=libA&s=512', u);
}

// 3. No name known: fall back to exactly the old URL rather than inventing a segment.
{
  const u = originalUrlFor(THUMB, '');
  check('an unnamed item yields the plain /file/ URL',
    u === '/file/28442?v=1700000000&r=libA&s=512', u);
  check('  and undefined behaves the same as empty',
    originalUrlFor(THUMB, undefined) === u, originalUrlFor(THUMB, undefined));
}

// 4. Awkward names must not break the URL. A space, a #, a ? or a / in a filename would each end
//    the path early or start a bogus query — i.e. fetch the wrong thing, or nothing.
{
  const u = originalUrlFor(THUMB, 'my pic #2 (final).png');
  check('a space/# name stays one path segment', u.split('?').length === 2, u);
  check('  and contains no raw space or #', !/[ #]/.test(u), u);
  const u2 = originalUrlFor(THUMB, '../../etc/passwd.png');
  check('a path-traversal name cannot add segments',
    u2.split('?')[0].split('/').length === 4, u2);
  const u3 = originalUrlFor(THUMB, 'a?b=c.png');
  check('a name with ? does not start a second query', u3.split('?').length === 2, u3);
}

// 5. A VIDEO asks for a different thing entirely: a PICTURE of its first frame carrying its own
//    workflow. Not a nicety — a browser only carries a file between windows when that file is the
//    one behind the dragged <img>, so a file attached by script arrives empty in ComfyUI. Dragging
//    a picture is the only route that works, so a video has to become one.
{
  const u = originalUrlFor(THUMB, 'MM_T2V_00042_clip.mp4', 'video');
  check('a video asks for /dragpng/, not /file/', u.indexOf('/dragpng/') === 0, u);
  const lastSeg = u.split('?')[0].split('/').pop();
  check('  and the drop is named .png, because that is what it IS',
    lastSeg === 'MM_T2V_00042_clip.png', lastSeg);
  check('  not .mp4 — ComfyUI would take the name at face value', !/\.mp4/.test(u), u);
  check('  root scoping survives here too', /[?&]v=1700000000\b/.test(u) && /[?&]r=libA\b/.test(u), u);
  // Only when it IS a video. A picture must never be routed through the frame-grabber.
  check('a picture is unaffected by the video route',
    originalUrlFor(THUMB, 'a.png') === originalUrlFor(THUMB, 'a.png', 'image'),
    originalUrlFor(THUMB, 'a.png', 'image'));
  check('  and still goes to /file/', originalUrlFor(THUMB, 'a.png', 'image').indexOf('/file/') === 0);
  // A video whose name is unknown still routes correctly, just unnamed.
  check('an unnamed video still asks for /dragpng/',
    originalUrlFor(THUMB, '', 'video') === '/dragpng/28442?v=1700000000&r=libA&s=512',
    originalUrlFor(THUMB, '', 'video'));
  // A name with no extension must gain .png rather than losing its identity.
  check('an extensionless name gains .png',
    originalUrlFor(THUMB, 'clip', 'video').split('?')[0].endsWith('/clip.png'),
    originalUrlFor(THUMB, 'clip', 'video'));
}

// 6. The caller's guard, which is what stops a card being upgraded twice into
//    /file/28442/a.png/a.png. It lives in gridUpgrade, not here, so pin it there.
{
  const g = src.slice(src.indexOf('function gridUpgrade('), src.indexOf('function gridDraggableImg('));
  check('gridUpgrade still refuses a URL that is not a thumbnail',
    /indexOf\('\/thumb\/'\)\s*<\s*0\)\s*return/.test(g),
    'the guard that keeps a card from being upgraded twice is gone');
}

console.log('\n' + (failures ? failures + ' FAILED' : 'all passed') + '\n');
process.exit(failures ? 1 : 0);
