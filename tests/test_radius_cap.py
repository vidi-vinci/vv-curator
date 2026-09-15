"""Regression test: a small mark's corner radius never exceeds a quarter of its own side.

Run:  python test_radius_cap.py

RADIUS IS RELATIVE EVEN THOUGH THE SCALE IS ABSOLUTE. The scale (2/4/6/8/10/14) was drawn for
fields, chips and cards -- boxes of roughly 24px and up. Drop the same step onto an icon-sized mark
and the corners eat the shape. The author reported it on 2026-08-18 as "a lot of the icons got way too
rounded", and the arithmetic was worse than it looked:

  * the set icon's leaves are HALF of --card-badge-h, i.e. 8px, and carried --radius-xs (4px).
    4 / 8 = 50%, which is not "very rounded" but a CIRCLE -- so three stacked cards rendered as
    three bubbles, exactly as reported.
  * the play / quality / note / hide marks are 16px and carried --radius-sm (6px) = 38%.
  * .facet-sortbtn inherited --radius-field (8px) onto a 16px box = 50%, invisible only because it
    paints no background.

None of that is visible in a diff: every one of those was a correct-looking token reference. It is
only wrong in RATIO, against a size declared somewhere else in the file. So the pin is the ratio.

THE RULE: a mark whose shortest side is <= 16px takes at most a quarter of that side. The escape
hatch is explicit -- say --radius-pill when a lozenge is the intent (count badges), never an
accidental 50%.

This reads the stylesheet rather than a browser, so it runs in the normal suite. It therefore only
checks rules whose size it can resolve from the same declaration block; the browser sweep that found
these is recorded in the commit. The named pins at the end cover the specific marks that shipped
wrong, including the two whose size comes from a percentage of a parent.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, '..', 'app', 'style.css'), encoding='utf-8').read()

failures = 0


def check(name, cond, detail=''):
    global failures
    if cond:
        print('  ok    ' + name)
    else:
        print('  FAIL  ' + name + ('\n          ' + detail if detail else ''))
        failures += 1


print('\nSmall marks keep a quarter-side radius cap\n')

# ---- resolve the :root token values we need to do arithmetic with ----------------------------
tokens = {}
root = re.search(r':root\s*\{(.*?)\n\}', CSS, re.S)
for m in re.finditer(r'(--[\w-]+):\s*([^;]+);', root.group(1) if root else ''):
    tokens[m.group(1)] = m.group(2).strip()


def px(value):
    """Resolve a value to pixels, or None when it isn't a plain length this test can reason about."""
    if value is None:
        return None
    value = value.strip()
    for _ in range(4):                                    # follow var() a few hops
        m = re.fullmatch(r'var\((--[\w-]+)\)', value)
        if not m:
            break
        value = tokens.get(m.group(1), '').strip()
    m = re.fullmatch(r'calc\(\s*var\((--[\w-]+)\)\s*/\s*([\d.]+)\s*\)', value)
    if m:
        base = px(f'var({m.group(1)})')
        return base / float(m.group(2)) if base else None
    m = re.fullmatch(r'([\d.]+)px', value)
    return float(m.group(1)) if m else None


check('the token scale was read', px('var(--radius-xs)') == 4 and px('var(--card-badge-h)') == 16,
      f'--radius-xs={px("var(--radius-xs)")} --card-badge-h={px("var(--card-badge-h)")}')
check('--radius-2xs exists for ~8px marks', px('var(--radius-2xs)') == 2,
      'the step below --radius-xs is what keeps an 8px mark from being a circle')
check('--radius-pill is declared, so an intended lozenge is never an accident',
      (px('var(--radius-pill)') or 0) >= 500)

# ---- sweep every rule that declares BOTH a size and a radius ---------------------------------
offenders = []
for block in re.finditer(r'([^{}]+)\{([^{}]*)\}', CSS):
    sel, body = block.group(1).strip(), block.group(2)
    if 'border-radius' not in body:
        continue
    rad = px((re.search(r'(?<!-)border-radius:\s*([^;]+);', body) or [None, None])[1])
    if rad is None or rad >= 500:                          # unresolvable, or a declared pill
        continue
    # BOTH dimensions have to be declared small, which is what separates a MARK from a BAR. .pbar is
    # a 6px-tall progress track running the full width of its pill: a 4px radius there is rounded
    # ENDS, which is conventional and right, and an early version of this test called it a 67%
    # violation. A rule that flags the correct thing is worse than no rule -- it teaches you to
    # ignore it. line-height counts as a height: for a single-line number badge it IS the height.
    widths = [px(m.group(1)) for m in
              re.finditer(r'(?:^|[;{\s])(?:width|min-width):\s*([^;]+);', body)]
    heights = [px(m.group(1)) for m in
               re.finditer(r'(?:^|[;{\s])(?:height|min-height|line-height):\s*([^;]+);', body)]
    widths = [s for s in widths if s]
    heights = [s for s in heights if s]
    if not widths or not heights:
        continue
    side = min(min(widths), min(heights))
    if side > 16.5 or rad <= side / 4 + 0.01:
        continue
    offenders.append(f'{sel.splitlines()[-1].strip()[:60]} -- {rad:g}px on a {side:g}px box '
                     f'({round(rad / side * 100)}% of the side, cap is 25%)')

check('no rule puts an oversized radius on a mark it also sizes', not offenders,
      '\n          '.join(offenders))

# ---- named pins for the marks that shipped wrong ---------------------------------------------
# The set leaves are sized as a PERCENTAGE of the parent, so the sweep above cannot see their real
# 8px side. Pinned by token instead: --radius-2xs is the only step that satisfies the cap there.
# Matched unscoped: the badge stopped being `.card .set-badge` on 2026-09-04, when the detail
# view's filmstrip started using the same component rather than going without a set mark.
leaves = re.search(r'(?<![\w.-])\.set-badge::before[^{]*\{([^}]*)\}', CSS)
check('the set icon\'s leaves use --radius-2xs (they are 8px, so 4px would be a circle)',
      bool(leaves) and 'var(--radius-2xs)' in leaves.group(1),
      'the leaves are width/height 50% of --card-badge-h; anything above 2px stops them reading as cards')

for sel, human in [(r'\.card \.motion-badge', 'the play/video badge'),
                   (r'\.facet-sortbtn', 'the facet sort button')]:
    m = re.search(sel + r'\s*\{([^}]*)\}', CSS)
    body = m.group(1) if m else ''
    r = re.search(r'border-radius:\s*([^;]+);', body)
    val = px(r.group(1)) if r else None
    check(f'{human} is within the cap', val is not None and val <= 4.01,
          f'resolved to {val}px on a 16px box' if val else 'no resolvable border-radius found')

print()
sys.exit(1 if failures else 0)
