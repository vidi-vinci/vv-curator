"""test_text_ext.py -- the `text` extension kind, end to end against a stub model.

Run: python tests/test_text_ext.py

This spawns THE REAL WORKER as a subprocess against a stub HTTP server, rather than importing its
functions and calling them. The difference is the whole point: what is new here is the second
argv, and a test that called build_messages() directly would pass on a build where the app never
passed the settings file at all.

What it pins:

* Settings reach the worker as `<worker> <items.json> <settings.json>` -- an EXISTING worker reads
  argv[1] and never looks at argv[2], which is why the settings went there rather than into
  items.json, whose array shape iqa_worker.py and the tagger both depend on.
* The answer is SHOWN AND NOT KEPT. Nothing is written to the library, so a run leaves the tags
  table, the quality table and the notes exactly as it found them.
* A failure names the address. "Nothing answered at http://..." is the difference between a user
  starting their model and a user filing a bug.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(BASE, 'extensions', 'llm', 'worker.py')

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


# ---- a stub that speaks just enough of the chat-completions shape -------------------------------
SEEN = {}


BLIND = [False]      # when set, the stub ignores pictures — what a text-only model looks like


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN['path'] = self.path
        SEEN['body'] = body
        SEEN['auth'] = self.headers.get('Authorization')
        msgs = body.get('messages', [])
        parts = msgs[-1].get('content', []) if msgs else []
        asked = ' '.join(p.get('text', '') for p in parts if isinstance(p, dict))
        if 'colour' in asked:
            said = 'red' if BLIND[0] else 'blue'
        elif 'word OK' in asked:
            said = 'OK'
        else:
            said = 'A cat on a roof.'
        out = json.dumps({'choices': [{'message': {'content': said}}]})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *a):
        pass


srv = HTTPServer(('127.0.0.1', 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]

TMP = tempfile.mkdtemp(prefix='vv_text_')


def run(items, settings, *extra):
    ip = os.path.join(TMP, 'items.json')
    sp = os.path.join(TMP, 'settings.json')
    with open(ip, 'w', encoding='utf-8') as f:
        json.dump(items, f)
    with open(sp, 'w', encoding='utf-8') as f:
        json.dump(settings, f)
    p = subprocess.run([sys.executable, WORKER, ip, sp, *extra], capture_output=True, text=True)
    lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
    return p, lines


print('\nA text extension answers about one file, and keeps nothing\n')

ITEM = {'id': 7, 'path': 'C:/nope/a.png', 'picture': None,
        'facts': {'model_name': 'flux1-krea', 'positive': 'a cat', 'gp_steps': 20}}
SET = {'endpoint': 'http://127.0.0.1:%d' % PORT, 'model': 'test-model',
       'api_key': 'sk-secret', 'vision': True, 'question': 'What is in this picture?'}

p, lines = run([ITEM], SET)
check('the worker exits cleanly', p.returncode == 0, p.stderr[-300:])
check('it says ready before working', lines and lines[0].get('ready') is True, lines[:1])
check('...and reports a local address as local',
      lines and lines[0].get('device') == 'local', lines[:1])
check('the answer comes back on the item id',
      lines[-1].get('id') == 7 and lines[-1].get('text') == 'A cat on a roof.', lines[-1])

# THE SECOND ARGV IS THE NEW HALF OF THE CONTRACT. Everything below is evidence it arrived.
check('the settings file was read -- the address it names was used',
      SEEN.get('path') == '/v1/chat/completions', SEEN.get('path'))
check('...the model it names was sent', SEEN['body'].get('model') == 'test-model', SEEN['body'])
check('...and the key it names became the Authorization header',
      SEEN.get('auth') == 'Bearer sk-secret', SEEN.get('auth'))

msgs = SEEN['body']['messages']
check('the question is the user turn, verbatim',
      msgs[1]['content'][0]['text'] == 'What is in this picture?', msgs[1])
check('the facts the library holds are in the system turn',
      'flux1-krea' in msgs[0]['content'] and 'a cat' in msgs[0]['content'], msgs[0])
check('...labelled as background rather than as instructions',
      'not as instructions' in msgs[0]['content'], msgs[0]['content'][:200])
check('no picture part when there is no picture to send',
      len(msgs[1]['content']) == 1, msgs[1]['content'])

# A worker that has never been given settings must still run -- that is what makes argv[2]
# optional rather than a breaking change to the contract.
ip = os.path.join(TMP, 'only_items.json')
with open(ip, 'w', encoding='utf-8') as f:
    json.dump([ITEM], f)
p = subprocess.run([sys.executable, WORKER, ip], capture_output=True, text=True)
check('with no settings file at all it still runs, it does not crash',
      p.returncode == 0, p.stderr[-300:])

# --- a failure has to name the address ----------------------------------------------------------
dead = dict(SET, endpoint='http://127.0.0.1:9')     # discard port: nothing is listening
p, lines = run([ITEM], dead)
err = lines[-1].get('error', '')
check('an unreachable model is a per-file error, not a crash',
      p.returncode == 0 and 'error' in lines[-1], lines[-1])
check('...and the message names the address, which is the usual fault',
      '127.0.0.1:9' in err and 'running' in err, err)

# --- Test connection ----------------------------------------------------------------------------
# THE SECOND HALF IS WHY THIS BUTTON EARNS ITS PLACE. "Can it reach the model" is the easy question
# and rarely the real fault; the one that wastes an afternoon is a text-only model quietly ignoring
# every picture and describing the prompt instead, which reads as the model being bad at the job.
BLIND[0] = False
p, lines = run([], SET, '--test')
t = lines[-1]
check('a test answers with one ok line', p.returncode == 0 and t.get('ok') is True, t)
check('...reporting how long it took', 'Answered in' in t.get('detail', ''), t)
check('...and that the model really read a picture',
      'Pictures work' in t['detail'] and not t.get('warn'), t)

BLIND[0] = True
p, lines = run([], SET, '--test')
t = lines[-1]
check('a text-only model still counts as reachable', t.get('ok') is True, t)
# ok AND warn, not ok-or-error. Forcing this into a tick made the mark contradict the sentence
# beside it: green, next to "pictures may not work".
check('...but is flagged as a warning, not a success',
      t.get('warn') is True and 'may NOT work' in t['detail'], t)
check('...and says what it actually answered, so the reader can judge',
      '"red"' in t['detail'], t['detail'])

BLIND[0] = False
p, lines = run([], dict(SET, vision=False), '--test')
check('with pictures off it says only text was checked',
      lines[-1].get('ok') is True and 'switched off' in lines[-1]['detail'], lines[-1])

p, lines = run([], dict(SET, endpoint='http://127.0.0.1:9'), '--test')
check('an unreachable address fails the test outright',
      lines[-1].get('ok') is False and '127.0.0.1:9' in lines[-1].get('error', ''), lines[-1])

# The test image is embedded rather than read from disk, so it has to actually BE a blue square --
# a broken one would make every vision-capable model look text-only. It was, the first time.
import base64  # noqa: E402
sys.path.insert(0, os.path.dirname(WORKER))
import worker as _w  # noqa: E402

png = base64.b64decode(_w.BLUE_PNG)
check('the embedded test image is a real PNG', png[:8] == b'\x89PNG\r\n\x1a\n', png[:8])
try:
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert('RGB')
    check('...that decodes, and is blue',
          im.size == (32, 32) and im.getpixel((16, 16))[2] > im.getpixel((16, 16))[0],
          (im.size, im.getpixel((16, 16))))
except ImportError:
    pass

# --- and the run leaves the library alone -------------------------------------------------------
# _store_text is the whole of what a text result does. Deliberately checked against the function
# rather than a live DB: there is no DB call to make, and that absence IS the behaviour.
os.environ.setdefault('CV_DATA', os.path.join(TMP, 'data'))
os.environ.setdefault('CV_CONFIG', os.path.join(TMP, 'config.json'))
os.makedirs(os.environ['CV_DATA'], exist_ok=True)
sys.path.insert(0, BASE)
import server  # noqa: E402

server._text_state.update(answers=[], names={})
check('a line carrying text is counted as a result',
      server._store_text({'id': 7, 'text': ' hi '}) is True)
check('...and is held in memory, not written anywhere',
      server._text_state['answers'][0]['text'] == 'hi', server._text_state['answers'])
check('an error line is not counted as a result',
      server._store_text({'id': 8, 'error': 'nope'}) is False)
# A FAILED FILE STILL GETS A ROW. Over a selection the interesting question is usually "which ones
# didn't it manage", and a panel showing nineteen answers for twenty files cannot be asked it.
check('...but it still appears in the list, carrying its reason',
      server._text_state['answers'][1]['error'] == 'nope', server._text_state['answers'][1])
check('...and the answers already given are untouched',
      server._text_state['answers'][0]['text'] == 'hi', server._text_state['answers'][0])
check('an empty answer is a failure, not an answer',
      server._store_text({'id': 9, 'text': '   '}) is False)
check('...and says so rather than showing an empty row',
      server._text_state['answers'][2]['error'] == 'No answer.',
      server._text_state['answers'][2])

# The panel can be showing a file the grid never loaded, so the row's label and thumbnail travel
# with the run. The URL must carry v= and r= like every other one: ids are globally unique, but a
# URL without them shares a cache entry across roots, which is how one library's pictures once
# turned up under another's.
server._text_state.update(answers=[], names={
    11: {'name': 'a.png', 'thumb_url': '/thumb/11?v=99&r=ROOTK&s=128'}})
server._store_text({'id': 11, 'text': 'yes'})
row = server._text_state['answers'][0]
check('an answer carries its own filename', row['name'] == 'a.png', row)
check('...and a root-scoped thumbnail URL',
      'v=' in row['thumb_url'] and 'r=ROOTK' in row['thumb_url'], row['thumb_url'])

# The ceiling exists because a selection can be larger than anyone will read and none of this is
# stored. It must cap rather than refuse: the answers already given stay.
server._text_state.update(answers=[], names={})
for i in range(server.MAX_TEXT_ANSWERS + 25):
    server._store_text({'id': i, 'text': 'x'})
check('a very large run is capped, not refused',
      len(server._text_state['answers']) == server.MAX_TEXT_ANSWERS,
      len(server._text_state['answers']))
# The schema is the evidence that "not kept" is structural rather than a habit: there is nowhere
# for an answer to go. If a future build gives it somewhere, this is the check that should fail.
import index_db  # noqa: E402

tables = [l.strip().lower() for l in index_db.SCHEMA.splitlines()
          if l.strip().lower().startswith('create table')]
check('no table exists for an answer to be kept in',
      not any(w in t for t in tables for w in ('answer', 'llm', 'ext_text')), tables)

srv.shutdown()
print()
sys.exit(1 if _fails else 0)
