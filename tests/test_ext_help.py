"""An extension's HELP.md reaches the Help window, and nothing else can be read the same way.

Run: python tests/test_ext_help.py

WHY THE SECOND HALF MATTERS MORE. /api/doc?name=ext:<id> serves a file from inside an extension's
folder. Joining the id onto a path as sent would make `ext:..` the folder above, and a doc viewer
into a file reader. The id is matched against the extensions actually found on disk instead, so
this sends the shapes an attacker would, over real HTTP, and expects nothing back.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix='vv_exthelp_')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')

import server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

EXT = os.path.join(TMP, 'extensions')
server.EXT_DIR = EXT


def make(ext_id, help_text=None):
    d = os.path.join(EXT, ext_id)
    os.makedirs(d)
    with open(os.path.join(d, 'extension.json'), 'w', encoding='utf-8') as f:
        json.dump({'name': ext_id.title(), 'produces': 'text', 'worker': 'w.py'}, f)
    open(os.path.join(d, 'w.py'), 'w').close()
    if help_text is not None:
        with open(os.path.join(d, 'HELP.md'), 'w', encoding='utf-8') as f:
            f.write(help_text)


make('helpful', '### Using it\n\nPress the button.\n')
make('quiet')
# A file one level above the extensions folder, which a traversal would reach.
with open(os.path.join(TMP, 'HELP.md'), 'w', encoding='utf-8') as f:
    f.write('SECRET ABOVE THE EXTENSIONS FOLDER')

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

failures = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label + ('' if ok else '  -> %r' % (got,)))
    if not ok:
        failures.append(label)


def doc(name):
    url = 'http://127.0.0.1:%d/api/doc?%s' % (PORT, urllib.parse.urlencode({'name': name}))
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, ''


payload = {e['id']: e for e in server._extensions_payload()}
check('an extension with a HELP.md says so', payload['helpful']['has_help'], True)
check('one without says not', payload['quiet']['has_help'], False)
check('the payload carries no path to it', 'HELP.md' in json.dumps(payload), False)

status, body = doc('ext:helpful')
check('its help is served', status, 200)
check('...as written', body.replace('\r\n', '\n'), '### Using it\n\nPress the button.\n')
check('one with no HELP.md is not found', doc('ext:quiet')[0], 404)
check('an extension that does not exist is not found', doc('ext:nobody')[0], 404)
for bad in ('ext:..', 'ext:../..', 'ext:..\\', 'ext:helpful/../..', 'ext:', 'ext:.'):
    status, body = doc(bad)
    check('%r reads nothing' % bad, (status, 'SECRET' in body), (404, False))
check("the app's own help is untouched", doc('help')[0], 200)

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
