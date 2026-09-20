"""Regression test for the theme's two modes and the edits each one carries.

Run: python test_theme_modes.py

WHY IT EXISTS. Dark and Light each keep their own colour edits, so switching across and back finds
your work where you left it, and Reset puts one mode back without touching the other. Before
2026-09-19 there was a single shared slot behind a third segment called Custom: a palette built on
Light was silently replaced the moment you tweaked a swatch while on Dark. The author, once it was
on the table: "Dark and Light EACH need a customized state that can be reset."

Asserted against config.json ON DISK, never the response. save_config() builds an EXPLICIT dict, so
a block missing from that whitelist is accepted, echoed back happily, written once, and then
silently dropped by the next save of anything else — here that would mean your colours surviving
until you changed one checkbox. The response cannot see that; the file can. Same shape as
test_window_config.py.

`theme` holds the RESOLVED colours the app applies and `theme_mode`/`theme_edits` are the recipe
that produced them. The client sends all three together because the server cannot resolve them
itself — the light preset's values live in app.js, beside the stylesheet they override — so the
test drives it the same way the client does.

The bar and the Reset button are pinned separately, in test_theme_seg.py.

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

TMP = tempfile.mkdtemp(prefix='vv-thememodes-')
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


def call(method, path, obj=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
    if obj is None:
        c.request(method, path)
    else:
        c.request(method, path, json.dumps(obj), {'Content-Type': 'application/json'})
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body or b'{}')
    except ValueError:
        return r.status, {}


def on_disk(key):
    if not os.path.exists(CFG):
        return None
    return json.load(io.open(CFG, encoding='utf-8')).get(key)


def save(mode, edits, resolved=None):
    """Drive it the way the client does: resolved colours plus the recipe, in one body."""
    return call('POST', '/api/settings', {
        'theme': resolved if resolved is not None else dict(edits.get(mode, {})),
        'theme_mode': mode, 'theme_edits': edits})


DARKED = {'--accent': '#b5651d'}                     # an edit made while on Dark
LIGHTED = {'--accent': '#2a6f97', '--bg': '#fbfbfd'}  # a different one made while on Light

print('\nTwo modes, each keeping its own edits (server)\n')

# 1. It reaches the FILE, not just the response.
st, j = save('dark', {'dark': DARKED, 'light': {}})
check('an edit on Dark is accepted', st == 200 and not j.get('error'), '%s %s' % (st, j))
check('  and reaches config.json on disk', on_disk('theme_edits') == {'dark': DARKED, 'light': {}},
      on_disk('theme_edits'))
check('  with the mode recorded beside it', on_disk('theme_mode') == 'dark', on_disk('theme_mode'))

# 2. THE WHITELIST, which is why this file exists. Saving anything else must not drop it.
st, _ = call('POST', '/api/settings', {'general': {'autoplay': True}})
check('  an unrelated save is accepted', st == 200, 'status %s' % st)
check('  and the edits survive it', on_disk('theme_edits') == {'dark': DARKED, 'light': {}},
      'theme_edits after saving general: %s' % (on_disk('theme_edits'),))
check('  as does the mode', on_disk('theme_mode') == 'dark', on_disk('theme_mode'))

# 3. THE WHOLE POINT: editing the other mode leaves the first one alone. One shared slot is what
#    made a palette vanish, and this is the assertion that would have caught it.
st, _ = save('light', {'dark': DARKED, 'light': LIGHTED}, resolved=LIGHTED)
check('switching to Light and editing it', on_disk('theme_mode') == 'light', on_disk('theme_mode'))
check('  keeps Light\'s own edits', (on_disk('theme_edits') or {}).get('light') == LIGHTED,
      on_disk('theme_edits'))
check('  and does NOT touch what Dark carries', (on_disk('theme_edits') or {}).get('dark') == DARKED,
      on_disk('theme_edits'))

# 4. RESET is one mode emptied, with the other untouched.
st, _ = save('light', {'dark': DARKED, 'light': {}}, resolved={})
check('resetting Light empties only Light', (on_disk('theme_edits') or {}).get('light') == {},
      on_disk('theme_edits'))
check('  Dark still has its colour', (on_disk('theme_edits') or {}).get('dark') == DARKED,
      on_disk('theme_edits'))

# 5. Both come back on the next open — this is what the Appearance tab seeds itself from.
st, cfg = call('GET', '/api/config')
check('/api/config hands back the mode', cfg.get('theme_mode') == 'light', cfg.get('theme_mode'))
check('  and both slots', cfg.get('theme_edits') == {'dark': DARKED, 'light': {}},
      cfg.get('theme_edits'))

# 6. Validated like any theme, so junk cannot reach the stylesheet, and an unknown mode falls back
#    rather than being stored as a third one.
st, _ = save('dark', {'dark': {'--accent': 'javascript:alert(1)'}, 'light': {}})
check('a junk colour is not stored',
      (on_disk('theme_edits') or {}).get('dark', {}).get('--accent') != 'javascript:alert(1)',
      on_disk('theme_edits'))
st, _ = call('POST', '/api/settings', {'theme_mode': 'custom'})
check('an unknown mode falls back to dark rather than being kept',
      on_disk('theme_mode') == 'dark', on_disk('theme_mode'))

# 7. MIGRATION. A config written before modes existed carries one flat palette and no recipe. It is
#    somebody's real work, so it is kept as the edits of whichever mode it reads as — decided by the
#    background, the one colour a preset always supplies and a user rarely touches.
httpd.shutdown()
legacy_light = {'--bg': '#eceef2', '--accent': '#2a6f97'}
io.open(CFG, 'w', encoding='utf-8').write(json.dumps(
    {'roots': [], 'active': None, 'theme': legacy_light}))
migrated = server.load_config() if hasattr(server, 'load_config') else None
if migrated is None:
    check('load_config is reachable for the migration check', False, 'no load_config in server')
else:
    check('a legacy light-based palette migrates to the Light mode',
          migrated.get('theme_mode') == 'light', migrated.get('theme_mode'))
    check('  carrying its colours as that mode\'s edits',
          (migrated.get('theme_edits') or {}).get('light') == legacy_light,
          migrated.get('theme_edits'))
    check('  and leaving Dark empty', (migrated.get('theme_edits') or {}).get('dark') == {},
          migrated.get('theme_edits'))

io.open(CFG, 'w', encoding='utf-8').write(json.dumps({'roots': [], 'active': None, 'theme': {}}))
plain = server.load_config()
check('a config with no theme at all comes up on Dark, unedited',
      plain.get('theme_mode') == 'dark' and plain.get('theme_edits') == {'dark': {}, 'light': {}},
      '%s %s' % (plain.get('theme_mode'), plain.get('theme_edits')))

print('')
if failures:
    print('%d FAILED: %s' % (len(failures), ', '.join(failures)))
else:
    print('all passed')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)
