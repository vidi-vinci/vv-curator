"""test_civitai_push.py -- the publishing worker, against a stub standing in for Civitai.

Run: python tests/test_civitai_push.py

The real proof that publishing works was a standalone harness run against the live site. What a
test can add is the things that would break quietly and that nobody would notice until a post came
out wrong -- or until a key leaked.

Pinned here:

  * THE KEY IS ON THE API CALLS AND ABSENT FROM THE PRESIGNED PUT. The upload URL belongs to a
    third party and its signature IS the credential; attaching ours would hand our key to them.
    This is the one failure in the file with a consequence outside the app.
  * NOTHING IS POSTED IF ANY UPLOAD FAILS. A draft that looks complete and is not is the failure
    people write bug reports about.
  * THE CREATE CALL IS NEVER RETRIED, because it is not idempotent -- a retry after a lost answer
    is how you get two drafts. The PUT, which is idempotent, IS retried once.
  * The model is credited from the hash ALREADY IN THE FILE, and the id is pinned only to files
    that carry no metadata of their own -- so a still keeps its own richer resources while the
    video beside it gets the checkpoint.
  * The prompt never reaches the description.

The worker is spawned as a subprocess, exactly as the app spawns it, so argv and the JSON-line
protocol are covered rather than assumed.
"""
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(BASE, 'extensions', 'civitai', 'worker.py')
TMP = tempfile.mkdtemp(prefix='vv_civpush_')

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


# ---- a PNG with a real parameters block, built here so the test owns its fixture ----------------

def png(w, h, parameters=None):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + \
            struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    if parameters:
        data += chunk(b'tEXt', b'parameters\x00' + parameters.encode('latin-1'))
    return data + chunk(b'IDAT', zlib.compress(b'\x00' * ((w * 3 + 1) * h))) + chunk(b'IEND', b'')


PROMPT = 'a castle on a hill, long and busy prompt text that must never become a description'
PARAMS = PROMPT + '\nSteps: 10, Sampler: er_sde, Seed: 1, Model hash: e889202c41'

STILL = os.path.join(TMP, 'run_First_00001_.png')
VIDEO = os.path.join(TMP, 'run_00001_.mp4')
open(STILL, 'wb').write(png(864, 480, PARAMS))
open(VIDEO, 'wb').write(b'\x00' * 2048)


# ---- the stub ------------------------------------------------------------------------------------

LOG = []            # (method, path, has_auth, body)
FAIL_PUT_TIMES = [0]
FAIL_CREATE = [False]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _send(self, code, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        LOG.append(('GET', self.path, bool(self.headers.get('Authorization')), b''))
        if self.path.startswith('/api/v1/model-versions/by-hash/'):
            return self._send(200, {'id': 3193337, 'name': 'FL2VA INT8 Pruned',
                                    'model': {'name': 'Minimax H3'}})
        if self.path == '/api/v1/me':
            # The REAL shape, read off the live API 2026-09-21: tokenScope is an opaque NUMBER, not
            # a list of grants. The stub said otherwise and that is how the self-test came to warn
            # that a perfectly good key "may not be allowed to post".
            return self._send(200, {'id': 11492205, 'username': 'VidiVinci', 'status': 'active',
                                    'tokenScope': 11492205, 'isMember': False})
        self._send(404, {})

    def do_POST(self):
        body = self._read()
        LOG.append(('POST', self.path, bool(self.headers.get('Authorization')), body))
        if self.path == '/api/v1/image-upload':
            n = sum(1 for m, p, _a, _b in LOG if p == '/api/v1/image-upload')
            return self._send(200, {'id': 'uuid-%d' % n,
                                    'uploadURL': 'http://127.0.0.1:%d/put/%d' % (PORT, n)})
        if self.path == '/api/trpc/post.createWithImages':
            if FAIL_CREATE[0]:
                return self._send(500, {'error': 'boom'})
            return self._send(200, {'result': {'data': {'json': {'id': 987654,
                                                                 'imageIds': [1, 2]}}}})
        self._send(404, {})

    def do_PUT(self):
        body = self._read()
        LOG.append(('PUT', self.path, bool(self.headers.get('Authorization')), body))
        if FAIL_PUT_TIMES[0] > 0:
            FAIL_PUT_TIMES[0] -= 1
            self.send_response(503)       # an HTTP error: must NOT be retried
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()


srv = HTTPServer(('127.0.0.1', 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def run(items, settings, extra=()):
    ip = os.path.join(TMP, 'items.json')
    sp = os.path.join(TMP, 'set.json')
    with io.open(ip, 'w', encoding='utf-8') as f:
        json.dump(items, f)
    with io.open(sp, 'w', encoding='utf-8') as f:
        json.dump(settings, f)
    env = dict(os.environ)
    p = subprocess.run([sys.executable, WORKER, ip, sp] + list(extra),
                       capture_output=True, text=True, env=env, timeout=120)
    lines = []
    for ln in (p.stdout or '').splitlines():
        ln = ln.strip()
        if ln:
            try:
                lines.append(json.loads(ln))
            except ValueError:
                pass
    return p, lines


# The worker talks to civitai.com; point it at the stub by rewriting its one constant in a copy.
src = io.open(WORKER, encoding='utf-8').read()
WORKER = os.path.join(TMP, 'worker_under_test.py')
io.open(WORKER, 'w', encoding='utf-8').write(
    src.replace("API = 'https://civitai.com'", "API = 'http://127.0.0.1:%d'" % PORT))

ITEMS = [{'id': 11, 'path': VIDEO}, {'id': 22, 'path': STILL}]
SETTINGS = {'api_key': 'sk-secret-key', 'title': 'Castle flyover', 'detail': '', 'tags': 'castle, fog'}

print('\nPublishing a video and its still as one draft\n')

# ---- the happy path ------------------------------------------------------------------------------
LOG[:] = []
p, lines = run(ITEMS, SETTINGS)
check('the worker exits cleanly', p.returncode == 0, p.stderr[-400:])

post = next((m['post'] for m in lines if m.get('control') and m.get('post')), None)
check('it reports the post', bool(post) and post.get('id') == 987654, post)
check('and calls it a draft', bool(post) and post.get('draft') is True, post)
# THE EDITOR, not the public page. A draft is not public, the remaining work happens there, and an
# R+ image is invisible on the public page unless you are on the right domain -- which is how the
# first version sent the author to a post he could not see.
check('the link goes to the EDITOR',
      (post or {}).get('url', '').endswith('/posts/987654/edit'), (post or {}).get('url'))
check('on civitai.com by default',
      (post or {}).get('url', '').startswith('https://civitai.com/'), (post or {}).get('url'))

LOG[:] = []
_p, red_lines = run(ITEMS, dict(SETTINGS, site='civitai.red'))
red = next((m['post'] for m in red_lines if m.get('control') and m.get('post')), None)
check('the domain is configurable, for someone whose posts live on the other one',
      (red or {}).get('url') == 'https://civitai.red/posts/987654/edit', (red or {}).get('url'))
check('but POSTING still went to the stub, not to the configured domain -- civitai.red is '
      'read-only and cannot create a post',
      any(pth == '/api/trpc/post.createWithImages' for _m, pth, _a, _b in LOG),
      [pth for _m, pth, _a, _b in LOG])
check('both files are reported uploaded',
      sorted(m['id'] for m in lines if m.get('uploaded')) == [11, 22],
      [m for m in lines if m.get('uploaded')])
check('progress is reported as control lines, which move no counter',
      any(m.get('control') and m.get('phase') for m in lines), lines)

create = next(b for m, pth, _a, b in LOG if pth == '/api/trpc/post.createWithImages')
payload = json.loads(create)['json']
check('the create call is wrapped in {"json": ...}', 'images' in payload, sorted(payload))
check('it posts a DRAFT', payload.get('publish') is False, payload.get('publish'))
check('the files keep their selection order',
      [i['index'] for i in payload['images']] == [0, 1], payload['images'])
check('the video is typed video and the still image',
      [i['type'] for i in payload['images']] == ['video', 'image'], payload['images'])

# ---- the key ------------------------------------------------------------------------------------
puts = [(a, b) for m, _p, a, b in LOG if m == 'PUT']
check('the bytes actually reached the upload URL', len(puts) == 2, len(puts))
check('THE KEY IS NEVER SENT TO THE PRESIGNED URL', not any(a for a, _b in puts), puts)
check('but it is on the API calls',
      all(a for m, pth, a, _b in LOG if pth == '/api/v1/image-upload'))
check('and the key is nowhere in the worker output', 'sk-secret-key' not in (p.stdout or ''))

# ---- the model, credited from the file ------------------------------------------------------------
check('the hash in the still is looked up',
      any(pth.endswith('/by-hash/e889202c41') for _m, pth, _a, _b in LOG),
      [pth for _m, pth, _a, _b in LOG])
check('the lookup needs no key',
      not any(a for _m, pth, a, _b in LOG if '/by-hash/' in pth))
by_type = {i['type']: i for i in payload['images']}
check('the VIDEO carries the resource id, having no metadata of its own',
      by_type['video'].get('modelVersionId') == 3193337, by_type['video'])
check('the STILL does not, so its own richer resources survive',
      'modelVersionId' not in by_type['image'], by_type['image'])
check('the still reports its real dimensions',
      (by_type['image'].get('width'), by_type['image'].get('height')) == (864, 480), by_type['image'])

# ---- the description ------------------------------------------------------------------------------
check('THE PROMPT NEVER BECOMES THE DESCRIPTION', PROMPT not in json.dumps(payload), payload.get('detail'))
check('the title stands in when no description was typed',
      payload.get('detail') == 'Castle flyover', payload.get('detail'))
check('tags are split on commas', payload.get('tags') == ['castle', 'fog'], payload.get('tags'))

LOG[:] = []
run(ITEMS, dict(SETTINGS, detail='Mine, typed by hand'))
typed = json.loads(next(b for _m, pth, _a, b in LOG
                        if pth == '/api/trpc/post.createWithImages'))['json']
check('a typed description wins over the title',
      typed.get('detail') == 'Mine, typed by hand', typed.get('detail'))

# ---- a failed upload abandons the post ------------------------------------------------------------
LOG[:] = []
FAIL_PUT_TIMES[0] = 99
p, lines = run(ITEMS, SETTINGS)
check('a failed upload is a non-zero exit', p.returncode != 0, p.returncode)
check('NOTHING IS POSTED when an upload fails',
      not any(pth == '/api/trpc/post.createWithImages' for _m, pth, _a, _b in LOG),
      [pth for _m, pth, _a, _b in LOG])
fatal = next((m['fatal'] for m in lines if m.get('fatal')), '')
check('and it says nothing was posted', 'nothing was posted' in fatal.lower(), fatal)
check('an HTTP error on the PUT is NOT retried',
      sum(1 for m, _p, _a, _b in LOG if m == 'PUT') == 1,
      sum(1 for m, _p, _a, _b in LOG if m == 'PUT'))
FAIL_PUT_TIMES[0] = 0

# ---- a lost answer from the create call is never retried -------------------------------------------
LOG[:] = []
FAIL_CREATE[0] = True
p, lines = run(ITEMS, SETTINGS)
creates = sum(1 for _m, pth, _a, _b in LOG if pth == '/api/trpc/post.createWithImages')
check('THE CREATE CALL IS TRIED EXACTLY ONCE, never retried', creates == 1, creates)
fatal = next((m['fatal'] for m in lines if m.get('fatal')), '')
check('and it says the post may exist', 'may or may not' in fatal, fatal)
check('and points at the drafts', 'drafts' in fatal.lower(), fatal)
FAIL_CREATE[0] = False

# ---- no key --------------------------------------------------------------------------------------
LOG[:] = []
p, lines = run(ITEMS, {'title': 'x'})
check('no key is refused before anything is sent', not LOG, LOG)
check('and says where to put one',
      'Settings' in next((m['fatal'] for m in lines if m.get('fatal')), ''), lines)

# ---- the self-test reports the SCOPE ----------------------------------------------------------------
LOG[:] = []
p, lines = run([], SETTINGS, extra=['--test'])
check('the self-test answers ok', lines and lines[0].get('ok') is True, lines)
check('it names the account, which is what it can actually prove',
      'VidiVinci' in (lines[0].get('detail') or ''), lines)
# The regression that produced a false warning on a working key: tokenScope is a NUMBER, so any
# attempt to read permissions out of it finds nothing and concludes the worst.
check('it does NOT claim to know whether the key may post',
      not lines[0].get('warn'), lines[0])
check('and says so plainly rather than implying it checked',
      'posting' in (lines[0].get('detail') or ''), lines[0])
check('the self-test posts nothing',
      not any(m == 'POST' for m, _p, _a, _b in LOG), LOG)

srv.shutdown()
print()
sys.exit(1 if _fails else 0)
