"""test_civitai_resources.py -- an image downloaded from Civitai knows its own model.

Run: python tests/test_civitai_resources.py

THE BUG, found 2026-09-14 from three real files the author dropped into samples/. A Civitai download
carries no `Model:` field at all. Its checkpoint, LoRAs and VAE are in a `Civitai resources` JSON
array instead -- and that array was being destroyed before anything could read it, because the
settings row is split on commas outside double quotes and the array is full of exactly those:

    Civitai resources: [{"type":"checkpoint","modelVersionId":479474,"modelName":"..."}]

So the row parsed into a dozen nonsense keys ("weight", {"type", "modelversionname") and the image
reported a prompt, steps, sampler, CFG and seed with no model. Every such file was invisible to the
Model filter, which is half of what the rail is for. The author's #5 -- "model type unknown, mostly with
older files" -- and these downloads are a large part of it.

WHAT IS AND IS NOT TAKEN. The checkpoint's `modelName` verbatim, its LoRAs with their weights, and
its VAE. NOT the version: folding "v4.0" into the name would file two versions of one checkpoint as
two models, the opposite of what the filter is for. NOT `embed` entries: the app has no concept of
an embedding, and a list it cannot act on is noise.

UNDER `Model:`, NEVER OVER IT, which is the rule this whole parser already follows. A file carrying
both says the same thing twice and the plain field wins, so no image that shows a model today can
change. The test asserts that directly rather than trusting the ordering to stay put.
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import comfy_meta  # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    # ASCII for the report: this console is cp1252 and these model names carry emoji and Chinese
    # characters, so printing a failure verbatim would crash the run instead of describing it.
    def safe(v):
        return repr(v).encode('ascii', 'replace').decode()[:90]
    print(('  ok    ' if ok else '  FAIL  ') + name + ('' if ok else '\n          got %s, wanted %s' % (safe(got), safe(want))))
    if not ok:
        failures.append(name)


# A real block, copied from one of the samples and trimmed. Kept INLINE rather than reading
# samples/: that folder is git-ignored and holds the author's own pictures, so a test depending on
# it would pass on his machine and fail everywhere else.
CIVITAI = (
    'a woman in an abandoned shopping mall, symmetric composition\n'
    'Negative prompt: BadDream, UnrealisticDream\n'
    'Steps: 51, Sampler: DPM++ 2M Karras, CFG scale: 7, Seed: 1615520243, Size: 832x1216, '
    'Clip skip: 2, Civitai resources: ['
    '{"type":"checkpoint","modelVersionId":479474,"modelName":"STOIQO NewRealityXL",'
    '"modelVersionName":"XL 4.0"},'
    '{"type":"embed","weight":1,"modelVersionId":77169,"modelName":"BadDream","modelVersionName":"v1.0"},'
    '{"type":"lora","weight":0.5,"modelVersionId":135867,"modelName":"Detail Tweaker XL","modelVersionName":"v1.0"},'
    '{"type":"vae","modelVersionId":333245,"modelName":"SDXL VAE","modelVersionName":"SDXL-VAE"}'
    ']'
)

got = comfy_meta.parse_a1111(CIVITAI)
check('the checkpoint is read', got.get('model_name'), 'STOIQO NewRealityXL')
check('  without its version appended', 'XL 4.0' not in (got.get('model_name') or ''), True)
check('the VAE is read', got.get('vae_name'), 'SDXL VAE')
check('the LoRA is read', [l['name'] for l in got.get('loras', [])], ['Detail Tweaker XL'])
check('  with its weight', [l['strength'] for l in got.get('loras', [])], [0.5])
check('an embed is ignored', 'BadDream' not in json.dumps(got.get('loras', [])), True)

# The rest of the row must survive the extraction -- the whole bug was one field destroying its
# neighbours, so the neighbours are what this checks.
check('steps still parse', got.get('steps'), 51)
check('cfg still parses', got.get('cfg'), 7.0)
check('seed still parses', got.get('seed'), 1615520243)
check('the sampler still parses', got.get('sampler_name'), 'DPM++ 2M')
check('  and its scheduler split off', got.get('scheduler'), 'karras')
check('the prompt is intact', got.get('positive'),
      'a woman in an abandoned shopping mall, symmetric composition')
check('  and did not swallow the settings row', 'Civitai' not in (got.get('positive') or ''), True)
check('the negative is intact', got.get('negative'), 'BadDream, UnrealisticDream')

# `Model:` OUTRANKS the list. Same file, both present.
BOTH = CIVITAI.replace('Clip skip: 2,', 'Clip skip: 2, Model: realvisxlV40_v40Bakedvae,')
got2 = comfy_meta.parse_a1111(BOTH)
check('a plain Model: field still wins', got2.get('model_name'), 'realvisxlV40_v40Bakedvae')
check('  and the LoRAs are still picked up alongside it',
      [l['name'] for l in got2.get('loras', [])], ['Detail Tweaker XL'])

# Nothing about a file WITHOUT this block may change.
PLAIN = ('a prompt\nNegative prompt: bad\n'
         'Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 1, Size: 512x512, Model: foo')
got3 = comfy_meta.parse_a1111(PLAIN)
check('an ordinary A1111 block is untouched: model', got3.get('model_name'), 'foo')
check('  prompt', got3.get('positive'), 'a prompt')
check('  steps', got3.get('steps'), 20)

# Malformed JSON must cost the resources and nothing else. A truncated array is what a partial
# download or a re-encode leaves behind, and it must not take the seed with it.
BROKEN = CIVITAI.replace('"modelName":"STOIQO NewRealityXL"', '"modelName":')
got4 = comfy_meta.parse_a1111(BROKEN)
check('malformed resources: no model rather than a wrong one', got4.get('model_name'), None)
check('  and the seed still survives', got4.get('seed'), 1615520243)
check('  and the prompt still survives', got4.get('positive'),
      'a woman in an abandoned shopping mall, symmetric composition')

# A nested array must not end the scan early, and a following field must not be swallowed.
NESTED = CIVITAI + ', Civitai metadata: {"remixOfId":123}'
got5 = comfy_meta.parse_a1111(NESTED)
check('a field after the array is not swallowed', got5.get('model_name'), 'STOIQO NewRealityXL')
check('  and the seed is still there', got5.get('seed'), 1615520243)

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
