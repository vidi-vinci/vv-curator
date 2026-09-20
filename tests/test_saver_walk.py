"""test_saver_walk.py -- the reader walks BACK from the node that saved the file.

WHAT THIS REPLACED. Until 2026-09-17 the question "which sampler made this picture?" was answered
by scanning the whole workflow and taking the sampler with the LONGEST POSITIVE PROMPT. That is not
a fact about workflows; it is a popularity contest on character count. It picked the cover art's
sampler for a song, and an upscale pass's for a picture whose prompt had merely gained a few
quality tags. The file carries the node that saved it, so the reader can walk backwards instead --
saver, decoder, sampler, model -- which REMOVES a guess rather than adding a rule.

WHY THE WALK IS RANKED AND NOT PLAIN. rgthree's Context nodes hand on the model AND the picture
together, so a depth-first walk can arrive at a different stage's sampler and be entirely confident
about it. Inputs are therefore followed picture-first: `images`/`samples`/`audio` before switches
and reroutes, and the model side last. `the walk prefers the picture over the model branch` below
is the check that pins it -- a plain DFS or BFS fails that one and passes everything else here.

NOT COVERED, DELIBERATELY: generation settings. Steps, CFG, sampler and seed still come from
`_pick_base_sampler`, because moving them changes numbers on files the author has already looked at
and that is his call to make on its own. `test_gen_params.py` still owns that behaviour and must
stay green.

Run: python test_saver_walk.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import comfy_meta as cm

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label +
          ('' if ok else '\n         got  %r\n         want %r' % (got, want)))
    if not ok:
        _fails.append(label)


def sampler(nid, positive, model_id='M'):
    return {nid: {'class_type': 'KSampler',
                  'inputs': {'latent_image': ['L', 0], 'steps': 8, 'seed': 1,
                             'model': [model_id, 0], 'positive': [positive, 0]}}}


def text(nid, s):
    return {nid: {'class_type': 'CLIPTextEncode', 'inputs': {'text': s}}}


def chain_of(g, hints=None):
    return cm.resolve_output_chain(g, hints)


print('\nThe reader walks back from the node that saved the file\n')

# ---- 1. the fan-out trap: a Context node carrying BOTH a picture and a model branch ----------
# This is the author's real KREA shape, reduced. The saver's picture comes down `images`; the same
# node also hands on `base_ctx`, and two hops down THAT sits a different sampler. Following inputs
# in dict order reaches the wrong one first, which is why the ranking exists.
FANOUT = {}
FANOUT.update(sampler('right', 'p_right'))
FANOUT.update(sampler('wrong', 'p_wrong'))
FANOUT.update(text('p_right', 'the picture that was saved'))
FANOUT.update(text('p_wrong', 'a much much much longer prompt on the other branch entirely'))
FANOUT.update({
    'ctx':   {'class_type': 'Context (rgthree)',
              'inputs': {'base_ctx': ['relay', 0], 'images': ['decode', 0]}},
    'relay': {'class_type': 'Context Big (rgthree)', 'inputs': {'model': ['wrong', 0]}},
    'decode': {'class_type': 'VAEDecode', 'inputs': {'samples': ['right', 0], 'vae': ['V', 0]}},
    'V':     {'class_type': 'VAELoader', 'inputs': {'vae_name': 'ae.safetensors'}},
    'M':     {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L':     {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'save':  {'class_type': 'SaveImage',
              'inputs': {'images': ['ctx', 0], 'filename_prefix': 'run'}},
})

check('the walk prefers the picture over the model branch',
      chain_of(FANOUT)['sampler'], 'right')

check('  ...even though the other prompt is far longer',
      cm.extract_gen_params and cm._pick_sampler(FANOUT, {'stem': 'run_00001_'}), 'right')

check('the decoder that made THIS picture comes off the walk',
      chain_of(FANOUT)['vae_decode'], 'decode')

# ---- 2. a song and its cover art: two savers, two branches ----------------------------------
# The shape behind the reported fault. The image sampler is given the LONGER prompt on purpose, so
# the old rule picks it for the song too.
SONG = {}
SONG.update(sampler('audio_s', 'p_audio'))
SONG.update(sampler('image_s', 'p_image'))
SONG.update(text('p_audio', 'a short style line'))
SONG.update(text('p_image', 'an extremely detailed description of the cover artwork, at length'))
SONG.update({
    'adec': {'class_type': 'VAEDecodeAudio', 'inputs': {'samples': ['audio_s', 0]}},
    'idec': {'class_type': 'VAEDecode', 'inputs': {'samples': ['image_s', 0]}},
    'M':    {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L':    {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'sa':   {'class_type': 'SaveAudio', 'inputs': {'audio': ['adec', 0], 'filename_prefix': 'trk'}},
    'si':   {'class_type': 'SaveImage', 'inputs': {'images': ['idec', 0], 'filename_prefix': 'trk'}},
})

check('an audio file resolves to the AUDIO sampler',
      chain_of(SONG, {'kind': 'audio'})['sampler'], 'audio_s')

check('  ...and an image file to the image one',
      chain_of(SONG, {'kind': 'image'})['sampler'], 'image_s')

# ---- 3. three branches told apart by the filename --------------------------------------------
# The author's LTX shape: three video savers, two sharing a prefix. Only the filename says which
# clip this file is.
THREE = {}
for i, name in enumerate(('a', 'b', 'c')):
    THREE.update(sampler('s_' + name, 'p_' + name))
    THREE.update(text('p_' + name, 'prompt ' + name))
    THREE['d_' + name] = {'class_type': 'VAEDecode', 'inputs': {'samples': ['s_' + name, 0]}}
THREE.update({
    'M': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'v_a': {'class_type': 'VHS_VideoCombine',
            'inputs': {'images': ['d_a', 0], 'filename_prefix': 'MrXin'}},
    'v_b': {'class_type': 'VHS_VideoCombine',
            'inputs': {'images': ['d_b', 0], 'filename_prefix': 'MrXin'}},
    'v_c': {'class_type': 'VHS_VideoCombine',
            'inputs': {'images': ['d_c', 0], 'filename_prefix': 'LTXJul28_Face/Face_'}},
})

check('the filename picks the right one of three savers',
      chain_of(THREE, {'stem': 'Face__00001', 'kind': 'video'})['sampler'], 's_c')

check('  ...and the reason is recorded as the prefix',
      chain_of(THREE, {'stem': 'Face__00001', 'kind': 'video'})['why'], 'prefix')

# ---- 4. the stage, matched against ANY string the node carries -------------------------------
# The author's own saver holds the literal "Custom..." under `stage` and the real answer under
# `stage_custom`, so asking one field BY NAME reads the wrong one.
STAGE = {}
STAGE.update(sampler('s_raw', 'p_raw'))
STAGE.update(sampler('s_fin', 'p_fin'))
STAGE.update(text('p_raw', 'raw'))
STAGE.update(text('p_fin', 'final'))
STAGE.update({
    'M': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'k_raw': {'class_type': 'VVSaveImageCivitai',
              'inputs': {'images': ['s_raw', 0], 'stage': 'Custom...', 'stage_custom': 'First'}},
    'k_fin': {'class_type': 'VVSaveImageCivitai',
              'inputs': {'images': ['s_fin', 0], 'stage': 'Final'}},
})

check('the stage matches through a custom field, not a named one',
      chain_of(STAGE, {'stage': 'First', 'kind': 'image'})['sampler'], 's_raw')

check('  ...and the plainly-named stage still works',
      chain_of(STAGE, {'stage': 'Final', 'kind': 'image'})['sampler'], 's_fin')

# ---- 5. a matched saver that walks to nothing must NOT veto ----------------------------------
# Real shape: a VV saver writing the first FRAME, whose picture traces to a LoadImage and no
# sampler at all. That is correct, and it must fall through to the other saver rather than report
# nothing.
DEADEND = {}
DEADEND.update(sampler('s', 'p'))
DEADEND.update(text('p', 'the video prompt'))
DEADEND.update({
    'M': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'dec': {'class_type': 'VAEDecode', 'inputs': {'samples': ['s', 0]}},
    'load': {'class_type': 'LoadImage', 'inputs': {'image': 'start.png'}},
    'vid': {'class_type': 'SaveVideo', 'inputs': {'video': ['dec', 0], 'filename_prefix': 'clip'}},
    'first': {'class_type': 'VVSaveImageCivitai',
              'inputs': {'images': ['load', 0], 'stage_custom': 'First'}},
})

check('a saver that traces to a loaded image does not veto the answer',
      chain_of(DEADEND, {'stage': 'First', 'kind': 'image'})['sampler'], 's')

check('  ...and says so: the answer came from every candidate agreeing',
      chain_of(DEADEND, {'stage': 'First', 'kind': 'image'})['method'], 'saver-walk-agreed')

# ---- 6. genuinely ambiguous: two savers, two samplers, nothing to tell them apart -------------
# A confident wrong answer is worse than the old stable one, so this must decline.
AMBIG = {}
AMBIG.update(sampler('s1', 'p1'))
AMBIG.update(sampler('s2', 'p2'))
AMBIG.update(text('p1', 'one'))
AMBIG.update(text('p2', 'two'))
AMBIG.update({
    'M': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'base.safetensors'}},
    'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512}},
    'a': {'class_type': 'SaveImage', 'inputs': {'images': ['s1', 0], 'filename_prefix': 'zzz'}},
    'b': {'class_type': 'SaveImage', 'inputs': {'images': ['s2', 0], 'filename_prefix': 'yyy'}},
})

check('two savers that disagree and nothing to choose between them: no answer',
      chain_of(AMBIG, {'stem': 'unrelated_00001', 'kind': 'image'})['sampler'], None)

check('  ...and no saver-walk method is claimed for it',
      chain_of(AMBIG, {'stem': 'unrelated_00001', 'kind': 'image'})['method'], None)

# ---- 7. the shapes that must not break it -----------------------------------------------------
check('a graph with no saver at all yields nothing, quietly',
      chain_of({'1': {'class_type': 'KSampler',
                      'inputs': {'latent_image': ['L', 0], 'steps': 4}}})['sampler'], None)

check('a dangling sampler is not mistaken for an output node',
      [nid for nid, _ in cm._saver_nodes(
          {'1': {'class_type': 'KSampler', 'inputs': {'latent_image': ['L', 0], 'steps': 4}}})],
      [])

check('a preview loses to the node that was told a filename',
      chain_of({**sampler('s', 'p'), **text('p', 'x'),
                'M': {'class_type': 'CheckpointLoaderSimple',
                      'inputs': {'ckpt_name': 'b.safetensors'}},
                'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 1}},
                'prev': {'class_type': 'PreviewImage', 'inputs': {'images': ['s', 0]}},
                'save': {'class_type': 'SaveImage',
                         'inputs': {'images': ['s', 0], 'filename_prefix': 'run'}}},
               {'kind': 'image'})['saver'], 'save')

# Found by shape, never by name: no class list is consulted anywhere in the walk.
check('a saver whose class nobody has ever heard of is still found',
      chain_of({**sampler('s', 'p'), **text('p', 'x'),
                'M': {'class_type': 'CheckpointLoaderSimple',
                      'inputs': {'ckpt_name': 'b.safetensors'}},
                'L': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 1}},
                'w': {'class_type': 'MyCustomWriterXYZ',
                      'inputs': {'images': ['s', 0], 'path': 'out/thing'}}},
               {'kind': 'image'})['sampler'], 's')

check('a cycle terminates rather than hanging',
      cm._walk_back_to_sampler({'x': {'class_type': 'A', 'inputs': {'images': ['y', 0]}},
                                'y': {'class_type': 'B', 'inputs': {'images': ['x', 0]}}}, 'x'),
      (None, []))

# Subgraph ids look like '1829:1773' and must survive the walk as the strings they are.
check('subgraph-prefixed node ids come back unchanged',
      cm._walk_back_to_sampler(
          {'7:1': {'class_type': 'SaveImage', 'inputs': {'images': ['7:2', 0]}},
           '7:2': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7:3', 0]}},
           '7:3': {'class_type': 'KSampler',
                   'inputs': {'latent_image': ['L', 0], 'steps': 4, 'positive': ['p', 0]}}},
          '7:1')[0],
      '7:3')

LONG = {'s': {'class_type': 'SaveImage', 'inputs': {'images': ['n0', 0]}}}
for i in range(600):
    LONG['n%d' % i] = {'class_type': 'Reroute', 'inputs': {'images': ['n%d' % (i + 1), 0]}}
LONG['n600'] = {'class_type': 'KSampler', 'inputs': {'latent_image': ['L', 0], 'steps': 4}}
check('a 600-hop chain gives up on its budget instead of grinding',
      cm._walk_back_to_sampler(LONG, 's')[0], None)

# ---- 8. the real files, which is the only evidence that counts --------------------------------
print('')
REAL = [('KREA_Tests_18-50-25~vv3i4ng1_Raw_00001_.png', 'saver-walk', 'fasciumKREA2_exp01092026'),
        ('Face__00001.png', 'saver-walk', 'ltx2310eros_v14'),
        ('MM_MartianBones_07-46-50~vv3hhkq2_First_00001_.png', 'saver-walk-agreed',
         'minimax_h3_fl2va_pruned_int8_convrot')]
for name, want_method, want_model in REAL:
    p = os.path.join(ROOT, 'samples', name)
    if not os.path.isfile(p):
        print('  skip  %s is not in samples/' % name)
        continue
    r = cm.extract(p)
    check('%s answers from the walk' % name[:34], r['method'], want_method)
    check('  ...naming the right model', r['model_name'], want_model)

# The whole point of routing the export through the same answer: one picture, one description.
p = os.path.join(ROOT, 'samples', 'KREA_Tests_18-50-25~vv3i4ng1_Raw_00001_.png')
if os.path.isfile(p):
    params = cm.build_civitai_parameters(p, 1369, 1760) or ''
    model = cm.extract(p)['model_name']
    check('the export names the same model the library indexed',
          ('Model: ' + model) in params, True)

print('\n%s\n' % ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
sys.exit(1 if _fails else 0)
