"""test_thumb_reuse.py — a reused file name gets its own thumbnail, not the old file's.

Run: python test_thumb_reuse.py

Thumbnails are cached by PATH. Recycle Maya_00052_.png and ComfyUI's counter writes the next
generation to the same name, and the card showed the recycled picture while hover showed the new
one (the author, 2026-10-25). A cached thumb older than its source is now remade. Pins:

  * the reused name gets a thumbnail of the NEW picture;
  * an unchanged file keeps its cached thumbnail (no remake on every view);
  * a source stamped ahead of this machine's clock (a network share) is remade once, not forever.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import thumbs  # noqa: E402
from PIL import Image  # noqa: E402

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def colour(thumbs_dir, rel):
    with Image.open(os.path.join(thumbs_dir, rel)) as im:
        return im.convert('RGB').getpixel((4, 4))


def main():
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, 'Maya_00052_.png')
        cache = os.path.join(tmp, 'thumbs')
        Image.new('RGB', (64, 64), (200, 0, 0)).save(src)
        old = time.time() - 600
        os.utime(src, (old, old))
        rel = thumbs.ensure_thumb(src, cache, size=128, src_mtime=os.stat(src).st_mtime)
        check('the first picture is thumbnailed red', colour(cache, rel)[0] > 150)

        print('\nThe same name, a new picture\n')
        os.remove(src)
        Image.new('RGB', (64, 64), (0, 0, 200)).save(src)
        rel = thumbs.ensure_thumb(src, cache, size=128, src_mtime=os.stat(src).st_mtime)
        check('the thumbnail is the new blue one', colour(cache, rel)[2] > 150)

        print('\nAn unchanged file\n')
        out = os.path.join(cache, rel)
        before = os.stat(out).st_mtime_ns
        thumbs.ensure_thumb(src, cache, size=128, src_mtime=os.stat(src).st_mtime)
        check('keeps its cached thumbnail', os.stat(out).st_mtime_ns == before)

        print('\nA source whose clock runs ahead\n')
        ahead = time.time() + 3600
        thumbs.ensure_thumb(src, cache, size=128, src_mtime=ahead)
        stamped = os.stat(out).st_mtime_ns
        thumbs.ensure_thumb(src, cache, size=128, src_mtime=ahead)
        check('is remade once, then kept', os.stat(out).st_mtime_ns == stamped)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + ('all passed' if not _fails else f'{len(_fails)} FAILED'))
    sys.exit(1 if _fails else 0)


if __name__ == '__main__':
    main()
