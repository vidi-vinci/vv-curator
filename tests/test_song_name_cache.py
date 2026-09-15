"""test_song_name_cache.py — the generic-song-name set is computed once, not once per page.

Run: python tests/test_song_name_cache.py

WHAT WENT WRONG. `_generic_song_names` is a GROUP BY over the whole images table, and it ran on
every page of results that contained even one song. A full-table aggregate, per page, to answer a
question that is the same for every page: which song "names" are really a batch prefix shared by
several genres, and so must not be printed as a title.

A trace on a 5,883-image library caught it costing 124ms on a single scroll page — 200ms against
the usual 74 — and 147ms of a 257ms random-sort search. It scales with the library and it spreads
as songs do, so on a large library with music throughout it would be paid on nearly every page.

WHAT MUST STAY TRUE, and it is the reason this is a test rather than a comment:

  1. A second call does no database work at all.
  2. A scan ending drops it, because a scan is what writes song_name and song_genre.
  3. A failed read is NOT cached — otherwise one locked database at the wrong moment would mean
     every song in the session is titled wrongly until restart.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server                                                          # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(label)
    print('  %s  %s' % ('ok  ' if ok else 'FAIL', label))
    if not ok:
        print('          got %r, want %r' % (got, want))


class CountingConn:
    """A real sqlite connection that records how many statements it was asked to run.

    Real, not a stub: the query has to actually execute against a real table for the GROUP BY and
    the HAVING to mean anything, and a hand-written fake result would test a program that does not
    exist.
    """

    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE images (id INTEGER PRIMARY KEY, song_name TEXT, '
                        'song_genre TEXT)')
        rows = [
            # A batch prefix: one name, several genres. This is the thing being detected.
            (1, 'Session 12', 'ambient'), (2, 'Session 12', 'techno'), (3, 'Session 12', 'jazz'),
            # A real title: one name, one genre, however many files carry it.
            (4, 'Falling Slowly', 'folk'), (5, 'Falling Slowly', 'folk'),
            # Noise the query must ignore.
            (6, None, 'folk'), (7, '', 'folk'),
        ]
        self.db.executemany('INSERT INTO images VALUES (?,?,?)', rows)
        self.queries = 0

    def execute(self, *a, **k):
        self.queries += 1
        return self.db.execute(*a, **k)


print('\nthe query itself still answers correctly')
server.clear_song_name_cache()
conn = CountingConn()
got = server._generic_song_names(conn)
check('a name spanning several genres is a batch prefix', 'Session 12' in got, True)
check('  and a name with one genre is a real title', 'Falling Slowly' not in got, True)
check('  empty and NULL names are ignored', got, {'Session 12'})
check('the first call does query the database', conn.queries, 1)

print('\nand is not asked twice')
server._generic_song_names(conn)
server._generic_song_names(conn)
check('two further calls add no database work', conn.queries, 1)
check('  and still return the same answer', server._generic_song_names(conn), {'Session 12'})

print('\na scan drops it, because a scan is what writes song names')
server.clear_song_name_cache()
before = conn.queries
server._generic_song_names(conn)
check('the next call after a scan re-reads', conn.queries, before + 1)


class BrokenConn:
    """A connection that fails, standing in for a locked or half-migrated database."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError('database is locked')


print('\na failed read is not remembered as the answer')
server.clear_song_name_cache()
check('a failure answers empty rather than raising', server._generic_song_names(BrokenConn()), set())
conn2 = CountingConn()
check('  and the next call tries again instead of serving the empty set',
      server._generic_song_names(conn2), {'Session 12'})

server.clear_song_name_cache()
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
