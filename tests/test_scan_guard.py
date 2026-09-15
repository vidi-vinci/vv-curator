"""test_scan_guard.py -- one file the scan cannot read costs you that file, not the refresh.

Run: python tests/test_scan_guard.py

The author's refresh stopped dead with a bare "Python int too large to convert to SQLite INTEGER" and
~78,000 files behind it unlooked-at. The seed that caused it is fixed (test_big_seed.py); this pins
the reason ONE odd file could do that much damage -- the scan's file loop had no guard at all, so
any single surprise ended the whole run.

Everything in that loop reads a file the app did not write -- a container, a PNG chunk, a graph --
and hands what it finds to SQLite. There will be another surprise.

**The dangerous half is not the crash, it is the recovery**, and that is what most of this file
asserts. The scan is the only thing that PRUNES rows, and it prunes by asking which paths it saw.
So the guard sits deliberately below `seen.add(path)`: a file that failed is still SEEN, its row
survives, and so do its tags, labels, favourite, note and quality score. A guard one line higher
would silently convert "I could not read this file" into "this file is gone" and take curation with
it -- the one outcome a rescan may never produce, because curation cannot be rebuilt.

And it must not be silent: the count and the first file's name come back in the stats, because a
refresh that quietly drops files is worse than one that stops -- nothing would tell you to look.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta  # noqa: E402
import index_db  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


def png(path, colour=(30, 40, 50)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (64, 64), colour).save(path)


print('\nOne unreadable file costs that file, not the refresh\n')

root = tempfile.mkdtemp(prefix='vv_guard_')
db = os.path.join(root, 'lib.db')
names = ['a.png', 'bad.png', 'c.png']
for nm in names:
    png(os.path.join(root, 'imgs', nm))

first = index_db.scan(root, db, thumbs_dir=None)
check('all three indexed to begin with', first.get('added') == 3, first)

# Curation on the file that is about to fail. `note` is a column the rescan UPDATE deliberately
# never names; the tag is a separate row keyed on the image id. Both are unrecoverable if the row
# goes, which is exactly why they are the thing under test.
conn = sqlite3.connect(db)
bad_id = conn.execute("SELECT id FROM images WHERE filename='bad.png'").fetchone()[0]
conn.execute("UPDATE images SET note=? WHERE id=?", ('keep me', bad_id))
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES (?,?,?)", (bad_id, 'favourite-shot', 'manual'))
conn.commit()
conn.close()

# Make every file look changed, so the rescan actually re-reads them rather than skipping on
# mtime -- an unchanged file never reaches the guarded body, and the test would prove nothing.
later = time.time() + 5000
for nm in names:
    os.utime(os.path.join(root, 'imgs', nm), (later, later))

# The failure itself, injected at the level the guard is meant to cover: reading the file. Any
# exception from anywhere in the body is the same case, and a real one (OverflowError from the
# SQLite bind) is what happened to the author.
real_extract = comfy_meta.extract


def exploding_extract(path, *a, **kw):
    if os.path.basename(path) == 'bad.png':
        raise OverflowError('Python int too large to convert to SQLite INTEGER')
    return real_extract(path, *a, **kw)


comfy_meta.extract = exploding_extract
try:
    stats = index_db.scan(root, db, thumbs_dir=None)
finally:
    comfy_meta.extract = real_extract

check('the scan finished instead of raising', isinstance(stats, dict), stats)
check('it reports one failure', stats.get('failed') == 1, stats.get('failed'))
check('it names the file that failed', 'bad.png' in (stats.get('first_error') or ''),
      stats.get('first_error'))
check('it says why', 'too large' in (stats.get('first_error') or ''), stats.get('first_error'))
check('the other two were still processed', stats.get('updated') == 2, stats)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# THE POINT OF THE WHOLE FILE. A failed file must not be mistaken for a deleted one.
check('nothing was pruned', stats.get('removed') == 0, stats.get('removed'))
rows = {r['filename']: r for r in conn.execute("SELECT id, filename, note FROM images")}
check('all three rows are still there', sorted(rows) == sorted(names), sorted(rows))
check('the failed file kept its row, same id', rows.get('bad.png') and rows['bad.png']['id'] == bad_id,
      rows.get('bad.png') and rows['bad.png']['id'])
check('the failed file kept its note', rows.get('bad.png') and rows['bad.png']['note'] == 'keep me',
      rows.get('bad.png') and rows['bad.png']['note'])
tags = [r[0] for r in conn.execute("SELECT tag FROM tags WHERE image_id=?", (bad_id,))]
check('the failed file kept its tag', tags == ['favourite-shot'], tags)
conn.close()

# A clean scan must stay clean and stay quiet -- the summary is shown only when failed > 0, so a
# stray non-zero here would put an error tail on every ordinary refresh.
comfy_meta.extract = real_extract
for nm in names:
    os.utime(os.path.join(root, 'imgs', nm), (later + 5000, later + 5000))
clean = index_db.scan(root, db, thumbs_dir=None)
check('a clean scan reports no failures', clean.get('failed') == 0, clean.get('failed'))
check('and carries no error to report', clean.get('first_error') is None, clean.get('first_error'))

print()
sys.exit(1 if _fails else 0)
