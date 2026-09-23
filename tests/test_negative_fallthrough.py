"""test_negative_fallthrough.py -- an EMPTY negative prompt must stay empty.

Run: python tests/test_negative_fallthrough.py

Reported 2026-09-21: a file's negative prompt read "Go to http://127.0.0.1:8188/loras to apply
LoRAs" five times over, followed by a <lora:...> tag. It showed in the Details pane and in the
Civitai export both, so a localhost address could end up under a published image.

The author found the rule that makes it reproducible without his file: WITH a real negative prompt
everything is fine, and only an EMPTY one goes wrong.

That is the whole mechanism. _follow_to_text treats a node as a text node if it merely HAS a `text`
input, which `Lora Loader (LoraManager)` does -- its widget holds that placeholder plus the run's
LoRA tags. With a real negative the encode returns its own text and the walk stops. With an empty
one the encode returns nothing, the walk carries on through the encode's OTHER links, and the only
one left is `clip` -- which runs straight back up the CLIP chain into the LoRA loaders.

So the fix is not about that node: a conditioning walk must never descend a `clip`, `model` or
`vae` link, because those are not conditioning. A denylist rather than an allowlist, because relay
nodes carry conditioning under names nobody can enumerate.

An empty negative is ORDINARY -- Flux and Krea runs at CFG 1 have one -- so this was never a rare
shape.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


# The placeholder is verbatim from the node, as captured in test_lora_stack.py.
PLACEHOLDER = ('Go to http://127.0.0.1:8188/loras to apply LoRAs '
               '<lora:size_diff_krea2_loraholic:8.00>')


def graph(neg_text, lora_class='Lora Loader (LoraManager)'):
    """A run whose CLIP chain passes through a LoRA node carrying a `text` widget."""
    return {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': 'krea.safetensors'}},
        '2': {'class_type': lora_class,
              'inputs': {'text': PLACEHOLDER, 'clip': ['1', 1], 'model': ['1', 0],
                         'loras': {'__value__': [{'name': 'size_diff_krea2_loraholic',
                                                  'strength': 8.0, 'active': True}]}}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a castle', 'clip': ['2', 1]}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': neg_text, 'clip': ['2', 1]}},
        '5': {'class_type': 'KSampler',
              'inputs': {'positive': ['3', 0], 'negative': ['4', 0], 'model': ['2', 0],
                         'steps': 10, 'cfg': 1.0, 'sampler_name': 'er_sde', 'seed': 1}},
    }


print('\nAn empty negative prompt stays empty\n')

# --- the reported case ----------------------------------------------------------------------------
g = graph('')
neg = comfy_meta._follow_to_text(g, ['4', 0])
check('an EMPTY negative reads as empty, not as the LoRA widget', neg == '', neg[:90])
check('the placeholder is nowhere in it', '127.0.0.1' not in neg, neg[:90])

# --- and the case that always worked, which must keep working ---------------------------------------
check('a real negative still reads',
      comfy_meta._follow_to_text(graph('blurry, watermark'), ['4', 0]) == 'blurry, watermark')
check('the positive is unaffected either way',
      comfy_meta._follow_to_text(g, ['3', 0]) == 'a castle',
      comfy_meta._follow_to_text(g, ['3', 0]))

# --- the LoRA itself must still be found ------------------------------------------------------------
# The node is not being ignored -- only its `text` widget is off the conditioning path. Its LoRAs
# are read from the `loras` list exactly as before (see test_lora_stack.py).
check('the LoRA on that node is still read',
      [l['raw'] for l in comfy_meta._extract_loras_raw(g)] == ['size_diff_krea2_loraholic'],
      comfy_meta._extract_loras_raw(g))

# --- through the WHOLE reader, which is the value that reaches the pane and the export -------------
# _follow_to_text is internal. This is the path a published post's metadata actually takes, so it is
# the assertion that says the localhost address cannot be published.
res = {}
comfy_meta._from_graph(json.dumps(g), res)
check('the assembled negative is empty, not the placeholder',
      (res.get('negative') or '') == '', (res.get('negative') or '')[:90])
check('and no part of the reader reintroduces the address',
      '127.0.0.1' not in json.dumps(res), 'found in %r' % sorted(res))
check('while the positive survives the same pass', res.get('positive') == 'a castle',
      res.get('positive'))

# --- not specific to one vendor's node ----------------------------------------------------------------
# The rule is about the LINK, not the class name, so any loader with a text widget behaves the same.
check('any other loader with a text widget behaves the same',
      comfy_meta._follow_to_text(graph('', 'LoraLoader|pysssss'), ['4', 0]) == '')

# --- a passthrough on the CONDITIONING side is still followed -------------------------------------------
# The denylist must not have made the walk timid: a real relay carrying conditioning still resolves.
relay = graph('')
relay['6'] = {'class_type': 'FluxGuidance', 'inputs': {'conditioning': ['3', 0], 'guidance': 3.5}}
check('a conditioning passthrough still resolves to its text',
      comfy_meta._follow_to_text(relay, ['6', 0]) == 'a castle',
      comfy_meta._follow_to_text(relay, ['6', 0]))

print()
sys.exit(1 if _fails else 0)
