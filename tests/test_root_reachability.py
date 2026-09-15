"""test_root_reachability.py -- a library that comes back is noticed without a restart.

Run: python tests/test_root_reachability.py

The author, 2026-09-06: "when libraries go offline, then come back online, they still show as missing,
even though the images get restored." Then, correcting himself a minute later and turning a cosmetic
complaint into a real one: "actually i take it back, it does not seem to recover - it shows cached
thumbnails."

THE SECOND SENTENCE IS THE BUG. Reachability reached the client from /api/config alone, which is
fetched at boot and on library edits -- add, rename, remove, set models folder -- and never on a
timer. So nothing on a running app ever re-asked. Meanwhile the grid went on drawing from
data/thumbs/, which is a LOCAL cache and works whether or not the share does, so the library looked
recovered while every full-size file, drag and playback was still refused. A wrong "(missing)" label
would have been cosmetic; a library that never comes back until you restart is not.

The fix rides /api/changes, the disk heartbeat that already runs -- on a filter change, on returning
to the window, on the R key, on an auto-refresh tick, and now on opening the Libraries list. This
file pins the two halves that can silently rot:

  * THE ENDPOINT ANSWERS FROM DISK, EVERY TIME. The value has to track a folder appearing while the
    process runs -- that is the entire bug -- so caching it anywhere is the regression to catch.
  * THE CLIENT REPAINTS THE GRID, NOT JUST THE ROW. Reachability decides each CARD's offline badge,
    whether it can be dragged, and whether a video or song will play. Updating the Libraries list
    alone would fix the label the author first reported and leave the thing he corrected it to.

Runs in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-reach-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402  (must follow the env vars above)
from http.server import ThreadingHTTPServer  # noqa: E402

HERE = os.path.join(TMP, 'here')          # a library that is present the whole time
GONE = os.path.join(TMP, 'gone')          # the unplugged share: created part-way through
os.makedirs(HERE, exist_ok=True)

server.CONFIG['roots'] = [
    {'path': HERE, 'name': 'Here', 'key': 'here'},
    {'path': GONE, 'name': 'Gone', 'key': 'gone'},
]

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
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    c.request('GET', path)
    r = c.getresponse()
    body = r.read().decode('utf-8')
    c.close()
    return r.status, json.loads(body)


# ---- 1. the endpoint reports reachability at all ----------------------------------------------
st, j = get('/api/changes')
check('/api/changes answers', st == 200, st)
check('  and reports reachability per library', isinstance(j.get('exists'), dict), j.get('exists'))
check('  the present library reads as reachable', j.get('exists', {}).get('here') is True, j)
check('  the missing one reads as unreachable', j.get('exists', {}).get('gone') is False, j)

# ---- 2. THE REGRESSION: it is answered from disk on every call, not remembered -----------------
# The share comes back while the process keeps running. Nothing is restarted, nothing is re-added,
# no config is rewritten -- exactly the author's case, and the one a cached value gets wrong.
os.makedirs(GONE, exist_ok=True)
st, j2 = get('/api/changes')
check('a library that comes back is seen WITHOUT a restart',
      j2.get('exists', {}).get('gone') is True, j2)
check('  and the other library is unaffected', j2.get('exists', {}).get('here') is True, j2)

# ...and it must fall back the same way, or "offline" would become a one-way door.
shutil.rmtree(GONE)
st, j3 = get('/api/changes')
check('a library that goes away again is seen too', j3.get('exists', {}).get('gone') is False, j3)

# ---- 3. /api/config keeps its own copy, and the two must agree ---------------------------------
# Both are live isdir calls. If one is ever cached, this is where the disagreement shows up.
st, cfg = get('/api/config')
by_key = {r['key']: r.get('exists') for r in cfg.get('roots', [])}
check('/api/config agrees with /api/changes', by_key == j3.get('exists'), (by_key, j3.get('exists')))

# ---- 4. the client half, asserted at source ----------------------------------------------------
# Source-level for the reason test_drag_name.py uses the same trick: the failure mode is a future
# edit that does not know the rule exists, and no fixture would catch that.
js = io.open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8').read()
fc = js[js.index('async function fetchChanges('):]
fc = fc[:fc.index('\n}')]
check('the client reads reachability off the changes poll', 'exists' in fc, fc[:200])
check('  and writes it back onto state.roots, which isRootOffline reads',
      'r.exists =' in fc or 'r.exists=' in fc, fc[:400])

# `_checkChanges` is the body; `checkChanges` is the single-flight wrapper in front of it, added
# 2026-09-15 so that returning to the window asks the shares once rather than twice. The repaint
# rules being checked here live in the body.
cc = js[js.index('async function _checkChanges('):]
cc = cc[:cc.index('\n}')]
check('a reachability FLIP repaints the grid, not only the Libraries row',
      'flipped' in cc and 'search(' in cc, cc)
check('  and an unchanged poll does NOT repaint it (this runs off every filter change)',
      'if (flipped)' in cc, cc)

# ONE CHECK AT A TIME. Returning to the window wakes probeChanges() and the auto-refresh tick at the
# same moment, and both ask the shares. A real trace on five libraries showed two overlapping
# /api/changes on EVERY return, about a second each. The guard has to wrap checkChanges rather than
# the fetch under it, because this function also repaints the list, may toast, and on a flip re-runs
# the whole search -- sharing only the network call would still do the expensive half twice.
wrapper = js[js.index('function checkChanges('):]
wrapper = wrapper[:wrapper.index('\n}')]
check('a second caller joins the check in flight rather than starting another',
      '_changesInFlight' in wrapper, wrapper)
check('  and the in-flight promise is cleared, so a failure cannot wedge later checks',
      'finally' in wrapper and '_changesInFlight = null' in wrapper, wrapper)

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
