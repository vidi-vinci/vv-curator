/* Regression test for which thumbnail size the client ASKS for (thumbSizeFor / thumbAt in app.js).
 *
 * Run: node test_thumb_pick.js
 *
 * Small function, disproportionate consequence. Serving fewer pixels than a card draws is the one
 * change here anyone would actually see — every thumbnail in the grid goes soft — and it would look
 * like a rendering bug rather than a sizing one. So the rule under test is: NEVER round down, and
 * always account for display scaling, where a 128px card is 160 or 256 real pixels.
 *
 * Extracted from app/app.js rather than copied, so the two cannot drift.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'app', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');
const a = src.indexOf('const THUMB_SIZES = [');
const end = src.indexOf('\n}', src.indexOf('function thumbAt('));
if (a < 0 || end < 0) throw new Error('could not find the thumb-size helpers in app/app.js');
const BODY = src.slice(a, end + 2);

function build(dpr) {
  return new Function('window', BODY + '\nreturn { thumbSizeFor, thumbAt };')({ devicePixelRatio: dpr });
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok    ' + name);
  else { console.log('  FAIL  ' + name + (detail ? '\n          ' + detail : '')); failures++; }
}

console.log('\nThumbnail size choice\n');

// 1. Unscaled display: ask for exactly what the card draws.
{
  const { thumbSizeFor } = build(1);
  for (const [card, want] of [[128, 128], [192, 192], [256, 256], [512, 512]]) {
    check(`a ${card}px card at 1x asks for ${want}px`, thumbSizeFor(card) === want,
      'got ' + thumbSizeFor(card));
  }
}

// 2. THE ONE THAT MATTERS. On a scaled display the card needs more real pixels than its CSS size,
//    and rounding down here is what would make every thumbnail in the grid look soft.
{
  const { thumbSizeFor } = build(1.5);
  check('a 128px card at 1.5x asks for 192px, not 128', thumbSizeFor(128) === 192,
    'got ' + thumbSizeFor(128));
  // 192 * 1.5 = 288 real pixels, and the sizes go 256 -> 512 with nothing between. 512 is the
  // right answer and 256 would be the bug: it is BELOW what the card draws. Written down because
  // 256 is the intuitive expectation, and it is wrong — the gap in the size ladder is wide at the
  // top, so a scaled display can jump two steps. The cost of being wrong in this direction is a
  // bigger download; the cost of the other direction is a grid full of soft pictures.
  check('a 192px card at 1.5x needs 288px, so it asks for 512 rather than round down',
    thumbSizeFor(192) === 512, 'got ' + thumbSizeFor(192));
}
{
  const { thumbSizeFor } = build(2);
  check('a 128px card at 2x asks for 256px', thumbSizeFor(128) === 256, 'got ' + thumbSizeFor(128));
  check('a 256px card at 2x asks for 512px', thumbSizeFor(256) === 512, 'got ' + thumbSizeFor(256));
}

// 3. Never ask for more than exists, however extreme the display.
{
  const { thumbSizeFor } = build(4);
  check('a 512px card at 4x clamps to the largest there is', thumbSizeFor(512) === 512,
    'got ' + thumbSizeFor(512));
}

// 4. Never round DOWN at an in-between size — the general form of case 2.
{
  const { thumbSizeFor } = build(1);
  check('an odd 200px card rounds UP to 256', thumbSizeFor(200) === 256, 'got ' + thumbSizeFor(200));
  check('  and 129px rounds up to 192', thumbSizeFor(129) === 192, 'got ' + thumbSizeFor(129));
}

// 5. The rewrite must replace the size and disturb nothing else — v= and r= are the cache key and
//    the library scope, and losing either serves the wrong image entirely (see the root-scoping bug).
{
  const { thumbAt } = build(1);
  const url = '/thumb/4821?v=1699887654&r=abc123&s=512';
  const out = thumbAt(url, 128);
  check('rewrites only the size', out === '/thumb/4821?v=1699887654&r=abc123&s=128', out);
  check('  keeps the cache-buster', /[?&]v=1699887654(&|$)/.test(out), out);
  check('  keeps the library scope', /[?&]r=abc123(&|$)/.test(out), out);
  check('  and the image id', out.startsWith('/thumb/4821?'), out);

  // s= first, and an absolute URL as the browser reports it from img.src
  check('handles s= in first position',
    thumbAt('/thumb/9?s=512&v=2&r=z', 256) === '/thumb/9?s=256&v=2&r=z',
    thumbAt('/thumb/9?s=512&v=2&r=z', 256));
  check('handles an absolute url',
    thumbAt('http://127.0.0.1:8770/thumb/9?v=2&r=z&s=512', 128)
      === 'http://127.0.0.1:8770/thumb/9?v=2&r=z&s=128');

  // A url with no s= is left alone rather than corrupted; the server defaults it.
  check('leaves a url without s= untouched',
    thumbAt('/thumb/9?v=2&r=z', 128) === '/thumb/9?v=2&r=z', thumbAt('/thumb/9?v=2&r=z', 128));
  check('survives an empty url', thumbAt('', 128) === '' && thumbAt(null, 128) === '');
}

console.log(failures ? `\n${failures} FAILED\n` : '\nall passed\n');
process.exit(failures ? 1 : 0);
