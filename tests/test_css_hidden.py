"""Regression test: every element that ships with class="hidden" has a rule that hides it.

Run:  python test_css_hidden.py

(Nothing to do with test_hidden.py, which is the Hidden curation mark -- "not this one, for now".
This one is purely about the CSS class.)

THIS APP HAS NO GLOBAL `.hidden` UTILITY, on purpose -- `.hidden { display: none }` set globally
would beat some components' own display and lose to others', so every component scopes its own rule.
`style.css` says so in three places. The cost of that choice is that adding `hidden` to a new element
looks like it works, silently does nothing, and the toggling JS reads as correct forever after.

It cost two shipped bugs, found together on 2026-08-18 and both invisible to code review:

  * `#dSong` (`.song-detail`) is `display: flex; width: 100%` inside `.detail-img`, which is a
    CENTRING flex row. Emptied but never hidden, it stayed a flex ITEM and took a share of the row,
    so on every ordinary image the picture was shoved against the left edge with dead space to its
    right -- 133px of it at a 1280px window. Nothing looked broken; it just stopped being centred.
  * `#dLmWrap` (`.detail-lm`) was a `<details>`, so `display: block`, drawing a 31px
    "LM response / Copy" row under EVERY image including the vast majority never asked about.
    (That element left with the LM bench on 2026-08-23; the lesson it taught is why the sweep
    below covers the whole class rather than a list of known offenders.)

Neither would fail any test that checked behaviour: the JS added and removed the class exactly as
intended. Only the rendered geometry disagreed. So the pin is structural, and it is on the whole
CLASS of element rather than on the two that were reported -- the second was found by sweeping, and
would otherwise still be there.

A new element carrying `hidden` fails this until it is given a rule. That is the point.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

html = open(os.path.join(ROOT, 'app', 'index.html'), encoding='utf-8').read()
css = open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()

failures = 0


def check(name, cond, detail=''):
    global failures
    if cond:
        print('  ok    ' + name)
    else:
        print('  FAIL  ' + name + ('\n          ' + detail if detail else ''))
        failures += 1


print('\nEvery hidden element has a rule that hides it\n')

elements = []
for m in re.finditer(r'<(\w+)([^>]*\bclass="([^"]*\bhidden\b[^"]*)"[^>]*)>', html):
    tag, attrs, cls = m.group(1), m.group(2), m.group(3)
    eid = (re.search(r'id="([^"]+)"', attrs) or [None, None])[1]
    elements.append((tag, eid, [c for c in cls.split() if c != 'hidden']))


def has_rule(tag, eid, classes):
    """Is there a selector that pairs .hidden with this element's class, id or tag?

    Deliberately loose about the selector's exact shape -- `.set-view.hidden`,
    `.dialog-actions button.hidden` and `.facet-sortbtn.hidden, .lib-more.hidden` are all real,
    valid forms in this file. Being strict here would make the test a style rule about how the
    selector is written, which is not what is being protected.
    """
    pats = [rf'\.{re.escape(c)}[^{{,]*\.hidden' for c in classes]
    pats += [rf'\.hidden[^{{,]*\.{re.escape(c)}' for c in classes]
    if eid:
        pats += [rf'#{re.escape(eid)}[^{{,]*\.hidden', rf'\.hidden[^{{,]*#{re.escape(eid)}']
    pats += [rf'\b{tag}\.hidden\b']
    return any(re.search(p, css) for p in pats)


check('the markup was parsed at all', len(elements) > 20,
      f'found only {len(elements)} elements carrying the class -- has index.html moved?')

# No global utility. If one is ever added this test's whole premise changes, so say so loudly
# rather than quietly passing everything.
check('there is still no global .hidden utility',
      not re.search(r'(^|[{}\s,])\.hidden\s*\{', css),
      'a global .hidden rule appeared -- it will beat some components and lose to others; '
      'that ambiguity is exactly what the per-component rules avoid')

uncovered = [e for e in elements if not has_rule(*e)]
check(f'all {len(elements)} hidden elements have a hiding rule', not uncovered,
      '\n          '.join(f'<{t} id={i} class={" ".join(c) or "(none)"}> has no rule '
                          f'-- adding "hidden" to it does nothing' for t, i, c in uncovered))


# ---- the extensions' opt-in rule -------------------------------------------------------------
# An extension's controls are marked `data-ext="<id>"` in the markup and hidden by ONE rule,
# `[data-ext].hidden`. They do not ship carrying the class, so the sweep above never sees them --
# and they are the exact shape that failed twice before: a class the JS toggles perfectly onto an
# element nothing hides. Marking a control is meant to be the whole job, so the rule that makes it
# so is pinned here.
ext_els = re.findall(r'<(\w+)[^>]*\bdata-ext="([^"]+)"', html)
check('controls are actually marked with data-ext', len(ext_els) >= 5,
      f'found only {len(ext_els)} -- has the Quality scorer stopped declaring its controls?')
check('[data-ext].hidden sets display:none',
      bool(re.search(r'\[data-ext\]\.hidden\s*\{[^}]*display:\s*none', css)),
      'without it, switching an extension off leaves every one of its controls on screen')

# An <option> is the one marked element the rule cannot serve: a display rule does not take an
# option out of a select's keyboard navigation, so applyExtensions() sets the HTML attribute there
# instead. If that branch is ever dropped, a disabled extension's sorts stay reachable by arrow key
# while looking gone.
js = open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()
check("an <option>'s marked control uses the hidden ATTRIBUTE, not the class",
      bool(re.search(r"tagName === 'OPTION'\)\s*el\.hidden =", js)),
      "applyExtensions() must set el.hidden on options -- the CSS rule alone leaves them "
      "selectable from the keyboard")

# Named so a future edit that drops the rule fails by name. There were two: `#dLmWrap`
# (`.detail-lm`) left with the LM bench on 2026-08-23, so only `#dSong` is still checkable.
# The general sweep above is what actually pins the class; this is the named-regression half.
for eid, cls in (('dSong', 'song-detail'),):
    check(f'.{cls} still hides itself (#{eid})',
          bool(re.search(rf'\.{re.escape(cls)}\.hidden\s*\{{[^}}]*display:\s*none', css)),
          f'.{cls}.hidden must set display:none -- without it #{eid} takes up space on every image')

print()
sys.exit(1 if failures else 0)
