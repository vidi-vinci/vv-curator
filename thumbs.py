"""
thumbs.py — generate and locate cached WebP thumbnails.

Thumbnails live under data/thumbs/<size>/<xx>/<sha1>.webp (sharded by the first
two hex chars of the sha1 of the absolute source path, so no single directory
holds all 20k files; the <size> top folder lets THUMB_SIZE change without a
manual cache clear). Used by both the indexer (bulk) and the server (lazy
fallback for anything missing).

Stills use Pillow directly. Videos (mp4/webm/mov) get a single poster frame
extracted with ffmpeg (the bundled imageio-ffmpeg binary, or a system ffmpeg
on PATH) and then run through the same WebP pipeline.
"""
import io
import os
import shutil
import sys
import hashlib
import subprocess
from PIL import Image

THUMB_SIZE = 512      # longest edge, px — sized for the grid's XL card (512px image box) so it
                      # renders 1:1 (native) instead of upscaled; S/M/L then downscale from it.
                      # The size is baked into the cache path (see thumb_rel), so changing it just
                      # regenerates lazily at the new size — no manual cache clear needed.
THUMB_QUALITY = 80

# Kept in sync with index_db.VIDEO_EXTS / AUDIO_EXTS (defined locally to avoid a circular import).
_VIDEO_EXTS = {'.mp4', '.webm', '.mov'}
_AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.opus', '.m4a'}


def thumb_rel(image_path, size=THUMB_SIZE):
    """Relative cache path (posix-style) for a source image at a given thumbnail size.

    The size is the top folder, so bumping THUMB_SIZE regenerates cleanly: the new size
    lands in its own subtree and any old-size thumbs are simply ignored (delete the old
    size folders to reclaim their space — nothing else needs clearing)."""
    h = hashlib.sha1(os.path.abspath(image_path).encode('utf-8')).hexdigest()
    return f"{size}/{h[:2]}/{h}.webp"


def _ffmpeg_exe():
    """Path to an ffmpeg binary: the bundled imageio-ffmpeg one, else system PATH, else None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which('ffmpeg')


def _video_poster_image(path):
    """Decode one poster frame from a video as a PIL image (full resolution), or None.

    Tries a 1-second seek first (skips black/fade-in intros); if the clip is shorter
    than that ffmpeg emits nothing, so we retry from the very start.
    """
    exe = _ffmpeg_exe()
    if not exe:
        return None

    def grab(seek):
        args = [exe, '-nostdin', '-loglevel', 'error']
        if seek:
            args += ['-ss', str(seek)]
        args += ['-i', path, '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']
        try:
            p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            return None
        return p.stdout if (p.returncode == 0 and p.stdout) else None

    data = grab(1) or grab(0)
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def _render_thumb(im, out, size, quality):
    """Downscale a PIL image to a cached WebP at `out`."""
    im = im.convert('RGB')
    im.thumbnail((size, size), Image.LANCZOS)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    im.save(out, 'WEBP', quality=quality, method=4)


def ensure_thumb_sized(image_path, thumbs_dir, size=THUMB_SIZE, quality=THUMB_QUALITY):
    """Create the thumbnail if missing. Returns (rel_path_or_None, src_size_or_None).

    src_size is the source's full (width, height) and is only reported for videos on
    fresh generation, so the server can backfill video dimensions (which the scan skips).
    """
    rel = thumb_rel(image_path, size)
    out = os.path.join(thumbs_dir, rel)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return rel, None
    ext = os.path.splitext(image_path)[1].lower()
    try:
        if ext in _VIDEO_EXTS:
            im = _video_poster_image(image_path)
            if im is None:
                return None, None
            with im:
                src_size = im.size
                _render_thumb(im, out, size, quality)
            return rel, src_size
        if ext in _AUDIO_EXTS:
            # A song's thumbnail is its EMBEDDED cover art, if it has any. Riding the normal
            # pipeline means a song with a cover needs no special handling anywhere downstream —
            # it simply has a `thumb` like every other file. A song without one returns None and
            # the card draws itself instead (see app.js songFaceHTML).
            import comfy_meta                    # local: only audio needs it, and it imports late
            data = comfy_meta.read_id3_cover(image_path)
            if not data:
                return None, None
            with Image.open(io.BytesIO(data)) as im:
                _render_thumb(im, out, size, quality)
            return rel, None
        with Image.open(image_path) as im:
            im.draft('RGB', (size, size))          # fast path for JPEG; no-op for PNG
            _render_thumb(im, out, size, quality)
        return rel, None
    except Exception:
        return None, None


def ensure_thumb(image_path, thumbs_dir, size=THUMB_SIZE, quality=THUMB_QUALITY):
    """Create the thumbnail if missing; return its relative path, or None on failure."""
    rel, _ = ensure_thumb_sized(image_path, thumbs_dir, size, quality)
    return rel


# ---- Audio: the waveform is a song's thumbnail --------------------------------------------------
# A song has no picture, so the card draws its loudness over time instead. That is the audio
# equivalent of a poster frame — generated from the file, different for every song, and it answers
# the two things a thumbnail answers for an image: how long is this, and what shape is it.
#
# Stored as a short list of numbers rather than a rendered image, so the card can draw it at any
# size, in either theme, and a change to the card design needs no cache rebuild.
PEAKS_N = 96          # bars across the card; ~2px each at the widest card, and ~400 bytes stored
PEAKS_RATE = 8000     # decode rate: loudness needs no fidelity, and this keeps a 5-min song ~5MB
_AUDIO_TIMEOUT = 120  # a long track on a slow network share still decodes well inside this


def audio_shape(path):
    """(duration_seconds, [0..100] * PEAKS_N) for an audio file. (None, None) if it can't be read.

    Both come from one decode, because reading the length any other way means trusting a header:
    the workflow's requested duration is not the delivered one (a run asking for 360s produced 18),
    and an MP3 frame count only works for MP3.

    Peaks are normalised to the song's OWN loudest moment. A quiet recording would otherwise draw
    as a flat line, which reads as "broken file" rather than "quiet song".
    """
    exe = _ffmpeg_exe()
    if not exe:
        return None, None
    args = [exe, '-nostdin', '-loglevel', 'error', '-i', path,
            '-ac', '1', '-ar', str(PEAKS_RATE), '-f', 's16le', '-']
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=_AUDIO_TIMEOUT)
    except Exception:
        return None, None
    raw = p.stdout
    if p.returncode != 0 or not raw:
        return None, None

    n_samples = len(raw) // 2
    if n_samples < PEAKS_RATE // 10:            # under a tenth of a second: not a playable song
        return None, None
    duration = round(n_samples / PEAKS_RATE, 2)

    import array
    samples = array.array('h')
    samples.frombytes(raw[:n_samples * 2])
    if sys.byteorder != 'little':
        samples.byteswap()

    step = n_samples / PEAKS_N
    peaks = []
    for i in range(PEAKS_N):
        lo = int(i * step)
        hi = max(lo + 1, int((i + 1) * step))
        chunk = samples[lo:hi]
        peaks.append(max(max(chunk), -min(chunk)) if chunk else 0)

    top = max(peaks) or 1
    return duration, [round(v * 100 / top) for v in peaks]
