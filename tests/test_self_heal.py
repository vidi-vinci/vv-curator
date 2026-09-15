"""A file read by an older build catches itself up the moment you open it.

Run: python tests/test_self_heal.py

Why this exists. Every time the reader learns something — generation settings and the VAE, stacked
LoRA nodes, a video's length, song lyrics in the search index, the negative-prompt fix — every
library indexed before that build goes on showing blanks, and nothing notices. The user sees an
empty row and concludes the app cannot read their files. Asking them to press a rescan is asking
them to diagnose something invisible to them, which is how the app ended up with two menu items
nobody could tell apart.

So each row records the READER_VERSION that read it, and opening a file whose stamp is behind
re-reads that one file. The author, 2026-09-12: *"can we detect a file that needs a rescan, and do it
on-the-fly? ... kinda self-healing?"*

What this pins, and why each one can rot silently:

  1. **A stale row heals on open, and a current one is left alone.** The second half is the cost
     control: without it every open re-reads, forever.
  2. **Curation survives the heal.** The re-read runs the same UPDATE a rescan does, whose safety
     comes entirely from the columns it does NOT name. A note, a label, a tag and the viewing
     history are what would be lost, and losing them is unrecoverable — there is no file to read
     them back from.
  3. **A read that fails leaves the stamp behind.** It must try again next time, because the file
     that failed to read once is one of the two reasons this feature exists; stamping it on failure
     would freeze the blank forever, which is the bug it is meant to cure.
  4. **It is keyed on the stamp, never on the row looking empty.** A PNG with no workflow correctly
     has no prompt. Healing on emptiness would re-read every such file on every open for the life of
     the library — the same permanent tax the scan already refuses to pay on WebMs.

Runs in-process on an ephemeral port against a temp data dir. Touches no real library.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-heal-')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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


def graph():
    return {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'sdxl/base.safetensors'}},
        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a lighthouse in a storm'}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'blurry'}},
        '4': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 64, 'height': 64}},
        '5': {'class_type': 'KSampler',
              'inputs': {'steps': 28, 'cfg': 6.5, 'sampler_name': 'euler', 'scheduler': 'karras',
                         'seed': 4242, 'model': ['1', 0], 'positive': ['2', 0],
                         'negative': ['3', 0], 'latent_image': ['4', 0]}},
    }


def png(path):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text('prompt', json.dumps(graph()))
    Image.new('RGB', (64, 64), (30, 40, 50)).save(path, pnginfo=info)


# ---- a library with one real PNG in it ----------------------------------------------------------
LIB = os.path.join(TMP, 'lib')
os.makedirs(LIB, exist_ok=True)
png(os.path.join(LIB, 'shot_00001.png'))
png(os.path.join(LIB, 'plain.png'))

key = server._root_key(LIB)
server.CONFIG['roots'] = [{'path': LIB, 'name': 'Lib', 'key': key}]
server.set_active(key)
server.run_scan()
while server._scan_state['running']:
    pass

conn = server.db()
rows = conn.execute("SELECT id, filename, reader_ver, positive FROM images ORDER BY filename").fetchall()
conn.close()
check('the scan indexed both files', len(rows) == 2, [dict(r) for r in rows])
check('  and stamped them with this build\'s reader version',
      all(r['reader_ver'] == index_db.READER_VERSION for r in rows), [dict(r) for r in rows])
iid = rows[0]['id']

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def get_image(i):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    c.request('GET', '/api/image/%d' % i)
    body = json.loads(c.getresponse().read().decode('utf-8'))
    c.close()
    return body


def row_of(i):
    conn = server.db()
    r = conn.execute("SELECT * FROM images WHERE id=?", (i,)).fetchone()
    conn.close()
    return r


# ---- 1. an older build's row, with curation on it -----------------------------------------------
# Exactly what an older reader leaves behind: values it never knew how to extract, and a stamp
# saying which build read it. The curation is added the way the app adds it.
conn = server.db()
conn.execute("UPDATE images SET reader_ver=0, positive='', gp_steps=NULL, gp_cfg=NULL, "
             "note='keep for the cover', last_opened=1234.5 WHERE id=?", (iid,))
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?,?,'user')", (iid, 'label:publish'))
conn.execute("INSERT INTO tags(image_id, tag, source) VALUES(?,?,'user')", (iid, 'lighthouse'))
conn.commit()
conn.close()

print('opening a file an older build read')
d = get_image(iid)
check('the prompt comes back', d.get('positive') == 'a lighthouse in a storm', d.get('positive'))
check('  and the settings it never had', d.get('gp_steps') == 28 and d.get('gp_cfg') == 6.5,
      (d.get('gp_steps'), d.get('gp_cfg')))
check('  in the SAME answer -- no second request needed', 'positive' in d)

# The search index is rebuilt with the row, so a healed file is findable BY the prompt it just
# regained. Easy to lose: the UPDATE and the two FTS calls are three statements that have to travel
# together, and nothing else would notice if the third stopped happening.
conn = server.db()
hits = [r[0] for r in conn.execute(
    "SELECT rowid FROM images_fts WHERE images_fts MATCH 'lighthouse'").fetchall()]
conn.close()
check('  and the file is now findable by that prompt', iid in hits, hits)

r = row_of(iid)
check('  the row is stamped current now', r['reader_ver'] == index_db.READER_VERSION, r['reader_ver'])

# ---- 2. the thing that must not be lost ---------------------------------------------------------
print('curation survives it')
check('the note is still there', r['note'] == 'keep for the cover', r['note'])
check('the label is still there', 'label:publish' in (d.get('tags') or []), d.get('tags'))
check('the tag is still there', 'lighthouse' in (d.get('tags') or []), d.get('tags'))
check('the viewing history is still there', r['last_opened'] == 1234.5, r['last_opened'])

# ---- 3. a current row is not re-read -------------------------------------------------------------
# Without this the feature costs a file read on every open, forever. Counted at the real function,
# not inferred from a timestamp.
print('a file this build already read')
calls = []
real_reread = index_db.reread_one


def counting_reread(*a, **k):
    calls.append(1)
    return real_reread(*a, **k)


index_db.reread_one = counting_reread
try:
    get_image(iid)
    check('opening it again re-reads nothing', not calls, '%d call(s)' % len(calls))
finally:
    index_db.reread_one = real_reread

# ---- 4. a read that fails must try again ---------------------------------------------------------
print('a file that cannot be read right now')
gone = rows[1]['id']
conn = server.db()
conn.execute("UPDATE images SET reader_ver=0, path=? WHERE id=?",
             (os.path.join(LIB, 'not-here.png'), gone))
conn.commit()
conn.close()

d2 = get_image(gone)
check('the detail view still answers', bool(d2 and not d2.get('error')), d2)
check('  and the stamp is LEFT BEHIND, so the next open tries again',
      row_of(gone)['reader_ver'] == 0, row_of(gone)['reader_ver'])

# ---- 5. emptiness is not the signal --------------------------------------------------------------
# A PNG with no workflow reads as empty and that is the correct answer. Once stamped it must be left
# alone, or every such file pays a read on every open for the life of the library.
print('a file that is empty because it has nothing to say')
bare = os.path.join(LIB, 'bare.png')
from PIL import Image  # noqa: E402
Image.new('RGB', (64, 64), (9, 9, 9)).save(bare)
conn = server.db()
conn.execute("INSERT INTO images(path,rel_path,folder,filename,ext,mtime,size,has_meta,motion,"
             "indexed_at,reader_ver,root_id) VALUES(?,?,?,?,?,?,?,0,0,?,?,?)",
             (bare, 'bare.png', '.', 'bare.png', '.png', os.path.getmtime(bare),
              os.path.getsize(bare), 0, index_db.READER_VERSION, key))
bare_id = conn.execute("SELECT id FROM images WHERE filename='bare.png'").fetchone()['id']
conn.commit()
conn.close()

calls = []
index_db.reread_one = counting_reread
try:
    b = get_image(bare_id)
    check('an empty row that is up to date is left alone', not calls, '%d call(s)' % len(calls))
    check('  and still reports itself as having no metadata', not b.get('has_meta'), b.get('has_meta'))
finally:
    index_db.reread_one = real_reread

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print('FAILED: %d' % len(failures))
    sys.exit(1)
print('all good')
