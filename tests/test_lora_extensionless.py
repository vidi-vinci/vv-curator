"""test_lora_extensionless.py -- a LoRA stored WITHOUT its extension still finds its file.

ComfyUI Lora Manager's loader records 'MyLora', not 'MyLora.safetensors'. Every other node stores
the category-relative filename, extension and all, so both lookups in `resolve_file` -- the exact
join and the recursive basename walk -- compared against a name that can never match, and the hash
came back None.

WHY THAT MATTERS AND WHY IT IS EASY TO MISS: the LoRA still SHOWS. It reads, it appears in the
detail view, it goes into the Civitai block as `<lora:MyLora:1>`. What is missing is the `Lora
hashes` line, which is the difference between a resource Civitai NAMES and one it LINKS. Nothing
looks broken at either end.

BOTH WAYS. A fallback that matches too eagerly is worse than none, because a wrong hash links the
upload to somebody else's model -- so it is asserted that a stored name carrying its own extension
is NOT stem-matched onto a different file, and that a miss is still a miss.

Run: python test_lora_extensionless.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'comfy_vv_saver'))
import model_hash
from vendor import model_hash as vendored

_fails = []


def check(label, got, want):
    ok = got == want
    print(('  ok   ' if ok else '  FAIL ') + label +
          ('' if ok else '\n         got  %r\n         want %r' % (got, want)))
    if not ok:
        _fails.append(label)


def rel(path, root):
    """A found path as a stable, comparable string -- or None."""
    return None if not path else os.path.relpath(path, root).replace('\\', '/')


root = tempfile.mkdtemp(prefix='vv_models_')
try:
    loras = os.path.join(root, 'loras')
    os.makedirs(os.path.join(loras, 'Krea'))
    for p in ('Krea/CRAIG_Mullins_krea2_3264412_epoch_20.safetensors',
              'Krea/detail_slider.ckpt',
              'plain.safetensors',
              'notes.txt'):
        with open(os.path.join(loras, *p.split('/')), 'w') as f:
            f.write('x')

    print('\nA LoRA stored without its extension still finds its file\n')

    # The real case: Lora Manager's stored name, the file two folders down with its extension on.
    check('an extension-less name finds the .safetensors',
          rel(model_hash.resolve_file(root, 'loras', 'CRAIG_Mullins_krea2_3264412_epoch_20'), loras),
          'Krea/CRAIG_Mullins_krea2_3264412_epoch_20.safetensors')

    check('...and it works for other model extensions too',
          rel(model_hash.resolve_file(root, 'loras', 'detail_slider'), loras),
          'Krea/detail_slider.ckpt')

    check('an extension-less name at the top level is found',
          rel(model_hash.resolve_file(root, 'loras', 'plain'), loras),
          'plain.safetensors')

    # The guard, tested the way that can fail: a name that DOES carry an extension must be matched
    # on that exact filename. Stem-matching it would let 'plain.ckpt' resolve to 'plain.safetensors'
    # -- a different file, and a wrong Civitai link rather than a missing one.
    check('a name carrying a WRONG extension is not stem-matched onto another file',
          model_hash.resolve_file(root, 'loras', 'plain.ckpt'), None)

    check('a name carrying the right extension still resolves exactly',
          rel(model_hash.resolve_file(root, 'loras', 'plain.safetensors'), loras),
          'plain.safetensors')

    # A stored path keeps working, extension-less or not: the walk is a fallback, never a bypass.
    check('a subfolder path with its extension still resolves',
          rel(model_hash.resolve_file(root, 'loras',
                                      'Krea\\CRAIG_Mullins_krea2_3264412_epoch_20.safetensors'), loras),
          'Krea/CRAIG_Mullins_krea2_3264412_epoch_20.safetensors')

    check('a subfolder path WITHOUT its extension resolves as well',
          rel(model_hash.resolve_file(root, 'loras',
                                      'Krea\\CRAIG_Mullins_krea2_3264412_epoch_20'), loras),
          'Krea/CRAIG_Mullins_krea2_3264412_epoch_20.safetensors')

    # A miss is still a miss. The point of returning None is that an unresolvable resource appears
    # as text and links to nothing -- it must never fall through to "closest thing on disk".
    check('a name matching nothing still yields None',
          model_hash.resolve_file(root, 'loras', 'never_installed'), None)

    # Not a model file. 'notes' would stem-match notes.txt if the extension set were not checked.
    check('a non-model file is never returned',
          model_hash.resolve_file(root, 'loras', 'notes'), None)

    # The node ships its own copy, and Export for Civitai in the app must agree with the hashes the
    # saver wrote at generation time -- a drifted copy means the same LoRA links from one and not
    # the other.
    print('')
    for name in ('CRAIG_Mullins_krea2_3264412_epoch_20', 'plain', 'plain.ckpt', 'notes'):
        check('the saver agrees with the viewer on %r' % name,
              rel(vendored.resolve_file(root, 'loras', name), loras),
              rel(model_hash.resolve_file(root, 'loras', name), loras))
finally:
    shutil.rmtree(root, ignore_errors=True)

print('\n%s\n' % ('FAILED: ' + '; '.join(_fails) if _fails else 'all checks passed'))
sys.exit(1 if _fails else 0)
