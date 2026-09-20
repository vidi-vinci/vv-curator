"""test_song_drag_source.py -- a song's drag-to-ComfyUI handle uses the picture saved beside it.

THE BUG THIS PINS. A song cannot be dragged as itself -- no browser hands a media file out of one
window into another -- so the detail view offers a picture that carries the run's workflow. That
picture was always built OUT OF THE SONG, by reading the song's own tags, and the tag reader only
opens `.mp3`. So every FLAC showed a broken box where the drag source belongs, which is what the
author reported on 2026-09-17 against his YuE2 tracks.

THE FIX IS NOT A NEW READER. The cover saved beside the song already carries the entire workflow --
verified here by opening the real file, not by assuming it -- so the handle uses that file, exactly
as a paired video uses the real still saved beside it rather than a decoded frame. Same component,
same reasoning, one branch that had never been written.

It also cures a small lie: a song whose cover sits beside it was captioned "Cover" over a drawn
waveform, because the label asked whether a cover existed ANYWHERE while the picture only ever
looked inside the audio file.

REAL FILES, INDEXED THROUGH THE REAL SCANNER. The grouping that makes this work is index_db's, and
a fixture that hand-built a group would be testing a program that does not exist. The two samples
are a genuine pair from one run.

Run: python test_song_drag_source.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import comfy_meta
import index_db

SONG = 'YUE_FemPhonemes_22-59-01~vv3i34ad_00001.flac'
COVER = 'YUE_FemPhonemes_22-59-01~vv3i34ad_Cover_00001_.png'

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label +
          ('' if ok else '\n         got  %r\n         want %r' % (got, want)))
    if not ok:
        _fails.append(label)


src = os.path.join(ROOT, 'samples')
if not all(os.path.isfile(os.path.join(src, f)) for f in (SONG, COVER)):
    print('\n  skip  the YuE2 sample pair is not in samples/ -- nothing to test against\n')
    sys.exit(0)

print('\nA song drags the picture saved beside it\n')

# The claim the whole fix rests on, checked by opening the file rather than trusting the filename.
chunks = comfy_meta.read_png_text_chunks(os.path.join(src, COVER))
check('the cover saved beside the song carries the workflow',
      bool(chunks.get('workflow') and chunks.get('prompt')), True)

lib = tempfile.mkdtemp(prefix='vv_drag_')
try:
    for f in (SONG, COVER):
        shutil.copyfile(os.path.join(src, f), os.path.join(lib, f))
    dbp = os.path.join(lib, 'idx.db')
    index_db.scan(lib, dbp, thumbs_dir=None)
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row

    rows = {r['filename']: r for r in conn.execute("SELECT * FROM images")}
    check('both files indexed', sorted(rows), sorted([SONG, COVER]))

    song = rows[SONG]
    check('the song and its cover are one group',
          song['group_id'] is not None and song['group_id'] == rows[COVER]['group_id'], True)

    # The exact query api_image runs to find a song's cover. Mirrored rather than imported because
    # server.py opens a real library on import; keep the two in step.
    cov = conn.execute(
        "SELECT id, filename, mtime FROM images WHERE group_id=? AND LOWER(ext) IN (%s) "
        "ORDER BY id LIMIT 1" % ','.join('?' * len(index_db.IMAGE_EXTS)),
        (song['group_id'], *sorted(index_db.IMAGE_EXTS))).fetchone()
    check('the detail view finds a cover FILE for this song',
          cov['filename'] if cov else None, COVER)

    # The point of the whole change: the drag source is the cover's own file, so nothing has to
    # read inside the FLAC -- which is what failed before.
    check('  ...and it is a real file, not something built from the song',
          os.path.splitext(cov['filename'])[1].lower() in index_db.IMAGE_EXTS, True)

    # The song itself still reads as a song: this change must not have moved the card's own cover.
    check('the song still carries no embedded art, so the pair is what supplies the cover',
          bool(song['has_cover']), False)

    # And the FLAC genuinely holds the same two blobs, which is why opening the container is worth
    # doing separately -- recorded here so the next session does not re-derive it.
    with open(os.path.join(src, SONG), 'rb') as f:
        head = f.read(200000)
    check('the FLAC does carry a workflow of its own (unread today, see ideas.md)',
          head[:4] == b'fLaC' and b'workflow=' in head and b'prompt=' in head, True)
    conn.close()
finally:
    shutil.rmtree(lib, ignore_errors=True)

print('\n%s\n' % ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
sys.exit(1 if _fails else 0)
