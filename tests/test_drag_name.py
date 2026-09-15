"""Regression test for drag-to-ComfyUI: the RIGHT file, under its OWN name.

Run: python tests/test_drag_name.py

Why this test exists. Dragging a card into ComfyUI hands the drop target the dragged <img>'s bytes,
and two things went wrong with that at once — found 2026-08-11 by looking inside ComfyUI's `input`
folder rather than reasoning about the code:

  1. **.webp files were in there.** A browser lets you drag ANY <img>, and the filmstrip's images
     are 160px WebP thumbnails. So a drag from the strip delivered a thumbnail with no workflow in
     it: it loads, it looks roughly right, and it is the wrong file. Only the grid card (which swaps
     its face to the original first) and the detail view's main picture are ever the real thing.

  2. **Everything landed as its row number** — `28442.png` — because the browser names a dragged
     file from the URL's last path segment, and ours ended in the id. A name that means nothing is
     one folder-clean away from a workflow that can't find its input.

So this pins the two halves separately: the HTTP shape that gives the drop its name, and a source
rule that no thumbnail is draggable. The second is a source-level assertion on purpose — the failure
mode is someone adding a new thumbnail somewhere and not knowing this rule exists.

Runs in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import os
import re
import sys
import shutil
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-dragname-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402  (must follow the env vars above)
from http.server import ThreadingHTTPServer  # noqa: E402

MEDIA = os.path.join(TMP, 'VV_00123_krea_x2.png')
BLOB = bytes(range(256)) * 40
with open(MEDIA, 'wb') as f:
    f.write(BLOB)

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


def get(path, headers=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    c.request('GET', path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


print('\nDrag-to-ComfyUI: the right file, under its own name\n')

# ---- 1. the name reaches the browser ---------------------------------------------------------
status, hdrs, body = get('/file/7/VV_00123_krea_x2.png?v=1&r=abc')
check('a named /file/ URL serves the original', status == 200 and body == BLOB,
      'status=%s len=%d of %d' % (status, len(body), len(BLOB)))
cd = hdrs.get('Content-Disposition', '')
check('  and names it in Content-Disposition', 'VV_00123_krea_x2.png' in cd, cd or '(absent)')
check('  INLINE, not as a download', cd.lower().startswith('inline'), cd or '(absent)')
check('  the row number is not the name', '/7.png' not in cd and 'filename="7' not in cd, cd)

# ---- 2. the old URL shape still works -------------------------------------------------------
# Every /file/ URL in the wild — a cached page, a video element mid-scrub — predates the name.
status, hdrs, body = get('/file/7?v=1&r=abc')
check('an unnamed /file/ URL still serves the original', status == 200 and body == BLOB,
      'status=%s len=%d' % (status, len(body)))

# ---- 3. video seeking still works through the longer path ------------------------------------
# The name is a new path segment, and Range is what a <video> uses on every scrub.
status, hdrs, body = get('/file/7/VV_00123_krea_x2.png?v=1&r=abc', {'Range': 'bytes=10-19'})
check('Range still works on a named URL', status == 206 and body == BLOB[10:20],
      'status=%s len=%d' % (status, len(body)))

# ---- 4. a hostile name cannot break the header or escape the folder --------------------------
# A quote would end the filename="…" early; a path separator would be a write outside the folder.
nasty = server._safe_filename('../..\\evil "quoted" über.png')
check('a hostile filename is defanged', '"' not in nasty and '/' not in nasty
      and '\\' not in nasty and '..' not in nasty.replace('.png', ''), nasty)
check('  and keeps its extension', nasty.endswith('.png'), nasty)
check('an empty filename still yields something', server._safe_filename('') == 'image',
      server._safe_filename(''))

# ---- 5. the URL builder keeps the root scoping ------------------------------------------------
# Every media URL needs v= AND r= — without them a per-root rowid + the 24h cache served one
# library's picture for another's. The name must not have quietly displaced them.
u = server._file_url(42, 1700000000, 'rootkey', 'My Pic.png')
check('_file_url keeps ?v= and &r=', 'v=1700000000' in u and 'r=rootkey' in u, u)
check('  and carries the name before the query', u.startswith('/file/42/My%20Pic.png?'), u)
check('  and omits the segment when there is no name', server._file_url(42, 1, 'k') == '/file/42?v=1&r=k',
      server._file_url(42, 1, 'k'))

# ---- 6. THE SOURCE RULE: no thumbnail is draggable -------------------------------------------
# A browser drags any <img> by default, so silence here means "draggable". Every <img> whose src is
# a thumbnail must say so explicitly. The grid card is the one legitimate exception: it is
# draggable="${canDrag}" and its dragstart handler refuses to fly until the face has been swapped
# to the real file.
with open(os.path.join(ROOT, 'app', 'app.js'), encoding='utf-8') as f:
    js = f.read()

offenders = []
for tag in re.findall(r'<img\b[^>]*>', js):
    if 'thumb_url' not in tag and 'thumbAt(' not in tag:
        continue
    if 'draggable=' not in tag:
        offenders.append(tag[:110])
check('every thumbnail <img> settles the question of dragging', not offenders,
      'no draggable= on:\n          ' + '\n          '.join(offenders))

thumb_tags = [t for t in re.findall(r'<img\b[^>]*>', js)
              if 'thumb_url' in t or 'thumbAt(' in t]
check('  and there are thumbnails to check at all', len(thumb_tags) >= 4, len(thumb_tags))

# THE RULE IS ABOUT THE FILE, NOT THE ELEMENT, and this test used to state it as "only the grid
# card may be draggable" — which is the rule's consequence rather than the rule. What actually went
# wrong was a THUMBNAIL reaching ComfyUI: a 160px WebP with no workflow in it.
#
# So a draggable <img> is fine when the file behind it cannot be a thumbnail. Two shapes qualify:
# the grid card's face, which is draggable="${canDrag}" and whose dragstart refuses to fly until the
# face has been swapped to the real file; and a song's invisible drag image, whose src goes through
# originalUrlFor(..., 'audio') and is therefore always /dragpng/ — a picture built to carry that
# song's workflow. A song has no "right original" to drag at all, so there is no smaller, worse
# version of it to hand over by mistake.
loose = [t[:110] for t in thumb_tags
         if 'draggable="false"' not in t
         and 'draggable="${canDrag}"' not in t
         and "originalUrlFor(" not in t]
check('  a draggable img is the card face or a /dragpng/ source, never a thumbnail', not loose,
      '\n          '.join(loose))

# ...and the exemption must be exactly that: every draggable thumbnail-derived img that is NOT the
# card face has to resolve through originalUrlFor, so a future one cannot claim it by accident.
exempt = [t for t in thumb_tags if 'draggable="true"' in t]
check('  the only draggable="true" imgs are dragpng sources',
      all('originalUrlFor(' in t for t in exempt), f'{len(exempt)} found')

# The filmstrip's PICTURE specifically, because that is the one that put .webp files in ComfyUI's
# input. A song in the strip carries a /dragpng/ instead and is the deliberate exception.
strip = re.search(r'class="strip-item".*?</button>', js, re.S)
check('the filmstrip image is explicitly not draggable',
      bool(strip) and 'draggable="false"' in strip.group(0))
check('  and the strip song rides originalUrlFor, not a thumbnail',
      bool(strip) and 'song-drag' in strip.group(0) and "originalUrlFor(" in strip.group(0))

# ---- 7. the detail view's drag handle ---------------------------------------------------------
# A <video> can never be a drag source — no browser hands one out as a file — so the small picture
# beside it IS the only way to drag a video out of the detail view. It was shown for a PAIRED video
# only; a lone txt2video had nothing, which the author reported as "no way to drag anything from the
# detail view". Both branches now use one component and differ only in label and source.
vs = js[js.index('function setVideoSource('):]
vs = vs[:vs.index('\n}')]
check('a lone video offers its first frame as the drag source',
      "originalUrlFor(" in vs and "'video'" in vs, vs[:160])
# Matched on the dragSourceLabel() CALL, not on the bare word: setPairStills( contains "Still", so
# `'Still' not in vs` passes for the wrong reason and would go on passing if the label were wrong.
check("  labelled honestly — it is a frame, not a saved still",
      "dragSourceLabel('Frame')" in vs and "dragSourceLabel('Still')" not in vs, vs[:160])
ps = js[js.index('function setPairSource('):]
ps = ps[:ps.index('\n}')]
check('a paired video still offers its REAL still',
      'file_url' in ps and "dragSourceLabel('Still')" in ps, ps[:160])
check('  and the two do not share a label', "dragSourceLabel('Frame')" not in ps)

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
