"""test_ext_control_line.py -- a worker line that reports PROGRESS rather than a result.

Run: python tests/test_ext_control_line.py

The shared runner used to know exactly two kinds of line: `ready`, and a result. That was enough
while every extension produced one thing per file. It stops being enough for a kind that is one
unit over a whole selection -- publishing five files as one post -- because such a worker has two
things to say that are not results:

  * "uploading 3 of 5", so a 200MB video does not leave the bar frozen for two minutes with no
    sign of life; and
  * one final summary, the receipt for the thing that now exists elsewhere.

Counted as results, those lines push `seen` past `total` and turn a finished job into one that
looks part-failed. So `{"control": true}` moves no counter. It is deliberately kind-agnostic: this
loop is shared *because* it is the contract, and teaching it one kind's vocabulary is the thing
that would make the next kind copy it instead.

This file drives run_ext_batch against a generated worker, so it pins the loop itself rather than
any one extension.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix='vv_extctl_')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


# A scratch extensions/ and a scratch database, so this says the same thing on any machine.
EXT = os.path.join(TMP, 'extensions')
os.makedirs(EXT)
server.EXT_DIR = EXT
DB = os.path.join(TMP, 'lib.db')
sqlite3.connect(DB).close()
server.ACTIVE['db'] = DB
server.CONFIG['extensions'] = {'poster': True}   # every extension ships off

# The worker emits, in order: ready, a result, a control line, a result, then a final control line
# carrying a receipt. Two results and three non-results -- if control lines counted, `seen` would
# read 5 against a total of 2.
WORKER = r'''
import json, sys
print(json.dumps({"ready": True, "device": "test"}), flush=True)
print(json.dumps({"control": True, "phase": "uploading 1 of 2"}), flush=True)
print(json.dumps({"id": 1, "uploaded": True}), flush=True)
print(json.dumps({"control": True, "phase": "uploading 2 of 2"}), flush=True)
print(json.dumps({"id": 2, "uploaded": True}), flush=True)
print(json.dumps({"control": True, "post": {"id": 987, "url": "https://example.invalid/posts/987"}}), flush=True)
'''

d = os.path.join(EXT, 'poster')
os.makedirs(d)
with io.open(os.path.join(d, 'extension.json'), 'w', encoding='utf-8') as f:
    f.write(json.dumps({'name': 'Poster', 'produces': 'post', 'worker': 'worker.py',
                        'compose': [{'key': 'title', 'type': 'text'}],
                        'settings': [{'key': 'api_key', 'type': 'password'}]}))
with io.open(os.path.join(d, 'worker.py'), 'w', encoding='utf-8') as f:
    f.write(WORKER)

state = {'running': False}
lock = threading.Lock()
seen_control = []


def todo_for(conn, ids):
    return [{'id': i, 'path': 'x'} for i in ids]


def store(conn, msg):
    if msg.get('control'):
        seen_control.append(msg)
        return False          # a control line stores nothing AND must not count as a failure
    return bool(msg.get('uploaded'))


print('\nA control line moves no counter\n')

started = server.run_ext_batch('poster', [1, 2], state, lock, todo_for, store, 'Poster',
                               compose={'title': 'A castle'})
check('the run started', started is True, started)
for _ in range(200):                       # a generated worker finishes in well under a second
    if state.get('done'):
        break
    time.sleep(0.05)

check('it finished', state.get('done') is True, state)
check('no error', not state.get('error'), state.get('error'))
check('total counts the files, not the lines', state.get('total') == 2, state.get('total'))
check('seen counts the RESULTS only', state.get('seen') == 2, state.get('seen'))
check('both results counted ok', state.get('ok') == 2, state.get('ok'))
check('a control line is not a failure', state.get('failed') == 0, state.get('failed'))
check('ready is still handled separately', state.get('device') == 'test', state.get('device'))
check('every control line reached the store callback', len(seen_control) == 3, seen_control)
check('including the final receipt',
      (seen_control[-1].get('post') or {}).get('id') == 987, seen_control[-1])

# --- and the typed values travel with the saved ones ---------------------------------------------
# One merged dict, one tempfile. The worker above ignores argv[2]; this run proves the merge does
# not break the launch, and test_civitai_push pins what the worker actually reads out of it.
ext = server.get_extension('poster')
values = server.ext_settings_for(ext)
values.update({'title': 'A castle'})
check('compose values merge over the saved settings without colliding',
      values.get('title') == 'A castle' and 'api_key' in values, values)

print()
sys.exit(1 if _fails else 0)
