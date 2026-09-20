"""Regression test: no hover paints a raw surface colour.

Run:  python test_hover_policy_css.py

WHY IT EXISTS. `--hover-lift` is translucent ink that composites over whatever is beneath, so one
rule serves a control resting on `--bg3` and a control resting on nothing alike. Before 2026-09-20
it was an absolute colour, `color-mix(--bg3, --fg 12%)`, and the twenty-odd controls that rest at
`transparent` had each been handed `--bg2` or `--bg3` by eye instead. 85 hover rules; six used the
token.

The damage was not untidiness. A facet row lives in a dropdown, whose ground is `--bg3`, and its
hover was `--bg2` -- so picking a model went DARKER in the dark theme and LIGHTER in the light one.
Nobody chose that; it is what "pick whichever surface looks right here" produces at scale, and
nothing failed while it was true.

So this pins the rule rather than the twenty call sites: a `:hover` rule may not name `--bg`,
`--bg2`, `--bg3` or `--sidebar-bg` as its background. Anything that genuinely needs one goes on the
allowlist below WITH ITS REASON, which is the part that keeps the list honest -- an entry nobody
can justify in a sentence is a regression wearing an exemption.

Static because it has to be: the automated browser pane does not render, so a rollover cannot be
hovered for real there, and this is exactly the class of fault that looks fine in every screenshot.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = io.open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()

SURFACES = ('--bg', '--bg2', '--bg3', '--sidebar-bg')

# Selector fragment -> why it is allowed to name a surface. Keep the reason; a bare list rots.
ALLOWED = {
    '#sidebar .quiet-field': 'NOT a hover step. One rule covers hover/focus/active/set, and --bg3 '
                             'there means "this control is doing something" -- the rollover on top '
                             'of it is #sidebar .score-range:hover > .quiet-field, which is ink.',
    '.card .check': 'On a PICTURE, not a surface. Its resting scrim darkens one step; the accent '
                    'edge beside it is the documented exception (see DESIGN.md).',
    '.nav': 'Over the image in the detail view, where a scrim is the ground -- --scrim-heavy, not '
            'a UI surface token, and matched here only because the pattern is broad.',
}


def rules():
    """(selector, body) for every rule whose selector mentions :hover, comments stripped."""
    text = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', text):
        sel = ' '.join(m.group(1).split())
        if ':hover' in sel:
            yield sel, ' '.join(m.group(2).split())


offenders = []
checked = 0
for sel, body in rules():
    m = re.search(r'background(?:-color)?\s*:\s*var\((--[\w-]+)\)', body)
    if not m or m.group(1) not in SURFACES:
        continue
    checked += 1
    if any(frag in sel for frag in ALLOWED):
        continue
    offenders.append((sel, m.group(1)))

print('\nHover backgrounds that name a raw surface\n')
print('  %d rule(s) name one; %d are allowlisted with a reason' % (checked, checked - len(offenders)))

if offenders:
    print('\nFAIL: a hover must be --hover-lift, which composites over whatever is beneath.')
    print('If one of these genuinely needs a surface, add it to ALLOWED **with its reason**.\n')
    for sel, tok in offenders:
        print('  %s\n      -> background: var(%s)' % (sel[:100], tok))
    sys.exit(1)

# The token itself has to stay translucent, or every rule above quietly goes back to being absolute
# while still reading as correct.
tok = re.search(r'--hover-lift:\s*(.*?);', CSS, re.S)
if not tok:
    print('\nFAIL: --hover-lift is gone.')
    sys.exit(1)
val = ' '.join(tok.group(1).split())
# BOTH SIDES OF THE PAIR, separately. Testing the whole string for "transparent" passes when only
# one branch is translucent -- caught by mutation-checking this file, where swapping just the light
# branch back to an opaque mix left the dark branch's `transparent` to satisfy the check. A guard
# that reads the right answer off the wrong half is the shape this whole test exists to stop.
branches = re.findall(r'color-mix\([^()]*(?:\([^()]*\)[^()]*)*\)', val) or [val]
opaque = [b for b in branches if 'transparent' not in b]
if len(branches) < 2:
    print('\nFAIL: --hover-lift is not a light-dark() pair any more -- it reads:\n  %s' % val)
    sys.exit(1)
if opaque:
    print('\nFAIL: a branch of --hover-lift is opaque, so it is an absolute colour again:\n  %s\n'
          'An opaque value is only correct on the one surface it was mixed from, which is the\n'
          'bug this whole rule replaced.' % '\n  '.join(opaque))
    sys.exit(1)

print('  --hover-lift is translucent: %s' % val)
print('\nok: every rollover is ink over whatever is beneath it')
