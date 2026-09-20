"""test_tag_counts_shape.py -- the sidebar's tag counts survived being made fast.

WHAT CHANGED AND WHY IT NEEDS PINNING. `/api/tags` used to JOIN tags to images whenever any filter
was on, which let SQLite drive from the tags side: it scanned every tag row and read a whole image
row by rowid for each one, prompt text and all, only to learn whether that image passed the filter.
On the author's library that was 3.0-3.7s against 0.2s for the unfiltered shape. It now asks which
images match ONCE, as a set of ids, and counts tags against that.

A faster query that counts differently is not a fix, so this asserts the two shapes agree -- on a
real database, through real filters, including the ones with a join of their own (the full-text
search) and the ones that match nothing.

THE SAME FAULT HAS NOW ARRIVED TWICE, as an EXISTS guard and then as the JOIN that replaced it, so
the last check here is the one that matters most: the shipped SQL must not mention the images table
in its counting half at all.

Run: python test_tag_counts_shape.py
"""
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

ANY_TAG = "(source='user' OR source LIKE 'ext:%')"
TAIL = "GROUP BY t.tag ORDER BY c DESC, t.tag COLLATE NOCASE"
SEL = "SELECT t.tag, COUNT(*) c, MAX(t.source='user') AS mine"

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label +
          ('' if ok else '\n         got  %r\n         want %r' % (got, want)))
    if not ok:
        _fails.append(label)


def build(conn):
    conn.executescript("""
        CREATE TABLE images (id INTEGER PRIMARY KEY, ext TEXT, model_name TEXT, folder TEXT,
                             positive TEXT, has_meta INTEGER DEFAULT 1, root_id TEXT);
        CREATE TABLE tags (image_id INTEGER NOT NULL, tag TEXT NOT NULL, source TEXT NOT NULL,
                           score REAL, PRIMARY KEY (image_id, tag, source));
        CREATE VIRTUAL TABLE images_fts USING fts5(positive, content='');
    """)
    rows = [
        (1, '.png', 'alpha', 'a', 'a neon street at dusk'),
        (2, '.png', 'alpha', 'a', 'a quiet forest'),
        (3, '.jpg', 'beta', 'b', 'a neon sign'),
        (4, '.mp4', 'beta', 'b', 'a running dog'),
        (5, '.png', None, 'c', 'nothing in particular'),
    ]
    conn.executemany("INSERT INTO images(id,ext,model_name,folder,positive) VALUES (?,?,?,?,?)",
                     rows)
    for r in rows:
        conn.execute("INSERT INTO images_fts(rowid, positive) VALUES (?,?)", (r[0], r[4]))
    tags = [
        (1, 'portrait', 'user'), (1, 'night', 'user'), (1, 'favourite-ish', 'ext:tagger'),
        (2, 'portrait', 'user'), (2, 'forest', 'user'),
        (3, 'night', 'user'), (3, 'portrait', 'ext:tagger'),
        (4, 'motion', 'user'),
        (5, 'portrait', 'user'),
        (1, 'x', 'fav'), (3, 'x', 'fav'),          # favourites ride the same table
        (2, 'ignored', 'other'),                    # neither user nor ext: must never be counted
    ]
    conn.executemany("INSERT INTO tags(image_id,tag,source) VALUES (?,?,?)", tags)
    conn.commit()


# The two shapes, built exactly as api_tags builds them.
def old_shape(joins, wsql):
    return (f"{SEL} FROM tags t JOIN images i ON i.id = t.image_id {joins} {wsql} "
            f"AND {ANY_TAG} {TAIL}")


def new_shape(joins, wsql):
    return (f"{SEL} FROM tags t "
            f"WHERE t.image_id IN (SELECT i.id FROM images i {joins} {wsql}) AND {ANY_TAG} {TAIL}")


def old_fav(joins, wsql):
    return (f"SELECT COUNT(*) c FROM tags t JOIN images i ON i.id = t.image_id "
            f"{joins} {wsql} AND t.source='fav'")


def new_fav(joins, wsql):
    return (f"SELECT COUNT(*) c FROM tags t "
            f"WHERE t.image_id IN (SELECT i.id FROM images i {joins} {wsql}) AND t.source='fav'")


conn = sqlite3.connect(':memory:')
build(conn)

print('\nThe tag counts survived being made fast\n')

# Real filter shapes, including one that carries a join of its own and one that matches nothing.
CASES = [
    ('a type filter', '', "WHERE LOWER(i.ext) IN ('.png')", []),
    ('a model filter', '', "WHERE i.model_name IS ?", ['alpha']),
    ('a model filter for (none)', '', "WHERE i.model_name IS ?", [None]),
    ('a folder filter', '', "WHERE (i.folder = ? OR i.folder LIKE ?)", ['a', 'a/%']),
    ('a text search (brings its own join)',
     'JOIN images_fts f ON f.rowid = i.id', "WHERE images_fts MATCH ?", ['neon']),
    ('a filter matching nothing', '', "WHERE i.folder = ?", ['nowhere']),
    ('two filters at once', '', "WHERE LOWER(i.ext) IN ('.png') AND i.model_name IS ?", ['alpha']),
]

for label, joins, wsql, params in CASES:
    a = conn.execute(old_shape(joins, wsql), params).fetchall()
    b = conn.execute(new_shape(joins, wsql), params).fetchall()
    check('same tag counts through %s' % label, b, a)
    fa = conn.execute(old_fav(joins, wsql), params).fetchone()[0]
    fb = conn.execute(new_fav(joins, wsql), params).fetchone()[0]
    check('  ...and the same favourite count', fb, fa)

# Spot-check one by hand, so a bug that broke BOTH shapes identically cannot pass unnoticed.
check('the numbers are actually right for a .png-only filter',
      conn.execute(new_shape('', "WHERE LOWER(i.ext) IN ('.png')"), []).fetchall(),
      # Ties break on the tag itself, case-insensitively -- hence favourite-ish before forest.
      [('portrait', 3, 1), ('favourite-ish', 1, 0), ('forest', 1, 1), ('night', 1, 1)])

check("a source that is neither user's nor an extension's is never counted",
      [r[0] for r in conn.execute(new_shape('', "WHERE 1=1"), []).fetchall() if r[0] == 'ignored'],
      [])

# THE REGRESSION GUARD. This fault has arrived twice now -- first as EXISTS(SELECT 1 FROM images
# WHERE id=t.image_id), then as the JOIN that replaced it -- and both times it was a probe into the
# images table once per TAG row. The counting half must not reach the images table at all.
src = open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()
body = src[src.index('def api_tags(self'):]
body = body[:body.index('\n    def ')]
counting = body[body.index("with self._phase('taglist')"):]
check('the shipped counting query never joins or probes the images table',
      bool(re.search(r'JOIN\s+images\b|EXISTS\s*\(\s*SELECT[^)]*FROM\s+images', counting,
                     re.IGNORECASE)),
      False)
check('  ...and it does ask which images match, once, as a set',
      'IN (SELECT i.id FROM images i' in body, True)

conn.close()
print('\n%s\n' % ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
sys.exit(1 if _fails else 0)
