"""Which member of a collapsed image SET fronts the card (server.py SET_FACE_RANK_SQL).

The expression had no test until 2026-09-07, when custom stages were made real: a stage the viewer
does not know now sorts FIRST in the pane order (index_db._stage_rank), which means it must sort
LAST here -- the first stage of a pipeline is its roughest image, and a set card must never
advertise its roughest member. The two look like they disagree only because this one is inverted,
so they are pinned together at the bottom of this file.

Also pinned: the filename fallback fires only when set_stage is BLANK. Before, a custom stage called
"Detail2" was scored as a Detail by the bare word "det" inside it -- a name the viewer does not know
deciding a rank as if it did.

The SQL is IMPORTED, not copied, so it cannot drift from the running app.

Run:  python test_set_face_rank.py
"""
import os
import sqlite3
import sys

# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db
import server

# (set_stage, filename, expected rank, why). 0 = fronts the card, 5 = never does.
CASES = [
    ('Final',   'x_Final_1.png',    0, "known stage: the most finished image fronts the card"),
    ('Upscale', 'x_Upscale_1.png',  1, "known stage"),
    ('Refine',  'x_Refine_1.png',   2, "known stage"),
    ('Detail',  'x_Detail_1.png',   3, "known stage"),
    ('Raw',     'x_Raw_1.png',      4, "known stage"),
    ('First',   'x_First_1.png',    5, "a CUSTOM stage is the first stage, so never the face"),
    ('Detail2', 'x_Detail2_1.png',  5, "a custom stage is not guessed at by a word inside it"),
    ('main',    'x_main_1.png',     4, "a legacy name in the column reads as its modern stage"),
    ('det',     'x_det_1.png',      3, "a legacy name in the column reads as its modern stage"),
    ('',        'X_3 REFINE_1.png', 2, "blank stage: the filename fallback answers"),
    ('',        'X_2 DET_1.png',    3, "blank stage: the filename fallback answers"),
    ('',        'X_1 MAIN_1.png',   4, "blank stage: the filename fallback answers"),
    (None,      'plain_1.png',      5, "nothing known at all"),
]


def ranks():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE images(id INTEGER PRIMARY KEY, set_stage TEXT, filename TEXT)")
    for stage, fn, _, _ in CASES:
        conn.execute("INSERT INTO images(set_stage,filename) VALUES (?,?)", (stage, fn))
    conn.commit()
    q = "SELECT set_stage, filename, %s FROM images ORDER BY id" % server.SET_FACE_RANK_SQL
    return [r[2] for r in conn.execute(q)]


def main():
    checks = []
    for (stage, fn, want, why), got in zip(CASES, ranks()):
        checks.append(("%r -> %s (%s)" % (stage, want, why), got == want, got))

    # The two halves of one vocabulary. Every stage that fronts the card EARLIER than another must
    # sit LATER in the pane order, custom stages included -- that inversion is the whole contract.
    face = {'final': 0, 'upscale': 1, 'refine': 2, 'detail': 3, 'raw': 4, 'First': 5}
    pane = sorted(face, key=index_db._stage_rank)
    checks.append(("pane order is the face order reversed",
                   pane == sorted(face, key=lambda s: -face[s]), pane))

    bad = [c for c in checks if not c[1]]
    for name, ok, got in checks:
        print("%-5s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "  got %r" % (got,)))
    print("\n%s" % ("all checks passed" if not bad else "%d FAILED" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
