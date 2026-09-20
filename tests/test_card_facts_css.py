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

# THE STYLESHEET WITH ITS PROSE REMOVED, and this exists because the bare-substring form of these
# checks failed OPEN on 2026-09-18. `'.card:has(.song-face) .card-facts' in CSS` was asserting that
# songs are exempt from the band; the rule was deleted that day and the check still passed, because
# the comment left in its place NAMES the selector it is explaining. A guard that reads comments is
# not reading the stylesheet. Every structural check below searches this instead.
CSS_RULES = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)

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

# ---- 4b. a song card carries the band, and prints its duration exactly once ---------------------
# Until 2026-09-18 a song was exempt from the band entirely, because the drawn face already showed
# the duration. That was true of duration and of nothing else: age, file size, model and folder were
# never on the face, so one overlapping fact suppressed four that had no overlap at all. The author,
# putting a track beside an image from the same grid: "the music cards don't align with the image
# cards... Unclear why audio/music cards don't behave the same."
#
# The exemption is gone. What replaces it is a HANDOVER, and these checks pin both ends of it: the
# band shows at Large and up, and that is exactly where the face's own .song-facts row stands down.
# Get one side without the other and the duration either prints twice or not at all.
check('the blanket song exemption is gone',
      '.card:has(.song-face) .card-facts' not in CSS_RULES)
song_facts = re.search(r'\.grid\.cards-lg \.card \.song-facts\s*\{([^{}]*)\}', CSS_RULES)
check('the face\'s own facts row stands down where the band appears',
      bool(song_facts) and 'display: none' in song_facts.group(1),
      song_facts and song_facts.group(1))
# The two sides of the handover must name the SAME threshold. They were a container query and a grid
# class for a while, which agreed only because the card sizes are discrete -- luck, not a guarantee.
check('  ...gated on the same class the band is', '.grid.cards-lg .card .card-facts' in CSS_RULES)

# The filename caption is the other half of the parity, and it is scoped rather than deleted: at
# Small and Medium the centred title fills the card and the two collide, which is what the original
# rule was for.
cap_rule = re.search(r'\.grid:not\(\.cards-lg\) \.card:has\(\.song-face\) \.cap\s*\{([^{}]*)\}', CSS_RULES)
check('the caption is off on song cards BELOW Large',
      bool(cap_rule) and 'display: none' in cap_rule.group(1), cap_rule and cap_rule.group(1))
check('  ...and therefore on at Large and above',
      '.card:has(.song-face) .cap' not in CSS_RULES.replace(':not(.cards-lg) ', ' ')
      or cap_rule is not None)

# THE COVER MUST BE ABLE TO GIVE WAY. It is sized as a share of the card while everything it shares
# the card with -- the top inset, the band clearance, the title, the waveform -- is fixed pixels. On
# a wide card that overhead is a small slice and the share fits; at the nominal 256 the same ~89px is
# more than a third of the box, and a fixed `width: 50cqw; height: 50cqw` overflowed it by ~7px,
# straight down behind the band. Cards are stretched to fill the row, so which width you get is the
# window's business and not a size anyone chose -- the 256 case is reachable, just not always.
# The fix is a flex BASIS plus aspect-ratio, so the art shrinks and stays square. A later edit
# "simplifying" it back to a fixed width would look correct at every width but one.
# Asserted on the BASE rule, which is where the shape lives: the per-size blocks only move the
# share (`flex-basis` / `max-width`), so a size that forgot to shrink is not a thing that can
# happen. It was on the Large rule while Large was the only size laid out this way.
cover = re.search(r'(?<!\S)\.card \.song-cover\s*\{([^{}]*)\}', CSS_RULES)
check('the cover is sized as a shrinkable share, not a fixed box', bool(cover), cover)
if cover:
    body = cover.group(1)
    check('  ...a flex basis, so a column flex can shrink its HEIGHT', 'flex: 0 1' in body, body)
    check('  ...kept square by aspect-ratio rather than a matching height', 'aspect-ratio: 1' in body, body)
    check('  ...with min-height:0, or the shrink never reaches it', 'min-height: 0' in body, body)
    # (?<![-\w]) and not \b: `\b` sits happily between the hyphen and the h in `min-height`, so the
    # obvious spelling of this check flagged the `min-height: 0` that the rule NEEDS.
    check('  ...and no fixed height to fight it',
          not re.search(r'(?<![-\w])height:\s*\d', body), body)
head = re.search(r'(?<!\S)\.card \.song-head\s*\{([^{}]*)\}', CSS_RULES)
check('  ...and its parent can shrink too, or the shrink stops one level up',
      bool(head) and 'min-height: 0' in head.group(1), head and head.group(1))
check('  ...in a column, which is what makes the basis a HEIGHT at all',
      bool(head) and 'flex-direction: column' in head.group(1), head and head.group(1))
# EVERY PER-SIZE OVERRIDE MOVES THE SHARE, NEVER THE BOX. A `width`/`height` pair in one of these
# would pin the art at that size and reintroduce the overflow the base rule exists to stop -- which
# is exactly the spelling all four size blocks used before 2026-09-18.
for sel in re.findall(r'\.card \.song-cover\s*\{([^{}]*)\}', CSS_RULES):
    if 'flex: 0 1' in sel:
        continue                                      # the base rule, checked above
    check('a per-size cover override moves the share only: %r' % sel.strip(),
          not re.search(r'(?<![-\w])(width|height):\s*\d', sel), sel)

# Tempo and key are audio's answer to dimensions, and the client table and the server's default
# order have to list the same keys or a saved config silently drops one.
SERVER = io.open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()
for k in ('bpm', 'key'):
    check("'%s' is a fact the grid can draw" % k, "key: '%s'" % k in JS)
    check("  ...and one the server will store", "'key': '%s'" % k in SERVER)

# ---- 5. no hardcoded colours --------------------------------------------------------------------
hexes = re.findall(r'#[0-9a-fA-F]{3,8}\b', band or '')
check('no hardcoded hex in the band', not hexes, hexes)

print('\n%s\n' % ('FAILED: ' + '; '.join(failures) if failures else 'all checks passed'))
sys.exit(1 if failures else 0)
