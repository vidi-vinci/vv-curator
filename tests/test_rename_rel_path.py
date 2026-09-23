"""test_rename_rel_path.py -- renaming a file that is not on the active library's drive.

Run: python tests/test_rename_rel_path.py

Reported 2026-09-21: "Rename failed: path is on mount 'D:', start on mount 'A:'", twice in a row,
and then every following attempt failed differently.

Both halves of that are pinned here.

THE CAUSE. rel_path was recomputed with os.path.relpath(new_path, ACTIVE['path']) -- the ACTIVE
library's root, not the FILE's. A merged library holds roots on several drives, and relpath across
two of them does not return a path, it RAISES. A rename never moves a file between folders, so the
directory part of rel_path is unchanged by construction and no root is needed to work it out.

THE CASCADE, which is the worse half. That raise happened AFTER os.rename had already succeeded and
it escaped the loop, so nothing was committed: the file was renamed on disk while its row still
named the old one, and so was every file renamed before it in the same run. The library then
disagreed with the disk, which is why the next attempt failed in a new way each time. Bookkeeping
about a rename that has already happened must never be able to abandon the run.
"""
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='vv_rename_')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_db  # noqa: E402
import server  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


DB = os.path.join(TMP, 'lib.db')
index_db.connect(DB).close()
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

FOLDER = os.path.join(TMP, 'runs', 'krea')
os.makedirs(FOLDER)

COLS = ('path', 'rel_path', 'folder', 'filename', 'ext', 'mtime', 'size',
        'has_meta', 'motion', 'has_cover', 'indexed_at', 'reader_ver')


def add(iid, name, rel):
    p = os.path.join(FOLDER, name)
    open(p, 'wb').write(b'x')
    conn.execute("INSERT INTO images(id, %s) VALUES (?%s)" % (', '.join(COLS), ', ?' * len(COLS)),
                 (iid, p, rel, 'krea', name, '.png', 0, 1, 0, 0, 0, 0, 0))
    # Through the app's own helper, so the search index and the table agree. A row inserted around
    # it leaves FTS inconsistent and the next delete reports the database as malformed -- which is
    # the fixture lying, not the code failing.
    index_db._fts_insert(conn, iid, '', '', name, 'krea', '')
    return p


A = add(1, 'Krea_Succubus_17-13-11.png', 'krea/Krea_Succubus_17-13-11.png')
B = add(2, 'Krea_Succubus_17-11-23.png', 'krea/Krea_Succubus_17-11-23.png')
conn.commit()


class Req(server.Handler):
    """Just enough of the handler to call the endpoint: no socket, no HTTP."""

    def __init__(self, body):
        self._body = body
        self.sent = None

    def _read_json(self):
        return self._body

    def _json(self, obj, code=200):
        self.sent = (code, obj)

    def _ids_from(self, b):
        return [int(i) for i in (b.get('ids') or [])]


def _fresh():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


# api_rename closes the connection it is given, so it gets its own each time rather than the one
# this test reads through.
server.ACTIVE['db'] = DB
server.db = _fresh


def rename(find, replace):
    r = Req({'ids': [1, 2], 'find': find, 'replace': replace})
    r.api_rename()
    return r.sent[1]


def row(iid):
    c = _fresh()
    try:
        return c.execute("SELECT path, rel_path, filename FROM images WHERE id=?", (iid,)).fetchone()
    finally:
        c.close()


print('\nRenaming a file that is not on the active library root\n')

# THE REPORTED CASE: the active library is somewhere else entirely. Before the fix this raised
# ValueError out of api_rename; the drive letters differ on Windows and the paths simply differ
# elsewhere, and either way no root is needed to answer the question.
server.ACTIVE['path'] = 'A:\\somewhere\\else' if os.name == 'nt' else '/somewhere/else'

out = rename('Succubus', 'Umbrella')
check('the run reports no errors', out.get('errors') == [], out.get('errors'))
check('both files were renamed', out.get('renamed') == 2, out)

for iid, stem in ((1, '17-13-11'), (2, '17-11-23')):
    r = row(iid)
    check('%d: the row names the new file' % iid,
          r['filename'] == 'Krea_Umbrella_%s.png' % stem, r['filename'])
    check('%d: and the file is there on disk' % iid, os.path.exists(r['path']), r['path'])
    check('%d: rel_path keeps its folder and swaps only the name' % iid,
          r['rel_path'] == 'krea/Krea_Umbrella_%s.png' % stem, r['rel_path'])
    check('%d: rel_path is not absolute -- it is relative to a root' % iid,
          not os.path.isabs(r['rel_path']), r['rel_path'])

# --- the cascade: bookkeeping must not abandon the run --------------------------------------------
# A rename that has happened on disk has to be recorded even if something after it goes wrong, or
# the library ends up describing files that are not there -- and every EARLIER file in the same run
# loses its update too, because nothing is committed.
add(3, 'Krea_Succubus_17-09-15.png', 'krea/Krea_Succubus_17-09-15.png')
conn.commit()
real = server.Handler._rename_bookkeeping
calls = {'n': 0}


def explode(self, c, p):
    calls['n'] += 1
    if calls['n'] == 1:
        raise RuntimeError('disk full')      # the first file's bookkeeping fails
    return real(self, c, p)


server.Handler._rename_bookkeeping = explode
r = Req({'ids': [1, 2, 3], 'find': 'Umbrella', 'replace': 'Parasol'})
r.api_rename()
server.Handler._rename_bookkeeping = real
out = r.sent[1]

check('a bookkeeping failure is reported rather than raised', r.sent[0] == 200, r.sent)
check('and names the file it happened to', len(out.get('errors') or []) == 1, out.get('errors'))
check('and says the file moved but the library did not',
      'renamed on disk' in (out['errors'][0]['error']), out['errors'][0])
check('the run CARRIES ON -- the other file is still recorded', out.get('renamed') == 1, out)
check('and that survivor really is committed',
      row(2)['filename'] == 'Krea_Parasol_17-11-23.png', row(2)['filename'])

print()
sys.exit(1 if _fails else 0)
