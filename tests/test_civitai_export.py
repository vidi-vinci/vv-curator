"""test_civitai_export.py -- the exported PNG puts its metadata where readers look.

Run: python tests/test_civitai_export.py

THE BUG, found 2026-09-20 while chasing a file Civitai read as blank. The export wrote its
`parameters` chunk after the last IDAT, immediately before IEND. That is legal PNG and our own
reader took it happily, which is exactly why it survived: `read_png_text_chunks` walks the whole
file. Every other writer -- ComfyUI, A1111, Pillow's own save -- puts text BEFORE the pixels, and a
reader that parses the header and stops at IDAT therefore saw a file with no metadata at all.

Pillow is such a reader, which is what makes this measurable rather than a theory: open an export
built the old way and `im.info` has no `parameters` key until you call `load()`. The check below is
that one, plus the raw chunk order, because `info` alone would keep passing if the chunk drifted
back after IDAT and something else happened to read it early.

Also pinned: Comfy's own `prompt`/`workflow` chunks are GONE from the copy. Leaving them in is what
sends Civitai down its graph parser, which is the reason this export exists at all.
"""
import io
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from PIL.PngImagePlugin import PngInfo

import comfy_meta

failures = []


def check(label, got, want):
    ok = got == want
    print(('  ok    ' if ok else '  FAIL  ') + label)
    if not ok:
        print('          got %r, want %r' % (got, want))
        failures.append(label)


def chunk_order(data):
    i, n, out = 8, len(data), []
    while i + 8 <= n:
        length, ctype = struct.unpack('>I4s', data[i:i + 8])
        out.append(ctype.decode('ascii', 'replace'))
        i += 12 + length
    return out


# A PNG shaped like one ComfyUI saved: text chunks first, then the pixels.
src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_export_src.png')
info = PngInfo()
info.add_text('prompt', '{"1": {"class_type": "KSampler"}}')
info.add_text('workflow', '{"nodes": []}')
info.add_text('vv_set_id', '20260920-101112-abcd')
Image.new('RGB', (8, 8), (30, 60, 90)).save(src, pnginfo=info)

TEXT = 'a castle on a hill\nSteps: 10, Sampler: Euler a, CFG scale: 1, Seed: 42, Size: 8x8'
try:
    out = comfy_meta.splice_parameters_png(src, TEXT)
    order = chunk_order(out)

    check('the parameters chunk comes before the first IDAT',
          order.index('tEXt') < order.index('IDAT'), True)
    check('nothing trails after IEND', order[-1], 'IEND')
    check('exactly one text chunk survives',
          sum(1 for c in order if c in ('tEXt', 'zTXt', 'iTXt')), 1)

    im = Image.open(io.BytesIO(out))
    # NO im.load() here on purpose -- that is the whole point of the test.
    check('a reader that stops at the pixels still sees the metadata',
          im.info.get('parameters'), TEXT)
    check("Comfy's graph is gone, so Civitai cannot fall back to it",
          'prompt' in im.info or 'workflow' in im.info, False)
    check('and our own set id went with it', 'vv_set_id' in im.info, False)

    check('our own reader still finds it too',
          comfy_meta.read_png_text_chunks(src).get('prompt') is not None, True)

    # The mechanism, so this file goes red if the placement ever drifts back: the SAME bytes moved
    # to just before IEND are invisible to the same reader. Without this the checks above would
    # keep passing on any reader that happened to walk the whole file anyway.
    i, n, head, param, tail = 8, len(out), bytearray(out[:8]), b'', b''
    while i + 8 <= n:
        length, ctype = struct.unpack('>I4s', out[i:i + 8])
        chunk = out[i:i + 12 + length]
        i += 12 + length
        if ctype == b'tEXt':
            param = chunk
        elif ctype == b'IEND':
            tail = chunk
        else:
            head += chunk
    old_style = bytes(head) + param + tail
    check('(control) the pre-fix placement really is invisible without load()',
          'parameters' in Image.open(io.BytesIO(old_style)).info, False)
finally:
    try:
        os.remove(src)
    except OSError:
        pass

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
