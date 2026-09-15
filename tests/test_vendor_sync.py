"""test_vendor_sync.py — the ComfyUI node's copy of the metadata reader must match the app's.

Run: python test_vendor_sync.py

WHY THIS EXISTS. `comfy_vv_saver/vendor/` holds standalone copies of `comfy_meta.py` and
`model_hash.py`, because ComfyUI loads custom nodes from its own folder and cannot import the app.
The node's README calls them a verbatim copy. On 2026-09-13 the reader was 1053 lines against the
app's 2168, and the drift was not the harmless kind: the app had learned to recognise samplers and
loaders by SHAPE (see test_loader_shape.py) and the node's copy had not. So a workflow using a
custom sampler or a custom checkpoint loader — NF4, GGUF, Nunchaku, the efficiency packs — saved a
PNG with no model and no settings, while the same image read correctly once the app indexed it. The
node and Export for Civitai are supposed to emit the SAME format, and quietly did not.

A byte compare, deliberately. Anything cleverer (compare only the functions the node calls, allow a
subset) is what let this through: the drift was inside shared functions, and every one of the
missing helpers looked unused until you followed the call.

If this fails, re-copy — do not edit the vendored file:
    cp comfy_meta.py model_hash.py comfy_vv_saver/vendor/
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


for fname in ('comfy_meta.py', 'model_hash.py'):
    src = os.path.join(ROOT, fname)
    vendored = os.path.join(ROOT, 'comfy_vv_saver', 'vendor', fname)
    check('%s is vendored at all' % fname, os.path.exists(vendored), vendored)
    if not os.path.exists(vendored):
        continue
    a = open(src, 'rb').read()
    b = open(vendored, 'rb').read()
    detail = 'app %d bytes, vendored %d bytes — re-copy it' % (len(a), len(b))
    check('%s matches the app copy byte for byte' % fname, a == b, detail)

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
