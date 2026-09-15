"""Regression test for WHEN a scan reports its progress (index_db.scan).

Run:  python test_scan_progress.py

The bug this pins: the scan reported every 100 files, and then went silent for the whole back half
of its work -- the prune, the group pass, the model-type pass, PRAGMA optimize. It did report the
final count, but only after all of that, at the same instant the job flipped to not-running, so the
bar came down before that number could ever be painted. What the user watched instead was the last
multiple of 100 (6,200 of 6,246 files -> a bar frozen on 99%) sitting there while the app was
plainly still busy.

So the rule under test is an ORDERING, not a value: **by the time the silent passes begin, progress
must already read 100%.** Testing only the final report would pass on the broken code, because the
broken code did eventually report 100% -- too late to be seen. That is exactly why this class of bug
got past 31 other tests.

Three files is enough, and deliberately fewer than the 100-file reporting interval: with the loop's
periodic tick unable to fire at all, the end-of-loop report is the only thing that can satisfy this.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

import index_db

failures = 0


def check(name, cond, detail=''):
    global failures
    if cond:
        print('  ok    ' + name)
    else:
        print('  FAIL  ' + name + ('\n          ' + detail if detail else ''))
        failures += 1


print('\nScan progress reporting\n')

tmp = tempfile.mkdtemp(prefix='vv_scanprog_')
try:
    for i in range(3):
        Image.new('RGB', (4, 4), (i * 40, 0, 0)).save(os.path.join(tmp, f'shot_{i}.png'))
    db = os.path.join(tmp, 'library.db')

    reports = []          # every (seen, total) the scan handed the UI, in order
    at_passes = []        # what the last report read as the moment each silent pass began
    totals = []

    real_groups = index_db.recompute_groups
    real_types = index_db.recompute_model_types

    def spy_groups(conn, root_id=None):
        at_passes.append(('recompute_groups', reports[-1] if reports else None))
        return real_groups(conn, root_id)

    def spy_types(conn, root_id=None):
        at_passes.append(('recompute_model_types', reports[-1] if reports else None))
        return real_types(conn, root_id)

    index_db.recompute_groups = spy_groups
    index_db.recompute_model_types = spy_types
    try:
        stats = index_db.scan(tmp, db, thumbs_dir=None,
                              progress=lambda n, t, a, u, s: reports.append((n, t)),
                              count_cb=totals.append, root_id='r1')
    finally:
        index_db.recompute_groups = real_groups
        index_db.recompute_model_types = real_types

    check('the walk counts every file', totals == [3], f'count_cb got {totals}')
    check('the scan indexed all three', stats['added'] == 3, f"added={stats['added']}")
    check('progress was reported at all', bool(reports), 'no progress callback ever fired')

    # THE ONE THAT MATTERS. Both silent passes must find the bar already at 100%.
    check('both silent passes ran', len(at_passes) == 2, f'{at_passes}')
    for name, seen_at in at_passes:
        check(f'progress reads 100% before {name}', seen_at == (3, 3),
              f'bar read {seen_at} when {name} started -- it will sit there, unmoving, '
              f'for however long that pass takes')

    # And the scan still leaves the bar on 100% when it ends.
    check('the last report is 100%', reports[-1] == (3, 3), f'last report {reports[-1]}')
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
sys.exit(1 if failures else 0)
