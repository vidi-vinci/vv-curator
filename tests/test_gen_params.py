"""Regression test for the GENERATION SETTINGS: seed, steps, CFG, sampler, scheduler.

Run: python tests/test_gen_params.py

The claim under test is not "we can read a KSampler" -- that was always easy. It is **which**
sampler we read, in a graph that has several.

The author's workflows run a base generation and then detailers, refiners and upscalers, and every saved
image carries the WHOLE graph rather than just the stage that made it. So a rule is needed, and the
rule is: the BASE sampler, defined as the one with no other sampler upstream of it.

Two wrong rules this pins against, both of which pass a single-sampler test:

  * "first in the file" -- node order in a saved graph does not have to follow the pipeline, so a
    graph listing its detailer first would report the detailer's settings.
  * "longest positive prompt" -- what the code did before, and still does for the PROMPT. When the
    stages share a prompt (normal) it picks arbitrarily.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comfy_meta

_fails = []


def check(label, ok, got=None):
    print(('OK   ' if ok else 'FAIL ') + label + ('' if ok else '  -> %r' % (got,)))
    if not ok:
        _fails.append(label)


def ksampler(steps, cfg, sampler, sched, seed, latent, positive='6'):
    return {'class_type': 'KSampler',
            'inputs': {'steps': steps, 'cfg': cfg, 'sampler_name': sampler,
                       'scheduler': sched, 'seed': seed,
                       'model': ['1', 0], 'positive': positive, 'negative': '7',
                       'latent_image': latent}}


def base_graph():
    """A three-stage run: base -> detail -> refine, wired through VAE round-trips like a real one.

    The detailer is deliberately listed FIRST and given the LONGEST positive prompt, so a
    first-in-file rule and a longest-prompt rule each pick it and each fail.
    """
    return {
        # id '2' sorts before '10', and appears first in the dict -- the trap for "first".
        '2':  ksampler(40, 3.5, 'dpmpp_3m_sde', 'sgm_uniform', 999, ['20', 0],
                       positive='a much longer prompt that would win a longest-positive contest'),
        '20': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['21', 0]}},
        '21': {'class_type': 'VAEDecode', 'inputs': {'samples': ['10', 0]}},
        # The real base: its latent is an empty one, so nothing upstream is a sampler.
        '10': ksampler(25, 7.5, 'euler', 'karras', 12345, ['11', 0], positive='short'),
        '11': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 1024, 'height': 1024}},
        '1':  {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'sdxl/base.safetensors'}},
    }


def main():
    g = base_graph()

    # --- the rule itself -------------------------------------------------------------------
    base = comfy_meta._pick_base_sampler(g)
    check('the base sampler is the one fed by an empty latent, not the one listed first',
          base == '10', base)
    check('the longest-prompt rule would have picked the detailer (so it is a real trap)',
          comfy_meta._pick_sampler(g) == '2', comfy_meta._pick_sampler(g))

    p = comfy_meta.extract_gen_params(g, base)
    check('steps come from the base', p['steps'] == 25, p['steps'])
    check('cfg comes from the base', p['cfg'] == 7.5, p['cfg'])
    check('sampler comes from the base', p['sampler_name'] == 'euler', p['sampler_name'])
    check('scheduler comes from the base', p['scheduler'] == 'karras', p['scheduler'])
    check('seed comes from the base', p['seed'] == 12345, p['seed'])

    # --- order must not matter -------------------------------------------------------------
    rev = dict(reversed(list(base_graph().items())))
    check('reversing the graph order changes nothing',
          comfy_meta._pick_base_sampler(rev) == '10', comfy_meta._pick_base_sampler(rev))

    # --- img2img: no empty latent anywhere -------------------------------------------------
    g2 = base_graph()
    g2['11'] = {'class_type': 'LoadImage', 'inputs': {'image': 'in.png'}}
    check('an img2img base is still found with no empty latent in the graph',
          comfy_meta._pick_base_sampler(g2) == '10', comfy_meta._pick_base_sampler(g2))

    # --- degenerate shapes must not crash or report nonsense --------------------------------
    check('a single-sampler graph returns that sampler',
          comfy_meta._pick_base_sampler({'5': ksampler(20, 8, 'euler', 'normal', 1, ['6', 0]),
                                         '6': {'class_type': 'EmptyLatentImage', 'inputs': {}}}) == '5')
    check('a graph with no sampler returns None',
          comfy_meta._pick_base_sampler({'1': {'class_type': 'SaveImage', 'inputs': {}}}) is None)
    cyc = {'a': ksampler(20, 8, 'euler', 'normal', 1, ['b', 0]),
           'b': ksampler(30, 9, 'ddim', 'simple', 2, ['a', 0])}
    check('a cycle falls back instead of hanging or returning nothing',
          comfy_meta._pick_base_sampler(cyc) is not None)

    # Two independent generations: no right answer, but it must be STABLE across rescans or the
    # same file would report different settings on different days.
    two = {'3': ksampler(11, 1, 'euler', 'normal', 1, ['30', 0]),
           '30': {'class_type': 'EmptyLatentImage', 'inputs': {}},
           '4': ksampler(22, 2, 'ddim', 'karras', 2, ['40', 0]),
           '40': {'class_type': 'EmptyLatentImage', 'inputs': {}}}
    a = comfy_meta._pick_base_sampler(two)
    b = comfy_meta._pick_base_sampler(dict(reversed(list(two.items()))))
    check('two independent generations pick the same one whichever order they arrive in',
          a == b, (a, b))

    # --- the A1111 route: the ONLY source for a video with a .txt sidecar --------------------
    got = comfy_meta.parse_a1111(
        'a lighthouse in a storm\n'
        'Negative prompt: blurry\n'
        'Steps: 30, Sampler: DPM++ 2M Karras, CFG scale: 4.5, Seed: 42, '
        'Size: 512x512, Model: minimax_h3')
    check('a1111: steps', got.get('steps') == 30, got.get('steps'))
    check('a1111: cfg', got.get('cfg') == 4.5, got.get('cfg'))
    check('a1111: seed', got.get('seed') == 42, got.get('seed'))
    check('a1111: sampler and scheduler are SPLIT, not stored blended',
          (got.get('sampler_name'), got.get('scheduler')) == ('DPM++ 2M', 'karras'),
          (got.get('sampler_name'), got.get('scheduler')))
    plain = comfy_meta.parse_a1111('x\nSteps: 5, Sampler: Euler, Model: m')
    check('a1111: a sampler with no scheduler word keeps its whole name',
          (plain.get('sampler_name'), plain.get('scheduler')) == ('Euler', None),
          (plain.get('sampler_name'), plain.get('scheduler')))

    # --- fills gaps, never overwrites -------------------------------------------------------
    res = {'positive': 'traced', 'model_name': 'traced.safetensors', 'steps': 25,
           'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None, 'loras': []}
    comfy_meta._fill_from_a1111(res, 'p\nSteps: 99, Sampler: Euler, CFG scale: 1, Seed: 7, Model: m')
    check('a traced setting is NOT overwritten by the a1111 block', res['steps'] == 25, res['steps'])
    check('a setting the graph left empty IS filled from the a1111 block',
          (res['cfg'], res['seed']) == (1.0, 7), (res['cfg'], res['seed']))

    # --- a custom sampler class must not be invisible -----------------------------------------
    # Reported as "some Krea files work, some don't". Both failed for one reason: RES4LYF's
    # ClownsharKSampler_Beta was not in SAMPLER_CLASSES, so the walker found NO sampler at all --
    # the settings came back empty and the prompt quietly dropped to the longest-CLIP fallback.
    # The node itself is entirely ordinary: sampler_name, scheduler, steps, cfg and seed all sit on
    # it inline. It was ignored purely because of its name.
    #
    # A name list cannot be the whole answer, so shape is the fallback: calls itself a sampler,
    # consumes a latent, carries a knob.
    clown = {'9': {'class_type': 'ClownsharKSampler_Beta', 'inputs': {
                 'sampler_name': 'linear/euler', 'scheduler': 'simple', 'steps': 8, 'cfg': 1.0,
                 'seed': 159426847837371, 'eta': 0.5, 'denoise': 0.9,
                 'model': ['1', 0], 'positive': ['6', 0], 'negative': ['7', 0],
                 'latent_image': ['11', 0]}},
             '11': {'class_type': 'EmptyLatentImage', 'inputs': {}}}
    check('a custom sampler class is recognised', comfy_meta._pick_base_sampler(clown) == '9')
    cp = comfy_meta.extract_gen_params(clown, '9')
    check('its inline settings are read',
          (cp['sampler_name'], cp['scheduler'], cp['steps'], cp['cfg'], cp['seed'])
          == ('linear/euler', 'simple', 8, 1.0, 159426847837371), cp)

    # An unknown class judged on shape alone -- the general case the name list cannot cover.
    unknown = {'9': dict(clown['9'], class_type='SomeFutureSamplerNode'), '11': clown['11']}
    check('an UNKNOWN sampler-shaped class is recognised too',
          comfy_meta._pick_base_sampler(unknown) == '9')

    # ...and the helpers that merely FEED a sampler must not be mistaken for one. KSamplerSelect
    # names a sampler and takes no latent; counting it would put a node that never ran anything
    # into the running.
    helpers = {
        'a': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': 'euler'}},
        'b': {'class_type': 'SamplerEulerAncestral', 'inputs': {'eta': 1.0, 's_noise': 1.0}},
        'c': {'class_type': 'BasicScheduler', 'inputs': {'steps': 20, 'scheduler': 'simple'}},
    }
    for nid, node in helpers.items():
        check('%s is NOT counted as a sampler' % node['class_type'],
              not comfy_meta._is_sampler_node(node))

    # --- the seed must survive the trip to the browser ---------------------------------------
    # A ComfyUI seed is a 64-bit integer. JSON numbers land in JavaScript as doubles, exact only to
    # 2^53, so a real seed like 1234567890123456789 arrives as ...456800 -- silently, and looking
    # entirely plausible. A seed exists to REPRODUCE a generation, so a nearly-right one is worse
    # than none: it cannot work and gives no sign of why. api_image therefore sends it as a STRING.
    # Caught in the browser during verification, not by this file, which is why it is pinned here.
    big = 1234567890123456789
    check('a 64-bit seed is not exactly representable as a double (so the hazard is real)',
          int(float(big)) != big, int(float(big)))
    import json as _json
    import server
    payload = _json.loads(_json.dumps({'gp_seed': str(big)}))
    check('sent as a string, the seed round-trips through JSON exactly',
          payload['gp_seed'] == str(big), payload['gp_seed'])
    src = open(server.__file__, encoding='utf-8').read()
    check('api_image stringifies the seed (guards the fix against a tidy-up)',
          "seed = str(d['gp_seed'])" in src)
    # The other half of the same rule, added 2026-08-29: a seed too large for SQLite's INTEGER is
    # stored in gp_seed_s instead, so api_image has to collapse the two columns back into the one
    # field the client knows. Dropping this line would send the client gp_seed = null for exactly
    # the seeds that were hardest to keep. See tests/test_big_seed.py for the storage half.
    check('api_image prefers the oversized-seed column when it is set',
          "seed = d.pop('gp_seed_s', None)" in src)

    # ---- A settings node that hands out SEVERAL values at once -------------------------------
    # rgthree's `KSampler Config` emits steps_total, refiner_step, cfg, sampler_name and scheduler
    # from one node, so every link into it is ['260', N] and the SLOT is the only thing telling
    # them apart. The resolvers used to discard the slot and scan the node's inputs for a name off
    # a list; none of these names are on either list, so the author's Krea workflow exported with
    # no Steps and no CFG scale at all -- and the scheduler came back as the SAMPLER's name,
    # because `sampler_name` sits first on the combo list. That is samples/civit cant read.png.
    cfg_node = {'class_type': 'KSampler Config (rgthree)',
                'inputs': {'steps_total': 10, 'refiner_step': 8, 'cfg': 1.0,
                           'sampler_name': 'euler_ancestral', 'scheduler': 'beta'}}
    slotted = {
        '260': cfg_node,
        '262': {'class_type': 'BasicScheduler',
                'inputs': {'steps': ['260', 0], 'scheduler': ['260', 4], 'model': ['1', 0]}},
        '261': {'class_type': 'CFGGuider',
                'inputs': {'cfg': ['260', 2], 'model': ['1', 0], 'positive': '6', 'negative': '7'}},
        '263': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': 5270441131205}},
        '266': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': ['260', 3]}},
        '228': {'class_type': 'SamplerCustomAdvanced',
                'inputs': {'noise': ['263', 0], 'guider': ['261', 0], 'sampler': ['266', 0],
                           'sigmas': ['262', 0], 'latent_image': ['11', 0]}},
        '11': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 936, 'height': 1672}},
        '1': {'class_type': 'CheckpointLoaderSimple',
              'inputs': {'ckpt_name': 'Krea/cielbleuKrea2_v1.safetensors'}},
    }
    sp = comfy_meta.extract_gen_params(slotted, '228')
    check('steps come off output slot 0, not a guessed input name', sp['steps'] == 10, sp['steps'])
    check('CFG comes off output slot 2', sp['cfg'] == 1.0, sp['cfg'])
    check('the scheduler is slot 4, NOT the sampler name sitting earlier in the node',
          sp['scheduler'] == 'beta', sp['scheduler'])
    check('the sampler is still read correctly', sp['sampler_name'] == 'euler_ancestral',
          sp['sampler_name'])
    # The point of all of it: A1111 always writes Steps and CFG scale, and a parser modelled on
    # A1111's output can reject a block that is missing them.
    text = comfy_meta._format_parameters({'positive': 'a castle', 'negative': '',
                                          'model_name': 'cielbleuKrea2_v1', 'loras': []},
                                         sp, 936, 1672)
    check('the exported block carries Steps', 'Steps: 10' in text, text)
    check('the exported block carries CFG scale', 'CFG scale: 1' in text, text)

    # The slot is trusted only when it holds a literal of the right type, which is what keeps a
    # node whose outputs do NOT mirror its inputs falling through to the old behaviour.
    passthru = {'9': {'class_type': 'Reroute', 'inputs': {'value': ['8', 0]}},
                '8': {'class_type': 'Seed (rgthree)', 'inputs': {'seed': 4242}}}
    check('a plain one-output relay still resolves through the link',
          comfy_meta._resolve_number(passthru, ['9', 0]) == 4242,
          comfy_meta._resolve_number(passthru, ['9', 0]))

    print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
    return 1 if _fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
