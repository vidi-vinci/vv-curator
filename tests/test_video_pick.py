"""Which video plays for a collapsed still+video card (server.py api_search `video_id`).

VHS Video Combine, with audio wired, writes THREE files for one run: the metadata PNG, a silent
'x_00001.mp4', and the muxed 'x_00001-audio.mp4'. All three now share a group_id (index_db pairs
them — see test_groups.py). This checks the read side: the card must play the '-audio' video, and an
image set (no video) must still report no video. Mirrors the `video_id` window expression in
server.py's api_search; keep the two in sync.

Run:  python test_video_pick.py
"""
import sqlite3
import sys

import os
# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db

VLIST = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
IS_VID = f"(CASE WHEN LOWER(ext) IN ({VLIST}) THEN 1 ELSE 0 END)"

# The expression under test, copied verbatim from server.py api_search.
VIDEO_ID_SQL = (
    f"FIRST_VALUE(CASE WHEN {IS_VID}=1 THEN id END) OVER (PARTITION BY grp "
    f"ORDER BY {IS_VID} DESC, "
    f"(CASE WHEN LOWER(filename) LIKE '%-audio.%' THEN 0 ELSE 1 END), id DESC "
    f"ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS video_id")


def pick(rows):
    """rows: list of (filename, ext), all one group. Returns the chosen video filename (or None)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE images(id INTEGER PRIMARY KEY, filename TEXT, ext TEXT, grp TEXT)")
    for fn, ext in rows:
        conn.execute("INSERT INTO images(filename,ext,grp) VALUES (?,?, 'g')", (fn, ext))
    conn.commit()
    q = conn.execute(f"SELECT id, filename, {VIDEO_ID_SQL} FROM images")
    got = {r[0]: (r[1], r[2]) for r in q}
    vids = {vid for _, (_, vid) in got.items()}
    assert len(vids) == 1, f"video_id disagrees within a group: {vids}"
    vid = vids.pop()
    return None if vid is None else {i: fn for i, (fn, _) in got.items()}[vid]


CASES = [
    # (label, rows, expected chosen filename)
    ("PNG + silent + audio -> plays audio",
     [("run_21-12-26_Video_00001.png", ".png"),
      ("run_21-12-26_Video_00001-audio.mp4", ".mp4"),   # sorts BEFORE silent -> lower id (the old trap)
      ("run_21-12-26_Video_00001.mp4", ".mp4")],
     "run_21-12-26_Video_00001-audio.mp4"),
    ("PNG + silent only -> plays silent",
     [("run_Video_00001.png", ".png"), ("run_Video_00001.mp4", ".mp4")],
     "run_Video_00001.mp4"),
    ("PNG + audio only -> plays audio",
     [("run_Video_00001.png", ".png"), ("run_Video_00001-audio.mp4", ".mp4")],
     "run_Video_00001-audio.mp4"),
    ("image set (no video) -> no video_id",
     [("s_1 MAIN_00001.png", ".png"), ("s_2 DET_00001.png", ".png")],
     None),
]


def main():
    failed = 0
    for label, rows, expected in CASES:
        got = pick(rows)
        ok = got == expected
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r}")
    print(f"\n{failed} failed" if failed else "\nall checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
