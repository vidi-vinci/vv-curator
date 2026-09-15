"""test_ext_tags.py -- a tag an extension wrote can never be mistaken for one you typed.

Run: python tests/test_ext_tags.py

An extension is a subprocess the app does not control the output of. It can emit anything, and this
one writes into the same `tags` table that carries the author's own tags, his exclusive curation labels,
his favourites and the Hidden mark. So the interesting assertions are not "does tagging work" --
they are about what a worker must NOT be able to do.

**The one that matters most: a worker may not set a curation label.** `label:<slug>` is the
exclusive one-key mark, read straight out of this table, so without a guard a tagger could mark a
thousand images "To publish" by emitting a string -- silently, in a bulk run, over curation that
cannot be rebuilt.

The rest pins the separation the author asked for when this was designed: machine tags carry `ext:<id>`
as their source, they never join the user's list, and they can be cleared as a group without
touching anything hand-made. And a re-run replaces that extension's tags rather than piling a
second pass's results on top of the first's.

Also here, because it is the same contract seen from the other end: the SQL fragments the rest of
the app filters with. Both were widened by hand when this shipped -- `tags.source` was documented as
'wd14' | 'vlm' | 'manual' and has actually been 'user' | 'fav' | 'hide' since the first commit, so
nothing was "already ready" and every read site was a deliberate edit.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix='vv_exttags_')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

import index_db  # noqa: E402
import server  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


print('\nAn extension\'s tags stay distinguishable from the user\'s\n')

db = os.path.join(TMP, 'lib.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
conn.executescript(index_db.SCHEMA)
conn.execute("INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size, "
             "indexed_at) VALUES (1, 'a.png', 'a.png', '.', 'a.png', '.png', 0, 0, 0)")

# What the user made. None of it may be disturbed by anything below.
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES (1,'my tag','user')")
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES (1,'label:publish','user')")
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES (1,'','fav')")
conn.commit()


def tags_of(source_sql, params=()):
    return sorted(r['tag'] for r in
                  conn.execute(f"SELECT tag FROM tags WHERE {source_sql}", params))


# --- a worker's output, including the two things it must not be allowed to do -------------------
ok = server._store_tags(conn, 'ram', {'id': 1, 'tags': [
    {'tag': 'Beach', 'score': 0.9},          # normalised to lower case, like a typed tag
    {'tag': '  surf  '},                     # trimmed
    {'tag': 'label:published'},              # A CURATION LABEL. Must be refused.
    {'tag': 'a,b'},                          # commas are the tag separator elsewhere
    'plain-string',                          # the short form of the contract
    {'tag': ''},                             # nothing at all
]})
conn.commit()

check('a line carrying tags is reported as a result', ok is True, ok)
machine = tags_of("source='ext:ram'")
check('tags are stored under the extension, not as the user',
      machine == ['a b', 'beach', 'plain-string', 'surf'], machine)
check('A WORKER CANNOT SET A CURATION LABEL',
      not any(t.startswith('label:') for t in machine), machine)
check('the user\'s label is still exactly his',
      tags_of("source='user' AND tag LIKE 'label:%'") == ['label:publish'])
check('the user\'s own tag is untouched', 'my tag' in tags_of("source='user'"))
check('the favourite mark is untouched',
      conn.execute("SELECT 1 FROM tags WHERE image_id=1 AND source='fav'").fetchone() is not None)

score = conn.execute("SELECT score FROM tags WHERE tag='beach' AND source='ext:ram'").fetchone()[0]
check('a score the worker gave is kept', score == 0.9, score)

# --- a line with no tags is a failure, not an empty result --------------------------------------
check('an error line is not counted as a result',
      server._store_tags(conn, 'ram', {'id': 1, 'error': 'boom'}) is False)

# --- a re-run replaces this extension's tags, and only this extension's -------------------------
server._store_tags(conn, 'florence', {'id': 1, 'tags': ['pier']})
server._store_tags(conn, 'ram', {'id': 1, 'tags': ['beach', 'sunset']})
conn.commit()
check('a re-run replaces that extension\'s previous tags',
      tags_of("source='ext:ram'") == ['beach', 'sunset'], tags_of("source='ext:ram'"))
check('and leaves another extension\'s alone',
      tags_of("source='ext:florence'") == ['pier'], tags_of("source='ext:florence'"))

# --- the SQL the rest of the app filters with ---------------------------------------------------
check('MACHINE_TAG_SQL selects every extension\'s tags and nothing else',
      tags_of(server.MACHINE_TAG_SQL) == ['beach', 'pier', 'sunset'],
      tags_of(server.MACHINE_TAG_SQL))
check('ANY_TAG_SQL covers the user\'s and the machine\'s, but not fav/hide',
      tags_of(server.ANY_TAG_SQL) == ['beach', 'label:publish', 'my tag', 'pier', 'sunset'],
      tags_of(server.ANY_TAG_SQL))

# --- the group undo -----------------------------------------------------------------------------
conn.execute(f"DELETE FROM tags WHERE {server.MACHINE_TAG_SQL}")
conn.commit()
check('clearing machine tags leaves the user with everything he made',
      tags_of("1=1") == ['', 'label:publish', 'my tag'], tags_of("1=1"))

conn.close()
print()
sys.exit(1 if _fails else 0)
