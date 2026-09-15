"""test_quick_scan.py — a rescan looks only where something changed, and touches nothing else.

Run: python test_quick_scan.py

A refresh for one new image used to cost a walk of every file in the library — ~6s on the author's
100k library over a share — because the scan ignored the per-folder signature it had already
collected for the ↻ indicator. It now walks only the folders whose timestamp moved.

**The risk is not that it misses a file; it is that it removes one.** The scan is the only thing
that prunes rows, and pruning takes an image's tags, labels, favourites, notes and quality score
with it. A quick scan that mistook "I didn't look there" for "it isn't there any more" would delete
curation that cannot be rebuilt. So the assertions below are mostly about what must SURVIVE:

  * a file in a folder nobody touched, and its curation;
  * every folder's signature entry, so the next check still notices the folders this scan skipped
    (writing the walked folders wholesale would blank the rest and make the library look new);
  * the whole library when the share is unreachable — the case that would otherwise read as "every
    folder was deleted".

And then the things it must still get right: a new file, a deleted file, a brand-new folder, and a
deleted folder.

Runs index_db directly against a temp tree — no server, no network.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db  # noqa: E402

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def png(path):
    """A real PNG header so image_probe reads it; content beyond that doesn't matter here."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (64, 64), (30, 40, 50)).save(path)


# Folder timestamps are compared with a 2-second tolerance (index_db.MTIME_TOL) because SMB reports
# them coarsely, so a test that changed a folder and looked immediately would see "no change" and
# prove nothing. Stamping the folder forward by a wide margin makes the change unambiguous without
# sleeping through the tolerance on every step.
_clock = [time.time() + 1000]


def bump(*dirs):
    _clock[0] += 100
    for d in dirs:
        os.utime(d, (_clock[0], _clock[0]))


def rows(db):
    conn = sqlite3.connect(db)
    out = {r[0]: r[1] for r in conn.execute("SELECT filename, id FROM images")}
    conn.close()
    return out


def quick(root, db):
    """A rescan the way server.run_scan does it when nothing forced a full walk."""
    cd = index_db.changed_dirs(db, root, 'A')
    assert cd is not None, 'no signature stored — the full scan did not complete'
    changed, gone = cd
    return index_db.scan(root, db, root_id='A',
                         only_dirs=set(changed) | {'.'}, gone_dirs=gone)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix='vv_quickscan_')
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp):
    root = os.path.join(tmp, 'lib')
    db = os.path.join(tmp, 'library.db')
    for f in ('alpha/a1.png', 'alpha/a2.png', 'beta/b1.png', 'beta/b2.png', 'gamma/g1.png'):
        png(os.path.join(root, f.replace('/', os.sep)))

    index_db.scan(root, db, root_id='A')            # first scan: no signature, so a full walk
    ids = rows(db)
    check('the first scan indexed everything', len(ids) == 5)

    # Curation on a file in a folder we will never touch again — the thing that must survive.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?, 'keeper', 'user')",
                 (ids['g1.png'],))
    conn.execute("UPDATE images SET note='hand written' WHERE id=?", (ids['g1.png'],))
    conn.commit()
    conn.close()

    print('\nA new file in one folder')
    png(os.path.join(root, 'alpha', 'a3.png'))
    bump(os.path.join(root, 'alpha'))
    st = quick(root, db)
    ids = rows(db)
    check('the new file is indexed', 'a3.png' in ids)
    check('nothing was removed', st['removed'] == 0 and len(ids) == 6)
    check('it walked one folder, not the library', st['total'] <= 3)

    print('\nA deleted file in one folder')
    os.remove(os.path.join(root, 'beta', 'b2.png'))
    bump(os.path.join(root, 'beta'))
    st = quick(root, db)
    ids = rows(db)
    check('the deleted file leaves the index', 'b2.png' not in ids)
    check('exactly one row went', st['removed'] == 1 and len(ids) == 5)

    print('\nThe folder nobody touched is untouched')
    conn = sqlite3.connect(db)
    keep = conn.execute("SELECT id, note FROM images WHERE filename='g1.png'").fetchone()
    tag = conn.execute("SELECT COUNT(*) FROM tags WHERE image_id=?", (keep[0],)).fetchone()[0]
    conn.close()
    check('its row survived every quick scan', keep is not None)
    check('so did its note', keep[1] == 'hand written')
    check('so did its tag', tag == 1)

    print('\nThe signature is merged, not replaced')
    # If a quick scan wrote only the folders it walked, every other folder would look new and the
    # next check would report the whole library as changed.
    sig = index_db.dir_signature(db, 'A')
    check('every folder still has an entry', set(sig) >= {'.', 'alpha', 'beta', 'gamma'})
    check('an untouched library reports no changes', not index_db.changes_detected(db, root, 'A'))
    png(os.path.join(root, 'gamma', 'g2.png'))
    bump(os.path.join(root, 'gamma'))
    check('a change in the skipped folder is still noticed',
          index_db.changes_detected(db, root, 'A'))
    quick(root, db)
    check('and the quick scan picks it up', 'g2.png' in rows(db))

    print('\nA brand-new folder, found through its parent')
    png(os.path.join(root, 'delta', 'd1.png'))
    bump(root)                                       # a new folder is found via its PARENT
    quick(root, db)
    ids = rows(db)
    check('the new folder was walked', 'd1.png' in ids)
    check('it is in the signature now', 'delta' in index_db.dir_signature(db, 'A'))

    print('\nA deleted folder takes its rows')
    shutil.rmtree(os.path.join(root, 'beta'))
    bump(root)
    st = quick(root, db)
    ids = rows(db)
    check('its remaining file left the index', 'b1.png' not in ids)
    check('nothing else went', set(ids) == {'a1.png', 'a2.png', 'a3.png', 'g1.png', 'g2.png',
                                            'd1.png'})
    check('and it left the signature', 'beta' not in index_db.dir_signature(db, 'A'))

    print('\nAn unreachable library prunes NOTHING')
    # The catastrophic case: every folder unreadable reads as "every folder was deleted". Simulated
    # by pointing the same signature at a root that is missing its folders, which is what a dropped
    # share looks like from here.
    before = set(rows(db))
    ghost = os.path.join(tmp, 'ghost')
    os.makedirs(ghost)
    conn = sqlite3.connect(db)
    sig = index_db.dir_signature(db, 'A')
    conn.close()
    cd = index_db.changed_dirs(db, ghost, 'A')
    gone = cd[1] if cd else set()
    check('every folder reads as missing', len(gone) >= len(sig) - 1)
    index_db.scan(ghost, db, root_id='A', only_dirs={'.'} | set(cd[0]), gone_dirs=gone)
    check('the library is still there', set(rows(db)) == before)

    print('\nA quick scan is only trusted for so long')
    # The net under everything above. Folder timestamps can lie — SMB caches them, the comparison
    # tolerates 2s of jitter, and none of it sees a file edited in place — so a change can stay
    # invisible until something else touches that folder, which for a finished folder is never.
    # The author hit that: a set deleted by hand survived repeated rescans and only went on a forced
    # rebuild. The clock below is what stops any such miss lasting indefinitely, whatever caused it.
    age = index_db.full_scan_age(db, 'A')
    check('a full walk sets the clock', age is not None and age < 60)
    quick(root, db)
    check('a quick scan does NOT reset it', abs(index_db.full_scan_age(db, 'A') - age) < 5)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value=? WHERE key='full_scan_at:A'",
                 (str(time.time() - index_db.FULL_SCAN_MAX_AGE - 60),))
    conn.commit()
    conn.close()
    check('once it expires, the next scan is due a full walk',
          index_db.full_scan_age(db, 'A') > index_db.FULL_SCAN_MAX_AGE)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta WHERE key='full_scan_at:A'")
    conn.commit()
    conn.close()
    check('and a library that has never had one is too',
          index_db.full_scan_age(db, 'A') is None)

    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
