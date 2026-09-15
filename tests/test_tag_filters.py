"""test_tag_filters.py -- the Labels and Tags counts must mean "how many you would get".

Run: python tests/test_tag_filters.py

THE BUG, found by the author on 2026-09-14: the rail read "To publish 10" beside a grid holding one card.
/api/tags counted the whole library, scoped only by which libraries were ticked -- no search text,
no model, no folder, no type, no dates, no quality. The SECOND count-vs-grid bug of that day; see
test_facet_type.py for the first, which was the same failure in the sidebar's dropdowns.

They are worth separating because the CAUSES were different, and only one of them was cheap. The
facet counts were a caller forgetting to pass filters the server already understood. Here the server
had no such ability at all: /api/tags never touched the grid's filter builder, so this needed real
work and cost real time -- measured at roughly +48ms on 110k images, and it now runs on every filter
change rather than once at boot.

A TAG DOES NOT NARROW ITSELF, the rule /api/facets already followed. `tags` and `fav` are dropped
before the filter is built, so ticking one label leaves the other four saying what switching to them
would give. Without it the exclusive labels would all read 0 the moment you picked one, and the list
could no longer be used to move between them -- which is most of what it is for.

ZERO IS NOT THE SAME EVERYWHERE, and that is the author's call rather than a consequence of the query. A
label that matches nothing keeps its row and reads 0, because five fixed rows vanishing reads as
breakage; Favorites does the same. A TAG that matches nothing simply drops out, because there can be
hundreds and a list of mostly-zero rows is harder to scan than a short one. The server delivers both
halves by construction: a tag with no matching images cannot appear in a GROUP BY over them, while
the label rows are drawn client-side from the fixed set and look their counts up.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The system temp, not the project folder -- the server thread still holds library.db open at
# cleanup on Windows, so a husk left here would stay. See the note in test_facet_counts.py.
_tmp = tempfile.mkdtemp(prefix='vv_tagfilters_')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)
_root = os.path.join(_tmp, 'lib0')
os.makedirs(_root, exist_ok=True)
with open(os.environ['CV_CONFIG'], 'w', encoding='utf-8') as f:
    json.dump({'roots': [{'path': _root}], 'port': 0}, f)

sys.path.insert(0, BASE)
import index_db                                          # noqa: E402
import server                                            # noqa: E402  (must follow the env vars)
from http.server import ThreadingHTTPServer              # noqa: E402

RK = server.CONFIG['roots'][0]['key']
failures = []


def check(name, got, want):
    ok = got == want
    print(('  ok    ' if ok else '  FAIL  ') + name + ('' if ok else '\n          got %r, wanted %r' % (got, want)))
    if not ok:
        failures.append(name)


# ---- the fixture -------------------------------------------------------------------------------
# 20 files. The label is on 10 of them, and EXACTLY ONE of those ten is a video -- so filtering to
# Videos must take the label from 10 to 1, which is the shape of what the author actually saw. A fixture
# where the label sat on the same proportion of every type would pass even with the filters still
# being ignored.
def build():
    conn = index_db.connect(os.path.join(os.environ['CV_DATA'], 'library.db'))
    rows = []
    for i in range(1, 21):
        ext = '.mp4' if i == 1 else '.png'
        rows.append(('%s/g/f%02d%s' % (RK, i, ext), 'g/f%02d%s' % (i, ext), 'g', 'f%02d%s' % (i, ext),
                     ext, 1_700_000_000 + i, 100, 1024, 1024, 'm.safetensors', 'm.safetensors',
                     'sdxl', 'a prompt', RK, 0))
    conn.executemany(
        "INSERT INTO images(path,rel_path,folder,filename,ext,mtime,size,width,height,"
        "model,model_name,model_type,positive,root_id,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    tags = [(i, 'label:publish', 'user') for i in range(1, 11)]     # 1..10, of which only 1 is a video
    tags += [(i, 'favorite', 'fav') for i in range(1, 6)]           # 1..5, of which only 1 is a video
    tags += [(i, 'stills-only', 'user') for i in range(2, 6)]       # 2..5: no videos at all
    conn.executemany("INSERT INTO tags(image_id, tag, source) VALUES(?,?,?)", tags)
    conn.commit()
    conn.close()


build()
httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def get(path, **kw):
    qs = urllib.parse.urlencode({k: v for k, v in kw.items() if v != ''})
    with urllib.request.urlopen('http://127.0.0.1:%d%s?%s' % (PORT, path, qs), timeout=60) as r:
        return json.loads(r.read().decode())


def counts(**kw):
    d = get('/api/tags', **kw)
    return {t['name']: t['count'] for t in d['tags']}, d['favorites']


def grid(**kw):
    return len(get('/api/search', limit=500, **kw)['items'])


# ---- the numbers are what picking them gives you -----------------------------------------------
c, fav = counts()
check('unfiltered: the label counts 10', c.get('label:publish'), 10)
check('unfiltered: favorites counts 5', fav, 5)
check('  and the grid agrees with the label', grid(tags='label:publish'), 10)

c, fav = counts(type='video')
check('filtered to Videos: the label drops to 1', c.get('label:publish'), 1)
check('  and the grid agrees', grid(tags='label:publish', type='video'), 1)
check('filtered to Videos: favorites drops to 1', fav, 1)

# ---- zero, which is two different answers ------------------------------------------------------
# A TAG with nothing matching leaves the list; the five LABELS are drawn client-side from a fixed
# set and read 0, which is why the server merely has to omit them rather than report them.
c, _ = counts(type='video')
check('a tag matching nothing is absent from the list', c.get('stills-only'), None)
check('  and it was there before the filter', counts()[0].get('stills-only'), 4)
c, fav = counts(type='audio')
check('nothing matches at all: the tag list is empty', c, {})
check('  and favorites reads 0 rather than the library total', fav, 0)

# ---- a tag does not narrow itself --------------------------------------------------------------
c, _ = counts(tags='label:publish')
check('a ticked label still counts its whole self', c.get('label:publish'), 10)
check('  and the other tags still show what switching would give', c.get('stills-only'), 4)
c, _ = counts(fav='1')
check('ticking Favorites does not narrow the label either', c.get('label:publish'), 10)

# ---- the caller, which is the half that broke last time -----------------------------------------
# The facet bug was app.js building a query string by hand and omitting filters. This request is
# built the same way, so it can fail the same way, and nothing above would notice.
src = open(os.path.join(BASE, 'app', 'app.js'), encoding='utf-8').read()
body = src[src.index('async function loadTags'):]
body = body[:body.index('getJSON(')]
for name in ('q:', 'model:', 'folder:', 'type:', 'meta:', 'roots:', 'after:', 'rmin:'):
    check('loadTags still sends %s' % name.rstrip(':'), name in body, True)

httpd.shutdown()
shutil.rmtree(_tmp, ignore_errors=True)
print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
