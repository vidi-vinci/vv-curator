"""model_hash.py — resolve a ComfyUI ckpt_name/lora_name to a file under the configured models
directory and return its Civitai **AutoV2** hash (first 10 hex chars of the file's SHA-256), cached
so a multi-GB checkpoint is hashed at most once.

Pure standard library. The app process reads the models directory here at runtime *only* while
serving an export request — it is deliberately separate from the scanner and is never used to walk
the library. `models_dir` points at the ComfyUI models root (standard `checkpoints/` + `loras/`
subdirs); resolution is by the exact category-relative path ComfyUI stores, with a recursive
basename fallback for a file that was moved but kept its name. A name with no match yields no hash
(the resource still appears as text) — it never hashes a *different* file, so links can't be wrong.
"""
import hashlib
import os
import sqlite3
import threading

_lock = threading.Lock()


def _cache_conn(cache_path):
    conn = sqlite3.connect(cache_path)
    conn.execute("CREATE TABLE IF NOT EXISTS file_hashes "
                 "(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT)")
    return conn


def _sha256(path, progress=None, should_stop=None):
    """SHA-256 of a file. `progress(bytes_read_so_far)` is called after each block;
    `should_stop()` truthy aborts and returns None (no partial cache)."""
    h = hashlib.sha256()
    read = 0
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            if should_stop and should_stop():
                return None
            h.update(block)
            read += len(block)
            if progress:
                progress(read)
    return h.hexdigest()


SAVER_DB_REL = os.path.join('custom_nodes', 'comfy_vv_saver', 'model_hashes.db')


def saver_caches(models_dirs):
    """The comfy_vv_saver node's own hash caches, found from the configured models folders.

    That node hashes models with *this* function into *this* schema as it saves an image, so the
    expensive read has usually already happened before you ever export. Its db sits at
    <ComfyUI>/custom_nodes/comfy_vv_saver/model_hashes.db and the models folder is <ComfyUI>/models,
    so one level up locates it — no configuration. Read live, so new generations count immediately.
    """
    out = []
    for md in models_dirs:
        if not md:
            continue
        p = os.path.join(os.path.dirname(os.path.normpath(md)), SAVER_DB_REL)
        if p not in out and os.path.isfile(p):
            out.append(p)
    return out


def _lookup(conn, ap, st):
    """A hash for this file from one cache: the exact path if it's there, else a row for the same
    basename at the same exact byte size — i.e. the same model reached by another path.

    mtime is ignored for the name+size case, since a copy legitimately differs. That pair is the
    identity claim; for multi-GB .safetensors a collision isn't a realistic worry.
    """
    row = conn.execute("SELECT sha256 FROM file_hashes WHERE path=? AND size=? AND mtime=?",
                       (ap, st.st_size, st.st_mtime)).fetchone()
    if row:
        return row[0]
    target = os.path.basename(ap).lower()
    for p, sha in conn.execute("SELECT path, sha256 FROM file_hashes WHERE size=? AND path<>?",
                               (st.st_size, ap)):
        if sha and os.path.basename(p).lower() == target:
            return sha
    return None


def _known_hash(cache_path, ap, st, extra_dbs=()):
    """Any SHA-256 for this file we can get without reading it — ours first, then the saver's.
    Anything found elsewhere is stored under this path so the next lookup is a plain hit."""
    with _lock:
        conn = _cache_conn(cache_path)
        try:
            sha = _lookup(conn, ap, st)
            if not sha:
                for db in extra_dbs:
                    try:
                        other = sqlite3.connect(db)
                    except sqlite3.Error:
                        continue
                    try:
                        sha = _lookup(other, ap, st)
                    except sqlite3.Error:        # not a hash cache — just decline
                        sha = None
                    finally:
                        other.close()
                    if sha:
                        break
            if sha:
                conn.execute("INSERT OR REPLACE INTO file_hashes(path,size,mtime,sha256) "
                             "VALUES(?,?,?,?)", (ap, st.st_size, st.st_mtime, sha))
                conn.commit()
            return sha
        finally:
            conn.close()


def cached_hash(path, cache_path, extra_dbs=()):
    """Return a SHA-256 for `path` if one can be had without hashing, else None. Lets a caller tell
    up front whether a (slow) read is still needed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _known_hash(cache_path, os.path.abspath(path), st, extra_dbs)


def sha256_cached(path, cache_path, progress=None, should_stop=None, extra_dbs=()):
    """Full SHA-256 of `path`, cached in `cache_path` keyed by (abspath, size, mtime) so an
    unchanged file is hashed once. Returns None if unreadable or aborted via should_stop."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    ap = os.path.abspath(path)
    known = _known_hash(cache_path, ap, st, extra_dbs)
    if known:
        return known
    digest = _sha256(path, progress, should_stop)   # slow read stays OUT of the lock
    if digest is None:                              # aborted — don't cache a partial
        return None
    with _lock:
        conn = _cache_conn(cache_path)
        try:
            conn.execute("INSERT OR REPLACE INTO file_hashes(path,size,mtime,sha256) VALUES(?,?,?,?)",
                         (ap, st.st_size, st.st_mtime, digest))
            conn.commit()
        finally:
            conn.close()
    return digest


# A model is tagged 'checkpoints' by the graph reader, but a Flux/Krea UNETLoader model lives in
# diffusion_models/ (or legacy unet/) — so try those folders too before falling back to walking the
# whole models root, which is slow and can match a same-named file in an unrelated category.
CATEGORY_DIRS = {'checkpoints': ('checkpoints', 'diffusion_models', 'unet')}

# What a model file is called on disk, for the extension-less fallback in resolve_file.
MODEL_EXTS = {'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.sft', '.gguf'}


def resolve_file(models_dir, category, raw_name):
    """Find the on-disk file for ComfyUI's stored `raw_name` (e.g.
    'Illustrious\\amanesseWorks_v20.safetensors'), relative to <models_dir>/<category>.
    `category` is 'checkpoints' or 'loras'. Returns a path or None."""
    if not models_dir or not raw_name:
        return None
    rel = raw_name.replace('\\', '/').lstrip('/')
    parts = [p for p in rel.split('/') if p not in ('', '.', '..')]
    if not parts:
        return None
    bases = [os.path.join(models_dir, c) for c in CATEGORY_DIRS.get(category, (category,))]
    bases.append(models_dir)
    for base in bases:                            # exact relative-path join
        p = os.path.join(base, *parts)
        if os.path.isfile(p):
            return p
    target = parts[-1].lower()                    # recursive basename fallback (moved, same name)
    for base in bases:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if fn.lower() == target:
                    return os.path.join(dirpath, fn)
    # STORED WITHOUT ITS EXTENSION. ComfyUI Lora Manager records 'MyLora', not
    # 'MyLora.safetensors', so neither join above can ever hit and the LoRA reads fine while its
    # hash stays unresolvable -- which on Civitai is the difference between a named resource and a
    # linked one. Only tried when the stored name carries no model extension of its own, so a real
    # filename is never second-guessed.
    if os.path.splitext(target)[1] in MODEL_EXTS:
        return None
    for base in bases:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                stem, ext = os.path.splitext(fn.lower())
                if stem == target and ext in MODEL_EXTS:
                    return os.path.join(dirpath, fn)
    return None


def resolve_file_multi(models_dirs, category, raw_name):
    """First matching file across an ordered list of models folders, or None."""
    for md in models_dirs:
        if md:
            p = resolve_file(md, category, raw_name)
            if p:
                return p
    return None


def autov2(models_dir, category, raw_name, cache_path, extra_dbs=()):
    """AutoV2 hash (first 10 hex of SHA-256) for a resolved resource, or None if not found."""
    path = resolve_file(models_dir, category, raw_name)
    if not path:
        return None
    digest = sha256_cached(path, cache_path, extra_dbs=extra_dbs)
    return digest[:10] if digest else None


def autov2_multi(models_dirs, category, raw_name, cache_path, extra_dbs=()):
    """Try each folder in order (e.g. this library's override, then the global default) and return
    the first AutoV2 hash that resolves — so a resource can live in either system's models folder."""
    for md in models_dirs:
        if md:
            h = autov2(md, category, raw_name, cache_path, extra_dbs)
            if h:
                return h
    return None


def check_dir(models_dir):
    """Is a configured models folder reachable, and does it have checkpoints/ + loras/ subdirs?
    Drives the 'found / not found' hint so the user can tell when a path isn't mounted here."""
    md = (models_dir or '').strip()
    if not md or not os.path.isdir(md):
        return {'exists': False, 'checkpoints': False, 'loras': False}
    return {'exists': True,
            'checkpoints': os.path.isdir(os.path.join(md, 'checkpoints')),
            'loras': os.path.isdir(os.path.join(md, 'loras'))}
