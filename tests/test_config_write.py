"""test_config_write.py -- an interrupted save must not cost you config.json.

Run: python tests/test_config_write.py

WHAT WENT WRONG BEFORE. save_config opened config.json with 'w', which truncates before writing a
byte, and it runs from a dozen places -- every Settings change, and the window position as the app
closes, which is exactly when the process is most likely to be killed. Interrupted there, the file
was left half-written. load_config then silently fell back to defaults (it still does, deliberately
-- a config you cannot read must not stop the app starting), and the NEXT save wrote those defaults
over the damaged file. Every library, snapshot and setting, gone from one badly-timed exit, and the
evidence gone with it.

THE PROPERTY THIS PINS: after a save that fails part way through, config.json is byte-for-byte what
it was. Not "still valid JSON" -- unchanged. The fix is to write beside it and os.replace it into
place, so a reader sees the whole old file or the whole new one.

IT INTERRUPTS THE REAL WRITE rather than simulating one. A test that checked only the happy path
would pass against the original code, which is what let this survive as long as it did.
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DIR = tempfile.mkdtemp()
os.environ['CV_CONFIG'] = os.path.join(_DIR, 'config.json')
os.environ['CV_DATA'] = os.path.join(_DIR, 'data')
os.environ.pop('PORT', None)

import server  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


def scratch_files():
    return [f for f in os.listdir(_DIR) if f.startswith('.config-')]


def on_disk():
    return io.open(os.environ['CV_CONFIG'], encoding='utf-8').read()


# A config with things in it that would actually hurt to lose.
server.CONFIG['roots'] = [{'key': 'abc', 'path': r'X:\pictures', 'name': 'Pictures'}]
server.CONFIG['snapshots'] = [{'name': 'Golden hour portraits', 'filters': {'q': 'portrait'}}]
server.save_config()
before = on_disk()
check('a normal save writes valid JSON', json.loads(before)['snapshots'][0]['name'] == 'Golden hour portraits')
check('and leaves no scratch file beside it', scratch_files() == [], scratch_files())

# THE INTERRUPTION. Part-written, then killed -- a KeyboardInterrupt because that is what a real
# one looks like, and because it is NOT an Exception: an `except Exception` around the write would
# skip its own cleanup here and leave litter next to start.bat.
_real_dump = json.dump


def _exploding_dump(obj, fp, **kw):
    fp.write('{\n  "roots": [\n    {\n      "key": "ab')      # a plausible truncation point
    raise KeyboardInterrupt('killed mid-write')


json.dump = _exploding_dump
server.CONFIG['snapshots'] = [{'name': 'THIS SHOULD NEVER LAND', 'filters': {}}]
try:
    server.save_config()
    check('the interrupted save raises rather than returning', False, 'it returned normally')
except KeyboardInterrupt:
    check('the interrupted save raises rather than returning', True)
finally:
    json.dump = _real_dump

after = on_disk()
check('config.json is byte-for-byte what it was', after == before)
check('it still parses', json.loads(after)['roots'][0]['path'] == r'X:\pictures')
check('the snapshot is still there', json.loads(after)['snapshots'][0]['name'] == 'Golden hour portraits')
check('the half-written attempt landed nowhere', 'THIS SHOULD NEVER LAND' not in after)
check('no scratch file survives the interruption', scratch_files() == [], scratch_files())

# The point of all of it: the app reads the real thing back, not DEFAULT_CONFIG.
cfg = server.load_config()
check('load_config returns the real config, not defaults',
      len(cfg['roots']) == 1 and len(cfg['snapshots']) == 1, cfg.get('roots'))

# A FILE SOMETHING ELSE HAS OPEN. On Windows a plain open() does not share delete, so while it is
# held os.replace onto that name is refused with WinError 5 -- what Defender or the indexer does to
# the file the previous save just wrote. The author hit it 2026-09-23 switching an extension on and off.
# Held for 300ms from another thread, as a scanner would; the save has to wait it out, not fail.
import threading  # noqa: E402
import time  # noqa: E402
if os.name == 'nt':
    held = open(os.environ['CV_CONFIG'], 'r', encoding='utf-8')
    threading.Thread(target=lambda: (time.sleep(0.3), held.close()), daemon=True).start()
    server.CONFIG['seen_help_hint'] = True
    try:
        server.save_config()
        ok, err = True, ''
    except OSError as e:
        ok, err = False, e
    check('a save waits out a file held open elsewhere, instead of failing', ok, err)
    check('...and what it saved is there', json.loads(on_disk()).get('seen_help_hint') is True)
    check('...and leaves no scratch file', scratch_files() == [], scratch_files())

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
