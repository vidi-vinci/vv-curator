"""test_lora_stack.py -- LoRAs survive whichever node shape applied them.

A LoRA reaches a graph in three different shapes, and knowing only two of them is how an entire
MiniMax video run reported no LoRAs at all (the author, 2026-09-05):

  * ONE PER NODE -- `lora_name` as a plain string. Matched on the class saying "lora" plus that
    key, never on a list of class names: the four that used to be hardcoded are not the four that
    exist, and the saver's copy was still on that list, so it missed loaders the viewer found;
  * rgthree's POWER LORA LOADER -- `lora_1..N`, each a dict;
  * A STACKER -- the whole list encoded as JSON in ONE string input. `LTX_lora_loader` ("LoRA
    Loader Stack (LTX / MiniMax H3 Compatible)") carries no `lora_name` whatsoever, so the first
    test failed and nothing else looked. This is the shape that shipped blind.

WHY BY SHAPE AND NOT BY NAME. The class name and the `stack_data` key are one pack's choices; the
convention -- a JSON list of entries with an `on` flag and a `lora` path -- is the part another
pack copies. Pinning the node name here would pass while the next stacker failed exactly as this
one did.

BOTH COPIES ARE CHECKED. `comfy_vv_saver/vendor/` is meant to be a verbatim copy so the node and
the viewer's Export for Civitai emit the same format, and it had silently drifted. The two agreeing
is the actual guarantee, so it is asserted rather than assumed -- a re-copy that misses one
function is invisible until someone compares output.

Run: python test_lora_stack.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'comfy_vv_saver'))
import comfy_meta
from vendor import comfy_meta as vendored

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label + ('' if ok else '\n         got  %r\n         want %r' % (got, want)))
    if not ok:
        _fails.append(label)


def names(graph, mod=comfy_meta):
    return [(l['name'], l['strength']) for l in mod._extract_loras(graph)]


# The real thing, trimmed to the node that matters: captured verbatim from a MiniMax H3 run whose
# LoRAs the viewer reported as none. Three entries, the middle one switched off.
STACK = json.dumps([
    {"on": True,  "lora": "MMH3\\Utilities\\VBVR_H3_attn_only.safetensors",  "str": 0.6, "v": 1, "a": 1, "t": 1},
    {"on": False, "lora": "MMH3\\Utilities\\H3_Motion_BoosterV2.safetensors", "str": 1,  "v": 1, "a": 1, "t": 1},
    {"on": True,  "lora": "MMH3\\FX\\DC Vast Expanse [Rank32] v1.safetensors", "str": 0.8, "v": 1, "a": 1, "t": 1},
])
STACKER = {'1': {'class_type': 'LTX_lora_loader',
                 'inputs': {'mode': 'minimax', 'stack_data': STACK, 'lora_ui': '',
                            'model': ['133', 0]}}}

print('\nLoRAs survive whichever node applied them\n')

check('a stacker yields its ACTIVE entries, in order',
      names(STACKER),
      [('VBVR_H3_attn_only', 0.6), ('DC Vast Expanse [Rank32] v1', 0.8)])

check('  ...and the switched-off one is not among them',
      [n for n, _ in names(STACKER) if 'Motion_Booster' in n], [])

check('the raw path is kept for Civitai hash resolution',
      [l['raw'] for l in comfy_meta._extract_loras_raw(STACKER)],
      ['MMH3\\Utilities\\VBVR_H3_attn_only.safetensors',
       'MMH3\\FX\\DC Vast Expanse [Rank32] v1.safetensors'])

# `on` absent means on. A stacker that writes only the disabled flag, or none at all, must not have
# its LoRAs silently dropped -- absence of a claim is not a claim of absence.
check('an entry with no `on` key counts as on',
      names({'1': {'class_type': 'LoraStacker',
                   'inputs': {'d': json.dumps([{"lora": "x/y.safetensors", "str": 0.5}])}}}),
      [('y', 0.5)])

# rgthree spells strength `strength`; this pack spells it `str`. Both are read, because the shape
# is what is being matched and neither spelling is more correct than the other.
check('`strength` is read as well as `str`',
      names({'1': {'class_type': 'AnyLoraStack',
                   'inputs': {'d': json.dumps([{"on": True, "lora": "a.safetensors", "strength": 0.25}])}}}),
      [('a', 0.25)])

# The two older shapes must keep working -- the stacker branch is an `elif`, and a regression here
# would mean the fix for one workflow broke every other.
check('a single LoraLoader still works',
      names({'1': {'class_type': 'LoraLoader',
                   'inputs': {'lora_name': 'sub/detail.safetensors', 'strength_model': 0.7}}}),
      [('detail', 0.7)])

check("a loader outside the old hardcoded four still works",
      names({'1': {'class_type': 'LoraLoader|pysssss',
                   'inputs': {'lora_name': 'z.safetensors', 'strength_model': 1}}}),
      [('z', 1)])

check('rgthree Power Lora Loader still works',
      names({'1': {'class_type': 'Power Lora Loader (rgthree)',
                   'inputs': {'lora_1': {'on': True, 'lora': 'p.safetensors', 'strength': 0.9},
                              'lora_2': {'on': False, 'lora': 'q.safetensors', 'strength': 1}}}}),
      [('p', 0.9)])

# A graph with no LoRA node at all must stay empty rather than pick up a stray JSON string: every
# graph carries long text inputs, and the gate is what stops a prompt mentioning lora being parsed.
check('a prompt that merely mentions lora yields nothing',
      names({'1': {'class_type': 'CLIPTextEncode',
                   'inputs': {'text': 'a painting, {"lora": "not a node"}'}}}),
      [])

check('a malformed stack is ignored rather than raising',
      names({'1': {'class_type': 'LoraStacker', 'inputs': {'d': '[{"lora": broken'}}}), [])

# ---- the two copies must agree -------------------------------------------------------------
print('')
for label, graph in [('the stacker', STACKER),
                     ('a single loader', {'1': {'class_type': 'LoraLoader',
                                                'inputs': {'lora_name': 'a.safetensors',
                                                           'strength_model': 1}}}),
                     ('a pysssss loader', {'1': {'class_type': 'LoraLoader|pysssss',
                                                 'inputs': {'lora_name': 'b.safetensors',
                                                            'strength_model': 1}}})]:
    check('the saver agrees with the viewer on %s' % label,
          names(graph, vendored), names(graph, comfy_meta))
    check('  ...including the raw names',
          vendored._extract_loras_raw(graph), comfy_meta._extract_loras_raw(graph))

print('\n%s\n' % ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
sys.exit(1 if _fails else 0)
