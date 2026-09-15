"""Which member of a collapsed image SET fronts the card (server.py api_search's `rn` ordering).

The bug this exists to stop coming back: the face was ordered by favorite, then Quality score, then
mtime, with a filename tiebreak whose comment claimed it "lands a same-gen set on its highest role
(...3 REFINE)". It never did — filename sat AFTER mtime, and mtime is a float with sub-second
precision, so it essentially never ties and the filename rule was unreachable. What actually chose
the face was "whichever member was written last", or once anything carried a Quality score,
"whichever scored". Sets therefore fronted their ROUGHEST member, and it reported as
"we're showing the low-res image as the card cover".

So the load-bearing claims are about PRECEDENCE, not about any one ordering:

  * the most finished stage wins, from the saver's stamp when there is one;
  * legacy filename-role sets (no stamp) reach the same answer;
  * a newer mtime does NOT beat a later stage — that is the exact regression;
  * a Quality score does NOT beat a later stage — an automatic number must never silently
    change which image represents a set;
  * nor does a favorite (decided 2026-08-03: stage always wins);
  * a still still beats a video for the face, since the face carries the metadata.

SET_FACE_RANK_SQL is IMPORTED from server.py rather than copied, so the two cannot drift.

Run:  python test_set_face.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

# Keep importing server.py from touching a real config/data dir.
# THE SYSTEM TEMP, NOT THE PROJECT FOLDER — 20 places said `dir=here`/`dir=BASE`, and every one
# of them was wrong for the same reason. Each cleans up in a finally with ignore_errors=True, and
# on Windows the server thread still holds library.db open when that runs, so the removal fails
# SILENTLY and the husk stays. 25 of them, 503MB, had accumulated in the master folder before the author
# spotted them. A test's mess belongs where the OS already sweeps up.
_tmp = tempfile.mkdtemp(prefix='vv_face_')
os.environ['CV_CONFIG'] = os.path.join(_tmp, 'config.json')
os.environ['CV_DATA'] = os.path.join(_tmp, 'data')

# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db                                    # noqa: E402
import server                                      # noqa: E402

VLIST = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
IS_VID = f"(CASE WHEN LOWER(ext) IN ({VLIST}) THEN 1 ELSE 0 END)"

# The surrounding ORDER BY mirrors api_search; the subtle part (SET_FACE_RANK_SQL) is imported.
RN_SQL = (f"ROW_NUMBER() OVER (PARTITION BY grp ORDER BY {IS_VID} ASC, "
          f"{server.SET_FACE_RANK_SQL} ASC, filename DESC, fav DESC, "
          f"reward IS NULL, reward DESC, mtime DESC, id DESC)")

_fails = []


def face(rows):
    """rows: (filename, ext, set_stage, mtime, reward, fav) for one group -> the chosen filename."""
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE k(id INTEGER PRIMARY KEY, filename TEXT, ext TEXT, set_stage TEXT,"
                 " mtime REAL, reward REAL, fav INT, grp TEXT)")
    for fn, ext, stage, mtime, reward, fav in rows:
        conn.execute("INSERT INTO k(filename,ext,set_stage,mtime,reward,fav,grp)"
                     " VALUES (?,?,?,?,?,?,'g')", (fn, ext, stage, mtime, reward, fav))
    conn.commit()
    got = conn.execute(f"SELECT filename, {RN_SQL} AS rn FROM k").fetchall()
    conn.close()
    return next(fn for fn, rn in got if rn == 1)


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + f'{label}   -> {got}' + ('' if ok else f'  (wanted {want})'))
    if not ok:
        _fails.append(label)


def main():
    print('stamped sets - the saver tells us the stage')
    check('raw/detail/refine picks refine',
          face([('a_raw.png', '.png', 'Raw', 100.0, None, 0),
                ('a_det.png', '.png', 'Detail', 101.0, None, 0),
                ('a_ref.png', '.png', 'Refine', 102.0, None, 0)]), 'a_ref.png')
    check('an upscale beats a refine',
          face([('b_ref.png', '.png', 'Refine', 100.0, None, 0),
                ('b_up.png', '.png', 'Upscale', 101.0, None, 0)]), 'b_up.png')
    check('final beats everything',
          face([('c_up.png', '.png', 'Upscale', 100.0, None, 0),
                ('c_fin.png', '.png', 'Final', 101.0, None, 0),
                ('c_raw.png', '.png', 'Raw', 102.0, None, 0)]), 'c_fin.png')

    print('\nlegacy filename-role sets - no stamp, same answer')
    check('1 MAIN / 2 DET / 3 REFINE picks REFINE',
          face([('X_1 MAIN_00001.png', '.png', None, 100.0, None, 0),
                ('X_2 DET_00001.png', '.png', None, 101.0, None, 0),
                ('X_3 REFINE_00001.png', '.png', None, 102.0, None, 0)]), 'X_3 REFINE_00001.png')
    check('MAIN vs DET picks DET (not the alphabetically later MAIN)',
          face([('Y_1 MAIN_00001.png', '.png', None, 100.0, None, 0),
                ('Y_2 DET_00001.png', '.png', None, 101.0, None, 0)]), 'Y_2 DET_00001.png')

    print('\nTHE REGRESSION: later stage must beat newer file, score and star')
    check('a NEWER raw does not steal the face',
          face([('d_ref.png', '.png', 'Refine', 100.0, None, 0),
                ('d_raw.png', '.png', 'Raw', 999.0, None, 0)]), 'd_ref.png')
    check('a Quality score on the raw does not steal the face',
          face([('e_ref.png', '.png', 'Refine', 100.0, None, 0),
                ('e_raw.png', '.png', 'Raw', 101.0, 0.99, 0)]), 'e_ref.png')
    check('a FAVORITE on the raw does not steal the face either',
          face([('f_ref.png', '.png', 'Refine', 100.0, None, 0),
                ('f_raw.png', '.png', 'Raw', 101.0, None, 1)]), 'f_ref.png')
    check('all three against it at once, still the refine',
          face([('g_ref.png', '.png', 'Refine', 100.0, None, 0),
                ('g_raw.png', '.png', 'Raw', 999.0, 0.99, 1)]), 'g_ref.png')

    print('\nthe rules that must survive unchanged')
    check('a still still beats a video for the face',
          face([('h_ref.mp4', '.mp4', 'Refine', 999.0, None, 0),
                ('h_raw.png', '.png', 'Raw', 100.0, None, 0)]), 'h_raw.png')
    check('unknown stages fall back to filename order',
          face([('i_1 alpha.png', '.png', None, 100.0, None, 0),
                ('i_3 zulu.png', '.png', None, 101.0, None, 0)]), 'i_3 zulu.png')
    check('within ONE stage, a favorite still decides',
          face([('j_a.png', '.png', 'Refine', 100.0, None, 0),
                ('j_b.png', '.png', 'Refine', 101.0, None, 1)]), 'j_b.png')

    print()
    if _fails:
        print(f'{len(_fails)} FAILED:')
        for f in _fails:
            print('  - ' + f)
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
