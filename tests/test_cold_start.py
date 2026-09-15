"""The first ten minutes on a machine that has never run this.

Run: python tests/test_cold_start.py

Every fault pinned here shares one property: it can only happen to someone else. An established
install has Python, has its packages, owns its port and has its library already indexed, so nothing
below is reachable from a working copy -- which is exactly how all four survived this long.

What it pins:

  1. **The launcher refuses the Microsoft Store stub.** Windows ships a zero-byte `python.exe` under
     WindowsApps that only opens the Store, and it answers `where python`. It satisfied the old test,
     so the one machine the friendly "install Python" message exists for was the one machine that
     never saw it.
  2. **pip's exit code is read, and the imports are re-tested.** A failed one-time install used to
     launch anyway, into a window that could never connect, with the console closing behind it.
  3. **A server that dies is not followed by a browser window.** The port being taken by something
     else produced a connection error with no cause, while the traceback sat unread in
     data/viewer.log. The distinction that keeps this honest is HasExited, not "did the poll
     succeed" -- a first index can legitimately outlast the poll's cap, and that must still open.
  4. **A folder that does not answer is not "not found".** os.path.isdir on a sleeping share blocks
     for tens of seconds and then produced `Folder not found`, the same words as a typo, which sends
     people hunting a spelling mistake that is not there.

Items 1-3 are source assertions, because their failure is a missing check rather than wrong output
and there is no way to run a .bat's unhappy path without a machine that is broken in that specific
way. Item 4 and the port guard are real: the probe is called, and the server is started for real
against a port that is already held.
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import http.client

TMP = tempfile.mkdtemp(prefix='vv-cold-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402  (must follow the env vars above)
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


# ---- 1. the launcher ---------------------------------------------------------------------------
bat = read('start.bat')

print('start.bat')
check('the Store stub is filtered out of `where python`',
      'WindowsApps' in bat and 'find /i' in bat)
check('  and the interpreter is asked its version rather than assumed',
      'sys.version_info' in bat)

# The floor is stated in two files and they have disagreed before: start.bat said 3.10+ while
# README said 3.8+, and neither was ever checked against the interpreter the user actually has.
m_bat = re.search(r'sys\.version_info >= \((\d+),\s*(\d+)\)', bat)
m_doc = re.search(r'Python (\d+)\.(\d+)\+', read('README.md'))
check('  the version floor matches README',
      bool(m_bat) and bool(m_doc) and m_bat.groups() == m_doc.groups(),
      'start.bat %s vs README %s' % (m_bat and m_bat.groups(), m_doc and m_doc.groups()))

# `if errorlevel 2` means "2 OR MORE", so it swallowed the 9009 that cmd returns for a command it
# could not run -- and a machine with no working Python was told its Python was too old, under a
# version line printed by nothing. Caught by running the three codes, not by reading the line.
# Matched at the start of a line, as a command -- the file also explains the trap in a rem, and a
# naive substring search finds the warning and calls it the bug.
check('  "too old" is exactly 2, not "2 or more"',
      '"%errorlevel%"=="2"' in bat and re.search(r'(?mi)^\s*if errorlevel 2\b', bat) is None)

installs = len(re.findall(r'pip install --quiet', bat))
guards = len(re.findall(r'if errorlevel 1 goto :pipfailed', bat))
check('every pip install is followed by an exit-code check',
      installs > 0 and guards >= installs, '%d installs, %d guards' % (installs, guards))
check('  and the imports are re-tested after installing',
      bat.count('import PIL, send2trash, imageio_ffmpeg') >= 2)
check('  a failed install stops, with a message and a pause',
      ':pipfailed' in bat and re.search(r':pipfailed[\s\S]{0,600}?pause[\s\S]{0,40}?exit /b 1', bat)
      is not None)

# The order of the two clauses is the whole fix: exited -> report, otherwise -> open regardless.
i_exit = bat.find('if ($p.HasExited) { Write-Host')
i_open = bat.find("if ($exe) { Start-Process -FilePath $exe")
check('a server that has exited prints its log instead of opening a window',
      i_exit != -1 and i_open != -1 and i_exit < i_open,
      'exit-report at %d, browser at %d' % (i_exit, i_open))
check('  and a slow start still opens -- the test is HasExited, not the poll',
      '$ok' in bat and 'if (-not $ok) { exit' not in bat)
check('  cmd notices the failure and holds the console open',
      re.search(r'if errorlevel 1 \([\s\S]{0,300}?pause', bat) is not None)

# ---- 2. a port that is already taken ------------------------------------------------------------
print('a busy port')
held = socket.socket()
held.bind(('127.0.0.1', 0))
held.listen(5)
busy = held.getsockname()[1]

env = dict(os.environ, PORT=str(busy), CV_DATA=os.path.join(TMP, 'data2'),
           CV_CONFIG=os.path.join(TMP, 'config2.json'))
os.makedirs(env['CV_DATA'], exist_ok=True)
p = subprocess.run([sys.executable, os.path.join(ROOT, 'server.py')], env=env,
                   capture_output=True, text=True, timeout=120)
err = (p.stderr or '') + (p.stdout or '')
check('the server stops instead of hanging or half-starting', p.returncode == 1, p.returncode)
check('  and names the port on stderr', str(busy) in (p.stderr or ''), p.stderr[-400:])
# NAMES THE FILE, not merely "change the port". The reader has no app to ask -- it just refused to
# start -- so the message has to carry the fix. It said config.json until 2026-09-14, when the port
# moved to port.txt; this assertion is what noticed the message had gone stale.
check('  and says what to do about it',
      'port.txt' in (p.stderr or ''), p.stderr[-400:])
check('  no traceback -- this is a message, not a crash',
      'Traceback' not in err, err[-400:])
held.close()

# ---- 3. a folder that does not answer -----------------------------------------------------------
print('an unreachable folder')
here = os.path.join(TMP, 'here')
os.makedirs(here, exist_ok=True)
check('a real folder reads as ok', server.probe_dir(here) == 'ok')
check('a wrong path reads as missing',
      server.probe_dir(os.path.join(TMP, 'nope')) == 'missing')

# THE DEADLINE IS THE THING UNDER TEST, so the slowness is supplied rather than waited for: a real
# dead share answers in its own time and would make this test both slow and flaky. What runs is the
# shipped function -- its thread, its join, its verdict.
real_isdir = os.path.isdir


def slow_isdir(p):
    if p == r'\\vv-test-host\share':
        time.sleep(30)
    return real_isdir(p)


os.path.isdir = slow_isdir
try:
    t0 = time.time()
    verdict = server.probe_dir(r'\\vv-test-host\share', timeout=1.0)
    took = time.time() - t0
    check('a folder that never answers reads as unreachable, not missing', verdict == 'unreachable',
          verdict)
    check('  and it gives up on time', took < 5, '%.1fs' % took)

    # The wiring, not just the helper: the endpoint has to ASK. Reverting it to a bare isdir is the
    # regression, and it would pass every other test in this folder.
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    c.request('POST', '/api/roots/add', json.dumps({'path': r'\\vv-test-host\share'}),
              {'Content-Type': 'application/json'})
    r = c.getresponse()
    body = json.loads(r.read().decode('utf-8'))
    c.close()
    httpd.shutdown()
    check('adding it says nothing answered, not "folder not found"',
          'not found' not in (body.get('error') or '').lower(), body)
    check('  and names the computer that did not answer',
          'vv-test-host' in (body.get('error') or ''), body)
finally:
    os.path.isdir = real_isdir

# A typo and a sleeping share must not read the same. This is the pair the whole item is about.
check('a missing drive is named as a missing drive',
      'no Z: drive' in server.missing_msg(r'Z:\pictures'), server.missing_msg(r'Z:\pictures'))
check('a sleeping share names the computer',
      'vv-test-host' in server.unreachable_msg(r'\\vv-test-host\share'))

# ---- done ---------------------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print('FAILED: %d' % len(failures))
    sys.exit(1)
print('all good')
