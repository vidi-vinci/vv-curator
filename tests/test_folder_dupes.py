"""test_folder_dupes.py — duplicate-copy culling used by the detail view's "Delete copies…".

Finds files that exist in more than one folder (same name + size + timestamp), keeps the one in the
folder you're viewing, and moves the copies' curation onto it before they're recycled.

Two things here are load-bearing and destructive if wrong:
  * an UNREACHABLE library must contribute nothing — _recycle_ids purges rows whose files are
    missing, and on an unplugged drive every file looks missing, so a cull would strip that
    library's tags/labels/favorites/notes/scores while the files sit safe on the disconnected disk;
  * curation on a deleted copy must land on the keeper, never be dropped.

Run: python test_folder_dupes.py
"""
import os
import shutil
import sqlite3
import tempfile

os.environ.setdefault('CV_CONFIG', os.path.join(tempfile.gettempdir(), 'vv_dupes_cfg.json'))
os.environ.setdefault('CV_DATA', tempfile.mkdtemp(prefix='vv_dupes_data_'))

import sys
# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db                                          # noqa: E402
import server                                            # noqa: E402

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def open_db(path):
    """Same shape of connection the server uses — server.db() sets a Row factory, and the code
    under test reads columns by name."""
    conn = index_db.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def add(conn, iid, root, folder, name, size, mtime, note=None):
    conn.execute(
        "INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size, "
        "note, root_id, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
        (iid, f"/{root}/{folder}/{name}", f"{folder}/{name}", folder, name,
         os.path.splitext(name)[1], mtime, size, note, root))
    # keep the FTS external-content table in step, as a real scan would — delete_by_ids removes the
    # FTS row too and errors on a table we never populated
    index_db._fts_insert(conn, iid, '', '', name, folder)
    return iid


def tag(conn, iid, t, source='user'):
    conn.execute("INSERT OR IGNORE INTO tags(image_id, tag, source, score) VALUES(?,?,?,NULL)",
                 (iid, t, source))


def tags_of(conn, iid):
    return sorted(r[0] for r in conn.execute("SELECT tag FROM tags WHERE image_id=?", (iid,)))


def labels_of(conn, iid):
    return [t for t in tags_of(conn, iid) if t.startswith('label:')]


def note_of(conn, iid):
    return (conn.execute("SELECT note FROM images WHERE id=?", (iid,)).fetchone()[0] or '')


def main():
    """Temp dirs live in the project folder, so cleanup must survive a failing check — otherwise a
    red run litters the repo (and shows up in git status)."""
    tmp = tempfile.mkdtemp(prefix='vv_dupes_')
    try:
        return logic_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def logic_checks(tmp):
    # two libraries that exist on disk, one that doesn't (the unplugged drive)
    live_a = os.path.join(tmp, 'libA'); os.makedirs(live_a)
    live_b = os.path.join(tmp, 'libB'); os.makedirs(live_b)
    gone_c = os.path.join(tmp, 'libC_unplugged')          # deliberately never created
    server.CONFIG = {'roots': [{'key': 'A', 'name': 'Main', 'path': live_a},
                               {'key': 'B', 'name': 'Archive', 'path': live_b},
                               {'key': 'C', 'name': 'Offline', 'path': gone_c}]}

    conn = open_db(os.path.join(tmp, 'library.db'))
    T = 1_700_000_000.0

    # the folder we're viewing, in library A — these are the keepers
    keep1 = add(conn, 1, 'A', 'keep', 'shot.png', 1000, T)
    keep2 = add(conn, 2, 'A', 'keep', 'other.png', 2000, T)
    add(conn, 3, 'A', 'keep', 'unique.png', 3000, T)      # no copies anywhere

    # copies elsewhere
    cp_same_root = add(conn, 10, 'A', 'backup', 'shot.png', 1000, T + 0.3)   # sub-second drift
    cp_other_lib = add(conn, 11, 'B', 'stash', 'shot.png', 1000, T)          # another library
    cp_offline = add(conn, 12, 'C', 'stash', 'shot.png', 1000, T)            # unplugged -> excluded

    # near misses that must NOT be treated as copies
    not_size = add(conn, 20, 'A', 'backup', 'other.png', 2001, T)            # one byte different
    not_time = add(conn, 21, 'A', 'backup2', 'other.png', 2000, T + 5)       # 5s apart
    conn.commit()

    reachable = server._reachable_root_ids()
    print('\nreachable libraries')
    check('a library whose folder is missing is excluded', reachable == {'A', 'B'})

    print('\nfinding copies')
    groups = server.find_folder_dupes(conn, keep1, reachable)
    by_keeper = dict(groups)
    doomed = [d for _k, ds in groups for d in ds]
    check('the kept file is the one in the folder being viewed', keep1 in by_keeper)
    check('a copy in another folder of the same library is found', cp_same_root in doomed)
    check('sub-second mtime drift still counts as the same file', cp_same_root in doomed)
    check('a copy in another library is found', cp_other_lib in doomed)
    check('THE GUARD: a copy on an unreachable library is NOT touched', cp_offline not in doomed)
    check('a same-name file of a different size is not a copy', not_size not in doomed)
    check('a same-name file 5s apart is not a copy', not_time not in doomed)
    check('a file with no copies yields no group', 3 not in by_keeper)
    check('a file whose only near-misses differ is not culled', keep2 not in by_keeper)

    print('\nculling from an offline library is refused outright')
    check('anchoring on an unreachable library finds nothing',
          server.find_folder_dupes(conn, cp_offline, reachable) == [])

    print('\ncuration that must survive the cull')
    tag(conn, keep1, 'sunset')                            # keeper's own
    tag(conn, cp_same_root, 'portrait')                   # only on a copy
    tag(conn, cp_other_lib, 'sunset')                     # duplicate of the keeper's
    tag(conn, cp_other_lib, 'fav', 'fav')                 # favorite lives on a copy
    tag(conn, cp_same_root, 'label:to-publish')           # label lives on a copy
    conn.execute("UPDATE images SET note='from the copy' WHERE id=?", (cp_same_root,))
    conn.execute("INSERT INTO quality(image_id, reward) VALUES(?,?)", (cp_other_lib, 0.77))
    conn.commit()

    server.merge_curation(conn, keep1, [cp_same_root, cp_other_lib])
    conn.commit()
    check('a tag only the copy had moves to the keeper', 'portrait' in tags_of(conn, keep1))
    check('the keeper keeps its own tag', 'sunset' in tags_of(conn, keep1))
    check('a favorite on a copy moves across',
          conn.execute("SELECT 1 FROM tags WHERE image_id=? AND source='fav'",
                       (keep1,)).fetchone() is not None)
    check('the label is adopted when the keeper had none', labels_of(conn, keep1) == ['label:to-publish'])
    check('the note is adopted when the keeper had none', note_of(conn, keep1) == 'from the copy')
    kq = conn.execute("SELECT * FROM quality WHERE image_id=?", (keep1,)).fetchone()
    check('the Quality score is adopted when the keeper had none', kq and kq['reward'] == 0.77)

    print('\nthe keeper is never overwritten')
    k2 = add(conn, 30, 'A', 'k2', 'a.png', 10, T)
    c2 = add(conn, 31, 'A', 'c2', 'a.png', 10, T)
    tag(conn, k2, 'label:published')                      # keeper already has a label
    tag(conn, c2, 'label:to-refine')                      # copy has a different one
    conn.execute("UPDATE images SET note='mine' WHERE id=?", (k2,))
    conn.execute("UPDATE images SET note='theirs' WHERE id=?", (c2,))
    conn.execute("INSERT INTO quality(image_id, reward) VALUES(?,?)", (k2, 0.3))
    conn.execute("INSERT INTO quality(image_id, reward) VALUES(?,?)", (c2, 0.5))
    conn.commit()
    server.merge_curation(conn, k2, [c2])
    conn.commit()
    check("the keeper's own label wins", labels_of(conn, k2) == ['label:published'])
    check('never two labels', len(labels_of(conn, k2)) == 1)
    check("the keeper's note is kept", note_of(conn, k2).startswith('mine'))
    check('a differing note is appended, not dropped', 'theirs' in note_of(conn, k2))
    q2 = conn.execute("SELECT * FROM quality WHERE image_id=?", (k2,)).fetchone()
    check("the keeper's own Quality score is not overwritten", q2['reward'] == 0.3)
    # the fill-an-empty-column branch: keeper has a quality ROW but a NULL reward
    k3 = add(conn, 32, 'A', 'k3', 'b.png', 11, T)
    c3 = add(conn, 33, 'A', 'c3', 'b.png', 11, T)
    conn.execute("INSERT INTO quality(image_id, reward) VALUES(?,NULL)", (k3,))
    conn.execute("INSERT INTO quality(image_id, reward) VALUES(?,?)", (c3, 0.9))
    conn.commit()
    server.merge_curation(conn, k3, [c3])
    conn.commit()
    check("a keeper's empty Quality column is filled from the copy",
          conn.execute("SELECT reward FROM quality WHERE image_id=?", (k3,)).fetchone()[0] == 0.9)

    print('\nrecycled files are anchored at their OWN library root')
    # Every file used to be anchored at the ACTIVE library's root. A root on another volume makes
    # the rename fail, so _move_to_recycle_folder fell back per-file and dropped a _ToRecycle into
    # every folder it touched. The file's own root is always the same volume -> one per library.
    seen = []
    real_recycle_one = server._recycle_one
    server._recycle_one = lambda path, anchor=None: (seen.append((path, anchor)), [os.path.dirname(path)])[1]
    real_delete = index_db.delete_by_ids
    index_db.delete_by_ids = lambda db_path, ids: len(ids)
    server.ACTIVE = {'path': live_a, 'db': os.path.join(tmp, 'library.db')}
    try:
        # a file that really exists on disk, indexed into library B
        os.makedirs(os.path.join(live_b, 'sub'), exist_ok=True)
        real = os.path.join(live_b, 'sub', 'anchor_me.png')
        with open(real, 'wb') as f:
            f.write(b'x' * 10)
        conn.execute("INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size, "
                     "root_id, indexed_at) VALUES(60,?,?,?,?,?,?,?,'B',0)",
                     (real, 'sub/anchor_me.png', 'sub', 'anchor_me.png', '.png', T, 10))
        conn.commit()
        server._recycle_ids([60])
        check('the anchor is the file\'s own library root, not the active one',
              seen and seen[0][1] == live_b)
    finally:
        server._recycle_one = real_recycle_one
        index_db.delete_by_ids = real_delete

    print('\none _ToRecycle at the library root, not one per folder')
    deep = os.path.join(live_b, 'a', 'b', 'c'); os.makedirs(deep, exist_ok=True)
    victim = os.path.join(deep, 'v.png')
    with open(victim, 'wb') as f:
        f.write(b'v')
    dest, base = server._move_to_recycle_folder(victim, live_b)
    check('it lands under the library root\'s _ToRecycle',
          os.path.dirname(dest) == os.path.join(live_b, '_ToRecycle') and base == live_b)
    check('no _ToRecycle is left beside the file', not os.path.isdir(os.path.join(deep, '_ToRecycle')))
    check('the file really moved', os.path.isfile(dest) and not os.path.exists(victim))
    # a second file of the same name is suffixed rather than clobbering the first
    with open(victim, 'wb') as f:
        f.write(b'v2')
    dest2, _ = server._move_to_recycle_folder(victim, live_b)
    check('a name collision is suffixed, not overwritten',
          dest2 != dest and os.path.isfile(dest) and os.path.isfile(dest2))

    print('\nlibrary-wide duplicate report (read-only)')
    # a set spread across libraries, where the MOST CURATED copy must be the keeper even though
    # it isn't the oldest — that's the whole point of the global keep rule
    w1 = add(conn, 40, 'A', 'w', 'wide.png', 500, T)          # oldest, but bare
    w2 = add(conn, 41, 'B', 'w', 'wide.png', 500, T + 0.2)    # newer, but curated
    w3 = add(conn, 42, 'C', 'w', 'wide.png', 500, T)          # offline library
    tag(conn, w2, 'worked-on'); tag(conn, w2, 'fav', 'fav')
    lone = add(conn, 43, 'A', 'w', 'lonely.png', 77, T)       # no partner
    near = add(conn, 44, 'B', 'w', 'lonely.png', 78, T)       # name matches, size does not
    conn.commit()

    allsets = server.find_all_dupes(conn, reachable)
    wide = [s for s in allsets if s['filename'] == 'wide.png']
    check('the cross-library set is found once', len(wide) == 1)
    check('the most-curated copy is kept, not the oldest', wide and wide[0]['keep']['id'] == w2)
    check('the offline copy is excluded from the set',
          wide and w3 not in [d['id'] for d in wide[0]['dupes']])
    check('the bare copy is listed as a duplicate', wide and [d['id'] for d in wide[0]['dupes']] == [w1])
    check('a same-name/different-size pair is not a set',
          not [s for s in allsets if s['filename'] == 'lonely.png'])
    check('every reported set has at least one duplicate',
          all(len(s['dupes']) >= 1 for s in allsets))
    check('the keeper is never also listed as a duplicate',
          all(s['keep']['id'] not in [d['id'] for d in s['dupes']] for s in allsets))
    # ties fall back to oldest, then id, so repeated runs agree
    check('the scan is deterministic',
          [s['keep']['id'] for s in server.find_all_dupes(conn, reachable)] ==
          [s['keep']['id'] for s in allsets])
    check('it reports nothing when no library is reachable', server.find_all_dupes(conn, set()) == [])

    print('\nafter the recycle purges the copies')
    conn.close()                                          # delete_by_ids opens its own connection
    index_db.delete_by_ids(os.path.join(tmp, 'library.db'), [cp_same_root, cp_other_lib, c2])
    conn2 = open_db(os.path.join(tmp, 'library.db'))
    check('the merged tags are still on the keeper', 'portrait' in tags_of(conn2, keep1))
    check('the merged note is still on the keeper', note_of(conn2, keep1) == 'from the copy')
    check('no orphaned tag rows', conn2.execute(
        "SELECT COUNT(*) FROM tags WHERE image_id NOT IN (SELECT id FROM images)").fetchone()[0] == 0)
    check('no orphaned quality rows', conn2.execute(
        "SELECT COUNT(*) FROM quality WHERE image_id NOT IN (SELECT id FROM images)").fetchone()[0] == 0)
    # every row on the unplugged library must survive — nothing about a cull elsewhere may touch it
    survivors = {r[0] for r in conn2.execute("SELECT id FROM images WHERE root_id='C'")}
    check('every offline-library row survived', survivors == {cp_offline, w3})

    conn2.close()
    return http_checks()


def http_checks():
    """Drive the two endpoints on a real server, over a pre-built index — so routing and the JSON
    shapes the client depends on are exercised, not just the functions underneath."""
    import json
    import socket
    import subprocess
    import sys
    import urllib.error
    import urllib.request

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix='vv_dupes_api_')
    data = os.path.join(tmp, 'data'); os.makedirs(data)
    live = os.path.join(tmp, 'libA'); os.makedirs(live)
    cfg = os.path.join(tmp, 'config.json')
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()

    conn = open_db(os.path.join(data, 'library.db'))
    T = 1_700_000_000.0
    add(conn, 1, 'A', 'keep', 'shot.png', 1000, T)        # keeper
    add(conn, 2, 'A', 'backup', 'shot.png', 1000, T)      # its copy, in another folder
    tag(conn, 2, 'from-the-copy')
    conn.commit(); conn.close()

    with open(cfg, 'w') as f:
        json.dump({'roots': [{'key': 'A', 'name': 'Main', 'path': live}],
                   'active': 'A', 'port': port}, f)

    def call(path, body=None):
        url = f'http://127.0.0.1:{port}{path}'
        req = (urllib.request.Request(url) if body is None else
               urllib.request.Request(url, data=json.dumps(body).encode(),
                                      headers={'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    env = dict(os.environ, CV_CONFIG=cfg, CV_DATA=data)
    proc = subprocess.Popen([sys.executable, 'server.py'], cwd=here, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        import time
        for _ in range(80):
            try:
                if call('/api/config'):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print('server never came up:\n' +
                  proc.stdout.read().decode(errors='replace')[-2000:])
            return 1

        print('\nlibrary-wide scan endpoint')
        d = call('/api/dupes/scan')
        check('it reports the set', d['sets'] == 1 and d['copies'] == 1)
        check('recoverable bytes = copies x size', d['bytes'] == d['largest'][0]['size'])
        check('each row carries a date to show',
              d['largest'][0]['keep']['mtime'] > 0 and d['largest'][0]['dupes'][0]['mtime'] > 0)
        check('rows name their library', d['largest'][0]['keep']['library'] == 'Main')
        # the global rule differs from the folder-anchored one on purpose: here the file in
        # 'backup' carries the tag, so it is the keeper even though 'keep' holds the anchor
        check('the curated copy wins globally, whichever folder it sits in',
              d['largest'][0]['keep']['id'] == 2
              and [x['id'] for x in d['largest'][0]['dupes']] == [1])
        check('under the cap everything lands in largest, smallest stays empty',
              len(d['largest']) == 1 and d['smallest'] == [])
        # add a second, much smaller set so each=1 has to split the list into both ends
        c = open_db(os.path.join(data, 'library.db'))
        add(c, 50, 'A', 'x', 'big.png', 9000, 111.0)
        add(c, 51, 'A', 'y', 'big.png', 9000, 111.0)
        add(c, 52, 'A', 'x', 'mid.png', 50, 222.0)
        add(c, 53, 'A', 'y', 'mid.png', 50, 222.0)
        c.commit(); c.close()
        d2 = call('/api/dupes/scan?each=1')
        ids = [s['keep']['id'] for s in d2['largest']] + [s['keep']['id'] for s in d2['smallest']]
        check('both ends are returned', len(d2['largest']) == 1 and len(d2['smallest']) == 1)
        check('the two ends never overlap', len(ids) == len(set(ids)))
        check('largest really is the bigger set',
              d2['largest'][0]['recoverable'] > d2['smallest'][0]['recoverable'])

        print('\nover HTTP')
        check('a missing id is rejected, not crashed', 'error' in call('/api/folder_dupes?id='))
        check('a non-existent id reports no copies', call('/api/folder_dupes?id=9999')['copies'] == 0)
        r = call('/api/folder_dupes?id=1')
        check('the count endpoint finds the copy', r['copies'] == 1 and r['groups'] == 1)
        check('it names the library the copy is in', r['by_library'] == {'Main': 1})
        check('no library is reported offline', r['offline'] == [])
        # NOTE these rows point at paths that don't exist on disk, so they take the "already gone"
        # branch: the index row is purged but no file moves. deleted counts files actually recycled,
        # purged counts index-only removals — conflating them is what made a cull look like it had
        # recycled thousands of files when nothing had moved.
        a = call('/api/folder_dupes/apply', {'id': 1})
        check('apply removes the copy', a['deleted'] + a['purged'] == 1 and not a['failed'])
        check('a file that was already gone counts as purged, not recycled',
              a['deleted'] == 0 and a['purged'] == 1)
        check('apply reports what it kept', a['kept'] == 1)
        check('the copy is gone afterwards', call('/api/folder_dupes?id=1')['copies'] == 0)
        conn = open_db(os.path.join(data, 'library.db'))
        check("the copy's tag landed on the keeper", 'from-the-copy' in tags_of(conn, 1))
        check('the keeper itself survived',
              conn.execute("SELECT COUNT(*) FROM images WHERE id=1").fetchone()[0] == 1)
        conn.close()

        print('\nbatched library-wide cull')
        before = call('/api/dupes/scan')
        check('two sets are waiting (big.png, mid.png)', before['sets'] == 2)
        c1 = call('/api/dupes/cull', {'sets': 1})
        check('one batch culls exactly one set',
              c1['sets'] == 1 and c1['deleted'] + c1['purged'] == 1)
        check('it reports the space freed', c1['bytes'] == 9000)
        check('it took the LARGEST set first',
              call('/api/dupes/scan')['largest'][0]['filename'] == 'mid.png')
        conn = open_db(os.path.join(data, 'library.db'))
        kept = {r[0] for r in conn.execute("SELECT id FROM images WHERE filename='big.png'")}
        check('exactly one file of the culled set survives', len(kept) == 1)
        conn.close()
        c2 = call('/api/dupes/cull', {'sets': 200})
        check('the next batch drains the rest',
              c2['sets'] == 1 and c2['deleted'] + c2['purged'] == 1)
        check('nothing is left to find', call('/api/dupes/scan')['sets'] == 0)
        c3 = call('/api/dupes/cull', {'sets': 200})
        check('culling an empty report is a no-op, not an error',
              c3['deleted'] == 0 and c3['sets'] == 0 and not c3.get('error'))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
