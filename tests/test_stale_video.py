"""A refresh must reach a video in a folder that has not changed.

Run:  python tests/test_stale_video.py

THE BUG, and the SECOND time the same fix was reported incomplete. A video indexed before its size
and length could be read out of the container carries neither, and the scan's skip check has an
exception that re-reads exactly those rows. That exception is real, and for weeks it could still
have done nothing, because it only fires for files the scan LOOKS at -- and an ordinary refresh is
usually a QUICK scan: it stats folder signatures and walks only the ones that moved.

A finished folder never moves. That is precisely where old videos live.

The author, 2026-08-25, having been told a plain refresh would be enough: "I did have to refresh metadata.
The times didn't show up for LTX until i did so." Worse than a wrong answer for him specifically --
Rebuild metadata is per-library, so his other two libraries would have gone on waiting silently.

The fix clears the full-walk clock once, which is the signal run_scan already reads for "a library
indexed by an older build". The next refresh walks everything ONE time, skipping unchanged files as
always, so only the videos actually missing something are re-read.

WHAT MAKES THIS TEST WORTH ITS RUNTIME is that three of its assertions are about the fix NOT firing:
the control proving a quick refresh really did miss the file, and the two proving a later restart
does not clear the clock again. A one-time repair that runs every time is a different bug, and it
would be invisible -- everything on screen would look right while every refresh walked the library.

The fixture is a subfolder ON PURPOSE. A quick scan always walks the root itself, so a file in the
top level is visited every time and would pass against the broken build. The first version of this
test did exactly that and reported all-clear.
"""

import json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import struct


def make_png(path, colour):
    """A small real PNG. INLINE, not imported from a sibling test: this used to read
    `from test_hidden import make_png`, and test_hidden.py was deleted with the Hidden mark
    (b1d926b) -- which stopped this file at its import line, so it had not run since. A test that
    depends on another test file is one deletion away from silently not existing."""
    from PIL import Image
    Image.new('RGB', (48, 48), colour).save(path)


def _box(typ, payload):
    return struct.pack('>I', len(payload) + 8) + typ + payload


def make_mp4(path, w, h, seconds, timescale=600):
    """A structurally real MP4 -- mvhd for the length, one video trak whose tkhd declares the size.
    No media: nothing here decodes a frame, so the test needs no ffmpeg. THE MOOV GOES LAST, which
    is where ComfyUI puts it and the layout every one of the author's samples uses."""
    mvhd = _box(b'mvhd', bytes(12) + struct.pack('>I', timescale)
                + struct.pack('>I', int(round(seconds * timescale))))
    tkhd = _box(b'tkhd', bytes(24) + bytes(52)
                + struct.pack('>I', w << 16) + struct.pack('>I', h << 16))
    with open(path, 'wb') as f:
        f.write(_box(b'ftyp', b'isom' + bytes(8)) + _box(b'mdat', bytes(8192))
                + _box(b'moov', mvhd + _box(b'trak', tkhd)))
here = ROOT
tmp = tempfile.mkdtemp(prefix='vvquick_')
lib = os.path.join(tmp,'lib')
# A SUBFOLDER, which is the whole point: a quick scan always walks the root itself, so a fixture
# in the top level is visited every time and proves nothing. The author's LTX videos live in
# 'LTXJul28_Face', a finished folder that never changes again.
sub = os.path.join(lib,'LTXJul28_Face'); os.makedirs(sub)
lib_files = sub
make_png(os.path.join(sub,'run_00001_.png'), (30,60,90))
make_mp4(os.path.join(sub,'run_00001_.mp4'), 384, 640, 6.0)
cfg=os.path.join(tmp,'config.json'); data=os.path.join(tmp,'data'); os.makedirs(data)
s=socket.socket(); s.bind(('127.0.0.1',0)); port=s.getsockname()[1]; s.close()
json.dump({'roots':[{'key':'A','name':'Main','path':lib}],'active':'A','port':port}, open(cfg,'w'))
dbp=os.path.join(data,'library.db')

def call(path, body=None, t=30):
    url=f'http://127.0.0.1:{port}{path}'
    req=(urllib.request.Request(url) if body is None else
         urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers={'Content-Type':'application/json'}))
    with urllib.request.urlopen(req, timeout=t) as r: return json.loads(r.read().decode())
def wait():
    for _ in range(200):
        st=call('/api/scan/status')
        if not st.get('running'): return st
        time.sleep(0.25)
fails=[]
def check(n,g,w):
    ok=g==w; print(('  ok    ' if ok else '  FAIL  ')+n+('' if ok else '   got %r want %r'%(g,w)))
    if not ok: fails.append(n)

log=open(os.path.join(tmp,'server.log'),'wb')
env=dict(os.environ,CV_CONFIG=cfg,CV_DATA=data)

def start():
    p=subprocess.Popen([sys.executable,'server.py'],cwd=here,env=env,stdout=log,stderr=log)
    for _ in range(80):
        try:
            call('/api/config'); return p
        except Exception: time.sleep(0.25)
    raise SystemExit('server never came up')

def stop(p):
    p.terminate(); time.sleep(0.6)

proc=start()
try:
    call('/api/scan',{'force':True}); wait()

    def make_old():
        """The library as an OLDER build left it: the video's length and size never learned, the
        full-walk clock fresh, and no record of this build's one-time walk."""
        c=sqlite3.connect(dbp)
        c.execute("UPDATE images SET duration=NULL, width=NULL, height=NULL WHERE ext='.mp4'")
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('full_scan_at:A', ?)",
                  (str(time.time()),))
        c.execute("DELETE FROM meta WHERE key='backfill:video_container'")
        c.commit(); c.close()

    def video_row():
        c=sqlite3.connect(dbp)
        got=c.execute("SELECT width,height,duration FROM images WHERE ext='.mp4'").fetchone()
        c.close(); return got

    # ---- the OLD behaviour, to prove the control is real ----------------------------------------
    make_old()
    c=sqlite3.connect(dbp)   # ...but pretend the one-time walk already happened
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('backfill:video_container','1')")
    c.commit(); c.close()
    call('/api/scan',{}); wait()
    check('WITHOUT the one-time walk, a quick refresh misses it (the bug the author hit)',
          video_row(), (None,None,None))

    # ---- what actually happens on update: the app RESTARTS, the migration runs ------------------
    make_old()
    stop(proc); proc=start()
    check('the migration cleared the full-walk clock',
          sqlite3.connect(dbp).execute(
              "SELECT COUNT(*) FROM meta WHERE key LIKE 'full_scan_at%'").fetchone()[0], 0)
    call('/api/scan',{}); st=wait()          # an ORDINARY refresh, nothing forced
    check('the refresh completed without error', st.get('error'), None)
    check('an ORDINARY refresh now fills it in', video_row(), (384,640,6.0))

    items=call('/api/search?limit=50&group=1&sets=1')['items']
    card=[i for i in items if i['filename']=='run_00001_.png'][0]
    check('and the card shows the length', card.get('duration'), 6.0)

    # ...ONCE. A restart must not clear the clock a second time.
    stop(proc); proc=start()
    check('a later restart does not clear it again',
          sqlite3.connect(dbp).execute(
              "SELECT COUNT(*) FROM meta WHERE key LIKE 'full_scan_at%'").fetchone()[0], 1)
    call('/api/scan',{}); st2=wait()
    check('the following refresh is back to skipping', st2.get('stats',{}).get('updated'), 0)
finally:
    stop(proc); log.close(); shutil.rmtree(tmp, ignore_errors=True)
print('')
print('%d FAILED' % len(fails) if fails else 'all passed')
print('')
sys.exit(1 if fails else 0)
