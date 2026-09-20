"""test_aspect_filter.py -- the Shape filter carves the library into three, and square has a band.

Run: python tests/test_aspect_filter.py

Shape (portrait / landscape / square) reads `width` and `height`, which are already on every image
and every video row -- no new column, no index, no backfill. What is worth pinning is not that it
works but the two things about it that are easy to break while looking correct:

  * SQUARE IS A TOLERANCE. The author's call, 2026-09-16: an upscale or a crop lands a pixel or two
    off a round number, and a 1024x1026 render filed under Landscape reads as a bug rather than as
    precision. The band is 2% OF THE LONGER SIDE, written `ABS(w-h) * 50 <= MAX(w,h)` so it needs no
    floating point. That expression is the kind a later pass "tidies" into `* 0.02` or into a
    percentage of the SHORTER side, either of which changes which pictures are square without
    changing anything that looks wrong in the diff.

  * THE THREE ARE A PARTITION. Portrait and landscape are defined AGAINST the square band, not
    against each other -- `h > w AND NOT square`. Drop that `NOT` and the bands overlap: a
    1024x1030 picture is both square and portrait, so the three counts stop summing to the whole and
    a card appears under two mutually exclusive filters. Nothing in the UI would show it.

A NULL dimension fails every comparison, which is how songs stay out without being named: a song has
no picture, so it has no shape. This asserts that too, because "exclude audio" is the kind of thing
someone later adds an explicit `ext NOT IN (...)` for, and a second definition of the same rule is
how two definitions drift.

The SQL is read OUT OF server.py rather than restated here -- a copy of the expression would pass
this test forever while the app used something else.
"""
import io
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRV = io.open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()

fail = []

# Lift the app's own square expression. If this stops matching, the filter has been rewritten and
# this test must be re-read rather than re-pointed.
m = re.search(r'sq = "([^"]+)"', SRV)
if not m:
    fail.append('could not find the square expression in server.py (`sq = "..."`)')
    SQ = None
else:
    SQ = m.group(1).replace('i.width', 'w').replace('i.height', 'h')

# And the three clauses, so the NOT-square guard cannot quietly leave the portrait/landscape arms.
for arm in ('portrait', 'landscape'):
    pat = re.compile(r"aspect == 'portrait'" if arm == 'portrait' else r'else:\s*\n\s*where\.append\("i\.width')
    if arm == 'portrait':
        seg = re.search(r"elif aspect == 'portrait':(.*?)else:", SRV, re.S)
    else:
        seg = re.search(r"elif aspect == 'portrait':.*?else:(.*?)\n\s*#|\Z", SRV, re.S)
    if not seg or 'NOT " + sq' not in seg.group(1):
        fail.append('the %s arm no longer excludes the square band -- the three overlap, so a '
                    'near-square picture answers to two filters at once' % arm)

if SQ:
    con = sqlite3.connect(':memory:')

    def verdict(w, h):
        q = ("SELECT CASE WHEN w IS NULL OR h IS NULL OR w<=0 OR h<=0 THEN 'none' "
             "WHEN {sq} THEN 'square' WHEN h > w THEN 'portrait' ELSE 'landscape' END "
             "FROM (SELECT ? AS w, ? AS h)").format(sq=SQ)
        return con.execute(q, (w, h)).fetchone()[0]

    # name, w, h, expected
    CASES = [
        ('an exact square',                1024, 1024, 'square'),
        ('one pixel off',                  1024, 1025, 'square'),
        ('inside the band (1.96%)',        1024, 1044, 'square'),
        ('outside the band (2.05%)',       1024, 1045, 'portrait'),
        ('outside, the other way',         1045, 1024, 'landscape'),
        ('a real portrait render',          832, 1216, 'portrait'),
        ('a real landscape render',        1216,  832, 'landscape'),
        # The band is a share of the longer side, so it does NOT widen with the picture: 96px is
        # inside it at 4096 and far outside it at 1024. A percentage of the SHORTER side, or a flat
        # pixel count, would get one of these two wrong.
        ('96px apart at 4096 (2.3%)',      4096, 4000, 'landscape'),
        ('60px apart at 4096 (1.5%)',      4096, 4036, 'square'),
        # THE CASE THAT SEPARATES MAX FROM MIN, and it had to be built on purpose: for anything
        # nearly square the two sides are within a whisker of each other, so a band measured off the
        # SHORTER one gives the same answer as this and every realistic case above passes either
        # way. They diverge only where the gap is about 1/50th of the sides, which is exactly here:
        # 100-98 is 2.00% of 100 and 2.04% of 98, so this is square by the rule we want and portrait
        # by the rule we do not. Found by mutation -- the first version of this list asserted it was
        # covered and was not.
        ('2.00% of the longer side',         98,  100, 'square'),
        ('a song, which has no picture',   None, None, 'none'),
        ('a row with dimensions missing',     0,    0, 'none'),
    ]
    for name, w, h, want in CASES:
        got = verdict(w, h)
        if got != want:
            fail.append('%s (%sx%s) is %s, expected %s' % (name, w, h, got, want))

    # THE PARTITION, stated as a sum rather than as three separate counts: every shaped row lands in
    # exactly one band, and nothing shaped is lost between them.
    rows = [(1024, 1024), (1024, 1044), (832, 1216), (1216, 832), (4096, 4000),
            (640, 384), (2559, 1523), (None, None), (0, 0)]
    seen = [verdict(w, h) for w, h in rows]
    shaped = [v for v in seen if v != 'none']
    if len(shaped) != 7:
        fail.append('%d of 9 rows came back shaped, expected 7 (two have no dimensions)' % len(shaped))
    if sorted(set(shaped)) != ['landscape', 'portrait', 'square']:
        fail.append('the shaped rows did not cover all three bands: %r' % sorted(set(shaped)))

print('\nShape filter: three bands, and square has a tolerance\n')
if fail:
    print('FAIL:')
    for f in fail:
        print('  - ' + f)
    sys.exit(1)
print('ok: the 2% band holds at both edges, the three bands partition, and a row with no '
      'dimensions lands in none of them')
