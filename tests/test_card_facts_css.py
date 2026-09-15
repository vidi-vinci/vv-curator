"""Regression test: the card's facts band never lands on top of a badge.

Run:  python test_card_facts_css.py

THE COLLISION IS ARITHMETIC, NOT APPEARANCE, which is why it is pinned statically as well as
measured in the browser. The band is positioned by its BOTTOM edge, above the two badge clusters --
and `.card-br` is a COLUMN: a card carrying both a quality score and a note badge is TWO badges tall
there, not one. A band clearing only one row looks perfectly fine on every card that happens to lack
a note, and sits across the number on the ones that have both.

So the offset is written as arithmetic over the same tokens the badges are sized from, and this
asserts the arithmetic clears the taller case. If `--card-badge-h` ever moves, both sides move
together -- that is the whole point of every card mark scaling from one knob, and this is what
stops a future edit from quietly writing `28px` instead.

It also pins the four properties that make the band harmless:

  * `position: absolute`  -- an in-flow band would relayout sixty cards on every mouseover;
  * `pointer-events: none` -- the card is an <a> whose whole face must stay clickable and
    draggable, and #grid's delegated mouseover walks e.target, so a hit-testable band would
    re-fire the drag preload every time the pointer crossed it;
  * a z-index BELOW the label strip and both clusters;
  * the size gate agreeing between the stylesheet and applyCardSize -- a rename on one side would
    silently disable the band rather than fail.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = io.open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()
JS = io.open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


# ---- resolve the :root tokens we need to do arithmetic with -------------------------------------
tokens = {}
root = re.search(r':root\s*\{(.*?)\n\}', CSS, re.S)
for m in re.finditer(r'(--[\w-]+):\s*([^;]+);', root.group(1) if root else ''):
    tokens[m.group(1)] = m.group(2).strip()

# AND THE TOKENS DECLARED ON `.card` ITSELF, which is where the ones that must follow the card size
# live. A custom property is substituted where it is DECLARED, so anything derived from
# --card-badge-h has to sit inside the scope that .grid.cards-lg overrides -- on :root it would
# resolve against the base size once and never see the override. --card-label-h moved here on
# 2026-09-15 for exactly that reason, and reading only :root would leave this test unable to see it
# at all, which is how it started failing the moment the derivation became real.
for m in re.finditer(r'(?:^|\n)\.card\s*\{([^{}]*)\}', CSS):
    for t in re.finditer(r'(--[\w-]+):\s*([^;]+);', m.group(1)):
        tokens.setdefault(t.group(1), t.group(2).strip())

# --card-bottom-inset is declared on `.card`, NOT on :root, because it has two values: a labelled
# card lifts everything at its foot clear of the label band, an unlabelled one keeps the tighter
# inset. Both are read here so the arithmetic below can be checked against each -- resolving only
# the :root half would leave this test unable to see the offset at all.
card_rule = re.search(r'\n\.card \{([^}]*)\}', CSS)
for m in re.finditer(r'(--[\w-]+):\s*([^;]+);', card_rule.group(1) if card_rule else ''):
    tokens[m.group(1)] = m.group(2).strip()
labelled = re.search(r'\.card:has\(\.label-strip\)\s*\{([^}]*)\}', CSS)
LABELLED_INSET = re.search(r'--card-bottom-inset:\s*([^;]+);', labelled.group(1) if labelled else '')
LABELLED_INSET = LABELLED_INSET.group(1).strip() if LABELLED_INSET else None


def px(value):
    """A plain length in px, following var() a few hops. rem is 16px here."""
    if value is None:
        return None
    value = value.strip()
    for _ in range(4):
        m = re.fullmatch(r'var\((--[\w-]+)\)', value)
        if not m:
            break
        value = tokens.get(m.group(1), '').strip()
    m = re.fullmatch(r'([\d.]+)px', value)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r'([\d.]+)rem', value)
    if m:
        return float(m.group(1)) * 16
    return None


def length(value, override=None):
    """A length in px, following var() and evaluating calc(). `override` substitutes one token,
    which is how the labelled and unlabelled cards are both checked from one expression."""
    if value is None:
        return None
    value = value.strip()
    if override:
        for name, sub in override.items():
            # Unwrap a substituted calc() first: CSS nests them happily, this evaluator does not,
            # and the value being substituted in is itself arithmetic.
            inner = re.fullmatch(r'calc\((.+)\)', (sub or '').strip())
            value = value.replace('var(%s)' % name, '(%s)' % (inner.group(1) if inner else sub))
    m = re.fullmatch(r'calc\((.+)\)', value)
    if not m:
        direct = px(value)
        if direct is not None:
            return direct
        m = re.fullmatch(r'\((.+)\)', value)
        if not m:
            return None
    expr = m.group(1)
    for tok in sorted(set(re.findall(r'var\((--[\w-]+)\)', expr)), key=len, reverse=True):
        inner = length(tokens.get(tok), override)
        if inner is None:
            return None
        expr = expr.replace('var(%s)' % tok, str(inner))
    try:
        return float(eval(expr, {'__builtins__': {}}, {}))    # arithmetic over resolved px only
    except Exception:
        return None


def rule(sel):
    m = re.search(re.escape(sel) + r'\s*\{([^}]*)\}', CSS)
    return m.group(1) if m else None


def prop(body, name):
    m = re.search(r'(?:^|;|\s)' + name + r':\s*([^;]+);', body or '')
    return m.group(1).strip() if m else None


print('\nThe facts band clears every badge\n')

band = rule('.card .card-facts')
check('the band has a rule at all', band is not None)

# ---- 1. the offset, against the tallest the bottom-right stack can get --------------------------
badge = px('var(--card-badge-h)')
inset = px('var(--space-2)')
gap = px('var(--space-1)')
check('the tokens resolve', None not in (badge, inset, gap), (badge, inset, gap))

# .card-br is a column of at most two marks (quality score, note badge), each --card-badge-h tall
# with a --space-1 gap, sitting at a --space-2 inset. That is the tallest thing the band must clear.
tallest_cluster = inset + 2 * badge + gap
# THE OFFSET FOLLOWS THE CARD'S OWN BADGES since 2026-08-28, so the rule that must clear the tall
# case is the :has() one, not the base. The base now sits at the card's edge, which is the fix the author
# reported from a camera archive: reserving room for a quality score on a photo that can never have
# one floated the band 49px up with nothing beneath it. Both halves are checked -- a base that
# quietly went back to reserving space would look identical on ComfyUI output and wrong on his.
base_bottom = prop(band, 'bottom')
check('with no badges the band sits at the card inset, not above a reservation',
      length(base_bottom) == inset, base_bottom)

# THE INSET HAS TWO VALUES since the label band grew to carry its name. Everything at the card's
# foot measures from --card-bottom-inset, so an unlabelled card is unchanged (8px) and a labelled
# one lifts clear of the band. Both are checked below: pinning only the default would let the
# labelled case regress silently, which is exactly how the band's old 5px height went unnoticed as
# a load-bearing number.
label_h = px('var(--card-label-h)')
check('the label band declares its own height token', label_h is not None, label_h)
check('a labelled card lifts its foot clear of the band',
      LABELLED_INSET is not None and length(LABELLED_INSET) is not None
      and length(LABELLED_INSET) >= label_h, LABELLED_INSET)
labelled_inset = length(LABELLED_INSET) if LABELLED_INSET else None
check('an unlabelled card is unchanged at the old inset',
      length('var(--card-bottom-inset)') == inset, tokens.get('--card-bottom-inset'))

two = rule('.card:has(.card-br .reward):has(.card-br .note-badge) .card-facts')
check('there is a rule for the two-badge case', two is not None)
bottom = prop(two, 'bottom')
check('the offset is arithmetic over the tokens, not a hardcoded px',
      bottom is not None and bottom.startswith('calc(') and 'var(' in bottom, bottom)
resolved = length(bottom)
check('it clears a TWO-badge bottom-right stack (%s >= %s)' % (resolved, tallest_cluster),
      resolved is not None and resolved >= tallest_cluster, bottom)
# ...and again on a labelled card, where the whole stack rides higher.
tall_labelled = (labelled_inset or 0) + 2 * badge + gap
resolved_labelled = length(bottom, {'--card-bottom-inset': LABELLED_INSET})
check('and clears it on a LABELLED card too (%s >= %s)' % (resolved_labelled, tall_labelled),
      resolved_labelled is not None and resolved_labelled >= tall_labelled, bottom)

# ---- 2. the properties that make it harmless ----------------------------------------------------
check('absolutely positioned, so a hover never relayouts the card',
      prop(band, 'position') == 'absolute', prop(band, 'position'))
check('never hit-testable', prop(band, 'pointer-events') == 'none', prop(band, 'pointer-events'))

z_band = px(prop(band, 'z-index') or '') or float(prop(band, 'z-index') or 0)
# Unscoped since 2026-09-05: the filmstrip shows the same items and carries the same label band, so
# the band stopped being a card part. `.card:has(.label-strip)` above is still card-scoped, because
# the INSET it sets is the card's own.
z_strip = float(prop(rule('.label-strip'), 'z-index') or 0)
z_bl = float(prop(rule('.card .card-bl'), 'z-index') or 0)
check('it paints under the label strip', z_band < z_strip, (z_band, z_strip))
check('and under the badge clusters', z_band < z_bl, (z_band, z_bl))

# ---- 3. the size gate agrees on both sides ------------------------------------------------------
# A rename on one side would not fail: the band would simply never appear.
check('the stylesheet gates on .grid.cards-lg', '.grid.cards-lg .card .card-facts' in CSS)
check('applyCardSize sets that same class', "classList.toggle('cards-lg'" in JS)
check('  ...at Large and above', re.search(r"toggle\('cards-lg',\s*px >= 256\)", JS) is not None)
check('the band is hidden by default, so a missing class means absent, not broken',
      prop(band, 'display') == 'none', prop(band, 'display'))

# ---- 4. a song card is exempt, the same way its caption is --------------------------------------
# A BAND WITH NOTHING TO SHOW AT REST MUST NOT DRAW ITSELF. cardFactsHTML emits the band whenever
# ANY tier has content, and the hover tier is display:none until you point at the card -- so setting
# every detail to "On hover" left a scrim strip with no text in it. It is keyed on the absence of an
# always-row and not on the settings, because the band is per-ITEM: Duration is an Always row that
# only a video fills, so one setting gives a full band on a video and an empty one on a still.
hide_empty = re.search(r'\.grid\.cards-lg \.card \.card-facts:not\(:has\(\.cf-always\)\)\s*\{([^{}]*)\}', CSS)
check('a band with no always-row is hidden at rest',
      bool(hide_empty) and 'display: none' in hide_empty.group(1), hide_empty and hide_empty.group(1))
show_hover = re.search(r'\.grid\.cards-lg \.card:hover \.card-facts:not\(:has\(\.cf-always\)\)\s*\{([^{}]*)\}', CSS)
check('  ...and comes back on hover', bool(show_hover) and 'display: flex' in show_hover.group(1),
      show_hover and show_hover.group(1))
# Source order matters: both rules are the same specificity, so the hover one has to come second.
if hide_empty and show_hover:
    check('  ...with the hover rule LAST, or it never wins', show_hover.start() > hide_empty.start())

check('a song face suppresses the band', '.card:has(.song-face) .card-facts' in CSS)
check('  ...as it already does the filename caption', '.card:has(.song-face) .cap' in CSS)

# ---- 5. no hardcoded colours --------------------------------------------------------------------
hexes = re.findall(r'#[0-9a-fA-F]{3,8}\b', band or '')
check('no hardcoded hex in the band', not hexes, hexes)

print('\n%s\n' % ('FAILED: ' + '; '.join(failures) if failures else 'all checks passed'))
sys.exit(1 if failures else 0)
