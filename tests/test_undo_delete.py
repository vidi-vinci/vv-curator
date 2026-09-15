"""test_undo_delete.py — recycling is DEFERRED, and Undo cancels it.

Run: python test_undo_delete.py

A recycle from the grid, the detail view or the set keeper is queued for UNDO_WINDOW_S rather than
performed. Nothing moves and no row is purged until the timer fires, so Undo is "cancel the job",
not "put the file, its row, its tags, its label, its note and its score back". Everything below
follows from that one decision, and each fails SILENTLY in the direction of the grid telling you
something untrue:

  * NOTHING MAY MOVE EARLY. If the files went at request time the feature is decorative. Asserted
    against the filesystem and the index, not against the reply.

  * THE THREE ENDPOINTS MUST AGREE, exactly as they must for the Hidden mark. A pending row is gone
    from /api/search, /api/facets and /api/ids at the same instant, because they share one WHERE
    builder. If the exclusion reached only some, "Select all matching" would hand back ids for
    images the grid has already removed — and the next bulk action would recycle them for real.

  * THE DENOMINATOR MUST FOLLOW. The count sites inline their own copy of the hidden predicate
    rather than going through _filters, so they need the pending one too; otherwise the strip counts
    files the grid has stopped showing and "179 of 1,876" lies for the length of the window.

  * UNDO MUST RESTORE THE ROW WHOLE. Tags, label, note and score are on the row the whole time —
    that is the point of deferring rather than snapshotting — so this checks they are all still
    attached afterwards. If this ever fails, something started purging up front.

  * A CLOSED WINDOW IS NOT AN ERROR. Racing the timer by a few milliseconds is the normal end of a
    batch's life, so undo answers 200 with undone=false rather than 4xx.

  * A FLUSH MUST NOT LAND MID-JOB. db() takes no lock; concurrency safety in this server rests on
    one long job at a time. The timer reschedules rather than becoming a second writer.

Runs the real server in-process on an ephemeral port. `_recycle_one` — the one step that would put
test files in the user's actual Recycle Bin — is patched the way test_set_cull.py patches it; every
other step (the index purge, the counts, the exclusion) runs exactly as it does in the app.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import http.client

TMP = tempfile.mkdtemp(prefix='vv-undo-')
LIB = os.path.join(TMP, 'lib')
os.makedirs(LIB)
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

from PIL import Image                                        # noqa: E402
NAMES = ['a.png', 'b.png', 'c.png', 'd.png', 'e.png', 'f.png']
for n, name in enumerate(NAMES):
    Image.new('RGB', (48, 48), (20 + n * 30, 60, 90)).save(os.path.join(LIB, name))

json.dump({'roots': [{'key': 'A', 'name': 'Main', 'path': LIB}], 'active': 'A', 'port': 0},
          io.open(os.environ['CV_CONFIG'], 'w', encoding='utf-8'))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server                                                # noqa: E402
import index_db                                              # noqa: E402
from http.server import ThreadingHTTPServer                  # noqa: E402

server.UNDO_WINDOW_S = 1          # keep the suite quick; the mechanism is identical
server.ensure_library()
server.set_active('A')

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# The one step that would reach the user's real Recycle Bin. Really removes the file, records it,
# and reports no touched folders — same seam and same shape as test_set_cull.py.
binned = []
_real_recycle_one = server._recycle_one
server._recycle_one = lambda path, anchor_root=None: (binned.append(path), os.remove(path), set())[2]

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def call(method, path, obj=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    if obj is None:
        c.request(method, path)
    else:
        c.request(method, path, json.dumps(obj), {'Content-Type': 'application/json'})
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body or b'{}')
    except ValueError:
        return r.status, {}


def wait_scan():
    for _ in range(200):
        if not call('GET', '/api/scan/status')[1].get('running'):
            return
        time.sleep(0.05)


def grid(extra=''):
    return call('GET', '/api/search?limit=99&group=0&sets=0' + extra)[1]


def ids_endpoint():
    return set(call('GET', '/api/ids?limit=99&group=0&sets=0')[1].get('ids') or [])


def facet_total():
    return call('GET', '/api/facets?')[1].get('total')


def rows_in_db():
    conn = server.db()
    n = conn.execute('SELECT COUNT(*) c FROM images').fetchone()['c']
    conn.close()
    return n


def wait_flushed(bid, timeout=8):
    for _ in range(int(timeout / 0.05)):
        st, j = call('GET', '/api/delete/result?batch=%d' % bid)
        if not j.get('pending') and 'deleted' in j:
            return j
        time.sleep(0.05)
    return None


try:
    call('POST', '/api/scan', {'force': True})
    wait_scan()
    items = grid()['items']
    by_name = {i['filename']: i['id'] for i in items}
    check('the test library indexed', len(by_name) == 6, sorted(by_name))

    # Curation on the doomed rows, so "undo restores the row whole" is not vacuous — and CHECKED
    # here, because the first cut of this test used the wrong parameter names, set nothing at all,
    # and its three "survived" assertions were comparing absence to absence.
    doomed = [by_name['a.png'], by_name['b.png']]
    call('POST', '/api/tag', {'ids': doomed, 'tag': 'keepme'})
    call('POST', '/api/favorite', {'ids': [doomed[0]], 'on': True})
    call('POST', '/api/note', {'id': doomed[0], 'text': 'a note worth not losing'})
    d0 = call('GET', '/api/image/%d' % doomed[0])[1]
    check('the curation under test was really applied',
          d0.get('note') == 'a note worth not losing' and bool(d0.get('favorite'))
          and 'keepme' in (d0.get('tags') or []), d0.get('tags'))

    print('\nA QUEUED RECYCLE MOVES NOTHING')
    before_rows, before_files = rows_in_db(), sorted(os.listdir(LIB))
    st, j = call('POST', '/api/delete', {'ids': doomed})
    bid = j.get('batch')
    check('the reply is a batch, not a count', st == 200 and isinstance(bid, int)
          and j.get('pending') == 2 and 'deleted' not in j, j)
    check('  no file has moved', sorted(os.listdir(LIB)) == before_files, os.listdir(LIB))
    check('  nothing was binned', binned == [], binned)
    check('  no row was purged', rows_in_db() == before_rows)

    print('\n...but the rows are gone from EVERY view, at the same instant')
    g = grid()
    shown = {i['id'] for i in g['items']}
    check('absent from /api/search', not (shown & set(doomed)), sorted(shown))
    check('absent from /api/ids — or Select-all would reach them', not (ids_endpoint() & set(doomed)))
    check('absent from /api/facets total', facet_total() == 4, facet_total())
    check('the matched count follows', g.get('total') == 4, g.get('total'))
    check('  and so does the denominator (root_files)', g.get('root_files') == 4, g.get('root_files'))

    print('\nUNDO CANCELS IT, and the row comes back whole')
    st, j = call('POST', '/api/delete/undo', {'batch': bid})
    check('undo reports what it restored', st == 200 and j.get('undone') is True
          and j.get('restored') == 2, j)
    g = grid()
    check('the images are back in the grid', {i['id'] for i in g['items']} >= set(doomed))
    check('  and back in /api/ids', ids_endpoint() >= set(doomed))
    check('  and the counts are whole again', g.get('total') == 6 and g.get('root_files') == 6, g)
    d = call('GET', '/api/image/%d' % doomed[0])[1]
    check('the note survived', d.get('note') == 'a note worth not losing', d.get('note'))
    check('the favorite survived', bool(d.get('favorite')))
    check('the tag survived', 'keepme' in (d.get('tags') or []), d.get('tags'))
    check('still nothing binned', binned == [], binned)
    time.sleep(server.UNDO_WINDOW_S + 0.6)      # the cancelled timer must not fire late
    check('a cancelled batch never fires', binned == [] and rows_in_db() == before_rows, binned)

    print('\nLETTING THE WINDOW CLOSE ACTUALLY RECYCLES')
    st, j = call('POST', '/api/delete', {'ids': [by_name['c.png']]})
    bid2 = j['batch']
    res = wait_flushed(bid2)
    check('the batch reports its result', res is not None and res.get('deleted') == 1, res)
    check('  the file really went to the bin', len(binned) == 1
          and binned[0].endswith('c.png'), binned)
    check('  and its row was purged', rows_in_db() == before_rows - 1)
    check('  the grid agrees', grid().get('total') == 5)

    print('\na window that has closed is not an ERROR — racing it is normal')
    st, j = call('POST', '/api/delete/undo', {'batch': bid2})
    check('undo answers 200, not 4xx', st == 200, st)
    check('  and says plainly that it did not undo',
          j.get('undone') is False and 'reason' in j, j)
    st, j = call('POST', '/api/delete/undo', {'batch': 999999})
    check('an unknown batch behaves the same way', st == 200 and j.get('undone') is False, j)
    st, j = call('POST', '/api/delete/undo', {})
    check('a missing batch id IS an error', st == 400, (st, j))

    print('\na flush waits for a running job rather than becoming a second writer')
    st, j = call('POST', '/api/delete', {'ids': [by_name['d.png']]})
    bid3 = j['batch']
    server._scan_state['running'] = True                 # as if a scan owned the index
    try:
        time.sleep(server.UNDO_WINDOW_S + 0.5)
        check('the timer fired but deferred to the job',
              call('GET', '/api/delete/result?batch=%d' % bid3)[1].get('pending') is True)
        check('  so the file is still there', os.path.exists(os.path.join(LIB, 'd.png')))
        check('  and it is still hidden from the grid meanwhile',
              by_name['d.png'] not in {i['id'] for i in grid()['items']})
    finally:
        server._scan_state['running'] = False
    check('once the job clears, it completes', wait_flushed(bid3, timeout=12) is not None)
    check('  and the file is gone', not os.path.exists(os.path.join(LIB, 'd.png')))

    print('\nONLY ONE BATCH IS EVER PENDING — recycling again commits the previous one')
    # The client calls /api/delete/commit before queueing the next batch. Blocking the second
    # delete instead was rejected: it would lock the grid for the whole window after every press,
    # which fights the fast culling this exists to make safe. Nothing is lost by committing early —
    # the older batch stopped being undoable the moment you acted again.
    st, j = call('POST', '/api/delete', {'ids': [by_name['f.png']]})
    bid4 = j['batch']
    check('the first is queued and still on disk', os.path.exists(os.path.join(LIB, 'f.png')))
    st, j = call('POST', '/api/delete/commit', {'batch': bid4})
    check('committing it reports that it ran', st == 200 and j.get('committed') is True, j)
    check('  the file went immediately, without waiting out the window',
          not os.path.exists(os.path.join(LIB, 'f.png')))
    check('  and its timer cannot fire a second time', not server._pending_ids, server._pending_ids)
    st, j = call('POST', '/api/delete/commit', {'batch': bid4})
    check('committing twice is a no-op, not an error', st == 200 and j.get('committed') is False, j)

    print('\nshutdown completes anything still queued')
    st, j = call('POST', '/api/delete', {'ids': [by_name['e.png']]})
    check('queued, and still on disk', os.path.exists(os.path.join(LIB, 'e.png')))
    server._flush_all_pending()                          # what the client watchdog calls on exit
    check('the pending recycle was completed, not dropped',
          not os.path.exists(os.path.join(LIB, 'e.png')))
    check('  and the exclusion set is empty again', not server._pending_ids, server._pending_ids)

    print('\nonly what was asked for went')
    # c, d, e and f were recycled; a + b were undone and must be untouched on BOTH sides.
    check('the undone pair is still on disk',
          all(os.path.exists(os.path.join(LIB, n)) for n in ('a.png', 'b.png')))
    check('  and still in the index', rows_in_db() == 2, rows_in_db())
    check('  and nothing else survived', sorted(os.listdir(LIB)) == ['a.png', 'b.png'],
          sorted(os.listdir(LIB)))
finally:
    server._recycle_one = _real_recycle_one
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

print('\n' + ('all checks passed' if not failures
              else '%d FAILED:\n  - %s' % (len(failures), '\n  - '.join(failures))))
sys.exit(1 if failures else 0)
