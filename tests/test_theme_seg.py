"""Regression test: the theme bar's segments, the Reset button, and the code behind them agree.

Run:  python test_theme_seg.py

WHY THIS EXISTS, in two rounds.

FIRST: until 2026-09-15 the tab offered Dark, Light and "Reset to defaults", and two of them were
the same click -- both cleared every override, and since dark IS the shipped default, clearing
overrides and choosing Dark are one act. A button with no behaviour of its own, and a user pressing
Reset on a default theme watched nothing happen and reported it broken.

SECOND: the fix was a three-segment bar -- Dark, Light, Custom -- deriving the lit segment from the
colours themselves. That put a STATUS in a row of CHOICES. You never picked Custom, you fell into it
by touching a swatch; it was greyed until you had; and both modes shared ONE slot, so a palette
built on Light was silently replaced by a tweak made on Dark. The author, 2026-09-19: "Dark and Light EACH
need a customized state that can be reset."

So there are two segments and a Reset again -- and the Reset is NOT the duplicate it was the first
time. Dark switches mode. Reset clears the edits on the mode you are already on and leaves you
there, which nothing else can do.

WHAT THIS PINS is the join between the two files: every segment in the markup is a mode the script
handles, every mode the script handles has a segment, and Reset is wired to clearing edits rather
than to switching mode. The behaviour of the two slots -- that editing one mode leaves the other
alone -- is pinned server-side in test_theme_modes.py.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = io.open(os.path.join(ROOT, 'app', 'index.html'), encoding='utf-8').read()
JS = io.open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()

fail = []

# 1. The markup's segments.
bar = re.search(r'<div id="themeSeg"[^>]*>(.*?)</div>', HTML, re.S)
if not bar:
    print('FAIL: #themeSeg is gone from index.html')
    sys.exit(1)
segments = re.findall(r'data-theme="([\w-]+)"', bar.group(1))
print('markup segments: %s' % ', '.join(segments))

if len(segments) != len(set(segments)):
    fail.append('two segments share a data-theme value')

# 2. THE MODES THE SCRIPT KNOWS. themeBase() is the one place that turns a mode into a preset, so
#    what it can distinguish IS the set of modes. 'light' is named; anything else is dark.
base_fn = re.search(r'function themeBase\(mode\) \{(.*?)\}', JS, re.S)
if not base_fn:
    fail.append('themeBase() is gone -- nothing turns a mode into a set of colours')
    modes = set()
else:
    modes = {'dark'} | set(re.findall(r"mode === '([\w-]+)'", base_fn.group(1)))
    print('modes themeBase understands: %s' % ', '.join(sorted(modes)))

missing = set(segments) - modes
if missing:
    fail.append('segment(s) %s are not modes themeBase understands -- they could never apply'
                % ', '.join(sorted(missing)))
orphan = modes - set(segments)
if orphan:
    fail.append('mode(s) %s have no segment -- the bar cannot show that state'
                % ', '.join(sorted(orphan)))

# 3. THERE IS NO THIRD SEGMENT. Custom was a status in a row of choices and is not to come back as
#    one; if a genuinely new mode is ever added it has to be a mode, with its own base.
if len(segments) != 2:
    fail.append('expected exactly two segments (dark, light), found %d: %s'
                % (len(segments), ', '.join(segments)))
if 'custom' in segments:
    fail.append('"custom" is a segment again -- it names a status, not a theme anyone chooses')

# 4. The click path has to handle every segment: a segment whose value never reaches the handler
#    is a button that does nothing, which is the fault both rounds of this were fixing.
click = re.search(r"querySelectorAll\('#themeSeg button'\)\.forEach\(b => b\.addEventListener\('click'.*?\n\}\)\);",
                  JS, re.S)
if not click:
    fail.append('the #themeSeg click handler is gone')
else:
    body = click.group(0)
    if '_themeMode = want' not in body:
        fail.append('the click handler no longer sets the mode from the segment pressed')
    if 'applyThemeEdit()' not in body:
        fail.append('the click handler does not apply the mode it switched to')

# 5. RESET EXISTS, AND IS NOT A SECOND WAY TO PRESS DARK. It must clear the current mode's edits
#    and must not assign a mode -- the moment it does either, it is the 2026-09-15 duplicate again.
if 'id="themeReset"' not in HTML:
    fail.append('the Reset button is gone from the markup')
reset = re.search(r"\$\('#themeReset'\)\.addEventListener\('click'.*?\n\}\);", JS, re.S)
if not reset:
    fail.append('#themeReset has no click handler -- a button that does nothing')
else:
    body = reset.group(0)
    if '_themeEdits[_themeMode] = {}' not in body:
        fail.append('Reset does not clear the current mode\'s edits')
    if re.search(r"_themeMode\s*=\s*'", body):
        fail.append('Reset assigns a mode -- it is a duplicate of a segment again')

# 6. Each mode's edits live in their own slot. One shared slot is what silently overwrote a
#    palette; the shape of the store is what stops that.
if not re.search(r"_themeEdits\s*=\s*\{\s*dark:", JS):
    fail.append('_themeEdits is no longer a per-mode store -- the single shared slot is back')

if fail:
    print('\nFAIL:')
    for f in fail:
        print('  - %s' % f)
    sys.exit(1)

print('ok: two segments, both handled; Reset clears this mode without switching it')
