"""Regression test: nothing puts --on-accent on a --accent ground.

Run:  python test_accent_fill.py

WHY THIS EXISTS. --accent (#4f8cff) carries white at 3.22:1 where body text needs 4.5, and the fix
is not a better blue -- there isn't one. To carry white at 4.5 a colour's relative luminance must be
<= 0.183; to stay readable as link text on --bg it must be >= 0.211. The two requirements do not
overlap, so one token could never do both jobs. --accent stayed the brand blue (the icon, the window
edge, links) and --accent-fill was split off as the ground under --on-accent.

THE PIN IS ON THE WHOLE CLASS, not on the nineteen rules that were converted on 2026-09-15. A new
`background: var(--accent); color: var(--on-accent)` looks exactly like every filled control already
in the file and would be unreadable in precisely the way this work removed -- and nobody reviewing it
would see 3.22:1. The sweep is how the original nineteen were found; leaving it behind as a test is
what stops the twentieth.

THE INHERITED-INK CASE IS WHY THIS READS TWO BLOCKS. `.card .zoom` sets --on-accent, and
`.card .zoom:hover` sets the fill -- the ink and the ground are in different rules, so a check that
only looked inside one block would have missed it. It did, on the first pass.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = io.open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()

# Comments in this file quote token names constantly, including the bad pairing this test forbids.
# Blanked (newlines kept) so a line number in a failure still points at the real rule.
SRC = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), CSS, flags=re.S)

FILL_RE = re.compile(r'(?:^|[;{\s])(?:background|background-color)\s*:[^;]*--accent(?![\w-])')

fails = []
blocks = []          # (selector, body, line)
for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', SRC):
    blocks.append((' '.join(m.group(1).split()),
                   m.group(2),
                   SRC.count('\n', 0, m.start(2)) + 1))

# An element's ink can be set on its base rule and its fill on a state rule (`.card .zoom` /
# `.card .zoom:hover`). Collect every selector that sets --on-accent anywhere, then treat a fill
# rule as inked if its own selector is one of those with a state suffix stripped.
inked = set()
for sel, body, _ in blocks:
    if '--on-accent' in body:
        inked.add(sel)

STATE = re.compile(r'(:hover|:focus|:active|:checked|\.on|\.active|:not\([^)]*\))+\s*$')

for sel, body, line in blocks:
    if not FILL_RE.search(body):
        continue
    base = STATE.sub('', sel).strip()
    if '--on-accent' in body or base in inked or sel in inked:
        fails.append((line, sel))

print('checked %d rules' % len(blocks))
if fails:
    print('\nFAIL: %d rule(s) fill with --accent while --on-accent sits on them.' % len(fails))
    print('      Use var(--accent-fill) -- see DESIGN.md, "Accents & state".\n')
    for line, sel in fails:
        print('  style.css:%-5d %s' % (line, sel))
    sys.exit(1)

# And the token itself has to still be derived from --accent, or a re-themed app paints its buttons
# a blue its links have left behind.
if not re.search(r'--accent-fill:\s*color-mix\(in srgb,\s*var\(--accent\)', SRC):
    print('\nFAIL: --accent-fill is no longer mixed from --accent.')
    sys.exit(1)

# Both stars carry the rim. Two mechanisms on purpose -- the card is an inline <svg> composing into
# its existing thumbnail shadow, #dFav is a mask with no path to stroke -- so neither can be checked
# by looking for one spelling.
if '--star-rim: var(--star-edge)' not in SRC:
    print('\nFAIL: the grid card star no longer sets --star-rim.')
    sys.exit(1)
if not re.search(r'#dFav\.on::before\s*\{[^{}]*--star-edge', SRC):
    print('\nFAIL: the detail favourite star no longer draws --star-edge.')
    sys.exit(1)

print('ok: no --on-accent on a raw --accent ground; both stars carry the rim')
