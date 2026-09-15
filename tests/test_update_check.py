"""Telling people a newer version exists, and refusing to say it when it doesn't.

Run: python tests/test_update_check.py

The feature: one HTTPS GET to GitHub's releases API at startup, and an amber arrow beside the logo
when the answer is yes. It checks and tells -- it never downloads and never installs, because this
app updates by copying a folder and a program that overwrites itself while running is a class of
problem worth not having.

What this pins, and why each one can rot quietly:

  1. **'1.10' is newer than '1.9'.** A string compare says the opposite, and it says it correctly
     for the first nine releases -- so this goes wrong in front of users, long after anyone is
     still testing the comparison.
  2. **A prerelease tag is not newer than its own release.** '1.0-beta' parsed into (1, 0, 0), which
     beats (1, 0), so the beta announced itself as newer than the thing it precedes. The fix is to
     truncate at the first non-numeric piece rather than split on it, and this is the case that
     proves it stayed truncated.
  3. **An unparseable tag never looks newer.** That is the one direction this may not fail in: a
     tag nobody can read would otherwise pop a notice on every launch forever, with nothing the
     user could do to satisfy it.
  4. **Draft and prerelease releases are ignored**, because announcing something a reader cannot
     sensibly install is worse than saying nothing.
  5. **Every network failure is a quiet no.** Offline, rate-limited, a 404 from a repo that does not
     exist -- none of these are an error worth showing, because the user did not ask a question.
  6. **A blank UPDATE_REPO makes NO REQUEST AT ALL.** This is what lets the feature ship before the
     repo exists, and "no request" rather than "a request that fails" is the difference.
  7. **The setting off makes no request either.** Off has to mean off, not off-as-far-as-you-can-see
     -- hiding the icon while still asking GitHub every day is the shape this design rejected.
  8. **update_check survives an unrelated Settings save.** api_save_settings rebuilds the `general`
     dict from a whitelist, so a key missing from that list does not merely lose its default: it
     disappears from config.json the next time anything else is saved.
  9. **The first-run pop-up carries the ask**, and the client only checks once it has been answered.

Runs in-process on an ephemeral port against a temp data dir. Never touches the network: GitHub is
stubbed at urllib.request.urlopen, which is the seam the real code actually calls.
"""
import io
import json
import os
import sys
import tempfile
import threading
import http.client
import urllib.error

TMP = tempfile.mkdtemp(prefix='vv-update-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402
import urllib.request  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def read(path):
    with io.open(os.path.join(ROOT, path), encoding='utf-8') as f:
        return f.read()


# ---- GitHub, stubbed at the seam the real code calls -------------------------------------------
# urlopen rather than a fake HTTP server: the point is to exercise check_for_update's own request
# and its own error handling, and a friendlier stand-in one layer up would test a program that does
# not exist. `calls` is what proves the "no request at all" cases.
calls = []
_reply = {}


class FakeResponse(object):
    def __init__(self, payload):
        self._b = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    calls.append(getattr(req, 'full_url', req))
    if isinstance(_reply, Exception):
        raise _reply
    return FakeResponse(_reply)


urllib.request.urlopen = fake_urlopen


def ask(payload, repo='someone/vv-curator', current='1.0'):
    """Run one full check against a given GitHub answer. Clears the per-process cache first."""
    global _reply
    _reply = payload
    server.UPDATE_REPO = repo
    server.APP_VERSION = current
    server._update_seen = None
    del calls[:]
    return server.check_for_update()


print('\nversion comparison')
v = server._ver_tuple
check("'1.10' is newer than '1.9'", v('1.10') > v('1.9'), (v('1.10'), v('1.9')))
check("'1.9' is NOT newer than '1.10'", not (v('1.9') > v('1.10')))
check("a 'v' prefix is ignored", v('v1.2') == v('1.2') == (1, 2))
check("'1.0-beta' is not newer than '1.0'", not (v('1.0-beta') > v('1.0')), v('1.0-beta'))
check("'1.2.3-rc1' is not newer than '1.2.3'", not (v('1.2.3-rc1') > v('1.2.3')))
check('an unparseable tag is never newer', not (v('junk') > v('1.0')), v('junk'))
check('an empty tag is never newer', not (v('') > v('1.0')))
check('a tag that only contains a number later is never newer',
      not (v('release-1.5') > v('1.0')), v('release-1.5'))

print('\nwhat counts as an update')
r = ask({'tag_name': 'v1.1', 'name': 'Sets get faster', 'body': '- One\n- Two',
         'html_url': 'https://example.invalid/r/1.1'})
check('a newer tag is available', r.get('available') is True, r)
check('the version drops its v', r.get('version') == '1.1', r)
check('the release name comes through', r.get('name') == 'Sets get faster', r)
check('the notes come through', r.get('notes') == '- One\n- Two', r)
check('the link comes through', r.get('url') == 'https://example.invalid/r/1.1', r)
check('one request was made', len(calls) == 1, calls)
check('the request went to the releases API for that repo',
      calls and calls[0] == 'https://api.github.com/repos/someone/vv-curator/releases/latest', calls)

check('the SAME version is not an update',
      ask({'tag_name': '1.0'}).get('available') is False)
check('an OLDER version is not an update',
      ask({'tag_name': '0.9'}).get('available') is False)
check('a draft is not an update',
      ask({'tag_name': '2.0', 'draft': True}).get('available') is False)
check('a prerelease is not an update',
      ask({'tag_name': '2.0', 'prerelease': True}).get('available') is False)
check('a release with no tag is not an update',
      ask({'name': 'nameless'}).get('available') is False)
check('a response that is not an object is not an update',
      ask(['not', 'a', 'dict']).get('available') is False)
check('a release with no notes still reports available',
      ask({'tag_name': '1.1'}).get('available') is True)

print('\nfailure is always a quiet no')
for label, exc in [('offline', urllib.error.URLError('no route')),
                   ('a 404 from a repo that does not exist',
                    urllib.error.HTTPError('u', 404, 'Not Found', {}, None)),
                   ('rate limited', urllib.error.HTTPError('u', 403, 'rate limit', {}, None)),
                   ('a timeout', TimeoutError('timed out'))]:
    r = ask(exc)
    check('%s -> not available, no exception' % label, r == {'available': False}, r)

# A body that parses but is not an object. Genuinely malformed JSON raises out of json.loads and
# is covered by the exception cases above -- this is the other half: valid JSON of the wrong shape.
r = ask('a bare string, not an object')
check('a body that parses to the wrong shape -> not available', r.get('available') is False, r)

print('\nnot asking at all')
r = ask({'tag_name': '9.9'}, repo='')
check('a blank UPDATE_REPO reports not available', r.get('available') is False, r)
check('a blank UPDATE_REPO makes NO request', calls == [], calls)

print('\nthe cache')
server.UPDATE_REPO = 'someone/vv-curator'
server.APP_VERSION = '1.0'
server._update_seen = None
_reply = {'tag_name': '1.1'}
del calls[:]
a, b = server.update_status(), server.update_status()
check('two calls to update_status make ONE request', len(calls) == 1, calls)
check('and both get the same answer', a == b and a.get('available') is True, (a, b))

# ---- over a real socket, the way the client asks -----------------------------------------------
httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def get(path):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    c.request('GET', path)
    body = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return body


def post(path, payload):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    c.request('POST', path, json.dumps(payload).encode('utf-8'),
              {'Content-Type': 'application/json'})
    body = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return body


print('\nthe endpoint, and the setting')
server._update_seen = None
_reply = {'tag_name': '1.1', 'html_url': 'https://example.invalid/r'}
server.CONFIG['general']['update_check'] = True
del calls[:]
j = get('/api/update')
check('/api/update reports an available update', j.get('available') is True, j)

server._update_seen = None
server.CONFIG['general']['update_check'] = False
del calls[:]
j = get('/api/update')
check('with the setting off it reports not available', j.get('available') is False, j)
check('with the setting off it makes NO request', calls == [], calls)

print('\nthe setting survives the whitelist')
server.CONFIG['general']['update_check'] = False
r = post('/api/settings', {'general': {'autoplay': True}})
check('saving an UNRELATED setting keeps update_check off',
      r.get('general', {}).get('update_check') is False, r.get('general'))
check('and the unrelated setting took', r.get('general', {}).get('autoplay') is True)
r = post('/api/settings', {'general': {'update_check': True}})
check('the first-run pop-up can post update_check ALONE',
      r.get('general', {}).get('update_check') is True, r.get('general'))
check('and posting it alone does not flatten its neighbours',
      r.get('general', {}).get('autoplay') is True, r.get('general'))

cfg = json.loads(io.open(os.environ['CV_CONFIG'], encoding='utf-8').read())
check('it reaches config.json', cfg.get('general', {}).get('update_check') is True,
      cfg.get('general'))

print('\nwhat the interface promises')
html = read('app/index.html')
css = read('app/style.css')
js = read('app/app.js')
check('the first-run pop-up carries the ask', 'helpHintUpdates' in html)
check('the ask is pre-ticked', 'id="helpHintUpdates" checked' in html)
check('Settings has the same switch', 'setUpdateCheck' in html and "update_check: 'setUpdateCheck'" in js)
check('the mark starts hidden', 'id="btnUpdate"' in html and 'is-update hidden' in html)
check('the mark is amber, and keeps its colour on hover',
      '#sidebar .brand #btnUpdate, #sidebar .brand #btnUpdate:hover { color: var(--modified); }' in css)
# There is no global .hidden in this app -- every component declares its own, and a component that
# forgets is simply never hidden. Both of this feature's are checked because the failure is silent.
check('#btnUpdate.hidden is scoped', '#sidebar .brand #btnUpdate.hidden { display: none; }' in css)
check('.update-notes.hidden is scoped', '.update-notes.hidden { display: none; }' in css)
# The auto margin has to MOVE to the arrow, not be shared: flexbox splits free space equally
# between two auto margins, which would park the arrow mid-rail with a gap on each side.
check('the arrow takes the auto margin when shown',
      '#sidebar .brand #btnUpdate:not(.hidden) { margin-left:auto; }' in css)
check('and Help gives it up', '#sidebar .brand #btnUpdate:not(.hidden) + #btnHelp { margin-left:0; }' in css)
check('the client waits to be asked before checking', 'if (cfg.seen_help_hint) checkForUpdate();' in js)
check('answering yes checks without waiting for a restart',
      'if (box.checked) checkForUpdate();' in js)
check('the notes render through the escaping markdown renderer', 'mdToHtml(j.notes)' in js)
check('the release page opens with noopener', "'_blank', 'noopener'" in js)
check('it never downloads or installs',
      'download' not in js[js.index('async function checkForUpdate'):js.index('function closeUpdateBox')].lower())

print('\n%s\n' % ('all checks passed' if not failures else
                  '%d FAILED: %s' % (len(failures), ', '.join(failures))))
httpd.shutdown()
sys.exit(1 if failures else 0)
