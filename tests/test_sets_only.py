"""test_sets_only.py — the Sets dropdown's "Images only" / "Video only" now filter to SETS only.

When exactly one kind collapses (image sets, or still+video pairs), the grid shows only the set
cards of that kind and hides every lone item — a focused culling view. Both ends must agree:

  * the GRID (api_search) shows only collapsed cards with >1 member of the active kind;
  * the counter's denominator becomes "library sets" (and the file tally, files in those sets);
  * Select-all (api_ids) returns the MEMBERS of the visible sets, not every matching file — or it
    would select lone items that aren't even on screen.

"All" (both kinds collapse) and "None" (neither) are unchanged — they still show everything.

The filter keys off whether a group contains a video member, which is decided at query time, so
this test forms groups by writing group_id directly (independent of the filename/­set_id heuristics
that build them — those are covered by test_groups.py).

Run: python test_sets_only.py
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
    tmp = tempfile.mkdtemp(prefix='vv_setsonly_')
    try:
        return run(here, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(here, tmp):
    from PIL import Image
    lib = os.path.join(tmp, 'lib')
    os.makedirs(lib)
    # an image SET (3 stills), a still+video PAIR, a SONG pair (track + cover), 2 lone stills
    set_names = ['set_a.png', 'set_b.png', 'set_c.png']
    pair_still, pair_vid = 'pair_still.png', 'pair_clip.mp4'
    song_cover, song_track = 'song_cover.png', 'song_track.mp3'
    lones = ['lone_1.png', 'lone_2.png']
    for n in set_names + [pair_still, song_cover] + lones:
        Image.new('RGB', (64, 64), (40, 40, 60)).save(os.path.join(lib, n))
    with open(os.path.join(lib, pair_vid), 'wb') as f:
        f.write(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 256)

    with open(os.path.join(lib, song_track), 'wb') as f:
        f.write(b'ID3' + bytes(512))

    cfg = os.path.join(tmp, 'config.json')
    data = os.path.join(tmp, 'data')
    os.makedirs(data)
    port = _free_port()
    with open(cfg, 'w') as f:
        json.dump({'roots': [{'key': 'A', 'name': 'Main', 'path': lib}], 'active': 'A', 'port': port}, f)

    def call(path):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=30) as r:
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

        # The server kicks off its own scan on first start, and a second scan while that one is
        # in flight is a 409 — so wait it out rather than racing it. (This test got away with
        # racing until an audio file joined the fixture and made the first scan slower.)
        for _ in range(80):
            if not call('/api/scan/status').get('running'):
                break
            time.sleep(0.25)

        urllib.request.urlopen(urllib.request.Request(
            f'http://127.0.0.1:{port}/api/scan', data=b'{"force":true}',
            headers={'Content-Type': 'application/json'}), timeout=30)
        for _ in range(80):
            if not call('/api/scan/status').get('running'):
                break
            time.sleep(0.25)

        # Form the groups by writing group_id directly (the server is idle after the scan).
        dbp = os.path.join(data, 'library.db')
        conn = sqlite3.connect(dbp)
        ids = {r[1]: r[0] for r in conn.execute("SELECT id, filename FROM images")}
        conn.execute("UPDATE images SET group_id='SET1' WHERE filename IN (?,?,?)", tuple(set_names))
        conn.execute("UPDATE images SET group_id='PAIR1' WHERE filename IN (?,?)",
                     (pair_still, pair_vid))
        conn.execute("UPDATE images SET group_id='SONG1' WHERE filename IN (?,?)",
                     (song_cover, song_track))
        conn.commit()
        conn.close()

        def search(sets_on, group_on):
            q = f'/api/search?limit=99&sets={1 if sets_on else 0}&group={1 if group_on else 0}'
            return call(q)

        print('\nImages only (sets=1, group=0): only the image set, lone items + pair hidden')
        s = search(True, False)
        names = sorted(i['filename'] for i in s['items'])
        check('exactly one card is shown', len(s['items']) == 1)
        check('it is the image set (a set representative)', s['items'][0]['is_set'] is True)
        check('the set card counts its 3 members', s['items'][0]['group_count'] == 3)
        check('matched total is 1 set', s['total'] == 1)
        check('the denominator is library sets (1), not files', s['root_total'] == 1)
        check('the file tally counts files in sets (3)', s['root_files'] == 3)
        check('no lone image leaked in', not any(n.startswith('lone') for n in names))
        check('the song pair is not an image set', not any(
            i['filename'] in (song_track, song_cover) for i in s['items']))

        print('\nVideo only (sets=0, group=1): only the still+video pair')
        s = search(False, True)
        check('exactly one card is shown', len(s['items']) == 1)
        check('the card carries a paired video', bool(s['items'][0]['video_id']))
        check('it is NOT classed as an image set', s['items'][0]['is_set'] is False)
        check('the pair card counts its 2 members', s['items'][0]['group_count'] == 2)
        check('matched total is 1 pair', s['total'] == 1)
        check('denominator is library pairs (1)', s['root_total'] == 1)
        check('file tally is the 2 pair files', s['root_files'] == 2)
        # A SONG PAIR IS NOT A VIDEO SET. It collapses on the same flag (a track plus its cover is
        # one media file and one still, exactly the shape of a still+video pair) and it has two
        # members, so it satisfied the sets-only cut on its own merits and sat in a view that says
        # video. Reported 2026-08-23; the conflict rule painted on the two bars had been
        # promising the opposite since it shipped.
        check('the song pair does not leak into Video sets', not any(
            i['filename'] in (song_track, song_cover) for i in s['items']))

        print('\nAll (sets=1, group=1): everything shows, nothing filtered')
        s = search(True, True)
        # image set(1) + pair(1) + song pair(1) + 2 lones = 5 cards
        check('all five cards show (set + pair + song + 2 lones)', s['total'] == 5)
        check('the library denominator counts every card (5)', s['root_total'] == 5)
        check('the file tally is all 9 files', s['root_files'] == 9)
        check('the song pair is one card in All', any(
            i['filename'] == song_track for i in s['items']))

        print('\nNone (sets=0, group=0): no collapsing, every file is its own card')
        s = search(False, False)
        check('all 9 files show as cards', s['total'] == 9)

        print('\nSelect-all matches the grid, not the underlying files')
        idsr = call('/api/ids?limit=99&sets=1&group=0')
        got = sorted(idsr['ids'])
        want = sorted(ids[n] for n in set_names)
        check('images-only Select-all returns the 3 set members only', got == want)
        idsr = call('/api/ids?limit=99&sets=0&group=1')
        got = sorted(idsr['ids'])
        want = sorted([ids[pair_still], ids[pair_vid]])
        check('video-only Select-all returns the 2 pair members only', got == want)
        idsr = call('/api/ids?limit=99&sets=1&group=1')
        check('all-mode Select-all still returns every file (9)', idsr['total'] == 9)
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
