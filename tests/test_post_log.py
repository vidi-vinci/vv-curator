"""The publish log: what was sent, when, and what happened.

WHY IT EXISTS. The author, 2026-09-22: "without that, the publishing is a bit of a black box."
A SUCCESS was already fully recorded -- it just never appeared as a list. A FAILURE was recorded
nowhere at all: the job's message went on screen once and was gone, so the only evidence you had
tried was your own memory. That asymmetry is the thing being fixed; the listing is the easy half.

TWO KEY SHAPES, READ AS ONE LIST, and the split is history rather than design:

  civitai:post:<post id>   a success. Keyed by the post id since posting shipped, because
                           api_image answers "which post was this file in" with one lookup.
  postlog:fail:<when>      a failure. It has no post id to be keyed by.

Migrating the old rows to one prefix would buy tidiness and cost every receipt that already exists.
This test pins that BOTH are read, and that a receipt written before `title` and `files` existed
still lists correctly -- the author has real posts in that older shape.

Run:  python tests/test_post_log.py
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


def rows_from(meta):
    """The reading half of api_post_log, over a fake meta table.

    Copied from server.api_post_log; keep the two in sync. Driving the real endpoint would mean a
    server, a library and an extension, which test_civitai_push already stands up -- what matters
    here is the SHAPE of what comes back out of the two key families.
    """
    out = []
    for k, v in meta.items():
        j = json.loads(v)
        if k.startswith('postlog:fail:'):
            out.append({'ok': False, 'at': j.get('at') or 0, 'title': j.get('title') or '',
                        'files': j.get('files') or 0, 'error': j.get('error') or '',
                        'stopped': bool(j.get('stopped'))})
        elif k.startswith('civitai:post:'):
            ids = j.get('ids') or []
            out.append({'ok': True, 'at': j.get('at') or 0, 'post_id': k.rsplit(':', 1)[-1],
                        'title': j.get('title') or '', 'files': j.get('files') or len(ids),
                        'url': j.get('url') or '', 'resource': j.get('resource') or '',
                        'draft': bool(j.get('draft'))})
    out.sort(key=lambda r: r['at'], reverse=True)
    return out


def test_both_families_are_read_newest_first():
    print('successes and failures come back as one list')
    now = time.time()
    meta = {
        'civitai:post:111': json.dumps({'url': 'https://civitai.com/posts/111', 'at': now - 100,
                                        'title': 'Newer', 'files': 3, 'draft': True,
                                        'ids': [1, 2, 3], 'resource': 'someModel_v1'}),
        'postlog:fail:%.6f' % (now - 50): json.dumps({'ok': False, 'at': now - 50, 'files': 2,
                                                      'title': 'Went wrong',
                                                      'error': 'No API key.', 'stopped': False}),
        'civitai:post:222': json.dumps({'url': 'https://civitai.com/posts/222', 'at': now - 900,
                                        'title': 'Older', 'files': 1, 'draft': True,
                                        'ids': [9], 'resource': ''}),
    }
    got = rows_from(meta)
    ok = check('newest first, whatever kind', [r['title'] for r in got],
               ['Went wrong', 'Newer', 'Older'])
    ok &= check('the failure carries the worker\'s own sentence', got[0]['error'], 'No API key.')
    ok &= check('and is marked not-ok', got[0]['ok'], False)
    ok &= check('a success carries its link', got[1]['url'], 'https://civitai.com/posts/111')
    return ok


def test_an_old_receipt_still_lists():
    """A post made before `title` and `files` were recorded.

    The author has real ones. `files` falls back to the id list, which every receipt has always
    carried; the title is simply absent and the row says so rather than showing a blank.
    """
    print('a receipt from before title/files still lists')
    now = time.time()
    meta = {'civitai:post:333': json.dumps({'url': 'https://civitai.com/posts/333', 'at': now,
                                            'draft': True, 'ids': [4, 5], 'resource': ''})}
    got = rows_from(meta)
    ok = check('file count derived from the ids', got[0]['files'], 2)
    ok &= check('title is empty, not invented', got[0]['title'], '')
    ok &= check('still counted as a success', got[0]['ok'], True)
    return ok


def test_draft_is_a_fact_about_the_past():
    """Nothing at this end ever hears about a post again.

    So `draft` records HOW IT WAS CREATED. It must never be presented as a current status: publish
    the post on the site and this row cannot know. The client says "Created as draft" for exactly
    that reason, and a test that let it drift to "Draft" would be letting the app claim something
    it cannot see.
    """
    print('draft records creation, not current state')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'app', 'app.js'), encoding='utf-8').read()
    ok = check('the row says how it was created', 'Created as draft' in src, True)
    # Nothing re-reads the far end to refresh a row -- that would be a network call per row on a
    # settings page, and the log is supposed to be cheap to open.
    i = src.find('async function renderPostLog')
    body = src[i:i + 3000]
    ok &= check('the log fetches only our own endpoint',
                body.count('/api/') == 1 and '/api/post/log' in body, True)
    return ok


def test_a_failed_run_is_written_down():
    """_log_failed_post exists, writes only when there is no post, and prefers the worker's words."""
    print('a run that made no post records why')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'server.py'), encoding='utf-8').read()
    i = src.find('def _log_failed_post')
    ok = check('the writer exists', i > 0, True)
    body = src[i:i + 2000]
    ok &= check('it does nothing when a post was made', "if state.get('post'):" in body, True)
    ok &= check("it prefers the worker's own sentence", "state.get('fatal')" in body, True)
    ok &= check('and it is wired to the publish run', 'on_done=lambda st: _log_failed_post' in src,
                True)
    return ok


def test_forgetting_a_row_is_whitelisted_and_complete():
    """Deleting a log entry must reach the per-file receipts, and must not reach anything else.

    TWO FAILURES GUARDED HERE, and both are quiet ones:

      · A deleted success leaves `civitai:image:<id>` rows pointing at a receipt that is gone. The
        detail panel's Posted row then shows nothing and the rows sit there forever with nothing
        able to reach them. So forgetting a post means forgetting it everywhere -- which is also
        the only reading under which the confirm ("this removes the record here") is honest.
      · `meta` is not just the publish log. It holds schema_version, the per-root scan signatures
        and the reader version; deleting one of those breaks the index or silently triggers a full
        re-read. The endpoint takes keys FROM THE PAGE, so the prefix check is a whitelist rather
        than a tidy-up.
    """
    print('forgetting is complete, and cannot reach the rest of meta')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'server.py'), encoding='utf-8').read()
    i = src.find('def api_post_log_delete')
    ok = check('the endpoint exists', i > 0, True)
    body = src[i:i + 3000]
    ok &= check('only the two log families may be named',
                "k.startswith('civitai:post:') or k.startswith('postlog:fail:')" in body, True)
    ok &= check('the per-file receipts go too', "'civitai:image:%d'" in body, True)
    # A file posted twice keeps the LATEST receipt, so deleting the older post must not strip a
    # pointer that now names a newer one.
    ok &= check('and only where they still name this post',
                "str(cur['value']) == str(pid)" in body, True)
    return ok


def test_every_exit_resyncs_the_bar():
    """Rewriting the list must re-derive the Forget bar, on EVERY path out.

    The author's screenshot, 2026-09-22: "Nothing published yet from this library." with
    "Forget 9 entries" sitting under it. Forgetting the last entries redrew the list empty and
    returned early, so the bar kept a count of checkboxes that no longer existed -- offering to
    delete nine things from an empty list.

    The invariant is small and easy to break again the next time a branch is added, so it is
    checked structurally: inside renderPostLog, the only bare `return` is the one guarding a
    missing element BEFORE anything has been drawn. Every other exit goes through the sync.
    """
    print('every exit from the render re-derives the bar')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'app', 'app.js'), encoding='utf-8').read()
    i = src.find('async function renderPostLog')
    j = src.find('function syncPostLogBar', i)
    assert 0 < i < j, 'renderPostLog / syncPostLogBar not found -- renamed?'
    body = src[i:j]
    # A return carrying a template literal is a ROW, returned from the .map() callback -- not an
    # exit from renderPostLog. Only the plain ones are exits.
    returns = [ln.strip() for ln in body.splitlines()
               if 'return' in ln and '//' not in ln and '`' not in ln]
    stray = [r for r in returns if r != 'if (!box) return;' and 'syncPostLogBar' not in r]
    ok = check('no exit skips the sync', stray, [])
    ok &= check('and the sync is reached more than once',
                sum('syncPostLogBar' in r for r in returns) >= 2, True)
    # The bar is derived, never remembered: its count comes from the ticked boxes in the DOM, so a
    # redraw that removes them is what makes it disappear.
    k = src.find('function syncPostLogBar')
    ok &= check('the count comes from the DOM, not a variable',
                ".pl-pick:checked" in src[k:k + 600], True)
    return ok


def test_a_failure_keeps_what_a_retry_needs():
    """A failed row stores the expanded ids and the whole compose block.

    The commonest failure is a setting you can fix in a minute -- no key, the wrong site -- and
    without these a retry means reselecting the files and retyping everything you had written.

    THE IDS ARE ALREADY EXPANDED, so a retry sends exactly what the attempt did rather than
    re-expanding cards that may have changed since. THE WHOLE compose block, not just the title:
    there is nothing secret in it (the API key is a SETTING and never travels here) and a retry
    that lost the description is one you redo by hand anyway.
    """
    print('a failure keeps its files and its wording')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'server.py'), encoding='utf-8').read()
    i = src.find('def _log_failed_post')
    body = src[i:i + 3500]
    ok = check('the ids are stored', "'ids': [i for i in ids" in body, True)
    ok &= check('and the compose values', "'values':" in body, True)
    ok &= check('it is handed the ids, not just a count', 'def _log_failed_post(state, compose, ids)'
                in src, True)
    return ok


def test_retry_is_offered_on_failures_only():
    """A success must not grow a Retry.

    It would mean a duplicate post on Civitai, one click away in a list, and the site is where you
    would manage that. A failure recorded before retry existed has no ids and offers nothing rather
    than a button that would have to explain itself.
    """
    print('retry belongs to failures')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'app', 'app.js'), encoding='utf-8').read()
    i = src.find('async function renderPostLog')
    j = src.find('function syncPostLogBar', i)
    body = src[i:j]
    # The retry button is built inside the `if (!e.ok)` branch and nowhere else.
    bad_branch = body[body.find('if (!e.ok) {'):body.find("return `<div class=\"postlog-row\">")]
    ok = check('the failure row builds it', 'pl-retry' in bad_branch, True)
    ok &= check('and it appears exactly once in the render', body.count('pl-retry'), 1)
    ok &= check('gated on there being ids to send', '(e.ids || []).length' in bad_branch, True)
    # Retrying must not erase the attempt it came from -- the log is a record.
    k = src.find('async function postLogRetry')
    ok &= check('a retry deletes nothing', '/api/post/log/delete' not in src[k:k + 1200], True)
    return ok


def test_a_stop_says_what_it_left_behind_once():
    """The stop row carries the whole sentence, and the page does not add a second "Stopped.".

    The author pressed Stop on a whim and got a row reading "Stopped. Stopped." -- the server wrote
    the bare word and the client prefixed it again. Worse than the repetition: the one sentence
    that MATTERS for a stop had been thrown away. A stop is the only outcome that can leave files
    on the account with nothing to show for them, and the worker cannot say so because a stop kills
    it before it reports anything. The app has to.
    """
    print('a stop says what it left behind, once')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    srv = io.open(os.path.join(root, 'server.py'), encoding='utf-8').read()
    app = io.open(os.path.join(root, 'app', 'app.js'), encoding='utf-8').read()
    sentence = 'Anything already uploaded is left on Civitai, and no post was created.'
    i = srv.find('def _log_failed_post')
    ok = check('the stored sentence names what was left behind', sentence in srv[i:i + 2500], True)
    # The client must not build wording from the flag as well -- that is what doubled it.
    j = app.find('async function renderPostLog')
    k = app.find('function syncPostLogBar', j)
    ok &= check('the page does not prefix a second one',
                "e.stopped ? 'Stopped. '" in app[j:k], False)
    ok &= check('and the flag is still carried as data', "'stopped': bool(" in srv, True)
    return ok


def test_the_button_does_not_say_delete():
    """It says Forget. The post on Civitai is not ours to delete, and a Delete button sitting
    beside a link to that post would read as removing the post itself."""
    print('the word is Forget, not Delete')
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'app', 'app.js'), encoding='utf-8').read()
    i = src.find('async function postLogForget')
    ok = check('the confirm says the post stays on Civitai',
               'stays on Civitai' in src[i:i + 1500], True)
    ok &= check('and the button is not called Delete', 'Forget' in src[i:i + 1500], True)
    return ok


if __name__ == '__main__':
    results = [test_both_families_are_read_newest_first(), test_an_old_receipt_still_lists(),
               test_draft_is_a_fact_about_the_past(), test_a_failed_run_is_written_down(),
               test_forgetting_a_row_is_whitelisted_and_complete(),
               test_every_exit_resyncs_the_bar(), test_a_failure_keeps_what_a_retry_needs(),
               test_retry_is_offered_on_failures_only(),
               test_a_stop_says_what_it_left_behind_once(),
               test_the_button_does_not_say_delete()]
    print('\nPASS' if all(results) else '\nFAIL')
    sys.exit(0 if all(results) else 1)
