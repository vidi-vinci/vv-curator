"""
index_db.py — scan a root folder of ComfyUI outputs into a searchable SQLite index.

Walks the tree, extracts metadata via comfy_meta, reads image dimensions from
file headers (no Pillow needed), and upserts rows into SQLite with an FTS5
full-text index over the prompt/model. Incremental: unchanged files (same
mtime+size) are skipped; vanished files are pruned.

Thumbnails are handled in a separate step (needs Pillow); the `thumb` column
is reserved here.
"""
import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import struct
import time

import comfy_meta
import thumbs as thumbs_mod

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
# THE LINE THAT USED TO BE HERE SAID "ComfyUI metadata lives in PNG; others index as no meta",
# and it was wrong for two years. ComfyUI writes the same graph into a JPEG or WebP's EXIF, so
# every WebP it has ever saved — including every animated one a video workflow produces — was
# indexed blank while carrying its full prompt, model, sampler and seed. comfy_meta.extract
# reads both now. The lesson is about the comment as much as the code: an assumption written
# down as fact stops anyone checking it.
VIDEO_EXTS = {'.mp4', '.webm', '.mov'}                   # true video: poster thumbnail + <video> playback
# Audio: no picture at all, so the card is drawn from the file's own data (see thumbs.audio_shape)
# rather than from a thumbnail. Only MP3 carries readable ComfyUI metadata here; the rest index and
# play with none, the same way a JPEG does.
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.opus', '.m4a'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS         # everything the scanner walks
# Folders never indexed: the manual "recycle bin" fallback used on network shares
# (see server._move_to_recycle_folder) — must stay hidden from the library.
SKIP_DIRS = {'_ToRecycle'}
# Tolerance (seconds) for folder-mtime comparison in changes_detected(). On network shares
# (SMB) os.stat can return a folder mtime that jitters by tens of ms between the value stored
# during a scan and the value read back — a 1µs tolerance flagged that noise as "changed", so
# the ↻ rescan icon never cleared. 2s is the classic FAT/SMB granularity (cf. robocopy /FFT);
# a real change bumps the folder mtime to "now" (seconds-to-days), so this never masks one.
MTIME_TOL = 2.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,
    rel_path     TEXT NOT NULL,
    folder       TEXT NOT NULL,
    filename     TEXT NOT NULL,
    ext          TEXT NOT NULL,
    mtime        REAL NOT NULL,
    size         INTEGER NOT NULL,
    width        INTEGER,
    height       INTEGER,
    model        TEXT,
    model_name   TEXT,
    model_type   TEXT,
    vae_name     TEXT,                         -- the VAE that decoded the picture (comfy_meta._resolve_vae)
    positive     TEXT,
    negative     TEXT,
    method       TEXT,
    has_meta     INTEGER NOT NULL DEFAULT 0,
    thumb        TEXT,
    loras        TEXT,
    motion       INTEGER NOT NULL DEFAULT 0,   -- 1 = animates (video, or animated gif/webp)
    group_id     TEXT,                         -- shared key for a merged still+video pair (NULL = ungrouped)
    set_id       TEXT,                         -- comfy_vv_saver's vv_set_id: shared by every image of one run
    set_stage    TEXT,                         -- comfy_vv_saver's vv_set_stage: Raw/Detail/Refine/Upscale/Final
    -- Generation settings, read from the BASE sampler only (comfy_meta._pick_base_sampler): the
    -- one that made the original image, before any detailer or upscaler. A multi-stage run saves
    -- several images and every one carries the WHOLE graph, so per-stage settings are not
    -- available from the file; the base generation is the fact they genuinely share. Read from the
    -- file, so these are rebuilt by a rescan and must NOT be added to the survives-rescan list.
    gp_steps     INTEGER,
    gp_cfg       REAL,
    gp_sampler   TEXT,
    gp_scheduler TEXT,
    gp_seed      INTEGER,                        -- can exceed 2^53; kept whole, never rounded
    gp_seed_s    TEXT,                           -- the rare seed too big even for INTEGER; see _split_seed
    -- Audio. A song has no width/height and no thumbnail, so these are what its card is drawn from.
    -- All read from the file, so a rescan rebuilds them and none belong on the survives-rescan list.
    duration     REAL,                         -- seconds, measured by decoding (headers lie: see thumbs.audio_shape)
    peaks        TEXT,                         -- JSON [0..100] loudness over time; the waveform the card draws
    lyrics       TEXT,                         -- the words, kept apart from `positive` (which holds the style caption)
    song_name    TEXT,                         -- the name typed into the VV Naming node, recovered from the filename
    song_bpm     INTEGER,                      -- these four are read out of the caption's PROSE, so any of them
    song_key     TEXT,                         -- can be NULL on a song that simply never stated it. NULL means
    song_genre   TEXT,                         -- "not stated", never "unknown" — the card omits what it hasn't got
    song_vocal   TEXT,                         -- male / female / androgynous
    has_cover    INTEGER NOT NULL DEFAULT 0,   -- the file carries embedded cover art. Recorded at
                                               -- scan because thumbnails are made lazily, so "has a
                                               -- thumb" answers a different question than "has art"
    note         TEXT,                          -- free-text per-image note (survives rescan; UPDATE never touches it)
    last_opened  REAL,                          -- when this image was last OPENED in the detail view (NULL = never). The deliberate-look signal. Survives rescan.
    last_seen    REAL,                          -- when this image was last on screen: written on OPEN. NULL = never. Survives rescan. The "Least seen" sort it fed was retired (CLN-1) along with the per-card grid tracking that also wrote it; the columns are kept because viewing history cannot be rebuilt.
    shuffle_key  INTEGER,                       -- a fixed random 0..1e9 per image, dealt once. The Random sort orders by (shuffle_key * seed) % prime: stable within a shuffle so paging can't repeat or skip, different for every seed. Survives rescan.
    root_id      TEXT,                          -- which root (library) this image belongs to; NULL only pre-merge
    indexed_at   REAL NOT NULL,
    reader_ver   INTEGER NOT NULL DEFAULT 0     -- which READER_VERSION read this row. Behind the current one = re-read it the next time this file is opened. 0 = read before this was recorded.
);
CREATE INDEX IF NOT EXISTS idx_images_folder     ON images(folder);
CREATE INDEX IF NOT EXISTS idx_images_model_name ON images(model_name);
CREATE INDEX IF NOT EXISTS idx_images_mtime      ON images(mtime);
-- idx_images_group is created in _migrate (group_id is a later-added column, so it can't be
-- indexed here against pre-existing tables that lack the column yet).

-- Full-text search index. Note: negative prompt is intentionally NOT indexed
-- (its boilerplate pollutes results); filename AND folder ARE indexed so Include/Exclude
-- search finds images by their name or the folder they live in.
-- LYRICS ARE INDEXED (schema 5). A song's style caption already lived in `positive` and was
-- findable; its words were not, so a song could only be searched for by how it sounds and never
-- by what it says. The trade is the one the negative prompt lost: a song now answers ordinary
-- word searches too. Put to the author before building it, since it is his result lists that get the
-- extra cards -- and unlike negative-prompt boilerplate, lyrics are the thing you would search for.
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
    positive, model_name, filename, folder, lyrics,
    content='images', content_rowid='id', tokenize='unicode61'
);

-- Reserved for Phase 2 (WD14 / VLM auto-tagging):
CREATE TABLE IF NOT EXISTS tags (
    image_id INTEGER NOT NULL,
    tag      TEXT NOT NULL,
    source   TEXT NOT NULL,      -- 'wd14', 'vlm', 'manual'
    score    REAL,
    PRIMARY KEY (image_id, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- pyiqa Quality score (one row per scored image).
CREATE TABLE IF NOT EXISTS quality (
    image_id INTEGER PRIMARY KEY,
    reward   REAL       -- pyiqa: no-reference image-quality reward (higher = better)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def png_size(path):
    """(width, height) from PNG IHDR without decoding pixels."""
    try:
        with open(path, 'rb') as f:
            if f.read(8) != b'\x89PNG\r\n\x1a\n':
                return (None, None)
            f.read(4)                      # IHDR length
            if f.read(4) != b'IHDR':
                return (None, None)
            w, h = struct.unpack('>II', f.read(8))
            return (w, h)
    except Exception:
        return (None, None)


def image_size(path):
    """(width, height) for any supported image. PNG uses the fast header parser above;
    other formats (jpg/webp) fall back to Pillow, which reads dimensions from the header
    without decoding pixels. Returns (None, None) if it can't be determined."""
    if os.path.splitext(path)[1].lower() == '.png':
        wh = png_size(path)
        if wh[0]:
            return wh
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)


def image_probe(path):
    """(width, height, animated) for a still-image file. Reuses image_size for the
    dimensions; `animated` is True only for multi-frame gif/webp (Pillow n_frames > 1)."""
    w, h = image_size(path)
    animated = False
    if os.path.splitext(path)[1].lower() in ('.gif', '.webp'):
        try:
            from PIL import Image
            with Image.open(path) as im:
                animated = getattr(im, 'n_frames', 1) > 1
        except Exception:
            animated = False
    return (w, h, animated)


# Empty metadata record for non-PNG media (videos), so we skip the PNG-chunk read entirely.
_EMPTY_META = {'loras': [], 'positive': None, 'negative': None,
               'model': None, 'model_name': None, 'method': None}


# Role-word segments dropped when pairing a still with its video, so names that differ only by
# their role (e.g. '..._BASE_00001.png' vs '..._INTERP_00001.mp4') still pair. Matched as WHOLE
# lowercased segments, so a word like 'wanBase' (one token) is left untouched. Extend as needed.
# 'audio' is here so VHS Video Combine's muxed '..._00001-audio.mp4' pairs with its still (VHS
# hardcodes that suffix); the silent '..._00001.mp4' pairs too, so keeping either collapses to one card.
PAIR_ROLE_WORDS = {'base', 'interp', 'audio'}

# Role labels that mark the members of an "image set" — several stills saved from ONE generation
# that share a prefix and differ only by an appended role token (e.g. 'X_1 MAIN_', 'X_2 DET_',
# 'X_3 REFINE_'). Single source of truth: to support other patterns later, widen this tuple (or
# promote it to config — no schema change, just rescan to recompute groups).
SET_ROLE_WORDS = ('main', 'det', 'refine')

# Pipeline order of comfy_vv_saver's stages, for laying out a set's cull panes. Filenames can't be
# sorted for this any more: the old '1 MAIN/2 DET/3 REFINE' carried an ordinal, but 'Raw/Detail/
# Refine/Upscale/Final' sorts alphabetically into nonsense. Legacy roles map into this order via
# LEGACY_STAGE_ALIASES; a stage NOT in this tuple is a custom one and sorts BEFORE 'raw' — see
# _stage_rank.
SET_STAGE_ORDER = ('raw', 'detail', 'refine', 'upscale', 'final')

# The legacy filename roles are the SAME pipeline stages under older names. Naming that here rather
# than inside a sort key matters: anything comparing two sets by their stages (the folder cull's
# shape matching) must see a legacy MAIN/DET/REFINE set and a stamped Raw/Detail/Refine set as the
# same shape, or one mixed library quietly splits into two families that never match each other.
LEGACY_STAGE_ALIASES = {'main': 'raw', 'det': 'detail'}   # 'refine' is already a stage name


def canonical_stage(stage):
    """One name per pipeline stage, whatever it was called when the file was written."""
    s = (stage or '').strip().lower()
    return LEGACY_STAGE_ALIASES.get(s, s)


def _stage_rank(stage):
    """Sort key for a set member's stage label. Legacy filename roles (main/det/refine) still land
    in pipeline order.

    A NAMED stage the vocabulary lacks — anything typed into the saver's `Custom...` field — is
    treated as the FIRST stage of the pipeline, ahead of 'raw'. The author's call, 2026-09-07, and it is
    the reading his own usage asks for: he types words like "First" for the shot a run starts from,
    so the escape hatch is used for what comes BEFORE the vocabulary, not after it. Sorting those
    last (what happened until now) put the earliest image at the end of the panes and let a set be
    ordered arbitrarily. Two DIFFERENT custom stages in one set still tie here and fall back to
    filename order; nothing in the name says which of two unknowns came first.

    A member with NO stage at all still sorts last. It is not an unnamed first stage, it is a file
    that never said, and putting it in front of 'raw' would be inventing a claim.
    """
    s = canonical_stage(stage)
    if s in SET_STAGE_ORDER:
        return SET_STAGE_ORDER.index(s)
    if not s:
        return len(SET_STAGE_ORDER)
    return -1


def _split_segs(filename):
    """Lowercased stem split into segments on separators, empties dropped."""
    stem = os.path.splitext(filename)[0].lower()
    return [s for s in re.split(r'[\s._-]+', stem) if s]


def _pair_key(filename):
    """Normalized key for matching a still to its video: the stem lowercased, split into segments
    on separators, with any role-word segment removed, then rejoined. So 'X_BASE_00001.png' and
    'X_INTERP_00001.mp4' share a key; exact-name pairs (no role word) match unchanged."""
    return '_'.join(s for s in _split_segs(filename) if s not in PAIR_ROLE_WORDS)


def _set_role(filename):
    """The image-set role label present in a filename (e.g. 'refine'), or None if none of
    SET_ROLE_WORDS appears as a whole segment."""
    for s in _split_segs(filename):
        if s in SET_ROLE_WORDS:
            return s
    return None


def _set_key(filename):
    """Key that all members of one image set share: the stem with the role label AND its
    directly-preceding short ordinal segment (the '1'/'2'/'3' in '1 MAIN'/'2 DET'/'3 REFINE')
    dropped, so members differing ONLY by role collapse. Everything else — prefix and the trailing
    ComfyUI counter — is kept, so unrelated generations stay distinct (conservative, like _pair_key).
    The <=2-digit guard on the ordinal avoids eating a real number like a 4-digit year."""
    out = []
    for s in _split_segs(filename):
        if s in SET_ROLE_WORDS:
            if out and out[-1].isdigit() and len(out[-1]) <= 2:
                out.pop()
            continue
        out.append(s)
    return '_'.join(out)


# ---- model type -------------------------------------------------------------------------------
# The model's FAMILY — Pony, Illustrious, Flux, SDXL. Stored per image rather than derived in SQL on
# every query, which is what it used to be: the facet and the filter both ran a CASE/instr over the
# model path for every row, every time. Storing it makes the rule arbitrarily smarter AND the
# queries cheaper, and the only cost is that a library needs one Rebuild metadata to fill it.
#
# THE VOCABULARY IS THE USER'S OWN. A model's path carries its folder — `Pony\thing.safetensors` —
# and that folder is the family, because the user put it there. So the folders present in a library
# ARE the taxonomy, and a model with no folder is matched against those same names appearing in its
# FILENAME. `flux_1Dev` and `duchaitenPonyReal_ponyRealV11Fix` both name their family; they just sit
# loose in the checkpoints directory, which is what put 14,185 of the author's images under "(none)".
#
# A small built-in list rides alongside, for a family that appears in names but never as a folder.
# Deliberately short: every entry is a chance to be confidently wrong, and the user's own folder
# names cost nothing and cannot be.
_MODEL_FAMILY_HINTS = ('pony', 'illustrious', 'flux', 'sdxl', 'krea', 'wan', 'ltx', 'minimax',
                       'noobai', 'animagine', 'qwen', 'hunyuan')
# A trailing version as its OWN segment: "krea 2" -> "krea", "SDXL v1.5" -> "SDXL". Glued digits are
# left alone here — see _GLUED_TAIL, which handles those and needs the library's vocabulary to do it
# safely. Stripping them unconditionally would merge families rather than versions: SD15 is not SD.
_VERSION_TAIL = re.compile(r'[ _\-]+v?\d+(?:[._]\d+)*\s*$', re.I)
# A GLUED trailing version: the "2" of "krea2", the "XL" of "PonyXL". Stripped REPEATEDLY by
# _fold_stem, so "ponyxl2" reaches "pony". Only ever applied when the stem it leaves is already a
# family in this library — see the fold in recompute_model_types.
_GLUED_TAIL = re.compile(r'(?:\d+|xl)$', re.I)
# STEMS NOTHING MAY FOLD INTO, however much the library seems to invite it. "SD" is a prefix shared
# by architectures that are not versions of each other — SD1.5, SDXL and SD3.5 cannot read each
# other's LoRAs and are not interchangeable in a workflow — so a bare "SD" bucket would merge the
# one distinction in this whole taxonomy that most needs keeping. The author's call, 2026-09-10, choosing
# the XL fold "but protect SD".
#
# Named rather than inferred. A length test would block "SD" today by coincidence and let the next
# two-letter family through, and the reason here is about what SD *means*, not how long it is.
_FOLD_NEVER = frozenset({'sd'})


def _fold_stem(key):
    """Strip glued version suffixes from a family key: 'ponyxl' -> 'pony', 'krea2' -> 'krea'.

    Repeats until stable, so a name carrying both ("ponyxl2") reaches the same place as one
    carrying either. Returns '' when nothing is left, which the caller treats as "do not fold".
    """
    prev = None
    out = key
    while out != prev:
        prev = out
        out = _GLUED_TAIL.sub('', out).rstrip(' _-')
    return out


def _model_top_folder(model_path):
    """The folder a checkpoint sits in — the family — or '' when it sits loose."""
    if not model_path:
        return ''
    p = str(model_path).replace('\\', '/')
    return p.rsplit('/', 1)[0].split('/')[0] if '/' in p else ''


def normalize_model_type(name):
    """Collapse a family name to its canonical form: trimmed, with a trailing version dropped."""
    out = _VERSION_TAIL.sub('', (name or '').strip())
    return out.strip()


def _family_in_name(filename, vocab):
    """The family named INSIDE a model's filename, or ''.

    Boundary-aware on purpose. A bare substring test is where "confidently wrong" lives, so a token
    only counts at the start, after a non-letter, or at a camelCase hump — which is what makes
    `duchaitenPonyReal` a Pony and stops `influx` becoming a Flux.
    """
    stem = os.path.splitext(os.path.basename(str(filename or '')))[0]
    best = ''
    for token in vocab:
        if not token:
            continue
        for m in re.finditer(re.escape(token), stem, re.I):
            i = m.start()
            before = stem[i - 1] if i else ''
            at_boundary = (not before) or (not before.isalpha()) or \
                          (before.islower() and stem[i].isupper())
            # Prefer the longest match, so "sdxl" wins over a hypothetical "sd".
            if at_boundary and len(token) > len(best):
                best = token
            break
    return best


def recompute_model_types(conn, root_id=None):
    """Fill images.model_type for every row that has a model. Runs after a scan, like the group pass.

    Whole-library, never root-scoped: the vocabulary is drawn from every library's folders, so a
    family the user only foldered in one library still classifies their loose models everywhere.
    """
    # Tuples, not named columns: index_db never sets a row_factory, and assuming one here made this
    # raise TypeError at the very end of every scan — after the inserts, so the library looked
    # indexed while the column stayed empty. Caught end-to-end; a unit test with a friendlier
    # connection had passed.
    rows = conn.execute("SELECT id, model, model_name FROM images "
                        "WHERE model IS NOT NULL AND model <> ''").fetchall()
    rows = [(r[0], r[1], r[2]) for r in rows]
    if not rows:
        return 0
    # Pass 1: the folders the user actually uses, normalised. This is the taxonomy.
    #
    # ONE SPELLING PER FAMILY. `krea 2` normalises to `krea` while a sibling folder is `Krea`, and
    # two spellings would be two rows in the dropdown for one family — the very thing this is meant
    # to collapse. The winner is the spelling used by the most images (ties broken alphabetically),
    # so the answer is stable across runs rather than depending on row order.
    spellings = {}
    for _id, model, _name in rows:
        fam = normalize_model_type(_model_top_folder(model))
        if fam:
            spellings.setdefault(fam.lower(), {})
            spellings[fam.lower()][fam] = spellings[fam.lower()].get(fam, 0) + 1
    vocab = {k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, v in spellings.items()}
    for h in _MODEL_FAMILY_HINTS:
        vocab.setdefault(h, h)
    # A GLUED VERSION FOLDS INTO ITS STEM, BUT ONLY WHERE THE STEM IS ALREADY A FAMILY HERE.
    # The author, 2026-09-10: "consolidate models where possible so that, for example, Krea and krea2
    # become the same." `krea 2` already collapsed (a separated version — see _VERSION_TAIL);
    # `krea2` did not, and deliberately so, because the same unconditional strip turns SD15 into SD
    # and merges two families that are not one.
    #
    # THE VOCABULARY DECIDES, WHICH IS WHY THIS IS SAFE. `krea2` folds because `Krea` is a folder in
    # this library; `SD15` folds only if the user actually keeps an `SD` folder beside it, and stays
    # `SD15` otherwise — `sd` is not a family anyone has named. That is the same principle the
    # vocabulary itself runs on, applied one level further in: a family with nothing to fold into is
    # left exactly as it was, so the rule can under-merge but never invent a merge.
    #
    # The hints above count as vocabulary here too, which is deliberate: `Flux2` and `Qwen3` are
    # versions of families that exist whether or not this user foldered them.
    #
    # AN XL SUFFIX FOLDS THE SAME WAY, with one family held out. `PonyXL` is Pony; `SDXL` is not SD,
    # and neither is `SD15` — see _FOLD_NEVER for why that one is named rather than reasoned about.
    #
    # SORTED, because the loop below can WRITE a spelling and two keys could fold into the same
    # stem — iteration order would otherwise decide which one won, and that must not vary by run.
    fold = {}
    for key in sorted(vocab):
        stem = _fold_stem(key)
        if stem and stem != key and stem in vocab and stem not in _FOLD_NEVER:
            fold[key] = stem
            # A stem that exists only as a HINT has no spelling of the user's, so it would show the
            # hint's lowercase — `Qwen3` came out as `qwen` beside a column of capitalised folder
            # names. Take the spelling from the folder that is folding IN instead: `Qwen3` -> `Qwen`.
            if stem not in spellings:
                vocab[stem] = _fold_stem(vocab[key])
    # Longest first, so a specific family beats a shorter one contained in it. Built from the
    # UNFOLDED keys, because this list is matched against filenames: a file called `krea2_x.safetensors`
    # has to be found by the token `krea2` before the hit is folded to `krea` below.
    ordered = sorted(vocab, key=len, reverse=True)
    # One lookup for both passes: fold first, then take that family's chosen spelling.
    def _canon(key, fallback=''):
        k = (key or '').lower()
        return vocab.get(fold.get(k, k), fallback)
    # Pass 2: folder if there is one, else the family named inside the filename. Either way the
    # stored value goes through `vocab`, so every image of a family carries the identical string.
    updates = []
    for _id, model, name in rows:
        fam = normalize_model_type(_model_top_folder(model))
        if fam:
            fam = _canon(fam, fam)
        else:
            hit = _family_in_name(model or name, ordered)
            fam = _canon(hit) if hit else ''
        updates.append((fam, _id))
    conn.executemany("UPDATE images SET model_type=? WHERE id=?", updates)
    conn.commit()
    return len(updates)


def recompute_groups(conn, root_id=None):
    """Assign a shared group_id to four kinds of group, then clear everything else.
    Cheap pass over rows already in the DB; idempotent, so deletes/renames self-heal
    on the next scan. The kinds share the group_id column and are told apart at query time by
    whether the group contains a video member:
      0. RUN CODE   — >=2 files whose names carry the same `~vv……` code (comfy_meta.run_code_key).
      1. still+video PAIRS  — an image and a video whose names match after dropping PAIR_ROLE_WORDS.
      2. explicit SETS      — >=2 images stamped with the same comfy_vv_saver `set_id` (vv_set_id).
      3. image SETS         — >=2 stills sharing a _set_key with >=2 distinct SET_ROLE_WORDS roles.

    Order matters, and pass 0 leads because it is the only one that KNOWS. The other three infer a
    generation — from names lining up, or from an id a video cannot carry — where a run code is
    stamped by the saver into every file of the run, video and sidecar included. So it claims its
    rows first and the inference passes divide up what is left.

    Nothing below it changed when it was added, and that is the whole compatibility story: a file
    with no code is invisible to pass 0, so a library written before this existed groups by exactly
    the rules it always did. The two systems can also sit side by side in one folder without
    interfering, because a coded run and an uncoded one can never land in the same bucket.

    In the merged DB, grouping is scoped PER ROOT: `root_id` is part of every bucket key AND folded
    into the group_id hash, so two roots with identical folder/filename layouts never cross-merge and
    every group_id belongs to exactly one root (which lets the read-side group queries stay root-blind
    — a group_id is globally unique). Pass `root_id` to recompute just one root (a per-root rescan);
    pass None to recompute the whole table (the one-time merge)."""
    where = " WHERE root_id IS ?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    rows = list(conn.execute(
        "SELECT id, root_id, folder, filename, ext, set_id FROM images" + where, params))
    grouped_ids = {}          # id -> group_id

    # Pass 0: the run code carried in the filename. Everything after the code is noise — stage,
    # ComfyUI's counter, VHS's role words — so a run's stills, its videos and its sidecar all land
    # in one bucket without any of them having to predict what the others were named.
    #
    # NOT folder-scoped, matching pass 2 and for the same reason: the code is explicit rather than
    # inferred, so a run whose stages were written to different folders is still one run, and a
    # library reorganised by hand keeps its groups. Still scoped per ROOT, which the read side
    # relies on — a group_id belongs to exactly one root.
    cbuckets = {}
    for iid, rid, folder, filename, ext, _sid in rows:
        key = comfy_meta.run_code_key(filename)
        if key:
            cbuckets.setdefault((rid, key), []).append(iid)
    for (rid, key), members in cbuckets.items():
        if len(members) < 2:            # a lone file is just a file, not a group
            continue
        gid = hashlib.sha1(f"vvrun|{rid}|{key}".encode('utf-8')).hexdigest()[:12]
        for iid in members:
            grouped_ids[iid] = gid

    # Pass 1: still+video pairs. Skips anything pass 0 claimed — it used to run first and so had no
    # need to, and without the guard it would re-key a coded run by filename and undo the pass above.
    buckets = {}
    for iid, rid, folder, filename, ext, _sid in rows:
        if iid in grouped_ids:
            continue
        buckets.setdefault((rid, folder, _pair_key(filename)), []).append((iid, (ext or '').lower()))
    for (rid, folder, key), members in buckets.items():
        exts = {ext for _, ext in members}
        # Only merge a bucket that has BOTH a real image and a video sharing the key.
        if not (exts & VIDEO_EXTS) or not (exts & IMAGE_EXTS):
            continue
        gid = hashlib.sha1(f"{rid}|{folder}|{key}".encode('utf-8')).hexdigest()[:12]
        for iid, _ in members:
            grouped_ids[iid] = gid

    # Pass 2: explicit sets — every image of one comfy_vv_saver run carries the same set_id.
    # NOT scoped by folder (unlike the heuristics): the id is explicit and authoritative, so it
    # groups a run even if its stages were written to different folders. Still scoped per ROOT, to
    # keep the invariant the read side relies on — a group_id belongs to exactly one root.
    vbuckets = {}
    for iid, rid, folder, filename, ext, sid in rows:
        if iid in grouped_ids or not sid:
            continue
        vbuckets.setdefault((rid, sid), []).append(iid)
    for (rid, sid), members in vbuckets.items():
        if len(members) < 2:            # a lone stage is just an image, not a set
            continue
        gid = hashlib.sha1(f"vvset|{rid}|{sid}".encode('utf-8')).hexdigest()[:12]
        for iid in members:
            grouped_ids[iid] = gid

    # Pass 3: image sets by filename (stills only; skip rows already claimed above).
    sbuckets = {}
    for iid, rid, folder, filename, ext, _sid in rows:
        if iid in grouped_ids or (ext or '').lower() not in IMAGE_EXTS:
            continue
        role = _set_role(filename)
        if not role:
            continue
        sbuckets.setdefault((rid, folder, _set_key(filename)), []).append((iid, role))
    for (rid, folder, key), members in sbuckets.items():
        # Need >=2 files with >=2 DISTINCT roles, so a lone still or duplicate roles don't group.
        if len(members) < 2 or len({role for _, role in members}) < 2:
            continue
        gid = hashlib.sha1(f"set|{rid}|{folder}|{key}".encode('utf-8')).hexdigest()[:12]
        for iid, _ in members:
            grouped_ids[iid] = gid

    # Apply: set the new group_id where it changed, clear it where a row is no longer grouped.
    for r in conn.execute("SELECT id, group_id FROM images" + where, params):
        want = grouped_ids.get(r[0])
        if want != r[1]:
            conn.execute("UPDATE images SET group_id=? WHERE id=?", (want, r[0]))
    conn.commit()


SCHEMA_VERSION = 5  # bump when the FTS layout changes (5 = index song lyrics too)

# WHAT THIS BUILD CAN GET OUT OF A FILE. Every row records the version that read it, so a row read
# by an older build can be recognised and re-read — which is how a file heals itself the moment you
# open it, with nothing to press.
#
# BUMP THIS WHENEVER THE READER LEARNS SOMETHING: a new setting, a node shape it could not walk, a
# format it could not open, a value it used to get wrong. Every one of those has happened —
# generation settings and the VAE, stacked LoRA nodes, a video's length, song lyrics into the search
# index, the negative-prompt fix — and each left existing libraries showing blanks that nothing
# noticed. Forgetting the bump is not a broken build; it is a build whose improvement silently
# reaches new files only.
#
# DO NOT bump it for a change to how something is DISPLAYED, sorted or filtered. Nothing is re-read
# by those, so the bump would buy a re-read of the whole library for no new value.
#
# Rows written before this existed are stamped 0, i.e. "unknown age" — which is honest: they were
# read by whatever build was current at the time, and that is exactly the thing nobody recorded.
# 2 (2026-09-14): images downloaded from Civitai carry their checkpoint, LoRAs and VAE in a
# `Civitai resources` list instead of the `Model:` field, and that list was being shredded before
# anything could read it. Those rows hold a prompt and settings and no model, so they are worth
# re-reading -- which is what bumping this asks the app to offer.
# 3 (2026-09-14, hours after 2): an MP4 written by ffmpeg -- which is what VHS writes
# through, and VHS is the most used video node in ComfyUI -- keeps its graphs in the comment
# atom, and we read only QuickTime's indexed form. Every such video reported its duration and
# dimensions and nothing else. Bumped AGAIN rather than folded into 2, because a library
# rescanned in the hours between the two would otherwise never be offered the second fix.
# 4 (2026-09-17): the reader now walks BACK from the node that saved a file to find the sampler
# that made it, instead of picking the one with the longest positive prompt. That guess reported a
# song's settings from its cover art's sampler, and a picture's from whichever later pass gained a
# few quality tags. Prompt, model and VAE move to the walked answer; generation settings
# deliberately do not, that being a separate call. Bumped so a file is re-read when it is next
# OPENED -- lazily, one at a time, never as a job that starts on its own.
# 5 (2026-09-20): FLAC tracks read their own tags, and LoRA bundles are read whether or not they
# are written out as text. Both landed AFTER 4 was set on the 17th, so every affected row was
# already stamped 4 and the app would never have offered to re-read it -- a FLAC indexed before
# this would have stayed blank in the Details pane for good, with nothing on screen suggesting a
# rescan would help. Caught while writing the 1.2 notes, which promise the offer appears: the
# claim is what found the gap, so the notes were doing the job a claim is supposed to do.
READER_VERSION = 6

# How many files a forced run must actually read before its speed is worth remembering. Enough to
# level out a slow first folder, small enough that a modest library still produces a number.
RATE_MIN_FILES = 200


# Match server.DB_BUSY_TIMEOUT_MS. Writers in TWO processes share this file -- the scan here and
# every API write there -- and WAL allows exactly one at a time, so both sides have to be willing to
# wait or the loser reports "database is locked" for what was only ever a queue. Python's default
# gives up at 5s, and a scan's final commit outlasts that on a large library. Stated here rather
# than imported: index_db must not depend on server.
BUSY_TIMEOUT_MS = 30000


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Rebuild derived structures (the FTS index) when their layout changes.
    Cheap: the FTS is repopulated from the images table, no file re-scan."""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    ver = int(row[0]) if row else 1
    # add columns introduced after the first release
    img_cols = [r[1] for r in conn.execute("PRAGMA table_info(images)")]
    if 'loras' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN loras TEXT")
    if 'motion' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN motion INTEGER NOT NULL DEFAULT 0")
    if 'group_id' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN group_id TEXT")
    if 'note' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN note TEXT")
    # Generation settings from the base sampler. Added empty on purpose: they are read from the
    # file, so a plain rescan skips unchanged images and leaves them NULL. Rebuild metadata is what
    # backfills an existing library -- the same rule the set ids needed. Nothing is lost meanwhile;
    # the detail view simply omits the section until there is something to show.
    for _gcol, _gtype in (('gp_steps', 'INTEGER'), ('gp_cfg', 'REAL'), ('gp_sampler', 'TEXT'),
                          ('gp_scheduler', 'TEXT'), ('gp_seed', 'INTEGER'),
                          ('gp_seed_s', 'TEXT')):
        if _gcol not in img_cols:
            conn.execute(f"ALTER TABLE images ADD COLUMN {_gcol} {_gtype}")
    # RETIRED 2026-08-23 with the AI features. Unlike the LLM scan's data below, which was kept
    # dormant, these are DROPPED on the author's instruction: the features were never refined enough for
    # the stored values to be worth carrying. An extension pack that brings them back re-adds the
    # columns with ALTER TABLE ADD COLUMN, exactly as the block this replaces did.
    #
    # Six separate DROP COLUMNs rather than a table rebuild: SQLite handles the index and trigger
    # bookkeeping itself, where a hand-written rebuild would have to reproduce it. Each drop
    # rewrites the rows, so this is measured, not assumed -- see the note in docs/notes.
    for _aicol in ('motion_prompt', 'lm_response',
                   'medal_gold', 'medal_silver', 'medal_bronze', 'rank_seen'):
        if _aicol in img_cols:
            conn.execute(f"ALTER TABLE images DROP COLUMN {_aicol}")
    if 'vae_name' not in img_cols:
        # Added empty: it is read from the FILE, so a rescan fills it for changed files and a
        # Rebuild metadata fills it for the rest. Same deal every read-from-file column has had.
        conn.execute("ALTER TABLE images ADD COLUMN vae_name TEXT")
    if 'model_type' not in img_cols:
        # The model's FAMILY, derived and stored (see recompute_model_types). Added empty:
        # a Rebuild metadata fills it, like the generation settings before it.
        conn.execute("ALTER TABLE images ADD COLUMN model_type TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_model_type ON images(model_type)")
    # Audio columns. Added empty, like the generation settings above: they are read from the file,
    # so an existing library fills them when its songs are scanned (they're new files, so a plain
    # rescan picks them up) or on a Rebuild metadata. A library with no audio simply never uses them.
    for _acol, _atype in (('duration', 'REAL'), ('peaks', 'TEXT'), ('lyrics', 'TEXT'),
                          ('song_name', 'TEXT'), ('song_bpm', 'INTEGER'), ('song_key', 'TEXT'),
                          ('song_genre', 'TEXT'), ('song_vocal', 'TEXT'),
                          ('has_cover', 'INTEGER NOT NULL DEFAULT 0')):
        if _acol not in img_cols:
            conn.execute(f"ALTER TABLE images ADD COLUMN {_acol} {_atype}")
    # Which reader last read this row (see READER_VERSION). Existing rows default to 0 rather than
    # to the current version, and that is the deliberate choice: they were read by a build nobody
    # recorded, so calling them current would declare exactly the thing we cannot know and leave
    # today's stale rows stale forever. 0 means "re-read me the first time you open me", which costs
    # one read per file you actually look at, once.
    if 'reader_ver' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN reader_ver INTEGER NOT NULL DEFAULT 0")
    if 'last_opened' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN last_opened REAL")   # deliberate-look signal
    if 'last_seen' not in img_cols:
        # Last time the image was ON SCREEN, not just opened. Added for the "Least seen" sort, which
        # used last_opened — only ever written by a detail-view open, so browsing a library for hours
        # recorded nothing and every image you had scrolled past still counted as unseen.
        # Backfill from last_opened: an image you opened was certainly seen. That invents no history
        # (it is a strictly weaker claim than the row already carried); everything else stays NULL.
        # The sort is retired (CLN-1) and this migration is NOT: it is what an existing library's
        # history rests on, and a library that has never run it still needs the column to exist.
        conn.execute("ALTER TABLE images ADD COLUMN last_seen REAL")
        conn.execute("UPDATE images SET last_seen = last_opened WHERE last_opened IS NOT NULL")
    if 'shuffle_key' not in img_cols:
        # The Random sort's stable deal. ALTER cannot carry a non-constant DEFAULT, so the column
        # arrives empty and is filled here; scan()'s INSERT assigns one to every new row after that.
        # Bounded to ~1e9 on purpose: the sort multiplies this by a seed, and an unbounded
        # abs(random()) (63 bits) would overflow int64 and silently fall back to float compares.
        conn.execute("ALTER TABLE images ADD COLUMN shuffle_key INTEGER")
        conn.execute("UPDATE images SET shuffle_key = abs(random() % 1000000007) "
                     "WHERE shuffle_key IS NULL")
    if 'root_id' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN root_id TEXT")   # merged-DB: which library a row belongs to
    if 'set_id' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN set_id TEXT")      # comfy_vv_saver vv_set_id
    if 'set_stage' not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN set_stage TEXT")   # comfy_vv_saver vv_set_stage
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_group ON images(group_id)")  # new + existing DBs
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_size ON images(size)")        # sort-by-largest
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_root ON images(root_id)")             # merged-DB root filter
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_root_group ON images(root_id, group_id)")
    # The sidebar's two boot queries (server.api_tags), which held the rail dimmed for seconds
    # after the grid was usable. Both are COVERING indexes — the query is answered without ever
    # opening an images or tags ROW, which is the whole point: a row carries the prompt text, and
    # reading 100k of those off a cold disk to look at three small columns was the cost.
    #   · (tag, source, image_id): the tag list groups by tag and selects on source, which had no
    #     index at all, so it walked every tag row and then sorted. Now it reads in order.
    # A third, (root_id, id, note), served the Unreviewed count and went with it on 2026-08-19.
    #
    # THE COLUMN ORDER OF THE FIRST ONE IS LOAD-BEARING, and (source, tag, image_id) — the obvious
    # order for the query it was added for — made the GRID 26x slower (919ms -> 24.4s per page,
    # caught by bench_collapse.py). `source` has three distinct values, which is exactly the shape
    # SQLite's skip-scan looks for: the grid's three per-row tag subqueries (label / fav / hidden)
    # stopped using the primary key and started seeking `source='fav'` and scanning that whole
    # partition for an image_id. Leading with `tag` — thousands of distinct values — leaves no
    # skip-scan to find, so those subqueries keep the PK, and the tag list still gets its covering
    # scan. A low-cardinality leading column on a table that is probed by its PK elsewhere is the
    # trap; the benchmark is what catches it.
    # ONE FULL WALK AFTER THIS BUILD, so a video indexed before its size and length could be read
    # is actually VISITED. The staleness rule that re-reads such a row only fires for files the
    # scan looks at, and an ordinary refresh is usually a QUICK scan -- it stats folder signatures
    # and walks only the ones that moved. A finished folder never moves, which is precisely where
    # old videos live, so the rule could sit there indefinitely doing nothing.
    #
    # Reported 2026-08-25, after being told a plain refresh would do it: "I did have to
    # refresh metadata. The times didn't show up for LTX until i did so." He was right and the
    # advice was wrong. Worse than wrong for him specifically: Rebuild metadata is per-library, so
    # his other two libraries would still be waiting.
    #
    # Clearing the full-walk clock is the whole fix -- it is the existing signal for "a library
    # indexed by an older build", and run_scan already reads it. The next refresh walks everything
    # ONCE, skipping unchanged files as usual, so only the videos actually missing something are
    # re-read. No forced rebuild, and nothing to click.
    if not conn.execute("SELECT 1 FROM meta WHERE key='backfill:video_container'").fetchone():
        conn.execute("DELETE FROM meta WHERE key='full_scan_at' OR key LIKE 'full_scan_at:%'")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('backfill:video_container','1')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag_source ON tags(tag, source, image_id)")
    # Anyone who ran the 2026-08-19 build before the reorder has the bad index on disk.
    conn.execute("DROP INDEX IF EXISTS idx_tags_source_tag")
    # The Unreviewed count is retired, and it was this index's only reader — 3.6MB at a 100k
    # library, plus an index write per row on every scan.
    conn.execute("DROP INDEX IF EXISTS idx_images_unreviewed")
    q_cols = [r[1] for r in conn.execute("PRAGMA table_info(quality)")]
    if 'reward' not in q_cols:
        conn.execute("ALTER TABLE quality ADD COLUMN reward REAL")
    # RETIRED: the LLM scan (removed 2026-07-19 — a VLM can't verify defects). Its per-image data
    # was kept dormant under legacy_llm_* names against a future rating feature that might seed
    # from it. No such feature arrived, and the rest of the AI work left on 2026-08-23, so it goes
    # now — the open `AI-1` ideas item was exactly this question.
    #
    # BOTH namings are handled. A library upgraded through a mid-2026 build carries legacy_llm_*;
    # one that skipped those builds still has the original short names. Dropping only the renamed
    # set would silently leave the other kind behind.
    conn.execute("DROP INDEX IF EXISTS idx_quality_style")
    for _qcol in ('legacy_llm_style', 'legacy_llm_score', 'legacy_llm_issues',
                  'legacy_llm_raw', 'legacy_llm_ran_at',
                  'style', 'score', 'issues', 'raw', 'ran_at'):
        if _qcol in q_cols:
            conn.execute(f"ALTER TABLE quality DROP COLUMN {_qcol}")
    # One-time purge of the scan's managed style: tags (flag-guarded: a style: tag the user makes
    # BY HAND after this runs must survive later launches).
    if not conn.execute("SELECT 1 FROM meta WHERE key='style_tags_purged'").fetchone():
        conn.execute("DELETE FROM tags WHERE tag LIKE 'style:%'")
        conn.execute("INSERT INTO meta(key, value) VALUES('style_tags_purged', '1')")
    # The Hidden mark was removed on 2026-09-08, and its rows go with it: nothing reads source='hide'
    # any more, so leaving them would be curation the user can neither see nor clear. Flag-guarded
    # like the purge above, though nothing can write one now -- the guard costs one lookup and means
    # this file needs no memory of whether it has already run.
    if not conn.execute("SELECT 1 FROM meta WHERE key='hide_marks_purged'").fetchone():
        conn.execute("DELETE FROM tags WHERE source='hide'")
        conn.execute("INSERT INTO meta(key, value) VALUES('hide_marks_purged', '1')")
    # Detect an FTS missing any column this version searches: pre-v2 had `negative` and no
    # `filename`, pre-v5 had no `lyrics`. Tested by COLUMN rather than by version alone, so a DB
    # whose schema_version was written by a newer build than its FTS still repairs itself.
    # Rebuilt from `images`, not re-read from disk: every value it indexes is already a column, so
    # this costs one pass over the table at the first launch after updating and no file I/O.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(images_fts)")]
    if ver < 5 or 'filename' not in cols or 'folder' not in cols or 'lyrics' not in cols:
        conn.execute("DROP TABLE IF EXISTS images_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE images_fts USING fts5(positive, model_name, filename, folder, "
            "lyrics, content='images', content_rowid='id', tokenize='unicode61')")
        conn.execute(
            "INSERT INTO images_fts(rowid, positive, model_name, filename, folder, lyrics) "
            "SELECT id, COALESCE(positive,''), COALESCE(model_name,''), COALESCE(filename,''), "
            "COALESCE(folder,''), COALESCE(lyrics,'') FROM images")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


# SQLite's INTEGER tops out at 2**63-1, and a ComfyUI seed does not: nodes randomise across the
# full UNSIGNED 64-bit range, so a seed above 9223372036854775807 is ordinary rather than exotic.
# Binding one raises OverflowError("Python int too large to convert to SQLite INTEGER"), which --
# with no per-file guard in the scan loop -- ended the whole library refresh on the first such file.
#
# NOT CLAMPED, and not stored as text in the INTEGER column either. Both lose the value: SQLite
# gives an INTEGER-affinity column a REAL when the text will not fit, so 18446744073709551615 comes
# back as 1.8446744073709552e+19. A seed exists to reproduce a generation, so a nearly-right one is
# worse than none -- it looks usable and cannot work. That is the same reasoning that already sends
# the seed to the client as a string (see api_image); this is the storage half of it.
#
# So the oversized one is kept EXACTLY, in a TEXT column, and gp_seed is left null for it. Exactly
# one of the two ever holds a value, which is what keeps "which one is the real seed" from being a
# question anyone has to ask.
_INT64_MIN, _INT64_MAX = -2**63, 2**63 - 1


def _split_seed(seed):
    """(gp_seed, gp_seed_s) for one seed -- at most one is non-None."""
    if seed is None:
        return None, None
    try:
        v = int(seed)
    except (TypeError, ValueError):
        return None, str(seed)          # not a number at all; keep whatever it was
    return (v, None) if _INT64_MIN <= v <= _INT64_MAX else (None, str(v))


def _fts_delete(conn, rowid):
    # An external-content FTS deletes by being handed the values it indexed, so EVERY indexed
    # column has to appear here: a delete that names fewer leaves the row half-removed and the
    # index disagreeing with the table. That is why this reads them back off the still-present row.
    conn.execute(
        "INSERT INTO images_fts(images_fts, rowid, positive, model_name, filename, folder, lyrics) "
        "VALUES('delete', ?, (SELECT positive FROM images WHERE id=?), "
        "(SELECT model_name FROM images WHERE id=?), (SELECT filename FROM images WHERE id=?), "
        "(SELECT folder FROM images WHERE id=?), (SELECT lyrics FROM images WHERE id=?))",
        (rowid, rowid, rowid, rowid, rowid, rowid),
    )


def delete_by_ids(db_path, ids):
    """Remove images (and their FTS + tag rows) by id. Returns count removed.
    Caller is responsible for the actual files (e.g. moving them to the trash)."""
    if not ids:
        return 0
    conn = connect(db_path)
    removed = 0
    for iid in ids:
        if not conn.execute("SELECT 1 FROM images WHERE id=?", (iid,)).fetchone():
            continue
        _fts_delete(conn, iid)                      # reads values from the still-present row
        conn.execute("DELETE FROM images WHERE id=?", (iid,))
        conn.execute("DELETE FROM tags WHERE image_id=?", (iid,))
        conn.execute("DELETE FROM quality WHERE image_id=?", (iid,))
        removed += 1
    conn.commit()
    conn.close()
    return removed


def _fts_insert(conn, rowid, positive, model_name, filename, folder='', lyrics=''):
    conn.execute(
        "INSERT INTO images_fts(rowid, positive, model_name, filename, folder, lyrics) "
        "VALUES (?,?,?,?,?,?)",
        (rowid, positive or '', model_name or '', filename or '', folder or '', lyrics or ''),
    )


def walk_media(root):
    """One directory walk that returns BOTH the media files (with their stat) and every
    directory's mtime — so the scan needs no second os.stat per file, and can store a cheap
    change-detection signature.

    Returns (files, dirs):
      files : list of (path, mtime, size) for each indexable media file
      dirs  : {relpath: mtime} for each directory (root itself keyed as '.')
    Uses os.scandir: DirEntry.stat() is free on Windows for regular files (mtime+size come
    from the directory listing — no extra round trip, the win on network shares). Matches
    os.walk semantics: prunes SKIP_DIRS and does NOT descend into symlinked/junction dirs
    (is_dir(follow_symlinks=False)), and is iterative so deep trees can't hit the recursion
    limit."""
    files = []
    dirs = {}
    root = os.path.abspath(root)
    try:
        dirs['.'] = os.stat(root).st_mtime
    except OSError:
        return files, dirs
    stack = [root]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in SKIP_DIRS:
                                continue
                            # Direct os.stat for the DIRECTORY mtime: on Windows a subdir's
                            # timestamp in its PARENT's listing (DirEntry.stat) updates lazily
                            # and can be stale — but changes_detected() reads it with a direct
                            # os.stat, so both must use the same call or every check false-fires.
                            # (Files below keep the free DirEntry.stat: file listing times are fresh.)
                            dirs[os.path.relpath(e.path, root).replace('\\', '/')] = os.stat(e.path).st_mtime
                            stack.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in MEDIA_EXTS:
                            st = e.stat()
                            files.append((e.path, st.st_mtime, st.st_size))
                    except OSError:
                        continue          # entry vanished mid-walk; skip it
        except OSError:
            continue                      # directory vanished/unreadable; skip it
    return files, dirs


def list_images(root):
    """Just the media paths — compat shim over walk_media (see server.py comment)."""
    return [f[0] for f in walk_media(root)[0]]


def dir_signature(db_path, root_id=None):
    """The stored {relpath: mtime} directory signature for a root, or None. In the merged DB the
    signature is keyed per-root (`dir_sig:<root_id>`); `root_id=None` reads the legacy single key."""
    conn = sqlite3.connect(db_path)
    key = 'dir_sig:' + root_id if root_id is not None else 'dir_sig'
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


# The freshness check is LATENCY-bound, not CPU-bound: on a network share every stat is a round
# trip, so a library with a thousand indexed folders was a thousand round trips ONE AFTER ANOTHER —
# seconds of pure waiting. Overlapping them collapses that to roughly (folders / workers) round
# trips. os.stat releases the GIL, so threads genuinely run at the same time here.
# 16 is a share-friendly number: enough to hide the latency, not so many that a NAS starts queuing.
_SIG_WORKERS = 16
_SIG_MIN_PARALLEL = 8   # below this a pool costs more to build than the stats cost to run


# How long a QUICK scan may be trusted before one full walk is done anyway. A quick scan believes
# folder timestamps, and they can lie: Windows caches directory metadata over SMB for seconds, the
# comparison carries a 2s tolerance for share jitter (MTIME_TOL), and neither notices a file edited
# in place. Every one of those makes a change invisible until something else touches that folder —
# which, for a folder that is finished with, is never. The author hit exactly that: a set deleted by hand
# stayed on screen through repeated rescans and only went when he forced a full rebuild.
#
# So the timestamps decide what a rescan LOOKS at, and this decides how long that is allowed to be
# the whole story. Six hours: a full walk costs ~6s on a 100k library over a share, which is
# invisible once or twice a day and unbearable on every refresh.
FULL_SCAN_MAX_AGE = 6 * 3600


def full_scan_age(db_path, root_id=None):
    """Seconds since the last full walk of this root, or None if there has never been one."""
    conn = sqlite3.connect(db_path)
    key = 'full_scan_at:' + root_id if root_id is not None else 'full_scan_at'
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        return max(0.0, time.time() - float(row[0]))
    except (TypeError, ValueError):
        return None


def changed_dirs(db_path, root, root_id=None):
    """WHICH indexed folders changed, rather than merely whether any did.

    Returns (changed, gone) — folders whose mtime moved, and folders that could not be read at all
    — or None when there is no stored signature to compare against, which means the caller has no
    choice but to walk everything.

    This is the same per-folder stat sweep as changes_detected() and costs the same (~0.5s on a
    100k library over a share, against ~6s to walk every file). It existed only to light the ↻;
    telling the scan WHICH folders to look at is what makes a rescan proportional to what changed
    instead of to the size of the library.
    """
    stored = dir_signature(db_path, root_id)
    if not stored or not os.path.isdir(root):
        return None
    root = os.path.abspath(root)

    def _check(item):
        rel, old = item
        p = root if rel == '.' else os.path.join(root, rel.replace('/', os.sep))
        try:
            return rel, abs(os.stat(p).st_mtime - old) > MTIME_TOL, False
        except OSError:
            return rel, True, True          # unreadable — changed, and possibly gone
    items = list(stored.items())
    if len(items) < _SIG_MIN_PARALLEL:
        results = [_check(it) for it in items]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SIG_WORKERS, len(items))) as ex:
            results = list(ex.map(_check, items))
    changed = {rel for rel, ch, missing in results if ch and not missing}
    gone = {rel for rel, _ch, missing in results if missing}
    return changed, gone


def walk_media_subset(root, only_dirs, known_dirs):
    """walk_media over a SUBSET: each named folder's own entries, plus any subfolder that is NEW
    (absent from known_dirs) walked in full — a new folder bumps its parent's mtime, so the parent
    is always in `only_dirs` and the new subtree is found from there.

    Returns (files, dirs, unreadable). A folder that could not be read lands in `unreadable` and is
    deliberately left out of `dirs`, which is what keeps the caller from pruning rows for files it
    never actually looked for.
    """
    files, dirs, bad, seen = [], {}, set(), set()
    root = os.path.abspath(root)
    stack = [root if rel == '.' else os.path.join(root, rel.replace('/', os.sep))
             for rel in only_dirs]
    while stack:
        dirpath = stack.pop()
        if dirpath in seen:
            continue
        seen.add(dirpath)
        rel = ('.' if os.path.abspath(dirpath) == root
               else os.path.relpath(dirpath, root).replace(os.sep, '/'))
        try:
            mt = os.stat(dirpath).st_mtime          # direct stat, as walk_media does — see there
        except OSError:
            bad.add(rel)
            continue
        try:
            with os.scandir(dirpath) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in SKIP_DIRS:
                                continue
                            crel = os.path.relpath(e.path, root).replace(os.sep, '/')
                            if crel not in known_dirs:
                                stack.append(e.path)     # a folder we have never indexed
                        elif os.path.splitext(e.name)[1].lower() in MEDIA_EXTS:
                            st = e.stat()
                            files.append((e.path, st.st_mtime, st.st_size))
                    except OSError:
                        continue
        except OSError:
            bad.add(rel)
            continue
        dirs[rel] = mt                               # only once the listing actually succeeded
    return files, dirs, bad


def changes_detected(db_path, root, root_id=None):
    """Cheap freshness check: compare each indexed folder's stored mtime against disk. True
    if any folder's contents changed (file/subdir added/removed/renamed) or the folder is
    gone. Costs one stat per directory (dirs << files) — NOT a full walk. Only misses
    in-place edits that don't bump a folder mtime (rare); a rescan catches those.

    Note which case is expensive: it returns as soon as it finds a change, so "something changed"
    is cheap and "nothing changed" — the common one — is the one that pays for every folder."""
    stored = dir_signature(db_path, root_id)
    if not stored or not os.path.isdir(root):
        return False
    root = os.path.abspath(root)
    items = list(stored.items())

    def _changed(item):
        rel, old = item
        p = root if rel == '.' else os.path.join(root, rel.replace('/', os.sep))
        try:
            return abs(os.stat(p).st_mtime - old) > MTIME_TOL
        except OSError:
            return True     # folder vanished -> something changed

    if len(items) < _SIG_MIN_PARALLEL:
        return any(_changed(it) for it in items)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_SIG_WORKERS, len(items))) as ex:
        futures = [ex.submit(_changed, it) for it in items]
        try:
            for fut in concurrent.futures.as_completed(futures):
                if fut.result():
                    return True     # early exit still works — the rest are dropped below
        finally:
            for fut in futures:
                fut.cancel()        # queued stats we no longer need; in-flight ones just finish
    return False


def refresh_dir_sig(db_path, root, root_id, rel_folders):
    """Update the stored folder-mtime signature for specific folders (relpaths, '/'-separated,
    '.' = root) to their CURRENT on-disk mtime. Called after the app itself moves files out of a
    folder (recycle), so its OWN change doesn't trip changes_detected()/the ↻ icon — only genuine
    external changes (new generations added outside the app) still flag the library.

    Only folders the last scan already tracks are refreshed; unknown folders are skipped. No-op if
    no signature is stored yet. Returns the number of entries updated."""
    key = 'dir_sig:' + root_id if root_id is not None else 'dir_sig'
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)   # this one writes; see connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return 0
        try:
            sig = json.loads(row[0])
        except Exception:
            return 0
        root = os.path.abspath(root)
        n = 0
        for rel in rel_folders:
            if rel not in sig:                  # only refresh folders the scan already knows
                continue
            p = root if rel == '.' else os.path.join(root, rel.replace('/', os.sep))
            try:
                sig[rel] = os.stat(p).st_mtime
                n += 1
            except OSError:
                pass
        if n:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(sig)))
            conn.commit()
        return n
    finally:
        conn.close()


def read_file_row(path, root, fmtime, fsize, thumbs_dir=None):
    """Read ONE file into the tuple the images table takes, and the meta dict behind it.

    Lifted out of scan()'s loop so the scan and the on-open self-heal cannot drift. That matters
    more than it looks: the column ORDER, the has_meta rule, the seed split and the thumbnail
    side-effect all have to agree between the two callers, and a second hand-written copy is how
    they would stop agreeing -- quietly, and only for the files nobody rescanned.

    The row ends with READER_VERSION, so both callers stamp it by construction rather than by
    remembering to. Returns (row, meta, has_meta).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        # Video: Pillow can't read dimensions, so those are still backfilled lazily when the
        # poster thumb is made. Metadata, though, IS read here.
        #
        # This used to say a video has no ComfyUI metadata of its own. That was wrong, and it
        # was load-bearing — it is why the `.txt` sidecar was invented and why a video showed no
        # model or seed. A ComfyUI video carries the same `prompt` graph a PNG does; the scan
        # simply never looked. extract_video reads it and then lets the sidecar fill blanks and
        # win on the prompt (see its docstring for why that order, not the reverse).
        #
        # The cost is a ~256KB head read per video, once — a rescan skips unchanged files, so
        # only new videos ever pay it again. Chosen over reading lazily at thumbnail time
        # because metadata that arrives only for videos you have LOOKED AT makes the model
        # filter quietly under-report, and a filter that lies is worse than a slower scan.
        meta = comfy_meta.extract_video(path) or _EMPTY_META
        motion = 1
        # From the mvhd and tkhd boxes, read during the same bounded head read that already
        # fetches the workflow -- no decoder, no second open. .get() because _EMPTY_META has
        # no such key.
        #
        # DIMENSIONS USED TO BE LEFT FOR THE POSTER FRAME, and that is why this changed: the
        # backfill fires when a video's thumbnail is made, and a video PAIRED WITH A STILL is
        # fronted by the still, so its thumbnail is never made and its size was never learned.
        # The poster-frame backfill stays where it is and still overwrites this -- ffmpeg
        # reports the true frame, `tkhd` reports the declared one, and they differ only for
        # anamorphic clips (see _mp4_dims). This is the answer that is always there.
        duration, peaks = meta.get('duration'), None
        w, h = meta.get('width'), meta.get('height')
    elif ext in AUDIO_EXTS:
        # Audio: the ComfyUI graph is in the file's ID3 tag, read the same way and by the same
        # walkers as a PNG's. There is no picture, so no dimensions and no thumbnail — the
        # waveform stands in for one, and it costs a decode (~0.5s for a minute of music).
        #
        # motion stays 0: a song is not "animated". The ▶ on its card comes from being audio,
        # not from this flag, so the Animated filter keeps meaning what it always meant.
        meta = comfy_meta.extract_audio(path)
        w = h = None
        motion = 0
        duration, peaks = thumbs_mod.audio_shape(path)
    else:
        meta = comfy_meta.extract(path)
        w, h, animated = image_probe(path)
        motion = 1 if animated else 0
        duration, peaks = None, None
    rel = os.path.relpath(path, root)
    folder = os.path.dirname(rel) or '.'
    # model_name as well as model: an image whose metadata came from the A1111 `parameters`
    # chunk has a model NAME but no model PATH (the block records what was used, not where it
    # lives). Testing the path alone would file such an image as "no meta" while the detail
    # view happily showed its model.
    has_meta = 1 if (meta['positive'] or meta['model'] or meta.get('model_name')) else 0
    thumb = thumbs_mod.ensure_thumb(path, thumbs_dir) if thumbs_dir else None
    loras_json = json.dumps(meta.get('loras') or [])
    # .get(): videos use _EMPTY_META, which has no set keys (they're PNG chunks).
    seed_i, seed_s = _split_seed(meta.get('seed'))   # see _split_seed: at most one is set
    row = (path, rel, folder, name_of(path), ext,
           fmtime, fsize, w, h,
           meta['model'], meta['model_name'], meta.get('vae_name'), meta['positive'],
           meta['negative'], meta['method'], has_meta, thumb, loras_json,
           meta.get('set_id'), meta.get('set_stage'),
           meta.get('steps'), meta.get('cfg'), meta.get('sampler_name'),
           meta.get('scheduler'), seed_i, seed_s,
           duration, json.dumps(peaks) if peaks else None, meta.get('lyrics'),
           comfy_meta.name_from_filename(path) if ext in AUDIO_EXTS else None,
           meta.get('bpm'), meta.get('key'), meta.get('genre'), meta.get('vocal'),
           meta.get('has_cover') or 0,
           motion, time.time(), READER_VERSION)

    return row, meta, has_meta


def update_file_row(conn, rid, row, meta, path, folder):
    """Rewrite one existing row from a freshly-read `row`, search entry included.

    The UPDATE names its columns on purpose and this function is the only place it lives: every
    hand-authored value (`note`, the curation marks, the viewing history) is absent from it, and
    that omission IS the mechanism by which they survive a re-read. Never widen it.
    """
    _fts_delete(conn, rid)
    conn.execute(
        "UPDATE images SET path=?,rel_path=?,folder=?,filename=?,ext=?,mtime=?,"
        "size=?,width=?,height=?,model=?,model_name=?,vae_name=?,positive=?,negative=?,"
        "method=?,has_meta=?,thumb=?,loras=?,set_id=?,set_stage=?,"
        "gp_steps=?,gp_cfg=?,gp_sampler=?,gp_scheduler=?,gp_seed=?,gp_seed_s=?,"
        "duration=?,peaks=?,lyrics=?,song_name=?,song_bpm=?,song_key=?,song_genre=?,"
        "song_vocal=?,has_cover=?,motion=?,indexed_at=?,reader_ver=? "
        "WHERE id=?",
        row + (rid,))
    _fts_insert(conn, rid, meta['positive'], meta['model_name'], name_of(path), folder,
                meta.get('lyrics'))


def reread_one(conn, row, root_path, thumbs_dir=None):
    """Re-read the file behind ONE row, because this build reads more than the one that indexed it.

    The whole of the self-heal. A file whose stamp is behind catches up the moment you open it —
    nothing to press, nothing in the background, one file, on your own click.

    WHAT IT CANNOT DO, and the reason it does not replace a rescan: it fixes what you are LOOKING
    AT, not what you look FOR. Searching, filtering and sorting read the whole library at once, so a
    prompt that was never indexed is still not findable and that image still will not appear under
    its model — until the library is rescanned.

    Never raises, and never stamps a row it failed to read. A file that has gone, a share that is
    not answering, an unreadable byte: the row is left exactly as it was, stamp included, so the
    next open tries again once the cause has passed. That is also the cure for the file that failed
    to read once and would otherwise keep its blank forever, since its size and date go on matching.
    """
    path = row['path']
    try:
        st = os.stat(path)
        new_row, meta, _ = read_file_row(path, root_path, st.st_mtime, st.st_size, thumbs_dir)
        update_file_row(conn, row['id'], new_row, meta, path, new_row[2])
    except Exception:
        return False
    return True


def scan(root, db_path, thumbs_dir=None, progress=None, should_stop=None, count_cb=None,
         force=False, root_id=None, only_dirs=None, gone_dirs=None):
    """Index `root` into `db_path`, tagging every row with `root_id`. Returns stats.

    In the merged DB, `root_id` scopes EVERYTHING to this one library: the incremental
    `existing` map, the prune of vanished files, the folder-change signature, and the group
    recompute all filter by `root_id` — so scanning one root never touches another root's rows.
    (`root_id=None` keeps the legacy whole-DB behavior, used only pre-merge.)

    progress(done, total, added, updated, skipped) is called periodically.
    count_cb(total) is called once the file count is known.
    should_stop() -> bool lets a caller cancel mid-scan; work done so far is
    committed and kept (pruning of vanished files is skipped when cancelled).
    """
    root = os.path.abspath(root)
    # Refuse an unreachable library (network share offline, drive unplugged). walk_media returns an
    # EMPTY list rather than raising for a missing root, which to the prune below is indistinguishable
    # from "the user deleted every file" — so without this, one Rescan while off-network would delete
    # the whole library's rows AND its tags/labels/favorites/notes/scores.
    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"Library folder isn't reachable: {root} — connect to it and try again. "
            f"Nothing was changed.")
    conn = connect(db_path)

    # A QUICK SCAN LOOKS ONLY WHERE SOMETHING CHANGED. `only_dirs` names the folders whose mtime
    # moved since the last scan (server.run_scan gets them from changed_dirs), and everything below
    # is then scoped to exactly those: the rows compared, the files walked, and — critically — the
    # rows eligible to be pruned. A folder nobody touched is not looked at and cannot be affected.
    #
    # This is proportional to what changed rather than to the library: one new generation used to
    # cost a walk of all 100k files (~6s on a share) because the scan ignored the per-folder
    # signature it had already collected.
    narrow = only_dirs is not None
    known = set(dir_signature(db_path, root_id) or {}) if narrow else set()
    if narrow:
        all_files, all_dirs, bad_dirs = walk_media_subset(root, only_dirs, known)
    else:
        all_files, all_dirs = walk_media(root)   # (path, mtime, size) per file + {dir: mtime}
        bad_dirs = set()

    # map path -> (mtime, size, id) for incremental comparison — scoped to this root, and on a
    # quick scan to the folders actually walked. `folder` is stored with the platform separator
    # while the signature keys use '/', so the walked set is converted rather than compared raw.
    q_where = "root_id IS ?" if root_id is not None else "1=1"
    q_params = [root_id] if root_id is not None else []
    if narrow:
        walked = [d.replace('/', os.sep) for d in all_dirs]
        existing = {}
        for chunk in (walked[i:i + 400] for i in range(0, len(walked), 400)):
            qm = ','.join('?' * len(chunk))
            for r in conn.execute(
                    f"SELECT path, mtime, size, id, width, duration FROM images "
                    f"WHERE {q_where} AND folder IN ({qm})",
                    q_params + chunk):
                existing[r[0]] = (r[1], r[2], r[3], r[4], r[5])
    else:
        # (mtime, size, id, width, duration). Widening this for the width silently broke the
        # prune's unpack below, inside the scan's own connection, which left the database LOCKED
        # rather than raising anything a caller could see. The prune now INDEXES instead of
        # unpacking, so appending a field here cannot break it a third time.
        existing = {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in
                    conn.execute(f"SELECT path, mtime, size, id, width, duration "
                                 f"FROM images WHERE {q_where}", q_params)}
    total = len(all_files)
    if count_cb:
        count_cb(total)

    seen = set()
    added = updated = skipped = no_meta = 0
    # Files the loop below could not process at all. See the guard inside it.
    failed = 0
    first_error = None
    t0 = time.time()
    n = 0
    stopped = False

    for path, fmtime, fsize in all_files:      # stat captured during the walk — no second os.stat
        if should_stop and should_stop():
            stopped = True
            break
        seen.add(path)
        n += 1
        # ONE UNREADABLE FILE MUST NOT END THE REFRESH. Everything below reads a file the app
        # did not write -- a container, a PNG chunk, a graph -- and hands what it finds to SQLite.
        # There was no guard here, so any single surprise ended the whole run: the author's refresh
        # stopped dead on one image whose seed was too large for an INTEGER, with ~78,000 files
        # behind it unlooked-at and a bare Python error on screen.
        #
        # DELIBERATELY AFTER seen.add(path), and that placement is the safety property. `seen` is
        # what the prune pass tests, so a file that failed here is still SEEN: its row survives,
        # and so do its tags, labels, favourite, note and quality score. Guarding one line higher
        # would turn "I could not read this file" into "this file is gone", and take the curation
        # with it -- the one outcome a rescan may never produce.
        #
        # NOT SILENT. The count and the first file's name come back in the stats and are reported
        # when the scan finishes, because a scan that quietly drops files is worse than one that
        # stops: you would have no reason to look.
        try:
            prev = existing.get(path)
            # THE ONE EXCEPTION TO "UNCHANGED MEANS SKIP": a video indexed before its size and length
            # were read out of the container carries neither, and never would -- the file will not
            # change, so no ordinary rescan would ever look at it again.
            #
            # BOTH are tested, not just the size, and that is the whole point of this shape. Keying it
            # on the dimensions alone left a hole exactly where the author found one: a video he had ever
            # viewed as its own card got its dimensions from the poster-frame backfill years-of-scans
            # ago, so it looked complete while its LENGTH stayed empty forever. A row is stale if it is
            # missing ANY fact the container now yields, not if it is missing the newest one.
            #
            # Re-reading costs the same bounded head read as a new video, once, and then this stops
            # matching. The cheap column tests come first so the extension split is only reached for
            # the handful of rows actually missing something.
            #
            # SCOPED TO THE FORMATS THAT CAN ANSWER, which is what keeps "once" true. A WebM carries no
            # readable metadata here at all, so its columns stay empty however many times it is opened
            # -- keyed on VIDEO_EXTS this would re-read every WebM in the library on every scan for the
            # rest of the library's life, a permanent tax paid for nothing.
            if (prev and not force and abs(prev[0] - fmtime) < 1e-6 and prev[1] == fsize
                    and not ((prev[3] is None or prev[4] is None)
                             and os.path.splitext(path)[1].lower()
                             in comfy_meta.VIDEO_META_EXTS)):
                skipped += 1
                if progress and n % 100 == 0:
                    progress(n, total, added, updated, skipped)
                continue

            row, meta, has_meta = read_file_row(path, root, fmtime, fsize, thumbs_dir)
            folder = row[2]
            if not has_meta:
                no_meta += 1

            if prev:
                update_file_row(conn, prev[2], row, meta, path, folder)
                updated += 1
            else:
                # shuffle_key is dealt by SQL, not passed in `row`: it is the one value that is neither
                # read from the file nor computed from it, and keeping it out of the tuple means the
                # Random sort costs this statement one expression rather than a signature change.
                cur = conn.execute(
                    "INSERT INTO images(path,rel_path,folder,filename,ext,mtime,size,width,"
                    "height,model,model_name,vae_name,positive,negative,method,has_meta,thumb,loras,"
                    "set_id,set_stage,gp_steps,gp_cfg,gp_sampler,gp_scheduler,gp_seed,gp_seed_s,"
                    "duration,peaks,lyrics,song_name,song_bpm,song_key,song_genre,song_vocal,has_cover,"
                    "motion,indexed_at,reader_ver,root_id,shuffle_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "abs(random() % 1000000007))",
                    row + (root_id,))
                _fts_insert(conn, cur.lastrowid, meta['positive'], meta['model_name'], name_of(path),
                            folder, meta.get('lyrics'))
                added += 1
        except Exception as e:
            failed += 1
            if first_error is None:
                first_error = '%s: %s' % (name_of(path), e)

        if n % 500 == 0:
            conn.commit()  # checkpoint progress so a stop/crash keeps it
        if progress and n % 100 == 0:
            progress(n, total, added, updated, skipped)

    # The last tick, HERE and not after the passes below. Reporting every 100 files means the final
    # report lands on the last multiple of 100 — so a 6,246-file library stopped at 6,200 and the
    # bar sat on 99% for the whole back half of the scan, never reaching 100%. The passes below
    # report nothing, so this is the number the user is left looking at.
    if progress:
        progress(n, total, added, updated, skipped)

    # prune vanished files only on a complete pass (a cancelled scan hasn't
    # visited everything, so we must not delete "unseen" rows)
    #
    # ...and never on an EMPTY walk that found nothing while the DB still has rows. A share that
    # dropped out mid-scan (or an unplugged drive) looks exactly like "every file was deleted", and
    # pruning would take the whole library's curation with it. Deleting every file for real is rare
    # and recoverable (remove the library); silently wiping tags/labels/scores is not.
    removed = 0
    # On a QUICK scan the guard is structural rather than a heuristic: `existing` only ever held
    # rows from folders we actually listed, and a folder that failed to read never made it into
    # that set — so an unreachable share yields nothing to prune instead of "everything vanished".
    # The empty-walk heuristic below is therefore for the FULL walk only, where it is the only
    # signal available. A quick scan legitimately finds zero files (the change was a deletion).
    vanished = (not narrow) and bool(existing) and not all_files
    complete_pass = (not stopped) and not vanished
    if vanished and not stopped:
        print(f"[scan] {root}: walk found 0 files but the index has {len(existing)} rows — "
              f"skipping the prune (library probably offline).")
    if complete_pass:
        # INDEXED, NOT UNPACKED: this tuple has been widened twice, and an unpack here breaks
        # the scan from inside its own connection -- see where `existing` is built.
        for path, _prev in existing.items():
            rid = _prev[2]
            if path not in seen:
                _fts_delete(conn, rid)
                conn.execute("DELETE FROM images WHERE id=?", (rid,))
                # like delete_by_ids: a pruned image must not leave tag/quality rows
                # behind (files renamed/removed outside the app orphaned these before,
                # inflating tag + favorite counts)
                conn.execute("DELETE FROM tags WHERE image_id=?", (rid,))
                conn.execute("DELETE FROM quality WHERE image_id=?", (rid,))
                removed += 1

    # Sweep any orphans left by older versions of the prune above (cheap: indexed PKs).
    conn.execute("DELETE FROM tags WHERE image_id NOT IN (SELECT id FROM images)")
    conn.execute("DELETE FROM quality WHERE image_id NOT IN (SELECT id FROM images)")

    # A FOLDER THAT IS GONE takes its rows with it. Only on a quick scan, and only for folders the
    # caller confirmed unreadable — with a sanity cap, because an offline share makes EVERY folder
    # look deleted and that is the one mistake here that destroys curation rather than costing time.
    if narrow and gone_dirs and complete_pass:
        # Half the library's folders unreadable at once is not a deletion anyone performed; it is
        # a share that dropped. `>= half` rather than a flat count, so the rule holds for a library
        # of five folders and one of five hundred. A genuine mass delete is still handled — by a
        # forced rescan, which walks everything and prunes with the full-walk guard instead.
        if len(gone_dirs) >= max(2, len(known) // 2):
            print(f"[scan] {root}: {len(gone_dirs)} of {len(known)} folders unreadable — "
                  f"treating the library as offline, pruning nothing.")
            gone_dirs = set()
        for rel in sorted(gone_dirs):
            p_abs = root if rel == '.' else os.path.join(root, rel.replace('/', os.sep))
            if os.path.isdir(p_abs):
                continue                     # came back between the check and now — leave it alone
            fold = rel.replace('/', os.sep)
            rows = conn.execute(
                f"SELECT id FROM images WHERE {q_where} AND (folder = ? OR folder LIKE ?)",
                q_params + [fold, fold + os.sep + '%']).fetchall()
            for (rid,) in rows:
                _fts_delete(conn, rid)
                conn.execute("DELETE FROM images WHERE id=?", (rid,))
                conn.execute("DELETE FROM tags WHERE image_id=?", (rid,))
                conn.execute("DELETE FROM quality WHERE image_id=?", (rid,))
                removed += 1

    # Store the folder-mtime signature so a later cheap changes_detected() can flag "rescan me"
    # without re-walking. Only on a complete pass — a cancelled walk didn't see every folder, and an
    # empty one would store {} and permanently stop the ↻ from firing.
    #
    # A quick scan MERGES: it only looked at a few folders, so writing `all_dirs` wholesale would
    # throw away every other folder's entry and make the next check believe the library was new.
    if complete_pass:
        sig_key = 'dir_sig:' + root_id if root_id is not None else 'dir_sig'
        sig = all_dirs
        if narrow:
            sig = dict(dir_signature(db_path, root_id) or {})
            sig.update(all_dirs)
            for rel in (gone_dirs or ()):
                sig.pop(rel, None)
                for k in [k for k in sig if k.startswith(rel + '/')]:
                    sig.pop(k, None)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     (sig_key, json.dumps(sig)))
        # Only a FULL walk resets the clock — a quick scan looked at a handful of folders and has
        # no opinion about the rest, which is the whole reason the clock exists.
        if not narrow:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                         ('full_scan_at:' + root_id if root_id is not None else 'full_scan_at',
                          str(time.time())))

    # HOW FAST THIS MACHINE READS THIS LIBRARY, so a rescan can be offered with a price on it rather
    # than as an open-ended wait. Recorded only from a FORCED run: an ordinary scan skips unchanged
    # files, and its rate is the speed of deciding not to do any work — the same lie jobPace refuses
    # to print. 200 files is enough to level out a slow first folder and small enough that a modest
    # library still produces a number. Per root, because a network share and an SSD are not
    # comparable and an average of the two describes neither.
    _read = added + updated
    _secs = time.time() - t0
    if force and _read >= RATE_MIN_FILES and _secs > 0 and not stopped:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     ('read_rate:' + (root_id or ''), str(round(_read / _secs, 3))))

    conn.commit()
    recompute_groups(conn, root_id)   # merge pairs/sets within THIS root (cheap string pass)
    recompute_model_types(conn)       # whole-library: the vocabulary spans every root
    conn.execute("PRAGMA optimize")
    conn.close()
    return {
        'total': total, 'processed': n, 'added': added, 'updated': updated,
        'skipped': skipped, 'removed': removed, 'no_meta': no_meta,
        'failed': failed, 'first_error': first_error,
        'stopped': stopped, 'seconds': round(time.time() - t0, 2),
    }


def name_of(path):
    return os.path.basename(path)


# columns copied verbatim from a per-root DB into the merged DB (root_id is stamped separately)
_MERGE_COLS = ("path,rel_path,folder,filename,ext,mtime,size,width,height,model,model_name,"
               "positive,negative,method,has_meta,thumb,loras,motion,note,indexed_at")
# NB: last_opened/last_seen and shuffle_key are
# deliberately absent — these columns are SELECTed straight out of an ATTACHed legacy per-root DB,
# which predates them (it would raise "no such column"). Nothing is lost: a DB written before a column
# existed has no value for it (and the merge is a one-time historical import, run long before any of
# these existed).


def merge_roots(merged_path, roots):
    """One-time: copy each per-root DB into the merged DB at `merged_path`, stamping `root_id`.
    `roots` = [(root_id, src_db_path), ...]. Idempotent — a `meta.merged` flag makes it a no-op
    after the first run. Non-destructive: source DBs are only read (ATTACH), never modified.
    Returns True if it performed the merge, False if already merged.

    ids are remapped implicitly: images are re-inserted (fresh autoincrement ids, globally unique
    because `path` is UNIQUE), and tags/quality are re-pointed by joining back through `path`.
    FTS is rebuilt in one shot; groups are recomputed root-namespaced."""
    conn = connect(merged_path)   # ensures schema incl. root_id
    if conn.execute("SELECT value FROM meta WHERE key='merged'").fetchone():
        conn.close()
        return False
    for root_id, src in roots:
        if not src or not os.path.exists(src):
            continue
        conn.execute("ATTACH DATABASE ? AS src", (src,))
        conn.execute(f"INSERT OR IGNORE INTO images({_MERGE_COLS}, root_id) "
                     f"SELECT {_MERGE_COLS}, ? FROM src.images", (root_id,))
        conn.execute(
            "INSERT OR IGNORE INTO tags(image_id, tag, source, score) "
            "SELECT li.id, s.tag, s.source, s.score FROM src.tags s "
            "JOIN src.images si ON si.id = s.image_id "
            "JOIN images li ON li.path = si.path WHERE li.root_id IS ?", (root_id,))
        conn.execute(
            # reward only: the legacy per-root DBs predate the LLM scan's removal, and that
            # feature's columns are retired — its old data is not carried into a fresh merge.
            "INSERT OR IGNORE INTO quality(image_id, reward) "
            "SELECT li.id, q.reward FROM src.quality q "
            "JOIN src.images si ON si.id = q.image_id "
            "JOIN images li ON li.path = si.path WHERE li.root_id IS ?", (root_id,))
        sig = conn.execute("SELECT value FROM src.meta WHERE key='dir_sig'").fetchone()
        if sig:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                         ('dir_sig:' + root_id, sig[0]))
        conn.commit()
        conn.execute("DETACH DATABASE src")
    conn.execute("INSERT INTO images_fts(images_fts) VALUES('rebuild')")   # repopulate external-content FTS
    recompute_groups(conn)                                                  # all rows, root-namespaced gids
    recompute_model_types(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('merged', '1')")
    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()
    return True


def delete_root(db_path, root_id):
    """Remove every row belonging to `root_id` (images + their FTS + tag/quality rows) from the
    merged DB. Used when a root is deleted with 'also delete its index'. Returns images removed."""
    conn = connect(db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM images WHERE root_id IS ?", (root_id,))]
    for iid in ids:
        _fts_delete(conn, iid)
        conn.execute("DELETE FROM tags WHERE image_id=?", (iid,))
        conn.execute("DELETE FROM quality WHERE image_id=?", (iid,))
    conn.execute("DELETE FROM images WHERE root_id IS ?", (root_id,))
    conn.execute("DELETE FROM meta WHERE key=?", ('dir_sig:' + root_id,))
    conn.commit()
    conn.close()
    return len(ids)


if __name__ == '__main__':
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, 'samples')
    db = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, 'data', 'index.db')
    thumbs_dir = os.path.join(os.path.dirname(db), 'thumbs')
    os.makedirs(os.path.dirname(db), exist_ok=True)

    def prog(done, total, a, u, s):
        print(f"  ...{done}/{total} files (added {a}, updated {u}, skipped {s})")

    print(f"Scanning: {root}")
    stats = scan(root, db, thumbs_dir=thumbs_dir, progress=prog)
    print("Done:", stats)
