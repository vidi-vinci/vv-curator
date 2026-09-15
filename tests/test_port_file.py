"""test_port_file.py -- the port comes from port.txt, and a bad one costs the default, not the app.

Run: python tests/test_port_file.py

WHY THIS FILE EXISTS. A port already in use stops the app starting, so it is the one setting that
cannot be changed from inside the app. The answer used to be "add a `port` line to config.json",
which was the only hand-edit this app ever asked anyone for -- and config.json also holds every
library, every snapshot and every setting, so one stray comma in it cost all of them at once.
`port.txt` holds a number and nothing else, so the worst a typo can do is cost the default port.

THE PROPERTY THIS PINS is that nothing in the resolution path can raise. A word, a blank file, a
number out of range, a file that cannot be decoded -- each falls through to the next source in
silence. That has to hold at STARTUP, where there is no app running to tell anyone anything: a
throw here is the app failing to start because of a typo in the file you were told to type into.

ORDER MATTERS AND IS TESTED. port.txt outranks config.json, because a stale `port` left in a config
from before this file existed must not outrank the one the user just set. config.json is still read
below it, because those installs would otherwise silently move to 8770.
"""
import io
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


def check(name, got, want):
    ok = got == want
    print(('  ok    ' if ok else '  FAIL  ') + name + ('' if ok else '\n          got %r, wanted %r' % (got, want)))
    if not ok:
        failures.append(name)


def port_file(text):
    p = os.path.join(_DIR, 'port.txt')
    if text is None:
        if os.path.exists(p):
            os.remove(p)
    else:
        io.open(p, 'w', encoding='utf-8').write(text)


# --- nothing set anywhere ---------------------------------------------------------------------
server.CONFIG['port'] = None
port_file(None)
check('with nothing set, the default', server._chosen_port(), 8770)

# --- port.txt, the file a user is told to make --------------------------------------------------
port_file('9123')
check('port.txt is used', server._chosen_port(), 9123)
# Typed by hand in Notepad, which leaves a trailing newline, and people indent things.
port_file('  9123\r\n')
check('surrounding whitespace is ignored', server._chosen_port(), 9123)

# --- every way a hand-typed file goes wrong. NONE of these may raise ---------------------------
for label, text in (('a word', 'banana'), ('empty', ''), ('only whitespace', '   \n'),
                    ('out of range', '99999'), ('zero', '0'), ('negative', '-1'),
                    ('a decimal', '8770.5'), ('a sentence', 'port = 9123')):
    port_file(text)
    check('port.txt %s falls back to the default' % label, server._chosen_port(), 8770)

# Not valid UTF-8. Saved as ANSI/UTF-16 from an editor, which is a realistic way to produce this.
io.open(os.path.join(_DIR, 'port.txt'), 'wb').write(b'\xff\xfe9\x001\x002\x003\x00')
check('port.txt that will not decode falls back', server._chosen_port(), 8770)

# --- the legacy path: a port set in config.json before port.txt existed -------------------------
port_file(None)
server.CONFIG['port'] = 8123
check('a port in config.json is still honoured', server._chosen_port(), 8123)
port_file('9123')
check('port.txt outranks a stale config.json port', server._chosen_port(), 9123)
port_file('banana')
check('a broken port.txt falls through to config.json', server._chosen_port(), 8123)

# --- the environment wins, which is how the tests and dev copies move the app -------------------
os.environ['PORT'] = '7777'
check('PORT in the environment outranks both files', server._chosen_port(), 7777)
del os.environ['PORT']

# --- config.json gains no `port` key it did not already have ------------------------------------
# A fresh install must not grow a key nothing tells you to edit -- that is the whole point of
# moving the number out. An install that already has one keeps it, or the next save of anything
# would move their app to 8770 and break the bookmark they use.
port_file(None)
server.CONFIG['port'] = None
server.save_config()
import json  # noqa: E402
on_disk = json.load(io.open(os.environ['CV_CONFIG'], encoding='utf-8'))
check('a fresh config.json has no port key', 'port' in on_disk, False)

server.CONFIG['port'] = 8123
server.save_config()
on_disk = json.load(io.open(os.environ['CV_CONFIG'], encoding='utf-8'))
check('an existing port survives a save', on_disk.get('port'), 8123)

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
