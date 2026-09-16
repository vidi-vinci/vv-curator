"""Regression test: undoing a set cull puts the SET VIEW back, not just the grid card.

Run:  python test_set_undo.py

WHAT BROKE. Keeping one member of an image set replaces the set's grid card with the keeper's, and
its undo is a REPLACE back rather than an insert -- it swaps the set card in where the keeper's is.
That part always worked. What it did not do was touch the open detail view.

So after a cull from the zoomed view the sequence was: the last openDetail ran on the kept SINGLE,
which has no members, so `state.setMembers` was null; undo restored the grid card and left that null
standing. The set view never came back and the arrows had nothing to step through. The author,
2026-09-15: "after undo, I can't use the arrows to move between two images from 1 run (e.g. Raw and
Detail)."

It looked fine from the grid -- the stacked-cards mark was back on the card -- which is why it took
someone actually pressing the arrow to find it. Confirmed by running the real cull and the real undo
with the fix disabled: the grid mark returned and setMembers stayed 0.

TWO THINGS THIS PINS, and the second is the one that will be tempting to drop:

  * the restore closure re-opens the detail view, so the members are re-fetched. Safe because
    Undo.run() AWAITS the server's restore before calling the closure -- the files are back on disk
    by then. If that ordering is ever inverted, this reopen starts reading a set that is still short
    a member.
  * it only re-opens IF YOU ARE STILL LOOKING AT THAT SET. With the Keep behaviour on "next" the
    view has already moved to another card, and yanking it back to the set you just undid would be a
    second surprise on top of the first.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = io.open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()

fail = []

# The cull's own restore closure -- the REPLACE-back branch, not undoRestorer's insert branch.
m = re.search(r'restore = \(\) => \{(.*?)\n    \};', JS, re.S)
if not m:
    print('FAIL: recycleSetOthers no longer builds a restore closure this test can find')
    sys.exit(1)
body = m.group(1)

if 'openDetail' not in body:
    fail.append('the restore closure does not re-open the detail view, so state.setMembers stays '
                'null and the arrows have nothing to step through')
if '#overlay' not in body:
    fail.append('the restore closure does not check whether the detail view is open')
# NOT just "state.current appears somewhere": the guard has to be COMPUTED from what is showing.
# Gutting it to `const wasThisSet = true` leaves the read above it untouched and slips past a check
# that only greps for the name -- which this one did, on its first run.
guard = re.search(r'const wasThisSet\s*=\s*([^;]+);', body)
if not guard:
    fail.append('the restore closure no longer works out whether you are still on this set')
elif 'showing' not in guard.group(1):
    fail.append('the reopen guard ignores what is on screen (%s) -- with Keep behaviour "next" it '
                'would yank the view back to the set you just undid' % guard.group(1).strip())

# AND THE ARROW HOLD. Keeping from the focus view parks the arrows on purpose -- a reflexive
# left/right must not carry you off a picture you have just decided about -- and the caption says so.
# Undo never released it, so the set came back and the arrows stayed dead on top of it, under a
# caption still claiming a keep that no longer existed: "it stays on the Kept card, but you can't
# scroll after... it is confusing." Reopening the set was not enough on its own, and the first fix
# for this bug shipped without it because the maximized path was never driven -- the ordinary detail
# view was, and it has no hold.
if 'releaseKeepHold' not in body:
    fail.append('the restore closure does not release the keep hold -- the set comes back but the '
                'arrows stay parked on the image you kept')
else:
    # It has to come BEFORE the reopen, or openDetail rebuilds the caption with the stale hint.
    # The CALL, not the word: 'openDetail' also appears in the comment explaining this ordering,
    # which is what this check tripped over on its first run.
    if body.index('releaseKeepHold()') > body.index('openDetail(oldItem.id)'):
        fail.append('releaseKeepHold runs after openDetail -- the caption is rebuilt while the hold '
                    'is still set, so it keeps saying "Kept"')

# And the ordering the reopen depends on: the server restore must be awaited before the closure runs.
run = re.search(r'async run\(\) \{(.*?)\n    \},', JS, re.S)
if not run:
    fail.append('Undo.run() is gone')
else:
    r = run.group(1)
    undo_at = r.find("'/api/delete/undo'")
    restore_at = r.find('c.restore()')
    if undo_at == -1 or restore_at == -1:
        fail.append('Undo.run() no longer calls the server undo and then the closure')
    elif not (undo_at < restore_at and 'await' in r[:undo_at + 40]):
        fail.append('Undo.run() calls the restore closure BEFORE awaiting the server -- the reopen '
                    'would then re-fetch a set that is still missing a member')

if fail:
    print('\nFAIL:')
    for f in fail:
        print('  - %s' % f)
    sys.exit(1)

print('ok: the set cull\'s undo re-opens the set it restored, and only when you are still on it')
