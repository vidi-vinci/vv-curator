"""Regression test: the ONE-PASS facet query returns exactly what five separate GROUP BYs did.

Run:  python test_facet_counts.py

`/api/facets` fills the sidebar's four count lists — models, folders, model types, libraries — plus
the overall total. It used to answer them with five separate queries, each a GROUP BY or COUNT over
the whole matching library, differing only in which dimension was left OUT of the narrowing (a
facet must not narrow itself away, or the value you picked disappears from the list you picked it
from). Five whole-library reads to answer five questions about the same rows: 847ms on a 120k
fixture. It is now one materialised pass carrying the four dimensions and a per-row flag for each,
and each tally applies the three flags that are not its own — 232ms, same answers.

**The saving is only free if the answers are identical, and that is the entire risk.** A facet
count that lies looks completely normal on screen: a dropdown showing "flux 812" when the grid
would show 790 is indistinguishable from the truth until you click it. So this runs both forms
side by side over a matrix of filter combinations and asserts every list matches, name for name and
count for count.

TWO ORACLES, because each covers what the other cannot:

  PART 1 rebuilds the OLD five-query form from the shipped `_filters`, exclude-one-dimension at a
  time, exactly as the endpoint used to call it — so the predicates under test are the real ones
  and not a copy that could drift. That lets it cover filters no test could reasonably recompute by
  hand: full-text search, exclusions, tag and quality filters, date ranges.

  PART 2 recomputes the unfiltered lists in PLAIN PYTHON from the fixture rows, which is the only
  check here that does not run through `_filters` at all. Part 1 alone would pass even if a shared
  predicate were wrong in the same way on both sides.

The combinations matter more than the row count. Each dimension has to be tested both as the
excluded one and as an applied one, and the flags are also where a NULL can go wrong — a row with
no model, or a library that is switched off — so the fixture carries all of those.
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

# THE SYSTEM TEMP, NOT THE PROJECT FOLDER — 20 places said `dir=here`/`dir=BASE`, and every one
# of them was wrong for the same reason. Each cleans up in a finally with ignore_errors=True, and
# on Windows the server thread still holds library.db open when that runs, so the removal fails
# SILENTLY and the husk stays. 25 of them, 503MB, had accumulated in the master folder before the author
# spotted them. A test's mess belongs where the OS already sweeps up.
_tmp = tempfile.mkdtemp(prefix='vv_facets_')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

# Three libraries, written to config BEFORE server is imported: server reads config.json at import.
ROOT_PATHS = [os.path.join(_tmp, 'lib%d' % i) for i in range(3)]
for p in ROOT_PATHS:
    os.makedirs(p, exist_ok=True)
with open(os.environ['CV_CONFIG'], 'w', encoding='utf-8') as f:
    json.dump({'roots': [{'path': p} for p in ROOT_PATHS], 'port': 0}, f)

sys.path.insert(0, BASE)
import index_db                                          # noqa: E402
import server                                            # noqa: E402  (must follow the env vars)
from http.server import ThreadingHTTPServer              # noqa: E402

DB = os.path.join(os.environ['CV_DATA'], 'library.db')
RK = [r['key'] for r in server.CONFIG['roots']]

_fails = []


def check(label, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + label + (
        '\n          ' + detail if (detail and not cond) else ''))
    if not cond:
        _fails.append(label)


# ---- the fixture ------------------------------------------------------------------------------
# Every shape a flag can take: a row with no model at all (so `has_model` and the '(none)' model
# type are both real), a model with no family folder, three libraries, favourited rows, notes,
# quality scores, videos and audio, and prompt text to search.
FAMILIES = ['sdxl', 'pony', 'flux', '']          # '' = a model sitting at the top level
WORDS = 'neon alley rain coat fog portrait golden light grain forest sunrise'.split()
ROWS = []


def build():
    conn = index_db.connect(DB)
    rows, qual, tags = [], [], []
    for i in range(900):
        rid = RK[i % 3]
        folder = 'gen/%03d' % (i % 12)
        fam = FAMILIES[i % 4]
        if i % 10 == 0:                            # a tenth carry no model
            model = mname = mtype = None
        else:
            mname = 'checkpoint_%02d.safetensors' % (i % 7)
            model = ('%s/%s' % (fam, mname)) if fam else mname
            mtype = fam
        ext = '.mp4' if i % 23 == 0 else ('.mp3' if i % 31 == 0 else '.png')
        pos = ' '.join(WORDS[(i + k) % len(WORDS)] for k in range(6))
        w, h = (5000, 5000) if i % 50 == 0 else (1024, 1024)
        note = 'keep this one' if i % 17 == 0 else None
        rows.append(('%s/%s/img_%04d%s' % (rid, folder, i, ext),
                     '%s/img_%04d%s' % (folder, i, ext), folder, 'img_%04d%s' % (i, ext), ext,
                     1_700_000_000 + i * 60, 1000 + i, w, h, model, mname, mtype, pos,
                     note, rid, 0))
        ROWS.append({'id': i + 1, 'root': rid, 'folder': folder, 'mname': mname, 'model': model,
                     'mtype': mtype or '', 'w': w, 'h': h})
    conn.executemany(
        "INSERT INTO images(path, rel_path, folder, filename, ext, mtime, size, width, height,"
        " model, model_name, model_type, positive, note, root_id, indexed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for i in range(1, 901):
        if i % 13 == 0:
            tags.append((i, 'favorite', 'fav'))
        if i % 7 == 0:
            tags.append((i, 'style%02d' % (i % 5), 'user'))
        if i % 11 == 0:
            qual.append((i, 0.2 + (i % 8) / 10.0))
    conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES(?,?,?)", tags)
    conn.executemany("INSERT OR IGNORE INTO quality(image_id, reward) VALUES(?,?)", qual)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('merged','1')")
    conn.execute("INSERT INTO images_fts(images_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


# ---- PART 1: the OLD five-query form, rebuilt from the shipped _filters ------------------------
# `_filters` still takes a single dimension name, which is exactly how api_facets used to call it
# — once per facet. So this is the previous endpoint, not a paraphrase of it, and the predicates
# it tests are the ones the grid uses. `_filters` touches nothing on `self`, so an unbound call
# with None is enough and avoids standing up a request to get a handler.
_F = server.Handler._filters
MT = server.MODEL_TYPE_SQL


def old_form(q):
    conn = server.db()
    jm, wm, pm = _F(None, q, exclude='model')
    models = [{'name': r['model_name'] or '(none)', 'count': r['c']}
              for r in conn.execute(f"SELECT i.model_name, COUNT(*) c FROM images i {jm} {wm} "
                                    f"GROUP BY i.model_name ORDER BY c DESC", pm)]
    jf, wf, pf = _F(None, q, exclude='folder')
    folders = [{'name': r['folder'], 'count': r['c']}
               for r in conn.execute(f"SELECT i.folder, COUNT(*) c FROM images i {jf} {wf} "
                                     f"GROUP BY i.folder ORDER BY c DESC, i.folder", pf)]
    jt, wt, pt = _F(None, q, exclude='mfolder')
    wt = (wt + " AND i.model IS NOT NULL AND i.model <> ''") if wt else \
         "WHERE i.model IS NOT NULL AND i.model <> ''"
    mtypes = [{'name': (r['tf'] if r['tf'] else '(none)'), 'count': r['c']}
              for r in conn.execute(f"SELECT {MT} tf, COUNT(*) c FROM images i {jt} {wt} "
                                    f"GROUP BY LOWER(tf) ORDER BY c DESC", pt)]
    jr, wr, pr = _F(None, q, exclude='root')
    rcounts = {r['root_id']: r['c'] for r in conn.execute(
        f"SELECT i.root_id, COUNT(*) c FROM images i {jr} {wr} GROUP BY i.root_id", pr)}
    roots = sorted(({'key': r['key'], 'name': r['name'], 'count': rcounts.get(r['key'], 0)}
                    for r in server.CONFIG['roots']), key=lambda x: -x['count'])
    ja, wa, pa = _F(None, q)
    total = conn.execute(f"SELECT COUNT(*) c FROM images i {ja} {wa}", pa).fetchone()['c']
    conn.close()
    return {'models': models, 'folders': folders, 'mtypes': mtypes, 'roots': roots,
            'total': total}


# ---- the live endpoint -------------------------------------------------------------------------
PORT = [0]


def start_server():
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    PORT[0] = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def facets(qs):
    with urllib.request.urlopen('http://127.0.0.1:%d/api/facets?%s' % (PORT[0], qs),
                                timeout=60) as r:
        return json.loads(r.read().decode())


def as_pairs(lst, key='name'):
    """Compare as a SET of (name, count). Order between equal counts was never defined by the old
    ORDER BY c DESC, so asserting on it would pin an arbitrary tie-break rather than the answer.
    The order that IS defined — descending count — is checked separately."""
    return sorted((d[key], d['count']) for d in lst)


CASES = [
    ('no filter', ''),
    ('one folder', 'folder=gen/007'),
    ('one model', 'model=checkpoint_03.safetensors'),
    ('the no-model bucket', 'model=(none)'),
    ('one model type', 'mfolder=flux'),
    ('the no-family bucket', 'mfolder=(none)'),
    ('one library', 'roots=%s' % RK[0]),
    ('two libraries', 'roots=%s,%s' % (RK[0], RK[1])),
    ('a text search', 'q=neon'),
    ('an excluded word', 'x=forest'),
    ('videos only', 'type=video'),
    ('images only', 'type=image'),
    ('songs only', 'type=audio'),
    ('favourites', 'fav=1'),
    ('has notes', 'note=1'),
    ('a tag', 'tags=style01'),
    ('a quality range', 'rmin=0.4&rmax=0.8'),
    ('a date range', 'after=1700000000&before=1700020000'),
    ('metadata missing', 'meta=missing'),
    ('folder + model', 'folder=gen/003&model=checkpoint_03.safetensors'),
    ('text + type + library', 'q=alley&mfolder=pony&roots=%s' % RK[1]),
    ('folder + no-family + favourites', 'folder=gen/005&mfolder=(none)&fav=1'),
    ('every dimension at once',
     'folder=gen/002&model=checkpoint_02.safetensors&mfolder=pony&roots=%s' % RK[2]),
    ('matches nothing', 'folder=nope/nope'),
]


def part1():
    print('\nPART 1 - one pass agrees with five, over %d filter combinations\n' % len(CASES))
    # A LIBRARY'S SIZE IS THE SAME NUMBER WHATEVER YOU HAVE FILTERED TO, which is the whole of what
    # The author asked for: "it should always read the total file count." The Libraries rows used to show
    # `count`, so every one of them shrank as you typed and the list re-ordered itself under the
    # cursor. Collected across every case below and asserted once at the end, because the property
    # is about them AGREEING WITH EACH OTHER, not about any single one being right.
    sizes = {}
    for label, qs in CASES:
        new = facets(qs)
        sizes[label] = {d['key']: d['total'] for d in new['roots']}
        old = old_form(urllib.parse.parse_qs(qs))
        bad = []
        for k in ('models', 'folders', 'mtypes', 'roots'):
            a, b = as_pairs(old[k], 'key' if k == 'roots' else 'name'), \
                   as_pairs(new[k], 'key' if k == 'roots' else 'name')
            if a != b:
                only_old = sorted(set(a) - set(b))[:3]
                only_new = sorted(set(b) - set(a))[:3]
                bad.append('%s: was %d rows now %d; old-only %s new-only %s'
                           % (k, len(a), len(b), only_old, only_new))
        if old['total'] != new['total']:
            bad.append('total: was %d now %d' % (old['total'], new['total']))
        check(label, not bad, ' | '.join(bad))
        # The one ordering the old form did promise: most first.
        for k in ('models', 'folders', 'mtypes'):
            counts = [d['count'] for d in new[k]]
            if counts != sorted(counts, reverse=True):
                check('%s - %s is ordered by count' % (label, k), False, str(counts[:8]))
        # ROOTS ARE THE EXCEPTION, from 2026-09-14, and in two ways. They carry the library's SIZE
        # as well as its share of the matches, because the Libraries rows show the size -- a library
        # is as big as it is whatever you have filtered to. And they come back in CONFIG ORDER, the
        # order the libraries were added, rather than ranked: the rows are drawn from state.roots and
        # use this only as a lookup table, so any ranking here is a claim about an order nothing
        # reads. `count` is still present and still filtered; it is simply not what the rows show.
        if [d['key'] for d in new['roots']] != RK:
            check('%s - roots come back in the order they were added' % label, False,
                  str([d['key'] for d in new['roots']]))
        if any('total' not in d for d in new['roots']):
            check('%s - every root carries its size' % label, False, str(new['roots'][:3]))

    # The unfiltered case is the truth; every other case must report the same sizes.
    base = sizes['no filter']
    odd = [lbl for lbl, s in sizes.items() if s != base]
    check('a library is the same size under every filter', not odd,
          'these disagreed with the unfiltered sizes: %s\n          unfiltered %s\n          e.g. %s'
          % (odd[:4], base, sizes[odd[0]]) if odd else '')
    # And it is not vacuous: the FILTERED count really does move, which is the number the rows used
    # to show. If this ever stops being true, the test above has stopped proving anything.
    filtered = {lbl: {d['key']: d['count'] for d in facets(qs)['roots']} for lbl, qs in CASES}
    check('while the filtered count genuinely varies',
          any(v != filtered['no filter'] for v in filtered.values()))


# ---- PART 2: an oracle that does not go through _filters ---------------------------------------
def part2():
    print('\nPART 2 - the unfiltered lists, recomputed in plain Python\n')
    new = facets('')
    # The oracle mirrored two global exclusions -- oversized rows and the Hidden mark -- and both
    # were removed on 2026-09-08. Nothing is held back from an unfiltered view now.
    vis = list(ROWS)
    check('the fixture is not trivially empty, and nothing is held back from it',
          len(vis) == len(ROWS) > 400, '%d visible of %d' % (len(vis), len(ROWS)))

    def tally(f, rows=None):
        """(name, count) pairs. A NULL model_name is a real bucket — it is what '(none)' means —
        so it is folded to the label the endpoint gives it rather than dropped or sorted against
        a string."""
        out = {}
        for r in (rows if rows is not None else vis):
            k = f(r)
            k = '(none)' if k is None else k
            out[k] = out.get(k, 0) + 1
        return sorted(out.items())

    want_models = [(k if k is not None else '(none)', c)
                   for k, c in tally(lambda r: r['mname'])]
    check('models', as_pairs(new['models']) == sorted(want_models),
          'want %s\n          got  %s' % (sorted(want_models)[:4], as_pairs(new['models'])[:4]))
    check('folders', as_pairs(new['folders']) == sorted(tally(lambda r: r['folder'])))
    withmodel = [r for r in vis if r['model']]
    want_mt = [(k if k else '(none)', c)
               for k, c in tally(lambda r: r['mtype'], withmodel)]
    check('model types (only rows that HAVE a model)',
          as_pairs(new['mtypes']) == sorted(want_mt),
          'want %s\n          got  %s' % (sorted(want_mt), as_pairs(new['mtypes'])))
    check('libraries', as_pairs(new['roots'], 'key') == sorted(tally(lambda r: r['root'])))
    check('the total', new['total'] == len(vis), '%d vs %d' % (new['total'], len(vis)))


def main():
    print('\nOne-pass facets agree with five separate queries')
    build()
    start_server()
    part1()
    part2()
    print('\n%s\n' % ('ALL PASS' if not _fails else '%d FAILED: %s' % (len(_fails), _fails[:5])))
    return 1 if _fails else 0


if __name__ == '__main__':
    try:
        rc = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(rc)
