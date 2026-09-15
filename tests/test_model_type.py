"""Regression test for MODEL TYPE — the family a checkpoint belongs to.

Run: python tests/test_model_type.py

Why this test exists. Model type used to be computed in SQL on every facet and every filter: the
top-level FOLDER of the model path, and nothing else. That left **14,185 of the author's images under
"(none)"** — checkpoints sitting loose in the checkpoints directory, whose family is named in the
FILENAME (`flux_1Dev`, `duchaitenPonyReal_ponyRealV11Fix`) rather than in a folder.

It is now derived once at scan and stored, which makes the queries cheaper AND lets the rule be
smarter than SQL can be. Three properties carry the whole thing, and each fails silently:

  1. **The vocabulary is the user's own folders**, not a list I invented. A family they never use
     cannot be conjured; a family they do use costs nothing to learn.
  2. **Matching is boundary-aware.** A bare substring test is where "confidently wrong" lives —
     `duchaitenPonyReal` must be Pony (camelCase hump) while `influxCapacitor` must be nothing.
  3. **One spelling per family.** `krea 2` and `Krea` are the same family, and two spellings would
     be two dropdown rows — the exact thing this was built to collapse.

The model names below are the author's real ones, taken from the (none) list he screenshotted.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db

BS = chr(92)
rows = [
    # (model path as stored, expected family)
    (BS.join(['Illustrious', 'someIllustriousThing.safetensors']), 'Illustrious'),
    (BS.join(['Pony', 'realDream_sdxlPony12.safetensors']), 'Pony'),
    (BS.join(['SDXL', 'CHEYENNE_v16.safetensors']), 'SDXL'),
    (BS.join(['krea 2', 'kreaModel.safetensors']), 'krea'),        # version merge
    (BS.join(['Krea', 'otherKrea.safetensors']), 'krea'),          # same family, one spelling
    # the loose ones, verbatim from the screenshot
    ('flux_1Dev.safetensors', 'flux'),
    ('duchaitenPonyReal_ponyRealV11Fix.safetensors', 'Pony'),
    ('duchaitenPonyRealV10.28zt.safetensors', 'Pony'),
    ('duchaitenPonyNoScore.6kXk.safetensors', 'Pony'),
    ('ponyFaetality_v11.safetensors', 'Pony'),
    ('aniversePonyXL_v10.safetensors', 'Pony'),
    # things that must NOT be classified
    ('influxCapacitor.safetensors', ''),        # 'flux' mid-word, no boundary
    ('somethingUnknown_v3.safetensors', ''),
]

TMP = tempfile.mkdtemp()
db = os.path.join(TMP, 't.db')
# A PLAIN connection, deliberately: index_db never sets a row_factory, and a test that set one
# passed while every real scan raised TypeError at the very end — after the inserts, so the library
# looked indexed while model_type stayed empty. Match production, not convenience.
conn = sqlite3.connect(db)
conn.executescript(index_db.SCHEMA)
for i, (path, _) in enumerate(rows, start=1):
    name = os.path.splitext(os.path.basename(path.replace(BS, '/')))[0]
    conn.execute("INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size,"
                 " model, model_name, has_meta, indexed_at) VALUES(?,?,?,?,?,?,0,0,?,?,1,0)",
                 (i, '/x/%d.png' % i, '%d.png' % i, '.', '%d.png' % i, '.png', path, name))
conn.commit()

n = index_db.recompute_model_types(conn)
print('classified %d rows\n' % n)
ok = True
for i, (path, want) in enumerate(rows, start=1):
    got = conn.execute("SELECT model_type FROM images WHERE id=?", (i,)).fetchone()[0]
    hit = (got or '').lower() == want.lower()
    ok = ok and hit
    print("  %-4s %-46s -> %-14r (want %r)" % ('ok' if hit else 'FAIL', path, got, want))

# ONE SPELLING PER FAMILY. Grouping is case-insensitive, so two spellings would still collapse in
# the dropdown — but the label shown would depend on row order. Assert the stored strings match.
krea = [conn.execute("SELECT model_type FROM images WHERE id=?", (i,)).fetchone()[0]
        for i, (p, w) in enumerate(rows, start=1) if w == 'krea']
same = len(set(krea)) == 1
ok = ok and same
print("\n  %-4s 'krea 2' and 'Krea' store one spelling -> %r" % ('ok' if same else 'FAIL', krea))

# THE VOCABULARY IS THE USER'S. A family they neither folder nor have a built-in hint for must NOT
# be invented — this is what keeps the rule from being confidently wrong.
made_up = index_db._family_in_name('someBrandNewArchitecture_v2.safetensors', ['pony', 'flux'])
ok = ok and not made_up
print("  %-4s an unknown family is not invented -> %r" % ('ok' if not made_up else 'FAIL', made_up))

# The version strip must not eat a family whose name ENDS in digits: SD15 and SDXL are families,
# not versions of "SD". Only a separate trailing segment is a version.
keeps = index_db.normalize_model_type('SD15') == 'SD15' and \
    index_db.normalize_model_type('SDXL') == 'SDXL' and \
    index_db.normalize_model_type('krea 2') == 'krea' and \
    index_db.normalize_model_type('Flux v1.5') == 'Flux'
ok = ok and keeps
print("  %-4s a version is stripped, a family ending in digits is not" % ('ok' if keeps else 'FAIL'))

# ---- a GLUED version folds, but only into a family this library already has -------------------
# The author, 2026-09-10: "consolidate models where possible so that, for example, Krea and krea2 become
# the same." `krea 2` was already handled above (a separated version); `krea2` was not, and the
# obvious fix -- strip trailing digits -- turns SD15 into SD and merges two families that are not
# one. So the fold asks the vocabulary first. BOTH halves are asserted here, because a rule that
# only ever merges looks correct on the case you are thinking about and is wrong on the next one.
def _types(paths):
    """model_type for each path, through the real recompute against a throwaway library."""
    c = sqlite3.connect(':memory:')
    c.executescript(index_db.SCHEMA)
    for i, path in enumerate(paths, start=1):
        nm = os.path.splitext(os.path.basename(path))[0]
        c.execute("INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, size,"
                  " model, model_name, has_meta, indexed_at) VALUES(?,?,?,?,?,?,0,0,?,?,1,0)",
                  (i, '/x/%d.png' % i, '%d.png' % i, '.', '%d.png' % i, '.png', path, nm))
    c.commit()
    index_db.recompute_model_types(c)
    out = [c.execute("SELECT COALESCE(model_type,'') FROM images WHERE id=?", (i,)).fetchone()[0]
           for i in range(1, len(paths) + 1)]
    c.close()
    return out

print()
folds = _types(['Krea/a.safetensors', 'Krea/b.safetensors', 'krea2/c.safetensors'])
hit = len(set(folds)) == 1
ok = ok and hit
print("  %-4s krea2 folds into the Krea folder beside it -> %r" % ('ok' if hit else 'FAIL', folds))

# THE GUARD, and the reason this is not just a digit strip. Nobody has an "SD" folder here, so
# there is no family for SD15 to fold into and it must stay whole.
guard = _types(['SD15/a.safetensors', 'SDXL/b.safetensors'])
hit = guard[0].lower() == 'sd15' and guard[1].lower() == 'sdxl'
ok = ok and hit
print("  %-4s SD15 stays SD15 when no SD family exists -> %r" % ('ok' if hit else 'FAIL', guard))

# ...AND IT STILL DOES NOT FOLD WITH AN SD FOLDER PRESENT. This assertion is inverted from the one
# that first shipped, on purpose: "SD" is a prefix shared by architectures that are not versions of
# each other, so it is held out of the fold by name (_FOLD_NEVER). An SD folder is not permission to
# pour SD15 and SDXL into it.
told = _types(['SD/a.safetensors', 'SD15/b.safetensors', 'SDXL/c.safetensors'])
hit = told[0].lower() == 'sd' and told[1].lower() == 'sd15' and told[2].lower() == 'sdxl'
ok = ok and hit
print("  %-4s nothing folds into a bare SD, folder or not -> %r" % ('ok' if hit else 'FAIL', told))

# AN XL SUFFIX FOLDS LIKE A TRAILING NUMBER. The author, 2026-09-10, asked for this after the digit fold
# landed: "what about Pony and PonyXL?" They are one family; SD and SDXL are not, which is the whole
# reason the hold-out above exists rather than a blanket rule either way.
pony = _types(['Pony/a.safetensors', 'PonyXL/b.safetensors'])
hit = len(set(pony)) == 1
ok = ok and hit
print("  %-4s PonyXL folds into Pony -> %r" % ('ok' if hit else 'FAIL', pony))

# Both suffixes at once must reach the same place as either alone.
both = index_db._fold_stem('ponyxl2')
hit = both == 'pony'
ok = ok and hit
print("  %-4s a stem strips repeatedly: ponyxl2 -> pony -> %r" % ('ok' if hit else 'FAIL', both))

# A stem that exists only as a built-in HINT has no spelling of the user's, and borrowing the
# hint's lowercase put "qwen" in a column of capitalised folder names. The folder folding IN
# supplies the spelling instead.
spell = _types(['Qwen3/a.safetensors'])
hit = spell[0] == 'Qwen'
ok = ok and hit
print("  %-4s a hint-only stem takes the user's capitalisation -> %r" % ('ok' if hit else 'FAIL', spell))

print('\n' + ('all passed' if ok else 'FAILED'))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
