"""test_loader_shape.py — an unrecognised loader must not cost you the model and the LoRAs.

Run: python test_loader_shape.py

THE BUG THIS EXISTS TO STOP COMING BACK. Checkpoint and LoRA loaders were recognised by a list of
known class names with no fallback. A workflow using anything else — an NF4 checkpoint loader, a
GGUF wrapper, Nunchaku, one of the "efficiency" bundles, any pack with its own loader node — yielded
**no model and no LoRAs**. The image landed under "(none)", its Civitai export carried no resources
to auto-link, and nothing anywhere said why. Custom loaders are as common as custom samplers.

The sampler reader was burned by exactly this (`ClownsharKSampler_Beta`: a perfectly ordinary
sampler, ignored for its name) and was fixed by testing SHAPE instead of extending the list. This is
that fix applied to loaders, and this test is what stops someone "tidying" it back into a list.

The two halves are equally load-bearing, so both are asserted here:

  * **Recognise the unknown ones** — a class that calls itself a loader and carries a model-ish key
    whose value looks like a model file.
  * **Refuse the look-alikes** — VAE, CLIP, ControlNet, style and LoRA loaders all say "loader" and
    all carry a `*_name` filename. The upscale-model loader is the dangerous one: it keeps its
    filename in `model_name`, which passes every gate except the class-name exclusion, and the
    any-loader-in-the-graph fallback iterates EVERY node. Wrong is worse than empty — it would put
    "4x-UltraSharp" in the Civitai export as the checkpoint.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta as cm   # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


def node(ct, **inputs):
    return {'class_type': ct, 'inputs': inputs}


print('\nLoader recognition by shape, not by name\n')

# --- the classes that were already known: unchanged behaviour ------------------------------------
for ct, key in cm.LOADER_KEYS.items():
    check('known class still reads its filename: ' + ct,
          cm._loader_filename(node(ct, **{key: 'sub/model.safetensors'})) == 'sub/model.safetensors')

# --- the ones that used to vanish ----------------------------------------------------------------
unknown = [
    ('CheckpointLoaderNF4', {'ckpt_name': 'flux-nf4.safetensors'}, 'flux-nf4.safetensors'),
    ('NunchakuFluxDiTLoader', {'model_path': 'svdq/flux.safetensors'}, 'svdq/flux.safetensors'),
    ('Efficient Loader', {'ckpt_name': 'sd15/dream.ckpt', 'lora_name': 'x.safetensors'}, 'sd15/dream.ckpt'),
    ('CR Load Checkpoint', {'ckpt_name': 'pony/xl.safetensors'}, 'pony/xl.safetensors'),
    ('UnetLoaderGGUFDisTorchMultiGPU', {'unet_name': 'flux-q8.gguf'}, 'flux-q8.gguf'),
]
for ct, inputs, want in unknown:
    check('unknown loader is read on shape: ' + ct,
          cm._loader_filename(node(ct, **inputs)) == want,
          repr(cm._loader_filename(node(ct, **inputs))))

# --- the look-alikes, which must stay refused -----------------------------------------------------
lookalikes = [
    ('VAELoader', {'vae_name': 'ae.safetensors'}),
    ('CLIPLoader', {'clip_name': 't5xxl.safetensors'}),
    ('ControlNetLoader', {'control_net_name': 'canny.safetensors'}),
    ('StyleModelLoader', {'style_model_name': 's.safetensors'}),
    ('LoraLoader', {'lora_name': 'l.safetensors'}),
    ('UpscaleModelLoader', {'model_name': '4x-UltraSharp.pth'}),
    ('CheckpointSave', {'ckpt_name': 'out.safetensors'}),          # a saver, not a loader
    ('SomeLoader', {'model_name': 'Realistic Vision'}),            # a label, not a file
]
for ct, inputs in lookalikes:
    check('look-alike stays refused: ' + ct,
          cm._loader_filename(node(ct, **inputs)) is None,
          repr(cm._loader_filename(node(ct, **inputs))))

# --- LoRAs ----------------------------------------------------------------------------------------
for ct in ('LoraLoader', 'LoraLoaderModelOnly', 'LoraLoader (JPS)', 'LoraLoader|pysssss',
           'CR LoRA Stack', 'LoraLoaderBlockWeight //Inspire'):
    check('lora node recognised: ' + ct, cm._is_lora_node(node(ct, lora_name='a.safetensors')) is True)
check('a lora node with no filename is not one', cm._is_lora_node(node('LoraLoader')) is False)
check('a checkpoint loader is not a lora node',
      cm._is_lora_node(node('CheckpointLoaderSimple', ckpt_name='a.safetensors')) is False)

# --- end to end, on the graph shape the reader actually walks --------------------------------------
# A whole generation whose EVERY node is unrecognised by name: an NF4 checkpoint, a third-party LoRA
# loader, and a custom sampler. Before the shape tests this graph yielded no model and no LoRAs.
g = {
    '1': node('CheckpointLoaderNF4', ckpt_name='flux/flux-dev-nf4.safetensors'),
    '2': node('LoraLoaderModelOnly', model=['1', 0], lora_name='style/filmgrain.safetensors',
              strength_model=0.8),
    '3': node('CLIPTextEncode', text='a cat on a bench', clip=['1', 1]),
    '4': node('CLIPTextEncode', text='blurry', clip=['1', 1]),
    '5': node('EmptyLatentImage', width=1024, height=1024),
    '6': node('ClownsharKSampler_Beta', model=['2', 0], positive=['3', 0], negative=['4', 0],
              latent_image=['5', 0], steps=28, cfg=3.5, seed=42,
              sampler_name='res_2s', scheduler='beta57'),
    '7': node('SaveImage', images=['6', 0]),
}
res = {'loras': [], 'positive': None, 'negative': None, 'model': None, 'model_name': None,
       'method': None, 'set_id': None, 'set_stage': None, 'steps': None, 'cfg': None,
       'sampler_name': None, 'scheduler': None, 'seed': None}
import json  # noqa: E402
cm._from_graph(json.dumps(g), res)
check('end to end: the model is read', (res['model'] or '').endswith('flux-dev-nf4.safetensors'), res['model'])
check('end to end: the LoRA is read', [l['name'] for l in res['loras']] == ['filmgrain'], res['loras'])
check('end to end: the prompt is read', res['positive'] == 'a cat on a bench', res['positive'])

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
