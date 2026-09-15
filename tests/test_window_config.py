"""Regression test for the TOP-LEVEL config keys that save_config has to remember: the window
geometry (/api/window) and the one-time Help pop-up's seen flag (/api/settings).

Run: python test_window_config.py

Asserted against config.json ON DISK, never the response. save_config() builds an EXPLICIT dict, so
a block missing from that whitelist is accepted by the endpoint, echoed back happily, written once,
and then silently dropped by the next save of anything else. The response cannot see that; the file
can. This is the same fault test_snapshots.py was written for when `snapshots` was added, and the
same shape of test.

The validation matters more than usual here: this value is handed to a browser on the next launch,
and a bad one is uniquely nasty — a window restored at (-32000, -32000) or sized 0x0 is a program
that simply stops appearing, with nothing on screen to explain why.

Runs in-process on an ephemeral port against a temp config. Touches no real library.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-window-')
CFG = os.path.join(TMP, 'config.json')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = CFG
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server                                              # noqa: E402
from http.server import ThreadingHTTPServer                # noqa: E402

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


def post(path, obj):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
    c.request('POST', path, json.dumps(obj), {'Content-Type': 'application/json'})
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body or b'{}')
    except ValueError:
        return r.status, {}


def on_disk(key='window'):
    if not os.path.exists(CFG):
        return None
    return json.load(io.open(CFG, encoding='utf-8')).get(key)


print('\nTop-level config keys that have to survive a save (server)\n')

# 1. It reaches the FILE, not just the response.
rect = {'x': 300, 'y': 200, 'w': 1400, 'h': 900}
st, j = post('/api/window', rect)
check('a window rect is accepted', st == 200 and j.get('ok'), '%s %s' % (st, j))
check('  and reaches config.json on disk', on_disk() == rect, on_disk())

# 2. THE WHITELIST. Saving something else must not drop it — this is the failure the response
#    cannot show, and the one that would make the feature work today and quietly stop tomorrow.
#    THE ENDPOINT IS /api/settings. This posted to /api/config for its first year, which is GET
#    only: the POST 404'd, nothing was saved, and the check passed without exercising anything.
#    A test that cannot fail is worse than no test, because it is counted.
st, _ = post('/api/settings', {'general': {'autoplay': True}})
check('  an unrelated config save is accepted', st == 200, 'status %s' % st)
check('  and the window survives it', on_disk() == rect,
      'window block after saving general: %s' % (on_disk(),))

# 3. A later position replaces the earlier one rather than accumulating.
rect2 = {'x': -1800, 'y': 140, 'w': 1200, 'h': 800}      # a left-hand second monitor: negative x
st, _ = post('/api/window', rect2)
check('a new position replaces the old', on_disk() == rect2, on_disk())
check('  including negative coordinates (a monitor to the left)', on_disk()['x'] == -1800)

# 4. Nonsense is REFUSED and does not overwrite the good value. A minimised window reports
#    (-32000, -32000) on Windows; storing that is an app that stops appearing.
bad = [
    ({'x': -32000, 'y': -32000, 'w': 1200, 'h': 800}, 'a minimised window'),
    ({'x': 10, 'y': 10, 'w': 0, 'h': 0}, 'a zero size'),
    ({'x': 10, 'y': 10, 'w': 120, 'h': 90}, 'an absurdly small window'),
    ({'x': 10, 'y': 10, 'w': 999999, 'h': 800}, 'an absurdly wide window'),
    ({'x': 'left', 'y': 10, 'w': 1200, 'h': 800}, 'a non-numeric coordinate'),
    ({'x': 10, 'y': 10}, 'a rect missing w/h'),
    ({}, 'an empty object'),
]
for obj, what in bad:
    st, _ = post('/api/window', obj)
    check('refuses %s' % what, st == 400, 'status %s' % st)
check('  and none of them clobbered the stored value', on_disk() == rect2, on_disk())

# 5. THE HELP POP-UP'S SEEN FLAG, the other top-level key, same hazard. It used to live in the
#    browser, where every copy of the folder shared one flag on localhost:8770 and a fresh copy
#    came up already marked as seen — so nobody could ever see the pop-up twice, including the
#    person testing it.
print('')
check('starts unset on a config with no libraries', on_disk('seen_help_hint') is False,
      on_disk('seen_help_hint'))
st, _ = post('/api/settings', {'seen_help_hint': True})
check('  /api/settings sets it', st == 200 and on_disk('seen_help_hint') is True,
      '%s %s' % (st, on_disk('seen_help_hint')))
st, _ = post('/api/settings', {'general': {'autoplay': False}})
check('  and it survives an unrelated config save', on_disk('seen_help_hint') is True,
      on_disk('seen_help_hint'))
# One-way. Nothing clears it: the flag means "this person has been shown the pop-up", which does
# not become untrue, and a body that could un-set it is a way to be shown it again by accident.
st, _ = post('/api/settings', {'seen_help_hint': False})
check('  and cannot be un-set', on_disk('seen_help_hint') is True, on_disk('seen_help_hint'))

# 6. ABSENT MEANS SEEN WHEN LIBRARIES EXIST — an existing user updating must not be told where to
#    start, and a genuinely fresh install must. This is read at load, so it is tested there.
def normalized(cfg):
    return server._normalize_config(dict(cfg))['seen_help_hint']

check('a fresh install (no libraries, no flag) has not seen it',
      normalized({}) is False, normalized({}))
check('an existing install (libraries, no flag) counts as seen',
      normalized({'roots': [{'path': TMP}]}) is True, normalized({'roots': [{'path': TMP}]}))
check('an explicit flag still wins over both',
      normalized({'roots': [{'path': TMP}], 'seen_help_hint': False}) is False)

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
