"""A post that would exceed the destination's limit is refused before anything is uploaded.

WHY REFUSING BEATS TRUNCATING, and why this is correctness rather than politeness. The publish
worker uploads every file in full and only THEN creates the post. So a run that overruns the far
end's cap is refused part-way: the files already sent stay on the account, no post is created, and
nothing at this end can take them back. It is the worst outcome the posting path has, and it is the
one the old code walked into -- MAX_POST_FILES was 50, a guess at when Civitai's editor gets
unwieldy, while the API actually refuses at 20. A 50-file post was therefore guaranteed to fail
that way, and the silent `[:50]` truncation meant a selection of 60 also lost 10 files with nothing
said.

THE NUMBER BELONGS TO THE DESTINATION. It is declared by the extension that talks to Civitai, as
`max_items` in its manifest, not hardcoded in the app -- the app enforces it without knowing where
it came from, the same way it draws settings fields it does not understand.

Run:  python tests/test_post_cap.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


def test_civitai_declares_the_real_limit():
    """20, which is Civitai's actual cap -- confirmed against the API and a third-party uploader
    that enforces the same number in its own UI and backend.

    If this ever needs changing, change it because the FAR END changed, not because a bigger post
    would be convenient. The failure mode for guessing high is orphaned uploads.
    """
    print('the manifest declares the destination\'s real limit')
    mf = json.load(io.open(os.path.join(ROOT, 'extensions', 'civitai', 'extension.json'),
                           encoding='utf-8'))
    ok = check('max_items is 20', mf.get('max_items'), 20)
    ok &= check('it says why, for whoever changes it', '_max_items' in mf, True)
    return ok


def test_the_parser_reads_it():
    print('the app reads the cap off the manifest')
    ext = server._read_extension(os.path.join(ROOT, 'extensions', 'civitai'))
    ok = check('parsed', ext['max_items'], 20)
    # An extension that declares nothing is uncapped by its own manifest and falls back to the
    # app's backstop -- a scorer or a tagger is one result per file, where a thousand is only a
    # longer wait.
    q = server._read_extension(os.path.join(ROOT, 'extensions', 'quality'))
    ok &= check('an extension that declares none reads 0', q['max_items'], 0)
    return ok


def test_the_backstop_is_not_the_limit():
    """MAX_POST_FILES is the app's ceiling for an undeclared extension, NOT Civitai's number.

    Conflating the two is what produced the bug: the app's guess was applied to a destination that
    had its own, smaller, real answer.
    """
    print('the app backstop is separate from the destination cap')
    ok = check('there is still a backstop', server.MAX_POST_FILES > 0, True)
    ok &= check('and it is not what Civitai gets', server.MAX_POST_FILES != 20, True)
    return ok


def test_the_cap_refuses_rather_than_truncates():
    """The endpoint must not silently post a subset.

    Read off the source rather than driven through a request: standing up the publish endpoint
    means a server, an extension and a fake Civitai, which test_civitai_push already does. What is
    worth pinning here is the SHAPE -- that the slice is gone and a refusal took its place, because
    the slice is the thing that failed quietly.
    """
    print('the endpoint refuses instead of trimming')
    src = io.open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()
    i = src.find('def api_post_run')
    assert i > 0, 'api_post_run not found -- has it been renamed?'
    body = src[i:i + 4000]
    ok = check('no silent truncation of the id list', '[:MAX_POST_FILES]' in body, False)
    ok &= check('the declared cap is consulted', "ext['max_items']" in body, True)
    ok &= check('and it answers with an error, not a trim', 'in one post' in body, True)
    return ok


if __name__ == '__main__':
    results = [test_civitai_declares_the_real_limit(), test_the_parser_reads_it(),
               test_the_backstop_is_not_the_limit(),
               test_the_cap_refuses_rather_than_truncates()]
    print('\nPASS' if all(results) else '\nFAIL')
    sys.exit(0 if all(results) else 1)
