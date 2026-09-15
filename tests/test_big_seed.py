"""test_big_seed.py -- a seed too large for SQLite is kept, exactly, and does not end the scan.

Run: python tests/test_big_seed.py

The author hit this as a bare error toast on a library refresh: **"Python int too large to convert to
SQLite INTEGER"**, and the refresh stopped there.

SQLite's INTEGER is signed 64-bit and tops out at 2**63-1 = 9223372036854775807. A ComfyUI seed
does not: nodes randomise across the full UNSIGNED 64-bit range, so a seed above that is ordinary
rather than exotic. Binding one raises OverflowError, and the scan loop has no per-file guard, so
one such file ended the whole library's refresh.

**The interesting half is that it must be kept EXACTLY.** The two cheap fixes both destroy it:

  * clamping (or wrapping) it produces a number that looks usable and cannot reproduce the
    generation, which is worse than storing nothing at all;
  * putting the digits in the INTEGER column as text does not work either -- SQLite gives an
    INTEGER-affinity column a REAL when the text will not fit, so 18446744073709551615 is stored as
    1.8446744073709552e+19 and reads back wrong by 1. That one passes a "did it scan?" test and
    fails silently forever after, which is why the assertion here is on the DIGITS.

So the oversized seed goes to `gp_seed_s` (TEXT) and `gp_seed` is left null; at most one of the two
is ever set. This runs the REAL scan against a real PNG carrying a real workflow, because the whole
failure was in the bind -- a test that called comfy_meta alone would have passed throughout.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


INT64_MAX = 2**63 - 1
UINT64_MAX = 2**64 - 1


def graph(seed):
    return {
        '1': {'class_type': 'CheckpointLoaderSimple',
              'inputs': {'ckpt_name': 'sdxl/base.safetensors'}},
        '2': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 64, 'height': 64}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a test image'}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
        '5': {'class_type': 'KSampler',
              'inputs': {'steps': 20, 'cfg': 7.0, 'sampler_name': 'euler',
                         'scheduler': 'karras', 'seed': seed, 'model': ['1', 0],
                         'positive': ['3', 0], 'negative': ['4', 0],
                         'latent_image': ['2', 0]}},
    }


def png(path, seed):
    """A real PNG carrying a real ComfyUI `prompt` chunk -- what the scan actually reads."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text('prompt', json.dumps(graph(seed)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (64, 64), (30, 40, 50)).save(path, pnginfo=info)


print('\nA seed too large for SQLite survives the scan, exactly\n')

root = tempfile.mkdtemp(prefix='vv_bigseed_')
db = os.path.join(root, 'lib.db')

# One ordinary seed, one at the exact boundary, and two past it -- including the largest a 64-bit
# unsigned randomiser can produce. The boundary case is here because an off-by-one in the range
# test would push a perfectly storable seed into the text column, which no user would ever notice.
cases = {
    'small.png': 12345,
    'boundary.png': INT64_MAX,
    'over.png': INT64_MAX + 1,
    'max.png': UINT64_MAX,
}
for name, seed in cases.items():
    png(os.path.join(root, 'imgs', name), seed)

stats = index_db.scan(root, db, thumbs_dir=None)

check('the scan finished rather than dying on the oversized seed', bool(stats))
check('every file was indexed', stats.get('added') == len(cases), stats)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = {os.path.basename(r['path']): r
        for r in conn.execute('SELECT path, gp_seed, gp_seed_s FROM images')}

check('all four rows are there', len(rows) == len(cases), sorted(rows))

for name, seed in cases.items():
    r = rows.get(name)
    if r is None:
        check(f'{name} was indexed', False)
        continue
    # The point of the whole exercise: whichever column holds it, the DIGITS must come back
    # unchanged. A float round-trip fails here and nowhere else.
    stored = r['gp_seed_s'] if r['gp_seed_s'] is not None else r['gp_seed']
    check(f'{name}: seed reads back exactly ({seed})', str(stored) == str(seed), stored)
    # Exactly one column, so nothing downstream has to work out which is authoritative.
    check(f'{name}: only one of the two seed columns is set',
          (r['gp_seed'] is None) != (r['gp_seed_s'] is None),
          (r['gp_seed'], r['gp_seed_s']))

check('a seed that fits stays in the INTEGER column',
      rows['small.png']['gp_seed'] == 12345 and rows['small.png']['gp_seed_s'] is None)
check('the boundary value itself still fits',
      rows['boundary.png']['gp_seed'] == INT64_MAX and rows['boundary.png']['gp_seed_s'] is None)
check('one past the boundary goes to TEXT',
      rows['over.png']['gp_seed'] is None
      and rows['over.png']['gp_seed_s'] == str(INT64_MAX + 1))

conn.close()

print()
sys.exit(1 if _fails else 0)
