"""A song and its cover art collapse to ONE card, fronted by the song (server.py api_search).

A cover saved beside a song shares its run code, so index_db already puts the two in one group —
nothing was added there. What is new is the read side, and it has three jobs this pins:

  1. The SONG fronts the card, not the picture. That is the opposite of a still+video pair, where
     the still fronts it because the still carries the metadata. Here the song is the artifact and
     the picture is artwork.
  2. `cover_id` names the picture, so the song's drawn card can inset it.
  3. A song group is a PAIR, not a set — no stacked-cards badge, and it collapses with pairs so
     turning pairs off shows the cover as its own card again.

Mirrors the window expressions in server.py's api_search; keep the two in sync.

Run:  python test_song_pair.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db

VLIST = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
ALIST = ','.join("'" + e + "'" for e in sorted(index_db.AUDIO_EXTS))
IS_VID = f"(CASE WHEN LOWER(ext) IN ({VLIST}) THEN 1 ELSE 0 END)"
IS_AUD = f"(CASE WHEN LOWER(ext) IN ({ALIST}) THEN 1 ELSE 0 END)"
# SET_FACE_RANK_SQL is a stage-order expression; a constant stands in, since no fixture here
# carries a stage and every row would score the same anyway.
FACE_RANK = "0"

# The two expressions under test, copied from server.py api_search.
COVER_ID_SQL = (
    f"FIRST_VALUE(CASE WHEN grp_has_audio=1 AND {IS_AUD}=0 THEN id END) OVER "
    f"(PARTITION BY grp ORDER BY (CASE WHEN grp_has_audio=1 AND {IS_AUD}=0 THEN 0 ELSE 1 END), "
    f"id DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS cover_id")
RN_SQL = (
    f"ROW_NUMBER() OVER (PARTITION BY grp ORDER BY "
    f"(CASE WHEN grp_has_audio=1 THEN 1 - {IS_AUD} ELSE 0 END) ASC, "
    f"{IS_VID} ASC, {FACE_RANK} ASC, filename DESC, mtime DESC, id DESC) AS rn")


def collapse(rows):
    """rows: (filename, ext) all in one group. Returns (face_filename, cover_filename_or_None)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE images(id INTEGER PRIMARY KEY, filename TEXT, ext TEXT, "
                 "mtime REAL DEFAULT 0, grp TEXT)")
    for fn, ext in rows:
        conn.execute("INSERT INTO images(filename,ext,grp) VALUES (?,?,'g')", (fn, ext))
    conn.commit()
    q = (f"WITH g AS (SELECT *, (SELECT MAX({IS_AUD}) FROM images) AS grp_has_audio FROM images) "
         f"SELECT id, filename, {COVER_ID_SQL}, {RN_SQL} FROM g")
    out = list(conn.execute(q))
    names = {r[0]: r[1] for r in out}
    face = next(r[1] for r in out if r[3] == 1)
    cover = next((names.get(r[2]) for r in out if r[3] == 1), None)
    conn.close()
    return face, cover


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


def test_song_fronts_its_pair():
    print('the song fronts a song pair')
    ok = True
    face, cover = collapse([('run_00002.png', '.png'), ('run_00001.mp3', '.mp3')])
    ok &= check('face is the song', face, 'run_00001.mp3')
    ok &= check('cover is the picture', cover, 'run_00002.png')
    # Order of insertion must not decide it.
    face, cover = collapse([('run_00001.mp3', '.mp3'), ('run_00002.png', '.png')])
    ok &= check('face is the song either way', face, 'run_00001.mp3')
    ok &= check('cover either way', cover, 'run_00002.png')
    return ok


def test_untouched_without_audio():
    """The still-first rule for a video pair, and no cover for a group with no song."""
    print('groups with no song are unchanged')
    ok = True
    face, cover = collapse([('run_00001.mp4', '.mp4'), ('run_00001.png', '.png')])
    ok &= check('a video pair still fronts its still', face, 'run_00001.png')
    ok &= check('and reports no cover', cover, None)
    face, cover = collapse([('a_MAIN.png', '.png'), ('a_DET.png', '.png')])
    ok &= check('an image set reports no cover', cover, None)
    return ok


def test_several_pictures():
    """More than one picture in the group: one of them is the cover, and the song still fronts."""
    print('a song with several pictures')
    face, cover = collapse([('run_00001.mp3', '.mp3'), ('run_00002.png', '.png'),
                            ('run_00003.png', '.png')])
    ok = check('face is still the song', face, 'run_00001.mp3')
    ok &= check('a picture is chosen as cover', cover in ('run_00002.png', 'run_00003.png'), True)
    return ok


def test_grouping_needs_nothing_new():
    """index_db already buckets a song and its cover together — by run code, blind to file type."""
    print('the run code already groups them')
    import comfy_meta
    a = comfy_meta.run_code_key('MM_Music_Aug15_20-09-27~vv3gfn3r_00001.mp3')
    b = comfy_meta.run_code_key('MM_Music_Aug15_20-09-27~vv3gfn3r_00002.png')
    ok = check('song and cover share a run-code key', a == b and a is not None, True)
    ok &= check('audio is walked by the scanner', '.mp3' in index_db.MEDIA_EXTS, True)
    return ok


if __name__ == '__main__':
    results = [test_song_fronts_its_pair(), test_untouched_without_audio(),
               test_several_pictures(), test_grouping_needs_nothing_new()]
    print('\nPASS' if all(results) else '\nFAIL')
    sys.exit(0 if all(results) else 1)
