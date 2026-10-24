"""test_text_allowlist.py -- only a text ENCODER can supply a prompt; a LoRA node's text never can.

Run: python tests/test_text_allowlist.py

Reported 2026-10-10 on a Krea run: the negative read "penis krea <lora:...> <lora:...>", the LoRA
Manager node's own `text` widget. test_negative_fallthrough.py closed the `clip` route into it; this
closes the class. A node was a prompt if it merely HAD a `text` input, so any route into any such
node (here a relay key nobody denied) published its widget.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


LORA_TEXT = 'penis krea <lora:Krea-CinemaGrade-7thEpoch:0.75> <lora:Purple_Grainy_KR2_V2_D:0.65>'


def graph(neg_inputs, extra=None):
    g = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'k.safetensors'}},
        '2': {'class_type': 'Lora Loader (LoraManager)',
              'inputs': {'text': LORA_TEXT, 'model': ['1', 0], 'clip': ['1', 0]}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'a castle', 'clip': ['2', 1]}},
        '4': neg_inputs,
        '5': {'class_type': 'KSampler',
              'inputs': {'positive': ['3', 0], 'negative': ['4', 0], 'model': ['2', 0],
                         'steps': 8, 'cfg': 1.0, 'sampler_name': 'euler', 'seed': 1}},
    }
    g.update(extra or {})
    return g


print('\nOnly an encoder is a prompt\n')

# the shape of the reported file: empty string node feeding the encode
g = graph({'class_type': 'CLIPTextEncode', 'inputs': {'text': ['9', 0], 'clip': ['2', 1]}},
          {'9': {'class_type': 'JWStringMultiline', 'inputs': {'text': ''}}})
check('empty negative from a string node reads empty',
      comfy_meta._follow_to_text(g, ['4', 0]) == '')

# a relay that reaches the LoRA node through a key no denylist names
g = graph({'class_type': 'SomeRelay', 'inputs': {'pipe': ['2', 0]}})
check('a route into the LoRA node through an unnamed key reads empty',
      comfy_meta._follow_to_text(g, ['4', 0]) == '')

# the real negative still wins
g = graph({'class_type': 'CLIPTextEncode', 'inputs': {'text': 'blurry', 'clip': ['2', 1]}})
check('a real negative still reads', comfy_meta._follow_to_text(g, ['4', 0]) == 'blurry')

# encoders from other packs
for cls in ('CLIPTextEncodeSDXL', 'BNK_CLIPTextEncodeAdvanced', 'smZ CLIPTextEncode',
            'WanVideoTextEncode'):
    g = graph({'class_type': cls, 'inputs': {'text': 'ugly', 'clip': ['2', 1]}})
    check(cls + ' still reads', comfy_meta._follow_to_text(g, ['4', 0]) == 'ugly')

print('\nThe backstop\n')
g = graph({'class_type': 'CLIPTextEncode', 'inputs': {'text': 'x'}})
check('the LoRA widget text is dropped', comfy_meta.drop_lora_text(g, LORA_TEXT) == '')
check('a placeholder is dropped',
      comfy_meta.drop_lora_text(g, 'Go to http://127.0.0.1:8188/loras to apply LoRAs') == '')
check('an ordinary negative survives', comfy_meta.drop_lora_text(g, 'blurry') == 'blurry')
check('a shorter negative sharing words survives', comfy_meta.drop_lora_text(g, 'penis krea') == 'penis krea')

print('\nThe real file\n')
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'samples',
                      'Krea_Test2_16-55-17_vv3jb3g5_Raw_00001_.png')
if os.path.exists(SAMPLE):
    saved = comfy_meta.read_png_text_chunks(SAMPLE).get('parameters', '')
    check('the fixed saver wrote no negative line', 'Negative prompt' not in saved, saved[-300:])
    check('the reader finds no negative', not comfy_meta.extract(SAMPLE).get('negative'))
    check('both LoRAs are still read', len(comfy_meta.extract(SAMPLE).get('loras') or []) == 2)

print('\n' + ('all passed' if not _fails else 'FAILED: %d' % len(_fails)))
sys.exit(1 if _fails else 0)
