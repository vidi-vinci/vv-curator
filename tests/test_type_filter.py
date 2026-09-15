"""Regression test: File type asks what a card IS, not what it contains.

Run:  python test_type_filter.py

**The bug this exists to stop coming back.** On 2026-08-19 filters started matching at CARD level —
any member matching brings the whole card — which is right for "does this run mention neon" and
wrong for File type. A still+video pair matched **Images** through its still and arrived wearing a
▶; a song matched **Images** through its cover art and arrived as a song card. The author, 2026-08-20:
*"selecting File Type 'Images' still shows Video cards."*

The distinction that was missing, and is now the fifth principle in
`docs/notes/sets-and-curation.md`:

  * a filter for what a card **contains** matches if ANY member matches — prompt, model, quality,
    date;
  * a filter for what a card **is** must test the card. File type is the only one of these.

So while a pair kind is collapsing, the three options carve the library into three
**non-overlapping** sets, decided by what the group holds rather than by the row that matched:
Videos = the group holds a video · Songs = the group holds audio · Images = neither.

**The partition is the real assertion here**, and it is what a per-option spot check would miss: a
card must appear under exactly one of the three, and every card must appear under one of them. Both
halves matter — the bug made cards appear under two, and an over-eager fix would make some appear
under none.

With merging OFF a card IS a file, so the row's own extension is the whole answer and the still from
a video run is an image again. That case is asserted too, because it is the one a fix aimed only at
the collapsed path would silently break.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE SYSTEM TEMP, NOT THE PROJECT FOLDER — 20 places said `dir=here`/`dir=BASE`, and every one
# of them was wrong for the same reason. Each cleans up in a finally with ignore_errors=True, and
# on Windows the server thread still holds library.db open when that runs, so the removal fails
# SILENTLY and the husk stays. 25 of them, 503MB, had accumulated in the master folder before the author
# spotted them. A test's mess belongs where the OS already sweeps up.
_tmp = tempfile.mkdtemp(prefix='vv_type_')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)
_root = os.path.join(_tmp, 'lib')
os.makedirs(_root, exist_ok=True)
with open(os.environ['CV_CONFIG'], 'w', encoding='utf-8') as f:
    json.dump({'roots': [{'path': _root}], 'port': 0}, f)

sys.path.insert(0, BASE)
import index_db                                          # noqa: E402
import server                                            # noqa: E402  (must follow the env vars)
from http.server import ThreadingHTTPServer              # noqa: E402

DB = os.path.join(os.environ['CV_DATA'], 'library.db')
RK = server.CONFIG['roots'][0]['key']
_fails = []


def check(label, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + label + (
        '\n          ' + detail if (detail and not cond) else ''))
    if not cond:
        _fails.append(label)


# ---- a fixture carrying every group KIND, because the answer branches on all of them ----------
ROWS, _n = [], 0


def add(fn, ext, grp, motion=0):
    global _n
    _n += 1
    ROWS.append((os.path.join(_root, fn), fn, 'gen', fn, ext, 1000.0 + _n, 1000, 512, 512,
                 grp, RK, motion, 0))


def build():
    for i in range(3):                       # still + video PAIRS — the reported case
        add('pair%d.png' % i, '.png', 'p%d' % i)
        add('pair%d.mp4' % i, '.mp4', 'p%d' % i, motion=1)
    for i in range(2):                       # lone stills
        add('lone%d.png' % i, '.png', None)
    add('solo.mp4', '.mp4', None, motion=1)  # a lone video
    for st in ('Raw', 'Detail', 'Refine'):   # an image SET — no video, no audio: still Images
        add('set_%s.png' % st, '.png', 's0')
    add('song.mp3', '.mp3', 'a0')            # a song pair: audio + its cover art
    add('song.png', '.png', 'a0')
    add('solo.mp3', '.mp3', None)            # a lone song, no cover
    conn = index_db.connect(DB)
    conn.executemany(
        "INSERT INTO images(path, rel_path, folder, filename, ext, mtime, size, width, height,"
        " group_id, root_id, motion, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ROWS)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('merged','1')")
    conn.execute("INSERT INTO images_fts(images_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


PORT = [0]


def start_server():
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    PORT[0] = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def search(**kw):
    qs = '&'.join('%s=%s' % (k, v) for k, v in kw.items())
    with urllib.request.urlopen('http://127.0.0.1:%d/api/search?limit=99&%s' % (PORT[0], qs),
                                timeout=60) as r:
        return json.loads(r.read().decode())


def ids(items):
    return {i['id'] for i in items}


def main():
    print('\nFile type asks what a card IS, not what it contains\n')
    build()
    start_server()

    for merging, label in ((1, 'merging ON (the default)'), (0, 'merging OFF')):
        print('  --- %s ---' % label)
        everything = search(sets=merging, group=merging)
        img = search(type='image', sets=merging, group=merging)
        vid = search(type='video', sets=merging, group=merging)
        aud = search(type='audio', sets=merging, group=merging)

        # THE PARTITION. Both halves: nothing in two buckets, nothing in none.
        parts = [ids(img['items']), ids(vid['items']), ids(aud['items'])]
        overlap = (parts[0] & parts[1]) | (parts[0] & parts[2]) | (parts[1] & parts[2])
        union = parts[0] | parts[1] | parts[2]
        allids = ids(everything['items'])
        check('%s — no card is in two of the three' % label, not overlap,
              'in more than one: %s' % sorted(overlap))
        check('%s — every card is in one of the three' % label, union == allids,
              'missing: %s' % sorted(allids - union))
        check('%s — the three totals sum to the library' % label,
              img['total'] + vid['total'] + aud['total'] == everything['total'],
              '%d + %d + %d != %d' % (img['total'], vid['total'], aud['total'],
                                      everything['total']))

        # ...and the reported symptom, stated directly: nothing under Images may play.
        playable = [i['filename'] for i in img['items']
                    if i.get('video_id') or i.get('is_video') or i.get('motion')]
        check('%s — no card under Images carries a video' % label, not playable, str(playable))
        songs = [i['filename'] for i in img['items'] if i['filename'].endswith('.mp3')]
        check('%s — no song is filed under Images' % label, not songs, str(songs))
        print('')

    # ---- the two cases the partition alone would not catch -----------------------------------
    print('  --- what each option should actually hold ---')
    on = dict(sets=1, group=1)
    img = search(type='image', **on)
    names = sorted(i['filename'] for i in img['items'])
    check('an image SET survives Images (no video, no audio in it)',
          any(n.startswith('set_') for n in names), str(names))
    check('the set is still ONE card, not three',
          sum(1 for i in img['items'] if i['filename'].startswith('set_')) == 1, str(names))
    check('lone stills survive Images', {'lone0.png', 'lone1.png'} <= set(names), str(names))
    check('a still+video pair does NOT appear under Images',
          not any(n.startswith('pair') for n in names), str(names))

    vid = sorted(i['filename'] for i in search(type='video', **on)['items'])
    check('a pair still appears under Videos, faced by its still',
          sum(1 for n in vid if n.startswith('pair')) == 3, str(vid))
    check('a lone video appears under Videos', 'solo.mp4' in vid, str(vid))

    aud = sorted(i['filename'] for i in search(type='audio', **on)['items'])
    check('both songs appear under Songs', aud == ['solo.mp3', 'song.mp3'], str(aud))

    # With nothing merged a card IS a file — the still from a video run is an image again.
    off = sorted(i['filename'] for i in search(type='image', sets=0, group=0)['items'])
    check('with merging off, a video run\'s still IS an image',
          all(('pair%d.png' % i) in off for i in range(3)), str(off))
    check('with merging off, a song\'s cover art IS an image', 'song.png' in off, str(off))

    print('\n%s\n' % ('ALL PASS' if not _fails else '%d FAILED: %s' % (len(_fails), _fails[:4])))
    return 1 if _fails else 0


if __name__ == '__main__':
    try:
        rc = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(rc)
