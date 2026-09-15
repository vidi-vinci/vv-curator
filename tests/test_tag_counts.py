"""test_tag_counts.py — the sidebar's numbers: tag counts and favourites.

Run: python test_tag_counts.py

Why this test exists. `/api/tags` used to guard both count queries with
`EXISTS(SELECT 1 FROM images WHERE id=t.image_id)` — an orphan check, run once per TAG row. It was
the reason the rail sat dimmed for seconds after the grid was usable (`bench_tags.py` measured the
endpoint at 10x its necessary cost), and it was dropped on 2026-08-19.

Dropping it rests on one load-bearing assumption: **a tag row cannot outlive its image.** So this
test pins both halves —

  * the counts themselves, whole-library and scoped to one library, including that a tag applied in
    one library never shows up in another's numbers (root scoping is the part that changed shape,
    from a per-row probe to one pass over the root index);
  * that removing an image removes its tag rows, so no count can drift upward behind the missing
    guard. If that ever stops being true, the sidebar starts over-counting and this is the test
    that says so.

It covered the Unreviewed count too until that filter was retired on 2026-08-19. The marks that
count fed are still set up here, because they are what the tag counts are made of.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix='vv_tagcount_')
    try:
        return run(here, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(here, tmp):
    from PIL import Image
    # TWO libraries, because root scoping is the half of this query that changed shape.
    libs = {'A': os.path.join(tmp, 'libA'), 'B': os.path.join(tmp, 'libB')}
    names = {'A': ['a1.png', 'a2.png', 'a3.png', 'a4.png'], 'B': ['b1.png', 'b2.png', 'b3.png']}
    for key, path in libs.items():
        os.makedirs(path)
        for n in names[key]:
            Image.new('RGB', (64, 64), (40, 40, 60)).save(os.path.join(path, n))

    cfg = os.path.join(tmp, 'config.json')
    data = os.path.join(tmp, 'data')
    os.makedirs(data)
    port = _free_port()
    with open(cfg, 'w') as f:
        json.dump({'roots': [{'key': 'A', 'name': 'One', 'path': libs['A']},
                             {'key': 'B', 'name': 'Two', 'path': libs['B']}],
                   'active': 'A', 'port': port}, f)

    def call(path):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    def post(path, body):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

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
            print('server never came up:\n' + proc.stdout.read().decode(errors='replace')[-2000:])
            return 1

        for key in ('A', 'B'):
            post('/api/roots/select', {'key': key})
            post('/api/scan', {'force': True})
            for _ in range(120):
                if not call('/api/scan/status').get('running'):
                    break
                time.sleep(0.25)

        ids = {}
        for it in call('/api/search?limit=99&group=0&sets=0')['items']:
            ids[it['filename']] = it['id']
        if len(ids) != 7:
            print('scan did not index both libraries (%d files)' % len(ids))
            return 1

        # One mark of each kind, spread across both libraries.
        post('/api/tag', {'ids': [ids['a1.png'], ids['a2.png'], ids['b1.png']],
                          'tag': 'portrait', 'op': 'add'})
        post('/api/tag', {'ids': [ids['b2.png']], 'tag': 'landscape', 'op': 'add'})
        post('/api/label', {'ids': [ids['a1.png']], 'label': 'publish'})
        post('/api/favorite', {'ids': [ids['a3.png']], 'on': True})
        post('/api/note', {'id': ids['a4.png'], 'text': 'come back to this'})

        def counts(roots=''):
            j = call('/api/tags' + ('?roots=' + roots if roots else ''))
            return ({t['name']: t['count'] for t in j['tags']}, j['favorites'])

        print('\nEvery library shown')
        t, fav = counts()
        check('portrait counts all three images that carry it', t.get('portrait') == 3)
        check('landscape counts its one', t.get('landscape') == 1)
        check('the label rides the tag list as label:<slug>', t.get('label:publish') == 1)
        check('one favourite', fav == 1)

        print('\nScoped to library One')
        t, fav = counts('A')
        check('portrait counts only its two images here', t.get('portrait') == 2)
        check("the other library's tag is absent entirely", 'landscape' not in t)
        check('the label is counted here', t.get('label:publish') == 1)
        check('the favourite is in this library', fav == 1)

        print('\nScoped to library Two')
        t, fav = counts('B')
        check('portrait counts only its one image here', t.get('portrait') == 1)
        check('landscape is here', t.get('landscape') == 1)
        check("the other library's label is absent entirely", 'label:publish' not in t)
        check('no favourite in this library', fav == 0)

        dbp = os.path.join(data, 'library.db')

        print('\nNo index on tags may lead with a low-cardinality column')
        # Not a style rule — a scar. The first cut of this fix indexed tags(source, tag, image_id),
        # the obvious order for the query it was added for, and made the GRID 26x slower: three
        # distinct values in `source` is exactly the shape SQLite's skip-scan looks for, so the
        # grid's per-row label/fav/hidden subqueries abandoned the primary key and began seeking
        # source='fav' and scanning that whole partition for an image_id. bench_collapse.py caught
        # it (919ms -> 24.4s per page). A structural assertion rather than a plan or timing one:
        # EXPLAIN QUERY PLAN depends on ANALYZE stats a seven-row fixture cannot produce, and a
        # timing check on a library this size would be noise.
        conn = sqlite3.connect(dbp)
        leads = []
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                    "AND tbl_name='tags' AND sql IS NOT NULL"):
            cols = [r[2] for r in conn.execute("PRAGMA index_info(%s)" % name)]
            leads.append((name, cols[0] if cols else None))
        conn.close()
        bad = [n for n, lead in leads if lead in ('source', 'score')]
        check('none of %d tags indexes leads with source' % len(leads), not bad)

        print('\nA tag row cannot outlive its image (what the dropped guard relied on)')
        sys.path.insert(0, here)
        import index_db
        index_db.delete_by_ids(dbp, [ids["a1.png"]])
        t, fav = counts()
        check('portrait drops to 2 when one of its images goes', t.get('portrait') == 2)
        check('the deleted image takes its label with it', 'label:publish' not in t)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


if __name__ == '__main__':
    raise SystemExit(main())
