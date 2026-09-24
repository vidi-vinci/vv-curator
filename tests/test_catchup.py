"""The offer to catch a library up after an update, and the price on it.

Run: python tests/test_catchup.py

The feature: on the first launch of a build that reads more than the one which indexed a library,
a dialog says so and offers to rescan. The author's design and his words, 2026-09-12 — *"that version
should include an optional trigger to recommend a File Rescan on first launch"* — and his wording
for the dialog, which names what does not work rather than what the reader can now parse.

What this pins, and why each one can rot quietly:

  1. **The count is FILES BEHIND, per library.** A run stopped half way leaves some rows stamped and
     some not, so the next offer has to be about what is left. Counting files, or counting once for
     the whole app, would both re-offer work that is already done.
  2. **A time is quoted only when it has been measured.** A fresh install has no rate and must say
     the file count alone. This is the number someone decides on, so a guessed minute figure is
     worse than none — and "we'll just use a default rate" is the exact shortcut this forbids.
  3. **The rate comes from a FORCED run only.** An ordinary scan skips unchanged files, so its speed
     is the speed of deciding not to work — the same lie the job bar's ETA refuses to print.
  4. **An unreachable library is reported, not hidden**, and not counted in the estimate. It keeps
     its old stamps, so it is offered again next launch.
  5. **The menu has one scan verb**, and the all-libraries action lives in the menu that is about all
     the libraries.
  6. **Two tiers, and they must not collapse into one.** "Do it later" stays sessionStorage —
     "until you next start the app" — because a postponed rescan that never returns is a
     lost one. The **Don't show this again** checkbox (the author, 2026-09-23) is the other answer
     and does persist, but KEYED ON THE READER VERSION, so a build that reads more asks again:
     his rule was "if they download a new version, then the 'Don't show again' should reset". A
     bare localStorage key would be "never again", and the user would never learn that a later
     version had left their library behind.
  7. **The sentence counts libraries, and its verb follows the numerator.** "1 of 4 libraries WAS
     indexed by an older version." The old wording said "Your libraries" and implied every one of
     them was stale, when usually only some are.

Runs in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-catchup-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import index_db  # noqa: E402
import server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from PIL import Image  # noqa: E402

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


def make_lib(name, n):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        Image.new('RGB', (8, 8), (i % 255, 40, 50)).save(os.path.join(d, '%s_%04d.png' % (name, i)))
    return d


# Two libraries: one that will stay reachable, one whose folder is taken away to stand in for a
# share that is asleep. Ten files is plenty for the counting; the rate test lowers its own bar.
A = make_lib('alpha', 10)
B = make_lib('beta', 4)
ka, kb = server._root_key(A), server._root_key(B)
server.CONFIG['roots'] = [{'path': A, 'name': 'Alpha', 'key': ka},
                          {'path': B, 'name': 'Beta', 'key': kb}]
for k in (ka, kb):
    server.set_active(k)
    server.run_scan()
    while server._scan_state['running']:
        pass

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def catchup():
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    c.request('GET', '/api/catchup')
    body = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return body


# ---- 1. nothing to offer when everything is current ---------------------------------------------
print('a library this build indexed itself')
j = catchup()
check('offers nothing', not j['libraries'] and not j['files'], j)

# ---- 2. rows left behind by an older build -------------------------------------------------------
# Half of Alpha and all of Beta, so the count has to be per library AND partial.
conn = server.db()
conn.execute("UPDATE images SET reader_ver=0 WHERE root_id=? AND id IN "
             "(SELECT id FROM images WHERE root_id=? LIMIT 6)", (ka, ka))
conn.execute("UPDATE images SET reader_ver=0 WHERE root_id=?", (kb,))
conn.commit()
conn.close()

print('after an update')
j = catchup()
by = {l['key']: l for l in j['libraries']}
check('both libraries are offered', set(by) == {ka, kb}, list(by))
check('  and each counts only ITS OWN files behind',
      by.get(ka, {}).get('files') == 6 and by.get(kb, {}).get('files') == 4, j['libraries'])
check('  the total is what would actually be re-read', j['files'] == 10, j['files'])
check('  with no time, because nothing has been measured yet', j['seconds'] is None, j['seconds'])

# ---- 3. a library that is not answering ----------------------------------------------------------
print('a library whose share is asleep')
os.rename(B, B + '_away')
j = catchup()
by = {l['key']: l for l in j['libraries']}
check('it is still named in the offer', kb in by, list(by))
check('  but marked unreachable', by[kb]['reachable'] is False, by[kb])
check('  and left out of the total', j['files'] == 6, j['files'])
os.rename(B + '_away', B)

# ---- 4. where the estimate comes from -------------------------------------------------------------
# The bar is lowered rather than writing 200 files: what is under test is WHICH RUN records a rate,
# not the size of the run.
print('measuring how fast this machine reads')
index_db.RATE_MIN_FILES = 4


def rate_for(key):
    conn = server.db()
    r = conn.execute("SELECT value FROM meta WHERE key=?", ('read_rate:' + key,)).fetchone()
    conn.close()
    return float(r['value']) if r else None


server.set_active(ka)
server.run_scan()                       # an ORDINARY scan: every file is unchanged, so none are read
while server._scan_state['running']:
    pass
check('an ordinary scan records no rate', rate_for(ka) is None, rate_for(ka))

server.run_scan(force=True)             # a forced one reads every file, which is the honest measure
while server._scan_state['running']:
    pass
check('a forced rescan does', (rate_for(ka) or 0) > 0, rate_for(ka))

# Alpha is current again after that forced run; put Beta behind and give it a rate of its own so the
# estimate has everything it needs.
conn = server.db()
conn.execute("UPDATE images SET reader_ver=0 WHERE root_id=?", (kb,))
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, '2.0')", ('read_rate:' + kb,))
conn.commit()
conn.close()
j = catchup()
check('the offer now carries a time', j['seconds'] == 2, j)      # 4 files at 2/s
check('  for the library that is actually behind',
      [l['key'] for l in j['libraries']] == [kb], j['libraries'])

httpd.shutdown()

# ---- 5. the interface ------------------------------------------------------------------------------
print('the menus')
html = read('app/index.html')
js = read('app/app.js')
check('the per-library menu has one scan verb',
      'data-act="rescan"' in html and 'data-act="rebuild"' not in html
      and 'data-act="rebuildall"' not in html)
check('  and nothing still routes to the removed items',
      'rebuildLib' not in js and 'rebuildAllLibs' not in js)
check('Rescan all libraries lives in the Libraries menu, above Add library',
      html.index('id="libRescanAll"') < html.index('id="libAdd"'),
      'libRescanAll must come first')

print('the offer')
# The old form of this forbade localStorage outright, and by 2026-09-23 it was passing by ACCIDENT:
# its regex was case-sensitive and the new key's constant is upper case, so the very storage it was
# meant to govern slipped straight past it. Name both tiers instead of banning one mechanism.
check('"Do it later" is remembered for the session only',
      'sessionStorage.setItem(CATCHUP_SNOOZE' in js)
check('  and the checkbox that does persist is keyed on the reader version',
      'localStorage.setItem(hushKey' in js
      and '`${CATCHUP_HUSH}.${j.reader}`' in js)
check('  and says where the action went', 'Rescan all libraries' in js)
# REPLACED 2026-09-14. It said "This version has new features that won't work until a rescan",
# which points at the interface, where nothing changes. Every alternative that named WHAT improved
# would need rewriting per release -- "reads more metadata" is false for a version that fixes
# something it read WRONGLY, and for one that only groups files differently, which the model-family
# merge already does. The author asked the question that settled it, of a wording I had proposed:
# "will 1 ALWAYS be true?" It would not have been. This sentence describes the GAP rather than what
# filled it, so it is true of every bump there can be, and what improved goes in the release notes.
check('the dialog uses the author\'s wording',
      'indexed by an older version. Rescanning brings ${them} up to date with ' in js)
# HOW MANY OF HOW MANY, the author's ask on 2026-09-23. The first clause used to read "Your
# libraries", which says every one of them is stale when usually only some are. Each branch is
# pinned separately: they are different sentences and only one is ever on screen.
check('  and says how many libraries are behind, of how many there are',
      '`${nBehind} of ${nTotal} libraries ' in js)
check('    with the verb following the NUMERATOR, not the word libraries',
      """${nBehind === 1 ? 'was' : 'were'}""" in js)
check('    and a word, not a digit, when every library is behind',
      """'Both libraries were'""" in js and '`All ${nTotal} libraries were`' in js)
check('    and the singular library never reads "1 of 1"',
      """nTotal <= 1 ? 'Your library was'""" in js)
# The numerator is what pressing Rescan would TOUCH, so a library that is behind AND offline is not
# in it -- that one is named in the skip sentence, which is the honest place for it.
check('    counting only the libraries this run would reach',
      'const nBehind = live.length;' in js)
# The heading carries the version so the sentence can be about what to do. It is also the one place
# the release number is spoken to a person, which is why it degrades to a plain sentence rather than
# printing "version  of" when the server sends none.
check('  under a heading naming the version',
      'Welcome to version ${_appVersion}' in js and 'has been updated' in js)
check('  and the dialog component clears a title it was not given',
      "t.classList.toggle('hidden', !title)" in js)
# Naming the menu route in the DIALOG, not only in the message after declining, is what makes "Do it
# later" a choice rather than a dead end — and it is the only place the user is told they can take
# one library instead of all of them.
check('  and names the menu route as an alternative',
      "Rescan in each library's" in js and 'Rescan all libraries in the Libraries menu' in js)
check('a time is only ever printed when one was measured',
      re.search(r'j\.seconds\s*\?', js) is not None)

# ONE WORD FOR THE ACT. The author, 2026-09-12. "Re-read" is the mechanism and belongs in the comments; a
# second word for the same thing in the interface is how Rescan and Rebuild metadata became two
# ideas people could not tell apart. Checked on the STRINGS, so the comments stay free to be
# accurate about what is happening.
# COMMENTS COME OUT FIRST. Without that, an apostrophe in prose ("don't") opens a quote that runs
# to the next one, and the "string" it yields is a paragraph of commentary — which is how the first
# version of this check reported nine failures, every one of them a comment doing its job.
_nocomments = re.sub(r'/\*.*?\*/', '', js, flags=re.S)
_nocomments = re.sub(r'(?m)^\s*//.*$', '', _nocomments)
strings = (re.findall(r'`([^`]*)`', _nocomments) + re.findall(r"'([^']*)'", _nocomments)
           + re.findall(r'"([^"]*)"', _nocomments))
offenders = [s for s in strings if re.search(r'\bre-read\b', s, re.I)]
# ASCII for the report: the console here is cp1252 and the app's strings carry arrows and ellipses,
# so printing a failure verbatim would crash the run instead of describing it.
check('the interface says "rescan", never "re-read"', not offenders,
      [s.encode('ascii', 'replace').decode()[:80] for s in offenders[:3]])
check('  and the retired words are gone from Help',
      'metadata rebuild' not in read('docs/help.md'))

shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print('FAILED: %d' % len(failures))
    sys.exit(1)
print('all good')
