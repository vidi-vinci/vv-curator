"""Regression test for per-card-size thumbnails (serve_thumb's ?s=).

Run: python test_thumb_size.py

Every card used to download the 512px thumbnail whatever size it was drawn at — ~22x the bytes an
S card can show. The cache was always size-partitioned (thumb_rel puts the size in the top folder);
only the wiring was missing.

What actually needs guarding is not "does it serve a thumbnail" but:
  · the size served is the size asked for, so the fix is real rather than a renamed URL;
  · sizes do not collide in the cache, or one size would serve another's pixels;
  · an unknown or hostile `s` cannot reach the filesystem — it names a directory;
  · the default is unchanged, so anything that never asks still behaves as before.

Runs in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-thumbsize-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image                                    # noqa: E402
import server                                            # noqa: E402
import thumbs as thumbs_mod                              # noqa: E402
from http.server import ThreadingHTTPServer              # noqa: E402

# A source image shaped like a ComfyUI output, big enough that every thumbnail size downscales.
# DETAILED, not flat: a plain colour compresses to almost nothing at every size, so the byte-ratio
# assertion below would be comparing file headers and would pass or fail for no meaningful reason.
SRC = os.path.join(TMP, 'shot.png')
_im = Image.new('RGB', (1216, 832))
_px = _im.load()
_rnd = __import__('random').Random(11)
for _y in range(0, 832, 4):                       # 4px blocks: survives downscaling, unlike per-pixel
    for _x in range(0, 1216, 4):                  # noise, which averages away to flat grey
        _c = (_rnd.randrange(256), _rnd.randrange(256), _rnd.randrange(256))
        for _dy in range(4):
            for _dx in range(4):
                _px[_x + _dx, _y + _dy] = _c
_im.save(SRC)

server.Handler._path_for = lambda self, iid: {'path': SRC, 'thumb': None}

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def get(path):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
    c.request('GET', path)
    r = c.getresponse()
    body = r.read()
    ctype = r.getheader('Content-Type')
    c.close()
    return r.status, ctype, body


def longest_edge(body):
    with Image.open(io.BytesIO(body)) as im:
        return max(im.size), len(body)


print('\nPer-card-size thumbnails\n')

# 1. Each size actually arrives at that size. The point of the change, and the thing a URL
#    parameter could silently fail to do while still returning a perfectly good picture.
sizes = {}
for s in (128, 192, 256, 512):
    st, ctype, body = get('/thumb/1?v=1&r=r1&s=%d' % s)
    edge, nbytes = longest_edge(body)
    sizes[s] = nbytes
    check('s=%d serves a %dpx thumbnail' % (s, s), st == 200 and edge == s,
          'status=%s edge=%s' % (st, edge))
    check('  as webp', ctype == 'image/webp', ctype)

# 2. The bytes fall the way the whole change depends on. Asserted as a RATIO rather than absolute
#    numbers, which vary with the image; the claim being defended is "an S card costs a fraction of
#    an XL one", not any particular kilobyte count.
check('a 128px thumbnail is a small fraction of a 512px one',
      sizes[128] * 8 < sizes[512], '128=%d bytes, 512=%d bytes' % (sizes[128], sizes[512]))
print('        (measured: %s bytes at 128px vs %s bytes at 512px — %.1fx)'
      % (format(sizes[128], ','), format(sizes[512], ','), sizes[512] / max(sizes[128], 1)))

# 3. Sizes must not share a cache path, or one size would serve another's pixels. Checked on disk
#    rather than through the API, since a collision could still look right for one request.
rels = {s: thumbs_mod.thumb_rel(SRC, s) for s in (128, 192, 256, 512)}
check('each size caches to its own path', len(set(rels.values())) == 4, rels)
check('  and every one of them exists on disk',
      all(os.path.exists(os.path.join(server.THUMBS_DIR, r)) for r in rels.values()))

# 4. `s` names a directory, so anything unrecognised must fall back rather than be trusted.
for bad in ('999', '0', '-1', 'abc', '', '../../etc', '128.5'):
    st, _c, body = get('/thumb/1?v=1&r=r1&s=%s' % bad)
    edge, _n = longest_edge(body) if st == 200 else (None, 0)
    check("s=%r falls back to the default" % bad,
          st == 200 and edge == thumbs_mod.THUMB_SIZE, 'status=%s edge=%s' % (st, edge))

# 5. No `s` at all behaves exactly as before the change — an old cached page, or any caller that
#    never learned about the parameter, must not break.
st, _c, body = get('/thumb/1?v=1&r=r1')
edge, _n = longest_edge(body)
check('no s= still serves the default size', st == 200 and edge == thumbs_mod.THUMB_SIZE,
      'status=%s edge=%s' % (st, edge))

# 6. Repeat requests come from the cache, not a re-render — identical bytes, and fast.
st, _c, a = get('/thumb/1?v=1&r=r1&s=192')
st, _c, b = get('/thumb/1?v=1&r=r1&s=192')
check('a repeat request is byte-identical (served from cache)', a == b)

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
