"""test_seen.py — the viewing history, and the Random sort.

**The "Least seen" sort this was written for is gone** (CLN-1, 2026-08-05), and so is the per-card
impression tracking that fed it — an IntersectionObserver plus a dwell timer on every card, POSTing
to `/api/seen` every few seconds, all for a sort nobody could choose any more. What did NOT go is
the history: `last_seen` and `last_opened` are a record of what you have looked at that cannot be
rebuilt from the files, and the sort may well come back. So this file guards the DATA, not a sort.

  * opening an image records both columns (opening is a strictly stronger event than seeing, so
    they can never disagree the wrong way) — the one write still running;
  * both must survive a rescan. They ride the same mechanism as `note` — the
    scanner's UPDATE names its columns and these aren't among them — and that omission looks like an
    oversight, so this is the check that stops someone widening it into a blanket update;
  * the migration must BACKFILL from last_opened on an existing library, since an image you opened
    was certainly seen — while inventing no history beyond that;
  * a RETIRED sort value must fall back rather than error. Snapshots store `sort` verbatim and the
    server never validated it, so a saved `seen`/`name`/`relevance` is still reachable and must
    come back as Newest — the server-side half of the client's own sortOptionExists() fallback.

Plus the whole Random/shuffle section, which was never about seeing at all.

Drives the real server over HTTP, exactly as the browser does.

Run: python test_seen.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

# Run from anywhere: this lives in tests/ and imports the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import time
import urllib.error
import urllib.request

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_png(path, colour):
    from PIL import Image
    Image.new('RGB', (48, 48), colour).save(path)


def main():
    """Temp dirs live in the project folder, so cleanup must survive a failing check — otherwise a
    red run litters the repo (and shows up in git status)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix='vv_seen_')
    try:
        return run(here, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(here, tmp):
    lib = os.path.join(tmp, 'lib')
    os.makedirs(lib)
    # Distinct, strictly increasing mtimes: oldest-first has to be a total order, or "the sort
    # collapsed to oldest-first" isn't something the test can actually see.
    names = ['a_oldest.png', 'b.png', 'c.png', 'd.png', 'e_newest.png']
    base = time.time() - 500000
    for n, name in enumerate(names):
        p = os.path.join(lib, name)
        make_png(p, (20 + n * 30, 40, 60))
        os.utime(p, (base + n * 1000, base + n * 1000))

    cfg = os.path.join(tmp, 'config.json')
    data = os.path.join(tmp, 'data')
    os.makedirs(data)
    port = free_port()
    with open(cfg, 'w') as f:
        json.dump({'roots': [{'key': 'A', 'name': 'Main', 'path': lib}],
                   'active': 'A', 'port': port}, f)

    def call(path, body=None):
        url = f'http://127.0.0.1:{port}{path}'
        req = (urllib.request.Request(url) if body is None else
               urllib.request.Request(url, data=json.dumps(body).encode(),
                                      headers={'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    def order(sort, grouped=False):
        # NB group=1 WITHOUT sets=1 is the "sets only" mode, which drops every lone image and would
        # quietly return an empty list here. The collapsed-but-showing-everything view the grid
        # actually uses is group=1&sets=1.
        qs = 'group=1&sets=1' if grouped else 'group=0&sets=0'
        return [i['filename'] for i in call(f'/api/search?limit=50&sort={sort}&{qs}')['items']]

    # Per-image state comes from /api/image/<id>, which returns the whole row. The search payload
    # deliberately doesn't carry last_seen (the grid has no use for it) — and asserting on a key
    # the response never had is how the first version of this test passed while checking nothing.
    def detail(name):
        return call(f'/api/image/{by_name[name]}')

    def wait_scan():
        for _ in range(120):
            if not call('/api/scan/status').get('running'):
                return
            time.sleep(0.25)

    env = dict(os.environ, CV_CONFIG=cfg, CV_DATA=data)
    proc = subprocess.Popen([sys.executable, 'server.py'], cwd=here, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
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

        call('/api/scan', {'force': True})
        wait_scan()
        items = call('/api/search?limit=50')['items']
        by_name = {i['filename']: i['id'] for i in items}
        check('the test library indexed', len(by_name) == 5)

        print('\na fresh library carries the columns, empty')
        check('the column exists on a fresh DB', 'last_seen' in detail('a_oldest.png'))
        check('every image starts never-seen',
              all(detail(n)['last_seen'] is None for n in names))
        check('and never-opened', all(detail(n)['last_opened'] is None for n in names))

        print('\nopening records BOTH — the one write still running')
        call('/api/opened', {'id': by_name['e_newest.png']})
        check('opening records the open', detail('e_newest.png')['last_opened'] is not None)
        check('opening also records it as seen (the stronger event implies the weaker)',
              detail('e_newest.png')['last_seen'] is not None)
        check('only the id sent was touched',
              detail('c.png')['last_seen'] is None and detail('c.png')['last_opened'] is None)
        call('/api/opened', {'id': by_name['b.png']})
        check('a second open is recorded independently',
              detail('b.png')['last_opened'] is not None)

        print('\nviewing history survives a rescan')
        before = {n: (detail(n)['last_seen'], detail(n)['last_opened']) for n in names}
        check('there is real history to lose',
              sum(s is not None for s, _ in before.values()) == 2)
        call('/api/scan', {'force': True})
        wait_scan()
        check('every last_seen came through the rescan unchanged',
              all(detail(n)['last_seen'] == before[n][0] for n in names))
        check('and every last_opened with it',
              all(detail(n)['last_opened'] == before[n][1] for n in names))

        print('\na RETIRED sort falls back instead of erroring (snapshots still hold these)')
        newest = list(reversed(names))
        for retired in ('seen', 'name', 'relevance', 'model', 'nonsense'):
            check(f'sort={retired} comes back as Newest, whole library',
                  order(retired) == newest)
        check('and the same in the collapsed view, which has its own copy of the sort branches',
              order('seen', True) == newest)

        # ---- Random ---------------------------------------------------------------------------
        # The claim that matters isn't "the order looks random", it's that PAGING a shuffle can
        # neither repeat nor skip an image. ORDER BY RANDOM() re-deals per query, so it fails this
        # silently — you only notice as a vague sense that the grid is odd.
        print('\nRandom: a shuffle is stable while you page through it')

        def page(seed, offset, limit=2):
            return [i['id'] for i in call(
                f'/api/search?sort=random&seed={seed}&limit={limit}&offset={offset}'
                '&group=0&sets=0')['items']]

        whole = page(4242, 0, 50)
        check('a shuffle returns the whole library', sorted(whole) == sorted(by_name.values()))
        paged = page(4242, 0) + page(4242, 2) + page(4242, 4)
        check('paging it reconstructs the shuffle exactly, in order', paged == whole)
        check('with no image repeated and none skipped',
              len(set(paged)) == len(whole) == len(by_name))
        check('the same seed replays the same order', page(4242, 0, 50) == whole)
        # 5 images = 120 orders, so a different seed COULD coincide; scan seeds for a different one
        # rather than asserting on a single lucky draw.
        others = [page(s, 0, 50) for s in (7, 99, 1234, 55555)]
        check('a different seed deals a different order', any(o != whole for o in others))
        check('every seed still returns the whole library',
              all(sorted(o) == sorted(by_name.values()) for o in others))
        check('the grouped view shuffles identically',
              [i['id'] for i in call('/api/search?sort=random&seed=4242&limit=50'
                                     '&group=1&sets=1')['items']] == whole)
        check('a missing seed still returns everything once, not an empty or partial grid',
              sorted(i['id'] for i in call('/api/search?sort=random&limit=50'
                                           '&group=0&sets=0')['items'])
              == sorted(by_name.values()))
        check('a junk seed is tolerated rather than 500ing',
              len(call('/api/search?sort=random&seed=abc&limit=50'
                       '&group=0&sets=0').get('items', [])) == 5)
        check('every row was dealt a shuffle key', all(
            detail(n)['shuffle_key'] is not None for n in names))
        keys = [detail(n)['shuffle_key'] for n in names]
        check('the keys are bounded, so key*seed cannot overflow int64',
              all(0 <= k < 1000000007 for k in keys))
        # The rejected design was `(a*id + b) % p` — no new column, and it looks shuffled. It is
        # AFFINE, so the sorted result walks the ids at a near-constant stride, and consecutive ids
        # here are one generation run: it would quietly return one image per run in a fixed pattern.
        # Constant GAPS between the keys is the signature of any such map, identity included.
        # (This used to assert `keys != sorted(keys)`, which with five images failed 1 run in 120 —
        # the very hazard the seed check two lines up already guards against.)
        gaps = {b - a for a, b in zip(keys, keys[1:])}
        check('and they are not derived from the row id — the gaps are not a fixed stride',
              len(gaps) > 1)

        print('\na shuffle key is dealt once and survives a rescan')
        call('/api/scan', {'force': True})
        wait_scan()
        check('the keys are unchanged', [detail(n)['shuffle_key'] for n in names] == keys)
        check('so the same seed still replays the same order', page(4242, 0, 50) == whole)

        # ---- the migration, on a DB that predates the column ----------------------------------
        # The real user's library IS this case, so it matters more than the fresh-DB path above.
        print('\nan existing library migrates, backfilling from last_opened')
        proc.terminate()
        proc.wait(timeout=15)
        dbp = os.path.join(data, 'library.db')
        con = sqlite3.connect(dbp)
        opened_at = 1700000000.0
        con.execute("UPDATE images SET last_opened=? WHERE filename='b.png'", (opened_at,))
        con.commit()
        try:
            con.execute("ALTER TABLE images DROP COLUMN last_seen")   # SQLite >= 3.35
            con.commit()
            dropped = 'last_seen' not in [r[1] for r in con.execute("PRAGMA table_info(images)")]
        except sqlite3.OperationalError:
            dropped = False
        con.close()
        if not dropped:
            print('  ..   skipped: this SQLite cannot DROP COLUMN '
                  f'(sqlite {sqlite3.sqlite_version}) — cannot simulate the old schema')
        else:
            import index_db
            con = index_db.connect(dbp)          # connect() runs _migrate()
            cols = [r[1] for r in con.execute("PRAGMA table_info(images)")]
            check('the migration adds the column', 'last_seen' in cols)
            rows = {r[0]: (r[1], r[2]) for r in
                    con.execute("SELECT filename, last_seen, last_opened FROM images")}
            con.close()
            check('an image you had OPENED is backfilled as seen',
                  rows['b.png'][0] == opened_at)
            # The precise claim: seen := opened, wherever an open was recorded, and NULL everywhere
            # else. b.png and e_newest.png were opened (one by hand above, one over the API earlier
            # in this run), so both are backfilled — a_oldest/c/d never were, and the migration must
            # not invent history for them.
            check('backfilled rows carry exactly their open time, never an invented one',
                  all(seen == opened for seen, opened in rows.values() if opened is not None))
            check('an image never opened stays never-seen — no history is invented',
                  all(seen is None for seen, opened in rows.values() if opened is None))
            check('the two groups are both non-empty, so neither check is vacuous',
                  any(o is not None for _, o in rows.values())
                  and any(o is None for _, o in rows.values()))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    print('\n' + ('all checks passed' if not _fails
                  else f'{len(_fails)} FAILED:\n  - ' + '\n  - '.join(_fails)))
    return 1 if _fails else 0


if __name__ == '__main__':
    sys.exit(main())
