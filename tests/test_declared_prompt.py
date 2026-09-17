"""test_declared_prompt.py -- a prompt you STATED beats one we inferred, on our own files only.

Run: python tests/test_declared_prompt.py

THE REPORT, 2026-09-16. The author wired an Illustrious cover-art workflow to his YuE2 tracks: the cover
is generated from a short description, and a concat of the song's style and its lyrics is wired into
VV Run Name's `positive_prompt` as what the run is actually about. The node does its job -- that
text reaches the saver and lands in the PNG's `parameters` chunk. The app then threw it away. Its
graph walk had already found the cover workflow's own CLIPTextEncode, and `_fill_from_a1111` fills
gaps and never overwrites, so what he SAID the prompt was lost to what we GUESSED from the wiring.
He saw the short description and asked why his text "isn't coming through".

WHY THE TEST FOR "STATED" IS A DISAGREEMENT rather than a new marker. The node builds its chunk as

    pos = positive if positive else _text_from(g, sid, "positive")

so an unwired input writes the graph's own answer straight back: the two agree and there is nothing
to prefer. A wired one makes them diverge. That is what lets a rescan repair images already on disk
-- a marker written from today would only ever fix files saved after it.

THE TRAP THIS PINS HARDEST is the lora one, because it is invisible and library-wide.
`parse_a1111` lifts `<lora:name:weight>` out of the positive; the graph walk does not, since it
reads LoRAs from the loader nodes instead. A comment at that transform claims both behave alike.
Compare the two raw and EVERY VV-saved image whose prompt carries lora tags reads as a
disagreement -- so the "fix" would have quietly stripped those tags out of the prompt everywhere.
Both sides are normalised before comparing, and case 4 below is what holds that.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import Image, PngImagePlugin                              # noqa: E402
import comfy_meta as cm                                            # noqa: E402

fail = []
TMP = tempfile.mkdtemp(prefix='vv_declared_')

# REAL FILES THROUGH THE REAL ENTRY POINT, and that is not fussiness. The first version of this test
# called _prefer_declared_prompt() directly and applied the "is this our file" guard IN THE FIXTURE
# rather than reading it from the app. Deleting that guard from comfy_meta.py then left the test
# passing -- it was checking its own logic, not the program's. Mutation found it. Everything below
# goes through cm.extract() on a PNG carrying the chunks a real save writes.

NL = chr(10)


def graph(pos, neg):
    return json.dumps({
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'thing.safetensors'}},
        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': pos, 'clip': ['1', 1]}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': neg, 'clip': ['1', 1]}},
        '4': {'class_type': 'KSampler', 'inputs': {
            'positive': ['2', 0], 'negative': ['3', 0], 'model': ['1', 0],
            'seed': 1, 'steps': 20, 'cfg': 7.0,
            'sampler_name': 'euler', 'scheduler': 'normal'}},
    })


def block(pos, neg=''):
    """A `parameters` chunk in the shape our saver writes."""
    return NL.join([pos, 'Negative prompt: ' + neg,
                    'Steps: 20, Sampler: euler, CFG scale: 7, Seed: 1, Model: thing'])


def png(name, graph_pos, chunk_pos, ours=True, graph_neg='blurry', chunk_neg=''):
    info = PngImagePlugin.PngInfo()
    info.add_text('prompt', graph(graph_pos, graph_neg))
    info.add_text('parameters', block(chunk_pos, chunk_neg))
    if ours:
        info.add_text('vv_set_id', '20260916-120000-abcd')   # the stamp our saver writes
    path = os.path.join(TMP, name + '.png')
    Image.new('RGB', (8, 8)).save(path, pnginfo=info)
    return path


def check(name, path, expect_pos, expect_neg=None):
    res = cm.extract(path)
    if res.get('positive') != expect_pos:
        fail.append('%s: positive was %r, expected %r' % (name, res.get('positive'), expect_pos))
    if expect_neg is not None and res.get('negative') != expect_neg:
        fail.append('%s: negative was %r, expected %r' % (name, res.get('negative'), expect_neg))


GRAPH = 'album cover, minimal, warm light'
DECLARED = 'folk retro warm analog style, lo-fi production'

# 1. THE REPORTED CASE. The graph found the cover description; the chunk carries what he stated.
check('a stated prompt overrides the graph walk',
      png('stated', GRAPH, DECLARED), DECLARED)

# 2. THE ORDINARY CASE, which is most of the library: nothing was wired, so the node wrote the
#    graph's own answer back, the two agree, and nothing must move.
check('an unwired run is left exactly alone',
      png('unwired', GRAPH, GRAPH), GRAPH)

# 3. NOT OUR FILE -- no vv_set_id. A third-party `parameters` block still cannot overrule the graph,
#    which is the regression the whole guard is scoped to avoid. THIS is the case that caught a
#    fixture pretending to be the app.
check('a foreign parameters block cannot overrule the graph',
      png('foreign', GRAPH, DECLARED, ours=False), GRAPH)

# 4. THE LORA TRAP. The graph keeps <lora:...> in the positive; parse_a1111 lifts them out. Raw,
#    these two "disagree" and the prompt would lose its tags across the library. Normalised, they
#    agree and it stands.
LORA_POS = 'a lighthouse at dusk <lora:filmgrain:0.6>'
check('lora tags in the prompt are not a disagreement',
      png('lora', LORA_POS, 'a lighthouse at dusk'), LORA_POS)

# 5. A gap is still a gap: _fill_from_a1111 owns the empty case and must keep owning it.
check('an empty graph prompt still fills from the block',
      png('gap', '', DECLARED), DECLARED)

# 6. A placeholder is not a statement. The saver that wrote "unknown" for every field is on record.
check('a placeholder never overrides a real prompt',
      png('placeholder', GRAPH, 'unknown'), GRAPH)

# 7. The negative travels the same road.
check('a stated negative overrides too',
      png('neg', GRAPH, DECLARED, chunk_neg='watermark, text'), DECLARED, 'watermark, text')

print(NL + 'A stated prompt beats an inferred one, on our files only' + NL)
if fail:
    print('FAIL:')
    for f in fail:
        print('  - ' + f)
    sys.exit(1)
print('ok: the reported case is fixed, and an unwired run, a foreign block, a lora-tagged prompt '
      'and a placeholder are all left alone')
