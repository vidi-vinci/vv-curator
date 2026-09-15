"""Song lyrics are in the search index, and an existing library gets there without a rescan.

Run: python test_lyrics_search.py

A song's style caption has always lived in `positive` and been searchable, so a song could be found
by how it SOUNDS ("chillhop") and never by what it SAYS. `lyrics` was a column on `images` that no
index looked at. Schema 5 adds it as a fifth FTS column.

Three things are pinned here, and the second is the one that matters to anyone updating:

  1. A row with lyrics is findable by a word that appears ONLY in them — not in the caption, the
     filename or the folder. That is the whole feature.
  2. A schema-4 database picks the lyrics up ON CONNECT, from the `images` table, with no scan and
     no file read. Every value the index holds is already a column, so re-reading the library would
     be an hour of I/O to learn what SQLite already knows.
  3. Rename and delete leave the index CONSISTENT. An external-content FTS is deleted from by being
     handed back the values it indexed, so a write path that names four columns while the index has
     five corrupts it silently — the rows stop matching and nothing raises. Both paths run here and
     both are integrity-checked, which is what turns that from silent into a failed test.

The real functions do the writing (`index_db._fts_insert`, `_fts_delete`, `delete_by_ids`,
`connect`), not hand-written SQL: the fault this guards against is a call site that forgot a
column, and a fixture with its own INSERT would never have the call sites in it.

Reading lyrics out of an MP3 is NOT tested here — that already worked and did not change. This is
about what the index does with them once they are a column.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db                                            # noqa: E402

TMP = tempfile.mkdtemp(prefix='vv-lyrics-')
DB = os.path.join(TMP, 'library.db')

# A word in the lyrics and nowhere else, so a hit can only have come from the lyrics column.
LYRIC_ONLY = 'crestfallen'
LYRICS = '[Verse]\nA %s hour, and the tape hiss holds\n[Chorus]\nSpecial is you and me' % LYRIC_ONLY
CAPTION = 'Global Metadata: Lo-fi hip-hop, chillhop. 78 BPM, warm vinyl crackle.'

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def fts_cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(images_fts)")]


def match(conn, term):
    return sorted(r[0] for r in conn.execute(
        "SELECT i.filename FROM images_fts f JOIN images i ON i.id=f.rowid "
        "WHERE images_fts MATCH ?", (term,)))


def integrity(conn):
    """FTS5's own consistency check. Raises when the index and the table disagree."""
    try:
        conn.execute("INSERT INTO images_fts(images_fts) VALUES('integrity-check')")
        return True
    except Exception as e:
        return str(e)


def add(conn, filename, positive, lyrics):
    cur = conn.execute(
        "INSERT INTO images(path, rel_path, folder, filename, ext, mtime, size, positive, lyrics, "
        "root_id, indexed_at) VALUES (?,?,?,?,?,0,0,?,?,'rk',0)",
        ('/x/' + filename, filename, 'songs', filename, os.path.splitext(filename)[1],
         positive, lyrics))
    index_db._fts_insert(conn, cur.lastrowid, positive, '', filename, 'songs', lyrics)
    return cur.lastrowid


print('\nLyrics in the search index\n')

conn = index_db.connect(DB)
conn.row_factory = sqlite3.Row
check('the index has a lyrics column', 'lyrics' in fts_cols(conn), fts_cols(conn))

sid = add(conn, 'MM_Song.mp3', CAPTION, LYRICS)
pid = add(conn, 'plain.png', 'a photograph of a hill', None)   # lyrics NULL, the ordinary case
conn.commit()

# 1. THE FEATURE.
check('a lyric word finds the song', match(conn, LYRIC_ONLY) == ['MM_Song.mp3'],
      match(conn, LYRIC_ONLY))
check('  and the caption still does', match(conn, 'chillhop') == ['MM_Song.mp3'],
      match(conn, 'chillhop'))
check('  and a word in neither finds nothing', match(conn, 'zzzznotaword') == [])
check('  and an image with no lyrics is unaffected', match(conn, 'photograph') == ['plain.png'],
      match(conn, 'photograph'))
conn.close()

# 2. THE MIGRATION. Put the DB back the way a schema-4 library on disk looks, then just OPEN it —
#    which is all a launch after updating does.
OLD = os.path.join(TMP, 'old.db')
shutil.copyfile(DB, OLD)
c = sqlite3.connect(OLD)
c.execute("DROP TABLE IF EXISTS images_fts")
c.execute("CREATE VIRTUAL TABLE images_fts USING fts5(positive, model_name, filename, folder, "
          "content='images', content_rowid='id', tokenize='unicode61')")
c.execute("INSERT INTO images_fts(rowid, positive, model_name, filename, folder) "
          "SELECT id, COALESCE(positive,''), COALESCE(model_name,''), COALESCE(filename,''), "
          "COALESCE(folder,'') FROM images")
c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version','4')")
c.commit()
before = c.execute("SELECT count(*) FROM images_fts WHERE images_fts MATCH ?",
                   (LYRIC_ONLY,)).fetchone()[0]
c.close()
check('a schema-4 index does NOT find the lyric', before == 0, before)

conn = index_db.connect(OLD)
conn.row_factory = sqlite3.Row
check('  opening it migrates to 5',
      conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '5')
check('  the lyrics column arrives', 'lyrics' in fts_cols(conn), fts_cols(conn))
check('  and the lyric is findable, with no rescan', match(conn, LYRIC_ONLY) == ['MM_Song.mp3'],
      match(conn, LYRIC_ONLY))
check('  while everything already indexed still matches',
      match(conn, 'chillhop') == ['MM_Song.mp3'] and match(conn, 'plain') == ['plain.png'])
conn.close()

# Idempotent: opening again must not rebuild or lose anything.
conn = index_db.connect(OLD)
conn.row_factory = sqlite3.Row
check('  and a second open changes nothing', match(conn, LYRIC_ONLY) == ['MM_Song.mp3'])
conn.close()

# 3. THE WRITE PATHS. A rename deletes the row from the index and puts it back — server.py's
#    api_rename — so it has to carry every indexed column across.
conn = index_db.connect(DB)
conn.row_factory = sqlite3.Row
r = conn.execute("SELECT positive, model_name, folder, lyrics FROM images WHERE id=?", (sid,)).fetchone()
index_db._fts_delete(conn, sid)
conn.execute("UPDATE images SET filename=? WHERE id=?", ('Renamed.mp3', sid))
index_db._fts_insert(conn, sid, r['positive'], r['model_name'], 'Renamed.mp3', r['folder'], r['lyrics'])
conn.commit()
check('a rename keeps the lyric searchable', match(conn, LYRIC_ONLY) == ['Renamed.mp3'],
      match(conn, LYRIC_ONLY))
check('  and leaves the index consistent', integrity(conn) is True, integrity(conn))
conn.close()

# A delete has to hand back what it indexed. Both rows go: one whose lyrics are set, one whose are
# NULL — the insert stored '' for that one, and a mismatch between the two is how this goes bad.
check('deleting an image with no lyrics works', index_db.delete_by_ids(DB, [pid]) == 1)
check('deleting a song with lyrics works', index_db.delete_by_ids(DB, [sid]) == 1)
conn = index_db.connect(DB)
conn.row_factory = sqlite3.Row
check('  nothing is left in the index', conn.execute("SELECT count(*) FROM images_fts").fetchone()[0] == 0)
check('  and it is still consistent', integrity(conn) is True, integrity(conn))
conn.close()

shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
