"""Every query the rail's numbers ride on carries every filter the grid does.

THE BUG THIS EXISTS FOR HAS HAPPENED THREE TIMES. app.js builds the query string in five places,
and each time a filter is added someone has to remember all of them:

  · `type` was forgotten in loadFacets -- the comment in loadTags still names it.
  · the label / tag / fav keys were missing from tagsKey, so picking a label stopped narrowing the
    tag counts (2026-09-14).
  · `coll` was forgotten in tagsKey when Groups landed (2026-09-22): with a group active the
    Labels pane read 10 / 46 / 49 / 11 across the whole library, and clicking any of them gave an
    empty grid.

Every one of them is the same failure -- a number that answers "how many EXIST" while presenting
itself as "how many you would GET" -- and every one was found by a person looking at the screen.
This test is so the fourth one is found here instead.

HOW IT WORKS: searchParams() is the canonical set, because it is the query that draws the grid. Any
other builder must carry the same keys, minus a short list of documented exceptions, each of which
has to say why below.

Run:  python tests/test_filter_params.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'app', 'app.js')


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


# Keys that are not filters: paging, sorting, and the shuffle seed. A builder feeding a COUNT has
# no use for them, so their absence is not a forgotten filter.
NOT_A_FILTER = {'limit', 'offset', 'sort', 'order', 'seed', 'for'}

# Where each builder starts in app.js, and what it is allowed to leave out.
# AN EXCEPTION HAS TO EARN ITS LINE HERE. Adding one silently is how the next forgotten filter gets
# waved through, so each carries the reason it is not a bug.
BUILDERS = {
    'loadFacets': (
        'const p = new URLSearchParams({',
        # `sets` is not read by _filters at all -- the sets-only case is the files-vs-cards gap
        # (BR-2), not something this query could answer.
        {'sets'},
    ),
    'groupCountParams': (
        'function groupCountParams() {',
        # `coll` ON PURPOSE: Groups are exclusive, so the list must not narrow ITSELF -- counting
        # every group under the group you already picked would make the others read 0 and the list
        # would stop being usable to move between them. Same rule the label counts follow.
        {'coll'},
    ),
    'tagsKey': (
        'function tagsKey(part) {',
        # `sets` as above. `tags` and `fav` ARE sent (the server drops them at its end so a list
        # does not narrow itself) -- this set is only what never leaves the client.
        {'sets'},
    ),
    'selectAllMatching': (
        'async function selectAllMatching() {',
        set(),
    ),
}


def keys_at(src, marker, name):
    """The parameter names in the URLSearchParams literal that starts at `marker`."""
    i = src.find(marker)
    assert i > 0, 'could not find %s in app.js -- has it been renamed?' % name
    j = src.find('new URLSearchParams({', i)
    assert j > 0, 'no URLSearchParams after %s' % name
    depth, k = 0, j + len('new URLSearchParams(')
    start = k
    while k < len(src):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                break
        k += 1
    body = src[start:k + 1]
    # `key:` at the start of a line or after a comma, skipping // comment lines.
    body = re.sub(r'//[^\n]*', '', body)
    return set(re.findall(r'([A-Za-z_]\w*)\s*:', body)) - NOT_A_FILTER


def test_every_builder_carries_every_filter():
    src = open(APP, encoding='utf-8').read()
    canon = keys_at(src, 'function searchParams(offset, limit) {', 'searchParams')
    print('searchParams is the canonical set: %d filters' % len(canon))
    ok = check('it has the ones this test was written for', {'coll', 'type', 'tags', 'fav'} <= canon, True)
    for name, (marker, allowed) in BUILDERS.items():
        got = keys_at(src, marker, name)
        missing = canon - got - allowed
        extra_excuses = allowed - (canon - got)
        ok &= check(f'{name} carries every filter', sorted(missing), [])
        # An exception listed but no longer needed is a comment that has stopped being true.
        ok &= check(f'{name} has no stale exceptions', sorted(extra_excuses), [])
    return ok


if __name__ == '__main__':
    print('the rail asks the same question the grid does')
    r = test_every_builder_carries_every_filter()
    print('\nPASS' if r else '\nFAIL')
    sys.exit(0 if r else 1)
