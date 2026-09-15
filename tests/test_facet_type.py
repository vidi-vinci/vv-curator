"""test_facet_type.py -- a dropdown's number must be what you GET when you pick it.

Run: python tests/test_facet_type.py

THE BUG THIS PINS, found 2026-09-14. The sidebar's Model and Folder counts come from /api/facets,
and the browser built that request by hand, listing the filters to pass. Three were missing: the
Type filter, the metadata filter, and `group`. So you could filter the grid to Videos and every
dropdown went on counting your images -- the Model list offering checkpoints you had only ever used
for stills, at their full still count, and handing you an empty grid when you picked one.

The number being off is not the failure. A DROPDOWN THAT OFFERS A CHOICE YIELDING NOTHING is, and
that is what this asserts: for every model the facet list returns, its count equals the number of
rows /api/search gives back for that same model. Same question, asked of the two endpoints that
have to agree.

WHY `group` RIDES WITH `type`. The server decides what counts as an Image from what the whole GROUP
holds rather than from the row (a still that is half of a still+video pair is not an Image), so
`type=image` means something different depending on whether pairs are collapsing. Passing `type`
without `group` would have traded a visible disagreement for a subtler one.

The server never needed changing -- _filters has always read `type`, and its own comment lists it
among the filters that "apply to all five tallies equally". Only the caller was wrong, which is why
test_facet_counts.py passed throughout: it asks the endpoint directly and never had this gap.
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

# The system temp, not the project folder -- see the note in test_facet_counts.py: the server
# thread still holds library.db open at cleanup on Windows, so a husk left here would stay.
_tmp = tempfile.mkdtemp(prefix='vv_facettype_')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)
ROOT_PATHS = [os.path.join(_tmp, 'lib0')]
os.makedirs(ROOT_PATHS[0], exist_ok=True)
with open(os.environ['CV_CONFIG'], 'w', encoding='utf-8') as f:
    json.dump({'roots': [{'path': p} for p in ROOT_PATHS], 'port': 0}, f)

sys.path.insert(0, BASE)
import index_db                                          # noqa: E402
import server                                            # noqa: E402  (must follow the env vars)
from http.server import ThreadingHTTPServer              # noqa: E402

DB = os.path.join(os.environ['CV_DATA'], 'library.db')
RK = server.CONFIG['roots'][0]['key']
_fails = []


def check(label, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + label + ('\n          ' + str(detail) if (detail and not cond) else ''))
    if not cond:
        _fails.append(label)


# ---- the fixture -------------------------------------------------------------------------------
# THE SHAPE THAT MATTERS: each checkpoint has a DIFFERENT mix of stills and videos, and one of them
# has no videos at all. A fixture where every model had the same ratio would pass even if `type`
# were still being dropped, because every count would be wrong by the same factor and the
# comparison below would not notice.
PLAN = [('alpha', 40, 10), ('beta', 25, 3), ('gamma', 18, 0), ('delta', 0, 12)]


def build():
    conn = index_db.connect(DB)
    rows, i = [], 0
    for mname, stills, vids in PLAN:
        for n in range(stills + vids):
            ext = '.png' if n < stills else '.mp4'
            i += 1
            folder = 'gen/%s' % mname
            rows.append(('%s/%s/f_%04d%s' % (RK, folder, i, ext),
                         '%s/f_%04d%s' % (folder, i, ext), folder, 'f_%04d%s' % (i, ext), ext,
                         1_700_000_000 + i * 60, 1000 + i, 1024, 1024,
                         '%s.safetensors' % mname, '%s.safetensors' % mname, mname,
                         'a prompt', RK, 0))
    conn.executemany(
        "INSERT INTO images(path, rel_path, folder, filename, ext, mtime, size, width, height,"
        " model, model_name, model_type, positive, root_id, indexed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
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


def model_counts(**kw):
    return {m['name']: m['count'] for m in get('/api/facets', **kw)['models']}


def grid_rows(**kw):
    return len(get('/api/search', limit=500, **kw)['items'])


# ---- 1. the parameter is honoured at all -------------------------------------------------------
plain = model_counts()
vids = model_counts(type='video')
check('the Model counts change when Type is set', plain != vids, '%s vs %s' % (plain, vids))

# ---- 2. THE PROPERTY: the number is what picking it gives you ----------------------------------
for name in plain:
    check('unfiltered: %s counts what the grid returns' % name,
          plain[name] == grid_rows(model=name),
          'facet %s, grid %s' % (plain.get(name), grid_rows(model=name)))

for name in vids:
    got = grid_rows(model=name, type='video')
    check('filtered to Videos: %s counts what the grid returns' % name,
          vids[name] == got, 'facet %s, grid %s' % (vids[name], got))

# ---- 3. the exact symptom: a choice that yields nothing ----------------------------------------
# `gamma` has stills and no videos. Filtered to Videos it must be ABSENT from the Model list, not
# present at its still count -- which is what the user saw and reported.
check('a model with no videos drops out of the list entirely', 'gamma.safetensors' not in vids, vids)
check('  and it did appear before the filter', 'gamma.safetensors' in plain, plain)
check('a model with only videos is still offered', 'delta.safetensors' in vids, vids)

# ---- 4. the metadata filter, the other dropped parameter ---------------------------------------
# The filter's two values are `ok` (has metadata) and `missing`. No fixture row carries a workflow,
# so every row is `missing`: `ok` must come back EMPTY rather than counting everything, and
# `missing` must count everything. Same fault as type= and it would regress the same way.
check('the metadata filter reaches the counts: ok is empty', model_counts(meta='ok') == {},
      model_counts(meta='ok'))
check('  and missing counts every row', model_counts(meta='missing') == plain,
      model_counts(meta='missing'))

# ---- 5. the caller, which is what actually broke ------------------------------------------------
# The server was always right; app.js built the query string by hand and omitted three filters.
# Assert the request still carries them, because the next person to touch loadFacets will be
# editing that literal list and nothing else here would notice.
src = open(os.path.join(BASE, 'app', 'app.js'), encoding='utf-8').read()
body = src[src.index('async function loadFacets'):]
body = body[:body.index('getJSON(')]
for name in ('type:', 'meta:', 'group:'):
    check('loadFacets still sends %s' % name.rstrip(':'), name in body)

httpd.shutdown()
shutil.rmtree(_tmp, ignore_errors=True)
print('\nall passed' if not _fails else '\n%d failed' % len(_fails))
sys.exit(1 if _fails else 0)
