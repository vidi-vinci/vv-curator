"""test_latest_count.py — a new file is found even when its folder's timestamp does not move.

Run: python test_latest_count.py

On the author's desktop a new image sat on disk for over two minutes while its folder's mtime, as the app read
it, had not moved, so every refresh said "up to date". The latest folder's file count is now
compared as well. This pins:

  * a new file in the latest folder, with that folder's mtime held still, is still a change, and
    the quick scan that follows looks in that folder and indexes it;
  * after that scan the library reads unchanged again, so it does not rescan on every check;
  * the app's own recycle out of the latest folder does not flag it.

Runs index_db directly against a temp tree — no server, no network.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db  # noqa: E402

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def png(path):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (64, 64), (30, 40, 50)).save(path)


def held(folder, fn):
    """Run fn with the folder's mtime put back afterwards: the symptom, made exact."""
    st = os.stat(folder)
    fn()
    os.utime(folder, (st.st_atime, st.st_mtime))


def quick(root, db):
    changed, gone = index_db.changed_dirs(db, root, 'A')
    return index_db.scan(root, db, root_id='A', only_dirs=set(changed) | {'.'}, gone_dirs=gone)


def main():
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, 'lib')
        old, out = os.path.join(root, 'old'), os.path.join(root, 'out')
        png(os.path.join(old, 'o1.png'))
        png(os.path.join(out, 'a.png'))
        os.utime(old, (1000, 1000))            # 'out' is the folder that changed last
        db = os.path.join(tmp, 'x.db')
        index_db.scan(root, db, root_id='A')

        print('\nA new file whose folder timestamp does not move\n')
        check('nothing changed yet', not index_db.changes_detected(db, root, 'A'))
        held(out, lambda: png(os.path.join(out, 'b.png')))
        check('the folder mtime really is unchanged',
              not index_db.changed_dirs(db, root, 'A')[0] - {'out'})
        check('the change check sees it', index_db.changes_detected(db, root, 'A'))
        check('the quick scan is told to look in that folder',
              'out' in index_db.changed_dirs(db, root, 'A')[0])
        st = quick(root, db)
        check('and indexes the new file', st['added'] == 1)
        check('after which the library reads unchanged again',
              not index_db.changes_detected(db, root, 'A'))

        print('\nThe app\'s own recycle\n')
        held(out, lambda: os.remove(os.path.join(out, 'a.png')))
        index_db.refresh_dir_sig(db, root, 'A', ['out'])
        check('does not flag the folder', not index_db.changes_detected(db, root, 'A'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + ('all passed' if not _fails else f'{len(_fails)} FAILED'))
    sys.exit(1 if _fails else 0)


if __name__ == '__main__':
    main()
