"""Regression test: the theme bar's segments and the code behind them agree.

Run:  python test_theme_seg.py

WHY THIS EXISTS. Until 2026-09-15 the Appearance tab offered three buttons -- Dark, Light and
"Reset to defaults" -- and two of them were the same click: both ran setThemeEdit({}), and since
dark IS the shipped default, clearing every override and choosing Dark are the same act. So one
button had no behaviour of its own, nothing on screen ever said which theme you were on, and a user
pressing Reset on a default theme watched nothing happen and reported it broken. That is exactly how
it was found.

The row is a segmented control now: exactly one segment is true of your colours at any moment, and
it is derived from the colours rather than from which button was last pressed -- so a theme restored
from config.json lights the right one with nothing having had to remember.

WHAT THIS PINS is the join between the two files, the same way test_escape_layers.js pins its two
lists: every segment in the markup must be a mode the script handles, and every mode the script
handles must have a segment. Adding a fourth segment to index.html and forgetting the click path
gives a button that does nothing -- which is the fault this whole change removed, returning by a
different door.
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

# 2. themeMode() decides which segment is lit, so the strings it can return ARE the modes.
mode_fn = re.search(r'function themeMode\(obj\) \{(.*?)\n\}', JS, re.S)
if not mode_fn:
    fail.append('themeMode() is gone -- the bar can no longer say which segment is true')
    modes = set()
else:
    modes = set(re.findall(r"return [^;]*?'([\w-]+)'", mode_fn.group(1)))
    modes |= set(re.findall(r"\?\s*'([\w-]+)'\s*:\s*'([\w-]+)'", mode_fn.group(1))[0]
                 if re.findall(r"\?\s*'([\w-]+)'\s*:\s*'([\w-]+)'", mode_fn.group(1)) else [])
    print('modes themeMode can return: %s' % ', '.join(sorted(modes)))

missing = set(segments) - modes
if missing:
    fail.append('segment(s) %s have no mode themeMode can return -- they could never light up'
                % ', '.join(sorted(missing)))
orphan = modes - set(segments)
if orphan:
    fail.append('mode(s) %s have no segment -- the bar cannot show that state'
                % ', '.join(sorted(orphan)))

# 3. The click path has to handle every segment. A segment whose value never appears there is a
#    button that does nothing, which is the bug this replaced.
click = re.search(r"querySelectorAll\('#themeSeg button'\)\.forEach\(b => b\.addEventListener\('click'.*?\n\}\)\);",
                  JS, re.S)
if not click:
    fail.append('the #themeSeg click handler is gone')
else:
    body = click.group(0)
    for seg in segments:
        # 'dark' is the else-branch of the light test rather than a literal, so accept either the
        # name appearing or the handler provably covering it via setThemeEdit({}).
        if ("'%s'" % seg) not in body and not (seg == 'dark' and 'setThemeEdit(' in body):
            fail.append('segment "%s" is never handled on click' % seg)

# 4. The old three-button row must not come back alongside the new one.
for dead in ('themeReset', 'themeDark', 'themeLight'):
    if 'id="%s"' % dead in HTML:
        fail.append('%s is back in the markup -- the duplicate-action row returned' % dead)

if fail:
    print('\nFAIL:')
    for f in fail:
        print('  - %s' % f)
    sys.exit(1)

print('ok: every segment is a mode, every mode is a segment, and each is handled on click')
