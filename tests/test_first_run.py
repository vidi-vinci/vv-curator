"""The first screen a new user sees, with no library configured.

Run: python tests/test_first_run.py

Why this test exists. The author ran a clean copy on 2026-09-07 and reported four faults in the start
state, three of which are invisible to every other test because they only exist on the ONE branch an
established user never reaches -- `boot()`'s early return when there is no library. Nothing indexes,
nothing renders, so nothing that checks the grid or the API can see any of it.

These are source-level assertions on purpose. The failures they pin are not wrong OUTPUT, they are a
missing call and a rule someone would not know to keep:

  1. **The title said "Loading..." forever.** index.html ships that as a placeholder and every other
     path passes updateTitleFromRoot() on the way through. The no-library branch returned first.
  2. **The welcome quotes the Libraries button's own glyph**, lifted from the live element. A second
     copy of the SVG would go on pointing at a shape the strip no longer wears -- and that strip
     changed its marks four times in a fortnight.
  3. **Two controls in the strip stand down until a library exists.** The auto-refresh clock is the
     one the author reported ("a false signal, and will draw attention from the Folder"); the tick beside
     it is dead for exactly the same reason, and fixing one without the other is how the pair drifts.
  4. **The add-library dialog asks the STATE whether this is a first run**, not which button was
     clicked -- the empty state now sends every new user through the route that used to answer wrong.
  5. **The Add library dialog's primary button is dead until there is a path to act on** -- and the
     folder picker tells it so, which a listener on keystrokes alone would have missed.
  6. **Help's format list matches what the scanner actually walks.** The welcome says only that
     stills, video and audio are supported and sends the reader to Help; the author's call, 2026-09-07:
     reference material does not belong on a screen someone sees once. That makes `docs/help.md` the
     ONE list, and a hand-written list is exactly the thing that drifts -- so this pins it against
     index_db.MEDIA_EXTS rather than against a copy of itself.

NO WORDING IS PINNED HERE, and that is deliberate. An earlier version asserted phrases -- "a
library is", the dialog's heading, the absence of "ComfyUI" -- and the first one failed the moment
The author tightened a sentence, which is the whole objection: copy is his to change, nobody edits it by
accident, and a test standing in front of it teaches the next reader not to touch the words. What is
pinned is what breaks SILENTLY: a missing call, a control that cannot re-enable, two lists that must
agree. The one apparent exception is the check that no format list is on the welcome, and that is a
placement rule rather than a phrase -- if the list comes back there, Help stops being the single
source and the drift guard below stops meaning anything.

Reads the source and the docs. Starts no server and touches no library.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import index_db  # noqa: E402

APP = open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()
CSS = open(os.path.join(ROOT, 'app', 'style.css'), encoding='utf-8').read()
HELP = open(os.path.join(ROOT, 'docs', 'help.md'), encoding='utf-8').read()
HTML = open(os.path.join(ROOT, 'app', 'index.html'), encoding='utf-8').read()


def section(src, start, length=1400):
    i = src.find(start)
    return src[i:i + length] if i >= 0 else ''


def main():
    checks = []

    # 1. The title is set BEFORE the early return, not after it.
    branch = section(APP, "if (!cfg || !cfg.active) {", 900)
    checks.append(("the no-library branch exists", bool(branch), branch[:40]))
    ti, hi = branch.find('updateTitleFromRoot()'), branch.find('showNoRootHint()')
    checks.append(("it sets the window title", ti >= 0, ti))
    checks.append(("and does so before it returns", 0 <= ti < hi, (ti, hi)))
    # THE PLACEHOLDER IS GONE, 2026-09-14, and this check changed direction rather than being
    # deleted. index.html now ships the real name as its static title, so there is no state in which
    # the title is wrong and nothing for a first-time user to read as "Loading..." in their taskbar.
    #
    # Asserted against the <title> ELEMENT, not the file: the first version of this check looked for
    # the string anywhere in index.html, and once the old placeholder was described in a COMMENT
    # explaining its removal, the check went on passing while meaning nothing. A test that matches
    # prose about the thing it is testing is a test that has stopped testing.
    m = re.search(r'<title[^>]*>([^<]*)</title>', HTML)
    checks.append(("index.html has a <title>", bool(m), 'missing'))
    title_text = (m.group(1) if m else '')
    checks.append(("it ships the app's name, not a placeholder",
                   title_text.strip() == 'VV Curator', title_text))
    # updateTitleFromRoot still matters: the name comes from the server, so a rename has to reach
    # the title. It must not be gated on the (now empty) suffix -- that would make it a no-op.
    fn = section(APP, 'function updateTitleFromRoot()', 600)
    checks.append(("the title update is gated on the NAME, not the suffix",
                   'if (_appName) {' in fn, fn[:120]))

    hint = section(APP, 'function showNoRootHint()', 3000)   # it grew when the formats line landed
    # 2. The glyph is read from the live button, never written out again.
    checks.append(("the welcome lifts the icon from #libScope",
                   "$('#libScope svg')" in hint, 'not lifted'))
    checks.append(("and embeds no SVG of its own",
                   '<svg' not in hint, 'a second copy of the glyph'))
    checks.append(("it is a panel, not a bare paragraph",
                   'empty-hint panel' in hint, 'no panel'))

    # 3. Both strip controls stand down, and from ONE place that already knows.
    stand = section(APP, 'function setNoLibraryStrip(')
    checks.append(("one function stands the strip down", bool(stand), 'missing'))
    for sel in ("#libRefreshAll", "#autoRefresh"):
        checks.append(("it covers %s" % sel, sel in stand, 'uncovered'))
    checks.append(("the scope button asks to be clicked", "'nudge'" in stand, 'no nudge'))
    checks.append(("renderLibTrigger drives it from all.length",
                   'setNoLibraryStrip(noLib)' in APP and 'const noLib = !all.length' in APP, 'no'))
    # Hiding needs a scoped rule: there is no global .hidden util in this stylesheet.
    checks.append(("a scoped rule actually hides them",
                   '.lib-stat.hidden' in CSS, 'nothing hides them'))
    # Filled with --active since 2026-09-07, which forces the FILL pulse: fading a glyph on a solid
    # ground leaves a blinking blob, per the rule beside the auto-refresh clock.
    nudge = section(CSS, '.lib-stat.nudge {', 260)
    checks.append(("the nudge is filled and pulses that fill",
                   'var(--active)' in nudge and 'cv-pulse-fill' in nudge, nudge[:60] or 'no rule'))
    # WHICH ink is the author's to choose (he took white over the computed near-black on 2026-09-07);
    # that it comes from a TOKEN rather than a raw hex is the rule, since --active is user-editable
    # and a hardcoded colour would survive a theme change it should not.
    checks.append(("its ink is a token, not a hardcoded colour",
                   'color: var(' in nudge and not re.search(r'color:\s*#', nudge),
                   'a raw hex on a themeable ground'))
    # Every filled state in this strip has to ANSWER the shared button.lib-stat:hover, which wins on
    # specificity and repaints a filled button as a plain grey one. Without this rule the green
    # flattens to grey with white marks on hover -- what the author saw.
    checks.append(("hover keeps the fill instead of flattening it",
                   '.lib-stat.nudge:hover' in CSS, 'the shared hover repaints it'))
    # The switch is restored from localStorage before boot() runs, so hiding the button is not enough.
    tick = section(APP, 'async function autoRefreshTick(')
    checks.append(("the timer stands down too, not just its button",
                   'state.roots || []' in tick, 'timer runs with no library'))

    # 4. ONE message, whatever the state. The author collapsed the first-run/second-library split on
    # 2026-09-07 ("no need to customize it"), which retired the bug this check used to guard: the
    # wording was picked by which control was clicked, so the strip route told a user with no
    # libraries about "another" folder. A single string cannot make that mistake, and this asserts
    # the SHAPE that guarantees it -- one assignment, no branch -- rather than the words in it.
    open_ = section(APP, 'function openAddRoot(')
    checks.append(("the dialog has one message, not a branch",
                   open_.count("$('#setupMsg').textContent") == 1 and '? ' not in open_,
                   'the message is conditional again'))

    # 5. The one thing about the formats that is NOT wording: WHERE the list lives. The author's call --
    # a screen someone sees once is not the home for reference material -- and if a list reappears
    # on the welcome, Help stops being the single place and the guard below stops meaning anything.
    checks.append(("the welcome does not carry a format list",
                   not re.search(r"'(PNG|JPG|WebP|MP4|MP3)", hint), 'a format list is back on it'))

    # THE DRIFT GUARD. Help is hand-written, so nothing but this stops it describing a format the
    # scanner stopped taking -- or missing one it started. JPEG is the single deliberate omission:
    # it is JPG to a reader, and printing both makes the list look padded rather than thorough.
    fmt_sec = section(HELP, '## What it reads', 900)
    checks.append(("Help has a formats section", bool(fmt_sec), 'missing'))
    named = set(re.findall(r'\b(PNG|JPG|JPEG|WebP|GIF|MP4|MOV|WebM|MP3|WAV|FLAC|M4A|Opus)\b',
                           fmt_sec))
    walked = {e.lstrip('.') for e in index_db.MEDIA_EXTS} - {'jpeg'}
    missing = {e for e in walked if not any(n.lower() == e for n in named)}
    extra = {n for n in named if n.lower() not in walked | {'jpeg'}}
    checks.append(("Help names every format the scanner walks", not missing, sorted(missing)))
    checks.append(("and names nothing the scanner ignores", not extra, sorted(extra)))

    # 6. The Add library dialog. Its WORDING is not pinned -- headings, phrasing and the placeholder
    # are the author's to change, and a test in front of them is friction with no payoff. What is pinned is
    # the gate, which is behaviour and breaks silently.
    # The gate: disabled (idle), and told by BOTH paths that can fill the field. The picker is the
    # one a listener on keystrokes alone would miss, and it is the likelier route.
    gate = section(APP, 'function syncSetupGo()', 300)
    checks.append(("a gate exists and disables rather than hides",
                   'go.disabled' in gate, 'no gate'))
    checks.append(("typing tells it", "$('#setupPath').addEventListener('input', syncSetupGo)" in APP,
                   'not wired to input'))
    checks.append(("and so does Browse", 'syncSetupGo();' in section(APP, 'async function pickFolder()', 900),
                   'the picker leaves the button dead'))

    # The welcome must not be squeezed into one fixed card column (it was: 194px wide, 386 tall).
    checks.append(("the grid drops out of grid flow for the welcome",
                   '.grid:has(> .empty-hint)' in CSS, 'welcome rides the card tracks'))

    bad = [c for c in checks if not c[1]]
    for name, ok, got in checks:
        print("%-5s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "  got %r" % (got,)))
    print("\n%s" % ("all checks passed" if not bad else "%d FAILED" % len(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
