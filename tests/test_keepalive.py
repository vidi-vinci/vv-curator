"""Regression test for HTTP keep-alive (protocol_version = HTTP/1.1) in server.py.

Run: python test_keepalive.py

Why this test exists. Switching to HTTP/1.1 is a one-line change with a disproportionate blast
radius: on HTTP/1.0 every response ended by closing the connection, so a body that didn't match its
Content-Length was harmless. On a REUSED connection the shortfall is read as the beginning of the
NEXT response, so one aborted video scrub could corrupt whatever the browser asked for afterwards.

So the assertions here are not "does keep-alive work" — they are "can a connection ever be left in a
state where the next response on it is wrong". The three ways in are a 416 with no body framing, a
range read that ends early, and a client that hangs up mid-download.

Runs entirely in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import json
import os
import sys
import shutil
import socket
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-keepalive-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402  (must follow the env vars above)
from http.server import ThreadingHTTPServer  # noqa: E402

# A file for the range paths to serve. Distinctive bytes, so a desynchronised connection shows up
# as wrong CONTENT rather than merely a wrong length.
MEDIA = os.path.join(TMP, 'clip.bin')
BLOB = bytes(range(256)) * 400          # 102,400 bytes
with open(MEDIA, 'wb') as f:
    f.write(BLOB)

# The routing needs a library row to resolve an id; the test supplies one instead of building a DB.
server.Handler._path_for = lambda self, iid: {'path': MEDIA, 'thumb': None}

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


print('\nHTTP keep-alive\n')

# 1. The switch is actually in effect, and the server is not asking to close after each response.
c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
c.request('GET', '/api/config')
r = c.getresponse()
body = r.read()
check('speaks HTTP/1.1', r.version == 11, 'version=%s' % r.version)
check('  does not close after a response', (r.getheader('Connection') or '').lower() != 'close',
      r.getheader('Connection'))
check('  and frames the body', r.getheader('Content-Length') == str(len(body)))

# 2. THE POINT OF THE CHANGE: many requests, one connection. Also the broadest desync detector —
#    if any response is mis-framed, a later one returns the wrong bytes or the read hangs.
ok, first = True, None
for i in range(12):
    c.request('GET', '/api/config')
    r = c.getresponse()
    b = r.read()
    if first is None:
        first = b
    if r.status != 200 or b != first:
        ok = False
        break
check('12 requests reuse one connection intact', ok)

sock_id_before = id(c.sock)
c.request('GET', '/api/config')
c.getresponse().read()
check('  and it is genuinely the same socket', id(c.sock) == sock_id_before)

# 3. A whole file, then a partial one, on that same connection.
c.request('GET', '/file/1')
r = c.getresponse()
b = r.read()
check('a full media response is complete', b == BLOB, 'got %d of %d bytes' % (len(b), len(BLOB)))

c.request('GET', '/file/1', headers={'Range': 'bytes=10-19'})
r = c.getresponse()
b = r.read()
check('a range request returns exactly its range', r.status == 206 and b == BLOB[10:20],
      'status=%s len=%d' % (r.status, len(b)))
check('  with a correct Content-Range',
      r.getheader('Content-Range') == 'bytes 10-19/%d' % len(BLOB), r.getheader('Content-Range'))

# 4. THE 416. Unsatisfiable range: it carries no body, and before this change it also carried no
#    Content-Length — which on a reused connection leaves the client waiting for a body forever.
c.request('GET', '/file/1', headers={'Range': 'bytes=999999-'})
r = c.getresponse()
b = r.read()
check('an unsatisfiable range is framed', r.status == 416 and r.getheader('Content-Length') == '0',
      'status=%s cl=%s' % (r.status, r.getheader('Content-Length')))
check('  with an empty body', b == b'')

c.request('GET', '/api/config')
r = c.getresponse()
check('  and the connection still works after it', r.status == 200 and r.read() == first)
c.close()

# 5. A client that hangs up mid-download — what scrubbing a video does on purpose, constantly.
#    The server must not die, and must not try to reuse a connection it only half-answered.
raw = socket.create_connection(('127.0.0.1', PORT), timeout=10)
raw.sendall(b'GET /file/1 HTTP/1.1\r\nHost: localhost\r\n\r\n')
raw.recv(64)                                   # take the head and a sliver of the body...
raw.close()                                    # ...then vanish

c2 = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
c2.request('GET', '/file/1')
r = c2.getresponse()
b = r.read()
check('an aborted download does not poison the server', b == BLOB,
      'got %d of %d bytes' % (len(b), len(BLOB)))
c2.request('GET', '/api/config')
r = c2.getresponse()
check('  and a later request on a new connection is intact', r.status == 200 and r.read() == first)
c2.close()

# 6. Concurrent connections, since a browser opens ~6 and that is the whole reason for this change.
errs = []


def hammer():
    try:
        h = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
        for _ in range(6):
            h.request('GET', '/file/1', headers={'Range': 'bytes=0-999'})
            rr = h.getresponse()
            if rr.read() != BLOB[:1000]:
                errs.append('short body')
        h.close()
    except Exception as e:      # noqa: BLE001
        errs.append(repr(e))


ts = [threading.Thread(target=hammer) for _ in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
check('6 concurrent connections stay correct', not errs, errs[:3])

# 7. AN UNREAD REQUEST BODY, which is the same hazard from the other end. The first six cases are
#    all about a mis-framed RESPONSE; this is a request whose body the server never consumed. A
#    handled POST reads its body via _read_json(), but an unhandled one used to answer 404 and
#    leave the bytes in the socket — where the next request on that connection parsed them as its
#    own request line. Found for real while adding an endpoint, as:
#        POST /api/delete -> 501 Unsupported method ('{"batch":4}POST')
#    The 404 itself looked fine. It broke the request AFTER it, which is why this is worth pinning.
c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
c.request('POST', '/api/no-such-route', json.dumps({'batch': 4}),
          {'Content-Type': 'application/json'})
r = c.getresponse()
r.read()
check('a POST to an unknown route 404s', r.status == 404, r.status)
c.request('GET', '/api/config')                       # SAME connection
r = c.getresponse()
r.read()
check('  and does not poison the next request on that connection', r.status == 200,
      'got %s — the unread body was parsed as the next request line' % r.status)
c.close()

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
