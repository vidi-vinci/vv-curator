"""test_post_receipt.py -- what a finished post leaves behind in the library.

Run: python tests/test_post_receipt.py

A publishing run makes one thing, and that thing lives on Civitai where someone who is not this app
can edit or delete it. So what lands locally is a RECEIPT, and the two decisions about it are the
ones pinned here.

THE RECEIPT GOES IN `meta`, NOT IN A TAG. A `post:<id>` tag row would be swept by one click on
Clear machine tags, and a publishing record that disappears with a tidy-up is worse than none.
`meta` is per-library, already exists, needs no migration, and nothing sweeps it. This is the first
real answer to "Published is a disposition, not a record": the label says THAT you posted, the
receipt says which post, when, and what went out alongside it.

THE APP SETS THE LABEL, NEVER THE WORKER. The guard in _store_tags refuses a worker that emits
`label:` anything, and that guard is not being loosened or worked around -- the worker reports a
post id, and the app decides that being in a post means Published. So the label must appear even
though nothing in the worker's output mentions it, and must not appear when the tick is off.
"""
import json
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='vv_receipt_')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db  # noqa: E402
import server  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


DB = os.path.join(TMP, 'lib.db')
index_db.connect(DB).close()          # creates the schema, including the meta table
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
# Two files, one already carrying a different label. Published is a Flag, so that label must
# survive the post -- see THE BUG THIS REPLACED below.
COLS = ('path', 'rel_path', 'folder', 'filename', 'ext', 'mtime', 'size',
        'has_meta', 'motion', 'has_cover', 'indexed_at', 'reader_ver')


def add(iid, name, ext):
    """A minimally valid images row. Every NOT NULL column, and nothing else -- this test is about
    what a post writes, not about what a scan reads."""
    vals = (name, name, '', name, ext, 0, 0, 0, 0, 0, 0, 0)
    conn.execute("INSERT INTO images(id, %s) VALUES (?%s)"
                 % (', '.join(COLS), ', ?' * len(COLS)), (iid,) + vals)


add(1, 'a.png', '.png')
add(2, 'b.mp4', '.mp4')
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES (1, 'label:publish', 'user')")
conn.commit()

RECEIPT = {'control': True, 'post': {'id': 987654, 'url': 'https://civitai.com/posts/987654',
                                     'images': 2, 'resource': 'Minimax H3 FL2VA',
                                     'ids': [1, 2]}}


def labels_on(iid):
    return sorted(r[0] for r in conn.execute(
        "SELECT tag FROM tags WHERE image_id=? AND tag LIKE 'label:%'", (iid,)))


def receipts():
    return {r[0]: json.loads(r[1]) for r in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'civitai:post:%'")}


print('\nWhat a finished post leaves behind\n')

# --- a result line is just a file that uploaded ---------------------------------------------------
server._post_state.update(phase=None, post=None, fatal=None)
check('an upload line counts as a result',
      server._store_post(conn, {'id': 1, 'uploaded': True}, True) is True)
check('a failure line does not',
      server._store_post(conn, {'id': 1, 'error': 'nope'}, True) is False)

# --- a phase line is not a result -----------------------------------------------------------------
check('a phase line is not counted as a result',
      server._store_post(conn, {'control': True, 'phase': 'uploading 1 of 2'}, True) is False)
check('but it is remembered, so the bar can say what is happening',
      server._post_state['phase'] == 'uploading 1 of 2', server._post_state['phase'])

# --- the receipt ----------------------------------------------------------------------------------
check('the receipt line reports itself stored',
      server._store_post(conn, RECEIPT, True) is True)
conn.commit()
got = receipts()
check('one meta row per post, keyed by its id',
      list(got) == ['civitai:post:987654'], list(got))
row = got.get('civitai:post:987654') or {}
check('it records WHICH post', row.get('url', '').endswith('/posts/987654'), row)
check('and WHAT went out together', row.get('ids') == [1, 2], row)
check('and WHEN', isinstance(row.get('at'), float) and row['at'] > 0, row.get('at'))
check('and that it is a draft', row.get('draft') is True, row)
check('and what got credited', row.get('resource') == 'Minimax H3 FL2VA', row)
check('the run state carries the post, so the client can show a link',
      (server._post_state.get('post') or {}).get('id') == 987654, server._post_state.get('post'))

# --- the label, set by the APP ---------------------------------------------------------------------
check('every file in the post is marked Published',
      'label:published' in labels_on(1) and 'label:published' in labels_on(2),
      (labels_on(1), labels_on(2)))
# THE BUG THIS REPLACED, and the reason to keep the check pointed the other way. Published was a
# Status until 2026-09-21, so a successful post deleted every label first: posting a file marked
# To refine silently threw that away. Published is a Flag now and adds nothing else's slot.
check('and the status it already had SURVIVES -- a flag takes no slot',
      'label:publish' in labels_on(1), labels_on(1))

check('a file can hold a status and a flag at once',
      labels_on(1) == ['label:publish', 'label:published'], labels_on(1))

# --- the guard the worker still cannot get past -----------------------------------------------------
# The app setting a label is NOT the worker being allowed to. _store_tags drops `label:` whatever
# else changes, and this is the assertion that says so if anyone ever loosens it.
conn.execute("DELETE FROM tags WHERE image_id=2")
conn.commit()
server._store_tags(conn, 'civitai', {'id': 2, 'tags': ['label:published', 'castle']})
conn.commit()
check('a WORKER still cannot set a label', labels_on(2) == [], labels_on(2))
check('while its ordinary tags land',
      [r[0] for r in conn.execute("SELECT tag FROM tags WHERE image_id=2")] == ['castle'],
      [r[0] for r in conn.execute("SELECT tag FROM tags WHERE image_id=2")])

# --- the tick off ------------------------------------------------------------------------------------
conn.execute("DELETE FROM tags")
conn.execute("DELETE FROM meta WHERE key LIKE 'civitai:post:%'")
conn.commit()
server._store_post(conn, RECEIPT, False)
conn.commit()
check('with the tick off, nothing is labelled', labels_on(1) == [] and labels_on(2) == [],
      (labels_on(1), labels_on(2)))
check('but the receipt is still recorded -- it is a record, not a preference',
      list(receipts()) == ['civitai:post:987654'], list(receipts()))

# --- a run that ended with no post --------------------------------------------------------------------
server._post_state.update(post=None, fatal=None)
server._store_post(conn, {'control': True, 'fatal': 'Nothing was posted.'}, True)
check('a fatal line is carried for the client to show',
      server._post_state['fatal'] == 'Nothing was posted.', server._post_state['fatal'])
check('and writes no receipt', len(receipts()) == 1, receipts())

# --- the reverse key, so a FILE can name its post ---------------------------------------------------
# Without this the only way to answer "which post was this file in" is to JSON-parse every
# `civitai:post:*` row, which gets slower with every post ever made. Nothing on screen shows the key
# itself, so its removal would surface only as a detail row that quietly stopped appearing.
def post_of(iid):
    """The lookup api_image performs: file -> post id -> the receipt."""
    r = conn.execute("SELECT value FROM meta WHERE key=?", ('civitai:image:%d' % iid,)).fetchone()
    if not r:
        return None
    pr = conn.execute("SELECT value FROM meta WHERE key=?",
                      ('civitai:post:%s' % r[0],)).fetchone()
    return json.loads(pr[1 if len(pr) > 1 else 0]) if pr else None


print()
for iid in (1, 2):
    got = post_of(iid)
    check('file %d can name the post it went in' % iid,
          got is not None and got.get('url', '').endswith('/posts/987654'),
          'no civitai:image:%d row -- the reverse key is not being written' % iid
          if got is None else got)
check('and the post says how many files went together',
      len((post_of(1) or {}).get('ids') or []) == 2, post_of(1))
check('a file that was never posted names nothing, so the detail row hides',
      post_of(99) is None, post_of(99))

# --- posts made before the reverse key existed ------------------------------------------------------
# The receipt has always carried the file list; the per-file key is new. Without a backfill the row
# would stay hidden on exactly the posts that already exist, which reads as the feature not working.
import index_db  # noqa: E402

conn.execute("DELETE FROM meta WHERE key LIKE 'civitai:image:%'")          # an old library's state
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
             ('civitai:post:111', json.dumps({'url': 'https://civitai.com/posts/111',
                                              'at': 100.0, 'ids': [1, 3], 'draft': True})))
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
             ('civitai:post:222', json.dumps({'url': 'https://civitai.com/posts/222',
                                              'at': 200.0, 'ids': [3], 'draft': True})))
conn.commit()
index_db._backfill_post_reverse_keys(conn)
conn.commit()

print()
check('a post made before this build gets its keys', post_of(1) is not None, post_of(1))
check('a file in two old posts lands on the LATER one',
      (post_of(3) or {}).get('url', '').endswith('/posts/222'), post_of(3))
check('and it is still idempotent', (index_db._backfill_post_reverse_keys(conn) or True)
      and (post_of(3) or {}).get('url', '').endswith('/posts/222'), post_of(3))

print()
sys.exit(1 if _fails else 0)
