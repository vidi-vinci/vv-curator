"""test_vae.py -- which VAE a file reports, and why it is that one.

The detail view shows ONE VAE (the author's call, 2026-08-23), and the newest video models make that a
real choice rather than a formality: MiniMax H3 decodes video and audio through separate VAEs, so
a single graph holds two. The rule is the same one the base sampler follows -- decide by what the
node DOES, never by where it sits in the node list, because a saved graph is not ordered by the
pipeline. So this pins:

  * one VAELoader          -> that one, folder and extension stripped;
  * video + audio VAEs     -> the one decoding the PICTURE, whichever order they appear in;
  * a baked-in checkpoint  -> nothing, which is a fact ("this run didn't choose one") and not a gap;
  * an A1111 `VAE:` line   -> read, since a downloaded JPEG and a video's sidecar have no graph;
  * a loader that calls its file something else -> still found. WanVideoVAELoader uses
    `model_name`, which is ALSO what the checkpoint loaders use, so that key counts only on a
    node whose class says VAE loader. Both halves are pinned; assuming `vae_name` everywhere
    would have shipped a feature that is blank on half of the author's video work.

Run: python test_vae.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label + ('' if ok else '   got %r, want %r' % (got, want)))
    if not ok:
        _fails.append(label)
    return ok


def vae_of(graph):
    """What a file carrying this graph would report."""
    res = {'loras': [], 'positive': None, 'negative': None,
           'model': None, 'model_name': None, 'method': None,
           'set_id': None, 'set_stage': None, 'vae_name': None,
           'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
    comfy_meta._from_graph(json.dumps(graph), res)
    return res['vae_name']


def _sampler(model_from='1'):
    return {'class_type': 'KSampler',
            'inputs': {'model': [model_from, 0], 'positive': ['9', 0], 'negative': ['10', 0],
                       'steps': 20, 'cfg': 7.0, 'sampler_name': 'euler', 'scheduler': 'simple',
                       'seed': 12345}}


def main():
    print('one VAE loader')
    g = {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'sdxl/base.safetensors'}},
        '2': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'vae/sdxl_vae.safetensors'}},
        '3': {'class_type': 'VAEDecode', 'inputs': {'samples': ['4', 0], 'vae': ['2', 0]}},
        '4': _sampler(),
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a cat'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('the loader it decodes through', vae_of(g), 'sdxl_vae')

    print('\nthe folder and the extension are dropped, like the model')
    g2 = dict(g)
    g2['2'] = {'class_type': 'VAELoader', 'inputs': {'vae_name': 'sub/dir/ae.safetensors'}}
    check('shortened to its name', vae_of(g2), 'ae')

    print('\ntwo VAEs -- video and audio, as MiniMax H3 saves them')
    # The AUDIO loader is listed FIRST and given the lower node id, so "the first VAELoader" and
    # "the first node" both give the wrong answer and this test catches them.
    mm = {
        '1': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'audio/minimax_audio_vae.safetensors'}},
        '2': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'video/minimax_video_vae.safetensors'}},
        '3': {'class_type': 'VAEDecodeAudio', 'inputs': {'samples': ['7', 0], 'vae': ['1', 0]}},
        '4': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['2', 0]}},
        '7': _sampler('8'),
        '8': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'minimax_h3.safetensors'}},
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a dog running'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('the picture decodes, not the audio', vae_of(mm), 'minimax_video_vae')

    # ...and the same graph with the two decoders swapped in the dict, since dict order is the
    # thing being ruled out.
    mm2 = {k: mm[k] for k in ('4', '3', '2', '1', '7', '8', '9', '10')}
    check('and order in the file does not decide it', vae_of(mm2), 'minimax_video_vae')

    print('\na baked-in checkpoint VAE names nothing, so nothing is claimed')
    baked = {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'pony/ponyReal.safetensors'}},
        '3': {'class_type': 'VAEDecode', 'inputs': {'samples': ['4', 0], 'vae': ['1', 2]}},
        '4': _sampler(),
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a horse'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('no VAE reported', vae_of(baked), None)

    print('\na loader nothing decodes through is still better than nothing')
    orphan = {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'a.safetensors'}},
        '2': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'kept.safetensors'}},
        '4': _sampler(),
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'x'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('falls back to the only loader', vae_of(orphan), 'kept')

    print()
    print('loaders that name the file something other than vae_name')
    # Checked against the author's real install, not invented: WanVideoVAELoader calls it `model_name`,
    # which is also what half the CHECKPOINT loaders call theirs -- so the looser key is trusted
    # only on a node whose class says VAE loader. Both halves of that rule are pinned here.
    wan = {
        '1': {'class_type': 'WanVideoModelLoader', 'inputs': {'model_name': 'wan/wan22_i2v.safetensors'}},
        '2': {'class_type': 'WanVideoVAELoader', 'inputs': {'model_name': 'wan/Wan2_1_VAE_bf16.safetensors'}},
        '3': {'class_type': 'VAEDecode', 'inputs': {'samples': ['4', 0], 'vae': ['2', 0]}},
        '4': _sampler(),
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a river'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('model_name on a VAE loader is the VAE', vae_of(wan), 'Wan2_1_VAE_bf16')

    # The same graph with nothing decoding through it, so the fallback runs: it must still refuse
    # the MODEL loader, which carries an identically-named input.
    wan_nodecode = {k: v for k, v in wan.items() if k != '3'}
    check('but model_name on a model loader is not', vae_of(wan_nodecode), 'Wan2_1_VAE_bf16')

    print()
    print('a song workflow does not report its audio VAE as the picture')
    audio_only = {
        '1': {'class_type': 'LTXVAudioVAELoader', 'inputs': {'ckpt_name': 'audio_vae.safetensors'}},
        '2': {'class_type': 'VAEDecodeAudio', 'inputs': {'samples': ['4', 0], 'vae': ['1', 0]}},
        '4': _sampler(),
        '9': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a song'}},
        '10': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
    }
    check('nothing, rather than the audio VAE', vae_of(audio_only), None)

    print('\nan A1111 block -- the only route for a downloaded JPEG or a video sidecar')
    got = comfy_meta.parse_a1111(
        'a portrait\n'
        'Negative prompt: blurry\n'
        'Steps: 30, Sampler: Euler a, CFG scale: 7, Seed: 42, '
        'Model: realisticVision, VAE: vae-ft-mse-840000.safetensors')
    check('the VAE line is read', got.get('vae_name'), 'vae-ft-mse-840000')
    check('and the model still is', got.get('model_name'), 'realisticVision')

    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
