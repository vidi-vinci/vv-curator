"""test_recycle_completeness.py -- recycling a run takes the whole run, and nothing it still needs.

Run: python tests/test_recycle_completeness.py

The author, 2026-09-06: "if I recycle a video set - which may include an mp4, png, and txt file - the txt
files hangs around and does not get deleted. I also think the png's may be getting orphaned, but
that seems inconsistent."

TWO SEPARATE BUGS, both reproduced before they were fixed:

  1. THE SIDECAR WAS NEVER ASKED FOR. `.txt` is not an indexed media type, and comfy_meta.sidecar_path
     had exactly one caller -- the metadata READER. The recycle path works from indexed rows, so the
     text file was never in the list to begin with.

  2. THE ORPHANS ARE THE VIEW'S EXCLUSIONS LEAKING INTO THE DELETE. A card's `group_members` is built
     by a window function over rows that survived the GLOBAL exclusions, and those stay applied
     per-file on purpose so a matching sibling cannot drag a hidden file back on screen.
     Right for looking; wrong for deleting. Measured on the fixture below: hiding one member dropped
     it from members (3 -> 2), and recycling the card then left that file on disk. THAT is the
     inconsistency -- it depended on whether any member of that run happened to be excluded.

     TWO exclusions could do this when the bug was found. Hide large images was the other, and it
     was removed on 2026-09-08; the Hidden mark below is the one that remains, and it is enough to
     reproduce the fault. The oversized file stays in the fixture on purpose -- it is now an
     ORDINARY member, and that it still goes with its run is the check that hide-large really is
     gone from this path rather than merely switched off.

WHY THE SIDECAR RULE IS CONSERVATIVE. sidecar_path's last candidate is the stem truncated at the run
code -- ONE sidecar for a whole generation. On this fixture all three files resolve to the SAME text
file, so "take the sidecar with the video" would delete the description of stills being kept. It
goes only when every indexed member of its group goes too; the last check here is that one, and it
is the one that must never regress, because a deleted sidecar is metadata nothing can rebuild.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-recycle-')
LIB = os.path.join(TMP, 'lib')
os.makedirs(LIB, exist_ok=True)
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PIL import Image  # noqa: E402
import comfy_meta  # noqa: E402
import index_db  # noqa: E402
import server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def build(code):
    """One generation: a normal still, an oversized still, a video, and one sidecar for all three."""
    small = os.path.join(LIB, 'Run_07-43-11%s_First_00001_.png' % code)
    big = os.path.join(LIB, 'Run_07-43-11%s_Big_00002_.png' % code)
    vid = os.path.join(LIB, 'Run_07-43-11%s_00001_.mp4' % code)
    side = os.path.join(LIB, 'Run_07-43-11%s.txt' % code)
    Image.new('RGB', (640, 480), (40, 60, 90)).save(small)
    Image.new('RGB', (2600, 2600), (90, 60, 40)).save(big)     # big; an ordinary row since 2026-09-08
    open(vid, 'wb').write(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 400)
    open(side, 'w').write('positive: a test\n')
    return small, big, vid, side


A_small, A_big, A_vid, A_side = build('~vvaaa111')
B_small, B_big, B_vid, B_side = build('~vvbbb222')

server.CONFIG['roots'] = [{'path': LIB, 'name': 'Repro', 'key': 'repro'}]
server.CONFIG['active'] = 'repro'
server.ACTIVE.update({'key': 'repro', 'path': LIB, 'db': server.LIBRARY_DB})
index_db.scan(LIB, server.LIBRARY_DB, root_id='repro')

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def call(method, path, body=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=15)
    payload = json.dumps(body) if body is not None else None
    c.request(method, path, payload, {'Content-Type': 'application/json'} if payload else {})
    out = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return out


def ids_by_name():
    conn = server.db()
    out = {r['filename']: r['id'] for r in conn.execute('SELECT id, filename FROM images')}
    conn.close()
    return out


def recycle(ids, collapsed):
    j = call('POST', '/api/delete', {'ids': ids, 'collapsed': collapsed})
    call('POST', '/api/delete/commit', {'batch': j['batch']})       # skip the undo window
    return j


names = ids_by_name()
check('the sidecar is not indexed (it is a .txt, not a media file)',
      os.path.basename(A_side) not in names, sorted(names))
check('  all three real files are', len(names) == 6, sorted(names))

# ---- the shared sidecar is genuinely shared, or the last check below proves nothing -------------
resolved = {os.path.basename(comfy_meta.sidecar_path(p) or '') for p in (A_small, A_big, A_vid)}
check('all three members resolve to ONE sidecar (the run-code form)',
      resolved == {os.path.basename(A_side)}, resolved)

# ---- 1. recycling the card takes the whole run, the hidden member included ----------------------
# Hide one member: the card's own membership now omits one of its three files. This is the state
# The author's libraries were in.
conn = server.db()
conn.execute("INSERT INTO tags (image_id, tag, source) VALUES (?, 'hide', 'hide')",
             (names[os.path.basename(A_small)],))
conn.commit()
conn.close()

# The client sends what the GRID gave it -- here, everything except the hidden still.
recycle([names[os.path.basename(A_vid)]], True)

check('the video went', not os.path.exists(A_vid))
check('  the HIDDEN still went with it, though the grid never listed it', not os.path.exists(A_small))
check('  and so did the oversized one, now an ordinary member', not os.path.exists(A_big))
check('  and the sidecar went, because nothing indexed still points at it',
      not os.path.exists(A_side))

conn = server.db()
left = [r['filename'] for r in conn.execute("SELECT filename FROM images WHERE path LIKE ?",
                                            ('%~vvaaa111%',))]
conn.close()
check('  no rows left behind for that run either', left == [], left)

# ---- 2. THE RULE THAT MUST NEVER REGRESS: a shared sidecar survives a partial delete ------------
# Recycle ONE member of run B with collapsing off, the way deleting a single file does. Two indexed
# members remain, and both still resolve to that sidecar -- so it has to stay.
names = ids_by_name()
recycle([names[os.path.basename(B_vid)]], False)

check('a single file recycles on its own when nothing is merging', not os.path.exists(B_vid))
check('  its siblings are untouched', os.path.exists(B_small) and os.path.exists(B_big))
check('  AND THE SHARED SIDECAR SURVIVES -- two kept files still describe themselves by it',
      os.path.exists(B_side))

# ...and once the rest of the run goes, it may finally go too.
names = ids_by_name()
recycle([names[os.path.basename(B_small)], names[os.path.basename(B_big)]], False)
check('once the last member goes, the sidecar follows', not os.path.exists(B_side))

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
