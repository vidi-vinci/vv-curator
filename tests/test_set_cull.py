"""'Apply to folder': repeating one image set's keep decision across the rest of its folder.

The whole feature is a batch DELETE derived from one demonstration, so what has to be pinned is
which sets it decides to touch — the numbers the user is shown before agreeing, and the batch the
server later recomputes from the same function.

Load-bearing claims:

  * shape matching is EXACT — a (main, det, refine) demonstration never touches a (main, refine)
    set sitting in the same folder;
  * the kept STAGE is kept, not the kept POSITION — proved with a set whose members were written
    in a different filename order from the anchor's;
  * subfolders are untouched (folder equality, never a LIKE prefix);
  * other libraries are untouched;
  * a set with two members in the same role has a different SHAPE, so it is never a match — which
    is what makes "which one do I keep" un-askable rather than guessed at;
  * a legacy MAIN/DET/REFINE set and a stamped Raw/Detail/Refine set are ONE shape, not two — they
    are the same pipeline stages under older names, and a mixed library must not split in half;
  * still+video pairs are never image sets, so they are never touched;
  * the anchor's own set is included in its own operation;
  * an offline library is refused outright rather than silently culled (a recycle purges rows whose
    file is missing, and on a downed share every file looks missing).

Run:  python test_set_cull.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Keep importing server.py from touching the real config/data dir.
# THE SYSTEM TEMP, NOT THE PROJECT FOLDER — 20 places said `dir=here`/`dir=BASE`, and every one
# of them was wrong for the same reason. Each cleans up in a finally with ignore_errors=True, and
# on Windows the server thread still holds library.db open when that runs, so the removal fails
# SILENTLY and the husk stays. 25 of them, 503MB, had accumulated in the master folder before the author
# spotted them. A test's mess belongs where the OS already sweeps up.
_tmp = tempfile.mkdtemp(prefix='vv_cull_')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')

# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db                                     # noqa: E402
import server                                       # noqa: E402

_fails = []
_next_id = [0]


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + f'{label}   -> {got}' + ('' if ok else f'  (wanted {want})'))
    if not ok:
        _fails.append(label)


ROOT_PATHS = {}


def add(conn, root, folder, filename, stage=None, group=None, ext=None):
    """One row in the index, plus a REAL file on disk behind it — _recycle_ids counts a row whose
    file is missing as 'purged' rather than 'moved', so a fixture of paths-only would silently test
    the wrong branch. group is the set key; stage is comfy_vv_saver's stamp (None = the role has to
    be read out of the filename, which is the legacy path)."""
    _next_id[0] += 1
    iid = _next_id[0]
    ext = ext or os.path.splitext(filename)[1]
    d = os.path.join(ROOT_PATHS[root], *folder.split('/'))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, filename)
    with open(path, 'wb') as f:
        f.write(b'x' * 10)
    conn.execute(
        "INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size, group_id,"
        " set_stage, root_id, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (iid, path, f'{folder}/{filename}', folder, filename, ext,
         time.time() + iid, 10, group, stage, root, time.time()))
    return iid


def ids(matches):
    """[(keeper, [doomed...])] -> a comparable, order-free view."""
    return sorted((k, tuple(d)) for k, d in matches)


def main():
    data = os.environ['CV_DATA']
    os.makedirs(data, exist_ok=True)
    # Two real directories, so _reachable_root_ids sees both libraries as online.
    r1 = os.path.join(_tmp, 'lib1')
    r2 = os.path.join(_tmp, 'lib2')
    os.makedirs(r1, exist_ok=True)
    os.makedirs(r2, exist_ok=True)
    server.CONFIG['roots'] = [{'key': 'r1', 'path': r1, 'name': 'One'},
                              {'key': 'r2', 'path': r2, 'name': 'Two'}]
    ROOT_PATHS.update(r1=r1, r2=r2)

    dbp = os.path.join(data, 'library.db')
    conn = index_db.connect(dbp)
    conn.row_factory = sqlite3.Row          # what server.db() hands every caller
    server.ACTIVE.update(key='r1', path=r1, db=dbp)   # what the running job writes through

    # --- the anchor: a plain stamped (raw, detail, refine) triple in folder A -------------------
    a_raw = add(conn, 'r1', 'A', 'gen 1 MAIN.png', 'Raw', 'gA')
    a_det = add(conn, 'r1', 'A', 'gen 1 DET.png', 'Detail', 'gA')
    a_ref = add(conn, 'r1', 'A', 'gen 1 REFINE.png', 'Refine', 'gA')

    # a second triple of the same shape, written in a DIFFERENT filename order — its refine is the
    # alphabetically FIRST member, so keeping "the third pane" would keep the wrong file here
    b_ref = add(conn, 'r1', 'A', 'aaa second.png', 'Refine', 'gB')
    b_raw = add(conn, 'r1', 'A', 'mmm second.png', 'Raw', 'gB')
    b_det = add(conn, 'r1', 'A', 'zzz second.png', 'Detail', 'gB')

    # a legacy set with no stamps at all: roles come out of the filenames
    c_main = add(conn, 'r1', 'A', 'old 1 MAIN.png', None, 'gC')
    c_det = add(conn, 'r1', 'A', 'old 2 DET.png', None, 'gC')
    c_ref = add(conn, 'r1', 'A', 'old 3 REFINE.png', None, 'gC')

    # a PAIR of the shape (main, refine) — one stage short, so out of scope
    add(conn, 'r1', 'A', 'short 1 MAIN.png', 'Raw', 'gD')
    add(conn, 'r1', 'A', 'short 3 REFINE.png', 'Refine', 'gD')

    # three members, but TWO of them refines — (raw, refine, refine) is a different shape, so the
    # "which refine did you mean" question never gets asked in the first place
    add(conn, 'r1', 'A', 'twin 1 MAIN.png', 'Raw', 'gE')
    add(conn, 'r1', 'A', 'twin 3a REFINE.png', 'Refine', 'gE')
    add(conn, 'r1', 'A', 'twin 3b REFINE.png', 'Refine', 'gE')

    # a still+video pair living in the same folder: a pair, never an image set
    add(conn, 'r1', 'A', 'clip.png', None, 'gF')
    add(conn, 'r1', 'A', 'clip.mp4', None, 'gF', ext='.mp4')

    # an identical triple one folder DOWN, and one in the other library
    add(conn, 'r1', 'A/sub', 'deep 1 MAIN.png', 'Raw', 'gG')
    add(conn, 'r1', 'A/sub', 'deep 2 DET.png', 'Detail', 'gG')
    add(conn, 'r1', 'A/sub', 'deep 3 REFINE.png', 'Refine', 'gG')
    add(conn, 'r2', 'A', 'other 1 MAIN.png', 'Raw', 'gH')
    add(conn, 'r2', 'A', 'other 2 DET.png', 'Detail', 'gH')
    add(conn, 'r2', 'A', 'other 3 REFINE.png', 'Refine', 'gH')

    # a second folder of three identical triples, for the Stop test further down
    for stem in ('p', 'q', 'r'):
        for role, stage in (('1 MAIN', 'Raw'), ('2 DET', 'Detail'), ('3 REFINE', 'Refine')):
            add(conn, 'r1', 'B', f'{stem} {role}.png', stage, 'g' + stem)

    # a lone image in the same folder — no group at all
    add(conn, 'r1', 'A', 'single.png', None, None)
    conn.commit()
    # The rows went in with raw INSERTs, which the external-content FTS index knows nothing about;
    # the real recycle path deletes from it, and an out-of-sync FTS fails there as "database disk
    # image is malformed". Sync it once, so the delete under test is the app's real one.
    conn.execute("INSERT INTO images_fts(images_fts) VALUES('rebuild')")
    conn.commit()

    print('\nmatching')
    matches, st = server.find_setcull_sets(conn, a_ref, 'refine')
    check('keeps the refine of every matching set', ids(matches),
          ids([(a_ref, [a_raw, a_det]), (b_ref, [b_raw, b_det]), (c_ref, [c_main, c_det])]))
    check('the anchor set is included', any(k == a_ref for k, _d in matches), True)
    check('stage wins over pane position', b_ref in [k for k, _d in matches], True)
    check('legacy filename roles match a stamped set of the same shape',
          c_ref in [k for k, _d in matches], True)

    print('\nthe counts the user is shown')
    check('sets', st['sets'], 3)
    check('files (what actually goes)', st['files'], 6)
    check('total sets in the folder', st['total_sets'], 5)   # A/B/C + the (main,refine) + the twin
    check('different shape, left alone', st['skipped_shape'], 2)   # the pair-shape one and the twin
    check('folder', st['folder'], 'A')

    print('\nscope')
    touched = {i for k, d in matches for i in d} | {k for k, _d in matches}
    subfolder = {r['id'] for r in conn.execute("SELECT id FROM images WHERE folder='A/sub'")}
    other_lib = {r['id'] for r in conn.execute("SELECT id FROM images WHERE root_id='r2'")}
    pair = {r['id'] for r in conn.execute("SELECT id FROM images WHERE group_id='gF'")}
    twin = {r['id'] for r in conn.execute("SELECT id FROM images WHERE group_id='gE'")}
    short = {r['id'] for r in conn.execute("SELECT id FROM images WHERE group_id='gD'")}
    check('subfolder untouched', touched & subfolder, set())
    check('other library untouched', touched & other_lib, set())
    check('still+video pair untouched', touched & pair, set())
    check('the two-refine set untouched', touched & twin, set())
    check('different shape untouched', touched & short, set())

    print('\nkeeping a different stage')
    matches_raw, st_raw = server.find_setcull_sets(conn, a_raw, 'raw')
    check('same sets match whichever stage is demonstrated', st_raw['sets'], 3)
    check('now the raws survive', sorted(k for k, _d in matches_raw), sorted([a_raw, b_raw, c_main]))

    print('\nrefusals')
    for label, iid, role, want in [
        ('a role not in this set', a_ref, 'upscale', 'that role is not in this set'),
        ('an image in no set', _next_id[0], 'refine', 'this image is not part of a set'),
    ]:
        try:
            server.find_setcull_sets(conn, iid, role)
            got = '(no error)'
        except ValueError as e:
            got = str(e)
        check(label, got, want)

    server.CONFIG['roots'] = [{'key': 'r1', 'path': os.path.join(_tmp, 'gone'), 'name': 'One'}]
    try:
        server.find_setcull_sets(conn, a_ref, 'refine')
        got = '(no error)'
    except ValueError as e:
        got = str(e)
    check('an offline library is refused, not culled', got, 'that library is offline')
    server.CONFIG['roots'] = [{'key': 'r1', 'path': r1, 'name': 'One'},
                              {'key': 'r2', 'path': r2, 'name': 'Two'}]

    # ---- the cull itself ----------------------------------------------------------------------
    # Curation that must land on the keepers rather than going in the bin with the files carrying it.
    conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?,?,?)", (a_raw, 'sunset', 'user'))
    conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?,?,?)", (a_det, 'fav', 'fav'))
    conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?,?,?)", (b_raw, 'label:to-publish', 'user'))
    conn.execute("UPDATE images SET note='worth keeping' WHERE id=?", (c_main,))
    conn.commit()

    _matches, st_before = server.find_setcull_sets(conn, a_ref, 'refine')
    doomed_before = [i for _k, d in _matches for i in d]
    keepers = [k for k, _d in _matches]

    # Recycle for real, minus the trip to the OS bin: _recycle_one is the one step that would put
    # test files in the user's actual Recycle Bin. Everything else — the index purge, the counts,
    # the merge ordering — runs exactly as it does in the app.
    binned = []
    real_recycle_one = server._recycle_one
    server._recycle_one = lambda path, anchor_root=None: (binned.append(path), os.remove(path), set())[2]
    try:
        print('\nthe cull')
        started = server.run_setcull(a_ref, 'refine')
        check('the job starts', started, True)
        for _ in range(200):
            if not server._setcull_state['running']:
                break
            time.sleep(0.05)
        s = server._setcull_state
        check('it did not error', s['error'], None)
        check('every matching set was culled', s['seen'], st_before['sets'])
        check('PREVIEW EQUALS REALITY: files moved == files promised', s['moved'], st_before['files'])
        check('nothing was merely purged from the index', s['purged'], 0)
        check('nothing failed', s['failed'], 0)
        check('a second run is refused while one is in flight',
              server.run_setcull(a_ref, 'refine') if s['running'] else 'not running', 'not running')
    finally:
        server._recycle_one = real_recycle_one

    fresh = sqlite3.connect(dbp)
    fresh.row_factory = sqlite3.Row
    gone = [i for i in doomed_before
            if fresh.execute("SELECT 1 FROM images WHERE id=?", (i,)).fetchone() is None]
    check('every doomed row left the index', len(gone), len(doomed_before))
    check('every doomed FILE left the folder', len(binned), len(doomed_before))
    check('the keepers are all still there',
          len([k for k in keepers
               if fresh.execute("SELECT 1 FROM images WHERE id=?", (k,)).fetchone()]), len(keepers))
    tags_a = {r['tag'] for r in fresh.execute("SELECT tag FROM tags WHERE image_id=?", (a_ref,))}
    check('a tag from a binned member moved to the keeper', 'sunset' in tags_a, True)
    check('a favorite from a binned member moved to the keeper',
          fresh.execute("SELECT 1 FROM tags WHERE image_id=? AND source='fav'",
                        (a_ref,)).fetchone() is not None, True)
    check('a label from a binned member moved to the keeper',
          fresh.execute("SELECT 1 FROM tags WHERE image_id=? AND tag='label:to-publish'",
                        (b_ref,)).fetchone() is not None, True)
    check('a note from a binned member moved to the keeper',
          (fresh.execute("SELECT note FROM images WHERE id=?", (c_ref,)).fetchone()['note'] or ''),
          'worth keeping')
    check('the survivors stop being sets',
          [r['group_id'] for r in fresh.execute(
              "SELECT group_id FROM images WHERE id IN (%s)" % ','.join('?' * len(keepers)), keepers)
           if r['group_id']], [])
    survivors = {r['id'] for r in fresh.execute("SELECT id FROM images WHERE folder='A/sub'")}
    check('the subfolder still has its whole set', len(survivors), 3)
    check('the other library still has its whole set',
          fresh.execute("SELECT COUNT(*) FROM images WHERE root_id='r2'").fetchone()[0], 3)
    # by filename, not by the fixture's group key: recompute_groups re-derives real group ids, so
    # the fake 'gF' is gone from a pair that is otherwise perfectly intact
    pair = fresh.execute("SELECT group_id FROM images WHERE filename IN ('clip.png','clip.mp4')").fetchall()
    check('the still+video pair still has both files', len(pair), 2)
    check('and they are still one pair', len({r['group_id'] for r in pair}) == 1 and pair[0]['group_id'] is not None, True)

    # ---- Stop keeps what is done and abandons the rest ----------------------------------------
    # The claim that makes Stop safe to offer at all: the cull commits ONE SET AT A TIME, so
    # stopping can never leave a set half-culled. Cancel is raised from inside the first set's
    # recycle, which is exactly when a real Stop would arrive.
    print('\nStop, mid-run')
    b_anchor = fresh.execute(
        "SELECT id FROM images WHERE folder='B' AND filename LIKE '%REFINE%' ORDER BY id").fetchone()['id']
    _m, st_b = server.find_setcull_sets(conn, b_anchor, 'refine')
    real_ids = server._recycle_ids

    def cancel_after_first(ids):
        server._setcull_state['cancel'] = True      # as if Stop were pressed during this set
        return real_ids(ids)

    server._recycle_one = lambda path, anchor_root=None: (binned.append(path), os.remove(path), set())[2]
    server._recycle_ids = cancel_after_first
    try:
        server.run_setcull(b_anchor, 'refine')
        for _ in range(200):
            if not server._setcull_state['running']:
                break
            time.sleep(0.05)
    finally:
        server._recycle_ids = real_ids
        server._recycle_one = real_recycle_one
    s = server._setcull_state
    check('it reports as stopped', s['stopped'], True)
    check('the set it was on is finished', s['seen'], 1)
    check('it did not do them all', st_b['sets'], 3)
    after = sqlite3.connect(dbp)
    after.row_factory = sqlite3.Row
    check('exactly one set-worth of files went',
          after.execute("SELECT COUNT(*) FROM images WHERE folder='B'").fetchone()[0], 9 - 2)
    check('no set is left half-culled',
          sorted(r[0] for r in after.execute(
              "SELECT COUNT(*) FROM images WHERE folder='B' AND group_id IS NOT NULL GROUP BY group_id")),
          [3, 3])
    after.close()
    fresh.close()

    conn.close()
    print('\n' + ('FAILED: ' + ', '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(code)
