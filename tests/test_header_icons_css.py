"""Regression test: both icons in a dialog title bar are bare, and both still have a rollover.

Run:  python test_header_icons_css.py

THE FAILURE IS A SPECIFICITY COLLISION, not an appearance, which is why it is pinned statically --
it cannot be seen in the automated browser pane, because that pane does not render and so cannot
be hovered for real.

WHAT WENT WRONG, twice, in the same two lines.

FIRST: the Settings title bar holds a Help `?` and a close `✕`. The `✕` was overridden to sit flat
in the bar -- `position: static; background: transparent` -- and the `?` was left as a plain
`.icon-btn`, which wears the FIELD skin: a `--bg3` ground and a 1px `--border-control` edge. Right
beside a text input; wrong beside a bare `✕`. The author, 2026-09-19: "the Help icon should not
have a box around it. Not sure why it does."

SECOND, found while fixing the first: `.close:hover` and `.settings-head .close` carry the SAME
specificity (0,2,0). The header override is later in the file, so it won outright and the `✕` in
the Settings and Help windows had NO rollover at all -- it had been that way since the override was
written. Measuring the `✕` in order to copy its behaviour is what turned up that there was nothing
to copy.

So the rule this pins is: in `.settings-head` and `.help-head`, BOTH icons are bare at rest and
BOTH light to `--bg3` on hover, and the hover rule names both so neither can lose it to the other's
rule again. Equal-specificity collisions are silent -- nothing errors, the later rule simply wins.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSS = io.open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()
HTML = io.open(os.path.join(ROOT, 'app', 'index.html'), encoding='utf-8').read()

fail = []


def rule_body(selector_fragment):
    """The declaration block of the first rule whose selector list contains this fragment."""
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', CSS):
        sel, body = m.group(1), m.group(2)
        sel = re.sub(r'/\*.*?\*/', '', sel, flags=re.S).strip()
        if selector_fragment in sel:
            return sel, body
    return None, None


# 1. The two buttons really are different components, so this can drift again.
if 'id="settingsHelp" class="icon-btn"' not in HTML:
    fail.append('the Help button is no longer an .icon-btn -- re-check what this test pins')
if 'id="settingsClose"' not in HTML or 'class="close"' not in HTML:
    fail.append('the close button is no longer a .close')

# 2. AT REST both are bare, and the icon button is named alongside the close.
sel, body = rule_body('.settings-head .icon-btn')
if not sel:
    fail.append('no rule makes a header .icon-btn bare -- the ? has its field box back')
else:
    if '.settings-head .close' not in sel:
        fail.append('the bare-at-rest rule no longer covers .close as well as .icon-btn')
    if 'background: transparent' not in body:
        fail.append('a header icon is not transparent at rest: %r' % body.strip())
    if 'border-color: transparent' not in body:
        fail.append('a header icon still draws an edge: %r' % body.strip())

# 3. ON HOVER both light, and BOTH ARE NAMED. This is the half that was missing: a hover rule
#    covering only one of them leaves the other to lose to the rest rule above.
hsel, hbody = rule_body('.settings-head .icon-btn:hover')
if not hsel:
    fail.append('a header .icon-btn has no hover -- pointing at the ? does nothing')
else:
    if '.settings-head .close:hover' not in hsel:
        fail.append('THE 2026-09-19 BUG: the hover rule does not name .close, so the header '
                    'override beats .close:hover on equal specificity and the x loses its rollover')
    # THE INVARIANT IS THAT BOTH ICONS SHARE ONE HOVER, not which colour it is. This asserted
    # `var(--bg3)` when it was written on 2026-09-19 and went red the next day for a change that
    # made it MORE correct: --hover-lift became translucent ink, and these two joined the rest of
    # the app rather than keeping a literal of their own. A test that pins the value instead of the
    # rule fails the day the rule is honoured.
    if 'var(--hover-lift)' not in hbody:
        fail.append('the header hover is not the shared --hover-lift: %r' % hbody.strip())
    if re.search(r'background[^;]*var\(--(?:bg|bg2|bg3|sidebar-bg)\)', hbody):
        fail.append('the header hover names a raw surface again: %r' % hbody.strip())

# 4. The rest rule must come BEFORE the hover rule. Equal-specificity order is what broke the x;
#    here the hover rule is more specific, but keeping the order is what makes that obvious.
rest_at = CSS.find('.settings-head .icon-btn {')
if rest_at == -1:
    rest_at = CSS.find('.settings-head .icon-btn,')
hover_at = CSS.find('.settings-head .close:hover')
if rest_at != -1 and hover_at != -1 and hover_at < rest_at:
    fail.append('the hover rule is written above the rest rule -- put it after, so the cascade '
                'reads in the order it applies')

if fail:
    print('\nFAIL:')
    for f in fail:
        print('  - %s' % f)
    sys.exit(1)

print('ok: both header icons are bare at rest, both light to --hover-lift on hover, both named')
