"""
server.py — local web server for the ComfyUI image viewer.

Pure standard library (http.server + sqlite3). Serves:
  GET  /                     -> the single-page UI (app/index.html)
  GET  /static/<file>        -> UI assets
  GET  /api/facets           -> models + folders with counts, totals
  GET  /api/search?...       -> paged results (q, model, folder, sort, order, limit, offset)
  GET  /api/image/<id>       -> full metadata for one image
  GET  /thumb/<id>           -> cached WebP thumbnail (lazily generated if missing)
  GET  /file/<id>            -> the original image bytes
  POST /api/scan             -> (re)index the root in a background thread
  GET  /api/scan/status      -> progress of an in-flight scan
  POST /api/reveal/<id>      -> open the file's folder in Explorer (local convenience)

Config is read from config.json next to this file (roots, settings, db path); the port comes from
port.txt beside it, falling back to config.json for installs that predate that file.
"""
import base64
import errno
import hashlib
import contextlib
import io
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, PngImagePlugin

import index_db
import thumbs as thumbs_mod
import prompt_miner
import comfy_meta
import model_hash

try:
    from send2trash import send2trash
except Exception:
    send2trash = None

# Folder name used as a manual "recycle bin" fallback on volumes that have no OS
# Recycle Bin (e.g. network / shared drives, where send2trash raises). The scanner
# skips this folder (see index_db.list_images) so moved files never re-index.
RECYCLE_DIRNAME = '_ToRecycle'


# WHEN TO MENTION THE RECYCLE FOLDER. The author asked for a nudge as it fills: "Time to recycle? You
# have 1,202 files." Whichever trips first, and both here so a later setting has one place to read.
# Files alone would miss his video work entirely -- a few dozen clips can be tens of GB -- and bytes
# alone would never fire on a folder of thumbnails.
RECYCLE_WARN_FILES = 500
RECYCLE_WARN_GB = 5.0
# BOUNDS, and the floor on size is the interesting one. The author, 2026-08-28: "I'd vote lower on
# memory. 0.1 GB? Some people don't do any video, but still want to avoid a build-up of files."
# A stills library trips the file count long before any size limit, so the low floor is not for
# everyone -- it is so someone who wants a tight leash can have one.
RECYCLE_FILES_MIN, RECYCLE_FILES_MAX = 50, 100000
RECYCLE_GB_MIN, RECYCLE_GB_MAX = 0.1, 500.0


def _clamped(value, lo, hi, default, decimals=None):
    """A number inside its bounds, or `default` when it isn't one.

    Written because the Miner block clamps silently on the server with no `max` on the input, so
    typing 999999 there becomes 100000 with nothing said. These fields state their range in the
    dialog and snap back in front of you; this is the backstop for a hand-edited config, not the
    place the user finds out.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:                                    # NaN survives float() and fails every comparison
        return default
    n = max(lo, min(hi, n))
    return round(n, decimals) if decimals else int(round(n))


def _recycle_folder_stats(root_path):
    """`(files, bytes)` in this library's `_ToRecycle`, or `(0, 0)` if it has none.

    ONE PASS, and the size is free: os.scandir hands back a DirEntry whose stat() is already cached
    from the directory read on Windows, so counting and summing cost the same walk.

    Top level only, deliberately -- _move_to_recycle_folder never nests, and a recursive walk over a
    folder on a network share is the kind of cost that turns a nudge into a stall.
    """
    folder = os.path.join(os.path.abspath(root_path), RECYCLE_DIRNAME)
    files = size = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file():
                        files += 1
                        size += e.stat().st_size
                except OSError:
                    pass                      # one unreadable entry must not lose the whole count
    except OSError:
        return 0, 0                           # no folder, or unreachable -- both mean "nothing to say"
    return files, size


def _log_exc(what):
    """Print a timestamped traceback to stderr. start.bat captures stderr into data/viewer.log,
    which is the only failure record the app has — the server runs hidden, so anything not written
    there is unrecoverable the moment it happens."""
    import traceback
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {what}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)


def _move_to_recycle_folder(path, anchor_root=None):
    """Move `path` into a `_ToRecycle` folder; return (new_path, base) where `base` is the folder
    the `_ToRecycle` was created in (its mtime got bumped).

    Fallback for volumes with no OS Recycle Bin (network/shared drives, where send2trash
    raises). Prefers the library `anchor_root` (one tidy _ToRecycle when the root is
    writable), then falls back to the file's own folder — guaranteed same volume and
    writable, since that's where the images are created. This covers roots that are
    read-only (WinError 5) or on a different volume via a junction (WinError 17). The
    scanner skips any _ToRecycle by name, so either location stays hidden. Collisions get
    a numeric suffix.
    """
    ap = os.path.abspath(path)
    name = os.path.basename(ap)
    bases = []
    if anchor_root and os.path.isdir(anchor_root):
        bases.append(os.path.abspath(anchor_root))
    bases.append(os.path.dirname(ap))          # the file's own folder (same volume, writable)

    last_err = None
    for base in bases:
        folder = os.path.join(base, RECYCLE_DIRNAME)
        try:
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, name)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(name)
                n = 1
                while os.path.exists(dest):
                    dest = os.path.join(folder, f"{stem}_{n}{ext}")
                    n += 1
            os.rename(ap, dest)
            return dest, base
        except Exception as e:
            last_err = e
            try:
                os.rmdir(folder)               # drop a just-created empty folder; no-op if non-empty
            except OSError:
                pass
    raise last_err


def _is_network_path(path):
    """True if `path` is on a network/remote volume (UNC or a mapped network drive).

    Critical: on such volumes send2trash has no Recycle Bin to use, so it PERMANENTLY
    deletes the file *without raising* — silent data loss. We route these to the
    recoverable _ToRecycle move instead of ever calling send2trash.
    """
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive.startswith('\\\\') or drive.startswith('//'):
        return True                                    # UNC \\server\share
    if os.name == 'nt' and len(drive) == 2 and drive.endswith(':'):
        try:
            import ctypes
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') == DRIVE_REMOTE
        except Exception:
            return False                               # can't tell -> treat as local
    return False


def _recycle_one(path, anchor_root=None):
    """Recycle one file, recoverably; raise on failure. Returns the library folders whose mtime the
    move bumped (source folder, plus the _ToRecycle parent on shares) so the caller can refresh
    their change-signature and not self-flag the ↻.

    Local disks: use the OS Recycle Bin (send2trash), falling back to a _ToRecycle folder
    if that fails. Network shares: go straight to _ToRecycle — send2trash would hard-delete
    without warning there (see _is_network_path).
    """
    ap = os.path.abspath(path)
    if send2trash is not None and not _is_network_path(path):
        try:
            send2trash(os.path.normpath(path))
            return [os.path.dirname(ap)]               # file left its source folder
        except Exception:
            pass                                       # OS recycle failed on a local disk -> folder move
    _dest, base = _move_to_recycle_folder(path, anchor_root)
    return [os.path.dirname(ap), base]                 # source folder + where _ToRecycle sits


def _refresh_signatures_for(folder_abspaths):
    """After the app recycles files, bring the stored folder-mtime signature back in line with disk
    for exactly the folders WE bumped — so the app's own culling doesn't light the ↻ (which should
    mean 'changed outside the app'). Each folder is mapped to its library + relpath; anything not
    under a configured library is ignored. CONFIG imported lazily to avoid init-order issues."""
    by_root = {}                                       # key -> (root_path, {rel folders})
    roots = [(r, os.path.abspath(r['path'])) for r in CONFIG['roots']]
    for f in folder_abspaths:
        fa = os.path.abspath(f)
        for r, rp in roots:
            if fa == rp or fa.startswith(rp + os.sep):
                rel = '.' if fa == rp else os.path.relpath(fa, rp).replace('\\', '/')
                by_root.setdefault(r['key'], (r['path'], set()))[1].add(rel)
                break
    for key, (root_path, rels) in by_root.items():
        try:
            index_db.refresh_dir_sig(LIBRARY_DB, root_path, key, rels)
        except Exception as e:
            print(f"[delete] signature refresh failed for {key}: {e!r}", file=sys.stderr)


def _file_in_use(err):
    """True if this is Windows refusing the operation because something still has the file open
    (32 = sharing violation, 33 = lock violation), rather than a real permission problem."""
    return getattr(err, 'winerror', None) in (32, 33)


def _recycle_one_settling(path, anchor_root=None, attempts=5, delay=0.12):
    """_recycle_one, retried briefly while the file is still held open by our own streaming.

    Deleting a video the viewer just played is what this exists for. The client now detaches the
    <video> before asking for the delete, but that abort is ASYNCHRONOUS: the thread streaming
    /file/ is parked in a blocking write and only discovers the client is gone when that write
    fails, so its file handle can outlive the delete request by tens of milliseconds. Retrying for
    ~half a second turns that race into a certainty.

    Only an in-use error is retried. A genuine permission or path failure re-raises on the first
    attempt, so nothing real gets masked or delayed.
    """
    for i in range(attempts):
        try:
            return _recycle_one(path, anchor_root)
        except Exception as e:
            if i == attempts - 1 or not _file_in_use(e):
                raise
            time.sleep(delay)


def _expand_to_cards(ids):
    """Every indexed file sharing a card with one of these ids, visible in the grid or not.

    Deliberately NOT filtered by the view: this exists precisely to catch the members the view is
    keeping out. Both things that used to hide one -- the Hidden mark and Hide large images -- were
    removed on 2026-09-08, and rows queued for recycle still can. See api_delete's call site."""
    if not ids:
        return ids
    conn = db()
    try:
        rows = conn.execute(
            f"SELECT id FROM images WHERE group_id IN (SELECT group_id FROM images "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND group_id IS NOT NULL)", ids).fetchall()
    finally:
        conn.close()
    return sorted(set(ids) | {r['id'] for r in rows})


def _sidecars_going_with(conn, ids):
    """The `.txt` sidecars that may be recycled alongside these files, and no others.

    A sidecar is not indexed -- `.txt` is not a media type -- so nothing outside comfy_meta knew
    they existed and a recycled video simply left its text file behind (the author, 2026-09-06).

    THE RULE IS CONSERVATIVE, because a sidecar is not always one-per-file. comfy_meta.sidecar_path
    resolves four candidates, and the last is the stem truncated at the run code -- ONE sidecar for
    a WHOLE generation, found from any of its members. A repro run of png + png + mp4 had all three
    resolve to the same text file, so "take the sidecar with the video" would have deleted the
    description of two stills that were being kept.

    So a sidecar goes only when nothing left behind still points at it:
      * a GROUPED file's sidecar goes when every indexed member of its group is going too;
      * an UNGROUPED file's goes only if it is the unambiguous `<file>.<ext>.txt` form, which by
        construction can belong to no other file.
    Anything else stays. A text file left on disk is untidy; one deleted out from under a file you
    kept is unrecoverable metadata, and only one of those two is worth risking.
    """
    idset = set(ids)
    keep_group = set()          # groups with at least one member NOT being recycled
    grouped = {}
    for r in conn.execute(f"SELECT id, path, group_id, root_id FROM images "
                          f"WHERE group_id IN (SELECT group_id FROM images WHERE id IN "
                          f"({','.join('?' * len(ids))}) AND group_id IS NOT NULL)", ids):
        grouped[r['id']] = (r['path'], r['group_id'], r['root_id'])
        if r['id'] not in idset:
            keep_group.add(r['group_id'])
    out = {}                    # sidecar path -> the root_id of the file it belongs to
    for iid in ids:
        path, gid, rid = grouped.get(iid, (None, None, None))
        if path is None:
            row = conn.execute("SELECT path, group_id, root_id FROM images WHERE id=?",
                               (iid,)).fetchone()
            if not row:
                continue
            path, gid, rid = row['path'], row['group_id'], row['root_id']
        try:
            sc = comfy_meta.sidecar_path(path)
        except Exception:
            sc = None
        if not sc:
            continue
        if gid:
            if gid not in keep_group:
                out[sc] = rid
        elif os.path.basename(sc) == os.path.basename(path) + '.txt':
            out[sc] = rid        # `clip.mp4.txt` -- can only ever describe this one file
    return out


def _recycle_ids(ids):
    """Recycle the given image ids and purge them from the index. Returns (moved, purged, failed).

    The single recycle implementation — /api/delete and the duplicate cull both go through here, so
    the Recycle-Bin-vs-_ToRecycle handling and the signature refresh can't drift apart.

    `moved` counts files actually recycled; `purged` counts rows whose file was already gone from
    disk. Those are reported separately because conflating them reads as "N files recycled" when
    nothing moved — which is indistinguishable, from the outside, from files vanishing.

    The missing-file rule is right for a file deleted behind our back and WRONG for one on an
    unplugged drive, so every caller must have already excluded unreachable libraries (see
    _reachable_root_ids).
    """
    conn = db()
    qmarks = ','.join('?' * len(ids))
    rows = {r['id']: (r['path'], r['root_id']) for r in
            conn.execute(f"SELECT id, path, root_id, group_id FROM images WHERE id IN ({qmarks})", ids)}
    sidecars = _sidecars_going_with(conn, ids)
    conn.close()
    # Anchor each file at ITS OWN library root, not the active one. _move_to_recycle_folder prefers
    # the anchor, and a root from a different library is often on another volume — the rename then
    # fails and it falls back per-file, littering a _ToRecycle into every folder it touches. The
    # file's own root is always the same volume, so each library gets one tidy _ToRecycle.
    root_paths = {r['key']: r['path'] for r in (CONFIG.get('roots') or [])}
    trashed, failed, touched, moved = [], [], set(), 0
    for iid in ids:
        p, rid = rows.get(iid, (None, None))
        if p is None or not os.path.exists(p):
            trashed.append(iid)              # already absent from disk -> still purge from index
            continue
        try:
            # OS Recycle Bin on local disks, _ToRecycle on shares; returns the folders it bumped
            touched.update(_recycle_one_settling(p, root_paths.get(rid) or ACTIVE.get('path')))
            trashed.append(iid)
            moved += 1
        except Exception as e:
            # Full traceback, not just the message: WHICH step failed (send2trash vs the
            # _ToRecycle rename) is most of the diagnosis, and str(e) alone doesn't say.
            _log_exc(f"[delete] id={iid} could not recycle; path={p} ({len(p)} chars); err={e!r}")
            failed.append({'id': iid, 'error': str(e)})
    # The sidecars last, and never fatally: they carry no row, so a failure here costs a stray text
    # file, while letting it raise would abandon the index cleanup below for files already binned.
    for sc, sc_rid in sidecars.items():
        try:
            if os.path.exists(sc):
                touched.update(_recycle_one_settling(sc, root_paths.get(sc_rid) or ACTIVE.get('path')))
                moved += 1
        except Exception as e:
            _log_exc(f"[delete] sidecar could not be recycled; path={sc}; err={e!r}")
    removed = index_db.delete_by_ids(ACTIVE['db'], trashed)
    # our own move bumped those folders' mtimes; re-baseline the signature so the ↻ doesn't
    # flag the library for a change WE made (only external new files should light it)
    if touched:
        _refresh_signatures_for(touched)
    return moved, removed - moved, failed


def _queue_recycle(ids):
    """Queue a recycle for UNDO_WINDOW_S, and hide the rows meanwhile. Returns the batch id."""
    global _pending_seq
    with _pending_lock:
        _pending_seq += 1
        bid = _pending_seq
        t = threading.Timer(UNDO_WINDOW_S, _flush_batch, [bid])
        t.daemon = True                      # never hold the process open on a pending delete
        _pending[bid] = {'ids': list(ids), 'timer': t, 'result': None}
        _pending_ids.update(ids)
        t.start()
    return bid


def _flush_batch(bid, force=False):
    """Execute a queued recycle. Called by its timer, or by shutdown with force=True.

    RESCHEDULES ITSELF while another job owns the index. db() takes no lock and concurrency safety
    in this server rests entirely on _job_running() permitting one long job at a time, so a flush
    landing in the middle of a scan would be a second writer. Waiting a few seconds costs nothing —
    the files are not going anywhere, and the rows stay hidden while we wait. Shutdown passes
    force=True because there is no "later" left."""
    with _pending_lock:
        b = _pending.get(bid)
        if b is None or b['result'] is not None:
            return                            # already flushed, or undone
        if _job_running() and not force:
            b['timer'] = threading.Timer(5, _flush_batch, [bid])
            b['timer'].daemon = True
            b['timer'].start()
            return
        ids = b['ids']
    moved, purged, failed = _recycle_ids(ids)
    with _pending_lock:
        b = _pending.get(bid)
        if b is not None:
            b['result'] = {'deleted': moved, 'purged': purged, 'failed': failed}
        # Only drop the ids that actually went. A file that could NOT be recycled still exists and
        # still has a row, so it has to come back into view rather than stay invisible forever.
        _recompute_pending_ids()
    return moved, purged, failed


def _recompute_pending_ids():
    """Rebuild the exclusion set from the batches that are still queued. Call under the lock."""
    _pending_ids.clear()
    for b in _pending.values():
        if b['result'] is None:
            _pending_ids.update(b['ids'])
    # Results are kept only until the client collects them; drop the oldest so a long session
    # cannot accumulate them without bound.
    done = [k for k, v in sorted(_pending.items()) if v['result'] is not None]
    for k in done[:-20]:
        _pending.pop(k, None)


def _undo_batch(bid):
    """Cancel a queued recycle. Returns the ids restored, or None if it had already run."""
    with _pending_lock:
        b = _pending.get(bid)
        if b is None or b['result'] is not None:
            return None                       # already flushed (or never existed) — not an error
        b['timer'].cancel()
        ids = b['ids']
        _pending.pop(bid, None)
        _recompute_pending_ids()
    return ids


def _commit_batch(bid):
    """Run a queued recycle NOW instead of waiting out its window. The client calls this when you
    recycle again: only one batch is ever undoable, so the previous one has nothing left to wait
    for. Its timer is cancelled first so it cannot fire a second time."""
    with _pending_lock:
        b = _pending.get(bid)
        if b is None or b['result'] is not None:
            return False
        b['timer'].cancel()
    _flush_batch(bid, force=True)
    return True


def _flush_all_pending():
    """Run every queued recycle now — closing the app completes what you asked for."""
    for bid in sorted(list(_pending)):
        try:
            _flush_batch(bid, force=True)
        except Exception as e:
            _log_exc(f"[delete] pending batch {bid} failed on shutdown: {e!r}")


# ---- duplicate file copies (same file sitting in more than one folder) ------------------------
# Two files can't share a name inside one folder, so a "copy" is always in a different folder. The
# folder you're viewing is the anchor: its files are kept, matching copies elsewhere are recycled.

NOTE_MERGE_SEP = '\n\n— merged from a deleted copy —\n'


def _reachable_root_ids():
    """Library keys whose folder is reachable right now.

    Unreachable libraries are excluded from the cull entirely, and this is a safety rule, not an
    optimisation: _recycle_ids purges any row whose file is missing from disk. On an unplugged drive
    EVERY file looks missing, so a cull would strip those rows — and their tags, labels, favorites,
    notes and scores — while the files sit safe on the disconnected disk.
    """
    out = set()
    for r in CONFIG.get('roots') or []:
        try:
            if r.get('path') and os.path.isdir(r['path']):
                out.add(r['key'])
        except OSError:
            pass
    return out


def find_folder_dupes(conn, image_id, reachable):
    """[(keeper_id, [doomed_id, ...]), ...] for the folder holding `image_id`, keepers first.

    A copy is a row with the same filename, the same exact byte size, and the same mtime — matched
    with a one-second tolerance rather than equality, because mtime is a raw float and a copy's can
    differ by sub-second rounding. (Truncating to whole seconds would instead split two copies that
    straddle a second boundary.) Name and size still gate it, so a false match would need two
    different files sharing a name, a byte size and a timestamp.

    Only reachable libraries take part, on both sides — see _reachable_root_ids.
    """
    row = conn.execute("SELECT root_id, folder FROM images WHERE id=?", (image_id,)).fetchone()
    if not row or row['root_id'] not in reachable:
        return []
    anchors = conn.execute(
        "SELECT id, filename, size, mtime FROM images WHERE root_id=? AND folder=? ORDER BY id",
        (row['root_id'], row['folder'])).fetchall()
    out = []
    for a in anchors:
        doomed = [r['id'] for r in conn.execute(
            "SELECT id, root_id FROM images "
            "WHERE filename=? AND size=? AND ABS(mtime - ?) < 1.0 AND id<>? ORDER BY id",
            (a['filename'], a['size'], a['mtime'], a['id'])) if r['root_id'] in reachable]
        if doomed:
            out.append((a['id'], doomed))
    return out


def curation_weight(conn):
    """image_id -> roughly how much curation work is invested in it: hand-applied tags, the label,
    a favorite, a note, a score. Used to pick which copy of a duplicate set to keep — losing the
    most-worked-on copy is the thing worth avoiding.

(source='hide' was excluded here too, until the Hidden mark was removed on 2026-09-08.)"""
    w = {}
    def bump(iid, n=1):
        w[iid] = w.get(iid, 0) + n
    for r in conn.execute("SELECT image_id, COUNT(*) c FROM tags "
                          "WHERE source IN ('user','fav') GROUP BY image_id"):
        bump(r['image_id'], r['c'])
    for r in conn.execute("SELECT id FROM images WHERE note IS NOT NULL AND TRIM(note) <> ''"):
        bump(r['id'])
    for r in conn.execute("SELECT image_id FROM quality WHERE reward IS NOT NULL"):
        bump(r['image_id'])
    return w


def find_all_dupes(conn, reachable, limit=None):
    """Every set of duplicate files across the reachable libraries, newest-heavy sets first.

    Same identity rule as the per-folder cull — same filename, same exact byte size, same timestamp
    to within a second — but with no anchor folder, so the keeper is chosen rather than given:
    the most-curated copy wins, ties break to the oldest file, then lowest id for determinism.

    Read-only. Returns [{filename, size, keep: row, dupes: [row, ...]}, ...].
    """
    buckets = {}
    for r in conn.execute("SELECT id, root_id, folder, filename, size, mtime FROM images "
                          "ORDER BY mtime, id"):
        if r['root_id'] in reachable:
            buckets.setdefault((r['filename'].lower(), r['size']), []).append(r)

    weight = curation_weight(conn) if buckets else {}
    sets = []
    for (_name, size), rows in buckets.items():
        if len(rows) < 2:
            continue
        # rows are mtime-ordered; cluster runs that sit within a second of each other rather than
        # bucketing on a rounded value, which would split two copies straddling a second boundary
        run = [rows[0]]
        for r in rows[1:] + [None]:
            if r is not None and abs(r['mtime'] - run[-1]['mtime']) < 1.0:
                run.append(r)
                continue
            if len(run) > 1:
                ranked = sorted(run, key=lambda x: (-weight.get(x['id'], 0), x['mtime'], x['id']))
                sets.append({'filename': ranked[0]['filename'], 'size': size,
                             'keep': ranked[0], 'dupes': ranked[1:]})
            run = [r] if r is not None else []
    sets.sort(key=lambda s: (-len(s['dupes']) * s['size'], s['filename']))
    return sets[:limit] if limit else sets


def curated_ids(conn, ids):
    """Which of `ids` carry curation worth telling the user about — a hand-applied tag or label, a
    favorite, a note, or a score. Machine tags (wd14/vlm) don't count; they'd be regenerated.

(source='hide' was excluded here too, until the Hidden mark was removed on 2026-09-08.)"""
    if not ids:
        return set()
    qm = ','.join('?' * len(ids))
    return {r['id'] for r in conn.execute(
        f"SELECT i.id FROM images i WHERE i.id IN ({qm}) AND ("
        " EXISTS(SELECT 1 FROM tags t WHERE t.image_id=i.id AND t.source IN ('user','fav'))"
        " OR EXISTS(SELECT 1 FROM quality q WHERE q.image_id=i.id"
        "           AND q.reward IS NOT NULL)"
        " OR (i.note IS NOT NULL AND TRIM(i.note) <> ''))", ids)}


def merge_curation(conn, keeper_id, doomed_ids):
    """Move any curation the doomed copies carry onto the keeper, before they are recycled.

    Never overwrites what the keeper already has — the keeper's own values always win, and this only
    fills gaps or unions. The point is that culling a copy can't cost you work. Must run BEFORE the
    delete: delete_by_ids drops each copy's tags and quality rows.
    """
    if not doomed_ids:
        return
    qm = ','.join('?' * len(doomed_ids))

    # tags, favorites, style tags AND the Hidden mark: the (image_id, tag, source) primary key makes
    # this idempotent, and the blanket copy is why storing Hidden as a tag row rather than a column
    # was worth it — the mark follows the surviving copy with no code here (pinned by test_hidden).
    # Labels are excluded — they're exclusive, so a union would leave the keeper with two.
    conn.execute(f"INSERT OR IGNORE INTO tags(image_id, tag, source, score) "
                 f"SELECT ?, tag, source, score FROM tags "
                 f"WHERE image_id IN ({qm}) AND tag NOT LIKE 'label:%'",
                 [keeper_id] + list(doomed_ids))

    # the exclusive label: keeper's own wins; otherwise adopt from the oldest copy that has one
    if not conn.execute("SELECT 1 FROM tags WHERE image_id=? AND tag LIKE 'label:%'",
                        (keeper_id,)).fetchone():
        lab = conn.execute(
            f"SELECT t.tag, t.source, t.score FROM tags t JOIN images i ON i.id = t.image_id "
            f"WHERE t.image_id IN ({qm}) AND t.tag LIKE 'label:%' ORDER BY i.mtime, i.id LIMIT 1",
            list(doomed_ids)).fetchone()
        if lab:
            conn.execute("INSERT OR IGNORE INTO tags(image_id, tag, source, score) VALUES(?,?,?,?)",
                         (keeper_id, lab['tag'], lab['source'], lab['score']))

    # note: adopt when the keeper has none, append when they differ — never drop typed text
    krow = conn.execute("SELECT note FROM images WHERE id=?", (keeper_id,)).fetchone()
    kn = ((krow['note'] if krow else '') or '').strip()
    extra, seen = [], {kn} if kn else set()
    for r in conn.execute(f"SELECT note FROM images WHERE id IN ({qm}) ORDER BY mtime, id",
                          list(doomed_ids)):
        n = (r['note'] or '').strip()
        if n and n not in seen:
            seen.add(n)
            extra.append(n)
    if extra:
        conn.execute("UPDATE images SET note=? WHERE id=?",
                     (NOTE_MERGE_SEP.join(([kn] if kn else []) + extra), keeper_id))

    # the Quality score: adopt when the keeper has none
    cols = ('reward',)
    kq = conn.execute("SELECT * FROM quality WHERE image_id=?", (keeper_id,)).fetchone()
    order = "ORDER BY image_id"
    if kq is None:
        src = conn.execute(f"SELECT * FROM quality WHERE image_id IN ({qm}) {order} LIMIT 1",
                           list(doomed_ids)).fetchone()
        if src:
            conn.execute(f"INSERT INTO quality(image_id, {', '.join(cols)}) "
                         f"VALUES({','.join('?' * (len(cols) + 1))})",
                         [keeper_id] + [src[c] for c in cols])
    else:
        for c in (c for c in cols if kq[c] is None):     # column names are from the fixed tuple
            src = conn.execute(f"SELECT {c} FROM quality WHERE image_id IN ({qm}) "
                               f"AND {c} IS NOT NULL {order} LIMIT 1", list(doomed_ids)).fetchone()
            if src:
                conn.execute(f"UPDATE quality SET {c}=? WHERE image_id=?", (src[c], keeper_id))


# ---- "Apply to folder": repeat one set's keep decision across the folder ----------------------
# You mark the keeper in one image set and tick "Apply to folder"; every OTHER set in that same
# folder with the same SHAPE loses everything except its member in the same role. The decision is
# taught by example, so nothing here is configured in the abstract.

def _member_role(row):
    """A set member's role: the saver's stamped stage when present, else the legacy filename role —
    the same rule api_image uses for the cull panes, then put through index_db.canonical_stage so a
    legacy MAIN/DET set and a stamped Raw/Detail set are one shape rather than two. 'unknown' names
    the case where neither could be read, so an unreadable set still has a shape and can only ever
    match other equally unreadable sets."""
    return index_db.canonical_stage(
        row['set_stage'] or index_db._set_role(row['filename']) or 'unknown')


def _set_shape(rows):
    """The sorted tuple of member roles — what makes two sets 'the same shape'. Sorted so pane
    ORDER can't change the shape; matching on it is what keeps a (main, refine) set out of a
    (main, det, refine) demonstration."""
    return tuple(sorted(_member_role(r) for r in rows))


def find_setcull_sets(conn, image_id, keep_role):
    """(matches, stats) for repeating image_id's keep decision across its folder.

    matches is [(keeper_id, [doomed_id, ...]), ...], the anchor's own set included — it is one of
    the sets in the folder, and excluding it would leave the set you demonstrated on as the only
    survivor. Raises ValueError with a user-facing reason when the demonstration itself can't be
    applied.

    Scope is the anchor's root_id + the EXACT folder string: equality, never a LIKE prefix, so a
    subfolder holding the same shape is untouched.
    """
    keep_role = index_db.canonical_stage(keep_role)
    anchor = conn.execute("SELECT id, root_id, folder, group_id FROM images WHERE id=?",
                          (image_id,)).fetchone()
    if not anchor:
        raise ValueError('image not found')
    if not anchor['group_id']:
        raise ValueError('this image is not part of a set')
    # An unreachable library is refused outright, not skipped: _recycle_ids purges any row whose
    # file is missing, and on a downed share EVERY file looks missing — see _reachable_root_ids.
    if anchor['root_id'] not in _reachable_root_ids():
        raise ValueError('that library is offline')

    cols = "id, filename, ext, set_stage, group_id, root_id, folder"
    anchor_rows = conn.execute(f"SELECT {cols} FROM images WHERE group_id=?",
                               (anchor['group_id'],)).fetchall()
    shape = _set_shape(anchor_rows)
    if keep_role not in shape:
        raise ValueError('that role is not in this set')
    if shape.count(keep_role) > 1:
        raise ValueError('this set has two %s images, so the choice is ambiguous' % keep_role)

    # Every group with a member in this exact folder, then the groups' FULL membership — a group
    # with any member living elsewhere is dropped, so the folder scope stays exact.
    gids = [r['group_id'] for r in conn.execute(
        "SELECT DISTINCT group_id FROM images WHERE root_id=? AND folder=? AND group_id IS NOT NULL",
        (anchor['root_id'], anchor['folder']))]
    groups = {}
    for chunk in (gids[i:i + 500] for i in range(0, len(gids), 500)):
        qm = ','.join('?' * len(chunk))
        for r in conn.execute(f"SELECT {cols} FROM images WHERE group_id IN ({qm})", chunk):
            groups.setdefault(r['group_id'], []).append(r)

    matches, total_sets, skipped_shape = [], 0, 0
    for gid, rows in groups.items():
        if len(rows) < 2:
            continue
        if any(r['root_id'] != anchor['root_id'] or r['folder'] != anchor['folder'] for r in rows):
            continue
        # groups holding a video are still+video PAIRS, not image sets — the same test api_image uses
        if any((r['ext'] or '').lower() in index_db.VIDEO_EXTS for r in rows):
            continue
        total_sets += 1
        if _set_shape(rows) != shape:
            skipped_shape += 1
            continue
        # Exactly one keeper is GUARANTEED here, not hoped for: the shape is a sorted multiset of
        # roles, it equals the anchor's, and the anchor was checked to hold the kept role once. So
        # there is no "ambiguous match" case to count — matching on shape is what buys that.
        keeper = next(r for r in rows if _member_role(r) == keep_role)
        matches.append((keeper['id'], sorted(r['id'] for r in rows if r['id'] != keeper['id'])))

    stats = {'sets': len(matches), 'total_sets': total_sets, 'files': sum(len(d) for _k, d in matches),
             'skipped_shape': skipped_shape,
             'keep_role': keep_role, 'folder': anchor['folder'], 'root_id': anchor['root_id']}
    return matches, stats


BASE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE, 'app')
DOCS_DIR = os.path.join(BASE, 'docs')
# The in-app Help window's sources. A fixed map, not a path parameter: the name arrives from a
# query string, and joining user text onto a directory is how a doc viewer becomes a file reader.
HELP_DOCS = {'help': 'help.md'}
# config.json + data/ live next to the app by default, but CV_CONFIG / CV_DATA can
# redirect them elsewhere (e.g. so a throwaway test run never touches the real setup).
DATA_DIR = os.environ.get('CV_DATA') or os.path.join(BASE, 'data')
ROOTS_DIR = os.path.join(DATA_DIR, 'roots')       # legacy per-root indexes: <key>.db (source for the merge)
THUMBS_DIR = os.path.join(DATA_DIR, 'thumbs')     # shared cache (keyed by absolute file path)
# Cache for serve_drag_png. Separate from thumbs/ because these are a different KIND of artifact —
# full-size PNGs carrying metadata, made on demand for one purpose — and mixing them into the
# thumbnail tree would put them in the path of every thumbnail size sweep.
DRAGPNG_DIR = os.path.join(DATA_DIR, 'dragpng')
CONFIG_PATH = os.environ.get('CV_CONFIG') or os.path.join(BASE, 'config.json')
# THE ONE SETTING THAT NEEDS CHANGING FROM OUTSIDE THE APP, in a file that holds nothing else.
# A port already in use stops the app starting, so it cannot be fixed from inside the app -- and the
# answer used to be "add a `port` line to config.json", the only hand-edit this app ever asked for.
# That file also holds every library, snapshot and setting, so one stray comma in it cost all of
# them. A file containing a single number cannot be broken that expensively: garbage here is worth
# exactly the default port.
PORT_PATH = os.path.join(os.path.dirname(CONFIG_PATH), 'port.txt')
# The single merged DB (Option C): every root's images live here, tagged with a `root_id` column.
# db() always opens this; ACTIVE just names the current management-target root. The per-root
# <key>.db files are kept intact as a fallback and are the one-time merge's source.
LIBRARY_DB = os.path.join(DATA_DIR, 'library.db')

# Content types for served originals (stills + video).
MEDIA_CTYPES = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif',
    '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
    '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.wav': 'audio/wav',
    '.opus': 'audio/ogg', '.m4a': 'audio/mp4',
}

# ---- A song's headline -------------------------------------------------------------------------
# Nothing inside an audio file names it, so the card's headline falls through three sources in
# order of how much it was chosen ON PURPOSE: the name typed into the VV Naming node, then the
# song's own first sung line, then its genre.
#
# The catch the chain exists for: that typed name is often a BATCH prefix, not a title —
# "MM_Music_Aug15" on every song made that day — and printing it would put one identical headline
# on a whole screen of cards.
#
# Telling the two apart by COUNTING is wrong, and it took two goes to see why. The rule was first
# "shared by more than three songs is a prefix", which the author rejected from how he actually works:
# **variations on one song are the normal case**. They all carry that song's name, the shared name
# IS the title, and what tells the takes apart is the timestamp in the filename — not the name. A
# rule keyed on how many files share a name gets that exact case backwards, and the more takes you
# make of a song you liked, the more confident it is that its name means nothing.
#
# So the only test left is what the songs themselves ARE: a name spanning more than one genre is a
# prefix over unrelated work, while takes on one song share its genre and keep their name. Nothing
# counts anything, and a name repeated across fifty variations still prints.
# Section markers, in the two shapes seen in real lyrics: "[Chorus]" and "**[Verse 1]**".
_LYRIC_MARKER_RE = re.compile(r'^[\s.*_~-]*\[[^\]]*\][\s.*_~-]*$')
_LYRIC_MIN_WORDS = 3          # "Mmm..." and "(rain on the window)" are atmosphere, not a title


def _first_lyric_line(lyrics):
    """The first line of a song that reads like a title — or None if it has no such line."""
    for raw in (lyrics or '').splitlines():
        line = raw.strip()
        if not line or _LYRIC_MARKER_RE.match(line):
            continue
        if line.startswith('(') and line.endswith(')'):    # an aside, not the lyric
            continue
        line = line.strip('*_ ')
        if len(line.split()) >= _LYRIC_MIN_WORDS:
            return line
    return None


# CACHED, BECAUSE THE ANSWER DOES NOT DEPEND ON THE PAGE. The query below is a GROUP BY over the
# whole images table, and it used to run on every page of results that happened to contain a single
# song -- a full-table aggregate, per page, to compute a library-wide fact. A trace on 5,883 images
# caught it costing 124ms on one scroll page (200ms against the usual 74) and 147ms of a 257ms
# random-sort search. The cost grows with the library, and it spreads as songs do.
#
# INVALIDATED WHEN A SCAN ENDS, which is the only thing that can change song_name or song_genre.
# Held for the process otherwise. A stale entry would mis-title a song card, not lose data.
_generic_songs = None
_generic_songs_lock = threading.Lock()


def clear_song_name_cache():
    """Called when a scan finishes — see _generic_song_names."""
    global _generic_songs
    with _generic_songs_lock:
        _generic_songs = None


def _generic_song_names(conn):
    """Names that are a batch prefix rather than a title — see the note above."""
    global _generic_songs
    with _generic_songs_lock:
        if _generic_songs is not None:
            return _generic_songs
    try:
        got = {r[0] for r in conn.execute(
            "SELECT song_name FROM images WHERE song_name IS NOT NULL AND song_name <> '' "
            "GROUP BY song_name HAVING COUNT(DISTINCT song_genre) > 1")}
    except Exception:
        return set()          # not cached: a failed read must not become the answer for the session
    with _generic_songs_lock:
        _generic_songs = got
    return got


def _song_headline(row, generic_names):
    """What to print at the top of a song's card. Never empty — the filename is the last resort."""
    name = (row['song_name'] or '').strip()
    if name and name not in generic_names:
        return name
    return (_first_lyric_line(row['lyrics'])
            or (row['song_genre'] or '').strip()
            or _readable_stem(row['filename']))


# Last resort, for an instrumental under a batch prefix: nothing named it, it has no words to quote
# and its caption never said a genre. The filename is all that is left — but the raw stem prints the
# machine tail too (`..._19-36-14~vv3gflke_00001`), which is noise wearing a name. Strip the run code
# and the counter and keep the timestamp, since that is the only part telling two of them apart.
_STEM_NOISE_RE = re.compile(r'~vv[0-9a-z]{4,10}|_\d{5,}$', re.I)


def _readable_stem(filename):
    stem = os.path.splitext(filename or '')[0]
    for _ in range(3):                      # counter, then code, then a trailing separator
        stem = _STEM_NOISE_RE.sub('', stem).rstrip('_-. ')
    return stem


def _cover_url(row, cover_id=None):
    """A song's cover art, from whichever of the two sources it has — or None.

    Embedded art wins: it is part of the file, so it is right even if the folder is reorganised.
    A cover saved beside the song (same run code, so already one group) is the fallback, and is
    what a workflow can produce today without any change to the saver node.
    """
    if row['has_cover']:                # the song's own thumbnail IS its embedded cover
        return (f'/thumb/{row["id"]}?v={int(row["mtime"] or 0)}'
                f'&r={row["root_id"] or ""}&s={thumbs_mod.THUMB_SIZE}')
    if cover_id:
        return (f'/thumb/{cover_id}?v={int(row["mtime"] or 0)}'
                f'&r={row["root_id"] or ""}&s={thumbs_mod.THUMB_SIZE}')
    return None


def _song_rows(conn, ids):
    """{id: row} of the song columns, for the handful of rows a page actually returns.

    These used to ride in the grid query's column list and were dragged through three
    window-function CTEs over the whole table — 1.19x on every page, in a library with no audio at
    all, for columns nothing filters or sorts by. Fetched here instead, against ~60 ids.
    """
    if not ids:
        return {}
    qm = ','.join('?' * len(ids))
    imgs = sorted(index_db.IMAGE_EXTS)
    iqm = ','.join('?' * len(imgs))
    # The cover comes along here too, as a correlated lookup over ~60 rows rather than a window
    # function over the whole library. `s.group_id IS NOT NULL` matters: without it every song
    # without a group would match every other ungrouped row.
    return {r['id']: r for r in conn.execute(
        f"SELECT s.id, s.duration, s.peaks, s.lyrics, s.song_name, s.song_bpm, s.song_key, "
        f"s.song_genre, s.song_vocal, s.has_cover, s.thumb, s.mtime, s.root_id, s.filename, "
        f"(SELECT c.id FROM images c WHERE s.group_id IS NOT NULL AND c.group_id = s.group_id "
        f" AND LOWER(c.ext) IN ({iqm}) ORDER BY c.id LIMIT 1) AS cover_id "
        f"FROM images s WHERE s.id IN ({qm})", imgs + list(ids))}


def _song_fields(row, generic_names):
    """The song half of a grid item: what the card draws in place of a thumbnail."""
    try:
        peaks = json.loads(row['peaks']) if row['peaks'] else None
    except Exception:
        peaks = None
    return {
        'is_audio': True,
        'title': _song_headline(row, generic_names),
        'duration': row['duration'],
        'peaks': peaks,
        'bpm': row['song_bpm'],
        'key': row['song_key'],
        'genre': row['song_genre'],
        'vocal': row['song_vocal'],
    }


def _build_drag_png(video_path):
    """One frame of a video as PNG bytes, carrying that video's own ComfyUI graphs. None if no
    frame can be decoded (no ffmpeg, or an unreadable file)."""
    im = thumbs_mod._video_poster_image(video_path)   # the same poster frame the thumbnail uses
    if im is None:
        return None
    meta = comfy_meta.read_video_meta(video_path)
    info = PngImagePlugin.PngInfo()
    # `workflow` is the one that matters — it is what a drop into ComfyUI restores. `prompt` rides
    # along because it is what OUR OWN reader traces, so an exported frame stays self-describing.
    # Written with add_text, i.e. exactly the call ComfyUI's own SaveImage uses, so the chunks come
    # out in the form its loader already expects.
    for k in ('workflow', 'prompt'):
        if meta.get(k):
            info.add_text(k, meta[k])
    buf = io.BytesIO()
    with im:
        im.convert('RGB').save(buf, 'PNG', pnginfo=info, compress_level=6)
    return buf.getvalue()


def _build_song_drag_png(audio_path):
    """A SONG, as a picture ComfyUI accepts on a drop: its own `workflow` and `prompt` graphs in PNG
    text chunks. None if the file carries no graph at all.

    Same reasoning as the video route above — a browser only carries a file between windows when
    that file is the one behind the `<img>` being dragged, so the dragged thing has to BE a picture.
    ComfyUI reads the graph out of the chunks and never looks at the pixels.

    The picture is the song's cover art when it has any, and a drawing of its waveform when it
    doesn't. Deliberately not a generic placeholder: dropped onto a Load Image node either one is a
    true picture OF this song rather than a stand-in for one.
    """
    frames = comfy_meta.read_id3_txxx(audio_path)
    if not (frames.get('workflow') or frames.get('prompt')):
        return None
    im = None
    art = comfy_meta.read_id3_cover(audio_path)
    if art:
        try:
            im = Image.open(io.BytesIO(art)).convert('RGB')
        except Exception:
            im = None
    if im is None:
        _dur, peaks = thumbs_mod.audio_shape(audio_path)
        im = _waveform_image(peaks)
    info = PngImagePlugin.PngInfo()
    for k in ('workflow', 'prompt'):
        if frames.get(k):
            info.add_text(k, frames[k])
    buf = io.BytesIO()
    with im:
        im.convert('RGB').save(buf, 'PNG', pnginfo=info, compress_level=6)
    return buf.getvalue()


def _waveform_image(peaks, size=512):
    """The song's loudness drawn as a picture, for a song with no cover art. Flat mid-grey when
    there are no peaks either (no ffmpeg) — the chunks are the payload, so it still drags."""
    im = Image.new('RGB', (size, size), (20, 22, 26))
    if not peaks:
        return im
    d = ImageDraw.Draw(im)
    n = len(peaks)
    step = size / n
    mid = size / 2
    for i, p in enumerate(peaks):
        h = max(2, (float(p) / 100.0) * (size * 0.7))
        x = i * step + step / 2
        d.line([(x, mid - h / 2), (x, mid + h / 2)], fill=(79, 140, 255), width=max(1, int(step * 0.6)))
    return im


# ---- Send to ComfyUI -------------------------------------------------------------------------
# The one-click equivalent of dragging a file onto the ComfyUI window. It needs a helper INSIDE
# ComfyUI (comfy_vv_bridge) because a canvas can only be loaded from its own page — no outside
# program can reach it, which is why dragging is otherwise the only way.
#
# The port is a constant rather than a setting: there is no UI for it, and a wrong one fails
# visibly ("ComfyUI isn't answering on port 8188") rather than quietly. If ComfyUI ever moves,
# this line is the one to change.
COMFY_URL = os.environ.get('CV_COMFY_URL') or 'http://127.0.0.1:8188'
COMFY_TIMEOUT = 4


def workflow_json(path):
    """The ComfyUI `workflow` graph embedded in a file, as raw JSON text — or None.

    One place for all three carriers, because "does this have a workflow" is asked twice: once to
    decide whether the Send button exists at all, and once to actually send.
    """
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in index_db.AUDIO_EXTS:
            return comfy_meta.read_id3_txxx(path).get('workflow') or None
        if ext in index_db.VIDEO_EXTS:
            return (comfy_meta.read_video_meta(path) or {}).get('workflow') or None
        return (comfy_meta.read_png_text_chunks(path) or {}).get('workflow') or None
    except Exception:
        return None


def comfy_bridge_ready():
    """True when the bridge is installed AND ComfyUI is running. Distinguishing the two is the
    whole reason the bridge answers a ping: without it, a send would be the only way to find out,
    and a silent failure would look exactly like a successful one."""
    try:
        with urllib.request.urlopen(COMFY_URL + '/vv_bridge/ping', timeout=COMFY_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def _file_url(iid, mtime, rkey, filename=None):
    """The media URL for an original. Root-scoped and versioned like every media URL (see
    api_search), plus the original's NAME as a trailing path segment where we know it — a browser
    names a dragged-out file from the URL's last segment, which is what makes a drop into ComfyUI
    arrive as itself instead of as the row number."""
    tail = '/' + urllib.parse.quote(_safe_filename(filename)) if filename else ''
    return f'/file/{iid}{tail}?v={int(mtime or 0)}&r={rkey}'


def _safe_filename(name, fallback='image'):
    """A basename fit to sit inside a `filename="…"` header and to be written to disk by whatever
    receives it. Conservative on purpose: a quote or a backslash in the header would break the
    parse, and non-ASCII names travel badly through HTTP headers."""
    base = os.path.basename(name or '')
    stem, ext = os.path.splitext(base)
    stem = re.sub(r'[^A-Za-z0-9 _.\-]', '_', stem).strip() or fallback
    ext = re.sub(r'[^A-Za-z0-9.]', '', ext)
    return stem + ext

# ─── App metadata — change this one line to rename the app everywhere ───
APP_NAME = 'VV Curator'

# THE RELEASE PEOPLE SEE, and the thing a download is called. Deliberately a typed number rather
# than BUILD below, which is derived: a build stamp answers "did update.bat pick this up?" and is
# right for that, but it cannot be compared to what is published, it means nothing to a reader, and
# "Welcome to version 2026-09-12 14:03" is not a sentence.
#
# BUMP IT WHEN SOMETHING IS RELEASED, not when something is changed — it is the number in the
# welcome after an update, and it is what a version check would compare against. READER_VERSION in
# index_db.py is a different number for a different job: what the app can extract from a file. They
# move independently, and a release that reads nothing new does not touch it.
APP_VERSION = '1.1'

# ---- telling people a newer version exists ----------------------------------------------------
# WHERE A RELEASE IS ANNOUNCED: owner/name of the GitHub repo. This is the whole reason the project
# is on GitHub rather than a zip attached to a forum post -- a release exposes a small JSON endpoint
# giving the latest version, its notes and the download's address, for one plain HTTPS request with
# no account and no key. A zip on a post has no address to ask.
#
# BLANK TURNS THE FEATURE OFF, which is what makes this safe to ship before the repo exists: no
# request, no icon, no setting that appears to do something.
#
# WHILE THE REPO IS PRIVATE this is set and harmless. GitHub's API answers 404 for a private repo
# without a token, and 404 is already one of the quiet-no cases -- no icon, no error, nothing said,
# exactly as if the machine were offline. Making it public is the only switch: the check starts
# working with no code change and no redeploy. That is deliberate, so the announcement and the
# feature going live are one action rather than two.
UPDATE_REPO = 'vidi-vinci/vv-curator'
UPDATE_API = 'https://api.github.com/repos/%s/releases/latest'
# Short on purpose. Nobody is waiting for this answer -- the client asks without blocking on it, and
# a missed check costs one launch's notice, where a long hang would hold a worker thread for a fact
# that does not matter today.
UPDATE_TIMEOUT = 4.0

# Asked ONCE PER PROCESS, and the answer kept here. Not because GitHub's 60-an-hour limit is near,
# but because the honest frequency for "is there a newer version" is "when you start the app": a
# timer that keeps asking while you work is the shape the design deliberately rejected.
# None = not asked yet. A dict = asked, and this is the answer, including a negative one.
_update_seen = None
_update_lock = threading.Lock()


def _ver_tuple(s):
    """'v1.10' -> (1, 10), so 1.10 sorts ABOVE 1.9 instead of below it.

    A string compare is the trap this exists to avoid: '1.10' < '1.9' is true of text and false of
    versions, and it goes wrong at exactly the tenth release rather than in testing.

    ONLY THE LEADING NUMBERS COUNT, and a suffix is DROPPED rather than read as another piece.
    Splitting on '-' as well looked equivalent and was not: '1.0-beta' became (1, 0, 0), which is
    greater than (1, 0), so a prerelease announced itself as newer than the very release it
    precedes. Truncating instead makes it equal, and equal does not announce. The same reasoning
    covers anything unparseable -- it reads as (0,), because a tag nobody can make sense of must
    never look NEWER than what is running. That is the direction that pops a notice on every launch
    forever, with nothing the user can do to satisfy it.

    (A genuine '1.1-beta' does then read as 1.1. GitHub's own `prerelease` flag is what excludes
    those, and check_for_update honours it before ever getting here.)
    """
    m = re.match(r'(\d+(?:[._]\d+)*)', (s or '').strip().lstrip('vV'))
    if not m:
        return (0,)
    return tuple(int(p) for p in re.split(r'[._]', m.group(1)))


def check_for_update():
    """One HTTPS GET to GitHub's releases API. Returns a dict, never raises.

    IT CHECKS AND TELLS. It does not download and it does not install: this app updates by copying
    a folder, and a program that overwrites itself while running is a class of problem worth simply
    not having. The feature ends at "1.4 is out", with the link.

    NOTHING ABOUT THE USER OR THEIR LIBRARY IS SENT -- it is a GET of a public URL, with no query,
    no body and no identifying header beyond a user agent naming the app. It is still the app's only
    outbound request, which is why the README discloses it and why the user is asked before the
    first one.

    Every failure is the same answer: no. Offline, rate-limited, no releases published yet, a repo
    that does not exist, a tag nobody can parse -- none of these are worth a message, because the
    user did not ask a question. They just mean no icon this launch.
    """
    if not UPDATE_REPO:
        return {'available': False}
    req = urllib.request.Request(
        UPDATE_API % UPDATE_REPO,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': '%s/%s' % (APP_NAME.replace(' ', ''), APP_VERSION)})
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return {'available': False}
    if not isinstance(j, dict):
        return {'available': False}
    # A draft is not published and a prerelease is not for everyone; either one reaching a user
    # would be this feature announcing something they cannot sensibly install.
    if j.get('draft') or j.get('prerelease'):
        return {'available': False}
    tag = (j.get('tag_name') or '').strip()
    if not tag:
        return {'available': False}
    if _ver_tuple(tag) <= _ver_tuple(APP_VERSION):
        return {'available': False}
    return {'available': True,
            # The NAME is what a human titled the release; the tag is the fallback because a
            # release with no title is a real thing on GitHub and "version " is not a sentence.
            'version': tag.lstrip('vV'),
            'name': (j.get('name') or '').strip(),
            'notes': (j.get('body') or '').strip(),
            'url': j.get('html_url') or ('https://github.com/%s/releases' % UPDATE_REPO)}


def update_status():
    """The cached answer, asking GitHub at most once per process.

    The lock is not about the cache being precious -- it is so two clients loading at once (a
    reload while the first page is still booting) make ONE request rather than two.
    """
    global _update_seen
    with _update_lock:
        if _update_seen is None:
            _update_seen = check_for_update()
        return _update_seen


def build_stamp():
    """When the code you are running was last changed — the app's version, shown in the header.

    DERIVED, never typed. The deployed copy has no .git, so a commit hash is unavailable in exactly
    the place the question gets asked ("did update.bat actually pick this up?"). update.bat
    robocopies with timestamps preserved, so the newest mtime across the code files IS the answer to
    that question, and there is no version string for anyone to forget to bump.

    Read once at import: these files cannot change under a running server without a restart, and
    the stamp is asked for on every settings load.
    """
    files = ['server.py', 'index_db.py', 'comfy_meta.py', 'thumbs.py', 'model_hash.py',
             os.path.join('app', 'app.js'), os.path.join('app', 'index.html'),
             os.path.join('app', 'style.css')]
    newest = 0
    for rel in files:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(BASE, rel)))
        except OSError:
            pass
    if not newest:
        return '—'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(newest))


BUILD = build_stamp()
# ─────────────────────────────────────────────────────────────────────────

# App-wide "General" settings (persisted in config.json alongside `miner` and `theme`).
# `show_hidden` WAS HERE and went with the Hidden mark on 2026-09-08 (see below).
#
# `hide_large` WAS HERE and is gone (2026-09-08). It hid every image over 2000px from the grid,
# the counts and the facets, and the reason it existed -- speed -- had stopped being true: the grid
# draws cached thumbnails and never the originals, so hiding big files saved nothing. What was left
# was an invisible filter that removed a user's best work with nothing on screen to say so, and
# half of the recycle bug in test_recycle_completeness.py. An old config may still carry the key;
# _normalize_config drops it.
DEFAULT_GENERAL = {'autoplay': False, 'keep_behavior': 'next',
                   'confirm_recycle': True, 'models_dir': '',
                   # Snapshots is a whole rail section some people will never use (the author's own words
                   # on proposing this: he barely uses it). ON by default -- an existing install
                   # must not lose a feature to a setting arriving.
                   'show_snapshots': True,
                   # The recycle-folder nudge. ON by default: the folder it watches is one nothing
                   # ever empties, and a default of off would mean the safeguard only reaches people
                   # who already knew to look for it.
                   'recycle_warn': True,
                   'recycle_warn_files': RECYCLE_WARN_FILES,
                   'recycle_warn_gb': RECYCLE_WARN_GB,
                   # Checking GitHub for a newer version. ON by default, but the default is not
                   # what decides it -- the first-run pop-up asks, with this as the pre-ticked
                   # answer, because this is the app's only outbound request and the README says
                   # nothing is uploaded. Someone who unticks it gets no icon now and none for the
                   # next version either; Settings switches it back on.
                   'update_check': True}


def _bool_or(val, default):
    """Coerce a stored boolean, treating None as ABSENT rather than as False.

    `bool(d.get(k, default))` looks equivalent and isn't: a key present with an explicit null
    returns None, and bool(None) is False. For a setting that defaults ON that silently flips it
    OFF — the one direction a safeguard may not fail in. Absence and null mean the same thing
    here: "no answer", so use the default."""
    return default if val is None else bool(val)

# ---- extensions -------------------------------------------------------------
# An extension is a FOLDER holding an extension.json, not a Python module. It never imports the
# app and the app never imports it: the app spawns its worker in the extension's OWN venv and
# talks to it in JSON lines. That is the whole contract, and it is why an extension cannot break
# the app by pulling an incompatible torch — the ComfyUI custom-node lesson applied.
#
# AN EXTENSION IS ITS FOLDER, ENTIRELY. Worker, setup script and venv all sit inside it and its
# manifest names them relatively, so a release decides what ships by leaving a folder out and the
# app finds nothing there rather than finding half of something. The quality scorer was the
# exception until 2026-09-13 -- its parts lived at the app root and its manifest reached back out
# with ../../, which meant it could not be left out at all.
EXT_DIR = os.path.join(BASE, 'extensions')

# What a manifest may declare. `produces` is what the extension contributes to an image —
# 'score' (a number), 'tags' (rows in the tags table), or 'text' (prose, SHOWN AND NOT STORED).
# Paths are resolved relative to the folder holding the manifest.
#
# 'text' is deliberately the only kind with nowhere to land. A score and a tag are both things the
# library can filter on, so they earn a column; prose cannot be filtered, and an answer kept
# forever is an answer that has to be cleared, versioned and searched. Until there is a reason to
# keep one, a text extension answers the question in front of you and the answer goes away with
# the image — which is also what makes a model that turns out to be bad at this cost nothing.
EXT_PRODUCES = ('score', 'tags', 'text')

# The field types an extension may ask for in its `settings` block. An extension declares fields;
# THE APP DRAWS THEM. That is the whole reason this is a schema rather than a page supplied by the
# extension: a panel built from the app's own components cannot drift from the rest of Settings,
# and an extension author cannot get the house style wrong because they never touch it.
EXT_FIELD_TYPES = ('text', 'password', 'number', 'checkbox', 'textarea')


def _ext_resolve(folder, rel):
    """Resolve a manifest path against the extension's own folder. Blank -> None."""
    rel = (rel or '').strip()
    return os.path.normpath(os.path.join(folder, rel)) if rel else None


def _ext_fields(raw):
    """Whitelist one manifest's `settings` block into a list of field descriptors.

    Raises ValueError on anything malformed, which the caller turns into the manifest's `error`.
    A BAD FIELD IS AN ERROR, NOT A DROPPED ONE — the quiet alternative is a setting the user can
    never reach and a worker reading a blank where it expected a URL, which looks like the model
    being broken rather than the manifest being wrong. Same reasoning as `requires`: an extension
    is entitled to say what it needs, and entitled to be told when it asked badly."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('`settings` must be a list')
    out, seen = [], set()
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            raise ValueError('setting %d is not an object' % (i + 1))
        key = str(f.get('key') or '').strip()
        if not key:
            raise ValueError('setting %d has no key' % (i + 1))
        if key in seen:
            raise ValueError('two settings share the key "%s"' % key)
        seen.add(key)
        ftype = str(f.get('type') or 'text').strip().lower()
        if ftype not in EXT_FIELD_TYPES:
            raise ValueError('setting "%s" has an unknown type "%s"' % (key, ftype))
        out.append({'key': key, 'type': ftype,
                    'label': str(f.get('label') or key).strip() or key,
                    'placeholder': str(f.get('placeholder') or '').strip(),
                    'help': str(f.get('help') or '').strip(),
                    'default': f.get('default')})
    return out


def _read_extension(folder):
    """Read one extensions/<id>/extension.json. Returns a dict, or None if there is no manifest.

    A BROKEN manifest still returns an entry, carrying `error` — an extension that fails to load
    has to be visible on the Extensions tab, because the alternative is a folder the user
    installed that simply never appears and never says why."""
    ext_id = os.path.basename(folder)
    mf = os.path.join(folder, 'extension.json')
    if not os.path.exists(mf):
        return None
    base = {'id': ext_id, 'name': ext_id, 'version': '', 'description': '',
            'produces': '', 'worker': None, 'setup': None, 'venv': None, 'requires': [],
            'settings': [], 'can_test': False,
            'installed': False, 'ready': False, 'error': None}
    try:
        with open(mf, 'r', encoding='utf-8') as f:
            m = json.load(f)
        if not isinstance(m, dict):
            raise ValueError('extension.json is not an object')
    except Exception as e:
        base['error'] = 'Could not read extension.json: %s' % e
        return base
    base['name'] = str(m.get('name') or ext_id).strip() or ext_id
    base['version'] = str(m.get('version') or '').strip()
    base['description'] = str(m.get('description') or '').strip()
    produces = str(m.get('produces') or '').strip().lower()
    base['produces'] = produces if produces in EXT_PRODUCES else ''
    base['worker'] = _ext_resolve(folder, m.get('worker'))
    base['setup'] = _ext_resolve(folder, m.get('setup'))
    base['venv'] = _ext_resolve(folder, m.get('venv'))
    # Extra files that must exist before the extension can actually run -- a model checkpoint,
    # typically. WITHOUT THIS, "installed" means only that a venv folder was created, which is why
    # the tagger reported Ready on a setup that had built its venv and then failed: The author got a
    # green status and a Python traceback from the same install. An extension is entitled to say
    # what finished looks like for it.
    base['requires'] = [p for p in (_ext_resolve(folder, r) for r in (m.get('requires') or []))
                        if p]
    # Whether the worker can check its own setup on demand. See run_ext_test: the app never learns
    # what "working" means for an extension — it asks the worker and repeats the answer.
    base['can_test'] = bool(m.get('test'))
    # What the user can configure. Declaring any field is what earns the extension its own tab in
    # Settings; declaring none leaves it a row in the installed list, which is all the scorer and
    # the tagger have ever needed.
    try:
        base['settings'] = _ext_fields(m.get('settings'))
    except ValueError as e:
        base['error'] = 'Its settings are malformed: %s.' % e
        return base
    if not base['worker']:
        base['error'] = 'extension.json names no worker.'
        return base
    if not os.path.exists(base['worker']):
        base['error'] = 'Its worker is missing (%s).' % os.path.basename(base['worker'])
        return base
    # An extension may declare no venv at all — a pure-stdlib worker needs none, and then there is
    # nothing to set up and it is installed the moment it is unzipped.
    base['installed'] = ((not base['venv']) or os.path.exists(ext_python(base))) \
        and all(os.path.exists(p) for p in base['requires'])
    base['ready'] = base['installed']
    return base


def ext_python(ext):
    """The interpreter that runs this extension's worker: its venv's, or the app's own."""
    return os.path.join(ext['venv'], 'Scripts', 'python.exe') if ext['venv'] else sys.executable


def list_extensions():
    """Every extension folder, read fresh — so a venv built while the app is open shows up on the
    next look at the tab rather than on the next restart."""
    out = []
    try:
        names = sorted(os.listdir(EXT_DIR))
    except OSError:
        return out
    for name in names:
        folder = os.path.join(EXT_DIR, name)
        if not os.path.isdir(folder):
            continue
        ext = _read_extension(folder)
        if ext:
            ext['enabled'] = ext_enabled(ext['id'])
            ext['active'] = bool(ext['enabled'] and ext['ready'] and not ext['error'])
            out.append(ext)
    return out


def get_extension(ext_id):
    folder = os.path.join(EXT_DIR, ext_id)
    if not os.path.isdir(folder):
        return None
    ext = _read_extension(folder)
    if ext:
        ext['enabled'] = ext_enabled(ext['id'])
        ext['active'] = bool(ext['enabled'] and ext['ready'] and not ext['error'])
    return ext


def merge_ext_values(ext, sent, cur):
    """Fold what the user has on screen (`sent`) into what is stored (`cur`), against the schema.

    ONE COPY, because two callers need the answer and must not disagree: Save writes the result to
    disk, and Test hands it to the worker without writing anything. A Test that used the stored
    values while Save used the typed ones would pass on an address you had not saved yet — or fail
    on one you had — and either way the button would be lying about the thing it exists to check.

    A key the manifest doesn't declare is dropped. An EMPTY PASSWORD MEANS UNCHANGED, not cleared:
    the client is never sent the stored value so it cannot send it back, and a blank submit would
    otherwise wipe the key every time any other field on the panel was touched.
    """
    out = dict(cur)
    for f in ext['settings']:
        if f['key'] not in sent:
            continue
        v = sent[f['key']]
        if f['type'] == 'checkbox':
            out[f['key']] = bool(v)
        elif f['type'] == 'number':
            try:
                out[f['key']] = float(v)
            except (TypeError, ValueError):
                out[f['key']] = ''
        else:
            s = str(v if v is not None else '')[:MAX_EXT_SETTING]
            if f['type'] == 'password' and not s.strip():
                continue
            out[f['key']] = s
    return out


def ext_settings_for(ext):
    """One extension's settings: its manifest defaults, overlaid with what the user has saved.
    Keyed on the manifest, so a field removed from a manifest stops being passed to the worker
    without the stored value being destroyed — rename the folder back and the endpoint is
    still there."""
    saved = CONFIG.get('ext_settings', {}).get(ext['id'], {})
    out = {}
    for f in ext.get('settings') or []:
        d = f.get('default')
        if f['type'] == 'checkbox':
            out[f['key']] = bool(saved.get(f['key'], d if d is not None else False))
        else:
            v = saved.get(f['key'], d if d is not None else '')
            out[f['key']] = v if isinstance(v, (str, int, float)) else ''
    return out


def _extensions_payload():
    """The Extensions tab's view of the world. Absolute paths stay on the server — the client
    needs to know THAT there is a setup script, never where it is, because it can only ask the
    server to run it by id.

    A PASSWORD FIELD'S VALUE NEVER LEAVES THE SERVER. The client is told only whether one is set,
    which is enough to render the field honestly and enough to edit it, and it keeps an API key
    out of /api/config — a payload that turns up in debug traces and in anything that logs a
    response. Submitting the field empty leaves the stored value alone; see api_ext_settings."""
    out = []
    for e in list_extensions():
        vals = ext_settings_for(e)
        secrets = {f['key'] for f in e['settings'] if f['type'] == 'password'}
        out.append({'id': e['id'], 'name': e['name'], 'version': e['version'],
                    'description': e['description'], 'produces': e['produces'],
                    'installed': e['installed'], 'enabled': e['enabled'], 'active': e['active'],
                    'has_setup': bool(e['setup']), 'error': e['error'],
                    'settings': e['settings'], 'can_test': e['can_test'],
                    'values': {k: ('' if k in secrets else v) for k, v in vals.items()},
                    'secrets_set': sorted(k for k in secrets if vals.get(k))})
    return out


def ext_enabled(ext_id):
    """Enabled unless the user has turned it off. Absent means on: an extension you installed is
    one you meant to use, and the switch exists to turn things OFF."""
    return _bool_or(CONFIG.get('extensions', {}).get(ext_id), True)


def ext_active(ext_id):
    """Installed, loadable AND switched on — the one question every gate on an extension's
    controls asks."""
    ext = get_extension(ext_id)
    return bool(ext and ext['active'])


# Raw output range of the active metric, used to rescale reward -> a friendly 1..10 for
# display + the Quality filter (1..10 maps linearly onto [lo,hi]). CLIP-IQA is 0..1; if you
# switch the metric in the scorer's worker, update this to that metric's range.
METRIC_RANGE = (0.0, 1.0)


def scorer_available():
    return ext_active('quality')


def scorer_cmd():
    """[interpreter, worker] for the Quality scorer, or None when it isn't available."""
    ext = get_extension('quality')
    if not ext or not ext['active']:
        return None
    return [ext_python(ext), ext['worker']]

DEFAULT_CONFIG = {'roots': [], 'active': None, 'port': 8770}


# Prompt-miner settings (persisted under `miner` in config.json). The miner mines the
# vocabulary already present in a root — positive prompts, folders, filenames — into a
# frequency-ranked list of candidate tags. `stoplist` is the editable boilerplate list;
# it's merged with prompt_miner.STOPWORDS at mine time.
DEFAULT_MINER = {
    'min_count': 5,          # a candidate must appear in >= this many images to be shown
    'max_candidates': 500,   # cap on how many candidates are returned
    'min_word_len': 3,       # ignore word/filename tokens shorter than this
    'max_phrase_words': 5,   # comma-phrases longer than this go to word/bigram mining
    'sources': {'prompt': True, 'folder': True, 'filename': True},
    'stoplist': list(prompt_miner.DEFAULT_STOPLIST),
}


def _coerce_miner(base, patch):
    """Merge a (partial) miner-settings patch onto `base` with type coercion + clamps."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in base.items()}
    if not isinstance(patch, dict):
        return out
    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    if 'min_count' in patch:
        out['min_count'] = num(patch['min_count'], 1, 100000, out['min_count'])
    if 'max_candidates' in patch:
        out['max_candidates'] = num(patch['max_candidates'], 10, 100000, out['max_candidates'])
    if 'min_word_len' in patch:
        out['min_word_len'] = num(patch['min_word_len'], 1, 20, out['min_word_len'])
    if 'max_phrase_words' in patch:
        out['max_phrase_words'] = num(patch['max_phrase_words'], 1, 20, out['max_phrase_words'])
    if isinstance(patch.get('sources'), dict):
        s = patch['sources']
        out['sources'] = {'prompt': bool(s.get('prompt', out['sources']['prompt'])),
                          'folder': bool(s.get('folder', out['sources']['folder'])),
                          'filename': bool(s.get('filename', out['sources']['filename']))}
    if 'stoplist' in patch:
        raw = patch['stoplist']
        if isinstance(raw, str):   # textarea: split on newlines/commas
            raw = re.split(r'[,\n]', raw)
        if isinstance(raw, list):
            out['stoplist'] = [t.strip().lower() for t in raw if str(t).strip()]
    return out


# ---- in-memory scan progress ------------------------------------------------
_scan_state = {'running': False, 'seen': 0, 'total': 0, 'added': 0, 'updated': 0,
               'skipped': 0, 'done': False, 'stopped': False, 'cancel': False,
               'key': None, 'first_index': False,
               'started_at': 0.0, 'ended_at': 0.0, 'stats': None, 'error': None}
_scan_lock = threading.Lock()

# ---- in-memory pyiqa reward-scoring progress (same shape; runs in the venv) ----
_reward_state = {'running': False, 'seen': 0, 'total': 0, 'ok': 0, 'failed': 0,
                 'done': False, 'stopped': False, 'cancel': False,
                 'started_at': 0.0, 'ended_at': 0.0, 'error': None, 'last_error': None,
                 'device': None}
_reward_lock = threading.Lock()

# ---- in-memory Civitai-export prep progress (hashing big model files) ----
# Byte-based progress so the shared status bar / Stop button can show a real bar and cancel.
_export_state = {'running': False, 'done': False, 'stopped': False, 'cancel': False,
                 'phase': '', 'done_bytes': 0, 'total_bytes': 0, 'error': None,
                 'started_at': 0.0, 'ended_at': 0.0, 'id': None}
_export_lock = threading.Lock()

# ---- most-recent prompt-miner run (per active root) --------------------------
# We stash the run's postings (candidate -> image ids) here so a following /api/miner/apply can
# bulk-tag exactly the mined images without the client round-tripping potentially large id lists.
# Only the latest run is kept.
_miner_state = {'root': None, 'postings': {}, 'scanned': 0}

# ---- in-memory "Apply to folder" set-cull progress (same shape as the scan) ----
# `seen`/`total` count SETS, which is what the confirm counted and what a Stop leaves half-done;
# moved/purged/failed count files, as everywhere else that recycles.
_setcull_state = {'running': False, 'seen': 0, 'total': 0, 'moved': 0, 'purged': 0, 'failed': 0,
                  'done': False, 'stopped': False, 'cancel': False,
                  'started_at': 0.0, 'ended_at': 0.0, 'error': None, 'folder': ''}
_setcull_lock = threading.Lock()

# ---- in-memory prompt-miner progress ----------------------------------------
# Same shape as _scan_state so the UI's status bar / Stop button can be reused. 'candidates' holds
# the finished result for the client to pick up on the poll that sees running=False.
_miner_job = {'running': False, 'seen': 0, 'total': 0, 'done': False, 'stopped': False,
              'cancel': False, 'started_at': 0.0, 'ended_at': 0.0, 'error': None,
              'candidates': None, 'found': 0}
_miner_lock = threading.Lock()


def _root_key(path):
    """Stable short id for a root, derived from its absolute path (also the DB filename)."""
    return hashlib.sha1(os.path.abspath(path).lower().encode('utf-8')).hexdigest()[:16]


def _db_path_for(key):
    return os.path.join(ROOTS_DIR, key + '.db')


# How long to wait for a folder to answer before calling it unreachable. Long enough that a share
# which is merely slow to wake still gets in, short enough that nobody watches a spinner wondering
# whether the app has hung.
DIR_PROBE_SECS = 6.0


def _heal_stale_row(conn, row):
    """Self-heal: a row read by an older build is re-read the moment you open that file.

    Hung on the fetch of one image's full record rather than on the open bump beside it, because
    this is the request whose ANSWER would otherwise be stale — so the fresh values ride back in the
    same response, with no second round trip and nothing repainting under the reader.

    NOT on the grid, deliberately. Cards are drawn from rows in bulk, so healing there would re-read
    files by the hundred as you scroll, and scroll speed is one of the things this app is judged by.
    The grid shows what the index holds until a rescan or an open fixes it.

    Costs one read of one file, once per file per reader version. A PNG is a chunk read; a video is
    a bounded head read. Anything that goes wrong leaves the row and its stamp alone — see
    index_db.reread_one.
    """
    try:
        if (row['reader_ver'] or 0) >= index_db.READER_VERSION:
            return row
    except (IndexError, KeyError):
        return row                      # a library older than the column; ensure_library adds it
    # Never queue behind a scan to open one picture: the writer would wait on the index for as long
    # as the scan holds it, and the detail view would sit there.
    if _job_running():
        return row
    root = _root_by_key(row['root_id'])
    if not root or not index_db.reread_one(conn, row, root['path'], THUMBS_DIR):
        return row
    conn.commit()
    # The other writer of song_name/song_genre, so it drops the generic-names set the same way a
    # scan does. Once per file per reader version, so this is not a per-open cost.
    clear_song_name_cache()
    return conn.execute("SELECT * FROM images WHERE id=?", (row['id'],)).fetchone() or row


def probe_dir(path, timeout=DIR_PROBE_SECS):
    """Is this folder there? Answered with a DEADLINE. Returns 'ok', 'missing' or 'unreachable'.

    os.path.isdir on a dead UNC path blocks for tens of seconds inside Windows' redirector and
    cannot be interrupted, so the only way to put a bound on it is to let a thread wear the wait and
    stop waiting for the thread. The daemon flag is the point: an orphaned probe must never hold up
    a quit.

    A TIMEOUT IS NOT "NOT FOUND", and that distinction is the whole reason this exists. `\\\\pc\\pix`
    with the machine asleep and `\\\\pc\\pixx` with a typo both ended as "Folder not found", which
    sent people looking for a spelling mistake that was not there.
    """
    out = {}

    def look():
        try:
            out['isdir'] = os.path.isdir(path)
        except OSError:
            out['isdir'] = False

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    if 'isdir' not in out:
        return 'unreachable'
    return 'ok' if out['isdir'] else 'missing'


def unreachable_msg(path):
    """Why a folder didn't answer, in the terms the user can act on."""
    p = (path or '').replace('/', '\\')
    if p.startswith('\\\\'):
        host = p.lstrip('\\').split('\\')[0] or 'that computer'
        return ('Nothing answered at \\\\%s. That computer may be off or asleep, or Windows may not '
                'be signed in to it yet — open the folder in File Explorer once, then try again.'
                % host)
    drive = p[:2].upper() if len(p) > 1 and p[1] == ':' else ''
    if drive:
        return ('Drive %s did not answer. If it is a network drive, it may have been disconnected '
                'since you signed in.' % drive)
    return 'That folder did not answer in time.'


def missing_msg(path):
    """Not found — but say WHICH part is not there when we can tell."""
    p = (path or '').replace('/', '\\')
    if len(p) > 1 and p[1] == ':' and not os.path.exists(p[:3]):
        return 'There is no %s drive on this computer.' % p[:2].upper()
    return 'Folder not found: %s' % path


def _default_name(path):
    return os.path.basename(os.path.normpath(path)) or path


# Theme editor: the CSS tokens it may override, guarded to 6-digit hex so nothing
# arbitrary can be injected into the stylesheet.
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
THEME_TOKENS = ('--accent', '--active', '--modified', '--danger', '--success', '--star',
                '--bg', '--bg2', '--bg3', '--sidebar-bg', '--fg', '--muted', '--border',
                '--reward-bg', '--tag-fav',
                '--label-publish', '--label-published', '--label-refine', '--label-explore',
                '--label-video')

# Curation labels: an exclusive, one-key disposition mark per image (To publish / Published /
# To refine / To explore / For video) for the publish-to-Civitai workflow. Stored as a managed
# tag `label:<slug>` — like `style:` tags — so filtering, counts, and the per-root DB come for
# free; exclusivity (max one per image) is enforced on write. Single source of truth: emitted
# to the client via /api/config so the two never drift. `key` = one-tap hotkey; `token` = the
# theme color (editable in Appearance). To add labels later, extend this list (f key free).
LABELS = [
    {'slug': 'publish',   'name': 'To publish', 'key': 'a', 'token': '--label-publish'},
    {'slug': 'published', 'name': 'Published',  'key': 'b', 'token': '--label-published'},
    {'slug': 'refine',    'name': 'To refine',  'key': 'c', 'token': '--label-refine'},
    {'slug': 'explore',   'name': 'To explore', 'key': 'd', 'token': '--label-explore'},
    {'slug': 'video',     'name': 'For video',  'key': 'e', 'token': '--label-video'},
]
_LABEL_SLUGS = {l['slug'] for l in LABELS}

# THE HIDDEN MARK WAS HERE, and went on 2026-09-08. "Not this one, for now": a per-image flag that
# took the image out of every view until cleared, stored as a tag row (source='hide') and applied to
# EVERY query by default -- the one exclusion that was not opt-in. The author built it once, marked five
# images, and never used it again; the five marks were deleted with it (see index_db's
# hide_marks_purged). Labels already carry "come back to this", and unlike Hide they say so on the
# card instead of removing it.
#
# What went with it: the `peek` query flag, the `show_hidden` setting, the ghosted card, the
# hidden_marked counter, and the per-file exclusion that was half of the 2026-09-06 recycle bug --
# a card's members were built from rows that survived this predicate, so a hidden member was left
# on disk when its card was recycled.

# ---- deferred recycle (Undo) -------------------------------------------------------------------
# A recycle is QUEUED, not performed: the files and the index rows are untouched for UNDO_WINDOW_S,
# and Undo simply cancels the job. That is the whole reason this is affordable — undo is "don't do
# it" rather than "put the file, its row, its tags, its label, its note and its score back".
#
# THE QUEUE IS IN MEMORY, AND THAT IS THE SAFE DIRECTION. Nothing has happened yet, so a crash
# inside the window costs a delete you asked for and nothing else — the files are on disk and the
# curation is intact. The rejected alternative was to purge the rows now and restore them from a
# snapshot on undo: that inverts the failure, because a crash mid-window would lose those images'
# tags/label/note/score while the files sat there, and the next rescan would re-index them as fresh.
# 5 seconds, the author's call 2026-08-15, down from 15. The window is not "how long to think about it" —
# it is how long the offer sits on screen while you are trying to look at pictures, and 15 was long
# enough that the pill became furniture. Undo is also bound to Ctrl+Z, so a longer window buys
# nothing a keyboard shortcut does not already cover. Shortening it does mean files reach the bin
# sooner, which is the intended trade and the reason this constant is here rather than inlined:
# the client only ever receives it (`window` in the queue response), never decides it.
UNDO_WINDOW_S = 5
_pending = {}                 # batch id -> {'ids': [...], 'timer': Timer, 'result': (m,p,f) | None}
_pending_ids = set()          # flat union of every queued id — what the queries must not return
_pending_lock = threading.Lock()
_pending_seq = 0


def _pending_sql(alias='i'):
    """Exclude queued-for-recycle rows. '' when nothing is pending, which is virtually always.

    Deliberately NOT a permanent predicate like NOT_HIDDEN_SQL: that one is always present and its
    cost was worth measuring, whereas this one vanishes from the SQL entirely when the queue is
    empty. So the common case pays literally nothing, and there is no NOT IN vs NOT EXISTS question
    to re-open. Ids are ints from the DB, so interpolating them is safe and keeps this a plain
    string like its neighbour.

    There is no peek. A pending row is invisible, full stop — it is on its way out."""
    if not _pending_ids:
        return ''
    return '%s.id NOT IN (%s)' % (alias, ','.join(str(int(i)) for i in _pending_ids))

# RETIRED 2026-08-19: "Unreviewed" — images carrying no curation signal at all. It was a sidebar
# row with a live count and a filter, and it cost more than anything else in the sidebar: the count
# scanned every image on every refresh, and on the author's library it ran 354–1417ms depending on how
# cold the disk was. The author's call, in his words: *"one clear thing to cut: unreviewed! that is
# worthless at this point."* A library that is mostly unreviewed makes the number a synonym for its
# size, which is the same reason the per-card unreviewed dot was dropped on 2026-08-05.
# Nothing was stored for it — it was derived on read — so there is no data to migrate or keep.

# Which member of a collapsed image SET fronts the card. The LAST stage of the pipeline — the most
# finished image — because that is the one you are judging the set by; showing the first stage means
# every set card advertises its roughest member.
# 0 sorts first = best face. Prefers comfy_vv_saver's stamped `set_stage`, and falls back to the
# legacy filename roles for sets that predate the stamp. Shares its vocabulary with
# index_db._stage_rank but is deliberately INVERTED: that one orders the detail view's panes in
# pipeline order (raw first), this one wants the other end of the same pipeline.
#
# A stage the vocabulary lacks — the saver's `Custom...` field — ranks 5, the worst face, and that
# is the SAME decision as _stage_rank putting it first: the first stage of a pipeline is its
# roughest image, and the roughest image must never be what a set card advertises. The two
# functions look like they disagree only because one is inverted.
#
# The filename fallback fires only when `set_stage` is BLANK. It used to catch a non-blank custom
# stage too, so a stage called "Detail2" was scored 3 by the bare word "det" inside it — a name the
# viewer does not know deciding a rank as if it did. A file that stamped its stage is answered by
# the stamp, right or wrong; guessing is for files that never said.
# The two legacy names are matched here as well as in the filename branch, so a set_stage column
# holding an old 'main'/'det' is ranked by the alias rather than swept into the custom bucket.
# Bare column names (no `i.` alias): this is evaluated inside the grouping CTE, not against images.
SET_FACE_RANK_SQL = (
    "CASE "
    "WHEN LOWER(COALESCE(set_stage,'')) = 'final' THEN 0 "
    "WHEN LOWER(COALESCE(set_stage,'')) = 'upscale' THEN 1 "
    "WHEN LOWER(COALESCE(set_stage,'')) = 'refine' THEN 2 "
    "WHEN LOWER(COALESCE(set_stage,'')) IN ('detail','det') THEN 3 "
    "WHEN LOWER(COALESCE(set_stage,'')) IN ('raw','main') THEN 4 "
    "WHEN COALESCE(set_stage,'') <> '' THEN 5 "
    "WHEN LOWER(filename) LIKE '%refine%' THEN 2 "
    "WHEN LOWER(filename) LIKE '%det%' THEN 3 "
    "WHEN LOWER(filename) LIKE '%main%' THEN 4 "
    "ELSE 5 END"
)

# The checkpoint's top-level folder (the folder right under models/checkpoints|diffusion_models|
# unet) = the user's own model-family organization (Illustrious/, Pony/, SDXL/, Flux/…). Derived
# from the model path baked into each image; '' when the model sits directly in the category dir.
# char(92) = backslash (ComfyUI stores Windows paths); also handle '/'.
# Model type is now DERIVED AT SCAN and stored in images.model_type (index_db.recompute_model_types),
# not computed here per query. Two reasons, and the second is the one that mattered: the CASE/instr
# ran over every row on every facet and filter, and a stored value lets the rule be smarter than SQL
# can be — it can match a family named inside a loose checkpoint's FILENAME, which is what rescued
# 14,185 of the author's images from "(none)". Rebuild metadata is what fills the column.
MODEL_TYPE_SQL = "COALESCE(i.model_type, '')"

# The four dimensions the sidebar offers a LIST for, and which therefore each have to be excluded
# from their own facet count. Named here because two places consume them: _filters puts them in the
# WHERE, and /api/facets carries them as per-row flags instead.
FACET_DIMS = ('root', 'model', 'folder', 'mfolder')


def _dim_clause(q, dim):
    """The WHERE clause for ONE faceted dimension: `(sql, params)`, or `(None, [])` when the user
    has not narrowed that dimension.

    Written once and read twice, on purpose. `_filters` appends these to its WHERE in the normal
    way; `/api/facets` needs the same four predicates as per-row *flags* so it can answer every
    facet from a single pass over the library. A second copy of any of them is a second chance for
    a facet count to disagree with the grid it sits beside."""
    if dim == 'root':
        # A comma list of root keys to include; empty = every root. The whole "hide/show individual
        # libraries" mechanism. NULL root_id (pre-merge rows) fails the IN and is excluded, which is
        # the same answer this gave as a plain WHERE clause.
        roots = [x for x in q.get('roots', [''])[0].split(',') if x]
        if not roots:
            return None, []
        return 'i.root_id IN (%s)' % ','.join('?' * len(roots)), roots
    if dim == 'model':
        model = q.get('model', [''])[0]
        if not model:
            return None, []
        return 'i.model_name IS ?', [None if model == '(none)' else model]
    if dim == 'folder':
        folder = q.get('folder', [''])[0]
        if not folder:
            return None, []
        return '(i.folder = ? OR i.folder LIKE ?)', [folder, folder + '/%']
    if dim == 'mfolder':
        # Model type = the model's top-level folder (family); '(none)' = a model with no subfolder,
        # which is not the same as having no model at all.
        mfolder = q.get('mfolder', [''])[0]
        if not mfolder:
            return None, []
        if mfolder == '(none)':
            return f"(i.model IS NOT NULL AND i.model <> '' AND ({MODEL_TYPE_SQL}) = '')", []
        return f"(i.model IS NOT NULL AND LOWER({MODEL_TYPE_SQL}) = LOWER(?))", [mfolder]
    raise ValueError('unknown facet dimension: %r' % (dim,))


def _coerce_theme(theme):
    """Keep only known tokens with valid #rrggbb values."""
    if not isinstance(theme, dict):
        return {}
    return {k: v.strip().lower() for k, v in theme.items()
            if k in THEME_TOKENS and isinstance(v, str) and _HEX_RE.match(v.strip())}


# ---- card facts --------------------------------------------------------------------------------
# What the grid card's small print shows, in what order. ONE ORDERED LIST -- position is the list's
# own, not a number anyone has to keep consistent -- and each entry says where that fact appears:
#   'always'  on the card, all the time (Large and Extra-large cards only)
#   'hover'   on the card, with the rest of its hover chrome
#   'off'     nowhere
#
# The KEYS are the client's CARD_FACTS registry, and the server deliberately does not know what any
# of them mean. It validates membership and nothing else, because the meaning is a formatting
# question and formatting lives in one place.
#
# UNKNOWN KEYS ARE DROPPED, MISSING ONES ARE APPENDED at their default. That pair is what makes a
# new fact appear for someone whose config predates it instead of being invisible until they open
# Settings -- the same reason a retired filter vanishes from an old snapshot.
CARD_FACT_KEYS = ('dims', 'duration', 'age', 'filesize', 'model', 'folder')
CARD_FACT_PLACES = ('always', 'hover', 'off')
# Age FIRST: The author's stated preference, 2026-08-24 -- "I prefer the time to come before the image
# size". It is the default rather than something he has to set, because a default nobody would
# choose is a chore handed to the user.
DEFAULT_CARD_FACTS = [
    {'key': 'age', 'place': 'always'},
    {'key': 'dims', 'place': 'always'},
    {'key': 'duration', 'place': 'always'},
    {'key': 'filesize', 'place': 'hover'},
    {'key': 'model', 'place': 'hover'},
    {'key': 'folder', 'place': 'hover'},
]


# A default that has been WRITTEN DOWN stops being a default. `cards` was materialised into
# config.json the moment the block existed, so changing the shipped order afterwards reached nobody
# who had ever saved anything -- The author hit exactly that within a day of it shipping.
#
# So the block is stored only when it DIFFERS from the defaults. An untouched install keeps no
# `cards` key at all and simply follows whatever this file says, which is what a default is for;
# a real preference is a difference, and differences are what get written down.
_SUPERSEDED_CARD_ORDERS = (
    # The order shipped in the commit before the author asked for age first. It lived for one commit, and
    # a config carrying it verbatim is a default nobody chose rather than a preference anyone set.
    ('dims', 'duration', 'age', 'filesize', 'model', 'folder'),
)


def _cards_or_none(value):
    """The coerced list, or None when it says nothing the defaults don't already say."""
    rows = _coerce_card_facts(value)
    if rows == DEFAULT_CARD_FACTS:
        return None
    if tuple(r['key'] for r in rows) in _SUPERSEDED_CARD_ORDERS and all(
            r['place'] == d['place'] for r, d in zip(sorted(rows, key=lambda x: x['key']),
                                                     sorted(DEFAULT_CARD_FACTS, key=lambda x: x['key']))):
        return None                                   # a superseded default, not a choice
    return rows


def _effective_cards():
    """What the client should draw from: the saved order, or the shipped one."""
    return CONFIG.get('cards') or [dict(r) for r in DEFAULT_CARD_FACTS]


def _coerce_card_facts(value):
    """An ordered list of {key, place}, every known key present exactly once."""
    out, seen = [], set()
    for row in (value if isinstance(value, list) else []):
        if not isinstance(row, dict):
            continue
        key = row.get('key')
        if key not in CARD_FACT_KEYS or key in seen:
            continue                                  # unknown or duplicate: drop it
        place = row.get('place')
        out.append({'key': key, 'place': place if place in CARD_FACT_PLACES else 'hover'})
        seen.add(key)
    for row in DEFAULT_CARD_FACTS:                    # anything new since this config was written
        if row['key'] not in seen:
            out.append(dict(row))
    return out


# ---- snapshots ---------------------------------------------------------------------------------
# A snapshot is a named capture of the sidebar's filter state — the term is Native Instruments'
# (Reaktor/Kontakt), where a snapshot is the saved state of every control on a panel. `filters`
# mirrors the client's serializeFilters() output key-for-key on purpose: no translation layer, so
# adding a filter means adding it to the whitelist here and nowhere else.
MAX_SNAPSHOTS = 50
MAX_SNAPSHOT_NAME = 60
_SNAP_STR_KEYS = ('model', 'folder', 'modelFolder', 'meta', 'type', 'sort', 'order', 'dfrom', 'dto')
# 'unreviewed' was here until 2026-08-19 and is deliberately NOT kept for compatibility: dropping it
# from the whitelist is what makes an old snapshot shed the retired filter on load instead of
# carrying a dead key forever.
#
# 'roots' left on 2026-08-20 by the same mechanism but for a different reason, and the distinction
# matters to anyone reading this list: 'unreviewed' was a filter that was RETIRED — the feature is
# gone. 'roots' was a filter that was RECLASSIFIED — the library show/hide selection is very much
# alive, it simply stopped being part of the filter set, because it is scope rather than a search.
# So a snapshot no longer carries a library selection, and restoring one leaves yours alone. The
# key is shed at config LOAD, so this takes effect on the first start of the new server, without a
# migration step.
_SNAP_BOOL_KEYS = ('group', 'sets', 'favOnly', 'hasNote')
_SNAP_LIST_KEYS = ('terms', 'xterms', 'tags')


def _coerce_snapshot_filters(f):
    """Whitelist one snapshot's filter payload; unknown keys are dropped here and never reach disk.
    `sort` is deliberately NOT validated against a list of options — the server has no business
    knowing the client's <select>; applyFilters falls back when an option no longer exists."""
    if not isinstance(f, dict):
        f = {}
    out = {}
    for k in _SNAP_STR_KEYS:
        v = f.get(k)
        out[k] = str(v)[:200] if isinstance(v, (str, int, float)) and v is not None else ''
    out['order'] = 'asc' if out['order'] == 'asc' else 'desc'
    for k in _SNAP_BOOL_KEYS:
        # group/sets default TRUE to match applyFilters — defaulting them false would silently
        # un-merge every set on a snapshot saved before those keys existed.
        default = k in ('group', 'sets')
        out[k] = bool(f.get(k, default))
    for k in _SNAP_LIST_KEYS:
        v = f.get(k)
        out[k] = [str(x)[:200] for x in v[:100] if isinstance(x, (str, int, float))] if isinstance(v, list) else []
    for k in ('rmin', 'rmax'):
        try:
            n = float(f.get(k))
            out[k] = n if n == n and abs(n) != float('inf') else ''
        except (TypeError, ValueError):
            out[k] = ''
    return out


def _coerce_snapshots(raw):
    """Saved snapshots list. Names are the identity, so they're de-duped case-insensitively; an
    over-long name is truncated rather than rejected, because the client adopts the echoed list
    and a truncation is then visible in the sidebar instead of failing silently."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for v in raw:
        if not isinstance(v, dict):
            continue
        name = str(v.get('name') or '').strip()[:MAX_SNAPSHOT_NAME]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({'name': name, 'filters': _coerce_snapshot_filters(v.get('filters'))})
        if len(out) >= MAX_SNAPSHOTS:
            break
    return out


# One extension's stored settings: {<ext_id>: {<field key>: scalar}}.
MAX_EXT_SETTING = 4000          # a prompt is the long one; anything past this is a paste accident


def _coerce_ext_settings(raw):
    """Whitelist the stored settings of every extension. Shape only — NOT the manifest's schema.

    Deliberately schema-blind, because the two are not available at the same time: config.json is
    read at import, an extension folder can be added, removed or edited while the app runs, and a
    coercer that dropped keys it couldn't find a manifest for would quietly wipe a configured
    extension every time its folder was renamed. So this guarantees only that what comes back is a
    dict of dicts of scalars; the SCHEMA is enforced where it is actually known, on the way in
    (api_ext_settings)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for ext_id, vals in raw.items():
        if not isinstance(ext_id, str) or not isinstance(vals, dict):
            continue
        kept = {}
        for k, v in vals.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, bool) or isinstance(v, (int, float)) and not isinstance(v, bool):
                kept[k] = v
            elif isinstance(v, str):
                kept[k] = v[:MAX_EXT_SETTING]
        if kept:
            out[ext_id] = kept
    return out


def _normalize_config(cfg):
    """Coerce config into {roots:[{path,name,key}], active:key, ...}, migrating the
    legacy single-root shape ({root: '...'}) and filling in keys/names/defaults."""
    roots = cfg.get('roots')
    if not isinstance(roots, list):
        roots = []
    if not roots and cfg.get('root'):          # migrate legacy single root
        roots = [{'path': cfg['root']}]
    norm, seen = [], set()
    for r in roots:
        if isinstance(r, str):
            r = {'path': r}
        path = (r.get('path') or '').strip()
        if not path:
            continue
        key = r.get('key') or _root_key(path)
        if key in seen:
            continue
        seen.add(key)
        norm.append({'path': path, 'key': key,
                     'name': (r.get('name') or '').strip() or _default_name(path),
                     'models_dir': (r.get('models_dir') or '').strip()
                                   if isinstance(r.get('models_dir'), str) else ''})
    cfg['roots'] = norm
    keys = {r['key'] for r in norm}
    cfg['active'] = cfg.get('active') if cfg.get('active') in keys else (norm[0]['key'] if norm else None)
    # Whether the one-time "open the Help" pop-up has been shown. It lives HERE rather than in the
    # browser: localStorage is keyed to the address the app is served at, so every copy of the folder
    # shares one flag and a fresh copy is silently pre-marked as seen. In config.json it belongs to
    # the install, which is what "once" was always meant to mean.
    # ABSENT WITH LIBRARIES ALREADY ADDED MEANS SEEN. Anyone updating has no flag and every reason
    # not to be told where to start, and this is the only moment that can tell a fresh install from
    # an established one. A brand-new install has no roots here, so it stays False.
    if cfg.get('seen_help_hint') is None:
        cfg['seen_help_hint'] = bool(norm)
    else:
        cfg['seen_help_hint'] = bool(cfg['seen_help_hint'])
    cfg.pop('root', None)                       # legacy field is gone now
    # The AI features left on 2026-08-23, so their settings are dropped rather than migrated: both
    # the `analysis` block and the older top-level `vlm_url` it superseded. save_config() writes an
    # explicit whitelist, so anything not popped here would linger in config.json unread anyway —
    # popping makes the removal visible in the file rather than leaving a dead block behind.
    cfg.pop('analysis', None)
    cfg.pop('vlm_url', None)
    # Extensions: ONLY the off switches are stored, keyed by folder name. An extension the user
    # has never touched has no entry and is on — so uninstalling one leaves no orphan setting,
    # and installing one doesn't need the config to have heard of it first.
    exts = cfg.get('extensions') if isinstance(cfg.get('extensions'), dict) else {}
    cfg['extensions'] = {str(k): bool(v) for k, v in exts.items() if v is False}
    gen = cfg.get('general') if isinstance(cfg.get('general'), dict) else {}
    gen.pop('hide_large', None)       # both removed 2026-09-08; drop rather than leave unread
    gen.pop('show_hidden', None)
    cfg['general'] = {'autoplay': bool(gen.get('autoplay', DEFAULT_GENERAL['autoplay'])),
                      'keep_behavior': ('stay' if gen.get('keep_behavior') == 'stay' else 'next'),
                      # Absent means ON: a config.json predating this setting must keep its
                      # prompts. Defaulting a safeguard to off on upgrade is the one direction
                      # this may not fail in — hence _bool_or, which counts null as absent too.
                      'confirm_recycle': _bool_or(gen.get('confirm_recycle'),
                                                  DEFAULT_GENERAL['confirm_recycle']),
                      # _bool_or, not bool(...): absent and null both mean "no answer" here, and
                      # the answer has to be ON. bool(None) is False, which would hide the section
                      # on every install that predates the setting.
                      'show_snapshots': _bool_or(gen.get('show_snapshots'),
                                                 DEFAULT_GENERAL['show_snapshots']),
                      'recycle_warn': _bool_or(gen.get('recycle_warn'),
                                               DEFAULT_GENERAL['recycle_warn']),
                      # _bool_or for the usual reason: absent and null both mean "never answered",
                      # and the answer for a config predating this setting is the default. Note
                      # the direction is the opposite of the recycle safeguards' -- defaulting THIS
                      # one on is a request going out, so the first-run ask is what actually
                      # protects it, not this line.
                      'update_check': _bool_or(gen.get('update_check'),
                                               DEFAULT_GENERAL['update_check']),
                      'recycle_warn_files': _clamped(gen.get('recycle_warn_files'),
                                                     RECYCLE_FILES_MIN, RECYCLE_FILES_MAX,
                                                     DEFAULT_GENERAL['recycle_warn_files']),
                      'recycle_warn_gb': _clamped(gen.get('recycle_warn_gb'),
                                                  RECYCLE_GB_MIN, RECYCLE_GB_MAX,
                                                  DEFAULT_GENERAL['recycle_warn_gb'], decimals=1),
                      'models_dir': (gen.get('models_dir') or '').strip()
                                    if isinstance(gen.get('models_dir'), str) else ''}
    miner = cfg.get('miner') if isinstance(cfg.get('miner'), dict) else {}
    cfg['miner'] = _coerce_miner(DEFAULT_MINER, miner)
    cfg['theme'] = _coerce_theme(cfg.get('theme'))
    # None when it matches the shipped order: absent means "follow the default", which is what lets
    # the default ever change again.
    cards = _cards_or_none(cfg.get('cards')) if cfg.get('cards') is not None else None
    if cards is None:
        cfg.pop('cards', None)
    else:
        cfg['cards'] = cards
    # Migrate the pre-rename key. Saved sets are real user work, so this has to run before the
    # coercer or they vanish silently on the next save (save_config writes a whitelist).
    if 'views' in cfg and 'snapshots' not in cfg:
        cfg['snapshots'] = cfg['views']
    cfg.pop('views', None)
    cfg['snapshots'] = _coerce_snapshots(cfg.get('snapshots'))
    cfg['ext_settings'] = _coerce_ext_settings(cfg.get('ext_settings'))
    return cfg


def _chosen_port():
    """The port to serve on: PORT in the environment, then port.txt, then config.json, then 8770.

    NOTHING HERE RAISES. A port is read at startup and a bad one must cost the default rather than
    the app -- the whole reason this moved out of config.json is that a typo should be cheap. So an
    unreadable file, a blank one, a word, or a number outside 1-65535 all fall through to the next
    source, silently: the user's next move on any of them is the same, open the file and fix it,
    and the app has to be running for them to be told anything at all.

    config.json is still read, below port.txt, because installs predating this file have their port
    in there and would otherwise move under them. It is no longer WRITTEN -- see save_config."""
    for src in (os.environ.get('PORT'), _read_port_file(), CONFIG.get('port')):
        try:
            n = int(str(src).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 65535:
            return n
    return 8770


def _read_port_file():
    try:
        with open(PORT_PATH, 'r', encoding='utf-8') as f:
            return f.read(32)
    except (IOError, OSError, UnicodeDecodeError):
        return None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return _normalize_config(cfg)


def save_config():
    tmp = {'roots': CONFIG['roots'], 'active': CONFIG['active'],
           'general': CONFIG.get('general', dict(DEFAULT_GENERAL)),
           'miner': CONFIG.get('miner', dict(DEFAULT_MINER)),
           'theme': CONFIG.get('theme', {}),
           'snapshots': CONFIG.get('snapshots', []),
           'extensions': CONFIG.get('extensions', {}),
           # What each extension has been configured with — subject to the note below like
           # everything else in this dict: leave the line out and a saved endpoint survives exactly
           # until the next save of anything else.
           'ext_settings': CONFIG.get('ext_settings', {}),
           # Where the app window was last left, for start.bat to restore. THIS LINE IS THE WHOLE
           # FEATURE: save_config builds an explicit dict, so a block missing from here is written
           # once and silently dropped by the next save of anything else. That is the exact fault
           # test_snapshots.py was written to catch when `snapshots` was added, which is why the
           # test for this asserts against config.json ON DISK rather than the response.
           'window': CONFIG.get('window'),
           # The one-time Help pop-up's seen flag, subject to the note above like everything else
           # here: leave this line out and it is written once, dropped by the next save of anything,
           # and the pop-up comes back forever.
           'seen_help_hint': bool(CONFIG.get('seen_help_hint'))}
    # Written ONLY when it differs from the shipped order — see _cards_or_none. Still explicit, and
    # still subject to the note above: a block that needs saving and is missing from this dict is
    # written once and dropped by the next save of anything else.
    if CONFIG.get('cards'):
        tmp['cards'] = CONFIG['cards']
    # KEPT ONLY IF IT IS ALREADY THERE. port.txt is where a port is set now, and a fresh install's
    # config.json should not grow a key nothing tells you to edit. But an install predating that
    # file has its port in here, and dropping the line would move their app to 8770 on the next
    # save of anything -- the same silent-drop trap the note above describes, arriving as "the
    # bookmark stopped working".
    if CONFIG.get('port') not in (None, 8770):
        tmp['port'] = CONFIG['port']
    # WRITTEN BESIDE, THEN SWAPPED IN. Opening CONFIG_PATH with 'w' truncates it first, and this
    # runs from a dozen places -- every Settings change, and the window position as the app closes,
    # which is the moment the process is most likely to be killed. A save interrupted there left
    # the file half-written, load_config silently fell back to defaults, and the NEXT save wrote
    # those defaults over it: every library, snapshot and setting gone, from one badly-timed exit.
    #
    # os.replace is atomic on Windows and POSIX alike, so a reader sees the whole old file or the
    # whole new one and never a partial. The temp file must be in the SAME directory -- a replace
    # across volumes is a copy, which is exactly the non-atomic thing being avoided. fsync before
    # the swap, or a crash can leave the new name pointing at a file whose contents are still in
    # the OS write cache: a valid rename to empty bytes, which is the same loss wearing a new hat.
    d = os.path.dirname(CONFIG_PATH) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(tmp, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        # Leave the REAL file untouched and take the scratch one with us. Not `except Exception`:
        # a KeyboardInterrupt or SystemExit mid-write is precisely the interruption this exists
        # for, and letting it skip the cleanup leaves .config-*.tmp litter next to start.bat.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


CONFIG = load_config()

# The current management-target root (rescan / Clear tags / Mine / delete act on it). `db` always
# points at the one merged LIBRARY_DB; when no root is configured, queries just return nothing.
ACTIVE = {'key': None, 'path': None, 'db': LIBRARY_DB}


def _root_by_key(key):
    return next((r for r in CONFIG['roots'] if r['key'] == key), None)


def _root_has_images(key):
    """Whether a root has any indexed rows in the merged DB."""
    if not key:
        return False
    try:
        conn = sqlite3.connect(LIBRARY_DB)
        r = conn.execute("SELECT 1 FROM images WHERE root_id IS ? LIMIT 1", (key,)).fetchone()
        conn.close()
        return r is not None
    except Exception:
        return False


def set_active(key):
    """Set the current *management-target* root (the one rescan / Clear tags / Mine / delete act on).
    Under the merged DB, db() always opens LIBRARY_DB — `key`/`path` here are just the target, not a
    DB switch."""
    r = _root_by_key(key) if key else None
    ACTIVE.update(key=(r['key'] if r else None), path=(r['path'] if r else None), db=LIBRARY_DB)
    CONFIG['active'] = ACTIVE['key']
    return r


# How long a write waits for another writer before giving up. Generous on purpose: the thing it
# waits for is a scan's final commit, and waiting is always better than refusing work the user
# asked for. It is a ceiling, not a delay -- an uncontended write never touches it.
DB_BUSY_TIMEOUT_MS = 30000


# ---- who is holding the write lock -------------------------------------------------------------
# INSTRUMENT, NOT A FIX. "database is locked" keeps arriving with nothing running and clears only on
# restart, which says a connection is being left open mid-transaction rather than two writers
# colliding -- WAL allows one writer, and an unclosed transaction blocks every other one for the
# life of the process. There are 16 write paths in this file that close only on the happy path, so
# the theory is cheap; proving WHICH one is not, and a sixteen-site edit on a guess is how the
# index_db blanket-replace went wrong. So: record where every connection was opened, and say so when
# a write hits a lock. The next occurrence names its own culprit.
#
# Costs a stack peek per connection and nothing else. Registry entries are removed on close(), and a
# test asserts it returns to empty, so the diagnostic cannot become the leak it is looking for.
_open_conns = {}
_open_conns_lock = threading.Lock()


class _TrackedConn(sqlite3.Connection):
    """A connection that remembers where it was opened until it is closed."""

    def close(self):
        with _open_conns_lock:
            _open_conns.pop(id(self), None)
        super().close()


def _note_open(conn):
    # Two frames up is the caller of db(); one is db() itself. Kept short on purpose -- a full stack
    # per connection is noise, and the opening function is the whole answer.
    where = '?'
    try:
        st = traceback.extract_stack(limit=4)[:-2]
        where = ' <- '.join('%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name)
                            for f in reversed(st))
    except Exception:
        pass
    with _open_conns_lock:
        _open_conns[id(conn)] = (time.time(), where)


def report_locked(what):
    """Print who is holding connections open. Goes to stderr, which start.bat captures into
    data/viewer.log -- so a user who hits this can send the block rather than describe it."""
    now = time.time()
    with _open_conns_lock:
        rows = sorted(_open_conns.values())
    lines = ['[db] %s hit "database is locked"' % what,
             '[db]   jobs running: scan=%s reward=%s setcull=%s miner=%s'
             % (_scan_state.get('running'), _reward_state.get('running'),
                _setcull_state.get('running'), _miner_job.get('running')),
             '[db]   connections still open: %d' % len(rows)]
    for opened, where in rows:
        lines.append('[db]     %6.1fs ago  %s' % (now - opened, where))
    if not rows:
        lines.append('[db]     (none -- the holder is outside this process, or already collected)')
    sys.stderr.write(chr(10).join(lines) + chr(10))
    sys.stderr.flush()


def db():
    conn = sqlite3.connect(ACTIVE['db'], factory=_TrackedConn)   # always LIBRARY_DB
    conn.row_factory = sqlite3.Row
    # The grid query materialises its filtered set (see api_search), and the default is to spill
    # that to a temp FILE — writing tens of megabytes to disk inside a read, which is the opposite
    # of the point. It is tens of megabytes at a 100k library, so RAM is the right place for it.
    conn.execute('PRAGMA temp_store=MEMORY')
    # WAIT FOR A WRITER INSTEAD OF FAILING AT ONE. The database is WAL, so readers never block --
    # but WAL still allows only ONE writer, and Python's default gives up after 5s. A scan commits
    # its whole batch at the end, and on a big library that outlasts 5 seconds easily, so any write
    # landing in that window died with "database is locked".
    #
    # The author hit it the obvious way: he added a library, then mined and applied tags while its first
    # scan was still finishing. The work was never in conflict -- one writer simply had to go
    # second -- and 5s was the only reason it did not.
    conn.execute('PRAGMA busy_timeout=%d' % DB_BUSY_TIMEOUT_MS)
    _note_open(conn)
    return conn


def ensure_library():
    """Create the merged DB (schema) if needed and run the one-time per-root merge. Idempotent:
    merge_roots() no-ops once meta.merged is set. Non-destructive — the per-root <key>.db files are
    only read."""
    os.makedirs(DATA_DIR, exist_ok=True)
    roots = [(r['key'], _db_path_for(r['key'])) for r in CONFIG['roots']]
    index_db.merge_roots(LIBRARY_DB, roots)


ensure_library()
set_active(CONFIG['active'])


_WORD = re.compile(r"\w+", re.UNICODE)


def to_fts(q, join=' '):
    """Turn free text into a safe FTS5 MATCH string of prefix terms.
    join=' ' -> implicit AND (search); join=' OR ' -> ANY (exclude set)."""
    terms = _WORD.findall(q or '')
    if not terms:
        return None
    return join.join(f'{t}*' for t in terms)


def run_scan(force=False, key=None):
    """Scan one library (by key, defaulting to the current target) on a worker thread.
    Returns True if a scan was started, False if one was already running or the key is unknown —
    the caller must not claim it started, or the UI blocks on a job that doesn't exist."""
    # dbp is always LIBRARY_DB; rid scopes it.
    r = _root_by_key(key) if key else _root_by_key(ACTIVE['key'])
    if not r:
        return False
    root, dbp, rid = r['path'], LIBRARY_DB, r['key']
    with _scan_lock:
        if _scan_state['running']:
            return False
        # `first_index` and `key` are for the CLIENT, and they are what make cancelling safe to
        # offer: a library with nothing in it yet has nothing curated to lose, so cancelling can
        # remove it outright instead of leaving a half-indexed one behind. Derived here rather than
        # passed in by the caller, so a browser that RELOADS mid-scan and reattaches gets the same
        # answer — that reattach is how the old trap would otherwise come straight back.
        _scan_state.update(running=True, seen=0, total=0, added=0, updated=0,
                           skipped=0, done=False, stopped=False, cancel=False,
                           key=rid, first_index=not _root_has_images(rid),
                           started_at=time.time(), ended_at=0.0, stats=None, error=None)

    def prog(done, total, a, u, s):
        _scan_state.update(seen=done, total=total, added=a, updated=u, skipped=s)

    def count_cb(total):
        _scan_state.update(total=total)

    def worker():
        try:
            os.makedirs(ROOTS_DIR, exist_ok=True)
            # LOOK ONLY WHERE SOMETHING CHANGED. The per-folder signature already tells us which
            # folders moved — that is what lights the ↻ — and it costs one stat per folder (~0.5s
            # on a 100k library over a share) against ~6s to walk every file. Until now the scan
            # threw that away and swept the lot, so adding one image cost the same as adding a
            # thousand: The author, on a refresh for a single new card, "why does this take 6 seconds?"
            #
            # A full walk still happens when it must: on a forced rescan (the answer to anything
            # the folder timestamps cannot see, such as a file edited in place), and whenever there
            # is no signature yet — a first scan, or a library indexed by an older build.
            #
            # ...and a quick scan is trusted only for a while. Folder timestamps can lie — SMB
            # caches them, the comparison tolerates 2s of share jitter, and none of it notices a
            # file edited in place — so a change can stay invisible until something else touches
            # that folder, which for a finished folder is never. A full walk every
            # FULL_SCAN_MAX_AGE puts a ceiling on how long anything can be wrong, whatever the
            # reason, including reasons not yet identified.
            only_dirs = gone = None
            _age = index_db.full_scan_age(dbp, rid)
            if not force and _age is not None and _age < index_db.FULL_SCAN_MAX_AGE:
                cd = index_db.changed_dirs(dbp, root, rid)
                if cd is not None:
                    only_dirs, gone = cd
                    # The root itself always goes in: a new folder is found by listing its parent,
                    # and '.' is the parent of the top level.
                    only_dirs = set(only_dirs) | {'.'}
            # Metadata-only scan (fast at 20k); thumbnails are generated lazily
            # by serve_thumb the first time each image is viewed.
            stats = index_db.scan(root, dbp, thumbs_dir=None,
                                  progress=prog, count_cb=count_cb, force=force,
                                  should_stop=lambda: _scan_state['cancel'], root_id=rid,
                                  only_dirs=only_dirs, gone_dirs=gone)
            _scan_state['stats'] = stats
            _scan_state['stopped'] = bool(stats.get('stopped'))
        except Exception as e:
            _scan_state['error'] = str(e)
        finally:
            # A scan is the only thing that writes song_name or song_genre, so it is the only thing
            # that can invalidate the generic-names set. In the finally with the state update, so a
            # scan that failed or was stopped part-way still drops it: it may have written rows
            # before it stopped.
            clear_song_name_cache()
            _scan_state.update(running=False, done=True, ended_at=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


def stop_scan():
    _scan_state['cancel'] = True


def run_mine(root_key):
    """Mine one library's prompts/folders/filenames on a worker thread. Pure text (one SELECT up
    front, then no I/O), but on a big library it's long enough to want a progress bar and a Stop —
    so it mirrors run_scan's shape. Returns True if started, False if one is already running."""
    with _miner_lock:
        if _miner_job['running']:
            return False
        _miner_job.update(running=True, seen=0, total=0, done=False, stopped=False, cancel=False,
                          started_at=time.time(), ended_at=0.0, error=None,
                          candidates=None, found=0)

    def worker():
        try:
            conn = db()
            # ORDER BY id: a stable order, so 'seen' means the same thing across runs (and would
            # let a future resume pick up by index).
            rows = conn.execute("SELECT id, positive, folder, filename FROM images "
                                "WHERE root_id IS ? ORDER BY id", (root_key,)).fetchall()
            conn.close()
            _miner_job.update(total=len(rows))
            candidates, postings = prompt_miner.mine(
                rows, CONFIG['miner'],
                progress=lambda n, t: _miner_job.update(seen=n, total=t),
                should_stop=lambda: _miner_job['cancel'])
            _miner_state.update(root=root_key, postings=postings, scanned=len(rows))
            _miner_job.update(candidates=candidates, found=len(candidates),
                              seen=len(rows), stopped=bool(_miner_job['cancel']))
        except Exception as e:
            _miner_job['error'] = str(e)
        finally:
            _miner_job.update(running=False, done=True, ended_at=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


def stop_mine():
    _miner_job['cancel'] = True


def _models_dirs_for(root_id):
    """Ordered models folders for an image: its library's override first, then the global default."""
    dirs = []
    r_root = _root_by_key(root_id) if root_id else None
    if r_root and r_root.get('models_dir'):
        dirs.append(r_root['models_dir'])
    gdir = (CONFIG.get('general') or {}).get('models_dir') or ''
    if gdir and gdir not in dirs:
        dirs.append(gdir)
    return dirs


def run_export_prepare(iid, todo):
    """Background: SHA-256 the given uncached resources [(name, path, size)], reporting byte progress
    so the UI shows a real bar + Stop. Results land in the hash cache so the /export download is fast."""
    with _export_lock:
        if _export_state['running']:
            return False
        _export_state.update(running=True, done=False, stopped=False, cancel=False,
                             phase='Preparing…', done_bytes=0,
                             total_bytes=sum(s for _, _, s in todo), error=None,
                             started_at=time.time(), ended_at=0.0, id=iid)

    def worker():
        try:
            cache_path = os.path.join(DATA_DIR, 'model_hashes.db')
            base = 0
            for name, fp, sz in todo:
                if _export_state['cancel']:
                    _export_state['stopped'] = True
                    break
                # "Reading", not "Hashing/Downloading": the byte counter climbing to 20+GB reads as
                # an internet download otherwise — it's a one-time local file read.
                _export_state['phase'] = 'Reading ' + name
                h = model_hash.sha256_cached(
                    fp, cache_path,
                    progress=lambda cum, b=base: _export_state.__setitem__('done_bytes', b + cum),
                    should_stop=lambda: _export_state['cancel'])
                if h is None and _export_state['cancel']:   # aborted mid-file
                    _export_state['stopped'] = True
                    break
                base += sz
                _export_state['done_bytes'] = base
        except Exception as e:
            _export_state['error'] = str(e)
        finally:
            _export_state.update(running=False, done=True, ended_at=time.time(), phase='')

    threading.Thread(target=worker, daemon=True).start()
    return True


def _progress_snapshot(state):
    """Serializable copy of a progress dict + a derived `elapsed` (secs)."""
    s = {k: v for k, v in state.items() if k != 'cancel'}
    if s.get('started_at'):
        end = s['ended_at'] if (s['ended_at'] and not s['running']) else time.time()
        s['elapsed'] = round(end - s['started_at'], 1)
    else:
        s['elapsed'] = 0
    return s


def _store_reward(conn, iid, reward):
    conn.execute("INSERT INTO quality(image_id, reward) VALUES (?,?) "
                 "ON CONFLICT(image_id) DO UPDATE SET reward=excluded.reward", (iid, reward))


def run_ext_batch(ext_id, ids, state, lock, todo_for, store, prefix):
    """Drive one extension's worker over `ids`, on a background thread. False if already running.

    THE SHARED HALF OF EVERY EXTENSION, and the reason it is shared rather than copied: the
    subprocess, the JSON-line loop, the cancel check, the progress counters and the "worker died
    before saying anything" message are identical for every extension, because they are properties
    of the CONTRACT rather than of the work. What differs is two callbacks:

      todo_for(conn, ids) -> [{'id', 'path'}]   which of these files still need doing
      store(conn, msg)    -> True if the message carried a result

    `prefix` names the extension in its error messages, because "the worker exited early" is
    useless when three of them can be installed.
    """
    ext = get_extension(ext_id)
    if not ext or not ext['active']:
        return False
    with lock:
        if state['running']:
            return False
        state.update(running=True, seen=0, total=0, ok=0, failed=0,
                     done=False, stopped=False, cancel=False,
                     started_at=time.time(), ended_at=0.0,
                     error=None, last_error=None, device=None)
    dbp = ACTIVE['db']   # snapshot: a root switch can't retarget a live run

    def worker():
        proc = None
        items_path = set_path = None
        conn = sqlite3.connect(dbp)
        conn.execute('PRAGMA busy_timeout=%d' % DB_BUSY_TIMEOUT_MS)   # see db()
        conn.row_factory = sqlite3.Row
        try:
            todo = todo_for(conn, ids)
            state['total'] = len(todo)
            if not todo:
                return
            fd, items_path = tempfile.mkstemp(suffix='.json', prefix='ext_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(todo, f)
            # The worker's settings, as a SECOND argument rather than a key inside items.json.
            # Both shapes were available and this one is the compatible one: an existing worker
            # reads argv[1] and never looks at argv[2], where changing items.json from an array to
            # an object would have broken the scorer's worker and the tagger on the same commit that
            # gained the feature. A file, not an environment variable, because an API key in the
            # environment is inherited by anything the worker itself spawns.
            argv = [ext_python(ext), ext['worker'], items_path]
            if ext['settings']:
                fd2, set_path = tempfile.mkstemp(suffix='.json', prefix='extset_')
                with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                    json.dump(ext_settings_for(ext), f)
                argv.append(set_path)
            proc = subprocess.Popen(argv,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', cwd=BASE)
            for line in proc.stdout:
                if state['cancel']:
                    state['stopped'] = True
                    proc.terminate()
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue        # a worker's stray print is not a protocol error
                if msg.get('ready'):
                    state['device'] = msg.get('device')
                    continue
                if store(conn, msg):
                    conn.commit()
                    state['ok'] += 1
                else:
                    state['failed'] += 1
                    state['last_error'] = msg.get('error')
                state['seen'] += 1
            rc = proc.wait()
            # Only when it produced NOTHING: a worker that scored half the batch and then fell over
            # has already reported those failures per-file, and a red banner over real results
            # reads as though they were lost too.
            if rc not in (0, None) and not state['cancel'] and state['ok'] == 0:
                err = (proc.stderr.read() or '').strip()
                state['error'] = ('%s exited early — is it set up (Settings → Extensions)? %s'
                                  % (prefix, err[-400:] or 'exit %s' % rc))
        except FileNotFoundError:
            state['error'] = ("%s isn't set up — set it up from Settings → Extensions." % prefix)
        except Exception as e:
            state['error'] = str(e)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            conn.close()
            # The settings file holds the API key, so it goes whatever happened above — including
            # the paths where the worker never started.
            for tmp in (items_path, set_path):
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            state.update(running=False, done=True, ended_at=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


EXT_TEST_TIMEOUT = 45      # long enough for a cold model to load, short enough to give up on


def run_ext_test(ext, values):
    """Ask one extension to check its own setup. Returns {'ok': bool, 'detail'|'error': str}.

    THE APP NEVER LEARNS WHAT "WORKING" MEANS. It spawns the same worker with the same settings
    file and a --test flag, and repeats whatever comes back — so an extension that talks to a
    model, a web service or a local binary each tests the thing it actually depends on, and none
    of that knowledge leaks in here. Same reasoning as `requires`: an extension is entitled to say
    what finished looks like for it.

    Synchronous, unlike a batch. It is one short call the user pressed a button for, there is no
    progress to show, and the server is threaded — a job with a progress bar for something that
    answers in a second would be machinery around nothing.

    `values` are what is ON SCREEN, not what is saved: testing an address you have just typed is
    the entire point of the button, and it would be a poor one that made you save first.
    """
    if not ext.get('can_test'):
        return {'ok': False, 'error': "%s has no way to test itself." % ext['name']}
    set_path = None
    try:
        fd, set_path = tempfile.mkstemp(suffix='.json', prefix='extset_')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(values, f)
        items_fd, items_path = tempfile.mkstemp(suffix='.json', prefix='ext_')
        with os.fdopen(items_fd, 'w', encoding='utf-8') as f:
            json.dump([], f)
        try:
            p = subprocess.run([ext_python(ext), ext['worker'], items_path, set_path, '--test'],
                               capture_output=True, text=True, encoding='utf-8',
                               cwd=BASE, timeout=EXT_TEST_TIMEOUT)
        finally:
            if os.path.exists(items_path):
                try:
                    os.remove(items_path)
                except OSError:
                    pass
        for line in (p.stdout or '').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue                    # a worker's stray print is not a protocol error
            if 'ok' in msg:
                # `warn` is the third answer: it worked, but not the way it is currently set up.
                # Two states would force that into either a tick or a cross, and both are lies.
                return {'ok': bool(msg.get('ok')), 'warn': bool(msg.get('warn')),
                        'detail': str(msg.get('detail') or '')[:600],
                        'error': str(msg.get('error') or '')[:600]}
        err = (p.stderr or '').strip()
        return {'ok': False,
                'error': 'It did not answer. %s' % (err[-300:] or 'Exit %s.' % p.returncode)}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'No answer after %ds — it may still be loading.'
                                      % EXT_TEST_TIMEOUT}
    except FileNotFoundError:
        return {'ok': False, 'error': "It isn't set up — set it up from Settings → Extensions."}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}
    finally:
        # Holds the API key, so it goes whatever happened above.
        if set_path and os.path.exists(set_path):
            try:
                os.remove(set_path)
            except OSError:
                pass


def _paths_for(conn, ids, have):
    """[{'id','path'}] for `ids`, minus anything already in `have`."""
    todo = []
    for iid in ids:
        if iid in have:
            continue
        row = conn.execute("SELECT path FROM images WHERE id=?", (iid,)).fetchone()
        if row:
            todo.append({'id': iid, 'path': row['path']})
    return todo


def run_reward_batch(ids, skip_scored=True):
    """Score a list of image ids with the pyiqa metric. Instance one of run_ext_batch."""
    def todo_for(conn, ids):
        have = set()
        if skip_scored:
            have = {r[0] for r in conn.execute(
                "SELECT image_id FROM quality WHERE reward IS NOT NULL")}
        return _paths_for(conn, ids, have)

    def store(conn, msg):
        if 'reward' not in msg:
            return False
        _store_reward(conn, msg['id'], msg['reward'])
        return True

    return run_ext_batch('quality', ids, _reward_state, _reward_lock,
                         todo_for, store, 'The Quality scorer')


# ---- tags written by an extension -------------------------------------------------------------
# A machine tag carries `ext:<id>` as its source, so it is distinguishable from a tag the author typed
# ('user') at every point that reads one. That is the whole requirement, and it is his: a bulk
# auto-tag must never be mistaken for something he decided. As a GROUP they are `source LIKE
# 'ext:%'`, which is what makes one bad run undoable without touching a single hand-made tag.
#
# THE SCHEMA COMMENT ON `tags.source` IS WRONG and has been since the first commit. It says
# 'wd14' | 'vlm' | 'manual'; the values actually in use are 'user', 'fav' and 'hide'. The
# extensions plan was written on the strength of that comment and concluded the table was "already
# ready" for machine tags -- it was not, and every read site below had to be widened deliberately.
MACHINE_TAG_SQL = "source LIKE 'ext:%'"
# Either a tag the author typed or one an extension found -- the set that is a TAG, as opposed to the
# favourite and hidden marks that also live in this table.
ANY_TAG_SQL = "(source='user' OR source LIKE 'ext:%')"

_tag_state = {'running': False, 'seen': 0, 'total': 0, 'ok': 0, 'failed': 0,
              'done': False, 'stopped': False, 'cancel': False,
              'started_at': 0.0, 'ended_at': 0.0, 'error': None, 'last_error': None,
              'device': None, 'ext': None}
_tag_lock = threading.Lock()


def _store_tags(conn, ext_id, msg):
    """One worker line -> tag rows. False when the line carried no tags (i.e. it was an error)."""
    if not isinstance(msg.get('tags'), list):
        return False
    src = 'ext:' + ext_id
    iid = msg['id']
    # A re-run REPLACES this extension's tags for this image rather than adding to them, so a
    # second pass with a better threshold does not leave the first pass's rejects behind. Scoped to
    # this extension's source, so it cannot touch the author's tags or another tagger's.
    conn.execute("DELETE FROM tags WHERE image_id=? AND source=?", (iid, src))
    rows = []
    for t in msg['tags']:
        name = (t.get('tag') if isinstance(t, dict) else t) or ''
        name = str(name).strip().replace(',', ' ').strip().lower()[:64]
        # An extension may not set a curation label. `label:<slug>` is the exclusive one-key mark,
        # and the app reads it straight out of this table -- so without this line a worker could
        # mark a thousand images "To publish" by emitting a string.
        if not name or name.startswith('label:'):
            continue
        score = t.get('score') if isinstance(t, dict) else None
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        rows.append((iid, name, src, score))
    if rows:
        conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source, score) "
                         "VALUES (?,?,?,?)", rows)
    return True


def run_tag_batch(ext_id, ids, skip_tagged=True):
    """Tag a list of image ids with one tagger extension. Instance two of run_ext_batch."""
    ext = get_extension(ext_id)
    if not ext or ext['produces'] != 'tags':
        return False
    src = 'ext:' + ext_id

    def todo_for(conn, ids):
        have = set()
        if skip_tagged:
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT image_id FROM tags WHERE source=?", (src,))}
        return _paths_for(conn, ids, have)

    _tag_state['ext'] = ext['name']
    return run_ext_batch(ext_id, ids, _tag_state, _tag_lock, todo_for,
                         lambda conn, msg: _store_tags(conn, ext_id, msg), ext['name'])


# ---- text extensions (answers shown and not kept) -----------------------------------------------
# Instance three of run_ext_batch, and the odd one: a score and a tag both go into the library, so
# their runs end at a commit. THIS ONE ENDS IN A LIST IN MEMORY. The answers live in the run state
# until the client picks them up, and the next run replaces them — which is the whole of "shown and
# not kept", and the reason there is no table, no migration and nothing to clear.
#
# A LIST RATHER THAN ONE ANSWER because a selection is a run of many. The single-image case is the
# batch of one it has always been: the detail view takes the answer whose id matches the file it is
# looking at, and the panel takes all of them in order.
_text_state = {'running': False, 'seen': 0, 'total': 0, 'ok': 0, 'failed': 0,
               'done': False, 'stopped': False, 'cancel': False,
               'started_at': 0.0, 'ended_at': 0.0, 'error': None, 'last_error': None,
               'device': None, 'ext': None, 'answers': [], 'names': {}}
_text_lock = threading.Lock()
# A selection can be larger than anyone will read, and the answers are never stored, so this is a
# ceiling on what one run will hold in memory rather than a limit on what may be asked.
MAX_TEXT_ANSWERS = 500

# What a text worker is told about the file besides its path. Read straight off the images row, so
# this list can only ever name things the library already knows — an extension cannot ask the app
# to go and work something out.
TEXT_FACTS = ('filename', 'folder', 'ext', 'width', 'height', 'model_name', 'model_type',
              'positive', 'negative', 'loras', 'gp_steps', 'gp_cfg', 'gp_sampler',
              'gp_scheduler', 'gp_seed')
# What the answers panel needs to draw a row, alongside the answer itself. Read at the same time as
# the facts, because the panel can be showing a file the grid never loaded — "Select all matching"
# reaches far past the loaded page, so the client cannot be asked to look these up.
TEXT_ROW_COLS = ('filename', 'mtime', 'root_id')


def _store_text(msg):
    """One worker line -> one answer appended to the run state. False when the line carried none.

    A FAILED FILE IS ALSO RECORDED, as an answer carrying `error` instead of `text` — over a
    selection the interesting question is usually "which ones didn't it manage", and a run that
    silently shows nineteen answers for twenty files cannot be asked it. It still counts as a
    failure for the progress counters, which is why this returns False for one."""
    iid = msg.get('id')
    row = _text_state['names'].get(iid) or {}
    txt = msg.get('text')
    ok = isinstance(txt, str) and bool(txt.strip())
    if len(_text_state['answers']) < MAX_TEXT_ANSWERS:
        _text_state['answers'].append(
            {'id': iid, 'name': row.get('name') or '', 'thumb_url': row.get('thumb_url') or '',
             'text': txt.strip() if ok else None,
             'error': None if ok else (str(msg.get('error') or 'No answer.')[:400])})
    return ok


def run_text_batch(ext_id, ids):
    """Ask one text extension about one file or a selection. Instance three of run_ext_batch."""
    ext = get_extension(ext_id)
    if not ext or ext['produces'] != 'text':
        return False

    def todo_for(conn, ids):
        todo = []
        cols = ', '.join(sorted(set(TEXT_FACTS) | set(TEXT_ROW_COLS)))
        for iid in ids:
            row = conn.execute("SELECT path, %s FROM images WHERE id=?" % cols, (iid,)).fetchone()
            if not row:
                continue
            # What the panel needs to draw this answer's row, built HERE rather than in the client.
            # The thumbnail URL carries v= and r= like every other one in the app: ids are globally
            # unique, but a URL without them shares a browser cache entry across roots, which is
            # exactly how one library's pictures once turned up under another's.
            _text_state['names'][iid] = {
                'name': row['filename'],
                'thumb_url': '/thumb/%d?v=%d&r=%s&s=128'
                             % (iid, int(row['mtime'] or 0), row['root_id'] or '')}
            facts = {k: row[k] for k in TEXT_FACTS if row[k] not in (None, '')}
            # `picture` is THE THUMBNAIL, not the original, and that is deliberate three times
            # over: a 20MB PNG becomes a 27MB base64 body for a model that downscales to about a
            # thousand pixels anyway; a video has no frame a worker could read without ffmpeg; and
            # a song's thumbnail is its cover art, so every kind of card arrives as something that
            # can be looked at. It is already on disk for anything the author has scrolled past.
            pic = None
            try:
                rel = thumbs_mod.ensure_thumb(row['path'], THUMBS_DIR)
                pic = os.path.join(THUMBS_DIR, rel) if rel else None
            except Exception:
                pass
            todo.append({'id': iid, 'path': row['path'], 'picture': pic, 'facts': facts})
        return todo

    _text_state.update(ext=ext['name'], answers=[], names={})
    # `store` takes (conn, msg) for every instance; this one has no use for the connection, which
    # is what a text extension not touching the library looks like from here.
    return run_ext_batch(ext_id, ids, _text_state, _text_lock, todo_for,
                         lambda conn, msg: _store_text(msg), ext['name'])


def stop_text():
    _text_state['cancel'] = True


def stop_tagging():
    _tag_state['cancel'] = True


def stop_reward():
    _reward_state['cancel'] = True


def run_setcull(image_id, keep_role):
    """Repeat one set's keep decision across its folder, in a background thread.

    The batch is recomputed here from the anchor rather than taken from the client — the same rule
    /api/dupes/cull follows, so a stale page cannot widen what gets recycled.

    ONE SET AT A TIME, COMMITTED AS IT GOES. That is what makes Stop safe: pressing it leaves every
    set already culled done and every remaining set completely untouched, rather than a half-culled
    set somewhere in the middle. Curation is merged onto the keeper BEFORE its siblings are recycled
    — delete_by_ids drops their tag and quality rows, so merging afterwards would move nothing.

    Returns False if another job already owns the index.
    """
    with _setcull_lock:
        if _setcull_state['running']:
            return False
        _setcull_state.update(running=True, seen=0, total=0, moved=0, purged=0, failed=0,
                              done=False, stopped=False, cancel=False,
                              started_at=time.time(), ended_at=0.0, error=None, folder='')
    dbp = ACTIVE['db']

    def worker():
        conn = sqlite3.connect(dbp)
        conn.execute('PRAGMA busy_timeout=%d' % DB_BUSY_TIMEOUT_MS)   # see db()
        conn.row_factory = sqlite3.Row
        root_id = None
        try:
            matches, stats = find_setcull_sets(conn, image_id, keep_role)
            root_id = stats['root_id']
            _setcull_state.update(total=len(matches), folder=stats['folder'])
            for keeper, doomed in matches:
                if _setcull_state['cancel']:
                    _setcull_state['stopped'] = True
                    break
                merge_curation(conn, keeper, doomed)
                conn.commit()
                moved, purged, failed = _recycle_ids(doomed)
                _setcull_state['moved'] += moved
                _setcull_state['purged'] += purged
                _setcull_state['failed'] += len(failed)
                _setcull_state['seen'] += 1
        except ValueError as e:
            _setcull_state['error'] = str(e)
        except Exception as e:
            _log_exc('[setcull]')
            _setcull_state['error'] = str(e)
        finally:
            # Sets that lost members are no longer sets. Runs even after a Stop, so the survivors of
            # the part that DID run stop reporting as collapsed cards.
            if root_id and _setcull_state['seen']:
                try:
                    index_db.recompute_groups(conn, root_id)
                    conn.commit()
                except Exception:
                    _log_exc('[setcull] regroup')
            conn.close()
            _setcull_state.update(running=False, done=True, ended_at=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


def stop_setcull():
    _setcull_state['cancel'] = True


def _job_running():
    """True while a scan, reward run or folder cull is in flight (one at a time)."""
    return (_scan_state['running'] or _reward_state['running'] or _tag_state['running']
            or _setcull_state['running'])


def _asset_version(path):
    """Short content hash of a UI asset, for cache-busting its URL. Changes only
    when the file's contents change, so browsers reload it exactly when needed."""
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()[:8]
    except OSError:
        return '0'


_STATIC_TYPES = {'.css': 'text/css', '.js': 'application/javascript', '.svg': 'image/svg+xml',
                 '.png': 'image/png', '.ico': 'image/x-icon'}


def _static_ctype(name):
    """Content type for a file served out of app/. Only the text formats get a charset --
    tacking one onto a PNG makes some clients treat the bytes as text and mangle them."""
    ext = os.path.splitext(name)[1].lower()
    ctype = _STATIC_TYPES.get(ext, 'text/plain')
    text = ctype.startswith('text/') or ctype in ('application/javascript', 'image/svg+xml')
    return ctype + '; charset=utf-8' if text else ctype


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1, so the browser can REUSE a connection instead of opening and closing one per
    # request. BaseHTTPRequestHandler defaults to 1.0, and that default was the app's scrolling
    # ceiling: a browser allows ~6 connections per host, and a scroll fires a burst of lazy
    # thumbnail requests that occupy all six, so the NEXT PAGE OF CARDS queues behind pictures.
    #
    # Measured, from a trace of a real scroll (BR-9). Server time per page sat rock steady at
    # ~300ms on the whole library and ~130ms in a folder, while the round trip the browser saw
    # climbed to 1,700-2,900ms — and dropped straight back to ~150ms on the pages fetched while
    # the user had paused and the connections were free. Same work, 20x the wait, and the
    # difference was entirely queueing. It is also why the complaint was identical on a folder
    # and on the whole library: the bottleneck was never the query.
    #
    # The cost of this is that a short or aborted body now poisons a REUSED connection rather
    # than harmlessly ending a doomed one — so every response must be exactly as long as its
    # Content-Length claims. All four response sites are framed; _serve_range guards the two
    # ways it can under-deliver (see there).
    protocol_version = 'HTTP/1.1'
    # An idle kept-alive connection otherwise pins its thread forever. Six browser connections is
    # six parked threads, which is fine; unbounded is not.
    timeout = 30

    def log_message(self, *a):
        pass  # quiet

    def _server_ms(self):
        """How long this request has been in the handler, for the debug trace's X-Elapsed-Ms.

        Worth the header rather than timing it client-side: a round trip measured in the browser
        bundles the query, the queue and the transfer together, so a slow view can't be attributed.
        _t0 is set in do_GET/do_POST; missing means something answered outside them."""
        t0 = getattr(self, '_t0', None)
        return None if t0 is None else round((time.perf_counter() - t0) * 1000, 1)

    @contextlib.contextmanager
    def _phase(self, name):
        """Time one part of a request, for the debug trace's X-Phases header.

        X-Elapsed-Ms says a request took two seconds; it cannot say WHICH two seconds. Every
        performance question this project has had was really "which part" — and the last one cost
        two wrong theories before a bisect answered it. This is the cheap instrument that would
        have answered it first."""
        t = time.perf_counter()
        try:
            yield
        finally:
            d = getattr(self, '_phases', None)
            if d is None:
                d = self._phases = {}
            d[name] = round(d.get(name, 0.0) + (time.perf_counter() - t) * 1000, 1)

    # -- helpers --
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        ms = self._server_ms()
        if ms is not None:
            self.send_header('X-Elapsed-Ms', str(ms))
        ph = getattr(self, '_phases', None)
        if ph:
            self.send_header('X-Phases', ' '.join(f'{k}={v}' for k, v in ph.items()))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, code=200, cache=False, disposition=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        # Thumbnails/originals are content-stable -> cache hard. UI files
        # (html/js/css) must stay fresh so updates show without a hard refresh.
        self.send_header('Cache-Control', 'public, max-age=86400' if cache else 'no-cache')
        ms = self._server_ms()
        if ms is not None:
            self.send_header('X-Elapsed-Ms', str(ms))
        if disposition:                         # e.g. attachment; filename="…" for a download
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype, cache=False):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return self._json({'error': 'not found'}, 404)
        self._bytes(data, ctype, cache=cache)

    def _serve_range(self, path, ctype, name=None):
        """Stream a file with HTTP Range support so video can seek. Sends 206 Partial
        Content for a Range request, 200 otherwise; streams in chunks so large videos
        are never read fully into memory.

        `name` sets an INLINE Content-Disposition — it names the file without turning the
        response into a download, so <img> and <video> are unaffected. It exists for
        drag-to-ComfyUI: the browser names the dropped file from this (or from the URL's last
        path segment), and without it every drop landed in ComfyUI's input folder as the
        item's row number, e.g. 28442.png."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({'error': 'not found'}, 404)
        start, end, partial = 0, size - 1, False
        rng = self.headers.get('Range')
        if rng and rng.startswith('bytes='):
            try:
                s, _, e = rng[6:].partition('-')
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else size - 1
                else:                                   # suffix range: bytes=-N -> last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    # Explicitly empty, not absent: on a reused connection a response with no
                    # Content-Length leaves the client waiting for a body that never comes.
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except Exception:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if name:
            self.send_header('Content-Disposition', f'inline; filename="{_safe_filename(name)}"')
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        if self.command == 'HEAD':
            return
        sent = 0
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(262144, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    sent += len(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                        # client seeked/closed — normal while scrubbing
        # THE ONE PLACE KEEP-ALIVE IS DANGEROUS. Both exits above can send fewer bytes than the
        # Content-Length promised: a file that shrank under us breaks the loop, and scrubbing a
        # video aborts the write on purpose — the common case, not the exotic one. On HTTP/1.0
        # that was harmless because the connection was closing anyway; on a REUSED connection the
        # shortfall would be read as the beginning of the next response, corrupting whatever the
        # browser asked for next. So a short body ends the connection rather than poisoning it.
        if sent != length:
            self.close_connection = True

    def serve_index(self):
        """Serve index.html (never cached) with its asset URLs stamped by content
        hash, so app.js/style.css auto-reload after any update — no hard refresh."""
        try:
            with open(os.path.join(APP_DIR, 'index.html'), 'r', encoding='utf-8') as f:
                html = f.read()
        except OSError:
            return self._json({'error': 'not found'}, 404)
        for name in ('style.css', 'app.js'):
            v = _asset_version(os.path.join(APP_DIR, name))
            html = html.replace(f'/static/{name}', f'/static/{name}?v={v}')
        self._bytes(html.encode('utf-8'), 'text/html; charset=utf-8', cache=False)

    # -- routing --
    def do_GET(self):
        self._t0 = time.perf_counter()
        # Reset per REQUEST, not per handler: with HTTP/1.1 one handler instance serves every
        # request on a kept-alive connection, so a dict left on `self` accumulates and reports a
        # 2-second page as 12 seconds. Found by the numbers being obviously impossible in a trace.
        self._phases = {}
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if p == '/' or p == '/index.html':
                return self.serve_index()
            if p.startswith('/static/'):
                name = os.path.basename(p)
                # Versioned URLs (?v=hash) mean these can cache hard; a code change
                # changes the hash -> new URL -> the browser fetches the fresh file.
                return self._file(os.path.join(APP_DIR, name), _static_ctype(name), cache=True)
            if p == '/favicon.ico':
                # Browsers ask for this by convention whatever the <link> tags say, and an
                # --app window asks early -- so answer it rather than 404ing into the JSON
                # fallthrough. It's the same PNG; the .ico name is the convention, not the format.
                return self._file(os.path.join(APP_DIR, 'icon.png'), 'image/png', cache=True)
            if p == '/api/doc':
                return self.api_doc(q)
            if p == '/api/recycle-status':
                return self.api_recycle_status()
            if p == '/api/config':
                return self.api_get_config()
            if p == '/api/extensions':
                return self._json({'extensions': _extensions_payload()})
            if p == '/api/facets':
                return self.api_facets(q)
            if p == '/api/search':
                return self.api_search(q)
            if p == '/api/ids':
                return self.api_ids(q)
            if p == '/api/tags':
                return self.api_tags(q)
            if p == '/api/comfy/status':
                return self._json({'ready': comfy_bridge_ready(), 'url': COMFY_URL})
            if p == '/api/changes':
                return self.api_changes()
            if p == '/api/catchup':
                return self.api_catchup()
            if p == '/api/update':
                return self.api_update()
            if p == '/api/delete/result':
                return self.api_delete_result(q)
            if p.startswith('/api/image/'):
                return self.api_image(int(p.rsplit('/', 1)[-1]))
            if p == '/api/scan/status':
                return self._json(_progress_snapshot(_scan_state))
            if p == '/api/reward/status':
                return self._json(_progress_snapshot(_reward_state))
            if p == '/api/tagger/status':
                return self._json(_progress_snapshot(_tag_state))
            if p == '/api/text/status':
                s = _progress_snapshot(_text_state)
                # Only what the client has not seen. A poll every half-second that re-sent every
                # answer would carry the whole run again each time, and a paragraph per file over
                # a large selection is real bytes -- the client appends, so it only needs the tail.
                try:
                    since = max(0, int((q.get('since') or ['0'])[0]))
                except (TypeError, ValueError):
                    since = 0
                all_a = _text_state['answers']
                s['answers'] = all_a[since:]
                s['answers_total'] = len(all_a)
                return self._json(s)
            if p == '/api/miner/status':
                return self.api_miner_status()
            if p.startswith('/thumb/'):
                return self.serve_thumb(int(p.rsplit('/', 1)[-1]), q)
            if p.startswith('/file/'):
                # /file/<id> and /file/<id>/<original name> both work; the trailing name is
                # decoration the browser reads, never something we look up. See serve_file.
                return self.serve_file(int(p.split('/')[2]))
            if p.startswith('/dragpng/'):        # a video, as a picture ComfyUI accepts on a drop
                return self.serve_drag_png(int(p.split('/')[2]))
            if p.startswith('/export/civitai/'):
                return self.export_civitai(int(p.rsplit('/', 1)[-1]))
            if p.startswith('/api/civitai_text/'):
                return self.civitai_text(int(p.rsplit('/', 1)[-1]), q)
            if p == '/api/models/check':
                return self.api_models_check(q)
            if p == '/api/folder_dupes':
                return self.api_folder_dupes(q)
            if p == '/api/dupes/scan':
                return self.api_dupes_scan(q)
            if p == '/api/setcull/preview':
                return self.api_setcull_preview(q)
            if p == '/api/setcull/status':
                return self._json(_progress_snapshot(_setcull_state))
            if p == '/api/export/civitai/status':
                return self.api_export_status()
            return self._json({'error': 'not found'}, 404)
        except Exception as e:
            # The client only ever sees str(e); the traceback is the half that says WHERE. It goes
            # to stderr, which start.bat captures into data/viewer.log (the server runs hidden, so
            # unlogged stderr is stderr thrown away).
            _log_exc('GET ' + p)
            # A lock is not a normal error: it means someone else is holding the single writer, and
            # the useful half is WHO. Reported at the request boundary so every endpoint is covered
            # by one place rather than sixteen.
            if isinstance(e, sqlite3.OperationalError) and 'locked' in str(e).lower():
                report_locked('GET ' + p)
            return self._json({'error': str(e)}, 500)

    def do_POST(self):
        self._t0 = time.perf_counter()
        # Reset per REQUEST, not per handler: with HTTP/1.1 one handler instance serves every
        # request on a kept-alive connection, so a dict left on `self` accumulates and reports a
        # 2-second page as 12 seconds. Found by the numbers being obviously impossible in a trace.
        self._phases = {}
        # THE BODY IS READ HERE, before dispatch, whatever the handler goes on to do with it.
        # HTTP/1.1 keeps the connection open, so a handler that answers WITHOUT reading the body
        # -- a 400 for a missing id, a 409 while a job is running -- leaves it in the socket, and
        # the next request on that connection reads it as a request line and answers 501
        # "Unsupported method". It was reachable but rare while every early return was rare;
        # switching an extension off made one of them ordinary. The body belongs to the request,
        # not to whichever branch happens to want it.
        try:
            ln = int(self.headers.get('Content-Length') or 0)
            self._body = self.rfile.read(ln) if ln else b''
        except Exception:
            self._body = b''
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p == '/api/pick-folder':
                return self.api_pick_folder()
            if p == '/api/roots/add':
                return self.api_root_add()
            if p == '/api/roots/select':
                return self.api_root_select()
            if p == '/api/roots/rename':
                return self.api_root_rename()
            if p == '/api/roots/models_dir':
                return self.api_root_models_dir()
            if p == '/api/folder_dupes/apply':
                return self.api_folder_dupes_apply()
            if p == '/api/dupes/cull':
                return self.api_dupes_cull()
            if p == '/api/setcull':
                return self.api_setcull()
            if p == '/api/setcull/stop':
                stop_setcull()
                return self._json({'ok': True})
            if p == '/api/export/civitai/prepare':
                return self.api_export_prepare()
            if p == '/api/export/civitai/stop':
                return self.api_export_stop()
            if p == '/api/roots/remove':
                return self.api_root_remove()
            if p == '/api/scan':
                _b = self._read_json()
                # run_scan only knows about other scans. A Quality run is reading the same index,
                # so a scan started underneath it would move the rows out from under a live job —
                # refuse here, where we can say which kind of job is in the way.
                if _reward_state['running']:
                    return self._json({'error': 'A Quality run is in progress — stop it '
                                                'first, then scan.'}, 409)
                _ok = run_scan(force=bool(_b.get('force')), key=_b.get('key'))
                if not _ok:
                    return self._json({'error': 'A scan is already running.'}, 409)
                return self._json({'started': True})
            if p == '/api/scan/stop':
                stop_scan()
                return self._json({'stopping': True})
            if p == '/api/delete':
                return self.api_delete()
            if p == '/api/delete/undo':
                return self.api_delete_undo()
            if p == '/api/delete/commit':
                return self.api_delete_commit()
            if p == '/api/tag':
                return self.api_tag()
            if p == '/api/label':
                return self.api_label()
            if p == '/api/tags/delete':
                return self.api_tag_delete()
            if p == '/api/tags/clear':
                return self.api_tags_clear()
            if p == '/api/miner/scan':
                return self.api_miner_scan()
            if p == '/api/miner/stop':
                stop_mine()
                return self._json({'stopping': True})
            if p == '/api/miner/apply':
                return self.api_miner_apply()
            if p == '/api/favorite':
                return self.api_favorite()
            if p == '/api/note':
                return self.api_note()
            if p == '/api/rename':
                return self.api_rename()
            if p == '/api/extensions/enable':
                return self.api_ext_enable()
            if p == '/api/extensions/settings':
                return self.api_ext_settings()
            if p == '/api/extensions/test':
                return self.api_ext_test()
            if p == '/api/extensions/setup':
                return self.api_ext_setup()
            if p == '/api/tagger/batch':
                return self.api_tag_batch()
            if p == '/api/tagger/stop':
                stop_tagging()
                return self._json({'stopping': True})
            if p == '/api/text/ask':
                return self.api_text_ask()
            if p == '/api/text/stop':
                stop_text()
                return self._json({'stopping': True})
            if p == '/api/tags/clear-machine':
                return self.api_clear_machine_tags()
            if p == '/api/reward/batch':
                return self.api_reward_batch()
            if p == '/api/reward/stop':
                stop_reward()
                return self._json({'stopping': True})
            if p == '/api/comfy/open':
                return self.api_comfy_open()
            if p == '/api/opened':
                return self.api_opened()
            if p == '/api/window':
                return self.api_window()
            if p == '/api/scores/clear':
                return self.api_clear_scores()
            if p == '/api/settings':
                return self.api_save_settings()
            if p == '/api/snapshots':
                return self.api_save_snapshots()
            if p == '/api/alive':
                return self.api_alive()
            if p == '/api/bye':
                return self.api_bye()
            if p.startswith('/api/reveal/'):
                return self.reveal(int(p.rsplit('/', 1)[-1]))
            if p == '/api/recycle-folder':
                return self.reveal_recycle()
            # DRAIN THE BODY BEFORE ANSWERING. A handled POST consumes its body via _read_json();
            # an unhandled one never did, so the bytes stayed in the socket and — on a kept-alive
            # connection — were parsed as the head of the NEXT request. Seen for real:
            #   POST /api/delete -> 501 Unsupported method ('{"batch":4}POST')
            # after a POST to a path that didn't exist yet. The 404 looked harmless; it silently
            # broke the request after it. This is the fourth way into the mis-framing hazard the
            # keep-alive work closed three of (see test_keepalive.py) — same class, opposite end:
            # there the RESPONSE was short, here the REQUEST is left unread.
            self._read_json()
            return self._json({'error': 'not found'}, 404)
        except Exception as e:
            _log_exc('POST ' + p)
            if isinstance(e, sqlite3.OperationalError) and 'locked' in str(e).lower():
                report_locked('POST ' + p)
            # Same reasoning as the 404 above: a handler that raised may have raised BEFORE reading
            # the body, and an unread body poisons the next request on this connection.
            self._read_json()
            return self._json({'error': str(e)}, 500)

    # -- endpoints --
    def _read_json(self):
        """The POST body, read once by do_POST. See the note there for why it is read there."""
        raw = getattr(self, '_body', None)
        if raw is None:                       # defensive: a caller outside the do_POST path
            try:
                ln = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(ln) if ln else b''
            except Exception:
                raw = b''
        try:
            return json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            return {}

    def api_doc(self, q):
        """Raw markdown for the Help window; the client renders it.

        Rendering server-side would need a markdown library, and this project has no package
        manager on the frontend and only three runtime pip dependencies -- all of which earn
        their place by doing something Python cannot. Our own docs use a small, boring subset
        (headings, tables, bold, code, lists, links, quotes), so the renderer is ~120 lines of
        app.js and costs nothing to install.

        Not cached: the docs change as often as the code, and this is read once per Help open."""
        name = (q.get('name') or [''])[0]
        fn = HELP_DOCS.get(name)
        if not fn:
            return self._json({'error': 'unknown doc'}, 404)
        # A deploy copies an explicit file list plus app/ -- docs/ was added to update.bat when
        # Help shipped. If it is missing, say so rather than serving an empty window.
        return self._file(os.path.join(DOCS_DIR, fn), 'text/markdown; charset=utf-8')

    def api_get_config(self):
        roots = [{'path': r['path'], 'name': r['name'], 'key': r['key'],
                  'models_dir': r.get('models_dir', ''),   # per-library export models-folder override
                  'exists': os.path.isdir(r['path']),
                  # Whether this library HAS a _ToRecycle folder, which is what decides whether the
                  # menu offers to open one. Tested rather than inferred from local-vs-network: a
                  # network library nothing has been deleted from has none either, and a LOCAL one
                  # can have one, since _recycle_one falls through to it when the OS bin refuses.
                  # Guarded by the isdir above so an unreachable share is never stat'd twice.
                  'has_recycle': (os.path.isdir(os.path.join(r['path'], RECYCLE_DIRNAME))
                                  if os.path.isdir(r['path']) else False),
                  'indexed': _root_has_images(r['key'])}
                 for r in CONFIG['roots']]
        self._json({'roots': roots, 'active': CONFIG['active'],
                    'general': CONFIG['general'],
                    'general_defaults': DEFAULT_GENERAL,
                    'miner': CONFIG['miner'],
                    'miner_defaults': DEFAULT_MINER,
                    'theme': CONFIG['theme'],
                    'cards': _effective_cards(),
                    'cards_defaults': [dict(r) for r in DEFAULT_CARD_FACTS],
                    'snapshots': CONFIG['snapshots'],
                    'labels': LABELS,
                    'extensions': _extensions_payload(),
                    'metric_range': list(METRIC_RANGE),
                    'app_name': APP_NAME,
                    'version': APP_VERSION,
                    'build': BUILD,
                    'seen_help_hint': bool(CONFIG.get('seen_help_hint')),
                    # Sent back so the client can see whether the launcher's --window-position was
                    # actually honoured: it compares this to where the window really is.
                    'window': CONFIG.get('window')})

    def api_clear_scores(self):
        """Wipe stored pyiqa Quality scores for ONE library so they can be re-run.

        `key` NAMES THE LIBRARY, and the ACTIVE fallback is only for a caller that has none. This
        used to read ACTIVE unconditionally, from a Settings button that had no library to send —
        so it cleared whichever library the invisible management pointer happened to hold, which
        the user never picked and could not see. The control moved into the library's own menu on
        2026-09-08 and sends its key; api_clear_machine_tags already worked this way, which is
        what the two now have in common.

        (`what` is kept in the API shape for the client, but 'metric' is all that's left
        since the LLM scan was removed.)"""
        if self._job_busy():   # 409 while a scan/metric run is in flight
            return
        b = self._read_json()
        what = (b.get('what') or 'metric').lower()
        if what != 'metric':
            return self._json({'error': "what must be 'metric'"}, 400)
        conn = db()
        rk = b.get('key') or ACTIVE['key']
        insub = "image_id IN (SELECT id FROM images WHERE root_id IS ?)"   # scope to the target root
        # A DELETE now, where this used to be `SET reward=NULL`. The NULL was to protect dormant
        # legacy_llm_* data sharing the row; those columns were dropped on 2026-08-23, so `reward`
        # is the only thing the table holds and an empty row is just a row with nothing in it.
        conn.execute(f"DELETE FROM quality WHERE {insub}", (rk,))
        n = conn.total_changes
        conn.commit()
        conn.close()
        self._json({'ok': True, 'what': what, 'changed': n})

    def api_miner_scan(self):
        """Start mining one library's prompts/folders/filenames (by key) on a worker thread.
        Returns immediately; the client polls /api/miner/status for progress + the result, and can
        cancel via /api/miner/stop. Stashes postings in _miner_state so api_miner_apply can tag
        by id."""
        if self._job_busy():                        # mining a library mid-scan reads a moving index
            return
        rk = self._read_json().get('key') or ACTIVE['key']
        if not isinstance(rk, str):                 # guard: never bind a non-string key into SQL
            rk = ACTIVE['key']
        if not run_mine(rk):
            return self._json({'error': 'A mine is already running.'}, 409)
        self._json({'started': True})

    def api_miner_status(self):
        """Miner progress. `candidates` can be thousands of entries, so it rides only on the poll
        that reports the run finished — not on every tick."""
        s = _progress_snapshot(_miner_job)
        if s.get('running'):
            s['candidates'] = None
        return self._json(s)

    def api_miner_apply(self):
        """Apply approved mined tags. Writes each tag to exactly the images the last mine's
        postings recorded (for whichever library was mined), as ordinary source='user' tags."""
        b = self._read_json()
        tags = [str(t).strip().lower() for t in (b.get('tags') or []) if str(t).strip()]
        postings = _miner_state.get('postings') or {}
        if not postings:
            return self._json({'error': 'No mine results to apply — run Mine again.'}, 409)
        conn = db()
        applied = 0
        for tag in tags:
            ids = postings.get(tag)
            if not ids:
                continue
            conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES (?,?,'user')",
                             [(i, tag) for i in ids])
            applied += 1
        n = conn.total_changes
        conn.commit()
        conn.close()
        self._json({'ok': True, 'tags_applied': applied, 'rows_added': n})

    def api_tags_clear(self):
        """Wipe one library's regular + style: tags (by key), keeping labels, favorites and Hidden.
        Destructive; the client confirms first. (label:* stays; source='fav' is
        untouched — the DELETE is source='user'-scoped, so curation marks are structurally safe.)"""
        if self._job_busy():   # 409 while a scan or Quality run is in flight
            return
        rk = self._read_json().get('key') or ACTIVE['key']
        conn = db()
        conn.execute("DELETE FROM tags WHERE source='user' AND tag NOT LIKE 'label:%' "
                     "AND image_id IN (SELECT id FROM images WHERE root_id IS ?)", (rk,))
        n = conn.total_changes
        conn.commit()
        conn.close()
        # A wipe invalidates any pending mine-apply for that library.
        if _miner_state.get('root') == rk:
            _miner_state.update(root=None, postings={}, scanned=0)
        self._json({'ok': True, 'removed': n})

    def _job_busy(self):
        """True (+ writes a 409) if a scan or Quality run is in flight.
        Wording stays generic: this guards root changes, the miner, Clear tags and the
        single-image scorers, so it can't name any one of them."""
        if _job_running():
            self._json({'error': 'A scan or a Quality run is in progress — wait for it to '
                                 'finish, or stop it first.'}, 409)
            return True
        return False

    def api_pick_folder(self):
        """Open a native OS folder chooser on the machine running the server (localhost)
        and return the chosen absolute path. Runs tkinter in a subprocess so it gets its
        own main thread (this HTTP server is threaded, and Tk isn't thread-friendly)."""
        script = (
            "import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw()\n"
            "try: r.attributes('-topmost', True)\n"
            "except Exception: pass\n"
            "p = filedialog.askdirectory(title='Select a folder to add as a root')\n"
            "r.destroy()\n"
            "import sys; sys.stdout.write(p or '')\n"
        )
        try:
            res = subprocess.run([sys.executable, '-c', script],
                                 capture_output=True, text=True, timeout=300)
        except Exception as e:
            return self._json({'error': f'Could not open the folder picker: {e}'}, 500)
        path = (res.stdout or '').strip()
        self._json({'path': os.path.normpath(path) if path else ''})

    def api_root_add(self):
        body = self._read_json()
        path = (body.get('path') or '').strip().strip('"')
        if not path:
            return self._json({'error': 'Enter a folder path.'}, 400)
        state = probe_dir(path)
        if state == 'unreachable':
            return self._json({'error': unreachable_msg(path), 'unreachable': True}, 400)
        if state == 'missing':
            return self._json({'error': missing_msg(path)}, 400)
        path = os.path.abspath(path)
        key = _root_key(path)
        # Only a root that still needs its first scan has to wait (one scan at a time).
        if not _root_has_images(key) and _job_running():
            return self._json({'error': 'Another folder is still indexing — add this one '
                                        'once it finishes.'}, 409)
        if not _root_by_key(key):
            CONFIG['roots'].append({'path': path, 'key': key,
                                    'name': (body.get('name') or '').strip() or _default_name(path)})
        set_active(key)
        try:
            save_config()
        except Exception as e:
            return self._json({'error': f'Could not save config: {e}'}, 500)
        scanning = False
        if not _root_has_images(key):
            run_scan()
            scanning = True
        self._json({'ok': True, 'active': key, 'scanning': scanning})

    def api_root_select(self):
        key = self._read_json().get('key')
        r = _root_by_key(key)
        if not r:
            return self._json({'error': 'unknown root'}, 404)
        # Switching to an already-indexed root is always safe — a running scan is
        # bound to its own root's DB. Only a root that needs its first scan must wait, since
        # we run one scan at a time.
        needs_scan = os.path.isdir(r['path']) and not _root_has_images(key)
        if needs_scan and _job_running():
            return self._json({'error': 'Another folder is still indexing — you can switch to '
                                        'already-indexed roots now; new folders index once it '
                                        'finishes.'}, 409)
        set_active(key)
        save_config()
        scanning = False
        if needs_scan:
            run_scan()
            scanning = True
        self._json({'ok': True, 'active': key, 'scanning': scanning})

    def api_root_rename(self):
        body = self._read_json()
        r = _root_by_key(body.get('key'))
        name = (body.get('name') or '').strip()
        if not r or not name:
            return self._json({'error': 'need key and name'}, 400)
        r['name'] = name
        save_config()
        self._json({'ok': True})

    def api_root_models_dir(self):
        """Set (or clear) a library's own ComfyUI models folder override for export hashing."""
        body = self._read_json()
        r = _root_by_key(body.get('key'))
        if not r:
            return self._json({'error': 'need key'}, 400)
        md = str(body.get('models_dir') or '').strip()
        r['models_dir'] = md
        save_config()
        self._json({'ok': True, 'check': model_hash.check_dir(md) if md else None})

    def api_models_check(self, q):
        """Is a models folder reachable from this machine (+ has checkpoints/loras)? UI hint."""
        self._json(model_hash.check_dir((q.get('path', [''])[0] or '').strip()))

    def api_root_remove(self):
        if self._job_busy():
            return
        body = self._read_json()
        key = body.get('key')
        if not _root_by_key(key):
            return self._json({'error': 'unknown root'}, 404)
        CONFIG['roots'] = [r for r in CONFIG['roots'] if r['key'] != key]
        if CONFIG['active'] == key:   # removed the active one -> fall back to the first remaining
            set_active(CONFIG['roots'][0]['key'] if CONFIG['roots'] else None)
        if body.get('delete_index'):
            try:
                index_db.delete_root(LIBRARY_DB, key)   # drop this root's rows from the merged DB
            except Exception:
                pass
        save_config()
        self._json({'ok': True, 'active': CONFIG['active']})

    def api_facets(self, q):
        """The sidebar's counts: models, folders, model types, libraries, and the overall total.

        ONE PASS OVER THE LIBRARY, not five. Each facet reflects the other active filters but NOT
        its own selection, so the currently-picked value stays available to switch away from — and
        that is the *only* way the five differ. Read as five queries that meant five whole-library
        GROUP BYs; read as one, it is a single scan carrying the four dimensions and a flag per
        dimension saying whether this row passes that dimension's filter. Each tally then applies
        the three flags that are not its own.

        Measured on a 120k-row fixture with real prompt text (`bench_facets.py`), old endpoint
        against new: **682 → 215ms unfiltered**, which is the boot case and the default view;
        683 → 136 with a folder filter; 394 → 175 on a text search. Every list identical
        (`test_facet_counts.py`). The win is not cleverness in any one tally — it is that the five
        tallies stopped each reading the whole `images` table off disk, prompt text and all, to
        look at one small column apiece.

        Covering indexes on the dimensions were measured as the alternative and rejected: they took
        the five-query form from 847 to 372ms for 8MB of index, where this is 232ms and costs
        nothing on disk.
        """
        conn = db()
        # Every faceted dimension leaves the WHERE and comes back as a flag. Everything else the
        # user narrowed — text, tags, dates, quality, type — stays in the WHERE, where it belongs:
        # it applies to all five tallies equally, so it should shrink the pass, not be re-tested.
        joins, wsql, params = self._filters(q, exclude=FACET_DIMS)
        flags, fparams = [], []
        for d in FACET_DIMS:
            sql, dp = _dim_clause(q, d)
            flags.append('1' if sql is None else '(%s)' % sql)
            fparams += dp
        ok_root, ok_model, ok_folder, ok_mfolder = flags
        # PARAMETER ORDER IS THE SELECT LIST FIRST. SQLite binds `?` by position in the finished
        # SQL text, and the flags are in the projection, ahead of the JOIN and the WHERE.
        params = fparams + params
        cte = (
            f"WITH facetrows AS MATERIALIZED (SELECT i.model_name AS model_name, i.folder AS folder, "
            f"{MODEL_TYPE_SQL} AS mtype, "
            f"(i.model IS NOT NULL AND i.model <> '') AS has_model, i.root_id AS root_id, "
            f"{ok_root} AS ok_root, {ok_model} AS ok_model, "
            f"{ok_folder} AS ok_folder, {ok_mfolder} AS ok_mfolder "
            f"FROM images i {joins} {wsql}) ")
        # MATERIALIZED is not decoration: without it SQLite re-runs the scan for each arm of the
        # compound below and the whole point is lost. db() sets temp_store=MEMORY so the
        # materialisation stays in RAM rather than being written out as a temp file mid-read.
        rows = conn.execute(cte + """
            SELECT 'model' AS dim, model_name AS k, COUNT(*) AS c FROM facetrows
              WHERE ok_root AND ok_folder AND ok_mfolder GROUP BY model_name
            UNION ALL SELECT 'folder', folder, COUNT(*) FROM facetrows
              WHERE ok_root AND ok_model AND ok_mfolder GROUP BY folder
            UNION ALL SELECT 'mtype', mtype, COUNT(*) FROM facetrows
              WHERE ok_root AND ok_model AND ok_folder AND has_model GROUP BY LOWER(mtype)
            UNION ALL SELECT 'root', root_id, COUNT(*) FROM facetrows
              WHERE ok_model AND ok_folder AND ok_mfolder GROUP BY root_id
            UNION ALL SELECT 'total', NULL, COUNT(*) FROM facetrows
              WHERE ok_root AND ok_model AND ok_folder AND ok_mfolder
        """, params).fetchall()
        # EVERY library's size, unfiltered — see the roots block below for why it cannot come out of
        # `facetrows`. Taken here because the connection closes on the next line.
        rtotals = {r['root_id']: r['n'] for r in conn.execute(
            "SELECT root_id, COUNT(*) n FROM images GROUP BY root_id").fetchall()}
        conn.close()
        # Sorting moved out of SQL with the compound — a compound SELECT takes one ORDER BY for the
        # whole thing, and these four lists want four different ones. The lists are tens to a few
        # hundred rows, so it is free here and was never the cost there.
        by = {}
        for r in rows:
            by.setdefault(r['dim'], []).append((r['k'], r['c']))
        models = [{'name': k or '(none)', 'count': c}
                  for k, c in sorted(by.get('model', []), key=lambda kc: -kc[1])]
        folders = [{'name': k, 'count': c}
                   for k, c in sorted(by.get('folder', []), key=lambda kc: (-kc[1], kc[0]))]
        mtypes = [{'name': k if k else '(none)', 'count': c}
                  for k, c in sorted(by.get('mtype', []), key=lambda kc: -kc[1])]
        # Roots facet (merged DB): count per root under the OTHER filters, then list EVERY
        # configured root (0 when nothing matches) so the checklist can always toggle it.
        #
        # AND ITS SIZE, which is a different question and the one the Libraries list asks. `count`
        # answers "how many of your matches are in here", which is what a facet is for; the library
        # rows were showing it as though it were the library's size, so a search made every library
        # shrink and the list re-order itself under the cursor. The author, 2026-09-14: "it should always
        # read the total file count." A separate tally because `facetrows` has the filters baked in
        # and cannot answer an unfiltered question; cheap because idx_images_root makes it an
        # index-only scan (measured with bench_facets.py, see the note above). Taken above, beside
        # the main query, because the connection is closed before this point.
        rcounts = dict(by.get('root', []))
        # IN CONFIG ORDER, which is the order the libraries were added, because that is the order the
        # Libraries list draws them in -- it maps over state.roots and uses this only as a lookup
        # table of counts. The other three facets are ranked lists where "most first" is the whole
        # point; this one is not a list at all by the time it reaches the screen. It was sorted by
        # count and briefly by size, and both were a claim about an order nothing reads: a later
        # reader could only conclude the rows were meant to follow it.
        roots = [{'key': r['key'], 'name': r['name'], 'count': rcounts.get(r['key'], 0),
                  'total': rtotals.get(r['key'], 0)}
                 for r in CONFIG['roots']]
        total = by.get('total', [(None, 0)])[0][1]
        self._json({'models': models, 'folders': folders, 'mtypes': mtypes, 'roots': roots,
                    'total': total, 'root': ACTIVE['path']})

    def _filters(self, q, exclude=None, card_key=False, flags=None):
        """Build the shared JOIN + WHERE + params for search/ids from query args.
        `exclude` omits one faceted dimension, or several: a name from FACET_DIMS, or any
        collection of them. A facet list has to reflect the OTHER active filters without narrowing
        itself away, and /api/facets excludes all four at once because it re-adds them as per-row
        flags (see _dim_clause).
        `card_key=True` matches at CARD level instead of file level: any member matching brings the
        whole group. Passed by the grid and Select-all while something is collapsing; see the tail.
        This builder is called eight times across /api/search, /api/facets (once per dimension)
        and /api/ids, and a threaded argument is eight chances for one caller
        to disagree — i.e. for "Select all" to grab images the grid isn't showing. A param that
        arrives inside `q` cannot diverge, because all three endpoints already pass `q` straight
        through."""
        text = (q.get('q', [''])[0]).strip()
        meta = q.get('meta', [''])[0]
        where, params, joins = [], [], ''
        # One name or several: /api/facets excludes every faceted dimension at once, because it
        # carries all four as flags instead (see _dim_clause).
        excl = {exclude} if isinstance(exclude, str) else set(exclude or ())

        def dim(name):
            """Append one faceted dimension's clause, unless this caller excluded it."""
            if name in excl:
                return
            sql, dp = _dim_clause(q, name)
            if sql:
                where.append(sql)
                params.extend(dp)

        # Roots show/hide filter (merged DB). This is the whole "hide/show individual libraries"
        # mechanism, and it sits with the global exclusions rather than the user's own filters:
        # a library being switched off is library visibility, not a search term.
        dim('root')
        # Queued for recycle (see _pending_sql). Same reasoning as the roots filter above — a rule
        # that removes rows without a filter being switched on — but NOT peek-aware: hidden is a
        # state you can look past, pending is a row on its way out. It goes in this shared builder
        # for the reason in the docstring: /api/ids must agree with the grid, or Select-all hands
        # back an image you can't see. Adds nothing to the SQL while the queue is empty.
        _pend = _pending_sql('i')
        if _pend:
            where.append(_pend)
        # WHERE THE GLOBAL EXCLUSIONS END AND THE USER'S OWN FILTERS BEGIN. Everything above is a
        # rule that removes rows without a filter being switched on — library visibility, oversized,
        # the Hidden mark, rows queued for recycle. Everything below is something the user asked
        # for. The split exists for card-level matching; see the tail of this function.
        n_glob_w, n_glob_p = len(where), len(params)
        fts = to_fts(text)
        if fts:
            joins = 'JOIN images_fts f ON f.rowid = i.id'
            where.append('images_fts MATCH ?')
            params.append(fts)
        dim('model')
        dim('folder')
        dim('mfolder')
        if meta == 'missing':
            where.append('i.has_meta = 0')
        elif meta == 'ok':
            where.append('i.has_meta = 1')
        # Media type. Each option now names its OWN extensions. "Image" used to mean "not a video",
        # which was fine while those were the only two kinds — the day audio was indexed it silently
        # filed every song under Images, since a song is indeed not a video.
        #
        # FILE TYPE ASKS WHAT A CARD *IS*, NOT WHAT IT CONTAINS — the one filter of that kind, and
        # the distinction is why it needs its own handling instead of riding the card-level match
        # at the tail of this function. That rule admits a whole card when ANY member matches,
        # which is right for "does this run mention neon" and wrong here: a still+video pair
        # matched Images through its still and arrived wearing a ▶, and a song matched Images
        # through its cover art and arrived as a song card. Reported 2026-08-20 as
        # "selecting Images still shows Video cards"; see docs/notes/sets-and-curation.md.
        #
        # So while a kind is collapsing, the three options carve the library into three
        # NON-OVERLAPPING sets, decided by what the group holds rather than by the row:
        #   Videos — the group holds a video      (this has been true since pairs shipped)
        #   Songs  — the group holds an audio file
        #   Images — the group holds NEITHER
        mtype = q.get('type', [''])[0]
        vids = sorted(index_db.VIDEO_EXTS)
        auds = sorted(index_db.AUDIO_EXTS)
        # Both pair kinds collapse under the Video-pairs toggle (see the `grp` expression in
        # api_search: audio and video groups key on `group`, image sets on `sets`), so that is the
        # switch that decides whether a card can hold a kind other than its face's.
        pairs_on = q.get('group', [''])[0] == '1'
        if mtype == 'audio':
            where.append("i.ext IN (%s)" % ','.join('?' * len(auds)))
            params += auds
        elif mtype in ('video', 'image'):
            ph = ','.join('?' * len(vids))
            if mtype == 'image':
                imgs = sorted(index_db.IMAGE_EXTS)
                if pairs_on:
                    # An image whose group holds no video and no song. Without the second half a
                    # pair or a song is admitted through its picture member and then draws as the
                    # thing it actually is, because the card's ▶ and its playback come from the
                    # WHOLE group (grp_has_video_full / video_id) and not from the row that matched.
                    aph = ','.join('?' * len(auds))
                    where.append(
                        f"(i.ext IN ({','.join('?' * len(imgs))}) AND (i.group_id IS NULL OR "
                        f"i.group_id NOT IN (SELECT group_id FROM images "
                        f"WHERE group_id IS NOT NULL AND (ext IN ({ph}) OR ext IN ({aph})))))")
                    params += imgs + vids + auds
                else:
                    # Nothing is merged, so a card IS a file and the row's own extension is the
                    # whole answer — the still from a video run is its own image card again.
                    where.append("i.ext IN (%s)" % ','.join('?' * len(imgs)))
                    params += imgs
            elif pairs_on:
                # Keep merged pairs intact under the Videos filter: admit a video OR a still that
                # shares a group with a video, so the pair still collapses to one video card.
                where.append(f"(i.ext IN ({ph}) OR (i.group_id IS NOT NULL AND i.group_id IN "
                             f"(SELECT group_id FROM images WHERE ext IN ({ph}))))")
                params += vids + vids
            else:
                where.append(f"i.ext IN ({ph})")
                params += vids
        # tag filters (AND) and favorites
        for t in [x.strip().lower() for x in q.get('tags', [''])[0].split(',') if x.strip()]:
            where.append("EXISTS (SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.tag=? "
                         f"AND {ANY_TAG_SQL.replace('source', 'tg.source')})")
            params.append(t)
        if q.get('fav', [''])[0] == '1':
            where.append("EXISTS (SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.source='fav')")
        if q.get('note', [''])[0] == '1':      # "Has notes" — same emptiness test the ✎ card mark uses
            where.append("i.note IS NOT NULL AND TRIM(i.note) <> ''")
        # exclude: drop anything matching ANY excluded term (positive/model/filename)
        xfts = to_fts(q.get('x', [''])[0], ' OR ')
        if xfts:
            where.append('i.id NOT IN (SELECT rowid FROM images_fts WHERE images_fts MATCH ?)')
            params.append(xfts)
        # quality (Metric/pyiqa reward) range; float, only matches scored images
        def _flt(s):
            try:
                return float(s)
            except (TypeError, ValueError):
                return None
        rlo, rhi = _flt(q.get('rmin', [''])[0]), _flt(q.get('rmax', [''])[0])
        if rlo is not None or rhi is not None:
            cond = "EXISTS (SELECT 1 FROM quality qq WHERE qq.image_id=i.id AND qq.reward IS NOT NULL"
            if rlo is not None:
                cond += " AND qq.reward >= ?"
                params.append(rlo)
            if rhi is not None:
                cond += " AND qq.reward <= ?"
                params.append(rhi)
            where.append(cond + ")")
        # file-date (mtime) range — inclusive; client sends Unix-second bounds (local day edges)
        after, before = _flt(q.get('after', [''])[0]), _flt(q.get('before', [''])[0])
        if after is not None:
            where.append('i.mtime >= ?')
            params.append(after)
        if before is not None:
            where.append('i.mtime <= ?')
            params.append(before)
        # Did the USER narrow anything, as opposed to the always-on exclusions? The caller cannot
        # tell from the finished WHERE, and it decides whether two of the counts are the same
        # number. Set before the rewrite below, which appends a clause of its own.
        if flags is not None:
            flags['user'] = len(where) > n_glob_w
        if card_key and len(where) > n_glob_w:
            # A CARD IS A GROUP OF FILES, AND A FILTER PICKS FILES. So while something is
            # collapsing, a filter that only some members carried used to rebuild the card out of
            # the ones that matched: a pair arriving without its video (no ▶, no playback), a set
            # without its stages. The author's rule, 2026-08-19: *"sets are intended to be different
            # aspects of one generation, so metadata should match (where it can)"* — with
            # collapsing ON a filter matches the CARD, and any member matching brings all of it.
            # With collapsing off, files are what you are looking at and matching stays per-file.
            #
            # The Videos filter has done exactly this for its own case since pairs shipped (the
            # group_id clause above); this is the same move for every other filter, in one place.
            #
            # The GLOBAL exclusions stay on the outer row as well as inside, so a matching sibling
            # cannot drag an oversized or hidden file back into view.
            #
            # THE OBVIOUS CHEAPER SHAPE DOES NOT WORK. "This row matches, OR a sibling did"
            # would let the ordinary match run untouched and charge only the rejected rows — but it
            # puts the full-text MATCH inside an OR, and SQLite refuses ("unable to use function
            # MATCH in the requested context"). MATCH has to stay a top-level AND, which is exactly
            # what keeping it inside this subquery does. Measured at the same time: that shape was
            # also SLOWER on a model filter (649ms against 467ms), so nothing was given up.
            # Keyed on group_id rather than on the collapse key `grp`, which would need the whole
            # group-kind aggregate here: with one collapse toggle off, a group of that kind expands
            # together even though it shows as separate cards. Narrow, and the alternative is
            # dragging the grouping CTE into the filter builder.
            inner = ("SELECT COALESCE(i.group_id, 'i'||i.id) FROM images i %s WHERE %s"
                     % (joins, ' AND '.join(where)))
            params = params[:n_glob_p] + params
            where = where[:n_glob_w] + ["COALESCE(i.group_id, 'i'||i.id) IN (%s)" % inner]
            joins = ''          # matching already happened inside; the outer needs no FTS join
        wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
        return joins, wsql, params

    def api_ids(self, q):
        """All image ids matching the current filters (for 'select all')."""
        group_on = q.get('group', [''])[0] == '1'
        sets_on = q.get('sets', [''])[0] == '1'
        # Card-level matching too, or Select-all takes the members the filter happened to match
        # rather than the cards the grid is showing.
        joins, wsql, params = self._filters(q, card_key=(group_on or sets_on))
        conn = db()
        # Sets-only (one Sets kind collapsing) hides lone items in the grid, so Select-all must match:
        # return the MEMBERS of the visible sets, not every matching file. Mirror api_search's grp key
        # (collapse the active kind, everything else to its own singleton) and keep multi-member grps.
        if group_on != sets_on:
            # THE THREE BRANCHES BELOW ARE api_search's `grp` KEY, VERBATIM. It had two when it was
            # written, because a group was a video pair or an image set and there was nothing else;
            # songs arrived, api_search grew a third branch for them, and this copy did not. A song
            # group has no video, so this read it as an image set: Select-all under "Image sets only"
            # handed back a song and its cover, neither of them on screen — the exact failure the
            # sets-only rule exists to prevent, from the one path that still disagreed. Found
            # 2026-08-23 alongside the same blind spot in the grid.
            vlist = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
            alist = ','.join("'" + e + "'" for e in sorted(index_db.AUDIO_EXTS))
            song_pairs_on = False   # named for the mirror: sets-only never collapses song pairs
            sql = (
                f"WITH filtered AS (SELECT i.id, i.ext, i.group_id FROM images i {joins} {wsql}), "
                f"g AS (SELECT id, group_id, (CASE WHEN group_id IS NOT NULL THEN "
                f"MAX(CASE WHEN LOWER(ext) IN ({vlist}) THEN 1 ELSE 0 END) "
                f"OVER (PARTITION BY group_id) ELSE 0 END) AS hv, "
                f"(CASE WHEN group_id IS NOT NULL THEN "
                f"MAX(CASE WHEN LOWER(ext) IN ({alist}) THEN 1 ELSE 0 END) "
                f"OVER (PARTITION BY group_id) ELSE 0 END) AS ha FROM filtered), "
                f"k AS (SELECT id, CASE "
                f"WHEN group_id IS NOT NULL AND ha=1 AND {1 if song_pairs_on else 0}=1 THEN group_id "
                f"WHEN group_id IS NOT NULL AND hv=1 AND {1 if group_on else 0}=1 THEN group_id "
                f"WHEN group_id IS NOT NULL AND hv=0 AND ha=0 "
                f"AND {1 if sets_on else 0}=1 THEN group_id "
                f"ELSE 'i'||id END AS grp FROM g) "
                f"SELECT id FROM k WHERE grp IN (SELECT grp FROM k GROUP BY grp HAVING COUNT(*) > 1)")
        else:
            sql = f"SELECT i.id FROM images i {joins} {wsql}"
        ids = [r[0] for r in conn.execute(sql, params)]
        conn.close()
        self._json({'ids': ids, 'total': len(ids)})

    # Random sort. A SHUFFLE HAS TO BE STABLE FOR AS LONG AS YOU SCROLL: the grid pages with
    # LIMIT/OFFSET, so a plain ORDER BY RANDOM() would re-deal on every page and hand back
    # duplicates and silent gaps. So the order is a pure function of (shuffle_key, seed) — same seed,
    # same order, however many pages; new seed, a genuinely different deal.
    #
    # It multiplies a STORED random key rather than scrambling the row id arithmetically, which is
    # the obvious no-new-column trick and is wrong: (a*id + b) % p is affine, so the sorted result
    # walks the ids at a near-constant stride (every ~4th, every ~17th...). Consecutive ids here are
    # the same generation run, so that quietly returns one image per run in a fixed pattern — it
    # looks random and isn't. Starting from a key that is already random has no such structure.
    #
    # The seed is bounded and multiplied into a key bounded at ~1e9, so the product stays inside
    # int64; overflow would demote the compare to float and lose the low bits that carry the order.
    # `, i.id` is not a formality — two rows CAN draw the same key (~60k images against a 1e9 space
    # is a coin-flip birthday collision), and an unbroken tie is an unstable sort, which is exactly
    # the paging bug this exists to prevent.
    SHUFFLE_MOD = 2147483647        # 2^31-1, prime: coprime to every seed below, so the map is 1:1

    def _shuffle_expr(self, q, col='i.shuffle_key'):
        try:
            seed = int(q.get('seed', ['0'])[0])
        except (TypeError, ValueError):
            seed = 0
        seed = seed % 1000000 or 1          # bounded, never 0 (which would collapse every key to 0)
        # Inlined rather than bound as a parameter on purpose: this lands in ORDER BY, whose
        # placeholders would have to be threaded between the WHERE params and LIMIT/OFFSET. It is an
        # int() by construction, so there is nothing to inject.
        return f'(COALESCE({col}, 0) * {seed}) % {self.SHUFFLE_MOD}'

    def api_search(self, q):
        sort = q.get('sort', ['date'])[0]
        order = 'ASC' if q.get('order', ['desc'])[0].lower() == 'asc' else 'DESC'
        limit = max(1, min(500, int(q.get('limit', ['120'])[0])))
        offset = max(0, int(q.get('offset', ['0'])[0]))
        # Read BEFORE the filters are built: whether anything is collapsing decides whether a filter
        # matches a file or a whole card (see _filters' card_key).
        group_on = q.get('group', [''])[0] == '1'   # collapse still+video pairs
        sets_on = q.get('sets', [''])[0] == '1'     # collapse image sets (MAIN/DET/REFINE)
        collapse = group_on or sets_on
        _fflags = {}
        joins, wsql, params = self._filters(q, card_key=collapse, flags=_fflags)
        qjoin = 'LEFT JOIN quality qs ON qs.image_id = i.id'   # for the Quality badge + sort

        # 'sort' is never validated against the dropdown's option list — a stale snapshot or a
        # hand-typed URL can name anything — so every unknown value has to land somewhere sane.
        # It lands here, on date, which is also what the client's own sortOptionExists() fallback
        # rewrites a retired sort to. The two agree by construction rather than by list.
        sort_col = {'date': 'i.mtime'}.get(sort, 'i.mtime')
        if sort in ('reward-asc', 'reward-desc'):
            order_by = f"ORDER BY qs.reward IS NULL, qs.reward {'ASC' if sort == 'reward-asc' else 'DESC'}"
        elif sort == 'size-desc':
            order_by = 'ORDER BY i.size DESC'
        elif sort == 'random':
            order_by = f'ORDER BY {self._shuffle_expr(q)}, i.id'
        else:
            order_by = f'ORDER BY {sort_col} {order}'

        # "Sets only": when exactly ONE kind collapses (the Sets dropdown's "Video only" / "Images
        # only"), show only the SET cards of that kind and hide every lone item — a focused culling
        # view. Inferred from the collapse flags, so it needs no separate param. A collapsed card is
        # a real set when its group has >1 (filtered) member, i.e. group_count > 1 below.
        sets_only = group_on != sets_on
        # A song group rides the PAIRS flag when it collapses (see grp_cte: a song plus its
        # cover is one media file and one still, the shape of a still+video pair). But
        # "Video sets only" is a FILTER, not just a collapse, and a song is not a video set.
        # Left riding the flag, a song pair passed the >1-member cut on its own merits and
        # songs sat in a view that says video — reported by the author 2026-08-23. The conflict
        # rule painted on the two bars already tells you Songs are empty against both
        # sets-only modes; this is the query finally agreeing with it.
        song_pairs_on = group_on and not sets_only
        # Shared column list for both flat and collapsed queries.
        inner_cols = (
            "i.id, i.filename, i.folder, i.width, i.height, i.model_name, i.root_id, "
            "i.thumb, i.has_meta, i.mtime, i.ext, i.motion, i.group_id, i.size, i.duration, "
            # NB the song columns are deliberately NOT here — see _song_rows(). They were, and it
            # cost every library a measured 1.19x on every page, audio or not: `lyrics` alone runs
            # to kilobytes per row and this list is dragged through three window-function CTEs over
            # the whole table. Nothing in the query filters or orders by them, so they are fetched
            # for the ~60 rows that survive instead.
            "qs.reward AS reward, "
            # exclusive curation label slug (label:<slug> tag, 'label:' stripped), or NULL
            "(SELECT SUBSTR(tg.tag,7) FROM tags tg WHERE tg.image_id=i.id AND tg.source='user' "
            "AND tg.tag LIKE 'label:%' LIMIT 1) AS label, "
            "i.note AS note, (i.note IS NOT NULL AND i.note != '') AS has_note, "
            # Random's collapsed ORDER BY names this, so it has to survive into the CTE.
            "i.shuffle_key, i.set_stage, "
            "EXISTS(SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.source='fav') AS fav")
        base = f"SELECT {inner_cols} FROM images i {joins} {qjoin} {wsql}"
        # WHAT THE COLLAPSE ACTUALLY NEEDS. The collapsed query below decides two things — which
        # cards exist, and in what order — and it used to decide them while carrying the full
        # display list for every matching row in the library, then throw all but a page away at the
        # LIMIT. Measured on the author's library: a page of 30 cards cost 1306ms and a page of 50 cost
        # 1200ms, which is only possible if the work is not about the cards at all.
        #
        # These are the columns the decision reads: the group key (id, group_id, ext), the
        # representative pick (filename, set_stage, fav, reward, mtime, id) and every sort key
        # (mtime, size, reward, shuffle_key).
        #
        # What is NOT here is the point: folder, model_name, thumb, width/height, has_meta, motion,
        # root_id, the free-text `note`, and `label` — which is one of the three correlated
        # subqueries per row. They are fetched below, for the page that survived.
        # Same rule as the counts and the song columns before them: **anything that only matters
        # for the rows you got back does not belong in the query that scans everything.**
        lean_cols = ("i.id, i.filename, i.mtime, i.ext, i.group_id, i.size, qs.reward AS reward, "
                     "i.shuffle_key, i.set_stage, "
                     "EXISTS(SELECT 1 FROM tags tg WHERE tg.image_id=i.id AND tg.source='fav') AS fav")
        lean_names = ("id, filename, mtime, ext, group_id, size, reward, "
                      "shuffle_key, set_stage, fav, "
                      "grp_has_video, grp_has_audio, grp")
        # Carried out of the collapse to the page: what the card is (the four group facts) plus
        # every column the LIMIT's own ORDER BY needs. Deliberately NOT the display columns.
        carry_names = ("id, group_count, group_members, video_id, grp_has_audio, "
                       "mtime, size, reward, shuffle_key")

        # Collapse each group to one representative row. A group is a still+video PAIR if any
        # member is a video, else an image SET; each kind collapses only when its toggle is on,
        # so rows in an off-kind (or ungrouped) fall back to their own singleton 'i'||id group
        # and page normally. video_id is the paired video (sets have none, so is_set can tell
        # them apart client-side).
        # These fragments are built unconditionally — it is pure string work, and it lets the
        # card-count helper below express "count on the scale this view is using" once instead of
        # once per branch (the two COUNTs had already drifted into near-duplicates).
        vlist = ','.join("'" + e + "'" for e in sorted(index_db.VIDEO_EXTS))
        is_vid = f"(CASE WHEN LOWER(ext) IN ({vlist}) THEN 1 ELSE 0 END)"
        alist = ','.join("'" + e + "'" for e in sorted(index_db.AUDIO_EXTS))
        is_aud = f"(CASE WHEN LOWER(ext) IN ({alist}) THEN 1 ELSE 0 END)"
        # Per-group "has a video member", then the conditional collapse key (same in count + page).
        # grp_has_video is over the FILTERED rows (drives the collapse key); grp_has_video_full is
        # over the WHOLE group in the DB (ignores filters), so a still+video pair stays classified as
        # a pair — not a set — even when the Type=Images filter hides its video member (matches
        # api_image's set-vs-pair test, so the ▤ set badge and the detail view agree).
        # A SONG GROUP is a third kind, and it rides the pairs rule rather than the sets rule: a
        # cover and its song are one media file plus one still, exactly the shape of a still+video
        # pair, and nothing like a set of alternative takes. So it collapses with pairs, and turning
        # pairs off shows the cover as its own card again — the same escape hatch video already has.
        # Tested against the WHOLE group (like grp_has_video_full) so filtering the cover out with
        # File type = Songs doesn't turn a song pair into something else.
        # ONE AGGREGATE OVER THE GROUPED ROWS, not two windows over every row. The window form asked
        # all 100k rows "does YOUR group hold a video", which needs the whole set sorted by group_id
        # twice, to answer a question about the third of rows that are in a group at all. This is
        # the same `gg` decomposition the counts already use (see _gg below) — kept as one shape so
        # the count and the page can never disagree about what collapses.
        #
        # grp_has_video_full — over the WHOLE group in the DB rather than the filtered rows, so a
        # still+video pair stays classified as a pair even when Type=Images hides its video member —
        # is GONE from here. It is a correlated EXISTS against images per row, it exists only to
        # label a returned card, and it is now evaluated on the page that survived. See the page
        # query below.
        grp_cte = (
            f"gg AS (SELECT group_id, MAX({is_vid}) hv, MAX({is_aud}) ha FROM filtered "
            f"WHERE group_id IS NOT NULL GROUP BY group_id), "
            f"k AS (SELECT f.*, COALESCE(gg.hv, 0) AS grp_has_video, "
            f"COALESCE(gg.ha, 0) AS grp_has_audio, CASE "
            f"WHEN f.group_id IS NOT NULL AND COALESCE(gg.ha,0)=1 "
            f"AND {1 if song_pairs_on else 0}=1 THEN f.group_id "
            f"WHEN f.group_id IS NOT NULL AND COALESCE(gg.hv,0)=1 "
            f"AND {1 if group_on else 0}=1 THEN f.group_id "
            f"WHEN f.group_id IS NOT NULL AND COALESCE(gg.hv,0)=0 AND COALESCE(gg.ha,0)=0 "
            f"AND {1 if sets_on else 0}=1 THEN f.group_id "
            f"ELSE 'i'||f.id END AS grp "
            f"FROM filtered f LEFT JOIN gg ON gg.group_id = f.group_id)")
        # COUNTING CARDS IS A DIFFERENT QUERY FROM SHOWING THEM, and it was being served by the
        # showing one. A count needs exactly the three columns `grp` is derived from; it was
        # dragging the full ~20-column display list — including THREE correlated subqueries per row
        # (label, fav, hidden) — across the whole library to answer "how many". At 100k rows that is
        # ~300k subquery executions per count, and a refresh runs two of these.
        #
        # grp_has_video_full goes too: it is a correlated EXISTS against `images` for every row, and
        # `grp` is computed from grp_has_video / grp_has_audio and the toggles — never from it. It
        # exists only to classify a returned row as a set-or-pair for its badge.
        #
        # Both omissions are exact by construction: dropping columns nothing groups by, filters on
        # or orders by cannot move a COUNT. The joins STAY — `wsql` may filter on `qs.reward` — only
        # the SELECT list is slimmed. Pinned by test_count_cols.py, which runs the fat and lean
        # forms side by side over 21 filter/toggle combinations and asserts identical numbers.
        #
        # This is the same rule the song columns and the page split already follow: **anything that
        # only matters for the rows you got back does not belong in the query that scans everything.**
        count_cols = "i.id, i.ext, i.group_id"
        # ...and it does not need the window functions either, which is the larger half.
        #
        # The page query asks "what is THIS ROW's group key" and must, because it returns rows. A
        # COUNT doesn't: it decomposes, and the decomposition only touches the grouped rows —
        # a minority of any real library.
        #
        #     cards = rows in NO group                    (each is its own card)
        #           + groups that COLLAPSE                (one card each)
        #           + rows in groups that DON'T collapse  (each is its own card)
        #
        # The window form sorted the whole table twice (two MAX() OVER PARTITION BY) and then a
        # third time for GROUP BY grp — three sorts of 100k rows to answer a question about the
        # ~20k that are grouped. This is one GROUP BY over those. Measured on a 120k fixture:
        # 280ms -> 54ms for the matched count, 237ms -> 48ms for the held-back one, same answers.
        #
        # Sets-only counts only collapsing groups with more than one member, which is what the old
        # `HAVING COUNT(*) > 1` over `grp` meant: an uncollapsed row keys to its own id and can
        # never reach two.
        #
        # Same decomposition the shipped page fix used — "run the windows over grouped rows only" —
        # taken to its conclusion, since a count needs no windows at all.
        _collapses = (f"((ha=1 AND {1 if song_pairs_on else 0}=1) "
                      f"OR (hv=1 AND {1 if group_on else 0}=1) "
                      f"OR (hv=0 AND ha=0 AND {1 if sets_on else 0}=1))")
        _gg = (f"gg AS (SELECT group_id, MAX({is_vid}) hv, MAX({is_aud}) ha, COUNT(*) n "
               f"FROM filtered WHERE group_id IS NOT NULL GROUP BY group_id)")

        def _count_sql(inner, files=False):
            """SQL yielding one column `c` over `inner` (the body of the `filtered` CTE).
            files=True counts FILES rather than cards, which only differs under sets-only."""
            # MATERIALIZED for the same reason as the page query below: `filtered` is read by
            # the aggregate AND by the lone-row count, and SQLite re-ran it for each.
            if sets_only:
                agg = "COALESCE(SUM(n),0)" if files else "COUNT(*)"
                return (f"WITH filtered AS MATERIALIZED ({inner}), {_gg} "
                        f"SELECT {agg} c FROM gg WHERE {_collapses} AND n > 1")
            return (f"WITH filtered AS MATERIALIZED ({inner}), {_gg} "
                    f"SELECT (SELECT COUNT(*) FROM filtered WHERE group_id IS NULL) "
                    f"+ (SELECT COUNT(*) FROM gg WHERE {_collapses}) "
                    f"+ (SELECT COALESCE(SUM(n),0) FROM gg WHERE NOT {_collapses}) AS c")
        # Sets-only makes the counter's scale "sets": denominator = library sets, and the file
        # tally = files that live in those sets — so "42 of 210 sets · 620 files" reads straight.
        having = "HAVING COUNT(*) > 1" if sets_only else ""

        conn = db()
        # SCROLLING DOESN'T PAY FOR THE COUNTS. Every response used to carry five totals — the
        # library size in cards, the same in files, the matched count, and the two "held back"
        # numbers — and four of them scan the WHOLE library, not the page. Measured with the debug
        # trace on a real 120k library: ~815ms of server time per request, IDENTICAL on a page
        # returning 4 cards and one returning 30, so scrolling one folder cost ten sequential
        # ~1s stalls. None of those numbers can change while you scroll: only a filter, a sort or
        # an edit moves them, and every one of those comes through here with offset=0.
        # So a paging request answers with rows alone and the client keeps the totals it already has.
        want_counts = (offset == 0)
        total = root_total = root_files = None
        # Unfiltered "library" size for the counter's denominator: ignores the user's
        # filters/tags, but not which libraries are shown. Computed on the SAME scale as the
        # numerator: grouped CARDS when collapse is on (see below), raw files otherwise — mixing
        # scales made the denominator "jump" once image sets shipped.
        # These counts hit `images` directly (bare column names, no `i.` alias), so they apply the
        # roots show/hide filter themselves — the visible library size must track which roots are
        # shown. (A hide-large clause lived here too until 2026-09-08.)
        _dconds, hide_params = [], []
        _droots = [x for x in q.get('roots', [''])[0].split(',') if x]
        if _droots:
            _dconds.append("root_id IN (%s)" % ','.join('?' * len(_droots)))
            hide_params += _droots
        # ...and queued-for-recycle rows leave it too, or the denominator counts files the grid has
        # already stopped showing and "179 of 1,876" lies for the length of the undo window.
        _dpend = _pending_sql('images')
        if _dpend:
            _dconds.append(_dpend.replace('images.id', 'id'))   # bare `id`: no alias in this query
        hide_sql = ("WHERE " + " AND ".join(_dconds)) if _dconds else ""

        def _card_count(j, w, p, cx):
            """Rows matching a filter set, counted on the scale THIS view is using — grouped cards
            when something is collapsing, raw files otherwise."""
            if collapse:
                b = f"SELECT {count_cols} FROM images i {j} {qjoin} {w}"
                return cx.execute(_count_sql(b), p).fetchone()['c']
            return cx.execute(f"SELECT COUNT(*) c FROM images i {j} {w}", p).fetchone()['c']

        def _all_counts(cx):
            """Every whole-library number one response carries, on one connection.

            THESE RUN ALONGSIDE THE PAGE, NOT BEFORE IT. Counting and showing are independent
            questions over the same rows, and doing them in sequence meant no card could appear
            until both had finished — on a cold start that was 1421ms of counting stacked on top of
            2408ms of card-building, in one response, with nothing on screen for the total.
            Nothing here is needed to draw a card; it fills in the "179 of 1,876" line and the two
            held-back indicators.
            """
            out = {'root_total': None, 'root_files': None, 'total': None,
                   }
            with self._phase('counts'):
                if collapse:
                    _universe = f"SELECT id, ext, group_id FROM images {hide_sql}"
                    out['root_total'] = cx.execute(_count_sql(_universe), hide_params).fetchone()['c']
                    # Raw visible FILE count too: with sets/pairs merged, cards < files and the UI
                    # shows "11,968 (20,589 files)" so the library's true size stays visible.
                    if sets_only:
                        out['root_files'] = cx.execute(_count_sql(_universe, files=True),
                                                       hide_params).fetchone()['c']
                    else:
                        out['root_files'] = cx.execute(
                            f"SELECT COUNT(*) c FROM images {hide_sql}", hide_params).fetchone()['c']
                else:
                    out['root_total'] = cx.execute(
                        f"SELECT COUNT(*) c FROM images {hide_sql}", hide_params).fetchone()['c']
                    out['root_files'] = out['root_total']    # no merging: cards ARE files
                # TWO IDENTICAL GROUPED COUNTS when nothing is filtered. `root_total` counts the
                # browsable universe and `total` counts what this view matched, and with no user
                # filter set those are the same question — the only difference between the two
                # WHERE clauses is the filters that are not there. It is the default view, so this
                # is the boot case and the one that hurts: a grouped count is ~500ms cold on a 100k
                # library, and it was being paid twice for one number.
                out['total'] = (out['root_total'] if not _fflags.get('user')
                                else _card_count(joins, wsql, params, cx))
            return out

        # Off it goes, on its own connection — sqlite3 objects belong to the thread that made them.
        # A paging request carries no totals at all, so it starts nothing.
        _counts = {}
        _counts_err = []

        def _counts_worker():
            cx = db()
            try:
                _counts.update(_all_counts(cx))
            except Exception as e:                 # re-raised on the main thread below: a count
                _counts_err.append(e)              # that failed silently would show as a blank total
            finally:
                cx.close()

        _cthread = threading.Thread(target=_counts_worker) if want_counts else None
        if _cthread:
            _cthread.start()

        if collapse:
            # The card order, written twice against the same columns: once bare, for the LIMIT
            # inside the collapse, and once qualified `p.`, for the join that fetches the display
            # columns afterwards. The second one has to be qualified — the join brings `images`
            # back into scope, and a bare `mtime` there is ambiguous between the two.
            # ONE function rather than two strings: these must not drift, or the page you get and
            # the order you get it in would come from different sorts.
            def _g_order(px=''):
                if sort in ('reward-asc', 'reward-desc'):
                    return (f"ORDER BY {px}reward IS NULL, "
                            f"{px}reward {'ASC' if sort == 'reward-asc' else 'DESC'}")
                if sort == 'size-desc':
                    return f"ORDER BY {px}size DESC"
                if sort == 'random':
                    return f"ORDER BY {self._shuffle_expr(q, px + 'shuffle_key')}, {px}id"
                return f"ORDER BY {px}{ {'date': 'mtime'}.get(sort, 'mtime') } {order}"
            g_order, p_order = _g_order(), _g_order('p.')
            # Representative row — the member that fronts the card. Still over video (the face
            # carries the metadata), then the PIPELINE STAGE: the most finished image wins.
            # This used to order by fav, quality, then mtime, with a filename tiebreak whose comment
            # claimed it "lands a same-gen set on its highest role (…3 REFINE)". It never did:
            # filename sat AFTER mtime, and mtime is a float with sub-second precision, so it
            # essentially never ties and the filename rule was dead code. What actually chose the
            # face was "whichever member was written last" — or, once anything had a Quality score,
            # whichever scored — which is why sets were fronting their roughest member.
            # Favorite and quality are now tiebreaks WITHIN a stage, not overrides of it: an
            # automatic score must not silently change which image represents a set.
            # THE WINDOWS RUN OVER GROUPED ROWS ONLY. A row in no group falls into a partition of
            # one, where every one of these answers is already known — count 1, members itself, no
            # paired video, its own hidden mark, rank 1 — and in a real library most rows are like
            # that. Computing five window functions to conclude that a lone image is a lone image
            # was measured at 73% of the collapsed query's cost (bench_collapse.py), and the author felt
            # it as ~2s per scroll page on an unfiltered 100k view.
            #
            # So grouped rows keep the full treatment and ungrouped rows are unioned back with the
            # constants. Semantically identical — the set face, the hidden rule and the video pick
            # are untouched, which matters because that logic was expensive to settle.
            #
            # `grp <> 'i'||id` is the test for "actually collapsing": grp is the group_id only when
            # this group's KIND is being collapsed, and its own singleton key otherwise, so a row
            # whose pair is switched off correctly takes the cheap branch.
            #
            # In sets-only mode the union half is skipped entirely rather than filtered — a lone row
            # can never be a set, so `group_count > 1` would discard every one of them anyway.
            lone_half = "" if sets_only else (
                f"UNION ALL SELECT id, 1 AS group_count, CAST(id AS TEXT) AS "
                f"group_members, NULL AS video_id, grp_has_audio, "
                f"mtime, size, reward, shuffle_key "
                f"FROM k WHERE grp = 'i'||id ")
            sql = (
                # MATERIALIZED, and it is not a hint — it removes an entire second read of
                # the library. `filtered` is consumed twice (once to aggregate the group
                # facts, once to build the rows) and SQLite re-ran it both times: two full
                # table scans per page. Measured on a 120k fixture: default view 409 ->
                # 328ms, model filter 505 -> 270ms, keyword search 803 -> 460ms, same cards
                # in every case. Warm, that is the CPU saved; the reason it matters more
                # than that is cold, where the second scan was a second trip to the disk —
                # which is what a refresh after the app has been idle actually pays for.
                f"WITH filtered AS MATERIALIZED (SELECT {lean_cols} FROM images i {joins} {qjoin} {wsql}), "
                f"{grp_cte}, "
                f"ranked AS (SELECT {lean_names}, "
                f"COUNT(*) OVER (PARTITION BY grp) AS group_count, "
                f"GROUP_CONCAT(id) OVER (PARTITION BY grp) AS group_members, "
                # Is the CARD hidden? MIN = every member is marked, deliberately not MAX = any.
                # A partially-marked group is reachable (a rescan folds a new file into a group
                # whose others are hidden; a dupe-cull merges a mark onto a keeper elsewhere; the
                # user marks members individually with Sets off) and cannot be prevented on the
                # read side without a group-level aggregate, which /api/facets — which never
                # collapses — could not share. So such a card reads as NOT hidden and one click on
                # its mark hides the remainder: the drift converges instead of needing a repair pass.

                # Which video plays. A group can hold two videos — VHS writes a silent 'x_00001.mp4'
                # AND a muxed 'x_00001-audio.mp4'; play the -audio one so a run keeps its sound with
                # no hand-culling. Order videos first, -audio ahead of silent, then newest.
                f"FIRST_VALUE(CASE WHEN {is_vid}=1 THEN id END) OVER (PARTITION BY grp "
                f"ORDER BY {is_vid} DESC, "
                f"(CASE WHEN LOWER(filename) LIKE '%-audio.%' THEN 0 ELSE 1 END), id DESC "
                f"ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS video_id, "
                # THE SONG fronts a song pair, not its cover — the opposite of a still+video pair,
                # where the still fronts because the still carries the metadata. A no-op for every
                # other group (all rows score 0), so the still-first rule below is untouched.
                f"ROW_NUMBER() OVER (PARTITION BY grp ORDER BY "
                f"(CASE WHEN grp_has_audio=1 THEN 1 - {is_aud} ELSE 0 END) ASC, "
                f"{is_vid} ASC, "
                f"{SET_FACE_RANK_SQL} ASC, filename DESC, fav DESC, "
                f"reward IS NULL, reward DESC, mtime DESC, id DESC) AS rn "
                f"FROM k WHERE grp <> 'i'||id) "
                # The union is WRAPPED and the sort applied outside it. SQLite will not take an
                # expression in a compound SELECT's ORDER BY — only a plain output column — and
                # two of the six sorts are expressions (Random's shuffle and Quality's
                # `reward IS NULL` nulls-last). Sorting the union as a subquery costs
                # nothing extra (it has to be materialised to sort either way) and keeps every sort
                # written exactly once, instead of a special case per compound-safe rewrite.
                f", page AS (SELECT * FROM ("
                f"SELECT {carry_names} "
                f"FROM ranked WHERE rn=1 {'AND group_count > 1' if sets_only else ''} "
                f"{lone_half}"
                f") {g_order} LIMIT ? OFFSET ?) "
                # AND ONLY NOW THE DISPLAY COLUMNS. Everything above this line ran on the fourteen
                # columns needed to decide which cards exist and in what order; this fetches the
                # other twelve — folder, model, thumbnail, note, label and the rest — for the page
                # that survived, by joining each surviving id back to its row.
                #
                # grp_has_video_full comes with it. It is a correlated EXISTS against images, asked
                # once per row before and once per CARD now, and its only job is to tell a returned
                # card whether it is a set or a pair.
                f"SELECT {inner_cols}, p.group_count, p.group_members, p.video_id, "
                f"p.grp_has_audio, (CASE WHEN i.group_id IS NOT NULL AND EXISTS("
                f"SELECT 1 FROM images vv WHERE vv.group_id=i.group_id "
                f"AND LOWER(vv.ext) IN ({vlist})) THEN 1 ELSE 0 END) AS grp_has_video_full "
                f"FROM page p JOIN images i ON i.id = p.id {qjoin} {p_order}")
            with self._phase('page'):
                rows = conn.execute(sql, params + [limit, offset]).fetchall()
        else:
            with self._phase('page'):
                rows = conn.execute(f"{base} {order_by} LIMIT ? OFFSET ?",
                                    params + [limit, offset]).fetchall()
        # Collect the counting that has been running beside the page query. `wait` is the part of
        # it that did NOT overlap — on a warm library it is near zero, and a big number there says
        # the counting outlasted the cards rather than hiding behind them.
        root_total = root_files = total = None
        if _cthread:
            with self._phase('countwait'):
                _cthread.join()
            if _counts_err:
                raise _counts_err[0]
            root_total, root_files = _counts['root_total'], _counts['root_files']
            total = _counts['total']
        # Only when this page actually holds a song — a library with no audio pays for neither of
        # these, which is the whole point of keeping the song columns out of the grid query.
        song_ids = [r['id'] for r in rows if (r['ext'] or '').lower() in index_db.AUDIO_EXTS]
        with self._phase('songs'):
            song_rows = _song_rows(conn, song_ids) if song_ids else {}
            generic_names = _generic_song_names(conn) if song_ids else set()
        # A PAIR CARD IS FRONTED BY THE STILL, and a still has no length -- so the one kind of card
        # most likely to want a duration was the one that never showed one. The video's row is the
        # one that knows, so ask for it: page-scoped, ~one id per pair, the same shape as the song
        # lookup above and for the same reason. `video_id == id` is a LONE video reporting itself,
        # which already has its own duration and needs nothing.
        pair_vids = [r['video_id'] for r in rows
                     if collapse and r['video_id'] and r['video_id'] != r['id']]
        # Keyed by the STILL that fronts the card, so the item literal below is a plain lookup on
        # the row it is already holding rather than a second walk of the pairing rule.
        pair_face = {}
        if pair_vids:
            q = ','.join('?' * len(pair_vids))
            # A DICT, NOT A TUPLE, because this list has already grown twice and the same
            # widening in index_db's scan silently broke an unpack somewhere else in the file.
            vid_rows = {row['id']: {'duration': row['duration'], 'width': row['width'],
                                    'height': row['height'], 'size': row['size']} for row in
                        conn.execute(f"SELECT id, duration, width, height, size FROM images "
                                     f"WHERE id IN ({q})", pair_vids)}
            for r in rows:
                if collapse and r['video_id'] and r['video_id'] != r['id']:
                    got = vid_rows.get(r['video_id'])
                    if got:
                        pair_face[r['id']] = got
        conn.close()

        def _vf(row, field):
            """One field of the VIDEO behind a pair card, or None when this row isn't a pair."""
            return (pair_face.get(row['id']) or {}).get(field)

        items = [{
            'id': r['id'], 'filename': r['filename'], 'folder': r['folder'],
            # EVERY FACT ABOUT THE FILE DESCRIBES THE VIDEO, NOT THE STILL IT IS FRONTED BY.
            # The author, 2026-08-25, first on the dimensions -- "it is the video that is most important.
            # It tells me what the relative quality is" -- and then on the file size, which was the
            # same fault one column over. A pair is usually an img2video run, so the still is the
            # SOURCE: its size, its resolution and its (absent) length all describe the input, and
            # the card was answering a question nobody asked.
            #
            # These OVERRIDE, where the duration below only fills a blank -- the still HAS a
            # resolution and a file size of its own, and they are the wrong ones. Each falls back
            # to the still's only while the video's is still unknown, which stops being possible
            # once a rescan has re-read it (see index_db's skip check).
            'width': _vf(r, 'width') or r['width'],
            'height': _vf(r, 'height') or r['height'],
            'model': r['model_name'],
            # The card's small print. `folder`, `width`, `height` and `model_name` were already
            # here and unused by the grid; these three are the additions. All four of the columns
            # behind them were ALREADY selected -- mtime to build the thumb URL below, size as a
            # sort key -- so only `i.duration` joined inner_cols, and inner_cols is the list that
            # runs on the page rather than the library. Nothing here touches lean_cols.
            #
            # `duration` is also set by _song_fields() below, which is spread in AFTER this literal,
            # so a song keeps its decoded length and a video gets its container's. Same key, two
            # sources, and the more accurate one wins by ordering -- not by accident.
            'size': _vf(r, 'size') or r['size'], 'mtime': r['mtime'],
            # The row's own length, or -- on a pair, where the row is the still -- its video's.
            'duration': (r['duration'] if r['duration'] is not None
                         else _vf(r, 'duration')),
            'has_meta': r['has_meta'], 'fav': r['fav'], 'reward': r['reward'],
            'label': r['label'], 'has_note': r['has_note'], 'note': r['note'],
            'motion': r['motion'], 'is_video': (r['ext'] or '').lower() in index_db.VIDEO_EXTS,
            'group_count': (r['group_count'] if collapse else 1),
            'video_id': (r['video_id'] if collapse else None),
            'group_members': (r['group_members'] if collapse else None),
            # A collapsed group with >1 member and no video member ANYWHERE in the group is an image
            # SET (vs a pair). Uses grp_has_video_full (whole-group, filter-independent) rather than the
            # filtered video_id, so hiding the video via Type=Images doesn't make a pair look like a set.
            # ...and a song pair is not a set either: it holds one song and its artwork, not
            # alternative takes, so it gets no stacked-cards badge.
            'is_set': bool(collapse and r['group_count'] and r['group_count'] > 1
                           and not r['grp_has_video_full']
                           and not (collapse and r['grp_has_audio'])),
            # Which library this row belongs to, so the client can mark cards whose library is
            # offline (thumbs are cached, so they still render — but the file itself is unreachable).
            'root_id': r['root_id'],
            # r= is derived per-row from the image's own root (ids are globally unique in the merged
            # DB, so the server resolves by id alone; r= just keeps the browser cache honest).
            'thumb_url': f'/thumb/{r["id"]}?v={int(r["mtime"] or 0)}&r={r["root_id"] or ""}&s={thumbs_mod.THUMB_SIZE}',
            # Audio: everything the card draws instead of a picture. Sent only for songs, so an
            # ordinary image's row is unchanged in size. The full lyrics stay on the server — the
            # card needs one line of them, and a page of 200 would otherwise carry half a megabyte
            # of verses nothing displays.
            **(dict(_song_fields(song_rows[r['id']], generic_names),
                    cover_url=_cover_url(song_rows[r['id']],
                                         song_rows[r['id']]['cover_id'] if collapse else None))
               if r['id'] in song_rows else {}),
        } for r in rows]
        # A paging response OMITS the totals rather than sending nulls, so the client's "did this
        # response carry a count?" test is `in`, not a truthiness check — 0 is a real total.
        out = {'offset': offset, 'limit': limit, 'items': items}
        if want_counts:
            out.update(total=total, root_total=root_total, root_files=root_files)
        self._json(out)

    def api_image(self, iid):
        conn = db()
        r = conn.execute("SELECT * FROM images WHERE id=?", (iid,)).fetchone()
        if not r:
            conn.close()
            return self._json({'error': 'not found'}, 404)
        r = _heal_stale_row(conn, r)
        d = dict(r)
        # The seed goes out as a STRING. SQLite holds it as a 64-bit integer correctly, but JSON
        # numbers land in JavaScript as doubles, which are only exact to 2^53 — so a real ComfyUI
        # seed like 1234567890123456789 arrives as ...456800. Silently. A seed is copied in order to
        # reproduce a generation, so a value that is *nearly* right is worse than none at all: it
        # looks usable and cannot work. Every other setting is small enough not to care.
        # gp_seed_s carries the rare seed too large for SQLite's INTEGER -- at most one of the
        # two columns is ever set, so this collapses them back into the one field the client knows.
        seed = d.pop('gp_seed_s', None)
        if seed is None and d.get('gp_seed') is not None:
            seed = str(d['gp_seed'])
        d['gp_seed'] = seed
        try:
            d['loras'] = json.loads(d.get('loras') or '[]')
        except Exception:
            d['loras'] = []
        d['tags'] = [row['tag'] for row in conn.execute(
            "SELECT tag FROM tags WHERE image_id=? AND source='user' ORDER BY tag COLLATE NOCASE", (iid,))]
        d['label'] = next((t[6:] for t in d['tags'] if t.startswith('label:')), None)   # exclusive curation label
        # Machine tags ride in their own field rather than joining d['tags']: that list carries the
        # label row and is what the tag editor writes back, so mixing in rows the user did not make
        # would put them one careless save away from becoming his.
        d['ext_tags'] = [{'tag': r['tag'], 'by': r['source'][4:], 'score': r['score']}
                         for r in conn.execute(
                             f"SELECT tag, source, score FROM tags WHERE image_id=? AND "
                             f"{MACHINE_TAG_SQL} ORDER BY score DESC, tag COLLATE NOCASE", (iid,))]
        d['favorite'] = conn.execute(
            "SELECT 1 FROM tags WHERE image_id=? AND source='fav'", (iid,)).fetchone() is not None
        qr = conn.execute("SELECT reward FROM quality WHERE image_id=?", (iid,)).fetchone()
        d['quality'] = ({'reward': qr['reward']} if qr else None)
        # Sibling ids of a group, so a detail-view recycle can take the whole group. For an image
        # SET (a group with no video member) also return per-member role info, in ordinal order, so
        # the detail view can render the side-by-side cull panes.
        gid = d.get('group_id')
        rkey = d.get('root_id') or ''                          # per-item root, not the active one
        d['root_key'] = d.get('root_id')
        d['root_name'] = (_root_by_key(rkey) or {}).get('name') if rkey else None
        if gid:
            # LEFT JOIN, not a second query per member: a set is two to four rows and the score
            # is shown on every pane, so fetching it here costs nothing and removes a round trip
            # the set view would otherwise make for each pane after a scoring run.
            members = conn.execute(
                "SELECT i.id, i.filename, i.ext, i.mtime, i.set_stage, i.width, i.height, "
                "i.size, i.duration, q.reward AS reward "
                "FROM images i LEFT JOIN quality q ON q.image_id = i.id WHERE i.group_id=?",
                (gid,)).fetchall()
            d['group_members'] = [m['id'] for m in members]
            has_video = any((m['ext'] or '').lower() in index_db.VIDEO_EXTS for m in members)
            if not has_video and len(members) > 1:
                d['group_kind'] = 'set'
                # Role = the saver's stamped stage when present, else the legacy filename role.
                d['set_members'] = sorted(
                    ({'id': m['id'], 'filename': m['filename'],
                      'role': (m['set_stage'] or index_db._set_role(m['filename'])),
                      # Shown under each pane: culling is a comparison, and which one is the upscale
                      # is a fact about the file, not something to infer from looking at it.
                      'width': m['width'], 'height': m['height'], 'size': m['size'],
                      # A SCORE PER PANE. Culling a set is a comparison, and the whole reason to
                      # score one is to have a number to compare -- one member's score against
                      # nothing is not a comparison. None where the member has not been scored.
                      'reward': m['reward'],
                      'file_url': _file_url(m['id'], m['mtime'], rkey, m['filename'])}
                     for m in members),
                    # Pipeline order (Raw->Final / MAIN->REFINE), then filename. Filename alone no
                    # longer works: the old roles carried a 1/2/3 ordinal, the new ones don't.
                    key=lambda m: (index_db._stage_rank(m['role']), m['filename']))
            else:
                d['group_kind'] = 'pair'
                # Same reason as the grid's pair cards: this record is the STILL, and the detail
                # view PLAYS THE VIDEO -- so a still's length (none) and a still's dimensions (the
                # img2video source's, not the output's) are both the wrong answer about what is on
                # screen. The video member is the one that knows.
                vids = [m for m in members if (m['ext'] or '').lower() in index_db.VIDEO_EXTS]
                # Each fact asks for the first member that HAS it, rather than trusting one member
                # to have both: a run can hold more than one video (VHS writes a silent file and an
                # `-audio` one), and a member missing a duration must not veto another's size.
                if d.get('duration') is None:
                    d['duration'] = next((m['duration'] for m in vids
                                          if m['duration'] is not None), None)
                # Overrides, where duration only fills a blank -- see the grid's note.
                wh = next(((m['width'], m['height']) for m in vids
                           if m['width'] and m['height']), None)
                if wh:
                    d['width'], d['height'] = wh
                sz = next((m['size'] for m in vids if m['size']), None)
                if sz:
                    d['size'] = sz
                # THE REST OF THE RUN. A video group is fronted by ONE still — the representative
                # whose record this is — and until now the others were fetched only as bare ids, for
                # recycling and selection. So a run that made three frames and animated them looked
                # exactly like a lone video (BR-11, raised by the author 2026-08-07). The detail view steps
                # through these in the drag-source box.
                #
                # Sent only when there is more than one, so an ordinary still+video pair and a song
                # and its cover carry nothing new and render exactly as before. Videos and audio are
                # excluded: the video is already playing in the big pane, and a song's cover is that
                # song's own face, not a sibling frame.
                stills = [m for m in members
                          if (m['ext'] or '').lower() not in index_db.VIDEO_EXTS
                          and (m['ext'] or '').lower() not in index_db.AUDIO_EXTS]
                if len(stills) > 1:
                    d['pair_stills'] = [
                        {'id': m['id'], 'filename': m['filename'],
                         'width': m['width'], 'height': m['height'],
                         # The drag carries the FILE, and its name comes from this URL — see
                         # test_drag_name.py. Stepping must therefore swap the whole url, never
                         # just the id, or ComfyUI receives the right bytes under a stale name.
                         'file_url': _file_url(m['id'], m['mtime'], rkey, m['filename'])}
                        for m in sorted(stills, key=lambda m: m['filename'])]
        else:
            d['group_members'] = [iid]
            d['group_kind'] = None
        d['is_audio'] = (d.get('ext') or '').lower() in index_db.AUDIO_EXTS
        if d['is_audio']:
            # The detail view gets the full lyrics (the grid deliberately doesn't), and the same
            # headline the card showed — computed here rather than sent up from the client, so the
            # title can never differ between a card and the view it opens.
            d.update(_song_fields(r, _generic_song_names(conn)))
            d['lyrics'] = r['lyrics']
            # The cover, found the same two ways the grid finds it — embedded first, then a still
            # sharing this song's group (which the run code already put there).
            cov = None
            if not r['has_cover'] and gid:
                # No `thumb IS NOT NULL` here: thumbnails are made lazily on first request, so a
                # cover nobody has looked at yet has none, and testing for one would hide exactly
                # the covers that have never been shown. /thumb/<id> generates on demand.
                cov = conn.execute(
                    "SELECT id FROM images WHERE group_id=? AND LOWER(ext) IN (%s) "
                    "ORDER BY id LIMIT 1"
                    % ','.join('?' * len(index_db.IMAGE_EXTS)),
                    (gid, *sorted(index_db.IMAGE_EXTS))).fetchone()
            d['cover_url'] = _cover_url(r, cov['id'] if cov else None)
        conn.close()
        v = int(d.get('mtime') or 0)
        d['thumb_url'] = f'/thumb/{iid}?v={v}&r={rkey}&s={thumbs_mod.THUMB_SIZE}'
        d['file_url'] = _file_url(iid, v, rkey, d.get('filename'))
        d['is_video'] = (d.get('ext') or '').lower() in index_db.VIDEO_EXTS
        # Whether Send to ComfyUI can do anything with this file. A property of the FILE, so the
        # button simply doesn't exist for one that never carried a graph (the author's call) — as opposed
        # to ComfyUI being down, which is temporary and gets a dimmed button instead.
        d['has_workflow'] = bool(workflow_json(d.get('path') or ''))
        self._json(d)

    def api_comfy_open(self):
        """Open this file's workflow on ComfyUI's canvas. Nothing is queued and nothing is saved —
        it stops exactly where a drag-and-drop stops."""
        b = self._read_json()
        iid = b.get('id')
        r = self._path_for(iid) if iid is not None else None
        if not r:
            return self._json({'error': 'not found'}, 404)
        wf = workflow_json(r['path'])
        if not wf:
            return self._json({'error': 'this file carries no workflow'}, 400)
        payload = json.dumps({'workflow': wf, 'name': os.path.basename(r['path'])}).encode('utf-8')
        req = urllib.request.Request(COMFY_URL + '/vv_bridge/open', data=payload, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=COMFY_TIMEOUT) as resp:
                if resp.status != 200:
                    return self._json({'error': f'ComfyUI answered {resp.status}'}, 502)
        except Exception as e:
            # Named separately from "no workflow" because the fixes are different: one means start
            # ComfyUI (or install the bridge), the other means this file never had a graph.
            return self._json({'error': f"couldn't reach ComfyUI on {COMFY_URL} ({e})"}, 502)
        return self._json({'ok': True})

    def _path_for(self, iid):
        conn = db()
        r = conn.execute("SELECT path, thumb FROM images WHERE id=?", (iid,)).fetchone()
        conn.close()
        return r

    # The sizes a thumbnail may be asked for — the grid's four card sizes. An allowlist, not a
    # free integer: `s` comes off the URL, and it names a directory under data/thumbs.
    THUMB_SIZES = (128, 192, 256, 512)

    def serve_thumb(self, iid, q=None):
        """Serve the cached thumbnail at the size the CALLER asked for (?s=), not one fixed size.

        Why this is worth a parameter. Every card used to download the 512px thumbnail whatever
        size it was drawn at, because 512 matches the XL card and S/M/L just downscaled it. That
        is 22x more image than an S card can show — measured at ~116KB versus ~5KB, so a first
        screenful of 117 cards was ~13MB rather than ~0.6MB. The bytes were never the visible
        cost on their own; what they did was saturate the browser's ~6 connections, so the NEXT
        PAGE OF CARDS queued behind pictures nobody could see the detail of anyway. A trace of a
        907-image folder showed 43 thumbnails still in flight after 1.2s and the whole screenful
        taking 5.3s, with page fetches stuck 300-1400ms behind them.

        The cache was always ready for this: thumb_rel() puts the size in the top folder, so each
        size lands in its own subtree and they coexist. Only the wiring was missing.
        """
        r = self._path_for(iid)
        if not r:
            return self._json({'error': 'not found'}, 404)
        try:
            size = int(((q or {}).get('s') or [thumbs_mod.THUMB_SIZE])[0])
        except (TypeError, ValueError):
            size = thumbs_mod.THUMB_SIZE
        if size not in self.THUMB_SIZES:
            size = thumbs_mod.THUMB_SIZE
        # The `thumb` column records the DEFAULT size only, so it stays a single value with a
        # single meaning. Any other size is resolved from the path, which thumb_rel derives
        # deterministically — no schema change to hold four paths per image.
        rel = r['thumb'] if size == thumbs_mod.THUMB_SIZE else None
        # Stored thumb paths are size-tagged (see thumb_rel); ignore one left from a previous
        # THUMB_SIZE so it regenerates at the current size instead of serving the stale one.
        if rel and not rel.startswith(f'{size}/'):
            rel = None
        if not rel:
            rel, src_size = thumbs_mod.ensure_thumb_sized(r['path'], THUMBS_DIR, size=size)
            # Videos skip dimension probing at scan time; backfill from the poster frame now.
            # The dimensions come from the SOURCE, so they are worth recording whichever size was
            # generated; the thumb path is only stored when it is the one that column describes.
            if rel and src_size and os.path.splitext(r['path'])[1].lower() in index_db.VIDEO_EXTS:
                try:
                    conn = db()
                    if size == thumbs_mod.THUMB_SIZE:
                        conn.execute("UPDATE images SET width=?, height=?, thumb=? WHERE id=?",
                                     (src_size[0], src_size[1], rel, iid))
                    else:
                        conn.execute("UPDATE images SET width=?, height=? WHERE id=?",
                                     (src_size[0], src_size[1], iid))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
        if rel:
            fp = os.path.join(THUMBS_DIR, rel)
            if os.path.exists(fp):
                return self._file(fp, 'image/webp', cache=True)
        # fall back to the original if a thumb can't be made
        return self.serve_file(iid)

    def serve_file(self, iid):
        r = self._path_for(iid)
        if not r:
            return self._json({'error': 'not found'}, 404)
        ext = os.path.splitext(r['path'])[1].lower()
        ctype = MEDIA_CTYPES.get(ext, 'application/octet-stream')
        # The original's own name, so a drag into ComfyUI lands as VV_00123_krea.png rather than
        # the row number. Cosmetic here, load-bearing there — see _serve_range.
        self._serve_range(r['path'], ctype, name=os.path.basename(r['path']))

    def serve_drag_png(self, iid):
        """A VIDEO, as a picture ComfyUI will accept from a drag: one frame, carrying the video's
        own `workflow` and `prompt` graphs as PNG text chunks.

        WHY THIS EXISTS, because the obvious approach was tried first and does not work. A drag hands
        the drop target a FILE, and the only file a browser will carry between windows is one the
        browser itself owns — i.e. the bytes behind an <img>. Attaching a file built in the page
        works within that page and arrives EMPTY in another window, which is what ComfyUI is. So a
        video cannot be dragged out directly at all; the fix is to make the thing being dragged a
        real picture, which is the route that has always worked.

        Nothing here reaches the user's library: the frame is decoded to memory and cached under
        data/. The picture is incidental — ComfyUI reads the graph out of the chunks and never looks
        at the pixels — but it is a true first frame rather than a placeholder, so dropping it onto a
        Load Image node gives something honest instead of a lie.
        """
        r = self._path_for(iid)
        if not r:
            return self._json({'error': 'not found'}, 404)
        path = r['path']
        ext = os.path.splitext(path)[1].lower()
        if ext not in index_db.VIDEO_EXTS and ext not in index_db.AUDIO_EXTS:
            return self.serve_file(iid)          # a picture is already draggable as itself
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            return self._json({'error': 'not found'}, 404)
        key = hashlib.sha1(f'{os.path.abspath(path)}|{mtime}'.encode('utf-8')).hexdigest()
        cache = os.path.join(DRAGPNG_DIR, key[:2], key + '.png')
        if not (os.path.exists(cache) and os.path.getsize(cache) > 0):
            try:
                data = (_build_song_drag_png(path) if ext in index_db.AUDIO_EXTS
                        else _build_drag_png(path))
            except Exception as e:
                _log_exc('drag png')
                return self._json({'error': f'could not read a frame: {e}'}, 500)
            if not data:
                return self._json({'error': 'no frame could be decoded'}, 500)
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                tmp = cache + '.part'             # never leave a half-written file in the cache
                with open(tmp, 'wb') as f:
                    f.write(data)
                os.replace(tmp, cache)
            except OSError:
                pass                             # cache is an optimisation; serve the bytes anyway
            stem = os.path.splitext(os.path.basename(path))[0]
            return self._bytes(data, 'image/png', cache=True,
                               disposition=f'inline; filename="{_safe_filename(stem + ".png")}"')
        stem = os.path.splitext(os.path.basename(path))[0]
        self._serve_range(cache, 'image/png', name=stem + '.png')

    def export_civitai(self, iid):
        """Download a PNG copy carrying an A1111 `parameters` chunk Civitai can parse (its ComfyUI
        parser fails on real graphs). Translates the embedded workflow to A1111 text, strips Comfy's
        own chunks, and returns the spliced copy as an attachment. PNG images only."""
        conn = db()
        row = conn.execute("SELECT path, width, height, root_id FROM images WHERE id=?", (iid,)).fetchone()
        conn.close()
        if not row:
            return self._json({'error': 'not found'}, 404)
        path = row['path']
        if os.path.splitext(path)[1].lower() != '.png':
            return self._json({'error': 'Export supports PNG images only'}, 400)
        # Optional resource auto-linking: resolve the graph's checkpoint/LoRA names to files under
        # this image's LIBRARY models folder (each library = the system that made it), falling back
        # to the global default. The app reads those folders here at runtime; hashes are cached in
        # data/ so a big checkpoint is read once. Missing/unreachable folders just yield no hash.
        dirs = _models_dirs_for(row['root_id'])
        resolver = None
        if dirs:
            cache_path = os.path.join(DATA_DIR, 'model_hashes.db')
            saver = model_hash.saver_caches(dirs)     # hashes the saver node already computed
            resolver = lambda category, raw: model_hash.autov2_multi(dirs, category, raw,
                                                                     cache_path, saver)
        try:
            text = comfy_meta.build_civitai_parameters(path, row['width'], row['height'],
                                                       hash_resolver=resolver)
        except Exception as e:
            return self._json({'error': f'metadata read failed: {e}'}, 500)
        if not text:
            return self._json({'error': 'No ComfyUI metadata found in this image'}, 400)
        try:
            data = comfy_meta.splice_parameters_png(path, text)
        except Exception as e:
            return self._json({'error': f'export failed: {e}'}, 500)
        stem = os.path.splitext(os.path.basename(path))[0]
        safe = re.sub(r'[^A-Za-z0-9 _.\-]', '_', stem).strip() or 'image'
        self._bytes(data, 'image/png',
                    disposition=f'attachment; filename="{safe}.civitai.png"')

    def civitai_text(self, iid, q):
        """Return a labeled, one-value-per-row metadata block as JSON for copy-to-clipboard (Civitai
        can't parse metadata from uploaded video, so a video post is filled in by hand — this is the
        text to paste into its fields). Reads generation settings live from the PNG graph; no
        resource hashing (hashes only help Civitai's embedded-metadata parser, useless for a manual
        paste, so we skip the models folder and stay instant). `?video=1` drops the settings block
        (sampler/CFG/seed/size don't matter for video). Falls back to the DB's prompt/model/LoRAs for
        images with no usable graph. Always returns `{text}` (empty string when nothing to copy)."""
        include_params = q.get('video', ['0'])[0] not in ('1', 'true')
        conn = db()
        row = conn.execute("SELECT positive, negative, model_name, loras, path, width, height "
                           "FROM images WHERE id=?", (iid,)).fetchone()
        conn.close()
        if not row:
            return self._json({'error': 'not found'}, 404)
        try:
            text = comfy_meta.build_readable_parameters(row['path'], row['width'], row['height'],
                                                        include_params=include_params)
        except Exception:
            text = None
        if not text:                                # no graph (non-PNG / "no meta"): build from the DB
            loras = []
            try:
                loras = json.loads(row['loras'] or '[]')
            except Exception:
                loras = []
            meta = {'positive': row['positive'] or '', 'negative': row['negative'] or '',
                    'model_name': row['model_name'] or '', 'loras': loras}
            if meta['positive'] or meta['model_name']:
                text = comfy_meta._format_readable(meta, {}, row['width'], row['height'],
                                                   loras, include_params)
        return self._json({'text': text or ''})

    def api_export_prepare(self):
        """Kick off (or skip) the slow part of an export: hashing this image's model/LoRA files.
        Returns {ready:true} when there's nothing to hash (no models folder, or all cached) so the
        client downloads immediately; otherwise starts a background job the client polls."""
        body = self._read_json()
        iid = body.get('id')
        conn = db()
        row = conn.execute("SELECT path, root_id FROM images WHERE id=?", (iid,)).fetchone()
        conn.close()
        if not row:
            return self._json({'error': 'not found'}, 404)
        if os.path.splitext(row['path'])[1].lower() != '.png':
            return self._json({'error': 'Export supports PNG images only'}, 400)
        dirs = _models_dirs_for(row['root_id'])
        cache_path = os.path.join(DATA_DIR, 'model_hashes.db')
        # The saver node already hashed these models when it wrote the image — consulting its cache
        # is what turns a 20+GB read into an instant answer.
        saver = model_hash.saver_caches(dirs)
        todo = []                                        # (name, path, size) — uncached resources
        if dirs:
            for cat, raw in comfy_meta.list_resources(row['path']):
                fp = model_hash.resolve_file_multi(dirs, cat, raw)
                if fp and not model_hash.cached_hash(fp, cache_path, saver):
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    todo.append((os.path.basename(fp), fp, sz))
        if not todo:
            return self._json({'ready': True})           # nothing to hash -> download now
        if _export_state['running']:
            return self._json({'error': 'An export is already preparing'}, 409)
        run_export_prepare(iid, todo)
        return self._json({'ready': False, 'started': True})

    def api_export_status(self):
        self._json(_progress_snapshot(_export_state))

    def api_export_stop(self):
        _export_state['cancel'] = True
        self._json({'ok': True})

    def reveal_recycle(self):
        """Open a library's `_ToRecycle` folder in Explorer.

        THE FOLDER IS WRITE-ONLY UNTIL NOW. On a network share Windows has no Recycle Bin, so a
        recycle moves the file here instead (see _move_to_recycle_folder) -- and nothing has ever
        mentioned it again: nothing counts it, surfaces it or empties it. The author, asking for this:
        "hidden debt that I currently have no good way to manage."

        IT OPENS THE FOLDER AND STOPS THERE, deliberately. Emptying one is a PERMANENT delete --
        there is no bin behind a share to catch it -- and it would be the only irreversible action
        in the app. The author's call, and the same line already drawn for the unused-models report:
        the app reports, and acting on files is a file manager's job.

        MISSING IS NORMAL, NOT AN ERROR. A local library recycles to the OS bin, so this folder is
        never created there at all. That answers `ok: False` with a reason for the caller to say
        out loud, rather than a 404 the client would have to translate.
        """
        b = self._read_json()
        r = _root_by_key((b or {}).get('key')) or _root_by_key(ACTIVE['key'])
        if not r:
            return self._json({'error': 'unknown library'}, 404)
        # Joined exactly the way _move_to_recycle_folder joins it, off the same constant, so the
        # reader and the writer cannot drift apart.
        folder = os.path.join(os.path.abspath(r['path']), RECYCLE_DIRNAME)
        if not os.path.isdir(folder):
            return self._json({'ok': False, 'reason': 'none', 'path': folder})
        try:
            subprocess.Popen(['explorer', os.path.normpath(folder)])
        except Exception as e:
            return self._json({'error': str(e)}, 500)
        self._json({'ok': True, 'path': folder})

    def api_recycle_status(self):
        """Per-library `_ToRecycle` counts, and the thresholds worth mentioning them at.

        A SEPARATE ENDPOINT RATHER THAN A FIELD ON /api/config, because this reads a directory that
        lives on a network share. Config is on the startup path and everything waits for it; this is
        a nudge nobody is waiting for, so a slow share may delay the nudge and must not delay the
        app.

        Unreachable roots are skipped rather than reported as empty -- an offline share has an
        unknown count, not a zero, and answering zero would silently reset the client's mark.
        """
        out = []
        for r in CONFIG.get('roots') or []:
            try:
                if not (r.get('path') and os.path.isdir(r['path'])):
                    continue
            except OSError:
                continue
            files, size = _recycle_folder_stats(r['path'])
            # Reachable-but-empty is REPORTED, as zero. Omitting it would make an emptied folder
            # indistinguishable from an offline one, and the client clears its "already mentioned"
            # mark on a zero -- so an offline share would silently re-arm the nudge.
            out.append({'key': r['key'], 'name': r['name'], 'files': files, 'bytes': size})
        g = CONFIG.get('general', {})
        self._json({'roots': out,
                    'enabled': bool(g.get('recycle_warn', DEFAULT_GENERAL['recycle_warn'])),
                    'warn_files': g.get('recycle_warn_files', DEFAULT_GENERAL['recycle_warn_files']),
                    'warn_bytes': float(g.get('recycle_warn_gb',
                                              DEFAULT_GENERAL['recycle_warn_gb'])) * 1024 ** 3})

    def reveal(self, iid):
        r = self._path_for(iid)
        if not r:
            return self._json({'error': 'not found'}, 404)
        try:
            subprocess.Popen(['explorer', '/select,', os.path.normpath(r['path'])])
        except Exception as e:
            return self._json({'error': str(e)}, 500)
        self._json({'ok': True})

    def _dupe_survey(self, iid):
        """(groups, doomed_ids, report) for the folder holding `iid`. Shared by the count endpoint
        and the apply endpoint so both describe exactly the same set."""
        conn = db()
        try:
            reachable = _reachable_root_ids()
            groups = find_folder_dupes(conn, iid, reachable)
            doomed = [d for _k, ds in groups for d in ds]
            by_lib = {}
            if doomed:
                qm = ','.join('?' * len(doomed))
                for r in conn.execute(f"SELECT root_id, COUNT(*) c FROM images "
                                      f"WHERE id IN ({qm}) GROUP BY root_id", doomed):
                    rt = _root_by_key(r['root_id']) or {}
                    by_lib[rt.get('name') or r['root_id'] or '?'] = r['c']
            # name any library sitting this out, so "found nothing" is never a mystery
            offline = [r.get('name') or r.get('key') for r in (CONFIG.get('roots') or [])
                       if r.get('key') not in reachable]
            return groups, doomed, {'groups': len(groups), 'copies': len(doomed),
                                    'curated': len(curated_ids(conn, doomed)),
                                    'by_library': by_lib, 'offline': offline}
        finally:
            conn.close()

    def api_dupes_scan(self, q):
        """Read-only sweep for duplicate files across every reachable library. Deletes nothing —
        it exists because the per-folder cull can only show duplicates you're already standing in,
        which is a poor way to find out whether you have any."""
        try:
            each = max(1, min(1000, int(q.get('each', ['200'])[0] or 200)))
        except ValueError:
            each = 200
        conn = db()
        try:
            reachable = _reachable_root_ids()
            sets = find_all_dupes(conn, reachable)      # already ordered by recoverable bytes, desc
            names = {r['key']: (r.get('name') or r['key']) for r in (CONFIG.get('roots') or [])}

            def row(r):
                return {'id': r['id'], 'folder': r['folder'], 'mtime': r['mtime'],
                        'library': names.get(r['root_id'], '?')}

            def out(s):
                return {'filename': s['filename'], 'size': s['size'],
                        'recoverable': len(s['dupes']) * s['size'],
                        'keep': row(s['keep']), 'dupes': [row(d) for d in s['dupes']]}

            # both ends of the same ordering: the big wins, and a sample of the small ones so the
            # rule can be sanity-checked across the whole range rather than just the top
            if len(sets) <= each * 2:
                largest, smallest = [out(s) for s in sets], []
            else:
                largest = [out(s) for s in sets[:each]]
                smallest = [out(s) for s in sets[-each:]]
            self._json({
                'sets': len(sets),
                'copies': sum(len(s['dupes']) for s in sets),
                'bytes': sum(len(s['dupes']) * s['size'] for s in sets),
                'largest': largest, 'smallest': smallest,
                'offline': [r.get('name') or r.get('key') for r in (CONFIG.get('roots') or [])
                            if r.get('key') not in reachable],
            })
        finally:
            conn.close()

    def api_dupes_cull(self):
        """Recycle the duplicates of the N largest sets, keeping each set's chosen keeper.

        Deliberately batched rather than all-at-once: a library-wide cull can be thousands of files
        and gigabytes, and Windows silently starts purging the oldest Recycle Bin contents once a
        drive's bin quota is exceeded — at which point "recoverable" stops being true. Batches keep
        each delete well inside that, and let the user stop after seeing the first one land.

        The batch is recomputed here, never taken from the client, so what gets deleted comes from
        the same query that produced the report.
        """
        if send2trash is None:
            return self._json({'error': 'Send2Trash is not installed. Re-run start.bat, '
                                        'or: python -m pip install Send2Trash'}, 500)
        body = self._read_json()
        try:
            want = max(1, min(1000, int(body.get('sets') or 200)))
        except (TypeError, ValueError):
            want = 200
        conn = db()
        try:
            batch = find_all_dupes(conn, _reachable_root_ids())[:want]
            doomed = [d['id'] for s in batch for d in s['dupes']]
            if not doomed:
                return self._json({'deleted': 0, 'failed': [], 'sets': 0, 'bytes': 0})
            for s in batch:                      # curation first — the recycle drops their rows
                merge_curation(conn, s['keep']['id'], [d['id'] for d in s['dupes']])
            conn.commit()
            freed = sum(len(s['dupes']) * s['size'] for s in batch)
        finally:
            conn.close()
        moved, purged, failed = _recycle_ids(doomed)
        self._json({'deleted': moved, 'purged': purged, 'failed': failed,
                    'sets': len(batch), 'bytes': freed})

    def api_setcull_preview(self, q):
        """Read-only: how many sets in this folder match the demonstration. Drives the count line
        under the "Apply to folder" checkbox, and later the confirm. Deletes nothing."""
        try:
            iid = int(q.get('id', ['0'])[0] or 0)
        except ValueError:
            return self._json({'error': 'bad id'}, 400)
        if not iid:
            return self._json({'error': 'need id'}, 400)
        conn = db()
        try:
            _matches, stats = find_setcull_sets(conn, iid, q.get('keep', [''])[0])
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        finally:
            conn.close()
        self._json(stats)

    def api_setcull(self):
        """Start the folder cull. The client sends the anchor image and the role it demonstrated —
        never a list of ids, so what gets recycled is derived from the same function that produced
        the count the user agreed to."""
        if send2trash is None:
            return self._json({'error': 'Send2Trash is not installed. Re-run start.bat, '
                                        'or: python -m pip install Send2Trash'}, 500)
        body = self._read_json()
        try:
            iid = int(body.get('id') or 0)
        except (TypeError, ValueError):
            return self._json({'error': 'bad id'}, 400)
        if not iid:
            return self._json({'error': 'need id'}, 400)
        if _job_running():
            return self._json({'error': 'Another job is running'}, 409)
        # Fail here, with a reason the user can read, rather than inside a thread whose only channel
        # back is a status field nobody is watching yet.
        conn = db()
        try:
            find_setcull_sets(conn, iid, body.get('keep'))
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        finally:
            conn.close()
        if not run_setcull(iid, body.get('keep')):
            return self._json({'error': 'Another job is running'}, 409)
        self._json({'started': True})

    def api_folder_dupes(self, q):
        """Read-only: how many copies of this folder's files sit elsewhere. Drives the confirm."""
        try:
            iid = int(q.get('id', ['0'])[0] or 0)
        except ValueError:
            return self._json({'error': 'bad id'}, 400)
        if not iid:
            return self._json({'error': 'need id'}, 400)
        _groups, _doomed, report = self._dupe_survey(iid)
        self._json(report)

    def api_folder_dupes_apply(self):
        """Merge each copy's curation onto the file we're keeping, then recycle the copies.

        The set is recomputed here rather than taken from the client, so what gets deleted is
        derived from the same query that produced the count the user agreed to.
        """
        if send2trash is None:
            return self._json({'error': 'Send2Trash is not installed. Re-run start.bat, '
                                        'or: python -m pip install Send2Trash'}, 500)
        body = self._read_json()
        try:
            iid = int(body.get('id') or 0)
        except (TypeError, ValueError):
            return self._json({'error': 'bad id'}, 400)
        if not iid:
            return self._json({'error': 'need id'}, 400)
        groups, doomed, report = self._dupe_survey(iid)
        if not doomed:
            return self._json({'deleted': 0, 'failed': [], 'kept': 0})
        conn = db()
        try:                                     # curation first — the recycle drops their rows
            for keeper, ds in groups:
                merge_curation(conn, keeper, ds)
            conn.commit()
        finally:
            conn.close()
        moved, purged, failed = _recycle_ids(doomed)
        self._json({'deleted': moved, 'purged': purged, 'failed': failed, 'kept': len(groups),
                    'merged': report['curated']})

    def api_delete(self):
        """QUEUE the selected images for recycling, giving the client UNDO_WINDOW_S to cancel.

        Nothing moves and no row is purged here — the ids go into the pending queue and drop out of
        every view (see _pending_sql), so the grid tells the truth while the timer runs. The reply
        is therefore a batch id, not a count: `deleted`/`purged`/`failed` cannot be known yet, and
        inventing them would be a lie the client then toasts. /api/delete/result has them after.

        Only THIS path defers. The cull endpoints merge curation onto a keeper before deleting, and
        that merge is not cleanly reversible (see merge_curation), so they stay immediate — they are
        confirmed up front and stoppable mid-run instead."""
        if send2trash is None:
            return self._json({'error': 'Send2Trash is not installed. Re-run start.bat, '
                                        'or: python -m pip install Send2Trash'}, 500)
        body = self._read_json()
        ids = [int(i) for i in (body.get('ids') or []) if str(i).lstrip('-').isdigit()]
        if not ids:
            return self._json({'error': 'no ids'}, 400)
        # THE CARD'S INVISIBLE MEMBERS GO TOO. The client expands a card from what the GRID gave it,
        # and a card's membership is built from rows that survived the global exclusions -- so a
        # member you have hidden with `h`, or an oversized one while Hide large images is on, is not
        # in that list and was being left on disk when its run was recycled (the author, 2026-09-06:
        # "the png's may be getting orphaned, but that seems inconsistent" -- inconsistent because
        # it depends on whether any member of that particular run was hidden or oversized).
        # Excluding them from the VIEW is deliberate and right; excluding them from the DELETE is
        # not, because recycling a card means recycling the run.
        #
        # Gated on the client's own `collapsed`, which is collapsingNow() -- with nothing merging, a
        # card IS a file and there is no run to take. Known limit: collapsingNow() is one flag for
        # all three merge kinds, so with (say) pairs off and sets on it can still expand a pair's
        # hidden sibling. Narrow, and the alternative is a third copy of api_search's collapse key,
        # which is exactly how api_ids drifted and started handing back songs.
        if body.get('collapsed'):
            ids = _expand_to_cards(ids)
        bid = _queue_recycle(ids)
        self._json({'batch': bid, 'pending': len(ids), 'window': UNDO_WINDOW_S})

    def api_delete_undo(self):
        """Cancel a queued recycle. A batch that already ran is a no-op that SAYS SO (200, not 4xx):
        the window closing is the normal end of its life, and the client racing it by a few ms is
        expected rather than exceptional."""
        try:
            bid = int((self._read_json() or {}).get('batch'))
        except (TypeError, ValueError):
            return self._json({'error': 'need a batch id'}, 400)
        ids = _undo_batch(bid)
        if ids is None:
            return self._json({'ok': True, 'undone': False, 'reason': 'already recycled'})
        self._json({'ok': True, 'undone': True, 'restored': len(ids), 'ids': ids})

    def api_delete_commit(self):
        """Run a queued recycle now rather than waiting out its window — what the client calls when
        you recycle again, since only one batch is ever undoable. Idempotent and never an error: a
        batch that already ran is simply reported as not committed here."""
        try:
            bid = int((self._read_json() or {}).get('batch'))
        except (TypeError, ValueError):
            return self._json({'error': 'need a batch id'}, 400)
        self._json({'ok': True, 'committed': _commit_batch(bid)})

    def api_delete_result(self, q):
        """What a flushed batch actually did. The client asks once its countdown ends, because with
        the recycle deferred a failure now arrives AFTER the cards left the grid."""
        try:
            bid = int(q.get('batch', [''])[0])
        except (TypeError, ValueError):
            return self._json({'error': 'need a batch id'}, 400)
        with _pending_lock:
            b = _pending.get(bid)
            res = b['result'] if b else None
            still_queued = bool(b and b['result'] is None)
        if res is None:
            return self._json({'pending': still_queued})
        self._json(dict(res, pending=False))

    # -- tags & favorites --
    def api_update(self):
        """Is there a newer release? Drives the one amber mark beside the logo.

        THE SETTING IS CHECKED HERE, not only in the client, so switching the feature off actually
        stops the request rather than merely hiding its result. That distinction is the whole
        difference between "off" and "off as far as you can see", and it was the shape this design
        rejected: hiding the icon while still asking GitHub every day is the worst of both.

        Always 200 with a shape the client can read. Nobody asked a question here -- this runs off
        boot -- so there is no failure worth a status code.
        """
        if not CONFIG['general'].get('update_check', True):
            return self._json({'available': False})
        return self._json(update_status())

    def api_catchup(self):
        """Which libraries were indexed by a reader older than this build's, and what catching them
        up would cost. Drives the dialog on the first launch after an update.

        THE COUNT IS THE ESTIMATE'S DENOMINATOR, so it counts FILES BEHIND rather than files: a run
        stopped half way leaves some rows stamped and some not, and the next offer has to be about
        what is left rather than starting its sums again.

        A TIME IS QUOTED ONLY IF EVERY LIBRARY IN THE OFFER HAS A MEASURED RATE. Rates are recorded
        at the end of a forced run and per library, because a network share and an SSD are not
        comparable — so a fresh install has none and the dialog says the file count alone. A guessed
        minute figure would be worse than no figure: it is the number the user decides on.
        """
        conn = db()
        behind = {r['root_id']: r['n'] for r in conn.execute(
            "SELECT root_id, COUNT(*) n FROM images WHERE reader_ver < ? GROUP BY root_id",
            (index_db.READER_VERSION,)).fetchall()}
        rates = {}
        for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'read_rate:%'").fetchall():
            try:
                rates[r['key'][len('read_rate:'):]] = float(r['value'])
            except (TypeError, ValueError):
                pass

        # EVERY library's size as well as its backlog, because there are two questions with the same
        # arithmetic behind them: "what would catching up cost?" (the offer, which is about the rows
        # left behind) and "what would Rescan all libraries cost?" (the menu, which is about every
        # file, and can be reached when nothing is behind at all). Answering only the first left the
        # menu's confirm saying "this can take a while" on a machine whose speed we had measured.
        total = {r['root_id']: r['n'] for r in conn.execute(
            "SELECT root_id, COUNT(*) n FROM images GROUP BY root_id").fetchall()}
        conn.close()

        libs, files, secs, files_all, secs_all = [], 0, 0.0, 0, 0.0
        for r in CONFIG['roots']:
            n = behind.get(r['key']) or 0
            all_n = total.get(r['key']) or 0
            # Same isdir the heartbeat uses, and for the same reason: it is the answer that tracks a
            # share waking up. An unreachable library is reported, not hidden — the dialog names what
            # it will skip rather than letting the user discover it afterwards.
            here = os.path.isdir(r['path'])
            rate = rates.get(r['key'])
            if n:
                # ITS OWN seconds, not just the run's total, because one library can now be caught
                # up on its own from its ⋯ menu and that confirm needs the price of THAT library.
                # None when this library has no measured rate — same rule as the total below: the
                # count alone beats a guessed minute figure, since this is the number someone
                # decides on.
                libs.append({'key': r['key'], 'name': r['name'], 'files': n, 'total': all_n,
                             'reachable': here,
                             'seconds': round(n / rate) if rate else None})
            if not here:
                continue
            files += n
            secs = (secs + n / rate) if (rate and secs is not None) else None
            files_all += all_n
            secs_all = (secs_all + all_n / rate) if (rate and secs_all is not None) else None
        self._json({'reader': index_db.READER_VERSION, 'libraries': libs,
                    'files': files, 'seconds': round(secs) if secs else None,
                    'files_all': files_all, 'seconds_all': round(secs_all) if secs_all else None})

    def api_changes(self):
        """Cheap per-library freshness check: for each library, has anything on disk changed since
        its last scan (files/folders added/removed/renamed)? One stat per indexed folder — no full
        walk. Returns {key: changed}, driving the per-row ↻ indicator.

        ALSO REPORTS REACHABILITY, and that is not a bolt-on. /api/config carries `exists` too, but
        the client reads that at boot and on library edits only — so a share that came back stayed
        marked "(missing)" until the app was restarted (the author, 2026-09-06). This endpoint is the
        app's existing disk heartbeat, and "is the folder there" is exactly the kind of fact it
        already goes to disk for. Nothing else runs often enough to notice.

        The isdir also GUARDS the freshness check now. changes_detected stats a folder that is not
        there, which on a downed share is a wait for a timeout to learn something isdir already
        said."""
        out, alive = {}, {}
        for r in CONFIG['roots']:
            here = os.path.isdir(r['path'])
            alive[r['key']] = here
            changed = False
            if here and _root_has_images(r['key']):
                try:
                    changed = index_db.changes_detected(LIBRARY_DB, r['path'], r['key'])
                except Exception:
                    changed = False
            out[r['key']] = changed
        self._json({'changes': out, 'changed': any(out.values()), 'exists': alive})

    def api_tags(self, q=None):
        """The sidebar's numbers: every user tag with its count, and the favourite count. Scoped to
        the currently-shown roots, so the rail describes the visible library rather than the whole
        index. It also counted how many images were still unreviewed until that was retired on
        2026-08-19 — that count alone was 354–1417ms of every refresh.

        THIS IS A BOOT QUERY, and the rail waits for it — it used to sit dimmed for seconds after
        the grid was already usable, which is worse than both being slow. What cost that was an
        orphan guard: `EXISTS(SELECT 1 FROM images WHERE id=t.image_id)` on the tag list and the
        favourite count, which probes the images table once per TAG row, by rowid, reading a whole
        row — prompt text included — to learn only that the row is there. Benchmarked at 10x the
        rest of the endpoint (`bench_tags.py`), and far worse cold, since those probes are random
        reads scattered through the largest table in the database.

        It was also guarding against something this app cannot produce. Every path that removes an
        image removes its tag rows in the same breath (index_db.delete_by_ids, delete_root, and the
        scan's prune), and every scan then sweeps any legacy strays. So the guard is gone, and root
        scoping — when it applies at all — is one pass over the root index rather than a probe per
        row. Phases are reported so the next trace can say which third of this is slow.

        THESE NUMBERS FOLLOW THE FILTERS, since 2026-09-14. They used to be whole-library totals
        scoped only by which libraries were ticked, so the rail could say "To publish 10" beside a
        grid holding one -- The author found exactly that. Same failure as the facet counts the same day,
        arriving by a different route: a number that answers "how many exist" while presenting
        itself as "how many you would get".

        A TAG DOES NOT NARROW ITSELF, the rule /api/facets already follows. `tags` and `fav` are
        dropped from the query before the filter is built, so ticking one label leaves the other
        four counting what switching to them would give. Without that, the exclusive labels would
        all read 0 the moment you picked one, and the list could not be used to move between them.

        THE COST, measured on 110k images: 5ms with nothing narrowing (the fast path below), 7ms on
        a model, 75-81ms on a text search or a type filter. It re-runs on every filter change rather
        than once at boot, so the expensive shapes are paid repeatedly -- which is why the cheap one
        matters as much as it does.
        """
        conn = db()
        # Its own dimension, removed. `q` is parsed query args (name -> list), so a shallow copy is
        # enough; the originals are left alone for every other reader of this request.
        q2 = dict(q or {})
        q2.pop('tags', None)
        q2.pop('fav', None)
        joins, wsql, params = self._filters(q2)
        any_tag = ANY_TAG_SQL.replace('source', 't.source')
        # NOTHING NARROWING MEANS NO JOIN, and this is not an optimisation for a rare case -- it is
        # the two cases that felt slow. Boot has no filters by definition, and Reset all clears them
        # by definition, so both were paying a join against every image to reach an answer the tags
        # table alone already had. The author reported both within minutes of the change: "reset all takes
        # longer - i'm sure of it", then "first load is slower too".
        #
        # Provably the same answer rather than an approximation: with no WHERE and no JOIN clauses
        # there is no predicate to apply, so joining to images could only re-find every row it
        # started with. It is also the shape this endpoint was optimised into once already -- the
        # docstring above is the record of that -- and the shape the boot path has always paid.
        plain = not wsql and not joins.strip()
        with self._phase('taglist'):
            sql = (f"SELECT t.tag, COUNT(*) c, MAX(t.source='user') AS mine FROM tags t "
                   f"WHERE {any_tag} GROUP BY t.tag ORDER BY c DESC, t.tag COLLATE NOCASE") if plain \
                else (f"SELECT t.tag, COUNT(*) c, MAX(t.source='user') AS mine "
                      f"FROM tags t JOIN images i ON i.id = t.image_id {joins} {wsql} AND {any_tag} "
                      "GROUP BY t.tag ORDER BY c DESC, t.tag COLLATE NOCASE")
            tags = [{'name': r['tag'], 'count': r['c'], 'machine': not r['mine']}
                    for r in conn.execute(sql, () if plain else params)]
        with self._phase('fav'):
            fsql = "SELECT COUNT(*) c FROM tags t WHERE t.source='fav'" if plain \
                else (f"SELECT COUNT(*) c FROM tags t JOIN images i ON i.id = t.image_id "
                      f"{joins} {wsql} AND t.source='fav'")
            fav = conn.execute(fsql, () if plain else params).fetchone()['c']
        conn.close()
        self._json({'tags': tags, 'favorites': fav})

    def _ids_from(self, body):
        return [int(i) for i in (body.get('ids') or []) if str(i).lstrip('-').isdigit()]

    def api_tag(self):
        b = self._read_json()
        ids = self._ids_from(b)
        tag = (b.get('tag') or '').strip().replace(',', ' ').strip().lower()
        if not ids or not tag:
            return self._json({'error': 'need ids and tag'}, 400)
        conn = db()
        if b.get('op') == 'remove':
            conn.executemany(f"DELETE FROM tags WHERE image_id=? AND tag=? AND {ANY_TAG_SQL}",
                             [(i, tag) for i in ids])
        else:
            conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES (?,?,'user')",
                             [(i, tag) for i in ids])
        conn.commit()
        conn.close()
        self._json({'ok': True, 'tag': tag})

    def api_label(self):
        """Set the exclusive curation label on each id (label=<slug>), or clear it (label
        null/empty). Stored as a managed `label:<slug>` tag: delete any existing label first,
        then set the new one, so at most one label exists per image."""
        b = self._read_json()
        ids = self._ids_from(b)
        slug = (b.get('label') or '').strip().lower() or None
        if not ids:
            return self._json({'error': 'need ids'}, 400)
        if slug is not None and slug not in _LABEL_SLUGS:
            return self._json({'error': f'unknown label: {slug}'}, 400)
        conn = db()
        conn.executemany("DELETE FROM tags WHERE image_id=? AND source='user' AND tag LIKE 'label:%'",
                         [(i,) for i in ids])
        if slug:
            conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES (?,?,'user')",
                             [(i, 'label:' + slug) for i in ids])
        conn.commit()
        conn.close()
        self._json({'ok': True, 'label': slug})

    def api_tag_delete(self):
        b = self._read_json()
        tag = (b.get('tag') or '').strip().lower()
        if not tag:
            return self._json({'error': 'need tag'}, 400)
        conn = db()
        n = conn.execute(f"DELETE FROM tags WHERE tag=? AND {ANY_TAG_SQL}", (tag,)).rowcount
        conn.commit()
        conn.close()
        self._json({'deleted': n})

    def api_favorite(self):
        b = self._read_json()
        ids = self._ids_from(b)
        if not ids:
            return self._json({'error': 'need ids'}, 400)
        conn = db()
        if b.get('on'):
            conn.executemany("INSERT OR IGNORE INTO tags(image_id, tag, source) VALUES (?,'favorite','fav')",
                             [(i,) for i in ids])
        else:
            conn.executemany("DELETE FROM tags WHERE image_id=? AND source='fav'", [(i,) for i in ids])
        conn.commit()
        conn.close()
        self._json({'ok': True})

    def api_note(self):
        """Set (or clear) the free-text note on a single image. Blank text stores NULL so the
        grid-card note indicator clears. Note lives in images.note (survives rescan)."""
        b = self._read_json()
        try:
            iid = int(b.get('id'))
        except (TypeError, ValueError):
            return self._json({'error': 'need id'}, 400)
        text = (b.get('text') or '').strip() or None
        conn = db()
        conn.execute("UPDATE images SET note=? WHERE id=?", (text, iid))
        conn.commit()
        conn.close()
        self._json({'ok': True, 'has_note': text is not None})

    # -- bulk rename (find & replace in filename stem) --
    def _rename_plan(self, conn, ids, find, replace):
        """Compute old->new names (in-place, same folder), auto-numbering collisions."""
        qmarks = ','.join('?' * len(ids))
        rows = {r['id']: r for r in conn.execute(
            f"SELECT id, path, folder, filename FROM images WHERE id IN ({qmarks})", ids)}
        claimed = set()   # lower-cased new abs paths taken during this batch
        plan = []
        for iid in ids:
            r = rows.get(iid)
            if not r:
                continue
            old_path = r['path']
            folder_abs = os.path.dirname(old_path)
            name = r['filename']
            stem, ext = os.path.splitext(name)
            new_stem = stem.replace(find, replace)
            if new_stem == stem:
                plan.append({'id': iid, 'old': name, 'new': name, 'status': 'unchanged'})
                continue
            cand = new_stem + ext
            n = 1
            while True:
                new_abs = os.path.join(folder_abs, cand)
                low = new_abs.lower()
                collides = low != old_path.lower() and (low in claimed or os.path.exists(new_abs))
                if not collides:
                    break
                cand = f"{new_stem}_{n}{ext}"
                n += 1
            new_abs = os.path.join(folder_abs, cand)
            claimed.add(new_abs.lower())
            plan.append({'id': iid, 'old': name, 'new': cand, 'status': 'rename',
                         'old_path': old_path, 'new_path': new_abs})
        return plan

    def api_rename(self):
        b = self._read_json()
        ids = self._ids_from(b)
        find = b.get('find') or ''
        replace = b.get('replace') or ''
        if not ids or find == '':
            return self._json({'error': 'need ids and a non-empty "find"'}, 400)
        conn = db()
        plan = self._rename_plan(conn, ids, find, replace)
        if b.get('dry_run'):
            conn.close()
            return self._json({'plan': [{k: p[k] for k in ('id', 'old', 'new', 'status')} for p in plan],
                               'changes': sum(1 for p in plan if p['status'] == 'rename')})
        root = ACTIVE['path'] or ''
        renamed, errors = 0, []
        for p in plan:
            if p['status'] != 'rename':
                continue
            try:
                os.rename(p['old_path'], p['new_path'])
            except Exception as e:
                errors.append({'old': p['old'], 'error': str(e)})
                continue
            # Every column the FTS indexes has to be re-supplied below, `lyrics` included: this path
            # deletes the row from the index and puts it back with a new filename, so a column
            # missing here is a value silently dropped from search by a rename.
            meta = conn.execute("SELECT positive, model_name, folder, lyrics FROM images WHERE id=?",
                                (p['id'],)).fetchone()
            index_db._fts_delete(conn, p['id'])
            rel = os.path.relpath(p['new_path'], root) if root else p['new']
            conn.execute("UPDATE images SET path=?, rel_path=?, filename=? WHERE id=?",
                         (p['new_path'], rel, p['new'], p['id']))
            index_db._fts_insert(conn, p['id'], meta['positive'], meta['model_name'], p['new'],
                                 meta['folder'], meta['lyrics'])
            try:  # move the cached thumbnail to its new key so it isn't regenerated
                old_t = os.path.join(THUMBS_DIR, thumbs_mod.thumb_rel(p['old_path']))
                new_t = os.path.join(THUMBS_DIR, thumbs_mod.thumb_rel(p['new_path']))
                if os.path.exists(old_t):
                    os.makedirs(os.path.dirname(new_t), exist_ok=True)
                    os.replace(old_t, new_t)
            except Exception:
                pass
            renamed += 1
        conn.commit()
        conn.close()
        self._json({'renamed': renamed, 'errors': errors})

    # -- Settings -> Extensions ------------------------------------------------
    def api_ext_enable(self):
        """Turn one extension on or off. Only OFF is stored — see _normalize_config."""
        b = self._read_json()
        ext_id = str(b.get('id') or '').strip()
        ext = get_extension(ext_id) if ext_id else None
        if not ext:
            return self._json({'error': 'no such extension'}, 404)
        exts = dict(CONFIG.get('extensions', {}))
        if b.get('enabled'):
            exts.pop(ext_id, None)
        else:
            exts[ext_id] = False
        CONFIG['extensions'] = exts
        save_config()
        self._json({'ok': True, 'extensions': _extensions_payload()})

    def _ext_and_values(self, b):
        """(extension, merged values) for a request carrying {id, values}. None on a bad id.

        Shared by Save and Test so the two cannot read the same panel differently."""
        ext_id = str(b.get('id') or '').strip()
        ext = get_extension(ext_id) if ext_id else None
        if not ext:
            return None, None
        sent = b.get('values') if isinstance(b.get('values'), dict) else {}
        cur = dict(CONFIG.get('ext_settings', {}).get(ext_id, {}))
        return ext, merge_ext_values(ext, sent, cur)

    def api_ext_settings(self):
        """Save one extension's settings, against the schema its manifest declares.

        THE SCHEMA IS ENFORCED HERE rather than in the config coercer, which is schema-blind on
        purpose (see _coerce_ext_settings): this is the one moment both the manifest and the new
        values are in hand. The folding rules live in merge_ext_values, with Test."""
        b = self._read_json()
        ext, merged = self._ext_and_values(b)
        if not ext:
            return self._json({'error': 'no such extension'}, 404)
        if not isinstance(b.get('values'), dict):
            return self._json({'error': 'no settings sent'}, 400)
        store = dict(CONFIG.get('ext_settings', {}))
        store[ext['id']] = merged
        CONFIG['ext_settings'] = store
        save_config()
        self._json({'ok': True, 'extensions': _extensions_payload()})

    def api_ext_test(self):
        """Ask an extension to check its own setup, with what is currently on screen.

        DELIBERATELY SAVES NOTHING. Testing an address is how you find out whether it is the right
        one; a button that had to write it down first would make the wrong address the thing you
        keep."""
        b = self._read_json()
        ext, merged = self._ext_and_values(b)
        if not ext:
            return self._json({'error': 'no such extension'}, 404)
        self._json(run_ext_test(ext, merged))

    def api_ext_setup(self):
        """Run an extension's setup script in ITS OWN console window.

        Deliberately not a job inside the app: a setup step downloads gigabytes, and a download
        that dies when you close the viewer is worse than one you can watch. The console is the
        progress bar. Nothing here starts without the click that reaches this endpoint."""
        b = self._read_json()
        ext_id = str(b.get('id') or '').strip()
        ext = get_extension(ext_id) if ext_id else None
        if not ext:
            return self._json({'error': 'no such extension'}, 404)
        script = ext['setup']
        if not script:
            return self._json({'error': "%s has no setup step." % ext['name']}, 400)
        if not os.path.exists(script):
            return self._json({'error': 'Its setup script is missing (%s).'
                                        % os.path.basename(script)}, 400)
        try:
            subprocess.Popen(['cmd', '/c', 'start', '', os.path.normpath(script)],
                             cwd=os.path.dirname(script), shell=False)
        except Exception as e:
            return self._json({'error': 'Could not start setup: %s' % e}, 500)
        self._json({'ok': True})

    # -- tagging a selection with one tagger extension (background venv job) --
    def api_tag_batch(self):
        b = self._read_json()
        ext_id = str(b.get('ext') or '').strip()
        ids = self._ids_from(b)
        if not ids:
            return self._json({'error': 'no ids'}, 400)
        ext = get_extension(ext_id) if ext_id else None
        if not ext or not ext['active'] or ext['produces'] != 'tags':
            return self._json({'error': "That tagger isn't available — set it up or switch it "
                                        "on in Settings \u2192 Extensions."}, 400)
        if _tag_state['running']:
            return self._json({'error': 'A tagging run is already in progress.'}, 409)
        if not run_tag_batch(ext_id, ids, skip_tagged=b.get('skip_tagged', True)):
            return self._json({'error': 'Could not start %s.' % ext['name']}, 400)
        self._json({'started': True, 'ext': ext['name']})

    # -- asking a text extension about a file, or about a selection --
    def api_text_ask(self):
        """One saved question, asked of each file in turn. One id from the detail view, many from
        the grid's selection menu.

        THE IDS ARE NOT EXPANDED into a card's members, which every other bulk action does. A card
        is one picture as far as the person looking at it is concerned, and a set's three stages
        are the same image three times — one answer per card is what "one answer per image" means
        on screen, and expanding would spend three times the model's time to say it three times."""
        b = self._read_json()
        ext_id = str(b.get('ext') or '').strip()
        ids = self._ids_from(b)[:MAX_TEXT_ANSWERS]
        if not ids:
            return self._json({'error': 'no ids'}, 400)
        ext = get_extension(ext_id) if ext_id else None
        if not ext or not ext['active'] or ext['produces'] != 'text':
            return self._json({'error': "That extension isn't available — switch it on in "
                                        "Settings → Extensions."}, 400)
        if _text_state['running']:
            return self._json({'error': 'It is still working on the last one.'}, 409)
        if not run_text_batch(ext_id, ids):
            return self._json({'error': 'Could not start %s.' % ext['name']}, 400)
        self._json({'started': True, 'ext': ext['name']})

    def api_clear_machine_tags(self):
        """Drop every tag written by an extension, for the ACTIVE library.

        The group undo, and the reason a bulk auto-tag is safe to try at all. Deliberately does NOT
        touch source='user', so nothing the author typed, no label, no favourite and no Hidden mark can
        be lost to it -- the same structural safety as Clear tags, from the other side."""
        if self._job_busy():
            return
        b = self._read_json()
        rk = b.get('key') or ACTIVE['key']
        ext_id = str(b.get('ext') or '').strip()
        conn = db()
        if ext_id:
            conn.execute("DELETE FROM tags WHERE source=? AND image_id IN "
                         "(SELECT id FROM images WHERE root_id IS ?)", ('ext:' + ext_id, rk))
        else:
            conn.execute(f"DELETE FROM tags WHERE {MACHINE_TAG_SQL} AND image_id IN "
                         "(SELECT id FROM images WHERE root_id IS ?)", (rk,))
        n = conn.total_changes
        conn.commit()
        conn.close()
        self._json({'ok': True, 'removed': n})

    # -- single-image pyiqa Quality score (detail "Run Quality"); synchronous, own thread --
    # -- pyiqa reward scoring over a selection (background venv job) --
    # THERE IS NO SINGLE-IMAGE ENDPOINT ANY MORE, and that is the fix rather than a tidy-up.
    # /api/reward scored one image synchronously, spawning a fresh venv worker and reloading the
    # model for each request -- tens of seconds with the request held open and nothing to show for
    # it, which is what the author reported as "no countdown" on 2026-09-10. The detail view now sends one
    # id (or a set's several) to this batch job, so every scoring run in the app has the same
    # progress, the same Stop, and one way to fail. A second path existed only to be faster and was
    # slower.
    def api_reward_batch(self):
        b = self._read_json()
        ids = self._ids_from(b)
        if not ids:
            return self._json({'error': 'no ids'}, 400)
        if not scorer_available():
            return self._json({'error': "The Quality scorer isn't available — set it up or "
                                        "switch it on in Settings → Extensions."}, 400)
        if _reward_state['running']:
            return self._json({'error': 'A Quality scoring run is already in progress.'}, 409)
        skip = b.get('skip_scored', True)
        if skip:
            conn = db()
            have = {r[0] for r in conn.execute(
                "SELECT image_id FROM quality WHERE reward IS NOT NULL")}
            conn.close()
            todo = sum(1 for i in ids if i not in have)
        else:
            todo = len(ids)
        if not todo:
            return self._json({'started': False, 'todo': 0, 'selected': len(ids)})
        run_reward_batch(ids, skip_scored=skip)
        self._json({'started': True, 'todo': todo, 'selected': len(ids)})

    def api_opened(self):
        """Record that an image was just opened in the detail view. Bumps BOTH last_opened (the
        deliberate-look signal) and last_seen (it was obviously on screen) — opening is a strictly
        stronger event than seeing, so the two can never disagree in the wrong direction.
        Fire-and-forget: a failure here must never disrupt viewing, so it just no-ops.

        NOTHING READS EITHER COLUMN TODAY, and this is kept deliberately (CLN-1, 2026-08-05). The
        "Least seen" sort they fed was retired along with the per-card impression tracking that was
        the expensive half of the signal — an IntersectionObserver and a dwell timer on every card,
        POSTing every few seconds. This is the cheap half: one write per detail open. Viewing
        history cannot be rebuilt, and the sort may well come back, so the trickle keeps running
        rather than freezing the record on the day the sort left."""
        try:
            iid = int(self._read_json().get('id'))
        except (TypeError, ValueError):
            return self._json({'error': 'need image id'}, 400)
        conn = db()
        conn.execute("UPDATE images SET last_opened=?, last_seen=? WHERE id=?",
                     (time.time(), time.time(), iid))
        conn.commit()
        conn.close()
        self._json({'ok': True})

    def api_window(self):
        """Remember where the app window was left, so start.bat can reopen it there.

        Chromium remembers an --app window's bounds itself, but keys them to an identity derived
        from the launch URL, so anything changing that URL between opening and closing loses the
        position. The boot splash did, and the window came back full-height at the left of the
        screen every time; the splash was reverted and this stayed, because it works regardless of
        what the browser does with its own memory.

        The client sends the FRAME rect (it converts from the viewport's, see windowRect()), which
        is what --window-position expects.

        Validated rather than trusted, because this value is passed to a browser on the next launch
        and a bad one is uniquely nasty: a window restored at (-32000, -32000) or sized 0x0 is a
        program that simply stops appearing, with nothing on screen to explain why. Bounds are
        generous — the job is to reject the absurd, not to police window sizes. Best-effort like
        api_opened: a geometry that cannot be written must never stop the app closing.
        """
        try:
            d = self._read_json() or {}
            r = {k: int(d[k]) for k in ('x', 'y', 'w', 'h')}
        except (TypeError, ValueError, KeyError):
            return self._json({'error': 'need integer x, y, w, h'}, 400)
        if not (400 <= r['w'] <= 20000 and 300 <= r['h'] <= 20000
                and -20000 < r['x'] < 20000 and -20000 < r['y'] < 20000):
            return self._json({'error': 'window geometry out of range'}, 400)
        CONFIG['window'] = r
        save_config()
        return self._json({'ok': True, 'window': r})

    # -- app settings (General, Miner, Appearance) --
    def api_save_settings(self):
        b = self._read_json()
        if isinstance(b.get('general'), dict):
            g = b['general']
            CONFIG['general'] = {'autoplay': bool(g.get('autoplay',
                                 CONFIG['general'].get('autoplay', False))),
                                 'keep_behavior': ('stay' if g.get('keep_behavior',
                                 CONFIG['general'].get('keep_behavior', 'next')) == 'stay' else 'next'),
                                 'confirm_recycle': _bool_or(g.get('confirm_recycle'),
                                 CONFIG['general'].get('confirm_recycle', True)),
                                 'show_snapshots': _bool_or(g.get('show_snapshots'),
                                 CONFIG['general'].get('show_snapshots', True)),
                                 'recycle_warn': _bool_or(g.get('recycle_warn'),
                                 CONFIG['general'].get('recycle_warn', True)),
                                 # Every entry here falls back to the CURRENT value, which is what
                                 # lets the first-run pop-up post `{general: {update_check: false}}`
                                 # on its own without flattening the other seven settings. Missing
                                 # from this whitelist would be worse than missing a default: the
                                 # dict is rebuilt wholesale, so the key would disappear from the
                                 # config on the next unrelated Settings save.
                                 'update_check': _bool_or(g.get('update_check'),
                                 CONFIG['general'].get('update_check', True)),
                                 'recycle_warn_files': _clamped(g.get('recycle_warn_files'),
                                 RECYCLE_FILES_MIN, RECYCLE_FILES_MAX,
                                 CONFIG['general'].get('recycle_warn_files', RECYCLE_WARN_FILES)),
                                 'recycle_warn_gb': _clamped(g.get('recycle_warn_gb'),
                                 RECYCLE_GB_MIN, RECYCLE_GB_MAX,
                                 CONFIG['general'].get('recycle_warn_gb', RECYCLE_WARN_GB), decimals=1),
                                 'models_dir': str(g.get('models_dir',
                                 CONFIG['general'].get('models_dir', ''))).strip()}
        if isinstance(b.get('miner'), dict):
            if b['miner'].get('reset'):
                CONFIG['miner'] = dict(DEFAULT_MINER)
            else:
                CONFIG['miner'] = _coerce_miner(CONFIG['miner'], b['miner'])
        if isinstance(b.get('theme'), dict):
            CONFIG['theme'] = _coerce_theme(b['theme'])   # replace wholesale (Reset sends {})
        if isinstance(b.get('cards'), list):
            CONFIG['cards'] = _cards_or_none(b['cards'])
            if CONFIG['cards'] is None:
                CONFIG.pop('cards', None)          # back to following the default
        # The one-time Help pop-up, riding this endpoint rather than owning a route: every branch
        # above no-ops on a body carrying only this key, so `{"seen_help_hint": true}` falls
        # straight through to the save. OUTSIDE the `general` branch deliberately -- that one
        # rebuilds its dict wholesale from a whitelist, so a flag kept in there would be dropped by
        # the next Settings save. One-way: set only, so a stray body cannot un-see the hint.
        if b.get('seen_help_hint'):
            CONFIG['seen_help_hint'] = True
        try:
            save_config()
        except Exception as e:
            return self._json({'error': f'Could not save settings: {e}'}, 500)
        self._json({'ok': True, 'general': CONFIG['general'],
                    'miner': CONFIG['miner'], 'theme': CONFIG['theme'],
                    'cards': _effective_cards()})

    def api_save_snapshots(self):
        """Replace the whole snapshots list. One endpoint rather than four CRUD ones: the client
        always holds the entire list, it's a handful of small objects, and a wholesale replace makes
        create/rename/update/delete the same three lines here. The echoed list is authoritative —
        the client adopts it, so server-side truncation and de-duping show up in the UI."""
        b = self._read_json()
        if not isinstance(b.get('snapshots'), list):
            return self._json({'error': 'snapshots must be a list'}, 400)
        CONFIG['snapshots'] = _coerce_snapshots(b['snapshots'])
        try:
            save_config()
        except Exception as e:
            return self._json({'error': f'Could not save snapshots: {e}'}, 500)
        self._json({'ok': True, 'snapshots': CONFIG['snapshots']})

    def api_alive(self):
        """The app window checking in. Also cancels a countdown that a closing SIBLING tab armed —
        the newest news wins, which is the whole reason no client ids are tracked."""
        self._read_json()
        with _LIVE_LOCK:
            _LIVE['seen'] = time.time()
            _LIVE['gone_at'] = None
            _LIVE['ever'] = True
        return self._json({'ok': True})

    def api_bye(self):
        """A window is going away. ARMS the countdown; never exits here, because this fires on F5
        just as it does on a real close and the two are indistinguishable at this instant."""
        self._read_json()
        with _LIVE_LOCK:
            if _LIVE['ever']:
                _LIVE['gone_at'] = time.time()
        return self._json({'ok': True})


# ---- client liveness: the server stops when its window does -------------------------------------
# The power button was removed on 2026-09-05 (the author: it sat one fat finger from Settings, and it made
# a user manage a "server" they never asked to run). So the process has to notice for itself.
#
# TWO SIGNALS, because neither works alone:
#   * a BEACON on pagehide — immediate and exact, but it fires on F5 too, so it can only ever ARM a
#     countdown, never stop the process where it stands;
#   * a HEARTBEAT — catches a browser that died without saying anything, but it cannot be the fast
#     path: Chromium throttles a hidden window's timers to roughly once a minute, and a grace period
#     long enough to survive that is far too long to wait on a real close.
#
# So the beacon arms a countdown and any heartbeat cancels it. A refresh checks back in within about
# a second. A MINIMISED window never sends a beacon at all, so throttling cannot end it — only the
# staleness backstop can, and that is three throttled beats away.
#
# THE NUMBERS ARE SET BY THE TWO-TAB CASE, not by the close: when one of two tabs closes, the
# survivor's next heartbeat has to land INSIDE the countdown, so the grace must be comfortably
# LONGER than the beat, never shorter. CLIENT_BEAT_S must stay in step with app.js's CLIENT_BEAT_MS.
_LIVE = {'seen': 0.0, 'gone_at': None, 'ever': False}
_LIVE_LOCK = threading.Lock()
CLIENT_BEAT_S = 5          # how often the page checks in
CLIENT_GRACE_S = 15        # after a beacon, how long to wait for anyone to say otherwise
CLIENT_STALE_S = 180       # heard nothing at all: assume the browser died without a beacon
WATCHDOG_TICK_S = 2        # how often the loop below looks
# A tick that took THIS much longer than it asked for means the machine was suspended, not that the
# loop was slow. Generous on purpose: ordinary jitter is milliseconds, and the cost of being wrong
# is one extra grace period, which is nothing.
SUSPEND_GAP_S = 10


def _watchdog_tick(now, slept):
    """Decide whether the client is gone. Returns True when the process should stop.

    `slept` is how long the caller's sleep ACTUALLY took, and it is the whole reason this takes
    arguments: it is the only way to tell a suspended laptop from a dead browser.

    THE SLEEP BUG. Both timeouts here are wall-clock, and a sleeping laptop suspends this process
    along with the browser. On wake, `time.time()` has jumped by however long the lid was shut, so
    `now - seen` is hours and the staleness rule fires within two seconds -- before a browser that
    is also just waking (and whose timers Chromium throttles besides) can possibly check in. The
    app was dead every time the author opened the lid, and it looked like the close-on-window-gone
    feature misfiring. It was the backstop BEHIND that feature, reading a suspend as a dead client.

    So a detected suspend resets the clocks and lets the ordinary rules run again from the wake.
    Nothing is lost by being wrong: a browser that really did die during the sleep is caught by the
    same staleness rule three minutes later. `gone_at` is RE-ARMED rather than cleared, so a window
    closed just before the lid still shuts the server down -- it simply gets its grace period
    measured from the wake, which is the only moment a reloading page could answer from."""
    with _LIVE_LOCK:
        if not _LIVE['ever']:
            return False
        if slept > WATCHDOG_TICK_S + SUSPEND_GAP_S:
            _LIVE['seen'] = now
            if _LIVE['gone_at'] is not None:
                _LIVE['gone_at'] = now
            return False
        gone = _LIVE['gone_at']
        return ((gone is not None and now - gone > CLIENT_GRACE_S)
                or now - _LIVE['seen'] > CLIENT_STALE_S)


def _client_watchdog():
    """Stop the process once its window is gone.

    NEVER BEFORE A WINDOW HAS EVER APPEARED. The server binds the port and start.bat only then
    launches the browser, so there is always a gap at boot with no client in it — and on a cold
    start behind a slow browser that gap can be seconds. Exiting into it would make the app fail to
    start, occasionally, for reasons nobody could see."""
    # MEASURED FROM THE LAST TICK, NOT FROM BEFORE THIS SLEEP. Timing only the sleep leaves the rest
    # of the loop unwatched, and a suspend landing in that gap is invisible: the next tick then sees
    # an ordinary 2-second sleep next to a `seen` from before the lid closed, and exits on staleness
    # -- the original bug, surviving in a window a millisecond wide. Carrying the previous timestamp
    # forward makes the measured span the WHOLE iteration, so there is no moment a suspend can hide
    # in. Rare is not the same as impossible, and this one costs a line.
    last = time.time()
    while True:
        time.sleep(WATCHDOG_TICK_S)
        now = time.time()
        slept, last = now - last, now
        if not _watchdog_tick(now, slept):
            continue
        # The two rules the power button answered to, kept for the same reasons. A job holds the
        # exit off rather than cancelling it: the loop comes back every 2s, so the process stops on
        # its own once the scan finishes, with nobody watching.
        if _job_running():
            continue
        # Complete anything still inside its undo window. Closing the window IS the answer to "did
        # you mean it?" — dropping the queue would silently un-delete files you asked to bin, and
        # the grid stopped showing them the moment you did.
        _flush_all_pending()
        os._exit(0)


def main():
    ensure_library()                         # create the merged DB + run the one-time merge if needed
    if ACTIVE['key']:
        # Index only if this root has never been scanned; a known root loads instantly.
        if os.path.isdir(ACTIVE['path']) and not _root_has_images(ACTIVE['key']):
            print(f"Indexing {ACTIVE['path']} ...")
            run_scan()
        else:
            print(f"Active root: {ACTIVE['path']}")
    else:
        print("No output folder set yet — choose one in the browser window that opens.")
    port = _chosen_port()
    # SAY WHICH PORT AND WHY, on stderr, because stderr is the only stream anyone ever sees: the
    # server runs hidden and start.bat catches stderr into data/viewer.log, then prints its tail
    # when we die like this. An unhandled OSError here used to produce a traceback ending in
    # "[WinError 10048]", which names no port, no app and no fix.
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as e:
        # BOTH WINDOWS ANSWERS COUNT. A busy port here reports 10013 (EACCES) far more often than
        # the 10048 anyone would go looking for, because HTTPServer sets SO_REUSEADDR: which of the
        # two you get depends on how the OTHER program opened the port. Catching only 10048 would
        # have caught the rarer half of the exact case this exists for.
        in_use = getattr(e, 'winerror', None) in (10013, 10048) \
            or e.errno in (errno.EADDRINUSE, errno.EACCES)
        if in_use:
            # NAME THE FILE, WHERE IT IS, AND WHAT GOES IN IT. This message is read by someone
            # whose app just refused to start, with no app to ask -- so "put a different port
            # somewhere" is not enough, and it used to say config.json, which is no longer true.
            print(f"Port {port} is already being used by another program, so VV Curator cannot "
                  f"start.\nClose whatever is using it, or make a file called port.txt beside "
                  f"start.bat holding just a number, e.g. 8771.", file=sys.stderr)
        else:
            print(f"VV Curator could not open port {port}: {e}", file=sys.stderr)
        raise SystemExit(1)
    url = f"http://127.0.0.1:{port}"
    print(f"VV Curator running at {url}  (active root: {ACTIVE['path']})")
    if os.environ.get('CV_OPEN_BROWSER') == '1':
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_client_watchdog, daemon=True).start()
    httpd.serve_forever()


if __name__ == '__main__':
    main()
