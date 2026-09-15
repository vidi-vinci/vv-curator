"""test_mp4_comment_tags.py -- an MP4 written by ffmpeg carries its graphs in the comment atom.

Run: python tests/test_mp4_comment_tags.py

THE BUG, 2026-09-14. An MP4 can carry its metadata two ways, and this read only one.

  QuickTime INDEXES its tags: a `keys` box lists the names, and each `ilst` entry points at one by
  number. That is what the reader handled.

  ffmpeg has no `keys` box at all. It writes the payload into the standard COMMENT atom -- `©cmt`,
  `\\xa9cmt` in the file -- and ComfyUI's is a JSON object wrapping the same two graphs a PNG
  carries: {"prompt": "…", "workflow": "…"}.

Finding no `keys`, the reader returned {} and the file reported its duration and dimensions and
nothing else. VHS writes through ffmpeg and is the most used video node in ComfyUI, so this was not
an edge: it was most people's video, and every one of them looked like a file with no metadata.

HOW IT WAS FOUND, which is the part worth keeping. The author said his recent videos showed only the file
basics. I said videos do carry metadata; he said his did not; I accepted that and blamed the saver.
Both of us were wrong -- a raw byte scan found `class_type`, `"nodes"` and `ComfyUI` sitting in the
file all along. He was right to keep pushing, and the reason I could not reason my way there is that
the reader's own failure mode is SILENCE: a video with no metadata and a video whose metadata we
cannot parse look exactly alike from the outside, which this docstring's predecessor called
"deliberately indistinguishable". That is a defensible choice for a caller and a terrible one for a
diagnosis.
"""
import json
import os
import struct
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import comfy_meta  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


def box(name, payload):
    return struct.pack('>I', len(payload) + 8) + name + payload


def data_atom(text):
    # `data`: type(4 bytes, 1 = UTF-8) + locale(4), then the payload.
    return box(b'data', struct.pack('>I', 1) + struct.pack('>I', 0) + text.encode('utf-8'))


def ffmpeg_style_mp4(comment):
    """A minimal MP4 shaped the way ffmpeg writes one: ilst with a comment atom, and NO keys box."""
    ilst = box(b'ilst', box(b'\xa9too', data_atom('Lavf61.7.100')) +
                        box(b'\xa9cmt', data_atom(comment)))
    meta = box(b'meta', struct.pack('>I', 0) + box(b'hdlr', b'\x00' * 25) + ilst)
    moov = box(b'moov', box(b'udta', meta))
    return box(b'ftyp', b'isom' + b'\x00' * 8) + moov


# The real shape: two graphs, each a JSON *string* holding more JSON, exactly as ComfyUI writes it.
GRAPH = json.dumps({'1': {'class_type': 'CheckpointLoaderSimple',
                          'inputs': {'ckpt_name': 'ltx2310eros_v14.safetensors'}}})
UI = json.dumps({'nodes': [{'type': 'CheckpointLoaderSimple'}]})
buf = ffmpeg_style_mp4(json.dumps({'prompt': GRAPH, 'workflow': UI}))

moov = comfy_meta._mp4_find_box(buf, b'moov', 0, len(buf))
tags = comfy_meta._mp4_tags(buf, moov)
check('the comment atom is read at all', bool(tags), tags)
check('  the prompt graph comes back', tags.get('prompt'), GRAPH)
check('  and the workflow graph too', tags.get('workflow'), UI)
check('  as STRINGS for the tracer to parse, not as objects',
      isinstance(tags.get('prompt'), str), type(tags.get('prompt')))

# The encoder tag beside it must not become a graph.
check('an unrelated atom is ignored', 'encoder' not in tags and '\xa9too' not in tags, sorted(tags))

# NOT EVERY COMMENT IS OURS. A human comment, or another tool's JSON, must yield nothing rather
# than a field full of the wrong thing -- the same rule the A1111 parser follows.
for label, text in (('a plain human comment', 'shot on a phone, lovely light'),
                    ('JSON that is not ours', '{"camera": "a7iii", "iso": 400}'),
                    ('JSON whose values are not strings', '{"prompt": {"1": {}}}'),
                    ('an empty comment', '')):
    b = ffmpeg_style_mp4(text)
    t = comfy_meta._mp4_tags(b, comfy_meta._mp4_find_box(b, b'moov', 0, len(b)))
    check('%s yields nothing' % label, t == {}, t)

# A file with NEITHER form still returns empty rather than raising.
empty = box(b'ftyp', b'isom' + b'\x00' * 8) + box(b'moov', box(b'udta', b''))
check('a video with no metadata is still empty, not an error',
      comfy_meta._mp4_tags(empty, comfy_meta._mp4_find_box(empty, b'moov', 0, len(empty))) == {})

# THE OTHER DIALECT MUST STILL WORK. A `keys` box present means the indexed path, untouched by this.
_kname = b'prompt'
keys = box(b'keys', struct.pack('>I', 0) + struct.pack('>I', 1) +
           struct.pack('>I', 8 + len(_kname)) + b'mdta' + _kname)
_dbox = box(b'data', struct.pack('>I', 1) + struct.pack('>I', 0) + GRAPH.encode())
ilst = box(b'ilst', struct.pack('>I', 8 + len(_dbox)) + struct.pack('>I', 1) + _dbox)
meta = box(b'meta', struct.pack('>I', 0) + keys + ilst)
qt = box(b'ftyp', b'isom' + b'\x00' * 8) + box(b'moov', box(b'udta', meta))
qtags = comfy_meta._mp4_tags(qt, comfy_meta._mp4_find_box(qt, b'moov', 0, len(qt)))
check('the QuickTime indexed form still reads', qtags.get('prompt'), GRAPH)

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
