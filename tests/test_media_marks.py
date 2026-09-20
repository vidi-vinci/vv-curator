"""Regression test: a mark on a picture carries its own ground.

Run:  python test_media_marks.py

THE RULE, and it took a day of getting it wrong to state properly: a mark laid over a picture cannot
rely on what is behind it, because a photograph follows nobody and the letterbox beside it follows
the theme. So it must bring its OWN ground -- a scrim, not a hairline -- and once it does, that
ground is free to follow the theme, because the mark is no longer depending on the surface under it.
What must stay absolute is anything with no ground of its own: the star, which has only a rim, and
the Quality badge's gold, which is the same in both presets and so needs an ink computed from
itself rather than from the page.

Everything below was found on 2026-09-15, when the light preset was looked at properly for the
first time:

  * A WHITE EDGE RING, 1.5px at 50%, went round every badge. On the light preset's near-white
    letterbox bars it simply vanished, taking the old set mark -- whose legibility rested entirely
    on it -- with it. Pinning the bars dark was tried and rejected ("the dark letterbox just looks
    like a broken light mode"), and the measurement agreed: against near-black the badge separated
    from its ground at 1.05:1, worse than the 5.62:1 it gets against a near-white bar. The ring was
    the fault, not the ground. A bare scrim badge does not care what is behind it.
  * THE SET MARK was three 78%-black squares overlapping on a diagonal, so where two met the alpha
    compounded to 95%. On a near-black letterbox both are just black; on a near-white one they are
    #363738 and #0c0c0c, which is three densities and six edges inside 16 pixels.
  * THE QUALITY BADGE's ink was var(--bg) on a gold that is the same in both themes: 9.14:1 in dark
    and 1.85:1 in light. A themed ink on an unthemed ground, and nothing was measuring it.
  * THE STAR was darkened to #d99a1a for the light preset, which helped it on a pale PAGE and hurt
    it in the corner of every picture, where it actually lives.

Each of those looks perfectly reasonable in the file. What makes them wrong is only visible when you
ask which SURFACE the value lands on, and that question is what this test asks on your behalf.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = io.open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()
JS = io.open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()
SRC = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), CSS, flags=re.S)

# Tokens the light preset redefines. Anything here is THEMED by definition, so the test does not
# need its own list and cannot drift from the app's.
m = re.search(r'const THEME_LIGHT = \{(.*?)\n\};', JS, re.S)
THEMED = set(re.findall(r"'(--[\w-]+)'", m.group(1))) if m else set()

fail = []
if not THEMED:
    fail.append('could not read THEME_LIGHT from app.js')

# 1. NO LIGHT EDGE RING on anything that sits over a picture. This is the one that actually broke:
#    a pale hairline is invisible on a pale ground, so a badge relying on it disappears exactly
#    where the letterbox is brightest. The scrim carries the badge instead, at any ground.
for sel in ('.card .motion-badge', '.card-br .note-badge', '.set-badge', '.card .zoom',
            '.strip-item .strip-play'):
    for r in re.finditer(re.escape(sel) + r'[^{}]*\{([^{}]*)\}', SRC):
        body = r.group(1)
        sh = re.search(r'box-shadow\s*:\s*([^;]+)', body)
        if sh and re.search(r'--edge|rgba\(\s*255', sh.group(1)):
            fail.append('%s carries a light edge ring again (%s) -- it vanishes on a pale '
                        'letterbox, which is what broke the light preset' % (sel, sh.group(1).strip()))

# 1b. THE CARD'S CHROME USES THE CARD TOKENS. The furniture laid over a picture -- facts bar,
#     caption, badges, zoom, selection check, the idle star -- was authored as a black scrim with
#     white ink, which recedes on a dark UI and turns into high-contrast dark blocks on a light one:
#     "it works, but it still feels far more busy to me than the dark mode." The --card-* pair is
#     theme-aware via light-dark(), so reaching for the fixed --scrim / --on-accent / --cap-fg /
#     --edge here puts one piece of furniture back on the wrong side of the switch, and a single
#     loud badge among quiet ones is worse than all of them being loud.
FIXED = ('--scrim', '--scrim-soft', '--scrim-strong', '--on-accent', '--cap-fg', '--edge', '--edge-soft')
CHROME = ('.card .card-facts', '.card .cap', '.card .zoom', '.card .check', '.card .motion-badge',
          '.note-badge', '.set-badge', '.strip-item .strip-play', '.set-pane .set-key')
for sel in CHROME:
    for r in re.finditer(r'(?:^|,\s*)' + re.escape(sel) + r'[^{},]*\{([^{}]*)\}', SRC, re.M):
        body = r.group(1)
        for tok in FIXED:
            # --accent-fill grounds keep --on-accent: that is an accent state, not card chrome.
            if re.search(r'var\(' + re.escape(tok) + r'\)', body) and 'accent-fill' not in body:
                fail.append('%s uses %s -- card chrome takes the theme-aware --card-* tokens'
                            % (sel, tok))

# 2. The set mark. ONE translucent layer is the shared badge recipe and is fine -- the fault was
#    STACKED translucency, three 78%-black leaves whose overlaps composited to 95%. So what this
#    pins is that the mark is drawn by the shared recipe (a single scrim pill carrying one --ico)
#    rather than as a pile of positioned shapes, which is the shape the bug had.
setb = re.findall(r'\.set-badge[^{}]*\{([^{}]*)\}', SRC)
if not setb:
    fail.append('.set-badge is gone')
else:
    body = ' '.join(setb)
    if '--ico' not in body:
        fail.append('.set-badge no longer draws an --ico -- it is back to being drawn by hand')
    stacked = len(re.findall(r'position\s*:\s*absolute', body))
    if stacked:
        fail.append('.set-badge positions %d shape(s) absolutely -- overlapping leaves are what '
                    'compounded into three densities' % stacked)
    # The glyph must come from the vendored set, not a retyped path.
    ico = re.search(r'--ico:\s*var\((--icon-[\w-]+)\)', body)
    if ico:
        name = ico.group(1).replace('--icon-', '')
        if not os.path.exists(os.path.join(ROOT, 'app', 'vendor', 'lucide', name + '.svg')):
            fail.append('--icon-%s has no vendored source at app/vendor/lucide/%s.svg' % (name, name))

# 3. The quality badge's ink must be computed, never a themed token.
rf = re.search(r'--reward-fg:\s*([^;]+);', SRC)
if not rf:
    fail.append('--reward-fg is gone')
else:
    for t in re.findall(r'var\((--[\w-]+)', rf.group(1)):
        if t in THEMED:
            fail.append('--reward-fg reads %s, which flips with the theme, onto a gold that does not' % t)
    if 'ink' not in rf.group(1):
        fail.append('--reward-fg is no longer a computed ink')
if "setProperty('--reward-bg-ink'" not in JS:
    fail.append('applyTheme no longer computes --reward-bg-ink')

# 4. The star lives in the corner of a picture, so its gold is absolute.
if '--star' in THEMED:
    fail.append("the light preset redefines --star; it lands on pictures, so it must not flip "
                "(--star-edge is what carries it on a themed surface)")

# 4b. THE UNFAVOURITED STAR IS ABSOLUTE FOR THE SAME REASON, and this is the half that was missed.
#     The gold was de-themed on 2026-09-15 and the hollow one was left on light-dark(): black at
#     .38 for the light preset, WHITE AT .5 for the dark one. On dark, over a pale thumbnail, that
#     composites to white on white -- 1:1 -- and the author lost one on 2026-09-16. The light half
#     held the mirrored fault over a dark thumbnail. A picture follows neither theme, so a mark with
#     no ground of its own cannot be authored per theme, whichever way round it is written.
es = re.search(r'--card-edge-soft:\s*([^;]+);', SRC)
if not es:
    fail.append('--card-edge-soft is gone -- it is the unfavourited star, which has no scrim')
elif 'light-dark' in es.group(1):
    fail.append('--card-edge-soft is themed again (%s) -- the idle star lands on a PICTURE, so it '
                'has to be one absolute value carried by --card-edge-rim' % es.group(1).strip())

# 4c. THE GOLD NEEDS ITS RIM. --star-edge is what carries the gold against a light ground (2.28:1
#     without it), so a .on state that stops declaring --star-rim is the 2026-09-15 bug returning.
#
#     THE IDLE STAR IS DELIBERATELY NOT IN THIS LIST, since 2026-09-18. It was, and the reasoning
#     was sound on paper -- a bare stroke is invisible against anything its own value, so give it a
#     rim too. In practice the rim is a second drop-shadow composited under the first, and two
#     blurs stack into a smudge that is on whenever the star is on. The author, comparing master
#     against the v1.1 he was testing: "there is NO way it was all fuzzy and black on just card
#     rollover." He chose 1.1's single soft shadow, having seen both.
#     So the pale-thumbnail case is open again and is on the list. What it needs is a CRISP edge,
#     not a second blur -- and pinning the old fix here would make putting a blur back the only way
#     to go green.
for sel, state in (('.card .star.on', 'gold'),
                   ('.strip-item .star.on', 'gold, in the filmstrip')):
    bodies = re.findall(r'(?:^|,\s*|\})\s*' + re.escape(sel) + r'\s*\{([^{}]*)\}', SRC, re.M)
    if not any('--star-rim' in b for b in bodies):
        fail.append('%s (%s) sets no --star-rim, so it falls back to transparent and the mark is '
                    'a bare stroke on an arbitrary picture' % (sel, state))

# 4d. THE FILMSTRIP DRAWS THE SAME STAR, so it needs the same filter. It did not: the card gained
#     the rim shadow on 2026-09-15 and the strip's copy kept only the dark drop-shadow, under a
#     comment claiming it was "the same contrast trick as the card's". Same mark, same item, two
#     renderings depending on which surface you were looking at -- the second card/strip drift after
#     .set-badge's. Compare the SHADOW COUNT rather than the text, so a reworded comment cannot
#     satisfy this and a real divergence cannot hide behind one.
def _shadows(sel):
    for b in re.findall(re.escape(sel) + r'\s+svg\s*\{([^{}]*)\}', SRC):
        f = re.search(r'filter\s*:\s*([^;]+)', b)
        if f:
            return f.group(1).count('drop-shadow'), ('--star-rim' in f.group(1))
    return None
card_sh, strip_sh = _shadows('.card .star'), _shadows('.strip-item .star')
if not card_sh or not strip_sh:
    fail.append('one of the two star surfaces no longer declares a filter on its svg')
elif card_sh != strip_sh:
    fail.append('the card star and the filmstrip star are drawn differently (card %r, strip %r) -- '
                'one mark, two surfaces, and they have drifted before' % (card_sh, strip_sh))
elif not card_sh[1]:
    fail.append('the star filter no longer composites --star-rim, so neither state has an edge')

# 5. ONE WEIGHT ACROSS THE ICON SET. Fill carries MEANING here, never emphasis: `--icon-star-on` is
#    filled because filled is what "favourited" means when you take the colour away, and it is the
#    outline half of a deliberate pair. Everything else is stroked. The play triangle was filled too
#    -- by media-player convention rather than by anything the app needed -- and sat in the same
#    badge cluster as the stroked set mark, one solid glyph against one drawn one. The author, on
#    seeing them side by side: "is the play arrow filled? that is another consistency we need to
#    align on." A new filled glyph almost always means someone reached for weight, which is what the
#    scrim and the size are for.
FILLED_BY_DESIGN = {'--icon-star-on'}
filled = set()
for m in re.finditer(r"(--icon-[\w-]+):\s*url\(\"data:image/svg\+xml,(.*?)\"\);", SRC, re.S):
    if re.search(r"fill='%23000'", m.group(2)):
        filled.add(m.group(1))
unexpected = filled - FILLED_BY_DESIGN
if unexpected:
    fail.append('filled icon(s) %s -- fill means state in this set, not emphasis'
                % ', '.join(sorted(unexpected)))
missing = FILLED_BY_DESIGN - filled
if missing:
    fail.append('%s is no longer filled -- that pair is how "favourited" reads without colour'
                % ', '.join(sorted(missing)))

# 6. THE CARD MARKS SCALE TOGETHER. --card-badge-h steps up on .grid.cards-lg / .cards-xl, and
#    everything at a card's foot derives from it -- but a custom property is substituted where it is
#    DECLARED, not where it is read. --card-label-h written on :root resolved against the base 16px
#    once and inherited that number straight past the override, so the label band stayed 16 while the
#    badges grew to 20 and 24. It has to be declared inside the scope the override applies to. Found
#    by measuring a real rendered band rather than trusting the derivation.
for owner in ('.card', '.strip-item'):
    if not re.search(re.escape(owner) + r'\s*\{[^{}]*--card-label-h:\s*var\(--card-badge-h\)', SRC):
        fail.append('%s does not declare --card-label-h from --card-badge-h -- on :root it resolves '
                    'once at the base size and never sees the .cards-lg override' % owner)
for cls in ('.grid.cards-lg', '.grid.cards-xl'):
    if not re.search(re.escape(cls) + r'\s*\{[^{}]*--card-badge-h', SRC):
        fail.append('%s no longer steps --card-badge-h up -- the marks stop scaling with the card' % cls)

if fail:
    print('\nFAIL:')
    for f in fail:
        print('  - %s' % f)
    print('\nSee DESIGN.md, "Marks on a picture".')
    sys.exit(1)

print('ok: no pale rings, set mark is a vendored icon, quality ink and star are absolute '
      '(%d themed tokens known)' % len(THEMED))
