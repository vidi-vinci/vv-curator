"""Regression test for reading a ComfyUI video's OWN metadata, and for the picture it becomes.

Run: python tests/test_video_meta.py

Why this test exists. This codebase spent its whole life believing a video carries nothing — it is
why the `.txt` sidecar was invented, and why a video shows no model, seed or settings. That belief
is false for anything ComfyUI saved: its video output carries the same two graphs a PNG does. Proved
2026-08-12 against a real MiniMax H3 file, where feeding the video's own `prompt` to the existing
reader yielded the model, steps, sampler, scheduler and seed with no new parsing at all.

What that buys immediately is the drag: a browser will only carry a file between windows when the
file is the one behind the <img> being dragged, so a video can never be dragged out as itself. It
CAN be dragged as a picture of its first frame carrying its own workflow — which is what ComfyUI
reads anyway.

Everything here is built in-process: the MP4 is assembled box by box and the frame is a PIL image,
so the test needs no ffmpeg, no sample file, and no library.
"""
import io
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import comfy_meta  # noqa: E402

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def box(typ, payload):
    return struct.pack('>I', len(payload) + 8) + typ + payload


def mvhd(seconds=None, version=0, timescale=600, unknown=False):
    """A structurally real `mvhd`. seconds=None keeps the all-zero box the other fixtures use,
    which is itself a case worth having: timescale 0 must not divide by zero."""
    if seconds is None:
        return box(b'mvhd', bytes(100))
    dur = int(round(seconds * timescale))
    if unknown:
        dur = 0xFFFFFFFFFFFFFFFF if version == 1 else 0xFFFFFFFF
    if version == 1:
        payload = (bytes([1]) + bytes(3) + bytes(8) + bytes(8)
                   + struct.pack('>I', timescale) + struct.pack('>Q', dur))
    else:
        payload = (bytes(4) + bytes(4) + bytes(4)
                   + struct.pack('>I', timescale) + struct.pack('>I', dur))
    return box(b'mvhd', payload)


def make_bare_mp4(mvhd_box):
    """An MP4 with a moov and NO udta -- a video from anything that isn't ComfyUI. Every tag
    reader returns {} for this, and it still has a length."""
    return box(b'ftyp', b'isom' + bytes(8)) + box(b'moov', mvhd_box) + box(b'mdat', bytes(4096))


def make_mp4(tags, moov_first=True, junk_before=b'', mvhd_box=None):
    """A structurally real MP4 carrying `tags` the way ComfyUI's SaveVideo writes them:
    moov > udta > meta > (keys naming each entry, ilst holding the values in the same order)."""
    names = list(tags)
    keys_payload = struct.pack('>I', 0) + struct.pack('>I', len(names))
    for n in names:
        nb = n.encode('utf-8')
        keys_payload += struct.pack('>I', len(nb) + 8) + b'mdta' + nb
    keys = box(b'keys', keys_payload)

    ilst_payload = b''
    for i, n in enumerate(names, start=1):
        val = tags[n].encode('utf-8')
        # data box: size + 'data' + type(4) + locale(4) + payload
        data = box(b'data', struct.pack('>I', 1) + struct.pack('>I', 0) + val)
        ilst_payload += struct.pack('>I', len(data) + 8) + struct.pack('>I', i) + data
    ilst = box(b'ilst', ilst_payload)

    meta = box(b'meta', struct.pack('>I', 0) + box(b'hdlr', b'\x00' * 8 + b'mdtaappl') + keys + ilst)
    moov = box(b'moov', (mvhd_box or mvhd()) + box(b'udta', meta))
    ftyp = box(b'ftyp', b'isom' + b'\x00' * 8)
    mdat = box(b'mdat', b'\x00' * 4096)
    return ftyp + junk_before + (moov + mdat if moov_first else mdat + moov)


print('\nA video carries its own ComfyUI metadata\n')

WF = json.dumps({'nodes': [{'type': 'SaveVideo'}], 'links': []})
API = json.dumps({'9': {'class_type': 'SaveVideo', 'inputs': {}}})

# ---- 1. the parser reads what ComfyUI writes --------------------------------------------------
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_meta.mp4')
with open(p, 'wb') as f:
    f.write(make_mp4({'workflow': WF, 'prompt': API, 'encoder': 'Lavf60'}))
got = comfy_meta.read_video_meta(p)
check('reads the tags out of an MP4', set(got) >= {'workflow', 'prompt'}, sorted(got))
check('  workflow comes back byte-identical', got.get('workflow') == WF)
check('  prompt comes back byte-identical', got.get('prompt') == API)
check('  and the values are matched to the RIGHT keys', json.loads(got['prompt']) == json.loads(API),
      'keys and values are indexed separately — an off-by-one swaps them silently')

# ---- 2. it never raises, whatever it is handed ------------------------------------------------
# This runs per file during a scan; one weird video must not abort the whole thing.
bad = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_bad.mp4')
with open(bad, 'wb') as f:
    f.write(b'not an mp4 at all, just bytes' * 10)
check('junk file yields {} rather than raising', comfy_meta.read_video_meta(bad) == {})
truncated = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_trunc.mp4')
with open(truncated, 'wb') as f:
    f.write(make_mp4({'workflow': WF})[:120])
check('truncated file yields {} rather than raising', comfy_meta.read_video_meta(truncated) == {})
check('missing file yields {}', comfy_meta.read_video_meta(os.path.join(ROOT, 'nope.mp4')) == {})
check('a PNG is not probed as a video', comfy_meta.read_video_meta(os.path.join(ROOT, 'README.md')) == {})

# A video with no ComfyUI tags at all — a phone clip — must read as empty, not as garbage.
plain = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_plain.mp4')
with open(plain, 'wb') as f:
    f.write(make_mp4({'encoder': 'Lavf60'}))
check('a video with no graphs reports none', 'workflow' not in comfy_meta.read_video_meta(plain))

# ---- 3. the graph a video carries feeds the EXISTING reader ------------------------------------
# The whole reason this is worth having: no new tracer, just a new place to find the same JSON.
res = {'loras': [], 'positive': None, 'negative': None, 'model': None, 'model_name': None,
       'method': None, 'set_id': None, 'set_stage': None,
       'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
sampler_graph = json.dumps({
    '1': {'class_type': 'KSampler',
          'inputs': {'steps': 20, 'cfg': 7.5, 'sampler_name': 'res_multistep',
                     'scheduler': 'simple', 'seed': 985535748215606,
                     'model': ['2', 0], 'positive': ['3', 0], 'negative': ['4', 0],
                     'latent_image': ['5', 0]}},
    '2': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'minimax_h3.safetensors'}},
    '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a cat'}},
    '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    '5': {'class_type': 'EmptyLatentImage', 'inputs': {}},
})
comfy_meta._from_graph(sampler_graph, res)
check('a video graph traces like an image graph', res['steps'] == 20 and res['seed'] == 985535748215606,
      res)
check('  including the model', (res['model_name'] or '').startswith('minimax_h3'), res['model_name'])

# ---- 4. the picture a video becomes, for dragging ---------------------------------------------
# ComfyUI's loader reads tEXt chunks. iTXt/zTXt would look fine in Pillow and be invisible to it.
import server  # noqa: E402  (imported late: it reads env at import time)
from PIL import Image, PngImagePlugin  # noqa: E402

vid = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_drag.mp4')
with open(vid, 'wb') as f:
    f.write(make_mp4({'workflow': WF, 'prompt': API}))
server.thumbs_mod._video_poster_image = lambda path: Image.new('RGB', (64, 48), (10, 20, 30))
png = server._build_drag_png(vid)
check('a video becomes a real PNG', png[:8] == b'\x89PNG\r\n\x1a\n')

i, chunks = 8, []
while i + 8 <= len(png):
    ln, typ = struct.unpack('>I4s', png[i:i + 8])
    if typ in (b'tEXt', b'iTXt', b'zTXt'):
        chunks.append((typ.decode(), png[i + 8:i + 8 + ln].split(b'\x00')[0].decode()))
    i += 12 + ln
    if typ == b'IEND':
        break
kinds = {t for t, _ in chunks}
check('  carrying tEXt chunks, which is what ComfyUI reads', kinds == {'tEXt'},
      'wrote %s — Pillow shows these fine and ComfyUI would not see them' % (kinds or 'nothing'))
check('  named workflow and prompt', {k for _, k in chunks} == {'workflow', 'prompt'}, chunks)

im = Image.open(io.BytesIO(png))
check('  and the workflow survives the round trip', im.text.get('workflow') == WF)
check('  as does the prompt graph', im.text.get('prompt') == API)

# ---- the same rule, against text that actually tests it -------------------------------------
# THE tEXt CHECK ABOVE PASSED FOR YEARS WITHOUT PROVING ANYTHING, because every graph it was given
# was pure ASCII and PIL writes tEXt for those whatever you do. `PngInfo.add_text` switches
# SILENTLY to iTXt the moment the string leaves Latin-1 — one curly apostrophe does it, and song
# lyrics are full of them — and ComfyUI reads only tEXt, so the drop is accepted and nothing loads.
# Our own reader takes all three chunk types, so the app would show such a file perfectly while
# ComfyUI saw nothing: the two halves disagreeing is exactly what makes this invisible.
# Zero hits is not evidence until the pattern has caught something it should.
import json as _json  # noqa: E402

CURLY = _json.dumps({'1': {'inputs': {'text': 'don’t stop \U0001F3B5'}}}, ensure_ascii=False)
info = PngImagePlugin.PngInfo()
server._add_graph_chunk(info, 'workflow', CURLY)
buf = io.BytesIO()
Image.new('RGB', (8, 8)).save(buf, 'PNG', pnginfo=info)
kinds2 = set()
raw = buf.getvalue()
i = 8
while i < len(raw):
    ln, typ = struct.unpack('>I4s', raw[i:i + 8])
    if typ in (b'tEXt', b'iTXt', b'zTXt'):
        kinds2.add(typ.decode())
    i += 12 + ln
    if typ == b'IEND':
        break
check('a graph holding a curly quote is still written as tEXt', kinds2 == {'tEXt'},
      'wrote %s — ComfyUI would accept the drop and load nothing' % (kinds2 or 'nothing'))
check('  and still parses to the same graph',
      _json.loads(Image.open(io.BytesIO(raw)).text['workflow']) == _json.loads(CURLY))
# The bare call is what the guard replaces, so pin that it really does differ — otherwise this
# test would keep passing if someone dropped _add_graph_chunk and went back to add_text.
bare = PngImagePlugin.PngInfo()
bare.add_text('workflow', CURLY)
buf2 = io.BytesIO()
Image.new('RGB', (8, 8)).save(buf2, 'PNG', pnginfo=bare)
check('  and add_text alone would NOT have', b'iTXt' in buf2.getvalue())

# ---- the cache must not outlive the builder ---------------------------------------------------
# A drag picture is cached under data/, and the key was the source file's path and date ALONE. So
# the picture was built once and served for ever, and BOTH chunk-type fixes above landed behind an
# entry written before them: the author updated, retested, and was handed the identical broken
# picture twice, with nothing on screen to say it was stale. The builder's version number is part
# of the key now. This pins that it participates at all — the failure it prevents is silent, and a
# cache that ignores the thing that fills it looks perfectly healthy from the outside.
import hashlib  # noqa: E402

_k = lambda build: hashlib.sha1(('/some/song.flac|1700000000|%s' % build).encode()).hexdigest()
check('bumping the drag-picture build changes its cache key', _k(1) != _k(2))
check('  and the current build is wired into server', isinstance(server._DRAGPNG_BUILD, int))

# A video with NO graphs must still produce a usable picture, just without chunks — the drag should
# degrade to "a picture of the frame", never to an error.
with open(vid, 'wb') as f:
    f.write(make_mp4({'encoder': 'Lavf60'}))
png2 = server._build_drag_png(vid)
check('a video with no graphs still yields a PNG', png2[:8] == b'\x89PNG\r\n\x1a\n')
check('  with no text chunks invented', not Image.open(io.BytesIO(png2)).text)

# ---- 5. THE SAFETY RULE: this may only ever ADD ----------------------------------------------
# Carried over from the generation-settings work, and it is the thing that decides whether this is
# safe to ship: a library showing a sidecar's prompt today must show the SAME prompt afterwards.
# Reading a video's graph is allowed to fill blanks; it is never allowed to rewrite an answer.
import shutil  # noqa: E402

TMPDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_side')
shutil.rmtree(TMPDIR, ignore_errors=True)
os.makedirs(TMPDIR)
withside = os.path.join(TMPDIR, 'clip_00001_.mp4')
with open(withside, 'wb') as f:
    f.write(make_mp4({'workflow': WF, 'prompt': sampler_graph}))
# A sidecar disagreeing with the graph on every field the two can both answer.
with open(os.path.join(TMPDIR, 'clip_00001_.txt'), 'w', encoding='utf-8') as f:
    # Sampler and scheduler are ONE A1111 field ("euler karras"), not two — writing them separately
    # leaves the scheduler unparsed, which made this test pass for the wrong reason once.
    f.write('a prompt only the sidecar knows\n'
            'Negative prompt: sidecar negative\n'
            'Steps: 99, Sampler: euler karras, CFG scale: 3.5, '
            'Seed: 111, Model: sidecarmodel\n'
            'VV set id: vvabc123\n')

before = comfy_meta.read_sidecar(withside)
after = comfy_meta.extract_video(withside)
check('the sidecar prompt is untouched', after['positive'] == before['positive'], after['positive'])
check('  and its negative', after['negative'] == before['negative'], after['negative'])
check('  and its model', after['model_name'] == before['model_name'], after['model_name'])
check('  and its seed', after['seed'] == before['seed'], after['seed'])
check('  and its steps', after['steps'] == before['steps'], after['steps'])
check('  and its sampler/scheduler',
      after['sampler_name'] == before['sampler_name'] and after['scheduler'] == before['scheduler'],
      (after['sampler_name'], after['scheduler']))
check('  and the set id, which only it can supply', after['set_id'] == 'vvabc123', after['set_id'])
# `method` is exempt on purpose: it is the diagnostic trail, not data anyone sees, and recording
# that both sources contributed is exactly its job.
nonempty_before = {k for k, v in before.items() if v not in (None, '', [])} - {'method'}
kept = all(after[k] == before[k] for k in nonempty_before)
check('NOTHING the sidecar said was overwritten', kept,
      {k: (before[k], after[k]) for k in nonempty_before if before[k] != after[k]})
check('  and a field the sidecar left blank WAS filled from the video',
      before['scheduler'] is None or after['scheduler'] is not None,
      'the video should fill gaps, otherwise the feature does nothing')
check('  and the trail says both were involved', 'sidecar' in (after['method'] or ''), after['method'])

# The other half of the rule: with NO sidecar, the video must actually contribute something.
nosidecar = os.path.join(TMPDIR, 'lonely_00001_.mp4')
with open(nosidecar, 'wb') as f:
    f.write(make_mp4({'workflow': WF, 'prompt': sampler_graph}))
solo = comfy_meta.extract_video(nosidecar)
check('with no sidecar the video answers for itself',
      solo['steps'] == 20 and solo['seed'] == 985535748215606
      and (solo['model_name'] or '').startswith('minimax_h3'), solo)
check('  and traces its prompt when the workflow allows', solo['positive'] == 'a cat', solo['positive'])
shutil.rmtree(TMPDIR, ignore_errors=True)


# ---- how long is the clip -----------------------------------------------------------------------
# Read from `mvhd`, a direct child of `moov`, during the SAME bounded head read that already fetches
# the workflow. The card shows it; nothing decodes anything.
print()
print('A video says how long it is')

DURDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_dur')
shutil.rmtree(DURDIR, ignore_errors=True)
os.makedirs(DURDIR)


def dur_of(data, name='clip.mp4'):
    f = os.path.join(DURDIR, name)
    with open(f, 'wb') as fh:
        fh.write(data)
    return comfy_meta.read_video_file(f)[1]


d_v0 = dur_of(make_mp4({'prompt': API}, mvhd_box=mvhd(12.5)))
check('a v0 mvhd gives the length', d_v0 == 12.5, d_v0)
d_v1 = dur_of(make_mp4({'prompt': API}, mvhd_box=mvhd(12.5, version=1)))
check('a v1 (64-bit) mvhd agrees', d_v1 == 12.5, d_v1)

# THE CASE THE EARLY RETURNS USED TO SWALLOW, and the reason the reader was factored at all.
# read_video_meta bails the moment udta/meta/keys/ilst are missing -- which is EVERY video that did
# not come out of ComfyUI. Those still have a duration, and it has to survive.
bare = make_bare_mp4(mvhd(90.0))
d_bare = dur_of(bare, 'bare.mp4')
check('a video with NO ComfyUI metadata still reports its length', d_bare == 90.0, d_bare)
bare_tags = comfy_meta.read_video_meta(os.path.join(DURDIR, 'bare.mp4'))
check('  ...while still reporting no tags at all', bare_tags == {}, bare_tags)

d_zero = dur_of(make_mp4({'prompt': API}, mvhd_box=mvhd()))
check('timescale 0 is no answer, not a crash', d_zero is None, d_zero)
d_unk0 = dur_of(make_mp4({'prompt': API}, mvhd_box=mvhd(1, unknown=True)))
check('a v0 "unknown" duration is no answer', d_unk0 is None, d_unk0)
d_unk1 = dur_of(make_mp4({'prompt': API}, mvhd_box=mvhd(1, version=1, unknown=True)))
check('a v1 "unknown" duration is no answer', d_unk1 is None, d_unk1)
d_webm = dur_of(bare, 'clip.webm')
check('a WebM says nothing, as documented', d_webm is None, d_webm)

# The scan takes duration from extract_video, where it is assigned BEFORE the no-prompt early
# return. That one line is what a future refactor is likeliest to move.
sidecar_only = os.path.join(DURDIR, 'sidecar_only_00001_.mp4')
with open(sidecar_only, 'wb') as fh:
    fh.write(make_bare_mp4(mvhd(7.25)))
with open(os.path.join(DURDIR, 'sidecar_only_00001_.txt'), 'w', encoding='utf-8') as fh:
    fh.write('''a horse
Steps: 20, Model: someModel''')
d_side = comfy_meta.extract_video(sidecar_only).get('duration')
check('extract_video carries duration even with no graph to trace', d_side == 7.25, d_side)

# ONE FILE, ONE READER. read_video_file exists because asking for tags and length separately would
# open a network-share file twice per video, every scan.
import builtins
_real_open, _opens = builtins.open, []
try:
    builtins.open = lambda f, *a, **k: (_opens.append(f), _real_open(f, *a, **k))[1]
    comfy_meta.extract_video(sidecar_only)
finally:
    builtins.open = _real_open
check('the video itself is opened at most twice (the two-step head read)',
      sum(1 for f in _opens if str(f).endswith('.mp4')) <= 2,
      [f for f in _opens if str(f).endswith('.mp4')])

shutil.rmtree(DURDIR, ignore_errors=True)

# ---- how big is the picture ---------------------------------------------------------------------
# Read from the video track's `tkhd`, in the SAME head read as the tags and the length.
#
# THE BUG THIS EXISTS FOR, reported by the author 2026-08-25: "the dimensions shown on the card are for
# the still image, NOT the video. And it is the video that is most important. It tells me what the
# relative quality is."
#
# A video's size used to arrive only from the POSTER FRAME, backfilled when a thumbnail was made.
# That never fires for a video paired with a still, because a pair card is fronted by the still --
# so the videos he generates most had no dimensions recorded anywhere, and their cards showed the
# img2video SOURCE's size instead of the output's.
#
# Built by hand here, like every other fixture in this file, so the test needs no ffmpeg. The parser
# was additionally verified during development against real libx264 output at five sizes, plus the
# two shapes where a declared size can diverge from the coded one: a rotated clip (which does NOT
# diverge -- rotation lives in the matrix) and an anamorphic one (which does, by design).
print()
print('A video says how big it is')

DIMDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_dim')
shutil.rmtree(DIMDIR, ignore_errors=True)
os.makedirs(DIMDIR)


def tkhd(w, h, version=0):
    """A structurally real `tkhd`. 0x0 is how an AUDIO track declares itself."""
    if version == 1:
        head = bytes([1]) + bytes(3) + bytes(8) + bytes(8) + bytes(4) + bytes(4) + bytes(8)
    else:
        head = bytes(4) + bytes(4) + bytes(4) + bytes(4) + bytes(4) + bytes(4)
    tail = bytes(8) + bytes(2) + bytes(2) + bytes(2) + bytes(2) + bytes(36)
    # 16.16 fixed point, which is the detail a hand-written reader gets wrong.
    return box(b'tkhd', head + tail + struct.pack('>I', w << 16) + struct.pack('>I', h << 16))


def with_traks(*tkhds, **kw):
    """A bare MP4 whose moov carries one `trak` per tkhd given."""
    moov = box(b'moov', mvhd(kw.get('seconds', 3.0)) + b''.join(box(b'trak', t) for t in tkhds))
    return box(b'ftyp', b'isom' + bytes(8)) + moov + box(b'mdat', bytes(4096))


def dims_of(data, name='clip.mp4'):
    f = os.path.join(DIMDIR, name)
    with open(f, 'wb') as fh:
        fh.write(data)
    return comfy_meta.read_video_file(f)[2]


got = dims_of(with_traks(tkhd(832, 480)))
check('a video track gives its size', got == (832, 480), got)
got = dims_of(with_traks(tkhd(1280, 720, version=1)), 'v1.mp4')
check('a v1 (64-bit) tkhd agrees', got == (1280, 720), got)

# THE ONE THAT PICKS THE WRONG TRACK IF YOU TAKE THE FIRST tkhd YOU FIND. VHS writes an `-audio`
# file, so a muxed clip is the normal case here, not an exotic one -- and an audio track's tkhd is
# a perfectly valid box declaring 0x0.
got = dims_of(with_traks(tkhd(0, 0), tkhd(720, 1280)), 'audiofirst.mp4')
check('an audio track is skipped, whichever order it sits in', got == (720, 1280), got)

# A video with no ComfyUI metadata at all still has a size -- the same early-return trap the
# duration section above exists for.
got = dims_of(with_traks(tkhd(512, 512)), 'bare_meta.mp4')
check('a video with NO ComfyUI metadata still reports its size', got == (512, 512), got)
plain_tags = comfy_meta.read_video_meta(os.path.join(DIMDIR, 'bare_meta.mp4'))
check('  ...while still reporting no tags at all', plain_tags == {}, plain_tags)

# No answer is not a wrong answer.
got = dims_of(with_traks(tkhd(0, 0)), 'audioonly.mp4')
check('an audio-only file reports no size rather than 0x0', got == (None, None), got)
got = dims_of(box(b'ftyp', b'isom' + bytes(8)) + box(b'moov', mvhd(2.0)) + box(b'mdat', bytes(64)),
              'notrak.mp4')
check('a moov with no trak at all is no answer, not a crash', got == (None, None), got)
got = dims_of(with_traks(tkhd(640, 360)), 'clip.webm')
check('a WebM says nothing, as documented', got == (None, None), got)

# THE LINE THE SCAN ACTUALLY READS. index_db takes w/h from extract_video's result, and that
# assignment sits BEFORE the no-prompt early return for the same reason duration's does -- a clip
# with no graph still has a size, and moving it below would silently drop every one of them.
sc = os.path.join(DIMDIR, 'sidecar_dim_00001_.mp4')
with open(sc, 'wb') as fh:
    fh.write(with_traks(tkhd(1024, 576)))
with open(os.path.join(DIMDIR, 'sidecar_dim_00001_.txt'), 'w', encoding='utf-8') as fh:
    fh.write('a horse\nSteps: 20, Model: someModel')
res = comfy_meta.extract_video(sc)
check('extract_video carries the size with no graph to trace',
      (res.get('width'), res.get('height')) == (1024, 576),
      (res.get('width'), res.get('height')))

shutil.rmtree(DIMDIR, ignore_errors=True)

# ---- where the metadata SITS in the file --------------------------------------------------------
# THE BUG, found on the author's own library 2026-08-25: two LTX runs minutes apart, one reporting its
# length and size and the other reporting nothing whatever. The runs were identical. The FILES were
# 3.4MB and 4.5MB, and the reader gave up after 4MB.
#
# An MP4 keeps `moov` either before the media or after it, and ComfyUI's video output puts it AFTER
# -- so the metadata sits at an offset that is simply however long the clip happens to be. A prefix
# read therefore decides by VIDEO LENGTH whether a file has metadata, and decides silently: past the
# limit a video reports no model, no seed, no workflow, no dimensions and no duration, exactly as
# though it had never carried any.
#
# THE FIXTURE IS DELIBERATELY BIGGER THAN THE OLD 4MB REACH. A smaller one would pass just as well
# against a reader that had merely been given a larger prefix, which is the fix this test exists to
# reject: there is no prefix that is right, because the offset scales with the clip.
print()
print('The metadata is found wherever it sits in the file')

TAILDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_tail')
shutil.rmtree(TAILDIR, ignore_errors=True)
os.makedirs(TAILDIR)

BIG = 5 * 1024 * 1024          # past any prefix this reader has ever used
tail_mp4 = os.path.join(TAILDIR, 'moov_at_end.mp4')
payload = (box(b'ftyp', b'isom' + bytes(8))
           + box(b'mdat', bytes(BIG))
           + box(b'moov', mvhd(11.5) + box(b'udta', b'')))
with open(tail_mp4, 'wb') as fh:
    fh.write(payload)
check('a clip whose moov sits past the head read still reports its length',
      comfy_meta.read_video_file(tail_mp4)[1] == 11.5, comfy_meta.read_video_file(tail_mp4)[1])

# The same, carrying real ComfyUI tags -- the half that decides whether a big video has a model.
tagged = make_mp4({'workflow': WF, 'prompt': API}, moov_first=False)
tagged = tagged[:tagged.index(b'mdat') - 4] + box(b'mdat', bytes(BIG)) + tagged[tagged.index(b'moov') - 4:]
big_tagged = os.path.join(TAILDIR, 'big_tagged.mp4')
with open(big_tagged, 'wb') as fh:
    fh.write(tagged)
got = comfy_meta.read_video_meta(big_tagged)
check('...and its workflow, which is what a big video used to lose entirely',
      sorted(got) == ['prompt', 'workflow'], sorted(got))

# THE FAST PATH MUST STAY A FAST PATH. A faststart file is answered by the head read alone, with
# no walk; a tail file walks. Both halves are asserted, because the interesting failure is the test
# passing for the wrong reason -- the first version of this wrapped the file handle in an object
# that was not a context manager, so `with open(...)` raised inside the reader, the read never
# happened, and "no seeks" was true because nothing had been done at all. It also held the handle
# open, which is what left a fixture on disk that Windows would not delete.
class _CountingFile(object):
    """Delegates everything and counts seeks. A REAL context manager, deliberately."""
    def __init__(self, f):
        self.f, self.seeks = f, 0

    def __enter__(self):
        self.f.__enter__()
        return self

    def __exit__(self, *a):
        return self.f.__exit__(*a)

    def read(self, *a):
        return self.f.read(*a)

    def seek(self, *a):
        self.seeks += 1
        return self.f.seek(*a)


front = os.path.join(TAILDIR, 'faststart.mp4')
with open(front, 'wb') as fh:
    fh.write(make_mp4({'prompt': API}, mvhd_box=mvhd(4.0)))


def seeks_reading(path):
    """(seeks, duration) for one read, so a count is only ever trusted beside a real answer."""
    import builtins
    real, seen = builtins.open, []

    def spy(f, *a, **k):
        h = real(f, *a, **k)
        if str(f).endswith('.mp4'):
            h = _CountingFile(h)
            seen.append(h)
        return h
    try:
        builtins.open = spy
        dur = comfy_meta.read_video_file(path)[1]
    finally:
        builtins.open = real
    return sum(h.seeks for h in seen), dur


n_front, d_front = seeks_reading(front)
check('a faststart clip is answered by the head read, with no walk',
      (n_front, d_front) == (0, 4.0), (n_front, d_front))
n_tail, d_tail = seeks_reading(tail_mp4)
check('  ...while a tail one does walk, so the count above means something',
      n_tail > 0 and d_tail == 11.5, (n_tail, d_tail))

# A file that is not a video, and a chain that lies about its own box sizes, must be no answer
# rather than a crash or a walk off the end.
broken = os.path.join(TAILDIR, 'broken.mp4')
with open(broken, 'wb') as fh:
    fh.write(box(b'ftyp', b'isom' + bytes(8)) + struct.pack('>I', 999999) + b'mdat' + bytes(64))
check('a box claiming more than the file holds is no answer, not a crash',
      comfy_meta.read_video_file(broken) == ({}, None, (None, None)),
      comfy_meta.read_video_file(broken))

shutil.rmtree(TAILDIR, ignore_errors=True)

for f in (p, bad, truncated, plain, vid):
    try:
        os.remove(f)
    except OSError:
        pass
print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
