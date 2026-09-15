"""test_exif_graph.py — ComfyUI metadata in a WebP or JPEG, which the reader never looked for.

Run: python test_exif_graph.py

THE BUG THIS EXISTS TO STOP COMING BACK. The reader only ever opened PNG text chunks. ComfyUI writes
the same graph into a JPEG or WebP's EXIF instead — `prompt:{…}` in Make (271), `workflow:{…}` in
Model (272) — so **every WebP ComfyUI has ever saved read as "no metadata"**, however complete it
was. That includes every animated WebP a video workflow produces, which for an LTX or AnimateDiff
setup is the entire output.

The assumption was written down and believed: `index_db.py` said "ComfyUI metadata lives in PNG;
others index as no meta". It had never been checked against a file.

HOW IT WAS FOUND, because the method is the transferable part. The plan predicted a different cause
entirely — a second graph inside the PNG. Four of the author's five "no meta" samples turned out to be
unrecoverable (two carried nothing at all, one had been flattened by GIMP, one was written with
placeholder text), and the fifth was this WebP with its whole graph in EXIF. **Reading the real
files first changed what got built.** Guessing would have shipped a fix for a case that wasn't his.

The sample is committed as `samples/Comfy_070255__00001_.webp` — a 97-frame LTX render — and this
test reads it rather than a synthesised stand-in, because a hand-built EXIF block would be testing
this test's idea of the format instead of ComfyUI's.

Also pinned here: **placeholder text is not a prompt.** One sample carries a full A1111 block whose
prompt is the word "unknown". Showing that as the prompt is worse than showing nothing — a blank
says "not recorded", the word looks like a description of the picture.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import comfy_meta as cm   # noqa: E402

SAMPLES = os.path.join(HERE, 'samples')
WEBP = os.path.join(SAMPLES, 'Comfy_070255__00001_.webp')
PLACEHOLDER = os.path.join(SAMPLES, 'nervousness_1.png')

failures = []
skipped = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


print('\nComfyUI metadata in EXIF: WebP and JPEG\n')

if not os.path.exists(WEBP):
    # A missing sample is a skip, not a failure: the file is committed, but a release payload may
    # legitimately drop samples/ and the suite must not read as broken when it does.
    print('  skip  %s is not present' % os.path.basename(WEBP))
    skipped.append(WEBP)
else:
    graph = cm.read_exif_comfy(WEBP)
    check('the API graph is found in EXIF', bool(graph) and graph.lstrip().startswith('{'),
          repr((graph or '')[:60]))

    r = cm.extract(WEBP)
    check('the model is read', (r['model_name'] or '') == 'ltx-video-2b-v0.9.1', r['model_name'])
    check('the prompt is read', (r['positive'] or '').startswith('best quality, detailed illustration'),
          (r['positive'] or '')[:60])
    check('the seed is read', r['seed'] == 887287182036038, r['seed'])
    check('the settings are read', (r['steps'], r['cfg'], r['sampler_name']) == (30, 3.0, 'euler'),
          (r['steps'], r['cfg'], r['sampler_name']))
    # The graph came from a SamplerCustom chain, which the sampler shape test has to accept for any
    # of the above to work — an easy thing to break from the other end.
    check('  via the sampler trace, not a fallback', r['method'] == 'sampler-trace', r['method'])

# A PNG must not pay for this: no EXIF read at all, and no behaviour change.
for name in ('_1 MAIN__04.png', 'v2InstaSelfie_11-58-59_3 REFINE __00024_.png'):
    p = os.path.join(SAMPLES, name)
    if os.path.exists(p):
        check('a PNG with nothing in it still reads as nothing: ' + name[:22],
              cm.read_exif_comfy(p) is None)

# --- placeholder text is not a prompt -------------------------------------------------------------
if os.path.exists(PLACEHOLDER):
    r = cm.extract(PLACEHOLDER)
    check('"unknown" is not shown as the prompt', r['positive'] is None, repr(r['positive']))
    check('  nor as the negative', r['negative'] is None, repr(r['negative']))
    check('  but the real settings beside it survive', r['steps'] == 20, r['steps'])

for word in ('unknown', 'UNKNOWN', ' none ', 'N/A', 'null', '-'):
    check('placeholder recognised: %r' % word, cm._is_placeholder(word) is True)
for word in ('unknown soldier', 'a none too subtle portrait', 'nan', ''):
    check('real text is not a placeholder: %r' % word[:24], cm._is_placeholder(word) is False)

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)
