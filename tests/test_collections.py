"""Groups: the storage, the CRUD, and the filter clause that makes one narrow the grid.

NOT test_groups.py, WHICH IS ABOUT SETS. That file tests index_db.recompute_groups -- the
`images.group_id` column, which buckets the still, the upscale and the video of one run. This file
is about the user-facing Group: a hand-picked LIST, stored as `collections` + `collection_members`.
The two are unrelated and their names collide, which is why the storage is called collections and
why this file is not called test_groups. (Learned the hard way: the first draft of this file WAS
named test_groups.py, and silently replaced the Sets one.)

The collision is also the thing most worth pinning here: if someone ever "tidies" the table to
`groups`, the first test below is what should stop them.

THE REAL MECHANISM, not a fixture like it: every test opens a database through index_db.connect(),
which is what the app calls, so the schema, the indexes and the migration all run exactly as they
do in the app. The CRUD goes through server.collection_* -- the same functions the endpoints call.

Run:  python tests/test_collections.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_db
import server


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


def fresh(n=4):
    """A real database with `n` image rows, opened the way the app opens one.

    THE FTS ROW IS WRITTEN TOO, because the scan writes one for every image and `images_fts` is an
    external-content table. Without it delete_by_ids raises "database disk image is malformed" on
    the first delete -- a fixture that skipped it would have tested a database the app never has.
    """
    path = os.path.join(tempfile.mkdtemp(prefix='vvgroups'), 'library.db')
    conn = index_db.connect(path)
    conn.row_factory = sqlite3.Row
    for i in range(1, n + 1):
        conn.execute("INSERT INTO images(id, path, rel_path, folder, filename, ext, mtime, "
                     "size, indexed_at, root_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (i, f'C:/lib/f{i}.png', f'f{i}.png', '', f'f{i}.png', '.png', 0, 0, 0, 'r1'))
        index_db._fts_insert(conn, i, '', '', f'f{i}.png', '')
    conn.commit()
    return conn, path


def test_storage_is_not_named_group():
    """The tables are `collections`, and images.group_id still means a Set.

    THIS IS THE POINT OF THE WHOLE NAMING DECISION. A table called `groups` beside a column called
    `group_id` that means something else is how a future session deletes the wrong rows.
    """
    print('storage does not collide with Sets')
    conn, _ = fresh()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ok = check('collections table exists', 'collections' in names, True)
    ok &= check('collection_members table exists', 'collection_members' in names, True)
    ok &= check('no table called groups', 'groups' in names, False)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(images)")]
    ok &= check('images.group_id still exists (it is the Set)', 'group_id' in cols, True)
    conn.close()
    return ok


def test_create_add_and_count():
    print('make one, fill it, count it')
    conn, _ = fresh()
    g = server.collection_create(conn, 'Ocean series')
    ok = check('created', bool(g and g['name'] == 'Ocean series'), True)
    ok &= check('two files go in', server.collection_add(conn, g['id'], [1, 2]), 2)
    ok &= check('re-adding one is a no-op', server.collection_add(conn, g['id'], [2, 3]), 1)
    rows = server.list_collections(conn)
    ok &= check('count is right', rows[0]['count'], 3)
    conn.close()
    return ok


def test_name_rules():
    """The name is the identity: trimmed, capped, and case-insensitively unique."""
    print('names are trimmed, capped and unique')
    conn, _ = fresh()
    a = server.collection_create(conn, '  Ocean   series  ')
    ok = check('whitespace collapsed', a['name'], 'Ocean series')
    b = server.collection_create(conn, 'OCEAN SERIES')
    ok &= check('a clash returns the SAME group, not a second one', b['id'], a['id'])
    ok &= check('still one group', len(server.list_collections(conn)), 1)
    long = server.collection_create(conn, 'x' * 200)
    ok &= check('capped at MAX_COLLECTION_NAME', len(long['name']), server.MAX_COLLECTION_NAME)
    ok &= check('the cap is 40, the width a chip can carry', server.MAX_COLLECTION_NAME, 40)
    ok &= check('an empty name is refused', server.collection_create(conn, '   '), None)
    conn.close()
    return ok


def test_remove_does_not_touch_files():
    """Removing from a group, and deleting a group, both leave `images` alone.

    The confirm the user reads says the files stay where they are. This is the test that keeps
    that sentence true.
    """
    print('removing and deleting leave the files alone')
    conn, _ = fresh()
    g = server.collection_create(conn, 'Ocean')
    server.collection_add(conn, g['id'], [1, 2, 3])
    ok = check('one comes out', server.collection_remove(conn, g['id'], [2]), 1)
    ok &= check('two left in the group', server.list_collections(conn)[0]['count'], 2)
    ok &= check('all 4 images still there', conn.execute(
        "SELECT COUNT(*) FROM images").fetchone()[0], 4)
    server.collection_delete(conn, g['id'])
    ok &= check('the group is gone', server.list_collections(conn), [])
    ok &= check('its membership rows went with it', conn.execute(
        "SELECT COUNT(*) FROM collection_members").fetchone()[0], 0)
    ok &= check('the images are STILL all there', conn.execute(
        "SELECT COUNT(*) FROM images").fetchone()[0], 4)
    conn.close()
    return ok


def test_rename():
    print('rename, and refuse a name already taken')
    conn, _ = fresh()
    a = server.collection_create(conn, 'Ocean')
    server.collection_create(conn, 'Portraits')
    ok = check('renamed', server.collection_rename(conn, a['id'], 'Sea'), True)
    ok &= check('refuses a clash', server.collection_rename(conn, a['id'], 'portraits'), False)
    ok &= check('refuses an empty name', server.collection_rename(conn, a['id'], '  '), False)
    names = sorted(c['name'] for c in server.list_collections(conn))
    ok &= check('names are as expected', names, ['Portraits', 'Sea'])
    conn.close()
    return ok


def test_deleting_a_file_empties_its_membership():
    """index_db.delete_by_ids must take the membership rows with it.

    SQLite does not enforce the declared foreign key unless PRAGMA foreign_keys is on, so this is
    not free. Without it a group keeps counting a recycled file and draws fewer cards than the
    number beside its own name.
    """
    print('recycling a file takes it out of every group')
    conn, path = fresh()
    g = server.collection_create(conn, 'Ocean')
    server.collection_add(conn, g['id'], [1, 2, 3])
    conn.commit()
    conn.close()
    index_db.delete_by_ids(path, [2])
    conn = index_db.connect(path)
    conn.row_factory = sqlite3.Row
    ok = check('the group now counts 2', server.list_collections(conn)[0]['count'], 2)
    ok &= check('no orphan membership row', conn.execute(
        "SELECT COUNT(*) FROM collection_members WHERE image_id=2").fetchone()[0], 0)
    conn.close()
    return ok


def test_filter_clause_selects_only_members():
    """The EXISTS clause _filters builds, run against a real table.

    Copied from server._filters; keep the two in sync. What it pins is that the clause selects the
    group's members and nothing else -- the failure it guards against is an id that silently
    matches everything.
    """
    print('the filter clause narrows to the group')
    conn, _ = fresh()
    g = server.collection_create(conn, 'Ocean')
    server.collection_add(conn, g['id'], [1, 3])
    sql = ("SELECT i.id FROM images i WHERE EXISTS (SELECT 1 FROM collection_members cm "
           "WHERE cm.image_id = i.id AND cm.collection_id = ?) ORDER BY i.id")
    got = [r[0] for r in conn.execute(sql, (g['id'],))]
    ok = check('only the members come back', got, [1, 3])
    empty = server.collection_create(conn, 'Nothing')
    ok &= check('an empty group selects nothing',
                [r[0] for r in conn.execute(sql, (empty['id'],))], [])
    conn.close()
    return ok


def test_count_follows_the_filters():
    """The rail number answers "how many you would get", not "how many exist".

    That convention was settled on 2026-09-14, after the rail read "To publish 10" beside a grid
    holding one. list_collections takes a `matching` fragment from _filters -- which carries the
    library scope with it -- and counts only members inside it.
    """
    print('the count follows the filters')
    conn, _ = fresh(6)
    # Two roots, so the library scope has something to hide.
    conn.execute("UPDATE images SET root_id='r2' WHERE id IN (4,5,6)")
    g = server.collection_create(conn, 'Ocean')
    server.collection_add(conn, g['id'], [1, 2, 4, 5])
    ok = check('unfiltered, the whole group', server.list_collections(conn)[0]['count'], 4)
    only_r1 = "SELECT i.id FROM images i WHERE i.root_id = ?"
    ok &= check('scoped to one library, only its members',
                server.list_collections(conn, None, only_r1, ('r1',))[0]['count'], 2)
    none_at_all = "SELECT i.id FROM images i WHERE i.root_id = ?"
    ok &= check('scoped to a library holding none of them',
                server.list_collections(conn, None, none_at_all, ('r9',))[0]['count'], 0)
    conn.close()
    return ok


def test_an_empty_group_survives_a_filter():
    """A group with no matching files reads 0 and stays in the list.

    It must not vanish: the row is how you rename it, delete it, or add to it. The Labels list
    behaves the same way -- a label nothing matches still shows, at 0.
    """
    print('a group with nothing matching still lists, at 0')
    conn, _ = fresh()
    server.collection_create(conn, 'Empty')
    g = server.collection_create(conn, 'Ocean')
    server.collection_add(conn, g['id'], [1])
    rows = server.list_collections(conn, None, "SELECT i.id FROM images i WHERE i.id = ?", (1,))
    got = {r['name']: r['count'] for r in rows}
    ok = check('both groups still listed', sorted(got), ['Empty', 'Ocean'])
    ok &= check('the one with no match reads 0', got['Empty'], 0)
    ok &= check('the other reads its matching members', got['Ocean'], 1)
    conn.close()
    return ok


def test_what_a_file_belongs_to():
    """The detail panel's question, which is the reverse of the rail's.

    The rail answers "what is IN this group"; this answers "what does this FILE belong to". The SQL
    is copied from api_image; keep the two in sync. It reads through idx_collection_members_image,
    which exists for this and nothing else.
    """
    print('what one file belongs to')
    conn, _ = fresh()
    b = server.collection_create(conn, 'Beta')
    a = server.collection_create(conn, 'Alpha')
    server.collection_add(conn, a['id'], [1, 2])
    server.collection_add(conn, b['id'], [1])
    sql = ("SELECT c.id, c.name FROM collection_members m JOIN collections c "
           "ON c.id = m.collection_id WHERE m.image_id = ? ORDER BY c.name COLLATE NOCASE")
    ok = check('both, name-ordered to match the rail',
               [r['name'] for r in conn.execute(sql, (1,))], ['Alpha', 'Beta'])
    ok &= check('a file in one', [r['name'] for r in conn.execute(sql, (2,))], ['Alpha'])
    ok &= check('a file in none', [r['name'] for r in conn.execute(sql, (3,))], [])
    conn.close()
    return ok


def test_a_new_group_is_visible_to_the_panel_immediately():
    """A group made from the detail panel must be in the list before its chip is drawn.

    THE BUG THIS PINS, 2026-09-22 (the author: "if i enter a new group on the detail page, the chip
    does not appear"). renderDetailGroups reconciles the open file's chips against the live list --
    so a name that no longer exists cannot linger after a rename or a delete. It also wrote the
    filtered list BACK. Adding a brand-new group therefore: patched the file's groups, drew, and
    the draw discarded the new entry because the list had not been re-read yet. The chip vanished
    and never returned, since the reload that would have vindicated it found nothing left.

    Two halves to the fix and this covers the server half -- that a create is immediately visible
    to the very next list call, with no second write needed. The client half is ordering: refresh
    the list, then patch, then draw; and the reconcile no longer writes back.
    """
    print('a new group is listed the moment it is made')
    conn, _ = fresh()
    g = server.collection_create(conn, 'Made just now')
    server.collection_add(conn, g['id'], [1])
    names = [c['name'] for c in server.list_collections(conn)]
    ok = check('the new group is in the very next listing', 'Made just now' in names, True)
    # And the file's own membership shows it, which is what the chip is drawn from.
    sql = ("SELECT c.name FROM collection_members m JOIN collections c ON c.id = m.collection_id "
           "WHERE m.image_id = ?")
    ok &= check('and the file belongs to it', [r['name'] for r in conn.execute(sql, (1,))],
                ['Made just now'])
    conn.close()
    return ok


def test_has_flag_for_the_picker():
    """`for=<image id>` marks which groups a file is already in, in one query."""
    print('the picker learns what a file is already in')
    conn, _ = fresh()
    a = server.collection_create(conn, 'Ocean')
    server.collection_create(conn, 'Portraits')
    server.collection_add(conn, a['id'], [1])
    rows = {c['name']: c['has'] for c in server.list_collections(conn, ids_for=1)}
    ok = check('in Ocean', rows['Ocean'], True)
    ok &= check('not in Portraits', rows['Portraits'], False)
    conn.close()
    return ok


if __name__ == '__main__':
    results = [test_storage_is_not_named_group(), test_create_add_and_count(), test_name_rules(),
               test_remove_does_not_touch_files(), test_rename(),
               test_deleting_a_file_empties_its_membership(),
               test_filter_clause_selects_only_members(), test_count_follows_the_filters(),
               test_an_empty_group_survives_a_filter(), test_what_a_file_belongs_to(),
               test_a_new_group_is_visible_to_the_panel_immediately(),
               test_has_flag_for_the_picker()]
    print('\nPASS' if all(results) else '\nFAIL')
    sys.exit(0 if all(results) else 1)
