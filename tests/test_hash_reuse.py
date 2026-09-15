"""test_hash_reuse.py — the model-hash reuse used by Export for Civitai (model_hash.py).

Export needs each model's AutoV2 hash, which normally means reading the whole file (20+GB for a
Flux checkpoint). Two ways it can skip that: the comfy_vv_saver node's own cache, and the same model
already hashed under a different folder path.

A wrong hash here would silently mis-link a resource on Civitai, so the load-bearing claim is
asserted directly: a reused hash is identical to one obtained by actually reading the file.

Run: python test_hash_reuse.py
"""
import json
import os
import shutil
import tempfile

import sys
# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comfy_meta
import model_hash

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def write(path, data):
    with open(path, 'wb') as f:
        f.write(data)
    return path


def graph_png(path, ckpt):
    """A minimal PNG carrying a ComfyUI graph that loads `ckpt`, so the real export chain can run."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a test prompt"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad hands"}},
        "4": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "steps": 20, "cfg": 7.0, "sampler_name": "euler",
                         "scheduler": "normal", "seed": 12345}},
    }
    info = PngInfo()
    info.add_text('prompt', json.dumps(graph))
    Image.new('RGB', (8, 8), (20, 20, 20)).save(path, pnginfo=info)
    return path


def main():
    tmp = tempfile.mkdtemp(prefix='vv_hash_')
    body = b'a model file, pretend it is 20GB' * 500

    # A ComfyUI layout: models/ beside custom_nodes/comfy_vv_saver/, as the saver installs.
    comfy = os.path.join(tmp, 'Comfy')
    models = os.path.join(comfy, 'models')
    ckpts = os.path.join(models, 'checkpoints'); os.makedirs(ckpts)
    node = os.path.join(comfy, 'custom_nodes', 'comfy_vv_saver'); os.makedirs(node)
    saver_db = os.path.join(node, 'model_hashes.db')
    model = write(os.path.join(ckpts, 'flux1_dev.safetensors'), body)

    print('\nfinding the saver cache')
    saver_hash = model_hash.sha256_cached(model, saver_db)      # as the node does, at save time
    found = model_hash.saver_caches([models])
    check('located from the models folder, no configuration', found == [saver_db])
    check('a models folder with no saver node yields nothing', model_hash.saver_caches([tmp]) == [])
    check('blank/None entries are ignored', model_hash.saver_caches(['', None]) == [])

    print('\nreusing instead of reading')
    ours = os.path.join(tmp, 'ours.db')
    check('cold, we do not know this model', model_hash.cached_hash(model, ours) is None)
    check('the saver cache answers it', model_hash.cached_hash(model, ours, found) == saver_hash)
    check('and it is remembered, so the saver db is not needed again',
          model_hash.cached_hash(model, ours) == saver_hash)

    other = os.path.join(tmp, 'other'); os.makedirs(other)
    copy = os.path.join(other, 'flux1_dev.safetensors')          # same model, another folder path
    shutil.copyfile(model, copy)
    os.utime(copy, (0, 0))                                       # a copy's mtime differs; must not matter
    reused = model_hash.cached_hash(copy, ours)
    check('the same model under another path is answered from cache', reused is not None)
    check('THE CLAIM: the reused hash equals a genuine read of that file',
          reused == model_hash._sha256(copy))

    print('\nreuse is refused when identity is not certain')
    nearly = os.path.join(other, 'nearly'); os.makedirs(nearly)
    bigger = write(os.path.join(nearly, 'flux1_dev.safetensors'), body + b'!')  # name matches, size not
    renamed = write(os.path.join(other, 'something_else.safetensors'), body)    # size matches, name not
    check('same name, size off by one byte -> no reuse', model_hash.cached_hash(bigger, ours) is None)
    check('same size, different name -> no reuse', model_hash.cached_hash(renamed, ours) is None)
    check('a junk file as a saver cache is declined, not raised',
          model_hash.cached_hash(model, os.path.join(tmp, 'c1.db'),
                                 [write(os.path.join(tmp, 'junk.db'), b'not sqlite')]) is None)
    check('a missing saver cache is declined, not raised',
          model_hash.cached_hash(model, os.path.join(tmp, 'c2.db'),
                                 [os.path.join(tmp, 'nope.db')]) is None)

    print('\nthe real export chain, reused vs genuinely read')
    png = graph_png(os.path.join(tmp, 'shot.png'), 'flux1_dev.safetensors')
    check('the graph resolves to the staged checkpoint',
          comfy_meta.list_resources(png) == [('checkpoints', 'flux1_dev.safetensors')])
    # exactly the resolver server.py builds for an export
    cold = os.path.join(tmp, 'export_reuse.db')
    text = comfy_meta.build_civitai_parameters(
        png, 512, 512,
        hash_resolver=lambda c, r: model_hash.autov2_multi([models], c, r, cold, found))
    read = os.path.join(tmp, 'export_read.db')
    text_read = comfy_meta.build_civitai_parameters(
        png, 512, 512,
        hash_resolver=lambda c, r: model_hash.autov2_multi([models], c, r, read))
    check('the exported block carries a Model hash',
          ('Model hash: ' + saver_hash[:10]) in (text or ''))
    check('THE CLAIM: reused and genuinely-read exports are identical', text == text_read)

    shutil.rmtree(tmp, ignore_errors=True)
    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
