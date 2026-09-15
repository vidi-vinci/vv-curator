"""Regression test: the LEAN count query returns exactly what the FAT one did.

Run:  python test_count_cols.py

Counting cards and showing them are different questions, and the count was being served by the
showing query: `_card_count` built the full ~20-column display list — including three correlated
subqueries per row (label, fav, hidden), the quality join and `grp_has_video_full`, itself a
correlated EXISTS against `images` — and dragged all of it across the whole library to answer "how
many". At 100k rows that is ~300k subquery executions per count, and a refresh runs two.

The lean form selects only `id, ext, group_id` — the three columns `grp` is derived from — and drops
`grp_has_video_full`, which the grouping never reads.

**The saving is only free if the answer is bit-identical, and that is the entire risk.** A wrong
count is far worse than a slow one: it is the denominator in "179 of 1,876", the `⊘ N` chip and the
Hidden held-back number, and nothing on screen would look broken while it lied. So this runs both
forms side by side over a matrix of filter and toggle combinations and asserts they agree exactly.

The combinations matter more than the row count. `grp` branches on group kind (video pair / song
pair / image set) and on the two collapse toggles, and the counts branch again on sets-only, so a
fixture with one kind of group and one toggle setting would pass while proving almost nothing.
"""
import itertools
import os
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db

failures = 0


def check(name, cond, detail=''):
    global failures
    if cond:
        print('  ok    ' + name)
    else:
        print('  FAIL  ' + name + ('\n          ' + detail if detail else ''))
        failures += 1


print('\nLean card-count agrees with the fat one\n')

VLIST = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
ALIST = ','.join("'" + e + "'" for e in sorted(index_db.AUDIO_EXTS))
IS_VID = f"(CASE WHEN LOWER(ext) IN ({VLIST}) THEN 1 ELSE 0 END)"
IS_AUD = f"(CASE WHEN LOWER(ext) IN ({ALIST}) THEN 1 ELSE 0 END)"

FAT_COLS = (
    "i.id, i.filename, i.folder, i.width, i.height, i.model_name, i.root_id, "
    "i.thumb, i.has_meta, i.mtime, i.ext, i.motion, i.group_id, i.size, "
    "qs.reward AS reward, "
    "(SELECT SUBSTR(tg.tag,7) FROM tags tg WHERE tg.image_id=i.id AND tg.source='user' "
    "AND tg.tag LIKE 'label:%' LIMIT 1) AS label, "
    "i.note AS note, (i.note IS NOT NULL AND i.note != '') AS has_note, "
    "i.shuffle_key, i.set_stage, "
    "EXISTS(SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.source='fav') AS fav, "
    "EXISTS(SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.source='hide') AS hidden")
LEAN_COLS = "i.id, i.ext, i.group_id"


def cte(group_on, sets_on, full):
    """The grouping CTE. `full` includes grp_has_video_full — the correlated EXISTS the lean form
    drops. Everything else is identical between the two by construction."""
    hv_full = (f"(CASE WHEN group_id IS NOT NULL AND EXISTS(SELECT 1 FROM images vv "
               f"WHERE vv.group_id=filtered.group_id AND LOWER(vv.ext) IN ({VLIST})) "
               f"THEN 1 ELSE 0 END) AS grp_has_video_full, ") if full else ""
    return (
        f"g AS (SELECT *, (CASE WHEN group_id IS NOT NULL "
        f"THEN MAX({IS_VID}) OVER (PARTITION BY group_id) ELSE 0 END) AS grp_has_video, "
        f"{hv_full}"
        f"(CASE WHEN group_id IS NOT NULL "
        f"THEN MAX({IS_AUD}) OVER (PARTITION BY group_id) ELSE 0 END) AS grp_has_audio "
        f"FROM filtered), "
        f"k AS (SELECT *, CASE "
        f"WHEN group_id IS NOT NULL AND grp_has_audio=1 AND {1 if group_on else 0}=1 THEN group_id "
        f"WHEN group_id IS NOT NULL AND grp_has_video=1 AND {1 if group_on else 0}=1 THEN group_id "
        f"WHEN group_id IS NOT NULL AND grp_has_video=0 AND grp_has_audio=0 "
        f"AND {1 if sets_on else 0}=1 THEN group_id "
        f"ELSE 'i'||id END AS grp FROM g)")


def agg_count(conn, group_on, sets_on, sets_only, where, params, files=False):
    """The shipped form: no window functions at all. A card count decomposes into rows in no
    group, groups that collapse, and rows in groups that don't -- and only the middle two need to
    look at groups. This is what server.api_search._count_sql builds."""
    c = (f"((ha=1 AND {1 if group_on else 0}=1) OR (hv=1 AND {1 if group_on else 0}=1) "
         f"OR (hv=0 AND ha=0 AND {1 if sets_on else 0}=1))")
    gg = (f"gg AS (SELECT group_id, MAX({IS_VID}) hv, MAX({IS_AUD}) ha, COUNT(*) n "
          f"FROM filtered WHERE group_id IS NOT NULL GROUP BY group_id)")
    b = f"SELECT {LEAN_COLS} FROM images i LEFT JOIN quality qs ON qs.image_id = i.id {where}"
    if sets_only:
        agg = "COALESCE(SUM(n),0)" if files else "COUNT(*)"
        q = f"WITH filtered AS ({b}), {gg} SELECT {agg} c FROM gg WHERE {c} AND n > 1"
    else:
        q = (f"WITH filtered AS ({b}), {gg} "
             f"SELECT (SELECT COUNT(*) FROM filtered WHERE group_id IS NULL) "
             f"+ (SELECT COUNT(*) FROM gg WHERE {c}) "
             f"+ (SELECT COALESCE(SUM(n),0) FROM gg WHERE NOT {c}) AS c")
    return conn.execute(q, params).fetchone()[0]


def count(conn, cols, group_on, sets_on, sets_only, where, params, full):
    having = "HAVING COUNT(*) > 1" if sets_only else ""
    b = f"SELECT {cols} FROM images i LEFT JOIN quality qs ON qs.image_id = i.id {where}"
    return conn.execute(f"WITH filtered AS ({b}), {cte(group_on, sets_on, full)} "
                        f"SELECT COUNT(*) c FROM (SELECT grp FROM k GROUP BY grp {having})",
                        params).fetchone()[0]


def count_files_old(conn, group_on, sets_on, where, params):
    """The previous sets-only FILE tally: rows living in groups of more than one."""
    b = f"SELECT {LEAN_COLS} FROM images i LEFT JOIN quality qs ON qs.image_id = i.id {where}"
    return conn.execute(
        f"WITH filtered AS ({b}), {cte(group_on, sets_on, True)} "
        f"SELECT COUNT(*) c FROM k WHERE grp IN "
        f"(SELECT grp FROM k GROUP BY grp HAVING COUNT(*) > 1)", params).fetchone()[0]


# ---- a fixture with every group KIND, because grp branches on all of them --------------------
tmp = tempfile.mkdtemp(prefix='vv_countcols_')
db = os.path.join(tmp, 'library.db')
conn = index_db.connect(db)
rnd = random.Random(11)
rows, n = [], 0


def add(fn, ext, grp, w=1024, h=1024, root='r1'):
    global n
    n += 1
    rows.append((f'{tmp}/{fn}', fn, 'gen', fn, ext, 1000.0 + n, 1000, w, h, grp, None, root, 0))


for i in range(60):                                    # lone stills
    add(f'lone_{i:03d}.png', '.png', None, w=(2400 if i % 7 == 0 else 1024))
for i in range(20):                                    # still + video PAIRS
    add(f'pair_{i:03d}.png', '.png', f'p{i}')
    add(f'pair_{i:03d}.mp4', '.mp4', f'p{i}')
for i in range(15):                                    # image SETS of three
    for st in ('Raw', 'Detail', 'Refine'):
        add(f'set_{i:03d}_{st}.png', '.png', f's{i}')
for i in range(10):                                    # SONG pairs (audio + cover)
    add(f'song_{i:03d}.mp3', '.mp3', f'a{i}')
    add(f'song_{i:03d}.png', '.png', f'a{i}')
for i in range(8):                                     # a group whose only VIDEO is hidden later
    add(f'vh_{i:03d}.png', '.png', f'v{i}')
    add(f'vh_{i:03d}.mp4', '.mp4', f'v{i}')
for i in range(12):                                    # a second root
    add(f'other_{i:03d}.png', '.png', None, root='r2')

conn.executemany(
    "INSERT INTO images(path, rel_path, folder, filename, ext, mtime, size, width, height,"
    " group_id, set_stage, root_id, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
ids = {r[0]: r[1] for r in conn.execute("SELECT filename, id FROM images")}
# curation, so the fat form's three subqueries have something to find — and specifically hide the
# VIDEO of some pairs, the case where including/excluding a row changes what its GROUP is.
conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES(?,?,'hide')",
                 [(ids[f'vh_{i:03d}.mp4'], 'hide') for i in range(8)])
conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES(?,?,'fav')",
                 [(ids[f'lone_{i:03d}.png'], 'fav') for i in range(0, 60, 5)])
conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES(?,?,'user')",
                 [(ids[f'set_{i:03d}_Raw.png'], 'label:publish') for i in range(0, 15, 3)])
conn.commit()

# Assert the KINDS are present rather than a row total: the row total says nothing about whether
# `grp`'s branches are all exercised, and a threshold on it is a number to maintain for no gain.
kinds = {
    'lone stills': "group_id IS NULL",
    'video pairs': f"group_id LIKE 'p%' AND LOWER(ext) IN ({VLIST})",
    'image sets': "group_id LIKE 's%'",
    'song pairs': f"group_id LIKE 'a%' AND LOWER(ext) IN ({ALIST})",
    'a hidden-video group': "group_id LIKE 'v%'",
    'a second root': "root_id = 'r2'",
}
missing = [k for k, w in kinds.items()
           if not conn.execute(f"SELECT COUNT(*) FROM images WHERE {w}").fetchone()[0]]
check('the fixture exercises every group kind', not missing,
      'absent: ' + ', '.join(missing) if missing else '')

HIDDEN = "i.id NOT IN (SELECT image_id FROM tags WHERE source='hide')"
WHERES = [
    ('no filter',            "", []),
    ('hide-large + hidden',  f"WHERE NOT (COALESCE(i.width,0) > ? OR COALESCE(i.height,0) > ?) "
                             f"AND {HIDDEN}", [2000, 2000]),
    ('peek (hidden shown)',  "WHERE NOT (COALESCE(i.width,0) > ? OR COALESCE(i.height,0) > ?)",
                             [2000, 2000]),
    ('one root',             "WHERE i.root_id = ?", ['r1']),
    ('images only',          f"WHERE LOWER(i.ext) NOT IN ({VLIST}) AND {HIDDEN}", []),
    ('favorites only',       "WHERE EXISTS(SELECT 1 FROM tags t WHERE t.image_id=i.id "
                             "AND t.source='fav')", []),
    ('matches nothing',      "WHERE i.folder = ?", ['nope']),
]
TOGGLES = list(itertools.product((True, False), (True, False), (True, False)))

mismatches, compared = [], 0
for wname, where, params in WHERES:
    for group_on, sets_on, sets_only in TOGGLES:
        fat = count(conn, FAT_COLS, group_on, sets_on, sets_only, where, params, True)
        lean = count(conn, LEAN_COLS, group_on, sets_on, sets_only, where, params, False)
        agg = agg_count(conn, group_on, sets_on, sets_only, where, params)
        compared += 1
        if not (fat == lean == agg):
            mismatches.append(f'{wname} · pairs={group_on} sets={sets_on} only={sets_only}: '
                              f'fat={fat} lean={lean} aggregate={agg}')

check(f'all {compared} filter/toggle combinations agree (fat = lean = aggregate)', not mismatches,
      '\n          '.join(mismatches))

# A count that is always zero would agree trivially. Prove the matrix actually moves.
spread = {agg_count(conn, g, s, o, w, p)
          for (_, w, p) in WHERES for g, s, o in TOGGLES}
check('the matrix produces a range of answers, not one constant', len(spread) >= 6,
      f'only {len(spread)} distinct counts: {sorted(spread)}')

files_mismatch = []
for wname, where, params in WHERES:
    for group_on, sets_on in itertools.product((True, False), (True, False)):
        old = count_files_old(conn, group_on, sets_on, where, params)
        new = agg_count(conn, group_on, sets_on, True, where, params, files=True)
        if old != new:
            files_mismatch.append(f'{wname} · pairs={group_on} sets={sets_on}: '
                                  f'old={old} new={new}')
check('the sets-only FILE tally agrees too', not files_mismatch,
      '\n          '.join(files_mismatch))

conn.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)

print()
sys.exit(1 if failures else 0)
