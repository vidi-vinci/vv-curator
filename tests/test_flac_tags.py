"""test_flac_tags.py — a FLAC's workflow lives in a Vorbis comment, not in ID3.

Run:  python test_flac_tags.py

WHAT THIS PINS. A ComfyUI audio save writes the same two blobs whatever the format; only the
envelope changes. The reader knew ID3 (MP3) and nothing else, so every FLAC indexed with no
prompt, no model, no settings — a blank Details pane — and the symptom the author actually
reported was one step further away: the detail view's drag-to-ComfyUI handle showed a BROKEN
IMAGE, because that handle is a picture built out of the song's own graph and there was no graph
to build from.

FLACS ARE BUILT HERE, NOT LOADED. samples/ is gitignored and holds one real 13MB track, so a test
that depended on it would skip everywhere except one machine — which is the same as not existing.
A FLAC's metadata blocks sit at the front of the file in a format simple enough to write, and the
reader never decodes audio, so a header with no audio behind it exercises the real path.

THE TWO ENDIANNESSES ARE THE WHOLE TRAP and the reason the round-trip is worth pinning: the block
header is big-endian (FLAC's own format) and the comment payload inside it is little-endian
(Vorbis's). Reading either with the other's rule yields lengths that walk off the end of the
block, and the failure is silent — you get {} rather than an error.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import comfy_meta                                             # noqa: E402

failures = 0


def check(name, got, want):
    global failures
    if got == want:
        print('  ok    ' + name)
    else:
        print('  FAIL  ' + name + '\n          got  %r\n          want %r' % (got, want))
        failures += 1


def block(btype, payload, last=False):
    """One FLAC metadata block: a big-endian 24-bit size behind a type byte."""
    return bytes([(0x80 if last else 0) | btype]) + len(payload).to_bytes(3, 'big') + payload


def comments(pairs, vendor=b'test'):
    """A VORBIS_COMMENT payload: little-endian lengths throughout."""
    out = struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', len(pairs))
    for k, v in pairs:
        one = k.encode('utf-8') + b'=' + v.encode('utf-8')
        out += struct.pack('<I', len(one)) + one
    return out


def picture(data, ptype=3, mime=b'image/png'):
    """A PICTURE block payload: big-endian throughout, unlike the comments beside it."""
    desc = b''
    return (struct.pack('>I', ptype)
            + struct.pack('>I', len(mime)) + mime
            + struct.pack('>I', len(desc)) + desc
            + struct.pack('>IIII', 1, 1, 8, 0)
            + struct.pack('>I', len(data)) + data)


def write_flac(tmp, blocks):
    path = os.path.join(tmp, 'x.flac')
    with open(path, 'wb') as f:
        f.write(b'fLaC' + b''.join(blocks))
    return path


print('\nA FLAC carries its graph in a Vorbis comment\n')

import tempfile                                               # noqa: E402
with tempfile.TemporaryDirectory() as tmp:
    STREAMINFO = block(0, b'\x00' * 34)

    # ---- the ordinary case: the two blobs ComfyUI writes ------------------------------------
    p = write_flac(tmp, [STREAMINFO,
                         block(4, comments([('prompt', '{"1":{}}'), ('workflow', '{"nodes":[]}')]),
                               last=True)])
    got = comfy_meta.read_vorbis_comments(p)
    check('prompt and workflow are read', (got.get('prompt'), got.get('workflow')),
          ('{"1":{}}', '{"nodes":[]}'))
    check('the dispatcher picks the FLAC reader for a .flac',
          comfy_meta.read_audio_tags(p).get('workflow'), '{"nodes":[]}')

    # A writer may use any case; the spec says the field name is case-insensitive. ffmpeg wrote
    # the author's file lowercase, so uppercase is the one that would go unnoticed.
    p = write_flac(tmp, [STREAMINFO,
                         block(4, comments([('WORKFLOW', 'W'), ('Prompt', 'P')]), last=True)])
    got = comfy_meta.read_vorbis_comments(p)
    check('keys are matched whatever their case', (got.get('workflow'), got.get('prompt')), ('W', 'P'))

    # ---- cover art, both places a writer may put it -------------------------------------------
    art = b'\x89PNG\r\n\x1a\n' + b'pretend'
    p = write_flac(tmp, [STREAMINFO, block(6, picture(art)),
                         block(4, comments([('workflow', 'W')]), last=True)])
    check('a PICTURE block is found', comfy_meta.read_flac_cover(p), art)
    check('the dispatcher picks it up too', comfy_meta.read_audio_cover(p), art)

    import base64
    b64 = base64.b64encode(picture(art)).decode('ascii')
    p = write_flac(tmp, [STREAMINFO,
                         block(4, comments([('METADATA_BLOCK_PICTURE', b64)]), last=True)])
    check('art base64d into a comment is found too', comfy_meta.read_flac_cover(p), art)

    # The author's own track has neither, which is why it draws a waveform. That has to read as
    # "no art" rather than as an error, or the card would have nothing to fall back to.
    p = write_flac(tmp, [STREAMINFO, block(4, comments([('workflow', 'W')]), last=True)])
    check('no art at all is None, not a raise', comfy_meta.read_flac_cover(p), None)

    # ---- has_cover is recorded during the same pass, so it must agree with the reader ---------
    p = write_flac(tmp, [STREAMINFO, block(6, picture(art)),
                         block(4, comments([('prompt', '{}')]), last=True)])
    check('has_cover is set from a PICTURE block', comfy_meta.extract_audio(p)['has_cover'], 1)
    p = write_flac(tmp, [STREAMINFO, block(4, comments([('prompt', '{}')]), last=True)])
    check('has_cover stays 0 with no art', comfy_meta.extract_audio(p)['has_cover'], 0)

    # ---- THE GUARD, BOTH WAYS. Zero hits is not evidence until the pattern has caught
    #      something it should, so each of these is a file the reader must REFUSE rather than
    #      misread. A silent {} on a real FLAC is exactly the bug being fixed here.
    p = os.path.join(tmp, 'notflac.flac')
    open(p, 'wb').write(b'ID3\x04\x00\x00' + b'\x00' * 60)
    check('a file that is not a FLAC yields nothing', comfy_meta.read_vorbis_comments(p), {})

    p = os.path.join(tmp, 'cut.flac')
    full = b'fLaC' + STREAMINFO + block(4, comments([('workflow', 'W' * 400)]), last=True)
    open(p, 'wb').write(full[:len(full) // 2])                # truncated mid-block
    check('a truncated block yields nothing', comfy_meta.read_vorbis_comments(p), {})

    # 12MB, not something absurd: a FLAC block size is 24 bits, so it CANNOT claim more than
    # 16MB and a test using a bigger number would only prove Python raises on the conversion.
    # 12MB is expressible, over the 8MB ceiling, and is what a corrupt header actually looks like.
    p = os.path.join(tmp, 'huge.flac')
    open(p, 'wb').write(b'fLaC' + b'\x84' + (12 * 1024 * 1024).to_bytes(3, 'big') + b'\x00' * 40)
    check('a header claiming 12MB is refused', comfy_meta.read_vorbis_comments(p), {})

    p = os.path.join(tmp, 'gone.flac')
    check('a missing file yields nothing', comfy_meta.read_vorbis_comments(p), {})

    # An MP3 must still go to the ID3 reader — the dispatcher is the seam and a mix-up here would
    # silently blank every song that already worked.
    p = os.path.join(tmp, 'x.mp3')
    open(p, 'wb').write(b'\x00' * 32)
    check('an .mp3 is not sent to the FLAC reader', comfy_meta.read_audio_tags(p), {})

# ---- the real track, when it is here ----------------------------------------------------------
SAMPLES = os.path.join(ROOT, 'samples')
real = [os.path.join(SAMPLES, f) for f in (os.listdir(SAMPLES) if os.path.isdir(SAMPLES) else [])
        if f.lower().endswith('.flac')]
if not real:
    print('  skip  (no samples/*.flac on this machine)')
else:
    r = comfy_meta.extract_audio(real[0])
    check('a real FLAC answers with its caption, not the cover art\'s prompt',
          r['method'], 'audio-caption')
    for field in ('positive', 'lyrics', 'model_name', 'seed'):
        check('a real FLAC has a %s' % field, bool(r[field]), True)

print()
sys.exit(1 if failures else 0)
